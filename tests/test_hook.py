"""The PreToolUse hook: payload in, one recorded row out.

The hook runs in front of every tool call the agent makes, so two
properties matter more than anything it records: it must never raise,
and it must never lose an event quietly. Both are tested here.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import hook
from vibe_sentinel.__main__ import main
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.journal import HOOK_EVENT_FIELDS, HookEvent


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


def payload(
    root: Path,
    command: str = "pytest tests/ -q",
    *,
    tool: str = "Bash",
    tool_use_id: str = "toolu_01",
    session_id: str = "sess_a",
    agent_id: str = "",
    agent_type: str = "",
    prompt_id: str = "prompt_1",
    tool_input: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    body: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "prompt_id": prompt_id,
        "transcript_path": "/home/u/.claude/projects/p/t.jsonl",
        "cwd": str(root),
        "permission_mode": "default",
        "tool_name": tool,
        "tool_use_id": tool_use_id,
        "tool_input": (
            tool_input
            if tool_input is not None
            else {"command": command, "description": "Run the tests"}
        ),
    }
    if agent_id:
        body["agent_id"] = agent_id
    if agent_type:
        body["agent_type"] = agent_type
    body.update(extra)
    return json.dumps(body)


def commands(project: Path) -> list:
    with get_db(project) as conn:
        return journal_store.list_commands(conn)


# --- the happy path --------------------------------------------------------


def test_bash_command_is_recorded(project: Path) -> None:
    assert hook.handle(payload(project)) == "recorded"

    (row,) = commands(project)
    assert row.command == "pytest tests/ -q"
    assert row.tool_name == "Bash"
    assert row.description == "Run the tests"
    assert row.session_id == "sess_a"
    assert row.prompt_id == "prompt_1"
    assert row.permission_mode == "default"


def test_main_thread_has_no_agent_id(project: Path) -> None:
    """An empty agent_id IS the main thread — not missing data."""
    hook.handle(payload(project))

    (row,) = commands(project)
    assert row.agent_id == ""
    assert row.agent_type == ""


def test_subagents_keep_separate_histories(project: Path) -> None:
    """The point of the module: parallel actors, independent streams."""
    hook.handle(payload(project, "git status", tool_use_id="t1"))
    hook.handle(
        payload(
            project,
            "rg TODO",
            tool_use_id="t2",
            agent_id="agent_x",
            agent_type="Explore",
        )
    )
    hook.handle(
        payload(
            project,
            "ls docs",
            tool_use_id="t3",
            agent_id="agent_y",
            agent_type="Plan",
        )
    )

    with get_db(project) as conn:
        actors = journal_store.list_agent_sessions(conn)
        explore = journal_store.list_commands(conn, agent_id="agent_x")
        main = journal_store.list_commands(conn, agent_id="")

    # Three actors, one session: the subagents' parent is the row with
    # the same session_id and an empty agent_id.
    assert len(actors) == 3
    assert {a.session_id for a in actors} == {"sess_a"}
    assert {a.agent_type for a in actors} == {"", "Explore", "Plan"}

    assert [c.command for c in explore] == ["rg TODO"]
    assert [c.command for c in main] == ["git status"]


def test_command_count_tracks_the_actor(project: Path) -> None:
    for i in range(3):
        hook.handle(payload(project, f"echo {i}", tool_use_id=f"t{i}"))
    hook.handle(payload(project, "echo sub", tool_use_id="s0", agent_id="agent_x"))

    with get_db(project) as conn:
        actors = {
            a.agent_id: a.command_count for a in journal_store.list_agent_sessions(conn)
        }
    assert actors == {"": 3, "agent_x": 1}


def test_unknown_payload_fields_are_kept(project: Path) -> None:
    """Anything the payload gains later survives in the envelope."""
    hook.handle(payload(project, effort={"level": "xhigh"}, some_new_field="x"))

    with get_db(project) as conn:
        envelope = json.loads(
            conn.execute("SELECT envelope_json FROM agent_commands").fetchone()[0]
        )
    assert envelope["effort"] == {"level": "xhigh"}
    assert envelope["some_new_field"] == "x"
    # The tool's own arguments are NOT duplicated into the envelope.
    assert "tool_input" not in envelope


# --- what it refuses to do -------------------------------------------------


def test_non_bash_tools_record_a_target_not_their_payload(project: Path) -> None:
    """A Write's file content must not end up in the history database."""
    hook.handle(
        payload(
            project,
            tool="Write",
            tool_input={"file_path": "/p/x.py", "content": "SECRET" * 5000},
        )
    )

    (row,) = commands(project)
    assert row.tool_name == "Write"
    assert row.target == "/p/x.py"
    assert row.command == ""
    assert "SECRET" not in row.command + row.target + row.description


def test_a_long_command_is_truncated_not_dropped(project: Path) -> None:
    hook.handle(payload(project, "x" * 50_000))

    (row,) = commands(project)
    assert len(row.command) < 50_000
    assert row.command.startswith("xxx")
    assert "truncated" in row.command


def test_an_unwatched_directory_is_left_alone(tmp_path: Path) -> None:
    """The hook may be installed globally; it must not litter."""
    assert hook.handle(payload(tmp_path)) == "unwatched"
    assert not (tmp_path / ".vibe-sentinel").exists()


def test_a_malformed_payload_never_raises(project: Path) -> None:
    assert hook.handle("not json at all") == "unparsed"
    assert hook.handle("[1, 2, 3]") == "unparsed"
    assert hook.handle("") == "unparsed"
    assert commands(project) == []


def test_a_foreign_payload_is_refused_rather_than_recorded_blank(
    project: Path,
) -> None:
    """Another program's schema must not become a contentless row.

    Every other field coerces, because a surprising value should cost
    that value and not the record. ``tool_name`` cannot: without it there
    is no tool and no command to store, so the row would assert that an
    agent did something unidentifiable — a false record, and one that
    looks exactly like a real call this build failed to read.
    """
    foreign = json.dumps(
        {
            "event": "before_tool",
            "tool": {"name": "shell", "args": {"cmd": "rm -rf /"}},
            "conversation": "abc123",
            "cwd": str(project),
        }
    )
    assert hook.handle(foreign) == "unparsed"
    assert commands(project) == []


def test_an_empty_tool_name_is_refused_too(project: Path) -> None:
    """Present but blank is the same absence, and coerces to the same row."""
    for value in ("", "   ", None):
        raw = json.dumps(
            {"hook_event_name": "PreToolUse", "cwd": str(project), "tool_name": value}
        )
        assert hook.handle(raw) == "unparsed"
    assert commands(project) == []


def test_a_surprising_field_value_still_costs_only_that_field(
    project: Path,
) -> None:
    """The refusal above must not have widened into refusing coercion."""
    raw = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_use_id": "toolu_odd",
            "prompt_id": None,
            "session_id": 12345,
            "tool_input": {"command": "echo hi"},
        }
    )
    assert hook.handle(raw) == "recorded"
    assert len(commands(project)) == 1


def test_a_replayed_tool_use_id_is_not_counted_twice(project: Path) -> None:
    assert hook.handle(payload(project)) == "recorded"
    assert hook.handle(payload(project)) == "duplicate"
    assert len(commands(project)) == 1


def test_events_without_a_tool_use_id_are_all_kept(project: Path) -> None:
    """Absent ids must not collapse into one row via the unique index."""
    hook.handle(payload(project, "one", tool_use_id=""))
    hook.handle(payload(project, "two", tool_use_id=""))

    assert sorted(c.command for c in commands(project)) == ["one", "two"]


# --- finding the project ---------------------------------------------------


def test_root_is_found_from_a_subdirectory(project: Path) -> None:
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    assert hook.find_project_root(nested) == project.resolve()


def test_a_config_file_alone_marks_a_project(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    assert hook.find_project_root(tmp_path) == tmp_path.resolve()


def test_the_events_own_cwd_decides_the_project(
    project: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A subagent may run somewhere else; the payload says where."""
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert hook.handle(payload(elsewhere)) == "unwatched"
    assert commands(project) == []


# --- losing nothing when the database is unavailable -----------------------


def test_an_unwritable_database_spills_instead_of_dropping(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def refuse(_root: Path, _synchronous: str = ""):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook, "get_db", refuse)
    assert hook.handle(payload(project)) == "spilled"

    lines = hook.spill_path(project).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert (
        json.loads(lines[0])["payload"]["tool_input"]["command"] == "pytest tests/ -q"
    )


def test_replay_drains_the_spill_keeping_the_original_time(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def refuse(_root: Path, _synchronous: str = ""):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook, "get_db", refuse)
    hook.handle(payload(project, "first", tool_use_id="t1"))
    hook.handle(payload(project, "second", tool_use_id="t2"))
    monkeypatch.undo()

    assert hook.replay_spill(project) == (2, 0)
    assert not hook.spill_path(project).exists()

    recorded = commands(project)
    assert sorted(c.command for c in recorded) == ["first", "second"]
    # The time recorded is when the tool ran, not when it was replayed.
    assert recorded[0].occurred_at < hook.now_iso()


def test_replay_of_an_already_recorded_event_does_not_duplicate(
    project: Path,
) -> None:
    hook.handle(payload(project))
    spill_line = json.dumps(
        {
            "occurred_at": hook.now_iso(),
            "reason": "test",
            "payload": json.loads(payload(project)),
        }
    )
    hook.spill_path(project).parent.mkdir(parents=True, exist_ok=True)
    hook.spill_path(project).write_text(spill_line + "\n", encoding="utf-8")

    hook.replay_spill(project)
    assert len(commands(project)) == 1


def test_an_unreadable_spill_line_is_kept_not_discarded(project: Path) -> None:
    path = hook.spill_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json\n", encoding="utf-8")

    assert hook.replay_spill(project) == (0, 1)
    assert path.read_text(encoding="utf-8").strip() == "{ this is not json"


def test_replay_with_no_spill_file_is_a_no_op(project: Path) -> None:
    assert hook.replay_spill(project) == (0, 0)


# --- installing ------------------------------------------------------------


def test_install_writes_the_pretooluse_entry(tmp_path: Path) -> None:
    path, changed = hook.install(tmp_path)

    assert changed
    settings = json.loads(path.read_text(encoding="utf-8"))
    entry = settings["hooks"]["PreToolUse"][0]
    assert entry["hooks"][0]["command"] == hook.HOOK_COMMAND
    assert entry["matcher"] == "*"


def test_install_can_narrow_to_one_tool(tmp_path: Path) -> None:
    path, _ = hook.install(tmp_path, matcher="Bash")

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def test_install_is_idempotent(tmp_path: Path) -> None:
    hook.install(tmp_path)
    path, changed = hook.install(tmp_path)

    assert not changed
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_install_preserves_other_settings_and_backs_up(tmp_path: Path) -> None:
    path = hook.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "permissions": {"defaultMode": "auto"},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "other"}],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    hook.install(tmp_path)

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["permissions"] == {"defaultMode": "auto"}
    commands_installed = [
        spec["command"]
        for entry in settings["hooks"]["PreToolUse"]
        for spec in entry["hooks"]
    ]
    assert commands_installed == ["other", hook.HOOK_COMMAND]
    assert list(path.parent.glob("settings.json.bak-*"))


def test_install_refuses_a_broken_settings_file(tmp_path: Path) -> None:
    path = hook.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        hook.install(tmp_path)


# --- the payload contract --------------------------------------------------
#
# The journal's whole identity model rests on these field names, and they
# belong to another program. Pinning the documented payload verbatim is
# what turns "the docs say agent_id" into something that fails loudly if
# it stops being true.

#: ``PreToolUse`` exactly as Claude Code's hook reference gives it
#: (code.claude.com/docs/en/hooks, read against Claude Code 2.1.258).
DOCUMENTED_PRE_TOOL_USE: dict[str, Any] = {
    "session_id": "abc123",
    "prompt_id": "550e8400-e29b-41d4-a716-446655440000",
    "transcript_path": "/home/user/.claude/projects/.../transcript.jsonl",
    "cwd": "/home/user/my-project",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {
        "command": "npm test",
        "description": "Run test suite",
        "timeout": 120000,
        "run_in_background": False,
    },
    "tool_use_id": "toolu_01ABC123...",
}

#: The same event as a subagent sends it: two extra fields, documented as
#: present only when running with ``--agent`` or inside a subagent.
DOCUMENTED_SUBAGENT_EXTRA: dict[str, Any] = {
    "agent_id": "subagent_123",
    "agent_type": "Explore",
}


def test_the_documented_payload_maps_field_for_field() -> None:
    event = HookEvent.from_payload(DOCUMENTED_PRE_TOOL_USE)

    assert event.session_id == "abc123"
    assert event.prompt_id == "550e8400-e29b-41d4-a716-446655440000"
    assert event.hook_event_name == "PreToolUse"
    assert event.permission_mode == "default"
    assert event.tool_name == "Bash"
    assert event.tool_use_id == "toolu_01ABC123..."
    assert event.command_text() == "npm test"
    assert event.description() == "Run test suite"
    assert event.is_subagent is False


def test_the_documented_subagent_payload_names_the_subagent() -> None:
    event = HookEvent.from_payload(DOCUMENTED_PRE_TOOL_USE | DOCUMENTED_SUBAGENT_EXTRA)

    assert event.is_subagent is True
    assert event.agent_id == "subagent_123"
    assert event.agent_type == "Explore"


def test_every_documented_field_is_declared_not_just_kept() -> None:
    """A documented field this build does not name would survive only in
    the envelope, and `commands --fields` would report it as new."""
    documented = set(DOCUMENTED_PRE_TOOL_USE) | set(DOCUMENTED_SUBAGENT_EXTRA)
    assert documented <= set(HOOK_EVENT_FIELDS)


def test_an_undocumented_field_costs_nothing(project: Path) -> None:
    """Forward compatibility: a payload that grows a field still records."""
    assert (
        hook.handle(payload(project, invented_by_a_later_build={"a": 1})) == "recorded"
    )


# --- the two CLI paths agree ----------------------------------------------


def test_the_fast_path_records_and_writes_no_stdout(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`vibe-sentinel hook` with no arguments skips the parser entirely.

    Stdout is where a PreToolUse hook puts a permission decision, so an
    empty one is not cosmetic — it is what keeps this unable to deny a
    tool call.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(payload(project, "make build")))

    assert main(["hook"]) == 0
    assert [c.command for c in commands(project)] == ["make build"]
    assert capsys.readouterr().out == ""


def test_the_parsed_path_records_the_same_thing(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any other argv goes through argparse and must not differ."""
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(payload(project, "make build", tool_use_id="t2"))
    )

    assert main(["hook", str(project)]) == 0
    assert [c.command for c in commands(project)] == ["make build"]
    assert capsys.readouterr().out == ""


def test_neither_path_leaves_a_log_directory(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The hook runs in the agent's working directory, wherever that is."""
    workdir = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr("sys.stdin", io.StringIO(payload(project)))

    assert main(["hook"]) == 0
    assert not (workdir / "logs").exists()


# --- why these models are not pydantic ------------------------------------


@pytest.mark.parametrize(
    ("what", "patch"),
    [
        ("prompt_id sent as null", {"prompt_id": None}),
        ("agent_id sent as null", {"agent_id": None}),
        ("session_id sent as a number", {"session_id": 12345}),
        ("tool_input sent as null", {"tool_input": None}),
    ],
)
def test_an_odd_field_costs_that_field_and_not_the_event(
    project: Path, what: str, patch: dict[str, Any]
) -> None:
    """Every one of these was dropped outright while the payload was a
    pydantic model — silently, with no spill, which is the single thing
    this log must never do. ``prompt_id: null`` is not hypothetical: the
    field is documented as absent until the first user prompt, and null
    is how a JSON emitter usually says absent."""
    body = json.loads(payload(project))
    body.update(patch)

    assert hook.handle(json.dumps(body)) == "recorded", what
    (row,) = commands(project)
    assert row.session_id == str(patch.get("session_id", "sess_a"))


def test_a_number_where_a_string_was_expected_is_recorded_as_text() -> None:
    event = HookEvent.from_payload({"session_id": 123, "prompt_id": None})

    assert event.session_id == "123"
    assert event.prompt_id == ""
    assert event.tool_input == {}


def test_recording_a_command_imports_no_logger_and_no_model_stack(
    project: Path,
) -> None:
    """The hook's cost is its import graph, so the graph is asserted.

    Three things are absent here and each was present once: pydantic
    (~55 ms, for validation this boundary does not want), httpx and the
    model client (pulled in by the package's own ``__init__``), and
    loguru (~38 ms, for lines this path never writes — every call site
    in the hook is a failure or first-run branch).

    It drives ``main()`` rather than importing :mod:`vibe_sentinel.hook`,
    because that is the path Claude Code invokes: a clean module can
    still be reached through an entry point that imported half the
    package first, which is exactly what happened once already. And it
    asserts the row landed, so a hook that silently did nothing cannot
    pass by importing nothing.
    """
    body = payload(project, "make build")
    probe = (
        "import sys, io; "
        f"sys.stdin = io.StringIO({body!r}); "
        "from vibe_sentinel.__main__ import main; "
        "assert main(['hook']) == 0; "
        "print(','.join(sorted("
        "{'pydantic', 'httpx', 'openai', 'loguru'} & set(sys.modules))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "", f"the hook now imports: {result.stdout.strip()}"
    assert [c.command for c in commands(project)] == ["make build"]


def test_a_spilled_event_round_trips_through_its_payload(project: Path) -> None:
    """The spill file holds a payload, not a dump of our own model, so a
    replay produces the row the hook would have written."""
    original = hook.parse_event(payload(project, "make build", effort={"level": "max"}))
    assert original is not None

    restored = HookEvent.from_payload(original.as_payload())
    assert restored == original
    assert restored.extras == {}
    assert restored.effort == {"level": "max"}


def test_the_hook_does_not_fsync_on_every_tool_call(project: Path) -> None:
    """Measured: 400 journal inserts cost 1552 ms at synchronous=FULL and
    72 ms at NORMAL, for byte-identical files. That is 3.88 ms of fsync in
    front of every tool call — about 7% of the hook's whole budget — to
    flush a record of a command that has not run yet.

    `db prune` trims this journal by default; it refuses to touch the scan
    history without --scans. The two have different value and now take
    different trades.
    """
    from vibe_sentinel.db.connection import JOURNAL_DURABILITY, get_db

    with get_db(project, JOURNAL_DURABILITY) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_the_scan_history_still_fsyncs(project: Path) -> None:
    """The irreplaceable half keeps FULL, and pays nothing for it: a whole
    scan is two commits."""
    from vibe_sentinel.db.connection import get_db

    with get_db(project) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_durability_is_per_connection_not_stored_in_the_file(project: Path) -> None:
    """Which is what lets the hook and a scan of the same database differ.
    A pragma written into the file would make the last writer decide for
    everyone."""
    from vibe_sentinel.db.connection import JOURNAL_DURABILITY, get_db

    with get_db(project, JOURNAL_DURABILITY) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    with get_db(project) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
