# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release is refused if the version it names has no section here. The date on
the heading is stamped at release; write `unreleased` and leave it.

## [unreleased]

### Added

- Dangers for the JavaScript toolchain: `publishing-a-package`,
  `deploying-to-production`, `forcing-a-dependency-resolution` and
  `exposing-a-dev-server`, with `lockfile` and `dependency-tree` as context
  signals. What they have in common is that the command reaches something
  outside the working tree and does not come back.
- `requires` on a danger: one filename that must sit at the project root for
  it to apply at all. The npm dangers above all carry
  `requires = "package.json"`, so a Python repository's danger set is
  unchanged — they are absent from it rather than sitting in it unable to
  match.
- `file-length` measures files that are not text when `UNIT = "bytes"`. An
  image has a size and does not have lines, so a second probe with an image
  glob tracks the weight of what a project ships. No existing baseline gains
  a key: the shipped categories match no binary.
- `docs/frontend-support.md`, a design note on what frontend support would
  take.

### Fixed

- `safety --print-dangers` indents a multi-paragraph question under its
  label. Every line after the first was printed at column zero, which is
  where the danger ids are, so the paragraph read as commentary on the whole
  list rather than as part of the entry above it.
- The automatic database check is skipped for `--print-probes`,
  `--print-config` and `--print-dangers`. All three return before anything
  opens the database, so a warning about its condition was the first thing
  printed by a command that could not have been affected by it.

## [0.2.0] — 2026-09-03

### Added

- A `dependency-versions` probe: the installed version and origin of every
  distribution, which neither a manifest nor a lock file records.
- A fourth kind of change, `changed`, for an observation whose measured
  identity moved rather than its number.
- Dangers that ask about the tree instead of matching the command text:
  `outside-project`, and `undeclared-install` for a package name no
  manifest declares.
- Every probe reports what it could not read or could not parse, and a scan
  that measured nothing says so rather than reporting no drift.
- `docs/drift.md` and `docs/getting-started.md`.

### Changed

- History schema 7. Run `vibe-sentinel migrate` before the first scan.
- Package provenance is read from the metadata entry that records it, so an
  editable install is no longer reported as installed from an index.

### Fixed

- The shipped probes run under the interpreter that has `vibe_sentinel`
  importable. Installed beside the project, all five were failing.
- `pattern-census` counted excluded directories in its total, and took
  shadow-utils' `sg` for ast-grep — a zero it never measured.
- The safety gate and the near-miss adjudication name every field they ask
  for. A model with nothing to say about one wrote whitespace to the token
  limit instead.

## [0.1.0] — 2026-09-03

First public release.

### Added

- Structural scan and drift report against a baseline.
- Probes for module organization, commentary, silent exceptions, patterns and
  file length.
- Drift horizons and fitted trends.
- Gates for dependency licences, package provenance and credentials.
- Lenses: the project declares what the model is asked about a report.
- Model answers are bounded and validated: a truncated, empty or unparseable
  one is retried rather than accepted.
- Append-only run history.
- Agent command journal, with optional review of dangerous commands.
- Model-free mode for CI: nothing is rated, and the report says so.

[0.2.0]: https://github.com/authentic-research-partners/vibe-sentinel/releases/tag/v0.2.0
[0.1.0]: https://github.com/authentic-research-partners/vibe-sentinel/releases/tag/v0.1.0
