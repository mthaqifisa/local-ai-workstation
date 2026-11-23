# Model benchmarks

Generated 2026-11-23 22:12 by `scripts/bench_models.py report` from raw results in `~/LAW/outputs/bench/`. Do not edit by hand. Medians of the timed runs; memory is the peak oMLX reported for loaded models. Tool-call rows have no decode figure (the call arrives as one chunk), so their TTFT is the whole turn; the Calls column says whether every run made the baseline's call. Protocol: ADR-0025.

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
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 4K | 23.2 | 421.4 | 8.24 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 32K | 20.5 | 328.0 | 95.44 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 64K | 18.7 | 294.9 | 211.89 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 4K | 18.0 | 334.5 | 10.1 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 32K | 16.5 | 332.5 | 93.8 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 64K | 14.9 | 291.6 | 214.05 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 4K | - | 274.4 | 10.75 | 31.5 | 3/3 | ok |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 32K | - | 322.6 | 94.91 | 31.5 | 3/3 | ok |
| 27b-oq8e-mtp-d2 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 64K | - | 290.3 | 213.39 | 31.5 | 3/3 | ok |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 4K | 25.4 | 360.5 | 9.63 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 32K | 23.0 | 328.1 | 95.4 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | code | 64K | 19.8 | 294.6 | 212.12 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 4K | 18.6 | 348.6 | 9.7 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 32K | 16.3 | 326.7 | 95.46 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | prose | 64K | 14.4 | 289.9 | 215.3 | 31.5 | 3/3 | - |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 4K | - | 294.9 | 10.0 | 31.5 | 3/3 | ok |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 32K | - | 321.0 | 95.35 | 31.5 | 3/3 | ok |
| 27b-oq8e-mtp-d3 | qwen3.8-27b-oq8e-mtp | 0.8.0 | toolcall | 64K | - | 291.4 | 212.55 | 31.5 | 3/3 | ok |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | code | 4K | 27.3 | 376.5 | 9.22 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | code | 32K | 24.6 | 333.5 | 93.85 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | code | 64K | 22.1 | 300.4 | 208.01 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | prose | 4K | 18.9 | 346.3 | 9.76 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | prose | 32K | 17.3 | 335.8 | 92.89 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | prose | 64K | 14.9 | 299.1 | 208.67 | 33.63 | 3/3 | - |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | toolcall | 4K | - | 288.8 | 10.21 | 33.63 | 3/3 | ok |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | toolcall | 32K | - | 326.9 | 93.65 | 33.63 | 3/3 | ok |
| 27b-8bit-dflash2-b4 | qwen3.8-27b-8bit | 0.8.0 | toolcall | 64K | - | 298.3 | 207.66 | 33.63 | 3/3 | ok |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | code | 4K | 26.3 | 363.7 | 9.54 | 33.63 | 3/3 | - |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | code | 32K | 24.2 | 357.1 | 87.29 | 33.63 | 3/3 | - |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | prose | 4K | 18.3 | 346.9 | 9.75 | 33.63 | 3/3 | - |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | prose | 32K | 16.5 | 361.6 | 85.97 | 33.63 | 3/3 | - |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | toolcall | 4K | - | 300.6 | 9.81 | 33.63 | 3/3 | ok |
| 27b-dflash-aneprefill | qwen3.8-27b-8bit | 0.8.0 | toolcall | 32K | - | 350.7 | 87.18 | 33.63 | 3/3 | ok |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | code | 4K | 70.1 | 2867.7 | 1.21 | 39.61 | 3/3 | - |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | code | 32K | 51.5 | 2241.6 | 13.91 | 39.61 | 3/3 | - |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | prose | 4K | 73.0 | 2885.9 | 1.17 | 39.61 | 3/3 | - |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | prose | 32K | 49.2 | 1892.6 | 16.43 | 39.61 | 3/3 | - |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | toolcall | 4K | - | 1803.4 | 1.64 | 39.61 | 3/3 | ok |
| 35b-a3b-8bit | qwen3.6-35b-a3b-8bit | 0.8.0 | toolcall | 32K | - | 1860.8 | 16.43 | 39.61 | 3/3 | ok |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | code | 4K | 110.6 | 3011.5 | 1.15 | 21.42 | 3/3 | - |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | code | 32K | 67.6 | 2106.5 | 14.8 | 21.42 | 3/3 | - |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | prose | 4K | 109.8 | 2347.4 | 1.44 | 21.42 | 3/3 | - |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | prose | 32K | 67.5 | 2080.7 | 14.94 | 21.42 | 3/3 | - |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | toolcall | 4K | - | 2574.0 | 1.15 | 21.42 | 3/3 | CHANGED |
| 35b-a3b-4bit | qwen3.6-35b-a3b-4bit | 0.8.0 | toolcall | 32K | - | 2051.1 | 14.91 | 21.42 | 3/3 | CHANGED |
