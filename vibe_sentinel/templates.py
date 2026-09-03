"""Probe templates — the scripts Sentinel runs, and the values they take.

A template is a command with ``{PLACEHOLDER}`` tokens and a declaration
of what each placeholder means and what it is set to. Every one carries a
``default``; that is the value, and a blank one is refused when the
config loads, because nothing fills it.

Why placeholders instead of fixed commands: the interesting structural
questions are project-specific. Which directory holds the data layer,
which packages are meant to stay small, which construct should stay in
one layer — a shipped script cannot know, and hard-coding it into every
company's config is what stops structural tooling getting adopted. So a
probe is parameterised, and the project supplies the vocabulary.

The project, and not a model. The model used to fill these by reading the
repository layout, which sounds adaptive and is the opposite: it answered
``SOURCE_ROOT`` four different ways across nineteen scans of a tree that
never changed, and every change of mind re-keys every observation, so the
next comparison reports a reorganisation that did not happen. For
``PATTERN`` it is worse than noise — the pattern is not part of the
observation key, so two runs asking about different constructs compare
their counts under one key and report the difference as growth. A value
that varies per run cannot be the axis of a comparison across runs.

Three ways to set one, in the order you should reach for them:

  - ``[probes.parameters]`` — ``{probe_id: {NAME: value}}``. Changes a
    value and nothing else, so the built-in command keeps improving.
  - a ``[[probe]]`` table reusing a built-in id — replaces it whole.
    Use when the command itself should differ.
  - a ``[[probe]]`` table with a new id — a probe of your own.

Safety: a value is never interpolated into a shell string. The command is
a fixed argv list, placeholders substitute into individual argv elements,
every value is checked against the placeholder's declared ``pattern``,
and execution uses ``shell=False``. That boundary is about what can reach
a command line, not about who proposed the value, so it is unchanged by
the value now coming from a config file: ``; rm -rf /`` in a TOML table
is a load-time error, not a shell metacharacter.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from vibe_sentinel.paths import CONFIG_FILENAME

#: Conservative default: paths, globs, module names, simple numbers —
#: anything a probe legitimately needs, and nothing that would survive as
#: a shell metacharacter even if the argv discipline were bypassed.
DEFAULT_PATTERN = r"^[\w./*@ -]{1,200}$"

_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")

_PERCENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")


def percent(spec: str) -> float:
    """``"15%"`` → ``0.15``. The only string form a tolerance takes."""
    match = _PERCENT_RE.match(spec.strip())
    if match is None:
        raise ValueError(
            f"tolerance {spec!r} is neither a number nor a percentage. Write "
            f"a bare number for a threshold in the value's own units "
            f"(tolerance = 0.05), or a percentage for one relative to the "
            f'value it is compared against (tolerance = "15%").'
        )
    return float(match.group(1)) / 100


class Placeholder(BaseModel):
    """One named parameter of a probe, and the value it takes."""

    name: str
    description: str
    """What this parameter is, for whoever edits the config. Name the
    kind of answer wanted — "the package directory holding database
    access code" beats "the db dir"."""
    pattern: str = DEFAULT_PATTERN
    """Checked against the value before it reaches an argv. The value now
    comes from a config file rather than from a model, which changes
    nothing here: this guards what can reach a command line, not who
    proposed it."""
    default: str | None = None
    """The value. Required — nothing fills a blank one. It is called
    ``default`` because a project overrides it by redeclaring the probe,
    which is the only way a value is chosen."""

    def validate_value(self, value: str) -> str:
        """Return ``value`` if it satisfies ``pattern``, else raise."""
        if re.match(self.pattern, value) is None:
            raise ValueError(
                f"Value {value!r} for placeholder {{{self.name}}} does not "
                f"match its declared pattern {self.pattern!r}. Probe not run."
            )
        return value


class Probe(BaseModel):
    """One templated script plus the blanks it needs filled."""

    id: str
    title: str
    description: str = ""
    """What structural question this probe answers. Documentation for
    whoever reads `scan --list-probes`, and for whoever has to decide
    whether a change it reported matters."""
    command: list[str] = Field(default_factory=list)
    placeholders: list[Placeholder] = Field(default_factory=list)
    timeout_s: float = 120.0
    tolerance: float | str = 0.0
    """How far an observation's value may move between runs before it
    counts as drift, in one of two forms.

    A bare number is **absolute**, in the value's own units. That is what
    a value which is already normalised wants: five points of a comment
    ratio mean the same thing wherever the ratio sits. ``0`` means any
    change at all surfaces, which is right for a count of modules.

    A percentage string — ``"15%"`` — is **relative** to the value it is
    compared against, and it is the only form that works for a probe
    whose values span orders of magnitude. Twenty-five lines is a rewrite
    of a forty-line module and rounding error in a seventeen-hundred-line
    one; no absolute number is right for both, and picking one means the
    probe is either blind to the small files or noisy about the large
    ones. A proportion is also unit-free, so a probe that changes what it
    counts does not have to re-tune this.

    What it costs is at the small end, where a proportion is a fraction
    of a line: a six-line file gaining one is a 17% move and is reported.
    """

    @field_validator("tolerance")
    @classmethod
    def _tolerance_is_a_number_or_a_percentage(cls, value: float | str) -> float | str:
        """Refused at load, where the message can name the line to edit."""
        if isinstance(value, str):
            percent(value)
        elif value < 0:
            raise ValueError(
                f"tolerance {value} is negative, so every value would count "
                f"as drift. Use 0 for that, or a positive threshold."
            )
        return value

    def moved(self, before: float, after: float) -> bool:
        """True when a value moved far enough to count as drift.

        Relative where the tolerance is a percentage, absolute where it
        is a bare number — see :attr:`tolerance`. A relative tolerance
        around a baseline of zero admits nothing, which is the answer
        that reads correctly: a key that was 0 and is now 3 moved by
        every proportion there is.
        """
        delta = abs(after - before)
        if isinstance(self.tolerance, str):
            return delta > percent(self.tolerance) * abs(before)
        return delta > self.tolerance

    def placeholder_names(self) -> list[str]:
        """Every ``{NAME}`` token appearing in the command, in order."""
        seen: list[str] = []
        for part in self.command:
            for match in _PLACEHOLDER_RE.finditer(part):
                if match.group(1) not in seen:
                    seen.append(match.group(1))
        return seen

    def declared_names(self) -> set[str]:
        return {p.name for p in self.placeholders}

    def check_consistent(self) -> None:
        """Fail on a template whose blanks and declarations disagree.

        An undeclared ``{NAME}`` would reach the runner as a literal
        brace and silently probe the wrong thing; a declared-but-unused
        placeholder means someone edited the command and left a stale
        declaration behind. Both are config bugs worth naming at load
        time rather than at run time.
        """
        used = set(self.placeholder_names())
        declared = self.declared_names()
        undeclared = used - declared
        if undeclared:
            raise ValueError(
                f"Probe {self.id!r} uses undeclared placeholder(s): "
                f"{', '.join(sorted(undeclared))}. Add a "
                f"[[probe.placeholders]] entry for each."
            )
        unused = declared - used
        if unused:
            raise ValueError(
                f"Probe {self.id!r} declares placeholder(s) its command never "
                f"uses: {', '.join(sorted(unused))}."
            )
        # Caught here rather than at exec: nothing fills a blank, so a
        # probe missing one can never run, and finding that out when the
        # config loads is the difference between a message naming the
        # line to edit and a failed probe in the middle of a scan.
        blank = sorted(p.name for p in self.placeholders if p.default is None)
        if blank:
            raise ValueError(
                f"Probe {self.id!r} has placeholder(s) with no value: "
                f'{", ".join(blank)}. Add `default = "..."` to each '
                f"[[probe.placeholders]] entry — probe parameters are "
                f"declared, not inferred."
            )

    def defaults(self) -> dict[str, str]:
        """This probe's parameter values, as its config declares them."""
        return {p.name: p.default for p in self.placeholders if p.default is not None}  # type: ignore[misc]

    def fill(self, values: dict[str, str]) -> list[str]:
        """Substitute ``values`` into the command, validating each one.

        Substitution happens per argv element, so a value containing
        spaces stays one argument rather than splitting into several.
        """
        by_name = {p.name: p for p in self.placeholders}
        missing = self.declared_names() - set(values)
        if missing:
            raise ValueError(
                f"Probe {self.id!r} is missing value(s) for: "
                f'{", ".join(sorted(missing))}. Add `default = "..."` to '
                f"the matching [[probe.placeholders]] entry."
            )

        out: list[str] = []
        for part in self.command:
            filled = part
            for name in _PLACEHOLDER_RE.findall(part):
                value = by_name[name].validate_value(values[name])
                filled = filled.replace(f"{{{name}}}", value)
            out.append(filled)
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def packaged_example_path() -> Path:
    """The scaffold a user copies to start a project config."""
    return Path(__file__).parent / "example.vibe-sentinel.toml"


def default_probes_path() -> Path:
    """The built-in probe set.

    Separate from the scaffold on purpose: these are defaults the tool runs,
    not boilerplate a user must carry. A config that declares no ``[[probe]]``
    gets them, so the scaffold stays short enough to read.
    """
    return Path(__file__).parent / "probes.default.toml"


def load_probes_from_toml(path: Path) -> list[Probe]:
    """Parse the ``[[probe]]`` tables in one TOML file.

    Validation is strict — unknown fields, wrong types, duplicate ids, and
    inconsistent placeholder declarations all raise ``ValueError`` naming
    the file. A probe config that half-loads is worse than one that
    refuses to.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FileNotFoundError(f"vibe-sentinel config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"vibe-sentinel config {path} is not valid TOML: {e}") from e

    try:
        probes = [Probe.model_validate(item) for item in raw.get("probe", []) or []]
    except ValidationError as e:
        raise ValueError(
            f"vibe-sentinel config {path} has invalid probe definitions:\n{e}"
        ) from e

    seen: set[str] = set()
    for p in probes:
        if p.id in seen:
            raise ValueError(
                f"vibe-sentinel config {path} declares probe id {p.id!r} twice."
            )
        seen.add(p.id)
        p.check_consistent()
    return probes


class ProbeSettings(BaseModel):
    """The ``[probes]`` table: which of the built-ins are in play."""

    #: Ids to drop from the effective set, built-in or your own. This is
    #: how you switch one off without copying the other five into your
    #: config just to leave them out.
    disable: list[str] = Field(default_factory=list)
    #: Set false to start from nothing and run only what you declare.
    use_builtins: bool = True
    #: Per-probe parameter values: ``{probe_id: {PLACEHOLDER: value}}``.
    #:
    #: The light way to change one value. Overriding a probe by
    #: redeclaring its ``[[probe]]`` table replaces the whole thing, so
    #: pointing one probe at a different directory used to mean copying
    #: its command, timeout and every other placeholder in order to
    #: change one string — and a copied command stops improving when the
    #: built-in does. This changes the value and nothing else.
    parameters: dict[str, dict[str, str]] = Field(default_factory=dict)


def load_probe_settings(path: Path) -> ProbeSettings:
    """Read the ``[probes]`` table from one config file."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        # load_probes_from_toml reports these against the same file with a
        # better message; returning defaults here avoids a duplicate error.
        return ProbeSettings()
    try:
        return ProbeSettings.model_validate(raw.get("probes", {}) or {})
    except ValidationError as e:
        raise ValueError(f"{path} has an invalid [probes] table:\n{e}") from e


def _apply_parameters(
    probes: dict[str, Probe], parameters: dict[str, dict[str, str]]
) -> None:
    """Apply ``[probes.parameters]`` to the merged set, in place.

    Every error here names the entry that caused it and refuses, rather
    than ignoring it. A parameter aimed at a probe that is not in the set
    — a typo, or a probe someone disabled — looks like a decision and
    changes nothing, which is the same failure ``[probes] disable`` and
    the pin tables already refuse. Silently measuring the default while
    the config says otherwise is the worst of the available outcomes.

    Values are validated against the placeholder's ``pattern`` here, at
    load, so a bad one names its config line instead of failing a probe
    in the middle of a scan.
    """
    for probe_id, values in parameters.items():
        probe = probes.get(probe_id)
        if probe is None:
            known = ", ".join(sorted(probes)) or "(none)"
            raise ValueError(
                f"[probes.parameters] sets values for unknown probe "
                f"{probe_id!r}. Known probes: {known}."
            )
        by_name = {p.name: p for p in probe.placeholders}
        for name, value in values.items():
            placeholder = by_name.get(name)
            if placeholder is None:
                declared = ", ".join(sorted(by_name)) or "(none)"
                raise ValueError(
                    f"[probes.parameters] sets {name!r} on probe {probe_id!r}, "
                    f"which declares no such placeholder. It declares: "
                    f"{declared}."
                )
            placeholder.default = placeholder.validate_value(str(value))


def load_probes(
    config_paths: Sequence[Path] | None = None,
    project_root: Path | None = None,
) -> list[Probe]:
    """Resolve the effective probe set.

    The built-in set is the base layer; your config is merged on top:

      - a ``[[probe]]`` with a NEW id **adds** a probe,
      - one reusing a built-in id **overrides** that built-in,
      - ``[probes.parameters]`` **changes one probe's values** without
        restating the rest of it,
      - ``[probes] disable = ["id", ...]`` **removes** one,
      - ``[probes] use_builtins = false`` starts from nothing.

    Adding rather than replacing is the important half. Declaring one probe
    used to drop the other five silently, so a user who wanted to add a
    check lost every check they already had — and nothing said so. Layering
    also means the built-ins keep improving under a config that overrode
    only one of them.

    Across several files, probes deduplicate by ``id`` with the later file
    winning, so a company-wide set layers under per-project overrides.
    Order follows first appearance, so output stays stable.
    """
    if config_paths:
        paths = list(config_paths)
    else:
        candidate = (project_root or Path.cwd()) / CONFIG_FILENAME
        paths = [candidate] if candidate.is_file() else []

    settings = ProbeSettings()
    for path in paths:
        if path.is_file():
            settings = load_probe_settings(path)

    merged: dict[str, Probe] = {}
    if settings.use_builtins:
        for probe in load_probes_from_toml(default_probes_path()):
            merged[probe.id] = probe
    for path in paths:
        for probe in load_probes_from_toml(path):
            merged[probe.id] = probe

    _apply_parameters(merged, settings.parameters)

    unknown = [pid for pid in settings.disable if pid not in merged]
    if unknown:
        known = ", ".join(sorted(merged)) or "(none)"
        raise ValueError(
            f"[probes] disable names probe(s) that do not exist: "
            f"{', '.join(unknown)}. Available: {known}. A stale entry here "
            f"silently protects nothing, so it is an error rather than a no-op."
        )
    for pid in settings.disable:
        del merged[pid]

    return list(merged.values())


def select_probes(ids: list[str] | None, pool: Sequence[Probe]) -> list[Probe]:
    """Return ``pool`` filtered by ``ids``, or all of it when None."""
    if ids is None:
        return list(pool)
    by_id = {p.id: p for p in pool}
    selected: list[Probe] = []
    unknown: list[str] = []
    for pid in ids:
        probe = by_id.get(pid)
        if probe is None:
            unknown.append(pid)
        else:
            selected.append(probe)
    if unknown:
        known = ", ".join(p.id for p in pool)
        raise ValueError(f"Unknown probe id(s): {', '.join(unknown)}. Known: {known}")
    return selected
