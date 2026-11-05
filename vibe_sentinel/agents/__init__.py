"""One adapter per agent: its hook payload in, its reply out.

The engine — :mod:`vibe_sentinel.journal`'s ``ActionRequest`` and
``Decision``, the journal, :mod:`vibe_sentinel.safety` — knows no agent's
field names. Each module here knows exactly one agent's, and offers three
things:

- ``NAME``, the agent's name as ``vibe-sentinel hook --agent`` takes it;
- ``read(raw)``, one stdin payload as an ``ActionRequest``, or None;
- ``reply(decision, request)``, the JSON that agent acts on, or None to
  print nothing.

``tests/test_layers.py`` holds the boundary: nothing in the engine imports
from here. Adapters load lazily, one per process, because the hook runs in
front of every tool call.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

#: ``--agent`` value -> the module that reads it.
ADAPTERS: dict[str, str] = {
    "claude-code": "vibe_sentinel.agents.claude_code",
    "cursor": "vibe_sentinel.agents.cursor",
    "codex": "vibe_sentinel.agents.codex",
}


def adapter(name: str) -> ModuleType:
    """The adapter for ``name``; ``KeyError`` names the ones that exist."""
    if name not in ADAPTERS:
        raise KeyError(f"no adapter for {name!r}; there is one for {sorted(ADAPTERS)}")
    return import_module(ADAPTERS[name])
