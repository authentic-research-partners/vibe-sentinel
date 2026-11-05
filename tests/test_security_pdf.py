"""PDF extraction failures must leave browser coverage explicitly incomplete."""

from __future__ import annotations

import shutil
from typing import Any

import pytest

from vibe_sentinel import security_pdf


def _tool(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: bytes = b"Extracted fixture text",
    diagnostics: bytes = b"",
    status: int = 0,
) -> None:
    monkeypatch.setattr(security_pdf.shutil, "which", lambda _: "/trusted/pdftotext")

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            assert argv == ["/trusted/pdftotext", "-enc", "UTF-8", "-nopgbrk", "-", "-"]
            assert kwargs["stdin"].read() == b"%PDF-fixture"
            assert "SECRET" not in kwargs["env"]
            kwargs["stdout"].write(output)
            kwargs["stderr"].write(diagnostics)
            kwargs["stdout"].flush()
            kwargs["stderr"].flush()

        def wait(self, timeout: float | None = None) -> int:
            return status

        def poll(self) -> int:
            return status

    monkeypatch.setattr(security_pdf.subprocess, "Popen", Process)


def test_pdf_extracts_text_with_fixed_local_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET", "private")
    _tool(monkeypatch)
    assert security_pdf.extract(b"%PDF-fixture") == "Extracted fixture text"


@pytest.mark.parametrize(
    ("output", "diagnostics", "status"),
    [
        (b"", b"", 0),
        (b"   \n\f", b"", 0),
        (b"partial text", b"private PDF diagnostics", 0),
        (b"partial text", b"", 1),
        (b"\xff", b"", 0),
    ],
)
def test_incomplete_pdf_extraction_is_not_a_clean_result(
    monkeypatch: pytest.MonkeyPatch, output: bytes, diagnostics: bytes, status: int
) -> None:
    _tool(monkeypatch, output=output, diagnostics=diagnostics, status=status)
    with pytest.raises(ValueError) as exc:
        security_pdf.extract(b"%PDF-fixture")
    assert "private" not in str(exc.value)
    assert "partial" not in str(exc.value)


def test_pdf_output_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_pdf, "MAX_TEXT_BYTES", 5)
    _tool(monkeypatch, output=b"long text")
    with pytest.raises(ValueError, match="limits"):
        security_pdf.extract(b"%PDF-fixture")


def test_pdf_missing_tool_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_pdf.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="requires pdftotext"):
        security_pdf.extract(b"%PDF-fixture")


def test_pdf_timeout_stops_the_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_pdf.shutil, "which", lambda _: "/trusted/pdftotext")
    monkeypatch.setattr(security_pdf, "EXTRACT_TIMEOUT", 0)
    stopped = []

    class Process:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            stopped.append(True)

        def wait(self) -> int:
            return -9

    monkeypatch.setattr(security_pdf.subprocess, "Popen", Process)
    with pytest.raises(ValueError, match="limits"):
        security_pdf.extract(b"%PDF-fixture")
    assert stopped == [True]


def _pdf_with_text(text: str) -> bytes:
    """A small valid document so installed-tool coverage needs no PDF library."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(document)


@pytest.mark.skipif(
    shutil.which("pdftotext") is None, reason="pdftotext is not installed"
)
def test_installed_pdf_extractor() -> None:
    assert "Public fixture document" in security_pdf.extract(
        _pdf_with_text("Public fixture document")
    )
