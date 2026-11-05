# Celery task and messaging declaration inventory

The optional `celery` probe reads explicitly selected Python source files with
`ast`. It measures named tasks, handler identities, exact routes and named
periodic schedules. It never imports application code, requires a Celery
installation, loads runtime configuration, runs a worker or accesses a broker.

Copy [probes/celery.toml](probes/celery.toml) into a trusted rule file and add it
to `[probes] rule_files` in your existing configuration. No default scan gains
this probe. Select the task and configuration files deliberately:

```toml
[probes.parameters.celery]
ROOT = "backend"
FILES = "jobs/tasks.py,jobs/celeryconfig.py"
```

`FILES` is a comma-separated list of explicit paths relative to `ROOT`. It does
not expand globs or walk directories. Empty, duplicate, absolute, traversing or
non-Python paths fail coverage. Commas in filenames are unsupported; each comma
is always a separator. Files and path components may not be symlinks.

```sh
vibe-sentinel scan --probes=celery
python -I -m vibe_sentinel.probes.celery --root backend --files jobs/tasks.py,jobs/celeryconfig.py
```

The supplied declaration uses fixed argv, isolated Python (`-I`) and
`compare_parameters = true`. Changing the selected source scope requires
explicit baseline acceptance before comparable measurements resume. Existing
source and dependency readers retain their behavior.

## Supported declarations

Task decorators must be proven through ordinary import aliases for
`celery.Celery` or `celery.shared_task`, or their imported modules. A locally
assigned `app = Celery(...)` identifies a supported application receiver.
The supported constructor accepts at most one literal main name (string or
`None`), literal `main`, `broker` and `backend` string/`None` keywords, and literal
`set_as_current`, `autofinalize` and `strict_typing` boolean keywords. Other
constructor options, including configuration/topology dictionaries, and
computed argument expressions fail coverage. These accepted payload values
are not exported. This is a subset of the
[Celery constructor](https://docs.celeryq.dev/en/stable/reference/celery.html#celery.Celery).
An alias of that proven application receiver is also supported. A decorator
must call `app.task(name="...")` or `shared_task(name="...")` with an explicit
literal name. Literal `bind=True` or `bind=False` is supported. Other task
options and decorator composition produce gaps.

The task name is its stable identity. A separate handler identity records the
selected source file and top-level function name, and a task-to-handler edge
connects them. This follows Celery's explicit
[task naming interface](https://docs.celeryq.dev/en/stable/userguide/tasks.html#names).
Unnamed tasks are incomplete rather than guessing an import name from a file
path. Duplicate task names, rebound aliases/handlers, nested task definitions,
class-based registration and unproven decorators are unsupported.

`task_routes` must be a literal mapping from exact task names to literal
`queue` and/or `routing_key` strings. Both standalone configuration variables
and assignments to a proven `app.conf.task_routes` are supported. The reader
records each route's links to its task, queue and routing key. It does not infer
a default queue, exchanges, queue creation or a broker's routing behavior. Glob
patterns, regular expressions, ordered/callable routers and other route options
produce gaps. See Celery's
[routing declarations](https://docs.celeryq.dev/en/stable/userguide/routing.html).

`beat_schedule` must be a literal mapping from schedule names to entries with
literal `task` names and a supported `schedule` expression. Supported forms are
nonnegative finite seconds, a proven `datetime.timedelta(...)` with literal
numeric arguments, and a proven `celery.schedules.crontab(...)` with bounded
literal numeric or common wildcard/range/step/name fields. Seconds and timedelta
expressions normalize to interval seconds. Crontab fields retain their declared
shape, with omitted fields represented as `*`. A literal `relative` flag is
recorded when present.

Schedule nodes retain cadence state and links to named tasks, following
Celery's [periodic task declarations](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html).
Time zones, next-run times, scheduler state and custom schedule implementations
are not inferred. Custom schedules, nonliteral expressions and unsupported
crontab forms produce gaps.

Routes and schedules are scoped by their source configuration identity. A
standalone config file and another application's `conf` are not silently merged
into one effective runtime configuration. Configuration loading order and
precedence are explicitly unassessed. Duplicate assignments to a selected
configuration setting fail coverage.

A referenced task not declared in the selected files receives a task-reference
observation with registration `unresolved-in-selected-files`, plus an incomplete
coverage gap. This does not assert that the task is absent from a running
application. Select the declaration source or use an appropriate future reader
for the registration mechanism that supplies it.

## Privacy and completeness

Task bodies are opaque except for recognizing explicit unsupported runtime
registration/configuration constructs. Ordinary task argument values, body text,
return values, schedule `args`/`kwargs`, broker URLs and application constructor
values are not exported. Schedule options `headers`, `expires`, `serializer`,
`priority` and `compression` are ignored payload metadata. Schedule routing
overrides or other custom options fail coverage, because ignoring them could
hide another source of topology.

Imports are parsed but never executed. Importing configuration settings,
`config_from_object`, `config_from_envvar`, `autodiscover_tasks`,
`register_task`, `add_periodic_task`, `add_defaults`, `conf.update`, selected-setting updates and
configuration subscript mutation produce gaps. Computed top-level bindings,
conditional setup, constructor-provided imports/config sources, computed
annotations and unsupported module statements are also outside the projection.
This is deliberately a narrow declaration reader, not an interpreter for
arbitrary application setup. Imported code can have side effects; actual
runtime registration remains unassessed even for a complete static projection.

Only topology identities and supported cadence fields become observations.
Names and source paths can themselves be sensitive and remain subject to the
existing scan/model/export boundary. Diagnostics contain bounded gap codes and
selected source paths rather than source expressions or arbitrary payloads.

Completeness refers only to these declarations in the explicit selected files.
It does not prove task registration at runtime, delivery guarantees, queue
existence, broker reachability or effective application configuration. The
reader adds no state gate and makes no judgement about task implementation.

## Limits and history

The selection is limited to 100 source files, 2 MiB per file, 16 MiB total and
100,000 AST nodes per file. Structural strings are limited to 2,048 characters;
output is limited to 30,000 observations and 16 MiB. Inputs must be regular
UTF-8 files within the selected root. The filesystem must remain stable while
reading; path checks are not a sandbox against concurrent directory replacement.

Every unsupported or unreadable input produces incomplete coverage and exit 2.
Standalone JSON may include partial observations. The existing subprocess
runner rejects failed output as unmeasured, so missing handlers cannot become
false removals or a clean empty inventory.

Relationships use shared `edge_schema = "1"`, ecosystem `celery`, phase
`declaration` and resolution `local`. Task/route/schedule/handler identities and
cadence state are deterministic. Count-preserving rewires therefore remain
visible: switching a schedule's task, a route's queue or a task's handler changes
the corresponding relationship identity. A scope observation names the
`celery-static-declarations-v1` projection and the runtime questions it leaves
unassessed.
