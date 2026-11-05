# Codex plugin

The plugin in [`plugins/codex/`](../plugins/codex/) connects Codex to the same
Vibe Sentinel engine used by the CLI. It bundles setup and inspection skills,
a local MCP connection and a pre-tool hook. It does not replace the configured
judge with Codex, and it shares the project's configuration and history.

Codex support is a checkout preview. Version 0.5.1 provides CLI status and
Claude Code and Cursor hooks, including explicit modes and managed installation.
For Codex, use the engine and plugin from the development checkout. The bundle
has its own version, `0.1.0`.

1. [Install the runtime](#install-the-runtime) and make both executables available to Codex.
2. [Install the plugin manually](#install-the-plugin-manually), then review and trust its hook.
3. [Configure a project](#configure-a-project) with `sentinel-setup` and choose a safety mode.
4. [Validate the installation](#validate-an-installation) with a harmless request before relying on the hook.

## Install the runtime

Use Python 3.13 for the scanner runtime. From the Vibe Sentinel checkout, run
these commands with that environment active.

```bash
uv pip install -e '.[plugin]'
command -v vibe-sentinel
command -v vibe-sentinel-mcp
vibe-sentinel --version
```

The plugin starts `vibe-sentinel-mcp` over stdio. Its hook invokes
`vibe-sentinel hook --agent codex`. Both executables must be on the **Codex
host's PATH**, which may differ from a terminal's activated environment.
Do not put credentials in the plugin JSON files.

All MCP calls take an absolute project `root` containing `.vibe-sentinel.toml`.
Their `interpreter` field reports the server's scanner runtime. By default it
also supplies installed package, licence and dependency-version measurements;
choosing a root does not activate its virtualenv. Set `[environment] python`
in each project's configuration to inspect a separate trusted application
interpreter. Scan `measured_environment` identifies a successful dependency
reading, or is null if that observation is absent or failed. See
[environment selection](environments.md) for supported targets and limitations.

Configure `[packages] extras` and `groups` to match the dependencies installed
in the measured environment. See
[dependency selection](gates.md#select-the-dependencies-installed-in-this-environment).

## Install the plugin manually

Use the [Codex manual plugin workflow](https://developers.openai.com/plugins/build/plugins).
The included [`marketplace.example.json`](../plugins/codex/marketplace.example.json)
is a template, not an active installation. In this checkout, copy its contents
to `.agents/plugins/marketplace.json`, or merge its entry into an existing
marketplace. Its `./plugins/codex` source path is relative to the checkout
root. If you copy the bundle elsewhere, adjust that entry accordingly.

Restart the ChatGPT desktop app, install the local `vibe-sentinel` plugin, enable it
for the intended project, and review and trust its hook definition. Installation
and trust are separate steps. Updates to hook definitions require renewed
review. Do not install a second standalone hook for the same project.

After updating the bundle, restart the desktop app to load the updated files.
If the MCP response still looks unchanged, confirm the plugin's scanner
interpreter points to the environment where you installed the updated engine.

Manual installation is the supported distribution path for this hook-bearing
bundle. It is not a public-directory submission. See the linked packaging
documentation for the host's current installation and trust controls.

## Configure a project

Run the plugin's `sentinel-setup` skill. It checks the runtime, project config,
model reachability and mode, then explains any outstanding activation checks.
It does not start a backend or silently select enforcement.

For a new project, inspect `vibe-sentinel scan --print-example` before creating
`.vibe-sentinel.toml`; never overwrite an existing configuration with that
output. Configure the judge using [Getting started](getting-started.md) and
check `vibe-sentinel backend status` from the target project. Existing
credential policy still controls whether redacted excerpts may reach a remote
model. Installing this plugin grants no additional network permission.

Choose one mode explicitly through setup or the CLI.

```bash
vibe-sentinel hook /absolute/path/to/project --configure --mode observe
```

Start with `observe` to see how your own requests are reviewed. The mode table
below describes each option. `--configure` changes only the project's
safety mode, leaving hook installation to Codex. A managed minimum still
applies. Configure the intended mode before relying on the hook for protection.
An enforced policy may block an agent's attempt to change its own safety mode,
including the MCP configuration tool. Make an authorized policy change from a
trusted terminal when that rule applies; do not bypass it through another tool.

The bundled hook has a 20-second host timeout. Keep the configured safety
timeout below that outer limit with room for process startup and journalling.
If you change either timeout, review the other one as well. A host terminating
the hook before it returns cannot receive the engine's intended decision.

## Tools and workflow

The plugin's skills guide setup and inspection, its MCP tools handle requested
operations, and its trusted hook runs before supported actions. The configured
judge still answers Sentinel's questions. The separate development plugin
provides role templates for repository work. [Sentinel development plugin](../plugins/development/README.md)

MCP is the local connection protocol that lets Codex call these operations
without composing a shell command. Each tool uses an isolated CLI subprocess
with the requested project root, preserving the CLI's policy and history.

| MCP tool | Purpose | Example request |
| --- | --- | --- |
| `status` | Report effective policy, judge reachability and available hook evidence; cannot confirm desktop trust | "Is Sentinel ready for this project?" |
| `scan` | Run declared probes and state gates, compare measurements, obtain the judge's assessment and record the result | "Scan this project and explain what changed." |
| `security_status` | Read managed security tool and advisory readiness offline | "Are the local security tools ready?" |
| `security_setup` | Explicitly install managed tools or refresh public advisory data | "Set up the local security tools." |
| `security` | Run selected local JS/TS security checks outside drift history | "Check `src` with the bundled source rules and report coverage." |
| `history` | List scans or inspect a recorded run without a fresh scan | "Show the last five scans." |
| `trends` | Fit measurements across recorded runs without a fresh scan | "Which measurements have been increasing?" |
| `commands` | Inspect proposed tool requests in the local journal, not proof of execution | "What commands did the agent propose?" |
| `safety_history` | Inspect recorded command reviews and decisions, optionally filtered by session or verdict | "Why was that command blocked?" |
| `configure_safety` | Set an explicitly chosen project safety mode; does not install or trust hooks | "Enable observe mode for this project." |

### Setup and inspection skills

[`sentinel-setup`](../plugins/codex/skills/sentinel-setup/SKILL.md) guides Codex
through locating the project configuration, checking the runtime and interpreter,
checking judge reachability, setting your chosen mode and testing whether a
harmless request reaches the journal. Use it for installation or troubleshooting.
It does not install a model or start a backend automatically.

[`sentinel-inspect`](../plugins/codex/skills/sentinel-inspect/SKILL.md) guides
everyday use: choose a fresh scan or recorded evidence, explain findings and
distinguish operational failures from completed results. It tells Codex to
respect redaction, preserve the configured judge's verdicts and avoid changing
baselines or policy exceptions without specific authorization.

The inspection skill uses recorded data for questions about previous runs and
performs a fresh scan when requested. A scan runs configured probe commands,
may call the model and writes history. Check unfamiliar probe declarations
before running them. History queries can initialize or health-check the
database.

Use `security_status(root, tools_directory?)` to inspect persistent managed
resources without network access. On an explicit setup request,
`security_setup(root, refresh?, tools_directory?)` installs pinned tools and a
public npm advisory snapshot; `refresh=true` updates the snapshot. It requires
existing `uv` and trusted Node installations. Setup does not change global
PATH, Codex settings or the application's environment.

Use `security(root, lockfile?, advisory_db?, source?, browser_artifacts?,
tools_directory?)` for an explicit security check. Managed tools and advisory
data are discovered automatically; `advisory_db` can select an existing cache
instead. The tool reports what each selected input covered and leaves drift
history and baselines untouched. It does not install or refresh resources.
[Inputs, prerequisites and outcomes](security-checks.md)

The updated server exposes ten tools. An already-running older server can
still expose only eight until Codex reloads or restarts its plugin connection.
Check the available tool list after updating; a completed CLI setup alone does
not update an existing server's tool list.

### Read a scan result

Each scan response includes the project root, scanner interpreter and an exit
code when the subprocess returned one. Check the recorded `data.result.outcome`
alongside that code. When the response has no result envelope, read its
top-level `outcome` and diagnostics.

| Exit | Recorded outcome |
| --- | --- |
| `0` | `complete`, with no findings that affect the exit code |
| `1` | `findings`, with completed work and findings to review |
| `2` | `incomplete` or `error`. Read the summary to identify the coverage gap or failed stage. |

Exit 2 can occur even when all current probes succeed. For example, the
baseline may lack a comparable measurement for a newly enabled probe. The
response reports that comparison gap separately from current state-gate findings.

Scan responses contain drift and gate findings, omitting the full measurement
snapshot and credential source excerpts. Raw scan stderr is also withheld:
probe failures can include source text or rejected observations. Inspect full
diagnostics with the local CLI outside the Codex conversation. The local CLI
retains the full report.
The first successful scan establishes an initial baseline; later scans compare
against it without automatically accepting their changes.

The MCP surface deliberately omits baseline acceptance, policy pin creation,
database deletion, backend lifecycle operations and online package lookups.
The existing CLI remains available for those workflows. A baseline update
cannot clear a state-gate finding; accepting drift and recording a scoped
policy decision remain separate actions.

### Automatic safety hook

The bundled `PreToolUse` hook invokes `vibe-sentinel hook --agent codex` before
supported calls. The adapter translates the proposed action into Sentinel's
shared request format; the engine records or reviews it according to the
effective policy and returns the decision to Codex.

| Mode | Behavior |
| --- | --- |
| `journal` | Record requests without safety review |
| `observe` | Record requests and safety reviews without blocking |
| `enforce` | Review requests and block when the effective policy requires it |

Shell review uses command text and recent history. Patch review uses every
affected path, including move destinations. Other supported tools can match
rules by tool name. Patch bodies and arbitrary MCP arguments are omitted.
Some declared rules settle a verdict directly; other flagged actions go to
the configured judge. These checks concern what an action will do, not whether
the proposed code is well written.

## Coverage and privacy

MCP results become context for the Codex assistant and can reach its hosted
model. Keeping the judge local does not make that conversation local. Scan
results use the [versioned result contract](results.md) and omit credential
excerpts, raw diagnostics and the complete installed-package snapshot.
Dependency-version changes, horizons and trends are also omitted because they
can reveal that inventory. Other reported changes, finding paths, verdicts and
explanations still enter the conversation. Unknown or malformed result versions
are rejected without forwarding raw output. Requested command history includes recorded shell command text.

The Codex adapter handles shell commands, patch target paths and named local
tools, including MCP calls. Arbitrary MCP arguments and patch bodies are not
journalled or sent to the judge by this adapter. This limits argument-specific
review; a tool-name rule is not a review of every value passed to that tool.
Shell command text still follows the existing journal behavior and may contain
sensitive values. Do not put credentials on command lines.

Every patch target is checked, including move destinations. The judge receives
the complete path set rather than the journal's shortened display. A path set
too large for the bounded review is recorded as unreviewed and follows the
configured no-verdict policy. Project and user Codex hook/config files, and the
installed bundle identified by Codex's `PLUGIN_ROOT`, count as control paths.
Changing them or invoking the safety configuration tool is covered by the
existing `changing-the-gate` rule.

Codex currently cannot implement the gate's `ask` response at `PreToolUse`.
The adapter converts it to a denial with an explanation. Hosted tools,
subsequent input through `write_stdin`, and specialized tool paths that opt
out are outside this hook's coverage. These are host boundaries, not evidence
that the unobserved action is safe. See [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and the project's [threat model](THREAT_MODEL.md).

CLI `status` can report configuration and standalone hook files; it cannot
prove that Codex has enabled and trusted a plugin. A local journal is a record
of requests, not proof of execution or a tamper-proof audit trail.
Consequently, `status` can exit 1 when only the Codex plugin is installed:
read its activation caveat and verify the hook in Codex rather than installing
a duplicate hook to make the exit code zero.

## Validate an installation

Complete these checks in the actual Codex host after installation:

1. Run setup in a fresh chat and confirm both bundled skills and the seven MCP
   tools are available. Repeat setup after installation in an existing chat.
2. Call `status` for the intended root. Confirm the reported scanner interpreter,
   effective mode and judge, and investigate any diagnostic instead of treating
   it as a clean result.
3. After trusting the hook, run `pwd` through Codex and find that request in
   `commands`. This checks the observation path without a dangerous command.
4. In a disposable configured project, add a temporary declared danger with a
   fixed unsafe verdict for a harmless marker command. Check that observe mode
   records it and enforce mode blocks it, then remove the temporary rule.
5. Request a scan and inspect its recorded run. Check that an unavailable judge
   produces an error rather than a clean report.

For step 4, add this temporary rule to the disposable project's configuration:

```toml
[[danger]]
id = "plugin-smoke-test"
title = "Harmless enforcement test"
pattern = 'SENTINEL_BLOCK_TEST'
verdict = "unsafe"
```

Ask Codex to execute `echo SENTINEL_BLOCK_TEST`. In observe mode it should run
and record the unsafe verdict. From a trusted terminal, set that disposable
project to enforce mode with
`vibe-sentinel hook /absolute/path/to/disposable-project --configure --mode enforce`,
then ask Codex to repeat the command. It should be blocked before execution.
Check `safety_history` for the decision and remove the temporary rule afterward.
The fixed verdict tests the hook's enforcement path without requiring a model
answer; it does not validate model-based safety review.

The repository's automated checks cover adapter and MCP behavior. They do not
replace these desktop installation, trust and tool-coverage checks. A bundle
that passes local validation has not thereby been tested in a live Codex
installation.
