# Full-stack relationship example

The [example source](examples/fullstack/) connects a vanilla JavaScript browser,
a Node HTTP API and a small Python service. A committed OpenAPI JSON artifact
describes the public greeting response. The example is implemented and verified
in source, and pending release.

The application has no third-party runtime dependencies or build step. Scanning
it requires Python 3.13 with Vibe Sentinel installed, Node 22 or newer, the
separately installed JavaScript pack and a configured judge. Installing that
pack adds its pinned TypeScript parser. Nothing in a scan installs dependencies,
starts these services or imports application modules.

| Component | Location | Measurement |
| --- | --- | --- |
| Browser | `apps/web/src` | JavaScript source membership and static import relationships |
| Node API | `apps/api/src` | Workspace membership, local imports and Node builtin imports |
| Python service | `services/python/service` | Opt-in Python module relationships within the declared source root |
| API declaration | `openapi.json` | Opt-in artifact structure, required fields, local schema references and declared security attachments |

HTTP calls between components are part of the runnable demonstration, not inferred
cross-language edges. The readers do not prove that the artifact matches the
servers, that a route is reachable, or that declared authorization is enforced.

## Copy and configure

Copy the example into a new directory outside the Sentinel checkout before
installing tools or recording a baseline:

```bash
cp -R /path/to/vibe-sentinel/docs/examples/fullstack /path/to/my-fullstack-example
cd /path/to/my-fullstack-example
python --version
python -c "import vibe_sentinel; print(vibe_sentinel.__version__)"
node --version
```

Use the Python environment where Sentinel is installed. The declared Python
readers invoke `python -I -m ...`; an application virtual environment without
Sentinel is not a replacement for that scanner environment. If necessary, set
`SCANNER_PYTHON` for both `python-dependency-edges` and `openapi-artifact` in the
example's `[probes.parameters]` tables to the scanner's interpreter.

Build the JavaScript pack's local npm tarball using the explicit
[source installation instructions](javascript-pack.md#install-deliberately-from-source).
Then, from the copied example directory:

```bash
npm install --save-dev --ignore-scripts --no-audit --no-fund /path/to/vibe-sentinel/packs/javascript/vibe-sentinel-javascript-0.1.0.tgz
vibe-sentinel scan --list-packs
vibe-sentinel scan --list-probes
```

The npm package name is a local artifact identity here, not a claim that it is
published to a registry. Explicit installation can contact the configured npm
registry for the parser dependency; scans themselves do not. The supplied
configuration selects the installed manifest at
`node_modules/@vibe-sentinel/javascript/pack.toml`, disables unrelated built-in
source probes, and declares the Python and OpenAPI readers with
`compare_parameters = true`. Each changed source/artifact scope requires a
deliberate new baseline before comparisons resume.

Configure the model endpoint and name using [judge setup](use-of-local-model.md),
then check it without starting a server:

```bash
vibe-sentinel backend status --config .vibe-sentinel.toml
```

The example retains the default organization lens and credentials gate. It does
not declare Python licence or package policies, and provides no npm installed
inventory or npm gate. Read each gate's status: an unconfigured policy has not
passed. Do not copy unrelated development licence policies into this example.

## Launch the application manually

Scanning does not require the application to run. To try the application,
start the Python service in one terminal from the copied example:

```bash
cd services/python
python -m service.server
```

In a second terminal at the copied example root:

```bash
node apps/api/src/server.js
```

Open `http://127.0.0.1:8765`. The Node process serves the browser and calls the
Python service on `127.0.0.1:8766`. These are loopback-only demonstration servers;
use free ports or adjust both callers and listeners deliberately. Stop each
process with Ctrl-C in its terminal. No test or reader launches them for you.

## Record and change the relationships

With the judge configured, record an initial scan and repeat it:

```bash
vibe-sentinel scan --format=json
vibe-sentinel scan --format=json
```

The first completed scan establishes a baseline. The unchanged repeat has no
structural drift. Normal JSON output includes local observations and the
versioned result. To record another scan and save only its aggregate result,
use the shareable profile.

```bash
vibe-sentinel scan --format=json --shareable > scan-summary.json
```

The shareable form contains statuses, counts and fingerprints. Each recorded
run stays in the copied example's `.vibe-sentinel/history.db`. Use historical
export to read an existing result without rerunning the scan. [Result contracts](results.md#export-recorded-evidence)

Try one edit at a time, then scan again:

| Edit | Expected structural evidence |
| --- | --- |
| In `apps/web/src/view.js`, replace `./format.js` with `./format_alt.js` | One browser edge disappears and another appears; import count stays the same |
| In `apps/api/src/server.js`, replace `./response.js` with `./response_alt.js` | The same count-preserving rewire in the Node API |
| In `services/python/service/server.py`, replace `from .messages import` with `from .messages_alt import` | The same count-preserving rewire in the Python service |
| In `openapi.json`, change the response `$ref` from `Greeting` to `AlternateGreeting` | Schema-reference identity changes, even with the same number of references |
| Change `Greeting.required` or the operation's `security` declaration | Artifact state changes; this does not establish runtime validation or access control |

The deterministic comparison identifies what moved; the configured judge
assesses those changes. Counts alone cannot reveal these source rewires. Use
`--update` only after reviewing changes you intend to accept; a measurement or
judge failure cannot become a baseline.

An unresolved or computed source import, unreadable artifact or unsupported
artifact feature is incomplete coverage, not evidence that relationships were
deleted. The scan reports the failed reader and exits 2. Fix or explicitly
change the input scope, then measure again.

## Limits and acceptance checks

The artifact is deliberately committed, not generated by importing the Python
service. Maintain it when the API changes. Scanning a stale artifact still
measures that artifact, not the live service. The example is not a framework
adapter, an API conformance test, a deployment recipe or an authorization test.
The JavaScript reader uses its documented TypeScript resolver scope; Python
source uses the scanner's parser. The [OpenAPI reader](openapi.md) accepts its
supported JSON projection and in-document references; it does not fetch remote
references or load YAML. Pack history currently supports baseline comparison,
not pack horizons or fitted trends.

The acceptance tests copy this exact source and invoke the real readers through
the normal runner and CLI. They cover baseline/repeat, count-preserving rewires,
API declaration changes, incomplete coverage, recorded envelopes and strict
shareable exports. Their judge is the ordinary test stub, not an option exposed
to users. No application server is started and application modules are not
imported by the tests.

From the Sentinel checkout, after explicitly installing the JavaScript pack's
development dependencies:

```bash
pytest tests/test_fullstack_example.py -q
```

Python-only contributors may see explicit skips when Node or the parser is
absent. Full-stack acceptance requires those tests to actually run. The
separate npm artifact test verifies installation from an offline tarball; this
suite uses the checkout's reader to exercise the composed workflow.
