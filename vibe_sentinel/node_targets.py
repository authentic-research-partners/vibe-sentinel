"""Bounded npm install targets, read as data without loading Node or project code.

This module is on the hook path. Its results distinguish declarations actually
compared from unsupported targeting; unknown never means root dependencies
apply to another workspace. Python installation parsing stays in requirements.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Literal, NamedTuple


class NodeManifestUnreadable(ValueError):
    """A selected declaration exists but cannot be interpreted as a manifest."""


class InstallTarget(NamedTuple):
    manager: str
    names: tuple[str, ...]
    manifest: Path | None
    status: Literal["resolved", "unknown"]
    undeclared: tuple[str, ...] = ()
    reason: str = ""


class _Token(NamedTuple):
    value: str
    operator: bool = False
    dynamic: bool = False


class _Request(NamedTuple):
    manager: str
    names: tuple[str, ...]
    workspaces: tuple[_Token, ...] = ()
    prefix: _Token | None = None
    unknown: str = ""


_ADD = {
    "npm": {"install", "i", "in", "add"},
    "pnpm": {"add", "install", "i"},
    "yarn": {"add"},
    "bun": {"add", "install", "i"},
}
_WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "command", "exec"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VALUE_OPTIONS = {
    "--registry",
    "--tag",
    "--save-prefix",
    "--omit",
    "--include",
    "--cache",
    "--userconfig",
    "--location",
}
_FLAGS = {
    "-D",
    "-P",
    "-O",
    "-E",
    "--save-dev",
    "--save-prod",
    "--save-optional",
    "--save-peer",
    "--save-exact",
    "--save",
    "--no-save",
    "--package-lock-only",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
    "--offline",
    "--prefer-offline",
    "--force",
    "--legacy-peer-deps",
    "--dry-run",
    "--silent",
    "--verbose",
    "--global=false",
}
_TARGET_OPTIONS = {
    "--prefix",
    "-w",
    "--workspace",
    "--filter",
    "-F",
    "--cwd",
    "--dir",
    "-C",
}
_NOT_NAME = (
    "file:",
    "link:",
    "git+",
    "git:",
    "github:",
    "http:",
    "https:",
    "workspace:",
    "portal:",
    "patch:",
)
_EXCLUDED = {"node_modules", ".git", ".venv", "vendor", "dist", "build"}


def package_name(spec: str) -> str:
    """A bare registry name, never a path, URL, alias or shell expansion."""
    if (
        not spec
        or spec.startswith(("-", ".", "/", "~", *_NOT_NAME))
        or "://" in spec
        or spec.endswith((".tgz", ".tar.gz"))
        or "@npm:" in spec
        or any(char in spec for char in "$`\\")
    ):
        return ""
    name = spec.rsplit("@", 1)[0] if "@" in spec[1:] else spec
    if re.fullmatch(r"(?:@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+", name):
        return name.lower()
    return ""


def _tokens(command: str) -> list[_Token]:
    """Literal POSIX words and operators, preserving quoted operator identity."""
    if len(command) > 32768:
        raise ValueError("command-limit")
    out: list[_Token] = []
    word: list[str] = []
    quote = ""
    active = dynamic = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 1
            if index == len(command):
                raise ValueError("unterminated-escape")
            escaped = command[index]
            if quote == '"' and escaped not in '\\"$`\n':
                word.append("\\")
            if escaped != "\n":
                word.append(escaped)
                active = True
        elif quote:
            if char == quote:
                quote = ""
            else:
                word.append(char)
                dynamic |= quote == '"' and char in "$`"
        elif char in "'\"":
            quote = char
            active = True
        elif char == "#" and not active:
            newline = command.find("\n", index)
            index = len(command) if newline < 0 else newline - 1
        elif char.isspace() or char in ";&|()<>":
            if active:
                out.append(_Token("".join(word), dynamic=dynamic))
                word = []
                active = dynamic = False
            if char == "\n":
                out.append(_Token(";", True))
            elif char in ";&|()<>":
                op = char
                if (
                    index + 1 < len(command)
                    and command[index + 1] == char
                    and char in "&|<>"
                ):
                    op += char
                    index += 1
                out.append(_Token(op, True))
        else:
            word.append(char)
            active = True
            dynamic |= char in "$`"
        index += 1
    if quote:
        raise ValueError("unterminated-quote")
    if active:
        out.append(_Token("".join(word), dynamic=dynamic))
    return out


def _request(tokens: list[_Token]) -> _Request | None:
    wrapped = bool(tokens and tokens[0].value in _WRAPPERS)
    while tokens and (
        tokens[0].value in _WRAPPERS or _ASSIGNMENT.match(tokens[0].value)
    ):
        # Environment assignments can alter npm targeting; don't infer their values.
        first = tokens.pop(0)
        if _ASSIGNMENT.match(first.value) and first.value.lower().startswith(
            ("npm_config_", "init_cwd=")
        ):
            return _unknown_request(tokens, "environment-targeting")
    if not tokens or tokens[0].value not in _ADD:
        if wrapped:
            for index, token in enumerate(tokens):
                if token.value in _ADD:
                    inner = _request(tokens[index:])
                    return (
                        inner._replace(unknown="unsupported-wrapper-options")
                        if inner
                        else None
                    )
        return None
    manager = tokens[0].value
    if manager == "yarn" and len(tokens) > 1 and tokens[1].value == "global":
        return None
    names: list[str] = []
    workspaces: list[_Token] = []
    prefixes: list[_Token] = []
    unknown = ""
    verb = ""
    positional = False
    dynamic_operand = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        value = token.value
        if value in {"-g", "--global=true", "--location=global"}:
            return None
        if value == "--global":
            if index + 1 < len(tokens) and tokens[index + 1].value == "false":
                index += 2
                continue
            return None
        if value == "--":
            positional = True
        elif value.startswith("-") and not positional:
            flag, equals, inline = value.partition("=")
            if flag in _TARGET_OPTIONS or flag in _VALUE_OPTIONS:
                if equals:
                    argument = _Token(inline, dynamic=token.dynamic)
                elif index + 1 < len(tokens):
                    index += 1
                    argument = tokens[index]
                else:
                    argument = _Token("")
                    unknown = "missing-option-value"
                if flag == "--location" and argument.value == "global":
                    return None
                if flag in {"-w", "--workspace"} and manager == "npm":
                    workspaces.append(argument)
                elif flag == "--prefix" and manager == "npm":
                    prefixes.append(argument)
                elif flag in _TARGET_OPTIONS or flag in {"--location", "--userconfig"}:
                    unknown = "unsupported-target-option"
            elif value not in _FLAGS:
                unknown = "unsupported-option"
        elif not verb:
            verb = value
            if verb not in _ADD[manager]:
                if manager == "yarn" and verb == "workspace":
                    return _unknown_request(
                        tokens[index + 1 :], "unsupported-workspace-manager", manager
                    )
                return None
        else:
            if token.dynamic or any(char in value for char in "*?[]{}"):
                dynamic_operand = True
                unknown = "dynamic-package-operand"
            name = package_name(value)
            if name and not token.dynamic:
                names.append(name)
        index += 1
    if verb not in _ADD[manager] or (not names and not dynamic_operand):
        return None
    if len(prefixes) > 1 or (prefixes and workspaces):
        unknown = "ambiguous-target-options"
    return _Request(
        manager,
        tuple(dict.fromkeys(names)),
        tuple(workspaces),
        prefixes[0] if prefixes else None,
        unknown,
    )


def _unknown_request(
    tokens: list[_Token], reason: str, manager: str = "npm"
) -> _Request | None:
    # Find a supported install verb only for wrappers we explicitly recognize.
    for index, token in enumerate(tokens):
        if token.value in _ADD.get(manager, ()):
            names = tuple(
                dict.fromkeys(
                    name
                    for item in tokens[index + 1 :]
                    if (name := package_name(item.value))
                )
            )
            if names or any(item.dynamic for item in tokens[index + 1 :]):
                return _Request(manager, names, unknown=reason)
    return None


def names_in_segment(segment: list[str]) -> list[str]:
    request = _request([_Token(value) for value in segment])
    return list(request.names) if request else []


def _read_regular(path: Path, limit: int) -> bytes:
    """Bound file input without blocking on a FIFO or following a swapped link."""
    if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        raise ValueError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not a regular file")
        return handle.read(limit + 1)


def _manifest(directory: Path) -> dict[str, Any] | None:
    import json

    path = directory / "package.json"
    if path.is_symlink():
        raise NodeManifestUnreadable(
            f"{path} is a symlink; use a manifest inside the selected project instead"
        )
    if not path.exists():
        return None
    try:
        data = _read_regular(path, 1024 * 1024)
        if len(data) > 1024 * 1024:
            raise ValueError("manifest limit")
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        for name in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            table = parsed.get(name, {})
            if not isinstance(table, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in table.items()
            ):
                raise ValueError("invalid dependency table")
        return parsed
    except (OSError, ValueError, UnicodeError) as exc:
        raise NodeManifestUnreadable(
            f"{path} could not be read as a package manifest. Fix package.json before comparing install targets."
        ) from exc


def declared_names(root: Path) -> frozenset[str] | None:
    data = _manifest(root)
    if data is None:
        return None
    names = {
        name.lower()
        for key in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        )
        for name in data.get(key, {})
    }
    own = data.get("name")
    if isinstance(own, str) and own:
        names.add(own.lower())
    return frozenset(names)


def _directory(root: Path, base: Path, token: _Token) -> Path | None:
    if token.dynamic or not token.value or token.value.startswith("~"):
        return None
    path = Path(token.value)
    candidate = path if path.is_absolute() else base / path
    try:
        relative = candidate.relative_to(root)
        current = root
        for part in relative.parts:
            current = current.parent if part == ".." else current / part
            if not current.is_relative_to(root) or current.is_symlink():
                return None
        return current if current.is_dir() else None
    except (OSError, ValueError):
        return None


def _nearest(root: Path, directory: Path) -> Path | None:
    while True:
        if (directory / "package.json").exists():
            return directory
        if directory == root:
            return None
        directory = directory.parent


def _workspace_targets(
    root: Path, cwd: Path, selectors: tuple[_Token, ...]
) -> tuple[tuple[Path, ...], str]:
    base = cwd
    data = None
    while True:
        data = _manifest(base)
        if data is not None and "workspaces" in data:
            break
        if base == root:
            return (), "no-workspace-declaration"
        base = base.parent
    configured = data["workspaces"]
    if isinstance(configured, dict):
        configured = configured.get("packages")
    if not isinstance(configured, list) or not all(
        isinstance(item, str) for item in configured
    ):
        raise NodeManifestUnreadable(
            f"{base / 'package.json'} has invalid workspace declarations"
        )
    if len(configured) > 64:
        return (), "workspace-limit"
    candidates: dict[Path, str] = {}
    for pattern in configured:
        parts = pattern.split("/")
        if not parts or any(
            part in {"", ".", "..", "**"}
            or ("*" in part and part != "*")
            or any(char in part for char in "?[]{}!\\")
            for part in parts
        ):
            return (), "unsupported-workspace-pattern"
        directories = [base]
        for part in parts:
            next_directories = []
            for directory in directories:
                try:
                    children = (
                        list(directory.iterdir()) if part == "*" else [directory / part]
                    )
                except OSError:
                    return (), "unreadable-workspace-directory"
                for child in children:
                    if child.name in _EXCLUDED:
                        continue
                    if child.is_symlink():
                        return (), "symlink-workspace"
                    if child.is_dir():
                        next_directories.append(child)
                    if len(next_directories) > 128:
                        return (), "workspace-limit"
            directories = next_directories
        for directory in directories:
            manifest = _manifest(directory)
            if manifest is None:
                continue
            name = manifest.get("name")
            if not isinstance(name, str) or not name:
                return (), "unnamed-workspace"
            candidates[directory] = name
            if len(candidates) > 128:
                return (), "workspace-limit"
    if len(set(candidates.values())) != len(candidates):
        return (), "duplicate-workspace-name"
    selected: list[Path] = []
    for selector in selectors:
        if selector.dynamic or not selector.value:
            return (), "dynamic-workspace-selector"
        matches = [
            directory
            for directory, name in candidates.items()
            if name == selector.value
        ]
        # npm can match a package name and a different exact directory at once.
        # Never let name-first lookup silently drop the second target.
        literal_path = not (
            any(char in selector.value for char in "*?[]{}!$")
            or ".." in Path(selector.value).parts
        )
        path_match = (
            _directory(root, base, selector)
            if literal_path and (cwd == base or Path(selector.value).is_absolute())
            else None
        )
        if path_match in candidates and path_match not in matches:
            matches.append(path_match)
        if len(matches) > 1:
            return (), "ambiguous-workspace-selector"
        if not matches:
            if (
                any(char in selector.value for char in "*?[]{}!$")
                or ".." in Path(selector.value).parts
            ):
                return (), "unsupported-workspace-selector"
            if cwd != base and not Path(selector.value).is_absolute():
                return (), "ambiguous-relative-workspace-selector"
            selected_directory = _directory(root, base, selector)
            matches = (
                [selected_directory]
                if selected_directory is not None and selected_directory in candidates
                else []
            )
        if len(matches) != 1:
            return (), "unknown-workspace-selector"
        if matches[0] not in selected:
            selected.append(matches[0])
    return tuple(selected), ""


def _npmrc_uncertainty(root: Path, directory: Path) -> str:
    """Detect targeting keys without parsing or exposing credential values."""
    keys = {
        "workspace",
        "workspaces",
        "include-workspace-root",
        "prefix",
        "global",
        "location",
        "userconfig",
    }
    if any(
        key.lower().startswith("npm_config_")
        and key.lower()[11:].replace("_", "-") in keys
        for key in os.environ
    ):
        return "environment-targeting"
    while True:
        path = directory / ".npmrc"
        if path.is_symlink():
            return "unsupported-npmrc"
        if path.exists():
            try:
                content = _read_regular(path, 65536)
                if len(content) > 65536:
                    return "unsupported-npmrc"
                for line in content.decode("utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", ";")) or "=" not in line:
                        continue
                    key = line.split("=", 1)[0].strip().removesuffix("[]").lower()
                    if key in keys:
                        return "npmrc-targeting"
            except (OSError, ValueError, UnicodeError):
                return "unsupported-npmrc"
        if directory == root:
            return ""
        directory = directory.parent


def _wrapped_cwd_change(tokens: list[_Token]) -> bool:
    index = 0
    while index < len(tokens) and (
        tokens[index].value in _WRAPPERS | {"builtin"}
        or _ASSIGNMENT.match(tokens[index].value)
    ):
        index += 1
    return index > 0 and any(
        token.value in {"cd", "pushd", "popd", "source", ".", "eval"}
        for token in tokens[index:]
    )


def assess(
    command: str, root: Path | None, cwd: Path | None = None
) -> tuple[InstallTarget, ...]:
    """Resolve each named install's target independently; never combine declarations."""
    if root is None or not re.search(r"\b(?:npm|pnpm|yarn|bun)\b", command):
        return ()
    root = root.resolve()
    try:
        tokens = _tokens(command)
    except ValueError:
        return (InstallTarget("npm", (), None, "unknown", reason="unreadable-shell"),)
    uncertain = any(
        token.operator and token.value not in {"&&", ";"} for token in tokens
    )
    directory = _directory(root, root, _Token(str(cwd or root)))
    output: list[InstallTarget] = []
    segment: list[_Token] = []
    for token in [*tokens, _Token(";", True)]:
        if not token.operator:
            segment.append(token)
            continue
        if segment and segment[0].value == "cd":
            directory = (
                _directory(root, directory, segment[1])
                if directory and len(segment) == 2 and token.value == "&&"
                else None
            )
        elif _wrapped_cwd_change(segment):
            directory = None
        elif segment and segment[0].value in {"pushd", "popd", "source", ".", "eval"}:
            directory = None
        else:
            request = _request(segment.copy())
            if request is not None:
                reason = (
                    request.unknown
                    or ("unsupported-shell-control-flow" if uncertain else "")
                    or ("unknown-working-directory" if directory is None else "")
                )
                if not reason and directory is not None and request.manager == "npm":
                    reason = _npmrc_uncertainty(root, directory)
                targets: tuple[Path, ...] = ()
                if not reason and directory is not None:
                    if request.workspaces:
                        targets, reason = _workspace_targets(
                            root, directory, request.workspaces
                        )
                    elif request.prefix:
                        selected = _directory(root, directory, request.prefix)
                        if selected is None or not (selected / "package.json").exists():
                            reason = "unknown-prefix-target"
                        else:
                            targets = (selected,)
                    else:
                        selected = _nearest(root, directory)
                        if selected is not None:
                            targets = (selected,)
                if reason:
                    output.append(
                        InstallTarget(
                            request.manager,
                            request.names,
                            None,
                            "unknown",
                            reason=reason,
                        )
                    )
                for target in targets:
                    declared = declared_names(target)
                    if declared is not None:
                        output.append(
                            InstallTarget(
                                request.manager,
                                request.names,
                                target / "package.json",
                                "resolved",
                                tuple(
                                    name
                                    for name in request.names
                                    if name not in declared
                                ),
                            )
                        )
        segment = []
    return tuple(output)
