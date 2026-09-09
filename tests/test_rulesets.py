"""What every declared rule set has in common.

Four things a project declares — ``[[probe]]``, ``[[lens]]``,
``[[danger]]``, ``[[secret]]`` — layer over the shipped set the same way
and keep their files the same way. "The same way" is the claim this file
holds to, because it was false twice: only the licence gate validated its
pins until :mod:`vibe_sentinel.policy` was extracted, and only the danger
and secret sets took ``rule_files`` until the probe and lens loaders got
it. Both times the asymmetry was invisible — each loader worked, and
nobody reads four loaders side by side.

So the symmetry is asserted rather than described. A fifth declarable
added without ``rule_files``, or an existing one quietly losing it, fails
here instead of being discovered by whoever writes the rule file that
does not load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel.analyze import load_lenses
from vibe_sentinel.credentials import load_secrets
from vibe_sentinel.paths import CONFIG_FILENAME
from vibe_sentinel.rulesets import RULE_TABLES, rule_file_paths, tables_in_file
from vibe_sentinel.safety import load_dangers
from vibe_sentinel.templates import load_probes

#: One file holding all four, which is the arrangement `rules/` exists for:
#: a probe and the lens that reads it are one document to whoever wrote
#: them, and a danger and the secret it is about often are too.
EVERY_KIND = """\
[[probe]]
id = "declared-probe"
title = "t"
command = ["echo"]

[[lens]]
id = "declared-lens"
question = "Did it move?"

[[danger]]
id = "declared-danger"
title = "t"
question = "Is this safe?"
pattern = 'zzz'

[[secret]]
id = "declared-secret"
title = "t"
question = "Is this live?"
pattern = 'zzz'
"""


def _project(root: Path, config: str, rules: str = EVERY_KIND) -> Path:
    (root / "rules").mkdir(exist_ok=True)
    (root / "rules" / "everything.toml").write_text(rules, encoding="utf-8")
    (root / CONFIG_FILENAME).write_text(config, encoding="utf-8")
    return root


#: Each declarable, as (config table, loader, the id its rule declares).
#: The parametrisation *is* the symmetry claim — a loader that cannot be
#: written as a row here is one that does not layer like the others.
LOADERS = [
    ("probes", lambda root: load_probes(project_root=root), "declared-probe"),
    ("drift", load_lenses, "declared-lens"),
    ("safety", load_dangers, "declared-danger"),
    ("credentials", load_secrets, "declared-secret"),
]


def _by_id(load, root: Path) -> dict[str, Any]:
    """One loader's active set, keyed by rule id."""
    return {rule.id: rule for rule in load(root)}


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_every_declarable_reads_rule_files(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """All four take the setting, and all four read the same file."""
    root = _project(tmp_path, f'[{table}]\nrule_files = ["rules/*.toml"]\n')
    assert declared_id in _by_id(load, root)


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_every_declarable_refuses_a_glob_matching_nothing(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """A renamed rule file otherwise stops checking whatever it held, while
    the config still says it is checked."""
    (tmp_path / CONFIG_FILENAME).write_text(
        f'[{table}]\nrule_files = ["nowhere/*.toml"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="matches no file"):
        load(tmp_path)


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_one_file_serves_every_declarable(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """The same file, globbed by all four, and each takes only its own half.

    This is the property that makes one `rules/` directory workable: a
    loader must not refuse a file because the tables in it belong to
    somebody else.
    """
    root = _project(tmp_path, f'[{table}]\nrule_files = ["rules/*.toml"]\n')
    found = set(_by_id(load, root))
    assert declared_id in found
    other_ids = {row[2] for row in LOADERS} - {declared_id}
    assert not (found & other_ids), "a loader took a table that is not its own"


def test_a_file_with_no_rule_tables_at_all_is_refused(tmp_path: Path) -> None:
    """Somebody wrote it expecting it to check something."""
    (tmp_path / "empty.toml").write_text("title = 'not a rule set'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="declares no rule tables"):
        tables_in_file(tmp_path / "empty.toml", "probe")


def test_a_file_holding_only_someone_elses_table_is_accepted(tmp_path: Path) -> None:
    """The other side of the same rule, and the reason it is `any` rather
    than `the one asked for`."""
    (tmp_path / "lens-only.toml").write_text(
        '[[lens]]\nid = "l"\nquestion = "q?"\n', encoding="utf-8"
    )
    assert tables_in_file(tmp_path / "lens-only.toml", "probe") == []


def test_rule_files_are_read_in_a_stable_order(tmp_path: Path) -> None:
    """Which of two files wins is a decision, not the filesystem's mood."""
    (tmp_path / "rules").mkdir()
    for name in ("zebra.toml", "alpha.toml", "middle.toml"):
        (tmp_path / "rules" / name).write_text(
            '[[lens]]\nid = "l"\nquestion = "q?"\n', encoding="utf-8"
        )
    found = rule_file_paths(
        tmp_path, ["rules/*.toml"], setting="[drift]", where="config"
    )
    assert [p.name for p in found] == ["alpha.toml", "middle.toml", "zebra.toml"]


def test_a_rule_files_value_that_is_not_a_list_says_so(tmp_path: Path) -> None:
    """The error names the shape it wanted, because the mistake is almost
    always a bare string where a list belongs."""
    with pytest.raises(ValueError, match="is not a list"):
        rule_file_paths(tmp_path, "rules/*.toml", setting="[drift]", where="config")


def test_the_declarables_are_the_four_this_project_has() -> None:
    """A fifth is a decision, and it is taken here as well as wherever it is
    loaded — this is the list every rule file is validated against."""
    assert RULE_TABLES == ("probe", "lens", "danger", "secret")
    assert {row[2] for row in LOADERS} == {f"declared-{name}" for name in RULE_TABLES}


# --- where a rule came from --------------------------------------------------
#
# The question `rule_files` created: after pointing a config at `rules/*.toml`,
# the only way to learn whether the file loaded was to make it error.


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_a_rule_says_which_file_declared_it(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """Relative to the project root, so it means the same on two machines."""
    root = _project(tmp_path, f'[{table}]\nrule_files = ["rules/*.toml"]\n')
    by_id = _by_id(load, root)
    assert by_id[declared_id].source == "rules/everything.toml"


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_a_shipped_rule_says_it_is_built_in(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """`built-in` rather than the shipped file's path: which of the two
    default TOMLs a rule sits in is this package's business, and a listing
    that repeated it forty times would bury the one line somebody is
    looking for."""
    (tmp_path / CONFIG_FILENAME).write_text("", encoding="utf-8")
    shipped = [r for r in _by_id(load, tmp_path).values() if r.id != declared_id]
    assert shipped, "every declarable ships a default set"
    assert {r.source for r in shipped} == {"built-in"}


@pytest.mark.parametrize(("table", "load", "declared_id"), LOADERS)
def test_the_file_that_won_is_the_one_named(
    tmp_path: Path, table: str, load, declared_id: str
) -> None:
    """A rule overridden by id reports the file that overrode it, not the
    one it came from first. Otherwise the listing answers "which file
    declared this name" when the question is "which one is in force"."""
    root = _project(
        tmp_path, f'[{table}]\nrule_files = ["rules/*.toml"]\n' + EVERY_KIND
    )
    assert _by_id(load, root)[declared_id].source == CONFIG_FILENAME
