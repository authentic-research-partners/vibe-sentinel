"""The packages gate — dependency provenance.

The checks here have to survive a specific pressure: a gate that raises a
false alarm gets switched off, and one that stays silent about a real
hallucinated import is worse than nothing. So the tests are mostly about
the boundary — what must be flagged, and what must never be.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import packages as pk


def write_project(
    root: Path,
    *,
    pyproject: str = "",
    modules: dict[str, str] | None = None,
) -> Path:
    """A minimal project tree: a pyproject and an importable package."""
    (root / "pyproject.toml").write_text(
        pyproject
        or '[project]\nname = "myapp"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    pkg = root / "myapp"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in (modules or {}).items():
        (pkg / name).write_text(body, encoding="utf-8")
    return root


# --- name normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Foo.Bar_baz", "foo-bar-baz"),
        ("typing_extensions", "typing-extensions"),
        ("  ruff  ", "ruff"),
        ("zope.interface", "zope-interface"),
    ],
)
def test_names_normalize_to_one_spelling(raw: str, expected: str) -> None:
    assert pk.normalize(raw) == expected


# --- requirement parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "name", "specifier"),
    [
        ("httpx>=0.28", "httpx", ">=0.28"),
        ("httpx", "httpx", ""),
        ("pytest-xdist >= 3.0", "pytest-xdist", ">= 3.0"),
        ("foo[bar]>=1.0", "foo", ">=1.0"),
        ("foo[bar]", "foo", ""),
        ('tomli>=2; python_version < "3.11"', "tomli", ">=2"),
    ],
)
def test_requirement_name_and_bound_are_read(
    raw: str, name: str, specifier: str
) -> None:
    req = pk.parse_requirement(raw, "project")
    assert req is not None
    assert (req.name, req.specifier) == (name, specifier)


def test_environment_marker_is_kept_separate_from_the_bound() -> None:
    req = pk.parse_requirement('tomli>=2; python_version < "3.11"', "project")
    assert req is not None
    assert req.marker == 'python_version < "3.11"'


def test_poetry_tables_are_read_like_pep621_ones(tmp_path: Path) -> None:
    """Poetry writes name = constraint, not a PEP 508 string. Same answer."""
    write_project(
        tmp_path,
        pyproject=(
            "[tool.poetry]\nname = 'myapp'\n"
            "[tool.poetry.dependencies]\n"
            'python = "^3.13"\n'
            'httpx = "^0.28"\n'
            'loguru = "*"\n'
            "[tool.poetry.group.dev.dependencies]\n"
            'pytest = "^8.0"\n'
        ),
    )
    reqs = {r.name: r for r in pk.declared_requirements(tmp_path)}
    assert set(reqs) == {"httpx", "loguru", "pytest"}, "python is not a package"
    assert reqs["httpx"].specifier == "^0.28"
    assert reqs["loguru"].specifier == "", "'*' states no bound"
    assert reqs["pytest"].group == "poetry:dev"


def test_pep735_dependency_groups_count_as_declared(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        pyproject=(
            '[project]\nname = "myapp"\nversion = "0.1.0"\ndependencies = []\n'
            "[dependency-groups]\n"
            'dev = ["pytest>=8", {include-group = "extra"}]\n'
        ),
    )
    reqs = {r.name for r in pk.declared_requirements(tmp_path)}
    assert reqs == {"pytest"}, "include-group is a reference, not a package"


# --- phantom imports: the hallucinated-package check -----------------------


def test_an_import_that_resolves_to_nothing_is_reported(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        modules={"main.py": "import fastjson_utils_xyz\n"},
    )
    inventory = pk.take_inventory(tmp_path)
    findings = pk.audit(inventory, pk.Policy())
    phantom = [f for f in findings if f.kind == "phantom"]
    assert [f.name for f in phantom] == ["fastjson_utils_xyz"]
    assert phantom[0].evidence == ("myapp/main.py:1",)


@pytest.mark.parametrize(
    "source",
    [
        "import json\n",  # stdlib
        "import os.path\n",  # stdlib, dotted
        "from myapp import other\n",  # first-party
        "import myapp.other\n",  # first-party, dotted
        "from . import sibling\n",  # relative
        "from ..pkg import thing\n",  # relative, deeper
    ],
)
def test_resolvable_imports_are_never_phantoms(tmp_path: Path, source: str) -> None:
    """The false-alarm boundary. Each of these would be a phantom under a
    naive "is it in pyproject.toml" check, and each is entirely normal."""
    write_project(tmp_path, modules={"main.py": source})
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    assert [f for f in findings if f.kind == "phantom"] == []


def test_an_installed_package_is_not_a_phantom(tmp_path: Path) -> None:
    """pytest is installed in the environment running this test."""
    write_project(tmp_path, modules={"main.py": "import pytest\n"})
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    assert [f for f in findings if f.kind == "phantom"] == []


def test_a_phantom_is_not_also_reported_as_undeclared(tmp_path: Path) -> None:
    """One name, one finding, under the most serious heading it earns."""
    write_project(tmp_path, modules={"main.py": "import fastjson_utils_xyz\n"})
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    kinds = [f.kind for f in findings if f.name == "fastjson_utils_xyz"]
    assert kinds == ["phantom"]


# --- unconstrained ---------------------------------------------------------


def test_a_dependency_with_no_version_bound_is_reported(tmp_path: Path) -> None:
    write_project(
        tmp_path,
        pyproject=(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["httpx>=0.28", "requests"]\n'
        ),
    )
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    unbounded = [f for f in findings if f.kind == "unconstrained"]
    assert [f.name for f in unbounded] == ["requests"]


# --- uninstalled: the guard on everything else ------------------------------


def test_a_declared_dependency_missing_from_the_environment_is_reported(
    tmp_path: Path,
) -> None:
    write_project(
        tmp_path,
        pyproject=(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["definitely-not-installed-xyz>=1"]\n'
        ),
    )
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    assert [f.name for f in findings if f.kind == "uninstalled"] == [
        "definitely-not-installed-xyz"
    ]


def test_an_extra_nobody_asked_for_is_not_reported_as_missing(
    tmp_path: Path,
) -> None:
    """Optional dependencies are legitimately absent, and a platform marker
    means the same thing. Reporting either would flood the gate."""
    write_project(
        tmp_path,
        pyproject=(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            "dependencies = [\"pywin32>=300; sys_platform == 'win32'\"]\n"
            "[project.optional-dependencies]\n"
            'gpu = ["definitely-not-installed-xyz>=1"]\n'
        ),
    )
    findings = pk.audit(pk.take_inventory(tmp_path), pk.Policy())
    assert [f for f in findings if f.kind == "uninstalled"] == []


# --- near misses -----------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("requests", "reqests", True),
        ("urllib3", "urllib", True),
        ("httpx", "httpx", False),  # identical is not a near miss
        ("pandas", "polars", False),
        ("colorama", "coloramaa", True),
    ],
)
def test_edit_distance_of_one(a: str, b: str, expected: bool) -> None:
    assert pk._within_one_edit(a, b) is expected
    assert pk._within_one_edit(b, a) is expected, "the relation is symmetric"


def test_a_trailing_version_digit_is_not_a_typo() -> None:
    """httpx / httpx2 and httpcore / httpcore2 are one edit apart and both real.
    Both pairs are installed in this repo's own environment, which is how this
    rule got written — a trailing number is how a project ships a rewrite."""
    pairs = pk.near_misses(["httpx", "httpx2", "httpcore", "httpcore2"])
    assert pairs == []


def test_two_names_one_letter_apart_are_reported() -> None:
    pairs = pk.near_misses(["requests", "reqests", "loguru"])
    assert pairs == [("reqests", "requests")]


def test_short_names_are_not_compared() -> None:
    """Below the length floor, one edit separates far too many real names."""
    assert pk.near_misses(["six", "sax", "sox"]) == []


# --- orphans ---------------------------------------------------------------


def test_only_the_root_of_an_orphan_tree_is_reported(tmp_path: Path) -> None:
    """One stray `pip install` must be one finding, not one per node of the
    tree it dragged in — the others cannot be acted on separately."""
    installed = {
        "toplevel": pk.Installed(
            name="toplevel", version="1.0", requires=("mid",), installer="pip"
        ),
        "mid": pk.Installed(
            name="mid", version="1.0", requires=("leaf",), installer="pip"
        ),
        "leaf": pk.Installed(name="leaf", version="1.0", requires=(), installer="pip"),
    }
    inventory = pk.Inventory(
        environment=pk.environment(),
        declared=(),
        installed=installed,
        closure=frozenset(),
        imports=(),
        first_party=frozenset(),
        project="myapp",
    )
    assert pk.outside_closure(inventory) == {"toplevel"}
    assert pk.dragged_in(inventory, "toplevel") == {"mid", "leaf"}

    findings = pk.audit(inventory, pk.Policy())
    orphans = [f for f in findings if f.kind == "orphan"]
    assert [f.name for f in orphans] == ["toplevel"]
    assert "mid" in orphans[0].detail and "leaf" in orphans[0].detail


def test_a_declared_package_is_never_an_orphan(tmp_path: Path) -> None:
    installed = {
        "wanted": pk.Installed(name="wanted", version="1.0", requires=("dep",)),
        "dep": pk.Installed(name="dep", version="1.0", requires=()),
    }
    declared = (pk.parse_requirement("wanted>=1", "project"),)
    inventory = pk.Inventory(
        environment=pk.environment(),
        declared=declared,  # type: ignore[arg-type]
        installed=installed,
        closure=pk.requirement_closure({"wanted"}, installed),
        imports=(),
        first_party=frozenset(),
        project="myapp",
    )
    assert pk.outside_closure(inventory) == frozenset()


def test_extras_only_requirements_stay_out_of_the_closure() -> None:
    """A requirement guarded by `extra == "..."` arrives only when asked for.
    Treating it as always-required would swell the closure and hide orphans."""

    class _Meta:
        def get_all(self, key: str) -> list[str]:
            return [
                "anyio>=4",
                'brotli>=1.2; (platform_python_implementation == "CPython") '
                'and extra == "brotli"',
            ]

        def get(self, key: str, default: object = None) -> object:
            return {"Name": "thing", "Version": "1.0"}.get(key, default)

    class _Dist:
        metadata = _Meta()

    assert pk._requires(_Dist()) == ("anyio",)  # type: ignore[arg-type]


# --- policy and pins -------------------------------------------------------


def test_pins_are_scoped_to_the_finding_kinds_they_name() -> None:
    """The difference between a pin and an ignore. Accepting today's finding
    must not accept tomorrow's different one for the same package."""
    policy = pk.Policy(
        pins=({"packages": ["some-tool"], "accept": ["orphan"]},),
    )
    assert policy.accepts("some-tool", "orphan")
    assert not policy.accepts("some-tool", "squatted")
    assert not policy.accepts("other-tool", "orphan")


def test_pin_patterns_glob() -> None:
    policy = pk.Policy(pins=({"packages": ["nvidia-*"], "accept": ["orphan"]},))
    assert policy.accepts("nvidia-cublas-cu12", "orphan")
    assert not policy.accepts("numpy", "orphan")


def test_pip_and_friends_are_ignored_by_default() -> None:
    assert pk.Policy().accepts("setuptools", "orphan")
    assert pk.Policy().accepts("pip", "orphan")


def test_a_missing_policy_yields_the_defaults_not_an_error(tmp_path: Path) -> None:
    """Unlike the licence policy there is no allow-list to get wrong, so
    'no policy' means 'check everything and pin nothing'."""
    policy = pk.load_policy(root=tmp_path)
    assert policy.source == "defaults"
    assert policy.new_package_days == 90


def test_the_project_config_table_is_used(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[packages]\nignore = ["mytool"]\nnew_package_days = 30\n', encoding="utf-8"
    )
    policy = pk.load_policy(root=tmp_path)
    assert policy.new_package_days == 30
    assert policy.accepts("mytool", "orphan")


def test_a_negative_age_threshold_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        "[packages]\nnew_package_days = -1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="new_package_days"):
        pk.load_policy(root=tmp_path)


# --- online confirmation ---------------------------------------------------


def _phantom(name: str = "invented_xyz") -> pk.Finding:
    return pk.Finding(
        kind="phantom",
        name=name,
        detail="imported",
        remediation="check it",
        evidence=("myapp/main.py:1",),
    )


def _inventory(tmp_path: Path) -> pk.Inventory:
    write_project(tmp_path)
    return pk.take_inventory(tmp_path)


def test_a_name_the_index_has_never_heard_of_is_unregistered(tmp_path: Path) -> None:
    facts = {"invented_xyz": pk.RegistryFact(name="invented_xyz", exists=False)}
    out = pk.confirm_online(_inventory(tmp_path), [_phantom()], facts, pk.Policy())
    assert [f.kind for f in out] == ["unregistered"]


def test_a_freshly_registered_hallucination_is_squatted(tmp_path: Path) -> None:
    """The whole point. A name the code invented, which someone registered
    days ago, is not a coincidence."""
    facts = {
        "invented_xyz": pk.RegistryFact(
            name="invented_xyz",
            exists=True,
            first_release="2026-08-27T09:12:00Z",
            release_count=1,
            age_days=6,
        )
    }
    out = pk.confirm_online(_inventory(tmp_path), [_phantom()], facts, pk.Policy())
    assert [f.kind for f in out] == ["squatted"]
    assert "6 days ago" in out[0].detail


def test_an_established_project_of_the_same_name_stays_a_phantom(
    tmp_path: Path,
) -> None:
    facts = {
        "invented_xyz": pk.RegistryFact(
            name="invented_xyz",
            exists=True,
            first_release="2015-12-22T00:00:00Z",
            release_count=9,
            age_days=3906,
        )
    }
    out = pk.confirm_online(_inventory(tmp_path), [_phantom()], facts, pk.Policy())
    assert [f.kind for f in out] == ["phantom"]


def test_a_failed_lookup_is_reported_rather_than_read_as_absence(
    tmp_path: Path,
) -> None:
    """ "We could not check" reported as "it is not there" is the shape of a
    review that did not happen — the one thing this tool must never do."""
    facts = {
        "invented_xyz": pk.RegistryFact(name="invented_xyz", error="ConnectTimeout")
    }
    out = pk.confirm_online(_inventory(tmp_path), [_phantom()], facts, pk.Policy())
    kinds = [f.kind for f in out]
    assert "unregistered" not in kinds
    assert kinds == ["phantom", "unchecked"]


def test_only_flagged_and_publicly_declared_names_are_sent_to_the_index(
    tmp_path: Path,
) -> None:
    """The online step must not hand PyPI an inventory of the machine."""
    write_project(
        tmp_path,
        pyproject=(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
            'dependencies = ["httpx>=0.28"]\n'
        ),
    )
    inventory = pk.take_inventory(tmp_path)
    names = pk.names_to_confirm(inventory, [_phantom()])
    assert "invented_xyz" in names
    assert "httpx" in names
    assert len(names) < len(inventory.installed), "not the whole environment"


def test_earliest_upload_is_taken_across_every_release() -> None:
    payload = {
        "releases": {
            "2.0": [{"upload_time_iso_8601": "2020-05-01T00:00:00Z"}],
            "1.0": [{"upload_time_iso_8601": "2018-01-09T00:00:00Z"}],
        }
    }
    assert pk._earliest_upload(payload) == "2018-01-09T00:00:00Z"


def test_a_registry_payload_with_no_timestamps_yields_no_age() -> None:
    assert pk._earliest_upload({"releases": {}}) == ""
    assert pk._age_days("") == -1
    assert pk._age_days("not a date") == -1


# --- environment identity --------------------------------------------------


def test_the_environment_is_identified_not_assumed() -> None:
    env = pk.environment()
    assert env.kind in {"conda", "venv", "poetry", "uv", "system"}
    assert env.prefix and env.python and env.executable
    assert env.label == f"{env.kind}:{env.name}"


# --- risk vocabulary -------------------------------------------------------


def test_findings_are_ordered_most_serious_first() -> None:
    """Alphabetical order prints `newborn` above `squatted`, which buries the
    one finding that means someone registered a name the code invented."""

    def f(kind: str, name: str = "x") -> pk.Finding:
        return pk.Finding(kind=kind, name=name, detail="d", remediation="r")

    ordered = pk.by_severity([f("unconstrained"), f("squatted"), f("orphan")])
    assert [x.kind for x in ordered] == ["squatted", "orphan", "unconstrained"]


def test_an_unknown_risk_kind_sorts_last_rather_than_crashing() -> None:
    assert pk.severity_rank("squatted") == 0
    assert pk.severity_rank("something-new") == len(pk.RISK_ORDER)


def test_every_finding_kind_has_a_place_in_the_risk_order() -> None:
    """A kind missing from RISK_ORDER would be recorded with an empty risk and
    vanish from every query against the column."""
    kinds = {
        "phantom",
        "undeclared",
        "orphan",
        "uninstalled",
        "unconstrained",
        "near-miss",
        "unregistered",
        "squatted",
        "newborn",
        "unchecked",
    }
    assert kinds == set(pk.RISK_ORDER)


def test_findings_carry_their_remediation() -> None:
    """House rule: an error names the command that fixes it."""
    inventory = pk.Inventory(
        environment=pk.environment(),
        declared=(),
        installed={
            "stray": pk.Installed(name="stray", version="1.0", requires=()),
        },
        closure=frozenset(),
        imports=(),
        first_party=frozenset(),
        project="myapp",
    )
    for finding in pk.audit(inventory, pk.Policy()):
        assert finding.remediation.strip(), finding.kind


# --- adjudicating the one finding that is a judgement -----------------------
#
# `near-miss` is the only kind here with a judgement in it, and the rule that
# finds it already carries one exception written by hand. What these pin is the
# boundary around asking: the model settles a pair the mechanism found, it never
# adds one, and a pair nobody reviewed fails exactly as it did before the step
# existed — this gate must not get weaker when the GPU is off.


def _dist(
    name: str, version: str = "1.0", summary: str = "", **kw: Any
) -> pk.Installed:
    return pk.Installed(name=name, version=version, summary=summary, **kw)


def _near_miss_env() -> pk.Inventory:
    return pk.Inventory(
        environment=pk.Environment(
            kind="uv", name=".venv", prefix="/x", python="3.13", executable="/x/py"
        ),
        declared=(),
        installed={
            "requests": _dist("requests", "2.32.3", "Python HTTP for Humans."),
            "reqests": _dist("reqests", "0.0.1"),
        },
        closure=frozenset({"requests"}),
        imports=(),
        first_party=frozenset(),
        project="demo",
    )


def _pair(name: str = "reqests") -> pk.Finding:
    return pk.Finding(
        kind="near-miss",
        name=name,
        detail="'reqests' and 'requests' are one edit apart and both installed",
        remediation="Confirm both are intended.",
        evidence=("reqests", "requests"),
    )


def _answer(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[tuple[str, str]]:
    """Stand in for the model; capture every (system, user) pair sent."""
    sent: list[tuple[str, str]] = []

    async def fake_query(system: str, user: str, *_a: Any, **_kw: Any) -> Any:
        sent.append((system, user))
        return reply(user) if callable(reply) else reply

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", fake_query)
    return sent


def _adjudicate(findings: list[pk.Finding], **kw: Any) -> pk.Adjudication:
    from vibe_sentinel.config import SentinelConfig

    return asyncio.run(
        pk.adjudicate(_near_miss_env(), findings, pk.Policy(), SentinelConfig(), **kw)
    )


def test_distinct_settles_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    _answer(monkeypatch, {"verdict": "distinct", "suspect": "", "reason": "both real"})
    out = _adjudicate([_pair()])
    assert out.reviewed is True
    assert out.failing() == ()


def test_typosquat_and_unclear_both_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    """`unclear` is a real answer here, and it is a finding rather than a pass."""
    for verdict in ("typosquat", "unclear"):
        _answer(monkeypatch, {"verdict": verdict, "suspect": "reqests", "reason": "r"})
        out = _adjudicate([_pair()])
        assert [f.name for f in out.failing()] == ["reqests"], verdict


def test_no_model_leaves_every_pair_standing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must not get weaker when the GPU is off.

    Unlike the credentials gate, where an unadjudicated pattern hit on a
    string is weak evidence, this finding failed on its own before any model
    existed. `--no-model` leaves its verdict byte-identical.
    """

    async def explode(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the model was asked with use_model=False")

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", explode)
    out = _adjudicate([_pair()], use_model=False)
    assert out.reviewed is False
    assert [f.name for f in out.failing()] == ["reqests"]
    assert "adjudicated by nobody" in out.note


def test_a_model_that_does_not_answer_leaves_the_pair_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, None)
    out = _adjudicate([_pair()])
    assert out.reviewed is False
    assert [f.name for f in out.failing()] == ["reqests"]


def test_a_pin_settles_it_without_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decision somebody recorded is not one an 8B model gets a vote on."""

    async def explode(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a pinned pair was sent to the model")

    monkeypatch.setattr("vibe_sentinel.llm.llm_query", explode)
    from vibe_sentinel.config import SentinelConfig

    policy = pk.Policy(
        pins=(
            {
                "packages": ["reqests"],
                "accept": ["near-miss"],
                "reason": "vendored deliberately",
                "verified": "2026-01-01",
            },
        )
    )
    out = asyncio.run(
        pk.adjudicate(_near_miss_env(), [_pair()], policy, SentinelConfig())
    )
    assert out.failing() == ()
    assert out.judgements[0].verdict == "pinned"


def test_only_near_misses_are_asked_about(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other kind is a fact, and a fact is not improved by an opinion."""
    sent = _answer(monkeypatch, {"verdict": "distinct", "suspect": "", "reason": "r"})
    orphan = pk.Finding(
        kind="orphan", name="leftover", detail="d", remediation="r", evidence=()
    )
    out = _adjudicate([orphan, _pair()])
    assert len(sent) == 1
    assert "leftover" in {f.name for f in out.failing()}


def test_one_request_per_pair_over_one_shared_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _answer(monkeypatch, {"verdict": "unclear", "suspect": "", "reason": "r"})
    _adjudicate([_pair("reqests"), _pair("reqeusts")])
    assert len(sent) == 2
    assert len({system for system, _ in sent}) == 1, "the context must not diverge"


def test_the_context_carries_what_an_edit_distance_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What each says it does, and whether anything asked for it."""
    context = pk.build_near_miss_context(_near_miss_env(), [_pair()])
    assert "Python HTTP for Humans." in context
    assert "NOTHING declares it and NOTHING requires it" in context


def test_the_findings_key_never_moves_with_an_opinion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that shifted with a verdict would not be stable across runs."""
    _answer(
        monkeypatch,
        {"verdict": "typosquat", "suspect": "requests", "reason": "r"},
    )
    out = _adjudicate([_pair("reqests")])
    assert out.judgements[0].finding.name == "reqests"
    assert out.judgements[0].suspect == "requests"


# --- provenance across two metadata entries for one name -------------------


class _FakeMeta:
    def __init__(self, name: str, version: str) -> None:
        self._d = {"Name": name, "Version": version, "Summary": ""}

    def get(self, key: str, default: object = None) -> object:
        return self._d.get(key, default)

    def get_all(self, key: str) -> list[str]:
        return []


class _FakeDist:
    """One metadata entry, with only the files its format would carry."""

    def __init__(self, name: str, version: str, files: dict[str, str]) -> None:
        self.metadata = _FakeMeta(name, version)
        self._files = files

    def read_text(self, name: str) -> str | None:
        return self._files.get(name)


_EDITABLE = '{"url": "file:///somewhere/proj", "dir_info": {"editable": true}}'


def _dist_info(name: str, version: str, **extra: str) -> _FakeDist:
    """A `.dist-info`, which is the format that can carry provenance."""
    return _FakeDist(name, version, {"WHEEL": "Wheel-Version: 1.0", **extra})


def _egg_info(name: str, version: str) -> _FakeDist:
    """An `.egg-info`, which has nowhere to put an installer or a URL."""
    return _FakeDist(name, version, {"PKG-INFO": f"Name: {name}"})


def test_provenance_is_taken_from_the_entry_that_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editable install leaves an `.egg-info` in the project root and a
    `.dist-info` in site-packages. The first wins the path race and has
    nowhere to record an installer, so reading provenance off it alone
    reports a local checkout as having come from an index."""
    monkeypatch.setattr(
        pk.md,
        "distributions",
        lambda: iter(
            [
                _egg_info("thing", "1.0"),
                _dist_info(
                    "thing", "1.0", INSTALLER="uv", **{"direct_url.json": _EDITABLE}
                ),
            ]
        ),
    )
    installed = pk.installed_distributions()["thing"]
    assert installed.installer == "uv"
    assert installed.direct_url == _EDITABLE
    assert installed.provenance_recorded is True


def test_identity_still_comes_from_the_first_entry_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only provenance is backfilled. Which code would be imported is
    still decided by path order, and that is what name and version
    describe."""
    monkeypatch.setattr(
        pk.md,
        "distributions",
        lambda: iter([_egg_info("thing", "1.0"), _dist_info("thing", "1.0")]),
    )
    assert pk.installed_distributions()["thing"].version == "1.0"


def test_nothing_is_carried_across_a_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two entries at the same version are one install written down twice.
    Two at different versions are a genuine disagreement, and attaching
    one's origin to the other's version would invent a fact."""
    monkeypatch.setattr(
        pk.md,
        "distributions",
        lambda: iter(
            [
                _egg_info("thing", "1.0"),
                _dist_info(
                    "thing", "2.0", INSTALLER="uv", **{"direct_url.json": _EDITABLE}
                ),
            ]
        ),
    )
    installed = pk.installed_distributions()["thing"]
    assert installed.version == "1.0"
    assert installed.direct_url == ""
    assert installed.provenance_recorded is False


def test_an_egg_info_alone_records_no_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction that matters: an empty direct_url here means
    nothing was written down, not that the package came from an index."""
    monkeypatch.setattr(
        pk.md, "distributions", lambda: iter([_egg_info("thing", "1.0")])
    )
    assert pk.installed_distributions()["thing"].provenance_recorded is False


def test_a_dist_info_with_no_direct_url_did_come_from_an_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of it. A `.dist-info` could have carried a direct
    URL and does not, so its absence is a measurement."""
    monkeypatch.setattr(
        pk.md,
        "distributions",
        lambda: iter([_dist_info("thing", "1.0", INSTALLER="pip")]),
    )
    installed = pk.installed_distributions()["thing"]
    assert installed.direct_url == ""
    assert installed.provenance_recorded is True
