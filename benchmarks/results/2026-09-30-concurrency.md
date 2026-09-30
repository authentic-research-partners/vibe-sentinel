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
| note | Ollama 0.33.2 on an M1 Max, 64 GB; vibe-sentinel-judge (gemma4:26b-a4b-it-q8_0, digest c226a3ce56d2), num_ctx 32768, thinking off; OLLAMA_NUM_PARALLEL=4 |

## concurrency

What does the fan-out width buy, on this backend?

16 requests per pass, one shared prefix, a fresh client per call — the tool's own shape, with only the semaphore moving.

| condition | seconds | req/s | median | slowest | vs first |
| --- | --- | --- | --- | --- | --- |
| concurrency = 1 | 15.57 ±7% | 1.03 | 0.98 | 1.11 | 1.00x |
| concurrency = 2 | 13.37 ±3% | 1.20 | 1.65 | 2.29 | 1.16x |
| concurrency = 4 | 12.47 ±37% | 1.28 | 3.21 | 6.34 | 1.25x |
| concurrency = 8 | 11.76 ±12% | 1.36 | 5.61 | 6.93 | 1.32x |
| concurrency = 16 | 10.95 ±17% | 1.46 | 7.05 | 12.17 | 1.42x |
