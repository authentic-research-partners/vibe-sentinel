# Offline CI evidence summaries

`vibe-sentinel summarize` turns an existing versioned result into aggregate
Markdown and returns that result's recorded scan exit code: 0 for complete
work without findings, 1 for findings, and 2 for incomplete or failed work.
Invalid or unreadable input also returns 2, with no summary on stdout.

```sh
vibe-sentinel export /path/to/project --run 42 > result.json
vibe-sentinel summarize result.json > summary.md
```

Export returns 0 when it successfully writes a recorded result, including a
failed scan. Summarize restores the embedded scan exit semantics so CI does not
mistake a successful export for a successful scan.

The command reads only the selected file. It does not scan, load current
configuration, open history, run maintenance, call a model or contact a network
service. It does not upload artifacts or post PR comments. The same Markdown
can be reviewed locally or placed in a configured CI summary.

## What the summary reports

The summary uses the revision, timestamp, run and baseline identities captured
by the scan. It never substitutes the checkout running the summary command.
Dirty state and missing source identity remain explicit. Build freshness is
unassessed; a recorded revision is not a content fingerprint of uncommitted
changes. Tool build and judge weight identities remain unverified.

Measurement counts, current gate findings, gate collection, judge statuses and
longer-horizon statuses are reported separately. A missing baseline comparison
does not become a clean comparison. An absent stage record does not mean that
stage passed. Evidence capture limits remain visible as unavailable detail.

Output is aggregate-only even when input uses the sensitive `local-detail`
profile. The centralized shareable projection removes finding identities,
paths, questions, answers, command arguments and diagnostics. The renderer then
uses only fixed status vocabulary, counts and sanitized revision/timestamp
fields. No input-provided Markdown, HTML, links or workflow-command text is
interpolated into output. This also prevents a malicious probe label or model
answer from creating a GitHub workflow annotation.

## Input boundary

Accepted inputs are either a complete versioned result envelope directly, or
an object containing exactly one `result` field. Versions 1, 2 and 3 retain
their original contract. Unknown versions, missing required fields and
contradictory outcomes, including a complete result with known findings, are
refused. The ordinary local scan wrapper containing
`snapshot`, `drift` and `gates` is not accepted; use a shareable scan or the
historical export command instead.

Version, exit-code and count fields must be JSON integers. Run and baseline IDs
must be JSON integers or `null`. Booleans, strings and decimal numbers such as
`1.0` are refused rather than coerced into those fields.

The input must be a regular UTF-8 JSON file, limited to 16 MiB, 500,000 parsed
values and 64 nesting levels. Duplicate object keys and nonfinite numbers are
refused. Parser errors do not echo input data. These are reader resource limits,
not judgement thresholds. A summary does not authenticate an artifact or prove
that someone has not edited it.

## Configured GitHub Actions example

This example assumes Sentinel and the project's dependencies are already
installed, the configured model endpoint is available, and any intended
baseline history has been supplied explicitly. A fresh checkout without that
history reports `no_baseline`; a completed first scan normally initializes the
history baseline for later scans. This workflow does not pass `--update` or
replace an existing baseline, and it creates no policy pins. Export and summarize
themselves remain read-only; running the scan records new history.

```yaml
- name: Scan and summarize recorded evidence
  shell: bash
  run: |
    set +e
    vibe-sentinel scan --format=json --shareable > sentinel-result.json
    scan_status=$?
    vibe-sentinel summarize sentinel-result.json > sentinel-summary.md
    summary_status=$?
    if ! cat sentinel-summary.md >> "$GITHUB_STEP_SUMMARY"; then
      exit 2
    fi
    if [ "$summary_status" -ne 0 ]; then
      exit "$summary_status"
    fi
    exit "$scan_status"
```

The shell captures both statuses so findings and failures still produce a
summary. A malformed or absent result fails the step. The example explicitly
chooses GitHub's step-summary destination; it contains no artifact upload or
PR-comment action. Retain and share the aggregate JSON or Markdown only through
the repository's deliberately configured workflow. Avoid redirecting local
stderr diagnostics into a shareable artifact.

See [versioned results](results.md) for privacy profiles, captured evidence and
historical export behavior.
