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


@pytest.mark.parametrize("status", [200, 400, 503])
def test_failed_model_output_is_never_logged(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "PRIVATE_MODEL_DIAGNOSTIC_DO_NOT_FORWARD"
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0)
    messages: list[str] = []
    sink = llm_mod.logger.add(lambda message: messages.append(str(message)))
    try:

        def respond(_: httpx.Request) -> httpx.Response:
            if status == 200:
                return httpx.Response(status, json=_completion(secret))
            return httpx.Response(status, text=secret)

        if status == 503:
            with pytest.raises(LLMConnectionError) as exc:
                _ask(respond)
            assert secret not in str(exc.value)
        else:
            assert _ask(respond) is None
    finally:
        llm_mod.logger.remove(sink)
    assert messages
    assert secret not in "".join(messages)


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
    assert "not optional" in message


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


@pytest.mark.parametrize(
    ("endpoint", "trust_env"),
    [
        ("http://127.0.0.1:11434/v1", False),
        ("http://[::1]:11434/v1", False),
        ("http://localhost:11434/v1", False),
        ("https://remote.example/v1", True),
    ],
)
def test_local_judge_bypasses_environment_proxies(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, trust_env: bool
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "")
    seen = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test"}]})
        return httpx.Response(200, json=_completion('{"ok": true}'))

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        seen.append(kwargs.get("trust_env"))
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)
    config = SentinelConfig(llm_endpoint=endpoint)
    assert asyncio.run(check_endpoint(config))[0]
    assert asyncio.run(llm_query("sys", "user", SCHEMA, "test", config)) == {"ok": True}
    assert seen == [trust_env, trust_env]


def test_check_endpoint_lists_served_models(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "qwen3-8b-fp8"}]})

    _patch_async_client(monkeypatch, handler)
    reachable, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert reachable
    assert "qwen3-8b-fp8" in detail


def test_check_endpoint_reads_a_null_model_list_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama with nothing pulled answers ``data: null``, not ``data: []``.

    The key is present and null, so ``.get("data", [])`` returns None and
    iterating it raises — which used to render as "an unexpected shape".
    That is the documented first-run state on the macOS path, where the
    next instruction is to pull a model, and the message sent the reader
    looking for a protocol fault instead of at an empty backend.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": None})

    _patch_async_client(monkeypatch, handler)
    reachable, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert reachable, "a server that answered is reachable, empty or not"
    assert "serving no models" in detail
    assert "unexpected shape" not in detail
    assert "[llm] model" in detail, "an empty backend has a remediation"


def test_check_endpoint_reads_an_empty_model_list_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``[]`` and ``null`` are one state: a list endpoint listing nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": []})

    _patch_async_client(monkeypatch, handler)
    _, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert "serving no models" in detail


def test_check_endpoint_still_refuses_a_genuinely_broken_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening the empty case must not swallow a real protocol fault."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"name": "no-id-field"}]})

    _patch_async_client(monkeypatch, handler)
    _, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert "unexpected shape" in detail


def test_check_endpoint_reports_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _patch_async_client(monkeypatch, handler)
    reachable, detail = asyncio.run(check_endpoint(SentinelConfig()))
    assert not reachable
    assert "ConnectError" in detail


def test_an_unreachable_backend_fails_fast_rather_than_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`llm_timeout` is how long the model may think, not how long a socket
    may take to open. It defaults to 300s, and applied to the connect phase
    that made an unreachable backend hang for five minutes per attempt,
    three attempts deep, before printing the error that names the fix.

    With the model no longer optional, that is the failure path a first run
    hits — so it is the one that has to be quick.
    """
    monkeypatch.setattr(llm_mod, "_RETRY_BACKOFF_S", 0.0)
    seen: dict[str, object] = {}
    real = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        seen["timeout"] = kwargs.get("timeout")
        # A transport that refuses at once, so the assertion is about the
        # configured bound rather than about waiting for a real one.
        return real(transport=httpx.MockTransport(_refuse))

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", spy)

    config = SentinelConfig(llm_timeout=300.0)
    with pytest.raises(LLMConnectionError):
        asyncio.run(llm_query("sys", "user", SCHEMA, "test", config=config))

    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect is not None and timeout.connect <= 15.0
    assert timeout.read == 300.0, "the model still gets its full budget to think"


# --- Who owns the answer budget ---------------------------------------------
#
# `[llm] max_tokens` is a ceiling, not a reservation: lowering it never makes
# an answer shorter, it only decides when a long one is cut off — and a cut-off
# answer is retried at double the budget, so one over-run costs two prefills
# and two generations for a single answer. Brevity is asked for in the prompt,
# where a model can act on it.
#
# `answer_budget` states the rule the other way round: the configured value is
# a *floor*, because a reasoning model spends tokens before the answer starts,
# so whoever raised it for that reason keeps what they set. Three gates used to
# clamp it to 512 regardless, which broke that silently.


def test_no_gate_clamps_the_configured_answer_budget() -> None:
    import inspect
    import re

    from vibe_sentinel import credentials, packages, safety

    for module in (credentials, packages, safety):
        source = inspect.getsource(module)
        hits = re.findall(r'"max_tokens":\s*(\d+)', source)
        assert not hits, (
            f"{module.__name__} pins max_tokens to {hits} instead of taking "
            f"the configured value. A ceiling below the config silently "
            f"overrides a user who raised it for a reasoning model, and pays "
            f"a retry every time an answer runs past it."
        )


def test_the_drift_pass_may_raise_it_because_it_computes_one() -> None:
    """The one call site allowed to move it, and it only ever moves up."""
    from vibe_sentinel.analyze import answer_budget

    for floor in (512, 2048, 8192):
        assert answer_budget(1, floor) >= floor
        assert answer_budget(200, floor) >= floor


def test_the_credentials_gate_asks_at_the_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour behind the source check above."""
    from vibe_sentinel import credentials as creds
    from vibe_sentinel import llm as llm_mod

    seen: list[int] = []

    async def capture(
        system: object,
        user: object,
        schema: object,
        schema_name: str,
        config: SentinelConfig | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        assert config is not None
        seen.append(config.max_tokens)
        return {"verdict": "placeholder", "reason": "test"}

    monkeypatch.setattr(llm_mod, "llm_query", capture)

    secrets = creds.builtin_secrets()
    rule = secrets[0]
    candidate = creds.Candidate(
        rule=rule.id,
        title=rule.title,
        path="config.py",
        exposure="tracked",
        local_excerpt="TOKEN = 'x'",
    )
    config = SentinelConfig(max_tokens=4096)
    asyncio.run(creds.review((candidate,), secrets, config, creds.Policy()))
    assert seen == [4096], (
        f"the gate asked at {seen}, not the configured 4096 — a budget below "
        f"the config is a clamp on a user who raised it deliberately"
    )


# --- an unreachable endpoint is one fact, not one per request --------------
#
# Every fan-out here is `asyncio.gather(..., return_exceptions=True)`, so one
# bad answer does not lose the rest. A dead backend is not one bad answer:
# each request discovers the same fact, so it arrives once per item, and
# folding it into the per-item "no answer" path turns a total outage into an
# ordinary shortfall. `llm.raise_if_unreachable` owns the distinction; these
# are what fails without it at each caller.


def test_raise_if_unreachable_reraises_the_connection_error() -> None:
    from vibe_sentinel.exceptions import LLMConnectionError
    from vibe_sentinel.llm import raise_if_unreachable

    boom = LLMConnectionError("endpoint unreachable after 3 attempts")
    with pytest.raises(LLMConnectionError, match="unreachable"):
        raise_if_unreachable([("a", {}), boom, ValueError("a different failure")])


def test_raise_if_unreachable_leaves_an_ordinary_failure_alone() -> None:
    """One bad answer stays one bad answer. Raising on those would lose
    the other items, which is the reason for `return_exceptions=True`."""
    from vibe_sentinel.llm import raise_if_unreachable

    raise_if_unreachable([("a", {}), ValueError("one bad answer"), None])
