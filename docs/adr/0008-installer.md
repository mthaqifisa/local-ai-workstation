# ADR-0008: Installer design
Status: Accepted (2026-10-19)

- `install.sh` (bash, stdlib only): requires Homebrew + Python; installs uv, then runs the `law` package from the repo with `uv run`.
- `law install` = ordered steps, each check → plan → apply → verify, guarded by a lock; state recorded in `~/LAW/state/install.json` and `owned.json`.
- Declarative inputs: `Brewfile`, `compose.yaml`, `settings/registry.yaml`, launchd templates.
- Six tested re-run guarantees (fresh, no-op, interrupted, damaged, settings diff, `~/LAW` deleted).
