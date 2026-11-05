# Versioned scan results

`vibe-sentinel scan --format=json` returns the existing `snapshot`, `drift` and
`gates` objects plus a versioned `result` envelope. Consumers should check
`result.schema_version`, `result.outcome` and `result.exit_code` before treating
measurements as complete. Version 3 uses exit 0 for a complete scan without
findings, 1 for findings, and 2 for incomplete or failed work.

All supported versions require their declared fields. New scans emit version 3;
versions 1 and 2 remain readable without newly invented evidence. Version 1
preserves its original lack of pack attribution. Missing fields and unknown or unsupported
versions are rejected rather than interpreted as empty results. Incompatible
contract shapes require a new result version; the database schema version is
a separate storage concern.

The envelope records the run ID, the exact baseline run ID used for comparison,
probe, gate and judge statuses, counts and provenance. An initial scan says
`comparison: "no_baseline"`; it has not compared the tree with a prior scan.
A selected probe absent from the baseline, or failed in that baseline, makes
its comparison incomplete. A successful current measurement can be deliberately
accepted with `--update`; the accepting run still reports its incomplete old
comparison, and subsequent scans compare against the newly accepted coverage.
Unconfigured gates are explicitly unconfigured. Requested judge answers that
fail are incomplete or failed, while a question that was not needed is not
asked. A missing horizon is reported separately from a completed baseline
comparison. `failed_stages` distinguishes configuration, measurement, judgement,
gate and storage failures, including failures in more than one stage.

A failed measurement or judgement records the measurements that actually ran
but cannot establish or replace a baseline, including under `--update`. Gate
findings remain a separate statement about current state; accepting measured
drift never clears them. Failed configuration attempts have no run ID. A
failure while finalizing the envelope is reported as a storage failure; the
measurements may already have been recorded.

## Export a limited report

```bash
vibe-sentinel scan --format=json --shareable > scan-result.json
```

This opt-in profile emits only the versioned envelope with statuses, counts and
hashes. It excludes observation keys and values, source paths, free-form notes,
credential excerpts, package inventory, configured command arguments and raw
diagnostics. Hashes identify equality; they are not proof that a low-entropy
input cannot be guessed. This command runs and records a scan, just like the
local JSON form. It does not upload anything. Diagnostics still go to the local
logging/stderr channels; export stdout alone.

The ordinary JSON profile is a local report and can contain source paths,
probe observations and diagnostics. Do not treat it as a shareable artifact.
The [Codex MCP interface](codex-plugin.md#coverage-and-privacy) uses the same
versioned contract and centralized projection with a different purpose: it
retains useful drift and gate explanations while omitting complete dependency
inventories, dependency-version changes and credential details. Those retained
paths and explanations can enter hosted assistant context.

## What provenance proves

Provenance is captured before measurements. It records effective configuration,
rule and selected scope hashes, including resolved horizon and trend settings,
tool version, scanner identity, configured judge identity and the successfully
measured [dependency environment](environments.md), when present. The scanner
and target interpreter are separate identities. Source provenance identifies
the Git checkout/revision where available; outside Git, it identifies the
canonical project location. It does not claim a content hash of every source
file. Configuration and rule fingerprints are not source-tree fingerprints.
Fingerprint scope excludes authentication and opaque provider `extra_body`
fields. Selected decoder settings are included, but not every configuration
edit changes the fingerprint. The recorded `fingerprint_scope` names this
boundary.

The run is explicitly `not_atomic`: files can change while commands run. A
configured judge name is not a verified model-weight digest. Tool build
identity and build freshness remain explicitly unverified or unassessed where
the tool has no evidence. Versions 2 and 3 record [explicit local packs](pack-contract.md),
including declared but unassessed inventory readers and baseline-only history
coverage. Version 1 retains its original `packs: "not_implemented"` value. A hash
does not fill those gaps.

The finalized envelope is stored with its run in history schema version 9,
using the existing backed-up migration process. Result versions 2 and 3 fit the same
JSON storage columns and need no additional database migration. `db.store.load_result` returns
that captured envelope, or `None` for older or unfinished records. It does not
reconstruct historical provenance from today's configuration. The historical export command below reads this finalized envelope directly.

Direct Python callers of `engine.scan_and_compare` retain its three-item return
value. Judge failures now return recorded partial work with failure information
in `snapshot.context` instead of raising a connection exception. Callers should
use `results.build_result` and inspect its status; a returned tuple alone does
not establish successful judgement. CLI scan finalizes the result after gates.
Python integrations that need historical evidence must likewise call
`db.store.save_result` with the built result after collecting gates.


## Captured questions and answers

Version 3 records the declared lens question, its source and watch list, each
validated rating resolved to a measured `(probe_id, key)`, and the lens that
supplied the final severity and note. An earlier answer remains recorded when
a later lens supplies a stronger rating. `applied` says whether a rating was
applied when processed; the change's `lens_id` names the final winner. Unknown
or ambiguous references contribute only to `dropped_ratings`, never to new
findings or exported model text.

First scans and unchanged scans record configured lenses as `not_asked`.
Failed questions remain `failed`, including when another lens answered in the
same fan-out. Source identity, baseline identity and measurement values sit
beside these decisions. The evidence preserves the question declaration and
validated answer, not a complete request transcript. Shared prompt context,
credential excerpts, configured argv, raw responses and diagnostics are absent.
Horizon and trend statuses remain separate from baseline findings; their
recomputed detailed readings are not stored by this contract.

Gate evidence records its origin: `mechanical`, `model`, `declared_verdict`,
`pin`, or `unknown` when an older carrier did not capture that distinction.
Only a model answer has `status: "answered"`. Pins and declared literal verdicts
say `not_asked`; failed decisions carry no answer. A failed gate can retain
partial answers and earlier policy decisions, with `failing: null` where no
final gate finding was produced. The gate's failure status still controls the
result. An unconfigured gate remains explicitly unconfigured.

## Export recorded evidence

```bash
# Aggregate artifact for CI or sharing; no new scan or upload.
vibe-sentinel export /path/to/project --run 42 > result.json

# Detailed, sensitive artifact for local inspection or an offline viewer.
vibe-sentinel export /path/to/project --run 42 --profile local-detail > evidence.json
```

Both forms emit only the versioned `result` envelope. Export returns exit 0
when it writes the selected recorded result, even when that result records a
failed scan. Consumers must inspect `result.outcome` and `result.exit_code`.
Missing runs and unfinished results fail export with exit 2. Versions 1 and 2
can be exported as aggregates; they cannot supply local-detail evidence that
was never captured. Changing the current configuration or deleting its rule
files does not change an export.

Export opens SQLite read-only, does not initialize an absent database, and
skips configuration loading, logging-file setup and automatic maintenance.
SQLite may use its normal WAL read-coordination sidecars; export does not
modify recorded rows, baseline markers or schema. It does not accept drift,
apply policy pins, execute probes, ask a judge, or upload an artifact.

`local-detail` uses a field allowlist. It includes relevant change values,
question and answer text, finding identities, credential match counts and
flagged licence identifiers. Dependency versions are omitted from gate evidence.
Changes retain their labels and an explicit set of structural identities:
`identity`, `pointer`, `file`, `edge_schema`, `ecosystem`, `source`, `target`,
`kind`, `phase`, `resolution`, `metric` and `unit`. Removal evidence comes from
the baseline observation. Near-miss answers retain the model-selected
`suspect`, including partial answers from a failed gate. It omits the dependency-version change inventory and
reports the number withheld. It does not include arbitrary observation or gate
attributes, credential excerpts, command arguments or diagnostics. It retains
source and environment identity, including local paths.

This is a sensitive local profile, not a promise to scrub secrets from
user-authored questions, custom observation keys/states, identifiers or model
answer text. Those fields are intentionally retained for traceability. Keep
it local unless you have reviewed its contents. The shareable profile remains
aggregate-only, and MCP withholds all new evidence detail; neither inherits
the new question/answer records. Filtering cannot turn an arbitrary custom
probe's data into a generally safe public artifact.

Evidence capture is bounded to 256 lenses, 40 resolved ratings per lens, 10,000
changes and 10,000 gate entries, with an 8 MiB encoded evidence ceiling.
Configured questions may contain up to 32,768 characters; identity, label and
other retained local text fields allow 8,192. Model assessments and rating notes
retain their existing 700- and 240-character bounds. These generous storage
limits are not judgement thresholds. An over-limit capture never shortens an
identity or question: it records `availability: "unavailable"`,
`failure: "limit_exceeded"` and an incomplete result with exit 2. Drift capture
is checked before a scan can establish or replace a baseline. Aggregate export
retains the failure code; local-detail export refuses unavailable evidence.
