# Local ecosystem pack contract

A pack is an explicitly selected local TOML manifest that declares fixed-argv
readers and their requirements. Sentinel loads and validates it without
installing software, importing pack code, searching package registries or
running executable version checks. Drift probes run only when a scan selects
them, through the existing subprocess observation protocol. Pack loading is not
a sandbox: the manifest and its executables must be trusted.

Protocol 1 also defines an ecosystem inventory boundary, instance identity and
pin matching. Inventory readers are validated declarations, not automatically
executed. Existing licence, package and credential gates do not consume their
output. This is a contract for future consumers, not JavaScript resolution,
npm auditing or extraction of the existing Python implementation. Existing
installations and Python pins retain their behavior.

## Select and inspect

```toml
# .vibe-sentinel.toml
[packs]
manifests = ["packs/project-shape.toml"]
```

Relative manifest paths are resolved against the scanned project root,
including when `--config` selects a different config file. Absolute local paths
are accepted; URLs, wildcard discovery and automatic fetching are not.

```bash
vibe-sentinel scan --list-packs
vibe-sentinel scan --list-probes
vibe-sentinel scan --probes=pack:project-shape:source-size
```

`--list-packs` reports identity, protocol, source, resolved executable
requirements, active/disabled drift declarations and inventory declarations
that are not executed or assessed. It does not scan or open history. A missing
manifest or executable, duplicate pack ID, incompatible protocol, unknown field
or invalid declaration is an error with a manual remedy. These failures never
silently reduce the selected coverage.

## Runnable protocol smoke fixture

[protocol-smoke.toml](packs/protocol-smoke.toml) is a complete executable example
using an already-installed `python` on PATH and its standard library. Copy it
into a trusted project's `packs/` directory, add its path to `[packs] manifests`,
then run:

```bash
vibe-sentinel scan --list-packs
vibe-sentinel scan --probes=pack:protocol-smoke:fixture --format=json
```

It emits one explicitly synthetic observation to exercise the boundary. It does
not measure application source, packages or security. Nothing is installed; if
`python` is absent, provide a trusted installed interpreter path in the manifest.

## Manifest protocol 1

```toml
protocol = 1
id = "project-shape"
version = "1.0.0"
ecosystems = ["example"]
supported_inputs = ["source-tree", "example-lockfile-v1"]

[[executable]]
id = "shape-reader"
command = "./bin/shape-reader"

[[probe]]
id = "source-size"
title = "Source size"
command = ["shape-reader", "measure", "--root", "{SOURCE_ROOT}"]
timeout_s = 30
tolerance = 0

[[probe.placeholders]]
name = "SOURCE_ROOT"
description = "Project-relative source directory"
default = "src"

[[reader]]
id = "inventory"
title = "Installed example inventory"
ecosystem = "example"
supported_inputs = ["example-lockfile-v1"]
command = ["shape-reader", "inventory", "--lockfile", "example.lock"]
timeout_s = 30
```

This example requires a compatible executable at `packs/bin/shape-reader`;
it does not install or supply one. Pack IDs and local reader IDs are lowercase
slugs. Pack versions are declared semantic version strings, not verified binary
versions. `supported_inputs` names the formats the author claims to support;
it does not itself prove that a particular input was measured.

Every `command[0]` names an executable alias declared in that manifest. A bare
executable requirement such as `node` resolves through PATH; a relative path
containing a separator resolves against the manifest directory. Absolute paths
are preserved, including interpreter symlinks. Remaining argv values and the
process working directory refer to the scanned project, not the manifest.
No argument is passed to a shell. The usual placeholder validation applies;
unknown fields and nonpositive or nonfinite timeouts are refused.

Drift IDs are namespaced as `pack:<pack-id>:<local-id>`. Project declarations
cannot impersonate that namespace. Existing builtins, project probes and rule
files continue to work. Parameters and disabling use the full namespaced ID:

```toml
[probes.parameters."pack:project-shape:source-size"]
SOURCE_ROOT = "app"

[probes]
disable = ["pack:project-shape:source-size"]
```

These are alternatives: remove `disable` when intending to run the configured
probe. The drift output remains the existing JSON observation protocol
specified in [adding a probe](adding-a-probe.md). No new judgement category or
state gate is inferred from a pack name.

## Inventory protocol 1

`packs.parse_inventory(payload, pack, reader_id)` validates a returned object
against the loaded pack and declared inventory reader. It accepts a bounded JSON-compatible
object and checks the producer identity and supported ecosystem. It does
not launch the reader or turn packages into observations or findings.

An inventory must explicitly identify:

| Field | Meaning |
| --- | --- |
| `protocol`, `pack_id`, `pack_version`, `reader_id` | Exact declared producer |
| `ecosystem` | Package ecosystem, distinct from package name |
| `workspace` | Workspace ID and root |
| `environment` | Environment ID, kind and reader-reported runtime |
| `selection` | ID, basis (`declared`, `locked`, `installed`), groups and extras |
| `coverage` | Complete/incomplete status, measured facets and explicit gaps |
| `packages` | Individual instances with names, versions or requirements, locators and evidence |

The strict typed shapes live in `pack_schemas.py`. Fields are not silently
filled from the scanner environment. A complete declared scope can still omit
facets: `packages`, `dependencies`, `licenses` and `origin` are distinct
coverage claims. An absent facet is unassessed, not clean. Incomplete coverage
requires explicit gap codes and scopes. A valid empty inventory must still
state its producer, environment, selection and coverage. Duplicate instances,
unknown dependency targets and evidence outside declared facets are refused.

`InventorySubject` includes ecosystem, workspace, environment, selection,
instance, name, version/requirement and locator. `subject_key` hashes that full
identity; equal package names across Python and npm, workspaces or installation
instances do not collapse together. `InventoryFindingIdentity` adds gate and
finding kind, and `finding_key` preserves those domains as well.

`InventoryPin` requires `accept = true`, a nonempty reason, verification date,
gate, finding kind and explicit scope.
`exact-instance` matches the complete subject. `ecosystem-package` deliberately
matches a package name across instances within one ecosystem and gate/kind.
`pin_matches` accepts a finding identity, so a licence decision cannot authorize
an unrelated provenance finding. These APIs do not read, migrate or broaden
existing Python policy pins. Future gate integration needs an explicit migration
and UI for these scopes before consuming them.

## Provenance and comparison limits

Result versions 2 and 3 capture every configured pack's identity, version,
protocol, manifest digest, resolved executables,
selected drift IDs and inventory declarations. Inventory-only packs remain
visible as `declared_not_assessed`. Disabling or deselecting a drift probe does
not pretend it ran. Executable provenance identifies resolved paths; file
contents and binary versions remain unverified. The strict shareable profile
hashes pack names, versions, paths and other free-form identifiers while
preserving fixed coverage statuses. New scans emit version 3. [Scan results](results.md)

A selected pack probe needs matching baseline attribution: pack identity,
version, manifest digest, resolved executable identity and filled probe parameters.
A changed input root or command also invalidates the comparison. A changed or missing
attribution makes that comparison incomplete; old values are not rated as
application drift. A successful scan can be deliberately accepted with
`--update`; that run still describes its comparison with the old baseline, and
subsequent scans use the accepted coverage. This does not affect existing
builtin baselines or invent pack attribution in historical version 1 records.

Pack history coverage is currently `baseline_only`. Pack series are excluded
from horizon comparisons and fitted trends, including the standalone trend
command, because those series may span incompatible reader versions. Builtin
history remains available. The result metadata and human-readable output state
this limit; it must not be interpreted as a finding of no pack-related trend.

Python callers can use `templates.load_probe_catalog` to obtain `probes` and
`packs`, then pass selected probes and `declared_packs=catalog.packs` to
`engine.scan_and_compare`. The existing `load_probes` API still returns a list
for mechanical probe callers; result-producing pack callers must supply the
catalog evidence. No manifest is reloaded during provenance capture.
