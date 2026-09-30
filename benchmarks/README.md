# Benchmarks

What this tool's model traffic costs, on your backend.

```bash
python -m benchmarks                 # every suite (tens of minutes: see below)
python -m benchmarks concurrency     # just the fan-out sweep
python -m benchmarks --repeat 1      # one pass per condition, for a look
```

Recorded runs live in `results/`, one markdown table and one JSON object
per run, named by date and suite.

## What these are, and are not

`evals/` measures whether the four model-facing judgements come back
**right**. This measures what getting them **costs**. Neither gates
anything, and neither is a threshold — a benchmark is the evidence you
would need before writing one down, which is the same standard every
constant in `trends.py` is held to.

These are also not a benchmark of the model or of the server. What is
measured here is the shape of the requests *this tool* sends — one narrow
question per request, a system message identical across the fan-out, a
semaphore over the lot — against whatever is serving them. Point it at a
backend that serialises and it will report a different answer. That is the
measurement working, and it is exactly why `concurrency` is a setting
rather than a constant.

## The three suites

### `concurrency` — what the fan-out width buys

Four settings share one default — `[drift] concurrency`, `[safety]
concurrency`, `[packages] concurrency`, `[credentials] concurrency` — and this
suite is where it came from: `config.DEFAULT_CONCURRENCY`, and the reason
beside it.
The suite sweeps the width over a fixed batch of requests and reports the
wall clock of the whole batch, not throughput at saturation: a scan asks N
questions and waits for all of them, so what a person waits for is the
last answer.

Two things bound the answer beyond the median. The `slowest` column
matters separately from `seconds`, because the safety gate runs inside its
own timeout in front of a tool call, where one long tail is a command
recorded `unreviewed`. And the tool's own fan-outs are small — a drift
pass asks once per watched lens, a safety review once per matched danger —
so a width above what your project actually asks for costs nothing and
buys nothing. The credentials gate on an unpinned tree is the one that
gets wide.

### `prefix` — what the identical system message buys

`One question per request, not one request per command` rests on a claim
about the wire: the command and its history, or the drift report, are the
system message and byte-identical across the fan-out, so a server that
caches prefixes prefills them once. Both arms send the same number of
requests at the same width and differ in one thing — whether the system
message is the same bytes. The distinct arm puts its nonce at the *front*
of the report, because a prefix cache is only defeated there.

### `client` — whether a client per call costs anything

`llm_query` builds its own `httpx.AsyncClient` unless handed one, and
every caller hands it none. Constructing one is not free — the suite
measures that in isolation and records it in the note — so the
optimisation suggests itself. Whether it survives contact with a
generation is what the two arms answer.

### `truncation` — what an over-run costs

`max_tokens` is a ceiling, not a reservation: a lower one never made an
answer shorter, it only decided when a long one was cut off. And a cut-off
answer is not thrown away — `llm_query` retries it at double the budget, so
one over-run pays a second prefill and a second generation for a single
answer. Three gates pinned it to 512 on the opposite assumption, and each
truncating candidate cost them two generations.

The arms differ in the budget alone. The suite then **checks its own
premise** from the backend's truncation counter, because the gap between
them is only a price if the arms did what their names say: an arm that
never truncated priced no retry, and one where *both* truncated understates
the cost. Either way it says `NOT MEASURED` and names the fix, rather than
reporting two durations as a comparison.

## Reading a result

Every condition is warmed with its own payload at its own width, that
warm-up is discarded, and the reported figure is the **median of
`--repeat` passes**. This is not politeness. Three things are cold on a
server that has just answered nothing: the first prefill and whatever the
runtime compiles with it, the prefix cache — which one suite exists to
measure — and this process's own import and connection path. Charge any
of them to whichever condition ran first and you have measured the order
of the loop. The first ad-hoc version of the client suite did exactly
that and made a shared client look 7% *slower* than a fresh one per call,
on nothing but running second.

Columns: `seconds` is the median pass **with the pass-to-pass spread
beside it**, `req/s` the batch divided by it, `median` the middle
single-request latency across every pass, `slowest` the worst single
request seen, and `vs first` the ratio against the first condition.

The spread is there because of what the first recorded sweep hid: a row
reading `30.07 ±115%` had passes of 41.06s and 19.08s, and its median
looked exactly as solid as every other row's. **A gap between two rows
means nothing until it is larger than the spread within them** — on this
project's own data, one pair of arms differed by 7% while overlapping
completely, and another differed by 10% with no overlap at all. Only the
second is a result.

A row flagged `N truncated+retried` had answers cut off at `max_tokens`,
counted from each response's `finish_reason` — part of the
OpenAI-compatible surface, so it is visible on every backend. A condition
whose requests did not all come back with a usable answer says so beside
its label — a pass that was fast because a third of its answers were
unparseable is not a fast pass, and the wall clock alone cannot tell you
which you are holding.

## Cost

The defaults sweep five widths at 16 requests, three passes each, plus a
warm-up per condition, and then three two-armed suites on top — and the
serial width is by construction the slowest thing in any of it. On the
backend the runs in `results/` came from, that was the better part of an
hour for the full run. `--repeat 1` and a shorter `--widths` list is the
quick look; the recorded runs in `results/` are the slow one.

There is no mechanical tier here and no `--no-model`. An eval can measure
the preparation without a backend because the preparation is arithmetic. A
duration cannot be measured without the thing whose duration it is, so a
backend that is not there fails with the error the tool itself gives,
naming the command that fixes it.
