# Benchmark run — 2026-09-30

What this tool's model traffic costs on one machine. Not a gate and
not a threshold: read it as the evidence behind the `concurrency`
defaults, and re-run it on your own backend before trusting either.

## Environment

| | |
| --- | --- |
| date | 2026-09-30 |
| model | `vibe-sentinel-judge` |
| endpoint reports | serving: vibe-sentinel-judge:latest, gemma4:26b-a4b-it-q8_0, qwen3:14b |
| structured output | `json_schema` |
| max_tokens / temperature | 1024 / 0.2 |
| GPU | unknown (no nvidia-smi) |
| CPU | arm |
| Python | 3.13.12 |
| requests per pass | 16 |
| passes (median reported) | 3, after a discarded warm-up |
| note | Ollama 0.33.2 on an M1 Max, 64 GB; vibe-sentinel-judge (gemma4:26b-a4b-it-q8_0, digest c226a3ce56d2), num_ctx 32768, thinking off; OLLAMA_NUM_PARALLEL unset, runner started with -np 1 |

## concurrency

What does the fan-out width buy, on this backend?

16 requests per pass, one shared prefix, a fresh client per call — the tool's own shape, with only the semaphore moving.

| condition | seconds | req/s | median | slowest | vs first |
| --- | --- | --- | --- | --- | --- |
| concurrency = 1 | 14.84 ±7% | 1.08 | 0.87 | 1.06 | 1.00x |
| concurrency = 2 | 14.92 ±8% | 1.07 | 1.81 | 2.12 | 0.99x |
| concurrency = 4 | 14.29 ±7% | 1.12 | 3.54 | 4.01 | 1.04x |
| concurrency = 8 | 14.35 ±4% | 1.11 | 6.92 | 7.48 | 1.03x |
| concurrency = 16 | 14.41 ±9% | 1.11 | 7.80 | 15.46 | 1.03x |

## prefix

What does the byte-identical system message buy?

16 requests per pass at width 4. The arms differ in one thing: whether every request's system message is the same bytes.

| condition | seconds | req/s | median | slowest | vs first |
| --- | --- | --- | --- | --- | --- |
| one shared prefix (what the tool sends) | 14.52 ±4% | 1.10 | 3.58 | 3.90 | 1.00x |
| a different prefix per request | 40.36 ±1% | 0.40 | 9.99 | 10.41 | 0.36x |

## client

Is a client per call costing anything measurable?

16 requests per pass at width 4, one shared prefix. Constructing one httpx.AsyncClient measured at 4.8 ms on this machine — the cost the fresh-client arm pays per request.

| condition | seconds | req/s | median | slowest | vs first |
| --- | --- | --- | --- | --- | --- |
| a fresh client per call (what llm_query does) | 14.90 ±7% | 1.07 | 3.76 | 4.17 | 1.00x |
| one client for the whole fan-out | 15.52 ±6% | 1.03 | 3.82 | 4.21 | 0.96x |

## truncation

What does an answer that overruns its budget cost?

16 requests per pass at width 4, one shared prefix. The arms differ in one thing: the answer budget — 1024 tokens against 256, which the answers overrun so llm_query retries them at 512.  NOT MEASURED: only 0 of 48 requests in the 256-token arm truncated, which moves a median by less than the spread between passes. The answers are shorter than this budget assumes — lower [_TRUNCATING_TOKENS] and run it again.

| condition | seconds | req/s | median | slowest | vs first |
| --- | --- | --- | --- | --- | --- |
| max_tokens = 1024 (the answer fits) | 14.90 ±9% | 1.07 | 3.75 | 4.12 | 1.00x |
| max_tokens = 256 (the answer is cut off) | 15.59 ±10% | 1.03 | 3.81 | 4.40 | 0.96x |
