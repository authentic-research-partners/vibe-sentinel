# JavaScript and TypeScript support

Sentinel can measure selected JavaScript and TypeScript source in Node services,
browser apps and mixed repositories. For a first scan from the development
checkout, choose a tested profile that matches your project. [Starter profiles](starter-profiles.md)

The matrix separates what the released CLI can do from readers available in the
development checkout. Planned coverage follows below. [Roadmap](roadmap.md)

## Supported coverage

| Capability | Available in | Coverage |
| --- | --- | --- |
| Pattern census | 0.5.1 | Select JavaScript, TypeScript or TSX as `LANGUAGE` and supply a compatible ast-grep pattern. |
| File lengths and asset sizes | 0.5.1 | Use `file-length` categories such as `code=*.ts,*.tsx` and `UNIT = "bytes"` for binary assets. |
| Credential candidates | 0.5.1 | The source walk recognizes `.npmrc` and npm token shapes. Client artifact exposure is a separate question. |
| JavaScript toolchain dangers | 0.5.1 | Declared dangers cover publishing, deployment, forced resolution and exposed dev servers in projects with a root `package.json`. |
| Undeclared npm-family installs | 0.5.1, expanded in checkout | Supported workspace and prefix selectors compare each target manifest separately in the checkout. Unsupported targeting remains explicit. [Target scope](gates.md#npm-install-targets) |
| JS/TS relationships | Development checkout | The separately installed pack measures workspace declarations and bounded static imports. [JavaScript pack](javascript-pack.md) |
| Vite build evidence | Development checkout | The optional reader measures logical entries, shared ownership and raw file sizes from existing manifests and outputs. Build freshness remains unassessed. [Vite reader](vite-builds.md) |
| Local security checks | Development checkout | Set up persistent tools, then select offline npm lockfile advisories, bounded JS/TS source checks or credential exposure in an existing Next.js static export. [Security checks](security-checks.md) |

The checkout's offline viewer displays captured structural evidence and its
recorded coverage. [Local viewer](local-viewer.md)

A custom probe can be a Node program: the protocol is one JSON object on
stdout, with diagnostics on stderr. Project-declared commands run as written,
with fixed argv and no shell interpolation. The shipped Python probes use the
interpreter running Vibe Sentinel.

Configuration support is not complete ecosystem support. The default source
probes for comments, module organization and exception handlers parse Python.
`packages.py`, `licenses.py` and `probes/dependencies.py` inspect Python
distributions. Reading npm install declarations does not give those readers an
installed JavaScript inventory. A frontend or Node starter configuration must
identify the measurements it enables and the environment it actually audits.

The built-in pattern placeholder also limits its characters. Patterns needing
`!`, `@`, `/`, `<`, `>` or `:` cannot simply be placed in that placeholder.
Use a deliberately declared probe or reviewed override where appropriate;
do not broaden validation casually. A lexer or parser is needed for comment
directives and other constructs a structural pattern cannot represent reliably.

## Shared JavaScript and Node foundations

### Workspace declarations and command targets

The safety gate now resolves supported npm workspace names, exact declared paths,
prefix directories and literal working-directory changes before comparing
package declarations. Repeated workspace selectors remain separate comparisons,
so a dependency in one application cannot authorize it for another. Unsupported
selectors and uncertain targets reach the existing review question as not
compared. The reader is stdlib-only and does not execute Node or npm; its bounds
and ambient configuration limitations are documented in
[npm install targets](gates.md#npm-install-targets).

The separately installed JavaScript pack measures workspace and import edges.
An edge is a structural observation; calling it a violation requires a project
declaration. Neither the safety target reader nor those probes supply an
installed npm inventory.

Executable resolution is a separate proposal. `npx` can run local executables
or fetch packages, so the verb alone is not a dangerous action. The question
is which executable would run, where it comes from and whether fetching is
involved. `npm exec`, `pnpm dlx` and `bunx` need their own supported semantics;
do not assume that all share npm's resolution rules. See
[npm exec](https://docs.npmjs.com/cli/v11/commands/npm-exec/).

## Proposed extensions

### Declared, resolved and installed dependencies

These are three distinct inventories. A manifest states intent, a lockfile
records resolution, and installed metadata describes the environment being
measured. Record versions and origins as identities, separately from numeric
counts. Preserve multiple installed versions and their locations rather than
collapsing everything into one row per package name.

Start with an explicitly supported package-manager layout. A shallow
`node_modules/*/package.json` walk is not universal: scoped and nested packages
need handling, pnpm uses a linked layout, and Yarn PnP may have no
`node_modules` directory. Unsupported layouts must report their coverage gap.
See [pnpm's layout](https://pnpm.io/symlinked-node-modules-structure) and
[Yarn PnP](https://yarnpkg.com/features/pnp).

Readers should remain offline and avoid running dependency code. Lockfile
origin and integrity fields are evidence to interpret, not proof that the
installed bytes match them. Source URLs may carry credentials and need
redaction before they enter reports or history. Dependency scope and storage
cost need measurement before choosing defaults.

An npm licence reader could reuse the existing SPDX expression evaluator,
licence categories and text fingerprinting after adapting the input source.
That adapter and the installed inventory are new work. Install-script presence
is another measurable capability, not an automatic unsafe verdict: any gate
must ask a declared policy question. Neither proposal adds CVE lookup.

### Imports and configuration drift

An import graph can measure fan-in, fan-out, re-export concentration and
cross-workspace edges in both Node services and browser code. Project lenses
can then ask about database access spreading into request handlers, shared
packages gaining application dependencies, or a browser entry reaching server
modules. Record the graph first; do not ask the model to invent its edges.

Static imports and re-exports are a bounded starting point. CommonJS,
TypeScript aliases, conditional exports, dynamic imports and generated modules
require explicit handling or reported uncertainty. A deep import is not
inherently wrong; whether its target is declared and exposed by the package is
a separate question.

Counts of type assertions, `any` and suppression directives can describe where
those constructs accumulate without deciding whether an individual use is
justified. Effective TypeScript settings are also useful state observations,
but reading a local `strict` field is insufficient: defaults, `extends` and
overrides change its meaning. TS and TSX commentary measurements should remain
separate categories until their interpretation is evaluated.

### React and Next.js structure

Direct `"use client"` declarations are entry-point measurements, not a count
of everything sent to the browser. Client scope also depends on the import
graph. A proposed reader should distinguish declared entry points, reachable
modules and unresolved edges. It must not treat adding a directive as always
correct or always suspicious. `"use server"` marks Server Functions; it does
not enumerate all Server Components. See
[Next's component model](https://nextjs.org/docs/app/getting-started/server-and-client-components)
and [React's server directive](https://react.dev/reference/rsc/use-server).

Other possible observations include hook locations, component counts and props,
styling-system usage, route declarations and public asset inventories. These
describe organization. Multiple styling systems do not prove who wrote the
code. A source route does not prove a URL is deployed, and a file in `public/`
does not prove it is currently reachable on a public host. Backend route
inventories need framework-specific readers for the same reason.

Accessibility violations, floating-promise errors and type-correctness checks
belong to their existing tools. Aggregating their verdicts by directory does
not turn them into structural measurements. A test-to-source ratio can be a
count, but must not be described as test coverage.

### Further build evidence and client exposure

The Vite reader establishes the existing-artifact path. Candidate extensions
include other builders, recorded freshness evidence and separate compressed
sizes. Link a builder, mode and source identity to an artifact only when the
build supplies that evidence. Keep server bundles and browser assets distinct.
Missing artifacts should remain a coverage gap. Build execution requires an
explicit workflow because it runs project scripts. Thresholds should follow
measurement or project policy.

#### Distinguish potential exposure from observed exposure

Public environment prefixes can identify values intended for browser use,
but neither a prefix nor a high-entropy string proves a leaked credential.
Public URLs and identifiers are legitimate. Framework, configured prefix,
mode, value classification and actual build evidence all matter. Vite, for
example, permits a custom `envPrefix`; see its
[environment documentation](https://vite.dev/guide/env-and-mode).

The checkout's artifact credential check looks for candidates in a selected
Next.js static export. Potential exposure through a client-exportable source
variable is a separate question. [Browser artifact credentials](security-checks.md#browser-artifact-credentials)

### Probe authoring

The existing JSON protocol is enough to author Node probes today. A small npm
SDK may help once several real probes reveal repeated validation and emission
needs; it is not a prerequisite for language support. Parser ownership and
supported runtime versions remain decisions even if a helper package exists.
