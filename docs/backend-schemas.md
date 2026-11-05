# Schema and migration declaration evidence

Two optional readers inventory existing declarations without importing an
application, running migrations, evaluating `env.py`, contacting a database or
making a network request. Both use only the Python standard library. They
measure structural declarations rather than checking code quality or claiming
what has been deployed.

Copy the relevant declaration from [probes/alembic.toml](probes/alembic.toml)
or [probes/json-schema.toml](probes/json-schema.toml) into a trusted rule file,
then add that file to `[probes] rule_files` in your existing configuration.
Neither reader is enabled in default scans. Adjust parameters for your project:

```toml
[probes.parameters.alembic]
ROOT = "backend"
VERSIONS = "alembic/versions"

[probes.parameters.json-schema]
ROOT = "backend"
ARTIFACT = "contracts/schema.json"
```

The declarations use fixed argv and isolated Python (`-I`). Both set
`compare_parameters = true`, so changing a selected root or artifact requires
explicit baseline acceptance before comparable measurements resume. Existing
OpenAPI and JavaScript readers retain their behavior.

```sh
vibe-sentinel scan --probes=alembic,json-schema
python -I -m vibe_sentinel.probes.alembic --root backend --versions alembic/versions
python -I -m vibe_sentinel.probes.json_schema --root backend --artifact contracts/schema.json
```

Standalone readers emit mechanical evidence. Scans pass measured changes to the
existing configured lens pipeline. A project can declare a lens that asks its
own question about migration branching or data contract growth.

## Alembic revision topology

The reader parses Python source with `ast` and reads top-level literal
`revision`, `down_revision`, `depends_on` and `branch_labels` assignments.
Both `Assign` and `AnnAssign` are accepted. Revision and parent declarations are
required. An omitted dependency or branch-label declaration means no declared
values for that field. Parents, dependencies and labels accept a string, a
literal tuple/list of unique strings, or `None`.

Each revision has a stable revision-ID identity, independent of its source
filename. Its relative source path is evidence in observation attributes.
Branch labels are compared as state. Separate relationship kinds distinguish
lineage parents from dependency prerequisites. This follows Alembic's
[branch and dependency declarations](https://alembic.sqlalchemy.org/en/latest/branches.html).

`down_revision` resolves exact revision IDs. `depends_on` resolves an exact ID
or a unique explicitly declared branch label to the revision that carries that
label. Partial IDs, relative selectors and branch-head expressions are not
resolved. Duplicate revisions, ambiguous labels, unknown targets and cycles in
the combined lineage/dependency graph fail coverage. Merge revisions with
several parents and separate bases are supported.

Position observations identify bases and *lineage heads*, using only
`down_revision` edges. A lineage head is not a claim about Alembic's effective
heads after dependency handling, migration application order or the contents of
an `alembic_version` table.

This is a deliberately bounded module shape. Imports and docstrings are allowed
but never executed. Ordinary undecorated function definitions are accepted;
their bodies are opaque, including `upgrade()` and `downgrade()`. Function
default expressions, decorators, top-level executable statements, conditional
metadata, metadata reassignment (including assignment expressions in annotations),
annotation calls, import bindings over metadata names and
computed assignment values fail coverage. Other top-level assignments must also
have literal values. A module containing a runtime computation is not guessed
into a literal declaration snapshot. Runtime metadata side effects from imports
or function bodies remain unassessed.

The selected versions directory is walked recursively using the core source
exclusion policy. Symlinks are not followed and are reported as gaps. Every
selected `.py` file must meet the declaration contract, except an empty
`__init__.py` namespace marker. An empty selection is incomplete. Additional
version locations must be explicitly selected in separate probe declarations;
Alembic configuration is not discovered or executed.

Source text, SQL, migration docstrings and function bodies never become
observations. This reader does not infer table/column operations, reversibility,
execution correctness or deployed database state.

## JSON Schema data contract inventory

The artifact must be a JSON object explicitly declaring
`"$schema": "https://json-schema.org/draft/2020-12/schema"`. This is a
single-resource structural projection of JSON Schema 2020-12. It is not a
relational schema export, an instance validator or proof that an application
actually enforces the contract.

Schema nodes are identified by escaped JSON pointers. Observations retain
primitive type declarations, property identities, required-property names,
`dependentRequired` names and boolean subschemas. Containment edges cover:

- Schema maps: `properties`, `$defs` and `dependentSchemas`.
- Individual subschemas: `items`, `additionalProperties`, `not`, `contains`,
  `propertyNames`, `if`, `then`, `else`, `unevaluatedProperties`,
  `unevaluatedItems` and `contentSchema`.
- Indexed arrays: `allOf`, `anyOf`, `oneOf` and `prefixItems`.

Array indexes are identities in this projection. Reordering composition
members can therefore record structural changes even when validation semantics
would be equivalent. The reader inventories containment without evaluating
applicator semantics against an instance.

Fragment-only JSON pointer `$ref` values resolve to registered schema nodes in
the same artifact. Root `#`, escaped pointer segments, URI percent encoding,
cycles and sibling schema keywords are supported. The reader records reference
edges instead of recursively expanding them. This follows the
[JSON Schema core reference and containment model](https://json-schema.org/draft/2020-12/json-schema-core).
A reference into ignored payload data is not promoted into a schema location.

A root `$id` may be present as opaque resource metadata and is not exported.
Only local fragment references are supported regardless of that identifier.
Nested `$id`, nested dialect declarations, anchor/dynamic references,
`$vocabulary`, external references, `patternProperties` and unknown keywords
produce explicit coverage gaps. No URI is fetched. The reader does not attempt
to remove hashes, guess dialects or resolve external resources.

Defaults, examples, enum/const values, regular expressions, descriptions and
other annotation values are not exported. Standard scalar validation
constraints such as numeric bounds, string length and formats are outside the
structural projection. Changing only those fields need not produce drift.
Only structural identifiers, relationships and selected shape fields reach
observations. Property names and source paths can themselves be sensitive;
ordinary scan/model/export policies still apply to that evidence.

Freshness, instance validation and deployed database state are explicitly
`not-assessed`. An unchanged artifact does not prove that it matches current
source or a deployed service.

## Bounds and failure behavior

Selected files must stay inside the explicit root and may not traverse
symlink components. Nonregular or unreadable files fail coverage. The readers
expect a stable filesystem during inspection; they are not a sandbox against
concurrent directory replacement. Source exclusions remain unchanged globally.

JSON input is strict UTF-8 and limited to 8 MiB, 100,000 parsed nodes and a depth
of 128. Duplicate keys and nonfinite numbers are refused. Alembic inputs are
limited to 20,000 directory entries, 5,000 Python files, 2 MiB per file, 32 MiB
in total and 100,000 AST nodes per file. Both readers bound structural strings
to 2,048 characters, observations to 30,000 and output to 16 MiB. Limits bound
resource usage; they are not judgement thresholds.

Incomplete coverage exits 2. Standalone JSON may contain partial evidence and
bounded gap codes/scopes. The existing runner rejects failed output as
unmeasured, preserving the difference between missing evidence and apparent
removals. No failed read becomes a clean empty inventory.

Relationships use shared `edge_schema = "1"` identities, with ecosystems
`alembic` and `json-schema`, phase `declaration` and resolution `local`.
Structural observations have version-one identities and explicit projection
versions in their scope records. The evidence is deterministic and portable
across checkout locations; reader interpretation is separate from application
runtime truth.
