"""Run opt-in security checks and report their coverage separately from drift."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from vibe_sentinel.config import SentinelConfig


CheckId = Literal["npm-advisories", "js-source", "browser-artifacts"]
CheckStatus = Literal["complete", "findings", "unavailable", "error", "not_requested"]


class SecurityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    message: str
    path: str | None = None
    line: int | None = None
    rule: str | None = None
    package: str | None = None
    version: str | None = None
    advisory: str | None = None


class SecurityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CheckId
    status: CheckStatus
    summary: str
    findings: list[SecurityFinding]
    coverage: dict[str, Any] | None = None


class SecurityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    checks: tuple[SecurityCheck, ...]

    @property
    def outcome(self) -> Literal["completed", "findings", "incomplete", "error"]:
        if any(check.status == "error" for check in self.checks):
            return "error"
        if any(check.status == "unavailable" for check in self.checks):
            return "incomplete"
        if any(check.findings or check.status == "findings" for check in self.checks):
            return "findings"
        return "completed"

    @property
    def exit_code(self) -> int:
        return {"completed": 0, "findings": 1, "incomplete": 2, "error": 2}[
            self.outcome
        ]


def selection_error(
    lockfile: str | None,
    advisory_db: str | None,
    source: str | None,
    browser_artifacts: str | None,
) -> str:
    if lockfile is None and advisory_db is not None:
        return "Select an npm lockfile for the advisory database."
    if all(target is None for target in (lockfile, source, browser_artifacts)):
        return "Select at least one security check."
    return ""


def _check_result(expected: CheckId, payload: dict[str, Any]) -> SecurityCheck:
    try:
        check = SecurityCheck.model_validate(payload)
        if check.id == expected and check.status != "not_requested":
            return check
    except ValidationError:
        pass
    return SecurityCheck(
        id=expected,
        status="error",
        summary="The check returned an invalid result.",
        findings=[],
    )


async def collect(
    root: Path,
    config: SentinelConfig,
    *,
    lockfile: str | None,
    advisory_db: Path | None,
    source: str | None,
    browser_artifacts: str | None,
    tools_directory: Path | None = None,
) -> SecurityReport:
    from vibe_sentinel import (
        security_artifacts,
        security_osv,
        security_source,
        security_setup,
    )

    tools = security_setup.resolve(tools_directory)

    reports = [
        SecurityCheck(
            id=check,
            status="not_requested",
            summary="This check was not selected.",
            findings=[],
        )
        for check in ("npm-advisories", "js-source", "browser-artifacts")
    ]
    if lockfile is not None:
        database = advisory_db or tools.get("advisory_db")
        reports[0] = _check_result(
            "npm-advisories",
            security_osv.check(root, lockfile, database, executable=tools.get("osv"))
            if database is not None
            else {
                "id": "npm-advisories",
                "status": "unavailable",
                "summary": "Run 'vibe-sentinel security setup' or select a prepared advisory database.",
                "findings": [],
            },
        )
    if source is not None:
        reports[1] = _check_result(
            "js-source",
            security_source.check(
                root,
                source,
                executable=tools.get("semgrep"),
                typescript=tools.get("typescript"),
                node=tools.get("node"),
            ),
        )
    if browser_artifacts is not None:
        reports[2] = _check_result(
            "browser-artifacts",
            await security_artifacts.check(root, browser_artifacts, config),
        )
    return SecurityReport(schema_version=1, checks=tuple(reports))
