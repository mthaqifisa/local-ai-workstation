# ADR-0025: DFlash speculative decoding on the 27B, chosen by benchmark

Status: accepted (owner yes, 2026-11-23)

## Context
The 27B 8-bit decoded at 8-10 tok/s on the M5 Pro (64 GB), which made long agent tickets slow. oMLX 0.8.0 offers two
ways to draft tokens that the model then verifies: MTP (the model's own multi-token-prediction heads, which needs a
quant that keeps them) and DFlash 2 (a separate 3.8 GB block-diffusion drafter, `z-lab/Qwen3.8-27B-DFlash2`).

## Protocol
`scripts/bench_models.py` runs each configuration on oMLX: code, prose and a tool-call fixture at 4K, 32K and 64K
context, three timed runs each, medians reported. A candidate is adopted only if decode is at least 25% faster at
4K, not slower at 32K, every tool call matches the baseline's, and the golden eval stays 10/10.

Brief corrections found while doing this:
- Per-model settings live in `~/LAW/runtime/omlx/model_settings.json`. The admin API needs a key LAW doesn't set.
- The tool-call fixture had to be rebuilt from a recorded agent turn.
- Tool-call replies arrive as one chunk, so they have a call check instead of a decode rate.

## Results (docs/BENCHMARKS.md)
| Config | Code 4K | Code 32K | Code 64K | Prose 4K | Peak GB |
|---|---|---|---|---|---|
| 8-bit baseline | 9.7 | 9.0 | 8.4 | 9.6 | 31.0 |
| oQ8e + MTP depth 2 | 23.2 | 20.5 | 18.7 | 18.0 | 31.5 |
| oQ8e + MTP depth 3 | 25.4 | 23.0 | 19.8 | 18.6 | 31.5 |
| 8-bit + DFlash 2, block 4 | **27.3** | **24.6** | **22.1** | **18.9** | 33.6 |

The time to the first token was unchanged in every configuration. All tool calls matched the baseline. DFlash's
golden eval scored 10/10.

## Decision
- DFlash 2 on the existing 8-bit model: the registry's `draft:` and `omlx:` keys, applied by the omlx-settings
  install step.
- The MTP quant (28 GB) is deleted. No new main model was needed.

## Later measurements (2026-11-23)
- **Two requests at once** on the 27B with DFlash: about 1.3x total output at 4K, none at 32K. Prefill runs one
  request at a time and dominates long inputs, so parallel tickets are not worth building.
- **SpecPrefill** (oMLX): rejected without a run. It keeps only part of the prompt (a draft model picks about 20% of
  the tokens), so the model does not see the whole input: a quality loss by design.
- **ANE prefill** (Neural Engine): time to first token 87 s instead of 94 s at 32K (about 7%), no change at 4K, and
  part of the work runs on an approximate path (oMLX log). Not adopted: too small a gain for a possible accuracy cost.
- Conclusion: prefill on this Mac is near its limit; time is saved by agents wasting fewer steps.

## Follow-up
TensorFold (Apache-2.0) reports about 2x oMLX's DFlash figures on the same chip; the owner chose to skip it
(2026-11-23): young, self-reported gains, no proof on this Mac.
