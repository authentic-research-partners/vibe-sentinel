"""Declared lenses: what the model is asked about a drift report.

The rules pinned here are the ones a shipped default cannot hold on its
own. A lens is a question in the project's words, so the loader has to
layer the same way probes, dangers and secrets do; the fan-out has to
send one question per request over one shared context, because that is
the only reason three lenses cost roughly what one does; and every lens
has to be accounted for whether or not it was asked, because a question
nobody asked must never read as a question that found nothing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import analyze as analyze_mod
from vibe_sentinel.analyze import (
    BUILTIN_LENSES,
    Lens,
    analyze_drift,
    build_context,
    load_lenses,
    unknown_watches,
    watched,
)
from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.schemas import (
    Anomaly,
    Change,
    DriftReport,
    HorizonDrift,
    ProbeResult,
    Snapshot,
    TrendFit,
)


def _config(tmp_path: Path, body: str) -> Path:
    (tmp_path / CONFIG_FILENAME).write_text(body)
    return tmp_path


def _lens(lens_id: str, *watch: str) -> Lens:
    return Lens(id=lens_id, title=lens_id, question=f"{lens_id}?", watch=tuple(watch))


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


def _report(*changes: Change, **kw: Any) -> DriftReport:
    return DriftReport(
        baseline_at="2026-01-01T00:00:00+00:00", changes=list(changes), **kw
    )


def _fit(probe_id: str = "p", key: str = "k", **kw: Any) -> TrendFit:
    return TrendFit(
        probe_id=probe_id,
        key=key,
        label=key,
        runs=24,
        first_value=3.0,
        last_value=11.0,
        slope=0.33,
        tau=0.71,
        p_value=0.002,
        significant=True,
        direction="rising",
        **kw,
    )


# --- the declared set ------------------------------------------------------


def test_no_config_is_the_shipped_set(tmp_path: Path) -> None:
    assert load_lenses(tmp_path) == BUILTIN_LENSES


def test_a_new_id_adds_and_a_reused_id_overrides(tmp_path: Path) -> None:
    """Declaring the one question you care about must not drop the rest."""
    root = _config(
        tmp_path,
        """
[[lens]]
id = "handler-bloat"
title = "Handlers as a dumping ground"
question = "Is api/handlers/ where anything unclaimed goes?"

[[lens]]
id = "organization"
question = "Our own take on structure."
""",
    )
    loaded = {lens.id: lens for lens in load_lenses(root)}
    assert set(loaded) == {"organization", "handler-bloat"}
    assert loaded["organization"].question == "Our own take on structure."


def test_use_keeps_only_those_named(tmp_path: Path) -> None:
    root = _config(
        tmp_path,
        """
[drift]
use = ["mine"]

[[lens]]
id = "mine"
question = "?"
""",
    )
    assert [lens.id for lens in load_lenses(root)] == ["mine"]


def test_disable_removes_one_without_restating_the_rest(tmp_path: Path) -> None:
    root = _config(
        tmp_path,
        """
[drift]
disable = ["organization"]

[[lens]]
id = "mine"
question = "?"
""",
    )
    assert [lens.id for lens in load_lenses(root)] == ["mine"]


def test_use_builtins_false_starts_from_nothing(tmp_path: Path) -> None:
    root = _config(
        tmp_path,
        """
[drift]
use_builtins = false

[[lens]]
id = "mine"
question = "?"
""",
    )
    assert [lens.id for lens in load_lenses(root)] == ["mine"]


def test_disabling_a_lens_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """A stale entry silently asks nothing, which is the failure it hides."""
    root = _config(tmp_path, '[drift]\ndisable = ["typo"]\n')
    with pytest.raises(ValueError, match="disable names lens"):
        load_lenses(root)


def test_a_lens_without_a_question_is_an_error(tmp_path: Path) -> None:
    root = _config(tmp_path, '[[lens]]\nid = "mine"\ntitle = "t"\n')
    with pytest.raises(ValueError, match="has no question"):
        load_lenses(root)


def test_a_lens_without_an_id_is_an_error(tmp_path: Path) -> None:
    root = _config(tmp_path, '[[lens]]\nquestion = "?"\n')
    with pytest.raises(ValueError, match="has no id"):
        load_lenses(root)


def test_watch_must_be_a_list_of_probe_ids(tmp_path: Path) -> None:
    root = _config(tmp_path, '[[lens]]\nid = "m"\nquestion = "?"\nwatch = "p"\n')
    with pytest.raises(ValueError, match="watch="):
        load_lenses(root)


def test_a_watch_naming_no_probe_is_reported(tmp_path: Path) -> None:
    lenses = (_lens("mine", "file-length", "gone"),)
    assert unknown_watches(lenses, {"file-length"}) == ["mine watches 'gone'"]


# --- the cheap stage -------------------------------------------------------


def test_a_lens_watching_nothing_in_particular_is_always_asked() -> None:
    assert watched(_lens("all"), _report(_change("p", "k"))) is True


def test_a_lens_is_not_asked_when_nothing_it_watches_moved() -> None:
    assert watched(_lens("mine", "other"), _report(_change("p", "k"))) is False


def test_a_fitted_trend_alone_is_enough_to_ask() -> None:
    """The run where nothing moved is the run a direction is the whole story."""
    report = _report(trends=[_fit(probe_id="other")])
    assert watched(_lens("mine", "other"), report) is True


def test_movement_over_a_horizon_alone_is_enough_to_ask() -> None:
    horizon = HorizonDrift(
        horizon="1m", run_id=4, age_days=31.0, changes=[_change("other", "k")]
    )
    assert watched(_lens("mine", "other"), _report(horizons=[horizon])) is True


# --- the context -----------------------------------------------------------


def test_the_timelines_reach_the_prompt() -> None:
    """A slope nobody interprets is a number in a report."""
    report = _report(
        _change("p", "k"),
        trends=[
            _fit(anomalies=[Anomaly(at="2026-02-01", value=19.0, expected=11.4, z=6.2)])
        ],
        horizons=[
            HorizonDrift(
                horizon="1m",
                run_id=4,
                age_days=31.0,
                changes=[_change("p", "older", severity="medium")],
            )
        ],
    )
    context = build_context(report, _snapshot())
    assert "<TIMELINES>" in context
    assert "rising over 24 runs" in context
    assert "off its own trend this run" in context
    assert "<OVER_LONGER_HORIZONS>" in context
    assert "key='older'" in context


def test_a_horizon_that_reached_nothing_says_so() -> None:
    """Not measured is not the same fact as found nothing."""
    report = _report(
        _change("p", "k"),
        horizons=[
            HorizonDrift(horizon="1m", unavailable="no run recorded 1m or more ago")
        ],
    )
    assert "not measured — no run recorded" in build_context(report, _snapshot())


def test_house_guidance_rides_above_every_question() -> None:
    context = build_context(
        _report(_change("p", "k")), _snapshot(), "We vendor on purpose."
    )
    assert "<HOUSE_NOTES>\nWe vendor on purpose.\n</HOUSE_NOTES>" in context


# --- the fan-out -----------------------------------------------------------


def _asked_about(user: str) -> str:
    """The lens id out of the question actually sent."""
    match = re.search(r"\(([\w-]+)\)", user)
    assert match is not None, user
    return match.group(1)


def _record(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[tuple[str, str]]:
    """Capture every (system, user) pair the fan-out sends."""
    sent: list[tuple[str, str]] = []

    async def fake_query(system: str, user: str, *_a: Any, **_kw: Any) -> Any:
        sent.append((system, user))
        return answer(user) if callable(answer) else answer

    monkeypatch.setattr(analyze_mod, "llm_query", fake_query)
    return sent


def test_each_lens_is_its_own_request_over_one_shared_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three questions, one prefix — which is why three cost what one does."""
    sent = _record(monkeypatch, {"assessment": "a", "changes": []})
    lenses = (_lens("one"), _lens("two"), _lens("three"))
    asyncio.run(analyze_drift(_report(_change("p", "k")), _snapshot(), lenses=lenses))

    assert len(sent) == 3
    assert len({system for system, _ in sent}) == 1, "the context must not diverge"
    assert {"one", "two", "three"} == {_asked_about(u) for _, u in sent}


def test_a_second_lens_may_raise_a_severity_and_never_lower_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lens that was not looking for something must not talk down one that was."""

    def answer(user: str) -> dict[str, Any]:
        severity = "high" if _asked_about(user) == "sharp" else "info"
        return {
            "assessment": "a",
            "changes": [
                {"key": "k", "probe_id": "p", "severity": severity, "note": severity}
            ],
        }

    _record(monkeypatch, answer)
    for order in ((_lens("sharp"), _lens("blunt")), (_lens("blunt"), _lens("sharp"))):
        report = asyncio.run(
            analyze_drift(_report(_change("p", "k")), _snapshot(), lenses=order)
        )
        assert report.changes[0].severity == "high"
        assert report.changes[0].note == "high"


def test_every_lens_is_accounted_for_asked_or_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record(monkeypatch, {"assessment": "seen", "changes": []})
    lenses = (_lens("asked", "p"), _lens("quiet", "elsewhere"))
    report = asyncio.run(
        analyze_drift(_report(_change("p", "k")), _snapshot(), lenses=lenses)
    )
    results = {r.id: r for r in report.lenses}
    assert results["asked"].asked and results["asked"].answered
    assert not results["quiet"].asked
    assert "nothing it watches moved" in results["quiet"].note


def test_a_lens_that_was_asked_and_did_not_answer_is_not_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule the safety gate keeps: a review that did not happen is not clean."""
    _record(monkeypatch, None)
    report = asyncio.run(
        analyze_drift(_report(_change("p", "k")), _snapshot(), lenses=(_lens("one"),))
    )
    assert report.analyzed is False
    assert report.lenses[0].asked is True
    assert report.lenses[0].answered is False
    assert report.changes[0].severity == "low", "the mechanical severity survives"


def test_one_lens_failing_does_not_lose_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_query(_system: str, user: str, *_a: Any, **_kw: Any) -> Any:
        if _asked_about(user) == "bad":
            raise RuntimeError("backend hiccup")
        return {"assessment": "kept", "changes": []}

    monkeypatch.setattr(analyze_mod, "llm_query", fake_query)
    report = asyncio.run(
        analyze_drift(
            _report(_change("p", "k")),
            _snapshot(),
            lenses=(_lens("bad"), _lens("good")),
        )
    )
    assert report.analyzed is True
    assert report.assessment == "kept"


def test_several_assessments_are_attributed(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(user: str) -> dict[str, Any]:
        return {"assessment": f"read of {_asked_about(user)}", "changes": []}

    _record(monkeypatch, answer)
    report = asyncio.run(
        analyze_drift(
            _report(_change("p", "k")), _snapshot(), lenses=(_lens("one"), _lens("two"))
        )
    )
    assert "one: read of one" in report.assessment
    assert "two: read of two" in report.assessment


def test_a_trend_with_no_change_is_still_worth_asking_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of showing the model a timeline it can read."""
    sent = _record(monkeypatch, {"assessment": "climbing all month", "changes": []})
    report = asyncio.run(
        analyze_drift(_report(trends=[_fit()]), _snapshot(), lenses=(_lens("one"),))
    )
    assert len(sent) == 1
    assert report.assessment == "climbing all month"


def test_a_silent_report_asks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a report with nothing in it was sent to the model")

    monkeypatch.setattr(analyze_mod, "llm_query", explode)
    assert asyncio.run(analyze_drift(_report(), _snapshot())).analyzed is False


# --- the answer has a size, and it is known in advance ----------------------


def test_the_budget_is_sized_from_the_bounds_not_guessed() -> None:
    """The failure this replaces: 2225 tokens wanted against 2048 configured."""
    from vibe_sentinel.analyze import answer_budget
    from vibe_sentinel.schemas import MAX_RATED

    assert answer_budget(21, floor=2048) > 2225
    assert answer_budget(1, floor=2048) == 2048, "the configured value is a floor"
    assert answer_budget(1, floor=0) < answer_budget(21, floor=0)
    assert answer_budget(MAX_RATED, floor=0) == answer_budget(MAX_RATED * 10, floor=0)


def test_a_verbose_answer_is_clipped_rather_than_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note three words long must not cost the ratings around it."""
    from vibe_sentinel.schemas import NOTE_CHARS

    _record(
        monkeypatch,
        {
            "assessment": "a" * 5000,
            "changes": [
                {"key": "k", "probe_id": "p", "severity": "high", "note": "n" * 5000}
            ],
        },
    )
    report = asyncio.run(
        analyze_drift(_report(_change("p", "k")), _snapshot(), lenses=(_lens("one"),))
    )
    assert report.analyzed is True
    assert report.changes[0].severity == "high"
    assert len(report.changes[0].note) == NOTE_CHARS


def test_the_prompt_quotes_the_cap_the_schema_enforces() -> None:
    """One number, said in both places a model could learn it from.

    In words, because the schema counts characters and no model does.
    """
    from vibe_sentinel.schemas import MAX_RATED, NOTE_CHARS, chars_to_words

    assert f"{chars_to_words(NOTE_CHARS)} words" in analyze_mod.ANALYZE_SYSTEM_PROMPT
    assert str(MAX_RATED) in analyze_mod.ANALYZE_SYSTEM_PROMPT
