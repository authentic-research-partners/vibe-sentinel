# Local JavaScript and TypeScript security checks

The development checkout provides a separate, opt-in `vibe-sentinel security`
command. It checks selected inputs and reports findings and coverage for each
check. It leaves drift history and the baseline untouched.

## Prepare the tools once

```bash
vibe-sentinel security setup
vibe-sentinel security status
```

Setup downloads pinned OSV-Scanner 2.7.0, Semgrep CE 1.183.0 and TypeScript
5.8.3, plus a public npm advisory database. It requires an existing `uv`
installation and a trusted system Node installation. Semgrep gets an isolated
Python environment. Setup does not change global PATH, Codex configuration or
the application's virtualenv, and does not run application package scripts.

Resources persist in `~/.cache/vibe-sentinel/security`. Use
`--tools-directory /absolute/path` on setup, status and scan to select another
store. A fresh CLI or plugin process discovers the default store automatically.
Status is read-only and offline: it validates the stored resources and reports
readiness, component paths and versions, advisory freshness and the last setup
failure when available. It does not launch the scanners.

```bash
vibe-sentinel security setup --refresh
```

Setup and refresh are explicit network operations. Refresh reuses valid pinned
tools and downloads a new public npm advisory snapshot. Neither operation sends
project source or a dependency inventory. Ordinary scans do not install tools or
refresh the database. A database older than seven days is reported as `stale`;
status exits `2`, but the validated snapshot remains usable for offline scans.
Run `setup --refresh` to update it. Ordinary `setup` reuses an already valid
store, including a stale database; it can repair missing or invalid resources.

Setup retains previously committed tool and database snapshots. The managed
store has a 4 GiB cap and fails with a diagnostic when it cannot fit new
resources. A failed setup does not publish an incomplete replacement. Read
status and the setup diagnostic before retrying.

## Select the inputs

```bash
vibe-sentinel security scan /path/to/project \
  --lockfile package-lock.json --source src --browser-artifacts out --format json
```

The root defaults to the working directory. Use project-relative paths for the
lockfile, source directory and export directory. Select one or more checks.
Omit `--format json` for text output. The earlier `vibe-sentinel security
/path/to/project ...` spelling remains supported.

Scans discover validated managed tools and the advisory database. You can
instead pass `--advisory-db /path/to/osv-cache`; the cache may be outside the
project. Unmanaged OSV-Scanner and Semgrep installations on PATH remain
supported when the corresponding managed tool is unavailable. The command
checks existing inputs; it does not build or run the application.

### npm lockfile advisories

`npm-advisories` needs a stable OSV-Scanner v2.7.0 or newer within v2 on
the managed store or scanner's PATH and a prepared npm advisory cache. An
explicit `--advisory-db` selects the cache root
containing `osv-scalibr/npm/all.zip`. Sentinel sets both
`OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY` and
`OSV_SCALIBR_LOCAL_DB_CACHE_DIRECTORY` to this root for the scanner process.
Version 2.7 introduced the package source evidence this check requires.
[OSV-Scanner 2.7 release](https://github.com/google/osv-scanner/releases/tag/v2.7.0),
[offline database configuration](https://github.com/google/osv-scanner/blob/main/docs/offline-mode.md)

Sentinel runs OSV-Scanner offline on the selected `package-lock.json`.
Findings identify a locked package, version and advisory. Coverage depends
on the selected database. Managed database freshness is reported by
`security status`; Sentinel does not verify freshness of an explicitly supplied
unmanaged cache.
Coverage reports `packages`, `assessed_packages` and `unassessed_packages`.
A package is assessed only when the scanner reports a public-registry source
and a valid resolved semantic version. Other packages leave the check
`unavailable` and the command exits `2`, retaining findings from assessed
packages. Reachability, the dependency inventory of a deployment and licence
policy are outside this check's scope.

### JS/TS source

`js-source` uses managed Semgrep CE or an installation on the scanner's PATH. It reads a bounded set of
`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs` and `.cjs` files from the selected directory and uses
Sentinel's bundled first-party rules. It runs with metrics and version
checks disabled, using the local rules file. [Semgrep CLI reference](https://docs.semgrep.dev/cli-reference)

The rules check selected flows from browser URL data into HTML sinks, and
from Node request data into shell commands or file read paths. The matches
identify locations for review. The rules cover flows within functions and
leave runtime authorization and exploitability for separate review.

With the managed TypeScript parser and trusted Node available, Sentinel encodes
bare ampersands only in JSX text in an isolated source copy before Semgrep
reads it. This accommodates the scanner's JSX parser while preserving
executable code and source line numbers. Project files are unchanged. Coverage
records `jsx_input_adapter` and the count in `jsx_text_encodings`. The helper
uses the managed parser, not TypeScript from the application's dependencies.

Tool errors or missing file coverage leave the check in `error` status and
the command exits `2`, retaining any candidates found. Recognized partial
parsing errors add `partial_parse_files` and `partial_parse_locations` to
coverage, with project-relative paths and line numbers. These locations help
you investigate incomplete parsing; they are not findings. A result with no
candidates and incomplete coverage does not establish that the source passed.

### Browser artifact credentials

`browser-artifacts` reads an existing Next.js static export, typically
`out/`. The check requires a POSIX host and an export with `index.html` and
a `_next/static` subtree.
Select the export itself. Build work directories such as `.next/` can
contain server files and caches. [Next.js static exports](https://nextjs.org/docs/app/guides/static-exports)

The check reads supported text assets including HTML, JavaScript, JSON,
source maps, CSS, SVG, Markdown (`.md`), XML and plain text. Supported binary
media and fonts are excluded. Finder's `.DS_Store` files are excluded as
metadata and counted in `metadata_excluded`. Coverage records limits and
unreadable or unsupported files.
This reading applies to the selected export. Deployment and build freshness
are outside its scope.

PDF document text is checked when `pdftotext` is already on the scanner's
PATH. Sentinel does not install it. This covers extracted text only, without
OCR, embedded image inspection or attachment inspection. Missing extraction
tools, failed extraction, empty text (including image-only PDFs), and
extraction limits leave coverage incomplete. Coverage counts successful and
failed extractions in `pdf_text_extracted` and `pdf_text_failed`. PDF findings
identify the file without assigning a physical line number to extracted text.

Sentinel applies its built-in credential content rules. Short strings flagged
only for looking password-shaped are excluded from review in generated output.
Literal assignments to names such as `password` still qualify. A candidate that needs
adjudication reaches the configured judge only as redacted evidence, and only
over a loopback endpoint. The CLI uses the project configuration when present.
Use `--config` to select a configuration explicitly. The export has its own
coverage. Source credential rules, pins, and remote-model permissions do not
apply. Matches are redacted before being divided into bounded review contexts,
so a long minified line does not by itself prevent review. The check accounts
for every nominated match rather than sampling matching lines.
Candidate, file and total-byte limits still apply and leave coverage incomplete
when reached. An individual context that cannot fit, or evidence obscured by
redacting an overlapping private-key block, is recorded as unassessed and leaves
coverage incomplete. Findings from safely reviewed evidence are retained.
This artifact handling does not change ordinary source credential review.
Candidate adjudication has a three-minute time limit. Findings
contain locations and fixed messages without credential values, excerpts or
model explanations.

## Read the result

JSON reports contain `schema_version: 1` and a `checks` array. Each entry has
a status, a summary, a findings list and coverage details when available.
Entries marked `not_requested` were outside the requested scope.
If invocation or configuration fails before the checks start, read the CLI
diagnostic for the reason.

| Exit | Meaning |
| --- | --- |
| `0` | The selected checks completed with no findings in their reported scope. |
| `1` | The selected checks completed and reported findings. |
| `2` | A selected check was unavailable or failed, or the request was invalid. Review any findings returned by the checks that produced them. |

For Codex, use the `security` MCP tool with the absolute configured project
root and the corresponding named inputs. Use `security_status` to inspect
managed readiness and explicitly request `security_setup` to install or refresh
resources. Read its `outcome` and `exit_code`
alongside the check statuses in `data.checks` when available. [Codex plugin](codex-plugin.md#tools-and-workflow)

## Verify the external adapters

The native fixture tests have been exercised with OSV-Scanner 2.7.0 and
Semgrep CE 1.183.0. From the development checkout, put the tools on PATH
for the test process, including their isolated environment's executable
directory when applicable, then run:

```bash
osv-scanner --version
semgrep --version
pytest tests/test_security_osv.py::test_real_offline_scanner_with_local_advisory -q
pytest tests/test_security_source.py::test_real_rules_safe_and_unsafe -q
```

These tests skip when their tool is absent from PATH. The OSV test creates a
local advisory archive and checks affected and fixed versions without an
advisory download. The Semgrep test checks safe and unsafe JS/TS fixtures
against the bundled rules. Neither test installs tools or changes global
configuration. A passing fixture test verifies the adapter on those inputs;
review each project's reported coverage separately.
