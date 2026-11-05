---
name: sentinel-setup
description: Set up or troubleshoot the Vibe Sentinel Codex plugin for a local project, including runtime, judge, safety mode, hook trust and explicitly requested managed security tools.
---

# Set up Vibe Sentinel

The plugin uses the installed Vibe Sentinel engine and its configured judge.
Installing the plugin does not install Python, the engine or a model backend.
Explain that returned tool results enter Codex context, potentially a hosted
model. Scan outputs omit credential source excerpts and the full dependency
snapshot, but findings and explicitly requested command history are shared.

1. Identify the intended project root and its `.vibe-sentinel.toml`. MCP tools
   require an absolute root and an existing config. Preserve existing settings.
   For a new configuration, inspect `vibe-sentinel scan --print-example` and
   create the config only as part of authorized project setup; never redirect
   that output over an existing file. Review declared probe commands before
   running a scan, since probes execute project configuration.
2. Check that the Codex host can resolve both `vibe-sentinel` and
   `vibe-sentinel-mcp` on PATH. This development version requires Python 3.13 and
   the `plugin` extra, installed from the checkout with
   `uv pip install -e '.[plugin]'` in the intended environment. Do not assume a
   published version includes it. Do not install dependencies merely because
   the plugin was selected.
3. Call the Vibe Sentinel `status` tool with the absolute project `root`.
   Read `interpreter`, `outcome`, `exit_code` and diagnostics. `interpreter`
   identifies the scanner runtime. Dependency and licence measurements use it
   by default; `[environment] python` can select a separate trusted application
   interpreter. Setting `root` alone does not switch environments. A desktop
   host may not inherit a terminal's activated virtualenv. Inspect the existing
   selection, and change it only within authorized setup. On a requested scan,
   `measured_environment` identifies the successfully measured dependency
   target; null means that observation is absent or failed. Do not infer a
   successful measurement from the configured path or run an unrequested scan
   merely to populate that field.
4. Check the configured judge's reachability using `status`. If needed, run
   `vibe-sentinel backend status` from the project. Explain missing model or
   endpoint configuration; do not start, stop or restart a backend unless asked.
   Keep an independent configured judge. Never loosen credential remote-model
   policy to make setup pass.
5. Establish the user's chosen mode: `journal` records requests; `observe`
   additionally records safety reviews; `enforce` can block calls. If a mode is
   already authorized, call `configure_safety(root, mode)`; otherwise explain
   the modes and ask for the choice. This writes project policy, not hook files.
   Preserve a stronger managed minimum and do not silently weaken an existing
   mode. Codex maps the gate's `ask` decision to a denial because its current
   `PreToolUse` cannot ask for permission.
   If an enforced policy blocks this configuration change, explain the need
   for an authorized change from a trusted terminal; do not retry through a
   different tool.
6. Explain that the user must install and enable the plugin and review/trust
   its hooks in Codex. Do not also install standalone hooks: that can duplicate
   journalling and review. CLI `status` cannot prove plugin enablement or trust.
   After the user enables and trusts the hook, run a harmless command such as
   `pwd` and inspect `commands` for the corresponding request. A journal entry
   confirms observation of that request, not full enforcement coverage.

Finish with the project, interpreter, judge reachability, effective mode and
what was actually verified. Hosted tools, subsequent `write_stdin` input and
specialized paths opting out of hooks are outside the hook's coverage. If
activation or trust was not tested in Codex, say so.

## Managed security tools

For security-tool setup or troubleshooting, call
`security_status(root, tools_directory?)` first. It reads and validates local
resources without network access or installation. Read component statuses,
paths, versions, advisory freshness and any last-setup diagnostic. This
readiness is separate from the judge and hook status above.

Only on an explicit user request to install or refresh security resources, call
`security_setup(root, refresh?, tools_directory?)`. Setup downloads pinned
OSV-Scanner, Semgrep CE and TypeScript plus public npm advisory data into
`~/.cache/vibe-sentinel/security` by default. It requires existing `uv` and a
trusted system Node installation. It does not change global PATH, Codex
configuration or the application's virtualenv. Explain a missing prerequisite;
do not install unrelated prerequisites or change global settings implicitly.

Use `refresh=true` when an advisory update is requested. Valid pinned tools are
reused. A stale database makes status exit `2`, but validated stale data remains
usable offline; ordinary setup does not refresh it. Missing or invalid resources
can be repaired by ordinary setup. Read the diagnostic if setup fails, including
the managed store's 4 GiB cap, and report the result rather than assuming success.
The CLI equivalents are `vibe-sentinel security setup`,
`vibe-sentinel security setup --refresh` and `vibe-sentinel security status`.
Use the same `tools_directory` (CLI `--tools-directory`) for later checks when
an alternate store was selected.

Scans discover managed resources and never install or update them automatically.
If the running plugin exposes eight tools without `security_setup` and
`security_status`, explain that its connection must reload or restart to expose
the updated ten-tool server. Do not restart Codex on the user's behalf. See
[security checks](../../../../docs/security-checks.md) for inputs and limits.
