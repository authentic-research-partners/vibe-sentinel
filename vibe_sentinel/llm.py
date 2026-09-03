"""Talking to the local model.

One function: ask an OpenAI-compatible endpoint a question, get back
parsed JSON. That is the entire surface, because it is all this tool
needs — a scan asks each declared lens about the drift report and each
credential candidate about itself, and none of those is large. Measuring
asks nothing: probes are subprocesses.

**What counts as an answer.** Two things, and conflating them is how a
partial answer gets treated as a small one: the content channel has to
carry something, *and* the server has to have stopped because the model
finished rather than because it ran out of room. A constrained decoder
emits valid-looking JSON right up to the token it is cut off at, so the
second half is only visible in ``finish_reason`` — never in the parse.
Anything else is retried, and a truncation is retried with more room,
because retrying it unchanged fails the same way.

Any OpenAI-compatible server works: vLLM, Ollama, llama.cpp's server,
LM Studio, LocalAI, text-generation-webui. Nothing here is specific to
one of them. That matters for the independence claim — the point is a
judge from a *different model family* than whatever wrote your code, and
tying that to one serving stack narrows the field for no benefit.

**Structured output.** Backends differ in how strictly they can be held
to a JSON shape, so ``structured_output`` picks the strongest mode yours
supports:

  - ``json_schema`` — the server constrains decoding to the schema.
    Best when available (vLLM, llama.cpp, LM Studio, recent Ollama).
  - ``json_object`` — the server guarantees valid JSON but not the
    shape. Widely supported.
  - ``none`` — plain text; the schema goes in the prompt instead.

Under every mode the response is validated against the Pydantic model by
the caller, so a backend that ignores the constraint produces a rejected
answer rather than a wrong one.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from loguru import logger

from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError

#: Retries for transient failures — a connection refused, a timeout, a
#: 5xx while the server loads a model. Not for 4xx, which means the
#: request itself is wrong and will be wrong again.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: A truncated answer is one that needed more room, so the retry gives it
#: some: retrying the same request at the same ceiling is deterministic
#: failure. One doubling, not a ladder — measured against the case this
#: was written for, a drift lens whose answer overran a 2048-token budget
#: by under a factor of two. A prompt whose answer still will not fit is
#: one to make smaller, and the message that gives up says so.
_TRUNCATION_ESCALATION = 2

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str, salvage: bool = True) -> dict[str, Any] | None:
    """Parse a JSON object out of a model response.

    Tries the whole string first. Only when that fails does it look for
    an embedded object — models in ``none`` mode wrap JSON in prose or a
    ``json`` fence, and the alternative to salvaging it is discarding an
    otherwise-good answer.

    ``salvage`` is off wherever the server was told to constrain the
    output. There, anything that is not already a JSON object is a
    failure of the constraint rather than prose around a good answer, and
    a regex that goes looking for braces inside a broken stream turns a
    partial answer into a confident wrong one.
    """
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    if not salvage:
        return None

    match = _JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _response_format(
    mode: str, schema: dict[str, Any], schema_name: str
) -> dict[str, Any] | None:
    """Build the ``response_format`` body field for the configured mode."""
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": True},
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def _system_with_schema(system: str, mode: str, schema: dict[str, Any]) -> str:
    """Append the schema to the system prompt when the server can't enforce it.

    In ``none`` mode this is the only thing steering the shape, so the
    schema has to be visible to the model rather than only to the caller.
    """
    if mode != "none":
        return system
    return (
        f"{system}\n\nReturn ONLY a JSON object matching this schema, with no "
        f"prose before or after it:\n{json.dumps(schema, indent=2)}"
    )


async def llm_query(
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str,
    config: SentinelConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Ask the model one question; return the parsed JSON object.

    Returns None when the model answers with something unparseable — the
    callers treat that as "no answer" and carry on with placeholder
    defaults or mechanical severities, because a scan that reports
    structure without the model's commentary is far more useful than no
    scan at all.

    Raises :class:`LLMConnectionError` only when the endpoint itself is
    unreachable after retries, which is a setup problem the user needs
    told about rather than degraded past.
    """
    config = config or SentinelConfig()
    mode = config.structured_output

    body: dict[str, Any] = {
        "model": config.llm_model,
        "messages": [
            {"role": "system", "content": _system_with_schema(system, mode, schema)},
            {"role": "user", "content": user},
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    response_format = _response_format(mode, schema, schema_name)
    if response_format is not None:
        body["response_format"] = response_format
    # Escape hatch for backend-specific knobs — a reasoning toggle, a
    # sampler setting. Passed through untouched so no backend needs
    # support added here.
    body.update(config.extra_body)

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    url = config.llm_endpoint.rstrip("/") + "/chat/completions"
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=config.llm_timeout)
    assert client is not None  # narrowing for mypy

    try:
        last_error = ""
        escalated = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "{}: request failed (attempt {}/{}): {}",
                    schema_name,
                    attempt,
                    _MAX_ATTEMPTS,
                    last_error,
                )
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BACKOFF_S * attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "{}: server busy or starting (attempt {}/{}): {}",
                    schema_name,
                    attempt,
                    _MAX_ATTEMPTS,
                    last_error,
                )
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BACKOFF_S * attempt)
                continue

            if response.status_code >= 400:
                # 4xx is a malformed request — the model name is wrong,
                # or the backend rejects this response_format. Retrying
                # produces the same error, so say what happened instead.
                logger.error(
                    "{}: request rejected (HTTP {}): {}\n"
                    "  endpoint: {}\n  model: {}\n  structured_output: {}\n"
                    "  If the backend does not support this mode, set "
                    "[llm] structured_output to 'json_object' or 'none'.",
                    schema_name,
                    response.status_code,
                    response.text[:300],
                    config.llm_endpoint,
                    config.llm_model,
                    mode,
                )
                return None

            payload = response.json()
            if _truncated(payload):
                # A constrained decoder emits *partial* JSON before it
                # runs out, so this has to be caught by the finish
                # reason rather than by the parse: a fragment that
                # happens to close its braces parses cleanly and is a
                # different answer from the one the model was giving.
                # Never claim a review that did not happen applies to
                # half of one too.
                ceiling = int(body["max_tokens"])
                if not escalated and attempt < _MAX_ATTEMPTS:
                    escalated = True
                    body["max_tokens"] = ceiling * _TRUNCATION_ESCALATION
                    logger.warning(
                        "{}: answer truncated at {} tokens; retrying with {}",
                        schema_name,
                        ceiling,
                        body["max_tokens"],
                    )
                    continue
                logger.error(
                    "{}: answer truncated at {} tokens and discarded — a "
                    "partial answer is not a smaller answer.\n"
                    "  Raise [llm] max_tokens (currently {}), or ask about "
                    "less at once.",
                    schema_name,
                    ceiling,
                    config.max_tokens,
                )
                return None
            parsed = _parse_completion(payload, schema_name, mode)
            if parsed is not None:
                return parsed
            # Empty content and a broken parse are both glitches this
            # close to the metal — a local server under a fan-out drops
            # one occasionally — and the cost of finding out is one more
            # small request. Giving up on the first is how a lens that
            # had something to say ends up recorded as not having
            # answered.
            last_error = "no usable answer"
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_S)
                continue
            return None

        raise LLMConnectionError(
            f"Model endpoint unreachable at {config.llm_endpoint} after "
            f"{_MAX_ATTEMPTS} attempts: {last_error}\n"
            f"  Check it is running:  vibe-sentinel backend status\n"
            f"  Start it:             vibe-sentinel backend start\n"
            f"  Or scan without it:   vibe-sentinel scan --no-model"
        )
    finally:
        if own_client:
            await client.aclose()


def _truncated(payload: dict[str, Any]) -> bool:
    """Whether the server stopped because it ran out of room.

    ``finish_reason`` is part of the OpenAI-compatible response, so this
    needs to know nothing about which server answered.
    """
    choices = payload.get("choices") or []
    return bool(choices) and choices[0].get("finish_reason") == "length"


def _parse_completion(
    payload: dict[str, Any], schema_name: str, mode: str = "none"
) -> dict[str, Any] | None:
    """Pull the message content out of a chat-completion response."""
    choices = payload.get("choices") or []
    if not choices:
        logger.warning("{}: response contained no choices", schema_name)
        return None

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        # Some reasoning models put everything in reasoning_content and
        # leave content empty when they run out of tokens mid-thought.
        if message.get("reasoning_content"):
            logger.warning(
                "{}: model returned only reasoning and no answer — it likely "
                "hit max_tokens. Raise [llm] max_tokens.",
                schema_name,
            )
        else:
            logger.warning("{}: model returned empty content", schema_name)
        return None

    parsed = _extract_json(content, salvage=mode == "none")
    if parsed is None:
        logger.warning(
            "{}: model response was not JSON: {}", schema_name, content[:200]
        )
    return parsed


async def check_endpoint(config: SentinelConfig) -> tuple[bool, str]:
    """Probe the endpoint's ``/models``. Returns ``(reachable, detail)``."""
    url = config.llm_endpoint.rstrip("/") + "/models"
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        return False, f"{type(e).__name__}: {e}"

    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        served = [m["id"] for m in response.json().get("data", [])]
    except (ValueError, KeyError, TypeError):
        return True, "reachable, but /models returned an unexpected shape"
    return True, f"serving: {', '.join(served) or '(none listed)'}"
