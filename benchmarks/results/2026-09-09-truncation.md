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
| requests per pass | 16 |
| passes (median reported) | 3, after a discarded warm-up |
| note | vLLM 0.18.0 serving RedHatAI/Qwen3-8B-FP8-dynamic under podman: --kv-cache-dtype fp8 --max-model-len 40960 --max-num-seqs 256 --max-num-batched-tokens 8192 --gpu-memory-utilization 0.85; the engine reports a 98,064-token GPU KV cache, and one request of this shape occupies about 2,300 of them, so the cache runs out somewhere past 40 concurrent. Widths 1 and 2 are omitted deliberately: measured at 207.64s +-1.1% and 101.02s +-6.6% over 16 requests, they cost two thirds of the runtime to re-confirm, so the baseline here is the shipped default of 4 |

## truncation

What does an answer that overruns its budget cost?

16 requests per pass at width 4, one shared prefix. The arms differ in one thing: the answer budget — 1024 tokens against 256, which the answers overrun so llm_query retries them at 512.  NOT MEASURED: 8 request(s) in the 256-token arm never came back — the doubled retry at 512 truncated too, and llm_query doubles once. That timed a failure, not a retry. Raise [_TRUNCATING_TOKENS].

| condition | seconds | req/s | median | slowest | vs first | cached |
| --- | --- | --- | --- | --- | --- | --- |
| max_tokens = 1024 (the answer fits) | 50.38 ±22% | 0.32 | 10.22 | 25.23 | 1.00x | 97% |
| max_tokens = 256 (the answer is cut off)  (8 unanswered, 56 truncated+retried) | 80.75 ±11% | 0.20 | 18.10 | 23.04 | 0.62x | 99% |
