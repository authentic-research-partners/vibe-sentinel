"""The gates: a state recorded, reported every run, cleared only by a pin.

The bug these exist to keep dead: licences, provenance and credentials
used to run as probes, and a probe's finding is a *diff*. ``compare()``
reports a key when it appears and says nothing while it stays, so a
``.env`` already committed on the first scan went into the baseline —
the first run makes the baseline unconditionally — and was never
mentioned again. Every assertion here is about the difference between
"what moved" and "what is true".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibe_sentinel.db import gate_store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.schemas import GateFinding, GateReport, GateState


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


def _finding(key: str, failing: bool = True, **kw: object) -> GateFinding:
    return GateFinding(
        gate=kw.pop("gate", "credentials"),  # type: ignore[arg-type]
        key=key,
        kind="aws-key",
        subject=key.split(":", 1)[-1],
        label=f"{key} — an access key",
        failing=failing,
        **kw,  # type: ignore[arg-type]
    )


def _state(
    *findings: GateFinding, gate: str = "credentials", **kw: object
) -> GateState:
    return GateState(
        reports=(
            GateReport(gate=gate, findings=findings, **kw),  # type: ignore[arg-type]
        )
    )


# --- the record ------------------------------------------------------------


def test_a_gate_run_and_its_findings_round_trip(project: Path) -> None:
    state = _state(_finding("aws-key:src/config.py", risk="tracked"))
    with get_db(project) as conn:
        gate_store.save_gate_state(conn, state, root=str(project), run_id=None)
        back = gate_store.state_at(conn, run_id=1)
        # run_id was None, so it belongs to no scan.
        assert back.reports == ()
        gate_run_id = gate_store.latest_gate_run(conn, "credentials")
        assert gate_run_id is not None
        found = gate_store.findings_at(conn, gate_run_id)
    assert [f.key for f in found] == ["aws-key:src/config.py"]
    assert found[0].risk == "tracked"
    assert found[0].failing


def test_a_scans_gates_are_reachable_from_its_run(project: Path) -> None:
    """One query answers 'what was true of this tree at run N'."""
    from vibe_sentinel.db import store
    from vibe_sentinel.schemas import DriftReport, ProbeResult, Snapshot

    snapshot = Snapshot(
        root="src",
        probes={"p": ProbeResult(probe_id="p", command=["true"], ok=True)},
    )
    with get_db(project) as conn:
        run_id = store.save_run(
            conn, snapshot, DriftReport(), baseline_run_id=None, make_baseline=True
        )
        gate_store.save_gate_state(
            conn,
            _state(_finding("aws-key:src/config.py")),
            root=str(project),
            run_id=run_id,
        )
        state = gate_store.state_at(conn, run_id)
    assert [r.gate for r in state.reports] == ["credentials"]
    assert len(state.failing) == 1


def test_a_standing_finding_is_recorded_on_every_run(project: Path) -> None:
    """The regression this whole change exists for.

    The same key, unchanged, on three runs. As a probe observation this
    produced exactly one `appeared` change and then silence forever. As a
    gate finding it is three rows, because it was true three times.
    """
    state = _state(_finding("aws-key:src/config.py"))
    with get_db(project) as conn:
        for _ in range(3):
            gate_store.save_gate_state(conn, state, root=str(project))
        seen = gate_store.first_seen(conn, "credentials", "aws-key:src/config.py")
    assert seen is not None
    _first_at, count = seen
    assert count == 3


def test_first_seen_dates_a_finding_that_never_changes(project: Path) -> None:
    """'When did this start' — the question a diff could not answer."""
    with get_db(project) as conn:
        gate_store.save_gate_state(conn, _state(), root=str(project))
        assert gate_store.first_seen(conn, "credentials", "aws-key:x") is None
        gate_store.save_gate_state(
            conn, _state(_finding("aws-key:x")), root=str(project)
        )
        seen = gate_store.first_seen(conn, "credentials", "aws-key:x")
    assert seen is not None and seen[1] == 1


def test_a_pinned_finding_is_kept_and_does_not_fail(project: Path) -> None:
    """A pin is a decision somebody made on a date, so the run where a
    finding stopped failing is worth as much as the run where it started."""
    pinned = _finding("aws-key:tests/fixtures.py", failing=False, pinned=True)
    with get_db(project) as conn:
        gate_store.save_gate_state(conn, _state(pinned), root=str(project))
        gate_run_id = gate_store.latest_gate_run(conn, "credentials")
        assert gate_run_id is not None
        back = gate_store.findings_at(conn, gate_run_id)
    assert back[0].pinned and not back[0].failing


def test_a_gate_run_without_its_findings_is_never_written(project: Path) -> None:
    """Atomic, for the reason save_run is: a gate_runs row on its own
    reads as a gate that completed and found nothing."""
    broken = GateState(
        reports=(
            GateReport(
                gate="credentials",
                findings=(_finding("aws-key:src/config.py"),),
            ),
        )
    )
    with get_db(project) as conn:
        conn.execute("DROP TABLE gate_findings")
        with pytest.raises(Exception):
            gate_store.save_gate_state(conn, broken, root=str(project))
        remaining = conn.execute("SELECT COUNT(*) FROM gate_runs").fetchone()[0]
    assert remaining == 0


# --- the three states, kept apart ------------------------------------------


def test_an_unconfigured_gate_is_neither_clean_nor_broken() -> None:
    state = GateState(
        reports=(
            GateReport(gate="licenses", ok=False, configured=False, error="no policy"),
        )
    )
    assert state.unconfigured
    assert not state.broken, "a gate nobody configured has not failed"
    assert not state.failing, "and it has not found anything either"


def test_a_broken_gate_is_broken_and_not_unconfigured() -> None:
    state = GateState(
        reports=(GateReport(gate="credentials", ok=False, error="walk truncated"),)
    )
    assert state.broken
    assert not state.unconfigured


def test_a_broken_gate_never_renders_as_clean(capsys: pytest.CaptureFixture) -> None:
    from vibe_sentinel.report import render_gate_state

    render_gate_state(
        GateState(
            reports=(GateReport(gate="credentials", ok=False, error="walk truncated"),)
        )
    )
    out = capsys.readouterr().out
    assert "DID NOT COMPLETE" in out
    assert "clean" not in out


def test_an_unconfigured_gate_names_the_block_to_add(
    capsys: pytest.CaptureFixture,
) -> None:
    from vibe_sentinel.report import render_gate_state

    render_gate_state(
        GateState(
            reports=(
                GateReport(
                    gate="licenses",
                    ok=False,
                    configured=False,
                    error="Add a [licenses] table to .vibe-sentinel.toml",
                ),
            )
        )
    )
    out = capsys.readouterr().out
    assert "no policy declared" in out
    assert "[licenses]" in out
    assert "clean" not in out


# --- what the renderers may claim ------------------------------------------


def test_the_report_says_a_finding_is_not_drift(
    capsys: pytest.CaptureFixture,
) -> None:
    from vibe_sentinel.report import render_gate_state

    render_gate_state(_state(_finding("aws-key:src/config.py", risk="tracked")))
    out = capsys.readouterr().out
    assert "states, not drift" in out
    assert "pin records the decision" in out


def test_an_unadjudicated_finding_says_so(capsys: pytest.CaptureFixture) -> None:
    """Same rule as DriftReport.analyzed: never claim a review that did
    not happen."""
    from vibe_sentinel.report import render_gate_state

    render_gate_state(_state(_finding("aws-key:src/config.py")))
    assert "[not adjudicated]" in capsys.readouterr().out


def test_the_agent_block_keeps_drift_and_findings_apart() -> None:
    """An agent told 'nothing drifted' while a key sits in the tree has
    been told the truth about the wrong question."""
    from vibe_sentinel.report import render_agent
    from vibe_sentinel.schemas import DriftReport

    text = render_agent(
        DriftReport(baseline_at="2026-01-01"),
        _state(_finding("aws-key:src/config.py")),
    )
    assert "NO STRUCTURAL DRIFT" in text
    assert "FINDINGS THAT STAND" in text
    assert "re-baselining does not clear them" in text


def test_the_agent_block_is_empty_when_nothing_stands() -> None:
    from vibe_sentinel.report import render_agent
    from vibe_sentinel.schemas import DriftReport

    text = render_agent(DriftReport(baseline_at="2026-01-01"), GateState())
    assert "FINDINGS THAT STAND" not in text


def test_json_always_carries_a_gates_key(capsys: pytest.CaptureFixture) -> None:
    """A consumer must not have to infer 'not checked' from a missing field."""
    import json

    from vibe_sentinel.report import render_json
    from vibe_sentinel.schemas import DriftReport, Snapshot

    render_json(Snapshot(root="."), DriftReport())
    payload = json.loads(capsys.readouterr().out)
    assert payload["gates"] == {"reports": []}


# --- the boundary that is stricter here ------------------------------------


def test_no_credential_value_reaches_the_database(project: Path) -> None:
    """`detail` carries the redacted excerpt and nothing else. The history
    is the one artifact that cannot be rebuilt, and a live key copied into
    it would turn the record of a leak into a second copy of it."""
    from vibe_sentinel import credentials as creds

    secret = "AKIAIOSFODNN7EXAMPLE"
    (project / "config.py").write_text(f'AWS_ACCESS_KEY_ID = "{secret}"\n')
    secrets = creds.load_secrets(project)
    policy = creds.load_policy(None, root=project)
    scan = creds.search(project, secrets, policy)
    assert scan.candidates, "the fixture should trip a rule"

    from vibe_sentinel.config import SentinelConfig

    found = asyncio.run(creds.adjudicate(scan, secrets, policy, SentinelConfig()))
    from vibe_sentinel.gates import shape_credentials

    report = shape_credentials(scan, found, policy)
    with get_db(project) as conn:
        gate_store.save_gate_state(
            conn, GateState(reports=(report,)), root=str(project)
        )
        rows = conn.execute("SELECT * FROM gate_findings").fetchall()
    written = " ".join(str(v) for row in rows for v in tuple(row))
    assert secret not in written


def test_a_gate_that_cannot_load_its_policy_does_not_raise(tmp_path: Path) -> None:
    """One broken policy must not lose the other two gates — the same rule
    that makes a failed probe a record rather than an exception."""
    from vibe_sentinel.gates import collect_all

    (tmp_path / ".vibe-sentinel.toml").write_text(
        "[licenses]\nallowed_categories = 3\n"
    )
    state = asyncio.run(collect_all(tmp_path, None))
    assert [r.gate for r in state.reports] == ["licenses", "packages", "credentials"]
    assert any(not r.ok for r in state.reports)


def test_node_only_project_reports_unavailable_dependency_gates(tmp_path: Path) -> None:
    import asyncio

    from vibe_sentinel.gates import collect_all
    from vibe_sentinel.schemas import NODE_GATE_UNAVAILABLE

    (tmp_path / "package.json").write_text("{}")
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[licenses]\nallowed_categories = ["permissive"]\n'
    )
    state = asyncio.run(collect_all(tmp_path))
    assert [(gate.gate, gate.error) for gate in state.unconfigured] == [
        ("licenses", NODE_GATE_UNAVAILABLE),
        ("packages", NODE_GATE_UNAVAILABLE),
    ]
    assert state.reports[-1].gate == "credentials"
    assert state.reports[-1].ok


def test_a_gate_that_could_not_ask_does_not_complete(tmp_path: Path) -> None:
    """Collection is mechanical for all three gates; the credentials
    verdict is not, and there is no shorter answer it can give. A gate
    that reported its candidates unjudged would read as a survey of the
    tree when it is a survey of what nobody looked at."""
    from vibe_sentinel.gates import collect_credentials

    (tmp_path / "config.py").write_text('KEY = "AKIAIOSFODNN7EXAMPLE"\n')
    report = asyncio.run(collect_credentials(tmp_path, None))
    assert not report.ok, "a gate that could not adjudicate has not completed"
    assert "no model is configured" in report.error
    assert "backend status" in report.error, "an error carries its remediation"


# --- end to end: the bug, in the command a person actually runs ------------


def _tiny_project(root: Path, secret: str) -> None:
    """One trivial probe, and a committed key sitting beside it."""
    (root / ".vibe-sentinel.toml").write_text(
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "tiny"\ntitle = "t"\n'
        'command = ["python", "-c", "import json; print(json.dumps('
        "{'observations': [{'key': 'a', 'value': 1.0, 'label': 'a'}], "
        "'summary': 'ok'}))\"]\n",
        encoding="utf-8",
    )
    (root / "config.py").write_text(
        f'AWS_ACCESS_KEY_ID = "{secret}"\n', encoding="utf-8"
    )


def test_a_key_present_from_the_first_scan_is_reported_on_every_scan(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The whole reason these are gates.

    As probes: run 1 established the baseline and reported nothing, run 2
    found the same observation unchanged and so produced no change, and
    the key was never mentioned again. As gates the answer is the same
    both times, because the tree is the same both times.
    """
    from vibe_sentinel.__main__ import main

    _tiny_project(tmp_path, "AKIAIOSFODNN7EXAMPLE")

    assert main(["scan", str(tmp_path)]) == 1
    first = capfd.readouterr().out
    assert "config.py" in first

    # Nothing changed. A diff would have nothing to say; this must.
    assert main(["scan", str(tmp_path)]) == 1
    second = capfd.readouterr().out
    assert "No change since" in second, "the drift half should be quiet"
    assert "config.py" in second, "the state half must not be"
    assert "states, not drift" in second


def test_accepting_drift_does_not_accept_a_credential(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """`--update` moves the baseline. It settles drift and nothing else —
    a pin is the only thing that settles one of these."""
    from vibe_sentinel.__main__ import main

    _tiny_project(tmp_path, "AKIAIOSFODNN7EXAMPLE")
    main(["scan", str(tmp_path)])
    capfd.readouterr()

    assert main(["scan", str(tmp_path), "--update"]) == 1
    assert "config.py" in capfd.readouterr().out


def test_the_scan_records_its_gates_against_its_run(tmp_path: Path) -> None:
    from vibe_sentinel.__main__ import main

    _tiny_project(tmp_path, "AKIAIOSFODNN7EXAMPLE")
    main(["scan", str(tmp_path)])
    with get_db(tmp_path) as conn:
        state = gate_store.state_at(conn, run_id=1)
    assert {r.gate for r in state.reports} == {"licenses", "packages", "credentials"}
    assert any(f.subject == "config.py" for f in state.failing)


def test_a_missing_licence_policy_does_not_fail_the_scan(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A gate people turn off protects nothing, and a scan that exits
    non-zero because nobody wrote a [licenses] block is one they turn
    off. It still may not read as passing."""
    from vibe_sentinel.__main__ import main

    (tmp_path / ".vibe-sentinel.toml").write_text(
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "tiny"\ntitle = "t"\n'
        'command = ["python", "-c", "import json; print(json.dumps('
        "{'observations': [{'key': 'a', 'value': 1.0, 'label': 'a'}], "
        "'summary': 'ok'}))\"]\n",
        encoding="utf-8",
    )
    assert main(["scan", str(tmp_path)]) == 0
    out = capfd.readouterr().out
    assert "no policy declared" in out
    assert "licenses: clean" not in out


def test_the_scan_can_adjudicate_credentials_with_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gates run *outside* the scan's event loop, and must.

    ``credentials.adjudicate`` used to open a loop of its own to fan the
    candidates out, so calling the gate stage from inside ``asyncio.run``
    raised "asyncio.run() cannot be called from a running event loop" —
    and it raised in the one gate whose answer costs a model call, which
    is the one that would then report DID NOT COMPLETE. One
    ``asyncio.run`` per command, in the CLI and nowhere else, is the rule
    that replaced it.
    """
    from vibe_sentinel.__main__ import main

    async def answers(*_a: object, **_k: object) -> dict[str, str]:
        return {"verdict": "real", "reason": "shaped like a live key"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", answers)
    _tiny_project(tmp_path, "AKIAIOSFODNN7EXAMPLE")

    assert main(["scan", str(tmp_path)]) == 1
    with get_db(tmp_path) as conn:
        state = gate_store.state_at(conn, run_id=1)
    creds = next(r for r in state.reports if r.gate == "credentials")
    assert creds.ok, f"the credentials gate did not complete: {creds.error}"
    # Without this the test passes vacuously: a run that never reached
    # the model never reached the loop that used to raise either.
    assert creds.adjudicated
    assert any(f.adjudicated and f.verdict == "real" for f in creds.findings)


# --- the shape the three gates share -----------------------------------------


def test_the_three_gates_name_their_stages_the_same_way() -> None:
    """gather / audit / adjudicate, and the same word wherever the stage is.

    They were `licenses.check`, `packages.audit` and `credentials.collect`
    for one role, with `Violation` / `Finding` / `Candidate` for one thing
    and a `credentials.Findings` container one letter from
    `packages.Finding`. Nothing was broken by it and nothing failed — three
    modules a reader could not carry one mental model across is not a bug,
    which is exactly why it lasted and why it is asserted here rather than
    described in a docstring nobody diffs.

    The empty cells are not asserted. The licence gate has nothing to
    gather because package metadata already is the inventory, and nothing
    to adjudicate because an SPDX expression against an allow-list is not
    an opinion; credentials has no separate audit because its pattern match
    is one. Those are consequences of the questions, and a test freezing
    them would be a test against ever asking a different one.
    """
    from vibe_sentinel import credentials, licenses, packages

    stages = {
        "gather": [(packages, "take_inventory"), (credentials, "search")],
        "audit": [(licenses, "audit"), (packages, "audit")],
        "adjudicate": [(packages, "adjudicate"), (credentials, "adjudicate")],
    }
    for stage, members in stages.items():
        for module, name in members:
            found = getattr(module, name, None)
            assert callable(found), (
                f"{module.__name__}.{name} is the {stage} stage and is gone. "
                f"If the stage moved, move the name with it — the table in "
                f"gates.py is what a reader holds all three in their head by."
            )


def test_the_item_a_gate_reports_is_called_a_finding() -> None:
    """One noun for one role, across the gates and the shape they share.

    `credentials` keeps `Candidate` as well, and that is the distinction
    working rather than an exception to it: a candidate is what the pattern
    match nominated, before anything judged it.
    """
    from vibe_sentinel import credentials, licenses, packages
    from vibe_sentinel.schemas import GateFinding

    assert licenses.Finding is not None
    assert packages.Finding is not None
    assert credentials.Candidate is not None
    assert not hasattr(credentials, "Findings"), (
        "`Findings` sat one letter from `packages.Finding` and held a "
        "different kind of thing. It is `Adjudication` now, which is what "
        "`packages` already called the same stage's result."
    )
    assert credentials.Adjudication is not None
    assert packages.Adjudication is not None
    assert GateFinding is not None


def test_a_broken_manifest_fails_the_packages_gate(tmp_path: Path) -> None:
    """Unreadable is not undeclared. It used to come back as "nothing in this
    tree declares its dependencies" — unconfigured, which a scan does not
    count against anyone — when what was true is that the gate could not
    read what the tree declares."""
    from vibe_sentinel.gates import collect_packages

    (tmp_path / "pyproject.toml").write_text(
        "authors = authors = [\n", encoding="utf-8"
    )
    report = asyncio.run(collect_packages(tmp_path))

    assert not report.ok
    assert report.configured, "a broken manifest is a failure, not an absence"
    assert "pyproject.toml could not be read" in report.error
