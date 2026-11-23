# ADR-0024: Runtime pins and the v6.1 runtime upgrade

Status: accepted, 2026-11-21

## Context

The Brewfile installed `opencode` unpinned, so a fresh Mac would get whatever Homebrew had that day. oMLX and Open WebUI
had newer releases with speed and security fixes. There was no single place that said which runtime versions LAW needs.

## Decision

- `settings/registry.yaml` gets a `pins:` block (`opencode`, `omlx`). The brief proposed a new `tools:` block, but
  `tools:` already lists LAW's Python tools, so the pins got their own block.
- OpenCode stays on 1.x (1.18.7, the version the agents were tuned on) and installs from npm `opencode-ai` through the
  `opencode-pin` step, with `autoupdate: false` as before. OpenCode 2 needs its own gates (brief E6).
- oMLX goes to 0.8.0 with `with-custom-kernel`. Open WebUI goes to 0.12.5, pinned by digest. 0.13.0 needs a DB
  migration and is left in the backlog.
- `law doctor` shows both pinned versions and fails on a mismatch.

## Facts that differed from the brief

- Homebrew's `opencode` was at 1.19.6, not 2.x. Pinning still matters, because the formula moves without LAW.
- Building oMLX 0.8.0 with the custom kernels needs Xcode's Metal toolchain (`xcodebuild -downloadComponent MetalToolchain`),
  which this Mac didn't have.
- LAW has no `/v1/messages` caller and its oMLX settings use no renamed keys, so those 0.8.0 notes need no change.

## Consequences

- The Homebrew copy of opencode stays installed (removing brew packages needs the owner) but is shadowed by
  `~/LAW/runtime/bin/opencode`.
