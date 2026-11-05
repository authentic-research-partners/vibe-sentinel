"""Static data contracts and migration topology, without application execution."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from vibe_sentinel.inventory import compare
from vibe_sentinel.probes import alembic, artifact_support, json_schema
from vibe_sentinel.runner import run_probe
from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Probe, load_probes


def schema_document(**kwargs: Any) -> dict[str, Any]:
    return {"$schema": json_schema.DIALECT, **kwargs}


def schema(root: Path, doc: Any) -> dict[str, Any]:
    (root / "schema.json").write_text(json.dumps(doc))
    return json_schema.Reader(root, "schema.json").measure()


def migration(
    root: Path, name: str, revision: str, down: Any = None, **kwargs: Any
) -> None:
    directory = root / "versions"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(
        "\n".join(
            f"{key} = {value!r}"
            for key, value in {
                "revision": revision,
                "down_revision": down,
                **kwargs,
            }.items()
        )
    )


def topology(root: Path) -> dict[str, Any]:
    return alembic.Reader(root, "versions").measure()


def codes(result: dict[str, Any]) -> set[str]:
    return {gap["code"] for gap in result["coverage"]["gaps"]}


def diff(before: dict[str, Any], after: dict[str, Any]) -> list:
    def snapshot(result: dict[str, Any]) -> Snapshot:
        return Snapshot(
            probes={
                "artifact": ProbeResult(
                    probe_id="artifact",
                    observations=[
                        Observation.model_validate(o) for o in result["observations"]
                    ],
                )
            }
        )

    return compare(
        snapshot(before), snapshot(after), [Probe(id="artifact", title="Artifact")]
    ).changes


def test_schema_types_properties_required_and_privacy(tmp_path: Path) -> None:
    doc = schema_document(
        type="object",
        properties={
            "name": {
                "type": "string",
                "default": "PRIVATE_DEFAULT",
                "examples": [{"PRIVATE_EXAMPLE": 3}],
                "enum": ["PRIVATE_ENUM"],
                "const": "PRIVATE_CONST",
                "description": "PRIVATE_DESCRIPTION",
                "pattern": "PRIVATE_REGEX",
            }
        },
        required=["name"],
    )
    before = schema(tmp_path, doc)
    assert before["coverage"]["status"] == "complete"
    assert "PRIVATE" not in json.dumps(before)
    doc["properties"]["name"]["type"] = ["string", "null"]
    after = schema(tmp_path, doc)
    assert any(change.kind == "changed" for change in diff(before, after))
    assert diff(after, schema(tmp_path, doc)) == []


def test_schema_local_pointer_refs_cycles_siblings_and_empty_names(
    tmp_path: Path,
) -> None:
    doc = schema_document(
        **{
            "$id": "https://private.invalid/schema",
            "$defs": {"a/b~c": {"$ref": "#", "type": "object"}},
            "properties": {"": {"$ref": "#/$defs/a~1b~0c", "type": "string"}},
            "required": [""],
        }
    )
    result = schema(tmp_path, doc)
    assert not codes(result)
    edges = [
        o["attrs"]
        for o in result["observations"]
        if o.get("attrs", {}).get("kind") == "reference"
    ]
    assert len(edges) == 2
    assert "private.invalid" not in json.dumps(result)
    assert any(o.get("state") == '{"types":["string"]}' for o in result["observations"])


@pytest.mark.parametrize("field", sorted(json_schema.SINGLES))
def test_schema_single_containment(field: str, tmp_path: Path) -> None:
    result = schema(tmp_path, schema_document(**{field: False}))
    assert not codes(result)
    assert any(o.get("state") == '{"boolean":false}' for o in result["observations"])


@pytest.mark.parametrize("field", sorted(json_schema.ARRAYS))
def test_schema_array_containment(field: str, tmp_path: Path) -> None:
    result = schema(tmp_path, schema_document(**{field: [{"type": "string"}, True]}))
    assert not codes(result)


def test_schema_dependent_names_and_schema_maps(tmp_path: Path) -> None:
    result = schema(
        tmp_path,
        schema_document(
            dependentRequired={"credit": ["billing"]},
            dependentSchemas={"credit": {"required": ["billing"]}},
        ),
    )
    assert not codes(result)
    assert "billing" in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("$vocabulary", {}),
        ("$dynamicRef", "#x"),
        ("$dynamicAnchor", "x"),
        ("$anchor", "x"),
        ("patternProperties", {"PRIVATE_REGEX": True}),
        ("definitions", {"x": True}),
        ("customKeyword", {"PRIVATE_PAYLOAD": True}),
    ],
)
def test_unknown_or_unsupported_structure_fails_without_values(
    tmp_path: Path, field: str, value: Any
) -> None:
    result = schema(tmp_path, schema_document(**{field: value}))
    assert "unsupported-schema-keyword" in codes(result)
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "target",
    [
        "https://private.invalid/schema",
        "other.json#/x",
        "#anchor",
        "#/%GG",
        "#/~2",
        "#/%FF",
        "#/missing",
        "#/default",
        "#/examples/0",
    ],
)
def test_reference_targets_are_only_registered_schema_locations(
    tmp_path: Path, target: str
) -> None:
    result = schema(
        tmp_path,
        schema_document(
            **{
                "$ref": target,
                "default": {"PRIVATE_VALUE": "data"},
                "examples": [{"PRIVATE_EXAMPLE": 1}],
            }
        ),
    )
    assert result["coverage"]["status"] == "incomplete"
    assert "PRIVATE" not in json.dumps(result)
    assert "private.invalid" not in json.dumps(result)


def test_root_dialect_nested_resource_and_schema_rebasing(tmp_path: Path) -> None:
    assert "explicit-2020-12-dialect-required" in codes(
        schema(tmp_path, {"type": "object"})
    )
    assert "explicit-2020-12-dialect-required" in codes(schema(tmp_path, True))
    assert "unsupported-resource-rebasing" in codes(
        schema(
            tmp_path,
            schema_document(properties={"x": {"$id": "nested", "type": "object"}}),
        )
    )
    assert "unsupported-dialect-or-resource" in codes(
        schema(
            tmp_path,
            schema_document(properties={"x": {"$schema": json_schema.DIALECT}}),
        )
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"type": ["string", "string"]},
        {"type": []},
        {"required": [1]},
        {"required": ["x", "x"]},
        {"properties": []},
        {"allOf": []},
        {"items": []},
        {"dependentRequired": []},
    ],
)
def test_invalid_structural_shapes_fail(tmp_path: Path, fields: dict[str, Any]) -> None:
    assert (
        schema(tmp_path, schema_document(**fields))["coverage"]["status"]
        == "incomplete"
    )


def test_alembic_branch_merge_dependency_alias_and_source_rename(
    tmp_path: Path,
) -> None:
    migration(tmp_path, "a.py", "a", branch_labels=("account",))
    migration(tmp_path, "b.py", "b", "a")
    migration(tmp_path, "c.py", "c", "a")
    migration(tmp_path, "merge.py", "merge", ("b", "c"))
    migration(tmp_path, "other.py", "other", depends_on="account")
    before = topology(tmp_path)
    assert not codes(before)
    edges = [
        o["attrs"]
        for o in before["observations"]
        if o.get("attrs", {}).get("edge_schema") == "1"
    ]
    assert any(e["kind"] == "depends_on" and e["target"] == "revision:a" for e in edges)
    (tmp_path / "versions/a.py").rename(tmp_path / "versions/renamed.py")
    assert not diff(before, topology(tmp_path))
    migration(tmp_path, "other.py", "other", depends_on="b")
    assert {change.kind for change in diff(before, topology(tmp_path))} >= {
        "appeared",
        "disappeared",
    }


def test_alembic_never_imports_modules_or_runs_function_bodies(tmp_path: Path) -> None:
    migration(tmp_path, "one.py", "a")
    file = tmp_path / "versions/one.py"
    file.write_text(
        'import nonexistent_package\nfrom typing import Optional\nrevision: str = "a"\ndown_revision: Optional[str] = None\n\ndef upgrade():\n    raise RuntimeError("PRIVATE_BODY")\n\ndef downgrade():\n    exec("PRIVATE_EXEC")\n'
    )
    result = topology(tmp_path)
    assert not codes(result)
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "suffix",
    [
        'revision = "b"',
        'revision += "b"',
        'if True:\n    revision = "b"',
        "revision = make_revision()",
        'globals()["revision"] = "b"',
        "fix_revision()",
        "from x import revision",
        "from x import *",
        "@decorator\ndef upgrade():\n    pass",
        "def upgrade(x=mutate()):\n    pass",
    ],
)
def test_ambiguous_or_computed_revision_modules_fail(
    tmp_path: Path, suffix: str
) -> None:
    migration(tmp_path, "a.py", "a")
    file = tmp_path / "versions/a.py"
    file.write_text(file.read_text() + "\n" + suffix)
    assert "unsupported-or-unreadable-migration" in codes(topology(tmp_path))


@pytest.mark.parametrize(
    "problem", ["duplicate", "missing", "cycle", "dependency-cycle", "label-collision"]
)
def test_invalid_revision_graphs_fail(tmp_path: Path, problem: str) -> None:
    migration(tmp_path, "a.py", "a", branch_labels="branch")
    if problem == "duplicate":
        migration(tmp_path, "b.py", "a")
    elif problem == "missing":
        migration(tmp_path, "b.py", "b", "missing")
    elif problem == "cycle":
        migration(tmp_path, "a.py", "a", "b")
        migration(tmp_path, "b.py", "b", "a")
    elif problem == "dependency-cycle":
        migration(tmp_path, "a.py", "a", depends_on="b")
        migration(tmp_path, "b.py", "b", depends_on="a")
    else:
        migration(tmp_path, "b.py", "b", branch_labels="branch")
    assert topology(tmp_path)["coverage"]["status"] == "incomplete"


def test_schema_duplicate_keys_bad_utf8_and_nonfinite_fail(tmp_path: Path) -> None:
    for raw in [b'{"x":1,"x":2}', b"\xff", b'{"x":NaN}', b'{"x":1e999}']:
        (tmp_path / "schema.json").write_bytes(raw)
        assert "unreadable-invalid-or-oversized-schema" in codes(
            json_schema.Reader(tmp_path, "schema.json").measure()
        )


def test_artifact_paths_and_symlinks_fail(tmp_path: Path) -> None:
    schema(tmp_path, schema_document())
    original = tmp_path / "schema.json"
    original.rename(tmp_path / "original.json")
    original.symlink_to(tmp_path / "original.json")
    assert (
        json_schema.Reader(tmp_path, "schema.json").measure()["coverage"]["status"]
        == "incomplete"
    )
    assert (
        json_schema.Reader(tmp_path, "../original.json").measure()["coverage"]["status"]
        == "incomplete"
    )
    migration(tmp_path, "a.py", "a")
    (tmp_path / "versions/link.py").symlink_to(tmp_path / "versions/a.py")
    assert "symlink-not-followed" in codes(topology(tmp_path))
    assert (
        alembic.Reader(tmp_path, "../versions").measure()["coverage"]["status"]
        == "incomplete"
    )


@pytest.mark.parametrize(
    "limit", ["MAX_NODES", "MAX_DEPTH", "MAX_OBSERVATIONS", "MAX_OUTPUT_BYTES"]
)
def test_schema_complexity_and_output_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(artifact_support, limit, 1)
    assert (
        schema(tmp_path, schema_document(properties={"x": {"type": "string"}}))[
            "coverage"
        ]["status"]
        == "incomplete"
    )


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_ENTRIES",
        "MAX_FILES",
        "MAX_SOURCE_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_AST_NODES",
    ],
)
def test_migration_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    migration(tmp_path, "a.py", "a")
    migration(tmp_path, "b.py", "b", "a")
    monkeypatch.setattr(alembic, limit, 1)
    assert topology(tmp_path)["coverage"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "module,option,value",
    [
        ("json_schema", "--artifact", "schema.json"),
        ("alembic", "--versions", "versions"),
    ],
)
def test_subprocess_protocol_and_incomplete_snapshot(
    tmp_path: Path, module: str, option: str, value: str
) -> None:
    schema(tmp_path, schema_document())
    migration(tmp_path, "a.py", "a")
    probe = Probe(
        id="artifact",
        title="Artifact",
        command=[
            sys.executable,
            "-m",
            f"vibe_sentinel.probes.{module}",
            "--root",
            str(tmp_path),
            option,
            value,
        ],
    )
    before = run_probe(probe, {}, cwd=tmp_path)
    assert before.ok and before.observations
    if module == "json_schema":
        (tmp_path / "schema.json").unlink()
    else:
        (tmp_path / "versions/a.py").write_text("revision = compute()")
    after = run_probe(probe, {}, cwd=tmp_path)
    assert not after.ok and not after.observations
    report = compare(
        Snapshot(probes={probe.id: before}), Snapshot(probes={probe.id: after}), [probe]
    )
    assert not report.changes and probe.id in report.unmeasured


@pytest.mark.parametrize(
    "name,module", [("json-schema", "json_schema"), ("alembic", "alembic")]
)
def test_opt_in_examples(name: str, module: str) -> None:
    root = Path(__file__).resolve().parents[1]
    selected = next(
        p
        for p in load_probes([root / f"docs/probes/{name}.toml"], root)
        if p.id == name
    )
    assert selected.compare_parameters
    assert selected.command[1:4] == ["-I", "-m", f"vibe_sentinel.probes.{module}"]
    assert not any(p.id == name for p in load_probes([], root))


@pytest.mark.parametrize(
    "source",
    [
        'revision: (revision := "changed") = "original"\ndown_revision = None',
        'revision = "original"\ndown_revision = None\ndef upgrade(x: (revision := "changed")): pass',
        'revision: mutate() = "original"\ndown_revision = None',
    ],
)
def test_alembic_computed_annotations_are_gaps(tmp_path: Path, source: str) -> None:
    migration(tmp_path, "a.py", "a")
    (tmp_path / "versions/a.py").write_text(source)
    assert "unsupported-or-unreadable-migration" in codes(topology(tmp_path))


def test_alembic_ordinary_future_annotations_remain_supported(tmp_path: Path) -> None:
    migration(tmp_path, "a.py", "a")
    (tmp_path / "versions/a.py").write_text(
        'from __future__ import annotations\nrevision: str = "a"\ndown_revision: str | None = None\ndef upgrade(value: str) -> None: pass'
    )
    assert not codes(topology(tmp_path))
