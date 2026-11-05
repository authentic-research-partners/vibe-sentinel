# Select the Python environment being measured

Vibe Sentinel runs on Python 3.13. It can inspect a separate local Python
environment without installing Sentinel or its dependencies there. By default,
it continues measuring the scanner's own environment; selecting a project root
does not activate that project's virtual environment.

Set the target in the project's `.vibe-sentinel.toml`:

```toml
[environment]
python = ".venv/bin/python"
timeout = 30
```

Use `.venv/Scripts/python.exe` on Windows. Relative paths are relative to the
project root, not the configuration file's directory. An absolute interpreter
path is also supported. Omit `python` to retain the scanner environment.
`timeout` bounds target inspection in seconds; it does not start or stop an
application or model server.

The same selection supplies installed package metadata, provenance, licence
evidence, dependency versions, environment-marker evaluation, standard-library
names and import ownership/availability. Source remains the selected project
tree. The credential gate and source-language probes do not become target
Python processes. A Node profile still does not acquire an npm inventory from
this Python setting.
Target Python 3.10+ support describes metadata inspection. Python source
readers still use the scanner's Python 3.13 parser; selecting a newer target
does not add support for that interpreter's newer source syntax.

## Trust and supported targets

The selected executable is trusted code: choosing it authorizes running that
binary to collect metadata. It must be a local Python 3.10 or newer interpreter.
Sentinel sends a bundled standard-library-only collector; the target does not
need Sentinel, Pydantic, packaging, or an activated shell. A missing executable,
unsupported interpreter, timeout, or invalid collector response fails the
measurement rather than reading the scanner environment instead.
The collector bounds individual metadata files at 512 KiB, licence files at
1 MiB, distribution count at 10,000 and the response at 32 MiB. Exceeding a
bound fails inspection rather than silently truncating the inventory.

Configured targets are inspected without importing application packages or
running Python startup hooks. This is a bounded static view of an environment,
not proof that every application import would succeed when run normally.
Reported coverage limitations remain part of the evidence. A package's presence
in metadata does not prove its imports or native extensions execute correctly.

The collector reads the selected interpreter's normal installation directories
and honours a virtual environment's `include-system-site-packages` setting.
Ordinary isolated virtual environments exclude the user site. System
interpreters and virtual environments with system-site packages enabled can
include permitted user-site directories, respecting `PYTHONNOUSERSITE` and
user/group security checks. `PYTHONPATH` is ignored. Existing directory
entries in `.pth` files can add search paths, but executable `.pth` lines,
including dynamic editable-install hooks, are never executed. Archive-based
search paths are unsupported. Discoverable `sitecustomize` hooks, including
ones in the standard-library directories, are not run. `usercustomize` is
also checked when the target's user-site policy permits it to run. Skipped
startup hooks may add metadata search paths,
so their presence is reported as incomplete coverage, not silently treated as
an empty extension of the inspected paths.

An incomplete configured inventory cannot pass package or licence checks. The
dependency probe emits `not-measured` coverage and exits with an error; the
scan records it as unmeasured and skips its inventory comparison. This avoids
reporting packages hidden behind an unexecuted hook as newly removed. Use a
supported target layout to obtain a complete measurement; Sentinel does not
enable startup hooks automatically to make a check pass. The default scanner
runtime retains its already-initialized environment behavior.

## Verify the selection

Review the target path and then use the usual commands from the project root:

```bash
vibe-sentinel packages
vibe-sentinel licenses --verbose
vibe-sentinel licenses --explain package-name
vibe-sentinel scan
```

The licence and package commands identify the measured interpreter separately
from the scanner. Licence explanations and drafted pins use the selected
environment's captured evidence, including licence file text. Scans still
require the configured model and retain their normal history and policy
behavior. Choosing a target neither installs dependencies nor accepts findings.

Dependency intent is a separate selection. Set `[packages] extras` and `groups`
to match what the application environment is meant to contain. The dependency
probe's `declared` scope follows that selection and the target's markers; its
`all` scope measures every captured installed distribution. See
[dependency selection](gates.md#select-the-dependencies-installed-in-this-environment).
A successfully inspected, explicitly configured environment can legitimately
contain no distributions; `all` records its identity and a zero count.
`declared` fails if selected dependencies lack installed metadata, so an empty
target does not become a completed measurement of an expected dependency set.

For a standalone dependency measurement without a model call or history write:

```bash
python -m vibe_sentinel.probes.dependencies --root . --config .vibe-sentinel.toml --scope all
```

Here `python` must be the scanner interpreter with Sentinel installed. The
collector runs the configured target. During a scan, the built-in dependency
probe receives the effective environment settings explicitly, including when
`scan --config` names a file outside the project. Programmatic configurations
work the same way. Custom probe commands, including overrides of
`dependency-versions`, run as declared. A custom invocation of this reader must
pass `--config` explicitly when it needs an alternate configuration.

## History and MCP

The dependency probe records an `environment:identity` state observation in
addition to its existing `version:` and `origin:` series. Changing the measured
environment is visible even when installed versions happen to match. The
scanner executable is recorded separately in attributes. Existing history
cannot establish an environment identity it never recorded: the first scan
with this observation introduces a new series, and does not backfill earlier
runs or accept a new baseline.

MCP's existing `interpreter` field identifies the scanner process. Scan results
also expose `measured_environment` from a successful dependency observation,
including the target executable, Python version and selection source. It is
null when the dependency observation is absent or failed. The full dependency
inventory and credential excerpts remain excluded from MCP responses.

Existing projects need no migration to retain their current runtime selection.
To separate the environments, keep Sentinel in its Python 3.13 runtime, set the
application interpreter explicitly, and review the first resulting changes.
Do not reset the baseline merely to conceal a scope change.
