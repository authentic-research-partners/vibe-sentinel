# Sentinel development plugin

`sentinel-development` is a reusable Codex development workflow, version
`0.1.0`. Its [develop-with-team skill](skills/develop-with-team/SKILL.md)
coordinates scoped implementation and independent QA using six bundled role
templates. It can be used in projects that do not run Vibe Sentinel.

This is separate from the [Vibe Sentinel runtime plugin](../../docs/codex-plugin.md),
which measures drift and exposes the engine's state gates and action review.
This bundle has no MCP server, hooks, runtime dependencies or background
workers. Installing it does not enable Sentinel protection or install a
Python package. Its workflow is instructions for the coding agent, not a
replacement for tests or CI gates.

## Roles and model preferences

Select **GPT-6 Astra, High** for the main chat in your host before invoking
the skill. A plugin cannot switch the current chat's model.

| Template | Responsibility | Model | Reasoning effort |
| --- | --- | --- | --- |
| [sentinel-lead](agents/sentinel-lead.toml) | Scope, assignments and integration | `gpt-6-astra` | `high` |
| [sentinel-backend](agents/sentinel-backend.toml) | Backend implementation | `gpt-6-astra` | `medium` |
| [sentinel-node](agents/sentinel-node.toml) | Node and TypeScript implementation | `gpt-6-astra` | `medium` |
| [sentinel-ui](agents/sentinel-ui.toml) | User interface implementation | `gpt-6-astra` | `medium` |
| [sentinel-qa](agents/sentinel-qa.toml) | Independent review and verification | `gpt-6-astra` | `high` |
| [sentinel-docs](agents/sentinel-docs.toml) | Documentation | `gpt-6.1-sol` | `medium` |

Only relevant roles should run. The lead assigns non-overlapping ownership,
preserves unrelated changes and integrates the results. QA reviews the result
independently from its implementer. This independence is a workflow instruction,
not an isolated permission boundary: agents may share the same filesystem and
host permissions. Tests and CI are the executable gates.

Bundled `agents/*.toml` files are templates. Automatic discovery of custom
roles inside a plugin is not documented. The skill can read these files and
explicitly request their model and effort through a host's spawn tool when
that tool supports both settings. Availability varies by host; a template
does not prove that its requested model ran. The workflow must report missing
capabilities or unsupported settings, and must not silently substitute another
model or claim delegation succeeded without evidence.

## Install manually

Follow the [Codex manual plugin workflow](https://developers.openai.com/plugins/build/plugins).
The [marketplace example](marketplace.example.json) is a standalone template,
not an active installation:

1. In a checkout containing this bundle, create `.agents/plugins/` if needed.
2. If `.agents/plugins/marketplace.json` does not exist, copy the example there.
   Otherwise merge this plugin entry into its existing `plugins` array,
   preserving existing entries and the marketplace's name. Do not overwrite it.
3. The example's `./plugins/development` source path is relative to the
   checkout root, not the marketplace file. Adjust the source if the bundle
   is stored elsewhere.
4. Restart the ChatGPT desktop app, install the local `sentinel-development` plugin,
   and enable it for the intended project. Start a fresh chat and confirm
   `develop-with-team` is available.

After updating the bundle, restart the desktop app so it loads the updated
files. [Local plugin installation and updates](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually)

These are installation instructions; adding the source files to a repository
does not perform installation. This plugin has no hook to trust. If you also
install the runtime plugin, follow its separate setup and trust instructions.

### Optional custom-role registration

Codex [documents custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
in project `.codex/agents/` or user
`~/.codex/agents/`. Registering the templates is optional and changes host
configuration. First review each template and check for existing files with
the same name. Copy only missing templates into the chosen directory; never
overwrite a role already there. Resolve any name collision explicitly. A
project registration applies to that project; a user registration is broader.

## Validate the bundle

From the repository root with Python 3.11 or newer, check JSON and TOML syntax:

```bash
python - <<'PY'
import json
from pathlib import Path
import tomllib

root = Path("plugins/development")
for path in sorted(root.glob("*.json")):
    json.loads(path.read_text())
    print(f"Valid JSON: {path}")
roles = sorted((root / "agents").glob("sentinel-*.toml"))
assert len(roles) == 6, "Expected six role templates"
for path in roles:
    tomllib.loads(path.read_text())
    print(f"Valid TOML: {path}")
assert (root / "skills/develop-with-team/SKILL.md").is_file()
PY
```

This checks local syntax and expected file presence, not the host's plugin
schema, skill activation or model availability.

## Smoke test in a disposable project

Local file validation does not establish that this bundle works in a live
Codex installation. Test the enabled plugin in the actual host before relying
on delegation:

1. Create a disposable project with `math_utils.py` containing
   `def add(a, b): return a - b` and a test asserting `add(2, 3) == 5`.
   Add `keep.txt` with a marker and record its contents. Use an existing Python
   environment; no dependency installation is needed for an `unittest` test.
2. Select Astra High in the main chat. Invoke `develop-with-team` and ask it
   to fix only `math_utils.py`, have a separate QA agent review and run the
   test, preserve `keep.txt`, and avoid installs, commits and server operations.
3. Inspect the delegation evidence. The relevant backend worker should request
   `gpt-6-astra` with `medium`; independent QA should request `gpt-6-astra`
   with `high`. Check host-reported settings when exposed. If only the request
   is visible, record that limitation rather than claiming the model was verified.
4. Confirm the QA agent is distinct from the implementer, receives the task and
   acceptance criteria, and reports test evidence and any findings. No unrelated
   UI, Node or docs worker is needed for this fixture.
5. Inspect the final diff and marker. Only the authorized implementation should
   change, tests should pass, and no dependency, configuration or unrelated file
   should be changed. Confirm the completion report distinguishes checks run
   from checks unavailable.
6. If the host cannot select the requested model or effort, confirm the workflow
   reports that limitation before delegation instead of quietly substituting.

The fixture checks orchestration behavior, not Sentinel's runtime gates or the
general quality of a model. No live-host smoke-test result is implied by this
documentation.
