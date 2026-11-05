"""Language-neutral version-one dependency edge identities.

The standalone JavaScript pack implements the same compact UTF-8 JSON tuple.
This foundation module depends on no scanner or language-specific reader.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def edge_key(
    ecosystem: str, source: str, target: str, kind: str, phase: str, resolution: str
) -> str:
    """Identify a relationship without line positions or checkout paths."""
    identity = [ecosystem, source, target, kind, phase, resolution]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return "edge:1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def edge_observation(
    ecosystem: str,
    source: str,
    target: str,
    kind: str,
    phase: str,
    resolution: str,
    *,
    count: int = 1,
) -> dict[str, Any]:
    """One observed six-tuple; count is occurrences, not imported symbols."""
    return {
        "key": edge_key(ecosystem, source, target, kind, phase, resolution),
        "value": count,
        "label": f"{source} {kind} {target} ({phase})",
        "attrs": {
            "edge_schema": "1",
            "ecosystem": ecosystem,
            "source": source,
            "target": target,
            "kind": kind,
            "phase": phase,
            "resolution": resolution,
        },
    }
