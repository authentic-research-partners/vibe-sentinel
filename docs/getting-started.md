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

Then point the config at it:

```toml
[llm]
endpoint = "http://localhost:11434/v1"
model = "qwen3:14b"

# Qwen3 is a reasoning model and thinks before it answers. Nothing here needs
# that — the model reviews findings the deterministic checks already made — and
# on a laptop the tokens it spends thinking are the ones you wait for.
[llm.extra_body]
reasoning_effort = "none"
```

If a scan reports that the backend rejected the request, drop `[llm] structured_output` to `"json_object"`, then to `"none"` — the strongest mode a backend accepts is a property of that backend, not of the model.

No backend is required: `--no-model` skips the two steps that need one and says so in the report. See [use-of-local-model.md](use-of-local-model.md).

## 3. Find out what is true now

These checks need no configuration and no history. They answer on this run:

```bash
vibe-sentinel credentials     # keys and passwords sitting in the tree
vibe-sentinel packages        # imports that resolve to nothing, undeclared deps
vibe-sentinel licenses        # dependencies your policy would not accept
```

Each exits `0` clean, `1` with findings, `2` if the run itself failed. Whatever they find is what you would have shipped: remove the cause, or record a pin. A pin does not remove the finding — the gate still finds it and the report still prints it in a band of its own; what changes is that it stops counting towards the exit code, so the next person sees both the finding and the decision somebody made about it. What each check looks for, and what a pin must carry, are in [gates.md](gates.md).

## 4. Record the baseline

```bash
vibe-sentinel scan --print-example > .vibe-sentinel.toml
vibe-sentinel scan
```

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
| CI | the same, with `--no-model` — measuring is mechanical, so no backend is needed |
| Back into the coding session | `--format agent`, which renders findings as constraints for the agent that caused them |
| Another tool | `--format json`. The JSON always carries a `gates` key, empty rather than absent when nothing ran, so "no findings" is never confused with "not checked" |
