"""Codex PreToolUse payloads, without copying patch bodies into the journal.

Contract: https://learn.chatgpt.com/docs/hooks (2026-11-02). Codex sends
shell and patch text in tool_input.command, but only shell text is a command
to record. A patch contributes every affected path, including move destinations.
Codex cannot express an ask decision; the engine records a denial instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vibe_sentinel.journal import (
    MAX_COMMAND_CHARS,
    MAX_FIELD_CHARS,
    ActionRequest,
    Decision,
    as_mapping,
    as_text,
    bounded_envelope,
    clip,
)

NAME = "codex"
HOOK_COMMAND = "vibe-sentinel hook --agent codex"

_PATCH_HEADERS = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
    "*** Move to: ",
)
_IDENTITY = (
    "hook_event_name",
    "session_id",
    "turn_id",
    "tool_use_id",
    "tool_name",
    "cwd",
    "permission_mode",
    "agent_id",
    "agent_type",
    "model",
)


def patch_targets(patch: str, cwd: str) -> tuple[str, ...]:
    """All patch paths, in order, resolved against the hook's working directory.

    Added and removed content lines start with + or -, and context with a
    space, so none is a header even when its content looks like one.
    """
    targets: dict[str, None] = {}
    for line in patch.splitlines():
        for header in _PATCH_HEADERS:
            if line.startswith(header):
                path = Path(line[len(header) :].strip())
                targets[str(path if path.is_absolute() else Path(cwd) / path)] = None
                break
    return tuple(targets)


def read(raw: str) -> ActionRequest | None:
    """Read only PreToolUse. Other lifecycle events are not proposed actions."""
    from vibe_sentinel.log import logger

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Codex hook input is not JSON; no action recorded")
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PreToolUse":
        logger.warning("Codex hook expects PreToolUse; no action recorded")
        return None
    name = as_text(payload.get("tool_name"))
    if not name.strip():
        logger.warning("Codex hook has no tool_name; no action recorded")
        return None
    args = as_mapping(payload.get("tool_input"))
    cwd = as_text(payload.get("cwd"))
    targets: tuple[str, ...] = ()
    command = ""
    if name == "apply_patch":
        targets = patch_targets(as_text(args.get("command")), cwd)
        tool_name = "Write"
    else:
        tool_name = name
        if name == "Bash":
            command = clip(as_text(args.get("command")), MAX_COMMAND_CHARS)
        elif name in ("Read", "Write", "Edit"):
            target = as_text(args.get("file_path"))
            if target:
                path = Path(target)
                targets = (str(path if path.is_absolute() else Path(cwd) / path),)

    # Explicit identity fields only: unknown extensions may carry source,
    # MCP arguments or credentials. Never put those in the envelope or spill.
    envelope = {key: payload[key] for key in _IDENTITY if key in payload}
    envelope["agent"] = NAME
    if targets:
        envelope["targets"] = list(targets)
    return ActionRequest(
        agent=NAME,
        tool_name=tool_name,
        command=command,
        target=clip(", ".join(targets), MAX_FIELD_CHARS),
        targets=targets,
        can_ask=False,
        cwd=cwd,
        session_id=as_text(payload.get("session_id")),
        agent_id=as_text(payload.get("agent_id")),
        agent_type=as_text(payload.get("agent_type")),
        prompt_id=as_text(payload.get("turn_id")),
        tool_use_id=as_text(payload.get("tool_use_id")),
        permission_mode=as_text(payload.get("permission_mode")),
        transcript_path=as_text(payload.get("transcript_path")),
        envelope=bounded_envelope(envelope),
    )


def reply(
    decision: Decision | None, request: ActionRequest | None
) -> dict[str, Any] | None:
    """Never send unsupported 'ask': Codex would report failure and continue."""
    if decision is None or decision.decision == "allow":
        return None
    reason = decision.reason
    if decision.decision == "ask":
        reason += " Codex cannot ask from PreToolUse, so this call is refused."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
