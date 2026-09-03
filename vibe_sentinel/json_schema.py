"""JSON-schema helpers for constrained decoding.

Schemas go to a range of backends, and the weakest common denominator
sets the rules. Two transforms cover it:

  - **Inline ``$ref``.** Pydantic emits ``$defs`` and references into
    them; several constrained decoders resolve neither and either error
    or silently ignore the constraint.
  - **Strip bounds.** ``maxLength`` / ``maxItems`` / ``pattern`` are
    accepted by some grammar compilers and catastrophic for others —
    on one stack a single ``maxLength`` took 30/30 successful requests
    to 0/30, every one hitting the client timeout. The Pydantic model
    still carries the bounds and still validates against them after
    decoding, so nothing is actually unchecked; only the wire schema is
    simplified.
"""

from __future__ import annotations

from typing import Any, get_args

from pydantic import BaseModel


def inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generate a JSON schema with ``$ref`` references inlined.

    Also strips Pydantic metadata (``title``) to keep the schema small,
    and marks every property required — see :func:`_require_all`. For
    models carrying ``Field(max_length=...)`` or list-length bounds, use
    :func:`unbounded_schema` instead.
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def _resolve(obj: Any) -> Any:
        if isinstance(obj, dict):
            if "$ref" in obj:
                ref_name = obj["$ref"].rsplit("/", 1)[-1]
                return _resolve(defs[ref_name])
            return {k: _resolve(v) for k, v in obj.items() if k != "title"}
        if isinstance(obj, list):
            return [_resolve(v) for v in obj]
        return obj

    return _require_all(_resolve(raw))


def _require_all(node: Any) -> Any:
    """Mark every property of every object required, recursively.

    Pydantic marks a field with a default as *not* required, and every
    field of every model here has one. That is right for Python and wrong
    on the wire: a decoder held to this schema is then free to emit
    ``{"reason": "..."}`` and leave out the verdict, and what comes back
    is the field's default — a judgement nobody made, indistinguishable
    from one they did. It is also what ``strict: true`` means in the
    OpenAI schema dialect this is sent under, which requires every key be
    listed.

    The defaults stay where they belong, on the Pydantic model, for the
    answers that never arrive at all.
    """
    if isinstance(node, dict):
        out = {k: _require_all(v) for k, v in node.items()}
        properties = out.get("properties")
        if isinstance(properties, dict) and properties:
            out["required"] = list(properties)
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(node, list):
        return [_require_all(v) for v in node]
    return node


# Constraint keys stripped by ``unbounded_schema``. Support for these
# ranges from "fine" to "collapses throughput" across grammar compilers,
# and there is no way to know which one is on the other end of an
# OpenAI-compatible URL. The Pydantic model still carries them for
# post-decode validation; they simply do not go on the wire.
_BANNED_SCHEMA_KEYS = frozenset(
    {
        "maxLength",
        "minLength",
        "maxItems",
        "minItems",
        "pattern",
        "multipleOf",
        "maximum",
        "minimum",
        "exclusiveMaximum",
        "exclusiveMinimum",
    }
)


def _strip_banned_keys(obj: Any) -> Any:
    """Recursively strip bounds keys from a schema tree.

    Returns a new structure; the input is not mutated.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_banned_keys(v)
            for k, v in obj.items()
            if k not in _BANNED_SCHEMA_KEYS
        }
    if isinstance(obj, list):
        return [_strip_banned_keys(v) for v in obj]
    return obj


def unbounded_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generate a schema with length / count / pattern bounds stripped.

    Use this **instead of** :func:`inline_schema` when the Pydantic model
    carries ``Field(max_length=...)`` or list-length bounds. The model
    still validates post-decode; only the wire schema is simplified.
    """
    return _strip_banned_keys(inline_schema(model))


def clip_to_bounds(model: type[BaseModel], data: Any) -> Any:
    """Clip an answer to the model's bounds instead of rejecting it.

    The wire schema is unbounded, so a model can and does emit a longer
    string or one more list item than it was asked for. Validating that
    strictly throws the whole answer away over a note that ran three
    words long, which is the wrong trade: the ratings in it were fine.

    So the bounds are enforced here, before ``model_validate``, and the
    Pydantic bound behind it becomes what it should be — the check that
    catches a bug in this function, not the thing standing between a
    good answer and the report.
    """
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            # A key the model invented. Left alone: rejecting it is
            # model_validate's job and it has the better error message.
            out[key] = value
            continue
        out[key] = _clip(value, field.annotation, _max_length(field))
    return out


def _max_length(field: Any) -> int | None:
    """The ``max_length`` a field carries, or None."""
    for constraint in getattr(field, "metadata", ()):
        limit = getattr(constraint, "max_length", None)
        if limit is not None:
            return int(limit)
    return None


def _clip(value: Any, annotation: Any, limit: int | None) -> Any:
    """Clip one value, recursing into a list of submodels."""
    if isinstance(value, str):
        return value if limit is None else value[:limit]
    if isinstance(value, list):
        clipped = value if limit is None else value[:limit]
        item = _item_model(annotation)
        if item is None:
            return clipped
        return [clip_to_bounds(item, entry) for entry in clipped]
    return value


def _item_model(annotation: Any) -> type[BaseModel] | None:
    """The submodel a ``list[...]`` holds, when it holds one."""
    for arg in get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None
