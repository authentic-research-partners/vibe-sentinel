"""Measuring the judgements this tool makes, against labelled examples.

Four decisions here end in a model's opinion — a command's safety, a
candidate credential, two package names one edit apart, and the severity
of a structural change — and every one of them has a prompt, a
temperature and a token budget behind it that started life as somebody's
guess. `A threshold is measured or it is not a threshold` is the rule the
rest of this codebase keeps. These are the instruments that let the four
model-facing decisions keep it too.

**This is not the test suite.** ``pytest tests/`` asserts that a
particular command escalates and that a particular pin clears a finding:
individual behaviours, model stubbed, pass or fail. An eval reports a
*rate* over a corpus — recall on the commands that must escalate, the
false-alarm rate on the far longer list that must not, verdict accuracy
against labels — and a rate is a number, not a verdict. Nothing in here
fails a build on its own.

**It is also not a benchmark of the model.** What it measures is this
tool's whole path to an answer: the pattern that decides whether to ask,
the prompt that gets asked, the schema the answer has to fit, and the
combining rule that turns several answers into one. A different local
model is exactly the variable it is built to be pointed at — the tool
tells you to declare your own ``[[danger]]`` and ``[[secret]]`` tables
and to run whatever model you have, and the canary that came back
``safe`` is why nobody should take on trust that a small one can answer
them.

Two tiers, and the split is the same one the tool is built around: *the
local model is the reader; everything else prepares what it reads.*

- **Mechanical** — triage, the credential pattern stage, the near-miss
  pairing, a declared verdict. These are the preparation, they are
  arithmetic, and measuring them needs nothing but a temp directory.
- **Judged** — the four verdicts, which need a backend running.

``--mechanical`` measures the first tier alone. That is a statement
about what these particular measurements are *of*, and never a model-free
path through the tool: there is no ``--no-model`` in vibe-sentinel, and a
suite that could not reach a backend says so rather than rendering as a
suite that found nothing.

Run them with ``python -m evals``.
"""
