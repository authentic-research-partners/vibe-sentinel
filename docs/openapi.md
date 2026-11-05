# OpenAPI artifact structure

The optional `openapi-artifact` probe reads an existing OpenAPI 3.0 or 3.1 JSON
file. It records paths, methods, schema structure and declared security
attachments. It does not generate the artifact, import the application, contact
a server or decide whether an API is correctly implemented.

Add [the example declaration](probes/openapi.toml) to `[probes] rule_files`, or
copy its `[[probe]]` declaration into your config. Set `ROOT` to the project
root and `ARTIFACT` to the JSON file relative to that root. `SCANNER_PYTHON`
names the Python interpreter with Sentinel installed; no Node runtime or
additional Python dependency is required. This probe is not enabled by default.

```sh
python -I -m vibe_sentinel.probes.openapi --root . --artifact api/openapi.json
```

`-I` keeps the measured directory and `PYTHONPATH` out of scanner startup module
lookup. Use the installed scanner interpreter. The reader reports JSON to
stdout, exits 0 for complete coverage of its documented projection and exits 2
for incomplete coverage. The scan runner treats exit 2 as a failed measurement,
so unsupported input cannot manufacture disappeared edges or a clean result.
Stderr names a bounded sample of gap codes and structural locations; run the
command directly for the full coverage list.

## Recorded projection

- Paths and their `get`, `put`, `post`, `delete`, `options`, `head`, `patch` and
  `trace` operation declarations, identified by document location.
- Component schemas and schemas attached to parameters, headers, request bodies
  and response media types. Unused components are included because this is an
  artifact declaration inventory.
- Schema `type`, property names, required-name lists, `nullable` in 3.0, and
  `readOnly`/`writeOnly` declarations. Required names remain declarations; the
  reader does not infer an effective required set through composition or
  request/response direction.
- Schema containment through `properties`, `items`, `additionalProperties`,
  `not`, `allOf`, `anyOf` and `oneOf`; 3.1 also supports boolean schemas, type
  arrays, `$defs` and `prefixItems`. Composition branches retain their array
  positions. They are not merged into a purported validated schema.
- Parameter name/location/required declarations, request-body required flags,
  response status identities, response headers and media types. Parameter
  declarations retain their locations and array positions; path and operation
  parameter lists are not flattened into an effective runtime parameter set.
- Security scheme names and types, API-key name/location, HTTP scheme, OAuth
  flow kinds and declared scope names, plus root and operation attachments.
  Endpoint URLs, credentials and human-readable scope descriptions are excluded.

The scope observation identifies the relative artifact path, OAS minor version
and reader definition. Freshness and runtime authorization are explicitly
`not-assessed`. A complete projection does not establish that this artifact
matches the current source or deployed service, that its schemas validate, or
that a declaration is enforced at runtime. Empty path maps are valid declaration
evidence, including artifacts filtered by their producer's access policy.

## Reference and security semantics

References are same-document JSON pointers only. Percent encoding and JSON
pointer escapes are decoded, and targets must belong to the expected measured
structural context. References into descriptions, examples or other arbitrary
objects cannot turn those values into observations. Direct schema reference
edges retain cycles without recursively expanding them. Cycles in component
reference chains or Path Item aliases fail coverage because no concrete object
can be established.

In 3.0, Reference Object siblings are ignored. In 3.1, supported Schema Object
siblings alongside `$ref` are measured; other Reference Object siblings remain
ignored, including annotation siblings outside this projection. Path Item
references with local method or parameter siblings fail as ambiguous instead of
assuming a merge. These are distinct object rules in the
[3.0.3 specification](https://spec.openapis.org/oas/v3.0.3.html#reference-object)
and [3.1.0 specification](https://spec.openapis.org/oas/v3.1.0.html#reference-object).

Security attachments retain both declaration mode and effective alternatives:

| Operation declaration | Recorded meaning |
| --- | --- |
| Omitted, root security absent | No declaration; alternatives absent |
| Omitted, root security present | Inherited root alternatives |
| `security: []` | Explicit empty alternatives, overriding root security |
| `security: [{}]` | Explicit alternative with no required schemes |
| `[{A: [], B: []}, {C: []}]` | `(A AND B) OR C`, without flattening |

Scheme and scope ordering is canonicalized, as is alternative order. Duplicate
alternatives remain counted. An explicit attachment equal to inherited security
still differs in declaration mode. Required scope names are recorded, not
verified against an authorization server. Non-OAuth scope arrays must be empty
in 3.0; 3.1 also permits role-name arrays. See the
[security requirement rules](https://spec.openapis.org/oas/v3.1.0.html#security-requirement-object).

## Limits and excluded values

The reader accepts one regular file contained in the project root, rejects
symlink path components and parent traversal, and performs no reference fetch.
Limits are 8 MiB input, 128 JSON nesting levels, 100,000 JSON values, 30,000
observations, 2,048 characters per structural identifier or pointer, and 16 MiB serialized
output. Duplicate JSON keys, invalid UTF-8, nonfinite numbers and exceeded
limits fail coverage. Parser diagnostics never echo source excerpts.

Unsupported relevant structure also fails coverage: external references,
anchors, dynamic references, `$id` rebasing, custom dialects, unknown schema
vocabularies, conditional/dependent/pattern/unevaluated schema applicators,
discriminators, callbacks/webhooks, response links and media encoding objects.
The supported 3.1 dialect is the default OpenAPI dialect
`https://spec.openapis.org/oas/3.1/dialect/base`. This is a bounded projection,
not a full OpenAPI or JSON Schema validator.

Known scalar validation constraints such as bounds, lengths, patterns, formats
and uniqueness are outside the measured projection. Examples, defaults, enum
and const values, descriptions, titles, comments, extensions, server URLs,
authorization/token/discovery URLs and other payload values are never emitted
or semantically traversed. The whole JSON file must still parse within the
resource limits. Identifiers such as routes, property names, schema names and
scope names are intentional local evidence and can themselves be sensitive.
Use the existing explicit shareable result profile for aggregate exports.

## Drift and history

A structural record uses `openapi:1:` plus SHA256 of compact UTF-8 JSON
`[kind, document_pointer]`, with sorted canonical JSON state. Schema and
reference edges reuse the [shared relationship tuple](python-relationships.md#shared-edge-identity)
with `ecosystem="openapi"`, `phase="declaration"` and `resolution="local"`.
A changed reference target produces changed edge identities even when all
counts remain equal. Required-name and security-group changes alter state at
the same structural identity. Object-map ordering does not create drift.
Array positions remain part of schema composition and parameter identities.

The example sets `compare_parameters = true`. Changing the artifact selection
or interpreter/command parameters makes recorded comparison scope incomparable;
horizons and trends apply the same existing scope guard described in
[Python relationships](python-relationships.md). No history schema or result
contract version changes are needed, and existing default probes are unchanged.
