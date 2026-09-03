"""Comparing a scan against the recorded structure.

The comparison itself is mechanical and needs no model. An observation
key absent from the baseline *appeared*; one no longer present
*disappeared*; one whose value moved past the probe's tolerance *grew* or
*shrank*. The model's later analysis rates these, but cannot invent or
suppress one — which is what keeps the drift set reproducible.

The same diff answers a horizon comparison — a scan reports what moved
since the baseline *and* what moved since the newest run a week or a
month old, because one point in time shows drift at one scale. Only the
second end changes; :mod:`vibe_sentinel.horizons` picks it and
:mod:`vibe_sentinel.engine` loops over them.

Storage lives in :mod:`vibe_sentinel.db`; this module holds only the
diff. Nothing here reads the database or the clock, which is what keeps
``compare`` testable against two literals.
"""

from __future__ import annotations

from vibe_sentinel.schemas import Change, DriftReport, Snapshot
from vibe_sentinel.templates import Probe


def _moved(probe: Probe | None, before: float, after: float) -> bool:
    """Whether this movement counts as drift.

    The probe owns the threshold, because the probe is what declared
    it — absolute in the value's own units, or relative to the value it
    is compared against. A probe that ran but is no longer declared has
    no threshold to apply, so any movement counts.
    """
    return probe.moved(before, after) if probe else before != after


def compare(
    baseline: Snapshot | None,
    current: Snapshot,
    probes: list[Probe],
) -> DriftReport:
    """Diff ``current`` against ``baseline``.

    Severity here is deliberately coarse — ``appeared`` and
    ``disappeared`` are ``medium`` because a structural element arriving
    or vanishing is a real organizational event, while a value moving
    past tolerance is ``low``. The analysis pass refines these; this
    function only decides what counts as a change at all.
    """
    if baseline is None:
        return DriftReport(
            current_at=current.generated_at,
            first_run=True,
            assessment=(
                "No previous run — this scan establishes the baseline. The "
                "next scan reports drift against it."
            ),
        )

    by_id = {p.id: p for p in probes}
    changes: list[Change] = []

    for probe_id, current_result in current.probes.items():
        if not current_result.ok:
            # A failed probe produced no observations. Treating that as
            # "everything it used to see has disappeared" would bury the
            # real signal under a wall of phantom changes.
            continue
        before_result = baseline.probes.get(probe_id)
        if before_result is None:
            changes.append(
                Change(
                    probe_id=probe_id,
                    key=probe_id,
                    kind="appeared",
                    label=f"probe {probe_id} has no baseline yet",
                    severity="info",
                )
            )
            continue

        before = before_result.by_key()
        after = current_result.by_key()
        probe = by_id.get(probe_id)

        for key in sorted(set(after) - set(before)):
            obs = after[key]
            changes.append(
                Change(
                    probe_id=probe_id,
                    key=key,
                    kind="appeared",
                    after=obs.value,
                    label=obs.label or key,
                    severity="medium",
                )
            )
        for key in sorted(set(before) - set(after)):
            obs = before[key]
            changes.append(
                Change(
                    probe_id=probe_id,
                    key=key,
                    kind="disappeared",
                    before=obs.value,
                    label=obs.label or key,
                    severity="medium",
                )
            )
        for key in sorted(set(before) & set(after)):
            old, new = before[key].value, after[key].value
            if old is None or new is None or not _moved(probe, old, new):
                continue
            changes.append(
                Change(
                    probe_id=probe_id,
                    key=key,
                    kind="grew" if new > old else "shrank",
                    before=old,
                    after=new,
                    label=after[key].label or key,
                    severity="low",
                )
            )

    for probe_id in sorted(set(baseline.probes) - set(current.probes)):
        changes.append(
            Change(
                probe_id=probe_id,
                key=probe_id,
                kind="disappeared",
                label=f"probe {probe_id} was not run this time",
                severity="info",
            )
        )

    return DriftReport(
        baseline_at=baseline.generated_at,
        current_at=current.generated_at,
        changes=changes,
    )
