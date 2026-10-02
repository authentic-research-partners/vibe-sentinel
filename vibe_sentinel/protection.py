"""What is protecting this repository, read from where each fact lives.

``vibe-sentinel status`` answers the first question a person asks of a
guardrail — is it on — and every part of the answer is read from the place
that decides it rather than from a second copy: the hook from the agent's
own settings files, the mode through :func:`safety.configured_safety` (the
reader the hook uses in front of every call), the judge by asking it, and
the verdicts from ``command_reviews``.

Each part that could not be read says so. A settings file that will not
parse is not a file with no hook in it, and a mode that will not parse is
not ``off``: both are reported as what they are, beside the rest.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from vibe_sentinel import hook
from vibe_sentinel.llm import check_endpoint
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import SchemaMismatchError, db_path, get_db
from vibe_sentinel import paths
from vibe_sentinel.safety import configured_safety, declared_safety, system_dangers


class HookSite(BaseModel):
    """One settings file and what it says about this hook."""

    agent: str
    scope: str
    path: Path
    matchers: list[str] = []
    """Empty when the file is there and runs no hook of this tool's."""
    present: bool = False
    error: str = ""


class Protection(BaseModel):
    """Everything ``status`` reports, each part read or marked unreadable."""

    sites: list[HookSite]
    mode: str = ""
    no_verdict: str = ""
    mode_error: str = ""
    endpoint: str
    model: str
    judge_reachable: bool = False
    judge_detail: str = ""
    system_path: Path | None = None
    system_present: bool = False
    system_mode: str = ""
    system_no_verdict: str = ""
    system_dangers: int = 0
    system_error: str = ""
    session_id: str = ""
    session_commands: int = 0
    session_counts: dict[str, int] = {}
    """``"verdict/decision"`` -> how many, for the newest agent session."""
    journal_error: str = ""

    @property
    def installed(self) -> list[HookSite]:
        return [s for s in self.sites if s.matchers]

    @property
    def repeated(self) -> dict[str, int]:
        """Agents whose hook is installed at more than one level, and how
        many: each of their calls runs the hook that many times."""
        counts: dict[str, int] = {}
        for site in self.installed:
            counts[site.agent] = counts.get(site.agent, 0) + 1
        return {agent: n for agent, n in counts.items() if n > 1}

    @property
    def ok(self) -> bool:
        """Installed, readable, and — when the gate judges — answered."""
        if not self.installed or self.mode_error or self.system_error:
            return False
        if any(s.error for s in self.sites):
            return False
        return self.mode == "off" or self.judge_reachable


def hook_sites(root: Path) -> list[HookSite]:
    """Every settings file that could carry the hook, for every agent with
    an adapter, and what each holds.

    For Claude Code a site lists the matchers of its ``PreToolUse`` entries;
    for Cursor, the events wired — each is what decides which calls reach
    the gate.
    """
    from vibe_sentinel.agents import cursor

    sites: list[HookSite] = []
    for scope, path in hook.claude_settings_files(root):
        site = HookSite(agent="Claude Code", scope=scope, path=path)
        try:
            settings = hook.read_settings(path)
        except ValueError as e:
            site.present, site.error = True, str(e)
        else:
            if settings is not None:
                site.present = True
                site.matchers = hook.hook_matchers(settings)
        sites.append(site)
    for scope, path in cursor.hooks_files(root):
        site = HookSite(agent="Cursor", scope=scope, path=path)
        try:
            data = cursor.read_hooks(path)
        except ValueError as e:
            site.present, site.error = True, str(e)
        else:
            if data is not None:
                site.present = True
                site.matchers = cursor.wired_events(data)
        sites.append(site)
    return sites


async def gather(config: SentinelConfig, root: Path) -> Protection:
    """Read every part of the answer; never raises for a part it cannot read."""
    report = Protection(
        sites=hook_sites(root),
        endpoint=config.llm_endpoint,
        model=config.llm_model,
    )
    try:
        report.mode, report.no_verdict = configured_safety(root)
    except ValueError as e:
        report.mode_error = str(e)

    report.system_path = paths.system_policy_path()
    report.system_present = report.system_path.is_file()
    if report.system_present:
        try:
            mode, no_verdict = declared_safety(report.system_path)
            report.system_mode, report.system_no_verdict = mode or "", no_verdict or ""
            report.system_dangers = len(system_dangers())
        except ValueError as e:
            report.system_error = str(e)

    report.judge_reachable, report.judge_detail = await check_endpoint(config)

    if db_path(root).is_file():
        try:
            with get_db(root) as conn:
                session = journal_store.latest_reviewed_session(conn)
                if session is not None:
                    report.session_id = session
                    report.session_commands = journal_store.commands_in_session(
                        conn, session
                    )
                    report.session_counts = {
                        f"{verdict}/{decision}": n
                        for (verdict, decision), n in journal_store.review_counts(
                            conn, session
                        ).items()
                    }
        except SchemaMismatchError as e:
            report.journal_error = str(e)
    return report


def enforce_preview(
    counts: dict[tuple[str, str], int], no_verdict: str
) -> dict[str, int]:
    """What ``enforce`` would have done with these recorded verdicts.

    The rule the hook applies: ``unsafe`` refused, a verdict that is not one
    asked about under ``no_verdict = "ask"``, the rest allowed.
    """
    preview = {"refused": 0, "asked": 0, "allowed": 0}
    for (verdict, _), n in counts.items():
        if verdict == "unsafe":
            preview["refused"] += n
        elif verdict in ("unclear", "unreviewed") and no_verdict == "ask":
            preview["asked"] += n
        else:
            preview["allowed"] += n
    return preview
