"""OpenAPI declarations: structural truth, bounded coverage and private values."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_sentinel.inventory import compare
from vibe_sentinel.probes import openapi
from vibe_sentinel.runner import run_probe
from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Probe, load_probes


def document(version: str = "3.1.0") -> dict:
    return {
        "openapi": version,
        "info": {"title": "Fixture", "version": "1"},
        "paths": {
            "/pets": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "Successful response",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Pet"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "age": {"type": "integer"},
                    },
                },
                "Other": {"type": "string"},
            },
            "securitySchemes": {
                "token": {"type": "http", "scheme": "bearer"},
                "key": {"type": "apiKey", "in": "header", "name": "X-Key"},
            },
        },
    }


def measure(root: Path, doc: dict) -> dict:
    (root / "openapi.json").write_text(json.dumps(doc), encoding="utf-8")
    return openapi.Reader(root, Path("openapi.json")).measure()


def states(payload: dict, kind: str) -> dict[str, object]:
    return {
        o["attrs"]["pointer"]: json.loads(o["state"])
        for o in payload["observations"]
        if o["attrs"].get("kind") == kind and "state" in o
    }


def gaps(payload: dict) -> set[str]:
    return {gap["code"] for gap in payload["coverage"]["gaps"]}


def difference(before: dict, after: dict) -> list:
    def snapshot(payload: dict) -> Snapshot:
        return Snapshot(
            probes={
                "openapi-artifact": ProbeResult(
                    probe_id="openapi-artifact",
                    observations=[
                        Observation.model_validate(o) for o in payload["observations"]
                    ],
                )
            }
        )

    return compare(
        snapshot(before), snapshot(after), [Probe(id="openapi-artifact", title="API")]
    ).changes


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_supported_complete_projection_is_stable(tmp_path: Path, version: str) -> None:
    doc = document(version)
    before = measure(tmp_path, doc)
    assert before["coverage"] == {"status": "complete", "gaps": []}
    assert difference(before, measure(tmp_path, doc)) == []
    assert states(before, "operation") == {"#/paths/~1pets/get": {"method": "get"}}
    assert states(before, "schema")["#/components/schemas/Pet"]["required"] == ["name"]


@pytest.mark.parametrize("change", ["path", "method", "required", "reference"])
def test_count_preserving_structure_changes_remain_visible(
    tmp_path: Path, change: str
) -> None:
    doc = document()
    before = measure(tmp_path, doc)
    if change == "path":
        doc["paths"]["/animals"] = doc["paths"].pop("/pets")
    elif change == "method":
        doc["paths"]["/pets"]["post"] = doc["paths"]["/pets"].pop("get")
    elif change == "required":
        doc["components"]["schemas"]["Pet"]["required"] = ["age"]
    else:
        doc["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"] = "#/components/schemas/Other"
    after = measure(tmp_path, doc)
    assert not gaps(after)
    assert len(before["observations"]) == len(after["observations"])
    changes = difference(before, after)
    assert changes
    assert {c.kind for c in changes} == (
        {"changed"} if change == "required" else {"appeared", "disappeared"}
    )


def test_security_presence_and_effective_inheritance_are_distinct(
    tmp_path: Path,
) -> None:
    doc = document()
    operation = doc["paths"]["/pets"]["get"]
    omitted = measure(tmp_path, doc)
    doc["security"] = [{"token": []}]
    inherited = measure(tmp_path, doc)
    operation["security"] = []
    cleared = measure(tmp_path, doc)
    operation["security"] = [{}]
    optional = measure(tmp_path, doc)
    identity = "#/paths/~1pets/get"
    assert states(omitted, "security-attachment")[identity] == {
        "declaration": "omitted",
        "alternatives": None,
    }
    assert states(inherited, "security-attachment")[identity] == {
        "declaration": "inherited",
        "alternatives": [[["token", []]]],
    }
    assert states(cleared, "security-attachment")[identity] == {
        "declaration": "explicit",
        "alternatives": [],
    }
    assert states(optional, "security-attachment")[identity] == {
        "declaration": "explicit",
        "alternatives": [[]],
    }
    assert difference(inherited, cleared) and difference(cleared, optional)


def test_security_and_or_groups_and_names_are_not_flattened(tmp_path: Path) -> None:
    doc = document()
    doc["security"] = [{"token": ["read", "write"], "key": []}, {}]
    before = measure(tmp_path, doc)
    doc["security"] = [{}, {"key": [], "token": ["write", "read"]}]
    assert measure(tmp_path, doc) == before
    doc["security"] = [{"token": ["read", "write"]}, {"key": []}]
    assert difference(before, measure(tmp_path, doc))
    doc["security"] = [{}, {}, {"token": []}]
    groups = states(measure(tmp_path, doc), "security-declaration")["#"]
    assert groups.count([]) == 2


def test_recursive_and_escaped_unicode_schema_references_are_finite(
    tmp_path: Path,
) -> None:
    doc = document()
    doc["components"]["schemas"]["数据/~"] = {
        "type": "object",
        "properties": {"next": {"$ref": "#/components/schemas/%E6%95%B0%E6%8D%AE~1~0"}},
    }
    result = measure(tmp_path, doc)
    assert not gaps(result)
    edges = [o for o in result["observations"] if o["attrs"].get("kind") == "reference"]
    assert any(o["attrs"]["target"] == "#/components/schemas/数据~1~0" for o in edges)


@pytest.mark.parametrize("version,has_siblings", [("3.0.3", False), ("3.1.0", True)])
def test_schema_reference_siblings_follow_version(
    tmp_path: Path, version: str, has_siblings: bool
) -> None:
    doc = document(version)
    doc["components"]["schemas"]["Other"] = {
        "$ref": "#/components/schemas/Pet",
        "required": ["age"],
        "properties": {"extra": {"type": "boolean"}},
    }
    result = measure(tmp_path, doc)
    assert not gaps(result)
    schemas = states(result, "schema")
    assert ("#/components/schemas/Other/properties/extra" in schemas) == has_siblings
    assert ("#/components/schemas/Other" in schemas) == has_siblings


def test_non_schema_reference_siblings_ignored_and_component_attachments_measured(
    tmp_path: Path,
) -> None:
    doc = document()
    doc["components"]["responses"] = {
        "Ok": doc["paths"]["/pets"]["get"]["responses"]["200"]
    }
    doc["paths"]["/pets"]["get"]["responses"]["200"] = {
        "$ref": "#/components/responses/Ok",
        "content": {"text/plain": {"schema": {"type": "boolean"}}},
    }
    result = measure(tmp_path, doc)
    assert not gaps(result)
    assert not any("text~1plain" in o["label"] for o in result["observations"])


@pytest.mark.parametrize(
    "ref",
    [
        "https://private.invalid/api.json#/X",
        "other.json#/X",
        "#anchor",
        "#/components/schemas/Missing",
        "#/components/schemas/Pet~2",
        "#/components/schemas/%ZZ",
        "#/info",
    ],
)
def test_unsupported_or_missing_reference_never_complete(
    tmp_path: Path, ref: str
) -> None:
    doc = document()
    doc["components"]["schemas"]["Other"] = {"$ref": ref}
    result = measure(tmp_path, doc)
    assert result["coverage"]["status"] == "incomplete"
    assert ref not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("$id", "private"),
        ("$anchor", "private"),
        ("$dynamicRef", "#private"),
        ("$schema", "https://custom.invalid/schema"),
        ("if", {"type": "string"}),
        ("discriminator", {"propertyName": "kind"}),
    ],
)
def test_unsupported_schema_structure_is_explicit(
    tmp_path: Path, field: str, value: object
) -> None:
    doc = document()
    doc["components"]["schemas"]["Pet"][field] = value
    assert measure(tmp_path, doc)["coverage"]["status"] == "incomplete"


def test_payloads_annotations_urls_and_extensions_are_not_traversed(
    tmp_path: Path,
) -> None:
    doc = document()
    baseline = measure(tmp_path, doc)
    canary = "SENSITIVE_PAYLOAD_CANARY"
    ignored = {
        "default": canary,
        "enum": [canary],
        "example": {canary: {"$ref": "https://remote.invalid"}},
        "examples": [canary],
        "description": canary,
        "x-internal": {canary: {"$dynamicRef": canary}},
    }
    doc["components"]["schemas"]["Pet"].update(ignored)
    doc["servers"] = [{"url": canary}]
    doc["info"].update(description=canary)
    doc["paths"]["/pets"]["get"].update(description=canary, summary=canary)
    result = measure(tmp_path, doc)
    assert result == baseline
    assert canary not in json.dumps(result)
    doc["components"]["securitySchemes"]["oauth"] = {
        "type": "oauth2",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": canary,
                "tokenUrl": canary,
                "scopes": {"read": canary},
            }
        },
    }
    doc["components"]["securitySchemes"]["oidc"] = {
        "type": "openIdConnect",
        "openIdConnectUrl": canary,
    }
    result = measure(tmp_path, doc)
    assert not gaps(result) and canary not in json.dumps(result)


@pytest.mark.parametrize(
    "value", [None, {}, "bad", [None], [{"missing": []}], [{"token": "bad"}]]
)
def test_malformed_security_does_not_become_clean(
    tmp_path: Path, value: object
) -> None:
    doc = document()
    doc["paths"]["/pets"]["get"]["security"] = value
    assert measure(tmp_path, doc)["coverage"]["status"] == "incomplete"


def test_schema_shapes_and_parameter_body_header_attachment(tmp_path: Path) -> None:
    doc = document()
    schema = doc["components"]["schemas"]["Pet"]
    schema.update(
        additionalProperties=False,
        allOf=[{"type": "object"}, {"$ref": "#/components/schemas/Other"}],
    )
    schema["properties"]["list"] = {
        "type": "array",
        "items": {"type": ["string", "null"]},
        "prefixItems": [False],
    }
    operation = doc["paths"]["/pets"]["get"]
    operation["parameters"] = [
        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}
    ]
    operation["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
        },
    }
    operation["responses"]["200"]["headers"] = {
        "Count": {"schema": {"type": "integer"}}
    }
    result = measure(tmp_path, doc)
    assert not gaps(result)
    assert (
        states(result, "parameters")["#/paths/~1pets/get/parameters/0"]["name"] == "q"
    )
    assert states(result, "additional-properties")["#/components/schemas/Pet"] is False


def test_path_item_reference_and_cycle_boundary(tmp_path: Path) -> None:
    doc = document()
    doc["components"]["pathItems"] = {"Pets": copy.deepcopy(doc["paths"]["/pets"])}
    doc["paths"]["/pets"] = {"$ref": "#/components/pathItems/Pets"}
    assert not gaps(measure(tmp_path, doc))
    doc["components"]["pathItems"]["Pets"] = {"$ref": "#/components/pathItems/Pets"}
    assert "cyclic-path-item-reference" in gaps(measure(tmp_path, doc))
    doc["paths"]["/pets"]["get"] = {}
    assert "ambiguous-path-item-reference" in gaps(measure(tmp_path, doc))


@pytest.mark.parametrize(
    "data",
    [
        b"{}",
        b'{"openapi":"3.1.0","paths":{},"paths":{}}',
        b'{"openapi":"3.1.0","x":NaN}',
        b"\xff",
        b"[]",
    ],
)
def test_invalid_json_version_or_duplicate_keys_fail(
    tmp_path: Path, data: bytes
) -> None:
    (tmp_path / "openapi.json").write_bytes(data)
    assert (
        openapi.Reader(tmp_path, Path("openapi.json")).measure()["coverage"]["status"]
        == "incomplete"
    )


def test_containment_size_and_output_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "real.json").write_text(json.dumps(document()))
    (tmp_path / "link.json").symlink_to(tmp_path / "real.json")
    for path in [Path("link.json"), Path("../outside.json"), tmp_path]:
        assert (
            openapi.Reader(tmp_path, path).measure()["coverage"]["status"]
            == "incomplete"
        )
    monkeypatch.setattr(openapi, "MAX_BYTES", 2)
    assert "artifact-size-limit" in gaps(
        openapi.Reader(tmp_path, Path("real.json")).measure()
    )
    monkeypatch.setattr(openapi, "MAX_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(openapi, "MAX_OUTPUT_BYTES", 2)
    result = openapi.Reader(tmp_path, Path("real.json")).measure()
    assert gaps(result) == {"output-limit"} and result["observations"] == []


def test_cli_protocol_and_failed_reader_do_not_invent_removed_edges(
    tmp_path: Path,
) -> None:
    doc = document()
    measure(tmp_path, doc)
    # -m uses the test-selected checkout through PYTHONPATH. Isolated installed
    # invocation is a separate artifact integration check, not faked by this.
    probe = Probe(
        id="openapi-artifact",
        title="API",
        command=[
            sys.executable,
            "-m",
            "vibe_sentinel.probes.openapi",
            "--root",
            str(tmp_path),
            "--artifact",
            "openapi.json",
        ],
    )
    before = run_probe(probe, {}, cwd=tmp_path)
    assert before.ok and before.observations
    doc["components"]["schemas"]["Other"] = {"$ref": "https://example.invalid/private"}
    measure(tmp_path, doc)
    after = run_probe(probe, {}, cwd=tmp_path)
    assert not after.ok and not after.observations
    assert "unsupported-reference" in after.error
    assert "https://example.invalid/private" not in after.error
    report = compare(
        Snapshot(probes={probe.id: before}), Snapshot(probes={probe.id: after}), [probe]
    )
    assert not any(change.kind == "disappeared" for change in report.changes)
    help_result = subprocess.run(
        [sys.executable, "-m", "vibe_sentinel.probes.openapi", "--help"],
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0 and "--artifact" in help_result.stdout


def test_example_is_opt_in_parameter_guarded_and_isolated() -> None:
    root = Path(__file__).resolve().parents[1]
    probe = next(
        p
        for p in load_probes([root / "docs/probes/openapi.toml"], root)
        if p.id == "openapi-artifact"
    )
    assert probe.compare_parameters
    assert probe.command[1:4] == ["-I", "-m", "vibe_sentinel.probes.openapi"]


def test_inline_typed_reference_targets_and_non_schema_cycle(tmp_path: Path) -> None:
    doc = document()
    doc["paths"]["/pets"]["parameters"] = [
        {"name": "q", "in": "query", "schema": {"type": "string"}}
    ]
    operation = doc["paths"]["/pets"]["get"]
    operation["parameters"] = [{"$ref": "#/paths/~1pets/parameters/0"}]
    assert not gaps(measure(tmp_path, doc))
    doc["paths"]["/pets"]["parameters"] = [{"$ref": "#/paths/~1pets/get/parameters/0"}]
    assert "cyclic-component-reference" in gaps(measure(tmp_path, doc))


def test_reference_cannot_promote_ignored_payload_object(tmp_path: Path) -> None:
    doc = document()
    doc["components"]["schemas"]["Pet"]["example"] = {
        "type": "object",
        "properties": {"PRIVATE_CANARY": {"type": "string"}},
    }
    doc["components"]["schemas"]["Other"] = {"$ref": "#/components/schemas/Pet/example"}
    result = measure(tmp_path, doc)
    assert "invalid-reference-target" in gaps(result)
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_nonfinite_exponent_and_surrogate_identifier_fail_safely(
    tmp_path: Path,
) -> None:
    (tmp_path / "openapi.json").write_text('{"openapi":"3.1.0","x-private":1e9999}')
    assert gaps(openapi.Reader(tmp_path, Path("openapi.json")).measure())
    doc = document()
    doc["components"]["schemas"]["\ud800"] = {"type": "string"}
    result = measure(tmp_path, doc)
    assert gaps(result)
    assert "\\ud800" not in json.dumps(result)


def test_non_oauth_security_role_names_are_version_specific(tmp_path: Path) -> None:
    doc = document("3.0.3")
    doc["security"] = [{"token": ["reader"]}]
    assert "invalid-security-scopes" in gaps(measure(tmp_path, doc))
    doc["openapi"] = "3.1.0"
    assert not gaps(measure(tmp_path, doc))


def test_lowercase_x_header_is_measured_and_long_pointer_is_bounded(
    tmp_path: Path,
) -> None:
    doc = document()
    doc["paths"]["/pets"]["get"]["responses"]["200"]["headers"] = {
        "x-trace": {"schema": {"type": "string"}}
    }
    assert any(
        "/headers/x-trace" in key for key in states(measure(tmp_path, doc), "headers")
    )
    doc["components"]["schemas"]["Pet"]["properties"]["a" * 2040] = {"type": "string"}
    assert "structural-pointer-limit" in gaps(measure(tmp_path, doc))


def test_expanded_path_item_does_not_invent_physical_pointer_target(
    tmp_path: Path,
) -> None:
    doc = document()
    doc["components"]["pathItems"] = {"Pets": doc["paths"]["/pets"]}
    doc["paths"]["/pets"] = {"$ref": "#/components/pathItems/Pets"}
    doc["components"]["schemas"]["Other"] = {
        "$ref": "#/paths/~1pets/get/responses/200/content/application~1json/schema"
    }
    result = measure(tmp_path, doc)
    assert "invalid-reference-target" in gaps(result)


@pytest.mark.parametrize("location", ["required", "property", "security"])
def test_lone_surrogate_never_reaches_observation_or_diagnostic(
    tmp_path: Path, location: str
) -> None:
    doc = document()
    if location == "required":
        doc["components"]["schemas"]["Pet"]["required"] = ["\ud800"]
    elif location == "property":
        doc["components"]["schemas"]["Pet"]["properties"]["\ud800"] = {}
    else:
        doc["security"] = [{"token": ["\ud800"]}]
    result = measure(tmp_path, doc)
    assert gaps(result)
    encoded = openapi.canonical(result).encode("utf-8")
    assert b"\\ud800" not in encoded
