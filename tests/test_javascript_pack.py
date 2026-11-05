"""Optional Node pack integration, including an offline installed npm artifact.

Python-only contributors can run the core suite without Node. Install the
pack's declared dependencies explicitly to run these tests; no test fetches or
installs anything from the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vibe_sentinel import engine
from vibe_sentinel.inventory import compare
from vibe_sentinel.templates import load_probe_catalog

PACK = Path(__file__).parents[1] / "packs/javascript"


@pytest.fixture(scope="module")
def node_pack() -> Path:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional JavaScript pack requires Node >=22")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True
    ).stdout
    if int(version.lstrip("v").split(".", 1)[0]) < 22:
        pytest.skip("Optional JavaScript pack requires Node >=22")
    if not (PACK / "node_modules/typescript/package.json").is_file():
        pytest.skip(
            "Install declared JavaScript pack dependencies with npm ci --ignore-scripts --no-audit --no-fund in packs/javascript"
        )
    return PACK


def project(root: Path) -> None:
    (root / "package.json").write_text(
        json.dumps(
            {"name": "fixture-root", "private": True, "workspaces": ["packages/*"]}
        )
    )
    ui = root / "packages/ui"
    api = root / "packages/api"
    (ui / "src").mkdir(parents=True)
    (api / "src").mkdir(parents=True)
    (ui / "package.json").write_text(
        json.dumps(
            {
                "name": "@fixture/ui",
                "version": "1.0.0",
                "dependencies": {"@fixture/api": "workspace:*"},
            }
        )
    )
    (api / "package.json").write_text(
        json.dumps({"name": "@fixture/api", "version": "1.0.0", "main": "src/index.ts"})
    )
    (api / "src/index.ts").write_text("export const value = 1;\n")
    (ui / "src/helper.ts").write_text("export const value = 2;\n")
    (ui / "src/alternate.ts").write_text("export const value = 3;\n")
    (ui / "src/main.tsx").write_text(
        'import {value} from "./helper";\nexport const view = <div>{value}</div>;\n'
    )


def catalog(root: Path, pack: Path):
    (root / ".vibe-sentinel.toml").write_text(
        "[probes]\nuse_builtins=false\n[packs]\nmanifests=["
        + json.dumps(str(pack / "pack.toml"))
        + "]\n"
    )
    return load_probe_catalog(project_root=root)


def test_count_preserving_import_rewire_reaches_python_comparison(
    tmp_path: Path, node_pack: Path
) -> None:
    project(tmp_path)
    loaded = catalog(tmp_path, node_pack)
    before = engine.scan(loaded.probes, tmp_path)
    assert all(result.ok for result in before.probes.values()), before.model_dump_json()
    imports = before.probes["pack:javascript:imports"]
    edges = [
        observation
        for observation in imports.observations
        if observation.key.startswith("edge:1:")
    ]
    assert edges
    assert any(
        observation.attrs["target"] == "file:packages/ui/src/helper.ts"
        for observation in edges
    )
    for observation in edges:
        attrs = observation.attrs
        identity = [
            attrs[key]
            for key in ("ecosystem", "source", "target", "kind", "phase", "resolution")
        ]
        digest = hashlib.sha256(
            json.dumps(identity, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        assert observation.key == f"edge:1:{digest}"
        assert attrs["edge_schema"] == "1"
    path = tmp_path / "packages/ui/src/main.tsx"
    path.write_text(path.read_text().replace('"./helper"', '"./alternate"'))
    after = engine.scan(loaded.probes, tmp_path)
    assert all(result.ok for result in after.probes.values()), after.model_dump_json()
    new_edges = [
        observation
        for observation in after.probes["pack:javascript:imports"].observations
        if observation.key.startswith("edge:1:")
    ]
    assert len(new_edges) == len(edges)
    changed = compare(before, after, loaded.probes)
    edge_changes = [
        change for change in changed.changes if change.key.startswith("edge:1:")
    ]
    assert {change.kind for change in edge_changes} == {"appeared", "disappeared"}


def test_computed_import_gap_cannot_look_like_clean_removed_edges(
    tmp_path: Path, node_pack: Path
) -> None:
    project(tmp_path)
    loaded = catalog(tmp_path, node_pack)
    before = engine.scan(loaded.probes, tmp_path)
    (tmp_path / "packages/ui/src/main.tsx").write_text(
        'const selected = "./helper";\nexport const view = import(selected);\n'
    )
    after = engine.scan(loaded.probes, tmp_path)
    assert not after.probes["pack:javascript:imports"].ok
    report = compare(before, after, loaded.probes)
    assert "pack:javascript:imports" in report.unmeasured
    assert not any(
        change.probe_id == "pack:javascript:imports" for change in report.changes
    )


def test_install_from_npm_tarball_runs_independently(
    tmp_path: Path, node_pack: Path
) -> None:
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("Optional npm artifact smoke requires npm")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = dict(
        os.environ,
        NODE_PATH="",
        npm_config_cache=str(tmp_path / "cache"),
        npm_config_update_notifier="false",
    )

    def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [npm, *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )

    packed = json.loads(
        run(
            "pack",
            "--ignore-scripts",
            "--offline",
            "--json",
            "--pack-destination",
            str(artifacts),
            cwd=node_pack,
        ).stdout
    )[0]
    filenames = {file["path"] for file in packed["files"]}
    assert {
        "pack.toml",
        "bin/vibe-sentinel-javascript.js",
        "README.md",
        "LICENSE",
    } <= filenames
    assert any(path.startswith("src/") for path in filenames)
    assert not any(path.startswith(("node_modules/", "test/")) for path in filenames)
    # Repack the already-installed dependency locally. A fresh offline cache
    # proves the smoke never depends on registry access or a host npm cache.
    parser = json.loads(
        run(
            "pack",
            "--ignore-scripts",
            "--offline",
            "--json",
            "--pack-destination",
            str(artifacts),
            cwd=node_pack / "node_modules/typescript",
        ).stdout
    )[0]
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "package.json").write_text('{"private":true}')
    run(
        "install",
        "--prefix",
        str(consumer),
        "--global=false",
        "--offline",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        str(artifacts / packed["filename"]),
        str(artifacts / parser["filename"]),
        cwd=consumer,
    )
    installed = consumer / "node_modules/@vibe-sentinel/javascript"
    assert (installed / "pack.toml").is_file()
    assert (consumer / "node_modules/typescript/package.json").is_file()
    application = tmp_path / "application"
    application.mkdir()
    project(application)
    loaded = catalog(application, installed)
    assert all(probe.command[0].startswith(str(installed)) for probe in loaded.probes)
    snapshot = engine.scan(loaded.probes, application)
    assert all(result.ok for result in snapshot.probes.values()), (
        snapshot.model_dump_json()
    )
    assert any(
        observation.key.startswith("edge:1:")
        for observation in snapshot.probes["pack:javascript:imports"].observations
    )


def test_workspace_alias_and_type_edges_keep_declared_component_scope(
    tmp_path: Path, node_pack: Path
) -> None:
    project(tmp_path)
    (tmp_path / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "baseUrl": ".",
                    "paths": {"@ui/*": ["packages/ui/src/*"]},
                },
                "include": ["packages/*/src/**/*"],
            }
        )
    )
    (tmp_path / "packages/ui/src/helper.ts").write_text(
        "export interface Thing { value: number }\n"
    )
    (tmp_path / "packages/ui/src/main.tsx").write_text(
        'import {value} from "@fixture/api";\n'
        'import type {Thing} from "@ui/helper";\n'
        'export type {Thing as Shared} from "@ui/helper";\n'
        "export const view = <div>{value}</div>;\n"
    )
    unrelated = tmp_path / "examples/unselected"
    unrelated.mkdir(parents=True)
    (unrelated / "package.json").write_text('{"name":"unselected"}')
    (unrelated / "index.ts").write_text('const target = "unknown"; import(target);\n')
    loaded = catalog(tmp_path, node_pack)
    snapshot = engine.scan(loaded.probes, tmp_path)
    assert all(result.ok for result in snapshot.probes.values()), (
        snapshot.model_dump_json()
    )
    components = snapshot.probes["pack:javascript:workspaces"].observations
    names = {observation.attrs.get("name") for observation in components}
    assert {"fixture-root", "@fixture/api", "@fixture/ui"} <= names
    assert "unselected" not in names
    edges = snapshot.probes["pack:javascript:imports"].observations
    assert any(
        observation.attrs.get("resolution") == "workspace" for observation in edges
    )
    assert {
        observation.attrs.get("kind")
        for observation in edges
        if observation.attrs.get("phase") == "type"
    } == {"import", "reexport"}
