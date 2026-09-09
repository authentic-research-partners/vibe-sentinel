# Use of the Local Model

## What this tool actually is

A small local model — 8B, one GPU, no internet — cannot be handed a repository
and asked whether it is well organised. It has no room for the tree, no memory
of last month, and a marked tendency to answer the easiest part of any question
containing several.

So it is not handed one. Every deterministic mechanism in this codebase exists
to turn a repository into a question small enough for that model to answer well,
and the model is asked nothing else. That is the architecture, in one sentence:
**the local model is the reader, and everything else prepares what it reads.**

| The preparation | Turns | Into | So the model is asked |
|---|---|---|---|
| probes (`probes/`, `runner.py`) | a tree of source | counted values, each keyed | never "read this repository" |
| `compare()`, `horizons.py`, `trends.py` | two runs, or a series | *what moved*, and how fast | never "spot the difference" |
| `watch` on a `[[lens]]` | every declared question | the ones whose probes actually moved | never a question with no subject |
| `[[secret]]`, `[[danger]]`, `near_misses()` | every file, every command | a handful of candidates | never "is anything wrong here" |
| `blind` / `partial` / `shape` | a live credential | a prefix and an entropy figure | never the secret itself |
| `schemas.py`, `json_schema.clip_to_bounds` | an open-ended answer | a bounded, typed one | never "write what you think" |
| one request per matched item | several concerns | one concern, with a shared prefill | never two things at once |

Most of the code in this project is in that left column. The measurements are
worth having on their own — the history is the product — but the reason they are
shaped the way they are is the right-hand column.

This also fixes what the model is *for*. It is not a second opinion on the
numbers, and it is not there to catch what the mechanisms missed. It answers the
one question the mechanism cannot: **does this matter?** A directory gained four
modules — mechanically true, and worth nothing until somebody says whether four
modules in `adapters/` is a refactor or a mess.

## Measurement is mechanical; judgement follows three different rules

Probes are subprocesses, `compare()` is a diff, the provenance audit is
arithmetic, the credential scan is a rule set, and the safety gate's triage is a
regex. No model chooses what is looked at, and none of that changes when the
backend is off.

What the model is then allowed to *do* differs by which question it answers:

| Check | Mechanism finds | Model judges | The answer can |
|---|---|---|---|
| `scan` (drift) | `compare()` builds the change list | how much each change matters, one question per lens | set a severity, which reaches the exit code |
| `credentials` | rules flag candidate secrets | whether a candidate is live, from a prefix and its shape | settle it — `placeholder` means no finding |
| `packages` | the audit finds two names one edit apart | two packages, or one typo | settle it — `distinct` means no finding |
| `licenses` | the SPDX resolver decides | drafts the note for a pin, after the gate has decided | nothing; it never sees the verdict |
| `safety` | triage flags a command | is this command safe, given the agent's own history | in `enforce` mode, refuse the call |

The last row does not fit a tidy sentence about measurement. Its
subject is what an agent is about to do rather than the codebase, and its answer
acts before anyone reads a report. That is why it is opt-in and off by default.

The second invariant is that **every one of those questions is yours to phrase.**
A `[[lens]]`, a `[[danger]]` and a `[[secret]]` each carry a `question`; `[drift]`,
`[licenses]` and `[packages]` take a `guidance` note that goes in front of theirs
and is reported when it applied. That is the same argument as the rule sets: a
shipped list of verbs cannot know which of your database hosts is production, a
shipped list of key formats cannot know which prefix your auth service mints, and
a shipped question about two package names cannot know that `httpx` and `httpx2`
are both real.

The invariant across all five: **the model never picks what to look at and never adds an entry to the list it was given.** What "the list" is differs, and reading
past that is how "it cannot hide a finding" gets misquoted — `compare()` owns the
change list outright, while the credentials and near-miss rules own a list of
*candidates* that settling is the model's whole job.

## The safety gate, because it can act

- **Off by default.** `[safety] mode` is `off`, `observe`, or `enforce`. Only
  `enforce` can refuse, and only on `unsafe` — an `unclear` verdict is recorded
  and the command proceeds.
- **It is the only call that gets history**: the command, its target, the cwd,
  and the agent's own previous `[safety] history` commands. `rm -rf "$DIR"` has
  no answer without the line that set `DIR`.
- **A `[[danger]]` carrying `verdict =` never reaches the model.** Faster, the
  same answer every time, and it still holds with no backend running.
- **Every failure path is permissive.** Unreachable, timed out, unparseable, or a
  bug in the gate: recorded as unreviewed and allowed through. A gate that turns
  a backend outage into a blocked command is worse than the risk it guards.

## Without a model

There is no `--no-model`, and there is no verdict meaning "nobody looked". A run
that cannot reach a model, or whose model does not answer, raises
`LLMConnectionError` and exits 2 with the remediation named.

That is a deliberate reversal. The tool used to degrade: it listed the
candidates, marked them unadjudicated, and carried on. The problem is what that
output looks like on the way past — a credentials report listing seven
candidates nobody judged is visually a report of a tree with seven things in it,
and a reader skimming exit codes cannot tell it from the real thing. A check
people can accidentally run in a state where it decides nothing is a check that
protects nothing.

| Command | With no backend reachable |
|---|---|
| `scan` | exits 2; nothing is compared away, and the run is still recorded |
| `credentials` | exits 2 — the candidates are known, and half the gate is deciding which are real |
| `packages` | the audit stands on its own; a near-miss the model did not settle stays a finding |
| `licenses` | the gate is unaffected; only the drafted pin note is missing, and it says so |
| `safety` | permissive, always — see below |

The safety gate is the exception and stays permissive on every failure path:
unreachable, timed out, unparseable, or a bug in the gate is recorded as
`unreviewed` and the command proceeds. It sits in front of an agent's tool call,
and a gate that turns a backend outage into a blocked session is worse than the
risk it guards.

The test suite runs with no GPU, but not by having a model-free path: an autouse
fixture in `tests/conftest.py` stubs `llm_query` with the dullest answer each
schema allows. A test that cares about a verdict patches over it. This is on
purpose — a code path that only CI exercises is a code path nobody reads.

## Which model

Any OpenAI-compatible `/v1/chat/completions` endpoint works; nothing in the package is backend-specific, and anything a backend needs goes in `[llm] extra_body`.

```toml
[llm]
endpoint = "http://localhost:5001/v1"
model = "qwen3-8b-fp8"
structured_output = "json_schema"   # json_schema | json_object | none
```

Pick a family **different from the one that writes your code** — one from the
same family shares its blind spots about what normal structure looks like. An 8B
model is enough; on an Apple Silicon Mac the default is Qwen3 14B under Ollama,
and any other 12–14B local model does the job. On a backend that serialises,
set `concurrency = 1` under `[safety]`, `[credentials]`, `[packages]` and
`[drift]`.

**One hard boundary:** a credential excerpt goes to the configured endpoint only
if that endpoint is loopback. `[credentials] allow_remote_model = true` is the
deliberate override, and is deliberately not a command-line flag.

## See Also

- [Gates](gates.md) — what each gate checks, how to configure it, and what clears a finding
- [Development](development.md) — where each of these lives in the code
