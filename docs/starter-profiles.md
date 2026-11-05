# Node and frontend starter profiles

These configurations use existing probes to measure source files, source
assets, and the location of a declared syntax pattern. They are a starting
point for a Node service or frontend, with explicit coverage rather than
Python-probe parity. The profiles ship in the repository's documentation;
installing the Python wheel does not copy them into your project.

| Profile | File measurements | Pattern census |
| --- | --- | --- |
| [Node service](profiles/node-service.toml) | Lines per JS, MJS, CJS, JSX, TS, MTS, CTS, and TSX file | Direct `fetch(...)` calls using ast-grep's TypeScript and JavaScript languages |
| [Frontend](profiles/frontend.toml) | The same source lines, plus CSS/SCSS lines and raw source image/font bytes | The same calls, plus a separate TSX census |

Both profiles disable the built-in probe set. They do not run Python comment,
module/import, exception-handler, or installed-distribution probes against a
JavaScript project. They do not inventory npm dependencies, resolve imports,
read TypeScript aliases or workspaces, or infer React/Next.js semantics.
Gate policies are separate: the profiles declare none, and an undeclared
policy does not mean its gate passed. See [gates](gates.md) before choosing
policies; Python distribution readers are not npm inventory readers.
The existing organization lens reads the changes. It does not establish
performance, security, or accessibility from these measurements.

## Requirements and first use

The runner requires Python 3.13 with Vibe Sentinel installed. These custom
probe commands invoke `python` exactly as written, so that executable on
`PATH` must import the installed package, even if the CLI was installed in a
separate tool environment. Confirm the active environment before scanning:

```bash
python --version
python -c "import vibe_sentinel; print(vibe_sentinel.__version__)"
ast-grep --version
```

The pattern probes require the real `ast-grep` binary on `PATH` (provided by
`ast-grep-cli` in this repository's development environment). The profiles
install nothing. Node.js is not required for these measurements: no Node
program, package script, compiler, or application code is executed. Running
or building the application itself retains its own Node/runtime requirements.

For a project without a configuration, copy the selected profile to
`.vibe-sentinel.toml` in that project's root. If a configuration already
exists, merge the profile's probe settings without overwriting that file or
losing the project's model settings, lenses, or policies. For an existing configuration,
inspect the resulting effective probe set: other declared probes still
merge with these declarations. All roots default to `.` and are relative to
the project being scanned, not the profile file's directory.

From the target project, inspect the copied configuration without a model
call or a history write:

```bash
vibe-sentinel scan --config .vibe-sentinel.toml --list-probes
```

An explicit profile path also works, for example from a repository clone:

```bash
vibe-sentinel scan /path/to/node-service --config docs/profiles/node-service.toml --list-probes
```

That command inspects the probe set only. `--config` does not relocate every
declaration loader: lenses and gate policies are read from the target project
root. Keep the merged configuration there for normal scans, and update any
existing lens watches that refer to probes this profile removes.

Normal scans require a reachable configured model. Preserve or set the target
project's `[llm]` settings, or use the documented endpoint/model environment
overrides in [local model setup](use-of-local-model.md). Check the backend
without starting it, then record a scan:

```bash
vibe-sentinel backend status --config .vibe-sentinel.toml
vibe-sentinel scan --config .vibe-sentinel.toml
```

The first successful scan establishes a baseline in the target project's
history. Subsequent scans compare against it. The profile tolerance is zero
so every measured line, byte, or pattern-count change can be reported; choose
project-specific tolerances after observing the noise in your own history.

## Adjust coverage deliberately

The source globs match at any depth below the selected root, including tests.
The frontend's asset probe matches PNG, JPG/JPEG, GIF, WebP, AVIF, SVG, ICO,
WOFF and WOFF2 files. It measures existing source assets; it does not build,
compress, total a production bundle, or establish what a browser downloads.

Change roots through the profile's existing placeholders, for example:

```toml
[probes.parameters]
source-lines = { SOURCE_ROOT = "src" }
fetch-typescript = { SOURCE_ROOT = "src" }
fetch-javascript = { SOURCE_ROOT = "src" }
# Frontend profile only:
fetch-tsx = { SOURCE_ROOT = "src" }
source-asset-bytes = { ASSET_ROOT = "public" }
```

The shared source exclusions remove `node_modules`, virtual environments,
vendor directories, caches, and build directories such as `dist` and `.next`
below each root. Do not point these source profiles at a build output root:
the explicitly selected root itself is not excluded. Keep the chosen roots
stable between scans because they participate in observation paths.

A missing root fails its probe. File measurement also fails when none of its
globs match; a frontend without the listed source assets should remove the
asset probe or explicitly disable `source-asset-bytes` in `[probes]`. This is
a declared scope choice, not a zero-byte asset inventory. An unreadable file
is reported through `not-measured` where a partial measurement is possible.

The pattern census records direct syntax matching `fetch($$$ARGS)`, by
directory and in total. A local function named `fetch` also matches; aliases,
`client.fetch(...)`, and `globalThis.fetch(...)` do not. The TypeScript,
JavaScript, and TSX parsers select their own supported file extensions; source
length support for an extension does not imply pattern support for it. The
fixtures exercise `.ts`, `.js`, and, in the frontend profile, `.tsx`. Zero
matches can mean no matching files or no occurrences, so read the source
inventory alongside the census. Missing ast-grep is a failed probe, not zero
matches. Replace the literal pattern with a construct whose location matters
to your project; no semantic import or network resolution is performed.

For more ambitious readers, see the [JavaScript/TypeScript coverage notes](frontend-support.md).
