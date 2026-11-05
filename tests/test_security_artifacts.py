"""Browser exposure is a separate, explicit walk with bounded private input."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import credentials, security_artifacts
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError


@pytest.fixture
def export(tmp_path: Path) -> Path:
    target = tmp_path / "out"
    (target / "_next/static").mkdir(parents=True)
    (target / "index.html").write_text("<html></html>")
    return target


def run(root: Path, artifact: str = "out", **config: Any) -> dict[str, Any]:
    return asyncio.run(
        security_artifacts.check(root, artifact, SentinelConfig(**config))
    )


def test_exposure_reuses_redaction_and_never_returns_value_or_judge_reason(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = f"sk_test_{'SyntheticFixture' * 2}"
    (export / "_next/static/chunk.js").write_text(f'const token="{value}";')
    (export.parent / ".vibe-sentinel.toml").write_text(
        "[credentials]\nuse_builtins = false\n"
    )
    calls = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert value not in system
        return {"verdict": "real", "reason": f"Sensitive judge output {value}"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert calls
    assert result["status"] == "findings"
    assert result["findings"][0]["path"] == "out/_next/static/chunk.js"
    assert result["findings"][0]["line"] == 1
    assert value not in json.dumps(result)
    assert "Sensitive judge output" not in json.dumps(result)
    assert result["findings"] == run(export.parent)["findings"]


def test_public_identifier_is_not_automatically_a_credential(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = "pk_live_aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY"
    (export / "_next/static/chunk.js").write_text(f'const api_key="{public}";')
    calls = []

    async def judge(*args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(args)
        return {"verdict": "placeholder", "reason": "Public publishable identifier."}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert calls
    assert result["coverage"]["candidates"] > 0
    assert result["status"] == "complete"
    assert result["findings"] == []
    assert public not in json.dumps(result)


def test_judge_batch_timeout_leaves_coverage_incomplete(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "index.html").write_text('<script>const password="hunter22";</script>')
    monkeypatch.setattr(security_artifacts, "REVIEW_TIMEOUT", 0)

    async def slow(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(credentials, "adjudicate", slow)
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert result["findings"] == []


def test_oversized_candidate_context_marks_coverage_incomplete(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "index.html").write_text('<script>const password="hunter22";</script>')
    monkeypatch.setattr(security_artifacts, "MAX_CONTEXT_CHARS", 1)
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["coverage"]["oversized_candidate_contexts"] > 0
    assert result["coverage"]["candidates"] == 0
    assert "exceeded 1 characters and remain unassessed" in result["summary"]


def test_remote_judge_never_receives_artifacts_even_with_policy_opt_in(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export.parent / ".vibe-sentinel.toml").write_text(
        "[credentials]\nallow_remote_model = true\n"
    )
    (export / "index.html").write_text('<script>const password="hunter22";</script>')

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Remote model must not be called")

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", forbidden)
    result = run(export.parent, llm_endpoint="https://judge.example/v1")
    assert result["status"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert result["findings"] == []


@pytest.mark.parametrize("selection", ["missing", "../out", "/tmp", "."])
def test_missing_escaping_or_nonexport_selection(export: Path, selection: str) -> None:
    assert run(export.parent, selection)["status"] == "unavailable"


def test_symlink_selection_and_symlink_files_are_not_read(export: Path) -> None:
    outside = export.parent / "private.js"
    outside.write_text('const password="hunter22";')
    (export.parent / "linked").symlink_to(export, target_is_directory=True)
    assert run(export.parent, "linked")["status"] == "unavailable"
    (export / "_next/static/private.js").symlink_to(outside)
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["coverage"]["unreadable_or_unsafe"] == 1
    assert result["coverage"]["candidates"] == 0


def test_explicit_artifact_walk_does_not_widen_source_walk(export: Path) -> None:
    (export / "index.html").write_text('<script>const password="hunter22";</script>')
    # Generated files remain excluded from the ordinary credential walk.
    generated = export.parent / ".next"
    generated.mkdir()
    (generated / "chunk.js").write_text('const password="hunter22";')
    scanned = credentials.search(
        export.parent, credentials.builtin_secrets(), credentials.Policy()
    )
    assert not any(c.path.startswith(".next/") for c in scanned.candidates)
    assert run(export.parent)["status"] == "findings"


@pytest.mark.parametrize("kind", ["size", "entries", "unsupported", "encoding"])
def test_partial_coverage_is_not_clean(
    export: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "size":
        monkeypatch.setattr(security_artifacts, "MAX_FILE_BYTES", 2)
    elif kind == "entries":
        monkeypatch.setattr(security_artifacts, "MAX_ENTRIES", 1)
    elif kind == "unsupported":
        (export / "packed.js.gz").write_bytes(b"compressed")
    else:
        (export / "chunk.js").write_bytes(b"\xff")
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["coverage"]["complete"] is False
    assert {
        "size": "A scan resource limit was reached",
        "entries": "A scan resource limit was reached",
        "unsupported": "1 file(s) have unsupported formats",
        "encoding": "1 file(s) could not be read safely",
    }[kind] in result["summary"]


def test_judge_failure_is_error_without_exception_details(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "index.html").write_text('<script>const password="hunter22";</script>')

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise LLMConnectionError("hunter22 sensitive failure")

    monkeypatch.setattr(credentials, "adjudicate", fail)
    result = run(export.parent)
    assert result["status"] == "error"
    assert "hunter22" not in json.dumps(result)
    assert result["coverage"]["complete"] is False


def test_media_exclusions_are_explicit_without_making_text_coverage_partial(
    export: Path,
) -> None:
    (export / "logo.png").write_bytes(b"\x89PNG\x00")
    result = run(export.parent)
    assert result["status"] == "complete"
    assert result["coverage"]["media_excluded"] == 1
    assert "excluded" in result["coverage"]["scope"]


def test_partial_coverage_retains_findings_but_is_unavailable(export: Path) -> None:
    (export / "index.html").write_text('<script>const password="hunter22";</script>')
    (export / "packed.js.br").write_bytes(b"compressed")
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["findings"]
    assert not result["coverage"]["complete"]


def test_candidate_and_request_counts_are_bounded(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(4):
        (export / f"_next/static/chunk{index}.js").write_text(
            'const password="hunter22";'
        )
    monkeypatch.setattr(security_artifacts, "MAX_CANDIDATES", 2)
    calls = []

    async def judge(*args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(args)
        return {"verdict": "real", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert len(calls) == 2
    assert result["findings"]
    assert result["coverage"]["candidates"] == 2
    assert result["coverage"]["limited"]


def test_weak_shape_in_generated_content_does_not_use_judge_budget(
    export: Path,
) -> None:
    (export / "_next/static/chunk.js").write_text('const route="hunter22";')
    assert any(
        candidate.rule == "password-shape"
        for candidate in credentials.search(
            export,
            credentials.builtin_secrets(),
            credentials.Policy(),
        ).candidates
    )
    result = run(export.parent)
    assert result["status"] == "complete"
    assert result["coverage"]["candidates"] == 0
    assert result["coverage"]["excluded_candidate_rules"] == ["password-shape"]


@pytest.mark.parametrize("separator", [";", "\n"])
def test_every_match_is_judged_in_bounded_redacted_contexts(
    export: Path, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    # More than the source walker's 12 matching lines, or one huge minified
    # line. The late match must reach the judge in both representations.
    values = [f"secretValue{index:04d}" for index in range(24)]
    statements = [
        f'const password_marker{index:02d}="{value}"'
        for index, value in enumerate(values)
    ]
    (export / "_next/static/large.js").write_text(
        "// header\n"
        + separator.join(statement + ";" * 5000 for statement in statements)
    )
    calls: list[str] = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert len(system) <= security_artifacts.MAX_CONTEXT_CHARS
        assert all(value not in system for value in values)
        return {"verdict": "real", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert result["status"] == "findings"
    assert result["coverage"]["complete"]
    assert result["coverage"]["oversized_candidate_contexts"] == 0
    assert len(calls) > 1
    assert all(f"password_marker{index:02d}" in "\n".join(calls) for index in range(24))
    assert len(result["findings"]) == 1
    assert result["findings"][0]["line"] == 2
    assert result["findings"][0]["path"] == "out/_next/static/large.js"


def test_neighbouring_and_overlapping_values_are_redacted_before_windowing(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = f"ghp_{'SyntheticFixture' * 2}"
    weak = "hunter22"
    nested = "mongodb://user:VeryPrivatePassword@production.example/database"
    (export / "_next/static/large.js").write_text(
        f'const route="{weak}";const password="{nested}";const token="{value}";'
    )
    calls = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert value not in system
        assert weak not in system
        assert "VeryPrivatePassword" not in system
        assert len(system) <= security_artifacts.MAX_CONTEXT_CHARS
        return {"verdict": "placeholder", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    assert run(export.parent)["status"] == "complete"
    assert calls


def test_late_real_window_outranks_earlier_unclear_with_original_line(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "index.html").write_text(
        "\n".join(
            f'const password_marker{index:02d}="hunter22";' for index in range(24)
        )
    )
    real_line = []

    async def judge(
        scan: credentials.Scan, *args: Any, **kwargs: Any
    ) -> credentials.Adjudication:
        candidate = scan.candidates[0]
        verdict = "real" if 24 in candidate.lines else "unclear"
        if verdict == "real":
            real_line.append(candidate.lines[0])
        return credentials.Adjudication(
            scan=scan,
            judgements=(credentials.Judgement(candidate=candidate, verdict=verdict),),
        )

    monkeypatch.setattr(credentials, "adjudicate", judge)
    result = run(export.parent)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["line"] == real_line[0]
    assert (
        result["findings"][0]["message"]
        == "Credential exposure in browser-delivered artifacts."
    )


def test_many_windows_in_one_file_hit_request_limit_without_claiming_complete(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "index.html").write_text(
        "\n".join(
            f'const password_marker{index:02d}="hunter22";' for index in range(100)
        )
    )
    monkeypatch.setattr(security_artifacts, "MAX_CANDIDATES", 2)
    calls = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert len(system) <= security_artifacts.MAX_CONTEXT_CHARS
        return {"verdict": "placeholder", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert len(calls) == 2
    assert result["status"] == "unavailable"
    assert result["coverage"]["limited"]
    assert not result["coverage"]["complete"]


@pytest.mark.parametrize("suffix", [".md", ".xml"])
def test_additional_text_formats_are_reviewed(export: Path, suffix: str) -> None:
    (export / f"document{suffix}").write_text('password = "hunter22"')
    result = run(export.parent)
    assert result["status"] == "findings"
    assert result["coverage"]["unsupported_files"] == 0
    assert result["findings"][0]["path"] == f"out/document{suffix}"


def test_ds_store_is_explicitly_excluded_metadata(export: Path) -> None:
    (export / ".DS_Store").write_bytes(b"\x00Finder metadata")
    result = run(export.parent)
    assert result["status"] == "complete"
    assert result["coverage"]["metadata_excluded"] == 1
    assert result["coverage"]["unsupported_files"] == 0


def test_pdf_document_text_is_reviewed_without_claiming_binary_line(
    export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (export / "document.pdf").write_bytes(b"%PDF-fixture")
    monkeypatch.setattr(
        security_artifacts.security_pdf, "extract", lambda _: 'password = "hunter22"'
    )
    result = run(export.parent)
    assert result["status"] == "findings"
    assert result["coverage"]["pdf_text_extracted"] == 1
    assert result["coverage"]["pdf_text_failed"] == 0
    assert "no OCR or attachments" in result["coverage"]["scope"]
    assert result["findings"][0]["path"] == "out/document.pdf"
    assert "line" not in result["findings"][0]


@pytest.mark.parametrize("failure", [ValueError, OSError])
def test_pdf_extraction_failure_remains_incomplete_without_diagnostics(
    export: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    (export / "document.pdf").write_bytes(b"%PDF-fixture")

    def fail(_: bytes) -> str:
        raise failure("private credential diagnostic")

    monkeypatch.setattr(security_artifacts.security_pdf, "extract", fail)
    result = run(export.parent)
    assert result["status"] == "unavailable"
    assert result["coverage"]["pdf_text_failed"] == 1
    assert not result["coverage"]["complete"]
    assert "private credential diagnostic" not in json.dumps(result)
    assert "PDF file(s) could not yield supported document text" in result["summary"]


@pytest.mark.parametrize("newline", ["\n", "\r\n", r"\n", ""])
@pytest.mark.parametrize("closed", [True, False])
def test_private_key_body_never_enters_header_context(
    export: Path, monkeypatch: pytest.MonkeyPatch, newline: str, closed: bool
) -> None:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDMabc123fixture"
    text = newline.join(
        (
            "-----BEGIN PRIVATE KEY-----",
            body,
            "-----END PRIVATE KEY-----" if closed else "",
        )
    )
    (export / "private.txt").write_text(text)
    secrets = tuple(
        s for s in credentials.builtin_secrets() if s.applies_to == "content"
    )
    candidates, oversized, obscured = security_artifacts._content_candidates(
        text, "private.txt", secrets, credentials.Policy()
    )
    assert candidates and not oversized and not obscured
    for candidate in candidates:
        assert body not in candidate.excerpt
        assert body not in candidate.local_excerpt
        assert "MIIEvQIBADANBgkqh" not in candidate.excerpt
        assert "MIIEvQIBADANBgkqh" not in candidate.local_excerpt
    # Source review keeps its existing header-only behavior.
    source = credentials._content_candidates(
        text, "private.txt", secrets, credentials.Policy()
    )
    assert source
    if newline in {"\n", "\r\n"}:
        assert all(body not in candidate.local_excerpt for candidate in source)
    calls = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert "MIIEvQIBADANBgkqh" not in system
        return {"verdict": "real", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert calls
    assert result["status"] == "findings"
    assert result["findings"][0]["path"] == "out/private.txt"
    assert result["findings"][0]["line"] == 1


@pytest.mark.parametrize("closed", [False, True])
def test_independent_nomination_hidden_by_pem_mask_is_explicitly_unassessed(
    export: Path, monkeypatch: pytest.MonkeyPatch, closed: bool
) -> None:
    token = f"sk_test_{'SyntheticFixture' * 2}"
    text = f'const pemStart="-----BEGIN PRIVATE KEY-----"; const token="{token}";'
    if closed:
        text += 'const pemEnd="-----END PRIVATE KEY-----";'
    (export / "chunk.js").write_text(text)
    calls = []

    async def judge(system: str, *args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(system)
        assert token not in system
        return {"verdict": "placeholder", "reason": "fixture"}

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", judge)
    result = run(export.parent)
    assert calls
    assert result["status"] == "unavailable"
    assert not result["coverage"]["complete"]
    assert result["coverage"]["obscured_candidate_matches"] >= 2
    assert result["coverage"]["oversized_candidate_contexts"] == 0
    assert "obscured by private-key redaction remain unassessed" in result["summary"]
    assert token not in json.dumps(result)
