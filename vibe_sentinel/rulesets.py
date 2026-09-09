"""Where a declared rule set keeps its files.

Four things a project declares — ``[[probe]]``, ``[[lens]]``,
``[[danger]]``, ``[[secret]]`` — layer over the shipped set the same way,
and past a certain size all four want the same thing: somewhere other
than ``.vibe-sentinel.toml`` to live. A team's danger set is the kind of
document a security reviewer owns, a probe set is the kind an architect
owns, and neither wants to be a section of a file the other one edits.

So each of the four takes ``rule_files``, a list of globs relative to the
project root, and this module answers the one question they share: which
files does that name, in what order, and what happens when it names none.

Sorted, always, so which of two files wins is a decision rather than the
filesystem's mood. Rule files first and the project's own config last,
so a shared set can be kept under version control and still overridden
locally by id.

A pattern matching nothing is an **error**, not an empty layer. It is the
same rule ``[probes] disable`` and the pin tables already keep: an entry
that protects nothing while looking like it protects something is worse
than no entry, because the next person reads the config rather than the
behaviour. A rule file that was renamed, or a glob with a typo in it,
otherwise silently stops checking whatever it held.

``tomllib`` is imported inside the function on purpose. ``safety.py``
reads a danger set in front of every tool call and imports this module at
module scope; in a fresh process that import costs ~4.7 ms, which a
project with the gate off should not pay for a set it never loads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def rule_file_paths(
    root: Path, patterns: Any, *, setting: str, where: Path | str
) -> list[Path]:
    """Every file the ``rule_files`` globs name, in a stable order.

    ``setting`` is the config table the list came from — ``[probes]``,
    ``[drift]``, ``[safety]``, ``[credentials]`` — and ``where`` is the
    file that declared it, so an error points at the line somebody has to
    edit rather than at this module.
    """
    if not isinstance(patterns, list):
        raise ValueError(
            f"{where}: {setting} rule_files = {patterns!r} is not a list. It "
            f"takes globs relative to the project root — rule_files = "
            f'["rules/*.toml"].'
        )

    found: list[Path] = []
    for pattern in patterns:
        matched = sorted(root.glob(str(pattern)))
        if not matched:
            raise ValueError(
                f"{where}: {setting} rule_files entry {str(pattern)!r} matches "
                f"no file under {root}. A rule file that is not there checks "
                f"for nothing, so it is an error rather than a no-op."
            )
        found.extend(matched)
    return found


#: What a rule that nobody declared says about itself.
BUILT_IN = "built-in"


def source_label(path: Path, root: Path | None = None) -> str:
    """How a rule file names itself when a listing shows where a rule came
    from: relative to the project root where it sits under one, so
    ``rules/fallbacks.toml`` rather than an absolute path that means
    nothing on another machine.
    """
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


#: The four things a project declares. A rule file has to hold at least
#: one of them; which one is not this module's business.
RULE_TABLES: tuple[str, ...] = ("probe", "lens", "danger", "secret")


def tables_in_file(path: Path, table: str) -> list[dict[str, object]]:
    """Every ``[[table]]`` in one rule file, unvalidated.

    The read is shared; what each table has to carry is not, and stays
    with the declarable that knows.

    A file holding *no* rule table of any kind is an error, for the same
    reason an unmatched glob is: somebody wrote it expecting it to check
    something, and a renamed heading or a typo otherwise stops checking
    whatever it held without saying so. Holding none of the *asked-for*
    table is fine, and deliberately so — one ``rules/fallbacks.toml`` can
    carry the ``[[probe]]`` that measures a thing and the ``[[lens]]``
    that asks about it, and each loader reads its own half. A measurement
    and the question about it are the same document to whoever wrote
    them.
    """
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"{path} is not readable as TOML: {e}") from e

    if not any(data.get(name) for name in RULE_TABLES):
        raise ValueError(
            f"{path} declares no rule tables. A rule file holds at least one "
            f"of {', '.join(f'[[{n}]]' for n in RULE_TABLES)}; one with none "
            f"in it is more likely a mistake than an intention."
        )

    tables = data.get(table, [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ValueError(
            f"{path}: [[{table}]] must be a list of tables — each entry its "
            f"own [[{table}]] heading."
        )
    return list(tables)
