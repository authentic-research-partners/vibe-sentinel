"""The shipped example reaches real readers, comparison, history and exports.

No test starts an application server or imports the example's Python modules.
The ordinary suite's judge stub supplies judgement; the user example has no
model-free setting. Node and the separately installed parser remain optional
for Python-only contributors, and required for this acceptance suite to run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel.__main__ import main
from vibe_sentinel.db import store
from vibe_sentinel.db.connection import get_db
from vibe_sentinel.results import parse_result, project_result
from vibe_sentinel.templates import load_probe_catalog

REPOSITORY = Path(__file__).parents[1]
EXAMPLE = REPOSITORY / "docs/examples/fullstack"
PACK = REPOSITORY / "packs/javascript"
JS = "pack:javascript:imports"
PYTHON = "python-dependency-edges"
OPENAPI = "openapi-artifact"
SOURCES = f"pack:javascript:workspaces,{JS},{PYTHON}"


@pytest.fixture
def example(tmp_path: Path) -> Path:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Full-stack acceptance requires optional Node >=22")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout
    if int(version.lstrip("v").split(".", 1)[0]) < 22:
        pytest.skip("Full-stack acceptance requires optional Node >=22")
    if not (PACK / "node_modules/typescript/package.json").is_file():
        pytest.skip("Install declared JavaScript pack dependencies before acceptance")
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True)
    config = tmp_path / ".vibe-sentinel.toml"
    # The example documents local-tarball installation. The separate artifact
    # smoke verifies installation; these tests exercise this checkout's reader.
    config.write_text(
        config.read_text().replace(
            '"node_modules/@vibe-sentinel/javascript/pack.toml"',
            json.dumps(str(PACK / "pack.toml")),
        )
    )
    return tmp_path


def scan(
    root: Path, capture: pytest.CaptureFixture[str], *arguments: str
) -> tuple[int, dict[str, Any]]:
    code = main(["scan", str(root), "--format=json", *arguments])
    payload = json.loads(capture.readouterr().out)
    result = parse_result(payload["result"])
    assert code == result.exit_code
    assert result.run_id is not None
    with get_db(root) as connection:
        recorded = store.load_result(connection, result.run_id)
    assert recorded is not None
    if "--shareable" in arguments:
        assert payload == project_result(recorded, profile="shareable")
    else:
        assert payload["result"] == recorded.model_dump(mode="json")
    return code, payload


def observations(payload: dict[str, Any], probe_id: str) -> list[dict[str, Any]]:
    return payload["snapshot"]["probes"][probe_id]["observations"]


def test_example_config_declares_scoped_readers(example: Path) -> None:
    catalog = load_probe_catalog(project_root=example)
    probes = {probe.id: probe for probe in catalog.probes}
    assert set(probes) == {"pack:javascript:workspaces", JS, PYTHON, OPENAPI}
    assert probes[PYTHON].compare_parameters
    assert probes[OPENAPI].compare_parameters
    assert probes[PYTHON].defaults()["SOURCE_ROOT"] == "services/python"
    assert probes[OPENAPI].defaults()["ARTIFACT"] == "openapi.json"
    assert catalog.packs[0].identity.id == "javascript"


def test_full_example_artifact_baseline_repeat_and_export(
    example: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_code, first = scan(example, capsys)
    assert first_code == 0 and first["result"]["comparison"] == "no_baseline"
    assert first["result"]["counts"]["probes"] == 4
    assert all(item["status"] == "complete" for item in first["result"]["probes"])
    repeat_code, repeat = scan(example, capsys)
    assert repeat_code == 0
    assert repeat["result"]["outcome"] == "complete"
    assert repeat["result"]["comparison"] == "compared"
    assert repeat["result"]["baseline_run_id"] == first["result"]["run_id"]
    assert repeat["drift"]["changes"] == []
    gates = {item["id"]: item["status"] for item in repeat["result"]["gates"]}
    assert gates["licenses"] == gates["packages"] == "unconfigured"
    assert gates["credentials"] == "complete"
    code, shared = scan(example, capsys, "--shareable")
    assert code == 0 and set(shared) == {"result"}
    assert shared["result"]["profile"] == "shareable"
    assert str(example) not in json.dumps(shared)
    assert "apps/web/src" not in json.dumps(shared)
    assert shared["result"]["baseline_run_id"] == first["result"]["run_id"]


@pytest.mark.parametrize(
    ("path", "before", "after", "probe_id"),
    [
        ("apps/web/src/view.js", '"./format.js"', '"./format_alt.js"', JS),
        ("apps/api/src/server.js", '"./response.js"', '"./response_alt.js"', JS),
        (
            "services/python/service/server.py",
            "from .messages import",
            "from .messages_alt import",
            PYTHON,
        ),
    ],
    ids=["frontend", "node-api", "python-service"],
)
def test_source_target_rewire_preserves_count_but_records_relationships(
    example: Path,
    capsys: pytest.CaptureFixture[str],
    path: str,
    before: str,
    after: str,
    probe_id: str,
) -> None:
    code, baseline = scan(example, capsys, f"--probes={SOURCES}")
    assert code == 0
    source = example / path
    assert source.read_text().count(before) == 1
    source.write_text(source.read_text().replace(before, after))
    code, changed = scan(example, capsys, f"--probes={SOURCES}")
    assert code == 1 and changed["result"]["outcome"] == "findings"
    assert len(observations(baseline, probe_id)) == len(observations(changed, probe_id))
    differences = [
        item for item in changed["drift"]["changes"] if item["probe_id"] == probe_id
    ]
    assert len(differences) == 2
    assert {item["kind"] for item in differences} == {"appeared", "disappeared"}
    assert all(item["key"].startswith("edge:1:") for item in differences)
    assert all(item["attrs"]["source"] == f"file:{path}" for item in differences)
    assert any(
        item["asked"] and item["answered"] for item in changed["drift"]["lenses"]
    )
    assert changed["result"]["baseline_run_id"] == baseline["result"]["run_id"]


@pytest.mark.parametrize("mutation", ["required", "reference", "security"])
def test_artifact_relationship_changes_reach_scan_history(
    example: Path, capsys: pytest.CaptureFixture[str], mutation: str
) -> None:
    code, baseline = scan(example, capsys)
    assert code == 0
    path = example / "openapi.json"
    document = json.loads(path.read_text())
    if mutation == "required":
        document["components"]["schemas"]["Greeting"]["required"] = ["via"]
    elif mutation == "reference":
        document["paths"]["/api/greeting"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"] = "#/components/schemas/AlternateGreeting"
    else:
        document["paths"]["/api/greeting"]["get"]["security"] = [{"ExampleKey": []}]
    path.write_text(json.dumps(document))
    code, changed = scan(example, capsys)
    assert code == 1
    differences = [
        item for item in changed["drift"]["changes"] if item["probe_id"] == OPENAPI
    ]
    assert differences
    assert not any(item["probe_id"] != OPENAPI for item in changed["drift"]["changes"])
    if mutation in {"required", "reference"}:
        assert len(observations(baseline, OPENAPI)) == len(
            observations(changed, OPENAPI)
        )
    if mutation == "reference":
        edges = [item for item in differences if item["key"].startswith("edge:1:")]
        assert {item["kind"] for item in edges} == {"appeared", "disappeared"}
    else:
        assert any(item["kind"] == "changed" for item in differences)
    assert changed["result"]["baseline_run_id"] == baseline["result"]["run_id"]


@pytest.mark.parametrize(
    "failed", [JS, PYTHON, OPENAPI], ids=["javascript", "python", "artifact"]
)
def test_incomplete_input_cannot_be_clean_or_accepted_as_baseline(
    example: Path, capsys: pytest.CaptureFixture[str], failed: str
) -> None:
    selected = [] if failed == OPENAPI else [f"--probes={SOURCES}"]
    code, first = scan(example, capsys, *selected)
    assert code == 0
    if failed == JS:
        path = example / "apps/web/src/main.js"
        path.write_text(
            path.read_text() + '\nconst target = "./format.js"; import(target);\n'
        )
    elif failed == PYTHON:
        path = example / "services/python/service/server.py"
        path.write_text(path.read_text() + '\n__import__("runtime_selected")\n')
    else:
        (example / "openapi.json").write_text("{")
    code, broken = scan(example, capsys, *selected, "--update")
    assert code == 2 and broken["result"]["outcome"] in {"error", "incomplete"}
    assert "measurement" in broken["result"]["failed_stages"]
    assert failed in broken["drift"]["unmeasured"]
    assert not any(item["probe_id"] == failed for item in broken["drift"]["changes"])
    with get_db(example) as connection:
        baseline = store.baseline_run(connection)
    assert baseline is not None and baseline.id == first["result"]["run_id"]


def test_source_measurement_does_not_execute_application_modules(
    example: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    python = example / "services/python/service/__init__.py"
    python.write_text(
        'raise RuntimeError("application must not execute during scan")\n'
    )
    javascript = example / "apps/api/src/response.js"
    javascript.write_text(
        javascript.read_text()
        + '\nthrow new Error("application must not execute during scan");\n'
    )
    code, result = scan(example, capsys, f"--probes={SOURCES}")
    assert code == 0
    assert all(item["ok"] for item in result["snapshot"]["probes"].values())
