"""Where a project's vibe-sentinel files live.

Three constants, and the system locations outside any project. They have their own module because the
PreToolUse hook has to recognise a project root before it can record
anything, and importing :mod:`vibe_sentinel.config` to learn one filename
would pull in pydantic and the whole configuration model — ~54 ms per
tool call for a string. Same reasoning as :mod:`vibe_sentinel.journal`,
and ``test_hook.py`` asserts the hook's import graph stays clean.

:mod:`vibe_sentinel.config` and :mod:`vibe_sentinel.db.connection` build
on these, so there is still exactly one definition of each name.
"""

from __future__ import annotations

from pathlib import Path

#: Hand-edited probe set and runtime settings, found by walking up from
#: the working directory.
CONFIG_FILENAME = ".vibe-sentinel.toml"

#: Everything vibe-sentinel writes for a project lives under here.
SENTINEL_DIR = ".vibe-sentinel"

#: The history database, inside :data:`SENTINEL_DIR`.
DB_FILENAME = "history.db"


# ---------------------------------------------------------------------------
# What sits outside the working tree
# ---------------------------------------------------------------------------
#
# Each is read from a system directory a project's files cannot reach and,
# on a machine set up for it, only an administrator can write. Fixed per
# operating system and deliberately not configurable — an environment
# variable that moved one would hand the agent the thing it exists to keep
# out of reach. Functions rather than constants so the platform is read
# when asked, and so a test can point one somewhere writable.


def _by_platform(mac: str, linux: str, windows: str) -> Path:
    import sys

    if sys.platform == "darwin":
        return Path(mac)
    if sys.platform == "win32":
        return Path(windows)
    return Path(linux)


def system_policy_path() -> Path:
    """The policy below every project's: a ``[safety] mode`` and
    ``[[danger]]`` set that a project's own config may add to and may not
    lower."""
    return _by_platform(
        "/Library/Application Support/vibe-sentinel/policy.toml",
        "/etc/vibe-sentinel/policy.toml",
        r"C:\ProgramData\vibe-sentinel\policy.toml",
    )


def claude_managed_hook_path() -> Path:
    """This tool's own file in Claude Code's ``managed-settings.d``.

    Claude Code merges every file there with ``managed-settings.json``,
    applies the result above every user and project setting, and runs its
    hooks alongside theirs; ``disableAllHooks`` set outside managed
    settings cannot turn them off (code.claude.com/docs, 2026-10-02). A file
    of its own, so installing never edits an organisation's shared policy.
    """
    return (
        _by_platform(
            "/Library/Application Support/ClaudeCode",
            "/etc/claude-code",
            r"C:\Program Files\ClaudeCode",
        )
        / "managed-settings.d"
        / "vibe-sentinel.json"
    )


def cursor_enterprise_hooks_path() -> Path:
    """Cursor's enterprise ``hooks.json``, which runs first of every level
    (cursor.com/docs/agent/hooks, 2026-10-02)."""
    return _by_platform(
        "/Library/Application Support/Cursor/hooks.json",
        "/etc/cursor/hooks.json",
        r"C:\ProgramData\Cursor\hooks.json",
    )
