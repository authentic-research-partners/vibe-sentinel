# Adding a probe

A probe produces a number. A number is not a finding.

`dir:src/utils` went from 2 to 11 — is that a team splitting a god-module up,
or a grab-bag becoming the biggest directory in the tree? Nothing in the
measurement answers that, and nothing can: the answer is a fact about your
project that lives in somebody's head until it is written down. Writing it down
is what a `[[lens]]` is, and checking that your local model can actually read it
is what a `[[lens.case]]` is.

So adding a measurement is three things, not one:

| | What it is | Where it lives |
|---|---|---|
| **the probe** | what gets measured | `[[probe]]`, a script printing JSON |
| **the question** | what the measurement *means* here | `[[lens]]`, in your own words |
| **the cases** | evidence about your model's answers | `[[lens.case]]`, under the question |

The first without the second gets you a number nobody reads. The second without
the third gets you a prompt that drifts — you will edit it, the answers will
change, and nothing will tell you in which direction.

## Where they live

All three can sit in `.vibe-sentinel.toml`, and for one probe that is the right
answer. Past a handful, put them in their own files and point the config at
them:

```toml
[probes]
rule_files = ["rules/*.toml"]

[drift]
rule_files = ["rules/*.toml"]
```

Globs are relative to the project root and are read *before* the config, so
anything you declare in `.vibe-sentinel.toml` still overrides a shared file by
`id`. A glob matching no file is an error rather than an empty set: a renamed
file otherwise stops measuring whatever it held while the config still says it
is measured.

`[safety]` and `[credentials]` take the same setting for `[[danger]]` and
`[[secret]]`, so one directory can hold every rule a project declares. A single
file may carry several kinds — `rules/fallbacks.toml` holding the `[[probe]]`
that measures something and the `[[lens]]` that reads it is the normal case,
because they are one document to whoever wrote them. Each loader takes its own
half and ignores the rest; a file holding none of the four is an error, for the
same reason an unmatched glob is.

A probe is a script, and a script you wrote can be wrong. Put its tests in
`rules/tests/` — beside the rules they are about, and outside whatever your
own CI calls the shipped suite:

```
rules/
    duplication.toml        # the [[probe]] and the [[lens]] that reads it
    probe_clones.py         # the script the probe runs
    tests/
        test_clones.py      # what that script does, run as a subprocess
        test_this_checkout.py   # facts about *this* repository
```

The split is worth keeping sharp, and the reason is not tidiness. A test of
the *tool* builds whatever tree it needs and passes in any clone. A test of
*your repository* — that your licence policy accepts what is installed, that
the lenses you declare are the ones you meant — is true here and nowhere
else. Mixed together, the second kind either fails in a fresh clone or
excuses itself with `pytest.skip`, and a skip reads as a pass in the summary
line anybody actually looks at. This project shipped three such tests and
did not notice for two releases.

So: if the assertion would still hold after somebody clones your repository
and deletes your config, it is a test of the tool. Otherwise it belongs in
`rules/tests/`, and nothing outside your own checkout needs to run it.

Every listing says where a rule came from, which is how you find out that a
rule file loaded at all:

```
vibe-sentinel scan --list-probes        # the probe set, with each one's values
vibe-sentinel scan --list-lenses        # every question, and which probes it watches
vibe-sentinel safety --print-dangers    # the danger set
vibe-sentinel credentials --print-rules # the secret rule set
```

A rule you declared carries a `from:` line naming its file; a built-in carries
none, because a column repeating "built-in" forty times buries the one line
you are looking for. Each listing ends with how many of the active set are
yours. A rule overridden by `id` reports the file that overrode it — the
question a listing answers is which rule is *in force*, not which file first
used the name.

`[[lens.case]]` travels with its lens. Cases sit under the `[[lens]]` they
test, in whichever file that is, and `python -m evals drift` reads the same
set — so moving a lens into `rules/` moves its cases too and nothing has to be
kept in step by hand. Case ids are unique across every file that declares one,
because an id names a failing case in the report.

Two consequences worth knowing. A rule file is *yours*, so it is where a house
rule belongs — something true of your repository and not of codebases in
general. And whether it is published is a question about your repository, not
about this tool: a directory your release process does not copy is a rule
nobody outside sees, which for most house rules is the point.

## 1. The probe

A script that prints `{"observations": [...], "summary": "..."}` to stdout, and
a `[[probe]]` table naming the command that runs it. The protocol, the
placeholders, `tolerance`, and the rules about keys are in
[Drift](drift.md#custom-probes).

Two things from there matter here more than the rest.

**Put the numbers your question needs in `attrs`.** They are the probe's own
workings — the counts behind the value — and the short ones are shown to the
lens beside the change. Without them a question about a *share* is handed two
counts and asked to overrule them, which it will not do however the question is
worded. That is measured, not supposed.

**Key every observation by something stable across runs.** The key is how this run's number is matched
to the last one, and it is also how the model names the change it is rating.
The shipped probes use a prefix and a path — `dir:src/utils`,
`lines:README.md`, `imports:src/engine.py`, `version:requests` — so that two
probes measuring the same directory produce different keys.

## 2. The question that reads it

Without a `[[lens]]` of your own, your probe's changes are read by the shipped
`organization` question, which names six signals:

| Signal in the shipped question | Probe it reads |
|---|---|
| code appearing in a directory that held none | `module-organization` |
| a directory meant to stay small growing substantially | `module-organization` |
| a module's coupling rising sharply — it has become a hub | `module-organization` |
| commentary rising out of line with the rest | `commentary-ratio` |
| a structural pattern appearing where it never was | `pattern-census` |
| a dependency's version or origin moving with nothing asking | `dependency-versions` |

Two shipped probes appear in no line of it: `file-length` and
`silent-exceptions`. Their changes still reach the model — every change in the
report does — but they arrive as a number with a label and no signal describing
what a movement in them is worth. Asked to rate seventeen new silently-swallowed
exception handlers, the model answers `low`, and says why: *"a code behavior
metric, not structural."* It is reading the question it was given.

A signal line was written for each, and both were reverted. That is the most
useful thing in this document, so it is worth the paragraph:

- **`file-length` failed on measurement.** The model dropped the line's
  qualifier and began rating length on its own — the calm case, five per cent
  of growth in step with its own tests, went from `low` to `high`. Six signals
  work; a seventh appears to be past what an 8B model holds at once.
- **`silent-exceptions` passed on measurement and was reverted anyway.** With
  the line it answered `high` every time. But the thing under test was a band
  somebody had written by hand, and editing the shipped prompt until the model
  agrees with your band is the same overfitting as editing the band. Worse, a
  signal here is a claim on behalf of every project that installs the tool —
  and whether discarded errors matter is a fact about *your* repository's
  rules, which is what `[[lens]]` is for.

So the reading lives in a declared lens instead, and the corpus holds both
answers: the shipped question rates that report `low` (correct — it is not
about error handling), and a lens declared by a project that bans silent
swallows in its lint config rates the same report `high` (also correct — it
does). Same measurement, two questions, two right answers. That is the
argument for writing a question rather than editing a shipped one, and it is
measured rather than asserted.

So write the question. It is literally the prompt:

```toml
[[lens]]
id = "thin-probes-directory"
title = "One file per probe"
watch = ["module-organization"]
question = """
One file per probe is the design in this repository, so probes/ growing is
usually the design working. What would not be: a module in there that is not a
probe — something shared, a helper, a base class — because that is the point at
which the directory stops being a list and starts being a package with its own
internals.
"""
```

Three things earn their place in that:

- **`watch`** names the probes the question is about, and it is the cheap
  stage: a lens whose probes did not move, did not move over a horizon, and
  have no fitted trend is not asked at all. Three declared lenses are not three
  requests every scan. A `watch` naming a probe that does not exist fails the
  scan, because a lens that is never asked reads exactly like a lens that found
  nothing.
- **What the question excludes** is worth as much as what it includes. "Growth
  inside a module that already owns its concern is not this question" is what
  stops a narrow lens rating everything — and across lenses the worst rating
  wins, so a lens that was not looking for something cannot talk down the one that
  was.
- **Say what your project knows.** Which directory is meant to stay thin, which
  file is generated, which package is vendored. A shipped list of signals cannot
  know any of it, which is the whole reason this is declared rather than
  shipped.

A standing note that belongs in front of *every* question goes in `[drift]
guidance` once, rather than being repeated in each lens.

## 3. The cases that keep it honest

Your question now goes to the model configured for your project. Whether that model can
answer it is an empirical question, and the honest answer is that sometimes it
cannot — this project has a case on record where a prompt saying "answer
unsafe, this is the canary" came back `safe`.

Cases are declared under the lens, in the same file:

```toml
[[lens.case]]
id = "one-more-probe"
why = """A sixth probe is the design working. If this rates above `low` the
question is not carrying its own first sentence."""
[[lens.case.change]]
probe = "module-organization"
key = "dir:vibe_sentinel/probes"
kind = "grew"
before = 5
after = 6
label = "vibe_sentinel/probes: 6 module(s), 1,480 lines"
accept = ["info", "low"]
```

Then:

```bash
python -m evals drift              # asks your model, scores against the bands
python -m evals drift --mechanical # validates the cases, asks nothing
```

`vibe-sentinel` itself reads `[[lens]]` and ignores `case` entirely, so
declaring cases costs a scan nothing and cannot break one.

### What each key is for

| Key | Meaning |
|---|---|
| `id` | names the case in the report. Unique across all loaded rule files and the project config. |
| `why` | the argument for the bands below it, for whoever disagrees later. Required, for the reason `reason` is required on a pin. |
| `guidance` | overrides `[drift] guidance` for this case. Declare `""` to measure the question without the standing note. |
| `summaries` | probe-id → the `summary` line the report would carry, quoted under `<CURRENT_STRUCTURE>`. |
| `[[lens.case.change]]` | one change in the report: `probe`, `key`, `kind`, `before`/`after` (or `before_state`/`after_state` when `kind = "changed"`), `label`, and `accept`. |
| `[[lens.case.trend]]` | a fitted series, for a question about a direction: `probe`, `key`, `direction`, `runs`, `first_value`, `last_value`, and optionally `tau`, `p_value`, `significant`. |

**`accept` is a band, not a value.** `medium` and `high` on a directory that
doubled are both defensible, and scoring one of them wrong measures your taste
rather than the model. Widen the band where a careful colleague would accept
two answers; keep it narrow where they would not.

### What a corpus needs to be worth having

- **A case where the right answer is "this is ordinary."** A model that answers
  `high` to everything scores perfectly on your alarming case. Without a calm
  one you cannot tell that apart from a model that is reading.
- **A case your question should *not* rate.** Something real, in a probe your
  lens watches, that the question deliberately excludes. This is what catches a
  question that has quietly widened.
- **The same case twice, with and without `guidance`.** The claim that a
  standing note changes the answers is a claim; this is how it becomes a
  measurement.

Everything a case declares is refused if it could never be measured: a band
that is not a severity, a probe nothing runs, a case whose changes do not trip
its own lens's `watch`. Each of those would otherwise show up as a corpus with
nothing wrong in it.

## Iterating on the question

```bash
python -m evals drift --verbose     # every case that missed its band, and why
python -m evals drift --repeat 3    # whether the same request answers the same
```

The loop is: read the cases that missed, decide whether the *question* or the
*band* was wrong, change one of them, re-run. Both directions are legitimate
and they are not the same:

- The question was wrong when the model's stated reason shows it answered a
  different question from the one you meant. Rewrite the question.
- The band was wrong when the model's reason is an argument you accept.
  Widen the band **and write the argument into `why`** — that is the record of
  having been persuaded, and without it the next reader sees a band with no
  reason and a corpus that always passes.

What ruins a corpus is editing bands until everything passes. The guard is the
calm case and the excluded case above: a question tuned until it rates your
alarming case `high` will start rating the ordinary one `high` too, and the
corpus will say so.

Two things worth knowing before you read a result as a regression:

- **A drift answer is not deterministic.** The gates ask at temperature 0.0;
  the drift pass uses the configured `[llm] temperature`, which ships at 0.2.
  The same corpus can move several points between runs. `--repeat` tells you
  how much of what you are looking at is that.
- **A model that did not answer is not a model that answered wrongly.** Cases
  the backend never returned are counted apart and never folded into the
  accuracy.

## Where this fits

- [Drift](drift.md) — the probe protocol, horizons, trends, and what a lens may
  and may not do to a report.
- [Evals](evals.md) — command safety, adversarial attacks, credentials and
  package-name suites, and how to read their output.
- [Use of the local model](use-of-local-model.md) — what is sent, to where, and
  what is never sent at all.
