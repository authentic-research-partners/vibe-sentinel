# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A release is refused if the version it names has no section here. The date on
the heading is stamped at release; write `unreleased` and leave it.

## [0.5.1] — 2026-10-02

### Added

- `vibe-sentinel status`: where the hook is installed, the safety mode,
  whether the judge answers, and the last session's verdicts.
- `hook --install` takes `--mode journal|observe|enforce`, required, and
  writes it to `[safety] mode`.
- `changing-the-gate` covers `vibe-sentinel hook --install`.
- Cursor: `vibe-sentinel hook --agent cursor`, and `hook --install --agent
  cursor`, which wires `beforeShellExecution`, `beforeMCPExecution` and
  `beforeReadFile` in `.cursor/hooks.json`. `changing-the-gate` covers that
  file.
- The adversarial suite measures reach once per agent adapter.
- `hook --install --managed`: the hook in Claude Code's managed settings or
  Cursor's enterprise `hooks.json`, and the mode in a system policy.
- A system policy file whose `[safety] mode` is a floor a project cannot
  lower and whose `[[danger]]` set a project cannot remove. `changing-the-gate`
  covers it and the managed hook files.

### Changed

- The engine reads an agent-neutral `ActionRequest` and returns a `Decision`;
  each agent's payload and reply live in `vibe_sentinel/agents/`. Claude
  Code's `HookEvent` moved there from `vibe_sentinel.journal`. A spill line
  written by an earlier build still replays.

- `running-unseen-code` matches a file run from a temp or download
  directory, and PowerShell's `iwr … | iex`.
- `deleting-files` matches an interpreter's deletion calls and PowerShell's
  `Remove-Item`.
- `removing-infrastructure` matches `terraform destroy` and the `destroy`
  of `pulumi`, `cdk` and `tofu`.

### Fixed

- A command the safety gate cannot split into words, such as one carrying
  bash's `$'...'` quoting, is asked about when its text names a sender or a
  path in the home directory, instead of being read as reaching nothing.

## [0.5.0] — 2026-09-30

### Added

- `docs/THREAT_MODEL.md` and `SECURITY.md`.
- `docs/CONTROL_MAPPING.md`: each risk against OWASP, MITRE ATLAS and the
  NIST AI RMF, with the tests and eval cases that evidence it.
- `python -m evals adversarial`: the threat model's attacks, each with a
  harmless twin, scored end to end.
- `changing-the-gate`, a shipped danger settled `unsafe` without a model: a
  write, move or delete of `.vibe-sentinel.toml`, `.vibe-sentinel/`,
  `.claude/settings*.json` or a declared rule file. `applies_to =
  "control-file"`.
- `secret-store` and `secret-store-target`, settled `unsafe`: a command or a
  tool naming a file that holds keys in the home directory.
- `reaching-into-home`, asked: a read or a shell write in the home directory,
  outside the project. `applies_to = "home-outside-project"`.
- `sending-data-off-the-machine`, asked: local data uploaded to a host not in
  `[safety] expected_hosts`. `applies_to = "egress"`.
- `installing-undeclared-dependency` reads npm, pnpm, yarn and bun installs
  against `package.json`.
- Under `enforce`, a flagged command with no verdict — `unclear`, or no
  answer within `[safety] timeout` — is asked with Claude Code's permission
  prompt instead of allowed. `[safety] no_verdict = "allow"` restores
  allowing it. A review records `decision`: `allow`, `ask` or `deny`.
- `applies_to = "tool"`, matching a danger's pattern against a tool's name,
  and `deleting-through-a-connected-tool`, settled `unsafe`: an MCP tool
  whose name says it deletes.

### Changed

- The default backend is Ollama: `[llm] endpoint` defaults to
  `http://localhost:11434/v1` and `model` to `gemma4:26b-a4b-it-qat`.
- `python -m benchmarks` counts truncated answers from each response's
  `finish_reason` rather than from a server's `/metrics`.
- `concurrency` under `[drift]`, `[safety]`, `[packages]` and `[credentials]`
  defaults to 1, from `config.DEFAULT_CONCURRENCY`.
- `python -m evals safety` asks about one case at a time by default.
- `[llm] extra_body` defaults to `reasoning_effort = "none"`, switching a
  reasoning model's thinking off.

### Removed

- `vibe-sentinel backend monitor`, and the benchmarks' `cached` column and
  preemption count, which read metrics only vLLM publishes.
- vLLM from the documentation.

### Fixed

- The `safety` and `adversarial` evals score what the gate decides: a
  declared verdict settles a case, as it does in the hook, instead of the
  model's opinion of it.
- A `pyproject.toml` that will not parse no longer reads as no manifest,
  which let every install through `undeclared-install`. The safety gate
  handles it by mode, `scan` reports it, and the `packages` gate fails on it.
- The safety gate reads `exec` in front of a command as a wrapper, as the
  install check already did.
- A safety gate failure is recorded as an `unreviewed` review naming the
  error, and a command the gate cannot split is logged.
- An install the gate cannot split into words, such as `pip install x #
  don't`, is asked about instead of being read as installing nothing.

## [0.4.0] — 2026-09-11

### Added

- `VIBE_SENTINEL_LLM_ENDPOINT`, `_MODEL`, `_API_KEY` and
  `_STRUCTURED_OUTPUT`, overriding the `[llm]` wiring for one machine.
  `docs/getting-started.md`.
- `backend status` prints a `Source:` line naming the overrides in force.

### Changed

- The README's opening: what a probe, a lens and a gate each are, stated
  before the argument for building it that way.
- The test stub answers a lens, and refuses any model call it has no answer
  for rather than letting it reach a backend.

### Removed

- The README's "Why it is built this way" section.

## [0.3.0] — 2026-09-09

### Added

- `python -m evals`, scoring the four model judgements against labelled
  corpora in `evals/`. `docs/evals.md`.
- `[[lens.case]]`, eval cases declared under a lens in the project's own
  config. `docs/adding-a-probe.md`.
- `python -m benchmarks`, timing this tool's model traffic on your own
  backend: fan-out width, the shared system message, a client per call, a
  truncation. `benchmarks/README.md`.
- `vibe-sentinel backend monitor`: queue depth, KV cache use, prefix-cache hit
  rate and truncated answers, from a Prometheus `/metrics` endpoint.
- `rule_files` under `[probes]`, `[drift]`, `[safety]` and `[credentials]`, so
  declared rules live in their own files rather than the config.
- `scan --list-lenses`, and a `source` on all four listings naming where a
  declared rule came from.
- `dangers.default.toml` and `secrets.default.toml`, the shipped danger and
  secret sets as data. They were Python literals.
- `minted-token` and `password-shape`, credential rules keyed on the value
  rather than the variable's name or a vendor's prefix.
- `pwd`, `pat`, `bearer`, `dsn`, `salt` and `signing`/`session`/`encryption`/
  `master` keys in the `assigned-secret` name list.
- Dangers for the JavaScript toolchain: `publishing-a-package`,
  `deploying-to-production`, `forcing-a-dependency-resolution` and
  `exposing-a-dev-server`, with `lockfile` and `dependency-tree` as context.
- `requires` on a danger: a filename that must sit at the project root for the
  danger to apply at all.
- `file-length` measures files that are not text when `UNIT = "bytes"`.
- `[packages] guidance`, a project's standing note about its own names, put in
  front of the near-miss question.
- A probe's `attrs` reach the model that rates its changes.
- `tests/test_layers.py`: the package's six layers, failing on an upward
  import, a cycle, or a module nobody placed.
- `docs/frontend-support.md`.

### Changed

- The local model is required. A run that cannot reach one, or whose model
  does not answer, exits 2.
- The three gates are named for the stage they are at: `licenses.audit` was
  `licenses.check`, `credentials.search` was `credentials.collect`,
  `licenses.Finding` was `Violation`, `credentials.Adjudication` was `Findings`.
- `pins.py` is `policy.py`. All three gates resolve policy through one order:
  the organisation's file, then the project's own table.

### Removed

- `--no-model`, and the degraded paths behind it: `Snapshot.used_model`,
  `DriftReport.analyzed`, the `unreviewed` credential verdict. The
  `runs.used_model` and `runs.analyzed` columns stay.

### Fixed

- Every shipped system prompt says its content is data rather than
  instruction, and names the field an injection attempt is reported in. Four
  of the five were missing it.
- The safety gate no longer substitutes the built-in `[[danger]]` set for one
  that will not load, and no longer reads an unparseable config as
  `mode = "off"`.
- One definition of what is not this project's source: `probes`, `packages`,
  `credentials` and `licenses` kept four disagreeing lists and now all read
  `probes.EXCLUDED_DIRS`, widened to their union. A tree keeping source in one
  of those directories should point `SOURCE_ROOT` at it.
- The safety, credentials and provenance gates ask at the configured
  `[llm] max_tokens` instead of pinning it to 512.
- The word limit a prompt states sits below the character cap the clipping
  enforces, converted by `schemas.chars_to_words`.
- `assigned-secret`, `password-shape` and `minted-token` match a quoted key —
  `"password": "x"`, the JSON and YAML form.
- `$(...)` and backticks join `${VAR}` as values that cannot be credentials,
  and a `sha512-…` subresource-integrity digest with them.
- `running-unseen-code` requires `eval` in a command position, so
  `git commit -m "Add the eval framework"` no longer escalates.
- `silent-exceptions` reports the discard share as `silent-share:<dir>` beside
  the count. Existing keys are unchanged.
- An unreachable model endpoint fails in seconds rather than the 300s
  `llm_timeout`, and is reported once rather than once per candidate.
- `backend status` reads an empty model list as one, not as an unexpected
  shape.
- A `.vibe-sentinel.toml` that cannot be parsed or validated exits 2 naming
  the file, instead of a pydantic traceback.
- A pin an earlier pin already shadowed is no longer inert; the lookup is
  `policy.matching`.
- `db check` reports an unreadable history timestamp as its own finding rather
  than as zero days.
- Every subcommand's own `--help` says what the command does, and five options
  gained help text.
- `parameters` no longer describes its values as the model's choice.
- `safety --print-dangers` indents a multi-paragraph question under its label.
- The automatic database check is skipped for `--print-probes`,
  `--print-config` and `--print-dangers`.
- `hook.handle` and `hook.guard` take `default_cwd`, not `fallback_cwd`.

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
