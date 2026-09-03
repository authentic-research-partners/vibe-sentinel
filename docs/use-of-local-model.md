# Use of the Local Model

## Measurement is mechanical; judgement is not one rule but three

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

The last row is the one that does not fit a tidy sentence about measurement. Its
subject is what an agent is about to do rather than the codebase, and its answer
acts before anyone reads a report. That is why it is opt-in and off by default.

The invariant across all five: **the model never picks what to look at, and never
adds an entry to the list it was handed.** What "the list" is differs, and reading
past that is how "it cannot hide a finding" gets misquoted — `compare()` owns the
change list outright, while the credentials and near-miss rules own a list of
*candidates* that settling is the model's whole job.

## The safety gate, because it can act

- **Off by default.** `[safety] mode` is `off`, `observe` or `enforce`. Only
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

Every command that uses one takes `--no-model`, and every report says so.
Measuring never needed one, so what you lose is commentary, not detection.

| Command | With `--no-model` |
|---|---|
| `scan` | mechanical severities, no lens asked, `analyzed` false and the report says so |
| `credentials` | candidates listed, marked adjudicated by nobody |
| `packages` | every near-miss stands — this gate does not weaken when the model is off |
| `safety` | nothing reviewed, nothing blocked |
| `licenses` | unaffected; only the drafted pin note is missing |

This is the CI path.

## Which model

Any OpenAI-compatible `/v1/chat/completions` endpoint; nothing in the package is
backend-specific, and anything one backend needs goes in `[llm] extra_body`.

```toml
[llm]
endpoint = "http://localhost:5001/v1"
model = "qwen3-8b-fp8"
structured_output = "json_schema"   # json_schema | json_object | none
```

Pick a family **different from whatever agent writes your code** — one from the
same family shares its blind spots about what normal structure looks like. An 8B
model is enough. On a backend that serialises, set `concurrency = 1` under
`[safety]`, `[credentials]`, `[packages]` and `[drift]`.

**One hard boundary:** a credential excerpt goes to the configured endpoint only
if that endpoint is loopback. `[credentials] allow_remote_model = true` is the
deliberate override, and is deliberately not a command-line flag.

## See Also

- [Gates](gates.md) — what each gate checks, how to configure it, and what clears a finding
- [Development](development.md) — where each of these lives in the code
