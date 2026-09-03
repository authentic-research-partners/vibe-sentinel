"""The project's logger, imported only if something is actually logged.

``from loguru import logger`` costs ~38 ms in a fresh process, and costs
it whether or not a line is ever written — an unused import measures the
same as a used one. That is nothing for a scan. It was the largest single
item left in a ``vibe-sentinel hook`` invocation, which runs once per
tool call and, on the path where it succeeds, logs nothing at all: every
call site in :mod:`vibe_sentinel.hook` and
:mod:`vibe_sentinel.db.connection` is a failure branch or a first-run
branch. Deferring it took the hook from ~90 ms to ~52 ms — less than the
cost of importing loguru and nothing else.

**Why not stdlib ``logging``, which imports in ~3 ms.** It saves less
than deferring loguru does (35 ms against 38 ms), and it buys a second
logging system to pay for it: different levels, different formatting,
different configuration, and an intercept shim to get it back into
loguru's sinks for everyone else. A slower answer that costs a whole
second mechanism is not the cheaper one. ``logging`` stays banned.

So the import is deferred rather than replaced. ``logger`` here is a
proxy that resolves to loguru's real logger on first attribute access:
call sites read exactly as they do everywhere else, the file sink and
format are unchanged, and the ~38 ms is paid only by a process that
actually logs.

Modules outside the hook's import graph can keep using
``from loguru import logger`` directly — it is the same object, and they
are not the ones paying per tool call. ``test_hook.py`` asserts that
recording a command imports no logger at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loguru import Logger


class _DeferredLogger:
    """Resolves to loguru's logger on first attribute access.

    Each lookup returns the real bound method, which is then called from
    the caller's own frame — so loguru's frame inspection still reports
    the right module, function and line. The proxy is never on the stack
    when a record is made.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        from loguru import logger as _logger

        return getattr(_logger, name)


if TYPE_CHECKING:
    logger: Logger
else:
    logger = _DeferredLogger()
