"""Rendering a scan and its drift."""

from __future__ import annotations

import json

import pytest

from vibe_sentinel.journal import ReviewRecord
from vibe_sentinel.report import (
    render_agent,
    render_json,
    render_reviews,
    render_terminal,
    render_trend_report,
)
from vibe_sentinel.schemas import (
    Anomaly,
    Change,
    DriftReport,
    HorizonDrift,
    Observation,
    ProbeResult,
    Snapshot,
    TrendFit,
)


def _snapshot() -> Snapshot:
    return Snapshot(
        root="src",
        probes={
            "commentary-ratio": ProbeResult(
                probe_id="commentary-ratio",
                filled={"SOURCE_ROOT": "src"},
                observations=[Observation(key="src", value=0.2, label="src: 0.2")],
                summary="1 package",
            )
        },
    )


def _report(**kw) -> DriftReport:
    base = {
        "baseline_at": "2026-01-01T00:00:00+00:00",
        "changes": [
            Change(
                probe_id="module-organization",
                key="dir:src/helpers",
                kind="appeared",
                after=3.0,
                label="src/helpers: 3 module(s)",
                severity="high",
                note="a directory that did not exist before",
            )
        ],
    }
    return DriftReport(**{**base, **kw})


def test_first_run_is_not_drift() -> None:
    assert DriftReport(first_run=True).drifted is False


def test_info_only_changes_are_not_drift() -> None:
    """A tool that calls every small movement drift gets muted."""
    report = DriftReport(
        changes=[Change(probe_id="p", key="k", kind="grew", severity="info")]
    )
    assert report.drifted is False


def test_any_non_info_change_is_drift() -> None:
    assert _report().drifted is True


def test_change_describes_itself_by_kind() -> None:
    appeared = Change(probe_id="p", key="k", kind="appeared", label="a dir")
    assert appeared.describe() == "new: a dir"
    gone = Change(probe_id="p", key="k", kind="disappeared", label="a dir")
    assert gone.describe() == "gone: a dir"
    grew = Change(
        probe_id="p", key="k", kind="grew", before=1.0, after=9.0, label="a dir"
    )
    assert grew.describe() == "a dir: 1 -> 9"


def test_agent_render_states_the_drift_and_the_ask() -> None:
    text = render_agent(_report(analyzed=True))
    assert text.startswith("VIBE SENTINEL: STRUCTURAL DRIFT DETECTED")
    assert "src/helpers" in text
    assert "a directory that did not exist before" in text
    assert "scan --update" in text


def test_agent_render_does_not_claim_a_review_that_did_not_happen() -> None:
    """With --no-model the severities are mechanical; saying a model
    reviewed them would be a false claim about provenance."""
    reviewed = render_agent(_report(analyzed=True))
    unreviewed = render_agent(_report(analyzed=False))
    assert "reviewed by an independent local model" in reviewed
    assert "reviewed by an independent local model" not in unreviewed
    assert "mechanical comparison only" in unreviewed


def test_agent_render_omits_info_changes() -> None:
    report = _report(
        changes=[
            Change(probe_id="p", key="k", kind="grew", severity="info", label="tiny")
        ]
    )
    assert "NO STRUCTURAL DRIFT" in render_agent(report)


def test_agent_render_orders_by_severity() -> None:
    report = _report(
        changes=[
            Change(
                probe_id="p",
                key="low-one",
                kind="grew",
                severity="low",
                label="minor-movement",
            ),
            Change(
                probe_id="p",
                key="high-one",
                kind="appeared",
                severity="high",
                label="new-directory",
            ),
        ]
    )
    text = render_agent(report)
    assert text.index("new-directory") < text.index("minor-movement")


def test_agent_render_on_first_run_says_baseline() -> None:
    assert "BASELINE RECORDED" in render_agent(DriftReport(first_run=True))


def test_json_render_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    render_json(_snapshot(), _report())
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"]["probes"]["commentary-ratio"]["summary"] == "1 package"
    assert payload["drift"]["changes"][0]["key"] == "dir:src/helpers"


def test_terminal_render_shows_structure_and_parameters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), _report())
    out = capsys.readouterr().out
    assert "commentary-ratio: 1 package" in out
    assert "SOURCE_ROOT=src" in out
    assert "src/helpers" in out


def test_terminal_render_reports_a_failed_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silently absent probe would read as 'nothing changed'."""
    snapshot = Snapshot(probes={"p": ProbeResult(probe_id="p", ok=False, error="boom")})
    render_terminal(snapshot, DriftReport(first_run=True))
    assert "FAILED — boom" in capsys.readouterr().out


def test_terminal_render_says_when_nothing_moved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), DriftReport(baseline_at="2026-01-01T00:00:00+00:00"))
    assert "No change since" in capsys.readouterr().out


def test_terminal_render_does_not_claim_a_review_that_did_not_happen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same rule render_agent keeps, in the renderer a person actually
    reads. It printed `[high]` under --no-model with nothing saying the
    severity came from the comparison rather than from a model."""
    render_terminal(_snapshot(), _report(analyzed=False))
    unreviewed = capsys.readouterr().out
    render_terminal(_snapshot(), _report(analyzed=True))
    reviewed = capsys.readouterr().out

    assert "mechanical comparison only" in unreviewed
    assert "reviewed by an independent local model" not in unreviewed
    assert "reviewed by an independent local model" in reviewed


def test_terminal_render_says_it_before_the_severities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It is what they mean, so it is read first."""
    render_terminal(_snapshot(), _report(analyzed=False))
    out = capsys.readouterr().out
    assert out.index("mechanical comparison only") < out.index("[high]")


def _review(**kw) -> ReviewRecord:
    base = {
        "id": 1,
        "command_id": 1,
        "reviewed_at": "2026-01-01T00:00:00",
        "signals": "deleting-files",
        "verdict": "unsafe",
        "reason": "it removes the home directory",
        "mode": "enforce",
        "command": "rm -rf ~/",
        "tool_name": "Bash",
        "session_id": "sess_a",
    }
    return ReviewRecord(**{**base, **kw})


def test_a_tuning_check_is_not_rendered_as_agent_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`safety --check` shares this table with the gate's own verdicts. It
    is marked in the database and the marking has to reach the listing —
    storing the distinction is not the same as showing it, and for a while
    only the storing was true."""
    render_reviews([_review(mode="check", agent_type="check")])
    out = capsys.readouterr().out
    assert "[check]" in out
    assert "typed at `safety --check`, not run by an agent" in out


def test_a_real_verdict_carries_no_check_tag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_reviews([_review(enforced=True)])
    out = capsys.readouterr().out
    assert "[check]" not in out
    assert "[BLOCKED]" in out


def test_an_unreviewed_row_is_listed_not_hidden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gate that quietly stopped working looks exactly like a gate with
    nothing to report."""
    render_reviews([_review(verdict="unreviewed", reason="", mode="observe")])
    out = capsys.readouterr().out
    assert "unreviewed" in out
    assert "backend status" in out


# --- horizons ---------------------------------------------------------------


def _horizon(**kw) -> HorizonDrift:
    base = {
        "horizon": "1m",
        "run_id": 12,
        "at": "2026-08-03T00:00:00+00:00",
        "age_days": 31.0,
        "changes": [
            Change(
                probe_id="module-organization",
                key="dir:src/db",
                kind="grew",
                before=4.0,
                after=9.0,
                label="src/db",
                severity="low",
            )
        ],
    }
    return HorizonDrift(**{**base, **kw})


def test_a_horizon_is_reported_even_when_the_baseline_is_calm(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The run this exists for. The baseline may be two days old, and
    what has been growing for a month is invisible to it by
    construction."""
    render_terminal(
        _snapshot(), DriftReport(baseline_at="2026-09-01", horizons=[_horizon()])
    )
    out = capfd.readouterr().out
    assert "No change since" in out
    assert "Also moved, over longer horizons" in out
    assert "run 12, 31d ago" in out
    assert "src/db: 4 -> 9" in out


def test_a_horizon_section_says_nobody_rated_it_and_nothing_fails(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A section that looks like the drift section is read as the drift
    section."""
    render_terminal(_snapshot(), _report(horizons=[_horizon()]))
    out = capfd.readouterr().out
    assert "no model rated" in out
    assert "changes the exit code" in out


def test_a_horizon_with_nothing_to_compare_says_so(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_terminal(
        _snapshot(),
        _report(
            horizons=[
                HorizonDrift(horizon="1y", unavailable="no run recorded 1y or more ago")
            ]
        ),
    )
    out = capfd.readouterr().out
    assert "not measured" in out
    assert "no run recorded 1y or more ago" in out


def test_a_quiet_horizon_is_distinguishable_from_one_that_did_not_run(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), _report(horizons=[_horizon(changes=[])]))
    out = capfd.readouterr().out
    assert "no movement" in out
    assert "not measured" not in out


def test_a_long_horizon_is_summarised_rather_than_dumped(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A month of a busy repository is a wall, and a wall is read as a
    wall. The whole list stays in --format json."""
    many = [
        Change(
            probe_id="p", key=f"k{i}", kind="appeared", label=f"k{i}", severity="low"
        )
        for i in range(9)
    ]
    render_terminal(_snapshot(), _report(horizons=[_horizon(changes=many)]))
    assert "and 4 more" in capfd.readouterr().out


def test_horizon_changes_do_not_count_as_drift() -> None:
    report = DriftReport(changes=[], horizons=[_horizon()])
    assert report.drifted is False


def test_the_agent_hears_about_a_horizon_as_context_not_as_a_failure() -> None:
    text = render_agent(_report(changes=[], horizons=[_horizon()]))
    assert "VIBE SENTINEL: MOVEMENT OVER A LONGER WINDOW" in text
    assert "not a failure" in text
    assert "src/db: 4 -> 9" in text
    # The verdict above it is still the verdict.
    assert text.startswith("VIBE SENTINEL: NO STRUCTURAL DRIFT")


def test_a_quiet_horizon_says_nothing_to_the_agent() -> None:
    """It repeats what the drift verdict above it already said, and the
    block's job is to state constraints."""
    text = render_agent(_report(changes=[], horizons=[_horizon(changes=[])]))
    assert "LONGER WINDOW" not in text


def test_horizons_reach_the_json_consumer_whole(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_json(_snapshot(), _report(horizons=[_horizon()]))
    payload = json.loads(capfd.readouterr().out)
    assert payload["drift"]["horizons"][0]["horizon"] == "1m"
    assert payload["drift"]["horizons"][0]["run_id"] == 12


# --- trends ----------------------------------------------------------------


def _fit(**kw) -> TrendFit:
    base = {
        "probe_id": "module-organization",
        "key": "dir:src/db",
        "runs": 30,
        "first_run": 1,
        "last_run": 30,
        "first_value": 4.0,
        "last_value": 19.0,
        "slope": 0.5,
        "intercept": 3.5,
        "fitted_change": 14.5,
        "tau": 0.94,
        "p_value": 0.0000031,
        "significant": True,
        "direction": "rising",
        "scale": 0.8,
    }
    return TrendFit(**{**base, **kw})


def test_a_trend_is_reported_even_when_the_baseline_is_calm(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), DriftReport(baseline_at="2026-09-01", trends=[_fit()]))
    out = capfd.readouterr().out
    assert "No change since" in out
    assert "Sustained trends" in out
    assert "rising" in out
    assert "dir:src/db" in out


def test_the_trend_section_says_nobody_rated_it_and_nothing_fails(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), _report(trends=[_fit()]))
    out = capfd.readouterr().out
    assert "no model rated" in out
    assert "changes the exit code" in out


def test_a_vanishing_p_value_is_not_printed_as_zero(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """`p=0.000` is not a probability and reads as one."""
    render_terminal(_snapshot(), _report(trends=[_fit()]))
    out = capfd.readouterr().out
    assert "p<0.001" in out
    assert "p=0.000" not in out


def test_an_ordinary_p_value_keeps_its_digits(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_terminal(_snapshot(), _report(trends=[_fit(p_value=0.021)]))
    assert "p=0.021" in capfd.readouterr().out


def test_a_departure_from_a_series_that_never_moved_is_stated_in_words(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """There is no z to print, and `z=+0.0` is the one rendering that
    makes the moment a steady series moved look like nothing."""
    fit = _fit(
        significant=False,
        direction="flat",
        scale=0.0,
        anomalies=[Anomaly(run_id=None, value=40.0, expected=14.0, z=None)],
    )
    render_terminal(_snapshot(), _report(trends=[fit]))
    out = capfd.readouterr().out
    assert "on every one of the last 30 runs" in out
    assert "z=" not in out


def test_a_scored_departure_prints_its_score(
    capfd: pytest.CaptureFixture[str],
) -> None:
    fit = _fit(anomalies=[Anomaly(run_id=None, value=40.0, expected=19.5, z=25.6)])
    render_terminal(_snapshot(), _report(trends=[fit]))
    out = capfd.readouterr().out
    assert "! anomaly" in out
    assert "z=+25.6" in out


def test_a_wall_of_steady_slopes_is_summarised(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A tree with three hundred observations has more steady slopes than
    anybody reads, and `trend` is where the whole list lives."""
    many = [_fit(key=f"dir:{i}") for i in range(9)]
    render_terminal(_snapshot(), _report(trends=many))
    out = capfd.readouterr().out
    assert "and 4 more" in out
    assert "vibe-sentinel trend" in out


def test_trends_do_not_count_as_drift() -> None:
    assert DriftReport(changes=[], trends=[_fit()]).drifted is False


def test_the_agent_hears_a_direction_as_context_not_as_a_failure() -> None:
    text = render_agent(_report(changes=[], trends=[_fit()]))
    assert "VIBE SENTINEL: WHERE THE HISTORY IS HEADING" in text
    assert "not a failure" in text
    assert "RISING" in text
    assert text.startswith("VIBE SENTINEL: NO STRUCTURAL DRIFT")


def test_a_flat_fit_says_nothing_to_the_agent() -> None:
    flat = _fit(significant=False, direction="flat")
    assert "HISTORY IS HEADING" not in render_agent(_report(changes=[], trends=[flat]))


def test_trends_reach_the_json_consumer_whole(
    capfd: pytest.CaptureFixture[str],
) -> None:
    render_json(_snapshot(), _report(trends=[_fit()]))
    payload = json.loads(capfd.readouterr().out)
    fit = payload["drift"]["trends"][0]
    assert fit["direction"] == "rising"
    assert fit["tau"] == 0.94
    assert fit["p_value"] == 0.0000031


def test_the_trend_command_separates_directions_from_departures(
    capfd: pytest.CaptureFixture[str],
) -> None:
    fits = [
        _fit(),
        _fit(
            key="dir:src/api",
            significant=False,
            direction="flat",
            anomalies=[
                Anomaly(run_id=12, at="2026-08-12", value=90.0, expected=14.0, z=41.0)
            ],
        ),
    ]
    render_trend_report(fits, runs=50, min_runs=10)
    out = capfd.readouterr().out
    assert "Trends (1 of 2 significant" in out
    assert "Anomalies (1 series" in out
    # Unlike a scan, this one names the run inside the window.
    assert "run 12" in out


def test_the_trend_report_names_the_floor_it_fitted_above(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Which series were left out is part of reading the ones that were
    not: an observation measured four times is absent, not flat."""
    render_trend_report([_fit()], runs=50, min_runs=10)
    assert "at least 10 run(s)" in capfd.readouterr().out


# ---------------------------------------------------------------------------
# A scan that measured nothing is not a scan that found nothing
# ---------------------------------------------------------------------------


def test_agent_render_never_calls_an_unmeasured_run_clean() -> None:
    """The failure this exists for: installed beside a project, every
    shipped probe ran under an interpreter that could not import the
    package, all five were recorded as failed, and the agent renderer —
    which cannot see a probe result — printed NO STRUCTURAL DRIFT. A
    failed probe is recorded rather than raised precisely so the scan
    completes, which is what makes the renderer the place this has to be
    caught."""
    report = _report(changes=[], probes_run=5, unmeasured=["a", "b", "c", "d", "e"])
    text = render_agent(report)
    assert "NOTHING WAS MEASURED" in text
    assert "NO STRUCTURAL DRIFT" not in text


def test_agent_render_says_a_baseline_of_nothing_is_not_a_baseline() -> None:
    """Worse than the no-drift case, because the empty baseline is what
    every later scan compares against."""
    report = DriftReport(first_run=True, probes_run=2, unmeasured=["a", "b"])
    text = render_agent(report)
    assert "NOTHING WAS MEASURED" in text
    assert "BASELINE RECORDED" not in text


def test_agent_render_qualifies_the_heading_when_only_some_probes_ran() -> None:
    """The qualification goes in the heading, not under it: the renderer's
    own comment says the heading is the sentence an agent stops reading
    after."""
    report = _report(changes=[], probes_run=5, unmeasured=["silent-exceptions"])
    text = render_agent(report)
    assert "NO DRIFT IN WHAT WAS MEASURED" in text
    assert "NO STRUCTURAL DRIFT" not in text
    assert "silent-exceptions" in text


def test_agent_render_names_the_missing_probes_even_when_drift_was_found() -> None:
    """Drift found by three probes says nothing about the two that did not
    report, and the block is appended to every verdict for that reason."""
    text = render_agent(_report(probes_run=5, unmeasured=["file-length"]))
    assert "STRUCTURAL DRIFT DETECTED" in text
    assert "NOT MEASURED THIS RUN: 1 of 5 probe(s) — file-length" in text


def test_agent_render_stays_quiet_when_every_probe_measured() -> None:
    assert "NOT MEASURED" not in render_agent(_report(probes_run=5))
