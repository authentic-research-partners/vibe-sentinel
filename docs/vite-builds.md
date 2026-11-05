# Vite build artifact evidence

The optional `vite-build` probe reads an existing Vite build manifest and
measures logical entries, shared artifact ownership and raw file weights. It
uses the Python standard library. Node, Vite and the JavaScript pack are not
required to inspect an already produced artifact. The reader never builds,
imports application code, evaluates Vite configuration or contacts a service.

Copy the declaration in [probes/vite.toml](probes/vite.toml) into a trusted rule
file and add that file to `[probes] rule_files` in your existing configuration.
This probe is not part of the default scan. The example uses `dist` and
`dist/.vite/manifest.json`; change both parameters for a different output layout:

```toml
[probes.parameters.vite-build]
ROOT = "web"
MANIFEST = "dist/.vite/manifest.json"
OUT_DIR = "dist"
```

The selected manifest must be inside the selected output directory, and both
must be inside `ROOT`. All paths after `ROOT` are explicit relative paths.
The supplied declaration uses fixed argv, isolated Python (`-I`) and
`compare_parameters = true`: changing the scope requires baseline acceptance
before comparable measurements resume. It does not alter existing pack
manifests, source readers or pack version attribution.

```sh
vibe-sentinel scan --probes=vite-build
python -I -m vibe_sentinel.probes.vite --root web --manifest dist/.vite/manifest.json --out-dir dist
```

The standalone command inspects evidence without asking a judge. A scan uses
the existing drift and configured lens pipeline. Select a project lens if the
project has a particular question about build growth or shared ownership.

## Identity and attribution

Vite's documented manifest uses source-relative keys for entry and dynamic
entry chunks. Generated non-entry chunk keys contain generated output names.
The reader therefore preserves entry keys and never removes apparent hashes
from filenames to invent stable chunk identities. See Vite's
[manifest format](https://vite.dev/guide/backend-integration) and
[manifest configuration](https://vite.dev/config/build-options#build-manifest).

`isEntry` and `isDynamicEntry` identify logical roots. For each root, the reader
follows `imports` statically and `dynamicImports` dynamically. Once a path has
crossed a dynamic import, its descendants are dynamic. If a file is also
statically reachable from that root, static ownership wins. The dynamic weight
is therefore additional to the static weight, not a duplicate of it. A dynamic
entry is also measured as its own root, whose own output is static in that
root's closure. Cycles terminate through visited node/phase pairs.

Every unique output path is grouped by:

- Its sorted manifest slot roles: `output` for `file`, `css` for `css` members,
  and `asset` for `assets` members. These are declaration roles, not inferred
  MIME types or filename extensions.
- Its sorted logical entry owners, each with static or dynamic reachability.

These groups, called cohorts in the output, include entry output files as well
as anonymous chunks and attached resources. Entries remain individually
identified, while cohorts preserve comparable aggregate bytes and file counts
when generated filenames change. A file appearing under several roles carries
the union of those roles and contributes its weight once. Unreachable declared
files form cohorts with no owners; they remain in the build total.

Direct imports between individually identified entries also retain their own
relationships, with kind `import` and static/dynamic phases. A direct entry
rewire remains visible even when transitive reachability is unchanged.

Entry-to-cohort relationships use the shared `edge_schema = "1"` protocol,
with ecosystem `vite`, kind `artifact-reachability`, phase `static` or `dynamic`
and resolution `manifest`. Numeric measurements use `vite:1:` keys hashing the
kind, logical identity and metric. Entry flags are compared as state. Scope
records the projection version and selected paths. Hashed output filenames do
not appear in comparable identities.

Anonymous internal chunk edges are intentionally not inventoried individually.
An internal rewire that preserves every entry's reachable file groups and
weights is invisible to this projection. Cohorts are aggregate identities,
not a promise that two builds emitted the same anonymous chunks.

Weights are regular-file byte sizes from local metadata, with no content read.
A file is counted once by output path in the global total and contributes its
full weight to every owning entry. Entry totals cannot be added to obtain the
global total because shared files overlap. Distinct paths, including hard
links, represent distinct served artifacts and are counted separately. Neither
gzip/Brotli size, transfer bytes, cache behavior, request counts nor actual
runtime performance is measured.

## Scope, freshness and failure

The manifest projection accepts the documented `file`, `src`, `name`, `css`,
`assets`, `imports`, `dynamicImports`, `isEntry` and `isDynamicEntry` fields.
`src` and `name` are validated but not exported or used as alternate identity
sources. Unknown fields fail coverage rather than silently dropping potentially
new structural relationships. Duplicate JSON keys and unsupported shapes fail.

Only files named by `file`, `css` and `assets` are measured. The reader does not
walk the output directory, so copied public files, separate source maps,
compression sidecars and other unlisted outputs are outside this scope. Inlined
resources contribute to their containing file's raw bytes without being
inventoried separately. This is not an SSR manifest reader.

Artifact freshness is always `not-assessed`. A readable manifest and existing
files do not prove that they correspond to the current source, source revision,
configuration or toolchain. This reader makes a measurement of those artifacts,
not of a current application build. File modification times do not establish
freshness and are not substituted for build provenance. Reusing old artifacts
can therefore yield unchanged artifact evidence; the scope remains explicit.

Missing files, dangling chunk references, invalid paths, symlinks in selected
paths, nonregular files, unreadable files and bounds reached produce incomplete
coverage and exit 2. An empty manifest or one without logical entries is also
incomplete. Paths may not traverse outside the root/output directory. The
filesystem must remain stable during inspection; this reader is not a sandbox
against a concurrent process replacing directory entries.

The limits are 8 MiB of manifest JSON, 10,000 chunks, 20,000 unique output paths,
50,000 declared links, 200,000 graph/ownership work units, 8 MiB of accumulated
owner identity text, 30,000 observations and 16 MiB of output. Structural strings
are limited to 2,048 characters. Size and graph limits are resource bounds, not
judgement thresholds. A bounded diagnostic carries gap codes without copying
arbitrary metadata values or file contents.

The standalone JSON can carry partial observations on failure. The scanner's
existing subprocess boundary rejects the nonzero result as unmeasured, so those
partials cannot become false removals or a zero-byte clean measurement. Source
identities and artifact scope paths can still be sensitive. They remain local
evidence unless the user selects a scan/model/export path that includes them;
no arbitrary artifact contents or metadata descriptions are sent to a model by
this reader.
