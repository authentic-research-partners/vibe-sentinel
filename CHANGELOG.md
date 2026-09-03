# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release is refused if the version it names has no section here. The date on
the heading is stamped at release; write `unreleased` and leave it.

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

[0.1.0]: https://github.com/authentic-research-partners/vibe-sentinel/releases/tag/v0.1.0
