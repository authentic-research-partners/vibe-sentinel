"""Bounded text extraction from explicitly selected browser PDF artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
EXTRACT_TIMEOUT = 15.0


def extract(body: bytes) -> str:
    """Read PDF text locally, without exposing tool output or executing PDF code."""
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ValueError("PDF text extraction requires pdftotext on PATH.")
    if not body or len(body) > MAX_INPUT_BYTES:
        raise ValueError("PDF input exceeds extraction limits.")
    with (
        tempfile.TemporaryDirectory(prefix="sentinel-pdf-") as directory,
        tempfile.TemporaryFile() as source,
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as diagnostics,
    ):
        source.write(body)
        source.seek(0)
        env = {"PATH": os.defpath, "HOME": directory, "LANG": "C"}
        process = subprocess.Popen(
            [executable, "-enc", "UTF-8", "-nopgbrk", "-", "-"],
            stdin=source,
            stdout=output,
            stderr=diagnostics,
            cwd=Path(directory),
            env=env,
        )
        deadline = time.monotonic() + EXTRACT_TIMEOUT
        try:
            while True:
                if (
                    time.monotonic() >= deadline
                    or os.fstat(output.fileno()).st_size > MAX_TEXT_BYTES
                    or os.fstat(diagnostics.fileno()).st_size > MAX_TEXT_BYTES
                ):
                    raise ValueError("PDF text extraction exceeded its limits.")
                try:
                    status = process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        output.seek(0)
        text = output.read(MAX_TEXT_BYTES + 1)
        if (
            status != 0
            or os.fstat(diagnostics.fileno()).st_size
            or len(text) > MAX_TEXT_BYTES
        ):
            raise ValueError("PDF text extraction did not complete.")
        try:
            decoded = text.decode("utf-8")
        except UnicodeError:
            raise ValueError("PDF text extraction returned invalid text.") from None
        if not decoded.strip():
            raise ValueError(
                "PDF has no extractable text; image content is unassessed."
            )
        return decoded
