---
name: sentinel-inspect
description: Run requested Vibe Sentinel drift scans or local JS/TS security checks, and explain recorded drift, trends, gate findings, command history and safety verdicts.
---

# Inspect Vibe Sentinel evidence

Use the Vibe Sentinel MCP server with the intended project's absolute `root`.
The root must contain `.vibe-sentinel.toml`. For setup or runtime failures, use
the packaged `sentinel-setup` skill. Do not scan automatically on every turn.

Choose the operation that answers the user's request:

| Question | Tool |
| --- | --- |
| What does a fresh measurement find? | `scan(root, since?, fit?)` |
| What do the selected local JS/TS security checks find? | `security(root, lockfile?, advisory_db?, source?, browser_artifacts?, tools_directory?)` |
| Are managed security tools and advisory data ready? | `security_status(root, tools_directory?)` |
| What was recorded previously? | `history(root, limit?, run?)` |
| What moved across the recorded series? | `trends(root, runs?)` |
| Which tool requests were recorded? | `commands(root, limit?, session?)` |
| Why was an action reviewed or blocked? | `safety_history(root, limit?, session?, verdict?)` |
| Which policy and judge are configured? | `status(root)` |

`since` is a comma-separated horizon string, such as `1w,1m`; `fit` controls
the scan's fitted series. A scan executes declared probes, calls the configured
model when needed and writes history. Inspect unfamiliar probe commands before
execution and only scan within the user's requested scope. A scan is not a
read-only query. History tools may initialize or health-check the database.
The first successful scan establishes the initial baseline; later scans do not
accept their changes automatically.

Run `security` only for requested checks. Select a project-relative npm
`package-lock.json`, a project-relative JS/TS source directory, or an existing
project-relative Next.js static export. Managed tools and the advisory database
are discovered automatically; `advisory_db` can override the database with a
prepared local OSV cache. Use `security_status` for read-only, offline readiness
and freshness checks. A stale validated database remains usable, but report
its age and the refresh remediation. Do not install or refresh automatically
because a scan is requested: use the setup skill and `security_setup` only for
an explicit installation or refresh request. Building the app is separate.
This tool leaves drift history and the baseline untouched. Check `outcome`, `exit_code` and each entry in `data.checks`.
Report findings alongside any coverage gaps. Exit 2 takes precedence over
findings when a check is unavailable or failed. For artifact findings, use
the reported locations and keep credential review on the loopback judge.

For scans, read `data.result.outcome` and the response `summary` when available.
Older plugin versions can report outer `outcome: error` for any exit code 2.
Use the validated inner result to distinguish `incomplete` coverage from an
`error` in a scan stage. If the result is absent, report the operation error
and any safe diagnostic notice. The notice that raw diagnostics stayed local
does not explain the scan outcome. For other tools, check `outcome` and
`exit_code` before interpreting the response.

When `counts.baseline_unmeasured` is positive, explain that the baseline lacks
comparable data for that many probes. Use `baseline_run_id` and recorded history
if the user asks which probes were missing or failed. Report current probe
success and failing state-gate findings separately from the comparison gap.
Check `failed_stages` and judge statuses before attributing a failure to the
judge. Include the scanner `interpreter` and recorded `measured_environment`
when installed dependencies or licences are relevant. `[environment] python` can select a
separate target; changing `root` alone does not activate its virtualenv.
`measured_environment` is null when the dependency observation is absent or
failed, so do not infer a successful reading from configuration or alter the
selection merely to make inspection succeed. Historical findings retain the
scope they recorded; current settings do not establish an earlier target.

Scan tool results omit the full snapshot, credential source excerpts and raw
stderr. Direct the user to local CLI diagnostics when needed; do not retrieve
those diagnostics or the full CLI JSON to work around these omissions. MCP
results become Codex context and may reach a hosted model even when the judge is local. Use
the reported paths, verdicts and explanations; the full report remains local.

Explain only changes and findings present in the engine's output. Distinguish
measured facts from the configured judge's severity or verdict. Codex can
explain the evidence, but must not invent extra measured changes, replace the
judge's verdict, or describe a drift scan as a code-quality or CVE scan.

Gate findings describe current state and remain relevant on every scan.
Report each gate's status separately, including unconfigured or unavailable
coverage. A recorded run ID confirms history was written, but a history
fallback alone does not establish that all gates passed. Describe file counts
from filesystem probes as measured files unless Git tracking was checked.
Baseline acceptance concerns drift; it does not clear a credential, licence
or provenance finding. Do not accept a baseline or create a policy pin unless
the user authorizes that specific decision. Neither action is exposed by this
MCP interface; if requested, use the existing CLI/config workflow with the
required pin reason and verification date.

Respect the engine's redaction. Never read or reproduce raw credentials to
explain a finding, and never enable remote credential review as a workaround.
The journal records proposed requests, not successful execution. Codex patch
bodies and arbitrary MCP arguments are omitted, while shell command text can
still contain sensitive information. Summarize command evidence without
repeating suspected secret values. A journal entry or CLI status cannot prove
that the plugin is enabled and trusted for every tool path.

When an action was blocked, explain the recorded reason and supported
remediation. Do not retry through another tool, disable the hook or weaken
policy to get the same blocked action through. Codex currently receives a
denial for the gate's `ask` decision as well as its `deny` decision.
