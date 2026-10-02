# Vibe Sentinel

Vibe Sentinel is a local-first security and governance layer for AI coding
agents. It records agent activity, can enforce declared policies before tool
execution, flags credential and dependency-provenance risks, and tracks
structural changes across the repository's history.

Measurements are deterministic; your configured local model, such as Gemma,
judges questions that require context. Claude Code and Cursor are the coding
agent integrations, not the judge. Their hooks connect proposed actions to the
same review engine; installation requires an explicit choice of journalling,
observation or enforcement. Policy decisions and findings remain auditable.

## Why use it?

An agent's task is to make the requested change. Your responsibility extends
to what it runs, what it brings into the project, and what repeated changes
leave behind. Reviewing the final diff is important, but does not answer all
three questions.

For a developer, Vibe Sentinel adds checks at those different points:

- **Before execution:** a deletion, upload or undeclared install can matter
  before there is a commit to review. The optional safety gate reviews flagged
  actions against declared dangers and recent command history.
- **In the working tree and environment:** a credential can already be present,
  an import can name an undeclared dependency, or an installed package can
  change without a source diff. State gates report current findings; dependency
  measurements record version and origin changes in the audited environment.
- **Across weeks of edits:** each change can look reasonable while a directory
  becomes a coupling hub or helpers accumulate somewhere they do not belong.
  Recorded measurements and project-specific questions make cumulative movement
  available for review, instead of relying on memory.

For a CTO or engineering lead, the value is making expectations explicit and
decisions inspectable. Declare which actions need scrutiny, which licences are
acceptable, and which structural changes matter to your team. Built-ins cannot
know which database is production or which directory must stay thin. Custom
rules and questions let the checks reflect those distinctions.

Exceptions also leave a decision: a scoped pin requires a reason and verification
date, rather than silently hiding a finding. The local history keeps recorded
tool requests, command reviews and repository measurements. It gives a team
evidence to discuss when reviewing agent-assisted work—not a score claiming
that the developers or their code are safe.

## What it does

| Piece | Question | Result |
|---|---|---|
| **Probes** | What changed? | Keyed measurements compared with an accepted baseline, older runs and fitted trends |
| **Lenses** | Does that change matter here? | A severity and explanation, using questions your project declares |
| **State gates** | What is true now? | Credentials, dependency provenance and licence-policy findings, reported on every scan |
| **Command safety** | Should this action proceed? | A review against the agent's recent command history, before execution |

Six built-in probes measure commentary ratios, module organization, discarded
exceptions, structural patterns, file sizes, and installed dependency versions
and origins. A custom probe can be any command that prints the observation
protocol. Lenses supply project context: growth in a directory meant to stay
thin can mean something different from growth in a directory designed to expand.

The measurements and comparison are reproducible. Model ratings can vary.
A lens cannot add or remove a measured change. Credential and package-name
reviews work differently: the model adjudicates candidates the rules found.

![Vibe Sentinel: declared policy guides command safety, state gates and structural drift. Command reviews can allow, ask or deny; scans report findings and changes. Local history records tool requests, verdicts and measurements, while pins and baseline updates make acceptance explicit.](docs/images/vibe-sentinel-overview.svg)

History supplies context for later reviews and comparisons. Pins record scoped
exceptions in policy; accepting a baseline is a separate, deliberate decision.

## What the findings look like

A state needs no history. A credential already present on the first scan is
still reported on later scans:

```text
State
  credentials: 1 failing — 105 file(s) read, 18 candidate(s), 1 failing

  [credentials] src/config.py — A cloud access key id  (tracked)
      | 12: AWS_ACCESS_KEY_ID = "<redacted: 20 chars, entropy 3.7>"
      -> cloud-access-key: the prefix and entropy are consistent with an issued key
```

Credential reviews estimate whether a candidate is real from redacted context;
they do not test whether a key works against its provider. Remove the cause or
record a scoped pin with a reason and verification date. Changing the drift
baseline does not settle a gate finding.

Drift needs a previous measurement:

```text
Drift since 2026-09-01T15:59:11+00:00
  + [high] new: src/helpers: 3 module(s), 36 lines
      A new helpers directory changes where shared code lives.
  ~ [medium] version:requests: 2.32.5 -> 2.28.0
      The installed version changed between scans.
```

These are illustrative excerpts. Every scan is recorded; `scan --update`
deliberately accepts the current measurements as the new baseline. Week and
month comparisons and fitted trends expose slower movement without changing
the baseline or independently failing a scan.

## Before an agent runs a command

The journal records tool calls before execution. The safety gate uses that
history to review actions such as deletion, force-pushing, undeclared installs,
uploads and changes to its own policy.

The hook integrates with **Claude Code** and **Cursor**. Under Cursor it gates
shell commands, MCP tools and file reads, but not file edits; the table in
[Gates](docs/gates.md#which-agents-and-what-each-lets-the-gate-do) says what each
agent lets it see. Scans can be used alongside other agents, but those agents do
not automatically call this hook.

Hook installation requires `--mode journal|observe|enforce`; without a mode,
it explains the choices and writes nothing. `journal` records requests without
safety review, `observe` also records verdicts without intervening, and `enforce`
denies unsafe calls. An unclear or missing verdict asks for permission by
default; Cursor file reads are denied instead because that event cannot ask.
Some declared rules settle a verdict without a model call. An explicit
`no_verdict = "allow"` can permit unjudged calls unless a system policy requires
`ask`. Without a declared project or system mode, safety remains off.

Run `vibe-sentinel status` to see hook locations, the effective safety mode,
whether the judge answers, and the last session's verdicts. No installed hook
is reported as `NOT INSTALLED`; unreadable settings are reported as unreadable,
not mistaken for an absent hook.

For centrally administered machines, `hook --install --managed --mode enforce`
installs managed hooks and sets a system-policy minimum that projects cannot
lower. System danger rules cannot be removed or overridden by project rules.
This protection depends on administrator-owned hook and policy files: without
permission to write them, installation prints the pending files and exits 2.
See [managed installation](docs/gates.md#out-of-the-agents-reach) for paths and
setup. It does not remove the agent's user privileges or prevent elevation;
on WSL, the user-writable Claude Code managed-settings directory does not
provide the same protection.

Managed configuration protects policy from project-level changes; it does not
make the gate a sandbox. The gate does not see every possible spelling of an
action, and a model can be wrong. See the
[threat model](docs/THREAT_MODEL.md) for measured coverage and remaining gaps.

The journal records requests before execution, not proof that an action ran or
succeeded. A local record in the agent's trust domain is not a tamper-proof audit
trail. Keep independent access controls, sandboxing and protected logs where
your risk requires them.

## Evidence and limits

Controlled research demonstrates relevant failure modes; these rates are not
ordinary developer-session telemetry or measurements of Vibe Sentinel:

| Finding | Study context |
|---|---|
| 54.7–84.7% harmful safety-violation rate | [Saber](https://arxiv.org/html/2606.01317v1): 13 coding-capable models, 716 executable tasks in sandboxed project workspaces |
| 74.83% realized harm without a command guard | [CARE](https://arxiv.org/html/2607.21642v2): 600 attack-intent commands in RedCode-gen |
| At least 5.2% vs. 21.7% hallucinated packages | [Spracklen et al.](https://arxiv.org/abs/2406.10279): commercial and open-source models across 576,000 generated samples |

Vibe Sentinel's own [evals](docs/evals.md) measure its rules, prompts and verdicts
against labelled cases. Results are specific to the corpus, model and setup;
they are not guarantees about arbitrary commands.

It is **not a linter, code-quality reviewer or vulnerability scanner**.
Keep your tests, type checker, code review and CVE tools. It records installed
version changes but does not look up vulnerabilities in those versions.

## What adoption requires

| Area | Cost or boundary |
|---|---|
| Runtime | Python 3.13; install beside your project with `uv tool`, or inside the environment you want audited |
| Model | A reachable OpenAI-compatible backend. The documented Gemma 4 26B QAT setup uses about 15 GB for the model; plan for 32 GB memory or more. Smaller models have different measured tradeoffs |
| Latency | Journalling measured about 52 ms per call. Flagged commands may require model review, with an 8-second default safety timeout; cold loading can exceed it |
| Environment scope | `packages`, `licenses` and `dependency-versions` inspect the interpreter running them. A separate tool environment measures its own installed packages |
| Platforms | Linux, macOS and Windows under WSL2; native Windows is untested |
| Privacy | Local inference by default. A remote model endpoint is configurable; credential excerpts require an additional explicit override. Package-index lookups require `--online` |

Use a judge from a different model family if you want a separate perspective
from the coding agent. Record the model you evaluated; a model name alone does
not verify immutable weights.

Vibe Sentinel is a CLI, not a library you need to import into your application.
It writes configuration and local history rather than rewriting your source.
Choose its execution environment deliberately: installation beside a project
does not automatically audit that project's installed dependencies.

**Pre-1.0:** the CLI and configuration format may change between minor versions.
The history database uses versioned migrations.

## Getting started

**Development-version features:** explicit install modes, `status`, Cursor
support and managed policy are implemented in this checkout but not yet
released in the published 0.5.0 package. The commands below require this
development version; use the [development setup](docs/development.md#setup)
when working from this checkout. Released-package installation and backend
setup are covered in [Getting Started](docs/getting-started.md).

```bash
vibe-sentinel backend status
vibe-sentinel scan
# Choose the integration for the agent you use:
vibe-sentinel hook --install --mode observe
# Or, for Cursor:
vibe-sentinel hook --install --agent cursor --mode observe
vibe-sentinel status
```

The first scan records a baseline and runs state gates; licence enforcement
requires a policy. Missing required model answers fail the scan instead of
producing an apparently completed review. To block commits or CI, route the
scan's exit code into that workflow. Only the installed safety hook can
intervene directly in agent actions.

For a team rollout, start with a baseline, state-gate policies and the journal.
Use safety `observe` mode to review verdicts on your workflow before choosing
`enforce`. Use managed installation where projects must not weaken a centrally
declared policy, and verify the effective setup with `status`. Assign
responsibility for findings and exceptions, route scan failures into the review
or CI process, and back up the history. Installing the package alone does not
establish an enforced team policy.

## Documentation

| Topic | Guide |
|---|---|
| Installation, backend and first scan | [Getting started](docs/getting-started.md) |
| Credentials, provenance, licences and command safety | [Gates](docs/gates.md) |
| Coverage and residual risks | [Threat model](docs/THREAT_MODEL.md) · [Control mapping](docs/CONTROL_MAPPING.md) |
| Probes, lenses, horizons and trends | [Drift](docs/drift.md) |
| Model responsibilities and failures | [Use of the local model](docs/use-of-local-model.md) |
| Custom measurements, questions and cases | [Adding a probe](docs/adding-a-probe.md) |
| Measured quality and latency | [Evals](docs/evals.md) · [Benchmarks](benchmarks/README.md) |
| Backups, migration and maintenance | [History database](docs/database.md) |
| Contributing | [Development](docs/development.md) |
| Frontend support and proposed extensions | [Frontend exploration](docs/frontend-support.md) |
| Reporting a vulnerability | [Security](SECURITY.md) |

## License

MIT
