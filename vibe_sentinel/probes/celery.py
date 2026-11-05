"""Bounded static Celery task, route and schedule declarations, never execution."""

from __future__ import annotations

import argparse
import ast
from datetime import timedelta
import math
from pathlib import Path
import re
from typing import Any

from vibe_sentinel.probes.artifact_support import (
    Evidence,
    emit,
    read_bytes,
    relative,
    text,
)

MAX_FILES = 100
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_AST_NODES = 100_000
SETTINGS = frozenset({"task_routes", "beat_schedule"})
REGISTRATION = frozenset(
    {
        "add_periodic_task",
        "add_defaults",
        "register_task",
        "config_from_object",
        "config_from_envvar",
        "autodiscover_tasks",
    }
)
CRON_FIELDS = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")
CRON_NAMES = frozenset(
    {
        "sun",
        "mon",
        "tue",
        "wed",
        "thu",
        "fri",
        "sat",
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "aug",
        "sep",
        "oct",
        "nov",
        "dec",
    }
)


def qualified(node: ast.AST, bindings: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, "")
    if isinstance(node, ast.Attribute):
        base = qualified(node.value, bindings)
        return f"{base}.{node.attr}" if base else ""
    return ""


def application(node: ast.AST, bindings: dict[str, str]) -> bool:
    return isinstance(node, ast.Name) and bindings.get(node.id, "").startswith("app:")


def literal_string(node: ast.AST) -> str:
    if (
        not isinstance(node, ast.Constant)
        or not isinstance(node.value, str)
        or not text(node.value)
    ):
        raise ValueError("literal name required")
    return node.value


def mapping(node: ast.AST) -> dict[str, ast.expr]:
    if not isinstance(node, ast.Dict):
        raise ValueError("literal mapping required")
    result: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            raise ValueError("mapping expansion")
        name = literal_string(key)
        if name in result:
            raise ValueError("duplicate mapping key")
        result[name] = value
    return result


def keywords(node: ast.Call) -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for keyword in node.keywords:
        if keyword.arg is None or keyword.arg in result:
            raise ValueError("keyword expansion or duplication")
        result[keyword.arg] = keyword.value
    return result


def number(node: ast.AST) -> float:
    value = ast.literal_eval(node)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("finite numeric interval required")
    return float(value)


def cadence(node: ast.AST, bindings: dict[str, str]) -> dict[str, Any]:
    if isinstance(node, ast.Constant):
        seconds = number(node)
        if seconds < 0:
            raise ValueError("negative interval")
        return {"kind": "interval", "seconds": seconds}
    if not isinstance(node, ast.Call):
        raise ValueError("unsupported schedule")
    name = qualified(node.func, bindings)
    kwargs = keywords(node)
    if name == "datetime.timedelta":
        fields = (
            "days",
            "seconds",
            "microseconds",
            "milliseconds",
            "minutes",
            "hours",
            "weeks",
        )
        if len(node.args) > len(fields) or kwargs.keys() - set(fields):
            raise ValueError("unsupported timedelta")
        values = {
            field: number(value)
            for field, value in zip(fields, node.args, strict=False)
        }
        if kwargs.keys() & values.keys():
            raise ValueError("duplicate timedelta field")
        values.update((field, number(value)) for field, value in kwargs.items())
        seconds = timedelta(**values).total_seconds()
        if seconds < 0:
            raise ValueError("negative interval")
        return {"kind": "interval", "seconds": seconds}
    if (
        name != "celery.schedules.crontab"
        or len(node.args) > len(CRON_FIELDS)
        or kwargs.keys() - set(CRON_FIELDS)
    ):
        raise ValueError("unsupported schedule")
    cron_values = dict(zip(CRON_FIELDS, node.args, strict=False))
    if cron_values.keys() & kwargs.keys():
        raise ValueError("duplicate crontab field")
    cron_values.update(kwargs)
    shape: dict[str, Any] = {"kind": "crontab"}
    for field in CRON_FIELDS:
        value = ast.literal_eval(cron_values[field]) if field in cron_values else "*"
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            shape[field] = value
        elif (
            text(value)
            and re.fullmatch(r"[0-9a-zA-Z*,/\-]+", value)
            and all(
                part.lower() in CRON_NAMES for part in re.findall(r"[a-zA-Z]+", value)
            )
        ):
            shape[field] = value.lower()
        else:
            raise ValueError("unsupported crontab field")
    return shape


class Reader:
    def __init__(self, root: Path, files: str) -> None:
        self.root = root.resolve()
        self.files = files
        self.evidence = Evidence("celery")
        self.tasks: set[str] = set()
        self.targets: set[str] = set()
        self.settings: set[tuple[str, str]] = set()
        self.total_bytes = 0

    def task(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        bindings: dict[str, str],
        source: str,
    ) -> None:
        if len(node.decorator_list) != 1:
            raise ValueError("unsupported decorator composition")
        decorator = node.decorator_list[0]
        if not isinstance(decorator, ast.Call) or decorator.args:
            raise ValueError("explicit named task required")
        target = qualified(decorator.func, bindings)
        if target != "celery.shared_task" and not (
            isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "task"
            and application(decorator.func.value, bindings)
        ):
            raise ValueError("unproven task decorator")
        kwargs = keywords(decorator)
        if kwargs.keys() - {"name", "bind"} or "name" not in kwargs:
            raise ValueError("unsupported task options or missing name")
        if "bind" in kwargs and not (
            isinstance(kwargs["bind"], ast.Constant)
            and isinstance(kwargs["bind"].value, bool)
        ):
            raise ValueError("nonliteral bind")
        name = literal_string(kwargs["name"])
        if name in self.tasks:
            raise ValueError("duplicate task identity")
        self.tasks.add(name)
        handler = f"handler:{source}:{node.name}"
        self.evidence.record(
            "task", f"task:{name}", {"registration": "literal-decorator"}
        )
        self.evidence.record("handler", handler, {}, file=source, qualname=node.name)
        self.evidence.edge(f"task:{name}", handler, "handler")

    def routes(self, node: ast.AST, owner: str) -> None:
        for task, value in sorted(mapping(node).items()):
            if any(char in task for char in "*?[]"):
                raise ValueError("pattern router")
            route = mapping(value)
            if not route or route.keys() - {"queue", "routing_key"}:
                raise ValueError("unsupported route options")
            identity = f"route:{owner}:{task}"
            self.evidence.record("route", identity, {})
            self.evidence.edge(identity, f"task:{task}", "task")
            self.targets.add(task)
            for field, target in sorted(route.items()):
                name = literal_string(target)
                endpoint = f"{field}:{name}"
                self.evidence.record(field, endpoint, {})
                self.evidence.edge(identity, endpoint, field)

    def schedules(self, node: ast.AST, owner: str, bindings: dict[str, str]) -> None:
        for name, value in sorted(mapping(node).items()):
            schedule = mapping(value)
            if (
                schedule.keys()
                - {"task", "schedule", "args", "kwargs", "options", "relative"}
                or not {"task", "schedule"} <= schedule.keys()
            ):
                raise ValueError("unsupported schedule entry")
            # Runtime routing overrides are a different routing source; never
            # silently discard them as arbitrary payload metadata.
            if "options" in schedule:
                options = mapping(schedule["options"])
                if options.keys() - {
                    "headers",
                    "expires",
                    "serializer",
                    "priority",
                    "compression",
                }:
                    raise ValueError("unsupported schedule options")
            task = literal_string(schedule["task"])
            shape = cadence(schedule["schedule"], bindings)
            if "relative" in schedule:
                value = ast.literal_eval(schedule["relative"])
                if not isinstance(value, bool):
                    raise ValueError("nonliteral relative flag")
                shape["relative"] = value
            identity = f"schedule:{owner}:{name}"
            self.evidence.record("schedule", identity, shape)
            self.evidence.edge(identity, f"task:{task}", "task")
            self.targets.add(task)

    def setting(
        self, target: ast.AST, bindings: dict[str, str], source: str
    ) -> tuple[str, str] | None:
        if isinstance(target, ast.Name) and target.id in SETTINGS:
            return f"config:{source}", target.id
        if isinstance(target, ast.Attribute) and target.attr in SETTINGS:
            if not (
                isinstance(target.value, ast.Attribute)
                and target.value.attr == "conf"
                and application(target.value.value, bindings)
            ):
                raise ValueError("unproven application configuration")
            return qualified(target.value, bindings), target.attr
        return None

    def read(self, source: str) -> None:
        data = read_bytes(self.root, source, MAX_FILE_BYTES)
        self.total_bytes += len(data)
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("total source limit")
        tree = ast.parse(data.decode("utf-8"))
        for index, node in enumerate(ast.walk(tree), start=1):
            if index > MAX_AST_NODES:
                raise ValueError("AST limit")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in REGISTRATION or (
                    node.func.attr == "update"
                    and (
                        (
                            isinstance(node.func.value, ast.Attribute)
                            and node.func.value.attr in SETTINGS | {"conf"}
                        )
                        or (
                            isinstance(node.func.value, ast.Name)
                            and node.func.value.id in SETTINGS
                        )
                    )
                ):
                    self.evidence.gap("runtime-registration-or-configuration", source)
            if isinstance(node, ast.Subscript) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                base = node.value
                while isinstance(base, ast.Subscript):
                    base = base.value
                if (
                    isinstance(base, ast.Attribute) and base.attr in SETTINGS | {"conf"}
                ) or (isinstance(base, ast.Name) and base.id in SETTINGS):
                    self.evidence.gap("configuration-or-registration-mutation", source)
        bindings: dict[str, str] = {}
        defined: set[str] = set()
        for statement in tree.body:
            try:
                annotations = []
                if isinstance(statement, ast.AnnAssign):
                    annotations.append(statement.annotation)
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [
                        *statement.args.posonlyargs,
                        *statement.args.args,
                        *statement.args.kwonlyargs,
                    ]
                    args += [
                        arg
                        for arg in (statement.args.vararg, statement.args.kwarg)
                        if arg is not None
                    ]
                    annotations.extend(
                        arg.annotation for arg in args if arg.annotation is not None
                    )
                    if statement.returns is not None:
                        annotations.append(statement.returns)
                    for default in [
                        *statement.args.defaults,
                        *statement.args.kw_defaults,
                    ]:
                        if default is not None:
                            ast.literal_eval(default)
                if any(
                    isinstance(node, (ast.NamedExpr, ast.Call))
                    for annotation in annotations
                    for node in ast.walk(annotation)
                ):
                    raise ValueError("computed annotation")
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    for alias in statement.names:
                        if alias.name == "*":
                            raise ValueError("wildcard import")
                        local = alias.asname or alias.name.split(".")[0]
                        imported = (
                            f"{statement.module}.{alias.name}"
                            if isinstance(statement, ast.ImportFrom)
                            and statement.level == 0
                            else alias.name
                            if isinstance(statement, ast.Import)
                            else ""
                        )
                        if isinstance(statement, ast.Import) and alias.asname is None:
                            imported = alias.name.split(".")[0]
                        if local in defined or local in SETTINGS:
                            raise ValueError("rebound or imported configuration")
                        defined.add(local)
                        bindings[local] = imported
                    continue
                if (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                ):
                    continue
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if statement.name in defined:
                        raise ValueError("rebound handler")
                    if statement.decorator_list:
                        self.task(statement, bindings, source)
                    if any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child is not statement
                        and child.decorator_list
                        for child in ast.walk(statement)
                    ):
                        raise ValueError("nested registration")
                    defined.add(statement.name)
                    bindings.pop(statement.name, None)
                    continue
                if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                    target, value = statement.targets[0], statement.value
                elif (
                    isinstance(statement, ast.AnnAssign) and statement.value is not None
                ):
                    target, value = statement.target, statement.value
                else:
                    raise ValueError("unsupported module statement")
                if any(isinstance(node, ast.NamedExpr) for node in ast.walk(value)):
                    raise ValueError("computed binding")
                setting = self.setting(target, bindings, source)
                if setting is not None:
                    if isinstance(target, ast.Name):
                        if target.id in defined:
                            raise ValueError("rebound configuration binding")
                        defined.add(target.id)
                    if setting in self.settings:
                        raise ValueError("duplicate configuration assignment")
                    self.settings.add(setting)
                    owner, field = setting
                    if field == "task_routes":
                        self.routes(value, owner)
                    else:
                        self.schedules(value, owner, bindings)
                    continue
                if not isinstance(target, ast.Name) or target.id in defined:
                    raise ValueError("unsupported assignment or rebound binding")
                defined.add(target.id)
                if (
                    isinstance(value, ast.Call)
                    and qualified(value.func, bindings) == "celery.Celery"
                ):
                    kwargs = keywords(value)
                    string_fields = {"main", "broker", "backend"}
                    bool_fields = {"set_as_current", "autofinalize", "strict_typing"}
                    if (
                        len(value.args) > 1
                        or kwargs.keys() - string_fields - bool_fields
                        or (value.args and "main" in kwargs)
                    ):
                        raise ValueError("unsupported application constructor options")
                    supplied = dict(kwargs)
                    if value.args:
                        supplied["main"] = value.args[0]
                    for field, expression in supplied.items():
                        option = ast.literal_eval(expression)
                        if (
                            field in string_fields
                            and option is not None
                            and not isinstance(option, str)
                        ):
                            raise ValueError("nonliteral application identifier")
                        if field in bool_fields and not isinstance(option, bool):
                            raise ValueError("nonliteral application flag")
                    bindings[target.id] = f"app:{source}:{target.id}"
                elif isinstance(value, ast.Name) and qualified(
                    value, bindings
                ).startswith("app:"):
                    bindings[target.id] = qualified(value, bindings)
                else:
                    ast.literal_eval(value)
            except (ValueError, TypeError, OverflowError, RecursionError):
                self.evidence.gap("unsupported-celery-declaration", source)

    def measure(self) -> dict[str, Any]:
        files = self.files.split(",")
        if (
            not files
            or len(files) > MAX_FILES
            or len(set(files)) != len(files)
            or not all(relative(file) and file.endswith(".py") for file in files)
        ):
            self.evidence.gap("invalid-source-file-selection")
        else:
            for source in sorted(files):
                try:
                    self.read(source)
                except (
                    OSError,
                    ValueError,
                    UnicodeError,
                    SyntaxError,
                    TypeError,
                    RecursionError,
                ):
                    self.evidence.gap("unreadable-invalid-or-oversized-source", source)
                if self.total_bytes > MAX_TOTAL_BYTES:
                    break
        for target in sorted(self.targets - self.tasks):
            self.evidence.record(
                "task-reference",
                f"task:{target}",
                {"registration": "unresolved-in-selected-files"},
            )
            self.evidence.gap("unresolved-task-registration")
        if not self.tasks and not self.settings:
            self.evidence.gap("no-celery-declarations")
        self.evidence.record(
            "scope",
            "selected-files",
            {
                "definition": "celery-static-declarations-v1",
                "files": sorted(files) if all(relative(file) for file in files) else [],
                "runtime_registration": "not-assessed",
                "broker_delivery": "not-assessed",
                "configuration_precedence": "not-assessed",
            },
        )
        return self.evidence.result(
            "Static Celery declarations; runtime registration, broker delivery and configuration precedence not assessed"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory explicit Celery tasks, literal routes and named beat schedules without importing code or accessing a broker."
    )
    parser.add_argument(
        "--root", required=True, help="Project root containing the source declarations"
    )
    parser.add_argument(
        "--files",
        required=True,
        help="Comma-separated explicit project-relative Python files; commas in filenames are unsupported",
    )
    args = parser.parse_args(argv)
    return emit(Reader(Path(args.root), args.files).measure(), "Celery")


if __name__ == "__main__":
    raise SystemExit(main())
