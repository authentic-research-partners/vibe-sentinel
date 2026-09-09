"""Installed names one edit apart, and which pairs are two packages.

The one provenance check that is a judgement rather than a fact. Every
other kind the audit reports is mechanical — an import resolves or it
does not, a version is bounded or it is not — but two installed names one
edit apart is a typosquat and a rewrite shipped under a new name in
exactly the same shape, and an edit distance cannot tell them apart.

So the corpus carries both halves. The squats are the shape a registered
hallucination has: a name nothing declares, nothing requires and nothing
imports, sitting beside one that everything does. The distinct pairs are
the ones that would be a false alarm — ``pyyaml`` and ``pyaml`` are both
real, ``types-toml`` and ``types-tomli`` are stubs for two different
packages, and the facts that separate them are in the summaries rather
than in the spelling.

Two pairs are labelled ``paired=False``: they are what the mechanical
stage deliberately does *not* report. A trailing digit is how a project
ships a rewrite (``httpx`` / ``httpx2``, both installed in this
repository), and the length floor keeps four-character names out of a
check whose false-alarm rate rises as names get shorter. A corpus with
no case for either would not notice the day one of them went away.
"""

from __future__ import annotations

from evals.cases import NearMissCase, PackageFacts

CASES: tuple[NearMissCase, ...] = (
    NearMissCase(
        id="requests-misspelled",
        why=(
            "One deletion from the most-installed name in Python, declared by "
            "nothing and imported by nothing. This is the exact shape of the "
            "attack: the invented names repeat, which is what makes "
            "registering them worth an attacker's time."
        ),
        left=PackageFacts(
            name="requests",
            version="2.32.3",
            summary="Python HTTP for Humans.",
            installer="uv",
            declared=True,
            imported=True,
        ),
        right=PackageFacts(
            name="requsts",
            version="0.0.1",
            summary="",
            installer="",
        ),
        paired=True,
        verdict="typosquat",
        suspect="requsts",
    ),
    NearMissCase(
        id="colorama-misspelled",
        why="A vowel swapped in a name every terminal library depends on.",
        left=PackageFacts(
            name="colorama",
            version="0.4.6",
            summary="Cross-platform coloured terminal text.",
            installer="uv",
            required_by=("pytest", "click"),
        ),
        right=PackageFacts(
            name="coloroma",
            version="1.0.0",
            summary="Cross-platform coloured terminal text.",
            installer="",
        ),
        paired=True,
        verdict="typosquat",
        suspect="coloroma",
        also_ok=("unclear",),
    ),
    NearMissCase(
        id="django-homoglyph",
        why=(
            "A single substituted letter in a framework name. The copied "
            "summary is part of the shape — a squat describes itself as the "
            "package it is imitating."
        ),
        left=PackageFacts(
            name="django",
            version="5.1.4",
            summary="A high-level Python web framework.",
            installer="uv",
            declared=True,
            imported=True,
        ),
        right=PackageFacts(
            name="djanqo",
            version="5.1.4",
            summary="A high-level Python web framework.",
            installer="",
        ),
        paired=True,
        verdict="typosquat",
        suspect="djanqo",
    ),
    NearMissCase(
        id="urllib3-misspelled",
        why=(
            "A deleted letter inside a name that also ends in a digit, so the "
            "digits rule must not swallow it: stripping the 3 from both still "
            "leaves two different names."
        ),
        left=PackageFacts(
            name="urllib3",
            version="2.3.0",
            summary="HTTP library with thread-safe connection pooling.",
            installer="uv",
            required_by=("requests",),
        ),
        right=PackageFacts(
            name="urlib3",
            version="1.0.0",
            summary="HTTP library with thread-safe connection pooling.",
            installer="",
        ),
        paired=True,
        verdict="typosquat",
        suspect="urlib3",
    ),
    NearMissCase(
        id="pyyaml-and-pyaml",
        why=(
            "Both real, both on PyPI for years, one edit apart. PyYAML is the "
            "parser; pyaml is a pretty-printer built on top of it and "
            "requires it. The facts that separate them are the summaries and "
            "the requirement, never the spelling."
        ),
        left=PackageFacts(
            name="pyyaml",
            version="6.0.2",
            summary="YAML parser and emitter for Python.",
            installer="uv",
            declared=True,
            imported=True,
        ),
        right=PackageFacts(
            name="pyaml",
            version="25.1.0",
            summary="PyYAML-based module to produce a bit more pretty and "
            "readable YAML-serialized data.",
            installer="uv",
            required_by=("some-tool",),
        ),
        paired=True,
        verdict="distinct",
    ),
    NearMissCase(
        id="typeshed-stubs-for-two-packages",
        why=(
            "`types-toml` stubs `toml` and `types-tomli` stubs `tomli`. Two "
            "packages exist because two packages are being described, and "
            "the name is one edit apart because the things they describe "
            "are."
        ),
        left=PackageFacts(
            name="types-toml",
            version="0.10.8",
            summary="Typing stubs for toml.",
            installer="uv",
            required_by=("mypy",),
        ),
        right=PackageFacts(
            name="types-tomli",
            version="1.2.0",
            summary="Typing stubs for tomli.",
            installer="uv",
            required_by=("mypy",),
        ),
        paired=True,
        verdict="distinct",
    ),
    NearMissCase(
        id="vendor-sdk-and-spec-toolkit",
        why=(
            "One is a vendor's client library, the other is tooling for an "
            "API description format. Nothing in the spelling says so and "
            "everything in the summaries does — which is the whole reason "
            "the context carries them."
        ),
        left=PackageFacts(
            name="openai",
            version="1.59.0",
            summary="The official Python library for the OpenAI API.",
            installer="uv",
            declared=True,
            imported=True,
        ),
        right=PackageFacts(
            name="openapi",
            version="1.1.0",
            summary="Tooling for working with OpenAPI specifications.",
            installer="uv",
            declared=True,
        ),
        paired=True,
        verdict="distinct",
        also_ok=("unclear",),
    ),
    NearMissCase(
        id="trailing-digit-rewrite",
        why=(
            "A trailing number is how a project ships a rewrite, not how a "
            "name is mistyped. Both of these are installed in this "
            "repository's own environment, which is how the rule came to be "
            "written — so the mechanical stage must not report the pair at "
            "all, and the model is never asked."
        ),
        left=PackageFacts(
            name="httpx",
            version="0.28.1",
            summary="The next generation HTTP client.",
            installer="uv",
            declared=True,
            imported=True,
        ),
        right=PackageFacts(
            name="httpx2",
            version="0.1.0",
            summary="A rewrite of httpx.",
            installer="uv",
        ),
        paired=False,
    ),
    NearMissCase(
        id="below-the-length-floor",
        why=(
            "`attr` is four characters, under the floor the pairing keeps. "
            "Short names collide constantly and the false-alarm rate rises "
            "with every character removed, so the check declines to look "
            "rather than reporting pairs nobody can act on."
        ),
        left=PackageFacts(
            name="attrs",
            version="24.3.0",
            summary="Classes without boilerplate.",
            installer="uv",
            required_by=("jsonschema",),
        ),
        right=PackageFacts(
            name="attr",
            version="0.3.2",
            summary="Simple decorator to set attributes of target function.",
            installer="uv",
        ),
        paired=False,
    ),
)


def all_names() -> list[str]:
    """Every name in the corpus, as an installed set for the pairing stage.

    One list rather than a pair per case, because ``near_misses`` compares
    everything with everything — which is also how it earns a false alarm,
    and the only way to measure that is to hand it the whole set at once.
    """
    names: list[str] = []
    for case in CASES:
        names += [case.left.name, case.right.name]
    return sorted(set(names))
