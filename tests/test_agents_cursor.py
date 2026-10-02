"""The Cursor adapter: its payloads in, its ``permission`` JSON out.

Read against Cursor's hooks reference (cursor.com/docs/agent/hooks). The
engine is the one Claude Code uses; what is tested here is the rim — that
each event reaches the engine as the call it is, that a file's content never
reaches the record, and that every reply is one Cursor can act on.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import hook
from vibe_sentinel.agents import cursor
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.journal import ActionRequest, Decision


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    return tmp_path


def set_mode(project: Path, mode: str) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        f'[safety]\nmode = "{mode}"\n', encoding="utf-8"
    )


def event(root: Path, name: str, **fields: Any) -> str:
    """One Cursor hook payload, with the fields every event carries."""
    body: dict[str, Any] = {
        "hook_event_name": name,
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "model": "some-model",
        "cursor_version": "2.0.0",
        "workspace_roots": [str(root)],
        "user_email": "someone@example.com",
        "transcript_path": "/tmp/t.jsonl",
    }
    body.update(fields)
    return json.dumps(body)


def shell(root: Path, command: str) -> str:
    return event(root, "beforeShellExecution", command=command, cwd=str(root))


def recorded(project: Path) -> list[Any]:
    with get_db(project) as conn:
        return journal_store.list_commands(conn)


# --- reading -----------------------------------------------------------------


def test_a_shell_command_is_the_engine_s_bash(project: Path) -> None:
    request = cursor.read(shell(project, "ls -la"))
    assert request is not None
    assert (request.agent, request.tool_name, request.command) == (
        "cursor",
        "Bash",
        "ls -la",
    )
    assert request.cwd == str(project)
    assert request.session_id == "conv-1"
    assert request.prompt_id == "gen-1"


def test_an_mcp_call_is_named_the_way_the_danger_set_reads_tools(
    project: Path,
) -> None:
    request = cursor.read(
        event(
            project,
            "beforeMCPExecution",
            mcp_server_name="github",
            tool_name="delete_repository",
            tool_input=json.dumps({"repo": "x"}),
        )
    )
    assert request is not None
    assert request.tool_name == "mcp__github__delete_repository"
    assert "tool_input" not in request.envelope


def test_a_read_keeps_the_path_and_never_the_content(project: Path) -> None:
    """beforeReadFile carries the whole file. A read of .env would otherwise
    copy its secrets into the history database."""
    secret = "CONTENT-MARKER-that-must-never-be-recorded"
    raw = event(
        project,
        "beforeReadFile",
        file_path=str(project / ".env"),
        content=f"NOTE={secret}\n",
    )
    request = cursor.read(raw)
    assert request is not None
    assert (request.tool_name, request.target) == ("Read", str(project / ".env"))

    assert hook.guard(raw, agent="cursor")[0] == "recorded"
    with get_db(project) as conn:
        stored = [
            json.dumps(list(row))
            for row in conn.execute("SELECT * FROM agent_commands")
        ]
    assert stored and not any(secret in row for row in stored)


def test_the_person_s_email_is_not_the_record(project: Path) -> None:
    request = cursor.read(shell(project, "ls"))
    assert request is not None
    assert "user_email" not in request.envelope


def test_the_workspace_root_stands_in_for_a_missing_cwd(project: Path) -> None:
    request = cursor.read(event(project, "beforeReadFile", file_path="/x"))
    assert request is not None
    assert request.cwd == str(project)


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", json.dumps({"command": "ls"})])
def test_what_is_not_a_cursor_event_is_not_recorded(raw: str) -> None:
    assert cursor.read(raw) is None


# --- replying -------------------------------------------------------------------


def _request(event_name: str) -> ActionRequest:
    return ActionRequest(agent="cursor", envelope={"hook_event_name": event_name})


def test_every_reply_is_explicit_because_cursor_reads_silence_as_failure() -> None:
    assert cursor.reply(None, _request("beforeShellExecution")) == {
        "permission": "allow"
    }
    assert cursor.reply(None, None) == {"permission": "allow"}


def test_a_deny_carries_the_reason_to_both_the_person_and_the_agent() -> None:
    reply = cursor.reply(Decision("deny", "no"), _request("beforeShellExecution"))
    assert reply == {"permission": "deny", "user_message": "no", "agent_message": "no"}


@pytest.mark.parametrize("name", ["beforeShellExecution", "beforeMCPExecution"])
def test_an_event_that_can_ask_asks(name: str) -> None:
    assert (
        cursor.reply(Decision("ask", "unsure"), _request(name))["permission"] == "ask"
    )


def test_a_read_cannot_ask_so_it_is_refused_with_the_reason() -> None:
    """beforeReadFile takes allow or deny. An ask that became allow would be
    a call nobody judged passing as one somebody did."""
    reply = cursor.reply(Decision("ask", "unsure"), _request("beforeReadFile"))
    assert reply["permission"] == "deny"
    assert reply["user_message"].startswith("unsure")
    assert "cannot ask" in reply["user_message"]


# --- the gate, end to end -----------------------------------------------------


def test_an_unsafe_shell_command_is_refused_under_enforce(project: Path) -> None:
    set_mode(project, "enforce")
    outcome, reply = hook.guard(
        shell(project, "echo x >> .vibe-sentinel.toml"), agent="cursor"
    )
    assert outcome == "recorded"
    assert reply is not None and reply["permission"] == "deny"


def test_reading_a_key_is_refused_under_enforce(project: Path) -> None:
    """secret-store-target, settled without a model: the home-directory rule
    for reads reaches Cursor's reads as it reaches Claude Code's Read."""
    set_mode(project, "enforce")
    raw = event(
        project, "beforeReadFile", file_path="/Users/dev/.ssh/id_rsa", content="k"
    )
    reply = hook.guard(raw, agent="cursor")[1]
    assert reply is not None and reply["permission"] == "deny"


def test_a_deleting_mcp_tool_is_refused_under_enforce(project: Path) -> None:
    set_mode(project, "enforce")
    raw = event(
        project,
        "beforeMCPExecution",
        mcp_server_name="github",
        tool_name="delete_repository",
        tool_input="{}",
    )
    reply = hook.guard(raw, agent="cursor")[1]
    assert reply is not None and reply["permission"] == "deny"


def test_ordinary_work_is_allowed_out_loud(project: Path) -> None:
    set_mode(project, "enforce")
    assert hook.guard(shell(project, "ls -la"), agent="cursor")[1] == {
        "permission": "allow"
    }
    assert recorded(project)[0].command == "ls -la"


def test_outside_a_watched_project_it_still_answers(tmp_path: Path) -> None:
    outside = tmp_path / "nowhere"
    outside.mkdir()
    outcome, reply = hook.guard(shell(outside, "ls"), agent="cursor")
    assert outcome == "unwatched"
    assert reply == {"permission": "allow"}


# --- installing -----------------------------------------------------------------


def test_install_wires_the_three_gated_events_and_keeps_the_rest(project: Path) -> None:
    path = project / ".cursor" / "hooks.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"version": 1, "hooks": {"stop": [{"command": "./audit.sh"}]}}),
        encoding="utf-8",
    )
    written, changed = cursor.install(project, hook.HOOK_TIMEOUT)
    assert (written, changed) == (path, True)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["stop"] == [{"command": "./audit.sh"}]
    for name in cursor.EVENTS:
        assert data["hooks"][name] == [
            {
                "command": "vibe-sentinel hook --agent cursor",
                "timeout": hook.HOOK_TIMEOUT,
            }
        ]
    assert not any(
        "failClosed" in e for entries in data["hooks"].values() for e in entries
    )
    assert sorted(cursor.wired_events(data)) == sorted(cursor.EVENTS)

    assert cursor.install(project, hook.HOOK_TIMEOUT) == (path, False)


def test_the_cli_installs_for_cursor_and_sets_the_mode(project: Path) -> None:
    from vibe_sentinel.__main__ import main
    from vibe_sentinel.safety import configured_safety

    args = ["hook", "--install", str(project), "--agent", "cursor", "--mode", "observe"]
    assert main(args) == 0
    assert (project / ".cursor" / "hooks.json").is_file()
    assert not (project / ".claude" / "settings.json").exists()
    assert configured_safety(project)[0] == "observe"


def test_status_names_a_cursor_hook(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe_sentinel import protection

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    cursor.install(project, hook.HOOK_TIMEOUT)
    (site,) = [
        s for s in protection.hook_sites(project) if s.agent == "Cursor" and s.matchers
    ]
    assert site.scope == "project"
    assert sorted(site.matchers) == sorted(cursor.EVENTS)


def test_an_agent_rewriting_cursor_s_hooks_is_changing_the_gate(project: Path) -> None:
    from vibe_sentinel import safety

    dangers = safety.load_dangers(project)
    for tool, command, target in (
        ("Write", "", ".cursor/hooks.json"),
        ("Bash", "echo '{}' > .cursor/hooks.json", ""),
    ):
        fired = safety.triage(tool, command, target, project, dangers)
        assert "changing-the-gate" in fired, (tool, command, target)


# --- the process -----------------------------------------------------------------


def test_the_cursor_hook_process_answers_on_stdout_and_loads_nothing_heavy(
    project: Path,
) -> None:
    """The fast path, as Cursor runs it: one JSON reply on stdout, and none of
    the three imports the hook's budget excludes."""
    script = (
        "import sys, io;"
        f"sys.stdin = io.StringIO({shell(project, 'ls')!r});"
        "from vibe_sentinel.__main__ import main;"
        "main(['hook', '--agent', 'cursor']);"
        "print(sorted({'pydantic', 'httpx', 'loguru'} & set(sys.modules)), file=sys.stderr)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {"permission": "allow"}
    assert result.stderr.strip().splitlines()[-1] == "[]"
