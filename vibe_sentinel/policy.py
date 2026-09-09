"""What a gate's policy is made of, in the one place all three share.

Licences, dependency provenance and credentials at rest each carry their
own ``Policy`` — different fields, different questions. Two things about
them are not the gate's own, and both used to be written three times:
where a policy is declared, and what makes a pin a decision rather than
an ignore.

**Where it comes from.** :func:`declared_layers` gives the ordered
sources. Three gates that resolve their policy three ways is not three
decisions, it is one decision nobody made — the licence gate read the
organisation's file first and the other two read it last, and no reader
could carry one mental model across them.

**What a pin has to carry.** A pin missing its ``reason`` or its
``verified`` date is ``--ignore`` with extra steps, which is the
mechanism these gates were written to replace. A pin with a misspelled
key is worse than that: it reads as a decision and covers nothing, so the
finding it was meant to settle comes back and the next person assumes the
gate is broken. Both are errors, named against the file and the entry
that caused them — the same rule ``[probes] disable`` and ``[safety]
disable`` already keep, that a stale entry protecting nothing is an error
rather than a no-op.

Only ``licenses.py`` enforced this for a while. The other two accepted
whatever TOML handed them, which meant the guarantee their own
documentation makes — "Pins Are Not Ignores" — held in one gate out of
three.

The *lookup* is here for the same reason the validation is. All three
gates wrote their own, all three returned the first pin whose selector
matched, and all three therefore had the same silent bug: a path or a
package already named by an earlier pin could not be widened by a later
one. The second entry read as a decision and settled nothing — which is
precisely the failure :func:`check_pins` refuses when a key is
misspelled, arriving instead through a spelling that is correct.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from vibe_sentinel.paths import CONFIG_FILENAME

#: The project's own config file. A gate's table here is the normal place
#: for its policy — one config file for the whole tool.
PROJECT_CONFIG = Path(CONFIG_FILENAME)


def declared_layers(
    root: Path, *, table: str, standalone: Path
) -> list[tuple[dict[str, Any], str]]:
    """Every declared source of one gate's policy, base first.

    Two places a project can put one, and they mean different things:

      1. ``standalone`` — ``security/<gate>-policy.toml``, the
         organisation's file, shared across repositories and usually
         owned by whoever reviews this kind of thing.
      2. A ``[table]`` table in the project's ``.vibe-sentinel.toml`` —
         this repository, and so the more specific of the two.

    Returned in that order, so the last layer is always the most specific
    and an empty list means nobody declared one. What a gate does with
    them stays the gate's: the licence gate merges, so a project
    declaring one pin keeps everything the organisation's file said;
    provenance and credentials take the last, because a half-overridden
    policy is harder to read than a replaced one. That is a choice each
    gate makes visibly, in one line, rather than three resolution orders
    that were never compared.

    ``--policy PATH`` is deliberately not here. It bypasses both layers,
    and the error it raises when the path is missing names the gate's own
    fallback — which differs, because the licence gate has none.

    Each source is parsed here, so a malformed file raises
    ``tomllib.TOMLDecodeError`` naming it rather than being read as an
    absent policy. That distinction is the whole of :func:`check_pins`
    applied one level up.
    """
    layers: list[tuple[dict[str, Any], str]] = []

    shared = root / standalone
    if shared.is_file():
        layers.append((tomllib.loads(shared.read_text()), str(shared)))

    project = root / PROJECT_CONFIG
    if project.is_file():
        data = tomllib.loads(project.read_text())
        if table in data:
            layers.append((data[table], f"{project} [{table}]"))

    return layers


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


def matching(
    pins: Iterable[Any], *, subject: str, matches: Callable[[str], bool]
) -> list[Mapping[str, Any]]:
    """Every pin whose selector matches, in declaration order.

    *Every* one, deliberately. Two pins over one subject is the normal way
    a scoped pin grows rather than an accident to resolve: one accepts
    ``assigned-secret`` for a test file because of what its fixtures are,
    a later one accepts ``password-shape`` there because of what a model
    name looks like, and both are true of the file. Returning only the
    first made the second silently inert.

    ``matches`` is the gate's own comparison — ``fnmatch`` over a path,
    a normalised name for a package — because what counts as the same
    subject differs and does not belong here.
    """
    found: list[Mapping[str, Any]] = []
    for pin in pins:
        if not isinstance(pin, Mapping):
            continue
        if any(matches(str(pattern)) for pattern in pin.get(subject, ())):
            found.append(pin)
    return found


def accepts_value(pin: Mapping[str, Any], value: str) -> bool:
    """Whether one pin's ``accept`` list covers ``value``.

    Case-insensitive, and ``*`` accepts everything — which is scoped to
    the subjects the pin names and is therefore still not an ignore. The
    licence gate does not use this: an SPDX expression needs canonical
    comparison, not a lowercased string.
    """
    accepted = {str(a).lower() for a in pin.get("accept", ())}
    return value.lower() in accepted or "*" in accepted
