# Python dependency relationships

The optional Python relationship reader records individual source dependency
edges. Redirecting an import changes its edge identity even when its import
count stays constant. It uses Python's AST, never imports application modules,
and needs neither Node nor dependencies installed in the application's Python.
The existing `module-organization` probe and its count series remain unchanged.
This reader is not enabled by default.

## Enable the separate measurement

Copy the declaration in [python-relationships.toml](probes/python-relationships.toml)
into your project configuration, or reference a local copy through
`[probes] rule_files`. Its ID is `python-dependency-edges`.

```toml
[probes]
rule_files = ["rules/python-relationships.toml"]

[probes.parameters.python-dependency-edges]
ROOT = "."
SOURCE_ROOT = "src"
```

`SOURCE_ROOT` is an import-search directory, not a package name. For
`src/example/__init__.py`, choose `src`; for `example/__init__.py` directly
under the project, choose `.`. One explicit search root is supported. Source
paths stay relative to the project root, so copying an unchanged project does
not change edge keys. Parent-directory segments and symlinked source roots
are refused. No `sys.path` probing or installed-module lookup occurs.

`SCANNER_PYTHON` must name the Python interpreter with Sentinel installed.
Its default is `python` on PATH. Set an explicit path when the application
and scanner use separate environments; the reader does not need Sentinel
installed in the application environment. The example uses Python `-I` so
project files such as `ast.py` cannot shadow scanner modules during startup.
Use the same isolated invocation for standalone measurements.

```bash
python -I -m vibe_sentinel.probes.python_edges --root . --source-root src
vibe-sentinel scan --probes=python-dependency-edges
```

Adding this probe uses a new measurement identity. The first comparison
reports that it has no baseline, rather than reinterpreting old import-count
observations as changed code. Existing measurements keep their keys.

## What is measured

The reader indexes `.py` files and package directories beneath the selected
search root. Regular modules and packages resolve to `file:<relative-path>`.
A namespace package resolves to `namespace:<relative-directory>`; it is never
represented as an invented `__init__.py`. Duplicate module/package candidates
are an ambiguity, not a guessed import precedence.

- `import package.module` records a relationship to an indexed local module.
- Relative imports use the importing file's package context. A relative import
  beyond that context is an explicit gap.
- `from package import child` resolves to a known submodule only when the base
  has no conflicting syntactic binding, dynamic attributes, dynamic path, or
  star-import ambiguity. Otherwise the reader reports a gap.
- An import of an attribute with no indexed submodule records the dependency
  on its base. It does not decide whether the attribute exists or the import
  will succeed. Multiple attributes from the same base in one statement are
  one module relationship.
- Absolute imports outside the source index are external **module-name
  references**, represented as `package:<module-name>`. This includes standard
  library names. The `pypi` ecosystem tag does not identify a distribution:
  `PIL` is not claimed to be a distribution named PIL, and no installed version
  or availability is inferred.

This is a bounded source map, not a prediction of Python's complete runtime
import behavior. It does not reproduce import hooks, arbitrary package
initialization, multiple search roots, installed namespace portions or
extension modules. It makes no judgement about code or import correctness.

## Phases and explicit gaps

Imports under a direct, unshadowed `typing.TYPE_CHECKING` guard are classified
as `type`. Direct module-level aliases of `typing` or `TYPE_CHECKING` are
supported, as is an inverted guard. Other imports are `runtime` syntax; this
is not proof that they execute. A rebound, locally shadowed, conditional or
compound type guard is unverified. Conservative binding checks span the file,
so an unrelated shadowing scope can produce a gap instead of a guessed phase.

Recognized `__import__` and `importlib.import_module` calls are dynamic gaps,
including literal arguments. Nested or ambiguous loader aliases also produce
gaps. This does not claim to detect every possible custom loader. Wildcard
imports, missing internal targets, ambiguous `from` imports, unreadable or
unparseable source, and symlinks are explicit coverage failures.

The source walk uses the core `EXCLUDED_DIRS` policy, including virtualenvs,
installed dependencies, vendored source, generated output and VCS metadata.
It does not follow source symlinks or read outside the selected project.
Files are bounded to 2 MiB each and 10,000 Python files per measurement.
Non-importable source paths and empty Python scopes require a supported
layout instead of being silently reported as a clean zero.

Stdout follows the observations/summary protocol and adds structured coverage
codes and project-relative scopes. Any gap exits 2. Stderr names a bounded
sample and the standalone command for full JSON. The scanner records a failed
measurement and does not compare partial edges as a complete graph. Source
snippets and dynamic loader arguments are not emitted. File/module names are
intentional local structural evidence and may themselves be sensitive.

## Shared edge identity

Python and the optional [JavaScript pack](javascript-pack.md) use edge schema 1:

```text
edge:1:SHA256(UTF8(compact_JSON([
  ecosystem, source, target, kind, phase, resolution
])))
```

JSON preserves Unicode, with no spaces between separators. The Python helper
is `relationships.edge_key`; shared Unicode fixtures were generated with the
JavaScript implementation. Attributes repeat the six fields and
`edge_schema = "1"`. Line numbers and source snippets are not identities.

Python uses ecosystem `pypi`, kind `import` or `dynamic-import`, phase `runtime`
or `type`, and resolution `local`, `external`, `unresolved` or `dynamic`.
Value counts occurrences of a relationship, not imported symbols. Namespace
targets are explicit directories. A type-only relationship and a runtime
relationship have different keys.

## Guarded comparison scope

The example sets `compare_parameters = true`. This opt-in declaration applies
to any probe and compares its actual executed argv and complete filled parameter
map with recorded runs. Changing either makes its old baseline incomparable;
it does not manufacture a code rewire. An explicit successful `scan --update`
can accept the newly measured scope. Existing built-in declarations retain
`false` and their prior behavior. Packs retain their existing always-on guard.

Horizon comparisons omit both ends of an incomparable guarded probe and name
which scopes could not be compared; other comparable probes remain available.
Trend fits retain only successful runs with exactly matching recorded argv
and parameters. The standalone `trend` command uses the **current declared**
comparison policy and filled defaults, without running the probe. It does not
reconstruct an immutable historical policy. Scope is filtered within the
requested recent history window; unmatched runs do not become zero values.
History rows, result versions and database schema are unchanged.
