"""``--help`` is the interface, and nothing else fails when it rots.

Every other surface here has something that notices when it goes wrong: a
probe that stops parsing fails a scan, a schema that drifts refuses to
open the database. Help text has nothing. An option shipped without
``help=`` renders as a bare ``--limit LIMIT`` and the tests still pass; a
subcommand declared with only ``help=`` gets a ``--help`` of its own that
lists every flag and never says what the command does; and a help string
describing behaviour the handler stopped having reads exactly like one
that is still true. All three had happened by the time this file was
written — five options with no help at all, fourteen commands with no
description, and ``parameters`` still claiming a model chose the values.

So the rule is mechanical rather than remembered: walk the parser the CLI
actually builds and refuse each of those shapes. A new subcommand is
declared through :func:`vibe_sentinel.__main__._command`, which is what
puts the description there; a new option carries ``help=``. Neither is a
style preference — they are the difference between a CLI a stranger can
read and one they have to read the source of.

These assert the *presence and shape* of help, never its wording. Whether
a sentence is still true of the handler is what ``test_cli.py`` is for.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator

import pytest

from vibe_sentinel.__main__ import _build_parser

# argparse adds these itself, and their help is argparse's to write.
GENERATED = {"help", "version"}


def _walk(
    parser: argparse.ArgumentParser, path: str
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    """Every parser in the tree, with the command line that reaches it."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _walk(sub, f"{path} {name}")


def _parsers() -> list[tuple[str, argparse.ArgumentParser]]:
    return list(_walk(_build_parser(), "vibe-sentinel"))


def _ids(pairs: list[tuple[str, argparse.ArgumentParser]]) -> list[str]:
    return [path for path, _ in pairs]


PARSERS = _parsers()


@pytest.mark.parametrize("path,parser", PARSERS, ids=_ids(PARSERS))
def test_every_command_says_what_it_is_for(
    path: str, parser: argparse.ArgumentParser
) -> None:
    """``vibe-sentinel <cmd> --help`` opens with a sentence, not a usage line.

    argparse fills in neither ``help`` nor ``description`` from the other,
    so this is the assertion that ``_command`` exists to satisfy.
    """
    assert parser.description, f"{path} --help says nothing about {path}"


@pytest.mark.parametrize("path,parser", PARSERS, ids=_ids(PARSERS))
def test_every_option_carries_help(path: str, parser: argparse.ArgumentParser) -> None:
    """Options and positionals alike — a bare ``--limit LIMIT`` tells a
    reader the flag's type and nothing about what it limits.

    Two words minimum: ``help="limit"`` is the same gap with a string in
    it. The subcommand slot is exempt because argparse renders it as the
    list of subcommands, each carrying its own help.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.dest in GENERATED:
            continue
        name = ", ".join(action.option_strings) or action.dest
        help_text = (action.help or "").strip()
        assert help_text, f"{path} {name} has no help"
        assert len(help_text.split()) >= 2, f"{path} {name} help is not a phrase"


@pytest.mark.parametrize("path,parser", PARSERS, ids=_ids(PARSERS))
def test_every_subcommand_is_listed_by_its_parent(
    path: str, parser: argparse.ArgumentParser
) -> None:
    """A subcommand without ``help`` is absent from the parent's listing —
    reachable, documented, and impossible to discover."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        listed = {choice.dest for choice in action._choices_actions if choice.help}
        for name in action.choices:
            assert name in listed, f"{path} {name} is not listed by {path} --help"


@pytest.mark.parametrize("path,parser", PARSERS, ids=_ids(PARSERS))
def test_every_help_renders(path: str, parser: argparse.ArgumentParser) -> None:
    """Formatting a help page can raise on its own — a ``%`` in a help
    string, a default that will not interpolate — and only rendering it
    finds out."""
    rendered = parser.format_help()
    assert parser.description
    assert parser.description.split()[0] in rendered
