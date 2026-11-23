# ADR-0028: OpenCode 2 deferred, LAW stays on OpenCode 1.x
Status: Accepted (2026-11-24)

## Context
OpenCode 2.4.0 was released on GitHub on 2026-11-23; npm `opencode-ai` still ships 1.x. LAW runs every agent through
`opencode run --format json` (1.18.7) and parses its event stream for progress lines, the stall guard, session
resume and the final answer.

## What was measured
Both versions ran the same coder task (write a file, run it, edit it, answer) on the live model, v2 isolated under
`~/LAW/runtime/opencode-v2` with its own data dirs and `--standalone`:

| | v1 1.18.7 (warm) | v2 2.4.0 |
|---|---|---|
| Wall time | 19.5 s | 17.2 s |
| Result | correct (5) | correct (5) |
| LAW's role config, `mac` MCP tools | loaded | loaded, no warnings |

Breaking changes found: `run` has no `--dir` (LAW passes it on every run; v2 uses the working directory), the CLI
connects to a background service unless `--standalone`, permissions need `--auto`, the `bash` tool is now `shell`,
and tool paths are relative (`add.py`, `.law/PLAN.md`) and use `path` instead of `filePath`.

## Decision
Stay on 1.x. v2 shows no measurable gain on LAW's workload (12 % on a 20 s task, within run-to-run noise), is one day
old and not yet on npm, and changes the CLI LAW drives. Proven improvements only.

What was kept so a later upgrade is cheap: both recorded streams are contract fixtures
(`tests/fixtures/opencode/v1`, `v2`, `tests/unit/test_opencode_contract.py`), and LAW's parsers already accept the v2
events (`shell`, relative `.law/` paths). Revisit when v2 is on npm and a few minor releases old: then the work is
`--dir` → `cwd`, `--standalone --auto`, and a Pantry ticket replay on a copy before any real ticket (owner's yes).
