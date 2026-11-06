# Getting started

Start with the CLI to measure a repository and record its first baseline. Add
the hook if you also want to record or review agent actions. Run project
commands from the repository you want to inspect.

| Goal | Start here |
| --- | --- |
| Scan a repository | [Install the CLI](#1-install), [check the model backend](#2-start-the-model-backend) and [run your first scan](#3-run-your-first-scan) |
| Record or review agent commands | [Install the Claude Code or Cursor hook](#4-add-an-agent-hook) |
| Use Sentinel in Codex | [Install the Codex plugin](codex-plugin.md) |
| Work on Vibe Sentinel itself | [Set up the development environment](development.md) |

Version 0.6.0 includes the Codex integration, optional artifact readers,
historical evidence exports, CI summaries, and the offline viewer. For npm
advisories, selected JS/TS source rules, and Next.js export credential review,
see the [separate security command](security-checks.md).

## 1. Install

The scanner requires Python 3.13. Install it as a separate tool so it can run
against repositories that use a different Python version or no Python at all.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first if
needed.

```bash
uv tool install --python 3.13 --with-executables-from ast-grep-cli vibe-sentinel
vibe-sentinel --version
```

From a Sentinel checkout, use `.` in place of `vibe-sentinel` as the install
target. If you prefer pipx, use `pipx install --python 3.13 vibe-sentinel` and
install `ast-grep-cli` separately. The default `pattern-census` probe needs
ast-grep on PATH. A scan without it records that probe as unmeasured.

By default, the package, licence and dependency-version checks inspect the
scanner's installed Python environment. To audit your application's environment,
set `[environment] python` to its trusted interpreter. Selecting a project root
alone does not activate its virtualenv.
For supported targets and startup-hook coverage, see [measured environments](environments.md).

## 2. Start the model backend

Sentinel uses an OpenAI-compatible backend. Its defaults point to local Ollama
with `gemma4:26b-a4b-it-qat`. The recommended model uses about 15 GB for weights
and was tested on a machine with at least 32 GB of memory. Model comparisons and
tradeoffs are recorded in [historical eval results](eval-results.md).

Install [Ollama](https://ollama.com/download) for your platform, then pull the
model and check the connection.

```bash
ollama pull gemma4:26b-a4b-it-qat
vibe-sentinel backend status
```

Start Ollama first if it is not running. Look for `Status: ready` and confirm the
reported endpoint and model. On Windows, use WSL2 and run Ollama inside WSL2 so
Sentinel can reach it on loopback. Native Windows Python is untested.

A terminal-managed Ollama instance can use the following settings. A 32,768-token
context leaves room for larger scan reports. Keeping the model loaded avoids a
cold start on a time-limited safety review, at the cost of holding model memory.

```bash
OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_KEEP_ALIVE=2h ollama serve
```

For the macOS app, set these values with `launchctl setenv`, then reopen Ollama.
For a Linux service, set the environment in the service override and restart
that service. Check the loaded model and context with `ollama ps`.

### Use another endpoint or model

Edit the existing `[llm]` table in the target project's `.vibe-sentinel.toml`,
or add it if absent. Set the model name exactly as the backend reports it.

```toml
[llm]
endpoint = "http://localhost:11434/v1"
model = "the-model-you-serve"
```

For machine-specific settings without editing the file, use
`VIBE_SENTINEL_LLM_ENDPOINT`, `VIBE_SENTINEL_LLM_MODEL`,
`VIBE_SENTINEL_LLM_API_KEY` or `VIBE_SENTINEL_LLM_STRUCTURED_OUTPUT`. Empty
variables leave the file settings in effect. `backend status` lists active
overrides on its `Source:` line. Sending credential excerpts to a remote model
requires an explicit credential-policy setting.

`backend status` checks that the endpoint reports the configured model. To
check which Ollama build serves that name, compare the ID in `ollama list`.
If the server rejects the default `reasoning_effort = "none"` request field,
add an empty `[llm.extra_body]` table to remove it. If it rejects the structured
response format, try `structured_output = "json_object"` under `[llm]`, then
`"none"` if necessary. See [use of the local model](use-of-local-model.md).

## 3. Run your first scan

```bash
vibe-sentinel scan
```

The defaults measure the current repository. To see configuration options,
run `vibe-sentinel scan --print-example`. Copy the relevant settings into
`.vibe-sentinel.toml` and uncomment them. Keep any existing settings when you
edit the file.

A completed first scan records an initial baseline for later scans. Its report
says there was no baseline comparison yet, while current state gates can
already report findings. The licence gate requires a policy to evaluate.

### Read the scan outcome

| Exit | Meaning |
| --- | --- |
| `0` | The requested work completed without findings that affect the exit code. |
| `1` | The scan completed and reported findings. |
| `2` | Work failed or coverage was incomplete. Read the reported gap before drawing a conclusion. |

A newly enabled probe can succeed while the
baseline lacks a comparable measurement for it. The scan then exits 2 and
reports incomplete comparison. Review the probe scope, then run
`vibe-sentinel scan --update` when you intend to accept the current complete
measurements as the new baseline. The accepting run can still report the old
comparison as incomplete. If measurement or required judgement failed, fix
that first.

Baseline acceptance applies to structural drift. State-gate findings remain
visible and require their own resolution or scoped policy pin. You can run
`vibe-sentinel credentials`, `vibe-sentinel packages` or `vibe-sentinel licenses`
on its own without comparison history. See [gates](gates.md) and [drift](drift.md).

Run `vibe-sentinel scan` again after making changes. Use `vibe-sentinel trend`
to inspect movement across recorded runs. For automation, check the exit code
and use `--format json` to read structured results. You can also
[export recorded results](results.md#export-recorded-evidence) and
[render an aggregate CI summary](ci-evidence.md).

## 4. Add an agent hook

Choose the command for the agent you use and start with `observe` to see what
the safety policy would do on your own requests.

```bash
# Claude Code
vibe-sentinel hook --install --mode observe

# Cursor
vibe-sentinel hook --agent cursor --install --mode observe

vibe-sentinel status
vibe-sentinel commands --sessions
```

| Mode | What the hook does |
| --- | --- |
| `journal` | Records supported requests. |
| `observe` | Records requests and review decisions without blocking. |
| `enforce` | Blocks unsafe requests and handles missing verdicts according to the agent's capabilities. |

`status` reports the hook, effective mode, backend and recent session evidence.
Use `commands --sessions` to inspect the journal. Managed installation can set a
minimum mode outside the repository. For setup, coverage by agent and managed
policy, see [command safety](gates.md#command-safety).
