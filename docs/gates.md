# Gates

A probe reports a **transition** — this directory gained four modules — and a new
baseline accepts it. A gate reports a **state**: this dependency is AGPL, this
key is in the tree, this command is about to run. A state is as true on the
two-hundredth scan as on the first, so a gate reports every run, exits non-zero,
and `scan --update` does not settle it. Only a pin does.

All three run inside `vibe-sentinel scan` and also stand alone, with the same
exit codes — `0` clean, `1` findings, `2` error — and all three take
`--no-model`. Their rules layer the same way: a table with a new `id` adds a
rule, one reusing a built-in `id` overrides it, `disable` drops one, `use` keeps
only what it names, `use_builtins = false` starts from nothing, and `rule_files`
shares a set across repos. A `verdict =` on any rule settles it without asking
the model — no call, no latency, and it still holds with the backend down.

Every key, with commentary: `vibe-sentinel scan --print-example`.

---

## Credentials at rest

```bash
vibe-sentinel credentials              # 0 clean, 1 findings, 2 error
vibe-sentinel credentials --no-model   # candidates only, adjudicated by nobody
vibe-sentinel credentials --home       # the stores in ~ no .gitignore ever covered
vibe-sentinel credentials --print-rules
```

Two questions. **Files whose purpose is holding credentials**, where the name is
the signal: `.env*`, private keys and keystores, cloud credential files, registry
and git tokens, `terraform.tfstate` and `*.tfvars`, database and cluster configs,
shell history. And **credentials hardcoded into files that should hold none**,
where the bytes are: PEM blocks, `AKIA…`/`AIza…`, vendor prefixes (`ghp_`,
`glpat-`, `sk-ant-`, `sk_live_`), JWTs, URLs with an inline password, and a name
meaning secret assigned a literal.

**Why a model decides and a regex only nominates.** `AKIAIOSFODNN7EXAMPLE`
appears in every AWS tutorial ever written. No pattern tells it from the key
beside it that opens an account, and a gate that flags both gets switched off
within a week. So the pattern nominates generously and a local model adjudicates:
`real` (it would open something), `placeholder` (a template, a documented
example, a revoked fixture), or `unclear`. `real` and `unclear` both fail —
`unclear` is a real answer and the one that sends it to a person.

**What it will not do.** This is the one check that reads secrets, so it refuses
to send an excerpt anywhere but loopback; `[credentials] allow_remote_model` is
the deliberate override and is deliberately not a flag. Values reach the local
model as a prefix plus their shape, and reach your terminal, the log and the
history database not at all:

```
API_KEY = "sk-ant-a…<48 chars, entropy 4.9>"
API_KEY = "your-api-key-here"
```

The exception is a value provably not a credential — a port, `true`,
`localhost` — shown whole, because `port = 5432` is what makes the rest readable.
That set is enumerated, never inferred from length: `hunter22` is eight
low-entropy characters and it is also somebody's password.

**`.gitignore` is a separate, optional rule** (`gitignored = allow | warn |
deny`, default `warn`), because it keeps a file out of a commit and does nothing
about the file — an agent with a shell reads `.env` with `cat` either way. We
recommend `deny`, and the keychain: a secret that is not on disk is the only one
an agent cannot read by accident. Findings carry how git actually sees the file,
and `unknown` is never quietly rounded to `ignored`.

---

## Dependency provenance

```bash
vibe-sentinel packages            # 0 clean, 1 findings, 2 error
vibe-sentinel packages --online   # also ask the index: does the name exist, how old
```

A coding model that does not know a library invents one, with a plausible name
and a plausible import. Those names *repeat* across runs and vendors, so they are
harvestable, and registering one turns every future hallucination into a working
install. Rates and incidents: see the citations in `packages.py`.

| Check | Fires when |
|---|---|
| `phantom` | your source imports a name that resolves to nothing |
| `undeclared` | you import it, it is installed, nothing declares it |
| `orphan` | installed, nothing declares it, nothing requires it (roots only) |
| `uninstalled` | a declared dependency is absent from the environment |
| `unconstrained` | declared with no version bound |
| `near-miss` | two installed names one edit apart |
| `unregistered` | *(`--online`)* the index has no project by that name |
| `squatted` | *(`--online`)* it **does** exist, registered days ago |
| `newborn` | *(`--online`)* a declared dependency released very recently |
| `unchecked` | the online half was asked for and did not happen |

`squatted` is the one to read twice: offline, a phantom import is a name your
code cannot resolve; online, the interesting answer is *"yes, that package
exists, and it was created last Tuesday."*

Nine of these are facts. `near-miss` is not — two names an edit apart describes a
typosquat and a rewrite shipped under a new name identically, and the rule had
already grown a hand-written exception for digit suffixes (`httpx`/`httpx2` are
both legitimate). So that pair, and nothing else here, goes to the local model
with what each package says it does and whether anything asked for it: `distinct`
settles it, `typosquat` and `unclear` stand. `--no-model` leaves every near-miss
standing — this gate does not weaken when the model is off.

It compares your environment against **itself**, never against a shipped list of
popular names: a published list of "names close to real ones" is a published
rule, and it enters the next training corpus. Everything is read from the
interpreter running the check, and that environment is named in the output and
recorded with the finding.

---

## Command safety

Probes measure what work left behind; the journal records the work itself — every
command an agent ran, in order, attributed to session and subagent, in an
append-only local log that outlives the agent that wrote it.

```bash
vibe-sentinel hook --install     # wires into .claude/settings.json
vibe-sentinel commands --sessions
vibe-sentinel commands --agent main --tool Bash
vibe-sentinel commands --run 12  # what an agent did during one scan's window
```

Recording costs ~52 ms per hooked tool call. Nothing is judged and nothing is
blocked unless you turn the gate on.

The gate is two stages, because a denylist cannot do this: `rm -rf "$DIR"` is on
no denylist, and whether it is routine or catastrophic depends on what set `DIR`.
A pattern match runs on every command and costs nothing measurable; ordinary work
never gets past it. What it flags reaches the model *with that agent's own recent
history*, scoped to `(session_id, agent_id)`:

```
Command about to run:
    rm -rf $TARGET

What this same agent ran before it, oldest first:
    11:14:02  Bash       export TARGET=build
    11:14:09  Bash       ls $TARGET
```

| `[safety] mode` | Behaviour |
|---|---|
| `off` | triage never runs; the hook only records. **The default** |
| `observe` | flagged commands reviewed, verdict stored, nothing blocked |
| `enforce` | an `unsafe` verdict refuses the call; `unclear` falls through |

**Guarantees.** Never blocks because the model was slow or the backend down — the
command runs and the row is marked `unreviewed`. Never blocks because of a bug in
the gate: any failure lets the command through. Verdicts are stored apart from
the commands, so the journal stays a record rather than a set of opinions.

What counts as dangerous is yours, because a shipped list of verbs cannot know
which of your database hosts is production. `pattern` is a regex over the command
text — keep it generous, a false match costs one model call — and `question` is
literally the prompt:

```toml
[[danger]]
id = "our-production-database"
title = "Anything pointed at production"
pattern = 'db-prod-1|PROD_DATABASE_URL'
question = """
Does this touch db-prod-1? Staging hosts are db-stg-* and are disposable —
restoring one takes five minutes. db-prod-1 holds live customer data.
"""
```

Try one against your real history without blocking anything:

```bash
vibe-sentinel safety --check 'rm -rf $TARGET' --show-prompt
vibe-sentinel safety --verdict unsafe      # what the gate has seen
vibe-sentinel safety --print-dangers
```

---

## Pins are not ignores

A finding you have decided about is recorded as a decision, scoped to the rules
it names: accepting `orphan` for a package does not accept `squatted` for it next
month. `reason` and `verified` are required — without them it is an `--ignore`
with extra steps, and the whole point is that somebody looked and said why.

## Recorded, not only reported

Every run of every gate appends to `gate_runs` and `gate_findings`, keyed
`<kind>:<subject>`, whether you ran it or `scan` did. That is what answers *when
this started* — the question a diff can never answer, because a key already
committed before your first scan never *appears*, it is simply always there. No
credential value is ever recorded: path, rule, count, exposure and the redacted
excerpt, never the value.

## See Also

- [Use of the local model](use-of-local-model.md) — what the model judges here, and what it cannot
- [History database](database.md) — where gate findings live, and how to trim them
