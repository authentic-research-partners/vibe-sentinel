"""Local pack declarations preserve execution, history and export boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from vibe_sentinel import __main__ as main_mod, engine
from vibe_sentinel.__main__ import main
from vibe_sentinel.config import load_config
from vibe_sentinel.db import get_db, store
from vibe_sentinel.results import build_result, parse_result, project_result
from vibe_sentinel.schemas import DriftReport, ScanResultV1
from vibe_sentinel.templates import load_probe_catalog, load_probes


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "_setup_logging", lambda config: None)


def configure(root: Path, *, probe: bool = True, reader: bool = False) -> Path:
    directory = root / "packs"
    directory.mkdir(exist_ok=True)
    manifest = directory / "fixture.toml"
    body = (
        'protocol=1\nid="fixture"\nversion="1.0.0"\n'
        'ecosystems=["fixture"]\nsupported_inputs=["fixture-source"]\n'
        '[[executable]]\nid="python"\n'
        f"command={json.dumps(sys.executable)}\n"
    )
    if probe:
        body += (
            '[[probe]]\nid="size"\ntitle="Source count"\n'
            'command=["python", "measure.py"]\n'
        )
    if reader:
        body += (
            '[[reader]]\nid="inventory"\ntitle="Inventory"\n'
            'ecosystem="fixture"\nsupported_inputs=["fixture-source"]\n'
            'command=["python", "inventory.py"]\n'
        )
    manifest.write_text(body)
    (root / "measure.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "print(json.dumps({'observations':[{'key':'source','value':len(Path('source.txt').read_text())}]}))\n"
    )
    (root / "inventory.py").write_text(
        "from pathlib import Path\nPath('inventory-ran').touch()\n"
    )
    (root / "source.txt").write_text("one")
    (root / ".vibe-sentinel.toml").write_text(
        '[packs]\nmanifests=["packs/fixture.toml"]\n[probes]\nuse_builtins=false\n'
    )
    return manifest


def run_scan(root: Path, *, update: bool = False):
    config = load_config(root / ".vibe-sentinel.toml")
    catalog = load_probe_catalog(project_root=root)
    return asyncio.run(
        engine.scan_and_compare(
            catalog.probes,
            root,
            config,
            update=update,
            lenses=(),
            declared_packs=catalog.packs,
        )
    )


def test_catalog_preserves_fixed_argv_and_selected_provenance(tmp_path: Path) -> None:
    configure(tmp_path, reader=True)
    catalog = load_probe_catalog(project_root=tmp_path)
    assert len(catalog.probes) == 1
    assert catalog.probes[0].id == "pack:fixture:size"
    assert catalog.probes[0].command == [sys.executable, "measure.py"]
    assert catalog.probes[0].source == "pack:fixture@1.0.0"
    assert load_probes(project_root=tmp_path)[0].id == catalog.probes[0].id
    snapshot, report, _ = run_scan(tmp_path)
    result = build_result(snapshot, report)
    assert result.schema_version == 3
    assert result.provenance.packs[0].selected_probes == ("pack:fixture:size",)
    assert result.provenance.packs[0].inventory_status == "declared_not_assessed"
    assert result.provenance.packs[0].history_coverage == "baseline_only"
    assert snapshot.probes["pack:fixture:size"].observations[0].value == 3
    assert not (tmp_path / "inventory-ran").exists()
    shared = project_result(result, profile="shareable")
    assert parse_result(shared["result"]).schema_version == 3
    assert "fixture" not in json.dumps(shared)
    assert str(tmp_path) not in json.dumps(shared)


def test_listing_inventory_only_pack_does_not_execute_or_create_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path, probe=False, reader=True)
    assert main(["scan", "--list-packs"]) == 0
    output = capsys.readouterr().out
    assert "fixture@1.0.0" in output
    assert "declared, not executed or assessed" in output
    assert "inventory" in output
    assert not (tmp_path / "inventory-ran").exists()
    assert not (tmp_path / ".vibe-sentinel/history.db").exists()


def test_inventory_only_attribution_survives_an_unrelated_probe(tmp_path: Path) -> None:
    configure(tmp_path, probe=False, reader=True)
    config_path = tmp_path / ".vibe-sentinel.toml"
    config_path.write_text(
        config_path.read_text()
        + '\n[[probe]]\nid="local"\ntitle="Local"\ncommand=["python", "measure.py"]\n'
    )
    snapshot, report, _ = run_scan(tmp_path)
    pack = build_result(snapshot, report).provenance.packs[0]
    assert pack.selected_probes == ()
    assert pack.inventory_readers == ("inventory",)
    assert pack.inventory_status == "declared_not_assessed"
    assert not (tmp_path / "inventory-ran").exists()


@pytest.mark.parametrize("change", ["version", "declaration"])
def test_changed_pack_semantics_need_explicit_baseline_acceptance(
    tmp_path: Path, change: str
) -> None:
    manifest = configure(tmp_path)
    _, _, first_id = run_scan(tmp_path)
    if change == "version":
        manifest.write_text(
            manifest.read_text().replace('version="1.0.0"', 'version="1.1.0"')
        )
    else:
        manifest.write_text(
            manifest.read_text().replace(
                'title="Source count"', 'title="Revised count"'
            )
        )
    (tmp_path / "source.txt").write_text("different measurement")
    snapshot, report, _ = run_scan(tmp_path)
    result = build_result(snapshot, report)
    assert result.exit_code == 2
    assert result.baseline_run_id == first_id
    assert result.counts.baseline_unmeasured == 1
    assert all(
        change.kind == "appeared" and change.severity == "info"
        for change in report.changes
    )
    assert not any(change.before is not None for change in report.changes)
    _, _, accepted_id = run_scan(tmp_path, update=True)
    snapshot, report, _ = run_scan(tmp_path)
    result = build_result(snapshot, report)
    assert result.exit_code == 0
    assert result.baseline_run_id == accepted_id


def test_pack_series_are_not_fit_or_compared_as_cross_version_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    snapshot, _, _ = run_scan(tmp_path)
    with get_db(tmp_path) as conn:
        horizons = engine.drift_over_horizons(
            conn, snapshot, load_probes(project_root=tmp_path), ["1w"]
        )
        assert "baseline-only" in horizons[0].unavailable
        assert not engine.fit_trends(conn, snapshot, limit_runs=50, min_runs=1)
    assert main(["trend", "source", "--probe", "pack:fixture:size"]) == 2
    assert "fit" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "declaration",
    [
        'unknown="ignored"',
        'command="python measure.py"',
        "timeout_s=-1",
        "timeout_s=nan",
        "tolerance=inf",
    ],
)
def test_pack_probe_adapter_rejects_invalid_declarations(
    tmp_path: Path, declaration: str
) -> None:
    manifest = configure(tmp_path)
    if declaration.startswith("command="):
        manifest.write_text(
            manifest.read_text().replace(
                'command=["python", "measure.py"]', declaration
            )
        )
    else:
        manifest.write_text(manifest.read_text() + declaration + "\n")
    with pytest.raises(ValueError):
        load_probe_catalog(project_root=tmp_path)


def test_project_cannot_impersonate_pack_provenance(tmp_path: Path) -> None:
    configure(tmp_path)
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text(
        path.read_text()
        + '\n[[probe]]\nid="pack:fixture:size"\ntitle="Impersonation"\ncommand=["python", "measure.py"]\n'
    )
    with pytest.raises(ValueError, match="reserved"):
        load_probe_catalog(project_root=tmp_path)


def test_pack_configuration_failure_is_versioned_and_has_no_raw_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(tmp_path)
    (tmp_path / "packs/fixture.toml").unlink()
    assert main(["scan", "--format=json", "--shareable"]) == 2
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["schema_version"] == 3
    assert result["failed_stages"] == ["configuration"]
    assert str(tmp_path) not in json.dumps(result)


def test_v1_history_keeps_exact_bytes_and_meaning(tmp_path: Path) -> None:
    original = (Path(__file__).parent / "fixtures/result-v1.json").read_text()
    payload = json.loads(original)
    result = parse_result(payload)
    assert isinstance(result, ScanResultV1)
    assert result.model_dump_json() == original
    assert result.provenance.packs == "not_implemented"
    assert project_result(result)["result"] == payload
    shared = project_result(result, profile="shareable")["result"]
    assert shared["schema_version"] == 1
    assert shared["provenance"]["packs"] == "not_implemented"
    with get_db(tmp_path) as conn:
        from vibe_sentinel.schemas import Snapshot

        run_id = store.save_run(conn, Snapshot(), DriftReport(), None, False)
        conn.execute(
            "UPDATE runs SET result_json=?, provenance_json=? WHERE id=?",
            (original, json.dumps(payload["provenance"]), run_id),
        )
        conn.commit()
        loaded = store.load_result(conn, run_id)
        assert loaded.model_dump_json() == original
        snapshot = store.load_snapshot(conn, run_id)
        assert snapshot.context.provenance.packs == "not_implemented"


@pytest.mark.parametrize("change", ["manifest", "executor"])
def test_catalog_evidence_cannot_be_mixed_between_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    manifest = configure(tmp_path)
    if change == "executor":
        manifest.write_text(
            manifest.read_text().replace(json.dumps(sys.executable), '"fixture-python"')
        )
        monkeypatch.setattr(
            "vibe_sentinel.packs.shutil.which", lambda _: sys.executable
        )
    first = load_probe_catalog(project_root=tmp_path)
    if change == "manifest":
        manifest.write_text(
            manifest.read_text().replace('version="1.0.0"', 'version="2.0.0"')
        )
    else:
        other = tmp_path / "other-python"
        other.symlink_to(sys.executable)
        monkeypatch.setattr("vibe_sentinel.packs.shutil.which", lambda _: str(other))
    second = load_probe_catalog(project_root=tmp_path)
    config = load_config(tmp_path / ".vibe-sentinel.toml")
    with pytest.raises(ValueError, match="matching loaded catalog"):
        asyncio.run(
            engine.scan_and_compare(
                first.probes, tmp_path, config, declared_packs=second.packs
            )
        )
    assert not (tmp_path / ".vibe-sentinel/history.db").exists()


def test_documented_protocol_smoke_example_runs(tmp_path: Path) -> None:
    import shutil

    example = Path(__file__).parents[1] / "docs/packs/protocol-smoke.toml"
    shutil.copyfile(example, tmp_path / "pack.toml")
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[packs]\nmanifests=["pack.toml"]\n[probes]\nuse_builtins=false\n'
    )
    snapshot, report, _ = run_scan(tmp_path)
    assert build_result(snapshot, report).exit_code == 0
    assert snapshot.probes["pack:protocol-smoke:fixture"].observations[0].value == 1


def test_pack_parameter_scope_change_requires_new_baseline(tmp_path: Path) -> None:
    manifest = configure(tmp_path)
    manifest.write_text(
        manifest.read_text().replace(
            'command=["python", "measure.py"]',
            'command=["python", "measure.py", "{ROOT}"]\n'
            '[[probe.placeholders]]\nname="ROOT"\ndescription="Selected input root"\ndefault="first"',
        )
    )
    for name, contents in (("first", "one"), ("second", "different scope")):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "source.txt").write_text(contents)
    (tmp_path / "measure.py").write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "print(json.dumps({'observations':[{'key':'source','value':len((Path(sys.argv[1])/'source.txt').read_text())}]}))\n"
    )
    _, _, first_id = run_scan(tmp_path)
    config_path = tmp_path / ".vibe-sentinel.toml"
    config_path.write_text(
        config_path.read_text()
        + '\n[probes.parameters."pack:fixture:size"]\nROOT="second"\n'
    )
    snapshot, report, _ = run_scan(tmp_path)
    result = build_result(snapshot, report)
    assert result.exit_code == 2
    assert result.baseline_run_id == first_id
    assert result.counts.baseline_unmeasured == 1
    assert not any(change.before is not None for change in report.changes)
    assert snapshot.probes["pack:fixture:size"].observations[0].value == len(
        "different scope"
    )
    _, _, accepted_id = run_scan(tmp_path, update=True)
    snapshot, report, _ = run_scan(tmp_path)
    result = build_result(snapshot, report)
    assert result.exit_code == 0
    assert result.baseline_run_id == accepted_id
