# Development

## Setup

Python 3.13 in a [uv](https://docs.astral.sh/uv/)-managed virtualenv — uv fetches
the interpreter, so no system Python is touched:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once per machine
uv venv --python 3.13
uv pip install -e ".[dev]"
source .venv/bin/activate                         # Windows: .venv\Scripts\activate
```

A `uv venv` has no `pip` in it: install with `uv pip install`, and add a tool by
editing `[project.optional-dependencies] dev` rather than installing it into the
environment directly — so the environment states what `pyproject.toml` says
rather than what someone once typed.

All three checks must pass, and none needs a GPU:

```bash
pytest tests/ -q
ruff format . && ruff check .
mypy vibe_sentinel/
```

The optional JavaScript pack has a separate Node 22+ toolchain. When changing
that pack, install its exact declared parser dependency deliberately and run
both its tests and the Python integration tests:

```bash
(cd packs/javascript && npm ci --ignore-scripts --no-audit --no-fund)
(cd packs/javascript && npm test)
pytest tests/test_javascript_pack.py -q
```

The installation command can access the configured npm registry. Tests use
local fixtures; the artifact smoke test installs temporary local tarballs with
network access disabled. Python-only contributors do not need Node, and the
optional integration tests report missing prerequisites as skips. Maintainer
release preflight requires the Node runtime and declared dependencies when the
JavaScript pack source is present, then runs its tests without installing or
fetching anything. See [JavaScript pack setup](javascript-pack.md).

The ruff rule set is pinned with `select`, not `extend-select`: the default set
widens between releases, and a gate that shifts under a routine tool upgrade
cannot gate anything. Adopting a rule is a deliberate edit.

Maintainers run one further gate before a release — ast-grep rules covering
layout conventions ruff cannot express. They live in the development repository,
so a contribution is judged on the three checks above and a rule violation comes
back as review feedback rather than as a check you cannot run.

The evals are separate, and deliberately not a check:

```bash
python -m evals all --mechanical   # triage, the credential patterns, the pairing
python -m evals                    # everything, against your backend
```

A test asserts a behaviour and fails; an eval reports a **rate** over a labelled
corpus, and a rate is a number rather than a verdict. Nothing in them fails a
build. Run them when you change a rule set, a prompt, or a token budget — those
are the things whose effect only shows up as a rate. [Evals](evals.md) has the
corpora, what each suite measures, and how to add a case.

## Is it a probe, a gate, or a lens?

Ask what the finding *is*, not what it looks at.

A **probe** reports a **transition**: this directory gained four modules.
`inventory.compare()` diffs it against the baseline, and `scan --update` accepts
it by moving the baseline.

A **gate** reports a **state**: this dependency is AGPL, this key is in the tree.
As true on the two-hundredth scan as the first, so it reports every run, exits
non-zero, and only a pin clears it. Licences, provenance and credentials were
probes once, and it lost them: a diff reports a key when it *appears* and says
nothing while it stays, so a `.env` already committed before the first scan went
into the baseline and was never mentioned again. Getting this backwards is the
mistake this codebase has already made.

A **lens** is neither and adds no measurement. It is a question about the report —
the changes, the horizons, the fits — written in the project's own words and put
to the model, one request each. If what is missing is a measurement, write a
probe; if what is missing is somebody reading one, write a lens.

**Horizons and fits are readings**, not findings: a horizon compares the same
probes against a run a week or a month old, a fit is a Theil–Sen slope over the
whole series. Neither moves the baseline, is stored, or reaches an exit code — a
finding there would repeat until it aged out with nothing anyone could do about
it.

## Rules that are not obvious from the code

- **The model judges; it never chooses what to measure.** It rates a change
  `compare()` found, adjudicates a candidate a rule flagged, and judges a command
  triage flagged. Probe parameters were the exception and are the cautionary
  tale: across nineteen scans of an unchanged tree the model answered
  `SOURCE_ROOT` four different ways, and every change of mind re-keyed every
  observation into a reorganisation that never happened.
- **A filled placeholder never reaches a shell.** Fixed argv, per-element
  substitution, `pattern` validated before exec, `shell=False`.
- **Async all the way down; one `asyncio.run` per command.** Nothing in the
  package opens an event loop except the CLI entry point. Fan-out is
  `asyncio.gather` under a `Semaphore` sized from config, never threads.
- **The hook's import graph is a budget.** `vibe-sentinel hook` runs once per
  tool call, so it loads neither pydantic, loguru, nor httpx, and a test asserts
  they stay out.
- **Pydantic for boundary schemas; loguru for logging.** The journal payload
  and safety danger models have explicit dataclass exceptions to keep the hook's
  import cost low. `print()` belongs only in declared output modules.
- **A failed probe is recorded, not raised** — one broken template must not lose
  the other probes' measurements.
- **A structural check resolves every unknown the quiet way.**
  `outside-project` and `undeclared-install` ask about the tree rather than
  the command text, and every unknown there reports nothing: a path that
  cannot be resolved, a tree with no manifest, an install argument that is
  not a bare package name. A miss leaves things as they were before the
  check existed; a false positive blocks a command that was fine, and a
  gate that stops `uv pip install -e ".[dev]"` is uninstalled by Thursday.
  A manifest that exists and will not parse is not an unknown of that kind:
  it raises, because reading it as "no manifest" answers "nothing declared".
- **Errors carry their remediation.** Name the command that fixes it.

## What runs on what

The engine, history, trends, reports, journal and safety gate are
language-agnostic — a probe is any command that prints the JSON protocol, and the
journal and most of the safety gate match command text. The shipped probes
that parse code (`commentary-ratio`, `module-organization`,
`silent-exceptions`) and the `licenses` and `packages` gates are Python-only,
while `undeclared-install` reads both `pyproject.toml` and root `package.json`
for Python and npm-family installs;
a danger may instead carry `requires`, naming one file that must be at the root
for it to apply at all — which is how the npm entries stay out of a Python
repository's set rather than sitting in it unable to match;
`pattern-census` covers any language ast-grep supports, and `file-length` and
`credentials` parse nothing. `dependency-versions` is Python-only for a
different reason — it parses no code at all, but reads the installed set
through `importlib.metadata`, so what it measures is the environment
vibe-sentinel itself is installed in.

The hook currently targets Claude Code's `PreToolUse` event. Any agent that can
run a command before a tool call and read a JSON verdict fits the same shape.

## License

MIT.
