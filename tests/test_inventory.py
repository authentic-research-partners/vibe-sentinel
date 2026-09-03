"""The mechanical drift comparison.

Persistence moved to the history database — see ``test_db_store.py``.
What is left here is the diff itself, which is deliberately model-free
and therefore fully testable without one.
"""

from __future__ import annotations

from vibe_sentinel.inventory import compare
from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Probe


def _probe(probe_id: str = "p", tolerance: float | str = 0.0) -> Probe:
    return Probe(id=probe_id, title="t", command=["echo"], tolerance=tolerance)


def _snapshot(observations: list[tuple[str, float]], probe_id: str = "p") -> Snapshot:
    return Snapshot(
        probes={
            probe_id: ProbeResult(
                probe_id=probe_id,
                observations=[
                    Observation(key=k, value=v, label=k) for k, v in observations
                ],
            )
        }
    )


def test_first_run_reports_baseline_not_drift() -> None:
    report = compare(None, _snapshot([("a", 1.0)]), [_probe()])
    assert report.first_run is True
    assert report.changes == []
    assert report.drifted is False


def test_unchanged_structure_produces_no_changes() -> None:
    snap = _snapshot([("a", 1.0), ("b", 2.0)])
    assert compare(snap, snap, [_probe()]).changes == []


def test_new_key_appears() -> None:
    """A key never seen before is the finding, with no threshold involved
    — this is the 'code started landing somewhere new' case."""
    report = compare(
        _snapshot([("a", 1.0)]), _snapshot([("a", 1.0), ("b", 3.0)]), [_probe()]
    )
    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.kind == "appeared"
    assert change.key == "b"
    assert change.after == 3.0
    assert change.severity == "medium"
    assert report.drifted is True


def test_removed_key_disappears() -> None:
    report = compare(
        _snapshot([("a", 1.0), ("b", 2.0)]), _snapshot([("a", 1.0)]), [_probe()]
    )
    assert [c.kind for c in report.changes] == ["disappeared"]
    assert report.changes[0].before == 2.0


def test_growth_past_tolerance_is_reported() -> None:
    report = compare(
        _snapshot([("a", 1.0)]), _snapshot([("a", 5.0)]), [_probe(tolerance=0.5)]
    )
    assert [c.kind for c in report.changes] == ["grew"]
    assert report.changes[0].delta == 4.0


def test_shrink_past_tolerance_is_reported() -> None:
    report = compare(
        _snapshot([("a", 9.0)]), _snapshot([("a", 1.0)]), [_probe(tolerance=0.5)]
    )
    assert [c.kind for c in report.changes] == ["shrank"]


def test_movement_within_tolerance_is_ignored() -> None:
    """Tolerance is what stops ordinary editing registering as drift."""
    report = compare(
        _snapshot([("a", 0.20)]), _snapshot([("a", 0.23)]), [_probe(tolerance=0.05)]
    )
    assert report.changes == []


def test_a_percentage_tolerance_is_measured_against_the_baseline() -> None:
    """The reason the form exists: one threshold cannot serve a probe
    whose values span orders of magnitude. The same 200-unit move is a
    doubling of one key and 12% of another, and only one of those is an
    event."""
    small = compare(
        _snapshot([("a", 200.0)]), _snapshot([("a", 400.0)]), [_probe(tolerance="15%")]
    )
    large = compare(
        _snapshot([("a", 1700.0)]),
        _snapshot([("a", 1900.0)]),
        [_probe(tolerance="15%")],
    )
    assert [c.kind for c in small.changes] == ["grew"]
    assert large.changes == []


def test_a_percentage_tolerance_admits_nothing_around_zero() -> None:
    """A key that was 0 and is now 3 moved by every proportion there
    is, and a threshold of 15% of nothing is nothing."""
    report = compare(
        _snapshot([("a", 0.0)]), _snapshot([("a", 3.0)]), [_probe(tolerance="99%")]
    )
    assert [c.kind for c in report.changes] == ["grew"]


def test_a_percentage_tolerance_reads_a_shrink_the_same_way() -> None:
    """Measured against the value it is compared to, whichever way it
    went: half of a file is half of a file."""
    report = compare(
        _snapshot([("a", 400.0)]), _snapshot([("a", 200.0)]), [_probe(tolerance="15%")]
    )
    assert [c.kind for c in report.changes] == ["shrank"]


def test_failed_probe_is_not_compared() -> None:
    """A probe that failed produced no observations; treating that as
    'everything disappeared' would bury the real signal."""
    current = Snapshot(probes={"p": ProbeResult(probe_id="p", ok=False, error="boom")})
    assert compare(_snapshot([("a", 1.0)]), current, [_probe()]).changes == []


def test_a_failed_probe_is_carried_into_the_report_not_only_dropped() -> None:
    """Not comparing it is right and is not enough. Nothing downstream
    could see it, so a scan whose probes all failed rendered as a scan
    that found nothing changed — which is the one sentence the report
    must not produce about a measurement never taken."""
    current = Snapshot(probes={"p": ProbeResult(probe_id="p", ok=False, error="boom")})
    report = compare(_snapshot([("a", 1.0)]), current, [_probe()])
    assert report.unmeasured == ["p"]
    assert report.probes_run == 1


def test_the_first_run_records_what_it_could_not_measure_too() -> None:
    """The baseline every later scan compares against is the worst place
    for a gap nobody can see afterwards."""
    current = Snapshot(probes={"p": ProbeResult(probe_id="p", ok=False, error="boom")})
    report = compare(None, current, [_probe()])
    assert report.first_run is True
    assert report.unmeasured == ["p"]


def test_probe_missing_from_this_run_is_noted_as_info() -> None:
    current = Snapshot(probes={})
    report = compare(_snapshot([("a", 1.0)]), current, [_probe()])
    assert [c.severity for c in report.changes] == ["info"]
    assert report.drifted is False


def test_info_changes_alone_do_not_count_as_drift() -> None:
    report = compare(_snapshot([("a", 1.0)]), Snapshot(probes={}), [_probe()])
    assert report.changes
    assert report.drifted is False


# ---------------------------------------------------------------------------
# `changed` — a state moving, which is what makes a version trackable
# ---------------------------------------------------------------------------


def _stated(states: list[tuple[str, str]], probe_id: str = "p") -> Snapshot:
    """A snapshot whose observations carry a state and no value."""
    return Snapshot(
        probes={
            probe_id: ProbeResult(
                probe_id=probe_id,
                observations=[
                    Observation(key=k, state=s, label=f"{k} {s}") for k, s in states
                ],
            )
        }
    )


def test_a_state_that_moved_is_reported_as_changed() -> None:
    report = compare(
        _stated([("version:requests", "2.32.5")]),
        _stated([("version:requests", "2.28.0")]),
        [_probe()],
    )
    (change,) = report.changes
    assert change.kind == "changed"
    assert change.before_state == "2.32.5"
    assert change.after_state == "2.28.0"
    assert change.severity == "medium"


def test_an_unchanged_state_is_not_a_change() -> None:
    snap = _stated([("version:requests", "2.32.5")])
    assert compare(snap, snap, [_probe()]).changes == []


def test_a_state_has_no_tolerance() -> None:
    """A version is an identity, so a probe's numeric tolerance cannot
    admit a move: 2.32.5 is not nearer 2.32.4 than 1.0.0."""
    report = compare(
        _stated([("version:x", "2.32.5")]),
        _stated([("version:x", "2.32.4")]),
        [_probe(tolerance="99%")],
    )
    assert [c.kind for c in report.changes] == ["changed"]


def test_a_state_arriving_where_there_was_none_is_not_a_change() -> None:
    """The run after a probe starts recording a state must not announce
    every key it emits as having changed from nothing into its first
    value — that is a definition change, not drift in the codebase."""
    before = _snapshot([("a", 1.0)])
    after = Snapshot(
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key="a", value=1.0, state="2.0.0")],
            )
        }
    )
    assert compare(before, after, [_probe()]).changes == []


def test_a_state_disappearing_is_not_a_change_either() -> None:
    """The other direction, for the same reason: a probe that stopped
    recording a state has not changed the thing it measured."""
    before = Snapshot(
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key="a", value=1.0, state="2.0.0")],
            )
        }
    )
    assert compare(before, _snapshot([("a", 1.0)]), [_probe()]).changes == []


def test_a_state_move_outranks_a_value_move_on_the_same_key() -> None:
    """One change per (probe_id, key). That pair is the identity the
    analysis pass rates against and the changes table indexes, so a
    second row under it would land the model's severity on whichever was
    seen last."""
    before = Snapshot(
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key="a", value=1.0, state="1.0.0")],
            )
        }
    )
    after = Snapshot(
        probes={
            "p": ProbeResult(
                probe_id="p",
                observations=[Observation(key="a", value=99.0, state="2.0.0")],
            )
        }
    )
    (change,) = compare(before, after, [_probe()]).changes
    assert change.kind == "changed"
    # The numbers ride along rather than being dropped.
    assert (change.before, change.after) == (1.0, 99.0)


def test_a_state_that_appeared_is_still_an_appeared() -> None:
    """A new key is a new key whatever it carries — `changed` is only
    about a key present on both sides."""
    report = compare(
        _stated([("version:a", "1.0")]),
        _stated([("version:a", "1.0"), ("version:b", "2.0")]),
        [_probe()],
    )
    (change,) = report.changes
    assert change.kind == "appeared"
    assert change.key == "version:b"


def test_changed_describes_itself_with_the_key_not_the_label() -> None:
    """The label names the state as it is now, so pairing it with the
    transition prints the new value twice."""
    report = compare(
        _stated([("version:requests", "2.32.5")]),
        _stated([("version:requests", "2.28.0")]),
        [_probe()],
    )
    assert report.changes[0].describe() == "version:requests: 2.32.5 -> 2.28.0"
