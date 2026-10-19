# HELP: skipped parts

Parts of LAW v6 skipped during the build because an automated safety check stopped the output.
Each row names the missing file and where to read the context.

| # | Skipped part | File | Context to read |
|---|---|---|---|
| 3 | Song and film planning: `song` / `film` job kinds that turn a plain request (duration, theme, genre/vibe, user lyrics or script) into exact music/video settings via a writer planning stage, then render with the `music` / `video` / `animate` handlers | `src/law/handlers/creative.py` (not present) | `src/law/handlers/media.py`, `src/law/handlers/core.py` (`answer`, `build_messages`), `src/law/app.py` (resolver), `src/law/telegram_bot.py` (`/music`, `/video`, `/animate` currently map straight to music/video) |
| 2 | `law save` / `law restore-state`: export Open WebUI presets, prompts and knowledge definitions into `v6/settings/openwebui/` and `v6/knowledge/`, and import them on install (secret scan before commit) | `src/law/state_sync.py` (not present) | `PROMPT.md` §1 rule 1 and P3 "Portable state", `src/law/ops.py`, `compose.yaml`, `src/law/secrets.py` |

Open items above. (Item 1, `src/law/api.py`, was completed in another session and verified on 2026-10-19:
8 tests passing, lint clean, file serving confined to `~/LAW/outputs`.)
