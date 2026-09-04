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

All three converge on the same finding, and only two of them ask the model
anything:

```mermaid
flowchart TB
  LIC["<b>licenses</b><br/>every installed distribution resolved to SPDX<br/>through an ordered chain, then matched to your policy"]
  PKG["<b>packages</b><br/>the declared graph<br/>vs the installed set"]
  CRD["<b>credentials</b><br/>the rule set,<br/>over the tree"]
  LQ["no model call —<br/>the expression is a fact"]
  QP["typo, or two packages?"]
  QC["is this key live?"]
  F["a finding, keyed — first_seen is what answers when this started"]
  R["restated on every run. A state never diffs"]
  PIN["a pin settles it: accept · reason · verified. A new baseline never does"]
  LIC -->|"what it cannot identify fails as UNIDENTIFIED,<br/>never silently passed"| LQ
  LQ --> F
  PKG -.->|"two installed names<br/>one edit apart"| QP
  CRD -.->|"a prefix and its shape, never<br/>the value, and only to loopback"| QC
  QP --> F
  QC --> F
  F --> R --> PIN
```

---

## Credentials at rest

```bash
vibe-sentinel credentials              # 0 clean, 1 findings, 2 error
vibe-sentinel credentials --no-model   # candidates only, adjudicated by nobody
vibe-sentinel credentials --home       # the stores in ~ no .gitignore ever covered
vibe-sentinel credentials --print-rules
```

Two questions arise. **Files whose purpose is holding credentials**, where the name is
the signal: `.env*`, private keys and keystores, cloud credential files, registry
and git tokens, `terraform.tfstate` and `*.tfvars`, database and cluster configs,
shell history. And **credentials hardcoded into files that should hold none**,
where the content reveals: PEM blocks, `AKIA…`/`AIza…`, vendor prefixes (`ghp_`,
`glpat-`, `sk-ant-`, `sk_live_`), JWTs, URLs with an inline password, and a name
meaning secret assigned a literal.

**Why a model decides rather than a regex.** `AKIAIOSFODNN7EXAMPLE`
appears in every AWS tutorial ever written. No pattern tells it from the key
beside it that opens an account, and a gate that flags both gets switched off
within a week. So the pattern nominates generously and a local model adjudicates:
`real` (it would open something), `placeholder` (a template, a documented
example, a revoked fixture), or `unclear`. `real` and `unclear` both fail —
`unclear` is a real answer and the one that sends it to a person.

**What it will not do:** This is the one check that reads secrets, so it refuses
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

**The `.gitignore` file is a separate, optional rule** (`gitignored = allow | warn |
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

Nine of these checks are factual. The `near-miss` check is not — two names an edit apart describes a
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

These are the two stages and where each verdict lands:

```mermaid
flowchart TB
  TC(["an agent tool call"])
  J[("journalled — every call, ~52 ms")]
  TRI{"does a [[danger]] match?"}
  REV["one model call per matched danger,<br/>with that agent's own recent commands"]
  ALLOW(["falls through to the permission<br/>prompt you already have"])
  DENY(["denied — the command never runs"])
  TC --> J
  TC --> TRI
  TRI -->|no| ALLOW
  TRI -->|yes| REV
  REV -->|"safe · unclear"| ALLOW
  REV -->|"unsafe, and only in enforce mode"| DENY
```

**Guarantees:** Never blocks because the model was slow or the backend down — the
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

### When the question is about the tree, not the text

Two dangers carry no `pattern` at all. They set `applies_to` instead, because
what they ask cannot be asked of a command's text: `outside-project`, whether a
write lands outside the project directory, and `undeclared-install`, whether a
command installs a package name that no manifest declares. That second one is
the only thing separating

```bash
uv pip install -e ".[dev]"     # re-syncing what pyproject.toml already says
uv pip install requests        # a name that came from nowhere
```

Those are the same shape of command. The difference is in the manifest, so the
check reads one — `project.dependencies`, the optional-dependency extras, PEP
735 dependency groups, Poetry's tables, and the distribution this tree itself
builds.

It reads `pip install`, `pip3 install`, `python -m pip install` and `uv pip
install`. Not `uv add`, `poetry add` or `pdm add`: those write the dependency
down, which is the remediation rather than the fault. Not `pipx install`, a
tool installed on purpose outside the project's dependencies, and not `conda
install`, whose names are not the ones `pyproject.toml` carries. Nothing that
is not a bare package name is treated as one — `-r requirements.txt`, `-e .`,
a wheel, a URL, `$PKG`. A tree with no `pyproject.toml` reports nothing at
all, because nowhere to read a declaration is not a declaration of nothing.

Every one of those is the same rule, and it points one way: when in doubt, say
nothing. A missed install leaves things as they were before the check existed.
A gate that blocks `uv pip install -e ".[dev]"` is uninstalled by Thursday, and
then it guards nothing. So the model is told which names are undeclared — that
part is mechanical — and asked only whether those names are ones the work
actually wanted.

It ships asking a question. One line makes it a decision instead, with no model
call and no latency, holding with the backend off:

```toml
[[danger]]
id = "installing-undeclared-dependency"   # the built-in id: this overrides it
title = "Installing a package that nothing in the project declares"
applies_to = "undeclared-install"
verdict = "unsafe"
```

### When a danger applies to some trees and not others

A danger may name a file that must sit at the project root for it to apply:

```toml
[[danger]]
id = "cargo-publish"
pattern = 'cargo\s+publish'
question = "Is this releasing a crate, and did anyone ask for a release?"
requires = "Cargo.toml"
```

Where that file is absent the danger is not in the set at all — not merely
unmatched. `safety --print-dangers` does not list it, and nothing it could
have matched is checked.

The distinction is for whoever reads the list. A denylist is audited by
people, and its value is that every line in it earns a place; eight entries
that cannot fire in this repository are worse than eight that are absent,
because matching nothing and being absent look identical from the outside and
are not the same fact.

That is how the JavaScript dangers ship. All of them carry
`requires = "package.json"`:

| Danger | What it asks about |
|---|---|
| `publishing-a-package` | `npm`/`yarn`/`pnpm`/`bun publish` — public immediately, and the version number is spent either way |
| `deploying-to-production` | `vercel --prod`, `netlify deploy --prod`, `firebase deploy`, `wrangler deploy`, `gh-pages` — replacing what the public is being served |
| `forcing-a-dependency-resolution` | `--force`, `--legacy-peer-deps` — proceeding past an incompatibility rather than resolving it, into a tree nothing declared and nothing tested |
| `exposing-a-dev-server` | a bare `--host`, or one bound to `0.0.0.0` — serving source maps and unminified source to the network |
| `lockfile`, `dependency-tree` | context only. `deleting-files` already escalates an `rm`; these ride along to say *what* is being deleted |

The last row is the rule those two follow rather than an exception to it.
`rm -rf node_modules` is routine, `deleting-files` already asks about it, and
a second danger on the same command would mean the model is asked twice — the
shape of gate somebody switches off. A signal attaches the fact without
raising an event, the same way `home-directory` and `unexpanded-variable` do.

`requires` is resolved last, after `use` and `disable`. So naming an npm
danger in either is never an error in a repository that has no
`package.json` — the id exists, it simply does not apply — and one shared
`rule_files` set works across repositories in different languages.

There is deliberately no danger on `npx`. The question worth asking is whether
the package it runs is one any manifest declares, which is
`undeclared-install`'s question and needs the manifest read; a pattern alone
would fire on `npx tsc` and `npx prettier`, which is ordinary work.

---

## Hooks worth wiring

`vibe-sentinel hook --install` writes one `PreToolUse` entry — the journal, and
the gate that reads it. The rest of `.claude/settings.json` is yours, and none
of the four below needs anything this package does not already ship. The hook
contract belongs to another program, so check the event names and what each one
does with a hook's output against your agent's own reference before pasting.

**`SessionStart` → `vibe-sentinel hook --replay`.** Drains
`.vibe-sentinel/hook-spill.jsonl`, where events go when the database was locked
or a migration was pending. Nobody remembers to run it by hand, and a gap in
the journal reads as *the agent ran nothing*.

**`SessionStart` → the gates.** A gate reports a state, every run, for as long
as it is true — and an agent that never reads one starts every session not
knowing this tree has an AGPL dependency in it and a key on disk. `vibe-sentinel
licenses; vibe-sentinel packages; vibe-sentinel credentials` costs about 1.3 s
on this repo and no model call. All three exit `1` when they find something,
which a hook runner may report as a failure of the hook; end the line with
`|| true` if yours does.

**`PostToolUse` on `Bash` → `vibe-sentinel packages`.** The other half of the
install question, and the half that cannot be asked earlier. A licence is not
resolvable before the package is on disk: `licenses.py` reads the
distribution's own `LICENSE` text, and an index offers only a declared
classifier, which is the evidence it trusts least. So `undeclared-install` asks
the one thing answerable in front of the command — *does anything declare this
name* — and everything else is answerable a moment after it. Guard the hook on
the command having actually been an install; one that costs 640 ms on every
shell command is one you switch off.

**Guard the guard.** An agent that can edit files can edit the file that turns
the gate off, and the refusal it just read names that file:

```toml
[[danger]]
id = "editing-the-guard"
title = "Editing vibe-sentinel's own configuration or the hook wiring"
applies_to = "target"
pattern = '\.vibe-sentinel\.toml$|\.claude/settings\.json$'
question = """
Does this change what the safety gate checks, what the licence or package
policy allows, or which pins stand? Each of those is a decision somebody
recorded. Changing one on purpose is ordinary; changing one to get past a
refusal that just named it is not.
"""
```

---

## Pins are not ignores

A finding you have decided about gets recorded as a decision, scoped to the rules
it names: accepting `orphan` for a package does not accept `squatted` for it next
month. `reason` and `verified` are required — without them it is an `--ignore`
with extra steps, and the whole point is that somebody looked and said why.

A pin does not remove the finding. The gate still finds it, the run still
records it, and the report still prints it — after the failures, in a band of
its own. What changes is that it stops counting towards the exit code. That is
the difference from an ignore, which makes a finding invisible: a pin leaves it
visible and accounted for, so the next person sees the finding and the decision
somebody made about it side by side.

This is why the record keeps it rather than dropping the row. A pin is a
decision somebody made on a date, and the run where a finding stopped failing
because a pin arrived is worth as much as the run where it started — so
`gate_findings` carries `pinned` beside `failing`. Editing a pin changes nothing
that already happened; earlier runs keep the answer they had.

## Recorded, not only reported

Every run of every gate appends to `gate_runs` and `gate_findings`, keyed
`<kind>:<subject>`, whether you ran it or `scan` did. That is what answers *when
this started* — the question a diff can never answer, because a key already
committed before your first scan never *appears*, it is simply always there. No
credential value is ever recorded: path, rule, count, exposure and the redacted
excerpt, never the value.

## See Also

- [Drift](drift.md) — the other half: what moved since the baseline
- [Use of the local model](use-of-local-model.md) — what the model judges here, and what it cannot
- [History database](database.md) — where gate findings live, and how to trim them
