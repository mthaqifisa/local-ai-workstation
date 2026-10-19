# ADR-0007: Evaluation and judging
Status: Accepted (2026-10-19)

- A deliverable passes only on deterministic checks **and** an independent judge verdict.
- Judge = a different model family from the generator (Gemma 4 for Qwen output). During the build, Claude acts as an additional reviewer.
- Rubrics live in `evals/rubrics/*.yaml`; golden sets in `trained/evals/`; verdicts (JSON) in `~/LAW/outputs/test-artifacts/verdicts/`. Reports are generated from raw results only.
