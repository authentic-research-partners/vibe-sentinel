"""The two schema transforms that make "any backend" true.

Both exist because of what a constrained decoder on the other end of an
OpenAI-compatible URL might do with a schema, and neither weakens what is
actually checked: the Pydantic model still validates the decoded answer,
so the bounds are enforced after the wire rather than on it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from vibe_sentinel.json_schema import (
    clip_to_bounds,
    inline_schema,
    unbounded_schema,
)


class Inner(BaseModel):
    name: str
    weight: int = Field(default=1, ge=0, le=10)


class Outer(BaseModel):
    verdict: Literal["yes", "no"] = "no"
    inner: Inner
    tags: list[str] = Field(default_factory=list, max_length=4)
    note: str = Field(default="", max_length=200)


def _walk(node: object) -> list[object]:
    """Every dict and list in a schema tree, itself included."""
    found: list[object] = [node]
    if isinstance(node, dict):
        for value in node.values():
            found += _walk(value)
    elif isinstance(node, list):
        for value in node:
            found += _walk(value)
    return found


def test_refs_are_inlined() -> None:
    """Several constrained decoders resolve neither ``$ref`` nor ``$defs``,
    and either error or drop the constraint silently."""
    schema = inline_schema(Outer)
    assert "$defs" not in schema
    assert not any(isinstance(n, dict) and "$ref" in n for n in _walk(schema)), (
        "a $ref survived inlining"
    )
    assert schema["properties"]["inner"]["properties"]["name"]["type"] == "string"


def test_titles_are_stripped() -> None:
    """Pydantic metadata the decoder does not need, and pays for."""
    assert not any(
        isinstance(n, dict) and "title" in n for n in _walk(inline_schema(Outer))
    )


def test_enums_survive() -> None:
    """The constraint that actually earns its place stays on the wire."""
    schema = inline_schema(Outer)
    assert schema["properties"]["verdict"]["enum"] == ["yes", "no"]


def test_bounds_are_stripped_from_the_wire_schema() -> None:
    """One ``maxLength`` took a grammar compiler from 30/30 successful
    requests to 0/30, every one hitting the client timeout."""
    banned = {"maxLength", "minLength", "maxItems", "minItems", "maximum", "minimum"}
    schema = unbounded_schema(Outer)
    present = {k for n in _walk(schema) if isinstance(n, dict) for k in n}
    assert not (present & banned), f"bounds reached the wire: {present & banned}"


def test_the_model_still_carries_the_bounds() -> None:
    """Nothing is actually unchecked — only the wire schema is simplified,
    and validation happens after decoding."""
    assert (
        Outer.model_json_schema()["$defs"]["Inner"]["properties"]["weight"]["maximum"]
        == 10
    )


def test_stripping_bounds_keeps_the_shape() -> None:
    schema = unbounded_schema(Outer)
    assert set(schema["properties"]) == {"verdict", "inner", "tags", "note"}
    assert schema["properties"]["tags"]["type"] == "array"


def test_the_input_model_is_not_mutated() -> None:
    """Both transforms are called at import time on shared models, so one
    that mutated its input would corrupt the next caller's schema."""
    before = Outer.model_json_schema()
    unbounded_schema(Outer)
    inline_schema(Outer)
    assert Outer.model_json_schema() == before


# --- clipping an over-run --------------------------------------------------
#
# The wire schema carries no bounds, so a model answers slightly over them
# routinely. Throwing the answer away over a note that ran three words long
# is the wrong trade — the ratings in it were fine.


class Listed(BaseModel):
    items: list[Inner] = Field(default_factory=list, max_length=2)


def test_a_long_string_is_trimmed_not_rejected() -> None:
    clipped = clip_to_bounds(Outer, {"note": "x" * 500})
    assert clipped["note"] == "x" * 200
    Outer.model_validate({**clipped, "inner": {"name": "a"}})


def test_a_long_list_is_clipped() -> None:
    assert clip_to_bounds(Outer, {"tags": list("abcdef")})["tags"] == list("abcd")


def test_submodels_inside_a_list_are_clipped_too() -> None:
    """The bound that matters is on the item, not on the list holding it."""
    data = {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    assert clip_to_bounds(Listed, data) == {"items": [{"name": "a"}, {"name": "b"}]}


def test_a_field_the_model_invented_is_left_alone() -> None:
    """model_validate has the better error message for that."""
    assert clip_to_bounds(Outer, {"nonsense": "kept"}) == {"nonsense": "kept"}


def test_a_value_within_bounds_is_unchanged() -> None:
    data = {"note": "short", "tags": ["a"], "verdict": "yes"}
    assert clip_to_bounds(Outer, data) == data


def test_a_non_dict_answer_passes_through() -> None:
    assert clip_to_bounds(Outer, ["not", "an", "object"]) == ["not", "an", "object"]


# --- a wire schema demands every key ---------------------------------------
#
# The bug behind these: Pydantic marks a field with a default as *not*
# required, and every field of every LLM-facing model here has one. A
# constrained decoder held to that schema is free to emit `{"reason": "..."}`
# and leave out the verdict — and what comes back is the default, a judgement
# nobody made, indistinguishable from one they did. Observed: a near-miss pair
# the model had correctly analysed in `reason` came back with the verdict
# still at its default.


def test_every_property_is_required_on_the_wire() -> None:
    schema = unbounded_schema(Outer)
    assert set(schema["required"]) == set(schema["properties"])


def test_a_nested_submodel_is_required_too() -> None:
    inner = unbounded_schema(Outer)["properties"]["inner"]
    assert set(inner["required"]) == set(inner["properties"])


def test_nothing_outside_the_schema_is_accepted() -> None:
    assert unbounded_schema(Outer)["additionalProperties"] is False


def test_the_pydantic_defaults_survive() -> None:
    """Required on the wire, defaulted in Python.

    The two say different things: the wire demands a complete answer, and
    the default is what stands in for an answer that never arrived at all.
    """
    assert Outer(inner=Inner(name="a")).note == ""
