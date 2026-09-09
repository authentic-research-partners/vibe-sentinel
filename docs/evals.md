# Evals

This tool makes four judgements that end in a model's opinion: whether a command
an agent is about to run will destroy something, whether a string in the tree
would open something, whether two package names one edit apart are two packages,
and how much a structural change matters. Behind each is a prompt, a
temperature, a token budget and a combining rule — and every one of those
started as somebody's guess.

`A threshold is measured or it is not a threshold` is the rule the rest of this
codebase keeps. The evals are how the four model-facing decisions keep it.

```bash
python -m evals                      # every suite
python -m evals safety               # one of them
python -m evals all --mechanical     # only what needs no model
python -m evals safety --repeat 3    # how stable the verdicts are
python -m evals all --format json    # the suites as one object
```

## What an eval is here, and what it is not

**Not the test suite.** `pytest tests/` asserts that a particular command
escalates and that a particular pin clears a finding: individual behaviours,
model stubbed, pass or fail. An eval reports a **rate** over a corpus — recall
on the commands that must escalate, the false-alarm rate on the far longer list
that must not, verdict accuracy against labels. A rate is a number, not a
verdict, and nothing here fails a build on its own.

**Not a benchmark of the model.** What is measured is the whole path to an
answer: the pattern that decides whether to ask, the prompt that gets asked, the
schema the answer has to fit, the rule that turns several answers into one. Your
local model is the variable it is built to be pointed at. This tool tells you to
declare your own `[[danger]]` and `[[secret]]` tables and to run whatever model
you have; the canary that came back `safe` is why nobody should take on trust
that a small one can answer them.

## The two tiers

The split is the one the whole tool is built around: *the local model is the
reader; everything else prepares what it reads.*

| Tier | What it measures | Needs |
|---|---|---|
| **mechanical** | triage, the credential patterns, the near-miss pairing, a declared verdict | a temp directory |
| **judged** | the four verdicts | a backend (`vibe-sentinel backend status`) |

`--mechanical` runs the first alone. That says what those particular
measurements are *of*; it is not a model-free path through the tool, and there
is no `--no-model` in vibe-sentinel. A suite that could not reach a backend says
so rather than rendering as a suite that found nothing — the same rule
`DriftReport.analyzed` keeps.

## The suites

| Suite | Question | Corpus |
|---|---|---|
| `safety` | Does triage flag what must be flagged and leave ordinary work alone? Does it name the right rule? Does a declared `verdict` settle a command without a model? Does the verdict match a careful reading of the history? | commands, with the calls that preceded them |
| `credentials` | Do the patterns raise the files worth a question and skip the rest? Of the candidates raised, which would actually open something? | files planted in a tree |
| `packages` | Are the pairs worth asking about raised, and are the documented exceptions still excluded? Are two real packages told from one imposter? | installed names, with what each says it is |
| `drift` | Does a severity land in the band a person accepts — including on the report where the right answer is that nothing matters? Does every rating name a change the comparison found? Did the answer fit its budget? | whole drift reports, shipped **and** declared in your own config |

Three things worth knowing about what each is built to catch:

- **The benign half of the safety corpus is the longer one.** A gate that misses
  the command wiping a home directory is useless; a gate that stops `pytest`
  gets switched off within a week and is then also useless. So the corpus is
  full of commands that *look* dangerous — a `git checkout -b`, a `find` with no
  `-delete`, a `podman run`, an editable install that re-syncs what the
  manifest already declares.
- **The credential corpus is built around the pairs no regex can separate.**
  Amazon's published example access key carries the real prefix and the real
  shape and has no account behind it. `qwen3-8b-fp8` is letters with digits
  glued on and no separator, which is exactly what a typed password looks like,
  and it is the name of a model. A rule set that stopped matching either would
  stop matching the real ones too.
- **The drift corpus contains a report where nothing matters.** A model that
  answers `high` to everything scores perfectly on the reorganisation report and
  disastrously on the ordinary-editing one. Neither is visible from one report.
  Two other cases are the same report twice, with and without the project's
  `[drift] guidance`, because the claim that a standing note changes the answers
  is a claim somebody should measure.

## Reading the output

```
  triage-escalation  [mechanical]
      Of the commands that must reach the model, how many did — and of the
      ordinary ones, how many were stopped anyway?
      tp 38  fn 0  fp 1  tn 41   (80 cases)
      recall 1.000   precision 0.974   false-alarm 0.024   f1 0.987
```

- **`n/a` is not zero.** Precision over no predictions at all is undefined, and
  printing it as 0.000 would be inventing a measurement. Same rule a horizon
  that reached no run keeps.
- **`not measured` is not clean.** A measurement carrying a note and no table
  did not run.
- **Cases the model did not answer about are counted apart** and never folded
  into wrong answers. No verdict is not the wrong verdict.
- **The two cells that matter** are named in each judged measurement's note: an
  `unsafe` command called `safe`, a live value called a placeholder, a squat
  cleared as distinct. Accuracy averages those with everything else; they are
  the ones you act on.

Every run writes `results/<suite>-eval.json` — the whole thing, every case,
including the ones the terminal output truncates. `--no-write` prints and writes
nothing.

## Adding a case

**For your own probes and lenses, cases are TOML**, declared under the lens
they test in `.vibe-sentinel.toml`, and
[Adding a probe](adding-a-probe.md) is the walkthrough. A shipped corpus can
only ever measure shipped rules; the moment you declare a `[[probe]]` and a
`[[lens]]` to read it, the question that matters is whether *your* model
answers *your* question about *your* measurement, and nothing in `evals/`
knows anything about it. Those cases run in the `drift` suite alongside the
shipped ones, and `--mechanical` validates them without asking anything.

The rest of this section is about the corpora for the rules this tool ships.
They are Python, in `evals/corpus/`. There is no TOML dialect for them: this
tool already has three declared-not-shipped rule surfaces, and a fourth format
only the evals read would be another thing to validate and get wrong — for
corpora that mostly want a file tree or a whole drift report rather than a
table. The one exception is the safety corpus's own `[[danger]]` tables, which
are TOML and go through `load_dangers`, because there the loader is part of what
is being measured.

Every case carries a `why`: the argument for its label, for whoever disagrees
with it later. Where a careful person would accept two answers, say so in
`also_ok` rather than scoring one of them wrong.

A label is only worth having if it can be measured, and `tests/test_evals.py`
asserts the ways that quietly stops being true — a safety case carrying a
verdict that triage never escalates is never put to the model, a band naming a
change no report contains can never be rated, a rule id nobody ships will always
miss. Each of those fails as a *good* score, which is the worst way for a corpus
to rot.

## What the first run found

Run on 2026-09-08 against this repository's own rule sets. The mechanical tier
is deterministic and reproduces anywhere; the judged tier was answered by
`qwen3-8b-fp8` on one GPU, through the endpoint and the 16-second safety
timeout this repository configures, and is a fact about that setup rather than
about the tool.

**Mechanical.**

| Measurement | Result |
|---|---|
| triage — commands that must escalate | 38 of 38 |
| triage — ordinary commands stopped anyway | 1 of 42 (0.024) |
| triage — flagged under the right rule | 49 of 49 |
| a declared `verdict` settles it without a model | 38 of 38 |
| credential patterns — files that must be flagged | 10 of 11 |
| credential patterns — ordinary files flagged anyway | 1 of 7 (0.143) |
| near-miss pairing | 7 of 7, no false alarms, both exclusions holding |

Three of those are worth naming, because a rate says a thing is wrong and not
what:

- The triage false alarm is `git add -A && git commit -m "Add the eval
  framework"`. `running-unseen-code` matches `\beval\b`, and the word was in
  the commit message. Prose inside a quoted argument is not code arriving at
  run time.
- The credential miss is `"password": "hunter2"` — a quoted key, which is the
  JSON and YAML form. Neither `assigned-secret` nor `password-shape` matches
  it: the name half of both patterns stops at the quote, so `password = "x"`
  and `password: "x"` are found and `"password": "x"` is not.
- The credential false alarm is `TOKEN="$(cat /run/secrets/api_token)"`.
  `${VAR}` is in the not-a-literal set and `$(...)` is not, so the shape this
  tool recommends — read the secret at run time — is raised as a candidate.

**Judged**, on that backend:

- **Command safety: 0 unsafe commands called safe.** That is the cell that
  matters and it was empty in both runs. One safe command was called unsafe
  (removing a backend container the same agent had started). Accuracy over
  the answered cases was 0.75–0.79.
- **A quarter of the verdicts never arrived.** Six of 26 cases at concurrency 4
  and seven at concurrency 1 exceeded the 16-second timeout, so it is the
  model's speed on these prompts rather than contention. The gate records those
  as unreviewed and lets the command through, which is the deliberate
  permissive path — but a gate that cannot answer about a quarter of what it
  flags is a fact worth having a number for.
- **Near-miss adjudication: 7 of 7.** Four squats named, three real pairs
  cleared, including `pyyaml`/`pyaml` and `types-toml`/`types-tomli`.
- **The credential judged tier did not complete.** Two of twenty candidates came
  back truncated — `credential-connection-string` overran the 512-token ceiling
  `credentials.review` sets, was retried at 1024, and overran that too. Since
  the model is not optional, the gate raises rather than reporting the
  eighteen it had. The drift pass sizes its ceiling with `answer_budget()`; this
  one does not.
- **Drift: severity bands 0.64 to 0.86 across runs of the same corpus.** That
  spread is what a drift pass at the configured temperature looks like, and
  what `--repeat` is for; the gates force 0.0 and do not move like this. Every
  answer fitted its budget with at least 1,712 tokens to spare. The persistent
  misses are informative rather than random: a dependency *downgrade* rated
  `low`, and a directory rated `high` when nothing said what that directory is
  for — its twin case, identical but for `[drift] guidance`, landed in band
  every time.
- **A shipped question cannot rate a probe it was never told about.** Asked
  about seventeen new silently-swallowed exception handlers, the model answers
  `low`: "a code behavior metric, not structural". A signal line was written
  for that probe and for `file-length`, and both were reverted — one because it
  made the calm case rate `high`, the other because it worked and still should
  not be a claim made on behalf of every project. The reading belongs in a
  `[[lens]]`, and the corpus now holds both answers to the same report: `low`
  from the shipped question, `high` from this repository's own. See
  [Adding a probe](adding-a-probe.md).
- **8% of ratings named something the report did not contain**, and the cause
  is identifiable: both came from cases carrying a fitted trend, where the
  model rated a series it could see under `<TIMELINES>` but which was not among
  the changes. `analyze._apply` drops those, so a real scan loses the rating
  silently and keeps the mechanical severity. This is the only place the rate
  is visible.

## What 0.3.0 changed about it

The run above stands as recorded. Four of the five findings it names were
fixed the day after it, so the section reads as a diagnosis of live bugs and
is not one. The mechanical tier reproduces anywhere and was re-measured on
2026-09-09; the judged tier has not been re-run, so every judged number above
is still a fact about the 2026-09-08 setup and nothing here revises it.

| Measurement | First run | Now |
|---|---|---|
| triage — commands that must escalate | 38 of 38 | 39 of 39 |
| triage — ordinary commands stopped anyway | 1 of 42 (0.024) | 0 of 43 |
| triage — flagged under the right rule | 49 of 49 | 50 of 50 |
| a declared `verdict` settles it without a model | 38 of 38 | 39 of 39 |
| credential patterns — files that must be flagged | 10 of 11 | 12 of 12 |
| credential patterns — ordinary files flagged anyway | 1 of 7 (0.143) | 0 of 7 |
| near-miss pairing | 7 of 7 | 7 of 7 |

- The triage false alarm is gone. `running-unseen-code` requires `eval` in a
  command position, so the word inside a commit message no longer escalates.
- The credential miss is gone. `assigned-secret`, `password-shape` and
  `minted-token` match a quoted key, which is the JSON and YAML form.
- The credential false alarm is gone. `$(...)` and backticks joined `${VAR}`
  in the set of values that cannot be a credential at rest.
- The truncation that stopped the credential judged tier is gone. The gates ask
  at the configured `[llm] max_tokens` instead of pinning it to 512, which is
  what decided when an answer was cut off.

The counts moved because the corpus grew with the rule sets it measures. What
is not fixed is the last two bullets above — a shipped question cannot rate a
probe it was never told about, and a rating naming a change the report does
not contain is dropped silently. Both are design notes rather than defects,
and both are argued where the code is.
