# Bounds on the wire — 2026-09-30

Not produced by `python -m benchmarks`: a one-off check of why the package
sends `json_schema.unbounded_schema` rather than the model's own bounds. On
an xgrammar build, one `maxLength` took 30/30 successful requests to 0/30.

## Environment

| | |
| --- | --- |
| backend | Ollama 0.33.2 on an M1 Max, 64 GB |
| model | `vibe-sentinel-judge` (gemma4:26b-a4b-it-q8_0, digest `c226a3ce56d2`), num_ctx 32768 |
| thinking | off (`reasoning_effort = "none"`) |
| request | `SecretOpinion`, `json_schema` response format, `max_tokens` 1024, temperature 0.2 |
| method | 30 requests per arm, one at a time, 60 s client timeout; each answer validated against the Pydantic model |

The arms differ in one thing: the schema sent carries the Pydantic bounds
(`inline_schema`, which adds `maxLength` on `reason`) or does not
(`unbounded_schema`, what the package sends).

## Result

| arm | valid | invalid | timed out | median | slowest | longest reason |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| unbounded (what ships) | 30 | 0 | 0 | 1.20 s | 5.25 s | 149 chars |
| bounded | 30 | 0 | 0 | 1.03 s | 1.23 s | 166 chars |

No cost on this backend. Bounds stay off the wire anyway: the tool has to
work on backends that do take the slow path, and `clip_to_bounds` is what
keeps `answer_budget` arithmetic either way.
