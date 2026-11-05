"""Codex's wire contract and the shared gate, without a running judge."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_sentinel import hook, safety
from vibe_sentinel.agents import codex
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import db_path, get_db, init_db
from vibe_sentinel.journal import ActionRequest, Decision


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    hook.set_mode(tmp_path, "enforce")
    return tmp_path


def event(root: Path, tool: str = "Bash", command: str = "ls", **extra: object) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "tool_use_id": "call-1",
            "cwd": str(root),
            "tool_name": tool,
            "tool_input": {"command": command},
            **extra,
        }
    )


def test_identity_and_shell_request(project: Path) -> None:
    request = codex.read(event(project, command="git diff", agent_id="child-1"))
    assert request is not None
    assert (request.agent, request.tool_name, request.command) == (
        "codex",
        "Bash",
        "git diff",
    )
    assert (request.session_id, request.agent_id, request.prompt_id) == (
        "codex-session",
        "child-1",
        "turn-1",
    )
    assert not request.can_ask
    assert ActionRequest.from_dict(request.as_dict()) == request


def test_patch_tracks_all_paths_without_recording_contents(project: Path) -> None:
    patch = (
        "*** Begin Patch\n*** Update File: src/first.py\n@@\n-secret-value\n+new-secret\n"
        "+*** Delete File: not-a-header\n*** Add File: added.py\n+hello\n"
        "*** Delete File: removed.py\n*** Update File: old.py\n*** Move to: new.py\n"
        "@@\n-old\n+new\n*** End Patch\n"
    )
    request = codex.read(
        event(project, "apply_patch", patch, unexpected={"content": "secret-value"})
    )
    assert request is not None and request.tool_name == "Write"
    assert request.targets == tuple(
        str(project / p)
        for p in ("src/first.py", "added.py", "removed.py", "old.py", "new.py")
    )
    assert not request.command
    serialized = json.dumps(request.as_dict())
    assert "secret-value" not in serialized and "new-secret" not in serialized
    assert "not-a-header" not in serialized
    assert ActionRequest.from_dict(json.loads(serialized)) == request


def test_patch_paths_use_subdirectory_cwd(project: Path) -> None:
    subdir = project / "src"
    subdir.mkdir()
    raw = event(
        subdir,
        "apply_patch",
        "*** Begin Patch\n*** Delete File: ../.vibe-sentinel.toml\n*** End Patch",
    )
    result = hook.guard(raw, agent="codex")[1]
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "header", ["Add File", "Update File", "Delete File", "Move to"]
)
def test_second_patch_target_cannot_hide_control_edit(
    project: Path, header: str
) -> None:
    patch = f"*** Begin Patch\n*** Update File: ordinary.py\n@@\n-a\n+b\n*** {header}: .codex/hooks.json\n*** End Patch"
    outcome, response = hook.guard(event(project, "apply_patch", patch), agent="codex")
    assert outcome == "recorded"
    assert (
        response is not None
        and response["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    with get_db(project) as conn:
        review = journal_store.list_reviews(conn)[0]
        assert "changing-the-gate" in review.signals
        assert review.decision == "deny" and review.enforced


def test_patch_outside_project_reaches_the_gate(project: Path) -> None:
    request = codex.read(
        event(
            project,
            "apply_patch",
            "*** Begin Patch\n*** Add File: ../outside.py\n+x\n*** End Patch",
        )
    )
    assert request is not None
    assert "writing-outside-the-project" in safety.triage(
        "Write", "", request.targets[0], project
    )


@pytest.mark.parametrize(
    "raw", ["bad", "[]", "{}", '{"hook_event_name":"PostToolUse","tool_name":"Bash"}']
)
def test_non_actions_are_not_recorded(raw: str) -> None:
    assert codex.read(raw) is None


def test_mcp_arguments_are_not_copied_to_journal(project: Path) -> None:
    raw = event(
        project, "mcp__github__delete_repository", tool_input={"token": "secret-value"}
    )
    request = codex.read(raw)
    assert request is not None and "secret-value" not in json.dumps(request.as_dict())
    assert (
        hook.guard(raw, agent="codex")[1]["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )


@pytest.mark.parametrize("fails", [False, True])
def test_unanswered_review_is_denied_and_recorded_truthfully(
    project: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    async def no_answer(*args: object, **kwargs: object) -> None:
        if fails:
            raise RuntimeError("test failure")
        return None

    monkeypatch.setattr(hook, "review", no_answer)
    response = hook.guard(event(project, command="rm generated.txt"), agent="codex")[1]
    assert response is not None
    output = response["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "cannot ask" in output["permissionDecisionReason"]
    with get_db(project) as conn:
        review = journal_store.list_reviews(conn)[0]
        assert review.verdict == "unreviewed"
        assert review.decision == "deny" and review.enforced


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_non_enforcing_modes_do_not_block(project: Path, mode: str) -> None:
    hook.set_mode(project, mode)
    assert (
        hook.guard(
            event(project, command="echo x >> .vibe-sentinel.toml"), agent="codex"
        )[1]
        is None
    )


def test_explicit_no_verdict_allow_is_respected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".vibe-sentinel.toml").write_text(
        '[safety]\nmode = "enforce"\nno_verdict = "allow"\n'
    )

    async def no_answer(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(hook, "review", no_answer)
    assert (
        hook.guard(event(project, command="rm generated.txt"), agent="codex")[1] is None
    )


def test_reply_never_emits_unsupported_ask() -> None:
    assert codex.reply(None, None) is None
    result = codex.reply(Decision("ask", "unknown"), None)
    assert (
        result is not None
        and result["hookSpecificOutput"]["permissionDecision"] == "deny"
    )


def test_mode_configuration_does_not_install_or_trust_a_hook(project: Path) -> None:
    from vibe_sentinel.__main__ import main

    assert main(["hook", str(project), "--configure"]) == 2
    assert safety.configured_safety(project)[0] == "enforce"
    assert main(["hook", str(project), "--configure", "--mode", "journal"]) == 0
    assert safety.configured_safety(project)[0] == "off"
    assert not (project / ".claude").exists() and not (project / ".codex").exists()


def test_codex_install_never_installs_a_claude_hook(project: Path) -> None:
    from vibe_sentinel.__main__ import main

    assert (
        main(
            ["hook", str(project), "--agent", "codex", "--install", "--mode", "observe"]
        )
        == 2
    )
    assert not (project / ".claude").exists()
    assert safety.configured_safety(project)[0] == "enforce"


def test_mode_changes_are_protected_from_shell_and_mcp(project: Path) -> None:
    for name, command in (
        ("Bash", "vibe-sentinel hook --configure --mode journal"),
        ("mcp__vibe_sentinel__configure_safety", ""),
    ):
        signals = safety.triage(name, command, "", project)
        assert (
            safety.declared_verdict(signals, safety.load_dangers(project))[0]
            == "unsafe"
        )


def test_fast_hook_process_keeps_heavy_imports_out(project: Path) -> None:
    hook.set_mode(project, "off")
    script = (
        "import sys, io;"
        f"sys.stdin=io.StringIO({event(project)!r});"
        "from vibe_sentinel.__main__ import main; main(['hook','--agent','codex']);"
        "print(sorted({'pydantic','httpx','loguru','mcp'} & set(sys.modules)), file=sys.stderr)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, check=True
    )
    assert result.stdout == "" and result.stderr.strip().splitlines()[-1] == "[]"


def test_status_does_not_claim_absence_of_undiscovered_plugin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vibe_sentinel.protection import Protection
    from vibe_sentinel.report import render_protection

    render_protection(
        Protection(sites=[], endpoint="local", model="judge", mode="off"),
        hook.MODE_MEANING,
    )
    output = capsys.readouterr().out
    assert "Codex plugin activation is unverified" in output
    assert "no agent's tool calls are recorded" not in output


def test_model_receives_every_patch_target(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe_sentinel.schemas import SafetyOpinion

    outside = str(project.parent / "outside.txt")
    seen: list[str] = []

    async def judge(
        tool: str, command: str, target: str, *args: object
    ) -> SafetyOpinion:
        seen.append(target)
        return SafetyOpinion(verdict="unsafe", reason="outside the project")

    monkeypatch.setattr(hook, "review", judge)
    patch = "*** Begin Patch\n" + "".join(
        f"*** Add File: ordinary_{i}.py\n+x\n" for i in range(30)
    )
    patch += f"*** Add File: {outside}\n+x\n*** End Patch"
    request = codex.read(event(project, "apply_patch", patch))
    assert request is not None and outside not in request.target
    response = hook.guard(event(project, "apply_patch", patch), agent="codex")[1]
    assert response is not None
    assert len(seen) == 1 and outside in seen[0]
    assert all(target in seen[0] for target in request.targets)


def test_oversized_patch_subject_is_not_judged_as_complete(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def judge(*args: object) -> None:
        pytest.fail("An oversized subject must not reach the model")

    monkeypatch.setattr(hook, "review", judge)
    patch = (
        "*** Begin Patch\n"
        + "".join(f"*** Add File: ../outside_{i:04}.py\n+x\n" for i in range(1000))
        + "*** End Patch"
    )
    response = hook.guard(event(project, "apply_patch", patch), agent="codex")[1]
    assert (
        response is not None
        and response["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert (
        "split it into smaller actions"
        in response["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_actual_codex_configuration_and_plugin_are_control_paths(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_dir = project.parent / "custom-codex"
    plugin_dir = project.parent / "installed-plugin"
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_dir))
    for path in (
        codex_dir / "config.toml",
        codex_dir / "hooks.json",
        plugin_dir / "hooks/hooks.json",
        plugin_dir / "plugin.json",
    ):
        assert "changing-the-gate" in safety.triage("Write", "", str(path), project)
