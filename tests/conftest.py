"""Shared pytest configuration.

``basetemp`` is made PID-unique so concurrent pytest processes (``-n
auto`` plus a second run in another shell) don't collide on the same
temp root and raise FileExistsError.

Nothing else lives here. There used to be a ``sample_rule_toml`` fixture
describing a ``[[rules]]`` config with ``severity`` and ``description``
fields — a shape this project has not had for a long time. Nothing used
it, so nothing failed when the config model moved on, and it sat here
documenting a schema that would not load.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp is None:
        base = Path(tempfile.gettempdir()) / f"pytest-vibe-sentinel-{os.getpid()}"
        config.option.basetemp = str(base)
