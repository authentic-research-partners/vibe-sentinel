"""What the agent cannot reach: the system policy and the managed hook.

Everything else the gate reads is in the working tree, which an agent that
can write files can rewrite. These are not: a policy file and a hook entry in
system directories, which on a machine set up for them only an administrator
can write. ``conftest.system_dir`` points every system location at a temp
directory, so these tests write there and never to the real ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe_sentinel import hook, paths, safety
from vibe_sentinel.__main__ import main
from vibe_sentinel.db.connection import db_path, init_db


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    init_db(db_path(root))
    (root / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    return root


def system_policy(body: str) -> Path:
    path = paths.system_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def project_config(project: Path, body: str) -> None:
    (project / ".vibe-sentinel.toml").write_text(body, encoding="utf-8")


# --- the mode is a floor -------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "local", "expected"),
    [
        ("enforce", "off", "enforce"),
        ("enforce", "observe", "enforce"),
        ("observe", "enforce", "enforce"),
        ("observe", None, "observe"),
        ("off", "observe", "observe"),
    ],
)
def test_the_system_mode_is_a_floor_a_project_may_raise_and_not_lower(
    project: Path, system: str, local: str | None, expected: str
) -> None:
    system_policy(f'[safety]\nmode = "{system}"\n')
    if local is not None:
        project_config(project, f'[safety]\nmode = "{local}"\n')
    assert safety.configured_safety(project)[0] == expected


def test_a_system_ask_holds_over_a_project_allow(project: Path) -> None:
    system_policy('[safety]\nno_verdict = "ask"\n')
    project_config(project, '[safety]\nmode = "enforce"\nno_verdict = "allow"\n')
    assert safety.configured_safety(project) == ("enforce", "ask")


def test_a_system_policy_that_says_nothing_about_no_verdict_changes_nothing(
    project: Path,
) -> None:
    system_policy('[safety]\nmode = "observe"\n')
    project_config(project, '[safety]\nmode = "enforce"\nno_verdict = "allow"\n')
    assert safety.configured_safety(project) == ("enforce", "allow")


def test_an_unreadable_system_policy_is_an_error_not_an_absent_one(
    project: Path,
) -> None:
    system_policy("[safety\nmode = ")
    with pytest.raises(ValueError, match="policy.toml could not be read"):
        safety.configured_safety(project)


def test_the_floor_holds_at_the_hook(project: Path) -> None:
    """The project says off; the organisation said enforce. The edit that
    would switch the gate off is refused anyway."""
    system_policy('[safety]\nmode = "enforce"\n')
    project_config(project, '[safety]\nmode = "off"\n')
    body = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s",
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_use_id": "t1",
            "tool_input": {"command": "echo x >> .vibe-sentinel.toml"},
        }
    )
    _, reply = hook.guard(body)
    assert reply is not None
    assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- dangers a project cannot remove -----------------------------------------------

_SYSTEM_DANGER = """
[[danger]]
id = "production-database"
title = "Touching the production database"
question = "?"
pattern = "prod-db"
verdict = "unsafe"
"""


def test_a_system_danger_is_in_every_project_s_set(project: Path) -> None:
    system_policy(_SYSTEM_DANGER)
    (danger,) = [
        d for d in safety.load_dangers(project) if d.id == "production-database"
    ]
    assert danger.verdict == "unsafe"
    assert danger.source.startswith("system policy")


@pytest.mark.parametrize(
    "config",
    [
        "[safety]\nuse_builtins = false\n",
        '[safety]\nuse = ["deleting-files"]\n',
        '[[danger]]\nid = "production-database"\ntitle = "x"\nquestion = "?"\npattern = "prod-db"\n',
    ],
)
def test_a_project_cannot_remove_or_soften_a_system_danger(
    project: Path, config: str
) -> None:
    system_policy(_SYSTEM_DANGER)
    project_config(project, config)
    (danger,) = [
        d for d in safety.load_dangers(project) if d.id == "production-database"
    ]
    assert danger.verdict == "unsafe"


def test_disabling_a_system_danger_is_an_error_naming_the_system_file(
    project: Path,
) -> None:
    system_policy(_SYSTEM_DANGER)
    project_config(project, '[safety]\ndisable = ["production-database"]\n')
    with pytest.raises(ValueError, match="may not switch off"):
        safety.load_dangers(project)


def test_a_system_policy_with_a_mode_and_no_dangers_is_fine(project: Path) -> None:
    system_policy('[safety]\nmode = "observe"\n')
    assert safety.load_dangers(project)


# --- the system files are the gate's own ------------------------------------------


@pytest.mark.parametrize(
    "location",
    ["system_policy_path", "claude_managed_hook_path", "cursor_enterprise_hooks_path"],
)
def test_writing_a_system_file_is_changing_the_gate(
    project: Path, location: str
) -> None:
    target = getattr(paths, location)()
    dangers = safety.load_dangers(project)
    for tool, command, file in (
        ("Bash", f"echo x | sudo tee '{target}'", ""),
        ("Write", "", str(target)),
    ):
        fired = safety.triage(tool, command, file, project, dangers)
        assert "changing-the-gate" in fired, (tool, command, file)


# --- installing where the agent cannot reach --------------------------------------


def test_a_managed_install_writes_the_hook_and_the_floor(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["hook", "--install", str(project), "--managed", "--mode", "enforce"]) == 0
    )
    entry = json.loads(paths.claude_managed_hook_path().read_text(encoding="utf-8"))
    assert hook.hook_matchers(entry) == ["*"]
    assert safety.declared_safety(paths.system_policy_path())[0] == "enforce"
    assert safety.configured_safety(project)[0] == "enforce"
    assert not (project / ".claude" / "settings.json").exists()


def test_a_managed_cursor_install_writes_the_enterprise_hooks(project: Path) -> None:
    from vibe_sentinel.agents import cursor

    args = ["hook", "--install", str(project), "--agent", "cursor", "--managed"]
    assert main([*args, "--mode", "observe"]) == 0
    data = json.loads(paths.cursor_enterprise_hooks_path().read_text(encoding="utf-8"))
    assert sorted(cursor.wired_events(data)) == sorted(cursor.EVENTS)


def test_without_the_rights_it_prints_what_to_write_and_fails(
    project: Path, system_dir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A printed instruction must never read as an installed gate."""
    system_dir.chmod(0o500)
    try:
        code = main(
            ["hook", "--install", str(project), "--managed", "--mode", "enforce"]
        )
    finally:
        system_dir.chmod(0o700)
    assert code == 2
    out = capfd.readouterr().out
    assert "administrator" in out
    assert str(paths.claude_managed_hook_path()) in out
    assert str(paths.system_policy_path()) in out
    assert 'mode = "enforce"' in out
    assert not paths.system_policy_path().exists()


def test_status_shows_the_managed_level_the_floor_and_a_repeated_install(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from vibe_sentinel import protection

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    async def answering(config: object) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(protection, "check_endpoint", answering)
    system_policy(_SYSTEM_DANGER)
    assert (
        main(["hook", "--install", str(project), "--managed", "--mode", "enforce"]) == 0
    )
    assert main(["hook", "--install", str(project), "--mode", "observe"]) == 0
    capfd.readouterr()

    assert main(["status", str(project)]) == 0
    out = capfd.readouterr().out
    assert "Claude Code, managed" in out
    assert "mode floor enforce" in out
    assert "1 danger(s) no project can remove" in out
    assert "installed at 2 levels" in out
    assert "mode enforce" in out


def test_a_project_install_under_a_higher_floor_says_which_mode_runs(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    system_policy('[safety]\nmode = "enforce"\n')
    assert main(["hook", "--install", str(project), "--mode", "observe"]) == 0
    out = capfd.readouterr().out
    assert "holds the mode at 'enforce'" in out
    assert safety.declared_safety(project / ".vibe-sentinel.toml")[0] == "observe"
