# Local AI Workstation

One-command setups that turn an Apple Silicon Mac (ideally 64 GB+) into a fully local,
private AI workstation: open models running on your own hardware, a multi-agent team you
can drive from Telegram, web search, and a live dashboard. No API keys, no cloud
inference, no data leaving your machine.

Each branch is a different take on the setup, from the first OpenClaw-based version to
the current MLX-native stack.

## Versions

> ⭐ **Recommended: [v6-simplified-mlx](https://github.com/mthaqifisa/local-ai-workstation/tree/v6-simplified-mlx).** It's the newest, simplest and highest-quality version. Start there.

| Version | Engine | What's different | Branch |
|---|---|---|---|
| ⭐ **v6 (recommended)** | oMLX | Simplified rewrite. One durable job queue with a memory-aware scheduler swaps big models in and out. Answers go through a **draft → independent review → revise** loop where a different model family does the review. Covers chat, coding agents, image, speech, music and video, all served from Telegram, CLI, browser and OpenAI/Anthropic-compatible APIs | [**v6-simplified-mlx**](https://github.com/mthaqifisa/local-ai-workstation/tree/v6-simplified-mlx) |
| v5 | MLX | Reworked v4 with a self-repairing `./install.sh` and the `law` CLI. Agents get real tools (shell, GUI control, vision), plus a QA/design quality loop, a command-center dashboard, a conversational Telegram bot (voice and photo), a watchdog, a memory guard and undo snapshots | [v5-enhanced-mlx](https://github.com/mthaqifisa/local-ai-workstation/tree/v5-enhanced-mlx) |
| v4 | MLX | Moves inference from Ollama to **mlx-openai-server** on Apple Metal. Loads one model at a time so 40 GB+ models fit on 64 GB. Adds 16 agent personas, autonomous SDLC, and media generation (image, TTS, video, vision) | [v4-mlx](https://github.com/mthaqifisa/local-ai-workstation/tree/v4-mlx) |
| v3.1 | Ollama | v3 with an enhanced dashboard (subagent monitoring, Ollama memory tracking, LAN IP detection) and automatic cleanup of older setups | [v3.1-enhanced-v3](https://github.com/mthaqifisa/local-ai-workstation/tree/v3.1-enhanced-v3) |
| v3 | Ollama | Combines v1 and v2. `setup.sh` provisions the infrastructure (Ollama, LiteLLM, SearXNG, Langfuse, dashboard), and the 7-agent team lives inside **OpenClaw** via a copy-paste prompt | [v3-combine-v1-and-v2](https://github.com/mthaqifisa/local-ai-workstation/tree/v3-combine-v1-and-v2) |
| v2 | Ollama | Drops OpenClaw for a **custom Python orchestrator**. Orion (a Telegram bot) leads a 7-agent dev team (PM, designer, dev, QA, pentester, trend watcher), with a Jira-style dashboard | [v2-pythonScript](https://github.com/mthaqifisa/local-ai-workstation/tree/v2-pythonScript) |
| v1 | Ollama | The first version. **OpenClaw** plus Telegram for phone control, coding agents (Aider, Cline, Continue), Open WebUI with SearXNG, Peekaboo GUI automation, and an optional OpenRouter fallback | [v1-openClaw](https://github.com/mthaqifisa/local-ai-workstation/tree/v1-openClaw) |

Setup steps, requirements and the model lineup for each version are in that branch's README.
