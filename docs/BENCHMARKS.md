# Model benchmarks

Generated 2026-11-23 12:49 by `scripts/bench_models.py report` from raw results in `~/LAW/outputs/bench/`. Do not edit by hand. Medians of the timed runs; memory is the peak oMLX reported for loaded models. Tool-call rows have no decode figure (the call arrives as one chunk), so their TTFT is the whole turn; the Calls column says whether every run made the baseline's call. Protocol: ADR-0025.

| Config | Model | oMLX | Workload | Context | Decode tok/s | Prefill tok/s | TTFT s | Peak GB | Runs | Calls |
|---|---|---|---|---|---|---|---|---|---|---|
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | code | 4K | 9.7 | 439.6 | 7.9 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | code | 32K | 9.0 | 337.5 | 92.76 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | code | 64K | 8.4 | 304.7 | 205.1 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | prose | 4K | 9.6 | 330.2 | 10.24 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | prose | 32K | 9.0 | 338.3 | 92.2 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | prose | 64K | 8.4 | 304.3 | 205.14 | 30.98 | 3/3 | - |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | toolcall | 4K | - | 219.4 | 13.43 | 30.98 | 3/3 | ok |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | toolcall | 32K | - | 320.3 | 95.57 | 30.98 | 3/3 | ok |
| 27b-8bit-baseline | qwen3.8-27b-8bit | 0.8.0 | toolcall | 64K | - | 296.4 | 208.98 | 30.98 | 3/3 | ok |
