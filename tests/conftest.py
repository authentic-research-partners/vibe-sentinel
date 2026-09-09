"""Shared pytest configuration.

``basetemp`` is made PID-unique so concurrent pytest processes (``-n
auto`` plus a second run in another shell) don't collide on the same
temp root and raise FileExistsError.

``stub_model`` is autouse and answers every model call with a fixed,
uninteresting verdict. It exists because the model is not optional: a
scan that cannot reach one fails, so without a stub the whole suite
would need a GPU — and the four mandatory checks are the ones that must
run anywhere.

It is deliberately the *dullest* answer each schema allows, so a test
asserting on a verdict has to say so itself by overriding this. A stub
that answered interestingly would make tests pass on the strength of
the fixture rather than the code.

There used to be a ``sample_rule_toml`` fixture describing a
``[[rules]]`` config with ``severity`` and ``description`` fields — a
shape this project has not had for a long time. Nothing used it, so
nothing failed when the config model moved on, and it sat here
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


#: What the stub answers, per schema name prefix. Each is the least
#: interesting answer its schema allows: a credential candidate cannot be
#: told either way, two package names are distinct, a command is safe. A
#: test that cares about a verdict says so by patching over this — a stub
#: that answered interestingly would let tests pass on the fixture rather
#: than on the code.
#:
#: ``unclear`` rather than ``placeholder`` for a credential: it is what an
#: honest model says about a string it was told nothing about, and it is
#: what a gate test asserting "this is reported every run" needs to see.
_DULL: dict[str, dict[str, object]] = {
    "credential": {"verdict": "unclear", "reason": "stubbed in tests"},
    "near-miss": {"verdict": "distinct", "reason": "stubbed in tests"},
    "safety": {"verdict": "safe", "reason": "stubbed in tests"},
}


@pytest.fixture(autouse=True)
def stub_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every model call, so no test needs a backend running."""

    from vibe_sentinel import llm as llm_mod

    real = llm_mod.llm_query

    async def answer(
        system: object,
        user: object,
        schema: object,
        schema_name: str,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object] | None:
        for prefix, reply in _DULL.items():
            if schema_name.startswith(prefix):
                return dict(reply)
        # Anything else — a licence draft with its own MockTransport, say —
        # goes through untouched. A stub that swallowed those would make a
        # test pass without exercising the request it was written for.
        return await real(system, user, schema, schema_name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm_mod, "llm_query", answer)
