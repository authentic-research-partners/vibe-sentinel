"""Cursor: its hook payloads in, its ``permission`` JSON out.

The one place that knows Cursor's field names, read against its hooks
reference (cursor.com/docs/agent/hooks, 2026-10-02). Three events are
gated, each with its own payload, and each is read into the engine's
vocabulary so the danger set applies unchanged:

==========================  =========================  ====================
Cursor event                engine ``tool_name``       what is kept
==========================  =========================  ====================
``beforeShellExecution``    ``Bash``                   ``command``, ``cwd``
``beforeMCPExecution``      ``mcp__<server>__<tool>``  the tool's name
``beforeReadFile``          ``Read``                   ``file_path``
==========================  =========================  ====================

``beforeReadFile`` sends the file's whole ``content``. It never reaches the
journal: a read of ``.env`` would otherwise copy its secrets into the
history database, which the credentials gate exists to keep them out of.
An MCP call's arguments are left out for the reason Claude Code's
``tool_input`` is — a record of what the agent did, not a copy of what it
sent. The person's email, which Cursor sends with every event, is not the
actor and is not recorded either.

Two differences from Claude Code decide the reply. Cursor reads an absent
reply as a failed hook, so every reply is explicit, ``allow`` included. And
``beforeReadFile`` takes ``allow`` or ``deny`` only, so an ``ask`` there —
a call with no verdict under ``enforce`` — is a refusal carrying the same
reason, as an unattended Claude Code run makes it. ``failClosed`` is not
set: the hook never raises, and a gate whose own failure blocked every
call would be uninstalled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from vibe_sentinel.journal import (
    MAX_COMMAND_CHARS,
    MAX_FIELD_CHARS,
    ActionRequest,
    Decision,
    as_text,
    bounded_envelope,
    clip,
)

NAME = "cursor"

#: The events ``hook --install --agent cursor`` wires, and the engine tool
#: each becomes.
EVENTS: dict[str, str] = {
    "beforeShellExecution": "Bash",
    "beforeMCPExecution": "mcp",
    "beforeReadFile": "Read",
}

#: The events whose reply may be ``ask``. ``beforeReadFile`` is not one.
_CAN_ASK = frozenset({"beforeShellExecution", "beforeMCPExecution"})

#: Payload fields that carry content rather than identity, or are recorded
#: in their own column: never in the envelope.
_NOT_RECORDED = frozenset({"content", "command", "tool_input", "user_email"})


def read(raw: str) -> ActionRequest | None:
    """One stdin payload as a request; None when it is not a usable event."""
    from vibe_sentinel.log import logger

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("hook: payload is not JSON ({}) — ignoring", e)
        return None
    if not isinstance(payload, dict):
        logger.debug("hook: payload is {}, not an object — ignoring", type(payload))
        return None
    event = as_text(payload.get("hook_event_name"))
    if not event:
        logger.warning(
            "hook: payload carries no 'hook_event_name', so nothing was "
            "recorded. `vibe-sentinel hook --agent cursor` reads Cursor's hook "
            "schema; the payload had: {}",
            ", ".join(sorted(payload)[:10]) or "(no fields)",
        )
        return None

    roots = payload.get("workspace_roots")
    first_root = as_text(roots[0]) if isinstance(roots, list) and roots else ""
    cwd = (
        as_text(payload.get("cwd"))
        or first_root
        or os.environ.get("CURSOR_PROJECT_DIR", "")
    )

    envelope = {k: v for k, v in payload.items() if k not in _NOT_RECORDED}
    envelope["agent"] = NAME
    command = target = ""
    if event == "beforeShellExecution":
        tool_name = "Bash"
        command = clip(as_text(payload.get("command")), MAX_COMMAND_CHARS)
    elif event == "beforeMCPExecution":
        tool_name = mcp_tool_name(
            as_text(payload.get("mcp_server_name")), as_text(payload.get("tool_name"))
        )
    elif event == "beforeReadFile":
        tool_name = "Read"
        target = clip(as_text(payload.get("file_path")), MAX_FIELD_CHARS)
    else:
        # Wired by hand to an event this build does not gate: recorded under
        # its own name, so the journal says what arrived rather than guessing.
        tool_name = event

    return ActionRequest(
        agent=NAME,
        tool_name=tool_name,
        command=command,
        target=target,
        cwd=cwd,
        session_id=as_text(payload.get("conversation_id")),
        prompt_id=as_text(payload.get("generation_id")),
        tool_use_id=as_text(payload.get("tool_use_id")),
        transcript_path=as_text(payload.get("transcript_path")),
        envelope=bounded_envelope(envelope),
    )


def mcp_tool_name(server: str, tool: str) -> str:
    """Cursor's server and tool, in the ``mcp__<server>__<tool>`` form the
    danger set's ``applies_to = "tool"`` patterns are written against."""
    return f"mcp__{server or 'unknown'}__{tool or 'unknown'}"


def reply(decision: Decision | None, request: ActionRequest | None) -> dict[str, Any]:
    """The JSON Cursor acts on — always one, ``allow`` included."""
    if decision is None or decision.decision == "allow":
        return {"permission": "allow"}
    event = as_text(request.envelope.get("hook_event_name")) if request else ""
    if decision.decision == "ask" and event in _CAN_ASK:
        return {
            "permission": "ask",
            "user_message": decision.reason,
            "agent_message": decision.reason,
        }
    reason = decision.reason
    if decision.decision == "ask":
        reason += (
            f" Cursor's {event or 'this'} event cannot ask, so the call is "
            f"refused rather than allowed unjudged."
        )
    return {"permission": "deny", "user_message": reason, "agent_message": reason}


# ---------------------------------------------------------------------------
# Installing: .cursor/hooks.json
# ---------------------------------------------------------------------------

#: What each entry in ``hooks.json`` runs.
HOOK_COMMAND = "vibe-sentinel hook --agent cursor"


def hooks_files(root: Path) -> list[tuple[str, Path]]:
    """``(scope, path)`` for each ``hooks.json`` that can run this hook for
    the project, broadest first. Team hooks, delivered from Cursor's
    dashboard, are not a file here and are not read."""
    from vibe_sentinel import paths

    return [
        ("enterprise", paths.cursor_enterprise_hooks_path()),
        ("user", Path.home() / ".cursor" / "hooks.json"),
        ("project", root / ".cursor" / "hooks.json"),
    ]


def is_this_hook(command: object) -> bool:
    """Whether a ``hooks.json`` entry runs this hook for Cursor."""
    if not isinstance(command, str):
        return False
    words = command.split()
    return (
        len(words) >= 4
        and words[-3:] == ["hook", "--agent", NAME]
        and Path(words[-4]).name == "vibe-sentinel"
    )


def wired_events(data: dict[str, Any]) -> list[str]:
    """The events in a parsed ``hooks.json`` that run this hook."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    found: list[str] = []
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and is_this_hook(entry.get("command")):
                found.append(str(event))
                break
    return found


def read_hooks(path: Path) -> dict[str, Any] | None:
    """A ``hooks.json`` as an object; None when there is no file.

    Raises ``ValueError`` naming the file when it is there and is not a JSON
    object — a file that will not parse is not one with no hook in it.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        loaded = json.loads(text) if text.strip() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{path} could not be read as JSON ({e})") from e
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} holds a {type(loaded).__name__}, not a JSON object")
    return loaded


def install(root: Path, timeout: int) -> tuple[Path, bool]:
    """Wire the three gated events in the project's ``.cursor/hooks.json``."""
    return install_at(root / ".cursor" / "hooks.json", timeout)


def install_at(path: Path, timeout: int) -> tuple[Path, bool]:
    """Wire the three gated events in one ``hooks.json``.

    Returns ``(path, changed)``. Every other key and entry is kept, and an
    existing file is backed up beside itself before it is rewritten. An
    event already running this hook is left as it is.
    """
    from datetime import UTC, datetime

    data = read_hooks(path)
    if data is None:
        data = {"version": 1}
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(
            f"{path} has a 'hooks' key that is not an object. Fix it, then "
            f"re-run `vibe-sentinel hook --install --agent cursor --mode <choice>`."
        )
    wired = set(wired_events(data))
    changed = False
    for event in EVENTS:
        if event in wired:
            continue
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(
                f"{path} has a 'hooks.{event}' key that is not a list. Fix it, "
                f"then re-run the install."
            )
        entries.append({"command": HOOK_COMMAND, "timeout": timeout})
        changed = True
    if not changed:
        return (path, False)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return (path, True)
