# Full-stack support and modular delivery

Planning baseline: 2026-11-03. This document records the agreed direction and
staged implementation status. Proposed packages, interfaces and capabilities below are not
available unless explicitly marked as implemented. There are no release dates
or reserved package names implied by this plan.

## Direction and boundaries

- Support Node backends as a first-class target alongside Python services and
  browser applications. JavaScript support must serve both Node and the browser.
- Keep one shared engine for configuration, measurements, comparison, history,
  policy and model interaction. Add optional ecosystem and framework support.
- Keep the project open-source. Prioritize useful results, repeat use and
  sustainable contributions over download counts or a larger feature list.
- Preserve the existing distinctions: probes describe movement, state gates
  describe current facts, and the safety gate reviews proposed actions.
- The configured judge remains independent of the coding agent. It may assess
  measured changes but cannot invent or suppress them. Missing answers are not
  clean results. This plan does not introduce a model-free scan mode.
- Do not become a linter, code-quality grader, CVE service or automatic fixer.
  Integrate evidence from suitable existing tools instead of duplicating their
  analyses when the integration preserves Sentinel's boundaries.

The three milestones below are complete in source and pending release, within
their documented coverage limits. Publication and baseline acceptance remain
separate actions.

## What exists today

| Area | Implemented | Missing or limited |
| --- | --- | --- |
| Structural measurements | Six built-in probes, custom subprocess probes, baselines, horizons and trends | Most semantic source readers are Python-specific |
| State gates | Credentials, licences and package provenance | Installed dependency and licence inventories use Python metadata |
| Agent actions | Claude Code, Cursor and Codex adapters; journal, observe and enforce modes | Host-specific coverage gaps; arbitrary MCP arguments are not reviewed |
| Customization | Declared probes, lenses, dangers, secrets, policies, evaluation cases and explicit local ecosystem packs | Pack inventory readers are contracts only; automatic inventory consumption remains unimplemented |
| Interfaces | CLI, version 3 results, read-only historical exports, aggregate CI summaries, offline local viewer, local history and Codex MCP tools/skills | Viewer distributed as checkout source, without action-review history; several MCP history tools return terminal text; detailed exports can contain sensitive text |
| JavaScript projects | Optional JS/TS workspace and import pack, bounded workspace-aware install checks, starter profiles, source asset measurements and an existing-artifact Vite reader | No npm installed inventory or npm licence/provenance gates; resolver scope is explicit and build freshness is unassessed |
| Local security checks | Opt-in offline npm advisories, selected JS/TS source rules, and credential review of existing Next.js static exports | Coverage depends on the advisory snapshot and selected inputs. Runtime authorization, deployment state and build freshness remain unassessed |
| Backend declarations | Optional OpenAPI, Alembic, JSON Schema 2020-12 and Celery readers | Bounded static projections; application behavior, deployed state and effective runtime configuration are unassessed |

Generic file sizes and selected JS/TS patterns can already be configured.
The optional [Vite reader](vite-builds.md) adds stable build-entry and shared
artifact measurements. Artifact freshness remains unassessed. Further framework
coverage and client-boundary evidence remain future work.

## Fix the foundations first

### Dependencies and measured environments

Preserve requested Python extras, evaluate environment markers and make selected
dependency groups explicit when calculating dependency closure. Do not include
all optional dependencies to hide orphan findings. The documented
`mcp -> pyjwt[crypto] -> cryptography` finding is a regression fixture for this
work, not a policy exception to add.

Separate scanner runtime from application environment. The implemented
[environment selection](environments.md) keeps Sentinel on Python 3.13 while
inspecting a configured local Python 3.10+ application interpreter. Selecting a
project root alone does not activate its environment. Measurements record the
selected interpreter; unavailable, incompatible or incomplete targets fail.
Integration verification passed, including a Python 3.13 scanner inspecting a
separate Python 3.12 target. See the environment guide for collector coverage,
startup-hook limitations and the unchanged source-parser scope. Pending release.

Acceptance: fixtures cover activated and inactive extras, transitive extras,
platform markers, selected groups, separate runtimes and genuinely orphaned
packages. Auditing one environment must never silently describe another.

### Relationships rather than counts alone

The Python module probe stores import counts as measurements and target names
in attributes. Attributes are deliberately not diffed. A rearrangement that
preserves counts can therefore escape detection.

Introduce stable observations for dependency edges and declared components.
Preserve existing series rather than silently changing their meaning. Shared
edge representation should support Python, TypeScript and JavaScript readers,
with explicit resolution coverage and runtime/type-only classification where
the reader can establish it.

Acceptance: an edge swap that preserves all counts is visible; repeated scans
of the same inputs are stable; unresolved imports are identified; adding a new
measurement definition does not manufacture a historical code change.

### One versioned result contract

CLI JSON, MCP and exports use the implemented [versioned result contract](results.md).
It includes run and baseline identity, source revision and dirty-tree state,
configuration/rule fingerprints, tool and pack versions, environment,
measurement scope and judge identity where available. Version 3 captures
configured questions, validated answers and final attribution. Read-only
historical export offers aggregate and sensitive local-detail profiles;
versions 1 and 2 remain readable without reconstructed evidence. A configured
model name alone is not proof of immutable model weights.

Keep completed work, findings, failed stages, unconfigured gates and unverified
coverage distinct. Non-Git projects need an explicit source identity policy;
they must not be rejected merely for lacking a commit SHA.

Acceptance: a failed probe, unavailable judge, missing baseline or stale build
cannot render as a clean comparison. Shared exports retain privacy filtering;
raw credentials, complete machine inventories and arbitrary diagnostics do not
become shareable merely because a new interface exists. Database changes use
versioned migrations and preserve recorded history.

## Packaging proposal

Split by ecosystem, framework, evidence format and integration, rather than
making frontend and backend two mutually exclusive packages.

| Proposed component | Owns | Dependency boundary |
| --- | --- | --- |
| Shared core (`vibe-sentinel`) | CLI, config, observation protocol, comparison, history, model client and shared policy machinery | No mandatory Node or browser runtime |
| Python pack | Python AST readers and Python environment inventory | Optional ecosystem implementation |
| JavaScript pack | JS/TS resolution, workspaces, Node dependency inventory | Optional Node-based helpers where needed |
| Framework packs | Next.js boundaries, FastAPI declarations, Celery tasks and other named adapters | Install only the frameworks being inspected |
| Artifact readers | OpenAPI, build manifests, schema exports and deployment declarations | Language-neutral when the format permits it |
| Agent integrations | MCP and host adapters | Keep heavy dependencies optional; preserve the hook import budget |
| Local viewer | Read exported evidence | Separate static application, no mandatory server |

These are responsibility boundaries, not a requirement for one distribution
per row. Small declarative profiles do not each need a package. Shared gates
may consume ecosystem readers without copying policy or verdict logic into
each pack. The core still requires the configured model when a judgement is
needed.

The pack contract must cover ecosystem inventory readers as well as drift
probes. Define how an inventory declares its ecosystem, workspace, environment,
selection and incomplete coverage before shared licence/provenance gates read
it. Standing findings must remain state-gate findings, not become observations
that disappear into an accepted baseline.

Define finding and pin identity across ecosystems before adding npm gates.
An identically named Python and npm package must not share an acceptance, and
multiple installed versions or workspace instances must remain distinguishable.
Specify explicit pin scope and migration behavior for existing Python findings;
never silently broaden an old acceptance to another ecosystem or instance.

Python extras select additional dependencies; they do not remove files from
the base wheel. Substantial optional implementations need separate Python or
npm distributions if avoiding unrelated downloads is the goal. Keep one
repository initially; separate distributions do not require separate histories,
databases, daemons or independent release teams.

### Incremental extraction

1. Define a small pack contract around the existing subprocess-to-JSON protocol:
   pack identity/version, supported inputs, probe declarations, required
   executables, protocol compatibility and validation fixtures.
2. Deliver JavaScript as the first external pack. Prove the boundary before
   moving the working Python implementation.
3. Extract Python-specific implementation only after the contract works.
   Preserve existing installation and command behavior through a compatibility
   extra or convenience distribution, chosen before that migration.
4. Offer Python, Node and full-stack setup profiles that list exactly what they
   install and measure. Do not automatically fetch executable packs because a
   manifest was discovered.

Use fixed argv, validated inputs and the existing runner boundary. A missing
or incompatible pack must name the remedy rather than silently omit coverage.
Record pack versions so changed reader semantics can be distinguished from
application drift. Do not introduce a broad in-process plugin framework before
the simpler protocol has demonstrated a need for it.

The Python runtime and configured judge remain an adoption cost for Node-only
users. Modularization does not solve that. Evaluate a supported installer or
bundled runtime separately; an npm wrapper must explain any Python dependency
or download rather than presenting itself as a standalone Node engine.

## Frontend and Node deliverables

| Order | Deliverable | First useful scope |
| --- | --- | --- |
| Implemented in source; pending release | [Tested starter profiles](starter-profiles.md) | TS/TSX/JS file measurements, source asset bytes and supported patterns; explicit coverage rather than implied Python-probe parity |
| Implemented in source; pending release | [Workspace-aware declarations and install targets](gates.md#npm-install-targets) | Resolve supported workspace selectors and current directories to the correct manifest; do not union all workspace dependencies |
| Implemented in source; pending release | [JS/TS dependency relationships](javascript-pack.md) | Static imports/re-exports, supported aliases and workspace edges; unresolved and dynamic resolution gaps stay explicit |
| Implemented in source; pending release | [Build-output history](vite-builds.md) | Existing Vite manifests, logical entries, shared artifact ownership and raw file weights; no build execution or freshness verification |
| Next | Client boundary evidence | Distinguish client entry directives from modules reachable through their imports; keep Server Functions separate |
| Next | Offline npm licence/provenance support | Start with one supported package-manager layout; distinguish declared, lock-resolved and installed states, including multiple versions |
| Implemented in source; pending release | [Local JS/TS security checks](security-checks.md) | Managed offline npm advisory snapshots, selected browser and Node source rules, and bounded Next.js export credential review with a loopback judge |
| Later | Broader frontend and Node security evidence | Additional source rules and framework-specific evidence, with separate runtime authorization testing |
| Later | Design-system and type-escape trends | Count declared styling approaches, token use and suppression/type-escape usage without judging individual lines |

Read existing build artifacts before adding optional build execution. Builds
execute project code and may take minutes. Hashed filenames are not stable
entry identities; missing or stale artifacts are not zero-byte measurements.
Do not infer actual runtime performance from payload size alone.

Public environment-variable prefixes indicate intended exposure, not proof of
a secret. A credential candidate in a public variable and one observed in a
browser artifact are distinct evidence. Preserve global source exclusions;
artifact inspection needs its own explicit scope and size/coverage limits.

Executable provenance for `npx`, `npm exec`, `pnpm dlx` and `bunx` follows
workspace resolution. These commands can use local executables; their verb
alone is not proof of a registry fetch or undeclared package execution.

## Backend application deliverables

| Order | Deliverable | First useful scope |
| --- | --- | --- |
| Implemented in source; pending release | Component dependency relationships | Optional [Python](python-relationships.md) and [JS/TS](javascript-pack.md) import relationships using shared edges; resolution and dynamic coverage limits stay explicit |
| Implemented in source; pending release | [API contract inventory](openapi.md) | Existing OpenAPI artifacts: methods, paths, schemas, required fields and declared security attachments |
| Implemented in source; pending release | [Schema and migration topology](backend-schemas.md) | Bounded Alembic revision relationships and JSON Schema 2020-12 declarations; deployed state and artifact freshness remain unassessed |
| Implemented in source; pending release | [Jobs and messaging topology](job-inventories.md) | Bounded Celery task, handler, exact route and schedule declarations; runtime registration, effective configuration and broker behavior remain unassessed |
| Later | Deployment/configuration structure | One supported declaration format: services, image references, ports, mounts, dependencies and environment-variable names |

Start with language-neutral artifacts where possible, then add named framework
adapters. Never import an application or start its server just to discover its
shape without explicit execution scope. Dynamic registrations must remain
unmeasured when they cannot be resolved.

An API declaration is not proof of actual authorization. A migration artifact
is not the deployed database. A queue declaration is not a delivery guarantee.
Do not execute migrations, connect to production or export configuration values
as part of these inventories. Existing action safety is a separate question.

## Interfaces and adoption deliverables

The implemented [offline local viewer](local-viewer.md) reads explicitly selected
exports without a server, network requests or browser storage. It separates
state gates, baseline drift, captured questions and answers, and coverage/source
identity. Historical or aggregate results show why detailed evidence is absent.
Horizon availability is visible; detailed recomputed trend readings and action
review history are outside this export. No single "safe project" score is
produced. Local-detail exports can contain sensitive project text. The viewer
is source distributed in the checkout, outside the Python wheel and sdist.

The implemented [CI summary command](ci-evidence.md) reads an existing result
and emits aggregate Markdown with its captured source identity and recorded
scan exit status. It does not run a scan, consult the current checkout, upload
an artifact or post a PR comment. Missing history is a missing comparison.
Uploads and PR comments require explicit configured intent; baseline acceptance
and policy pins remain separate authorized decisions.

Provide runnable examples for a Python service, a Node service and a frontend/
API monorepo. Each includes configuration, a baseline, an intentional change
and expected evidence. Demonstrate environment drift without source changes,
architectural relationship changes and a harmless declared enforcement test.
Publish setup cost, scan/hook latency, false positives and model-dependent
evaluation results with their conditions rather than claiming universal rates.

## Delivery sequence and completion criteria

| Milestone | Scope | Done when |
| --- | --- | --- |
| 1: trustworthy foundations | Dependency accounting, environment scope, result provenance, frontend starter profiles and pack contract | Fixtures cover extras, distinct runtimes, unsupported inputs and incomplete results; existing users retain working installs |
| 2: balanced application coverage | JavaScript pack, workspace/import support, Python edge observations and OpenAPI inventory | A real frontend/API monorepo demonstrates meaningful changes on both sides, including count-preserving relationship changes |
| 3: richer evidence and usability | Build artifacts, schema/job inventories, local viewer and CI evidence | A reviewer can trace each finding to its measurement, source identity, configured question and recorded answer |

Current module status; checked items are verified and pending release:

- [x] Dependency accounting: implemented and verified, including explicit
  [extras and dependency-group selection](gates.md#select-the-dependencies-installed-in-this-environment).
- [x] [Node and frontend starter profiles](starter-profiles.md): implemented
  and verified.
- [x] [Environment scope](environments.md): implemented and verified, including
  separate runtimes and incomplete-target failures; pending release.
- [x] [Versioned result contract](results.md): implemented and verified, including
  persisted provenance, failure statuses and limited exports; pending release.
- [x] [Ecosystem pack contract](pack-contract.md): implemented and verified,
  pending release. Inventory support is a validated contract only, without
  automatic reader execution or gate consumption; pack history is baseline-only.

Integrated checks and independent review passed for the checked modules. The
structural scan completed with expected drift findings, all requested lenses
answered and all state gates complete with no failing findings. Its stored result
matched the JSON output, the limited version 2 export validated, and recorded
version 1 results remained readable. No baseline or policy pins were changed.
Milestone 1 is complete in source and pending release.

Milestone 2 module status:

- [x] [JavaScript pack](javascript-pack.md): implemented and verified, pending
  release, including the independently installed npm artifact.
- [x] [Workspace-aware install targeting](gates.md#npm-install-targets): implemented
  and verified, pending release. Supported npm workspace
  and directory selectors compare manifests separately; unsupported targeting
  remains explicit.
- [x] [Python component relationships](python-relationships.md): implemented and
  verified, pending release, including guarded comparison scope.
- [x] [OpenAPI artifact inventory](openapi.md): implemented and verified, pending
  release, including isolated scanner startup and explicit reference coverage.
- [x] [Frontend/API monorepo acceptance](fullstack-example.md): implemented and
  verified, pending release. Static readers never launch the demonstration
  servers; installed-wheel baseline and repeat scans preserve result contracts.

Milestone 2 is complete in source and pending release. The full test suites,
independent review, isolated Python wheel and npm artifact checks passed. The
final structural scan completed on an unchanged-settings retry after one model
answer was truncated; all eight lenses answered, all state gates completed,
and no gate findings failed. The incomplete attempt remains recorded as an
error. No baseline, policy pins or configured model limits were changed.

Python extraction remains later work.

Milestone 3 module status; checked items are verified in source and pending release:

- [x] Captured evidence and historical export: preserve original configured
  questions, individual answers and decision origins alongside measurements;
  historical export never substitutes current configuration for missing evidence.
  Existing aggregate shareable exports retain their privacy boundary.
  Version 3 captures bounded structural identities and answer attribution;
  versions 1 and 2 remain readable without reconstructed evidence.
- [x] Build artifacts: an opt-in reader for existing Vite manifests and raw
  referenced-file sizes, with stable entry identities, explicit aggregation of
  generated chunks and unassessed freshness. No builds or application imports.
- [x] Schema and migration inventories: optional [Alembic and JSON Schema
  2020-12 readers](backend-schemas.md) provide bounded structural evidence from
  declarations and migration relationships, without application imports,
  migration execution or database connections. Unsupported structure stays
  explicit; deployed state and artifact freshness remain unassessed.
- [x] Job inventories: an optional [Celery reader](job-inventories.md) records
  bounded task, handler, exact route and supported schedule declarations, with
  count-preserving relationship changes and explicit dynamic coverage gaps.
  Runtime registration, effective configuration and broker behavior remain
  unassessed.
- [x] [Local viewer](local-viewer.md): a separate offline, read-only application
  over explicitly exported evidence. Drift, state, judge failures and unavailable
  evidence remain distinct; imported text is rendered as text. No action-review
  history or current-source verification is implied.
- [x] [CI evidence](ci-evidence.md): offline aggregate Markdown summaries tied to
  captured source identity and recorded scan exit status, including historical
  versions 1 and 2. Strict bounded input rejects unsupported or contradictory
  results. No automatic upload, PR posting, baseline acceptance or policy changes.

Milestone 3 is complete in source and pending release. The readers retain the
existing probe boundary; the viewer and CI summary consume captured evidence
without running scans or inferring past questions. Each module was independently
reviewed and verified. Package and private tests, viewer and JavaScript-pack
tests, mandatory mechanical checks and isolated build/install checks passed.
The final live structural scan completed with recorded drift findings and exit 1.
All eight requested lenses answered and no state gate findings failed. The
baseline remained unchanged. Browser visual
verification was blocked by local-file policy; automated contract and DOM
checks do not establish rendered layout or browser compatibility.

## Further work

The next coverage candidates remain npm installed inventory and licence/provenance
handling, client boundaries, explicitly scoped browser-artifact credential checks
and further framework or dynamic-registration support. Distribution beyond a
checkout and additional artifact formats require separate decisions. These are
not release commitments or claims of implemented support.

All implementation work follows the repository's mandatory checks. Each new
measurement needs stable-key and coverage tests; each new judgement needs calm,
concerning and out-of-scope evaluation cases. No universal threshold is added
without evidence, and no model answer is treated as a correctness guarantee.

## Development roles

The separate [Sentinel development plugin](../plugins/development/README.md)
provides a reusable workflow and role templates for this development pattern.

Development can use a lead integrator, ecosystem implementers and an independent
QA/review role. These are task-scoped collaborators, not installed runtime
components. Assign a JavaScript/Node specialist to workspace and import readers,
a backend/core specialist to environment accounting and API/schema readers,
and a UI specialist for viewer changes. QA should define acceptance
fixtures, exercise failure/privacy/compatibility cases and verify the assembled
result without weakening expectations to match the implementation. The lead
owns shared contracts and integration. Separate roles improve review coverage;
they do not guarantee correctness or independent model judgement.

## Decisions still to make

- Distribution names, install syntax, version compatibility and the eventual
  Python-pack migration strategy.
- Which installed Node package-manager layout and additional TypeScript
  resolution modes to support next, based on real pilot repositories.
- Environment selection and how to establish active extras/groups when an
  installed environment does not retain sufficient installation intent.
- Source fingerprints for dirty or non-Git trees, artifact freshness evidence
  and rules for comparing different branches/environments.
- Further backend framework, schema/job artifact and dynamic-registration
  coverage beyond the bounded OpenAPI, Alembic, JSON Schema and Celery readers.
- Installer/runtime portability, supported platforms and maintenance budget.
- Viewer distribution beyond checkout source and any future export size/privacy
  policy changes beyond the implemented bounded profiles.

Defer a broad Node SDK until external probes reveal repeated needs. Defer a
hosted dashboard, automatic remediation, automatic baseline acceptance and
automatic policy exceptions. Framework breadth should follow demonstrated
use, not a promise to support every stack at once.

## Related documentation

- [Frontend exploration](frontend-support.md): ecosystem-specific details and
  corrected assumptions.
- [Adding a probe](adding-a-probe.md): current protocol, lenses and cases.
- [Codex plugin](codex-plugin.md): implemented agent integration and limits.
- [Threat model](THREAT_MODEL.md): existing controls and residual risks.
