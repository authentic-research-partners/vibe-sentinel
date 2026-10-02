# Gates

The three state gates — credentials, packages and licences — check what is
true now. They run inside `vibe-sentinel scan` and as standalone commands.
Removing the cause or recording an applicable pin settles a finding;
`scan --update` does not. Exit codes are `0` for no failing findings,
`1` for findings and `2` for an error. An unconfigured licence gate is
reported as unconfigured during a scan, not as clean.

The safety gate is separate: it reviews proposed agent actions through the
hook, not through `scan`.

The `[[secret]]` and `[[danger]]` rule sets support the same selection
settings as probes and lenses: `use`, `disable`, `use_builtins` and
`rule_files`. A new id adds a rule; reusing an id replaces it.
`safety --print-dangers` and `credentials --print-rules` show the source
of declared rules. A declared `verdict` settles a matching rule without a
model call. Licence and package policies have their own settings below.

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
  QC["does this look like a real credential?"]
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
vibe-sentinel credentials --home       # the stores in ~ no .gitignore ever covered
vibe-sentinel credentials --print-rules
```

Three questions arise. **Files whose purpose is holding credentials**, where the name is
the signal: `.env*`, private keys and keystores, cloud credential files, registry
and git tokens, `terraform.tfstate` and `*.tfvars`, database and cluster configs,
shell history. **Credentials hardcoded into files that should hold none**,
where the content reveals: PEM blocks, `AKIA…`/`AIza…`, vendor prefixes (`ghp_`,
`glpat-`, `sk-ant-`, `sk_live_`), JWTs, URLs with an inline password, and a name
meaning secret assigned a literal.

And **a value that looks like a credential whatever it is called**, because the two
above both need a name or a vendor to recognise, and `DB = "p4ssw0rd-prod"` offers
neither. `minted-token` reads a value that looks generated — 32 characters or more,
mixed case, 4.3 bits per character or denser. `password-shape` reads one that looks
typed: a short single token with digits or symbols glued into the letters, which is
how a password is written and not how code is, since code joins words with a
separator. That second signal is weak on purpose. `qwen3-8b` is the same shape as
`hunter22` and nothing structural separates them, so the rule nominates both and the
model settles it — which is the whole reason a nomination is cheap and a pin is a
decision.

**Why a model decides rather than a regex.** `AKIAIOSFODNN7EXAMPLE`
appears in every AWS tutorial ever written. A pattern alone does not establish whether either key is usable, and a gate that flags both gets switched off
within a week. So the pattern nominates generously and a local model adjudicates:
`real` (it would open something), `placeholder` (a template, a documented
example, a revoked fixture), or `unclear`. `real` and `unclear` both fail —
`unclear` is a real answer and the one that sends it to a person. The model
infers from redacted context; it never authenticates against the provider, so
`real` is not proof that a key is currently valid.

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
settles it, `typosquat` and `unclear` stand. A pair the model does not settle
also stands when its individual answer is unusable: the audit already raised
it. A connection failure is different and fails the command with exit 2 when
model review is needed.

`[packages] guidance` is your standing note about your own names, put in front
of that question — the same shape as `[licenses] guidance` and `[drift]
guidance`, and it rides in the shared context so a prefix cache prefills it once
however many pairs there are. `httpx` and `httpx2` are two real packages and
nothing outside your ecosystem knows it; a pin records that after the finding,
and this says it before the question is asked.

It compares your environment against **itself**, never against a shipped list of
popular names: a published list of "names close to real ones" is a published
rule, and it enters the next training corpus. Everything is read from the
interpreter running the check, and that environment is named in the output and
recorded with the finding.

---

## Licence policy

```bash
vibe-sentinel licenses
vibe-sentinel licenses --list-categories
vibe-sentinel licenses --explain <package>
```

Declare what the project accepts before running the standalone gate:

```toml
[licenses]
allowed_categories = ["permissive", "public-domain"]
```

The gate reads installed distributions from the interpreter running it and
licence headers in the source tree. It resolves metadata and shipped licence
text to SPDX expressions, then evaluates the declared policy. Unidentified
licences fail. This is policy evaluation, not a legal determination.

A shared `security/license-policy.toml` is loaded before the project's
`[licenses]` table; `--policy PATH` selects an explicit policy instead.
With no policy, standalone `licenses` exits 2; `scan` reports this gate
as unconfigured.

`--explain` shows the evidence for a package and can draft a pin when needed.
The model does not choose the gate's verdict. Check the licence evidence before
recording an acceptance; a pin needs `packages`, `accept`, `reason`
and `verified`.

---

## Command safety

Probes measure what work left behind; the journal records proposed tool calls,
in order, attributed to session and subagent. It runs before execution, so a
row is not proof that a command ran or completed successfully.

```bash
vibe-sentinel hook --install --mode journal   # wires into .claude/settings.json
vibe-sentinel status
vibe-sentinel commands --sessions
vibe-sentinel commands --agent main --tool Bash
vibe-sentinel commands --run 12  # what an agent did during one scan's window
```

Recording costs ~52 ms per hooked tool call. Nothing is judged and nothing is
blocked unless you turn the gate on.

### Which agents, and what each lets the gate do

The gate is the same for every agent; what differs is which calls the agent
sends a hook before, and what that hook may answer. One row per agent with an
adapter (`vibe-sentinel hook --agent`):

| Agent | Install | Can block | Can ask | Cannot see |
|---|---|---|---|---|
| Claude Code | `hook --install --mode …` writes `.claude/settings.json` (`PreToolUse`, matcher `*`) | every tool call: shell, file reads and writes, MCP tools | every tool call | nothing it calls a tool |
| Cursor | `hook --install --agent cursor --mode …` writes `.cursor/hooks.json` (`beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`) | shell commands, MCP tools, file reads | shell commands and MCP tools; a read with no verdict is refused instead, because `beforeReadFile` cannot ask | file writes and edits: Cursor's `afterFileEdit` runs after the edit, so it could only record one, and `preToolUse`'s `Write` input is undocumented |

So under Cursor the home-directory and secret-store rules reach reads as they
do under Claude Code, and `changing-the-gate` holds for a shell command that
rewrites the gate's files — but not for Cursor's own edit tool. The adversarial
suite measures this per adapter: through Cursor, the three attacks made with a
file-writing tool are missed (`attack-reaches-the-gate-cursor` in
[evals.md](evals.md)). A file's content, which `beforeReadFile` sends whole,
never reaches the journal.

### Out of the agent's reach

Everything above lives in the working tree, and an agent that can write files
there can try to rewrite what judges it. `changing-the-gate` refuses that under
`enforce`, but a refusal from inside the agent's own trust domain is a cost,
not a wall. Two things can be moved where a project's files cannot reach:

```bash
# `sudo` with the full path: a `uv tool` install is not on root's PATH.
sudo "$(command -v vibe-sentinel)" hook --install --managed --mode enforce
sudo "$(command -v vibe-sentinel)" hook --install --managed --agent cursor --mode enforce
```

- **The hook**, in Claude Code's managed settings — this tool's own file,
  `managed-settings.d/vibe-sentinel.json`, so an organisation's shared policy is
  never edited — or in Cursor's enterprise `hooks.json`. Claude Code runs
  managed hooks alongside a project's, and a project's `disableAllHooks` cannot
  turn them off.
- **The mode and a danger set**, in the system policy:
  `/Library/Application Support/vibe-sentinel/policy.toml` on macOS,
  `/etc/vibe-sentinel/policy.toml` on Linux,
  `C:\ProgramData\vibe-sentinel\policy.toml` on Windows. Its `[safety] mode` is
  a floor a project may raise and not lower, a `no_verdict = "ask"` there holds,
  and its `[[danger]]` tables are applied after the project's — a project may
  add dangers, and may not override, `use`, `disable` or `use_builtins = false`
  away the ones declared there:

  ```toml
  [safety]
  mode = "enforce"

  [[danger]]
  id = "production-database"
  title = "Touching the production database"
  question = "Does this reach the production database, and is that the work?"
  pattern = 'prod-db\.internal'
  verdict = "unsafe"
  ```

Without an administrator's rights, `--managed` prints each file and where it
goes, and exits 2. The files must be readable by everyone and writable only by
an administrator, or they move nothing out of reach. `vibe-sentinel status`
shows the level each hook came from, the floor, and a hook installed at two
levels — which runs twice per call. What it buys is stated in the
[threat model](THREAT_MODEL.md): the agent cannot switch the gate off by
editing files it owns, and every attempt is recorded. It still runs as the
user; `sudo` is asked about, not prevented.

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
| `enforce` | `unsafe` denies; `unclear` or `unreviewed` asks by default (`no_verdict = "ask"`) |

These are the two stages and where each verdict lands:

```mermaid
flowchart TB
  TC(["an agent tool call, with safety mode enforce"])
  J[("journalled — every call, ~52 ms")]
  TRI{"does a [[danger]] match?"}
  REV["one model call per matched danger,<br/>with that agent's own recent commands"]
  ALLOW(["falls through to the permission<br/>prompt you already have"])
  DENY(["denied — the command never runs"])
  ASK(["ask for permission — default for no verdict"])
  TC --> J
  TC --> TRI
  TRI -->|no| ALLOW
  TRI -->|yes| REV
  REV -->|"safe"| ALLOW
  REV -->|"unsafe"| DENY
  REV -->|"unclear or unreviewed"| ASK
  TRI -->|"declared unsafe verdict"| DENY
```

**Missing verdicts:** Under `enforce`, a slow or unavailable model or a review
failure requests permission by default. A flagged command with no verdict — `unclear`, or no answer
within `[safety] timeout` — is *asked*: Claude Code shows its own permission
prompt with the gate's reason on it, and the row records `decision = ask`.
Where nobody can answer, Claude Code denies the request instead, so the reason
names the fix: the timeout it missed, and `vibe-sentinel backend status`.
`[safety] no_verdict = "allow"` lets such a command run, for a setup nobody
watches that prefers that trade. `observe` records and never acts. Verdicts are
stored apart from the commands, so the journal stays a record rather than a set
of opinions, and `vibe-sentinel safety` counts how many commands were blocked
and how many were asked.

**Unreadable policy:** An invalid safety configuration, unloadable danger set,
or unreadable manifest needed for triage is a different failure: enforcement
refuses the call and names the file to fix. A valid `off` mode skips triage;
`observe` records policy failures without refusing calls.

One limit no setting closes: if the hook itself runs past the timeout Claude
Code gives it, Claude Code cancels it and the call proceeds. That is why
`[safety] timeout` stays well under the hook's own.

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

Five dangers carry no `pattern` at all. They set `applies_to` instead,
because what they ask needs more than a regex over a command's text:
`outside-project`, whether a write lands outside the project directory;
`control-file`, whether a call changes the gate itself;
`home-outside-project`, whether it reaches into the user's home directory;
`egress`, whether it sends local data to a host the project does not expect;
and `undeclared-install`, whether a command installs a package name that no
manifest declares.

`egress` is `sending-data-off-the-machine`, and only sending counts: `curl`
or `wget` with data or an upload, a raw socket (`nc`, `socat`), a copy whose
destination is another machine (`scp`, `rsync`, `sftp`), `ssh` with data
piped into it, and a cloud-storage upload. `curl https://…` fetches and
passes; loopback is not off the machine; `git push` to the project's own
remote is the work. Hosts the project sends to on purpose go in
`[safety] expected_hosts`, and a subdomain of one counts as it. A destination
nobody can read off the command — `curl -d "$DATA" "$URL"` — is asked about,
because a host no one can name is not one the project listed. The question
is what leaves and where it goes; a secret sent to an expected host is not
caught.

The home directory is watched in two tiers. A file that holds keys —
`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`, `~/.docker/config.json`,
`~/.kube/config`, `~/.npmrc`, `~/.pypirc`, `~/.config/gh`, the macOS keychains
— is matched by pattern, as `secret-store` for a command and
`secret-store-target` for a tool's path, and settled `unsafe`: `git`, `aws`,
`ssh` and `kubectl` read their own without the agent ever naming the path, so
an agent that names one is steered or mistaken. That includes
`ssh -i ~/.ssh/key`, which a project that needs it overrides. Anything else in
the home directory and outside the project — another repository, the git
config, a shell start-up file — is `reaching-into-home`, and asked about,
because reading it is sometimes the work. The rest of the filesystem is not
watched by this check; that does not establish whether it contains sensitive
data. A command that
names a path without opening it — `echo "$HOME/.config"`, `cd ~` — does not
count, and nor does the program being run, so `~/.local/bin/uv` is not a read.

A pattern can also be matched against a tool's own name, with
`applies_to = "tool"`. That is how an MCP call — which reaches the gate as a
name like `mcp__github__delete_repository`, with no command and usually no
path — is matched at all. One ships: `deleting-through-a-connected-tool`,
settled `unsafe`, for a connected tool whose name says it deletes, removes,
destroys, drops, purges, erases or wipes. It is settled rather than asked
because the model would see the tool's name and not which repository or page,
which it can judge no better than the pattern can. A project declares its
own:

```toml
[[danger]]
id = "production-database-tools"
title = "Anything through the production database server"
pattern = '^mcp__prod_db__'
applies_to = "tool"
verdict = "unsafe"
```

`control-file` is what `changing-the-gate` uses, and it is one of the shipped
dangers that settle instead of asking: `verdict = "unsafe"`. A control the
agent can switch off is not a control, and asking a model would hand the
decision to the text the agent is writing into the file. It covers
`.vibe-sentinel.toml`, everything under `.vibe-sentinel/` — the history
database included — `.claude/settings.json` and `settings.local.json`, and
every file a `rule_files` glob in the project's config names. A writing tool
aimed at one of them counts. From a shell, a redirect into one counts, a copy
counts only if one is its destination, a delete or a move counts if it names
one or a directory holding one (`rm -rf .`), and a command that only reads —
`cat`, `ls`, `diff`, `git diff`, `git add` — does not. `ruff format .` names
the directory that holds the settings and writes none of them, so it does not
count either. A project that wants its agent to edit these files redeclares
`changing-the-gate` without the verdict, or disables it.

`undeclared-install` is the only thing separating

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

For JavaScript it reads `npm install`, `npm i` and `npm add`, `pnpm add` and
`pnpm install`, `yarn add`, and `bun add` and `bun install` — with names —
against `package.json`'s dependency tables and its own name. There is no npm
counterpart to `uv add` to leave out, because every one of these records the
name it adds; what is asked about is the name, before its install scripts
run. The same command without names re-syncs and is not read, and neither is
a global tool install (`-g`, `yarn global add`), for the reason `pipx
install` is not. A local path, a tarball, a git or GitHub reference and an
alias (`foo@npm:bar`) are not registry names. The manifest read is the
project root's: a monorepo sub-package's own `package.json` is not.

A `pyproject.toml` that will not parse is not that case, and is not treated
as it. Read as "no manifest", it would declare nothing, so no install could be
undeclared — a typo in the file would switch the check off. So an install by
name in a tree whose manifest will not parse is handled the way an unloadable
`[[danger]]` set is: `observe` records it as unreviewed with the parse error,
`enforce` refuses it and names `vibe-sentinel packages` as the check, and
`off` does nothing. A command that installs nothing never reads the file.
`scan` checks it up front, and the `packages` gate fails on it.

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

## Additional hooks

`vibe-sentinel hook --install --mode <journal|observe|enforce>` installs the
journal and safety gate for Claude Code, or for Cursor with `--agent cursor`,
and sets the mode it runs in. Other hooks must be configured separately against your agent's event
contract.

- A session-start hook can run `vibe-sentinel hook --replay` to drain events
  spilled while the database was unavailable.
- A session-start scan can report standing findings before work begins. It may
  need model calls; its cost depends on the tree and policy.
- After a dependency install, run both `vibe-sentinel packages` and
  `vibe-sentinel licenses` in the audited environment. Guard an automatic
  hook so it runs only after installs.

Keep findings distinct from execution errors when wiring exit codes. Do not
hide both with an unconditional `|| true`.

The built-in `changing-the-gate` danger already protects the policy, declared
rule files and hook settings under `enforce`; adding a second rule for those
same paths is unnecessary. See [Command safety](#command-safety) for its scope.

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
