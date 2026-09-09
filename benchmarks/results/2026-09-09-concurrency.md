# Benchmark run — 2026-09-09

What this tool's model traffic costs on one machine. Not a gate and
not a threshold: read it as the evidence behind the `concurrency`
defaults, and re-run it on your own backend before trusting either.

## Environment

| | |
| --- | --- |
| date | 2026-09-09 |
| model | `qwen3-8b-fp8` |
| endpoint reports | serving: qwen3-8b-fp8 |
| structured output | `json_schema` |
| max_tokens / temperature | 1024 / 0.2 |
| GPU | NVIDIA RTX 4000 Ada Generation, 20475 MiB |
| CPU | Intel(R) Core(TM) i7-14700 |
| Python | 3.13.2 |
| requests per pass | 64 |
| passes (median reported) | 3, after a discarded warm-up |
| note | vLLM 0.18.0 serving RedHatAI/Qwen3-8B-FP8-dynamic under podman: --kv-cache-dtype fp8 --max-model-len 40960 --max-num-seqs 256 --max-num-batched-tokens 8192 --gpu-memory-utilization 0.85; the engine reports a 98,064-token GPU KV cache, and one request of this shape occupies about 2,300 of them, so the cache runs out somewhere past 40 concurrent. Widths 1 and 2 are omitted deliberately: measured at 207.64s +-1.1% and 101.02s +-6.6% over 16 requests, they cost two thirds of the runtime to re-confirm, so the baseline here is the shipped default of 4 |

## concurrency

What does the fan-out width buy, on this backend?

64 requests per pass, one shared prefix, a fresh client per call — the tool's own shape, with only the semaphore moving.

| condition | seconds | req/s | median | slowest | vs first | cached |
| --- | --- | --- | --- | --- | --- | --- |
| concurrency = 4 | 185.87 ±4% | 0.34 | 10.01 | 25.14 | 1.00x | 92% |
| concurrency = 8 | 103.07 ±4% | 0.62 | 10.81 | 29.77 | 1.80x | 93% |
| concurrency = 16  (1 truncated+retried) | 67.64 ±10% | 0.95 | 12.96 | 48.80 | 2.75x | 92% |
| concurrency = 32  (2 truncated+retried) | 68.16 ±34% | 0.94 | 17.26 | 49.01 | 2.73x | 92% |
| concurrency = 64 | 41.96 ±6% | 1.53 | 25.77 | 42.40 | 4.43x | 92% |
