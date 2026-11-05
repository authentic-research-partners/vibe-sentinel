"""The measured interpreter can differ from the scanner without importing apps."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import venv
from pathlib import Path

import pytest

from vibe_sentinel import _python_inventory as reader
from vibe_sentinel import packages as pk
from vibe_sentinel.config import SentinelConfig, load_config
from vibe_sentinel.python_environment import (
    EnvironmentInspectionError,
    collect_environment,
)


@pytest.fixture
def target(tmp_path: Path) -> tuple[Path, Path, SentinelConfig]:
    prefix = tmp_path / "target"
    venv.EnvBuilder(with_pip=False).create(prefix)
    executable = prefix / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    site = prefix / (
        "Lib/site-packages"
        if sys.platform == "win32"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    site.mkdir(parents=True, exist_ok=True)
    config = SentinelConfig(environment_python=str(executable.relative_to(tmp_path)))
    return tmp_path, site, config


def distribution(site: Path, name: str = "target-only", requires: str = "") -> Path:
    metadata = site / f"{name.replace('-', '_')}-7.2.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Name: {name}\nVersion: 7.2\nLicense-Expression: MIT\n{requires}",
        encoding="utf-8",
    )
    (metadata / "WHEEL").write_text("Wheel-Version: 1.0\n", encoding="utf-8")
    (metadata / "INSTALLER").write_text("fixture\n", encoding="utf-8")
    return metadata


def test_target_without_sentinel_has_its_own_metadata_and_identity(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    metadata = distribution(site)
    (metadata / "top_level.txt").write_text("target_module\n", encoding="utf-8")
    (site / "target_module.py").write_text(
        "raise RuntimeError('must not import')\n", encoding="utf-8"
    )
    snapshot = collect_environment(root, config)
    assert [(dist.name, dist.version) for dist in snapshot.distributions] == [
        ("target-only", "7.2")
    ]
    assert snapshot.environment.prefix == str(root / "target")
    assert snapshot.environment.executable == str(root / config.environment_python)
    assert snapshot.environment.selection == "configured"
    assert snapshot.environment.scanner_executable == sys.executable
    assert snapshot.import_owners == {"target_module": ("target-only",)}
    assert "target_module" in snapshot.importable
    assert "json" in snapshot.stdlib_modules
    assert set(snapshot.marker_environment) == reader.MARKER_KEYS
    assert not any("Executable .pth" in note for note in snapshot.limitations)
    assert (
        snapshot.license_distributions()[0].metadata.get("License-Expression") == "MIT"
    )


def test_isolated_target_never_runs_startup_or_application_hooks(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    distribution(site)
    marker = root / "executed"
    trap = f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    (site / "startup.pth").write_text("import sitecustomize\n", encoding="utf-8")
    (site / "sitecustomize.py").write_text(trap, encoding="utf-8")
    (site / "usercustomize.py").write_text(trap, encoding="utf-8")
    (root / "sitecustomize.py").write_text(trap, encoding="utf-8")
    snapshot = collect_environment(root, config)
    assert snapshot.limitations
    assert not marker.exists()
    with pytest.raises(EnvironmentInspectionError, match="incomplete"):
        pk.take_inventory(root, snapshot=snapshot)
    assert not marker.exists()


def test_path_only_pth_metadata_and_modules_are_read_without_importing(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    extra = root / "editable-source"
    extra.mkdir()
    distribution(extra)
    (extra / "editable_module.py").write_text(
        "raise RuntimeError('no import')\n", encoding="utf-8"
    )
    (site / "paths.pth").write_text(str(extra) + "\n", encoding="utf-8")
    (site / "README").write_text("data file", encoding="utf-8")
    snapshot = collect_environment(root, config)
    assert len(snapshot.distributions) == 1
    assert "editable_module" in snapshot.importable
    assert "README" not in snapshot.importable
    assert not any("Executable .pth" in note for note in snapshot.limitations)


def test_licenses_are_captured_and_package_data_is_not_a_metadata_license(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    metadata = distribution(site)
    (metadata / "LICENSE").write_text("MIT licence fixture", encoding="utf-8")
    (metadata / "RECORD").write_text(
        f"{metadata.name}/LICENSE,,\npackage/LICENSE,,\n", encoding="utf-8"
    )
    snapshot = collect_environment(root, config)
    assert snapshot.distributions[0].license_files == (
        ("LICENSE", "MIT licence fixture"),
    )
    (metadata / "LICENSE").unlink()
    assert snapshot.license_distributions()[0].captured_license_files == (
        ("LICENSE", "MIT licence fixture"),
    )  # type: ignore[attr-defined]


@pytest.mark.parametrize("escape", ["traversal", "symlink"])
def test_license_metadata_cannot_read_outside_its_directory(
    target: tuple[Path, Path, SentinelConfig], escape: str
) -> None:
    root, site, config = target
    metadata = distribution(site)
    outside = site / "LICENSE"
    outside.write_text("not distribution metadata", encoding="utf-8")
    if escape == "symlink":
        (metadata / "LICENSE").symlink_to(outside)
        reference = f"{metadata.name}/LICENSE"
    else:
        reference = f"{metadata.name}/../LICENSE"
    (metadata / "RECORD").write_text(reference + ",,\n", encoding="utf-8")
    with pytest.raises(EnvironmentInspectionError, match="inspection failed"):
        collect_environment(root, config)


def test_dependency_and_origin_urls_are_sanitized_before_export(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    token = secrets.token_hex(16)
    url = f"https://reader:{token}@example.invalid/package.whl?token={token}#fragment"
    metadata = distribution(
        site,
        requires=f'Requires-Dist: child[feature] @ {url} ; python_version >= "3"\n',
    )
    (metadata / "direct_url.json").write_text(
        json.dumps({"url": url}), encoding="utf-8"
    )
    snapshot = collect_environment(root, config)
    exported = snapshot.model_dump_json()
    assert token not in exported and "fragment" not in exported
    assert snapshot.distributions[0].requires == (
        'child[feature]; python_version >= "3"',
    )
    assert (
        json.loads(snapshot.distributions[0].direct_url)["url"]
        == "https://example.invalid/package.whl"
    )


def test_selected_marker_environment_and_stdlib_override_scanner_facts(
    target: tuple[Path, Path, SentinelConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, site, config = target
    distribution(site, requires='Requires-Dist: child; python_version < "3.12"\n')
    distribution(site, name="child")
    snapshot = collect_environment(root, config)
    payload = snapshot.model_dump()
    payload["marker_environment"].update(
        python_version="3.11", python_full_version="3.11.0"
    )
    payload["stdlib_modules"] = frozenset({"sys", "builtins", "targetstdlib"})
    # This synthetic snapshot isolates marker/stdlib routing from startup hooks
    # that a particular host's Python distributor may install in the stdlib.
    payload["limitations"] = ()
    snapshot = type(snapshot).model_validate(payload)
    (root / "pyproject.toml").write_text(
        '[project]\nname="app"\ndependencies=["target-only>=1"]\n', encoding="utf-8"
    )
    (root / "app.py").write_text(
        "import targetstdlib\nimport target_module\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        pk,
        "is_importable",
        lambda name: pytest.fail("scanner importability must not be used"),
    )
    inventory = pk.take_inventory(root, snapshot=snapshot)
    assert inventory.closure == {"target-only", "child"}
    assert [imp.module for imp in inventory.external_imports] == ["target_module"]
    assert any(
        finding.kind == "phantom" for finding in pk.audit(inventory, pk.Policy())
    )
    with pytest.raises(EnvironmentInspectionError, match="incomplete"):
        pk.requirement_closure(
            {"target-only"}, inventory.installed, {"python_version": "3.11"}
        )


def test_unavailable_target_never_falls_back_to_runtime(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentInspectionError, match="could not be started"):
        collect_environment(
            tmp_path, SentinelConfig(environment_python="missing/python")
        )


def test_incomplete_protocol_is_rejected_without_echoing_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe_sentinel import python_environment as env

    token = secrets.token_hex(16)
    monkeypatch.setattr(env, "_run_reader", lambda *args: token.encode())
    with pytest.raises(EnvironmentInspectionError) as failure:
        collect_environment(tmp_path, SentinelConfig(environment_python="python"))
    assert token not in str(failure.value)
    assert failure.value.__suppress_context__


def test_configured_target_settings_are_validated(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text(
        '[environment]\npython=".venv/bin/python"\ntimeout=12\n', encoding="utf-8"
    )
    config = load_config(path)
    assert config.environment_python == ".venv/bin/python"
    assert config.environment_timeout == 12
    path.write_text('[environment]\npython=""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="nonempty"):
        load_config(path)


def test_target_owner_normalization_and_recorded_wheel_survive_adapter(
    target: tuple[Path, Path, SentinelConfig],
) -> None:
    root, site, config = target
    metadata = distribution(site, name="Mixed_Name")
    (metadata / "top_level.txt").write_text("mixed_module\n", encoding="utf-8")
    (metadata / "INSTALLER").unlink()
    snapshot = collect_environment(root, config)
    assert snapshot.import_owners == {"mixed_module": ("mixed-name",)}
    assert pk.installed_distributions(snapshot)["mixed-name"].provenance_recorded


def test_captured_multiline_license_is_not_rewritten() -> None:
    from vibe_sentinel.python_environment import (
        CapturedDistribution,
        DistributionSnapshot,
    )

    license_text = "First line\nSecond line\n  indented line"
    adapter = CapturedDistribution(
        DistributionSnapshot(name="fixture", version="1", license=license_text)
    )
    assert adapter.metadata.get("License") == license_text
    assert "\n Second line" in adapter.read_text("METADATA")


@pytest.mark.parametrize(
    "field",
    ["prefix", "executable", "kind", "name", "stdlib_modules", "marker_environment"],
)
def test_incomplete_target_identity_is_rejected(
    target: tuple[Path, Path, SentinelConfig],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    from vibe_sentinel import python_environment as env

    root, _, config = target
    payload = collect_environment(root, config).model_dump(mode="json")
    if field in {"stdlib_modules", "marker_environment"}:
        payload[field] = [] if field == "stdlib_modules" else {}
    else:
        payload["environment"][field] = ""
    monkeypatch.setattr(env, "_run_reader", lambda *args: json.dumps(payload).encode())
    with pytest.raises(EnvironmentInspectionError, match="invalid or incomplete"):
        collect_environment(root, config)


@pytest.mark.skipif(os.name != "posix", reason="POSIX wrapper process-group cleanup")
def test_reader_timeout_kills_children_holding_the_output_pipe(tmp_path: Path) -> None:
    from vibe_sentinel.python_environment import _run_reader

    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text("#!/bin/sh\nsleep 20 &\nwait\n", encoding="utf-8")
    wrapper.chmod(0o700)
    started = time.monotonic()
    with pytest.raises(EnvironmentInspectionError, match="timed out"):
        _run_reader(wrapper, tmp_path, 0.1)
    assert time.monotonic() - started < 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable transport fixture")
def test_reader_bounds_output_and_does_not_echo_child_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vibe_sentinel.python_environment import _run_reader

    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text("#!/bin/sh\nprintf 0123456789abcdef\n", encoding="utf-8")
    wrapper.chmod(0o700)
    monkeypatch.setattr(reader, "MAX_OUTPUT_BYTES", 8)
    with pytest.raises(EnvironmentInspectionError, match="output limit"):
        _run_reader(wrapper, tmp_path, 2)
    token = secrets.token_hex(16)
    wrapper.write_text(f"#!/bin/sh\nprintf {token} >&2\nexit 2\n", encoding="utf-8")
    with pytest.raises(EnvironmentInspectionError) as failure:
        _run_reader(wrapper, tmp_path, 2)
    assert token not in str(failure.value)


@pytest.mark.parametrize("include_system", [False, True])
def test_normal_venv_user_site_policy_controls_paths_and_usercustomize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_system: bool,
) -> None:
    prefix, base, user = (tmp_path / name for name in ("venv", "base", "user"))
    for path in (prefix, base, user):
        (path / "site-packages").mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text(
        f"include-system-site-packages = {str(include_system).lower()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(reader.sys, "executable", str(prefix / "bin/python"))
    monkeypatch.setattr(reader.sys, "prefix", str(base))
    monkeypatch.setattr(reader.sys, "base_prefix", str(base))
    monkeypatch.setattr(reader.sys, "base_exec_prefix", str(base))
    monkeypatch.setattr(reader.sys, "path", [])
    monkeypatch.setattr(
        reader.site,
        "getsitepackages",
        lambda prefixes: [str(Path(path) / "site-packages") for path in prefixes],
    )
    monkeypatch.setattr(
        reader.site, "getusersitepackages", lambda: str(user / "site-packages")
    )
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    (prefix / "site-packages/usercustomize.py").write_text(
        "raise RuntimeError('must not run')\n", encoding="utf-8"
    )
    distribution(user / "site-packages", name="user-only")
    _, paths, limits = reader.target_paths()
    expected = [prefix / "site-packages"]
    if include_system:
        expected.extend((user / "site-packages", base / "site-packages"))
    assert paths == expected
    assert bool(limits) is include_system
    assert [dist["name"] for dist in reader.collect_target()["distributions"]] == (
        ["user-only"] if include_system else []
    )
    (prefix / "site-packages/sitecustomize.py").write_text(
        "raise RuntimeError('must not run')\n", encoding="utf-8"
    )
    assert reader.target_paths()[2], (
        "sitecustomize applies even when user site is disabled"
    )


@pytest.mark.parametrize(
    ("setting", "allowed"), [("", True), ("0", True), ("1", False), ("yes", False)]
)
def test_normal_user_site_environment_flag_is_not_the_helpers_forced_isolation(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    allowed: bool,
) -> None:
    monkeypatch.setenv("PYTHONNOUSERSITE", setting)
    assert reader._normal_user_site_allowed() is allowed
