"""The second model pass, with the model mocked.

What is worth pinning here is a boundary rather than a behaviour: the
change list going into the prompt is the change list coming out of the
report, with a severity and a note attached. The model may not add an
entry to it and may not remove one, because that is what keeps the drift
set reproducible — and nothing else in the codebase enforces it.

The ``(probe_id, key)`` tests are the ones with a bug behind them. Ratings
used to be attached by key alone, so two probes measuring the same
directory collapsed into one entry and one probe's rating landed on the
other's change, silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vibe_sentinel import analyze as analyze_mod
from vibe_sentinel.analyze import analyze_drift
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    Observation,
    ProbeResult,
    Snapshot,
)


def _report(*changes: Change, **kw: Any) -> DriftReport:
    return DriftReport(
        baseline_at="2026-01-01T00:00:00+00:00",
        changes=list(changes),
        **kw,
    )


def _change(probe_id: str, key: str, severity: str = "low") -> Change:
    return Change(
        probe_id=probe_id,
        key=key,
        kind="grew",
        before=1.0,
        after=9.0,
        label=key,
        severity=severity,  # type: ignore[arg-type]
    )


def _snapshot() -> Snapshot:
    return Snapshot(probes={"p": ProbeResult(probe_id="p", summary="one package")})


def _analyze(
    report: DriftReport,
    answer: dict[str, Any] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> DriftReport:
    """Run the pass with ``answer`` standing in for the model's reply."""

    async def fake_query(*_args: Any, **_kw: Any) -> dict[str, Any] | None:
        return answer

    monkeypatch.setattr(analyze_mod, "llm_query", fake_query)
    return asyncio.run(analyze_drift(report, _snapshot()))


# --- the boundary ----------------------------------------------------------


def test_the_model_cannot_invent_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finding the comparison did not make is not a finding.

    If you want the model to add one, add a probe instead — that is the
    whole reason the change list is computed before the model sees it.
    """
    report = _analyze(
        _report(_change("p", "real")),
        {
            "assessment": "a",
            "changes": [
                {"key": "real", "probe_id": "p", "severity": "high", "note": "n"},
                {"key": "invented", "probe_id": "p", "severity": "high", "note": "n"},
            ],
        },
        monkeypatch,
    )
    assert [c.key for c in report.changes] == ["real"]


def test_the_model_cannot_suppress_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saying nothing about a change must not delete it — it leaves the
    mechanical severity standing, which is a weaker claim, not no claim."""
    report = _analyze(
        _report(_change("p", "rated"), _change("p", "ignored", severity="medium")),
        {
            "assessment": "a",
            "changes": [
                {"key": "rated", "probe_id": "p", "severity": "high", "note": "n"}
            ],
        },
        monkeypatch,
    )
    assert [c.key for c in report.changes] == ["rated", "ignored"]
    assert report.changes[1].severity == "medium"


# --- which change a rating lands on ---------------------------------------


def test_a_rating_lands_on_the_probe_that_reported_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two probes can measure one directory, so the key alone does not
    identify a change. Keying on it attached this rating to whichever of
    them happened to be seen last."""
    report = _analyze(
        _report(
            _change("commentary-ratio", "vibe_sentinel/db"),
            _change("module-organization", "vibe_sentinel/db"),
        ),
        {
            "assessment": "a",
            "changes": [
                {
                    "key": "vibe_sentinel/db",
                    "probe_id": "commentary-ratio",
                    "severity": "high",
                    "note": "commentary exploded",
                }
            ],
        },
        monkeypatch,
    )
    rated = {c.probe_id: (c.severity, c.note) for c in report.changes}
    assert rated["commentary-ratio"] == ("high", "commentary exploded")
    assert rated["module-organization"] == ("low", "")


def test_an_unambiguous_key_needs_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that answers with only a key is still answering usefully
    wherever that key names exactly one change."""
    report = _analyze(
        _report(_change("p", "only-one")),
        {
            "assessment": "a",
            "changes": [{"key": "only-one", "severity": "high", "note": "n"}],
        },
        monkeypatch,
    )
    assert report.changes[0].severity == "high"


def test_an_ambiguous_key_with_no_probe_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither change gets the rating, because there is no way to know
    which one it was written about. The mechanical severity is a worse
    answer than the right one and a much better answer than the wrong
    one."""
    report = _analyze(
        _report(_change("a", "shared"), _change("b", "shared")),
        {
            "assessment": "a",
            "changes": [{"key": "shared", "severity": "high", "note": "n"}],
        },
        monkeypatch,
    )
    assert [c.severity for c in report.changes] == ["low", "low"]
    assert all(c.note == "" for c in report.changes)


# --- honesty about whether a review happened -------------------------------


def test_a_model_that_does_not_answer_leaves_the_severities_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scan that reports structure without commentary is far more useful
    than no scan, so this degrades rather than raises — but it must not
    then claim the changes were reviewed."""
    report = _analyze(_report(_change("p", "k")), None, monkeypatch)
    assert report.changes[0].severity == "low"
    assert report.assessment == ""


def test_an_assessment_is_kept_only_when_the_model_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _analyze(
        _report(_change("p", "k")),
        {"assessment": "read as ordinary growth", "changes": []},
        monkeypatch,
    )
    assert report.assessment == "read as ordinary growth"


def test_a_first_run_is_never_sent_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to rate, and a call that cannot produce a finding
    is a call worth not making."""

    async def explode(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise AssertionError("the model was asked about a first run")

    monkeypatch.setattr(analyze_mod, "llm_query", explode)
    report = asyncio.run(analyze_drift(_report(first_run=True), _snapshot()))
    assert report.assessment == ""


def test_no_changes_means_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise AssertionError("the model was asked to rate an empty list")

    monkeypatch.setattr(analyze_mod, "llm_query", explode)
    assert asyncio.run(analyze_drift(_report(), _snapshot())).assessment == ""


# --- the prompt ------------------------------------------------------------


def test_the_prompt_carries_the_probes_own_short_facts() -> None:
    """`Observation.attrs` says it is carried for a reader. This is one.

    Without it a lens asking about a *share* gets two counts and a
    sentence, and is asked to overrule the counts — which it declines to
    do whatever the question says. Long attrs stay out: the change list is
    a shared prefix paid once per lens, and a sentence is prose for a
    person reading the database.
    """
    change = _change("silent-exceptions", "silent-in:pkg")
    change.attrs = {
        "share": "0.263",
        "handlers": "118",
        "where": "a sentence naming every file and line the probe found",
    }
    report = _report(change)
    prompt = analyze_mod.build_context(report, _snapshot())
    assert "share=0.263" in prompt
    assert "handlers=118" in prompt
    assert "where=" not in prompt


def test_an_attr_never_becomes_a_change() -> None:
    """Carried for the reader, and still not diffed."""
    from vibe_sentinel.inventory import compare

    def snapshot(share: str) -> Snapshot:
        return Snapshot(
            probes={
                "p": ProbeResult(
                    probe_id="p",
                    observations=[
                        Observation(key="k", value=1.0, attrs={"share": share})
                    ],
                )
            }
        )

    report = compare(snapshot("0.1"), snapshot("0.9"), ())
    assert report.changes == []


def test_the_prompt_names_both_the_key_and_the_probe() -> None:
    """The model cannot answer with a pair it was never shown."""
    prompt = analyze_mod.build_context(
        _report(_change("commentary-ratio", "vibe_sentinel/db")), _snapshot()
    )
    assert "key='vibe_sentinel/db'" in prompt
    assert "probe=commentary-ratio" in prompt


# --- an unreachable endpoint is one fact, not one per request --------------
#
# Every fan-out here is `asyncio.gather(..., return_exceptions=True)`, so one
# bad answer does not lose the rest. A dead backend is not one bad answer:
# each request discovers the same fact, so it arrives once per item, and
# folding it into the per-item "no answer" path turns a total outage into an
# ordinary shortfall. `llm.raise_if_unreachable` owns the distinction; these
# are what fails without it at each caller.


def test_a_dead_backend_fails_the_drift_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """It was swallowed as "a lens failed" and the report came back normally.

    `report.py` prints "reviewed by an independent local model" on the drift
    header unconditionally, so a scan whose every lens died on a dead backend
    rendered as a reviewed report and exited 0 or 1.
    """
    from vibe_sentinel.exceptions import LLMConnectionError

    async def dead(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise LLMConnectionError("Model endpoint unreachable after 3 attempts")

    monkeypatch.setattr(analyze_mod, "llm_query", dead)
    with pytest.raises(LLMConnectionError, match="unreachable"):
        asyncio.run(analyze_drift(_report(_change("p", "real")), _snapshot()))
