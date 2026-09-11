"""Config discovery and the backend-agnostic [llm] section."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibe_sentinel.config import (
    ENV_OVERRIDES,
    SentinelConfig,
    find_config,
    load_config,
)


def test_defaults_point_at_a_local_endpoint() -> None:
    config = SentinelConfig()
    assert config.llm_endpoint == "http://localhost:5001/v1"
    assert config.structured_output == "json_schema"
    assert config.config_path is None
    assert config.start_command == []


def test_find_config_walks_up_to_the_project_root(tmp_path: Path) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_config(nested) == tmp_path / ".vibe-sentinel.toml"


def test_find_config_does_not_reach_into_a_sibling(tmp_path: Path) -> None:
    """The walk goes up, never sideways — a sibling project's config must
    not silently govern this one."""
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    here = tmp_path / "here"
    here.mkdir()
    assert find_config(here) != tmp_path / "other" / ".vibe-sentinel.toml"


def test_load_config_reads_every_llm_key(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text(
        """
[llm]
endpoint = "http://gpu-box:11434/v1"
model = "qwen3:8b"
api_key = "sk-local"
timeout = 42.0
structured_output = "json_object"
temperature = 0.7
max_tokens = 4096
start_command = ["ollama", "serve"]
stop_command = ["pkill", "ollama"]

[llm.extra_body.chat_template_kwargs]
enable_thinking = true

[output]
format = "agent"

[logging]
level = "info"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.llm_endpoint == "http://gpu-box:11434/v1"
    assert config.llm_model == "qwen3:8b"
    assert config.api_key == "sk-local"
    assert config.llm_timeout == 42.0
    assert config.structured_output == "json_object"
    assert config.temperature == 0.7
    assert config.max_tokens == 4096
    assert config.start_command == ["ollama", "serve"]
    assert config.stop_command == ["pkill", "ollama"]
    assert config.extra_body == {"chat_template_kwargs": {"enable_thinking": True}}
    assert config.output_format == "agent"
    assert config.log_level == "INFO"
    assert config.config_path == path
    assert config.project_dir == tmp_path


def test_missing_keys_keep_their_defaults(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text('[llm]\nmodel = "llama3"\n', encoding="utf-8")
    config = load_config(path)
    assert config.llm_model == "llama3"
    assert config.llm_endpoint == SentinelConfig().llm_endpoint


def test_bad_structured_output_fails_at_load_not_at_first_call(
    tmp_path: Path,
) -> None:
    """A typo here would otherwise surface as a 4xx mid-scan."""
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text('[llm]\nstructured_output = "strict_json"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "none"])
def test_every_structured_output_mode_is_accepted(tmp_path: Path, mode: str) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text(f'[llm]\nstructured_output = "{mode}"\n', encoding="utf-8")
    assert load_config(path).structured_output == mode


def test_horizons_ship_with_a_value(tmp_path: Path) -> None:
    """Unlike the database ceilings, which start at 0 because a shipped
    threshold would be a claim about somebody else's project. A horizon is
    not a claim — no answer changes because it was declared — so the
    drift nobody would otherwise see is visible without configuring it."""
    assert SentinelConfig().drift_horizons == ["1w", "1m"]


def test_load_config_reads_the_drift_horizons(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text('[drift]\nhorizons = ["3d", "6mo"]\n', encoding="utf-8")
    assert load_config(path).drift_horizons == ["3d", "6mo"]


def test_horizons_can_be_turned_off(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text("[drift]\nhorizons = []\n", encoding="utf-8")
    assert load_config(path).drift_horizons == []


def test_a_misspelt_horizon_fails_at_load_not_at_the_first_scan(
    tmp_path: Path,
) -> None:
    """Same rule as structured_output: the config states what will
    happen, so a statement that cannot happen is an error while somebody
    is still looking at the file."""
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text('[drift]\nhorizons = ["1 week"]\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_trend_runs_ships_with_a_bounded_default(tmp_path: Path) -> None:
    """Bounded because the fit is quadratic in the length of a series,
    and because a direction averaged over two years is not one anybody
    can act on."""
    assert SentinelConfig().drift_trend_runs == 50


def test_load_config_reads_the_trend_window(tmp_path: Path) -> None:
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text("[drift]\ntrend_runs = 120\n", encoding="utf-8")
    assert load_config(path).drift_trend_runs == 120


def test_zero_trend_runs_turns_the_fits_off(tmp_path: Path) -> None:
    """The same shape as an empty `horizons`, and as the database
    ceilings: 0 means nothing was asked for, not that nothing was
    found."""
    path = tmp_path / ".vibe-sentinel.toml"
    path.write_text("[drift]\ntrend_runs = 0\n", encoding="utf-8")
    assert load_config(path).drift_trend_runs == 0


# --- the environment names the backend this machine can reach ---------------
#
# The config file is one checkout shared by everyone, and it pins a vLLM
# server on an NVIDIA card. A second contributor on a machine with neither
# had no way to run the tool at all without editing a file that states what
# the project pins rather than what their laptop can reach.


def test_the_environment_overrides_the_endpoint_the_file_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[llm]\nendpoint = "http://localhost:5001/v1"\nmodel = "qwen3-8b-fp8"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_SENTINEL_LLM_ENDPOINT", "http://localhost:11434/v1")
    monkeypatch.setenv("VIBE_SENTINEL_LLM_MODEL", "qwen3:14b")

    config = load_config(tmp_path / ".vibe-sentinel.toml")

    assert config.llm_endpoint == "http://localhost:11434/v1"
    assert config.llm_model == "qwen3:14b"


def test_an_override_names_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``backend status`` prints these, because an override the reader
    cannot see is the stale-backend failure with a config file as alibi."""
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("VIBE_SENTINEL_LLM_MODEL", "qwen3:14b")

    config = load_config(tmp_path / ".vibe-sentinel.toml")

    assert config.env_overrides == ["VIBE_SENTINEL_LLM_MODEL"]


def test_nothing_exported_is_not_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[llm]\nmodel = "qwen3-8b-fp8"\n', encoding="utf-8"
    )
    for variable in ENV_OVERRIDES:
        monkeypatch.delenv(variable, raising=False)

    config = load_config(tmp_path / ".vibe-sentinel.toml")

    assert config.llm_model == "qwen3-8b-fp8"
    assert config.env_overrides == []


def test_an_empty_variable_is_not_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``export FOO=`` is how a shell says "no value". Taken literally it
    would point the endpoint at the empty string and report a connection
    failure against nothing."""
    (tmp_path / ".vibe-sentinel.toml").write_text(
        '[llm]\nendpoint = "http://localhost:5001/v1"\n', encoding="utf-8"
    )
    monkeypatch.setenv("VIBE_SENTINEL_LLM_ENDPOINT", "   ")

    config = load_config(tmp_path / ".vibe-sentinel.toml")

    assert config.llm_endpoint == "http://localhost:5001/v1"
    assert config.env_overrides == []


def test_a_misspelt_override_fails_at_load_not_at_the_first_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule the file gets: a statement that cannot happen is an error
    while somebody is still looking at it."""
    (tmp_path / ".vibe-sentinel.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("VIBE_SENTINEL_LLM_STRUCTURED_OUTPUT", "json_schmea")

    with pytest.raises(ValidationError):
        load_config(tmp_path / ".vibe-sentinel.toml")


def test_the_environment_is_read_when_there_is_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running without a config file is supported, so the one case with
    nothing to override must not be the one case an override is ignored."""
    monkeypatch.setenv("VIBE_SENTINEL_LLM_ENDPOINT", "http://localhost:11434/v1")
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.config_path is None
    assert config.llm_endpoint == "http://localhost:11434/v1"


def test_no_override_reaches_the_probe_set_or_the_policies() -> None:
    """Four variables, all of them about which backend this machine can
    reach. The probes, the policies and the thresholds are statements
    about the project, identical on every machine — a second way to set
    one would be a second answer to a question that has one.

    ``allow_remote_model`` is absent for the reason it is not a
    command-line flag either: the credentials gate refuses a non-loopback
    endpoint whatever is set, and that refusal must not be reachable by
    exporting something.
    """
    assert set(ENV_OVERRIDES.values()) == {
        "llm_endpoint",
        "llm_model",
        "api_key",
        "structured_output",
    }
