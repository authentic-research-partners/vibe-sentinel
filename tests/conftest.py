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
import sys
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
#:
#: A lens rates nothing, which is the dullest answer that schema allows and
#: leaves every mechanical severity standing. It is keyed on the prefix
#: ``analyze-drift`` because that is what ``analyze.py`` builds the name
#: from — ``analyze-drift-{lens.id}`` — so one entry covers every lens a
#: project declares rather than the shipped one only. Without it these
#: calls fell through to the real ``llm_query`` and twenty tests in
#: ``test_engine.py`` and ``test_cli.py`` reached the network, which passed
#: on the maintainer's box and failed everywhere else — the suite claiming
#: in its own docstring to need no backend.
_DULL: dict[str, dict[str, object]] = {
    "credential": {"verdict": "unclear", "reason": "stubbed in tests"},
    "near-miss": {"verdict": "distinct", "reason": "stubbed in tests"},
    "safety": {"verdict": "safe", "reason": "stubbed in tests"},
    "analyze-drift": {"assessment": "", "changes": []},
}


@pytest.fixture(autouse=True)
def system_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Where this test's system locations are: the system policy, Claude
    Code's managed hook file, Cursor's enterprise hooks.

    Never the real ones. A test that read this machine's system policy would
    pass or fail by what an administrator wrote, and one that installed a
    managed hook would install it for every session on the machine. A test
    that needs a system policy writes one here; nothing is here otherwise.
    """
    from vibe_sentinel import paths

    root = tmp_path_factory.mktemp("system")
    monkeypatch.setattr(
        paths, "system_policy_path", lambda: root / "vibe-sentinel" / "policy.toml"
    )
    monkeypatch.setattr(
        paths,
        "claude_managed_hook_path",
        lambda: root / "ClaudeCode" / "managed-settings.d" / "vibe-sentinel.json",
    )
    monkeypatch.setattr(
        paths, "cursor_enterprise_hooks_path", lambda: root / "Cursor" / "hooks.json"
    )
    return root


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
        # A caller that brought its own transport — a licence draft with a
        # MockTransport, say — goes through untouched. A stub that swallowed
        # those would make a test pass without exercising the request it was
        # written for, and the transport means it reaches no network.
        if kwargs.get("client") is not None or len(args) > 1:
            return await real(system, user, schema, schema_name, *args, **kwargs)  # type: ignore[arg-type]

        # Anything else would open a socket. Say so here rather than there:
        # an uncovered schema name used to fall through to a live backend,
        # which passed on the maintainer's box and failed everywhere else
        # with a connection error naming the endpoint instead of the gap.
        raise AssertionError(
            f"no stubbed answer for schema {schema_name!r}, and the caller "
            "supplied no client — this test would reach a real backend.\n"
            "Add the dullest answer its schema allows to _DULL in "
            "tests/conftest.py, keyed on the prefix that names it, or pass "
            "a client=httpx.AsyncClient(transport=httpx.MockTransport(...)) "
            "if the request itself is what the test is about."
        )

    monkeypatch.setattr(llm_mod, "llm_query", answer)

    # ``analyze.py`` binds ``llm_query`` at module import; the other four
    # callers import it inside the function that asks. Patching the ``llm``
    # module alone therefore reaches the deferred callers and misses the
    # bound one, which is how twenty tests came to need a backend. Patch
    # every name already bound to the real function, so a module that binds
    # it at import tomorrow is covered without anyone remembering to add it
    # here.
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if not name.startswith("vibe_sentinel.") or module is llm_mod:
            continue
        if getattr(module, "llm_query", None) is real:
            monkeypatch.setattr(module, "llm_query", answer)
