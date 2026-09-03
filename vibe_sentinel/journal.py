"""What a journalled command is, and how one is read off a hook payload.

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
   :meth:`HookEvent.from_payload` coerces instead: an unreadable field
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

#: What survives when an oversized envelope is reduced to identity alone.
_IDENTITY_KEYS = frozenset(
    {
        "agent_id",
        "agent_type",
        "cwd",
        "hook_event_name",
        "permission_mode",
        "prompt_id",
        "session_id",
        "tool_name",
        "tool_use_id",
    }
)


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


def _clip(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit``, saying so rather than hiding it."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … [truncated, {len(text) - limit} more chars]"


def _as_text(value: object) -> str:
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


def _as_mapping(value: object) -> dict[str, Any]:
    """Whatever arrived, as a dict — or an empty one."""
    return dict(value) if isinstance(value, dict) else {}


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
                known[key] = _as_text(value)
            elif key in _MAPPING_FIELDS:
                known[key] = _as_mapping(value)
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
        return _clip(value, MAX_COMMAND_CHARS) if isinstance(value, str) else ""

    def target(self) -> str:
        """What the tool acted on — a path, a pattern, a URL.

        Deliberately not the whole of ``tool_input``: a Write's arguments
        include the file's entire new content, and this is a record of
        what the agent did, not a second copy of the codebase.
        """
        for key in _TARGET_KEYS:
            value = self.tool_input.get(key)
            if isinstance(value, str) and value:
                return _clip(value, MAX_FIELD_CHARS)
        return ""

    def description(self) -> str:
        """The agent's own one-line account of the call, when it gave one."""
        value = self.tool_input.get("description")
        return _clip(value, MAX_FIELD_CHARS) if isinstance(value, str) else ""

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
        if _rough_size(data) <= MAX_ENVELOPE_CHARS:
            return data
        reduced: dict[str, Any] = {k: v for k, v in data.items() if k in _IDENTITY_KEYS}
        reduced["envelope_truncated"] = True
        return reduced


def _rough_size(data: dict[str, Any]) -> int:
    """Cheap stand-in for the serialized length of an envelope."""
    return sum(len(str(k)) + len(str(v)) + 4 for k, v in data.items())


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
