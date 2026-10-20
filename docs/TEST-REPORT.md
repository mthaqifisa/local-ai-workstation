# Test report

Generated 2026-10-20 09:53 on arm64 macOS 27.0.1 by `scripts/test_report.py` from raw results in `~/LAW/outputs/test-artifacts/`. Do not edit by hand.

## Engineering gates

| Layer | Result | Tests | Failures | Skipped | Time (s) |
|---|---|---|---|---|---|
| lint (ruff) | PASS |  |  |  | 0.0 |
| types (mypy --strict) | PASS |  |  |  | 0.4 |
| unit | PASS | 181 | 0 | 0 | 10.2 |
| integration | PASS | 9 | 0 | 0 | 32.7 |
| integration-heavy (media) | PASS | 5 | 0 | 0 | 3018.7 |
| installer re-run (expect 0 changes) | PASS |  |  |  | 4.5 |

## Capability evaluation (golden set, independent judge)

Latest result per case from `eval-20261019-213459.json`, `eval-20261020-074822.json`. **10/10 passed.**

| Case | Domain | Result | Checks | Judge | Time (s) |
|---|---|---|---|---|---|
| calc-compound-interest | computation | PASS | number ✓ | n/a | 77.3 |
| calc-loan-payment | computation | PASS | number ✓ | n/a | 150.8 |
| calc-statistics | computation | PASS | number ✓ | n/a | 143.5 |
| reason-logic | reasoning | PASS | any ✓ | n/a | 28.6 |
| reason-base-rate | reasoning | PASS | number ✓ | n/a | 170.5 |
| judgement-vendor | judgement | PASS | must ✓, judge ✓ | 5/5 | 1094.2 |
| doc-exec-memo | documentation | PASS | must ✓, judge ✓ | 5/5 | 1854.4 |
| code-function | coding | PASS | must ✓, judge ✓ | 5/5 | 255.2 |
| code-sql | coding | PASS | must ✓, judge ✓ | 5/5 | 184.0 |
| design-critique | design | PASS | must ✓, judge ✓ | 5/5 | 793.4 |
