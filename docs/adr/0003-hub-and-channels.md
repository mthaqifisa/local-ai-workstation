# ADR-0003: Hub and channels
Status: Accepted (2026-10-19)

- **Telegram** (primary channel): thin aiogram 3 adapter (MIT), long polling, allow-listed user IDs.
- **`law` CLI + `law` API** (FastAPI, `0.0.0.0:8100`): one job API used by CLI, Telegram and dashboards.
- **Open WebUI** (Docker on Colima, `0.0.0.0:3000`, `WEBUI_AUTH=False`): chat, RAG, tools, voice, image generation via ComfyUI.
- **Homepage** (`0.0.0.0:3010`): service status for all devices. **SearXNG** (`0.0.0.0:8888`): web search.
- No custom chat UI, gateway or dashboard (v4/v5 defect sources).
