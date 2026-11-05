# JavaScript and TypeScript pack

The optional `@vibe-sentinel/javascript` pack measures declared workspace
components and static JS/TS dependency relationships. It is implemented in
source and pending release. The npm name is a local package identity here;
these instructions do not assume that a registry package has been published.

The pack requires Node 22 or newer and its declared TypeScript 6.0.3 parser
dependency. The Vibe Sentinel scanner still requires Python 3.13 and a configured
judge when judgement is needed. Installing the Python engine does not install
Node or this pack. Neither scans nor pack loading fetch dependencies.

## Install deliberately from source

From a trusted checkout, install only the pack's declared dependencies and
produce its npm artifact:

```bash
cd /path/to/vibe-sentinel/packs/javascript
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm pack --ignore-scripts
```

Then explicitly install the generated tarball into the project to be measured:

```bash
cd /path/to/my-project
npm install --save-dev --ignore-scripts --no-audit --no-fund /path/to/vibe-sentinel/packs/javascript/vibe-sentinel-javascript-0.1.0.tgz
```

These installation commands can contact the configured npm registry for the
pinned parser dependency. They run only when you invoke them. The reader itself
works offline and does not install packages, run builds, evaluate application
modules or execute npm lifecycle scripts. An installation does not select the
pack for scans; add it explicitly to the project's existing config:

```toml
[packs]
manifests = ["node_modules/@vibe-sentinel/javascript/pack.toml"]
```

Merge this table without overwriting existing configuration. Retain the
measurements that apply to your project. A Node-only project can deliberately
set `[probes] use_builtins = false` to avoid unrelated Python source probes.
This does not enable npm licence or provenance gates: the existing package and
licence gates still inspect Python distributions.

```bash
vibe-sentinel scan --list-packs
vibe-sentinel scan --list-probes
vibe-sentinel scan --probes=pack:javascript:workspaces,pack:javascript:imports
```

Both readers default to the scanned root. To measure a nested application,
set the same explicit root for each selected reader:

```toml
[probes.parameters."pack:javascript:workspaces"]
ROOT = "web"

[probes.parameters."pack:javascript:imports"]
ROOT = "web"
```

Arguments remain fixed argv values, and subprocesses run from the scanned
project directory. The manifest's executable path is resolved relative to its
installed package, so the reader does not depend on a globally installed npm
binary. See the [pack contract](pack-contract.md) for trust and path boundaries.

## What is measured

| Reader | Evidence |
| --- | --- |
| `pack:javascript:workspaces` | Root package and supported explicitly selected workspace declarations, names, component paths and source-file membership |
| `pack:javascript:imports` | Parsed JS/JSX/TS/TSX imports, re-exports, literal `require` and literal dynamic imports, with supported local, workspace and alias resolution |

Workspace discovery reads `package.json` workspace arrays or the supported
`workspaces.packages` form. It does not union every nested package it encounters.
Workspace patterns are bounded filesystem globs; unsupported selectors are
coverage gaps. `node_modules`, generated build output and virtual environments
are excluded. Symlinks are not followed. Inputs are bounded by file size and
traversal limits, and readers report a gap when a limit prevents inspection.

Import resolution uses the nearest supported `tsconfig.json` or `jsconfig.json`,
including in-root relative JSONC inheritance, `baseUrl` and `paths`. Without
one, the reader uses TypeScript's Bundler/ESNext resolution defaults. A resolved
edge is therefore not a promise that Node accepts that extension or directory
import at runtime. Conditional workspace exports, package `#imports`,
package-based config inheritance, project references, custom resolver plugins
and pnpm-only workspace configuration are explicit unsupported gaps. The
[package README](../packs/javascript/README.md) lists the full bounded scope.
Declared external package edges identify source relationships, not installed
packages or versions.

The TypeScript parser identifies syntax rather than matching import-like text
in comments or strings. Type-only relationships and runtime relationships have
distinct identities. Mixed declarations can produce one relationship of each
kind; these are lexical classifications, not a claim about emitted bundles or
runtime reachability. A repeated relationship records its occurrence count.

Every edge uses the shared `edge_schema = "1"` observation shape. Its key is
`edge:1:` followed by the SHA-256 of compact UTF-8 JSON containing
`[ecosystem, source, target, kind, phase, resolution]`. Sources use project-relative
`file:` identities; targets distinguish files, packages and builtins. Line
numbers and snippets do not define identity. Redirecting one import from one
module to another therefore records a removed and an added edge even when the
total count stays unchanged.

An unresolved relationship or computed dynamic import is a coverage gap.
Incomplete reader output exits nonzero, so the scanner records the probe as
unmeasured and does not turn missing edges into apparent removals or a clean
result. Diagnostic JSON may contain partial observations, but the scanner does
not accept them as a complete snapshot. Unsupported layouts and syntax need a
supported input or a reader change; they are not silently guessed.

This module does not provide an installed npm inventory, licence/CVE checks,
framework-specific React/Next.js semantics, execution of application code,
bundler analysis or runtime performance measurements. Workspace observations
do not change the command-safety gate's npm install-target interpretation.
The existing [starter profiles](starter-profiles.md) remain useful for source
lengths, patterns and asset bytes.

## History and validation

Result versions 2 and 3 record the selected pack manifest and executable paths.
A changed pack version, declaration or `ROOT` selection requires explicit
baseline acceptance before comparable measurements resume. Pack history is currently
baseline-only: pack series are excluded from horizons and fitted trends.
Baseline drift remains available, and inventory capabilities are not inferred
from this pack's presence. See [scan results](results.md).

The package's `npm test` suite exercises the JavaScript implementation. The
optional Python integration tests run the real loaded manifest and subprocess
runner, compare a count-preserving edge rewire, and check incomplete coverage.
An installation smoke test builds the npm tarball, installs it in a separate
temporary project using only local artifacts and a fresh offline cache, then
runs its installed manifest. It verifies package contents and does not rely on
the source checkout's dependency resolution.

```bash
# From the repository root, after explicitly installing pack dependencies:
pytest tests/test_javascript_pack.py -q
```

Python-only contributors can run the core suite without Node; these optional
integration tests report a skip if Node 22+ or the pack's declared parser is
unavailable. JavaScript pack verification requires both suites to actually run.
Pack source is included in the public repository, while Python wheels and
source distributions exclude it. No release step publishes npm artifacts.
