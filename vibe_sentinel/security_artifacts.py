"""Credential review of an explicitly selected, already built Next.js export.

This walk is separate from source inventory. It never builds, runs git, or
changes the ordinary generated-directory exclusions. Only reduced candidate
values reach a loopback judge; results contain fixed messages and locations.
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from vibe_sentinel import credentials, security_pdf
from vibe_sentinel.config import SentinelConfig
from vibe_sentinel.exceptions import LLMConnectionError

MAX_ENTRIES = 5000
MAX_CANDIDATES = 100
MAX_CONTEXT_CHARS = 4096
REVIEW_TIMEOUT = 180
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
TEXT_SUFFIXES = frozenset(
    {".html", ".js", ".mjs", ".json", ".map", ".css", ".txt", ".svg", ".md", ".xml"}
)
MEDIA_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".mp4",
        ".webm",
    }
)


def _result(status: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "browser-artifacts",
        "status": status,
        "summary": summary,
        "findings": [],
        **extra,
    }


def _open_export(root: Path, artifact: str) -> int:
    """Open every component without following links, including ancestors."""
    selected = Path(artifact)
    if selected.is_absolute() or not selected.parts or ".." in selected.parts:
        raise ValueError("Select a relative export directory inside the project.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root.resolve(), flags)
    try:
        for part in selected.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _reduced_text(
    text: str, spans: list[tuple[int, int]], reveal: int | None
) -> tuple[str, list[tuple[int, int]]]:
    """Reduce before windowing, so boundaries cannot expose neighbouring values."""
    pieces: list[str] = []
    offsets: list[tuple[int, int]] = []
    previous = size = 0
    for start, end in spans:
        prefix = text[previous:start]
        value = text[start:end]
        reduced = (
            credentials.blind(value)
            if reveal is None
            else credentials.partial(value, reveal)
        )
        pieces.extend((prefix, reduced))
        size += len(prefix)
        offsets.append((size, size + len(reduced)))
        size += len(reduced)
        previous = end
    pieces.append(text[previous:])
    return "".join(pieces), offsets


def _content_candidates(
    text: str,
    path: str,
    secrets: tuple[credentials.Secret, ...],
    policy: credentials.Policy,
) -> tuple[list[credentials.Candidate], int, int]:
    """Pack every nominated match into independently reviewed bounded contexts.

    Unlike source review's sampled matching lines, browser artifacts cover all
    nominations, including late matches in minified files. All rules redact,
    including weak shapes that do not themselves warrant a judge request.
    """
    starts = credentials._line_starts(text)
    hits: dict[str, list[tuple[int, int, int]]] = {}
    all_spans: list[tuple[int, int]] = []
    private_spans: list[tuple[int, int]] = []
    for secret in secrets:
        for match in re.finditer(secret.pattern, text):
            start, end = credentials._secret_span(match)
            if secret.id == "private-key-block":
                # The built-in nominates only a PEM header. Browser exports
                # can serialize the body onto that same line, so redact the
                # entire block, including unfinished blocks, before windowing.
                closing = re.compile(secret.pattern.replace("BEGIN", "END")).search(
                    text, end
                )
                end = closing.end() if closing else len(text)
                private_spans.append((start, end))
            refine = credentials._REFINERS.get(secret.id)
            if refine is not None and not refine(text[start:end]):
                continue
            all_spans.append((start, end))
            if secret.id != "password-shape":
                hits.setdefault(secret.id, []).append(
                    (start, end, credentials._line_of(starts, match.start()))
                )
    if not hits:
        return [], 0, 0
    spans = credentials._merged(all_spans)
    span_starts = [start for start, _ in spans]
    safe, safe_offsets = _reduced_text(text, spans, None)
    local, local_offsets = _reduced_text(text, spans, policy.reveal_chars)
    found: list[credentials.Candidate] = []
    oversized = 0
    obscured = 0
    private_spans = credentials._merged(private_spans)
    private_starts = [start for start, _ in private_spans]
    for secret in secrets:
        candidate = credentials.Candidate(
            rule=secret.id,
            title=secret.title,
            path=path,
            exposure="browser-delivered static export",
        )
        seen: set[tuple[int, int]] = set()
        for start, end, number in hits.get(secret.id, []):
            private_index = bisect.bisect_left(private_starts, end) - 1
            if (
                secret.id != "private-key-block"
                and private_index >= 0
                and private_spans[private_index][1] > start
            ):
                # Full PEM masking can hide independent nominated evidence,
                # especially after an unfinished header. A judgement about
                # the enclosing block cannot stand in for that missing input.
                obscured += 1
                continue
            index = bisect.bisect_right(span_starts, start) - 1
            if (index, number) in seen:
                continue
            seen.add((index, number))
            excerpts = []
            for reduced, offsets in ((safe, safe_offsets), (local, local_offsets)):
                left, right = offsets[index]
                # Match the source gate's line-local context. A private-key
                # rule nominates its header, not the unmarked body on the
                # next line, which must never enter the surrounding window.
                line_start = reduced.rfind("\n", 0, left) + 1
                line_end = reduced.find("\n", right)
                if line_end < 0:
                    line_end = len(reduced)
                left = max(line_start, left - credentials._CONTEXT_CHARS)
                right = min(line_end, right + credentials._CONTEXT_CHARS)
                excerpts.append(
                    f"{number}: "
                    + ("…" if left else "")
                    + reduced[left:right]
                    + ("…" if right < len(reduced) else "")
                )
            piece = candidate.model_copy(
                update={
                    "lines": (number,),
                    "excerpt": excerpts[0],
                    "local_excerpt": excerpts[1],
                }
            )
            combined = piece.model_copy(
                update={
                    "lines": tuple(sorted(set((*candidate.lines, number)))),
                    "excerpt": "\n".join(
                        filter(None, (candidate.excerpt, piece.excerpt))
                    ),
                    "local_excerpt": "\n".join(
                        filter(None, (candidate.local_excerpt, piece.local_excerpt))
                    ),
                }
            )
            if (
                len(credentials.build_context(path, candidate.exposure, [combined]))
                <= MAX_CONTEXT_CHARS
            ):
                candidate = combined
                continue
            if candidate.lines:
                found.append(candidate)
            if (
                len(credentials.build_context(path, piece.exposure, [piece]))
                > MAX_CONTEXT_CHARS
            ):
                oversized += 1
                candidate = candidate.model_copy(
                    update={"lines": (), "excerpt": "", "local_excerpt": ""}
                )
            else:
                candidate = piece
        if candidate.lines:
            found.append(candidate)
    return found, oversized, obscured


def _gather(
    descriptor: int, secrets: tuple[credentials.Secret, ...], policy: credentials.Policy
) -> tuple[list[credentials.Candidate], dict[str, Any]]:
    candidates: list[credentials.Candidate] = []
    coverage: dict[str, Any] = {
        "scope": "Next.js static export HTML, JavaScript, JSON, source maps, CSS, SVG, Markdown, XML and text; PDF extracted document text only (no OCR or attachments); binary images, fonts, media and .DS_Store metadata excluded",
        "excluded_candidate_rules": ["password-shape"],
        "complete": True,
        "files_read": 0,
        "entries_seen": 0,
        "bytes_read": 0,
        "unsupported_files": 0,
        "oversized_candidate_contexts": 0,
        "obscured_candidate_matches": 0,
        "media_excluded": 0,
        "metadata_excluded": 0,
        "pdf_text_extracted": 0,
        "pdf_text_failed": 0,
        "unreadable_or_unsafe": 0,
        "limited": False,
        "has_html": False,
        "has_next_static": False,
    }

    def walk(directory: int, prefix: str, depth: int) -> None:
        if depth > 32:
            coverage["limited"] = True
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                coverage["entries_seen"] += 1
                if coverage["entries_seen"] > MAX_ENTRIES:
                    coverage["limited"] = True
                    return
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory,
                        )
                        try:
                            if path == "_next/static":
                                coverage["has_next_static"] = True
                            walk(child, path, depth + 1)
                        finally:
                            os.close(child)
                        if coverage["limited"]:
                            return
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        coverage["unreadable_or_unsafe"] += 1
                        continue
                    if entry.name == ".DS_Store":
                        coverage["metadata_excluded"] += 1
                        continue
                    suffix = Path(entry.name).suffix.lower()
                    if suffix in MEDIA_SUFFIXES:
                        coverage["media_excluded"] += 1
                        continue
                    if suffix not in TEXT_SUFFIXES and suffix != ".pdf":
                        coverage["unsupported_files"] += 1
                        continue
                    if info.st_size > MAX_FILE_BYTES:
                        coverage["limited"] = True
                        return
                    file_fd = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    with os.fdopen(file_fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            coverage["unreadable_or_unsafe"] += 1
                            continue
                        body = stream.read(MAX_FILE_BYTES + 1)
                    coverage["bytes_read"] += len(body)
                    if (
                        len(body) > MAX_FILE_BYTES
                        or coverage["bytes_read"] > MAX_TOTAL_BYTES
                    ):
                        coverage["limited"] = True
                        return
                    if suffix == ".pdf":
                        try:
                            text = security_pdf.extract(body)
                        except (OSError, ValueError):
                            coverage["pdf_text_failed"] += 1
                            continue
                        coverage["pdf_text_extracted"] += 1
                    else:
                        text = body.decode("utf-8")
                    coverage["files_read"] += 1
                    coverage["has_html"] |= path.endswith(".html")
                    nominated, oversized, obscured = _content_candidates(
                        text, path, secrets, policy
                    )
                    coverage["oversized_candidate_contexts"] += oversized
                    coverage["obscured_candidate_matches"] += obscured
                    candidates.extend(nominated)
                    if len(candidates) > MAX_CANDIDATES:
                        del candidates[MAX_CANDIDATES:]
                        coverage["limited"] = True
                        return
                except (OSError, UnicodeError):
                    coverage["unreadable_or_unsafe"] += 1

    # Check conventional markers before a limit can interrupt traversal of
    # a large chunk directory. A partial scan retains already judged findings.
    try:
        coverage["has_html"] = stat.S_ISREG(
            os.stat("index.html", dir_fd=descriptor, follow_symlinks=False).st_mode
        )
        next_fd = os.open(
            "_next", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
        )
        try:
            coverage["has_next_static"] = stat.S_ISDIR(
                os.stat("static", dir_fd=next_fd, follow_symlinks=False).st_mode
            )
        finally:
            os.close(next_fd)
    except OSError:
        coverage["has_next_static"] = False
    walk(descriptor, "", 0)
    coverage["complete"] = not any(
        coverage[key]
        for key in (
            "limited",
            "unsupported_files",
            "pdf_text_failed",
            "unreadable_or_unsafe",
            "oversized_candidate_contexts",
            "obscured_candidate_matches",
        )
    )
    coverage["candidates"] = len(candidates)
    return candidates, coverage


async def check(root: Path, artifact: str, config: SentinelConfig) -> dict[str, Any]:
    """Check existing browser artifacts without executing application code."""
    if os.name != "posix":
        return _result(
            "unavailable", "Browser artifact inspection requires a POSIX host."
        )
    try:
        descriptor = _open_export(root, artifact)
    except (OSError, ValueError):
        return _result(
            "unavailable",
            "Select an existing relative export directory without symlinks.",
        )
    try:
        # Source rules and pins do not authorize browser exposure.
        # Redaction and adjudication still share the credential gate.
        policy = credentials.Policy(allow_remote_model=False)
        secrets = tuple(
            s for s in credentials.builtin_secrets() if s.applies_to == "content"
        )
        candidates, coverage = _gather(descriptor, secrets, policy)
    except (OSError, ValueError):
        return _result(
            "error", "Artifact inputs or credential rules could not be read."
        )
    finally:
        os.close(descriptor)
    if not coverage["has_html"] or not coverage["has_next_static"]:
        coverage["complete"] = False
        return _result(
            "unavailable",
            "The selected directory is not a supported Next.js static export.",
            coverage=coverage,
        )
    if candidates and not credentials.endpoint_is_local(config.llm_endpoint)[0]:
        coverage["complete"] = False
        return _result(
            "unavailable",
            "Artifact candidates require a loopback judge; check 'vibe-sentinel backend status'.",
            coverage=coverage,
        )
    try:
        async with asyncio.timeout(REVIEW_TIMEOUT):
            judgements: list[credentials.Judgement] = []
            # Review each bounded batch separately. Credential review groups
            # by real file path and would otherwise reassemble a huge context.
            for candidate in candidates:
                judged = await credentials.adjudicate(
                    credentials.Scan(candidates=(candidate,), files_read=1),
                    secrets,
                    policy,
                    config,
                )
                judgements.extend(judged.judgements)
    except TimeoutError:
        coverage["complete"] = False
        return _result(
            "unavailable",
            "Browser artifact candidate review exceeded its time limit.",
            coverage=coverage,
        )
    except LLMConnectionError:
        coverage["complete"] = False
        return _result(
            "error",
            "Artifact candidate review failed; check 'vibe-sentinel backend status'.",
            coverage=coverage,
        )
    findings = []
    reported: dict[tuple[str, str], credentials.Judgement] = {}
    for judgement in judgements:
        if judgement.verdict not in {"real", "unclear"}:
            continue
        candidate = judgement.candidate
        key = (candidate.rule, candidate.path)
        previous = reported.get(key)
        if previous is None or (
            previous.verdict == "unclear" and judgement.verdict == "real"
        ):
            reported[key] = judgement
    for judgement in reported.values():
        candidate = judgement.candidate
        identity = hashlib.sha256(
            f"{candidate.rule}:{candidate.path}".encode()
        ).hexdigest()[:20]
        findings.append(
            {
                "id": f"browser-credential-{identity}",
                "message": "Credential exposure in browser-delivered artifacts."
                if judgement.verdict == "real"
                else "Unresolved credential candidate in browser-delivered artifacts.",
                "path": (Path(artifact) / candidate.path).as_posix(),
                **(
                    {"line": candidate.lines[0]}
                    if candidate.lines and Path(candidate.path).suffix.lower() != ".pdf"
                    else {}
                ),
            }
        )
    status = (
        "unavailable"
        if not coverage["complete"]
        else "findings"
        if findings
        else "complete"
    )
    summary = "Browser artifact credential review completed."
    if not coverage["complete"]:
        reasons = [
            f"{coverage[key]} {description}"
            for key, description in (
                ("unsupported_files", "file(s) have unsupported formats"),
                (
                    "pdf_text_failed",
                    "PDF file(s) could not yield supported document text",
                ),
                (
                    "oversized_candidate_contexts",
                    f"candidate context(s) exceeded {MAX_CONTEXT_CHARS} characters and remain unassessed",
                ),
                (
                    "obscured_candidate_matches",
                    "candidate match(es) obscured by private-key redaction remain unassessed",
                ),
                ("unreadable_or_unsafe", "file(s) could not be read safely"),
            )
            if coverage[key]
        ]
        if coverage["limited"]:
            reasons.append("A scan resource limit was reached")
        summary = (
            f"Read {coverage['files_read']} file(s), reviewed "
            f"{coverage['candidates']} candidate(s), found {len(findings)} finding(s). "
            "Browser artifact credential coverage is partial. "
            + ". ".join(reasons)
            + "."
        )
    return _result(
        status,
        summary,
        findings=findings,
        coverage=coverage,
    )
