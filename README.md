# Local AI Workstation

One command turns a fresh Apple Silicon Mac into a **local-first AI workstation**: local LLMs as the default brain, live web search, multiple coding agents, phone control over Telegram, optional GUI automation, and a live monitoring dashboard. Everything in the default path is free and open-source, and runs on your machine — no per-token costs, no data leaving the Mac unless you explicitly reach for a cloud backup.

> **Why local?** Unlimited inference at zero marginal cost. The local models do the work by default; an optional free OpenRouter key is wired in only as a backup for the occasional hard task.

-----

## What you get

|Goal                   |Tools                           |How it works                                     |
|-----------------------|--------------------------------|-------------------------------------------------|
|Build apps & scripts   |Aider, Cline, Continue (VS Code)|local models write and edit code                 |
|Up-to-date answers     |Open WebUI + SearXNG            |private web search wired into chat and agents    |
|Command from your phone|OpenClaw + Telegram             |DM a bot; it plans and executes tasks            |
|Control the Mac / GUI  |OpenClaw + Peekaboo             |mouse/keyboard/app control, with an approval gate|
|Monitor everything     |Live dashboard + Langfuse       |service health, hardware metrics, agent activity |

All services run behind a single **LiteLLM gateway** that exposes friendly model names over an OpenAI-compatible API and logs every call.

-----

## Requirements

- **macOS on Apple Silicon** (M-series).
- **32GB+ unified memory** (64GB recommended for the larger models). The MLX inference backend needs 32GB+ and auto-falls back to Metal below that.
- **~150GB free disk** for the full model set (pull fewer models to use less).
- Homebrew is installed automatically if missing.

-----

## Quick start

```bash
git clone https://github.com/mthaqifisa/local-ai-workstation.git
cd local-ai-workstation
bash setup_ai_workstation.sh
```

The script is interactive: it asks before installing, walks you through a few free tokens (Telegram, optional OpenRouter and Langfuse keys), and pauses where macOS requires manual permission grants. It is **safe to re-run** — healthy steps are skipped and broken ones are repaired.

When it finishes, open the dashboard:

```
http://localhost:8800
```

-----

## Commands

|Command                           |What it does                                                                        |
|----------------------------------|------------------------------------------------------------------------------------|
|`bash setup_ai_workstation.sh`    |Install / repair (converge to installed)                                            |
|`--status`                        |Show which services are running                                                     |
|`--start` / `--stop` / `--restart`|Manage all services                                                                 |
|`--update`                        |Pull the latest of everything (brew, models, Python tools, Docker images)           |
|`--reset`                         |Remove containers, services, and the workspace (models and Homebrew are left intact)|
|`--help`                          |Print the header documentation                                                      |

Services auto-start at login via `launchd`. Stopping preserves all data, models, and configs.

-----

## Services & ports

|Service        |Port |Purpose                                         |
|---------------|-----|------------------------------------------------|
|Live dashboard |8800 |health, hardware metrics, models, agent activity|
|Ollama         |11434|local model server                              |
|LiteLLM gateway|4000 |OpenAI-style routing over all models            |
|Open WebUI     |3001 |chat UI with web search                         |
|SearXNG        |8888 |private web search                              |
|Langfuse       |3000 |agent traces / logs                             |

-----

## Models

The model set is **accuracy-first and sized to fit 64GB unified memory**. Reasoning and coding are split across specialized models; the dashboard groups them by what they’re best at.

|Role                    |Model             |Notes                                       |
|------------------------|------------------|--------------------------------------------|
|Reasoning / orchestrator|`qwen3.6:35b-a3b` |MoE, ~24GB, strong benchmark profile        |
|Primary coder           |`qwen3.6:27b`     |dense, most accurate coder that fits        |
|Heavy agentic coder     |`qwen3-coder-next`|~46GB, run alone for repo-level work        |
|Agentic edits           |`devstral:24b`    |multi-file edits, tool calls, test-fix loops|
|Autocomplete            |`codestral:22b`   |fast fill-in-the-middle in the IDE          |
|Multimodal              |`gemma4:12b`      |text + image + audio + video                |
|Lightweight vision      |`qwen2.5vl:7b`    |reads screenshots for GUI control           |
|Embeddings              |`nomic-embed-text`|RAG / memory / search reranking             |

Edit the `MODELS` array near the top of the script to change the lineup. Model tags drift over time — if a pull fails, check [ollama.com/library](https://ollama.com/library) for the current tag.

### Apple Silicon performance (MLX)

On Apple Silicon, Ollama 0.19+ runs inference on Apple’s **MLX** backend (~2x faster than the older Metal path, and faster still on M5-class chips with GPU Neural Accelerators). The script checks the Ollama version and verifies the backend is active. There is no flag to force it — Ollama selects MLX automatically when the model architecture is supported and the machine has 32GB+ RAM.

-----

## Cloud backup (optional)

Local models are the default. If you add a free [OpenRouter](https://openrouter.ai) key, the gateway also exposes a couple of `:free`-tier cloud aliases (`openrouter-r1`, `openrouter-coder`) as a backup for hard tasks. The free tier is rate-limited (roughly 20 requests/min), so it’s for occasional use, not a primary path.

-----

## Security

By default, the web services bind to `0.0.0.0`, so other devices on your network (e.g. your phone) can reach them by your Mac’s LAN IP. This is convenient on a **trusted home Wi-Fi**, but the services are **unauthenticated** — anyone on the same network can reach them.

On shared or public networks:

- Change the `0.0.0.0` bindings back to `127.0.0.1` in the script, **or**
- Add a password to Open WebUI and a `master_key` to the LiteLLM gateway.

Never expose these ports to the public internet.

**Agents:** the included agent rules enforce a human-approval gate on high-impact actions. Keep it on, and **do not install third-party agent “skills” without reading their source** — community skill ecosystems have a poor security track record.

-----

## How it’s structured

- A dedicated Python venv runs the LiteLLM gateway and dashboard; Aider is installed in isolation (it pins an exact LiteLLM version that would otherwise conflict with the proxy).
- Docker services run via [Colima](https://github.com/abiosoft/colima). Because Colima only forwards container ports to localhost, lightweight `socat` bridges expose them on the LAN.
- The dashboard is a small Flask app reading live data from Ollama, the gateway, and Langfuse.
- Secrets live in `~/ai-workstation/.env` (chmod 600). The full workspace, including a quick-reference `README_AI.md`, is created under `~/ai-workstation`.

-----

## Honest limits

- **GUI/app control** (mouse, opening apps from a text command) is the least reliable part of any agent stack today, and local models drive it worse than frontier cloud models. Supervise it; keep approvals on.
- **Coding quality**: the best free local coders get close to frontier cloud models on many tasks but won’t fully match them on hard, multi-step agentic work. That’s what the optional cloud backup is for.
- **Model tags** reflect mid-2026 and may need updating as new releases land.

-----

## License

MIT. Provided as-is, without warranty. This script installs software, downloads large models, and registers background services — review it before running.
