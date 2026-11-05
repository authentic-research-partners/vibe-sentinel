"""The scan envelope records real work and never accepts a failed judgement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe_sentinel import __main__ as main_mod
from vibe_sentinel.__main__ import main
from vibe_sentinel.db import get_db, store
from vibe_sentinel.exceptions import LLMConnectionError


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_setup_logging", lambda config: None)


def configure(root: Path, value: int = 1, *, fail: bool = False) -> None:
    command = (
        "raise RuntimeError('private-diagnostic')"
        if fail
        else (
            "import json; print(json.dumps(dict(observations=[dict(key='private-key', "
            f"value={value})])))"
        )
    )
    (root / ".vibe-sentinel.toml").write_text(
        '[probes]\nuse_builtins=false\n[[probe]]\nid="tiny"\ntitle="Tiny"\n'
        f'command=["python", "-c", {json.dumps(command)}]\n'
    )


def scan(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = main(["scan", "--format=json", *args])
    return code, json.loads(capsys.readouterr().out)


def test_final_result_is_stored_and_first_run_is_not_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    code, data = scan(capsys)
    assert code == 0
    result = data["result"]
    assert result["comparison"] == "no_baseline"
    assert result["baseline_run_id"] is None
    assert set(data) == {"result", "snapshot", "drift", "gates"}
    configure(tmp_path, 2)
    with get_db(tmp_path) as conn:
        recorded = store.load_result(conn, result["run_id"])
        assert recorded is not None
        assert recorded.model_dump(mode="json") == result
        assert store.baseline_run(conn).id == result["run_id"]


@pytest.mark.parametrize("failure", ["connection", "invalid-json", "unanswered"])
def test_failed_judgement_keeps_measurement_and_prior_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    configure(tmp_path)
    _, first = scan(capsys)
    configure(tmp_path, 9)

    async def fail(*args, **kwargs):
        if failure == "connection":
            raise LLMConnectionError("private-judge-diagnostic")
        if failure == "invalid-json":
            raise json.JSONDecodeError("private-judge-diagnostic", "secret", 0)
        return None

    monkeypatch.setattr("vibe_sentinel.analyze.llm_query", fail)
    code, data = scan(capsys, "--update")
    assert code == 2
    assert "judgement" in data["result"]["failed_stages"]
    assert data["snapshot"]["probes"]["tiny"]["observations"][0]["value"] == 9
    assert "private-judge-diagnostic" not in json.dumps(data)
    if failure != "connection":
        assert data["drift"]["lenses"][0]["asked"]
        assert not data["drift"]["lenses"][0]["answered"]
    with get_db(tmp_path) as conn:
        assert store.baseline_run(conn).id == first["result"]["run_id"]
        assert store.load_result(conn, data["result"]["run_id"]).exit_code == 2


def test_failed_first_measurement_cannot_become_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path, fail=True)
    code, data = scan(capsys, "--update", "--shareable")
    assert code == 2
    assert set(data) == {"result"}
    assert data["result"]["failed_stages"] == ["measurement"]
    assert "private-diagnostic" not in json.dumps(data)
    with get_db(tmp_path) as conn:
        assert store.baseline_run(conn) is None


def test_configured_json_failure_remains_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[output]\nformat="json"\n[probes]\nuse_builtins=false\n'
    )
    assert main(["scan"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["result"]["failed_stages"] == ["configuration"]
    assert data["result"]["run_id"] is None


def test_shareable_has_no_observation_keys_or_local_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    code, data = scan(capsys, "--shareable")
    assert code == 0
    encoded = json.dumps(data)
    assert set(data) == {"result"}
    assert data["result"]["profile"] == "shareable"
    assert "private-key" not in encoded
    assert str(tmp_path) not in encoded


def test_judge_and_gate_failures_both_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    scan(capsys)
    configure(tmp_path, 9)

    async def fail(*args, **kwargs):
        raise LLMConnectionError("private-failure")

    monkeypatch.setattr("vibe_sentinel.analyze.llm_query", fail)
    monkeypatch.setattr("vibe_sentinel.engine.run_gates", fail)
    code, data = scan(capsys)
    assert code == 2
    assert set(data["result"]["failed_stages"]) == {"judgement", "gates"}
    assert data["result"]["gates_status"] == "error"


def test_new_probe_requires_baseline_coverage_before_complete_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    code, first = scan(capsys)
    assert code == 0
    assert first["result"]["comparison"] == "no_baseline"
    config = tmp_path / ".vibe-sentinel.toml"
    config.write_text(
        config.read_text()
        + (
            '\n[[probe]]\nid="added"\ntitle="Added"\n'
            'command=["python", "-c", "print(\'{\\"observations\\": []}\')"]\n'
        )
    )
    code, added = scan(capsys, "--shareable")
    assert code == 2
    assert added["result"]["comparison"] == "incomplete"
    assert added["result"]["counts"]["baseline_unmeasured"] == 1
    assert added["result"]["baseline_run_id"] == first["result"]["run_id"]
    with get_db(tmp_path) as conn:
        assert store.baseline_run(conn).id == first["result"]["run_id"]

    code, accepted = scan(capsys, "--update")
    assert code == 2  # This run still compared with the incomplete old baseline.
    assert accepted["result"]["baseline_run_id"] == first["result"]["run_id"]
    with get_db(tmp_path) as conn:
        assert store.baseline_run(conn).id == accepted["result"]["run_id"]

    code, after = scan(capsys)
    assert code == 0
    assert after["result"]["comparison"] == "compared"
    assert after["result"]["counts"]["baseline_unmeasured"] == 0
    assert after["result"]["baseline_run_id"] == accepted["result"]["run_id"]
