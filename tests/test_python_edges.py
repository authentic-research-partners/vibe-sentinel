"""Opt-in Python source relationships, without application import execution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_sentinel.inventory import compare
from vibe_sentinel.probes.python_edges import Reader
from vibe_sentinel.relationships import edge_key
from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Probe


def write(root: Path, files: dict[str, str | bytes]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())


def read(root: Path, source_root: str = ".") -> dict:
    return Reader(root.resolve(), (root / source_root).absolute()).measure()


def edges(payload: dict) -> list[dict]:
    return [
        item for item in payload["observations"] if item["key"].startswith("edge:1:")
    ]


def gaps(payload: dict) -> set[str]:
    return {gap["code"] for gap in payload["coverage"]["gaps"]}


def test_shared_unicode_keys_match_javascript_generated_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "relationships-v1.json"
    for sample in json.loads(fixture.read_text()):
        assert edge_key(*sample["tuple"]) == sample["key"]


def test_count_preserving_rewire_and_new_probe_do_not_rewrite_old_series(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "from . import one\n",
            "pkg/one.py": "",
            "pkg/two.py": "",
        },
    )
    before = read(tmp_path)
    assert before["coverage"]["status"] == "complete"
    assert read(tmp_path) == before
    write(tmp_path, {"pkg/main.py": "from . import two\n"})
    after = read(tmp_path)
    assert len(edges(before)) == len(edges(after)) == 1
    assert edges(before)[0]["key"] != edges(after)[0]["key"]
    probe = Probe(id="python-dependency-edges", title="Edges")

    def snapshot(payload: dict) -> Snapshot:
        return Snapshot(
            probes={
                probe.id: ProbeResult(
                    probe_id=probe.id,
                    observations=[
                        Observation.model_validate(item)
                        for item in payload["observations"]
                    ],
                )
            }
        )

    changed = compare(snapshot(before), snapshot(after), [probe])
    assert {change.kind for change in changed.changes} == {"appeared", "disappeared"}
    legacy = Snapshot(
        probes={"module-organization": ProbeResult(probe_id="module-organization")}
    )
    expanded = snapshot(after)
    expanded.probes["module-organization"] = legacy.probes["module-organization"]
    first = compare(legacy, expanded, [probe])
    assert len(first.changes) == 1 and first.changes[0].severity == "info"


def test_relative_from_attributes_and_multi_symbol_occurrence_counts(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/data.py": "A=1\nB=2\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/main.py": "from ..data import A, B\nfrom .. import data\nimport pkg.data\n",
        },
    )
    payload = read(tmp_path)
    assert payload["coverage"]["status"] == "complete"
    assert len(edges(payload)) == 1
    assert edges(payload)[0]["value"] == 3
    assert edges(payload)[0]["attrs"]["target"] == "file:pkg/data.py"


def test_namespace_identity_is_a_directory_not_a_fake_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {"space/child.py": "", "main.py": "import space\nfrom space import child\n"},
    )
    payload = read(tmp_path)
    assert payload["coverage"]["status"] == "complete"
    assert {item["attrs"]["target"] for item in edges(payload)} == {
        "namespace:space",
        "file:space/child.py",
    }


@pytest.mark.parametrize(
    "initializer",
    [
        "child = 42\n",
        "def __getattr__(name):\n    return None\n",
        "from external import *\n",
    ],
)
def test_from_submodule_binding_ambiguity_is_explicit(
    tmp_path: Path, initializer: str
) -> None:
    write(
        tmp_path,
        {
            "pkg/__init__.py": initializer,
            "pkg/child.py": "",
            "main.py": "from pkg import child\n",
        },
    )
    assert "ambiguous-from-import" in gaps(read(tmp_path))


def test_module_namespace_collision_and_missing_relative_imports_are_gaps(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        {
            "pkg.py": "",
            "pkg/child.py": "from . import absent\n",
            "top.py": "from . import something\n",
            "bad.py": "import pkg.missing\n",
        },
    )
    found = gaps(read(tmp_path))
    assert "ambiguous-module-path" in found
    assert "outside-relative-import" in found
    assert "unresolved-internal-import" in found


def test_external_names_are_module_references_not_distribution_claims(
    tmp_path: Path,
) -> None:
    write(tmp_path, {"main.py": "import os\nfrom PIL import Image\n"})
    payload = read(tmp_path)
    assert payload["coverage"]["status"] == "complete"
    assert {item["attrs"]["target"] for item in edges(payload)} == {
        "package:os",
        "package:PIL",
    }
    assert all(item["attrs"]["resolution"] == "external" for item in edges(payload))


def test_lexical_type_guards_aliases_and_inversion(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "runtime.py": "",
            "types_only.py": "",
            "main.py": "import typing as t\nfrom typing import TYPE_CHECKING as TC\nif t.TYPE_CHECKING:\n    import types_only\nelse:\n    import runtime\nif not TC:\n    import runtime\nelse:\n    import types_only\n",
        },
    )
    payload = read(tmp_path)
    assert payload["coverage"]["status"] == "complete"
    selected = {
        item["attrs"]["target"]: item
        for item in edges(payload)
        if item["attrs"]["target"].startswith("file:")
    }
    assert selected["file:types_only.py"]["attrs"]["phase"] == "type"
    assert selected["file:types_only.py"]["value"] == 2
    assert selected["file:runtime.py"]["attrs"]["phase"] == "runtime"


@pytest.mark.parametrize(
    "source",
    [
        "from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    import local\n",
        "from typing import TYPE_CHECKING as TC\ndef f(TC):\n    if TC:\n        import local\n",
        "def f():\n    from typing import TYPE_CHECKING as TC\n    if TC:\n        import local\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING and other:\n    import local\n",
    ],
)
def test_ambiguous_type_guard_never_claims_proved_type_only(
    tmp_path: Path, source: str
) -> None:
    write(tmp_path, {"main.py": source, "local.py": ""})
    assert "ambiguous-type-guard" in gaps(read(tmp_path))


def test_local_typing_module_does_not_prove_standard_type_guard(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "typing.py": "TYPE_CHECKING=True",
            "main.py": "import typing\nif typing.TYPE_CHECKING:\n    import target\n",
            "target.py": "",
        },
    )
    assert "ambiguous-type-guard" in gaps(read(tmp_path))


@pytest.mark.parametrize(
    "source",
    [
        "__import__('target')\n",
        "import importlib as il\nil.import_module('target')\n",
        "from importlib import import_module as load\nload(variable)\n",
        "def f():\n    import importlib as il\n    return il.import_module('target')\n",
        "if choice:\n    from importlib import import_module as load\n    load('target')\n",
    ],
)
def test_dynamic_loader_calls_fail_coverage_without_execution(
    tmp_path: Path, source: str
) -> None:
    write(
        tmp_path,
        {
            "main.py": source,
            "target.py": "raise RuntimeError('Never execute this module')\n",
        },
    )
    payload = read(tmp_path)
    assert gaps(payload) & {"dynamic-import", "ambiguous-dynamic-loader"}
    assert "Never execute" not in json.dumps(payload)


def test_invalid_source_symlinks_and_source_root_containment(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, {"secret.py": "import should_not_read\n"})
    root = tmp_path / "project"
    write(
        root,
        {
            "main.py": "",
            "bad.py": b"\xc3(",
            ".venv/ignored.py": "__import__('ignored')",
        },
    )
    (root / "linked.py").symlink_to(outside / "secret.py")
    payload = read(root)
    assert gaps(payload) == {"unparsed-source", "symlink-not-followed"}
    assert "should_not_read" not in json.dumps(payload)
    assert gaps(read(root, "../outside")) == {"invalid-source-root"}


def test_cli_failure_reports_coverage_and_never_claims_empty_success(
    tmp_path: Path,
) -> None:
    write(tmp_path, {"main.py": "__import__('unknown')\n"})
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "vibe_sentinel.probes.python_edges",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["coverage"]["status"] == "incomplete"
    assert "dynamic-import" in result.stderr
    assert len(result.stderr) < 500


def test_optional_example_declares_scope_guard_without_enabling_builtins() -> None:
    from vibe_sentinel.templates import load_probes_from_toml

    example = (
        Path(__file__).parents[1] / "docs" / "probes" / "python-relationships.toml"
    )
    (probe,) = load_probes_from_toml(example)
    assert probe.id == "python-dependency-edges"
    assert probe.compare_parameters is True
    assert probe.fill(probe.defaults()) == [
        "python",
        "-I",
        "-m",
        "vibe_sentinel.probes.python_edges",
        "--root",
        ".",
        "--source-root",
        ".",
    ]


def test_isolated_scanner_never_imports_hostile_project_shadows(tmp_path: Path) -> None:
    trap = (
        "raise RuntimeError('application module must not execute during measurement')\n"
    )
    write(
        tmp_path,
        {
            "ast.py": trap,
            "typing.py": trap,
            "pydantic.py": trap,
            "vibe_sentinel/__init__.py": trap,
            "normal.py": "VALUE=1\n",
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "vibe_sentinel.probes.python_edges",
            "--root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["coverage"]["status"] == "complete"
    assert "application module must not execute" not in result.stdout
