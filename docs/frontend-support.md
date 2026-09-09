# Frontend support — design exploration

**Status: mostly exploration.** Two pieces of this have shipped and are marked
below; everything else records what TypeScript/JavaScript and React support
*could* look like, and why each piece would earn its place under the rules the
rest of the tool already follows. Read [drift.md](drift.md) and
[gates.md](gates.md) first; the vocabulary here is theirs.

**Shipped so far:**

- **Dangers for the JavaScript toolchain** — publishing, deploying, forcing a
  resolution, exposing a dev server, with lock file and dependency tree as
  context signals. All conditional on a `package.json`, so a Python repository
  never sees them. See [gates.md](gates.md#when-a-danger-applies-to-some-trees-and-not-others).
- **Asset weight** — `file-length` measures files that are not text under
  `UNIT = "bytes"`, so an image glob tracks what a site ships. See
  [drift.md](drift.md#measuring-what-a-project-ships).

Deliberately not shipped with them: a danger on `npx`. The question worth
asking is whether the package it runs is one any manifest declares, which
needs the npm side of the manifest reader below — a pattern alone fires on
`npx tsc`, which is ordinary work.

## Table of Contents

- [What already works](#what-already-works)
- [Where the Python coupling actually lives](#where-the-python-coupling-actually-lives)
- [The three worth building first](#the-three-worth-building-first)
- [Parity: the npm ecosystem](#parity-the-npm-ecosystem)
- [Net-new: probes that only exist in frontend](#net-new-probes-that-only-exist-in-frontend)
- [Two structural bets](#two-structural-bets)
- [Traps](#traps)
- [Not covered here](#not-covered-here)

## What already works

More than it looks like. The core carries no language assumption:

- **A probe is a command that prints one JSON object.** It is not a plugin and
  not a Python entry point, so a probe written in TypeScript, Go or awk is a
  first-class probe. `probes.default.toml` rewrites `argv[0]` only for the
  probes shipped in this package, because those are modules of it; a
  `[[probe]]` declared by a project runs exactly as written.
- **`pattern-census` already accepts `typescript`, `tsx` and `javascript`** —
  its `LANGUAGE` placeholder is passed to `ast-grep`, which supports them. A
  TSX pattern census needs a config change, not a code change.
- **The credentials gate is mostly language-neutral.** It walks files, not
  ASTs, and already knows `.npmrc` and `npm_` tokens.
- **The licence text fingerprinting is entirely reusable.** `identify_text`,
  the marker table, the category machinery and the SPDX `AND`/`OR` evaluator
  in `licenses.py` read strings. Only the *source* of those strings is
  Python-specific.
- **`file-length` works today** with `CATEGORIES` and `FILE_GLOB` pointed at
  `*.ts,*.tsx`.
- **The journal and the safety gate are already ecosystem-agnostic** — they
  judge shell commands, and a `[[danger]]` is a regex plus a question in your
  own words.

## Where the Python coupling actually lives

Five places, and only five:

| Module | The assumption |
|---|---|
| `packages.py` | `importlib.metadata` for the installed set, `pyproject.toml` for the declared set |
| `probes/dependencies.py` | `importlib.metadata` for versions and origins |
| `requirements.py` | `pyproject.toml` shapes, and pip/uv/poetry argv parsing |
| `licenses.py` | Trove classifiers and `importlib.metadata` — but *only* as the input source |
| `probes/{comments,modules,handlers}.py` | Python's `ast` and `tokenize` |

Everything else — `engine.py`, `inventory.py`, `compare()`, `trends.py`,
`horizons.py`, the database, the pin machinery, the report — is indifferent to
what language the observations came from.

## The three worth building first

### 1. Bundle weight, as a probe

```
key       bundle:<chunk>
value     bytes
tolerance "15%"
```

The single highest-signal number in frontend work, and it is exactly the shape
this protocol wants: a magnitude with stable per-chunk keys and a relative
tolerance. An agent swaps a date helper for `moment`, or imports all of
`lodash` to use one function, and the main chunk gains three hundred kilobytes.
Nothing in `git diff` says so.

It is the same argument that justifies `dependency-versions`, one level
further out: **a declared constraint is in the manifest, a resolved version is
in the lockfile, and the bytes you actually ship are in neither.**

Sibling keys worth emitting from the same run: chunk count, largest chunk,
application bytes vs. vendor bytes. The last one is the interesting ratio —
vendor bytes climbing while application bytes hold steady is a dependency
decision nobody made deliberately.

A probe rather than a gate, by the test at the bottom of `probes.default.toml`:
a 240 KB main chunk is not news on the two-hundredth scan. A main chunk going
240 KB → 610 KB overnight is the whole finding.

### 2. The escape-hatch census

Count per directory: `any`, `as any`, `as unknown as`, non-null `!`,
`@ts-ignore`, `@ts-expect-error`, `// eslint-disable-next-line`.

This is the `silent-exceptions` argument, restated in another language.
Whether a given `any` is justified is a question about that line, and `tsc`
and eslint answer it — which is the restriction that makes a per-line rule
tolerable at all. **How many there are, and which layer they landed in, is a
fact about the codebase**, and it is the fact that moves while an agent is
making a type error stop. The fastest way to silence `tsc` is to widen a type.

A `// eslint-disable-next-line` does not move the number, because the number
is not a verdict.

Pair it with a **`tsconfig` strictness state**: `strict`, `noImplicitAny`,
`strictNullChecks`, `skipLibCheck`. An agent flipping `strict: false` is one
line in a config file nobody reads closely, and it silently reclassifies every
`any` in the tree.

### 3. Client/server boundary drift

Count `"use client"` per directory. Count `"use server"` separately.

An agent hits a hook error inside a server component, adds `"use client"` at
the top of the file, and the error stops. It is the correct local fix. Repeat
it forty times over three months and the boundary has crept to the root of the
tree, every component ships to the browser, and no single commit in that
history looks wrong.

This is the cleanest instance of invisible structural drift the frontend
offers: the fix is always locally right, the cumulative effect is a different
application, and nothing measures the cumulative effect.

`"use server"` moves the same way for a different reason — the drift there is
a security boundary rather than a performance one.

## Parity: the npm ecosystem

### Environment and installed set

The analogue of `_env_kind` in `packages.py`: detect npm / yarn / pnpm / bun,
find the workspace root, record the Node version.

Read `node_modules/*/package.json` **directly** rather than shelling out to
`npm ls --json`. Reading the tree is offline; it keeps the property the README
claims for the Python path — that this tool tells nobody what is installed on
your machine.

### Declared set

`package.json` `dependencies` / `devDependencies` / `peerDependencies` /
`optionalDependencies`, plus `workspaces`. The two fields with no pip analogue
are the ones worth the most:

- **`overrides` / `resolutions`.** An agent silences a peer-dependency
  conflict by pinning a transitive package. One line, in a field nobody
  reviews, that changes what the whole tree resolves to.

### Install-command parsing

The `requirements.py` equivalent: `npm i`, `pnpm add`, `yarn add`, `bun add`,
with their flag shapes. And the one Python has no counterpart for:

> **`npx` and `bunx` execute a registry package that no manifest declares and
> no lockfile records.**

That is the hallucinated-package surface at its sharpest — a name the model
produced, fetched and run in a single step, leaving nothing in the tree. The
journal already sees the command.

### Packages gate

The port is the obvious half. The half worth more:

- **`postinstall` / `preinstall` scripts** in the dependency tree. A state, so
  a gate, and pinnable. This is the actual npm supply-chain vector, and it has
  no Python equivalent of comparable reach.
- **Lockfile vs. `node_modules` divergence.** The installed tree stops
  matching the lock routinely, and nothing in the repository records that it
  has. Directly on the thesis this tool exists for.
- **Deep imports into transitive packages** — `import x from 'foo/lib/internal'`
  works by hoisting accident and breaks under pnpm's stricter layout.
- **Provenance is *easier* here than in Python.** `package-lock.json` records
  `resolved` and `integrity` per package, so "this dependency stopped coming
  from the registry" is a direct read rather than an inference.

### Licences gate

Read `license` / `licenses` from each installed `package.json`; fall back to
fingerprinting the LICENSE file with the existing `identify_text`. The marker
table, the category machinery and the SPDX expression evaluator port unchanged.

This is where the gate earns the most, for a boring reason: npm trees run one
to two orders of magnitude larger than Python ones, so nobody has ever read
theirs.

### Credentials gate

Mostly works already. It needs one new rule class, and it is the most valuable
single frontend idea after the three above:

> **A secret in a Python file is a key at rest. A secret behind `VITE_`,
> `NEXT_PUBLIC_`, `REACT_APP_`, `PUBLIC_`, `GATSBY_` or `EXPO_PUBLIC_` is a key
> published to every visitor.**

The prefix *is* the finding. It is not a heuristic about entropy — the
developer explicitly opted that value into the client bundle, and the build
tool will inline it into JavaScript that anyone can read. A rule keyed on the
prefix plus a live-looking shape has an answer that does not depend on
anyone's taste, which is the bar `gates.md` sets.

The second half is scanning **built output** — `dist/`, `.next/`, `build/`,
`out/`. That means inverting `EXCLUDED_DIRS` for this one gate. Those
directories are excluded everywhere else for good reasons that all still hold;
here the artifact is the entire point, because the artifact is what ships.

### Safety dangers for the JS toolchain

Frontend agents have irreversible, outward-facing commands within reach in a
way Python agents usually do not. This is where "stop it before it runs" pays
for itself:

- `npm publish`, `vercel --prod`, `netlify deploy --prod`, `firebase deploy`,
  `wrangler publish`, `gh-pages -d dist` — one command, publicly visible,
  not undoable
- `npm i --force`, `npm i --legacy-peer-deps` — silencing a real
  incompatibility rather than resolving it
- deleting a lockfile, or `rm -rf node_modules && npm i` — regenerates a tree
  that may resolve differently, and leaves no diff behind
- a dev server bound to `--host 0.0.0.0` — exposes the dev server and its
  source maps to the network
- writes into `public/` — anything placed there is served to the world

## Net-new: probes that only exist in frontend

| Probe | What moves | Why it is not a lint |
|---|---|---|
| **Import graph / barrel hubs** | fan-in and fan-out per module, from ES import specifiers | A `components/` or `utils/` barrel becomes the thing everything depends on without its own file ever changing |
| **Hook census per layer** | where `useEffect`, `useState`, `useContext` occur, per directory | The location is the finding: data-fetching effects arriving in a directory meant to be presentational |
| **Floating promises** | async calls with no `await` and no `.catch`, per directory | The same fact as `silent-exceptions` — the error goes nowhere — in different syntax |
| **Styling-system census** | inline `style={{}}` vs. utility classes vs. CSS modules vs. styled-components, per directory | Three coexisting systems is the signature of a codebase several models took turns on. Which layer uses which is the drift |
| **Component shape** | props per component, JSX nesting depth, components per directory | A component gaining eight props is the React form of a module becoming a coupling hub |
| **Route inventory** | pages under `app/` or `pages/`, keyed by route segment | A new key means a URL now exists that did not. `app/admin/page.tsx` arriving is worth a line of output |
| **Accessibility census** | `<div onClick>`, images without alt, controls with no accessible name, per directory | Mechanical, and the drift reading is "the new feature directory started shipping inaccessible controls" |
| **Public asset inventory** | files under `public/` | A new key means a file became world-readable on your domain |
| **Test-to-component ratio** | `*.test.tsx` against component files, per directory | Not coverage. A structural ratio that moves |

The four shipped Python probes also need TS/TSX counterparts —
`comments`, `modules`, `handlers`, `length`. `length` needs only configuration;
the other three need a TS parse.

## Two structural bets

### A Node probe SDK

The probe protocol is "a command that prints one JSON object to stdout".
Probes therefore need not be Python, and the interesting consequence is that
**this project does not have to port the frontend ecosystem — it can let the
ecosystem write its own probes.**

A small `@vibe-sentinel/probe` npm package (the observation schema, a
validator, an emit helper) lets a frontend team write probes in the language
they already work in, while the core stays Python and stays small. Everything
in the section above becomes something a team can write in an afternoon
instead of something this repository has to own forever.

### Monorepo and workspace awareness

Frontend repositories are overwhelmingly workspaces — pnpm workspaces, npm
workspaces, Turborepo, Nx. Probes keyed per workspace package rather than per
directory is table stakes.

The bet is what that unlocks: **cross-workspace import violations.**
`apps/web` reaching into `apps/admin/src/...` instead of going through a shared
package is pure structure, invisible in any single commit, and exactly the
subject this tool claims. There is no Python equivalent because Python
projects are rarely shaped this way.

## Traps

- **`scope = "all"` is wrong for npm.** A typical `node_modules` holds
  hundreds to low thousands of packages, each costing two rows per scan for
  the life of the history. `declared` should probably be the npm default,
  inverting the choice made for Python.
- **The commentary-ratio probe does not port naively.** TSX has a
  structurally different natural comment ratio than TS, because JSX is markup.
  Measured together the number means nothing; they need separate categories.
- **The "installed version lives in neither manifest nor lockfile" argument is
  weaker for npm** — lockfiles are committed near-universally there, so the
  resolved version *is* in the repository. It is stronger in a different way:
  `node_modules` diverges from the lock constantly, and nothing records that
  either. The argument needs re-deriving for npm rather than reusing.
- **A build-dependent probe has a cost the others do not.** Bundle weight
  requires running the build. That is minutes, not the seconds the AST probes
  take, and it means the probe can fail for reasons that have nothing to do
  with drift. It likely wants its own timeout and its own opt-in.

## Not covered here

A React viewer over `history.db` — a drift timeline per key, the trends
`trends.py` and `horizons.py` already compute, the journal as a filterable
log, pin drafting. It is a reasonable thing to want and it is a different axis
from everything above: this file is about *measuring* frontend codebases, that
would be a frontend *for the tool*.

If it is built, it has to be a local static build reading an exported JSON
file. Anything that phones home contradicts the property the README claims in
the same paragraph where it declines to be a vulnerability scanner.
