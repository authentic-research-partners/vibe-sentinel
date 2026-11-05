"""Read a bounded structural projection of a local OpenAPI JSON artifact.

This is declaration evidence, not schema validation or runtime authorization.
Only explicit structural fields are traversed; payload and annotation values
never become observations or diagnostic text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from vibe_sentinel.relationships import edge_observation

MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 128
MAX_NODES = 100_000
MAX_OBSERVATIONS = 30_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2048
METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)
DIALECT = "https://spec.openapis.org/oas/3.1/dialect/base"
TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
COMPONENTS = (
    "schemas",
    "parameters",
    "headers",
    "requestBodies",
    "responses",
    "securitySchemes",
    "pathItems",
)
UNSUPPORTED_SCHEMA = frozenset(
    {
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$recursiveRef",
        "$recursiveAnchor",
        "patternProperties",
        "dependentSchemas",
        "dependencies",
        "if",
        "then",
        "else",
        "contains",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contentSchema",
        "discriminator",
    }
)
# Known annotations and validation constraints are outside this projection.
# Unknown vocabulary cannot silently introduce unmeasured schema structure.
IGNORED_SCHEMA = frozenset(
    {
        "title",
        "description",
        "$comment",
        "default",
        "example",
        "examples",
        "enum",
        "const",
        "format",
        "deprecated",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
        "contentEncoding",
        "contentMediaType",
        "xml",
        "externalDocs",
    }
)
SUPPORTED_SCHEMA = frozenset(
    {
        "$ref",
        "$schema",
        "type",
        "nullable",
        "readOnly",
        "writeOnly",
        "required",
        "properties",
        "$defs",
        "items",
        "additionalProperties",
        "not",
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
    }
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pointer(parent: str, name: str | int) -> str:
    return parent + "/" + str(name).replace("~", "~0").replace("/", "~1")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


class Reader:
    """Own file containment, structural traversal, and the coverage boundary."""

    def __init__(self, root: Path, artifact: Path) -> None:
        self.root = root.resolve()
        self.artifact = artifact if artifact.is_absolute() else self.root / artifact
        self.document: dict[str, Any] = {}
        self.version = ""
        self.observations: dict[str, dict[str, Any]] = {}
        self.gaps: set[tuple[str, str]] = set()
        self.schemas: set[str] = set()
        self.references: list[tuple[str, str, str]] = []
        self.objects: dict[str, set[str]] = {kind: set() for kind in COMPONENTS}
        self.visited: set[tuple[str, str]] = set()
        self.relative = "artifact"

    def gap(self, code: str, scope: str = "#") -> None:
        # Scope is a structural pointer, never an arbitrary reference value.
        if len(self.gaps) < 100:
            self.gaps.add((code, scope[:MAX_TEXT]))

    def text(self, value: Any, scope: str) -> bool:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_TEXT
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
        ):
            self.gap("invalid-structural-name", scope)
            return False
        return True

    def record(self, kind: str, scope: str, state: Any) -> None:
        if len(scope) > MAX_TEXT:
            self.gap("structural-pointer-limit")
            return
        key = (
            "openapi:1:" + hashlib.sha256(canonical([kind, scope]).encode()).hexdigest()
        )
        if len(self.observations) >= MAX_OBSERVATIONS and key not in self.observations:
            self.gap("observation-limit")
            return
        self.observations[key] = {
            "key": key,
            "value": 1,
            "state": canonical(state),
            "label": f"{kind} {scope}",
            "attrs": {"kind": kind, "pointer": scope},
        }

    def edge(self, source: str, target: str, kind: str) -> None:
        if len(source) > MAX_TEXT or len(target) > MAX_TEXT:
            self.gap("structural-pointer-limit")
            return
        item = edge_observation("openapi", source, target, kind, "declaration", "local")
        if (
            len(self.observations) < MAX_OBSERVATIONS
            or item["key"] in self.observations
        ):
            self.observations[item["key"]] = item
        else:
            self.gap("observation-limit")

    def load(self) -> bool:
        try:
            parts = self.artifact.relative_to(self.root).parts
            if not parts or ".." in parts:
                raise ValueError("outside project")
            current = self.root
            for part in parts:
                current /= part
                if current.is_symlink():
                    raise ValueError("symlink")
            if not stat.S_ISREG(self.artifact.stat().st_mode):
                raise ValueError("not regular")
            self.relative = Path(*parts).as_posix()
            if not self.text(self.relative, "#"):
                self.relative = "artifact"
                return False
            with self.artifact.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                self.gap("artifact-size-limit")
                return False
            document = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
                parse_float=finite_float,
            )
            pending = [(document, 0)]
            count = 0
            while pending:
                value, depth = pending.pop()
                count += 1
                if count > MAX_NODES or depth > MAX_DEPTH:
                    self.gap("document-complexity-limit")
                    return False
                if isinstance(value, dict):
                    pending.extend((v, depth + 1) for v in value.values())
                elif isinstance(value, list):
                    pending.extend((v, depth + 1) for v in value)
            if not isinstance(document, dict):
                raise ValueError("document object required")
            self.document = document
            version = document.get("openapi")
            if (
                not isinstance(version, str)
                or re.fullmatch(r"3\.[01]\.\d+", version) is None
            ):
                self.gap("unsupported-openapi-version")
                return False
            self.version = version[:3]
            if "jsonSchemaDialect" in document and (
                self.version != "3.1" or document["jsonSchemaDialect"] != DIALECT
            ):
                self.gap("unsupported-schema-dialect")
                return False
            return True
        except (OSError, ValueError, UnicodeError, RecursionError):
            self.gap("unreadable-or-invalid-json-artifact")
            return False

    def mapping(self, value: Any, scope: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.gap("expected-object", scope)
            return {}
        return value

    def ref_pointer(self, value: Any, scope: str) -> str | None:
        if (
            not isinstance(value, str)
            or len(value) > MAX_TEXT
            or not value.startswith("#/")
        ):
            self.gap("unsupported-reference", scope)
            return None
        try:
            fragment = unquote(value[1:], encoding="utf-8", errors="strict")
            if re.search(r"%(?![0-9a-fA-F]{2})", value[1:]):
                raise ValueError("invalid percent escape")
            parts = fragment[1:].split("/")
            target = "#"
            for part in parts:
                if re.search(r"~(?![01])", part):
                    raise ValueError("invalid pointer escape")
                name = part.replace("~1", "/").replace("~0", "~")
                if not self.text(name, scope):
                    return None
                target = pointer(target, name)
            return target
        except (ValueError, UnicodeError):
            self.gap("invalid-reference-pointer", scope)
            return None

    def reference(self, obj: dict[str, Any], scope: str, kind: str) -> bool:
        if "$ref" not in obj:
            return False
        target = self.ref_pointer(obj["$ref"], scope)
        if target is not None:
            self.references.append((scope, target, kind))
        return True

    def schema(self, value: Any, scope: str) -> None:
        if len(scope) > MAX_TEXT:
            self.gap("structural-pointer-limit")
            return
        if scope in self.schemas:
            return
        self.schemas.add(scope)
        if isinstance(value, bool):
            if self.version == "3.1":
                self.record("schema", scope, {"boolean": value})
            else:
                self.gap("boolean-schema-requires-3.1", scope)
            return
        obj = self.mapping(value, scope)
        referenced = self.reference(obj, scope, "schemas")
        if referenced and self.version == "3.0":
            return
        if "$schema" in obj and (self.version != "3.1" or obj["$schema"] != DIALECT):
            self.gap("unsupported-schema-dialect", scope)
        if UNSUPPORTED_SCHEMA.intersection(obj) or any(
            key not in SUPPORTED_SCHEMA | IGNORED_SCHEMA and not key.startswith("x-")
            for key in obj
        ):
            self.gap("unsupported-schema-structure", scope)
        shape: dict[str, Any] = {}
        if "type" in obj:
            types = obj["type"]
            if isinstance(types, str):
                types = [types]
            if (
                not isinstance(types, list)
                or not types
                or any(not isinstance(t, str) or t not in TYPES for t in types)
                or (
                    self.version == "3.0"
                    and (
                        len(types) != 1
                        or types[0] == "null"
                        or isinstance(obj["type"], list)
                    )
                )
            ):
                self.gap("invalid-schema-type", scope)
            else:
                shape["types"] = sorted(set(types))
        for field in ("nullable", "readOnly", "writeOnly"):
            if field in obj:
                if not isinstance(obj[field], bool) or (
                    field == "nullable" and self.version == "3.1"
                ):
                    self.gap("unsupported-schema-flag", scope)
                else:
                    shape[field] = obj[field]
        if "required" in obj:
            required = obj["required"]
            if not isinstance(required, list) or not all(
                self.text(name, scope) for name in required
            ):
                self.gap("invalid-required-names", scope)
            elif len(set(required)) != len(required):
                self.gap("duplicate-required-name", scope)
            else:
                shape["required"] = sorted(required)
        self.record("schema", scope, shape)
        for field in ("properties", "$defs"):
            if field not in obj:
                continue
            if field == "$defs" and self.version != "3.1":
                self.gap("unsupported-schema-structure", scope)
                continue
            entries = self.mapping(obj[field], scope)
            for name, child in sorted(entries.items()):
                if self.text(name, scope):
                    child_scope = pointer(pointer(scope, field), name)
                    self.edge(scope, child_scope, field)
                    self.schema(child, child_scope)
        for field in ("items", "additionalProperties", "not"):
            if field in obj:
                child_scope = pointer(scope, field)
                if field == "additionalProperties" and isinstance(obj[field], bool):
                    self.record("additional-properties", scope, obj[field])
                else:
                    self.edge(scope, child_scope, field)
                    self.schema(obj[field], child_scope)
        for field in ("allOf", "anyOf", "oneOf", "prefixItems"):
            if field not in obj:
                continue
            children = obj[field]
            if not isinstance(children, list) or (
                field == "prefixItems" and self.version != "3.1"
            ):
                self.gap("unsupported-schema-structure", scope)
                continue
            self.record(
                "schema-composition", pointer(scope, field), {"members": len(children)}
            )
            for index, child in enumerate(children):
                child_scope = pointer(pointer(scope, field), index)
                self.edge(scope, child_scope, field)
                self.schema(child, child_scope)

    def content(self, value: Any, scope: str) -> None:
        for media, entry in sorted(self.mapping(value, scope).items()):
            if not self.text(media, scope):
                continue
            child_scope = pointer(scope, media)
            obj = self.mapping(entry, child_scope)
            self.record("media-type", child_scope, {})
            if "schema" in obj:
                schema_scope = pointer(child_scope, "schema")
                self.edge(child_scope, schema_scope, "schema")
                self.schema(obj["schema"], schema_scope)
            if obj.get("encoding"):
                self.gap("unsupported-encoding-structure", child_scope)

    def object(self, kind: str, value: Any, scope: str) -> None:
        self.objects[kind].add(scope)
        identity = (kind, scope)
        if identity in self.visited:
            return
        self.visited.add(identity)
        obj = self.mapping(value, scope)
        if self.reference(obj, scope, kind):
            return
        shape: dict[str, Any] = {}
        if kind in {"parameters", "headers", "requestBodies"} and "required" in obj:
            if isinstance(obj["required"], bool):
                shape["required"] = obj["required"]
            else:
                self.gap("invalid-required-flag", scope)
        if kind == "parameters":
            if self.text(obj.get("name"), scope) and obj.get("in") in {
                "query",
                "header",
                "path",
                "cookie",
            }:
                shape.update(name=obj["name"], location=obj["in"])
            else:
                self.gap("invalid-parameter", scope)
        if kind == "securitySchemes":
            scheme_type = obj.get("type")
            permitted = {"apiKey", "http", "oauth2", "openIdConnect"}
            if self.version == "3.1":
                permitted.add("mutualTLS")
            if not isinstance(scheme_type, str) or scheme_type not in permitted:
                self.gap("invalid-security-scheme", scope)
            else:
                shape["type"] = scheme_type
            # Scheme/header names and scope names are structural identifiers;
            # no server, token, authorization, discovery URL or text is read.
            if scheme_type == "apiKey":
                if self.text(obj.get("name"), scope) and obj.get("in") in {
                    "query",
                    "header",
                    "cookie",
                }:
                    shape.update(name=obj["name"], location=obj["in"])
                else:
                    self.gap("invalid-security-scheme", scope)
            if scheme_type == "http":
                if self.text(obj.get("scheme"), scope):
                    shape["scheme"] = obj["scheme"]
            if scheme_type == "oauth2":
                flows = self.mapping(obj.get("flows"), scope)
                for flow, details in sorted(flows.items()):
                    if flow.startswith("x-"):
                        continue
                    if flow not in {
                        "implicit",
                        "password",
                        "clientCredentials",
                        "authorizationCode",
                    }:
                        self.gap("invalid-oauth-flow", scope)
                        continue
                    flow_scope = pointer(pointer(scope, "flows"), flow)
                    scopes = self.mapping(
                        self.mapping(details, flow_scope).get("scopes"), flow_scope
                    )
                    names = [name for name in scopes if self.text(name, flow_scope)]
                    self.record("oauth-flow", flow_scope, sorted(names))
        self.record(kind, scope, shape)
        if "schema" in obj and kind in {"parameters", "headers"}:
            child_scope = pointer(scope, "schema")
            self.edge(scope, child_scope, "schema")
            self.schema(obj["schema"], child_scope)
        if "content" in obj and kind in {
            "parameters",
            "headers",
            "requestBodies",
            "responses",
        }:
            self.content(obj["content"], pointer(scope, "content"))
        if "headers" in obj and kind == "responses":
            for name, header in sorted(self.mapping(obj["headers"], scope).items()):
                if self.text(name, scope):
                    self.object(
                        "headers", header, pointer(pointer(scope, "headers"), name)
                    )
        if kind == "responses" and obj.get("links"):
            self.gap("unsupported-link-structure", scope)

    def security(self, value: Any, scope: str) -> list[Any] | None:
        if not isinstance(value, list):
            self.gap("invalid-security-requirements", scope)
            return None
        groups: list[Any] = []
        for group in value:
            if not isinstance(group, dict):
                self.gap("invalid-security-requirements", scope)
                continue
            members = []
            for scheme, scopes in sorted(group.items()):
                if not self.text(scheme, scope):
                    continue
                target = pointer("#/components/securitySchemes", scheme)
                if target not in self.objects["securitySchemes"]:
                    self.gap("undeclared-security-scheme", scope)
                if not isinstance(scopes, list) or not all(
                    self.text(name, scope) for name in scopes
                ):
                    self.gap("invalid-security-scopes", scope)
                    continue
                if self.version == "3.0" and scopes:
                    definition = self.component("securitySchemes", target, scope)
                    if definition is not None and definition.get("type") not in {
                        "oauth2",
                        "openIdConnect",
                    }:
                        self.gap("invalid-security-scopes", scope)
                members.append([scheme, sorted(set(scopes))])
            groups.append(members)
        return sorted(groups, key=canonical)

    def component(self, kind: str, target: str, scope: str) -> dict[str, Any] | None:
        """Follow only registered component objects; schema cycles stay edges."""
        seen: set[str] = set()
        while target in self.objects[kind]:
            if target in seen:
                self.gap("cyclic-component-reference", scope)
                return None
            seen.add(target)
            value = self.lookup(target)
            if not isinstance(value, dict):
                self.gap("invalid-reference-target", scope)
                return None
            if "$ref" not in value:
                return value
            next_target = self.ref_pointer(value["$ref"], target)
            if next_target is None:
                return None
            target = next_target
        self.gap("invalid-reference-target", scope)
        return None

    def parameters(self, value: Any, scope: str) -> None:
        if not isinstance(value, list):
            self.gap("invalid-parameters", scope)
            return
        for index, item in enumerate(value):
            self.object("parameters", item, pointer(scope, index))

    def path_item(
        self, value: Any, scope: str, chain: frozenset[str] = frozenset()
    ) -> None:
        obj = self.mapping(value, scope)
        if any(
            key
            not in METHODS | {"$ref", "summary", "description", "servers", "parameters"}
            and not key.startswith("x-")
            for key in obj
        ):
            self.gap("unsupported-path-item-field", scope)
        if "$ref" in obj:
            target = self.ref_pointer(obj["$ref"], scope)
            if any(key in obj for key in METHODS | {"parameters"}):
                self.gap("ambiguous-path-item-reference", scope)
                return
            if target is None:
                return
            if target in chain:
                self.gap("cyclic-path-item-reference", scope)
                return
            allowed = self.objects["pathItems"]
            paths = self.document.get("paths", {})
            if isinstance(paths, dict):
                allowed = allowed | {
                    pointer("#/paths", p) for p in paths if p.startswith("/")
                }
            if target not in allowed:
                self.gap("invalid-reference-target", scope)
                return
            self.edge(scope, target, "reference")
            self.path_item(self.lookup(target), scope, chain | {target})
            return
        self.record("path", scope, {})
        if "parameters" in obj:
            self.parameters(obj["parameters"], pointer(scope, "parameters"))
        for method in sorted(METHODS & obj.keys()):
            operation_scope = pointer(scope, method)
            operation = self.mapping(obj[method], operation_scope)
            self.record("operation", operation_scope, {"method": method})
            declared = "security" in operation
            effective = operation.get("security", self.document.get("security"))
            groups = (
                self.security(effective, operation_scope)
                if effective is not None
                else None
            )
            self.record(
                "security-attachment",
                operation_scope,
                {
                    "declaration": "explicit"
                    if declared
                    else "inherited"
                    if "security" in self.document
                    else "omitted",
                    "alternatives": groups,
                },
            )
            if declared and operation["security"] is None:
                self.gap("invalid-security-requirements", operation_scope)
            if "parameters" in operation:
                self.parameters(
                    operation["parameters"], pointer(operation_scope, "parameters")
                )
            if "requestBody" in operation:
                self.object(
                    "requestBodies",
                    operation["requestBody"],
                    pointer(operation_scope, "requestBody"),
                )
            if "responses" in operation:
                for status, response in sorted(
                    self.mapping(operation["responses"], operation_scope).items()
                ):
                    if status.startswith("x-"):
                        continue
                    if (
                        status != "default"
                        and re.fullmatch(r"[1-5](?:\d{2}|XX)", status) is None
                    ):
                        self.gap("invalid-response-status", operation_scope)
                        continue
                    self.object(
                        "responses",
                        response,
                        pointer(pointer(operation_scope, "responses"), status),
                    )
            if operation.get("callbacks"):
                self.gap("unsupported-callback-structure", operation_scope)

    def lookup(self, scope: str) -> Any:
        value: Any = self.document
        for token in scope[2:].split("/"):
            name = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, dict):
                value = value[name]
            elif (
                isinstance(value, list)
                and name.isdecimal()
                and (name == "0" or not name.startswith("0"))
            ):
                value = value[int(name)]
            else:
                raise ValueError("invalid pointer target")
        return value

    def traverse(self) -> None:
        components = self.mapping(self.document.get("components", {}), "#/components")
        for kind in COMPONENTS:
            values = self.mapping(
                components.get(kind, {}), pointer("#/components", kind)
            )
            if kind == "pathItems" and values and self.version != "3.1":
                self.gap("unsupported-component-path-items")
                continue
            for name in values:
                if self.text(name, pointer("#/components", kind)):
                    self.objects[kind].add(pointer(pointer("#/components", kind), name))
        for kind in COMPONENTS:
            for scope in sorted(self.objects[kind]):
                value = self.lookup(scope)
                if kind == "schemas":
                    self.schema(value, scope)
                elif kind == "pathItems":
                    self.path_item(value, scope)
                else:
                    self.object(kind, value, scope)
        if "security" in self.document:
            self.record(
                "security-declaration",
                "#",
                self.security(self.document["security"], "#"),
            )
        else:
            self.record("security-declaration", "#", {"omitted": True})
        if "paths" not in self.document and self.version == "3.0":
            self.gap("missing-paths")
        for path, value in sorted(
            self.mapping(self.document.get("paths", {}), "#/paths").items()
        ):
            if path.startswith("x-"):
                continue
            if not self.text(path, "#/paths") or not path.startswith("/"):
                self.gap("invalid-path", "#/paths")
                continue
            self.path_item(value, pointer("#/paths", path))
        if self.document.get("webhooks") or components.get("callbacks"):
            self.gap("unsupported-callback-or-webhook-structure")
        for source, target, kind in self.references:
            allowed = self.schemas if kind == "schemas" else self.objects[kind]
            if target not in allowed:
                self.gap("invalid-reference-target", source)
            else:
                try:
                    self.lookup(target)
                except (KeyError, IndexError, ValueError):
                    # Expanded Path Items have effective observation locations,
                    # but those locations do not create physical JSON pointers.
                    self.gap("invalid-reference-target", source)
                    continue
                self.edge(source, target, "reference")
                if kind != "schemas":
                    self.component(kind, target, source)

    def measure(self) -> dict[str, Any]:
        if self.load():
            try:
                self.traverse()
            except (RecursionError, KeyError, IndexError, ValueError, TypeError):
                self.gap("invalid-or-complex-structure")
        self.record(
            "scope",
            "#",
            {
                "definition": "openapi-artifact-v1",
                "artifact": self.relative,
                "openapi": self.version,
                "freshness": "not-assessed",
                "runtime_authorization": "not-assessed",
            },
        )
        result = {
            "observations": [
                self.observations[key] for key in sorted(self.observations)
            ],
            "coverage": {
                "status": "incomplete" if self.gaps else "complete",
                "gaps": [
                    {"code": code, "scope": scope} for code, scope in sorted(self.gaps)
                ],
            },
        }
        if len(canonical(result).encode()) > MAX_OUTPUT_BYTES:
            result["observations"] = []
            result["coverage"] = {
                "status": "incomplete",
                "gaps": [{"code": "output-limit", "scope": "#"}],
            }
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure structural declarations in a local OpenAPI JSON artifact without running the application."
    )
    parser.add_argument(
        "--root", required=True, help="Project root containing the artifact"
    )
    parser.add_argument(
        "--artifact", required=True, help="JSON artifact path relative to root"
    )
    args = parser.parse_args(argv)
    result = Reader(Path(args.root), Path(args.artifact)).measure()
    print(canonical(result))
    if result["coverage"]["status"] != "complete":
        sample = canonical(result["coverage"]["gaps"][:2])
        print(
            f"OpenAPI coverage incomplete: {sample[:260]}. Run python -I -m vibe_sentinel.probes.openapi --root . --artifact <file.json> for full JSON coverage.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
