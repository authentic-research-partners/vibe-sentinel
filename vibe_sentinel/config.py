"""TOML-based configuration for vibe-sentinel.

Looks for ``.vibe-sentinel.toml`` in the current directory, then parent
directories (like ``.eslintrc``). Falls back to built-in defaults.

One file holds everything: the judge-model wiring (``[llm]``) and the
probe set (``[[probe]]``). Probes are parsed by
:mod:`vibe_sentinel.templates`; this module owns only the runtime knobs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from vibe_sentinel.horizons import parse_horizon
from vibe_sentinel.paths import CONFIG_FILENAME

StructuredOutput = Literal["json_schema", "json_object", "none"]
SafetyMode = Literal["off", "observe", "enforce"]


class SentinelConfig(BaseModel):
    """Resolved configuration for a vibe-sentinel run."""

    # Where the TOML was found (None when running on built-in defaults)
    config_path: Path | None = None
    project_dir: Path = Path(".")

    # --- Output ---
    output_format: str = "terminal"
    color: bool = True

    # --- Drift horizons ---
    #
    # The baseline is one point in time, and one point can only show
    # drift at one scale. A directory gaining two modules a week is under
    # every tolerance between consecutive scans and a reorganisation by
    # Christmas — and which scale it shows up at is not something you know
    # in advance, so a scan that reports one is still reporting the wrong
    # one half the time. Each horizon here is compared as well as the
    # baseline, against the newest run at least that old.
    #
    # Unlike every threshold below, these ship with a value. They are not
    # a claim about your project — no answer changes because you declared
    # them — they cost nothing (both ends are already recorded, and no
    # model is asked), and a horizon nothing reaches back to says so
    # rather than reading as clean. Set it to [] to turn them off.
    drift_horizons: list[str] = Field(default_factory=lambda: ["1w", "1m"])

    #: How many recent runs a trend fit looks back over. 0 turns the fits
    #: off entirely, the same way an empty ``horizons`` does.
    #:
    #: A horizon compares two points; a fit reads all of them, which is
    #: the only way a *direction* is visible at all. Bounded because the
    #: fit is quadratic in the length of a series and because a slope
    #: averaged over two years is not one anybody can act on — the
    #: question is what the last fifty runs did.
    drift_trend_runs: int = 50

    #: A standing note about how this project wants its drift report
    #: read, in front of every lens's question. The licence gate's
    #: ``guidance`` convention: a thing you would otherwise repeat in
    #: each ``[[lens]]`` belongs once, above all of them.
    drift_guidance: str = ""
    #: How many lenses are asked at once. Each is its own request sharing
    #: a cached prefix — the report is byte-identical across the fan-out —
    #: so this is free against a server that batches. vLLM does. Ollama
    #: serialises regardless; set it to 1 there.
    drift_concurrency: int = 4

    # --- The safety gate ---
    #
    # off:     triage never runs; the hook only records. The default,
    #          because a gate nobody asked for that adds seconds to a
    #          tool call is a gate that gets uninstalled.
    # observe: flagged commands are reviewed and the verdict recorded.
    #          Nothing is blocked. This is how you measure the false
    #          positive rate on your own traffic before trusting it.
    # enforce: an `unsafe` verdict denies the tool call.
    safety_mode: SafetyMode = "off"
    #: How many of the actor's own previous commands the review sees.
    #: Long enough to resolve a variable set earlier, short enough for a
    #: local model to read quickly.
    safety_history: int = 100
    #: Hard wall-clock ceiling on one verdict, retries included. Must
    #: stay comfortably under ``hook.HOOK_TIMEOUT`` (the 10 s Claude Code
    #: allows the hook), because the tool call is blocked while this
    #: runs. Running out means the command proceeds and is recorded as
    #: unreviewed — never blocked because a model was slow.
    safety_timeout: float = 8.0
    #: How many of a command's questions are asked at once. Each question
    #: is its own request sharing a cached prefix, so this is free against
    #: a server that batches — vLLM does. Ollama serialises regardless;
    #: set it to 1 there and expect the latency to add up.
    safety_concurrency: int = 4

    # --- The history database ---
    #
    # Every threshold below is a ceiling *you* declare, not one this tool
    # ships. A default that flags a 100 MB database would be wrong for a
    # team that has journalled two years of agent work, and a default
    # that never flags anything is not a check. So the size and volume
    # ceilings start at 0 — meaning "no ceiling declared, do not report
    # on it" — and the ones with a real answer (is the file corrupt, are
    # the indexes there, has it ever been backed up) always run.
    #: Run a health check when one has not run in this long. Read before
    #: any command that touches the database, so a project scanned hourly
    #: still pays for one check a day.
    db_check_interval_hours: float = 24.0
    #: False turns the automatic check off entirely. `vibe-sentinel db
    #: check` still works — this only governs the one that runs itself.
    db_auto_check: bool = True
    #: Report when the database (file plus WAL) passes this. 0 = never.
    db_max_size_mb: int = 0
    #: Report when the journal holds more tool calls than this. 0 = never.
    db_max_journal_commands: int = 0
    #: Report journal records older than this, and the default cutoff for
    #: `vibe-sentinel db prune`. 0 = keep everything, report nothing.
    db_journal_retention_days: int = 0
    #: Report when the newest backup is older than this. 30 by default
    #: because this file is the one artifact here that cannot be
    #: regenerated, and noticing that a month late is the whole failure.
    db_backup_max_age_days: int = 30

    # --- Logging ---
    log_level: str = "DEBUG"  # file sink level; stderr sink stays at INFO

    # --- The judge model ---
    #
    # Any OpenAI-compatible endpoint: vLLM, Ollama, llama.cpp's server,
    # LM Studio, LocalAI. Nothing here is specific to one of them.
    llm_endpoint: str = "http://localhost:5001/v1"
    #: Model name as the backend serves it — `ollama list`, or vLLM's
    #: --served-model-name. Not an alias this tool resolves; whatever
    #: string your server answers to.
    llm_model: str = "qwen3"
    #: Most local servers ignore this. Set it for one that doesn't.
    api_key: str = ""
    #: Whole-request timeout in seconds. Generous by default because a
    #: cold backend may be loading weights on the first call.
    llm_timeout: float = 300.0

    #: How strictly the backend can be held to the JSON shape. Start at
    #: json_schema; drop to json_object, then none, if yours rejects it.
    #: Responses are validated against the Pydantic model either way, so
    #: a weaker mode costs retries, never correctness.
    structured_output: StructuredOutput = "json_schema"

    temperature: float = 0.2
    max_tokens: int = 2048

    #: Passed into the request body untouched — a reasoning toggle, a
    #: sampler setting, anything one backend supports and others don't.
    #: The escape hatch that keeps backend-specific support out of the
    #: client. Example for a Qwen3 vLLM server::
    #:
    #:     [llm.extra_body.chat_template_kwargs]
    #:     enable_thinking = true
    extra_body: dict[str, Any] = Field(default_factory=dict)

    #: Optional command that starts your backend, run verbatim by
    #: ``vibe-sentinel backend start``. Empty means "I start it myself".
    #: A list, never a shell string — no shell is involved.
    start_command: list[str] = Field(default_factory=list)
    #: Optional matching stop command.
    stop_command: list[str] = Field(default_factory=list)

    @field_validator("drift_horizons")
    @classmethod
    def _horizons_parse(cls, value: list[str]) -> list[str]:
        """A misspelt horizon fails at load, not at the first scan.

        Same rule as ``structured_output``: the config is the statement of
        what will happen, so a statement that cannot happen is an error
        while somebody is still looking at the file.
        """
        for horizon in value:
            parse_horizon(horizon)
        return value


def is_wsl2() -> bool:
    """Detect WSL2 via /proc/version (contains 'microsoft')."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        # OSError here means /proc/version is absent — i.e. not Linux,
        # therefore not WSL2. The negative result IS the answer.
        return False


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for the config file."""
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(path: Path | None = None) -> SentinelConfig:
    """Load configuration from a TOML file, or use defaults.

    If ``path`` is None, searches for ``.vibe-sentinel.toml`` via
    :func:`find_config`. Running without a config file is supported: the
    defaults point at a local server on port 5001 and the shipped probes.
    """
    config = SentinelConfig()

    toml_path = path or find_config()
    if toml_path is None:
        return config

    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    config.config_path = toml_path
    config.project_dir = toml_path.parent

    output = data.get("output", {})
    if "format" in output:
        config.output_format = output["format"]
    if "color" in output:
        config.color = bool(output["color"])

    drift = data.get("drift", {})
    if "horizons" in drift:
        config.drift_horizons = [str(h) for h in drift["horizons"]]
    if "trend_runs" in drift:
        config.drift_trend_runs = int(drift["trend_runs"])
    if "guidance" in drift:
        config.drift_guidance = str(drift["guidance"])
    if "concurrency" in drift:
        config.drift_concurrency = int(drift["concurrency"])

    safety = data.get("safety", {})
    if "mode" in safety:
        config.safety_mode = safety["mode"]
    if "history" in safety:
        config.safety_history = int(safety["history"])
    if "timeout" in safety:
        config.safety_timeout = float(safety["timeout"])
    if "concurrency" in safety:
        config.safety_concurrency = int(safety["concurrency"])

    database = data.get("database", {})
    if "check_interval_hours" in database:
        config.db_check_interval_hours = float(database["check_interval_hours"])
    if "auto_check" in database:
        config.db_auto_check = bool(database["auto_check"])
    if "max_size_mb" in database:
        config.db_max_size_mb = int(database["max_size_mb"])
    if "max_journal_commands" in database:
        config.db_max_journal_commands = int(database["max_journal_commands"])
    if "journal_retention_days" in database:
        config.db_journal_retention_days = int(database["journal_retention_days"])
    if "backup_max_age_days" in database:
        config.db_backup_max_age_days = int(database["backup_max_age_days"])

    logging_section = data.get("logging", {})
    if "level" in logging_section:
        config.log_level = str(logging_section["level"]).upper()

    llm = data.get("llm", {})
    if "endpoint" in llm:
        config.llm_endpoint = llm["endpoint"]
    if "model" in llm:
        config.llm_model = llm["model"]
    if "api_key" in llm:
        config.api_key = llm["api_key"]
    if "timeout" in llm:
        config.llm_timeout = float(llm["timeout"])
    if "structured_output" in llm:
        config.structured_output = llm["structured_output"]
    if "temperature" in llm:
        config.temperature = float(llm["temperature"])
    if "max_tokens" in llm:
        config.max_tokens = int(llm["max_tokens"])
    if "extra_body" in llm:
        config.extra_body = dict(llm["extra_body"])
    if "start_command" in llm:
        config.start_command = list(llm["start_command"])
    if "stop_command" in llm:
        config.stop_command = list(llm["stop_command"])

    # Re-validate: the assignments above bypass field validation, and a
    # bad structured_output should fail at load, not at the first call.
    return SentinelConfig.model_validate(config.model_dump())
