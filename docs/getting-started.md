# Getting Started

Vibe Sentinel requires Python 3.13 and an OpenAI-compatible local backend — Ollama, llama.cpp, LM Studio, or anything else that serves one. Your project needs neither — this is a command you point at a repository, not a library you import into one.

All commands below run from the root of the repository you want to watch. Developing Vibe Sentinel itself requires a different setup — see [development.md](development.md).

## 1. Install

Install it *beside* your project rather than into it. The environment your code already uses — conda, venv, uv, poetry, pyenv — is left alone, and `vibe-sentinel` works from any directory.

```bash
# uv, once per machine
curl -LsSf https://astral.sh/uv/install.sh | sh                                    # macOS / Linux
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows

# the tool, once per machine
uv tool install --with-executables-from ast-grep-cli vibe-sentinel
# or, from a clone: uv tool install --with-executables-from ast-grep-cli .
```

`uv tool` fetches the 3.13 interpreter it needs and the probes run under it, so whatever Python your project runs on is left alone. If you already use pipx, `pipx install --python 3.13 vibe-sentinel` does the same.

`ast-grep-cli` supplies the executable required by the built-in `pattern-census`
probe. `--with-executables-from` exposes its command on PATH as described in
[uv's tool documentation](https://docs.astral.sh/uv/concepts/tools/#installing-executables-from-additional-packages).
If ast-grep is already on PATH, omit that option. With pipx, install
`ast-grep-cli` as a separate tool. A default scan without ast-grep records
`pattern-census` as unmeasured.

Three checks inspect the running interpreter's environment: `packages`,
`licenses`, and the `dependency-versions` probe. A separate tool install reads
its own installed dependencies, not those of the repository you point it at.
Use the environment install below to audit your project's installed set.

If your project's own environment is already on Python 3.13 and you would rather have the tool inside it — to run it in CI alongside your other dev dependencies, for example — activate that environment and install there instead:

```bash
uv pip install git+https://github.com/authentic-research-partners/vibe-sentinel   # uv-managed venv
pip install git+https://github.com/authentic-research-partners/vibe-sentinel      # conda, venv, pyenv
```

## 2. Start the model backend

These instructions use **Ollama**, and the tool's defaults point at it: Ollama's own port, and the model below. It runs on macOS, Linux and Windows and serves the same local endpoint on each. Nothing in the tool is specific to it — any OpenAI-compatible endpoint works, such as llama.cpp or LM Studio — so if you already serve models another way, point `[llm] endpoint` and `[llm] model` at it as [step 4](#4-tell-vibe-sentinel-about-the-model) shows.

The recommended model is **Gemma 4 26B in its QAT build**, about 15 GB, so a machine with 32 GB of memory or more. In the measurements in [evals.md](evals.md) it judged commands and package names best of the models tried, and ran fastest; Qwen3 14B rated code changes better, but called two dangerous commands safe. Smaller models run, and measured weaker: Qwen3 8B, the previous recommendation, let a misspelled package name through as a real one.

Tested on Linux, macOS, and Windows under WSL2. Native Windows — a Windows Python in PowerShell — is untested.

#### 1. Install and start Ollama

- **macOS:** download it from [ollama.com/download/mac](https://ollama.com/download/mac).
- **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`
- **Windows:** run Vibe Sentinel under WSL2 and install Ollama *inside* WSL2 with the Linux command. An Ollama installed on the Windows side is not on `localhost` from inside WSL2 under its default networking, and the address that does reach it is not loopback — so the credentials gate, which only sends an excerpt to a model on this machine, refuses it.

After installation, confirm it is available:

```bash
ollama --version
```

**Give it a large enough context before you start it.** Ollama sizes the context it reads to the GPU memory it finds, and below 24 GB that is 4,096 tokens — which includes most 32 GB Macs, whose GPU gets only part of the memory. A question longer than that is cut to fit without an error, and the answer comes back about half the report while reading like an answer about all of it. The questions this tool asks run past 6,000 tokens on a mid-sized repository, so set 32,768:

```bash
# Ollama started from a terminal
OLLAMA_CONTEXT_LENGTH=32768 ollama serve

# The macOS app: set it, then quit Ollama from the menu bar and open it again
launchctl setenv OLLAMA_CONTEXT_LENGTH 32768

# Linux, where the installer set Ollama up as a service
sudo systemctl edit ollama     # add, under [Service]:  Environment="OLLAMA_CONTEXT_LENGTH=32768"
sudo systemctl restart ollama
```

`ollama ps` shows the context in force once a model is loaded, in the `CONTEXT` column.

**And keep the model loaded between checks.** Ollama unloads a model after five idle minutes by default, and loading it again takes longer than the safety gate's eight seconds — so the first flagged command after a pause gets no verdict, and under `enforce` you are asked about it rather than told. The OpenAI-compatible endpoint this tool uses ignores a per-request `keep_alive`, so it has to be set on the server, the same way as the context:

```bash
OLLAMA_KEEP_ALIVE=2h OLLAMA_CONTEXT_LENGTH=32768 ollama serve   # from a terminal
launchctl setenv OLLAMA_KEEP_ALIVE 2h                            # the macOS app, then reopen it
```

The cost is the model's memory, about 15 GB, held while you are not using it — which is why two hours rather than a day: long enough to cover an ordinary break, short enough to hand the memory back when you stop for the evening. `-1` keeps the model loaded until the server stops.

#### 2. Install the recommended model

```bash
ollama pull gemma4:26b-a4b-it-qat
```

Pick a family different from the one that writes your code: a judge that shares the agent's blind spots about what normal code looks like is not much of a second opinion.

#### 3. Test the model

```bash
ollama run gemma4:26b-a4b-it-qat
```

This opens a chat prompt. Type anything to confirm the model answers, then type `/bye` to leave it — Vibe Sentinel talks to Ollama in the background and does not need this window open.

#### 4. Tell Vibe Sentinel about the model

If you pulled `gemma4:26b-a4b-it-qat` into Ollama, there is nothing to set: the defaults name that model on Ollama's port, and they switch its thinking off. Gemma 4 thinks before it answers unless told not to, and with thinking on, 26 of 27 safety verdicts missed their 8-second timeout in the evals — the gate in front of an agent's commands would stop working.

To use a different model or backend, say so in a file named `.vibe-sentinel.toml`, which lives in the project you want to check — not in Vibe Sentinel's own directory:

```bash
cd ~/path/to/your-project
cat >> .vibe-sentinel.toml <<'EOF'

[llm]
endpoint = "http://localhost:11434/v1"
model = "the-model-you-serve"
EOF
```

Copy the whole thing, `EOF` lines included, and paste it into your terminal in one go. The `model` value has to be the exact name the backend knows the model by — for Ollama, the first column of `ollama list`.

Thinking is switched off by sending `reasoning_effort = "none"` with every question. Ollama and llama.cpp's server honour it. If your server rejects it, the error names it; add an empty `[llm.extra_body]` table to the file, which replaces the default and sends nothing. The same empty table turns thinking back on, if you want it and can wait for it.

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
export VIBE_SENTINEL_LLM_MODEL=gemma4:26b-a4b-it-qat
```

`backend status` prints a `Source:` line naming the ones in force — worth
reading, for the reason under step 5: a backend answering under
the expected name while being something else is the failure that check exists
to catch, and an override you cannot see is that failure with a config file as
its alibi.

Only those four. The probes, the policies and the thresholds have no variable:
they are statements about the project, identical on every machine, and a second
way to set one would be a second answer to a question that has one. `allow_remote_model` also has no environment override: permitting credential
excerpts to reach a remote endpoint requires an explicit credential-policy edit.

Setting one to nothing (`export VIBE_SENTINEL_LLM_MODEL=`) is how a shell says
"no value", so it leaves the file's setting standing.

These override the file but not the thinking setting above, which has no
variable and needs none: it is off unless a config turns it back on.

#### 5. Check that it worked

```bash
vibe-sentinel backend status
```

You want to see your endpoint and `Status: ready`:

```
Endpoint: http://localhost:11434/v1
Model:    gemma4:26b-a4b-it-qat
Status:   ready — ...
```

Two things that commonly go wrong:

- **It shows a different endpoint or model than you set.** Then it did not read your settings. The usual cause is running the command from a different directory than the one holding `.vibe-sentinel.toml`. Check the file has an active block with `grep -n -A6 '^\[llm\]' .vibe-sentinel.toml` — if that prints nothing, the settings are not there, so run the `cat >>` command above from inside the project.
- **It says `unreachable`.** Ollama is not running. Start it with `ollama serve` and try again.

The line `No [llm] start_command configured` is not an error and needs no fixing. It only means you start Ollama yourself, which is what the Ollama app does for you.

One more, only if a later scan complains that the backend rejected the request: add `structured_output = "json_object"` inside the `[llm]` block, and `"none"` if that still fails. How strictly a backend can be held to a JSON shape is a property of the backend, not the model.

**`backend status` proves that *something* answers, not that it is yours.** It
asks `/v1/models` and reports what comes back, so anything on that port serving
a model by that name reads as `ready`. Port 11434 is shared by everything on the
machine that uses Ollama, and a tag like `gemma4:26b-a4b-it-qat` names whatever was last
pulled under it — so another project's pull, or an upgrade, changes what judges
your scans without changing a line of your config. An *absent* backend is caught
by the connection error the tool raises; a *substituted* one is caught by
nothing. If a result surprises you, check what is serving:

```bash
ollama ps       # what is loaded right now
ollama list     # what each name points at — the ID column is the digest
```

A project that wants the answer to be one model and no other can build a
name nothing else uses — `ollama create` from a Modelfile — and put that in
`[llm] model`.

A backend is required. Rating a change and adjudicating a credential candidate are half of what this tool does, so a run that cannot reach a model exits 2 and names the fix rather than reporting the mechanical half as though it were the whole answer. See [use-of-local-model.md](use-of-local-model.md).

## 3. Find out what is true now

These checks need no history. Credentials and packages have defaults; licences
require an acceptance policy, described in [Gates](gates.md#licence-policy):

```bash
vibe-sentinel credentials     # keys and passwords sitting in the tree
vibe-sentinel packages        # imports that resolve to nothing, undeclared deps
vibe-sentinel licenses        # dependencies your policy would not accept
```

Each exits `0` with no failing findings, `1` with findings, and `2` if the run
failed. Credentials need the model only for candidates not already settled by
policy or declared verdicts; unanswered candidates fail the run. Packages ask
about similar installed names: unusable individual answers leave findings,
while a connection failure exits 2. Licences resolve mechanically and use the
model only for a requested pin draft.
Findings can include untracked files and development dependencies, so they do
not by themselves describe a release artifact. Remove the cause or record a
scoped pin after review. A pin keeps the finding visible while accepting it for
the exit code; [Gates](gates.md#pins-are-not-ignores) explains the required fields.

## 4. Record the baseline

```bash
vibe-sentinel scan --print-example > .vibe-sentinel.toml
vibe-sentinel scan
```

**If you already made a `.vibe-sentinel.toml` while setting up the backend, skip the first line** — `>` overwrites the file, and your `[llm]` settings would go with it. To see the example without losing anything, run `vibe-sentinel scan --print-example` on its own and read it on screen. Every line it prints starts with a `#`, which means the setting is switched off and shown only as documentation; a setting takes effect once the `#` in front of it is gone.

The config declares which probes run and where they point; the defaults measure the current directory and are usually right to start with. This first scan tells you nothing about drift — that is the point of a baseline — but it runs the three gates above, so it is still useful.

## 5. Wire in the journal

```bash
vibe-sentinel hook --install --mode observe   # wires .claude/settings.json, sets [safety] mode
vibe-sentinel status                          # what is protecting the repository now
vibe-sentinel commands --sessions
```

The hook records calls in `.vibe-sentinel/history.db`, at about 52 ms per call
in the recorded measurements. `--mode` is required, because what the hook does is a choice rather than something inherited:

- `journal` records every tool call and judges none.
- `observe` judges each flagged call and records the verdict, and blocks nothing.
- `enforce` refuses an unsafe call and asks you about one with no verdict.

Start with `observe`. It measures how often the gate would stop work on your own traffic before anything is stopped; `vibe-sentinel hook --install` with no mode prints what enforce would have done with the verdicts recorded so far. `vibe-sentinel status` says where the hook is installed, which mode is in force, whether the judge answers, and how the last session's calls were judged — and says plainly when no hook is installed.

An agent cannot make this choice for you: `vibe-sentinel hook --install` run by an agent is refused under `enforce`, like an edit to the config.

## 6. Check for drift later

```bash
vibe-sentinel scan       # baseline, 1w and 1m horizons, and the fitted trend
vibe-sentinel trend      # movement across many runs
```

A scan that finds drift does not become the new baseline. `scan --update` accepts what it found and moves it — accepting drift is a deliberate act. The probes, the horizons, and the trend fit are in [drift.md](drift.md).

## Routing it somewhere

The scan and standalone state gates report through exit codes; the installed
safety hook can directly ask about or deny agent calls under `enforce`.
Route a scan or state gate into a workflow to make its findings block work:

| Where | How |
| --- | --- |
| Pre-commit | run `vibe-sentinel scan` (or a single gate) in the hook; a non-zero exit stops the commit |
| CI | the same — the runner needs a reachable `[llm] endpoint`, because a scan that could not ask is a scan that failed |
| Back into the coding session | `--format agent`, which renders findings as constraints for the agent that caused them |
| Another tool | `--format json`. The JSON always carries a `gates` key, empty rather than absent when nothing ran, so "no findings" is never confused with "not checked" |
