"""Claude Code: its hook payload in, its permission JSON out.

The one place that knows Claude Code's field names. ``PreToolUse`` sends a
payload on stdin — ``session_id``, ``agent_id``, ``tool_name``,
``tool_input`` and the rest — and reads ``hookSpecificOutput`` back off
stdout. :class:`HookEvent` reads the first, coercing rather than refusing,
and :func:`reply` writes the second.

A dataclass, and stdlib only, for the reason :mod:`vibe_sentinel.journal`
gives: this runs in front of every tool call, and pydantic alone cost more
than the whole hook. The ruff ban on ``dataclasses`` is lifted for this
file for the same reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
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

NAME = "claude-code"

#: Where a tool keeps its subject, most specific first. A tool that names
#: no target records none — this never guesses one.
_TARGET_KEYS = (
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "shell_id",
    "subagent_type",
)


@dataclass(slots=True)
class HookEvent:
    """One Claude Code hook payload, as it arrived on stdin.

    Every field defaults to empty because the payload belongs to another
    program: a build that stops sending ``prompt_id``, or one that adds a
    field, must cost a column's worth of detail and never an exception in
    front of a tool call. ``extras`` is the same bet from the other side
    — anything this build does not name is kept there and lands in the
    envelope, rather than being silently discarded.

    ``agent_id`` is the field that makes several actors legible: empty is
    the main thread, and any other value is a subagent running under the
    same ``session_id``.
    """

    hook_event_name: str = ""
    session_id: str = ""
    prompt_id: str = ""
    """The turn — one user prompt. Absent before the first input."""
    transcript_path: str = ""
    cwd: str = ""
    permission_mode: str = ""
    agent_id: str = ""
    """Empty for the main thread; the subagent's id inside one."""
    agent_type: str = ""
    """The subagent's name — ``Explore``, ``Plan``, a custom agent."""
    tool_name: str = ""
    tool_use_id: str = ""
    """Unique per call. The key that makes a replayed event a no-op."""
    effort: dict[str, Any] = field(default_factory=dict)
    """Reasoning budget, ``{"level": ...}``, when the model reports one.

    Named but given no column: worth keeping beside a command, not worth
    querying by. Naming it is what keeps ``commands --fields`` honest —
    an unnamed field there means Claude Code's payload has genuinely
    grown something, not that this build never got round to it.
    """
    tool_input: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    """Everything the payload carried that this build does not name."""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> HookEvent:
        """Read an event off a raw payload, coercing rather than refusing.

        Never raises. A field of an unexpected type costs that field and
        nothing else — the alternative is dropping a record of something
        the agent really did because a string arrived as a number.
        """
        known: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _TEXT_FIELDS:
                known[key] = as_text(value)
            elif key in _MAPPING_FIELDS:
                known[key] = as_mapping(value)
            else:
                extras[key] = value
        return cls(**known, extras=extras)

    @property
    def is_subagent(self) -> bool:
        """Whether a subagent made this call, rather than the main thread."""
        return bool(self.agent_id)

    def command_text(self) -> str:
        """The shell command, for the tools that run one."""
        value = self.tool_input.get("command")
        return clip(value, MAX_COMMAND_CHARS) if isinstance(value, str) else ""

    def target(self) -> str:
        """What the tool acted on — a path, a pattern, a URL.

        Deliberately not the whole of ``tool_input``: a Write's arguments
        include the file's entire new content, and this is a record of
        what the agent did, not a second copy of the codebase.
        """
        for key in _TARGET_KEYS:
            value = self.tool_input.get(key)
            if isinstance(value, str) and value:
                return clip(value, MAX_FIELD_CHARS)
        return ""

    def description(self) -> str:
        """The agent's own one-line account of the call, when it gave one."""
        value = self.tool_input.get("description")
        return clip(value, MAX_FIELD_CHARS) if isinstance(value, str) else ""

    def as_payload(self) -> dict[str, Any]:
        """The event back as a payload, for the spill file.

        Round-trips through :meth:`from_payload`, so an event spilled
        while the database was unavailable replays into exactly the row
        it would have been.
        """
        data: dict[str, Any] = {name: getattr(self, name) for name in HOOK_EVENT_FIELDS}
        data.update(self.extras)
        return data

    def envelope(self) -> dict[str, Any]:
        """Everything but the tool's arguments, for the record.

        Unnamed fields land here too, which is what lets a payload gain
        ``agent_id`` — or anything else — without a migration, and lets a
        stored row be checked afterwards against what was really sent.
        """
        data: dict[str, Any] = {
            name: getattr(self, name)
            for name in HOOK_EVENT_FIELDS
            if name != "tool_input"
        }
        data.update(self.extras)
        data["agent"] = NAME
        return bounded_envelope(data)

    def request(self) -> ActionRequest:
        """The engine's view of this call."""
        return ActionRequest(
            agent=NAME,
            tool_name=self.tool_name,
            command=self.command_text(),
            target=self.target(),
            description=self.description(),
            cwd=self.cwd,
            session_id=self.session_id,
            agent_id=self.agent_id,
            agent_type=self.agent_type,
            prompt_id=self.prompt_id,
            tool_use_id=self.tool_use_id,
            permission_mode=self.permission_mode,
            transcript_path=self.transcript_path,
            envelope=self.envelope(),
        )


#: The fields this build names, in declaration order. Exported so callers
#: can compare them against what a payload really carried without
#: importing ``dataclasses`` themselves.
HOOK_EVENT_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(HookEvent) if f.name != "extras"
)

_TEXT_FIELDS = frozenset(
    name for name in HOOK_EVENT_FIELDS if name not in ("effort", "tool_input")
)
_MAPPING_FIELDS = frozenset({"effort", "tool_input"})


def read(raw: str) -> ActionRequest | None:
    """One stdin payload as a request; None when it is not a JSON object."""
    event = parse(raw)
    return None if event is None else event.request()


def parse(raw: str) -> HookEvent | None:
    """Parse one hook payload, or None when it is not usable.

    Three things are unusable: text that is not JSON, JSON that is not an
    object, and an object with no ``tool_name``. Everything past that is
    coerced by :meth:`HookEvent.from_payload` — a field of an unexpected
    type costs that field, not the event. Refusing the whole payload over
    one surprising value would drop a record of something the agent really
    did, at exactly the moment something unexpected was happening.

    ``tool_name`` is the exception because it is not a surprising field,
    it is the whole event: a ``PreToolUse`` payload always carries one,
    so an object without it came from a different program's schema.
    Coercion would happily accept it and append a row naming no tool and
    no command — a record asserting that an agent did something
    unidentifiable, which is worse than no row at all.
    """
    from vibe_sentinel.log import logger

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.debug("hook: payload is not JSON ({}) — ignoring", e)
        return None
    if not isinstance(payload, dict):
        logger.debug("hook: payload is {}, not an object — ignoring", type(payload))
        return None
    if not str(payload.get("tool_name") or "").strip():
        # Loud, not debug: every payload reaching here is one somebody
        # wired up, so this is a misconfiguration rather than noise, and
        # the fields it did carry are the only clue to whose schema it is.
        logger.warning(
            "hook: payload carries no 'tool_name', so nothing was recorded. "
            "`vibe-sentinel hook` reads Claude Code's PreToolUse schema — for "
            "Cursor, the hook is `vibe-sentinel hook --agent cursor`. The "
            "payload had: {}",
            ", ".join(sorted(payload)[:10]) or "(no fields)",
        )
        return None
    return HookEvent.from_payload(payload)


def reply(decision: Decision | None, request: ActionRequest) -> dict[str, Any] | None:
    """The JSON Claude Code acts on, or None to say nothing and let it run.

    ``ask`` shows Claude Code's own permission prompt with the reason on
    it; where nobody can answer — a ``claude -p`` run with no permission
    host — Claude Code denies the request instead.
    """
    if decision is None or decision.decision == "allow":
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision.decision,
            "permissionDecisionReason": decision.reason,
        }
    }
