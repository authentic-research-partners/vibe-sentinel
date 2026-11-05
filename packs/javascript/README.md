# Vibe Sentinel JavaScript pack

An optional Node package that reads JavaScript and TypeScript source without
executing the measured application's code. It requires Node 22 or newer and
pins TypeScript 6.0.3, the JavaScript compiler API used for AST parsing,
configuration selection and static resolution. It is not a type checker,
security scanner, npm inventory or state gate.

This source package is installable with npm; it has not been published. After
an explicit local installation, select its installed `pack.toml` in Sentinel:

```toml
[packs]
manifests = ["tools/node_modules/@vibe-sentinel/javascript/pack.toml"]
```

The manifest resolves its executable wrapper relative to itself. Its Node
requirement is resolved through PATH. The working directory and `ROOT` probe
parameter refer to the measured project. No dependency installation, package
manager invocation, lifecycle hook, application import or network request is
made by either reader. The selected Node runtime, pack and parser dependency
remain trusted executable software, not a sandbox.

```sh
vibe-sentinel-javascript workspaces --root /path/to/project
vibe-sentinel-javascript imports --root /path/to/project
```

The two drift IDs are `pack:javascript:workspaces` and
`pack:javascript:imports`. Workspace measurement reports the root package,
explicit npm/Yarn workspaces, and source file membership. It does not inspect
imports or require a supported TypeScript config. Import measurement applies
the nearest `tsconfig.json` or `jsconfig.json`, follows resolved source imports
beyond those initial roots, then reports static dependency
relationships. An undeclared nested package is outside the selected workspace
set; it is not silently added.

## Supported static scope

- npm/Yarn `package.json` workspaces, either an array or `workspaces.packages`.
  Workspace patterns support relative segments, `*`, `**` and `?`.
- JS, JSX, TS, TSX, MJS, CJS, MTS and CTS, including declaration files.
- Static imports and re-exports, import-equals declarations, literal dynamic
  imports, unshadowed literal `require`, TypeScript import types and JSDoc
  import types supported by the parser.
- TypeScript's files/include/exclude rules, in-root relative JSONC config
  inheritance, `baseUrl` and `paths`. Configs are parsed as data; plugins are
  never loaded. TypeScript's matcher operates on a bounded filesystem host.
- Relative resolution and supported aliases through TypeScript's resolver.
  Workspace package names resolve plain string exports, exact string subpath
  exports, or a supported main/types/index entry. Resolved target files must
  remain in a selected workspace inside the project.
- Declared external package specifiers and Node builtins. An external edge
  means the source names a declared dependency. It does not establish that a
  package or version is installed, or which implementation will run.

Conditional or pattern workspace exports, package `#imports`, browser/module
entry remapping, project references, `rootDirs`, `moduleSuffixes`, custom
conditions, plugins, package-based config inheritance, pnpm-only workspace
configuration and unsupported patterns produce explicit gaps. Nonliteral
loading, unresolved imports, parser errors and source symlinks also produce
gaps. Any binding or assignment named `require` in a file conservatively makes
its `require(...)` calls unverified; the reader does not guess whether those
calls invoke the Node loader. This can report a gap for an unshadowed call in
another scope of the same file.

The pack has its own versioned source exclusion policy. It skips installed,
vendored and generated directories such as `node_modules`, `.venv`, `venv`,
`vendor`, `build`, `dist`, `.next`, `.nuxt`, VCS metadata and common caches.
The exact set is in `src/project.js`; it is not dynamically imported from the
Python scanner. Config and resolver reads stay inside the root and do not
follow source symlinks. Individual input files are limited to 2 MiB and the
walk to 30,000 entries. Malformed UTF-8 is rejected.

## Evidence and failure

Edges have `key = "edge:1:" + SHA256(UTF8(JSON.stringify(tuple)))`, where the
six-element tuple is `[ecosystem, source, target, kind, phase, resolution]`.
For another language, use compact JSON with Unicode preserved and no spaces.
`ecosystem` is `npm`. File identities use `file:<project-relative POSIX path>`;
external and builtin identities use `package:<specifier>` and `builtin:<name>`.

Kinds are `import`, `reexport`, `require`, and `dynamic-import`. Phases are
`runtime` or `type`: these describe the syntax, not proven emitted or executed
code. Mixed import/export declarations contribute one edge in each applicable
phase. Resolution is `local`, `workspace`, `external`, `builtin`, `unresolved`
or `dynamic`. Value counts occurrences of the same six-tuple, not imported
symbols. Keys do not contain line positions. Rewiring one edge changes its
identity even if every count stays constant.

Stdout is the existing observations/summary JSON protocol, with an additional
`coverage` object. Any gap causes exit 2 and a bounded stderr diagnostic naming the first gap codes and scopes. The
standalone JSON includes partial observations and structured gap codes/scopes;
the existing Sentinel runner records a failed probe and does not compare those
partial observations as a complete measurement. A missing or unsupported
input therefore cannot become a clean zero. No source snippets or URL import
authentication/query strings are emitted.

Run `npm test` from this package after its declared dependency is installed.
Tests use disposable fixtures and never run their application code.
