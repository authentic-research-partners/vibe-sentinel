"""A structural inventory of a single declared JSON Schema 2020-12 resource."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from vibe_sentinel.probes.artifact_support import (
    Evidence,
    emit,
    json_document,
    pointer,
    read_bytes,
    relative,
    text,
)

DIALECT = "https://json-schema.org/draft/2020-12/schema"
TYPES = frozenset({"null", "boolean", "object", "array", "number", "integer", "string"})
MAPS = frozenset({"properties", "$defs", "dependentSchemas"})
SINGLES = frozenset(
    {
        "items",
        "additionalProperties",
        "not",
        "contains",
        "propertyNames",
        "if",
        "then",
        "else",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contentSchema",
    }
)
ARRAYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
IGNORED = frozenset(
    {
        "title",
        "description",
        "$comment",
        "default",
        "examples",
        "enum",
        "const",
        "format",
        "pattern",
        "deprecated",
        "readOnly",
        "writeOnly",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
        "contentEncoding",
        "contentMediaType",
    }
)
SUPPORTED = (
    MAPS
    | SINGLES
    | ARRAYS
    | {"$schema", "$id", "$ref", "type", "required", "dependentRequired"}
)


def reference(value: Any) -> str | None:
    if (
        not text(value)
        or not value.startswith("#")
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        return None
    try:
        decoded = unquote(value[1:], encoding="utf-8", errors="strict")
        if decoded == "":
            return "#"
        if not decoded.startswith("/"):
            return None
        target = "#"
        for part in decoded[1:].split("/"):
            if re.search(r"~(?![01])", part):
                return None
            name = part.replace("~1", "/").replace("~0", "~")
            if not text(name, empty=True):
                return None
            target = pointer(target, name)
        return target if text(target) else None
    except UnicodeError:
        return None


class Reader:
    def __init__(self, root: Path, artifact: str) -> None:
        self.root = root.resolve()
        self.artifact = artifact
        self.evidence = Evidence("json-schema")
        self.schemas: set[str] = set()
        self.references: list[tuple[str, str]] = []

    def names(self, value: Any, scope: str) -> list[str] | None:
        if (
            not isinstance(value, list)
            or not all(text(v, empty=True) for v in value)
            or len(set(value)) != len(value)
        ):
            self.evidence.gap("invalid-property-names", scope)
            return None
        return sorted(value)

    def schema(self, value: Any, scope: str) -> None:
        if not text(scope):
            self.evidence.gap("structural-pointer-limit")
            return
        if not isinstance(value, (dict, bool)):
            self.evidence.gap("invalid-schema", scope)
            return
        self.schemas.add(scope)
        if isinstance(value, bool):
            self.evidence.record("schema", scope, {"boolean": value})
            return
        if value.keys() - SUPPORTED - IGNORED:
            self.evidence.gap("unsupported-schema-keyword", scope)
        if "$schema" in value and (scope != "#" or value["$schema"] != DIALECT):
            self.evidence.gap("unsupported-dialect-or-resource", scope)
        if "$id" in value:
            identity = value["$id"]
            try:
                if scope != "#" or not text(identity) or urlsplit(identity).fragment:
                    raise ValueError("rebasing")
            except ValueError:
                self.evidence.gap("unsupported-resource-rebasing", scope)
        shape: dict[str, Any] = {}
        if "type" in value:
            types = [value["type"]] if isinstance(value["type"], str) else value["type"]
            if (
                not isinstance(types, list)
                or not types
                or not all(isinstance(t, str) and t in TYPES for t in types)
                or len(set(types)) != len(types)
            ):
                self.evidence.gap("invalid-schema-types", scope)
            else:
                shape["types"] = sorted(types)
        if "required" in value:
            shape["required"] = self.names(value["required"], scope)
        if "dependentRequired" in value:
            dependencies = value["dependentRequired"]
            if not isinstance(dependencies, dict):
                self.evidence.gap("invalid-dependent-required", scope)
            else:
                shape["dependent_required"] = {}
                for name, names in sorted(dependencies.items()):
                    if not text(name, empty=True):
                        self.evidence.gap("invalid-property-name", scope)
                        continue
                    shape["dependent_required"][name] = self.names(names, scope)
        self.evidence.record("schema", scope, shape)
        if "$ref" in value:
            target = reference(value["$ref"])
            if target is None:
                self.evidence.gap("unsupported-reference", scope)
            else:
                self.references.append((scope, target))
        for field in sorted(MAPS):
            if field not in value:
                continue
            children = value[field]
            if not isinstance(children, dict):
                self.evidence.gap("invalid-schema-map", scope)
                continue
            for name, child in sorted(children.items()):
                if not text(name, empty=True):
                    self.evidence.gap("invalid-property-name", scope)
                    continue
                self.child(scope, pointer(pointer(scope, field), name), field, child)
        for field in sorted(SINGLES):
            if field in value:
                self.child(scope, pointer(scope, field), field, value[field])
        for field in sorted(ARRAYS):
            if field not in value:
                continue
            children = value[field]
            if not isinstance(children, list) or not children:
                self.evidence.gap("invalid-schema-array", scope)
                continue
            for index, child in enumerate(children):
                self.child(scope, pointer(pointer(scope, field), index), field, child)

    def child(self, parent: str, location: str, kind: str, value: Any) -> None:
        self.evidence.edge(parent, location, kind)
        self.schema(value, location)

    def measure(self) -> dict[str, Any]:
        try:
            document = json_document(read_bytes(self.root, self.artifact))
            if not isinstance(document, dict) or document.get("$schema") != DIALECT:
                self.evidence.gap("explicit-2020-12-dialect-required")
            else:
                self.schema(document, "#")
                for source, target in self.references:
                    if target not in self.schemas:
                        self.evidence.gap("invalid-reference-target", source)
                    else:
                        self.evidence.edge(source, target, "reference")
        except (OSError, ValueError, UnicodeError, RecursionError):
            self.evidence.gap("unreadable-invalid-or-oversized-schema")
        self.evidence.record(
            "scope",
            "#",
            {
                "definition": "json-schema-2020-12-projection-v1",
                "artifact": self.artifact if relative(self.artifact) else "invalid",
                "freshness": "not-assessed",
                "instance_validation": "not-assessed",
                "deployed_database": "not-assessed",
            },
        )
        return self.evidence.result(
            "JSON Schema declarations; instances and deployed database not assessed"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory an existing JSON Schema 2020-12 data contract without validating instances or accessing a database."
    )
    parser.add_argument(
        "--root", required=True, help="Project root containing the artifact"
    )
    parser.add_argument(
        "--artifact",
        required=True,
        help="Existing JSON Schema artifact relative to root",
    )
    args = parser.parse_args(argv)
    return emit(Reader(Path(args.root), args.artifact).measure(), "JSON Schema")


if __name__ == "__main__":
    raise SystemExit(main())
