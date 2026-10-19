# ADR-0010: Computer use (full Mac control)
Status: Accepted (2026-10-19)

Agents control the Mac through an MCP server exposing: `osascript` (AppleScript/JXA), `shortcuts`, `open`, Homebrew casks, system volume/media, **Peekaboo** (MIT; screenshots + accessibility-tree click/type), `cliclick` fallback, and Playwright for the browser. A vision model (Qwen3.6-35B-A3B or Qwen3.8-27B) reads screenshots to plan and verify steps. TCC permissions (Accessibility, Screen Recording, Automation, Full Disk Access) are granted by the user once per install; `law doctor` verifies them.
