"""The command journal's tables: actors, commands, and the run window."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_sentinel.db import journal_store, store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.journal import HOOK_EVENT_FIELDS, HookEvent
from vibe_sentinel.schemas import DriftReport, Snapshot


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


def event(
    command: str = "ls",
    *,
    session_id: str = "s1",
    agent_id: str = "",
    agent_type: str = "",
    prompt_id: str = "p1",
    tool_use_id: str = "",
    tool_name: str = "Bash",
) -> HookEvent:
    return HookEvent(
        hook_event_name="PreToolUse",
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        prompt_id=prompt_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        cwd="/p",
        permission_mode="default",
        tool_input={"command": command},
    )


def record(project: Path, ev: HookEvent, at: str) -> int | None:
    with get_db(project) as conn:
        return journal_store.record_command(conn, ev, at)


# --- actors ----------------------------------------------------------------


def test_the_actor_row_is_created_once(project: Path) -> None:
    record(project, event("a"), "2026-01-01T00:00:00.000+00:00")
    record(project, event("b"), "2026-01-01T00:00:01.000+00:00")

    with get_db(project) as conn:
        actors = journal_store.list_agent_sessions(conn)

    assert len(actors) == 1
    assert actors[0].command_count == 2
    assert actors[0].first_seen_at == "2026-01-01T00:00:00.000+00:00"
    assert actors[0].last_seen_at == "2026-01-01T00:00:01.000+00:00"


def test_the_same_agent_id_in_two_sessions_is_two_actors(project: Path) -> None:
    record(
        project, event(session_id="s1", agent_id="a"), "2026-01-01T00:00:00.000+00:00"
    )
    record(
        project, event(session_id="s2", agent_id="a"), "2026-01-01T00:00:01.000+00:00"
    )

    with get_db(project) as conn:
        assert len(journal_store.list_agent_sessions(conn)) == 2
        assert len(journal_store.list_agent_sessions(conn, session_id="s1")) == 1


def test_a_learned_agent_type_is_not_erased_by_a_later_blank(project: Path) -> None:
    """Some payloads name the subagent and some don't. Keep the name."""
    record(
        project,
        event(agent_id="x", agent_type="Explore"),
        "2026-01-01T00:00:00.000+00:00",
    )
    record(project, event(agent_id="x", agent_type=""), "2026-01-01T00:00:01.000+00:00")

    with get_db(project) as conn:
        (actor,) = journal_store.list_agent_sessions(conn)
    assert actor.agent_type == "Explore"


# --- commands --------------------------------------------------------------


def test_a_duplicate_tool_use_id_is_refused(project: Path) -> None:
    first = record(
        project, event("a", tool_use_id="t1"), "2026-01-01T00:00:00.000+00:00"
    )
    second = record(
        project, event("a", tool_use_id="t1"), "2026-01-01T00:00:01.000+00:00"
    )

    assert first is not None
    assert second is None
    with get_db(project) as conn:
        assert len(journal_store.list_commands(conn)) == 1
        # The refused insert must not have bumped the count either.
        assert journal_store.list_agent_sessions(conn)[0].command_count == 1


def test_commands_are_newest_first(project: Path) -> None:
    for i in range(3):
        record(project, event(f"cmd{i}"), f"2026-01-01T00:00:0{i}.000+00:00")

    with get_db(project) as conn:
        assert [c.command for c in journal_store.list_commands(conn)] == [
            "cmd2",
            "cmd1",
            "cmd0",
        ]
        assert len(journal_store.list_commands(conn, limit=2)) == 2


def test_filters_narrow_to_one_actor_or_turn(project: Path) -> None:
    record(project, event("main", prompt_id="p1"), "2026-01-01T00:00:00.000+00:00")
    record(
        project,
        event("sub", agent_id="x", agent_type="Explore", prompt_id="p2"),
        "2026-01-01T00:00:01.000+00:00",
    )
    record(
        project,
        event("read", tool_name="Read", prompt_id="p1"),
        "2026-01-01T00:00:02.000+00:00",
    )

    with get_db(project) as conn:
        assert [c.command for c in journal_store.list_commands(conn, agent_id="")] == [
            "read",
            "main",
        ]
        assert [
            c.command for c in journal_store.list_commands(conn, agent_type="Explore")
        ] == ["sub"]
        assert [
            c.command for c in journal_store.list_commands(conn, prompt_id="p1")
        ] == [
            "read",
            "main",
        ]
        assert [
            c.command for c in journal_store.list_commands(conn, tool_name="Bash")
        ] == [
            "sub",
            "main",
        ]


def test_tool_counts_and_totals(project: Path) -> None:
    record(project, event("a"), "2026-01-01T00:00:00.000+00:00")
    record(project, event("b"), "2026-01-01T00:00:01.000+00:00")
    record(project, event("c", tool_name="Read"), "2026-01-01T00:00:02.000+00:00")
    record(
        project,
        event("d", session_id="s2", agent_id="x"),
        "2026-01-01T00:00:03.000+00:00",
    )

    with get_db(project) as conn:
        assert journal_store.tool_counts(conn) == [("Bash", 3), ("Read", 1)]
        assert journal_store.tool_counts(conn, session_id="s2") == [("Bash", 1)]
        assert journal_store.journal_totals(conn) == (2, 2, 4)


# --- correlating with a scan ----------------------------------------------


def _save_run(project: Path, at: str) -> int:
    with get_db(project) as conn:
        return store.save_run(
            conn,
            Snapshot(generated_at=at, root="src", model="m"),
            DriftReport(),
            None,
            make_baseline=False,
        )


def test_commands_for_run_is_the_window_since_the_previous_run(project: Path) -> None:
    record(project, event("before-everything"), "2026-01-01T00:00:00.000+00:00")
    first = _save_run(project, "2026-01-01T00:00:10+00:00")

    record(project, event("during"), "2026-01-01T00:00:15.500+00:00")
    record(project, event("also-during"), "2026-01-01T00:00:16.000+00:00")
    second = _save_run(project, "2026-01-01T00:00:20+00:00")

    record(project, event("after"), "2026-01-01T00:00:25.000+00:00")

    with get_db(project) as conn:
        # The first run has no predecessor: everything up to it.
        assert [c.command for c in journal_store.commands_for_run(conn, first)] == [
            "before-everything"
        ]
        assert [c.command for c in journal_store.commands_for_run(conn, second)] == [
            "also-during",
            "during",
        ]
        assert journal_store.commands_for_run(conn, 999) == []


def test_millisecond_stamps_compare_correctly_against_run_stamps(project: Path) -> None:
    """Commands carry milliseconds and runs carry seconds; a command in
    the same second as a run must still sort after it."""
    first = _save_run(project, "2026-01-01T00:00:10+00:00")
    record(project, event("same-second"), "2026-01-01T00:00:10.500+00:00")
    second = _save_run(project, "2026-01-01T00:00:11+00:00")

    with get_db(project) as conn:
        assert journal_store.commands_for_run(conn, first) == []
        assert [c.command for c in journal_store.commands_for_run(conn, second)] == [
            "same-second"
        ]


# --- checking the payload contract against recorded data -------------------


def test_observed_fields_counts_values_not_keys(project: Path) -> None:
    """The envelope always holds every declared field, so the question
    worth answering is which ones ever arrived with something in them."""
    record(project, event("ls"), "2026-01-01T00:00:00.000+00:00")
    record(
        project,
        event("rg", agent_id="a1", agent_type="Explore", tool_use_id="t1"),
        "2026-01-01T00:00:01.000+00:00",
    )

    with get_db(project) as conn:
        scanned, fields = journal_store.observed_fields(conn)

    seen = dict(fields)
    assert scanned == 2
    assert seen["session_id"] == 2
    assert seen["tool_name"] == 2
    # Only the subagent's event carried these.
    assert seen["agent_id"] == 1
    assert seen["agent_type"] == 1
    assert seen["tool_use_id"] == 1
    # Never populated in either event — absent from the counts entirely.
    assert "transcript_path" not in seen
    assert "effort" not in seen


def test_observed_fields_surfaces_a_field_this_build_does_not_declare(
    project: Path,
) -> None:
    """The point of keeping the envelope: a payload that grows a field
    says so here, instead of the new field vanishing."""
    grown = HookEvent.from_payload(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "invented_by_a_later_build": "yes",
        }
    )
    record(project, grown, "2026-01-01T00:00:00.000+00:00")

    with get_db(project) as conn:
        _, fields = journal_store.observed_fields(conn)

    assert ("invented_by_a_later_build", 1) in fields
    assert "invented_by_a_later_build" not in HOOK_EVENT_FIELDS


def test_observed_fields_on_an_empty_journal(project: Path) -> None:
    with get_db(project) as conn:
        assert journal_store.observed_fields(conn) == (0, [])
