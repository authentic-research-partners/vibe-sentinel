"""The benchmark harness: the properties its numbers depend on.

A benchmark has the same rot problem a corpus has, and it fails the same
silent way. The prefix suite's whole claim is that its two arms differ in
one thing — whether the system message is the same bytes — and if the
"distinct" arm ever stopped being distinct, or started differing in size
as well, it would report a clean *no difference* and look like a finding.
So the properties the arms rest on are asserted here rather than trusted.

Nothing in this file needs a model. What it checks is the preparation,
which is the same split ``evals/`` keeps.
"""

from __future__ import annotations

import pytest

from benchmarks import report as render
from benchmarks import suites
from benchmarks.__main__ import _widths, build_parser
from benchmarks.harness import Condition, Measurement, Pass, Suite, question, report


def test_shared_prefix_is_byte_identical() -> None:
    """The arm that claims to share a prefix has to actually share one."""
    assert report() == report()
    assert suites._shared(1)[0] == suites._shared(2)[0]


def test_distinct_prefixes_differ_at_the_front() -> None:
    """A prefix cache is only defeated at the front, so the nonce goes there.

    Differing at the *end* would leave the cached prefix intact and make
    the distinct arm quietly measure the same cache hit as the shared one.
    """
    first, second = report(nonce="r0001"), report(nonce="r0002")
    assert first != second
    prefix_len = len("You rate changes in a codebase's organisation.")
    assert first[:prefix_len] == second[:prefix_len], "the fixed header moved"
    # The first difference has to land well before the end of the string.
    diff_at = next(i for i, (a, b) in enumerate(zip(first, second)) if a != b)
    assert diff_at < len(first) // 4


def test_the_arms_differ_only_in_the_nonce() -> None:
    """Same size, same shape. A longer prompt is a second variable."""
    shared, distinct = report(), report(nonce="r0001")
    assert shared.count("\n") == distinct.count("\n")
    assert abs(len(shared) - len(distinct)) <= len("r0001") + len("shared")


def test_questions_are_distinct_per_request() -> None:
    """Identical user halves would let a server dedupe the fan-out away."""
    asked = [question(i) for i in range(16)]
    assert len(set(asked)) == len(asked)


def test_a_width_wider_than_the_batch_is_refused() -> None:
    """A semaphore that is never the limit measures nothing, and says so."""
    with pytest.raises(ValueError) as caught:
        suites._check_widths((4, 32), requests=16)
    message = str(caught.value)
    assert "32" in message
    assert "--requests" in message and "--widths" in message


def test_a_width_equal_to_the_batch_is_allowed() -> None:
    suites._check_widths((1, 16), requests=16)


def test_every_option_carries_its_help() -> None:
    """The project's rule for its own CLI, applied to this one.

    An option shipped without ``help=`` renders as a bare ``--limit LIMIT``
    and every other check still passes.
    """
    parser = build_parser()
    assert parser.description
    missing = [
        action.option_strings or action.dest
        for action in parser._actions
        if not action.help and action.dest != "help"
    ]
    assert not missing, f"options with no help: {missing}"


def test_widths_rejects_what_is_not_a_width() -> None:
    import argparse

    for bad in ("four", "1,x", "", "0", "-2"):
        with pytest.raises(argparse.ArgumentTypeError):
            _widths(bad)
    assert _widths("1,2,16") == (1, 2, 16)


def _measurement(id_: str, seconds: list[float], unanswered: int = 0) -> Measurement:
    return Measurement(
        id=id_,
        label=id_,
        width=4,
        requests=2,
        passes=[
            Pass(
                seconds=s,
                answered=2 - unanswered,
                unanswered=unanswered,
                latencies=[s / 2, s / 2],
            )
            for s in seconds
        ],
    )


def test_the_median_pass_is_the_reported_one() -> None:
    """Three passes, one of them an outlier, and the middle one survives."""
    m = _measurement("w", [10.0, 11.0, 40.0])
    assert m.seconds == 11.0
    assert m.fastest == 10.0
    assert m.slowest_pass == 40.0


def test_a_condition_that_did_not_answer_says_so_in_the_table() -> None:
    """A fast pass whose requests came back unusable is not a fast pass."""
    suite = Suite(
        id="s",
        question="q",
        measurements=[_measurement("clean", [10.0]), _measurement("silent", [1.0], 2)],
    )
    rendered = "\n".join(render._table(suite))
    assert "2 unanswered" in rendered
    assert "unanswered" not in rendered.split("\n")[2]  # not on the clean row


def test_the_environment_records_no_machine_identity() -> None:
    """A result file is written to be committed and read by strangers."""
    fields = set(render.Environment.model_fields)
    assert not fields & {"hostname", "host", "user", "path", "cwd", "endpoint"}


def test_a_condition_is_a_callable_payload() -> None:
    """The one shape the harness needs from a suite."""
    condition = Condition(id="c", label="c", width=2, payload=suites._shared)
    system, user = condition.payload(3)
    assert system == report()
    assert "3" in user


# --- The truncation suite, and the premise it has to check -------------------
#
# Its two arms are named "fits" and "truncates", and the gap between them is
# only a price if both did what their names say. An arm that behaved otherwise
# makes the gap a measurement of something else, and a duration with the wrong
# name on it is worse than no duration — the same rule `DriftReport.unmeasured`
# keeps upstream.


def _passes(seconds: float, truncated: int | None) -> list[Pass]:
    return [
        Pass(
            seconds=seconds,
            answered=2,
            unanswered=0,
            truncated=truncated,
            latencies=[seconds / 2, seconds / 2],
        )
    ]


def _canned(id_: str, seconds: float, truncated: int | None) -> Measurement:
    return Measurement(
        id=id_, label=id_, width=4, requests=2, passes=_passes(seconds, truncated)
    )


def _run_truncation(
    monkeypatch: pytest.MonkeyPatch,
    fits: int | None,
    truncates: int | None,
    requests: int = 2,
) -> Suite:
    import asyncio

    from vibe_sentinel.config import SentinelConfig

    async def fake_measure(condition, config, requests, repeat):  # type: ignore[no-untyped-def]
        seconds = 10.0 if condition.id == "fits" else 18.0
        return _canned(
            condition.id, seconds, fits if condition.id == "fits" else truncates
        )

    monkeypatch.setattr(suites, "measure", fake_measure)
    return asyncio.run(
        suites.truncation(
            SentinelConfig(max_tokens=1024), requests=requests, repeat=1, width=2
        )
    )


def test_the_truncating_arm_gets_a_budget_the_answer_overruns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is the only thing the two arms differ in, and it is
    absolute rather than a share of the other arm — what forces an over-run
    is a ceiling below the answer, which has nothing to do with what the
    fitting arm was given."""
    suite = _run_truncation(monkeypatch, fits=0, truncates=3)
    assert "1024 tokens against 256" in suite.note
    assert "retries them at 512" in suite.note


def test_a_measured_truncation_reports_as_a_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _run_truncation(monkeypatch, fits=0, truncates=3)
    assert "NOT MEASURED" not in suite.note


def test_an_arm_that_barely_truncated_priced_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not "more than zero": a handful of over-runs in a batch moves a median
    by less than the spread between passes, so the arms differ in nothing a
    reader could act on. This is the check that let a null manipulation
    through once — three truncations in forty-eight, reported as measured."""
    for truncated in (0, 3):
        suite = _run_truncation(monkeypatch, fits=0, truncates=truncated, requests=10)
        assert "NOT MEASURED" in suite.note
        assert f"only {truncated} of 10 requests" in suite.note


def test_an_arm_that_mostly_truncated_is_a_manipulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half is the line, so half passes and the whole batch certainly does."""
    for truncated in (5, 10):
        suite = _run_truncation(monkeypatch, fits=0, truncates=truncated, requests=10)
        assert "NOT MEASURED" not in suite.note


def test_both_arms_truncating_understates_the_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _run_truncation(monkeypatch, fits=2, truncates=5)
    assert "NOT MEASURED" in suite.note
    assert "both arms paid retries" in suite.note


def test_a_backend_with_no_metrics_cannot_attribute_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None is not zero here either: unseen is not the same as none happened."""
    suite = _run_truncation(monkeypatch, fits=None, truncates=None)
    assert "NOT MEASURED" in suite.note
    assert "publishes no metrics" in suite.note


def test_a_condition_can_carry_its_own_budget() -> None:
    """The one request field a condition may move, and only this suite does."""
    plain = Condition(id="c", label="c", width=2, payload=suites._shared)
    assert plain.max_tokens is None
    capped = Condition(
        id="c", label="c", width=2, payload=suites._shared, max_tokens=256
    )
    assert capped.max_tokens == 256


def test_an_arm_whose_retry_also_failed_did_not_price_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`llm_query` doubles once. An answer that overruns twice is given up on,
    and timing that is timing a failure rather than a retry."""
    import asyncio

    from vibe_sentinel.config import SentinelConfig

    async def fake_measure(condition, config, requests, repeat):  # type: ignore[no-untyped-def]
        truncated, unanswered = (0, 0) if condition.id == "fits" else (4, 2)
        return Measurement(
            id=condition.id,
            label=condition.id,
            width=4,
            requests=2,
            passes=[
                Pass(
                    seconds=12.0,
                    answered=2 - unanswered,
                    unanswered=unanswered,
                    truncated=truncated,
                    latencies=[6.0, 6.0],
                )
            ],
        )

    monkeypatch.setattr(suites, "measure", fake_measure)
    suite = asyncio.run(
        suites.truncation(
            SentinelConfig(max_tokens=1024), requests=2, repeat=1, width=2
        )
    )
    assert "NOT MEASURED" in suite.note
    assert "never came back" in suite.note


def test_an_unseen_cache_rate_is_a_dash_not_a_zero() -> None:
    """A backend that reports nothing has not reported a cache that missed."""
    suite = Suite(id="s", question="q", measurements=[_canned("a", 10.0, None)])
    rendered = "\n".join(render._table(suite))
    assert "—" in rendered
    assert "0%" not in rendered


def test_a_measured_cache_rate_is_pooled_across_passes() -> None:
    """A rate of rates would weight a short pass like a long one."""
    m = Measurement(
        id="a",
        label="a",
        width=4,
        requests=2,
        passes=[
            Pass(
                seconds=1.0,
                answered=2,
                unanswered=0,
                prefix_hits=90,
                prefix_queries=100,
                latencies=[0.5, 0.5],
            ),
            Pass(
                seconds=1.0,
                answered=2,
                unanswered=0,
                prefix_hits=10,
                prefix_queries=100,
                latencies=[0.5, 0.5],
            ),
        ],
    )
    assert m.prefix_hit_rate == 0.5
    assert "50%" in "\n".join(
        render._table(Suite(id="s", question="q", measurements=[m]))
    )


def test_a_single_pass_has_no_spread_to_report() -> None:
    """One pass cannot disagree with itself, and `±0%` would claim it did."""
    assert _canned("a", 10.0, 0).spread is None
    assert "±" not in "\n".join(
        render._table(Suite(id="s", question="q", measurements=[_canned("a", 10.0, 0)]))
    )


def test_disagreeing_passes_say_so_beside_the_median() -> None:
    """The failure this exists for: a median of two that looked solid."""
    m = Measurement(
        id="w16",
        label="concurrency = 16",
        width=16,
        requests=2,
        passes=_passes(41.06, 0) + _passes(19.08, 0),
    )
    assert m.spread is not None
    assert round(m.spread * 100) == 115
    assert "±115%" in "\n".join(
        render._table(Suite(id="s", question="q", measurements=[m]))
    )


def test_preemption_is_reported_when_the_cache_ran_out() -> None:
    """The cliff that bounds a fan-out, named on the row it happened to.

    Throughput flattens gently and noisily; preemption does not. A width can
    be called too wide because the server evicted work, which is a reason,
    rather than because a median moved, which on this data often is not.
    """
    m = Measurement(
        id="w64",
        label="concurrency = 64",
        width=64,
        requests=2,
        passes=[
            Pass(
                seconds=30.0,
                answered=2,
                unanswered=0,
                preemptions=7,
                latencies=[15.0, 15.0],
            )
        ],
    )
    assert m.preemptions == 7
    assert "7 preempted" in "\n".join(
        render._table(Suite(id="s", question="q", measurements=[m]))
    )


def test_no_preemption_says_nothing() -> None:
    """A quiet row stays quiet; zero is not worth a column of its own."""
    rendered = "\n".join(
        render._table(Suite(id="s", question="q", measurements=[_canned("a", 10.0, 0)]))
    )
    assert "preempted" not in rendered
