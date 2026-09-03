"""The collect stage the three deterministic gates share.

Licences, dependency provenance and credentials at rest ask a different
question from a probe, and the difference is what the finding *is*. A
probe's finding is a transition — this directory gained four modules,
this construct spread into a third layer — and reporting it once, when it
happens, is exactly right. A licence or a key sitting in the tree is not
a transition. It is a state, and it is as true on the two-hundredth scan
as the first.

That distinction is the whole reason this module exists. These three used
to run as probes, and routing a standing fact through a diff breaks it in
two places at once:

  - ``compare()`` reports a key only when it *appears*. A ``.env`` that
    was already committed on the first scan lands in the baseline — the
    first run makes the baseline unconditionally — and is present and
    unchanged on every scan after it, so it is never a change and never
    mentioned again.
  - Accepting drift means making it the baseline, which records nobody's
    name, no reason and no date. Accepting one of these findings means
    writing a pin, which :mod:`vibe_sentinel.pins` refuses to let you
    write without all three.

So a gate reports every run, at its own severity, and only a pin clears
it. What this module adds is one shape — :class:`GateFinding` — so the
scan can record and render all three without knowing what a licence
expression or a git exposure is, and one place where each gate's own
vocabulary is translated into it.

Nothing here judges. Each function calls the gate's own collector, which
is the same call ``vibe-sentinel licenses`` / ``packages`` /
``credentials`` makes, and reshapes what comes back. A gate that cannot
complete returns ``ok=False`` carrying the reason, for the same purpose
a failed probe is recorded rather than raised: one broken policy file
must not lose the other two gates, and a gate that reported nothing
would read on the next run as a tree somebody had cleaned up.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from vibe_sentinel.schemas import GateFinding, GateReport, GateState

if TYPE_CHECKING:
    from vibe_sentinel.config import SentinelConfig

#: The gates that run as part of a scan, in the order they are reported.
GATES: tuple[str, ...] = ("licenses", "packages", "credentials")


def _failed(gate: str, error: str, started: float) -> GateReport:
    """A gate that could not answer, recorded as not having answered."""
    logger.warning("gate {}: {}", gate, error)
    return GateReport(
        gate=gate,
        ok=False,
        error=error,
        summary=f"{gate} did not complete: {error}",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def shape_licenses(
    ok_count: int,
    bad: Iterable[Any],
    in_source: Iterable[Any],
    accepted: Callable[[str], bool],
    policy: Any,
    project_spdx: str = "",
    duration_ms: int = 0,
) -> GateReport:
    """Turn one licence check into findings, without re-running it.

    Separate from :func:`collect_licenses` so ``vibe-sentinel licenses``
    can record exactly the result it printed. Recomputing it here would
    make the record a second, differently-configured check that merely
    usually agrees — and the gate command is the one that carries
    ``--policy``.

    Two populations, one gate. A dependency's licence arrives through
    package metadata; a vendored file's arrives in its header and touches
    ``pyproject.toml`` never. Keyed apart: ``<package>`` and
    ``source:<path>``.
    """
    findings: list[GateFinding] = []
    for violation in bad:
        resolved = violation.resolved
        findings.append(
            GateFinding(
                gate="licenses",
                key=resolved.name,
                kind="dependency",
                subject=f"{resolved.name}:{resolved.version}",
                label=f"{resolved.name} {resolved.version} — {resolved.spdx}",
                detail=violation.why,
                verdict="not-accepted",
                failing=True,
                attrs={
                    "spdx": resolved.spdx,
                    "version": resolved.version,
                    "source": resolved.source,
                    "category": policy.category_of(resolved.spdx) or "uncategorised",
                },
            )
        )

    for found in in_source:
        allowed = accepted(found.spdx)
        findings.append(
            GateFinding(
                gate="licenses",
                key=f"source:{found.path}",
                kind="vendored",
                subject=found.path,
                label=f"{found.path} carries {found.spdx}",
                detail=f"licence header found via {found.source}",
                verdict="accepted" if allowed else "not-accepted",
                failing=not allowed,
                attrs={
                    "spdx": found.spdx,
                    "source": found.source,
                    "category": policy.category_of(found.spdx) or "uncategorised",
                },
            )
        )

    failing = sum(1 for f in findings if f.failing)
    return GateReport(
        gate="licenses",
        findings=tuple(findings),
        summary=(
            f"{ok_count} package(s) accepted, {failing} finding(s)"
            + (f"; project licence {project_spdx}" if project_spdx else "")
        ),
        duration_ms=duration_ms,
    )


def _unconfigured(gate: str, hint: str, started: float) -> GateReport:
    """A gate this project never declared a policy for.

    Not a failure. A project that has not decided which licences it
    accepts has not broken anything, and a scan that exited non-zero over
    it would be a gate people turn off. But it has not passed either —
    the report says so and names the block to add, because a gate shown
    as clean on a policy nobody wrote is the one result here that would
    be a lie.
    """
    return GateReport(
        gate=gate,
        ok=False,
        configured=False,
        error=hint,
        summary=f"{gate}: no policy declared",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def collect_licenses(root: Path, policy_path: Path | None = None) -> GateReport:
    """Run the licence gate's collect stage over ``root``."""
    started = time.monotonic()
    import importlib.metadata as md

    from vibe_sentinel.licenses import (
        check,
        evaluate_expression,
        load_policy,
        project_license,
        scan_source,
    )

    try:
        policy = load_policy(policy_path, root=root)
    except FileNotFoundError as e:
        return _unconfigured("licenses", str(e), started)
    except (KeyError, ValueError) as e:
        return _failed("licenses", str(e), started)

    ok, bad = check(list(md.distributions()), policy)
    own = project_license(root, policy.all_markers())
    return shape_licenses(
        ok_count=len(ok),
        bad=bad,
        in_source=scan_source(root, markers=policy.all_markers()),
        accepted=lambda spdx: evaluate_expression(
            spdx, policy.allowed, policy.exceptions
        ),
        policy=policy,
        project_spdx=own.spdx if own else "",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def shape_packages(
    inventory: Any,
    findings: Iterable[Any],
    online: bool = False,
    duration_ms: int = 0,
    adjudication: Any = None,
) -> GateReport:
    """Turn one provenance audit into findings, without re-running it.

    ``online`` is carried into the summary rather than assumed. A name
    reported as ``phantom`` after the index was asked and one reported
    before it was asked are different claims, and the record has to keep
    them apart — a lookup that never happened must never read later as a
    lookup that came back empty.

    ``adjudication`` settles the near-misses and nothing else. Every
    finding is still shaped and recorded — a pair the model called
    ``distinct`` is a thing that was found and then decided, which is a
    different record from a pair that was never there.
    """
    judged = (
        {j.finding.name: j for j in adjudication.judgements}
        if adjudication is not None
        else {}
    )
    shaped = []
    for finding in findings:
        judgement = judged.get(finding.name) if finding.kind == "near-miss" else None
        attrs = {"evidence": " | ".join(finding.evidence[:8])}
        if judgement is not None and judgement.suspect:
            attrs["suspect"] = judgement.suspect
        shaped.append(
            GateFinding(
                gate="packages",
                key=f"{finding.kind}:{finding.name}",
                kind=finding.kind,
                subject=finding.name,
                label=f"{finding.name} — {finding.kind}",
                detail=finding.detail,
                verdict=judgement.verdict if judgement is not None else finding.kind,
                failing=judgement.failing if judgement is not None else True,
                pinned=judgement is not None and judgement.verdict == "pinned",
                adjudicated=judgement is not None and judgement.reviewed,
                reason=(
                    judgement.reason
                    if judgement is not None and judgement.reason
                    else finding.remediation
                ),
                attrs=attrs,
            )
        )
    env = inventory.environment
    failing = sum(1 for f in shaped if f.failing)
    reviewed = adjudication is not None and adjudication.reviewed
    return GateReport(
        gate="packages",
        adjudicated=reviewed,
        findings=tuple(shaped),
        summary=(
            f"{len(inventory.installed)} installed, {len(inventory.direct)} declared "
            f"in {env.label}; {len(shaped)} finding(s), {failing} failing, "
            + ("index queried" if online else "index not queried")
        ),
        duration_ms=duration_ms,
    )


async def collect_packages(
    root: Path,
    config: SentinelConfig | None = None,
    policy_path: Path | None = None,
    use_model: bool = True,
) -> GateReport:
    """Run the provenance gate's collect stage over ``root``.

    The index is never queried here. ``--online`` is a deliberate act on
    the gate command, and a scan is not the place to decide a third party
    should hear about this project's dependency names.

    The model is asked about one thing and only when there is one to ask
    about: two installed names an edit apart. Without it every near-miss
    stands, exactly as it did before this step existed.
    """
    started = time.monotonic()
    from vibe_sentinel.packages import (
        adjudicate,
        audit,
        by_severity,
        load_policy,
        take_inventory,
    )

    try:
        policy = load_policy(policy_path, root=root)
    except (FileNotFoundError, KeyError, ValueError) as e:
        return _failed("packages", str(e), started)

    try:
        inventory = take_inventory(root)
    except (OSError, ValueError) as e:
        return _failed("packages", str(e), started)

    if not inventory.declared:
        # Every installed package is an orphan when nothing declares
        # anything, so the finding set is not information — it is the
        # absence of a declared set, said once per package. Provenance
        # needs something to be provenance *against*. The gate command
        # still reports it, because there you asked; a scan must not
        # exit non-zero over the shape of the ambient environment.
        return _unconfigured(
            "packages",
            f"nothing in {root} declares its dependencies, so every installed "
            f"package would read as an orphan. Declare them in "
            f"pyproject.toml, or run `vibe-sentinel packages` to see the "
            f"environment as it is.",
            started,
        )

    findings = by_severity(audit(inventory, policy))
    adjudged = await adjudicate(
        inventory, findings, policy, config, use_model=use_model
    )
    return shape_packages(
        inventory,
        findings,
        online=False,
        duration_ms=int((time.monotonic() - started) * 1000),
        adjudication=adjudged,
    )


def shape_credentials(
    scan: Any,
    found: Any,
    policy: Any,
    duration_ms: int = 0,
) -> GateReport:
    """Turn one credential adjudication into findings, without re-running it.

    No value is ever put in a finding. ``Candidate.excerpt`` is the
    redacted one — its own module names it as the only one of the two
    that may be printed, logged or written to the database — and the raw
    value reaches neither this function nor anything downstream of it.
    """
    failing = {id(j) for j in found.failing(policy)}
    findings = [
        GateFinding(
            gate="credentials",
            key=f"{j.candidate.rule}:{j.candidate.path}",
            kind=j.candidate.rule,
            subject=j.candidate.path,
            label=f"{j.candidate.path} — {j.candidate.title}",
            detail=j.candidate.excerpt,
            risk=j.candidate.exposure,
            verdict=j.verdict,
            failing=id(j) in failing,
            pinned=j.verdict == "pinned",
            adjudicated=j.reviewed,
            reason=j.reason,
            attrs={
                "matches": str(len(j.candidate.lines)),
                "applies_to": j.candidate.applies_to,
            },
        )
        for j in found.judgements
    ]
    return GateReport(
        gate="credentials",
        adjudicated=found.reviewed,
        findings=tuple(findings),
        summary=(
            f"{scan.files_read} file(s) read, {len(scan.candidates)} candidate(s), "
            f"{len(failing)} failing"
            + ("" if found.reviewed else " — NOT adjudicated, verdicts are mechanical")
        ),
        duration_ms=duration_ms,
    )


async def collect_credentials(
    root: Path,
    config: SentinelConfig | None = None,
    policy_path: Path | None = None,
    use_model: bool = True,
) -> GateReport:
    """Run the credentials gate over ``root``.

    A truncated walk is a failure, not a small answer. A count that is a
    floor, recorded beside counts that were inventories, is the one
    result that reads later as a tree getting cleaner.
    """
    started = time.monotonic()
    from vibe_sentinel import credentials as creds

    try:
        secrets = creds.load_secrets(root)
        policy = creds.load_policy(policy_path, root=root)
    except (FileNotFoundError, KeyError, ValueError) as e:
        return _failed("credentials", str(e), started)

    scan = creds.collect(root, secrets, policy)
    if scan.truncated:
        return _failed(
            "credentials",
            f"the walk stopped at max_files ({policy.max_files}), so this is a "
            f"floor rather than an inventory. Raise [credentials] max_files, or "
            f"narrow the tree with [credentials] exclude.",
            started,
        )

    found = await creds.adjudicate(scan, secrets, policy, config, use_model=use_model)
    return shape_credentials(
        scan, found, policy, duration_ms=int((time.monotonic() - started) * 1000)
    )


async def collect_all(
    root: Path,
    config: SentinelConfig | None = None,
    use_model: bool = True,
) -> GateState:
    """Run every gate's collect stage over ``root``.

    Sequential, like the probes and for the same reason: three
    filesystem walks, not three GPU calls. The one model-bound part —
    adjudicating the credential candidates — fans out inside
    ``credentials.review`` under its own semaphore, which is where the
    concurrency belongs.

    An exception from one gate is turned into a failed report rather than
    allowed out, so the other two still answer.
    """
    reports: list[GateReport] = []
    for gate in GATES:
        started = time.monotonic()
        try:
            if gate == "licenses":
                reports.append(collect_licenses(root))
            elif gate == "packages":
                reports.append(
                    await collect_packages(root, config, use_model=use_model)
                )
            else:
                reports.append(
                    await collect_credentials(root, config, use_model=use_model)
                )
        except Exception as e:  # noqa: BLE001 - one gate must not lose the others
            reports.append(_failed(gate, f"{type(e).__name__}: {e}", started))
    return GateState(reports=tuple(reports))
