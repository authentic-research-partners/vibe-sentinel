"""What a journalled command is: the engine's request and decision, and the records.

The scan pipeline's models live in :mod:`vibe_sentinel.schemas` and are
pydantic, because they cross a boundary with a language model: the JSON
schema pydantic generates is what constrains the model's output, and
validating what comes back is the whole job.

**These are dataclasses instead, deliberately, and this is the one place
in the package that is.** Two reasons, in this order:

1. **Pydantic's strictness is wrong at this boundary.** The payload is
   another program's output, and this module's single promise is that a
   gap in the log means the agent ran nothing. A pydantic model refuses
   ``prompt_id: null`` — a shape the documentation all but invites, since
   the field is "absent until first input" — and refusing means the event
   is dropped, silently, exactly when something unexpected is happening.
   :mod:`vibe_sentinel.agents` coerces instead: an unreadable field
   costs one blank column, never the row. For an append-only record,
   recording something imperfect beats recording nothing.

2. **The hook pays this cost thousands of times a session.** Importing
   pydantic and compiling its first model costs ~55 ms in a fresh
   process — measured against the same process built on a dataclass,
   interleaved so both saw the same machine. The whole invocation now
   costs ~52 ms, so that one import was worth more than everything the
   hook does. Nothing here is validated against a model,
   serialized to JSON schema, or shown to an LLM, so that was 55 ms
   bought for nothing. Keeping this module free of pydantic is what
   makes the difference, and ``test_hook.py`` asserts the hook's whole
   import graph stays clean rather than trusting it to: the leak that
   cost the entire saving the first time was one constant imported from
   ``config.py``.

The ban on ``dataclasses`` in the ruff config is lifted for this file
only, in ``[tool.ruff.lint.per-file-ignores]``, and this docstring is the
justification it points at.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

#: A command can legitimately be long — a heredoc carrying a whole script
#: is still one command — so the cap sits well above ordinary use. But it
#: caps: the history database must not grow without bound because one
#: tool call was handed a megabyte of input.
MAX_COMMAND_CHARS = 20_000

#: Path- and description-shaped fields.
MAX_FIELD_CHARS = 500

#: The envelope carries identity, not content.
MAX_ENVELOPE_CHARS = 4_000

#: What survives when an oversized envelope is reduced: the keys that say
#: who acted and where, under any agent's spelling of them.
_IDENTITY_KEYS = frozenset(
    {
        "agent",
        "agent_id",
        "agent_type",
        "conversation_id",
        "cwd",
        "generation_id",
        "hook_event_name",
        "permission_mode",
        "prompt_id",
        "session_id",
        "tool_name",
        "tool_use_id",
    }
)


def bounded_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """An envelope no bigger than :data:`MAX_ENVELOPE_CHARS`.

    Whole when it fits; otherwise only the identity keys, marked as cut, so
    one oversized payload cannot grow the history without bound.
    """
    if _rough_size(data) <= MAX_ENVELOPE_CHARS:
        return data
    reduced: dict[str, Any] = {k: v for k, v in data.items() if k in _IDENTITY_KEYS}
    reduced["envelope_truncated"] = True
    return reduced


def _rough_size(data: dict[str, Any]) -> int:
    """Cheap stand-in for the serialized length of an envelope."""
    return sum(len(str(k)) + len(str(v)) + 4 for k, v in data.items())


def _actor_name(agent_id: str, agent_type: str) -> str:
    """How to name whoever made a call.

    An empty ``agent_id`` means the main thread — that reading of the
    payload lives here and nowhere else, so if the contract ever changes
    there is one line to correct rather than three.

    An actor that named its own type is called by that type even with no
    ``agent_id``. Claude Code sends ``agent_type`` only inside a subagent,
    where there is an ``agent_id`` too, so the only thing this changes is
    a row that is not an agent at all: a ``vibe-sentinel safety --check``
    verdict, which used to render as ``main`` and read exactly like
    something the agent had really run.
    """
    return agent_type or agent_id or "main"


def clip(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit``, saying so rather than hiding it."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … [truncated, {len(text) - limit} more chars]"


def as_text(value: object) -> str:
    """Whatever arrived, as a string.

    ``None`` becomes empty rather than ``"None"``: absent and absent-ish
    are the same fact, and a literal ``"None"`` in a session id column
    would be a lie that survives forever.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def as_mapping(value: object) -> dict[str, Any]:
    """Whatever arrived, as a dict — or an empty one."""
    return dict(value) if isinstance(value, dict) else {}


@dataclass(slots=True)
class ActionRequest:
    """One tool call an agent is about to make, in the engine's words.

    The journal records it, and the gate judges it. No agent's field names
    appear here: an adapter in :mod:`vibe_sentinel.agents` reads its agent's
    payload into one of these, and turns a :class:`Decision` back into that
    agent's reply. ``tool_name`` is the engine's vocabulary — ``Bash``,
    ``Read``, ``Write``, ``mcp__<server>__<tool>`` — which is what the
    danger set's patterns and structural checks are written against.
    """

    agent: str
    """Which adapter read it: ``claude-code``, ``cursor``."""
    tool_name: str = ""
    command: str = ""
    target: str = ""
    targets: tuple[str, ...] = ()
    """Every path in a multi-file action, before the display target is clipped."""
    can_ask: bool = True
    """False when the host cannot prompt; an undecided enforced call is denied."""
    description: str = ""
    cwd: str = ""
    session_id: str = ""
    agent_id: str = ""
    """Empty for the main thread; a subagent's id inside one."""
    agent_type: str = ""
    prompt_id: str = ""
    tool_use_id: str = ""
    """Unique per call, when the agent sends one: the key that makes a
    replayed event a no-op."""
    permission_mode: str = ""
    transcript_path: str = ""
    envelope: dict[str, Any] = field(default_factory=dict)
    """Everything the payload carried but the tool's arguments and content,
    with ``agent`` added — bounded by :func:`bounded_envelope`."""

    def as_dict(self) -> dict[str, Any]:
        """For the spill file; :meth:`from_dict` reads it back."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionRequest:
        """Read one back off the spill file, coercing rather than refusing."""
        known = {f.name for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key == "targets":
                values[key] = (
                    tuple(as_text(v) for v in value)
                    if isinstance(value, (list, tuple))
                    else ()
                )
            elif key == "can_ask":
                values[key] = value is True
            else:
                values[key] = as_mapping(value) if key == "envelope" else as_text(value)
        return cls(**values)


@dataclass(slots=True)
class Decision:
    """What the gate decided about one call, in no agent's words.

    ``allow``, ``ask`` or ``deny``. An adapter turns it into its agent's
    reply, and says so when its agent cannot express it — an ``ask`` on an
    event that has no prompt becomes a refusal carrying the same reason.
    """

    decision: str
    reason: str = ""


@dataclass(slots=True)
class AgentSessionRecord:
    """One actor the hook has seen: a session, or a subagent within it.

    Identity is ``(session_id, agent_id)``. A subagent's parent is the
    row sharing its ``session_id`` with an empty ``agent_id``.
    """

    id: int
    session_id: str
    agent_id: str = ""
    agent_type: str = ""
    cwd: str = ""
    transcript_path: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    command_count: int = 0

    @property
    def actor(self) -> str:
        """How to name this actor in output."""
        return _actor_name(self.agent_id, self.agent_type)


@dataclass(slots=True)
class CommandRecord:
    """One tool call, as the hook saw it just before it ran."""

    id: int
    occurred_at: str
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""
    prompt_id: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    command: str = ""
    target: str = ""
    description: str = ""
    cwd: str = ""
    permission_mode: str = ""

    @property
    def actor(self) -> str:
        return _actor_name(self.agent_id, self.agent_type)

    def describe(self) -> str:
        """The call on one line: the command, or the tool and its target."""
        if self.command:
            return self.command
        if self.target:
            return f"{self.tool_name} {self.target}"
        return self.tool_name

    def as_dict(self) -> dict[str, Any]:
        """The row as plain data, for ``--format json``."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(slots=True)
class ReviewRecord:
    """One recorded verdict on one command.

    ``reviewed`` is False when triage flagged the command but the model
    never answered — backend down, timed out, or switched off. The
    verdict is then mechanical, and nothing may render it as a review.
    Same rule as ``DriftReport.analyzed`` on the structural side.
    """

    id: int
    command_id: int
    reviewed_at: str
    signals: str
    verdict: str
    reason: str = ""
    model: str = ""
    reviewed: bool = False
    mode: str = ""
    enforced: bool = False
    decision: str = "allow"
    """``allow``, ``ask`` or ``deny`` — what the gate did. ``enforced`` is
    True exactly when this is ``deny``; ``ask`` handed the call to the
    person."""
    history_count: int = 0
    duration_ms: int = 0
    command: str = ""
    tool_name: str = ""
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""

    @property
    def actor(self) -> str:
        return _actor_name(self.agent_id, self.agent_type)

    def signal_list(self) -> list[str]:
        return [s for s in self.signals.split(",") if s]

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}
