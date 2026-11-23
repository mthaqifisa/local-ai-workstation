# ADR-0008: Installer design
Status: Accepted (2026-10-19)

- `install.sh` (bash, stdlib only): requires Homebrew + Python; installs uv, then runs the `law` package from the repo with `uv run`.
- `law install` = ordered steps, each check → plan → apply → verify, guarded by a lock; state recorded in `~/LAW/state/install.json` and `owned.json`.
- Declarative inputs: `Brewfile`, `compose.yaml`, `settings/registry.yaml`, launchd templates.
- Six tested re-run guarantees (fresh, no-op, interrupted, damaged, settings diff, `~/LAW` deleted).

## Update 2026-11-21 (ADR-0024)

New steps: `opencode-pin` (npm `opencode-ai` at `pins.opencode` into `~/LAW/runtime/opencode`, linked into
`~/LAW/runtime/bin`, which is first on every LAW PATH) and `git-hooks` (ADR-0023). The Brewfile no longer lists
`opencode`, so Homebrew can't move it.
