"""Celery declaration topology, privacy and honest unsupported coverage."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

from vibe_sentinel.inventory import compare
from vibe_sentinel.probes import artifact_support, celery
from vibe_sentinel.runner import run_probe
from vibe_sentinel.schemas import Observation, ProbeResult, Snapshot
from vibe_sentinel.templates import Probe, load_probes

TASKS = """from celery import Celery as Application, shared_task as shared
app = Application("private_app", broker="PRIVATE_BROKER")
@app.task(name="jobs.first")
def first(payload=None):
    payload["ordinary"] = "PRIVATE_BODY"
    return payload
@shared(name="jobs.second", bind=True)
def second(self):
    return "PRIVATE_RESULT"
"""
CONFIG = """from datetime import timedelta as delta
from celery.schedules import crontab as cron
broker_url = "PRIVATE_BROKER"
task_routes = {
 "jobs.first": {"queue": "alpha", "routing_key": "alpha.run"},
 "jobs.second": {"queue": "beta", "routing_key": "beta.run"},
}
beat_schedule = {
 "every-minute": {"task": "jobs.first", "schedule": delta(minutes=1), "args": ["PRIVATE_ARGS"], "kwargs": {"password": "PRIVATE_KWARGS"}, "options": {"headers": {"auth": "PRIVATE_HEADER"}}},
 "weekly": {"task": "jobs.second", "schedule": cron(hour=7, minute=30, day_of_week="mon")},
}
"""


def fixture(root: Path, tasks: str = TASKS, config: str = CONFIG) -> None:
    (root / "tasks.py").write_text(tasks)
    (root / "config.py").write_text(config)


def measure(root: Path, files: str = "tasks.py,config.py") -> dict[str, Any]:
    return celery.Reader(root, files).measure()


def codes(result: dict[str, Any]) -> set[str]:
    return {gap["code"] for gap in result["coverage"]["gaps"]}


def diff(before: dict[str, Any], after: dict[str, Any]) -> list:
    def snapshot(result: dict[str, Any]) -> Snapshot:
        return Snapshot(
            probes={
                "celery": ProbeResult(
                    probe_id="celery",
                    observations=[
                        Observation.model_validate(o) for o in result["observations"]
                    ],
                )
            }
        )

    return compare(
        snapshot(before), snapshot(after), [Probe(id="celery", title="Jobs")]
    ).changes


def test_two_tasks_queues_schedules_and_private_payloads(tmp_path: Path) -> None:
    fixture(tmp_path)
    result = measure(tmp_path)
    assert not codes(result)
    assert "PRIVATE" not in json.dumps(result) and "private_app" not in json.dumps(
        result
    )
    for kind in ("task", "handler", "queue", "schedule"):
        assert (
            len(
                [
                    o
                    for o in result["observations"]
                    if o.get("attrs", {}).get("kind") == kind
                    and not o["attrs"].get("edge_schema")
                ]
            )
            == 2
        )
    assert not diff(result, measure(tmp_path, "config.py,tasks.py"))
    assert any(
        json.loads(o["state"]) == {"kind": "interval", "seconds": 60.0}
        for o in result["observations"]
        if o.get("state")
    )


@pytest.mark.parametrize(
    "change", ["handler", "queue", "routing_key", "schedule_target", "cadence"]
)
def test_constant_count_rewires_and_schedule_shape_change(
    tmp_path: Path, change: str
) -> None:
    fixture(tmp_path)
    before = measure(tmp_path)
    tasks, config = TASKS, CONFIG
    if change == "handler":
        tasks = tasks.replace("def first(", "def renamed(")
    elif change == "queue":
        config = (
            config.replace('"queue": "alpha"', '"queue": "TEMP"')
            .replace('"queue": "beta"', '"queue": "alpha"')
            .replace('"queue": "TEMP"', '"queue": "beta"')
        )
    elif change == "routing_key":
        config = (
            config.replace('"alpha.run"', '"TEMP"')
            .replace('"beta.run"', '"alpha.run"')
            .replace('"TEMP"', '"beta.run"')
        )
    elif change == "schedule_target":
        config = (
            config.replace('"task": "jobs.first"', '"task": "TEMP"')
            .replace('"task": "jobs.second"', '"task": "jobs.first"')
            .replace('"task": "TEMP"', '"task": "jobs.second"')
        )
    else:
        config = config.replace("minutes=1", "minutes=2")
    fixture(tmp_path, tasks, config)
    after = measure(tmp_path)
    assert not codes(after)
    changes = diff(before, after)
    assert changes
    if change != "cadence":
        assert {change.kind for change in changes} >= {"appeared", "disappeared"}
    else:
        assert any(change.kind == "changed" for change in changes)


def test_module_aliases_app_config_and_plain_bodies(tmp_path: Path) -> None:
    tasks = """import celery as c
import datetime as dt
import celery.schedules as schedules
app = c.Celery("app")
@app.task(name="work")
def work():
    values = {}
    values["x"] = 1
    return values
app.conf.task_routes = {"work": {"queue": "q"}}
app.conf.beat_schedule = {"tick": {"task": "work", "schedule": dt.timedelta(seconds=5)}}
"""
    fixture(tmp_path, tasks, "")
    assert not codes(measure(tmp_path))


@pytest.mark.parametrize(
    "decorator",
    [
        "@app.task",
        "@app.task()",
        '@unknown(name="work")',
        "@app.task(name=NAME)",
        '@app.task(name="work", queue="q")',
        '@app.task(name="work", **OPTIONS)',
    ],
)
def test_unproven_unnamed_or_custom_tasks_fail(tmp_path: Path, decorator: str) -> None:
    fixture(
        tmp_path,
        "from celery import Celery\napp = Celery()\n"
        + decorator
        + "\ndef work(): pass\n",
        "",
    )
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


@pytest.mark.parametrize(
    "statement",
    [
        "app.add_periodic_task(3, first.s())",
        "app.register_task(first)",
        'app.config_from_object("config")',
        "app.autodiscover_tasks()",
        "app.conf.update(task_routes={})",
        'app.conf["task_routes"] = {}',
        'app.conf.task_routes["jobs.first"] = {"queue": "q"}',
        "task_routes.update({})",
    ],
)
def test_runtime_registration_and_config_mutation_fail(
    tmp_path: Path, statement: str
) -> None:
    fixture(tmp_path, TASKS + "\n" + statement, CONFIG)
    assert measure(tmp_path)["coverage"]["status"] == "incomplete"


def test_runtime_registration_inside_function_is_explicit_gap(tmp_path: Path) -> None:
    fixture(
        tmp_path,
        TASKS + "\ndef later():\n    app.add_periodic_task(3, first.s())\n",
        CONFIG,
    )
    assert "runtime-registration-or-configuration" in codes(measure(tmp_path))


@pytest.mark.parametrize(
    "route",
    [
        '{"jobs.*": {"queue": "q"}}',
        '[("jobs.first", {"queue": "q"})]',
        "router",
        '{re.compile("x"): {"queue": "q"}}',
        '{"jobs.first": {"exchange": "q"}}',
        '{"jobs.first": {"queue": compute()}}',
        '{"jobs.first": {"queue": "q"}, "jobs.first": {"queue": "other"}}',
    ],
)
def test_unsupported_router_forms_fail_without_values(
    tmp_path: Path, route: str
) -> None:
    fixture(tmp_path, TASKS, "task_routes = " + route)
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


@pytest.mark.parametrize(
    "schedule",
    [
        "custom_schedule()",
        "schedule_variable",
        "cron(nowfun=custom)",
        'cron(hour="PRIVATE_VALUE")',
        'delta(seconds=float("inf"))',
        "cron(hour=1, **kwargs)",
        "delta(seconds=-1)",
    ],
)
def test_custom_or_nonliteral_schedules_fail(tmp_path: Path, schedule: str) -> None:
    fixture(
        tmp_path,
        TASKS,
        'from celery.schedules import crontab as cron\nfrom datetime import timedelta as delta\nbeat_schedule = {"tick": {"task": "jobs.first", "schedule": '
        + schedule
        + "}}",
    )
    result = measure(tmp_path)
    assert "unsupported-celery-declaration" in codes(result)
    assert "PRIVATE_VALUE" not in json.dumps(result)


@pytest.mark.parametrize(
    "schedule",
    [
        "3",
        "3.5",
        "delta(minutes=1)",
        "cron()",
        'cron("*/5", hour="1-5", day_of_week="mon-fri")',
    ],
)
def test_supported_schedule_shapes(tmp_path: Path, schedule: str) -> None:
    fixture(
        tmp_path,
        TASKS,
        'from celery.schedules import crontab as cron\nfrom datetime import timedelta as delta\nbeat_schedule = {"tick": {"task": "jobs.first", "schedule": '
        + schedule
        + "}}",
    )
    assert not codes(measure(tmp_path))


@pytest.mark.parametrize(
    "extra",
    [
        "task_routes = {}\ntask_routes = {}",
        "from config import task_routes",
        "from config import *",
        "from celery import shared_task\nshared_task = unknown",
        "app = 3",
        "if True:\n    app.conf.task_routes = {}",
    ],
)
def test_imported_config_duplicate_assignments_and_rebinding(
    tmp_path: Path, extra: str
) -> None:
    fixture(tmp_path, TASKS + "\n" + extra, "")
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


def test_unselected_task_registration_is_unresolved_not_absent(tmp_path: Path) -> None:
    fixture(tmp_path, "", 'task_routes = {"external.task": {"queue": "q"}}')
    result = measure(tmp_path)
    assert "unresolved-task-registration" in codes(result)
    assert any(
        o.get("state") == '{"registration":"unresolved-in-selected-files"}'
        for o in result["observations"]
    )


def test_duplicate_task_names_and_schedule_names_fail(tmp_path: Path) -> None:
    fixture(tmp_path, TASKS.replace('name="jobs.second"', 'name="jobs.first"'), "")
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))
    fixture(
        tmp_path,
        TASKS,
        'beat_schedule={"tick": {"task":"jobs.first", "schedule":1}, "tick": {"task":"jobs.second", "schedule":2}}',
    )
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


@pytest.mark.parametrize(
    "selection",
    [
        "tasks.py,tasks.py",
        "../tasks.py",
        "tasks.py,missing.py",
        "tasks.py,",
        "/etc/passwd",
    ],
)
def test_selection_failures(tmp_path: Path, selection: str) -> None:
    fixture(tmp_path)
    assert measure(tmp_path, selection)["coverage"]["status"] == "incomplete"


def test_no_imports_execution_and_symlink_rejection(tmp_path: Path) -> None:
    fixture(tmp_path, "import nonexistent_package\n" + TASKS, CONFIG)
    assert not codes(measure(tmp_path))
    (tmp_path / "tasks.py").rename(tmp_path / "actual.py")
    (tmp_path / "tasks.py").symlink_to(tmp_path / "actual.py")
    assert "unreadable-invalid-or-oversized-source" in codes(measure(tmp_path))


@pytest.mark.parametrize(
    "limit", ["MAX_FILES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_AST_NODES"]
)
def test_source_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    fixture(tmp_path)
    monkeypatch.setattr(celery, limit, 1)
    assert measure(tmp_path)["coverage"]["status"] == "incomplete"


@pytest.mark.parametrize("limit", ["MAX_OBSERVATIONS", "MAX_OUTPUT_BYTES"])
def test_output_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    fixture(tmp_path)
    monkeypatch.setattr(artifact_support, limit, 1)
    assert measure(tmp_path)["coverage"]["status"] == "incomplete"


def test_subprocess_failure_is_unmeasured_without_false_removals(
    tmp_path: Path,
) -> None:
    fixture(tmp_path)
    probe = Probe(
        id="celery",
        title="Jobs",
        command=[
            sys.executable,
            "-m",
            "vibe_sentinel.probes.celery",
            "--root",
            str(tmp_path),
            "--files",
            "tasks.py,config.py",
        ],
    )
    before = run_probe(probe, {}, cwd=tmp_path)
    assert before.ok and before.observations
    (tmp_path / "tasks.py").unlink()
    after = run_probe(probe, {}, cwd=tmp_path)
    assert not after.ok and not after.observations
    result = compare(
        Snapshot(probes={probe.id: before}), Snapshot(probes={probe.id: after}), [probe]
    )
    assert not result.changes and probe.id in result.unmeasured


def test_opt_in_isolated_parameter_guarded_example() -> None:
    root = Path(__file__).resolve().parents[1]
    probe = next(
        p
        for p in load_probes([root / "docs/probes/celery.toml"], root)
        if p.id == "celery"
    )
    assert probe.compare_parameters
    assert probe.command[1:4] == ["-I", "-m", "vibe_sentinel.probes.celery"]
    assert not any(p.id == "celery" for p in load_probes([], root))


def test_constructor_walrus_cannot_silently_rebind_task_alias(tmp_path: Path) -> None:
    fixture(
        tmp_path,
        'from celery import Celery, shared_task\napp = Celery((shared_task := unknown))\n@shared_task(name="work")\ndef work(): pass',
        "",
    )
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


def test_interval_literal_and_timedelta_are_same_shape(tmp_path: Path) -> None:
    fixture(tmp_path)
    before = measure(tmp_path)
    fixture(tmp_path, TASKS, CONFIG.replace("delta(minutes=1)", "60"))
    assert not diff(before, measure(tmp_path))


@pytest.mark.parametrize(
    "constructor",
    [
        'Celery(task_routes={"jobs.first": {"queue": "q"}})',
        'Celery(beat_schedule={"tick": {"task": "jobs.first", "schedule": 5}})',
        'Celery(changes={"task_routes": {}})',
        'Celery("app", loader="custom")',
        "Celery(broker=mutate())",
        "Celery(mutate())",
        'Celery(main="a", set_as_current=compute())',
        'Celery("a", "custom_loader")',
    ],
)
def test_constructor_topology_and_computed_payloads_are_gaps(
    tmp_path: Path, constructor: str
) -> None:
    tasks = (
        "from celery import Celery, shared_task\napp = "
        + constructor
        + '\n@shared_task(name="jobs.first")\ndef first(): pass'
    )
    fixture(tmp_path, tasks, "")
    result = measure(tmp_path)
    assert "unsupported-celery-declaration" in codes(result)


def test_literal_constructor_subset_omits_private_values(tmp_path: Path) -> None:
    fixture(
        tmp_path,
        'from celery import Celery\napp = Celery(main="PRIVATE_MAIN", broker="PRIVATE_BROKER", backend="PRIVATE_BACKEND", set_as_current=False, autofinalize=True, strict_typing=True)\n@app.task(name="work")\ndef work(): pass',
        "",
    )
    result = measure(tmp_path)
    assert not codes(result) and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "source",
    [
        'from celery import Celery\napp = Celery()\n@app.not_the_app.task(name="work")\ndef work(): pass',
        'from celery import Celery, shared_task\napp = Celery()\n@shared_task(name="work")\ndef work(): pass\napp.not_the_app.conf.task_routes = {"work": {"queue":"q"}}',
        'from celery import shared_task\n@shared_task(name="work")\ndef work(): pass\ntask_routes = {"work": {"queue":"q"}}\ndef task_routes(): pass',
        'from celery import shared_task\n@shared_task(name="work")\ndef work(): pass\ndef task_routes(): pass\ntask_routes = {"work": {"queue":"q"}}',
    ],
)
def test_intermediate_receivers_and_setting_rebindings_are_unproven(
    tmp_path: Path, source: str
) -> None:
    fixture(tmp_path, source, "")
    assert "unsupported-celery-declaration" in codes(measure(tmp_path))


def test_direct_application_alias_preserves_proven_receiver(tmp_path: Path) -> None:
    fixture(
        tmp_path,
        'from celery import Celery\napp = Celery()\nother = app\n@other.task(name="work")\ndef work(): pass\nother.conf.task_routes = {"work": {"queue":"q"}}',
        "",
    )
    assert not codes(measure(tmp_path))
