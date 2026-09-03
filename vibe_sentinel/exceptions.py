"""Typed exception hierarchy for Python-API consumers.

CLI consumers rely on 0/1/2 exit codes (0 = clean, 1 = violations,
2 = tool error). Python-API consumers rely on ``except VibeSentinelError``.
Keep the hierarchy narrow — only add typed subclasses when a specific
raise site justifies it.

The base class lives here; two subclasses do not, because they belong
with the code that raises them and importing them costs nothing extra:
:class:`vibe_sentinel.db.connection.SchemaMismatchError` and
:class:`vibe_sentinel.db.migration.MigrationError`. Both are re-exported
from the package root, so ``except VibeSentinelError`` has to reach them
— it did not for a while, and the two it missed were the likeliest
failures a caller sees.

This module imports nothing, deliberately: ``db.connection`` is in the
PreToolUse hook's import graph, and a base class that pulled in pydantic
would be paid once per tool call.
"""

from __future__ import annotations


class VibeSentinelError(Exception):
    """Base for all vibe-sentinel domain errors."""


class LLMConnectionError(VibeSentinelError):
    """Judge model unreachable — at startup, during batch, or mid-request."""
