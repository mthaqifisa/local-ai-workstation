# HELP: skipped parts

Parts of LAW v6 that were skipped during the build because an automated safety check stopped the output.

**No open items.** All three were completed and verified:

| # | Part | Done | Where |
|---|---|---|---|
| 1 | Job API | 2026-10-19 (another session); 8 tests, file serving confined to `~/LAW/outputs` | `src/law/api.py` |
| 2 | `law save` / `law restore-state` (Open WebUI presets, prompts, tools, functions, knowledge definitions; secret scan before anything is written; add-only restore, also run by `law install`) | 2026-10-20; live round trip and secret-guard test on this Mac | `src/law/state_sync.py` |
| 3 | Song planner (theme, genre/vibe and duration in; lyrics written unless given; rendered with ACE-Step) | 2026-10-20; Whisper hears the planned chorus in the rendered song | `src/law/handlers/creative.py` |

Not built: a film planner (script to multi-shot video). Video generation itself works (`/video`, `/animate`).
