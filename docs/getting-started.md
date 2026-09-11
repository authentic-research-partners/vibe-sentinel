# Getting Started

Vibe Sentinel requires Python 3.13 and an OpenAI-compatible local backend such as vLLM, Ollama, llama.cpp, or LM Studio. Your project needs neither — this is a command you point at a repository, not a library you import into one.

All commands below run from the root of the repository you want to watch. Developing Vibe Sentinel itself requires a different setup — see [development.md](development.md).

## 1. Install

Install it *beside* your project rather than into it. The environment your code already uses — conda, venv, uv, poetry, pyenv — is left alone, and `vibe-sentinel` works from any directory.

```bash
# uv, once per machine
curl -LsSf https://astral.sh/uv/install.sh | sh                                    # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows

# the tool, once per machine
uv tool install vibe-sentinel
# or, from a clone of this repo: uv tool install .
```

`uv tool` fetches the 3.13 interpreter it needs and the probes run under it, so whatever Python your project runs on is left alone. If you already use pipx, `pipx install --python 3.13 vibe-sentinel` does the same.

Two checks are exceptions by design: `packages` and `licenses` read the installed distributions of the interpreter executing them, because what is installed is what will actually run — see [gates.md](gates.md). When run beside your project, they report on the tool's own dependencies rather than yours, so run those two from the environment you want audited — the second install method below.

If your project's own environment is already on Python 3.13 and you would rather have the tool inside it — to run it in CI alongside your other dev dependencies, for example — activate that environment and install there instead:

```bash
uv pip install git+https://github.com/authentic-research-partners/vibe-sentinel   # uv-managed venv
pip install git+https://github.com/authentic-research-partners/vibe-sentinel      # conda, venv, pyenv
```

## 2. Start the model backend

Any OpenAI-compatible endpoint works — vLLM, Ollama, llama.cpp, LM Studio. Point `[llm] endpoint` and `[llm] model` at whatever you serve. An 8B model is enough: a scan makes a few small calls, and none of them take long. On an Apple Silicon Mac, see [macOS — recommended setup](#macos--recommended-setup) below.

A known-good setup, on an NVIDIA GPU with about 20GB, under Podman — Docker takes the same arguments with `--gpus all` in place of `--device`:

```bash
podman run -d --name vibe-sentinel-llm --device nvidia.com/gpu=all \
    -p 5001:8000 -v ~/.cache/huggingface:/root/.cache/huggingface \
    docker.io/vllm/vllm-openai:v0.18.0 RedHatAI/Qwen3-8B-FP8-dynamic \
    --served-model-name qwen3-8b-fp8 --max-model-len 40960 \
    --gpu-memory-utilization 0.85 --kv-cache-dtype fp8
```

Put that argv in `[llm] start_command` and the tool runs it for you:

```bash
vibe-sentinel backend start
vibe-sentinel backend status
```

Once it is up, `backend monitor` says what it is doing:

```bash
vibe-sentinel backend monitor              # one snapshot
vibe-sentinel backend monitor --count 300  # watch a scan; Ctrl-C to stop
```

```
  requests running    3
  requests waiting    2   <- queueing: the fan-out is wider than the server will batch
  KV cache in use     7.4%
  prefix cache hits   91.0%  (910 of 1,000)
  answers finished    420
  cut off at the      138   <- each one costs a retry at double the budget
    token ceiling
```

Those last two rows are the ones worth acting on. A **waiting** count above
zero means the `concurrency` settings are asking for more than the server will
batch at once. A **cut off** count means answers are hitting `[llm] max_tokens`
and being retried at double the budget, so each of those candidates cost two
generations instead of one — raise `max_tokens` rather than live with it.

This reads a Prometheus `/metrics` endpoint beside the OpenAI-compatible one.
vLLM publishes one; Ollama, llama.cpp and LM Studio do not, and on those the
rows read `unavailable` rather than zero — a server nobody can see is not a
server with an empty queue. Nothing in a scan reads any of it.

Two things about that container are worth knowing before you trust a scan.

**`backend status` proves that *something* answers, not that it is yours.** It
asks `/v1/models` and reports what comes back, so any server on that port
serving that `--served-model-name` reads as `ready` — including one started by
another project, on a different image, with different flags. An *absent*
backend is caught by the connection error the tool raises; a *substituted* one
is caught by nothing. This is not hypothetical: it is how a set of timings in
`benchmarks/results/` were nearly recorded against the wrong vLLM version, with
double the KV cache of the container the config named. If a number surprises
you, check what is actually serving:

```bash
podman ps --format '{{.Names}} {{.Image}} {{.Ports}}'
```

**`--restart unless-stopped` does not survive a reboot on its own.** Rootless
podman restarts containers through a user service that ships disabled:

```bash
systemctl --user enable --now podman-restart.service
loginctl enable-linger "$USER"    # so it starts without you logged in
```

Without both, the container comes back only when you start it — which
`vibe-sentinel backend start` does.

### Why there is no compose file

There is one container and no service graph, which is the thing compose exists
to describe. Splitting the argv across a second file also costs the property
worth having here: `[llm] start_command` is the only place the image version
and the serving flags are written down, so the config the tool reads and the
container it talks to cannot drift apart without it being visible in one diff.
Nothing is built either — the image is pulled from Docker Hub as published, so
there is no `Containerfile` or `Dockerfile` to keep.

### macOS — recommended setup

On Apple Silicon Macs, the recommended local backend is **Ollama**. It is native on Apple Silicon and exposes the local API Vibe Sentinel needs. Nothing here is Ollama-specific — vLLM and the rest work too — but vLLM's Apple Silicon support is experimental and needs a source build, so Ollama is the path these instructions take.

Vibe Sentinel does not need a large model. The model only reviews findings already produced by deterministic checks, so an 8B model is already enough; a 12–14B model is the comfortable default on a Mac with the memory for it.

#### 1. Install Ollama

Install Ollama for macOS from [ollama.com/download/mac](https://ollama.com/download/mac).

After installation, confirm it is available:

```bash
ollama --version
```

If Ollama is not already running in the background, start it:

```bash
ollama serve
```

#### 2. Install the recommended model

The default recommendation is Qwen3 14B. Any 12–14B local model works just as well — pick a family different from the one that writes your code:

```bash
ollama pull qwen3:14b
```

The standard Ollama build is quantized and uses roughly 9 GB for the model, so it runs comfortably on Macs with 16 GB or more unified memory while leaving room for the editor, coding agent, and Vibe Sentinel.

#### 3. Test the model

```bash
ollama run qwen3:14b
```

This opens a chat prompt. Type anything to confirm the model answers, then type `/bye` to leave it — Vibe Sentinel talks to Ollama in the background and does not need this window open.

#### 4. Tell Vibe Sentinel where the model is

Ollama is now running, but Vibe Sentinel does not know that yet. Out of the box it looks for a model on port 5001, which is where the vLLM setup above listens. Ollama listens on port **11434**, so you have to say so.

You do that in a file named `.vibe-sentinel.toml`, which lives in the project you want to check — not in Vibe Sentinel's own directory. Change into that project first:

```bash
cd ~/path/to/your-project
```

Then run this. It adds the settings to `.vibe-sentinel.toml`, creating the file if it is not there yet:

```bash
cat >> .vibe-sentinel.toml <<'EOF'

[llm]
endpoint = "http://localhost:11434/v1"
model = "qwen3:14b"

# Qwen3 is a reasoning model and thinks before it answers. Nothing here needs
# that — the model reviews findings the deterministic checks already made — and
# on a laptop the tokens it spends thinking are the ones you wait for.
[llm.extra_body]
reasoning_effort = "none"
EOF
```

Copy the whole thing, `EOF` lines included, and paste it into your terminal in one go.

The `model` value has to be the exact name Ollama knows the model by. Run `ollama list` and copy the name from the first column. If you pulled `qwen3:14b` above, it is `qwen3:14b` — the `:14b` part matters.

#### When the project's config is not yours to change

`.vibe-sentinel.toml` is usually checked in, so it says what the *project*
judges with — the same for everyone who clones it. If that pins a backend your
machine cannot reach, four environment variables override the `[llm]` wiring
without touching the file:

| Variable | Sets |
|---|---|
| `VIBE_SENTINEL_LLM_ENDPOINT` | `endpoint` |
| `VIBE_SENTINEL_LLM_MODEL` | `model` |
| `VIBE_SENTINEL_LLM_API_KEY` | `api_key` |
| `VIBE_SENTINEL_LLM_STRUCTURED_OUTPUT` | `structured_output` |

```bash
export VIBE_SENTINEL_LLM_ENDPOINT=http://localhost:11434/v1
export VIBE_SENTINEL_LLM_MODEL=qwen3:14b
```

`backend status` prints a `Source:` line naming the ones in force — worth
reading, for the same reason as the warning above: a backend answering under
the expected name while being something else is the failure that check exists
to catch, and an override you cannot see is that failure with a config file as
its alibi.

Only those four. The probes, the policies and the thresholds have no variable:
they are statements about the project, identical on every machine, and a second
way to set one would be a second answer to a question that has one. Nor does
`allow_remote_model` — the credentials gate refuses a non-loopback endpoint
whatever is set, and that refusal is not reachable by exporting something.

Setting one to nothing (`export VIBE_SENTINEL_LLM_MODEL=`) is how a shell says
"no value", so it leaves the file's setting standing.

These override the file but not the reasoning setting above, which has no
variable. On a laptop, put `reasoning_effort = "none"` in a config the way step
4 shows — without it Qwen3 thinks before every answer, and on a repository with
several declared lenses that is most of the wait.

#### 5. Check that it worked

```bash
vibe-sentinel backend status
```

You want to see your endpoint and `Status: ready`:

```
Endpoint: http://localhost:11434/v1
Model:    qwen3:14b
Status:   ready — ...
```

Two things that commonly go wrong:

- **It still says `http://localhost:5001/v1`.** Then it did not read your settings. The usual cause is running the command from a different directory than the one holding `.vibe-sentinel.toml`. Check the file has an active block with `grep -n -A6 '^\[llm\]' .vibe-sentinel.toml` — if that prints nothing, the settings are not there, so run the `cat >>` command above from inside the project.
- **It says `unreachable`.** Ollama is not running. Start it with `ollama serve` and try again.

The line `No [llm] start_command configured` is not an error and needs no fixing. It only means you start Ollama yourself, which is what the Ollama app does for you.

One more, only if a later scan complains that the backend rejected the request: add `structured_output = "json_object"` inside the `[llm]` block, and `"none"` if that still fails. How strictly a backend can be held to a JSON shape is a property of the backend, not the model.

A backend is required. Rating a change and adjudicating a credential candidate are half of what this tool does, so a run that cannot reach a model exits 2 and names the fix rather than reporting the mechanical half as though it were the whole answer. See [use-of-local-model.md](use-of-local-model.md).

## 3. Find out what is true now

These checks need no config file and no history — they answer on this run,
against the backend from step 2:

```bash
vibe-sentinel credentials     # keys and passwords sitting in the tree
vibe-sentinel packages        # imports that resolve to nothing, undeclared deps
vibe-sentinel licenses        # dependencies your policy would not accept
```

Each exits `0` clean, `1` with findings, `2` if the run itself failed — and a
backend it cannot reach is a run that failed. `credentials` puts every candidate
to the model, because deciding which pattern hits are real is half of what it
does; `packages` asks only about two installed names one edit apart; `licenses`
resolves without asking anyone and uses the model only for the note on a drafted
pin. There is no flag that skips them. Whatever they find is what you would have shipped: remove the cause, or record a pin. A pin does not remove the finding — the gate still finds it and the report still prints it in a band of its own; what changes is that it stops counting towards the exit code, so the next person sees both the finding and the decision somebody made about it. What each check looks for, and what a pin must carry, are in [gates.md](gates.md).

## 4. Record the baseline

```bash
vibe-sentinel scan --print-example > .vibe-sentinel.toml
vibe-sentinel scan
```

**If you already made a `.vibe-sentinel.toml` while setting up the backend, skip the first line** — `>` overwrites the file, and your `[llm]` settings would go with it. To see the example without losing anything, run `vibe-sentinel scan --print-example` on its own and read it on screen. Every line it prints starts with a `#`, which means the setting is switched off and shown only as documentation; a setting takes effect once the `#` in front of it is gone.

The config declares which probes run and where they point; the defaults measure the current directory and are usually right to start with. This first scan tells you nothing about drift — that is the point of a baseline — but it runs the three gates above, so it is still useful.

## 5. Wire in the journal

```bash
vibe-sentinel hook --install     # records every tool call to .claude/settings.json
vibe-sentinel commands --sessions
```

Recording is all it does, at ~52 ms per hooked tool call. The safety gate that reads the journal ships as `off`; turn it on with `[safety] mode = "observe"` to see verdicts without blocking anything, and `"enforce"` when you want it to refuse commands.

## 6. Check for drift later

```bash
vibe-sentinel scan       # baseline, 1w and 1m horizons, and the fitted trend
vibe-sentinel trend      # movement across many runs
```

A scan that finds drift does not become the new baseline. `scan --update` accepts what it found and moves it — accepting drift is a deliberate act. The probes, the horizons, and the trend fit are in [drift.md](drift.md).

## Routing it somewhere

None of the checks stops anything on its own; each exits `1` when it has findings, and that is the whole interface. A check becomes a gate when you route it:

| Where | How |
| --- | --- |
| Pre-commit | run `vibe-sentinel scan` (or a single gate) in the hook; a non-zero exit stops the commit |
| CI | the same — the runner needs a reachable `[llm] endpoint`, because a scan that could not ask is a scan that failed |
| Back into the coding session | `--format agent`, which renders findings as constraints for the agent that caused them |
| Another tool | `--format json`. The JSON always carries a `gates` key, empty rather than absent when nothing ran, so "no findings" is never confused with "not checked" |
