"""The OpenAI-compatible client, with the network mocked.

These pin the behaviour that makes "any backend" true: the three
structured-output modes, salvaging JSON from a chatty response, retrying
only what is worth retrying, and failing loudly when the endpoint is
simply not there.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from vibe_sentinel import llm as llm_mod
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError
from vibe_sentinel.llm import _extract_json, check_endpoint, llm_query

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def _completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ask(handler, config: SentinelConfig | None = None):
    async def run():
        async with _client(handler) as client:
            return await llm_query(
                "sys", "user", SCHEMA, "test", config=config, client=client
            )

    return asyncio.run(run())


# --- JSON extraction -------------------------------------------------------


def test_plain_json_parses() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_json_is_salvaged_from_surrounding_prose() -> None:
    """In `none` mode models wrap JSON in commentary or a fence. The
    alternative to salvaging it is discarding a good answer."""
    assert _extract_json('Here you go:\n```json\n{"a": 1}\n```\nHope that helps') == {
        "a": 1
    }


def test_non_object_json_is_rejected() -> None:
    assert _extract_json("[1, 2, 3]") is None
    assert _extract_json('"just a string"') is None


def test_unparseable_text_is_none() -> None:
    assert _extract_json("I cannot help with that") is None
    assert _extract_json("") is None


# --- structured output modes ----------------------------------------------


def test_json_schema_mode_sends_the_schema() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion('{"ok": true}'))

    result = _ask(handler, SentinelConfig(structured_output="json_schema"))
    assert result == {"ok": True}
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["schema"] == SCHEMA
    assert captured["response_format"]["json_schema"]["strict"] is True


def test_json_object_mode_sends_the_weaker_constraint() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion('{"ok": true}'))

    _ask(handler, SentinelConfig(structured_output="json_object"))
    assert captured["response_format"] == {"type": "json_object"}


def test_none_mode_sends_no_response_format_and_inlines_the_schema() -> None:
    """A backend that supports neither still needs to know the shape, so
    the schema moves into the system prompt."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion('{"ok": true}'))

    _ask(handler, SentinelConfig(structured_output="none"))
    assert "response_format" not in captured
    assert "properties" in captured["messages"][0]["content"]


def test_extra_body_is_passed_through() -> None:
    """The escape hatch: backend-specific knobs without backend-specific
    code in the client."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion("{}"))

    _ask(
        handler,
        SentinelConfig(extra_body={"chat_template_kwargs": {"enable_thinking": True}}),
    )
    assert captured["chat_template_kwargs"] == {"enable_thinking": True}


def test_api_key_becomes_a_bearer_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_completion("{}"))

    _ask(handler, SentinelConfig(api_key="sk-test"))
    assert seen["auth"] == "Bearer sk-test"


def test_no_api_key_sends_no_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=_completion("{}"))

    _ask(handler)
    assert seen["auth"] == ""


# --- failure handling ------------------------------------------------------


def test_4xx_is_not_retried_and_returns_none() -> None:
    """A rejected request will be rejected identically next time; the
    useful response is a message naming structured_output."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text="unknown field response_format")

    assert _ask(handler) is None
    assert attempts["n"] == 1


def test_5xx_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="loading model")

    with pytest.raises(LLMConnectionError, match="unreachable"):
        _ask(handler)
    assert attempts["n"] == llm_mod._MAX_ATTEMPTS


def test_transient_failure_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend still loading weights answers 503 first, then works."""
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="loading")
        return httpx.Response(200, json=_completion('{"ok": true}'))

    assert _ask(handler) == {"ok": True}


def test_connection_error_raises_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMConnectionError) as exc:
        _ask(handler)
    message = str(exc.value)
    assert "backend status" in message
    assert "--no-model" in message


def test_empty_content_is_retried_then_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local server under a fan-out drops one occasionally.

    Giving up on the first is how a lens that had something to say ends up
    recorded as not having answered — and one more small request is the
    whole cost of finding out.
    """
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, json=_completion(""))

    assert _ask(handler) is None
    assert attempts["n"] == llm_mod._MAX_ATTEMPTS


def test_empty_content_then_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(200, json=_completion(""))
        return httpx.Response(200, json=_completion('{"ok": true}'))

    assert _ask(handler) == {"ok": True}


def test_reasoning_only_response_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasoning models that exhaust max_tokens mid-thought return empty
    content and a full reasoning field."""
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "", "reasoning_content": "..."}}]
            },
        )

    assert _ask(handler) is None


def test_no_choices_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    assert _ask(handler) is None


def test_non_json_content_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("I refuse"))

    assert _ask(handler) is None


# --- a partial answer is not a small answer --------------------------------


def _truncated(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}, "finish_reason": "length"}]}


def test_a_truncated_answer_is_discarded_even_when_it_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the parse cannot see.

    A constrained decoder emits valid JSON right up to the token it is cut
    off at, so a fragment that happens to close its braces parses cleanly
    and is a different answer from the one the model was giving. Only
    ``finish_reason`` says so.
    """
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json=_truncated('{"ok": true}'))

    assert _ask(handler) is None
    assert sent == [2048, 4096], "one escalation, not a ladder"


def test_a_truncated_answer_is_retried_with_more_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying the same request at the same ceiling fails the same way."""
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        if len(sent) == 1:
            return httpx.Response(200, json=_truncated('{"ok": fal'))
        return httpx.Response(200, json=_completion('{"ok": true}'))

    assert _ask(handler) == {"ok": True}
    assert sent == [2048, 4096]


def test_a_finished_answer_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stop` is the ordinary case and must not pay for the check."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
                ]
            },
        )

    assert _ask(handler) == {"ok": True}


def test_prose_is_not_salvaged_when_the_server_was_told_to_constrain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a constraint, prose around the answer is a broken constraint.

    Going looking for braces inside a stream that was supposed to be JSON
    is how a partial answer becomes a confident wrong one.
    """
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('sure thing: {"ok": true}'))

    assert _ask(handler, SentinelConfig(structured_output="json_schema")) is None
    assert _ask(handler, SentinelConfig(structured_output="none")) == {"ok": True}


# --- endpoint probe --------------------------------------------------------


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Point httpx.AsyncClient at a mock transport.

    The real class is captured first: `llm_mod.httpx` is the httpx module
    itself, so building the replacement lazily would recurse into the
    patched attribute.
    """
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("timeout", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)


def test_check_endpoint_lists_served_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "qwen3-8b-fp8"}]})

    _patch_async_client(monkeypatch, handler)
    reachable, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert reachable
    assert "qwen3-8b-fp8" in detail


def test_check_endpoint_reports_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_async_client(monkeypatch, handler)
    reachable, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert not reachable
    assert "ConnectError" in detail
