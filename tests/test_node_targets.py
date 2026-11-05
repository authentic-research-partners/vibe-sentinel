"""Install declarations belong to the selected package, never its siblings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibe_sentinel import hook, node_targets, requirements, safety
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import db_path, get_db, init_db


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO inputs require POSIX")
@pytest.mark.parametrize("filename", ["package.json", ".npmrc"])
def test_special_file_cannot_block_target_triage(tmp_path: Path, filename: str) -> None:
    if filename != "package.json":
        manifest(tmp_path, "example", "react")
    os.mkfifo(tmp_path / filename)
    code = (
        "import json, sys; from pathlib import Path; "
        "from vibe_sentinel.node_targets import assess, NodeManifestUnreadable\n"
        "try:\n"
        " targets = assess('npm install react', Path(sys.argv[1]))\n"
        " print(json.dumps([(t.status, t.reason) for t in targets]))\n"
        "except NodeManifestUnreadable:\n"
        " print('unreadable-manifest')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    if filename == "package.json":
        assert result.stdout.strip() == "unreadable-manifest"
    else:
        assert json.loads(result.stdout) == [["unknown", "unsupported-npmrc"]]


def manifest(directory: Path, name: str, *dependencies: str, **extra: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(
        json.dumps(
            {"name": name, "dependencies": dict.fromkeys(dependencies, "1"), **extra}
        ),
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    manifest(tmp_path, "root", "root-only", workspaces=["apps/*"])
    manifest(tmp_path / "apps/a", "@example/a", "only-a")
    manifest(tmp_path / "apps/b", "b", "only-b")
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "npm --prefix apps/a install root-only only-a",
        "npm install root-only only-a --prefix apps/a",
        "npm install --prefix=apps/a root-only only-a",
        "npm -w @example/a install root-only only-a",
        "npm install root-only only-a --workspace=@example/a",
        "npm install -w apps/a root-only only-a",
        "npm install --workspace ./apps/a root-only only-a",
        "cd apps/a && npm install root-only only-a",
        "cd apps/a&&npm install root-only only-a",
    ],
)
def test_supported_targets_use_only_selected_manifest(
    workspace: Path, command: str
) -> None:
    (target,) = requirements.js_install_targets(command, workspace)
    assert target.status == "resolved"
    assert target.manifest == workspace / "apps/a/package.json"
    assert target.undeclared == ("root-only",)
    assert requirements.undeclared_installs(command, workspace) == ("root-only",)
    assert not requirements.unreadable_install(command, workspace)


def test_repeated_workspace_selectors_compare_separately(workspace: Path) -> None:
    command = "npm install only-a only-b -w @example/a --workspace b"
    first, second = requirements.js_install_targets(command, workspace)
    assert first.manifest == workspace / "apps/a/package.json"
    assert second.manifest == workspace / "apps/b/package.json"
    assert first.undeclared == ("only-b",)
    assert second.undeclared == ("only-a",)
    assert requirements.undeclared_installs(command, workspace) == ("only-b", "only-a")


def test_name_and_directory_collision_does_not_skip_a_target(workspace: Path) -> None:
    manifest(workspace, "root", workspaces=["apps/*", "b"])
    manifest(workspace / "b", "different-package")
    (target,) = node_targets.assess("npm -w b install only-b", workspace)
    assert target.status == "unknown"
    assert target.reason == "ambiguous-workspace-selector"
    assert target.undeclared == () and target.manifest is None


def test_cwd_and_ordered_literal_cd_are_respected(workspace: Path) -> None:
    cwd = workspace / "apps/a/src"
    cwd.mkdir()
    (first, second, third) = node_targets.assess(
        "npm install root-only; cd ../../b && npm install only-a; npm install only-b",
        workspace,
        cwd,
    )
    assert [item.manifest for item in (first, second, third)] == [
        workspace / "apps/a/package.json",
        workspace / "apps/b/package.json",
        workspace / "apps/b/package.json",
    ]
    assert [item.undeclared for item in (first, second, third)] == [
        ("root-only",),
        ("only-a",),
        (),
    ]


def test_quoted_separator_is_a_directory_name_not_control_flow(workspace: Path) -> None:
    manifest(workspace / "apps/a; npm install root-only", "odd", "only-odd")
    (target,) = node_targets.assess(
        'cd "apps/a; npm install root-only" && npm install only-odd root-only',
        workspace,
    )
    assert target.status == "resolved"
    assert target.undeclared == ("root-only",)
    assert target.manifest == workspace / "apps/a; npm install root-only/package.json"


@pytest.mark.parametrize(
    "command",
    [
        "npm i -w packages/web root-only",
        "cd missing && npm install root-only",
        "cd apps/a || npm install root-only",
        "cd apps/a; npm install root-only",
        "cd $TARGET && npm install root-only",
        "X=1 cd apps/a && npm install root-only",
        "command cd apps/a && npm install root-only",
        "builtin cd apps/a && npm install root-only",
        "env -C apps/a npm install root-only",
        "sudo -u somebody npm install root-only",
        "npm install root-only --prefix apps/a --prefix apps/b",
        "npm install root-only --prefix apps/a -w b",
        "npm install root-only --workspaces",
        "npm install root-only -w apps",
        "npm install root-only -w 'apps/*'",
        "npm install root-only -w missing",
        "npm install root-only --userconfig custom.npmrc",
        "npm --userconfig=custom.npmrc install root-only",
        "NPM_CONFIG_WORKSPACE=apps/a npm install root-only",
        'npm install "$PKG" -w @example/a',
        "npm install * -w @example/a",
        "pnpm --filter @example/a add root-only",
        "pnpm add root-only --dir apps/a",
        "yarn workspace @example/a add root-only",
        "yarn --cwd apps/a add root-only",
        "bun add root-only --cwd apps/a",
    ],
)
def test_unknown_target_is_not_compared_with_root(
    workspace: Path, command: str
) -> None:
    targets = requirements.js_install_targets(command, workspace)
    assert targets and all(item.status == "unknown" for item in targets)
    assert all(item.manifest is None and item.undeclared == () for item in targets)
    assert requirements.undeclared_installs(command, workspace) == ()
    assert requirements.unreadable_install(command, workspace)
    assert "installing-undeclared-dependency" in safety.triage(
        "Bash", command, "", workspace
    )


def test_unknown_prompt_does_not_invent_undeclared_names(workspace: Path) -> None:
    command = 'npm install "$PKG" -w @example/a'
    dangers = safety.load_dangers(workspace)
    signals = safety.triage("Bash", command, "", workspace, dangers)
    prompt = safety.build_prompt(
        "Bash", command, "", str(workspace), workspace, signals, [], dangers
    )
    assert "target not compared: dynamic-package-operand" in prompt
    assert "does not establish" in prompt
    assert "declares: (none)" not in prompt
    question = next(
        item.question
        for item in dangers
        if item.id == "installing-undeclared-dependency"
    )
    assert "not proof that any name is undeclared" in question
    assert "specific compared manifests" in question


def test_prompt_identifies_each_compared_manifest(workspace: Path) -> None:
    command = "npm install only-a -w @example/a -w b"
    dangers = safety.load_dangers(workspace)
    signals = safety.triage("Bash", command, "", workspace, dangers)
    prompt = safety.build_prompt(
        "Bash", command, "", str(workspace), workspace, signals, [], dangers
    )
    assert (
        f"Compared {workspace / 'apps/a/package.json'}: requested only-a; undeclared (none)."
        in prompt
    )
    assert (
        f"Compared {workspace / 'apps/b/package.json'}: requested only-a; undeclared only-a."
        in prompt
    )


@pytest.mark.parametrize(
    "key",
    [
        "workspace",
        "workspace[]",
        "workspaces",
        "prefix",
        "include-workspace-root",
        "global",
        "location",
        "userconfig",
    ],
)
def test_npmrc_target_keys_are_unknown_without_exposing_values(
    workspace: Path, key: str
) -> None:
    (workspace / ".npmrc").write_text(
        f"//example.invalid/:_authToken=private-value\n{key}=private-target\n"
    )
    (target,) = node_targets.assess("npm install root-only", workspace)
    assert target.status == "unknown"
    assert target.reason == "npmrc-targeting"
    assert "private" not in repr(target)


def test_non_target_npmrc_values_do_not_enter_evidence(workspace: Path) -> None:
    (workspace / ".npmrc").write_text(
        "//example.invalid/:_authToken=private-value\nregistry=https://example.invalid\n"
    )
    (target,) = node_targets.assess("npm install root-only", workspace)
    assert target.status == "resolved" and target.undeclared == ()
    assert "private-value" not in repr(target)


def test_environment_target_keys_are_not_ignored(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("npm_config_workspace", "apps/a")
    (target,) = node_targets.assess("npm install root-only", workspace)
    assert target.status == "unknown" and target.reason == "environment-targeting"


def test_manifest_symlink_does_not_authorize_external_declarations(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    manifest(outside, "external", "root-only")
    (workspace / "apps/a/package.json").unlink()
    (workspace / "apps/a/package.json").symlink_to(outside / "package.json")
    with pytest.raises(requirements.ManifestUnreadable, match="symlink"):
        requirements.undeclared_installs(
            "npm install root-only --prefix apps/a", workspace
        )


def test_external_or_symlink_directory_is_unknown(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("external")
    manifest(outside, "external", "root-only")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    for command in (
        f"npm --prefix {outside} install root-only",
        "npm --prefix linked install root-only",
        "cd linked/.. && npm install root-only",
    ):
        (target,) = node_targets.assess(command, workspace)
        assert target.status == "unknown" and target.undeclared == ()


def test_malformed_selected_manifest_is_not_empty(workspace: Path) -> None:
    (workspace / "apps/a/package.json").write_text('{"dependencies": []}')
    with pytest.raises(requirements.ManifestUnreadable):
        requirements.undeclared_installs(
            "npm install root-only --prefix apps/a", workspace
        )


def test_object_workspace_declaration_supported(workspace: Path) -> None:
    manifest(workspace, "root", workspaces={"packages": ["apps/*"]})
    (target,) = node_targets.assess("npm -w b install only-b", workspace)
    assert (
        target.manifest == workspace / "apps/b/package.json" and target.undeclared == ()
    )


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "npm ci",
        "npm install -g typescript",
        "npm install ./local-package",
        "npm install foo@npm:bar",
    ],
)
def test_sync_global_and_non_registry_specs_are_outside_named_install_scope(
    workspace: Path, command: str
) -> None:
    assert node_targets.assess(command, workspace) == ()


def test_python_manifest_scope_is_unchanged(workspace: Path) -> None:
    (workspace / "pyproject.toml").write_text(
        '[project]\nname="example"\ndependencies=["httpx"]\n'
    )
    assert requirements.undeclared_installs(
        "cd /tmp && pip install httpx requests", workspace, cwd=workspace / "apps/a"
    ) == ("requests",)


@pytest.mark.parametrize("explicit_cwd", [True, False])
def test_hook_uses_actual_cwd_without_rewriting_journal(
    workspace: Path, explicit_cwd: bool
) -> None:
    init_db(db_path(workspace))
    (workspace / ".vibe-sentinel.toml").write_text(
        '[safety]\nmode="enforce"\n[[danger]]\n'
        'id="installing-undeclared-dependency"\ntitle="Install target"\n'
        'applies_to="undeclared-install"\nverdict="unsafe"\n'
    )
    cwd = workspace / "apps/a"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "workspace-test",
        "tool_name": "Bash",
        "tool_use_id": "first",
        "tool_input": {"command": "npm install root-only"},
    }
    if explicit_cwd:
        payload["cwd"] = str(cwd)
    outcome, reply = hook.guard(json.dumps(payload), default_cwd=cwd)
    assert outcome == "recorded"
    assert reply and reply["hookSpecificOutput"]["permissionDecision"] == "deny"
    with get_db(workspace) as conn:
        (recorded,) = journal_store.list_commands(conn)
        (review,) = journal_store.list_reviews(conn)
    assert recorded.cwd == (str(cwd) if explicit_cwd else "")
    assert recorded.command == "npm install root-only"
    assert "installing-undeclared-dependency" in review.signals

    payload["tool_use_id"] = "second"
    payload["tool_input"] = {"command": "npm install only-a"}
    outcome, reply = hook.guard(json.dumps(payload), default_cwd=cwd)
    assert outcome == "recorded" and reply is None
    with get_db(workspace) as conn:
        assert len(journal_store.list_reviews(conn)) == 1
