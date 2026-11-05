"""Captured questions and answers remain evidence after declarations change."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from vibe_sentinel import analyze, credentials, gates, llm
from vibe_sentinel.__main__ import main
from vibe_sentinel.analyze import Lens
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.db import get_db
from vibe_sentinel.db import store
from vibe_sentinel.exceptions import LLMConnectionError
from vibe_sentinel.results import build_result, parse_result, project_result
from vibe_sentinel.schemas import (
    Change,
    DriftReport,
    GateFinding,
    GateReport,
    GateState,
    Observation,
    ProbeResult,
    RunProvenance,
    ScanContext,
    Snapshot,
)


def current() -> Snapshot:
    return Snapshot(
        context=ScanContext(
            provenance=RunProvenance(
                availability="captured", fingerprint_status="captured"
            ),
            baseline_run_id=1,
        ),
        probes={name: ProbeResult(probe_id=name) for name in ("p", "q")},
    )


def changed() -> DriftReport:
    return DriftReport(
        changes=[Change(probe_id="p", key="same", kind="grew", before=1, after=4)]
    )


async def judged(
    report: DriftReport, snapshot: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> DriftReport:
    answers = {
        "analyze-drift-first": {
            "assessment": "First answer",
            "changes": [
                {
                    "probe_id": "p",
                    "key": "same",
                    "severity": "medium",
                    "note": "First rating",
                },
            ],
        },
        "analyze-drift-second": {
            "assessment": "Second answer",
            "changes": [
                {
                    "probe_id": "p",
                    "key": "same",
                    "severity": "high",
                    "note": "Second rating",
                },
                {
                    "probe_id": "p",
                    "key": "invented",
                    "severity": "high",
                    "note": "RAW-UNKNOWN",
                },
            ],
        },
    }

    async def query(_context, _question, _schema, name, **_kwargs):
        return answers[name]

    monkeypatch.setattr(analyze, "llm_query", query)
    return await analyze.analyze_drift(
        report,
        snapshot,
        lenses=(
            Lens(
                id="first",
                title="First",
                source="original.toml",
                question="Original first question",
            ),
            Lens(id="second", title="Second", question="Original second question"),
        ),
    )


def test_each_answer_and_winning_attribution_are_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = current()
    report = asyncio.run(judged(changed(), snapshot, monkeypatch))
    result = build_result(snapshot, report)
    assert parse_result(result.model_dump(mode="json")) == result
    assert [lens.ratings[0].note for lens in result.evidence.lenses] == [
        "First rating",
        "Second rating",
    ]
    assert result.evidence.changes[0].lens_id == "second"
    assert result.evidence.lenses[1].dropped_ratings == 1
    assert "RAW-UNKNOWN" not in result.model_dump_json()
    assert result.evidence.lenses[0].question == "Original first question"
    assert result.evidence.lenses[0].source == "original.toml"


def test_repeated_keys_and_lower_ratings_do_not_invent_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = current()
    report = changed()
    report.changes.append(Change(probe_id="q", key="same", kind="appeared"))

    async def query(*_args, **_kwargs):
        return {
            "assessment": "",
            "changes": [
                {"key": "same", "severity": "high", "note": "ambiguous"},
                {"probe_id": "q", "key": "same", "severity": "high", "note": "correct"},
                {"probe_id": "q", "key": "same", "severity": "low", "note": "lower"},
            ],
        }

    monkeypatch.setattr(analyze, "llm_query", query)
    asyncio.run(
        analyze.analyze_drift(
            report, snapshot, lenses=(Lens(id="l", title="L", question="Q"),)
        )
    )
    evidence = build_result(snapshot, report).evidence
    assert evidence.changes[0].lens_id == ""
    assert evidence.changes[1].lens_id == "l"
    assert [rating.applied for rating in evidence.lenses[0].ratings] == [True, False]
    assert evidence.lenses[0].dropped_ratings == 1


@pytest.mark.parametrize("first_run", [True, False])
def test_no_question_needed_still_records_declaration(
    first_run: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Must not ask")

    monkeypatch.setattr(analyze, "llm_query", forbidden)
    report = DriftReport(first_run=first_run)
    asyncio.run(
        analyze.analyze_drift(
            report, current(), lenses=(Lens(id="l", title="L", question="Captured"),)
        )
    )
    assert report.lenses[0].evidence.status == "not_asked"
    assert report.lenses[0].evidence.question == "Captured"


def test_mixed_connection_failure_keeps_answered_and_failed_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def query(_context, _question, _schema, name, **_kwargs):
        if name.endswith("bad"):
            raise LLMConnectionError("RAW-DIAGNOSTIC")
        return {"assessment": "Recorded", "changes": []}

    monkeypatch.setattr(analyze, "llm_query", query)
    report = changed()
    with pytest.raises(LLMConnectionError):
        asyncio.run(
            analyze.analyze_drift(
                report,
                current(),
                lenses=(
                    Lens(id="good", title="Good", question="Good question"),
                    Lens(id="bad", title="Bad", question="Bad question"),
                ),
            )
        )
    assert [lens.evidence.status for lens in report.lenses] == ["answered", "failed"]
    encoded = build_result(current(), report).model_dump_json()
    assert "Recorded" in encoded and "Bad question" in encoded
    assert "RAW-DIAGNOSTIC" not in encoded


def test_detail_allowlist_and_remote_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = current()
    snapshot.probes["p"].command = ["ARGV-CANARY"]
    snapshot.probes["p"].error = "ERROR-CANARY"
    snapshot.probes["p"].observations = [
        Observation(key="same", attrs={"arbitrary": "ATTR-CANARY"})
    ]
    snapshot.probes["dependency-versions"] = ProbeResult(
        probe_id="dependency-versions",
        observations=[Observation(key="INVENTORY-CANARY", state="9.99")],
    )
    report = asyncio.run(judged(changed(), snapshot, monkeypatch))
    report.changes.append(
        Change(probe_id="dependency-versions", key="INVENTORY-CANARY", kind="appeared")
    )
    state = GateState(
        reports=(
            GateReport(
                gate="credentials",
                error="ERROR-CANARY",
                findings=(
                    GateFinding(
                        gate="credentials",
                        key="rule:path",
                        detail="EXCERPT-CANARY",
                        attrs={"arbitrary": "ATTR-CANARY"},
                    ),
                ),
            ),
        )
    )
    result = build_result(snapshot, report, state)
    detail = project_result(result, profile="local-detail")
    encoded = json.dumps(detail)
    for canary in (
        "ARGV-CANARY",
        "ERROR-CANARY",
        "ATTR-CANARY",
        "EXCERPT-CANARY",
        "INVENTORY-CANARY",
    ):
        assert canary not in encoded
    assert detail["result"]["evidence"]["withheld_dependency_changes"] == 1
    for profile in ("mcp", "shareable"):
        projected = project_result(result, snapshot, report, state, profile=profile)
        assert projected["result"]["evidence"]["availability"] == "withheld"
        assert "Original first question" not in json.dumps(projected)
        assert "First rating" not in json.dumps(projected)
        parse_result(projected["result"])


def test_history_export_uses_only_immutable_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = current()
    report = asyncio.run(judged(changed(), snapshot, monkeypatch))
    with get_db(tmp_path) as conn:
        run_id = store.save_run(conn, snapshot, report, 1, False)
        result = build_result(snapshot, report)
        store.save_result(conn, run_id, result)
        before = list(conn.iterdump())
    (tmp_path / ".vibe-sentinel.toml").write_text("deliberately invalid configuration")
    (tmp_path / "original.toml").write_text("New question must not enter history")
    assert (
        main(
            ["export", str(tmp_path), "--run", str(run_id), "--profile", "local-detail"]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert (
        exported["result"]["evidence"]["lenses"][0]["question"]
        == "Original first question"
    )
    with get_db(tmp_path, readonly=True) as conn:
        assert list(conn.iterdump()) == before
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM runs")
        assert store.load_result(conn, run_id) == result
    assert not (tmp_path / ".vibe-sentinel/logs").exists()


def test_export_missing_and_unfinished_history_are_explicit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export", str(tmp_path), "--run", "1"]) == 2
    assert not (tmp_path / ".vibe-sentinel").exists()
    with get_db(tmp_path) as conn:
        store.save_run(conn, Snapshot(), DriftReport(), None, False)
    assert main(["export", str(tmp_path), "--run", "1"]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("version", [1, 2])
def test_frozen_results_preserve_bytes_and_refuse_invented_detail(version: int) -> None:
    original = (Path(__file__).parent / f"fixtures/result-v{version}.json").read_text()
    result = parse_result(json.loads(original))
    assert result.model_dump_json() == original
    assert "evidence" not in project_result(result)["result"]
    with pytest.raises(ValueError, match="unavailable"):
        project_result(result, profile="local-detail")
    assert (
        project_result(result, profile="shareable")["result"]["schema_version"]
        == version
    )


@pytest.mark.parametrize(
    "corruption",
    ["identity", "status", "winner", "missing", "remote", "application", "omitted"],
)
def test_evidence_contract_rejects_contradictions(
    corruption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = current()
    report = asyncio.run(judged(changed(), snapshot, monkeypatch))
    payload = build_result(snapshot, report).model_dump(mode="json")
    evidence = payload["evidence"]
    if corruption == "identity":
        evidence["changes"][0]["probe_id"] = "unknown"
    elif corruption == "status":
        evidence["lenses"][0]["status"] = "not_asked"
    elif corruption == "winner":
        evidence["changes"][0]["lens_id"] = "first"
    elif corruption == "missing":
        del evidence["lenses"][0]["question"]
    elif corruption == "application":
        evidence["lenses"][0]["ratings"][0]["applied"] = False
    elif corruption == "omitted":
        evidence["lenses"] = []
        evidence["changes"][0]["lens_id"] = ""
    else:
        payload["profile"] = "shareable"
    with pytest.raises(ValueError, match="Incompatible"):
        parse_result(payload)


def test_credentials_literal_and_pin_are_not_model_answers() -> None:
    rule = credentials.Secret(
        id="fixture",
        title="Fixture",
        question="Configured question",
        pattern="value",
        verdict="placeholder",
    )
    candidate = credentials.Candidate(
        rule=rule.id, title=rule.title, path="fixture.py", lines=(1,)
    )
    scan = credentials.Scan(candidates=(candidate,))
    declared = asyncio.run(credentials.adjudicate(scan, (rule,), credentials.Policy()))
    assert declared.judgements[0].evidence.origin == "declared_verdict"
    state = GateState(
        reports=(gates.shape_credentials(scan, declared, credentials.Policy()),)
    )
    result = build_result(current(), DriftReport(), state)
    assert result.judges[0].status == "not_asked"
    assert result.evidence.gates[0].decision.status == "not_asked"
    pinned_policy = credentials.Policy(
        pins=({"paths": ["fixture.py"], "accept": ["fixture"]},)
    )
    pinned = asyncio.run(credentials.adjudicate(scan, (rule,), pinned_policy))
    assert pinned.judgements[0].evidence.origin == "pin"


def test_failed_credentials_preserve_partial_answers_and_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = tuple(
        credentials.Secret(
            id=name,
            title=name,
            question=f"Question {name}",
            pattern=name,
            verdict="placeholder" if name == "declared" else "",
        )
        for name in ("good", "bad", "declared")
    )
    scan = credentials.Scan(
        candidates=tuple(
            credentials.Candidate(rule=rule.id, title=rule.title, path=f"{rule.id}.py")
            for rule in rules
        )
    )

    async def query(_context, _question, _schema, name, **_kwargs):
        if name.endswith("bad"):
            raise LLMConnectionError("PRIVATE-FAILURE")
        return {"verdict": "placeholder", "reason": "Recorded answer"}

    monkeypatch.setattr(llm, "llm_query", query)
    evidence = {}
    with pytest.raises(LLMConnectionError):
        asyncio.run(
            credentials.adjudicate(
                scan, rules, credentials.Policy(), SentinelConfig(), evidence=evidence
            )
        )
    assert evidence[("good", "good.py")].decision.status == "answered"
    assert evidence[("good", "good.py")].reason == "Recorded answer"
    assert evidence[("bad", "bad.py")].decision.status == "failed"
    assert evidence[("declared", "declared.py")].decision.origin == "declared_verdict"
    assert "PRIVATE-FAILURE" not in str(evidence)


def test_evidence_bound_failure_keeps_exact_question_out_of_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = current()
    report = DriftReport(first_run=True)
    snapshot.context.baseline_run_id = None
    oversized = "x" * 32769
    asyncio.run(
        analyze.analyze_drift(
            report,
            snapshot,
            lenses=(Lens(id="large", title="Large", question=oversized),),
        )
    )
    assert report.lenses[0].evidence.question == oversized
    result = build_result(snapshot, report)
    assert (result.outcome, result.exit_code) == ("incomplete", 2)
    assert result.evidence.failure == "limit_exceeded"
    assert result.evidence.availability == "unavailable"
    assert not result.evidence.lenses
    assert oversized not in result.model_dump_json()
    assert parse_result(result.model_dump(mode="json")) == result
    shared = project_result(result, profile="shareable")
    assert shared["result"]["evidence"]["failure"] == "limit_exceeded"
    parse_result(shared["result"])


def test_oversized_identity_and_total_capture_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = current()
    report = changed()
    report.changes[0].key = "k" * 8193
    assert build_result(snapshot, report).evidence.failure == "limit_exceeded"
    report = changed()
    monkeypatch.setattr("vibe_sentinel.results.EVIDENCE_MAX_BYTES", 1)
    result = build_result(snapshot, report)
    assert (result.outcome, result.evidence.failure) == ("incomplete", "limit_exceeded")


def test_capture_limit_cannot_establish_baseline(tmp_path: Path) -> None:
    from vibe_sentinel import engine

    snapshot, report, run_id = asyncio.run(
        engine.scan_and_compare(
            [],
            tmp_path,
            lenses=(Lens(id="large", title="Large", question="x" * 32769),),
        )
    )
    with get_db(tmp_path) as conn:
        assert store.baseline_run(conn) is None
        store.save_result(conn, run_id, build_result(snapshot, report))
        assert store.load_result(conn, run_id).evidence.failure == "limit_exceeded"


def test_removed_and_added_measurements_retain_structural_identity() -> None:
    from vibe_sentinel.inventory import compare

    before = current()
    after = current()
    before.probes["p"].observations = [
        Observation(
            key="old",
            state="old-state",
            label="Old endpoint",
            attrs={
                "identity": "GET /old",
                "pointer": "/paths/~1old/get",
                "private": "ATTR-CANARY",
            },
        )
    ]
    after.probes["p"].observations = [
        Observation(
            key="new",
            state="new-state",
            label="New endpoint",
            attrs={
                "identity": "GET /new",
                "pointer": "/paths/~1new/get",
                "private": "ATTR-CANARY",
            },
        )
    ]
    report = compare(before, after, [])
    result = build_result(after, report)
    changes = {entry.key: entry for entry in result.evidence.changes}
    assert (
        changes["old"].label,
        changes["old"].before_state,
        changes["old"].structural.identity,
    ) == ("Old endpoint", "old-state", "GET /old")
    assert (
        changes["new"].label,
        changes["new"].after_state,
        changes["new"].structural.pointer,
    ) == ("New endpoint", "new-state", "/paths/~1new/get")
    assert "ATTR-CANARY" not in result.model_dump_json()


def test_near_miss_suspect_survives_success_and_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe_sentinel import packages

    first = packages.Finding(
        kind="near-miss",
        detail="Measured pair",
        remediation="Review",
        name="requests/requsets",
        evidence=["requests", "requsets"],
    )
    second = packages.Finding(
        kind="near-miss",
        detail="Measured pair",
        remediation="Review",
        name="other/othre",
        evidence=["other", "othre"],
    )
    monkeypatch.setattr(packages, "build_near_miss_context", lambda *_args: "")

    async def query(_context, _question, _schema, name, **_kwargs):
        if "other" in name:
            raise LLMConnectionError("unreachable")
        return {
            "verdict": "typosquat",
            "suspect": "requsets",
            "reason": "Review this pair.",
        }

    monkeypatch.setattr(llm, "llm_query", query)
    evidence = {}
    with pytest.raises(LLMConnectionError):
        asyncio.run(
            packages.review_near_misses(
                [first, second], None, packages.Policy(), evidence=evidence
            )
        )
    assert evidence[first.name].suspect == "requsets"
    adjudication = asyncio.run(
        packages.review_near_misses([first], None, packages.Policy())
    )
    finding = GateFinding(
        gate="packages",
        key="pair",
        kind="near-miss",
        verdict=adjudication[0].verdict,
        reason=adjudication[0].reason,
        evidence=adjudication[0].evidence,
        attrs={"suspect": adjudication[0].suspect},
    )
    result = build_result(
        current(),
        DriftReport(),
        GateState(reports=(GateReport(gate="packages", findings=(finding,)),)),
    )
    assert result.evidence.gates[0].suspect == "requsets"
