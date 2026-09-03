"""What makes a pin a decision rather than an ignore.

Three gates here can be told "yes, I know about that one, and it is fine":
licences, dependency provenance, and credentials at rest. All three record
that answer the same way — a table naming what it covers, what it accepts,
why, and when somebody last checked — because the shape *is* the argument
for having pins at all. So the shape is checked in one place rather than
three.

A pin missing its ``reason`` or its ``verified`` date is ``--ignore`` with
extra steps, which is the mechanism these gates were written to replace. A
pin with a misspelled key is worse than that: it reads as a decision and
covers nothing, so the finding it was meant to settle comes back and the
next person assumes the gate is broken. Both are errors, named against the
file and the entry that caused them — the same rule ``[probes] disable``
and ``[safety] disable`` already keep, that a stale entry protecting
nothing is an error rather than a no-op.

Only ``licenses.py`` enforced this for a while. The other two accepted
whatever TOML handed them, which meant the guarantee their own
documentation makes — "Pins Are Not Ignores" — held in one gate out of
three.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Every pin carries these three, whatever it is pinning. The fourth is the
#: selector, and it is the only part that differs between gates: ``packages``
#: for a licence or provenance pin, ``paths`` for a credential one.
_ALWAYS: tuple[str, ...] = ("accept", "reason", "verified")


def required_keys(subject: str) -> tuple[str, ...]:
    """The keys a pin for ``subject`` must carry, selector first."""
    return (subject, *_ALWAYS)


def check_pins(pins: Iterable[Any], *, subject: str, where: str) -> None:
    """Validate one policy's pins, or raise ``ValueError`` naming the entry.

    ``subject`` is the selector key this gate's pins use — ``packages`` or
    ``paths``. ``where`` names the file, so the error points at the line
    somebody has to edit rather than at this module.
    """
    required = required_keys(subject)
    known = frozenset(required)
    for index, pin in enumerate(pins, start=1):
        if not isinstance(pin, Mapping):
            raise ValueError(
                f"{where}: pin #{index} is not a table. Each one is its own "
                f"[[...pin]] entry carrying {', '.join(required)}."
            )
        unknown = sorted(str(k) for k in pin if str(k) not in known)
        if unknown:
            raise ValueError(
                f"{where}: pin #{index} has unknown key(s): {', '.join(unknown)}. "
                f"Known: {', '.join(required)}. A misspelled key does not fail "
                f"loudly on its own — it makes a pin that looks like a decision "
                f"and covers nothing."
            )
        missing = [key for key in required if not pin.get(key)]
        if missing:
            named = pin.get(subject) or "<unnamed>"
            raise ValueError(
                f"{where}: the pin for {named} is missing {', '.join(missing)}. "
                f"A pin records something somebody read and accepted — without a "
                f"reason and a date it is an ignore, which these gates do not "
                f"have."
            )
