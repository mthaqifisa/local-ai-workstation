# ADR-0007: Evaluation and judging
Status: Accepted (2026-10-19)

- A deliverable passes only on deterministic checks **and** an independent judge verdict.
- Judge = a different model family from the generator (Gemma 4 for Qwen output). During the build, Claude acts as an additional reviewer.
- Rubrics live in `evals/rubrics/*.yaml`; golden sets in `trained/evals/`; verdicts (JSON) in `~/LAW/outputs/test-artifacts/verdicts/`. Reports are generated from raw results only.

## Revision 2026-11-24: judge A/B (E5), Gemma stays
`scripts/judge_calibration.py`, 30 graded answers (20 with a planted flaw, 10 good), 3 repeats per judge:

| Judge | Catch % | False-fail % | Agreement % | Median s |
|---|---|---|---|---|
| gemma-4-31b-8bit (live) | 65.0 | 16.7 | 93.3 | 33.4 |
| muse-glimmer-30b-8bit | 80.0 | 26.7 | 83.3 | 202.6 |

Switch rule: catch ≥ +10 points **and** false-fail not higher **and** agreement not lower **and** time ≤ 1.5×.
Glimmer catches more but fails good work more often, is less consistent between repeats and is 6× slower, so it fails
three of the four conditions. Gemma 4 stays the judge; Glimmer was deleted. Raw verdicts: `~/LAW/outputs/judge/`.
