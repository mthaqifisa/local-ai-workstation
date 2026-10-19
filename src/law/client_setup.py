"""Files this Mac serves so another machine on the same network can use it as an offline AI server.

GET /client/install.sh     -> sets up OpenCode on the other machine to use this Mac's models (no internet needed)
GET /client/opencode.json  -> the OpenCode config, with this Mac's address as the other machine sees it
"""

from __future__ import annotations

import json
from typing import Any

from law import registry, roles
from law.paths import LawPaths

OFFLINE_ENV = {
    "OPENCODE_DISABLE_MODELS_FETCH": "1",   # don't fetch the online model catalogue
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",   # don't try to download language servers
    "OPENCODE_DISABLE_SHARE": "1",
}


def opencode_config(paths: LawPaths, host: str) -> dict[str, Any]:
    """OpenCode config for a remote machine: this Mac's endpoint, the coder model for both slots, no Mac tools."""
    reg = registry.load(paths.registry_file)
    loaded = roles.load(paths.settings / "roles.yaml")
    coder = reg.model_for_role(loaded.get("coder").model_role)
    base = f"http://{host}:{reg.ports.llm_public}/v1"
    models = {m.id: {"name": m.id, "limit": {"context": m.context or 32768, "output": 16384}}
              for m in reg.llm_models if m.lane == "heavy"}
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"law": {"npm": "@ai-sdk/openai-compatible", "name": "LAW (home AI server)",
                             "options": {"baseURL": base, "apiKey": "local"}, "models": models}},
        "model": f"law/{coder.id}",
        "small_model": f"law/{coder.id}",  # one large model at a time on the server
        "autoupdate": False,
        "share": "disabled",
        "instructions": [],
        "agent": {"build": {"prompt": loaded.system_prompt("coder")}},
    }


def install_script(server_host: str, api_port: int, llm_port: int) -> str:
    env_lines = "\n".join(f'export {k}={v}' for k, v in OFFLINE_ENV.items())
    return f"""#!/bin/sh
# LAW client setup: use the AI on {server_host} from this machine (works with no internet).
set -eu
SERVER="{server_host}"
CONF_DIR="$HOME/.config/law-client"
mkdir -p "$CONF_DIR"

echo "Checking the AI server at $SERVER ..."
if ! curl -fsS "http://$SERVER:{llm_port}/v1/models" >/dev/null; then
  echo "Cannot reach http://$SERVER:{llm_port}. Are both machines on the same Wi-Fi?" >&2
  exit 1
fi
curl -fsS "http://$SERVER:{api_port}/client/opencode.json" -o "$CONF_DIR/opencode.json"

# The user's login shell decides the profile (on macOS /bin/sh is bash even when the user runs zsh).
case "$(basename "${{SHELL:-/bin/zsh}}")" in
  zsh) PROFILE="$HOME/.zshrc" ;;
  bash) PROFILE="$HOME/.bashrc" ;;
  *) PROFILE="$HOME/.profile" ;;
esac
if ! grep -q "law-client" "$PROFILE" 2>/dev/null; then
  cat >> "$PROFILE" <<'PROFILE_EOF'
# >>> law-client >>>
export OPENCODE_CONFIG="$HOME/.config/law-client/opencode.json"
{env_lines}
export LAW_SERVER="http://{server_host}:{api_port}"
# <<< law-client <<<
PROFILE_EOF
fi

if command -v opencode >/dev/null 2>&1; then
  echo "Done. Open a new terminal, cd into your project and run: opencode"
else
  echo "Config saved. OpenCode is not installed yet: install it while you have internet"
  echo "(brew install opencode, or npm install -g opencode-ai), then run: opencode"
fi
"""


def as_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"
