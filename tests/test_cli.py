"""The command layer: dispatch, exit codes, and what reaches stdout.

The largest module in the package had no test importing it, and the
defects that survived there were the ones only a real invocation shows:
a handler that opens the database on a path documented as not needing it,
a ``--help`` string describing behaviour the handler had stopped having.
So these drive ``main()`` — argument parsing, dispatch and handler
together — rather than calling the handlers with a hand-built namespace.

Exit codes are the contract for CI: 0 clean, 1 findings, 2 could not run.

Two capture fixtures, because there are two output channels and the
distinction is load-bearing: ``capfd`` reads stdout, which is the result,
and ``logged`` reads the logger, which is where errors and their
remediations go. ``print`` and the logger are not interchangeable here —
a `scan --format json` piped somewhere has to stay parseable no matter
what goes wrong.

``_setup_logging`` is stubbed out because it adds a loguru file sink per
call, which would leave a ``logs/`` tree wherever pytest ran and stack a
sink per test.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibe_sentinel import __main__ as main_mod
from vibe_sentinel.__main__ import main
from vibe_sentinel.db import journal_store
from vibe_sentinel.db.connection import db_path, get_db, init_db

TINY_PROBE = "import json; print(json.dumps({'observations': [], 'summary': 'ok'}))"


@pytest.fixture(autouse=True)
def _no_log_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "_setup_logging", lambda config: None)


@pytest.fixture
def logged() -> Iterator[list[str]]:
    """Every line the project's logger emitted during one test.

    Errors reach the user through loguru rather than ``print``, and
    loguru writes to the ``sys.stderr`` it bound when it was imported —
    which is neither the object ``capsys`` swaps in nor the descriptor
    ``capfd`` watches. Adding a sink is how you read what a command
    actually told somebody, and every "names the way out" assertion
    below is really an assertion that an error carried its remediation.
    """
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    init_db(db_path(tmp_path))
    return tmp_path


def _config(root: Path, body: str) -> None:
    (root / ".vibe-sentinel.toml").write_text(body, encoding="utf-8")


def _one_probe(root: Path, value: float = 1.0) -> None:
    """A config declaring one probe that needs no model and no GPU."""
    emit = (
        "import json; print(json.dumps({'observations': "
        f"[{{'key': 'a', 'value': {value}, 'label': 'a'}}], 'summary': 'ok'}}))"
    )
    _config(
        root,
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "tiny"\ntitle = "t"\n'
        f'command = ["python", "-c", "{emit}"]\n',
    )


def _downgrade(root: Path) -> None:
    """Make the database look like one an older build wrote."""
    from vibe_sentinel.db.schema import SCHEMA_VERSION

    conn = sqlite3.connect(str(db_path(root)))
    conn.execute("DELETE FROM schema_version WHERE version = ?", (SCHEMA_VERSION,))
    conn.commit()
    conn.close()


# --- db: the commands that work on the file, not through it ----------------


def test_vacuum_survives_a_database_it_cannot_open(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """vacuum, backup and reindex exist for a file in a state ``get_db``
    refuses to open, so none of them may be gated behind opening it.
    vacuum did its work, printed success, and then raised on the way out
    while recording the upkeep it had just performed."""
    _downgrade(project)
    assert main(["db", "vacuum", str(project)]) == 0
    assert "Vacuumed" in capfd.readouterr().out


def test_backup_survives_a_database_it_cannot_open(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _downgrade(project)
    assert main(["db", "backup", str(project)]) == 0
    assert "Backed up to" in capfd.readouterr().out


def test_reindex_survives_a_database_it_cannot_open(project: Path) -> None:
    _downgrade(project)
    assert main(["db", "reindex", str(project)]) == 0


def test_a_command_that_reads_the_database_names_the_way_out(
    project: Path, logged: list[str]
) -> None:
    """The other half of the same rule: where opening the file IS the
    command, refusing is right — and the error has to carry its
    remediation rather than a stack trace."""
    _downgrade(project)
    assert main(["db", "status", str(project)]) == 2
    assert "vibe-sentinel migrate" in "".join(logged)


def test_db_with_no_database_says_what_makes_one(
    tmp_path: Path, logged: list[str]
) -> None:
    assert main(["db", "status", str(tmp_path)]) == 2
    assert "vibe-sentinel scan" in "".join(logged)


def test_db_check_is_quiet_on_a_healthy_database(project: Path) -> None:
    assert main(["db", "check", str(project)]) == 0


def test_db_check_exits_one_when_something_needs_attention(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """1, not 2: a finding is a result, not a failure to run. A dropped
    index is a one-line change to how every query on that table performs,
    and nothing else would notice it."""
    conn = sqlite3.connect(str(db_path(project)))
    conn.execute("DROP INDEX idx_runs_started")
    conn.commit()
    conn.close()

    assert main(["db", "check", str(project)]) == 1
    out = capfd.readouterr().out
    assert "idx_runs_started" in out
    assert "vibe-sentinel db reindex" in out


def test_db_check_json_is_parseable(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    main(["db", "check", str(project), "--format", "json"])
    assert "findings" in json.loads(capfd.readouterr().out)


# --- scan ------------------------------------------------------------------


def test_scan_records_a_run_and_reports_no_drift(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--no-model"]) == 0
    out = capfd.readouterr().out
    assert "recorded as run 1" in out


def test_scan_exits_one_on_drift(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path, value=1.0)
    main(["scan", str(tmp_path), "--no-model"])
    _one_probe(tmp_path, value=9.0)
    assert main(["scan", str(tmp_path), "--no-model"]) == 1
    assert "Drift since" in capfd.readouterr().out


def test_scan_does_not_claim_a_review_that_did_not_happen(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The terminal renderer is the one a person actually reads, and it
    used to print `[medium]` under --no-model with nothing saying the
    severities came from the comparison rather than from a model."""
    _one_probe(tmp_path, value=1.0)
    main(["scan", str(tmp_path), "--no-model"])
    _one_probe(tmp_path, value=9.0)
    main(["scan", str(tmp_path), "--no-model"])
    out = capfd.readouterr().out
    assert "mechanical comparison only" in out
    assert "reviewed by an independent local model" not in out


def test_scan_json_stays_parseable(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A scan piped into another tool must not have prose in it."""
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model", "--format", "json"])
    payload = json.loads(capfd.readouterr().out)
    assert payload["snapshot"]["probes"]["tiny"]["summary"] == "ok"


def test_scan_agent_format_is_the_constraint_block(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model", "--format", "agent"])
    assert "VIBE SENTINEL: BASELINE RECORDED" in capfd.readouterr().out


def test_an_unknown_probe_id_is_an_error_not_an_empty_run(
    tmp_path: Path, logged: list[str]
) -> None:
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--no-model", "--probes", "nope"]) == 2
    assert "Unknown probe id" in "".join(logged)


def _seed_history(root: Path, values: list[float], key: str = "a") -> None:
    """Write runs straight into the database, oldest first.

    A fit needs twenty runs before it will say anything, and twenty real
    scans is twenty subprocesses and twenty gate passes to produce a
    series this can write in a millisecond. What the CLI tests are for is
    the wiring — that the flag reaches the fit and the fit reaches
    stdout — and the arithmetic has its own suite.
    """
    from vibe_sentinel.db import store as db_store
    from vibe_sentinel.schemas import DriftReport, Observation, ProbeResult, Snapshot

    init_db(db_path(root))
    with get_db(root) as conn:
        for value in values:
            db_store.save_run(
                conn,
                Snapshot(
                    root=str(root),
                    probes={
                        "tiny": ProbeResult(
                            probe_id="tiny",
                            observations=[Observation(key=key, value=value, label=key)],
                        )
                    },
                ),
                DriftReport(),
                None,
                make_baseline=True,
            )


def _backdate(root: Path, run_id: int, days: float) -> None:
    """Move a recorded run back in time, so a horizon can reach it.

    Every run a test makes lands inside the same second, and a horizon is
    a question about days. Rewriting the timestamp is the only way to ask
    it without waiting a week.
    """
    from datetime import UTC, datetime, timedelta

    at = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path(root)))
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (at, run_id))
    conn.commit()
    conn.close()


def test_scan_reports_the_declared_horizons_without_being_asked(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The shipped set is 1w and 1m, and a flag nobody types catches
    nothing — which is why they are not a flag."""
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model"])
    capfd.readouterr()
    main(["scan", str(tmp_path), "--no-model"])
    out = capfd.readouterr().out
    assert "Also moved, over longer horizons" in out
    assert "1w" in out and "1m" in out


def test_since_replaces_the_declared_horizons_for_one_run(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model"])
    capfd.readouterr()
    main(["scan", str(tmp_path), "--no-model", "--since", "2y,3y"])
    out = capfd.readouterr().out
    assert "2y" in out and "3y" in out
    assert "1w" not in out


def test_an_empty_since_turns_horizons_off_for_one_run(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Distinguished from "not given" by the empty string, which a
    truthiness test would fold into the default."""
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model"])
    capfd.readouterr()
    main(["scan", str(tmp_path), "--no-model", "--since", ""])
    assert "longer horizons" not in capfd.readouterr().out


def test_a_horizon_that_is_not_one_stops_the_scan_and_names_the_units(
    tmp_path: Path, logged: list[str]
) -> None:
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--no-model", "--since", "1 week"]) == 2
    assert "w (weeks)" in "".join(logged)


def test_a_horizon_reports_what_the_baseline_cannot_and_still_exits_zero(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The scan is clean against its baseline and the horizon is not, and
    0 is still the answer: a horizon reaches back to a fixed point, so a
    finding there would fail every scan until it aged out, with nothing
    anyone could do to clear it."""
    _one_probe(tmp_path, value=1.0)
    main(["scan", str(tmp_path), "--no-model"])
    _backdate(tmp_path, 1, days=2)

    _one_probe(tmp_path, value=9.0)
    main(["scan", str(tmp_path), "--no-model", "--update"])
    capfd.readouterr()

    assert main(["scan", str(tmp_path), "--no-model", "--since", "1d"]) == 0
    out = capfd.readouterr().out
    assert "No change since" in out
    assert "1d" in out
    assert "1 -> 9" in out


def test_a_failed_probe_makes_the_scan_exit_two(
    tmp_path: Path, logged: list[str]
) -> None:
    _config(
        tmp_path,
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "broken"\ntitle = "t"\n'
        'command = ["python", "-c", "raise SystemExit(3)"]\n',
    )
    assert main(["scan", str(tmp_path), "--no-model"]) == 2
    assert "probe(s) failed" in "".join(logged)


def test_list_probes_reports_the_effective_set(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--list-probes"]) == 0
    assert "tiny" in capfd.readouterr().out


def test_print_probes_emits_loadable_toml(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """It is meant to be piped into a config, so it has to parse as one."""
    import tomllib

    assert main(["scan", str(tmp_path), "--print-probes"]) == 0
    data = tomllib.loads(capfd.readouterr().out)
    assert {p["id"] for p in data["probe"]}


def test_print_example_emits_loadable_toml(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    import tomllib

    assert main(["scan", str(tmp_path), "--print-example"]) == 0
    tomllib.loads(capfd.readouterr().out)


# --- history and trends ----------------------------------------------------


def test_history_with_nothing_recorded_names_the_command(
    project: Path, logged: list[str]
) -> None:
    assert main(["history", str(project)]) == 2
    assert "vibe-sentinel scan" in "".join(logged)


def test_history_lists_the_run_it_compares_against(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _one_probe(tmp_path)
    main(["scan", str(tmp_path), "--no-model"])
    assert main(["history", str(tmp_path)]) == 0
    assert "*baseline" in capfd.readouterr().out


def test_trend_without_enough_history_says_so(project: Path, logged: list[str]) -> None:
    assert main(["trend", str(project)]) == 2
    assert "Not enough history" in "".join(logged)


def test_trend_needs_a_probe_alongside_a_key(project: Path, logged: list[str]) -> None:
    assert main(["trend", str(project), "--key", "a"]) == 2
    assert "--probe is required" in "".join(logged)


# --- the agent journal -----------------------------------------------------


def test_commands_with_nothing_recorded_names_the_hook(
    project: Path, logged: list[str]
) -> None:
    assert main(["commands", str(project)]) == 2
    assert "hook --install" in "".join(logged)


def test_hook_print_config_is_the_settings_fragment(
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert main(["hook", "--print-config"]) == 0
    entry = json.loads(capfd.readouterr().out)["hooks"]["PreToolUse"][0]
    assert entry["hooks"][0]["command"] == "vibe-sentinel hook"


def test_hook_install_writes_settings_and_is_idempotent(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert main(["hook", "--install", str(project)]) == 0
    assert "installed" in capfd.readouterr().out
    assert main(["hook", "--install", str(project)]) == 0
    assert "already installed" in capfd.readouterr().out


# --- safety ----------------------------------------------------------------

_CANARY = (
    '[safety]\nmode = "observe"\n\n'
    '[[danger]]\nid = "the-canary"\ntitle = "Canary"\n'
    "pattern = 'canary-probe'\nverdict = \"unsafe\"\n"
    'question = "The deliberate end-to-end test of this gate."\n'
)


def test_check_reaches_a_declared_verdict_without_a_model(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A declared verdict is the only kind of rule that still holds when
    the GPU is off, so it must not need one."""
    _config(project, _CANARY)
    assert main(["safety", str(project), "--check", "touch canary-probe.txt"]) == 1
    out = capfd.readouterr().out
    assert "verdict: unsafe" in out
    assert "declared; no model asked" in out


def test_a_check_is_recorded_and_marked_as_one(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """It is recorded, because a verdict reached while tuning is still a
    verdict. It is marked, because it is not something an agent ran — and
    for a while the marking was stored and never rendered, so the listing
    showed it as `main` doing the work."""
    _config(project, _CANARY)
    main(["safety", str(project), "--check", "touch canary-probe.txt"])
    capfd.readouterr()

    with get_db(project) as conn:
        (review,) = journal_store.list_reviews(conn)
    assert review.mode == "check"
    assert review.actor == "check"

    assert main(["safety", str(project)]) == 0
    listing = capfd.readouterr().out
    assert "[check]" in listing
    assert "typed at `safety --check`, not run by an agent" in listing


def test_a_command_triage_ignores_never_reaches_the_model(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert main(["safety", str(project), "--check", "pytest tests/ -q"]) == 0
    assert "not flagged" in capfd.readouterr().out


def test_print_dangers_shows_what_is_asked(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert main(["safety", str(project), "--print-dangers"]) == 0
    out = capfd.readouterr().out
    assert "deleting-files" in out
    assert "context signal" in out


def test_a_broken_danger_set_is_an_error_with_the_file_named(
    project: Path, logged: list[str]
) -> None:
    _config(project, '[[danger]]\nid = "no-question"\npattern = "x"\n')
    assert main(["safety", str(project), "--print-dangers"]) == 2
    assert "has no question" in "".join(logged)


def test_match_measures_a_pattern_against_real_history(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Writing a triage pattern is otherwise done blind."""
    _config(project, _CANARY)
    main(["safety", str(project), "--check", "touch canary-probe.txt"])
    capfd.readouterr()
    assert main(["safety", str(project), "--match", "canary"]) == 0
    assert "already flagged" in capfd.readouterr().out


# --- the gates -------------------------------------------------------------


def test_credentials_no_model_says_nobody_judged(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The CI path. Listing candidates is fine; letting the listing read
    as a verdict is not."""
    (tmp_path / ".env").write_text('AWS_SECRET_ACCESS_KEY="notarealkeyatall"\n')
    assert main(["credentials", str(tmp_path), "--no-model"]) == 1
    out = capfd.readouterr().out
    assert "NOT adjudicated" in out
    assert "adjudicated by nobody" in out


def test_credentials_redacts_what_it_prints(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The excerpt reaching stdout is the one guarantee this gate makes
    that nothing else can make for it."""
    secret = "sk-ant-" + "a1B2c3D4" * 6
    (tmp_path / "settings.py").write_text(f'API_KEY = "{secret}"\n')
    main(["credentials", str(tmp_path), "--no-model"])
    out = capfd.readouterr().out
    assert secret not in out
    assert "redacted" in out


def test_credentials_print_rules_lists_the_active_set(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert main(["credentials", str(tmp_path), "--print-rules"]) == 0
    assert "private-key-block" in capfd.readouterr().out


def test_a_pin_missing_its_reason_is_an_error(
    tmp_path: Path, logged: list[str]
) -> None:
    """Without a reason and a date a pin is an ignore, which none of these
    gates has."""
    _config(
        tmp_path,
        "[credentials]\n\n[[credentials.pin]]\n"
        'paths = ["x/*.pem"]\naccept = ["private-key-file"]\n',
    )
    assert main(["credentials", str(tmp_path), "--no-model"]) == 2
    assert "is missing reason, verified" in "".join(logged)


def test_licenses_without_a_policy_refuses_rather_than_allowing_everything(
    tmp_path: Path, logged: list[str]
) -> None:
    assert main(["licenses", str(tmp_path)]) == 2
    assert "allowed_categories" in "".join(logged)


def test_licenses_list_categories_works_with_no_policy_at_all(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """This is the command you run to find out what to write, which is
    exactly when there is nothing to load."""
    assert main(["licenses", str(tmp_path), "--list-categories"]) == 0
    assert "permissive" in capfd.readouterr().out


# --- the automatic health check --------------------------------------------


def test_the_automatic_check_cannot_change_an_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check people turn off protects nothing, and one that breaks the
    command they asked for is one they turn off."""
    from vibe_sentinel.db import maintenance

    def explode(*_args: object, **_kw: object) -> None:
        raise RuntimeError("the check itself is broken")

    monkeypatch.setattr(maintenance, "maybe_check", explode)
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--no-model"]) == 0


def test_the_automatic_check_never_writes_to_stdout(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """`scan --format json` piped somewhere has to stay parseable whatever
    the check finds, so its findings go to stderr through the logger."""
    _one_probe(tmp_path)
    _config(
        tmp_path,
        (tmp_path / ".vibe-sentinel.toml").read_text()
        + "\n[database]\nmax_journal_commands = 1\nbackup_max_age_days = 1\n",
    )
    main(["scan", str(tmp_path), "--no-model", "--format", "json"])
    json.loads(capfd.readouterr().out)


# --- dispatch --------------------------------------------------------------


def test_no_command_prints_help(capfd: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "vibe-sentinel" in capfd.readouterr().out


def test_backend_with_no_action_names_one(
    logged: list[str],
) -> None:
    assert main(["backend"]) == 2
    assert "backend status" in "".join(logged)


def test_list_probes_shows_the_command_that_will_run(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Not the template — the filled command. Parameters are declared, so
    this is the whole of what the next scan runs and there is nothing left
    for a model to decide between here and the run."""
    _config(
        tmp_path,
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "p"\ntitle = "t"\n'
        'command = ["echo", "{WORD}"]\n\n'
        '[[probe.placeholders]]\nname = "WORD"\ndescription = "a word"\n'
        'default = "hello"\n',
    )
    assert main(["scan", str(tmp_path), "--list-probes"]) == 0
    out = capfd.readouterr().out
    assert "WORD=hello" in out
    assert "echo hello" in out
    assert "{WORD}" not in out


def test_list_probes_quotes_a_value_that_would_read_as_shell(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A value may hold a space or a semicolon and still be one
    argument. Printed bare, the preview would read as two commands."""
    _config(
        tmp_path,
        "[probes]\nuse_builtins = false\n\n"
        '[[probe]]\nid = "p"\ntitle = "t"\n'
        'command = ["echo", "{SPEC}"]\n\n'
        '[[probe.placeholders]]\nname = "SPEC"\ndescription = "a spec"\n'
        'pattern = "^[\\\\w.*;= -]+$"\ndefault = "docs=*.md; code=*.py"\n',
    )
    assert main(["scan", str(tmp_path), "--list-probes"]) == 0
    out = capfd.readouterr().out
    assert "command:      echo 'docs=*.md; code=*.py'" in out


def test_scan_fits_the_history_without_being_asked(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Every step here is inside the tolerance, so the baseline
    comparison is calm and the series has trebled."""
    _seed_history(tmp_path, [4.0 + i for i in range(20)])
    # Same value the last run recorded, so the baseline comparison has
    # nothing to say and the section under test is the only one talking.
    _one_probe(tmp_path, value=23.0)
    assert main(["scan", str(tmp_path), "--no-model"]) == 0
    out = capfd.readouterr().out
    assert "Sustained trends" in out
    assert "rising" in out


def test_fit_zero_turns_the_fits_off_for_one_run(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _seed_history(tmp_path, [4.0 + i for i in range(20)])
    # Same value the last run recorded, so the baseline comparison has
    # nothing to say and the section under test is the only one talking.
    _one_probe(tmp_path, value=23.0)
    assert main(["scan", str(tmp_path), "--no-model", "--fit", "0"]) == 0
    assert "Sustained trends" not in capfd.readouterr().out


def test_a_negative_fit_window_is_refused(tmp_path: Path, logged: list[str]) -> None:
    _one_probe(tmp_path)
    assert main(["scan", str(tmp_path), "--no-model", "--fit", "-5"]) == 2
    assert "number of runs" in "".join(logged)


def test_a_trend_does_not_change_the_exit_code(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A slope persists for as many scans as the window is long, and
    nothing anyone could do would clear it."""
    _seed_history(tmp_path, [4.0 + i for i in range(20)])
    # Same value the last run recorded, so the baseline comparison has
    # nothing to say and the section under test is the only one talking.
    _one_probe(tmp_path, value=23.0)
    assert main(["scan", str(tmp_path), "--no-model"]) == 0
    assert "rising" in capfd.readouterr().out


def test_trend_reports_slopes_and_their_significance(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _seed_history(tmp_path, [4.0 + i for i in range(20)])
    assert main(["trend", str(tmp_path)]) == 0
    out = capfd.readouterr().out
    assert "Theil" in out and "Mann" in out
    assert "rising" in out
    assert "tau=" in out


def test_trend_on_one_key_prints_the_series_beside_the_fit(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _seed_history(tmp_path, [4.0 + i for i in range(20)])
    assert main(["trend", str(tmp_path), "--probe", "tiny", "--key", "a"]) == 0
    out = capfd.readouterr().out
    assert "run    1" in out
    assert "fit " in out


def test_trend_says_when_the_history_is_too_short_to_fit(
    tmp_path: Path, logged: list[str]
) -> None:
    """Refusing is the finding — three points make a line through
    anything — and 2 is what "could not answer" means for this command.
    The error names the way out, like every other one here."""
    _seed_history(tmp_path, [1.0, 2.0, 3.0])
    assert main(["trend", str(tmp_path)]) == 2
    logs = "".join(logged)
    assert "Not enough history" in logs
    assert "vibe-sentinel scan" in logs
