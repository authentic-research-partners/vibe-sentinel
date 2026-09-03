"""Probe templates: loading, placeholder consistency, and safe filling."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_sentinel.templates import (
    Placeholder,
    Probe,
    default_probes_path,
    load_probe_settings,
    load_probes,
    load_probes_from_toml,
    packaged_example_path,
    select_probes,
)

#: The built-in probe set. Pinned deliberately: a probe silently vanishing
#: from the defaults is worth a failing test.
#:
#: Five, not eight. Licences, provenance and credentials are gates rather
#: than probes — their placeholders were a {PROJECT_ROOT} that was always
#: ".", which is a config table's answer and not a question for a model,
#: and what they report is a state rather than drift. See
#: :mod:`vibe_sentinel.gates`.
SHIPPED_PROBES = {
    "commentary-ratio",
    "module-organization",
    "silent-exceptions",
    "pattern-census",
    "file-length",
}


def _probe(**kw) -> Probe:
    base = {
        "id": "p",
        "title": "t",
        "command": ["echo", "{NAME}"],
        "placeholders": [Placeholder(name="NAME", description="a name")],
    }
    return Probe(**{**base, **kw})


def test_default_probes_load() -> None:
    probes = load_probes_from_toml(default_probes_path())
    assert {p.id for p in probes} == SHIPPED_PROBES


def test_scaffold_declares_no_probes() -> None:
    """The scaffold is a template, not a copy of the defaults. Carrying the
    built-in probes in it made a 348-line starter file for a tool whose
    minimum viable config is two lines."""
    assert load_probes_from_toml(packaged_example_path()) == []


def test_scaffold_still_carries_a_usable_licence_policy() -> None:
    """Short, but not empty: [licenses] is the one required block."""
    import tomllib

    data = tomllib.loads(packaged_example_path().read_text(encoding="utf-8"))
    assert data["licenses"]["allowed_categories"]


def test_default_probe_placeholders_are_consistent() -> None:
    """Every built-in probe must survive its own consistency check."""
    for probe in load_probes_from_toml(default_probes_path()):
        probe.check_consistent()


def test_placeholder_names_found_in_order() -> None:
    probe = _probe(
        command=["run", "--a", "{ALPHA}", "--b", "{BETA}", "--a2", "{ALPHA}"],
        placeholders=[
            Placeholder(name="ALPHA", description="a"),
            Placeholder(name="BETA", description="b"),
        ],
    )
    assert probe.placeholder_names() == ["ALPHA", "BETA"]


def test_undeclared_placeholder_is_rejected() -> None:
    probe = _probe(command=["echo", "{NAME}", "{EXTRA}"])
    with pytest.raises(ValueError, match="undeclared placeholder"):
        probe.check_consistent()


def test_declared_but_unused_placeholder_is_rejected() -> None:
    probe = _probe(
        command=["echo", "hello"],
        placeholders=[Placeholder(name="NAME", description="a name")],
    )
    with pytest.raises(ValueError, match="never uses"):
        probe.check_consistent()


def test_fill_substitutes_into_argv() -> None:
    assert _probe().fill({"NAME": "src"}) == ["echo", "src"]


def test_fill_keeps_a_spaced_value_as_one_argument() -> None:
    """Substitution is per argv element, so a value with a space must not
    split into two arguments."""
    filled = _probe().fill({"NAME": "my dir"})
    assert filled == ["echo", "my dir"]


def test_fill_rejects_shell_metacharacters() -> None:
    """The default pattern must not admit anything that would be
    dangerous if the argv discipline were ever bypassed."""
    with pytest.raises(ValueError, match="does not match"):
        _probe().fill({"NAME": "src; rm -rf /"})


def test_fill_rejects_command_substitution() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _probe().fill({"NAME": "$(whoami)"})


def test_fill_reports_a_missing_value() -> None:
    with pytest.raises(ValueError, match="missing value"):
        _probe().fill({})


def test_defaults_are_collected() -> None:
    probe = _probe(
        command=["echo", "{NAME}", "{OTHER}"],
        placeholders=[
            Placeholder(name="NAME", description="a", default="x"),
            Placeholder(name="OTHER", description="b"),
        ],
    )
    assert probe.defaults() == {"NAME": "x"}


def test_custom_pattern_is_enforced() -> None:
    probe = _probe(
        placeholders=[
            Placeholder(name="NAME", description="lowercase only", pattern="^[a-z]+$")
        ]
    )
    assert probe.fill({"NAME": "python"}) == ["echo", "python"]
    with pytest.raises(ValueError, match="does not match"):
        probe.fill({"NAME": "Python3"})


def test_duplicate_probe_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dupes.toml"
    path.write_text(
        '[[probe]]\nid = "a"\ntitle = "t"\ncommand = ["echo"]\n'
        '[[probe]]\nid = "a"\ntitle = "t2"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="twice"):
        load_probes_from_toml(path)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[[probe]\nid = ", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid TOML"):
        load_probes_from_toml(path)


def test_missing_config_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_probes_from_toml(tmp_path / "absent.toml")


def test_later_config_wins_on_id_collision(tmp_path: Path) -> None:
    first = tmp_path / "company.toml"
    first.write_text(
        '[[probe]]\nid = "shared"\ntitle = "company"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    second = tmp_path / "project.toml"
    second.write_text(
        '[[probe]]\nid = "shared"\ntitle = "project"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    probes = {p.id: p for p in load_probes([first, second])}
    assert probes["shared"].title == "project"
    # The built-ins are the base layer, so they come along too.
    assert SHIPPED_PROBES <= set(probes)


def test_no_config_falls_back_to_the_built_in_probes(tmp_path: Path) -> None:
    probes = load_probes(project_root=tmp_path)
    assert {p.id for p in probes} == SHIPPED_PROBES


def test_config_without_probes_still_gets_the_built_in_set(tmp_path: Path) -> None:
    """What lets the scaffold stay short: a config that sets licences and
    nothing else still runs the full probe set."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[licenses]\nallowed_categories = ["permissive"]\n', encoding="utf-8"
    )
    assert {p.id for p in load_probes(project_root=tmp_path)} == SHIPPED_PROBES


def test_declaring_a_probe_adds_to_the_built_in_set(tmp_path: Path) -> None:
    """Adding a check must not cost you the checks you already had. This
    used to replace the built-ins silently, so a user who added one probe
    lost five and nothing said so."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[[probe]]\nid = "mine"\ntitle = "t"\ncommand = ["echo"]\n', encoding="utf-8"
    )
    ids = {p.id for p in load_probes(project_root=tmp_path)}
    assert ids == SHIPPED_PROBES | {"mine"}


def test_reusing_a_built_in_id_overrides_it(tmp_path: Path) -> None:
    """Same id means "I want this one, my way" — not a duplicate."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[[probe]]\nid = "commentary-ratio"\ntitle = "mine"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    probes = {p.id: p for p in load_probes(project_root=tmp_path)}
    assert set(probes) == SHIPPED_PROBES
    assert probes["commentary-ratio"].title == "mine"


def test_disable_removes_one_without_copying_the_rest(tmp_path: Path) -> None:
    """The point of [probes] disable: switching one off must not require
    restating the five you are keeping."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[probes]\ndisable = ["pattern-census"]\n', encoding="utf-8"
    )
    ids = {p.id for p in load_probes(project_root=tmp_path)}
    assert ids == SHIPPED_PROBES - {"pattern-census"}


def test_disable_also_applies_to_your_own_probes(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[probes]\ndisable = ["mine"]\n\n'
        '[[probe]]\nid = "mine"\ntitle = "t"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    assert {p.id for p in load_probes(project_root=tmp_path)} == SHIPPED_PROBES


def test_use_builtins_false_starts_from_nothing(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "mine"\ntitle = "t"\ncommand = ["echo"]\n',
        encoding="utf-8",
    )
    assert [p.id for p in load_probes(project_root=tmp_path)] == ["mine"]


def test_disabling_an_unknown_probe_is_an_error(tmp_path: Path) -> None:
    """A stale disable entry silently protects nothing — usually a typo, or
    a probe renamed upstream. Either is worth knowing about."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[probes]\ndisable = ["patern-census"]\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="do not exist"):
        load_probes(project_root=tmp_path)


def test_invalid_probes_table_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text('[probes]\nuse_builtins = "yes please"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid \\[probes\\] table"):
        load_probe_settings(path)


def test_select_probes_filters_and_reports_unknown() -> None:
    pool = [_probe(id="a"), _probe(id="b")]
    assert [p.id for p in select_probes(["a"], pool)] == ["a"]
    assert len(select_probes(None, pool)) == 2
    with pytest.raises(ValueError) as exc:
        select_probes(["nope"], pool)
    assert "nope" in str(exc.value)
    assert "a, b" in str(exc.value)


# --- [probes.parameters] ---------------------------------------------------


def _config(root: Path, body: str) -> Path:
    path = root / ".vibe-sentinel.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_parameters_change_a_value_without_restating_the_probe(
    tmp_path: Path,
) -> None:
    """The light override. Redeclaring the probe replaces it wholesale,
    which means copying a command that then stops improving."""
    _config(
        tmp_path,
        '[probes.parameters]\ncommentary-ratio = { SOURCE_ROOT = "src" }\n',
    )
    probes = {p.id: p for p in load_probes(project_root=tmp_path)}
    assert probes["commentary-ratio"].defaults()["SOURCE_ROOT"] == "src"
    # Everything else is still the built-in's.
    assert probes["commentary-ratio"].defaults()["FILE_GLOB"] == "*.py"
    assert probes["commentary-ratio"].command == (
        load_probes_from_toml(default_probes_path())[0].command
    )


def test_a_parameter_for_an_unknown_probe_is_an_error(tmp_path: Path) -> None:
    """A stale entry that looks like a decision and changes nothing is the
    failure `[probes] disable` and the pin tables already refuse."""
    _config(tmp_path, '[probes.parameters]\nno-such-probe = { X = "1" }\n')
    with pytest.raises(ValueError, match="unknown probe"):
        load_probes(project_root=tmp_path)


def test_a_parameter_for_an_undeclared_placeholder_is_an_error(
    tmp_path: Path,
) -> None:
    _config(
        tmp_path,
        '[probes.parameters]\ncommentary-ratio = { NOT_A_PLACEHOLDER = "x" }\n',
    )
    with pytest.raises(ValueError, match="declares no such placeholder"):
        load_probes(project_root=tmp_path)


def test_a_parameter_is_validated_against_its_pattern_at_load(
    tmp_path: Path,
) -> None:
    """Named against the config line, not against a probe that failed in
    the middle of a scan."""
    _config(
        tmp_path,
        '[probes.parameters]\npattern-census = { LANGUAGE = "Python 3; rm -rf /" }\n',
    )
    with pytest.raises(ValueError, match="does not match its declared pattern"):
        load_probes(project_root=tmp_path)


def test_a_placeholder_with_no_default_is_refused_at_load(tmp_path: Path) -> None:
    """Nothing fills a blank, so a probe missing one can never run."""
    _config(
        tmp_path,
        '[[probe]]\nid = "p"\ntitle = "t"\ncommand = ["echo", "{X}"]\n\n'
        '[[probe.placeholders]]\nname = "X"\ndescription = "a thing"\n',
    )
    with pytest.raises(ValueError, match="no value"):
        load_probes(project_root=tmp_path)


def test_a_percentage_tolerance_is_relative_and_a_bare_number_is_not(
    tmp_path: Path,
) -> None:
    """Two forms, one field: there is no mode to set separately and
    therefore no way to declare a threshold whose units are a guess."""
    _config(
        tmp_path,
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "ratio"\ntitle = "t"\ncommand = ["echo"]\n'
        "tolerance = 0.05\n\n"
        '[[probe]]\nid = "size"\ntitle = "t"\ncommand = ["echo"]\n'
        'tolerance = "15%"\n',
    )
    probes = {p.id: p for p in load_probes(project_root=tmp_path)}

    assert probes["ratio"].moved(0.20, 0.26)
    assert not probes["ratio"].moved(0.20, 0.24)
    # The same absolute move, judged against what it moved from.
    assert probes["size"].moved(40.0, 60.0)
    assert not probes["size"].moved(1700.0, 1720.0)


def test_a_tolerance_that_is_neither_a_number_nor_a_percentage_is_refused(
    tmp_path: Path,
) -> None:
    """A string that is not a percentage has no units anyone can name,
    and the message has to carry both forms rather than the type."""
    _config(
        tmp_path,
        '[[probe]]\nid = "p"\ntitle = "t"\ncommand = ["echo"]\ntolerance = "25"\n',
    )
    with pytest.raises(ValueError, match="neither a number nor a percentage"):
        load_probes(project_root=tmp_path)


def test_a_negative_tolerance_is_refused(tmp_path: Path) -> None:
    """It would report every value as drift, which is what 0 already says
    and says on purpose."""
    _config(
        tmp_path,
        '[[probe]]\nid = "p"\ntitle = "t"\ncommand = ["echo"]\ntolerance = -1.0\n',
    )
    with pytest.raises(ValueError, match="negative"):
        load_probes(project_root=tmp_path)
