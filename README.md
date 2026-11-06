# Vibe Sentinel

**Audit your dependencies, catch exposed credentials, and see when your
codebase changes shape.** Vibe Sentinel is an open-source repository security
and change monitoring tool for Python services, Node backends, frontend apps,
and mixed repositories. If you use an AI coding agent, Sentinel can also review
its commands against your project policy before they run.

| What you want to know | What Sentinel gives you |
| --- | --- |
| **What needs attention today?** | Find credential candidates, undeclared or missing Python dependencies, and licences outside your policy. For JS/TS, check npm lockfiles for known advisories, selected source flows, and credentials in built Next.js exports. |
| **What changed across many edits?** | Compare installed dependency versions, module organization, exception handling, and file sizes with a baseline and older scans. Add readers for imports, API contracts, migrations, jobs, and build output. |
| **What is the agent about to do?** | Record supported requests, review commands such as a risky deletion or an undeclared install, and optionally block them before execution. |

[Start with Python](#scan-a-python-project) · [Start with JavaScript or TypeScript](#check-a-javascript-or-typescript-project) · [Use Codex or an agent hook](#review-agent-actions)

## Install

Install the CLI with [uv](https://docs.astral.sh/uv/getting-started/installation/).
The scanner runs on Python 3.13 in its own environment. It can inspect a
different Python environment or a project that uses no Python at all.
The install command also exposes ast-grep for structural pattern probes.

```bash
uv tool install --python 3.13 --with-executables-from ast-grep-cli vibe-sentinel
vibe-sentinel --version
```

For drift review and credential adjudication, connect to a running
OpenAI-compatible model backend. The defaults target local Ollama. If you
already have another backend, set its endpoint and model in
`.vibe-sentinel.toml`. [Model setup and hardware guidance](docs/getting-started.md#2-start-the-model-backend)

```bash
# With Ollama installed and running
ollama pull gemma4:26b-a4b-it-qat
vibe-sentinel backend status
```

## Scan a Python project

From the application repository, add these settings to `.vibe-sentinel.toml`.
Use the path to your application's **existing, trusted** Python interpreter.
The example accepts permissive and public-domain licences. Choose the categories
that fit your project. If the environment relies on executable startup hooks,
Sentinel reports incomplete dependency coverage. See the supported layouts
before choosing a target. [Measured Python environments](docs/environments.md)

```toml
[environment]
python = ".venv/bin/python"

[licenses]
allowed_categories = ["permissive", "public-domain"]
```

```bash
vibe-sentinel scan
```

The first complete scan establishes a baseline and can already report current
findings. For example, it can flag an unresolved import, an import from an
installed package missing from your declarations, a licence outside your
policy, or a credential candidate in source. Run the same command after a
change to see which package versions, module measurements, or exception-handler
counts moved.

- **Investigate a single concern** with `vibe-sentinel packages`,
  `vibe-sentinel licenses`, or `vibe-sentinel credentials`.
- **Measure the right dependencies** by selecting the application interpreter
  and any installed extras or dependency groups. Sentinel reads metadata without
  importing application packages. [Environment selection](docs/environments.md) · [Dependency selection](docs/gates.md#select-the-dependencies-installed-in-this-environment)
- **Track architecture** with optional readers for [Python import relationships](docs/python-relationships.md),
  [OpenAPI contracts](docs/openapi.md), [Alembic migrations and JSON Schema](docs/backend-schemas.md),
  and [Celery jobs](docs/job-inventories.md).

## Check a JavaScript or TypeScript project

Start with a lockfile and source check in your Node or frontend repository.
Managed setup needs `uv` and an installed Node runtime. It downloads the
scanner tools and a public advisory snapshot once, then reuses them.

```bash
vibe-sentinel security setup
vibe-sentinel security status
vibe-sentinel security scan --lockfile package-lock.json --source src
```

Select the inputs your project has. The lockfile check looks for known npm
advisories offline. The source check reviews selected flows, such as browser URL
data reaching HTML sinks and Node request data reaching shell commands or file
reads. For an existing Next.js static export, add `--browser-artifacts out` to
review its contents for exposed credentials with a local judge. Each check
reports coverage alongside findings. [Security checks and limits](docs/security-checks.md)

For change tracking, start with a [Node or frontend profile](docs/starter-profiles.md)
to measure JS/TS source and assets. Add the separately installed
[JavaScript pack](docs/javascript-pack.md) for workspace declarations and static
imports, or the [Vite reader](docs/vite-builds.md) for entry points, shared build
artifacts, and file sizes. The [full-stack example](docs/fullstack-example.md)
combines browser, Node, and Python evidence in one scan.

## Review agent actions

Start with **observe** mode to see which commands would be flagged. Move to
**enforce** after reviewing the verdicts on your own workflows. If you only
need a record of proposed requests, use **journal** mode.

| Agent | Setup |
| --- | --- |
| **Codex** | Install the [Codex plugin](docs/codex-plugin.md) from the repository. It gives Codex ten MCP tools for scans, security checks, history, and safety, plus setup and inspection skills. Review and trust its optional hook separately. |
| **Claude Code** | Run `vibe-sentinel hook --install --mode observe` from the target repository. |
| **Cursor** | Run `vibe-sentinel hook --agent cursor --install --mode observe` from the target repository. |

Use `vibe-sentinel status` to inspect protection and `vibe-sentinel commands` to
read recorded requests. A journal entry records a proposed action. Execution
evidence comes from the host. Teams can use [managed policy](docs/gates.md#command-safety)
to set a minimum safety mode on administered machines.

## Make results part of your workflow

- **Compare over time.** Run `vibe-sentinel scan` again to check the baseline and
  configured comparison horizons. Use `vibe-sentinel trend` to see sustained
  movement across comparable runs. [Drift and trends](docs/drift.md)
- **Review findings and coverage in CI.** Use `vibe-sentinel scan --format json --shareable`
  for a limited aggregate result. Exit `0` means the requested checks completed
  without findings that affect the exit code, `1` means findings, and `2` means
  failure or incomplete coverage. Review missing coverage before accepting a
  result. [CI summaries](docs/ci-evidence.md) · [Scan results](docs/results.md)
- **Inspect a recorded run.** Use `vibe-sentinel history` or export saved evidence
  to the [offline viewer](docs/local-viewer.md). The viewer and Codex plugin
  bundle are distributed as repository files.
- **Set rules that fit your team.** Declare probes, drift questions, licence
  policy, credential patterns, and command dangers in configuration. A baseline
  accepts structural change. A scoped pin records why a specific state finding
  is acceptable. [Gates and pins](docs/gates.md) · [Write a probe](docs/adding-a-probe.md) · [Ecosystem pack contract](docs/pack-contract.md)

Sentinel measures drift before asking the model to interpret it. The model can
rate and explain measured changes. Credential adjudication uses redacted evidence
and defaults to a loopback judge. JS/TS security checks cover selected inputs
and rules. The Python dependency gates cover provenance and licences. Pair those
with a Python advisory scanner when you need known-vulnerability checks.
[Model responsibilities](docs/use-of-local-model.md) · [Threat model](docs/THREAT_MODEL.md) · [Evaluations](docs/evals.md)

## Project links

[Changelog](CHANGELOG.md) · [Roadmap](docs/roadmap.md) ·
[Development setup](docs/development.md) · [Development plugin](plugins/development/README.md) ·
[Report a vulnerability](SECURITY.md) · [MIT licence](LICENSE)
