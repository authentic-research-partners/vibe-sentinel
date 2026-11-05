"""Vite artifact identity, attribution and failure contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel.probes import vite


def fixture(root: Path, suffix: str = "aaa") -> dict[str, Any]:
    chunks = {
        "index.html": {
            "file": f"assets/main-{suffix}.js",
            "isEntry": True,
            "imports": [f"_shared-{suffix}.js"],
            "dynamicImports": ["lazy.js"],
        },
        "other.html": {
            "file": f"assets/other-{suffix}.js",
            "isEntry": True,
            "imports": [f"_shared-{suffix}.js"],
        },
        "lazy.js": {
            "file": f"assets/lazy-{suffix}.js",
            "isDynamicEntry": True,
            "imports": [f"_shared-{suffix}.js"],
        },
        f"_shared-{suffix}.js": {
            "file": f"assets/shared-{suffix}.js",
            "css": [f"assets/style-{suffix}.css"],
            "assets": [f"assets/image-{suffix}.png"],
        },
    }
    output = root / "dist/assets"
    output.mkdir(parents=True, exist_ok=True)
    for name, size in (("main", 10), ("other", 20), ("lazy", 30), ("shared", 40)):
        (output / f"{name}-{suffix}.js").write_bytes(b"x" * size)
    (output / f"style-{suffix}.css").write_bytes(b"x" * 5)
    (output / f"image-{suffix}.png").write_bytes(b"x" * 7)
    save(root, chunks)
    return chunks


def save(root: Path, chunks: Any) -> None:
    manifest = root / "dist/.vite/manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(chunks))


def measure(root: Path) -> dict[str, Any]:
    return vite.Reader(root, "dist/.vite/manifest.json", "dist").measure()


def metric(result: dict[str, Any], kind: str, identity: Any, name: str) -> int:
    return next(
        item["value"]
        for item in result["observations"]
        if item.get("attrs", {}).get("kind") == kind
        and item["attrs"]["identity"] == vite.canonical(identity)
        and item["attrs"]["metric"] == name
    )


def gap(result: dict[str, Any], code: str) -> None:
    assert result["coverage"]["status"] == "incomplete"
    assert code in {item["code"] for item in result["coverage"]["gaps"]}


def test_hash_renames_and_mapping_order_do_not_change_evidence(tmp_path: Path) -> None:
    fixture(tmp_path)
    before = measure(tmp_path)
    chunks = fixture(tmp_path, "bbb")
    save(tmp_path, dict(reversed(list(chunks.items()))))
    assert measure(tmp_path) == before
    assert before["coverage"]["status"] == "complete"


def test_unique_weights_and_shared_entry_attribution(tmp_path: Path) -> None:
    fixture(tmp_path)
    result = measure(tmp_path)
    assert metric(result, "build", "manifest-declared", "raw-bytes") == 112
    assert metric(result, "entry", "index.html", "static-raw-bytes") == 62
    assert metric(result, "entry", "index.html", "dynamic-raw-bytes") == 30
    assert metric(result, "entry", "other.html", "static-raw-bytes") == 72
    assert metric(result, "entry", "lazy.js", "static-raw-bytes") == 82
    assert any(
        item.get("attrs", {}).get("edge_schema") == "1"
        for item in result["observations"]
    )
    assert "not-assessed" in next(
        item["state"]
        for item in result["observations"]
        if item["key"] == "vite:1:scope"
    )


def test_roles_union_same_file_and_global_dedup(tmp_path: Path) -> None:
    chunks = fixture(tmp_path)
    shared = chunks["_shared-aaa.js"]
    shared["assets"].append(shared["file"])
    shared["css"].append(shared["file"])
    save(tmp_path, chunks)
    result = measure(tmp_path)
    assert metric(result, "build", "manifest-declared", "raw-bytes") == 112
    identities = [
        json.loads(item["attrs"]["identity"])
        for item in result["observations"]
        if item.get("attrs", {}).get("kind") == "cohort"
    ]
    assert any(identity[0] == ["asset", "css", "output"] for identity in identities)


def test_rewire_same_total_changes_ownership_and_cycles_terminate(
    tmp_path: Path,
) -> None:
    chunks = fixture(tmp_path)
    before = measure(tmp_path)
    chunks["other.html"].pop("imports")
    chunks["lazy.js"]["imports"].append("index.html")
    save(tmp_path, chunks)
    after = measure(tmp_path)
    assert after["coverage"]["status"] == "complete"
    assert metric(after, "build", "manifest-declared", "raw-bytes") == 112
    assert {o["key"] for o in before["observations"]} != {
        o["key"] for o in after["observations"]
    }


def test_entry_role_change_is_comparable_state(tmp_path: Path) -> None:
    chunks = fixture(tmp_path)
    before = measure(tmp_path)
    chunks["index.html"]["isEntry"] = False
    chunks["index.html"]["isDynamicEntry"] = True
    save(tmp_path, chunks)
    after = measure(tmp_path)
    old = {o["key"]: o for o in before["observations"]}
    assert any(
        o["key"] in old and o.get("state") != old[o["key"]].get("state")
        for o in after["observations"]
    )


@pytest.mark.parametrize(
    "bad",
    [
        "../secret",
        "/etc/passwd",
        "assets/../secret",
        "assets\\secret",
        "https://example.org/x",
        "assets/missing.js",
    ],
)
def test_bad_output_paths_fail(tmp_path: Path, bad: str) -> None:
    chunks = fixture(tmp_path)
    chunks["index.html"]["file"] = bad
    save(tmp_path, chunks)
    assert measure(tmp_path)["coverage"]["status"] == "incomplete"


def test_missing_reference_and_unknown_structure_fail(tmp_path: Path) -> None:
    chunks = fixture(tmp_path)
    chunks["index.html"]["imports"] = ["missing"]
    save(tmp_path, chunks)
    gap(measure(tmp_path), "missing-chunk-reference")
    chunks["index.html"]["imports"] = []
    chunks["index.html"]["newRelationships"] = []
    save(tmp_path, chunks)
    gap(measure(tmp_path), "unsupported-manifest-field")


@pytest.mark.parametrize(
    "kind", ["file-symlink", "directory-symlink", "fifo", "directory"]
)
def test_nonregular_and_symlink_outputs_fail(tmp_path: Path, kind: str) -> None:
    fixture(tmp_path)
    target = tmp_path / "dist/assets/main-aaa.js"
    target.unlink()
    if kind == "file-symlink":
        target.symlink_to(tmp_path / "dist/assets/other-aaa.js")
    elif kind == "directory-symlink":
        (tmp_path / "dist/assets").rename(tmp_path / "real")
        (tmp_path / "dist/assets").symlink_to(
            tmp_path / "real", target_is_directory=True
        )
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    gap(measure(tmp_path), "missing-or-unsafe-output-file")


@pytest.mark.parametrize("raw", [b'{"a":{},"a":{}}', b"[]", b'{"x":NaN}', b"\xff"])
def test_invalid_json_fails_without_echoing_contents(
    tmp_path: Path, raw: bytes
) -> None:
    fixture(tmp_path)
    (tmp_path / "dist/.vite/manifest.json").write_bytes(raw)
    gap(measure(tmp_path), "unreadable-or-invalid-manifest")


def test_metadata_not_exported_and_asset_contents_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = fixture(tmp_path)
    chunks["index.html"]["name"] = "private annotation sentinel"
    chunks["index.html"]["src"] = "private source metadata sentinel"
    save(tmp_path, chunks)
    original_open = os.open

    def guarded(file: Any, *args: Any, **kwargs: Any) -> int:
        assert str(file).endswith("manifest.json")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    result = measure(tmp_path)
    assert result["coverage"]["status"] == "complete"
    assert "sentinel" not in vite.canonical(result)


@pytest.mark.parametrize(
    ("limit", "code"),
    [
        ("MAX_BYTES", "manifest-size-limit"),
        ("MAX_CHUNKS", "manifest-chunk-limit"),
        ("MAX_LINKS", "manifest-link-limit"),
        ("MAX_FILES", "artifact-file-limit"),
        ("MAX_WORK", "graph-work-limit"),
        ("MAX_OWNER_BYTES", "owner-identity-limit"),
        ("MAX_OBSERVATIONS", "observation-limit"),
        ("MAX_OUTPUT_BYTES", "output-limit"),
    ],
)
def test_bounds_are_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, code: str
) -> None:
    fixture(tmp_path)
    monkeypatch.setattr(vite, limit, 1)
    gap(measure(tmp_path), code)


def test_unlisted_files_ignored_and_no_entries_not_zero(tmp_path: Path) -> None:
    fixture(tmp_path)
    before = measure(tmp_path)
    (tmp_path / "dist/unlisted.map").write_bytes(b"private" * 100)
    assert measure(tmp_path) == before
    save(tmp_path, {})
    gap(measure(tmp_path), "no-logical-entries")


def test_manifest_scope_containment_and_symlinks(tmp_path: Path) -> None:
    fixture(tmp_path)
    assert (
        vite.Reader(tmp_path, "../manifest.json", "dist").measure()["coverage"][
            "status"
        ]
        == "incomplete"
    )
    assert (
        vite.Reader(tmp_path, "dist/.vite/manifest.json", "../dist").measure()[
            "coverage"
        ]["status"]
        == "incomplete"
    )
    manifest = tmp_path / "dist/.vite/manifest.json"
    manifest.rename(tmp_path / "manifest.json")
    manifest.symlink_to(tmp_path / "manifest.json")
    gap(measure(tmp_path), "unreadable-or-invalid-manifest")


def test_cli_incomplete_has_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        vite.main(
            [
                "--root",
                str(tmp_path),
                "--manifest",
                "dist/.vite/manifest.json",
                "--out-dir",
                "dist",
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["coverage"]["status"] == "incomplete"
    assert "coverage incomplete" in output.err


def test_runner_rejects_partial_snapshot_and_records_weight_drift(
    tmp_path: Path,
) -> None:
    import sys

    from vibe_sentinel.inventory import compare
    from vibe_sentinel.runner import run_probe
    from vibe_sentinel.schemas import Snapshot
    from vibe_sentinel.templates import Probe

    fixture(tmp_path)
    probe = Probe(
        id="vite-build",
        title="Build",
        command=[
            sys.executable,
            "-m",
            "vibe_sentinel.probes.vite",
            "--root",
            str(tmp_path),
            "--manifest",
            "dist/.vite/manifest.json",
            "--out-dir",
            "dist",
        ],
    )
    before = run_probe(probe, {}, cwd=tmp_path)
    assert before.ok and before.observations
    artifact = tmp_path / "dist/assets/main-aaa.js"
    artifact.write_bytes(b"x" * 11)
    after = run_probe(probe, {}, cwd=tmp_path)
    assert after.ok
    report = compare(
        Snapshot(probes={probe.id: before}), Snapshot(probes={probe.id: after}), [probe]
    )
    assert any(change.kind == "grew" for change in report.changes)
    artifact.unlink()
    broken = run_probe(probe, {}, cwd=tmp_path)
    assert not broken.ok and not broken.observations
    report = compare(
        Snapshot(probes={probe.id: before}),
        Snapshot(probes={probe.id: broken}),
        [probe],
    )
    assert not report.changes
    assert probe.id in report.unmeasured


def test_example_is_opt_in_parameter_guarded_and_isolated() -> None:
    from vibe_sentinel.templates import load_probes

    root = Path(__file__).resolve().parents[1]
    probe = next(
        probe
        for probe in load_probes([root / "docs/probes/vite.toml"], root)
        if probe.id == "vite-build"
    )
    assert probe.compare_parameters
    assert probe.command[1:4] == ["-I", "-m", "vibe_sentinel.probes.vite"]
    assert not any(probe.id == "vite-build" for probe in load_probes([], root))


@pytest.mark.parametrize(
    "field,phase", [("imports", "static"), ("dynamicImports", "dynamic")]
)
def test_direct_entry_rewire_survives_constant_closure(
    tmp_path: Path, field: str, phase: str
) -> None:
    output = tmp_path / "dist"
    output.mkdir()
    chunks = {}
    for name in ("a", "b", "c", "d"):
        (output / f"{name}.js").write_bytes(b"123")
        chunks[f"{name}.js"] = {"file": f"{name}.js", "isEntry": True}
    chunks["b.js"][field] = ["c.js", "d.js"]
    chunks["a.js"][field] = ["b.js", "c.js"]
    save(tmp_path, chunks)
    before = measure(tmp_path)
    chunks["a.js"][field] = ["b.js", "d.js"]
    save(tmp_path, chunks)
    after = measure(tmp_path)
    assert not before["coverage"]["gaps"] and not after["coverage"]["gaps"]

    def direct(result):
        return {
            o["key"]: o
            for o in result["observations"]
            if o.get("attrs", {}).get("kind") == "import"
        }

    old, new = direct(before), direct(after)
    removed, added = set(old) - set(new), set(new) - set(old)
    assert len(removed) == len(added) == 1
    assert old[removed.pop()]["attrs"] == {
        "edge_schema": "1",
        "ecosystem": "vite",
        "source": "entry:a.js",
        "target": "entry:c.js",
        "kind": "import",
        "phase": phase,
        "resolution": "manifest",
    }
    assert new[added.pop()]["attrs"]["target"] == "entry:d.js"
    assert [o for o in before["observations"] if o["key"] not in old] == [
        o for o in after["observations"] if o["key"] not in new
    ]
