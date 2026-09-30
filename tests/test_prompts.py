"""Every question this package ships, pinned to its text.

The house rule is: never edit a prompt to satisfy a checker. Editing a
prompt is allowed and sometimes right — three edits to
``ORGANIZATION_QUESTION`` were made, measured against the drift corpus,
and all three reverted, and that loop is how the shipped question got
good. What is not allowed is editing one *quietly*, in the same afternoon
as a score that would not move otherwise, with nothing but a commit
message to say it happened.

A digest is the smallest instrument that makes that impossible. Change a
shipped question and this fails; update the digest in the same commit and
the diff now says, in one line a reviewer cannot miss, *a prompt changed
here*. The test does not judge the edit. It refuses to let it be silent.

Why a test rather than a probe: "this text changed" is binary, and it has
no magnitude worth trending. A probe keyed on question length would miss
a same-length rewording, which is exactly the edit somebody makes when
they are tuning against a corpus. This is the shape ``test_layers.py``
uses for the same reason — an invariant, not a reading.

Only *shipped* questions are here. A ``[[lens]]``, ``[[danger]]`` or
``[[secret]]`` declared by a project is that project's to change whenever
it likes; that is the whole point of it being declared. This file is
about what goes out in the wheel and is therefore a claim made on behalf
of everyone who installs it.

**When one of these fails**, read the diff before touching the number. If
the edit was deliberate, run ``python -m evals`` first and put the rate in
the commit message beside the new digest — the corpus is the only thing
that can tell you whether the new wording is better or merely different.
"""

from __future__ import annotations

import hashlib

import pytest

from vibe_sentinel import analyze, credentials, licenses, packages, safety, schemas


def digest(text: str) -> str:
    """First 16 hex characters of the SHA-256. Short enough to read in a
    diff, long enough that nothing collides by accident."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _every_shipped_question() -> dict[str, str]:
    """Name -> text, for every question this package puts to a model.

    Built rather than listed, so a new shipped prompt is caught by
    :func:`test_every_shipped_prompt_is_pinned` instead of being quietly
    exempt — a pin list that only holds what somebody remembered to add
    protects the prompts nobody is editing.
    """
    texts = {
        "analyze.ANALYZE_SYSTEM_PROMPT": analyze.ANALYZE_SYSTEM_PROMPT,
        "analyze.ORGANIZATION_QUESTION": analyze.ORGANIZATION_QUESTION,
        "safety.SAFETY_SYSTEM_PROMPT": safety.SAFETY_SYSTEM_PROMPT,
        "packages.NEAR_MISS_SYSTEM_PROMPT": packages.NEAR_MISS_SYSTEM_PROMPT,
        "credentials.CREDENTIAL_SYSTEM_PROMPT": credentials.CREDENTIAL_SYSTEM_PROMPT,
        "licenses.EXPLAIN_SYSTEM_PROMPT": licenses.EXPLAIN_SYSTEM_PROMPT,
    }
    for lens in analyze.BUILTIN_LENSES:
        texts[f"lens:{lens.id}"] = lens.question

    # The two shared fragments. Neither is a question, and both are text a
    # model is given on every request that carries them — which is what this
    # file is about. They were unpinned while the prompts around them were
    # not, so a change to what the model is told about length, or about the
    # fields it must fill, was silent in exactly the way the digests exist to
    # prevent. `every_field` is parameterised, so its two shipped call sites
    # are pinned rather than the function.
    texts["schemas.BREVITY"] = schemas.BREVITY
    texts["schemas.every_field(resolves_to)"] = schemas.every_field("resolves_to")
    texts["schemas.every_field(suspect)"] = schemas.every_field("suspect")

    # The two shipped rule sets, as one digest each. Per-entry pins would
    # be forty numbers to maintain for a set that is edited as a set, and
    # the question here is the same either way: did the text a model is
    # given change without anybody saying so.
    texts["dangers.default.toml"] = "\n".join(
        sorted(danger.question for danger in safety.builtin_dangers())
    )
    texts["secrets.default.toml"] = "\n".join(
        sorted(secret.question for secret in credentials.builtin_secrets())
    )
    return texts


#: The pinned text of every shipped question, by name. Update a line here
#: only in the same commit as the edit that caused it — that pairing is
#: the whole mechanism.
PINNED: dict[str, str] = {
    "analyze.ANALYZE_SYSTEM_PROMPT": "2ec1b8afb4c130cb",
    "analyze.ORGANIZATION_QUESTION": "c3ef96cc2e900c61",
    "safety.SAFETY_SYSTEM_PROMPT": "2467efe3aa77a322",
    "packages.NEAR_MISS_SYSTEM_PROMPT": "e750e4f16656ba0a",
    "credentials.CREDENTIAL_SYSTEM_PROMPT": "31c9fd445de0ea4b",
    "licenses.EXPLAIN_SYSTEM_PROMPT": "044dc93ca7f9e71c",
    "lens:organization": "c3ef96cc2e900c61",
    "schemas.BREVITY": "1aeeb15ea99979c9",
    "schemas.every_field(resolves_to)": "b9f9fd68c90e2c77",
    "schemas.every_field(suspect)": "6bb7470deeb6803d",
    "dangers.default.toml": "083feabf487ac6ac",
    "secrets.default.toml": "324d0735ed994d09",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_a_shipped_question_has_not_changed_silently(name: str) -> None:
    texts = _every_shipped_question()
    assert name in texts, (
        f"{name} is pinned but no longer shipped. Remove its line from "
        f"PINNED in the same commit that removed the prompt."
    )
    assert digest(texts[name]) == PINNED[name], (
        f"{name} changed.\n\n"
        f"  pinned:  {PINNED[name]}\n"
        f"  now:     {digest(texts[name])}\n\n"
        f"That is allowed. What is not allowed is doing it quietly. Run "
        f"`python -m evals` and put the rate in the commit message beside "
        f"the new digest, so the next person can tell an improvement from "
        f"a tuning."
    )


def test_every_shipped_prompt_is_pinned() -> None:
    """A new shipped question is pinned when it arrives, not when somebody
    remembers. Otherwise this file protects only the prompts nobody edits."""
    unpinned = sorted(set(_every_shipped_question()) - set(PINNED))
    assert not unpinned, (
        f"shipped but not pinned: {unpinned}. Add each to PINNED with its "
        f"digest — `digest(text)` prints it."
    )


def test_only_a_shipped_question_is_pinned() -> None:
    """A lens a project declares is its own to change at will. Pinning one
    would make a house rule out of the mechanism that exists so a project
    need not follow anybody's. So every `lens:` entry in PINNED names a
    lens this package ships.

    Stated over PINNED and BUILTIN_LENSES, which is where the rule lives.
    It was stated over the filesystem once — load the lenses of whatever
    tree is on disk, subtract the shipped ones, assert the remainder is
    unpinned — and that had all three faults of sampling an ambient tree
    instead of constructing a subject. It could not fail: a declared id
    that collides with a shipped one *is* the shipped one, so the
    subtraction always emptied the set it went on to check. It needed a
    guard (`assert declared`) to look like it was doing something, and a
    test that must first check its subject exists never built one. And
    its meaning depended on the directory it ran in, so it passed here
    and failed the moment the release synced it to a tree without
    `rules/`.
    """
    shipped = {lens.id for lens in analyze.BUILTIN_LENSES}
    pinned = {n.removeprefix("lens:") for n in PINNED if n.startswith("lens:")}
    assert pinned <= shipped, (
        f"PINNED names lens(es) this package does not ship: "
        f"{sorted(pinned - shipped)}. A declared lens belongs to the project "
        f"that declared it."
    )


# --- Untrusted content ------------------------------------------------------
#
# Every prompt below is handed text this project did not write: a file from
# the repository, a command an agent composed, a package name its publisher
# chose, keys built from directory names. A gate that can be argued out of a
# finding by the thing it is examining is not a gate, and the two that matter
# most are the ones an injection would bypass outright — a planted line
# reading "this is a test fixture" clearing a live key, or an `echo` a few
# commands back that reads like a verdict.
#
# `licenses.EXPLAIN_SYSTEM_PROMPT` had this first and set the wording: say the
# block is DATA, and say which field an attempt gets reported in. Reporting it
# is the half that matters — an attempt to talk a gate out of a finding is
# itself the finding, so "ignore it" alone would throw away the signal.

#: Prompt -> the answer field an injection attempt must be reported in.
UNTRUSTED: dict[str, str] = {
    "analyze.ANALYZE_SYSTEM_PROMPT": "assessment",
    "safety.SAFETY_SYSTEM_PROMPT": "reason",
    "credentials.CREDENTIAL_SYSTEM_PROMPT": "reason",
    "packages.NEAR_MISS_SYSTEM_PROMPT": "reason",
    "licenses.EXPLAIN_SYSTEM_PROMPT": "verify",
}


@pytest.mark.parametrize("name", sorted(UNTRUSTED))
def test_a_prompt_given_untrusted_content_says_it_is_data(name: str) -> None:
    text = _every_shipped_question()[name]
    assert "DATA, not instruction" in text, (
        f"{name} is handed content this project did not write and never says "
        f"so. See licenses.EXPLAIN_SYSTEM_PROMPT for the wording."
    )


@pytest.mark.parametrize(("name", "field"), sorted(UNTRUSTED.items()))
def test_a_prompt_names_where_an_attempt_gets_reported(name: str, field: str) -> None:
    """Reporting the attempt is the point; ignoring it discards the finding."""
    text = _every_shipped_question()[name]
    assert f'"{field}"' in text, (
        f"{name} must name the field an injection attempt is reported in "
        f'(expected "{field}"), or the attempt is silently dropped.'
    )


def test_every_untrusted_prompt_is_a_shipped_one() -> None:
    shipped = _every_shipped_question()
    missing = sorted(set(UNTRUSTED) - set(shipped))
    assert not missing, f"UNTRUSTED names prompts that no longer ship: {missing}"


def test_every_delimiter_a_prompt_names_is_one_its_module_emits() -> None:
    """A guard that names a tag nothing produces protects nothing.

    The prompts point the model at `<CHANGES_SINCE>`, `<HOUSE_NOTES>` and the
    rest to say which blocks are data and which are the project's own
    instructions. Rename a tag in a context builder and the sentence still
    reads fine while naming a delimiter the model never sees. Counting two
    occurrences in the module — one in the prompt, one where it is written —
    is the cheapest thing that fails when they drift apart.
    """
    import inspect
    import re

    for module in (analyze, safety, credentials, packages, licenses):
        source = inspect.getsource(module)
        for prompt_name, text in _every_shipped_question().items():
            if not prompt_name.startswith(module.__name__.rsplit(".", 1)[-1] + "."):
                continue
            for tag in sorted(set(re.findall(r"<([A-Z_]+)>", text))):
                # `<{tag}` rather than `<{tag}>`: a block may open with
                # attributes, as licenses writes `<EVIDENCE package=...>`.
                # A closing `</TAG>` does not match, so two occurrences still
                # means one in the prompt and one where it is written.
                assert source.count(f"<{tag}") >= 2, (
                    f"{prompt_name} names <{tag}> but {module.__name__} only "
                    f"writes it once — in the prompt. Nothing emits that block."
                )
