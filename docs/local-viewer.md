# Read recorded evidence offline

The source repository includes a read-only viewer in `viewer/`. Open
`viewer/index.html` directly as a local `file://` page in a modern browser, then
choose a Sentinel JSON export with **Open a result file**, or drop one file onto
the page. No server, build, package installation or Node runtime is needed.
Keep the viewer's neighboring files together. A browser or enterprise policy
that prohibits local scripts may prevent it from opening; the CLI export remains
available without changing that browser policy. **Explore a synthetic example**
opens the bundled demonstration without selecting a file. Its labels identify
it as synthetic; it contains no real project measurements.

```bash
# Sensitive detail for local inspection.
vibe-sentinel export /path/to/project --run 42 --profile local-detail > evidence.json

# Aggregate statuses and counts, with private evidence withheld.
vibe-sentinel export /path/to/project --run 42 > summary.json
```

The viewer uses the same [versioned result contract](results.md). It reads only
the selected file. It does not scan the current tree, query a model, inspect the
history database, accept a baseline, apply a policy pin, or send an artifact.
A recorded source revision is the identity captured for that run, not proof
that all files measured matched that revision or that the working tree still
matches it. The viewer does not verify current source or authenticate the
export. Dirty-tree, non-atomic capture and unverified judge/build identity
limits remain visible. Build freshness remains unassessed.

## Inspect a result

- **Baseline drift** shows the measured before/after values, structural identities
  and labels. Expand a finding to see its configured questions, individual
  ratings and the lens responsible for the final severity and note. Search and
  severity filters change only what is visible.
- **State gates** keeps standing facts separate from baseline changes. A policy
  pin or a declared verdict is identified as such. A failed decision has no
  invented answer; partial decisions from a failed gate remain explicitly partial.
- **Questions & answers** shows the captured declarations and assessments,
  including not-asked and failed lenses. Current rule files are never consulted.
- **Coverage & source** shows probe, gate and judge statuses, source/environment
  identity and horizon availability. A missing horizon is not a completed
  comparison. Action reviews and command history are outside a scan export and
  are explicitly unavailable here.

A completed scan is not a universal safety score. Unconfigured gates have not
passed, and no baseline means no baseline comparison was made. Versions 1 and 2
retain their statuses and counts but cannot acquire evidence that was never
captured. Aggregate or MCP profiles explicitly withhold detailed evidence.

## Privacy and import limits

Local-detail exports intentionally retain potentially sensitive questions,
answers, source labels and identifiers. The viewer renders every imported string
as text. It does not turn paths or model output into links, markup or commands.
The viewer writes no files and uses no network requests, remote fonts, CDN
assets or browser storage. **Close result** clears its loaded result and visible
data; the viewer makes no claim about browser or OS memory management beyond
its own behavior.

Only result versions 1, 2 and 3 are supported. Input must contain a `result`
envelope, as written by the export commands above. A scan JSON wrapper may also
contain `snapshot`, `drift` and `gates`; those sections do not supply evidence
missing from the envelope. A bare result object is not accepted.

Required fields, field types, profiles, statuses, counts and answer attribution
are checked before display.
Contradictory or unknown contracts are refused with an error. A failed import
clears the previous result so stale evidence cannot be mistaken for the new file.
Unreadable files, invalid UTF-8, malformed JSON and duplicate object keys are
also rejected. Errors do not echo imported payload text.
The maximum input is 16 MiB, with a 250,000-node and 48-level import budget.
Captured evidence retains the engine's 8 MiB ceiling, per-field bounds and
record-count limits. A very large unrestricted local scan JSON may exceed these
limits; use the bounded historical export instead.

Lists are paginated at 30 records. Detailed finding bodies are created only when
opened. Filters preserve the original result, and tabs, disclosures and paging
controls support keyboard navigation with visible focus. The included example
is synthetic and says so throughout the interface.

## Developer checks

The viewer is distributed as source in the repository, separately from the
Python wheel and source distribution. From the repository root, run the
dependency-free tests with Node 20 or newer and npm:

```bash
npm test --offline --ignore-scripts --prefix viewer
```

These tests exercise contract parsing and renderer behavior through Node's
built-in test runner and a small DOM adapter. They do not verify rendered
browser layout or compatibility with a browser's local-file policy.
