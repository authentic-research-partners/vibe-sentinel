"""Explicit pack metadata and ecosystem evidence contracts, without execution."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from vibe_sentinel.pack_schemas import (
    InventoryFindingIdentity,
    InventoryPin,
    InventorySubject,
)
from vibe_sentinel.packs import (
    PackError,
    inventory_subject,
    finding_key,
    load_packs,
    parse_inventory,
    pin_matches,
    subject_key,
)


def manifest(
    root: Path,
    *,
    name: str = "sample",
    executable: str | None = None,
    filename: str = "pack.toml",
) -> Path:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""protocol = 1
id = {json.dumps(name)}
version = "1.2.3-beta+fixture"
ecosystems = ["npm", "python"]
supported_inputs = ["package.json", "pyproject.toml"]
[[executable]]
id = "runtime"
command = {json.dumps(executable or sys.executable)}
[[probe]]
id = "shape"
title = "Fixture"
command = ["runtime", "-m", "example_reader", "--root", "{{ROOT}}"]
[[probe.placeholders]]
name = "ROOT"
description = "Project root"
default = "."
[[reader]]
id = "inventory"
title = "Inventory fixture"
ecosystem = "npm"
supported_inputs = ["package.json"]
command = ["runtime", "-m", "example_reader", "--inventory"]
""")
    return path


def payload() -> dict:
    return {
        "protocol": 1,
        "pack_id": "sample",
        "pack_version": "1.2.3-beta+fixture",
        "reader_id": "inventory",
        "ecosystem": "npm",
        "workspace": {"id": "workspace-one", "root": "."},
        "environment": {
            "id": "install-one",
            "kind": "node_modules",
            "runtime": "node 24.0.0",
        },
        "selection": {
            "id": "production",
            "basis": "installed",
            "groups": [],
            "extras": [],
        },
        "coverage": {
            "status": "complete",
            "facets": ["packages", "dependencies", "licenses", "origin"],
            "gaps": [],
        },
        "packages": [
            {
                "instance": "root/shared",
                "name": "@scope/shared",
                "version": "1.0.0",
                "requirement": None,
                "locator": "node_modules/@scope/shared",
                "dependencies": ["nested/shared"],
                "license_expression": "MIT",
                "origin": "registry",
            },
            {
                "instance": "nested/shared",
                "name": "@scope/shared",
                "version": "2.0.0",
                "requirement": None,
                "locator": "node_modules/other/node_modules/@scope/shared",
                "dependencies": [],
                "license_expression": None,
                "origin": "unknown",
            },
        ],
    }


def test_only_explicit_manifests_load_and_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("Loading a pack executed a command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    (tmp_path / "discovered.toml").write_text("invalid discovered file")
    assert load_packs(tmp_path, []) == ()
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    assert pack.identity.id == "sample"
    assert pack.probes[0]["id"] == "pack:sample:shape"
    assert pack.probes[0]["source"] == "pack:sample@1.2.3-beta+fixture"
    assert pack.probes[0]["command"][:3] == [sys.executable, "-m", "example_reader"]
    assert pack.readers[0]["id"] == "inventory"
    assert len(pack.manifest_sha256) == 64


def test_executable_paths_are_relative_to_manifest_and_preserve_symlink(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "pack"
    directory.mkdir()
    linked = directory / "runtime"
    linked.symlink_to(sys.executable)
    manifest(tmp_path, filename="pack/pack.toml", executable="./runtime")
    (pack,) = load_packs(tmp_path, ["pack/pack.toml"])
    assert pack.executables[0].path == str(linked)
    assert pack.probes[0]["command"][0] == str(linked)
    assert pack.probes[0]["command"][-1] == "{ROOT}"


def test_bare_executable_is_resolved_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    monkeypatch.setattr(
        shutil,
        "which",
        lambda value: sys.executable if value == "fixture-runtime" else None,
    )
    manifest(tmp_path, executable="fixture-runtime")
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    assert pack.executables[0].path == sys.executable
    assert pack.executables[0].command == "fixture-runtime"


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("protocol = 1", "protocol = 2"),
        lambda text: text.replace("protocol = 1", "protocol = true"),
        lambda text: text.replace('id = "shape"', 'id = "pack:fake:shape"'),
        lambda text: text.replace(
            'command = ["runtime", "-m", "example_reader", "--inventory"]',
            'command = "runtime --inventory"',
        ),
        lambda text: text.replace(
            'command = ["runtime", "-m", "example_reader", "--inventory"]',
            'command = ["undeclared", "--inventory"]',
        ),
        lambda text: text.replace('ecosystem = "npm"', 'ecosystem = "unlisted"'),
        lambda text: text.replace(
            'title = "Fixture"', 'title = "Fixture"\nsource = "built-in"'
        ),
    ],
)
def test_incompatible_or_ambiguous_manifest_fails_explicitly(
    tmp_path: Path, change
) -> None:
    path = manifest(tmp_path)
    path.write_text(change(path.read_text()))
    with pytest.raises(PackError):
        load_packs(tmp_path, ["pack.toml"])


def test_duplicates_and_missing_executables_are_errors(tmp_path: Path) -> None:
    manifest(tmp_path)
    with pytest.raises(PackError, match="more than once"):
        load_packs(tmp_path, ["pack.toml", "./pack.toml"])
    manifest(tmp_path, filename="second.toml")
    with pytest.raises(PackError, match="IDs must be unique"):
        load_packs(tmp_path, ["pack.toml", "second.toml"])
    manifest(tmp_path, executable="./missing-binary")
    with pytest.raises(PackError, match="Install it manually"):
        load_packs(tmp_path, ["pack.toml"])


def test_manifest_bounds_and_sanitized_errors(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(PackError, match="1 MiB"):
        load_packs(tmp_path, ["pack.toml"])
    private = uuid4().hex
    path.write_text(f'unknown_private_setting = "{private}"\n')
    with pytest.raises(PackError) as raised:
        load_packs(tmp_path, ["pack.toml"])
    assert private not in str(raised.value)
    with pytest.raises(PackError, match="local file paths"):
        load_packs(tmp_path, ["https://example.invalid/pack.toml"])


def test_inventory_keeps_versions_and_instances_distinct(tmp_path: Path) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    inventory = parse_inventory(payload(), pack, "inventory")
    assert len(inventory.packages) == 2
    first = inventory_subject(inventory, "root/shared")
    second = inventory_subject(inventory, "nested/shared")
    assert first.name == second.name
    assert first.version != second.version
    assert subject_key(first) != subject_key(second)
    assert subject_key(first).startswith("inventory:1:npm:")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.pop("protocol"),
        lambda data: data.update(protocol=True),
        lambda data: data.update(protocol=2),
        lambda data: data.update(pack_version="9.0.0"),
        lambda data: data.update(pack_id="other"),
        lambda data: data.update(ecosystem="python"),
        lambda data: data.pop("coverage"),
        lambda data: data["coverage"].update(
            gaps=[{"code": "not_measured", "scope": "all"}]
        ),
        lambda data: data["coverage"].update(status="incomplete"),
        lambda data: data["packages"].append(copy.deepcopy(data["packages"][0])),
        lambda data: data["packages"][0].update(dependencies=["missing"]),
        lambda data: data["packages"][0].update(version=None),
        lambda data: data["selection"].update(basis="declared"),
        lambda data: data["coverage"].update(facets=["packages"]),
    ],
)
def test_bad_or_overclaimed_inventory_is_not_a_clean_empty_result(
    tmp_path: Path, mutate
) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    data = payload()
    mutate(data)
    with pytest.raises(PackError, match="Invalid or incompatible"):
        parse_inventory(data, pack, "inventory")


def test_incomplete_and_declared_inventory_are_explicit(tmp_path: Path) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    data = payload()
    data["selection"]["basis"] = "declared"
    for package in data["packages"]:
        package.update(version=None, requirement="^1.0.0")
    data["coverage"].update(
        status="incomplete",
        gaps=[{"code": "dynamic_resolution", "scope": "workspace-two"}],
    )
    inventory = parse_inventory(data, pack, "inventory")
    assert inventory.coverage.status == "incomplete"
    assert inventory.selection.basis == "declared"
    assert inventory.packages[0].version is None
    assert inventory.packages[0].requirement == "^1.0.0"


def _licensed(subject: InventorySubject) -> InventoryFindingIdentity:
    return InventoryFindingIdentity(gate="licenses", kind="license", subject=subject)


def test_exact_pin_never_broadens_to_ecosystem_version_or_instance(
    tmp_path: Path,
) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    inventory = parse_inventory(payload(), pack, "inventory")
    subject = inventory_subject(inventory, "root/shared")
    exact = InventoryPin(
        accept=True,
        verified="2026-11-03",
        gate="licenses",
        kind="license",
        scope="exact-instance",
        ecosystem="npm",
        name=subject.name,
        subject=subject,
        reason="Reviewed this exact instance",
    )
    assert pin_matches(exact, _licensed(subject))
    for field, value in (
        ("ecosystem", "python"),
        ("version", "2.0.0"),
        ("locator", "other/path"),
        ("instance", "another"),
        ("name", "different"),
    ):
        assert not pin_matches(
            exact, _licensed(subject.model_copy(update={field: value}))
        )
    assert not pin_matches(
        exact,
        _licensed(
            subject.model_copy(
                update={
                    "environment": subject.environment.model_copy(
                        update={"id": "another-env"}
                    )
                }
            )
        ),
    )
    assert not pin_matches(
        exact,
        _licensed(
            subject.model_copy(
                update={
                    "workspace": subject.workspace.model_copy(
                        update={"id": "another-workspace"}
                    )
                }
            )
        ),
    )
    assert not pin_matches(
        exact,
        _licensed(
            subject.model_copy(
                update={
                    "selection": subject.selection.model_copy(
                        update={"groups": ("dev",)}
                    )
                }
            )
        ),
    )
    broad = InventoryPin(
        accept=True,
        verified="2026-11-03",
        gate="licenses",
        kind="license",
        scope="ecosystem-package",
        ecosystem="npm",
        name=subject.name,
        subject=None,
        reason="Accept all npm instances explicitly",
    )
    assert pin_matches(broad, _licensed(inventory_subject(inventory, "nested/shared")))
    assert not pin_matches(
        broad, _licensed(subject.model_copy(update={"ecosystem": "python"}))
    )
    assert not pin_matches(
        broad, _licensed(subject.model_copy(update={"name": "@Scope/shared"}))
    )
    with pytest.raises(ValidationError):
        InventoryPin(
            accept=True,
            verified="2026-11-03",
            gate="licenses",
            kind="license",
            scope="ecosystem-package",
            ecosystem="npm",
            name=subject.name,
            subject=None,
            reason=" ",
        )
    with pytest.raises(ValidationError):
        InventoryPin(
            accept=True,
            verified="2026-11-03",
            gate="licenses",
            kind="license",
            scope="exact-instance",
            ecosystem="npm",
            name=subject.name,
            subject=None,
            reason="Missing scope",
        )


def test_selection_set_order_does_not_rekey_identical_evidence(tmp_path: Path) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    data = payload()
    data["selection"]["groups"] = ["dev", "test"]
    subject = inventory_subject(parse_inventory(data, pack, "inventory"), "root/shared")
    reordered = subject.model_copy(
        update={
            "selection": subject.selection.model_copy(
                update={"groups": ("test", "dev")}
            )
        }
    )
    assert subject_key(subject) == subject_key(reordered)


def test_pin_scope_cannot_cross_gate_or_finding_kind(tmp_path: Path) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    subject = inventory_subject(
        parse_inventory(payload(), pack, "inventory"), "root/shared"
    )
    finding = _licensed(subject)
    pin = InventoryPin(
        accept=True,
        verified="2026-11-03",
        gate="licenses",
        kind="license",
        scope="exact-instance",
        ecosystem=subject.ecosystem,
        name=subject.name,
        subject=subject,
        reason="Reviewed exact finding",
    )
    assert pin_matches(pin, finding)
    for update in ({"gate": "packages"}, {"kind": "unrecorded-origin"}):
        other = finding.model_copy(update=update)
        assert finding_key(other) != finding_key(finding)
        assert not pin_matches(pin, other)
    for update in (
        {"accept": False},
        {"accept": 1},
        {"verified": ""},
        {"verified": "invalid"},
    ):
        with pytest.raises(ValidationError):
            InventoryPin.model_validate({**pin.model_dump(mode="json"), **update})


@pytest.mark.parametrize(
    "section,field",
    [
        ("workspace", "id"),
        ("environment", "id"),
        ("environment", "runtime"),
        ("selection", "id"),
    ],
)
def test_inventory_does_not_claim_complete_with_blank_identity(
    tmp_path: Path, section: str, field: str
) -> None:
    manifest(tmp_path)
    (pack,) = load_packs(tmp_path, ["pack.toml"])
    for invalid in (" ", "\x00"):
        data = payload()
        data[section][field] = invalid
        with pytest.raises(PackError):
            parse_inventory(data, pack, "inventory")
    data = payload()
    data["packages"][0]["version"] = " "
    with pytest.raises(PackError):
        parse_inventory(data, pack, "inventory")
