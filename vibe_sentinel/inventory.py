"""Comparing a scan against the recorded structure.

The comparison itself is mechanical and needs no model. An observation
key absent from the baseline *appeared*; one no longer present
*disappeared*; one whose value moved past the probe's tolerance *grew* or
*shrank*; one whose *state* moved at all *changed*. The model's later
analysis rates these, but cannot invent or suppress one — which is what
keeps the drift set reproducible.

The fourth kind is why a version can be tracked here at all. A key is
stable or it is nothing — it is the entire drift mechanism — so the
version cannot go in it, and ``value`` is a float, so the version cannot
go there either. Keying an installed package ``requests==2.32.5`` would
make an upgrade read as one thing vanishing and an unrelated thing
arriving, end that key's series at one point, and break the stability
``Observation.key`` promises. So the identity stays in the key, the
version goes in ``state``, and the transition between two states is its
own kind.

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


def _restated(before: str, after: str) -> bool:
    """Whether an observation's state moved from one thing to another.

    Both ends must be non-empty. A state *arriving* where a baseline has
    none is not a transition of the thing measured — it is the probe
    beginning to measure something it did not measure before, and the
    whole recorded history of that key predates the field. Reporting it
    as a change would announce a definition change as drift in the
    codebase, on every key the probe emits, on the one run after somebody
    edited the probe.

    No tolerance, because a string has no distance: ``2.32.5`` is not
    nearer to ``2.32.4`` than to ``1.0.0`` in any sense a probe could
    declare. A probe that does not want every move reported keeps the
    field out of its state.
    """
    return bool(before) and bool(after) and before != after


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

    Severity here is deliberately coarse — ``appeared``,
    ``disappeared`` and ``changed`` are ``medium`` because a structural
    element arriving, vanishing, or turning into a different thing under
    the same name is a real organizational event, while a value moving
    past tolerance is ``low``. The analysis pass refines these; this
    function only decides what counts as a change at all.
    """
    unmeasured = sorted(pid for pid, r in current.probes.items() if not r.ok)

    if baseline is None:
        return DriftReport(
            current_at=current.generated_at,
            first_run=True,
            probes_run=len(current.probes),
            unmeasured=unmeasured,
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
            was, now = before[key], after[key]
            if _restated(was.state, now.state):
                # A state move outranks a value move on the same key, and
                # only one change may carry a given (probe_id, key): that
                # pair is the identity `ChangeAssessment` rates against and
                # the `changes` table indexes, so a second row under it
                # lands the model's severity on whichever was seen last.
                # The numbers ride along rather than being dropped.
                changes.append(
                    Change(
                        probe_id=probe_id,
                        key=key,
                        kind="changed",
                        before=was.value,
                        after=now.value,
                        before_state=was.state,
                        after_state=now.state,
                        label=now.label or key,
                        severity="medium",
                    )
                )
                continue
            old, new = was.value, now.value
            if old is None or new is None or not _moved(probe, old, new):
                continue
            changes.append(
                Change(
                    probe_id=probe_id,
                    key=key,
                    kind="grew" if new > old else "shrank",
                    before=old,
                    after=new,
                    label=now.label or key,
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
        probes_run=len(current.probes),
        unmeasured=unmeasured,
    )
