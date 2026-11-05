# Evals

Evals measure Sentinel's triage and model decisions against labelled cases.
Run them from the development checkout with its Python environment active.
The measured results describe specific corpora, rules and backend settings.
[Recorded evaluation results](eval-results.md)

## Run the suites

```bash
python -m evals                      # every suite
python -m evals safety               # one suite
python -m evals all --mechanical     # only stages that need no model
python -m evals safety --repeat 3    # compare repeated verdicts in terminal output
python -m evals all --format json    # JSON for the latest run of each suite
```

| Tier | What it measures | Needs |
| --- | --- | --- |
| Mechanical | Safety triage, credential patterns, near-miss pairing and declared verdicts | A temporary directory |
| Judged | Model verdicts and drift ratings after the mechanical stage | A reachable configured backend. Check with `vibe-sentinel backend status`. |

`--mechanical` selects the mechanical tier for the eval. An unavailable model
leaves its judged measurements unmeasured. The eval scores the full decision
path, including candidate selection, prompts, answer schemas and verdict
combination. The repository tests check individual behaviors with stubbed
model calls. Eval rates do not set build pass thresholds.

Each suite writes its latest run to `results/<suite>-eval.json` by default,
including case details omitted from terminal output. Use `--no-write` to
skip result files, or `--output-dir` to choose a location.
`--verbose` shows all missed labels in terminal output.

## What each suite measures

| Suite | What to look for | Corpus |
| --- | --- | --- |
| `safety` | Triage recall and false alarms, rule matches, declared verdicts and model decisions in context | Proposed commands and the calls that preceded them |
| `credentials` | Which files raise candidates, and whether the raised candidates match their verdict labels | Files planted in a temporary tree |
| `packages` | Whether near-miss pairs are raised, exclusions hold and verdicts distinguish real pairs from lookalikes | Installed names and their metadata |
| `drift` | Whether severities land in the accepted band, ratings name measured changes and answers fit the token budget | Drift reports with shipped cases and project-declared lens cases |
| `adversarial` | Which attacks reach review, which receive refusal verdicts and how often harmless twins are interrupted | Threat-model attacks and benign twins against the shipped danger set |

The safety corpus deliberately includes more benign examples than commands that
must escalate, including ordinary commands that resemble dangerous ones. The
credential corpus includes benign strings shaped like real credentials. Drift
cases include calm reports and paired cases with and without project guidance.
For attack scope and examples, see [the threat model](THREAT_MODEL.md).

## Read the output

Mechanical measurements show true and false positives and negatives, along
with rates such as recall, precision and false alarms. Read the case counts
beside each rate. An `n/a` rate has no defined denominator, and `not measured`
means the stage did not run. Judged results count unanswered cases separately
from cases answered with an incorrect verdict.

Look at the labelled mistakes as well as accuracy. A single unsafe command
rated safe, live credential rated a placeholder or package lookalike cleared
as distinct can matter more than a small movement in the overall rate.

In the adversarial suite, `attack-stopped` counts `unsafe` verdicts, including
declared verdicts. Unflagged, unclear and unanswered attacks still count in
its denominator. `twin-interrupted` counts permission requests and refusals on
harmless twins. These metrics describe recorded decisions. The configured
policy and agent adapter determine what happens to an actual request.

## Add a case

For project probes and lenses, declare a `[[lens.case]]` under its lens in
the project configuration or lens rule file. Those cases run with the shipped
cases in the `drift` suite. Run `python -m evals drift --mechanical --no-write`
to validate them without asking the model. [Adding a probe](adding-a-probe.md)

Add shipped cases under `evals/corpus/` in Python. The safety corpus also
includes TOML danger declarations that exercise the same loader as real rules.
Give each case a `why` that explains the label, and use `also_ok` when more
than one answer is acceptable.

Check that the case reaches the decision it labels. A verdict on a command
triage never escalates, a severity band for a change absent from the report,
or a rule ID the tool does not ship cannot measure the intended behavior.
The corpus integrity tests cover those cases in `tests/test_evals.py`.
