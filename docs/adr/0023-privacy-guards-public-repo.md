# ADR-0023: Personal data out of the public repo, and guards that keep it out

Status: accepted, 2026-11-20

## Context

The repo is public. Its history carried the owner's personal email (author field on most commits and defaults in the
v2/v3/v3.1 setup scripts), the macOS username in home paths (v2 script, v4 agent logs), the Mac's hostname (author
emails) and the home LAN subnet (v5 docs and tests). CLAUDE.md said gitleaks ran in pre-commit and CI, but only
`law save` ran it: there was no pre-commit hook and no CI workflow, and gitleaks' default rules do not look for
personal data at all.

## Decision

- **History**: each branch tip was fixed properly first (the setup scripts take `MASTER_EMAIL` from the environment or
  `.env` with no default, and v2's paths come from its existing `$AI_WORKSPACE`), then all 8 branches were rewritten
  with git filter-repo (mailmap to the GitHub noreply identity, text replacement in files and messages) and
  force-pushed. Quoted commit SHAs in PROGRESS.md were remapped from the filter-repo commit map. The pre-rewrite
  mirror is kept as a bundle in `~/LAW/backups/`, never pushed.
- **Rules**: `.gitleaks.toml` extends the default rules with `personal-email`, `home-path`, `mac-hostname` and
  `lan-address`. They describe shapes, never the owner's values, and allow only the generic examples.
- **Where they run**: committed hooks in `.githooks/` (set by the installer's `git-hooks` step through
  `core.hooksPath`): pre-commit (staged content and the committer identity), commit-msg, and pre-push (content,
  messages and author/committer of every pushed commit, then Git LFS). CI runs the same checks over all history.
  `law save` passes the config to its gitleaks scan.
- **Runtime writers**: `law.paths.display()` shows the home folder as `~`; `law.privacy.scrub()` also replaces the
  owner's email and Telegram IDs, read at runtime from `~/LAW/state/`. `law save` and `scripts/test_report.py`
  (docs/TEST-REPORT.md) scrub before writing.

## Facts that differed from the brief (the installed tools win)

- v2 already had `AI_WORKSPACE` for the output folder, so it was reused instead of adding `AI_AGENT_DIR`.
- gitleaks uses Go RE2, which has no lookahead, so the exceptions are allowlist regexes on the match.
- gitleaks' default global allowlist skips any whole match shaped like `/home/...`, which hid `/home/<name>` paths.
  The `home-path` rule therefore reports the user name (`secretGroup = 1`).
- The hostname only ever appeared in commit metadata, which gitleaks does not scan. The identity checks in the
  pre-commit and pre-push hooks and in CI cover it.
- The `.gitleaksignore` fingerprint for the v4 QA log was replaced by a path allowlist scoped to `curl-auth-header`,
  so a future rewrite cannot break it.

## Consequences

- Every commit SHA changed once. Old clones must be re-cloned or reset; GitHub still serves the old commits through
  the closed PRs' refs and cached views until GitHub Support purges them (owner's request, drafted 2026-11-20).
- Test fixtures that need a realistic positive build it at runtime, so the test files pass the same rules.
- A false positive is fixed by tightening a rule in `.gitleaks.toml` with a test, not by an ad-hoc ignore.
