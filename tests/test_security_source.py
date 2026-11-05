"""Local source adapter protocol and optional real Semgrep rule fixtures."""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from vibe_sentinel import security_source as source

UNSAFE = """
const cp = require("node:child_process");
const fs = require("node:fs");
function handler(req) {
  cp.exec(req.query.command);
  fs.readFileSync(req.params.filename);
}
document.body.innerHTML = window.location.hash;
"""
SAFE = """
const cp = require("node:child_process");
const fs = require("node:fs");
function handler(req) {
  cp.execFile("echo", [req.query.message]);
  fs.readFileSync("config.json");
}
document.body.textContent = window.location.hash;
document.body.innerHTML = "<b>static</b>";
"""
JSX_UNSAFE = """const cp = require("node:child_process");
const fs = require("node:fs");
const handler = (req: any) => {
  cp.exec(req.query.command);
  fs.readFileSync(req.params.filename);
  return <p>Research & Arts {document.body.innerHTML = window.location.hash}</p>;
};
"""


@pytest.fixture
def trusted_typescript() -> Path:
    """Opt-in real parser tests use an explicit trusted tool, never project modules."""
    value = os.environ.get("SENTINEL_TEST_TYPESCRIPT")
    if not value or shutil.which("node") is None:
        pytest.skip("Set SENTINEL_TEST_TYPESCRIPT to the managed TypeScript module")
    path = Path(value)
    assert path.is_absolute() and path.is_file()
    return path


def fake_tool(
    monkeypatch: pytest.MonkeyPatch, payload: Any, code: int = 0
) -> list[Any]:
    calls: list[Any] = []
    monkeypatch.setattr(source.shutil, "which", lambda _: "/trusted/semgrep")

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls.append((argv, kwargs))
            kwargs["stdout"].write(json.dumps(payload).encode())

        def wait(self, timeout: float | None = None) -> int:
            return code

        def poll(self) -> int:
            return code

    monkeypatch.setattr(source.subprocess, "Popen", Process)
    return calls


def payload(*, results: list | None = None, errors: list | None = None) -> dict:
    return {
        "results": results or [],
        "errors": errors or [],
        "paths": {"scanned": ["source-0.js"]},
    }


def finding(**changes: Any) -> dict:
    return {
        "check_id": "browser-url-to-html",
        "path": "source-0.js",
        "start": {"line": 3},
        "extra": {"message": "SECRET TOOL TEXT", "lines": "SECRET SOURCE"},
        **changes,
    }


def test_missing_tool_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source.shutil, "which", lambda _: None)
    assert source.check(tmp_path, ".")["status"] == "unavailable"


def test_protocol_redacts_tool_text_and_isolates_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.js").write_text(UNSAFE)
    (tmp_path / ".semgrepignore").write_text("app.js")
    monkeypatch.setenv("SEMGREP_RULES", "https://untrusted.invalid/rules")
    monkeypatch.setenv("SEMGREP_APP_TOKEN", "SECRET")
    calls = fake_tool(monkeypatch, payload(results=[finding(), finding()]))
    result = source.check(tmp_path, ".")
    assert result["status"] == "findings"
    assert "jsx_input_adapter" not in result["coverage"]
    assert "jsx_text_encodings" not in result["coverage"]
    assert len(result["findings"]) == 1
    item = result["findings"][0]
    assert item["id"].startswith("browser-url-to-html:")
    assert item["rule"] == "browser-url-to-html"
    assert item["path"] == "app.js"
    assert item["line"] == 3
    assert "SECRET" not in json.dumps(result)
    argv, options = calls[0]
    assert argv[argv.index("--config") + 1] == str(source.RULES)
    assert {
        "--oss-only",
        "--metrics=off",
        "--disable-version-check",
        "--disable-nosem",
    } <= set(argv)
    assert "SEMGREP_RULES" not in options["env"]
    assert "SEMGREP_APP_TOKEN" not in options["env"]
    assert options["cwd"] != tmp_path
    assert options["start_new_session"] is True


@pytest.mark.parametrize(
    "data",
    [payload(errors=[{"message": "SECRET"}]), {**payload(), "paths": {"scanned": []}}],
)
def test_partial_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict
) -> None:
    (tmp_path / "app.js").write_text(UNSAFE)
    fake_tool(monkeypatch, data)
    result = source.check(tmp_path, ".")
    assert result["status"] == "error"
    assert result["coverage"]["partial"]
    assert "SECRET" not in json.dumps(result)


def partial_parsing(*locations: dict, **changes: Any) -> dict:
    return {
        "code": 3,
        "type": ["PartialParsing", list(locations)],
        "message": "SECRET SOURCE AND TOOL PROSE",
        **changes,
    }


def test_partial_parsing_reports_only_validated_locations_and_keeps_findings(
    tmp_path: Path,
) -> None:
    location = {"path": "source-0.js", "start": {"line": 7}, "text": "SECRET"}
    second = {"path": "source-1.tsx", "start": {"line": 11}}
    data = payload(
        results=[finding()],
        errors=[partial_parsing(location, second, location), {"message": "SECRET"}],
    )
    data["paths"]["scanned"].append("source-1.tsx")
    result = source._parse(
        data, tmp_path, {"source-0.js": "app.js", "source-1.tsx": "src/page.tsx"}
    )
    assert result["status"] == "error"
    assert len(result["findings"]) == 1
    assert "partial parsing affects 2 files" in result["summary"]
    assert result["coverage"] == {
        "files": 2,
        "scanned": 2,
        "partial": True,
        "partial_parse_files": 2,
        "partial_parse_locations": [
            {"path": "app.js", "line": 7},
            {"path": "src/page.tsx", "line": 11},
        ],
    }
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize(
    "error",
    [
        partial_parsing({"path": "../SECRET.js", "start": {"line": 1}}),
        partial_parsing({"path": "unknown-SECRET.js", "start": {"line": 1}}),
        partial_parsing({"path": "source-0.js", "start": {"line": True}}),
        partial_parsing({"path": "source-0.js", "start": {"line": 0}}),
        partial_parsing({"path": "source-0.js", "start": {"line": "SECRET"}}),
        partial_parsing(
            {"path": "source-0.js", "start": {"line": source.MAX_FILE_BYTES + 1}}
        ),
        partial_parsing({"path": "source-0.js", "start": {"line": 1}}, code=True),
        partial_parsing({"path": "source-0.js", "start": {"line": 1}}, code=999),
        partial_parsing(type=["SECRET", []]),
        partial_parsing(type=["PartialParsing", "SECRET"]),
        partial_parsing(type=["PartialParsing", ["SECRET"]]),
    ],
)
def test_untrusted_partial_diagnostics_remain_opaque_errors(
    tmp_path: Path, error: dict
) -> None:
    result = source._parse(
        payload(results=[finding()], errors=[error]),
        tmp_path,
        {"source-0.js": "app.js"},
    )
    assert result["status"] == "error"
    assert len(result["findings"]) == 1
    assert result["coverage"]["partial_parse_files"] == 0
    assert result["coverage"]["partial_parse_locations"] == []
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize(
    "entry",
    [
        finding(path="../outside.js"),
        finding(check_id="foreign"),
        finding(start={"line": True}),
        finding(start={"line": 0}),
    ],
)
def test_invalid_tool_locations_and_rules_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: dict
) -> None:
    (tmp_path / "app.js").write_text(UNSAFE)
    fake_tool(monkeypatch, payload(results=[entry]))
    assert source.check(tmp_path, ".")["status"] == "error"


def test_process_failure_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.js").write_text(UNSAFE)
    fake_tool(monkeypatch, payload(), code=2)
    assert source.check(tmp_path, ".")["status"] == "error"


def test_limits_and_outside_source_are_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.js").write_text(UNSAFE)
    calls = fake_tool(monkeypatch, payload())
    monkeypatch.setattr(source, "MAX_FILE_BYTES", 2)
    assert source.check(tmp_path, ".")["status"] == "error"
    assert source.check(tmp_path, "..")["status"] == "error"
    assert not calls


def test_symlink_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "app.js").symlink_to(tmp_path / "target.txt")
    (tmp_path / "target.txt").write_text(UNSAFE)
    calls = fake_tool(monkeypatch, payload())
    assert source.check(tmp_path, ".")["status"] == "error"
    assert not calls


def test_linked_directory_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "safe.js").write_text(SAFE)
    (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
    calls = fake_tool(monkeypatch, payload())
    assert source.check(tmp_path, ".")["status"] == "error"
    assert not calls


def test_snapshot_excludes_dependencies_and_copies_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.ts").write_text(SAFE)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "dep.js").write_text(UNSAFE)
    target = tmp_path / "snapshot"
    target.mkdir()
    assert source._snapshot(project, ".", target) == {"source-0.ts": "app.ts"}
    assert (target / "source-0.ts").read_text() == SAFE


@pytest.mark.skipif(
    shutil.which("semgrep") is None, reason="Semgrep CE is optional and not installed"
)
@pytest.mark.parametrize("extension", [".js", ".ts"])
def test_real_rules_safe_and_unsafe(tmp_path: Path, extension: str) -> None:
    app = tmp_path / f"app{extension}"
    app.write_text(UNSAFE)
    result = source.check(tmp_path, ".")
    assert result["status"] == "findings", result
    assert {f["rule"] for f in result["findings"]} == set(source.MESSAGES)
    app.write_text(SAFE)
    assert source.check(tmp_path, ".")["status"] == "complete"


def test_empty_scope_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_tool(monkeypatch, payload())
    assert source.check(tmp_path, ".")["status"] == "unavailable"
    assert not calls


@pytest.mark.parametrize("control", ["\x1b", "\x9b", "\u202e"])
def test_control_char_path_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    (tmp_path / f"unsafe{control}.js").write_text(UNSAFE)
    calls = fake_tool(monkeypatch, payload())
    assert source.check(tmp_path, ".")["status"] == "error"
    assert not calls


def test_identity_distinguishes_locations(tmp_path: Path) -> None:
    mapping = {"source-0.js": "one.js", "source-1.js": "two.js"}
    data = payload(
        results=[finding(), finding(start={"line": 4}), finding(path="source-1.js")]
    )
    data["paths"]["scanned"].append("source-1.js")
    result = source._parse(data, tmp_path, mapping)
    assert len({f["id"] for f in result["findings"]}) == 3
    assert result == source._parse(data, tmp_path, mapping)


def test_oversize_output_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.js").write_text(SAFE)
    fake_tool(monkeypatch, payload())
    monkeypatch.setattr(source, "MAX_OUTPUT_BYTES", 2)
    assert source.check(tmp_path, ".")["status"] == "error"


def test_runtime_limit_terminates_tool_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.js").write_text(SAFE)
    monkeypatch.setattr(source.shutil, "which", lambda _: "/trusted/semgrep")
    ticks = iter([0, 91])
    monkeypatch.setattr(source.time, "monotonic", lambda: next(ticks))
    killed = []
    monkeypatch.setattr(source.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    class Process:
        pid = 1234

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            return -9

    monkeypatch.setattr(source.subprocess, "Popen", Process)
    assert source.check(tmp_path, ".")["status"] == "error"
    assert killed == [(1234, source.signal.SIGKILL)]


@pytest.mark.parametrize(
    "edits",
    [
        [],
        {"unexpected.tsx": []},
        {"source-0.tsx": "SECRET"},
        {"source-0.tsx": [True]},
        {"source-0.tsx": [-1]},
        {"source-0.tsx": [999]},
        {"source-0.tsx": [0]},
        {"source-0.tsx": [5, 5]},
    ],
)
def test_jsx_helper_rejects_invalid_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edits: Any
) -> None:
    original = b"<p>A & B</p>"
    (tmp_path / "source-0.tsx").write_bytes(original)

    def invoke(argv: Any, target: Any, env: Any, output: Any, stdin: Any) -> int:
        output.write(json.dumps(edits).encode())
        return 0

    monkeypatch.setattr(source, "_invoke", invoke)
    with pytest.raises(ValueError):
        source._jsx_input(
            tmp_path, {"source-0.tsx": "app.tsx"}, {}, Path("node"), Path("ts.js")
        )
    assert (tmp_path / "source-0.tsx").read_bytes() == original


def test_jsx_helper_preserves_every_byte_except_selected_ampersands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"\xef\xbb\xbf<p>\xc3\xa9 & B\r\n& C</p>"
    (tmp_path / "source-0.tsx").write_bytes(original)
    offsets = [index for index, value in enumerate(original) if value == ord("&")]

    def invoke(argv: Any, target: Any, env: Any, output: Any, stdin: Any) -> int:
        assert json.load(stdin) == ["source-0.tsx"]
        assert argv == [
            "/trusted/node",
            "--max-old-space-size=512",
            str(source.JSX_HELPER),
            "/trusted/ts.js",
        ]
        output.write(json.dumps({"source-0.tsx": offsets}).encode())
        return 0

    monkeypatch.setattr(source, "_invoke", invoke)
    encodings = source._jsx_input(
        tmp_path,
        {"source-0.tsx": "app.tsx"},
        {},
        Path("/trusted/node"),
        Path("/trusted/ts.js"),
    )
    assert encodings == 2
    assert (tmp_path / "source-0.tsx").read_bytes() == original.replace(b"&", b"&#38;")


def test_selected_jsx_parser_failure_never_runs_semgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.tsx").write_text(JSX_UNSAFE)
    calls = fake_tool(monkeypatch, {"SECRET": []}, code=1)
    result = source.check(
        tmp_path, ".", typescript=Path("/trusted/ts.js"), node=Path("/trusted/node")
    )
    assert result["status"] == "error"
    assert "JSX" in result["summary"]
    assert "SECRET" not in json.dumps(result)
    assert len(calls) == 1
    assert calls[0][0][0] == "/trusted/node"


def test_half_configured_jsx_parser_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.tsx").write_text(JSX_UNSAFE)
    calls = fake_tool(monkeypatch, payload())
    assert source.check(tmp_path, ".", node=Path("/trusted/node"))["status"] == "error"
    assert not calls


def test_real_jsx_parser_only_encodes_text(
    tmp_path: Path, trusted_typescript: Path
) -> None:
    original = """\ufeff// & a comment with <p> & </p>
const text = "& a string with <p> & </p>";
const template = `& a template ${text && text}`;
type A = { a: number } & { b: number };
const node = <p title="A & B">é & 🌌 &amp; &#38; &#x26; &unknown;
  <b>child & text</b>{text && (document.body.innerHTML = window.location.hash)}
  {"& an expression"}<i /> tail & text
</p>;
""".replace("\n", "\r\n")
    expected = (
        original.replace("é & 🌌", "é &#38; 🌌")
        .replace("child & text", "child &#38; text")
        .replace("tail & text", "tail &#38; text")
    )
    (tmp_path / "source-0.tsx").write_bytes(original.encode())
    source._jsx_input(
        tmp_path,
        {"source-0.tsx": "app.tsx"},
        {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path)},
        Path(shutil.which("node") or "node"),
        trusted_typescript,
    )
    assert (tmp_path / "source-0.tsx").read_bytes() == expected.encode()


@pytest.mark.parametrize("data", [b"const x = ;", b"const x = '\xff';"])
def test_real_jsx_parser_rejects_invalid_input(
    tmp_path: Path, trusted_typescript: Path, data: bytes
) -> None:
    (tmp_path / "source-0.tsx").write_bytes(data)
    with pytest.raises(ValueError, match="JSX parser failed"):
        source._jsx_input(
            tmp_path,
            {"source-0.tsx": "app.tsx"},
            {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path)},
            Path(shutil.which("node") or "node"),
            trusted_typescript,
        )


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="Semgrep CE not installed")
def test_real_tsx_ampersands_preserve_rules_and_original_lines(
    tmp_path: Path, trusted_typescript: Path
) -> None:
    (tmp_path / "app.tsx").write_text(JSX_UNSAFE)
    result = source.check(
        tmp_path,
        ".",
        typescript=trusted_typescript,
        node=Path(shutil.which("node") or "node"),
    )
    assert result["status"] == "findings", result
    assert result["coverage"]["partial"] is False
    assert result["coverage"]["jsx_input_adapter"] == "typescript-jsx-text-entities"
    assert result["coverage"]["jsx_text_encodings"] == 1
    assert {
        (entry["rule"], entry["path"], entry["line"]) for entry in result["findings"]
    } == {
        ("node-request-to-shell", "app.tsx", 4),
        ("node-request-to-file", "app.tsx", 5),
        ("browser-url-to-html", "app.tsx", 6),
    }
    assert (tmp_path / "app.tsx").read_text() == JSX_UNSAFE
