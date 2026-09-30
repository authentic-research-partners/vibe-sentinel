"""What this tool's model traffic costs, measured against your backend.

Four decisions here end in a model's opinion, and `evals/` measures
whether they come back right. This measures what getting them costs —
wall clock, and how it moves with the three things the design already
decided:

- **The fan-out width.** Every model-bound stage asks one question per
  request and runs them under a semaphore: ``[drift] concurrency``,
  ``[safety] concurrency``, ``[packages] concurrency``,
  ``[credentials] concurrency``. All four default to
  ``config.DEFAULT_CONCURRENCY``, which this measured.
- **The shared prefix.** One question per request is only cheap because
  the system message is byte-identical across the fan-out, so a server
  that caches prefixes prefills it once. That is the load-bearing claim
  under `One question per request, not one request per command`, and it
  had never been measured.
- **The HTTP client.** ``llm_query`` builds its own ``httpx.AsyncClient``
  per call unless handed one. Constructing one is not free, and whether
  that matters against a generation is a question with a number.
- **An answer that overruns its budget.** ``max_tokens`` is a ceiling
  rather than a reservation, so a lower one never shortened an answer — it
  decided when one was cut off, and a cut-off answer is retried at double
  the budget rather than discarded. Three gates pinned it to 512 on the
  opposite assumption. That was fixed on the argument; this prices it.

**Not an eval, and not a gate.** An eval reports a rate and this reports
a duration; neither fails a build, and nothing in either is a threshold.
Read a benchmark the way `A threshold is measured or it is not a
threshold` asks you to read any constant here — as the evidence you would
need before writing one down.

**Not a benchmark of the model, or of the server.** What this measures
is the shape of the requests *this tool* sends: whether asking four
narrow questions in parallel beats asking them in a row, on your
machine, with your model. Point it at a backend that serialises and it
will tell you the answer is different — that is the measurement working,
and it is why ``concurrency`` is a setting rather than a constant.

**Warm-up is part of the measurement, not politeness.** Three things are
cold on a server that has just answered nothing: the first prefill and
whatever the runtime compiles with it, the prefix cache — which is the
very thing one suite here exists to measure — and this process's own
import and connection path. Charge any of them to whichever condition
happened to run first and you have measured the order of the loop. That
is not hypothetical: the first ad-hoc version of the client suite made a
shared client look 7% *slower* than a fresh one per call, on nothing but
running second. So every condition warms with its own payload, discards
it, and reports the median of repeated passes rather than one.

**No fallbacks, like everywhere else.** A backend that is not there fails
with the error the tool itself gives, naming the command that fixes it. A
benchmark that quietly measured something else would be a duration with
nothing behind it.

Run them with ``python -m benchmarks``.
"""
