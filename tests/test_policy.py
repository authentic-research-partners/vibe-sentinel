"""What makes a pin a decision rather than an ignore.

The rule is the same in all three gates, so it is checked in one place —
and these tests exist because for a while it was only *enforced* in one
place. The licence gate rejected a pin missing its reason; provenance and
credentials took whatever TOML handed them, which meant a misspelled key
produced a pin that read as a decision and covered nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_sentinel import credentials as creds
from vibe_sentinel import licenses as lic
from vibe_sentinel import packages as pkg
from vibe_sentinel.policy import check_pins, required_keys

GOOD = {
    "packages": ["certifi"],
    "accept": ["MPL-2.0"],
    "reason": "CA bundle, used unmodified",
    "verified": "2026-01-15",
}


def test_a_complete_pin_passes() -> None:
    check_pins([GOOD], subject="packages", where="cfg")


def test_the_selector_key_differs_per_gate() -> None:
    assert required_keys("packages")[0] == "packages"
    assert required_keys("paths")[0] == "paths"
    assert set(required_keys("paths")) == {"paths", "accept", "reason", "verified"}


@pytest.mark.parametrize("dropped", sorted(GOOD))
def test_every_one_of_the_four_is_required(dropped: str) -> None:
    pin = {k: v for k, v in GOOD.items() if k != dropped}
    with pytest.raises(ValueError, match=dropped):
        check_pins([pin], subject="packages", where="cfg")


def test_an_empty_value_counts_as_missing() -> None:
    """`reason = ""` is not a reason."""
    with pytest.raises(ValueError, match="reason"):
        check_pins([{**GOOD, "reason": ""}], subject="packages", where="cfg")


def test_an_unknown_key_is_an_error_not_a_no_op() -> None:
    """A pin that looks like a decision and covers nothing is worse than
    one that fails: the finding comes back and the next person assumes the
    gate is broken."""
    with pytest.raises(ValueError, match="accepts"):
        check_pins([{**GOOD, "accepts": ["MIT"]}], subject="packages", where="cfg")


def test_the_error_names_the_file_and_the_entry() -> None:
    with pytest.raises(ValueError, match=r"security/license-policy\.toml"):
        check_pins(
            [GOOD, {"packages": ["x"]}],
            subject="packages",
            where="security/license-policy.toml",
        )


def test_something_that_is_not_a_table_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a table"):
        check_pins(["certifi"], subject="packages", where="cfg")


# --- every gate actually applies it ---------------------------------------


def _write(root: Path, body: str) -> Path:
    (root / ".vibe-sentinel.toml").write_text(body, encoding="utf-8")
    return root


def test_the_licence_gate_rejects_an_incomplete_pin(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "[licenses]\n"
        'allowed_categories = ["permissive"]\n'
        '[[licenses.pin]]\npackages = ["certifi"]\naccept = ["MPL-2.0"]\n',
    )
    with pytest.raises(ValueError, match="reason, verified"):
        lic.load_policy(root=root)


def test_the_provenance_gate_rejects_an_incomplete_pin(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        '[packages]\n[[packages.pin]]\npackages = ["some-tool"]\naccept = ["orphan"]\n',
    )
    with pytest.raises(ValueError, match="reason, verified"):
        pkg.load_policy(root=root)


def test_the_credentials_gate_rejects_an_incomplete_pin(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "[credentials]\n[[credentials.pin]]\n"
        'paths = ["tests/fixtures/*.pem"]\naccept = ["private-key-file"]\n',
    )
    with pytest.raises(ValueError, match="reason, verified"):
        creds.load_policy(root=root)


def test_a_credentials_pin_selects_on_paths_not_packages(tmp_path: Path) -> None:
    """Naming the licence gate's selector here is a typo that would
    otherwise pin nothing at all."""
    root = _write(
        tmp_path,
        "[credentials]\n[[credentials.pin]]\n"
        'packages = ["x"]\naccept = ["*"]\nreason = "r"\nverified = "2026-01-15"\n',
    )
    with pytest.raises(ValueError, match="packages"):
        creds.load_policy(root=root)


def test_a_complete_pin_loads_in_every_gate(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "[licenses]\n"
        'allowed_categories = ["permissive"]\n'
        "[[licenses.pin]]\n"
        'packages = ["certifi"]\naccept = ["MPL-2.0"]\n'
        'reason = "CA bundle"\nverified = "2026-01-15"\n\n'
        "[packages]\n[[packages.pin]]\n"
        'packages = ["some-tool"]\naccept = ["orphan"]\n'
        'reason = "profiling"\nverified = "2026-01-15"\n\n'
        "[credentials]\n[[credentials.pin]]\n"
        'paths = ["tests/fixtures/*.pem"]\naccept = ["private-key-file"]\n'
        'reason = "test fixtures"\nverified = "2026-01-15"\n',
    )
    assert lic.load_policy(root=root).pin_for("certifi") is not None
    assert pkg.load_policy(root=root).accepts("some-tool", "orphan")
    assert creds.load_policy(root=root).accepts(
        "tests/fixtures/server.pem", "private-key-file"
    )


# --- a second pin over one subject, in all three gates ---------------------
#
# The bug these guard: every gate's lookup returned the *first* pin whose
# selector matched, so a path or a package an earlier entry already named
# could never be widened by a later one. The second pin read as a decision
# and settled nothing — the same failure `check_pins` refuses for a
# misspelled key, arriving through a spelling that is correct.


def test_a_later_credential_pin_widens_a_path_an_earlier_one_names() -> None:
    policy = creds.Policy(
        pins=(
            {"paths": ["tests/*.py"], "accept": ["assigned-secret"]},
            {"paths": ["tests/*.py"], "accept": ["password-shape"]},
        )
    )
    assert policy.accepts("tests/test_config.py", "assigned-secret")
    assert policy.accepts("tests/test_config.py", "password-shape")
    assert not policy.accepts("tests/test_config.py", "private-key-block")


def test_the_credential_pin_on_record_is_the_one_that_accepted() -> None:
    """Which reason gets recorded matters: it is what a reader is shown
    as the argument for this finding being settled."""
    policy = creds.Policy(
        pins=(
            {"paths": ["a.py"], "accept": ["assigned-secret"], "reason": "fixtures"},
            {"paths": ["a.py"], "accept": ["password-shape"], "reason": "model names"},
        )
    )
    first = policy.pin_for("a.py", "assigned-secret")
    second = policy.pin_for("a.py", "password-shape")
    assert first is not None and first["reason"] == "fixtures"
    assert second is not None and second["reason"] == "model names"
    assert policy.pin_for("a.py", "private-key-block") is None


def test_a_later_package_pin_widens_a_name_an_earlier_one_names() -> None:
    policy = pkg.Policy(
        pins=(
            {"packages": ["filelock"], "accept": ["orphan"]},
            {"packages": ["FileLock"], "accept": ["undeclared"]},
        )
    )
    # The second pin spells it with different case; normalisation makes them
    # one name, which is exactly how a second pin ends up shadowed.
    assert policy.accepts("filelock", "orphan")
    assert policy.accepts("filelock", "undeclared")
    assert not policy.accepts("filelock", "squatted")


def test_a_later_licence_pin_widens_a_package_an_earlier_one_names() -> None:
    """Two readings of one package's licence — an older release pinned to
    one expression, a newer to another — both have to count."""
    policy = lic.Policy(
        allowed=frozenset({"MIT"}),
        pins=(
            {
                "packages": ["chardet"],
                "accept": ["LGPL-2.1"],
                "reason": "r",
                "verified": "2026-01-01",
            },
            {
                "packages": ["chardet"],
                "accept": ["MIT"],
                "reason": "r",
                "verified": "2026-01-01",
            },
        ),
    )
    assert policy.pin_for("chardet", "MIT") is not None
    assert policy.pin_for("chardet", "LGPL-2.1") is not None
    assert policy.pin_for("chardet", "AGPL-3.0") is None
    assert len(policy.pins_for("chardet")) == 2
