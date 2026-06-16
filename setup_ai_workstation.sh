#!/usr/bin/env bash
# Re-exec under bash if started by a non-bash shell (sh/dash choke on the arrays below).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
# =============================================================================
#  LOCAL AI WORKSTATION  —  fresh-Mac bootstrap  (Apple Silicon / M5 Pro / 64GB)
# -----------------------------------------------------------------------------
#  GOAL: a fully local-first AI workstation that can:
#    1. Build web apps / native apps / scripts using LOCAL models (Aider, Continue, Cline).
#    2. Answer questions with UP-TO-DATE info from the internet (SearXNG web search
#       wired into Open WebUI and the agent).
#    3. Be driven from your phone over Telegram (OpenClaw agent).
#    4. Make changes to the Mac & control GUI apps (OpenClaw + Peekaboo) — with a
#       human-approval gate on every high-impact action.
#    5. Show a live DASHBOARD of every service + recent agent activity.
#
#  EVERYTHING HERE IS FREE / OPEN-SOURCE OR HAS A FREE TIER. No paid keys required.
#
#  LOCAL IS PRIMARY. The local Ollama models are the default brain for every task.
#  The optional OpenRouter free key is BACKUP only — used when you explicitly reach for it.
#
#  RE-RUNNABLE: run this script as many times as you like. Every step checks whether it
#  already succeeded and skips it; half-finished/broken pieces are rolled back and redone.
#
#  REQUIREMENTS: macOS on Apple Silicon (M-series) with 32GB+ unified memory (64GB
#  recommended for the larger models). ~150GB free disk for the full model set.
#  The MLX inference backend needs Ollama 0.19+ and 32GB+ RAM (auto-falls back below).
#
#  CONTROL:  --status | --start | --stop | --restart | --update | --reset | --help
#
#  SECURITY: by default the web services bind 0.0.0.0 (reachable across the local
#  network). This is convenient on a trusted home Wi-Fi but exposes unauthenticated
#  services. On shared/public networks, change the 0.0.0.0 bindings back to 127.0.0.1.
#
#  LICENSE: MIT. Provided as-is, without warranty. Review before running; it installs
#  software, downloads models, and registers launchd services.
#
#  OFFICIAL SOURCES (verify before trusting; look-alikes exist):
#    Ollama ollama.com  ·  Open WebUI github.com/open-webui/open-webui
#    Aider aider.chat  ·  Continue continue.dev  ·  Cline github.com/cline/cline
#    OpenClaw github.com/openclaw/openclaw  ·  Peekaboo peekaboo.boo
#    SearXNG github.com/searxng/searxng  ·  Langfuse github.com/langfuse/langfuse
#    LiteLLM github.com/BerriAI/litellm  ·  Colima github.com/abiosoft/colima
# =============================================================================
set -uo pipefail   # NOT -e: optional steps must continue on failure.

# ----------------------------- USER CONFIG -----------------------------------
WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
ENV_FILE="$WORKDIR/.env"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

OLLAMA_MAX_LOADED="${OLLAMA_MAX_LOADED:-1}"

PORT_OLLAMA=11434; PORT_OPENWEBUI=3001; PORT_LANGFUSE=3000
PORT_SEARXNG=8888; PORT_GATEWAY=4000;  PORT_DASHBOARD=8800

# Models — ACCURACY-FIRST selection that fits 64GB. EDIT tags at https://ollama.com/library.
MODELS=(
  "qwen3.6:35b-a3b|MAX-ACCURACY reasoning that fits 64GB (MoE 35B/3B active, ~24GB); newer than 3.5, stronger benchmark profile"
  "qwen3.6:27b|PRIMARY coder: best DENSE model (77.2% SWE-bench, ~22GB at Q6) — most accurate coder that fits"
  "qwen3-coder-next|dedicated agentic coding (80B MoE, ~46GB); fits 64GB for code-only sessions"
  "devstral:24b|agentic coding: multi-file edits, tool calls, test-fix loops (~16GB)"
  "codestral:22b|fast FIM autocomplete for the IDE"
  "gemma4:12b|MULTIMODAL (text+image+audio+video, Apache-2.0, 256K ctx) — best free vision/omni that fits"
  "qwen2.5vl:7b|lightweight vision fallback: reads screenshots for GUI control"
  "nomic-embed-text|embeddings for memory / RAG / web-search reranking"
)

# ----------------------------- PRETTY LOGGING --------------------------------
c_reset=$'\033[0m'; c_blue=$'\033[1;34m'; c_grn=$'\033[1;32m'; c_yel=$'\033[1;33m'; c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'
log()  { printf "\n%s %s\n" "${c_blue}==>${c_reset}" "$*"; }
ok()   { printf "%s %s\n" "${c_grn}  ok${c_reset}" "$*"; }
warn() { printf "%s %s\n" "${c_yel}  ! ${c_reset}" "$*"; }
err()  { printf "%s %s\n" "${c_red}  x ${c_reset}" "$*" 1>&2; }
have() { command -v "$1" >/dev/null 2>&1; }
opt()  { "$@" || warn "non-fatal failure: $*"; }
hr()   { printf "%s\n" "${c_cyn}--------------------------------------------------------------------${c_reset}"; }

# ----------------------------- .env helpers ----------------------------------
ensure_env_file() { mkdir -p "$WORKDIR"; [ -f "$ENV_FILE" ] || : > "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
get_env() { ensure_env_file; sed -n "s/^$1=//p" "$ENV_FILE" | head -n1; }
set_env() {
  ensure_env_file
  local key="$1" val="$2" tmp; tmp="$(mktemp)"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"; export "$key=$val"
}
load_env() { ensure_env_file; set -a; . "$ENV_FILE"; set +a; }

# ----------------------------- validators / prompts --------------------------
validate_telegram() { have curl || return 0; curl -fsS "https://api.telegram.org/bot$1/getMe" 2>/dev/null | grep -q '"ok":true'; }
validate_openrouter() { have curl || return 0; [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "https://openrouter.ai/api/v1/key" 2>/dev/null)" = "200" ]; }
press_enter() { printf "\n%sPress Enter when ready to continue...%s" "$c_yel" "$c_reset"; read -r _; }

prompt_secret() {  # prompt_secret VAR "Title" validator_fn tutorial_fn
  local var="$1" title="$2" validator="$3" tut_fn="$4" current
  current="$(get_env "$var")"
  if [ -n "$current" ]; then
    if [ -z "$validator" ] || "$validator" "$current"; then ok "$title already set."; return 0; fi
    warn "$title set but failed validation; re-enter."
  fi
  [ -n "$tut_fn" ] && { hr; "$tut_fn"; hr; }
  local tries=0 val=""
  while :; do
    printf "%sPaste %s and press Enter (or 'skip'): %s" "$c_cyn" "$title" "$c_reset"; read -r val
    case "$val" in skip|SKIP) warn "Skipped $title."; return 0 ;; esac
    [ -z "$val" ] && { warn "Empty - try again."; continue; }
    if [ -z "$validator" ] || "$validator" "$val"; then break; fi
    tries=$((tries+1)); [ "$tries" -ge 3 ] && { warn "Couldn't validate; saving as-is."; break; }
    warn "Didn't validate. Try again."
  done
  set_env "$var" "$val"; ok "$title saved."
}

tut_telegram() {
cat <<TUT
${c_cyn}#############  ACTION: TELEGRAM BOT TOKEN  #############${c_reset}
  1. Open Telegram, search @BotFather (official, blue check).
  2. Send /newbot ; give it a name and a username ending in 'bot'.
  3. Copy the token it returns (looks like 123456789:ABCdEf...).
  (After setup you DM this bot to command your agent.)
${c_cyn}#######################################################${c_reset}
TUT
}
tut_openrouter() {
cat <<TUT
${c_cyn}#############  ACTION: OPENROUTER FREE API KEY (optional)  #############${c_reset}
  1. Open https://openrouter.ai and sign in (email or GitHub; no credit card needed).
  2. Go to Keys: https://openrouter.ai/workspaces/default/keys
  3. Click "Create Key", name it, and copy it once (starts with sk-or-...).
  4. Free tier gives ~20 requests/min and 50-1000/day across many ':free' models
     (DeepSeek, Llama, Qwen3-Coder, Gemma, etc.). Great as an occasional backup.
${c_cyn}#######################################################################${c_reset}
TUT
}

# ----------------------------- health probes ---------------------------------
http_ok()  { have curl && curl -fsS -m 4 "$1" >/dev/null 2>&1; }
docker_up(){ have docker && docker info >/dev/null 2>&1; }
container_running(){ docker_up && [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

ensure_container() {
  local name="$1"; shift
  if container_running "$name"; then return 0; fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  "$@"
}

# =============================================================================
#  PHASE 0 — PREFLIGHT
# =============================================================================
preflight() {
  log "Preflight"
  [ "$(uname -s)" = "Darwin" ] || { err "macOS only."; exit 1; }
  [ "$(uname -m)" = "arm64" ] || warn "Expected Apple Silicon; got $(uname -m)."
  ok "macOS $(sw_vers -productVersion 2>/dev/null) on $(uname -m)"
  ensure_env_file
  cat <<BANNER

${c_yel}This builds a local AI workstation in: ${WORKDIR}
It installs dev tools, several GB of models, Docker services, coding agents, and the
OpenClaw phone-driven agent. You'll be asked for your password (Homebrew) and a few
free tokens. Re-running is safe; --reset removes what it creates.

IMPORTANT: Update macOS first (Apple menu > System Settings > General > Software Update).${c_reset}
BANNER
  printf "Proceed? [y/N] "; read -r r; case "$r" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
}

setup_xcode_clt() {
  log "Xcode Command Line Tools"
  if xcode-select -p >/dev/null 2>&1; then ok "Already installed."; return; fi
  warn "Installer popup will appear — click Install and wait."
  xcode-select --install >/dev/null 2>&1 || true
  printf "Waiting for Xcode CLT"; while ! xcode-select -p >/dev/null 2>&1; do printf "."; sleep 5; done
  printf "\n"; ok "Xcode CLT installed."
}

setup_homebrew() {
  log "Homebrew"
  if ! have brew; then
    NONINTERACTIVE=1 /bin/bash -c \
      "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
      || { err "Homebrew install failed."; exit 1; }
  fi
  [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
  grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null || \
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
  ok "$(brew --version | head -n1)"
}

setup_core_tools() {
  log "Core CLI tools"
  local pkgs="ollama colima docker docker-compose node git jq wget lazydocker uv socat"
  for p in $pkgs; do
    if brew list "$p" >/dev/null 2>&1; then ok "$p present"; else opt brew install "$p"; fi
  done
  have node && ok "node $(node -v)"
  have uv && ok "uv $(uv --version 2>/dev/null)"
}

setup_java() {
  log "Java for development (Eclipse Temurin — latest free OpenJDK LTS)"
  if brew list --cask temurin >/dev/null 2>&1 || /usr/libexec/java_home >/dev/null 2>&1; then
    ok "Java already installed: $(java -version 2>&1 | head -n1)"
  else
    opt brew install --cask temurin
  fi
  if ! grep -q 'JAVA_HOME' "$HOME/.zprofile" 2>/dev/null; then
    echo 'export JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null)"' >> "$HOME/.zprofile"
  fi
  export JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null)"
  have java && ok "java: $(java -version 2>&1 | head -n1)" || warn "java not on PATH yet; open a new terminal."
}

setup_ides() {
  log "IDEs (VS Code + IntelliJ IDEA Community Edition)"
  if brew list --cask visual-studio-code >/dev/null 2>&1; then ok "VS Code present"
  else opt brew install --cask visual-studio-code; fi
  if brew list --cask intellij-idea-ce >/dev/null 2>&1 || [ -d "/Applications/IntelliJ IDEA CE.app" ]; then
    ok "IntelliJ IDEA CE present"
  else
    brew install --cask intellij-idea-ce 2>/dev/null && ok "IntelliJ IDEA CE installed" \
      || warn "IntelliJ CE cask unavailable — get the free Community edition from https://www.jetbrains.com/idea/download (choose the Community .dmg)."
  fi
}

setup_gui_apps() {
  log "GUI + CLI apps (LM Studio, Copilot CLI, Cline)"
  if brew list --cask lm-studio >/dev/null 2>&1; then ok "lm-studio present"
  else opt brew install --cask lm-studio; fi
  # GitHub Copilot CLI (free tier available; optional login later).
  if brew list copilot-cli >/dev/null 2>&1 || have copilot; then ok "copilot-cli present"
  else brew install copilot-cli 2>/dev/null && ok "copilot-cli installed" \
       || opt npm install -g @github/copilot; fi
  # Cline CLI — autonomous terminal coding agent with first-class Ollama integration.
  if have cline; then ok "Cline CLI present"
  else opt npm install -g cline; fi
}

# =============================================================================
#  PHASE 1 — OLLAMA + LOCAL MODELS
# =============================================================================
setup_ollama() {
  log "Ollama service + local models"
  if ! grep -q OLLAMA_MAX_LOADED_MODELS "$HOME/.zprofile" 2>/dev/null; then
    echo "export OLLAMA_MAX_LOADED_MODELS=$OLLAMA_MAX_LOADED" >> "$HOME/.zprofile"
  fi
  export OLLAMA_MAX_LOADED_MODELS="$OLLAMA_MAX_LOADED"
  if ! http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
    opt brew services start ollama
    for _ in $(seq 1 15); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
  fi
  http_ok "http://localhost:$PORT_OLLAMA/api/tags" && ok "Ollama up on :$PORT_OLLAMA" \
    || warn "Ollama not responding; run 'ollama serve' in another terminal, then re-run."

  local installed; installed="$(ollama list 2>/dev/null)"
  # Oversized-model guard: qwen3.5:122b-a10b (~81GB) does not fit in 64GB unified memory.
  # If it was pulled by mistake, offer to remove it and reclaim the disk space.
  if printf "%s" "$installed" | grep -q "qwen3.5:122b"; then
    warn "qwen3.5:122b-a10b is installed (~81GB) but CANNOT run on 64GB RAM."
    printf "Remove it now to reclaim ~81GB? [y/N] "
    read -r rmbig; case "$rmbig" in y|Y|yes) opt ollama rm qwen3.5:122b-a10b; ok "Removed qwen3.5:122b-a10b." ;; *) warn "Kept it (wastes disk; remove later with: ollama rm qwen3.5:122b-a10b)." ;; esac
  fi
  for entry in "${MODELS[@]}"; do
    local tag="${entry%%|*}" role="${entry#*|}"
    if printf "%s" "$installed" | grep -q "^${tag%%:*}" && ollama show "$tag" >/dev/null 2>&1; then
      ok "model present: $tag"
    else
      printf "    pulling %s (%s)\n" "$tag" "$role"
      ollama pull "$tag" || warn "pull failed for '$tag' — check the tag at ollama.com/library."
    fi
  done

  # --- Apple Silicon MLX backend (Ollama 0.19+) ---------------------------------
  # MLX (Apple's ML framework) replaced the llama.cpp/Metal backend on Apple Silicon
  # in Ollama 0.19 and is ~2x faster, using the GPU Neural Accelerators on M5-class
  # chips. It requires 32GB+ unified memory and Ollama auto-selects it — there is NO
  # env var to force it. The steps below ensure Ollama is new enough, then verify the
  # MLX backend is actually serving (some model architectures fall back silently).
  ollama_mlx_check
}

# Ensures Ollama >= 0.19 (MLX requires it) and verifies the MLX backend is actually live.
ollama_mlx_check() {
  local ver; ver="$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1)"
  if [ -n "$ver" ]; then
    local maj min; maj="${ver%%.*}"; min="$(printf '%s' "$ver" | cut -d. -f2)"
    if [ "$maj" -eq 0 ] && [ "$min" -lt 19 ]; then
      warn "Ollama $ver predates the MLX backend (needs 0.19+). Upgrading for ~2x faster inference."
      opt brew upgrade ollama
      opt brew services restart ollama
      for _ in $(seq 1 15); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
    else
      ok "Ollama $ver supports the MLX backend (0.19+)."
    fi
  else
    warn "Couldn't read Ollama version; if older than 0.19, run: brew upgrade ollama"
  fi
  # Verify MLX is the engine actually serving. The only reliable signal is the serve log
  # line 'using mlx backend'. We trigger a tiny generation, then grep the brew log.
  local logf; logf="$(brew --prefix 2>/dev/null)/var/log/ollama.log"
  if [ "${#MODELS[@]}" -gt 0 ]; then
    ollama run "${MODELS[0]%%|*}" "ok" >/dev/null 2>&1 &
    local pid=$!; sleep 4; kill "$pid" >/dev/null 2>&1 || true
  fi
  if [ -f "$logf" ] && grep -qi "using mlx backend" "$logf" 2>/dev/null; then
    ok "MLX backend ACTIVE — Apple Silicon acceleration confirmed (~2x vs Metal)."
  elif [ -f "$logf" ]; then
    warn "Could not confirm 'using mlx backend' in $logf."
    warn "Some model architectures fall back to llama.cpp/Metal silently — still GPU-accelerated."
    warn "To check live: tail -f \"$logf\" while a model runs; look for 'using mlx backend'."
  else
    warn "Ollama log not found at $logf; verify MLX with: ollama serve (watch for 'using mlx backend')."
  fi
}

# =============================================================================
#  PHASE 2 — PYTHON ENVS  (gateway venv + isolated Aider)
# =============================================================================
GW_PKGS='"litellm[proxy]" openai langfuse python-dotenv flask requests rich psutil'
venv_ok() { [ -x "$WORKDIR/.venv/bin/python" ] && [ -x "$WORKDIR/.venv/bin/litellm" ] \
            && "$WORKDIR/.venv/bin/python" -c "import litellm, flask, requests, psutil" >/dev/null 2>&1; }
setup_python() {
  log "Gateway/dashboard Python env (litellm proxy + flask; Aider kept separate)"
  if venv_ok; then ok "Gateway venv healthy."
  else
    [ -d "$WORKDIR/.venv" ] && { warn "venv incomplete — rebuilding."; rm -rf "$WORKDIR/.venv"; }
    ( cd "$WORKDIR" && opt uv venv --python 3.12 .venv \
        && opt uv pip install --python "$WORKDIR/.venv/bin/python" $GW_PKGS )
    venv_ok && ok "Gateway venv ready (litellm proxy + flask)." \
            || warn "Gateway venv incomplete; re-run to retry (this is what the gateway needs)."
  fi

  log "Aider — isolated install via uv tool (its pinned litellm stays out of the gateway)"
  if have aider || [ -x "$HOME/.local/bin/aider" ]; then ok "Aider already installed."
  else
    opt uv tool install --force --python python3.12 --with pip aider-chat@latest
    ( have aider || [ -x "$HOME/.local/bin/aider" ] ) && ok "Aider installed (isolated)." \
      || warn "Aider install incomplete; retry later: uv tool install aider-chat@latest"
  fi
  if ! grep -q '.local/bin' "$HOME/.zprofile" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"
  fi
}

# =============================================================================
#  PHASE 3 — DOCKER (Colima) + web services
# =============================================================================
setup_colima() {
  log "Docker engine (Colima)"
  if docker_up; then ok "Docker already up."; return; fi
  opt colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"
  for _ in $(seq 1 25); do docker_up && break; sleep 1; done
  docker_up && ok "Docker up via Colima" || warn "Docker down; Open WebUI/SearXNG/Langfuse skipped (re-run later)."
}

setup_openwebui() {
  log "Open WebUI — local ChatGPT-style chat over your models (:$PORT_OPENWEBUI)"
  docker_up || { warn "Docker down; skipping."; return; }
  if container_running open-webui && http_ok "http://localhost:$PORT_OPENWEBUI/"; then ok "Open WebUI already running."; return; fi
  opt docker volume create open-webui
  # 0.0.0.0 so other devices on your Wi-Fi (e.g. your phone) can reach it by LAN IP.
  ensure_container open-webui docker run -d --name open-webui --restart unless-stopped \
    -p "0.0.0.0:$PORT_OPENWEBUI:8080" \
    -e OLLAMA_BASE_URL="http://host.docker.internal:$PORT_OLLAMA" \
    --add-host=host.docker.internal:host-gateway \
    -v open-webui:/app/backend/data \
    ghcr.io/open-webui/open-webui:main
  ok "Open WebUI starting -> http://localhost:$PORT_OPENWEBUI (create a local account on first open)."
}

searxng_ok() { http_ok "http://localhost:$PORT_SEARXNG/"; }
setup_searxng() {
  log "SearXNG — private web search (gives agents live, up-to-date knowledge) (:$PORT_SEARXNG)"
  docker_up || { warn "Docker down; skipping."; return; }
  if container_running searxng && searxng_ok; then ok "SearXNG already running."; return; fi
  local SX="$WORKDIR/searxng"; mkdir -p "$SX"
  if [ ! -f "$SX/settings.yml" ]; then
    local secret; secret="$(openssl rand -hex 24)"
    cat > "$SX/settings.yml" <<YAMLEOF
use_default_settings: true
server:
  secret_key: "$secret"
  bind_address: "0.0.0.0"
  limiter: false
search:
  formats:
    - html
    - json
YAMLEOF
  fi
  # 0.0.0.0 so other devices on your Wi-Fi can reach it by LAN IP.
  ensure_container searxng docker run -d --name searxng --restart unless-stopped \
    -p "0.0.0.0:$PORT_SEARXNG:8080" -v "$SX:/etc/searxng" searxng/searxng:latest
  for _ in $(seq 1 20); do searxng_ok && break; sleep 1; done
  searxng_ok && ok "SearXNG up (JSON API: /search?q=...&format=json)" || warn "SearXNG slow; check lazydocker."
}

langfuse_ok() { http_ok "http://localhost:$PORT_LANGFUSE/api/public/health"; }
setup_langfuse() {
  log "Langfuse — agent trace/log dashboard (:$PORT_LANGFUSE)"
  docker_up || { warn "Docker down; skipping."; return; }
  local LF="$WORKDIR/langfuse"
  if langfuse_ok; then ok "Langfuse running."
  else
    [ -d "$LF/.git" ] && ( cd "$LF" && dc down >/dev/null 2>&1 || true )
    [ -d "$LF/.git" ] || opt git clone --depth=1 https://github.com/langfuse/langfuse.git "$LF"
    ( cd "$LF" && opt dc up -d )
    printf "Waiting for Langfuse"; for _ in $(seq 1 60); do langfuse_ok && break; printf "."; sleep 2; done; printf "\n"
    langfuse_ok && ok "Langfuse up." || warn "Langfuse slow to start; check later in lazydocker."
  fi
  load_env
  local epk esk; epk="$(get_env LANGFUSE_PUBLIC_KEY)"; esk="$(get_env LANGFUSE_SECRET_KEY)"
  if [ -n "$epk" ] && [ -n "$esk" ] && \
     [ "$(curl -s -o /dev/null -w '%{http_code}' -u "$epk:$esk" "http://localhost:$PORT_LANGFUSE/api/public/projects" 2>/dev/null)" = "200" ]; then
    ok "Langfuse API keys already configured and valid — skipping."; return
  fi
  cat <<TUT

${c_cyn}#############  ACTION: LANGFUSE API KEYS  #############${c_reset}
  1. Open http://localhost:$PORT_LANGFUSE  -> Sign up (local account).
  2. Create an Organization, then a Project.
  3. Settings -> API Keys -> Create. Copy PUBLIC (pk-lf-...) and SECRET (sk-lf-...).
${c_cyn}######################################################${c_reset}
TUT
  press_enter
  local pk sk
  printf "%sPaste PUBLIC key (or 'skip'): %s" "$c_cyn" "$c_reset"; read -r pk
  case "$pk" in skip|SKIP|"") warn "Skipped Langfuse keys (dashboard will show services only)."; return ;; esac
  printf "%sPaste SECRET key: %s" "$c_cyn" "$c_reset"; read -r sk
  set_env LANGFUSE_PUBLIC_KEY "$pk"; set_env LANGFUSE_SECRET_KEY "$sk"
  set_env LANGFUSE_HOST "http://localhost:$PORT_LANGFUSE"; ok "Langfuse keys saved."
}

# =============================================================================
#  PHASE 4 — LiteLLM GATEWAY (one OpenAI-style endpoint over all local models)
# =============================================================================
setup_cloud_optional() {
  log "Optional BACKUP cloud models (OpenRouter free tier) — local stays the default brain"
  if [ -n "$(get_env OPENROUTER_API_KEY)" ]; then ok "OpenRouter key already configured."
  else
    printf "Add a free OpenRouter API key as a cloud backup tier? [y/N] "
    read -r r; case "$r" in y|Y|yes) prompt_secret OPENROUTER_API_KEY "OpenRouter API Key" validate_openrouter tut_openrouter ;; *) ok "Staying fully local (no cloud backup)." ;; esac
  fi
}

setup_litellm() {
  log "LiteLLM gateway (:$PORT_GATEWAY) — friendly model names; logs to Langfuse"
  load_env
  local CFG="$WORKDIR/litellm.config.yaml"
  if [ -f "$CFG" ]; then
    ok "Existing litellm.config.yaml found — keeping it as-is (not overwritten)."
    if ! grep -q 'success_callback' "$CFG"; then
      warn "Your config has no Langfuse callback; the dashboard activity panel will stay empty."
      warn "To enable it, add at the end of $CFG:"
      printf '       litellm_settings:\n         success_callback: ["langfuse"]\n         failure_callback: ["langfuse"]\n'
    fi
  else
    warn "No litellm.config.yaml in $WORKDIR — writing a default."
    cat > "$CFG" <<'YAMLEOF'
model_list:
  - model_name: reasoning
    litellm_params: { model: ollama/qwen3.6:35b-a3b,   api_base: http://127.0.0.1:11434 }
  - model_name: qwen3.6-coder
    litellm_params: { model: ollama/qwen3.6:27b,       api_base: http://127.0.0.1:11434 }
  - model_name: qwen3-coder-next
    litellm_params: { model: ollama/qwen3-coder-next,  api_base: http://127.0.0.1:11434 }
  - model_name: devstral
    litellm_params: { model: ollama/devstral:24b,      api_base: http://127.0.0.1:11434 }
  - model_name: codestral
    litellm_params: { model: ollama/codestral:22b,     api_base: http://127.0.0.1:11434 }
  - model_name: qwen2.5vl
    litellm_params: { model: ollama/qwen2.5vl:7b,      api_base: http://127.0.0.1:11434 }
  - model_name: gemma4
    litellm_params: { model: ollama/gemma4:12b,        api_base: http://127.0.0.1:11434 }
  - model_name: nomic-embed-text
    litellm_params: { model: ollama/nomic-embed-text,  api_base: http://127.0.0.1:11434 }
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  drop_params: true
  request_timeout: 1200
YAMLEOF
    ok "Default litellm.config.yaml written."
  fi

  # Inject OpenRouter free-tier models if a key is configured (portable awk; robust).
  # Names use the openrouter/ provider prefix and the :free suffix (free routing).
  local or_key; or_key="$(get_env OPENROUTER_API_KEY)"
  if [ -n "$or_key" ] && ! grep -q "openrouter-" "$CFG"; then
    log "Injecting OpenRouter free models into litellm.config.yaml."
    awk -v k="$or_key" '
      /^model_list:/ && !done {
        print
        print "  - model_name: openrouter-r1"
        print "    litellm_params: { model: openrouter/deepseek/deepseek-r1:free, api_key: \"" k "\" }"
        print "  - model_name: openrouter-coder"
        print "    litellm_params: { model: openrouter/qwen/qwen3-coder:free, api_key: \"" k "\" }"
        done=1; next
      }
      { print }
    ' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
  fi

  cat > "$WORKDIR/start_gateway.sh" <<SHEOF
#!/usr/bin/env bash
# Launched by launchd, so use ABSOLUTE paths (no reliance on cwd or login PATH).
set -a; [ -f "$WORKDIR/.env" ] && . "$WORKDIR/.env"; set +a
LITELLM="$WORKDIR/.venv/bin/litellm"
CONFIG="$WORKDIR/litellm.config.yaml"
if [ ! -x "\$LITELLM" ]; then
  echo "ERROR: \$LITELLM not found. Run setup_python (the gateway venv is missing)." >&2
  exit 1
fi
exec "\$LITELLM" --config "\$CONFIG" --port $PORT_GATEWAY --host 0.0.0.0
SHEOF
  chmod +x "$WORKDIR/start_gateway.sh"; ok "Gateway launcher written (absolute paths)."
}

# =============================================================================
#  PHASE 5 — CONTINUE (VS Code extension wired to local models)
# =============================================================================
setup_continue() {
  log "Continue — VS Code AI extension using your LOCAL models"
  if have code; then
    code --install-extension continue.continue >/dev/null 2>&1 && ok "Continue extension installed." \
      || warn "Could not auto-install; in VS Code, Extensions -> search 'Continue'."
  else
    warn "'code' CLI not on PATH yet. In VS Code run: Cmd+Shift+P -> 'Shell Command: Install code command', then re-run."
  fi
  mkdir -p "$HOME/.continue"
  if [ ! -f "$HOME/.continue/config.yaml" ]; then
    cat > "$HOME/.continue/config.yaml" <<'YAMLEOF'
name: Local AI
version: 1.0.0
models:
  - name: Coder (Qwen 3.6 27B — most accurate that fits 64GB)
    provider: ollama
    model: qwen3.6:27b
    roles: [chat, edit, apply]
  - name: Coder Next (Qwen3-Coder-Next — heavy, code-only)
    provider: ollama
    model: qwen3-coder-next
    roles: [chat, edit, apply]
  - name: Autocomplete (Codestral)
    provider: ollama
    model: codestral:22b
    roles: [autocomplete]
  - name: Embeddings
    provider: ollama
    model: nomic-embed-text
    roles: [embed]
YAMLEOF
    ok "Continue config written (~/.continue/config.yaml)."
  else ok "Continue config already exists."; fi
}

# =============================================================================
#  PHASE 6 — LIVE DASHBOARD (service health + recent agent activity)
# =============================================================================
write_dashboard() {
  log "Custom live dashboard (:$PORT_DASHBOARD)"
  local D="$WORKDIR/dashboard"; mkdir -p "$D"
  cat > "$D/app.py" <<'PYEOF'
#!/usr/bin/env python3
"""Mission control: service health + open ports + live Ollama models + CLI tools + traces."""
import os, shutil, requests, psutil
from flask import Flask, jsonify
from dotenv import load_dotenv
HOME = os.environ.get("AI_HOME", os.path.expanduser("~/ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))
LF = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
PK = os.environ.get("LANGFUSE_PUBLIC_KEY", ""); SK = os.environ.get("LANGFUSE_SECRET_KEY", "")

SERVICES = [
    ("Live dashboard",  "http://localhost:8800/",                  8800, "this page"),
    ("Ollama",          "http://localhost:11434/api/tags",         11434, "local model server"),
    ("LiteLLM gateway", "http://localhost:4000/health/liveliness", 4000, "OpenAI-style routing + logging"),
    ("Open WebUI",      "http://localhost:3001/",                  3001, "chat UI + web search"),
    ("SearXNG",         "http://localhost:8888/",                  8888, "private web search"),
    ("Langfuse",        "http://localhost:3000/api/public/health", 3000, "agent traces / logs"),
]

CLI_TOOLS = [
    ("aider",     "AI pair-programmer (terminal)"),
    ("cline",     "autonomous coding agent — 'ollama launch cline'"),
    ("openclaw",  "phone-driven agent (Telegram/Discord)"),
    ("copilot",   "GitHub Copilot CLI (cloud backup)"),
    ("peekaboo",  "macOS GUI automation for agents"),
    ("ollama",    "model pull/run CLI"),
    ("lazydocker","container TUI monitor"),
]

app = Flask(__name__)

def probe(url):
    try: return requests.get(url, timeout=3).status_code < 500
    except Exception: return False

# On macOS APFS, '/' is the read-only system volume (looks ~empty). The real user data
# lives on the Data volume, so probe that first to report true storage usage.
def disk_pct():
    for path in ("/System/Volumes/Data", os.path.expanduser("~"), "/"):
        try:
            return psutil.disk_usage(path).percent
        except Exception:
            continue
    return 0

def battery_pct():
    # Returns int 0-100, or None if no battery (e.g. desktop / sensor unavailable).
    try:
        b = psutil.sensors_battery()
        if b is None:
            return None
        return {"percent": round(b.percent), "charging": bool(b.power_plugged)}
    except Exception:
        return None

# Cloud/backup models live in the LiteLLM gateway, not in Ollama. Pull them from the
# gateway's OpenAI-style /v1/models so they show on the dashboard too (e.g. openrouter-*).
def gateway_models():
    try:
        r = requests.get("http://localhost:4000/v1/models", timeout=3)
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        # show only the cloud aliases (local ones already appear under Ollama)
        return sorted([i for i in ids if i.startswith("openrouter") or i.startswith("deepseek")])
    except Exception:
        return []

MODEL_GUIDE = [
    ("qwen3-coder-next", "Coding",    "Heaviest agentic coder (~46GB). Repo-level, multi-file. Run alone."),
    ("qwen3.6:35b",      "Reasoning", "Orchestrator brain. Planning, routing, tool-calling, general Q&A (newest)."),
    ("qwen3.6",          "Coding",    "Primary coder. Best accuracy that fits 64GB. Day-to-day code + agentic."),
    ("codestral",        "Coding",    "Fast inline autocomplete (FIM) in the IDE."),
    ("devstral",         "Coding",    "Agentic edits: multi-file changes, tool calls, test-fix loops."),
    ("openrouter-coder", "Coding",    "Cloud backup coder (OpenRouter free: Qwen3-Coder)."),
    ("qwen3.5:35b",      "Reasoning", "Older reasoning brain (superseded by qwen3.6:35b-a3b)."),
    ("qwen3.5",          "Reasoning", "Older Qwen3.5 (122B won't fit 64GB; remove it)."),
    ("qwen3",            "Reasoning", "General reasoning / chat."),
    ("openrouter-r1",    "Reasoning", "Cloud backup reasoning (OpenRouter free: DeepSeek R1)."),
    ("gemma4",           "Multimodal","Images, audio, video + text. Use for screenshots, docs, media."),
    ("qwen2.5vl",        "Multimodal","Lightweight vision. Reads screenshots for GUI control."),
    ("llava",            "Multimodal","Vision-language: describe/QA images."),
    ("nomic-embed",      "Embeddings","Not for chat. Powers RAG / memory / search reranking."),
    ("mxbai-embed",      "Embeddings","Not for chat. Embeddings for RAG / search."),
]
GROUP_ORDER = {"Coding": 0, "Reasoning": 1, "Multimodal": 2, "Embeddings": 3, "Other": 4}

def classify(name):
    low = name.lower()
    for prefix, group, tip in MODEL_GUIDE:
        if low.startswith(prefix):
            return group, tip
    return "Other", "General-purpose model."

def ollama_models():
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        out = []
        for m in r.json().get("models", []):
            size = m.get("size", 0)
            name = m.get("name", "?")
            group, tip = classify(name)
            out.append({"name": name,
                        "size": f"{size/1e9:.1f} GB" if size else "?",
                        "group": group, "tip": tip})
        return sorted(out, key=lambda x: (GROUP_ORDER.get(x["group"], 9), x["name"]))
    except Exception:
        return []

def cli_tools():
    out = []
    for name, desc in CLI_TOOLS:
        local = os.path.expanduser(f"~/.local/bin/{name}")
        path = shutil.which(name) or (local if os.path.exists(local) else None)
        out.append({"name": name, "desc": desc, "installed": bool(path), "path": path or ""})
    return out

@app.route("/api/status")
def status():
    svc = [{"name": n, "port": port, "purpose": purpose, "ok": probe(h)}
           for (n, h, port, purpose) in SERVICES]
    traces = []
    if PK and SK:
        try:
            r = requests.get(f"{LF}/api/public/traces", params={"limit": 15}, auth=(PK, SK), timeout=4)
            for t in r.json().get("data", []):
                traces.append({"name": t.get("name") or "(trace)",
                               "time": (t.get("timestamp") or "")[:19].replace("T", " "),
                               "latency": round((t.get("latency") or 0), 2)})
        except Exception: pass
    hardware = {
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
        "storage": disk_pct(),
        "battery": battery_pct(),
    }
    return jsonify({"services": svc, "models": ollama_models(), "gateway_models": gateway_models(), "tools": cli_tools(), "traces": traces, "hardware": hardware})

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>AI Workstation - Mission Control</title>
<style>
 :root{--bg:#0a0e14;--card:#121823;--line:#1e2a3a;--ink:#e6edf3;--dim:#7d8896;--up:#2ea043;--down:#f85149;--accent:#4c8dff}
 *{box-sizing:border-box} body{background:var(--bg);color:var(--ink);font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:32px;max-width:1200px;margin:0 auto}
 h1{font-size:22px;margin:0 0 2px;letter-spacing:-.3px} .sub{color:var(--dim);font-size:13px;margin-bottom:26px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:30px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
 .row{display:flex;align-items:center;gap:9px} .dot{height:9px;width:9px;border-radius:50%;flex:none}
 .up{background:var(--up);box-shadow:0 0 8px var(--up)} .down{background:var(--down)}
 .name{font-weight:600;font-size:14px} a{color:var(--accent);text-decoration:none;font-size:12px} a:hover{text-decoration:underline}
 .state{font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-left:auto} .s-up{color:var(--up)} .s-down{color:var(--down)}
 .port{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim);background:#0d1420;border:1px solid var(--line);border-radius:5px;padding:1px 6px;margin-left:8px}
 .purpose{color:var(--dim);font-size:12px;margin-top:4px}
 h3{font-size:14px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:26px 0 12px}
 table{width:100%;border-collapse:collapse;font-size:13px} th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line)}
 th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px} .muted{color:var(--dim)}
 .pill{font-size:11px;padding:2px 8px;border-radius:20px} .pill-on{background:rgba(46,160,67,.15);color:var(--up)} .pill-off{background:rgba(248,81,73,.12);color:var(--down)}
 .grouphead td{background:#0d1420;color:var(--accent);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.7px;padding:7px 12px}
 code{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--ink)}
 .hw-container{display:flex;gap:20px;margin-bottom:20px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;}
 .hw-item{flex:1;text-align:center;} .hw-val{font-size:24px;font-weight:bold;color:var(--accent);margin-top:5px;}
</style></head><body>
<h1>AI Workstation</h1><div class="sub">Mission control — auto-refreshes every 5s — everything runs locally on this Mac</div>

<h3>Hardware Resource Metric Status</h3>
<div class="hw-container">
  <div class="hw-item"><div>CPU Usage</div><div id="hw-cpu" class="hw-val">--%</div></div>
  <div class="hw-item"><div>RAM Usage</div><div id="hw-ram" class="hw-val">--%</div></div>
  <div class="hw-item"><div>Storage Usage</div><div id="hw-storage" class="hw-val">--%</div></div>
  <div class="hw-item"><div>Battery</div><div id="hw-battery" class="hw-val">--%</div></div>
</div>

<h3>Services &amp; open ports</h3>
<div id="svc" class="grid"></div>

<h3>Local models available (Ollama) — grouped by what they're best at</h3>
<table><thead><tr><th>Model</th><th>Size</th><th>Best for</th></tr></thead>
<tbody id="models"><tr><td colspan=3 class="muted">loading...</td></tr></tbody></table>

<h3>Cloud models (via gateway — backup only)</h3>
<table><thead><tr><th>Alias</th><th>Routed through</th></tr></thead>
<tbody id="cloud"><tr><td colspan=2 class="muted">loading...</td></tr></tbody></table>

<h3>CLI tools</h3>
<table><thead><tr><th>Tool</th><th>Status</th><th>What</th></tr></thead>
<tbody id="tools"><tr><td colspan=3 class="muted">loading...</td></tr></tbody></table>

<h3>Recent agent activity</h3>
<table><thead><tr><th>When</th><th>Agent / call</th><th>Latency (s)</th></tr></thead>
<tbody id="tr"><tr><td colspan=3 class="muted">loading...</td></tr></tbody></table>

<script>
async function tick(){
 try{ const d=await (await fetch('/api/status')).json();
  const currentHost = window.location.hostname;
  if(d.hardware){
    document.getElementById('hw-cpu').innerText = d.hardware.cpu + '%';
    document.getElementById('hw-ram').innerText = d.hardware.ram + '%';
    document.getElementById('hw-storage').innerText = d.hardware.storage + '%';
    const b = d.hardware.battery;
    document.getElementById('hw-battery').innerText =
      (b && typeof b.percent === 'number') ? (b.percent + '%' + (b.charging ? ' ⚡' : '')) : 'N/A';
  }
  document.getElementById('svc').innerHTML=d.services.map(s=>{
   const serviceUrl = 'http://' + currentHost + ':' + s.port + '/';
   return '<div class="card"><div class="row"><span class="dot '+(s.ok?'up':'down')+'"></span>'+
   '<span class="name">'+s.name+'</span><span class="port">:'+s.port+'</span>'+
   '<span class="state '+(s.ok?'s-up':'s-down')+'">'+(s.ok?'live':'down')+'</span></div>'+
   '<div class="purpose">'+s.purpose+'</div>'+
   '<div style="margin-top:6px"><a href="'+serviceUrl+'" target="_blank">'+serviceUrl+'</a></div></div>';
  }).join('');
  if(d.models.length){
   let last=''; let rows='';
   for(const m of d.models){
    if(m.group!==last){ rows+='<tr class="grouphead"><td colspan=3>'+m.group+'</td></tr>'; last=m.group; }
    rows+='<tr><td><code>'+m.name+'</code></td><td class="muted">'+m.size+'</td><td class="muted">'+m.tip+'</td></tr>';
   }
   document.getElementById('models').innerHTML=rows;
  } else {
   document.getElementById('models').innerHTML='<tr><td colspan=3 class="muted">Ollama not reachable or no models pulled yet.</td></tr>';
  }
  const cm = d.gateway_models || [];
  document.getElementById('cloud').innerHTML = cm.length ? cm.map(m=>
   '<tr><td><code>'+m+'</code></td><td class="muted">OpenRouter free tier (cloud)</td></tr>').join('')
   : '<tr><td colspan=2 class="muted">No cloud models — add an OpenRouter key, or gateway not running.</td></tr>';
  document.getElementById('tools').innerHTML = d.tools.map(t=>
   '<tr><td><code>'+t.name+'</code></td><td><span class="pill '+(t.installed?'pill-on':'pill-off')+'">'+(t.installed?'installed':'—')+'</span></td><td class="muted">'+t.desc+'</td></tr>').join('');
  document.getElementById('tr').innerHTML = d.traces.length ? d.traces.map(t=>
   '<tr><td class="muted">'+(t.time||'')+'</td><td>'+t.name+'</td><td>'+t.latency+'</td></tr>').join('')
   : '<tr><td colspan=3 class="muted">No traces yet — run an agent through the gateway.</td></tr>';
 }catch(e){}
}
tick(); setInterval(tick,5000);
</script></body></html>"""
@app.route("/")
def home(): return PAGE
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8800, threaded=True)
PYEOF
  ok "Dashboard written (services+ports, live models, CLI tools, traces)."
}

# =============================================================================
#  PHASE 7 — ALWAYS-ON SERVICES (launchd)
# =============================================================================
install_launch_agent() {
  local label="${1:-}" prog="${2:-}" plist
  if [ -z "$label" ] || [ -z "$prog" ]; then warn "install_launch_agent: missing label/program — skipping."; return; fi
  plist="$LAUNCH_DIR/$label.plist"
  mkdir -p "$LAUNCH_DIR"; launchctl unload "$plist" >/dev/null 2>&1 || true
  cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-lc</string><string>$prog</string></array>
  <key>EnvironmentVariables</key><dict><key>AI_HOME</key><string>$WORKDIR</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.out.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err.log</string>
</dict></plist>
PLISTEOF
  launchctl load "$plist" >/dev/null 2>&1 && ok "service loaded: $label" || warn "could not load $label"
}
setup_services() {
  log "Registering always-on services (launchd)"
  mkdir -p "$WORKDIR/logs"
  install_launch_agent "com.aiws.colima"    "/opt/homebrew/bin/colima start || true; while true; do sleep 86400; done"
  install_launch_agent "com.aiws.litellm"   "$WORKDIR/start_gateway.sh"
  install_launch_agent "com.aiws.dashboard" "$WORKDIR/.venv/bin/python $WORKDIR/dashboard/app.py"
  # LAN bridges: Colima only forwards Docker ports to 127.0.0.1 on this Mac, so other
  # devices can't reach them by LAN IP. socat listens on 0.0.0.0 (all interfaces, runs
  # NATIVELY like the dashboard) and forwards to the localhost port Colima publishes.
  # This makes Open WebUI / SearXNG / Langfuse reachable at the Mac's LAN IP.
  local SOCAT; SOCAT="$(command -v socat || echo /opt/homebrew/bin/socat)"
  install_launch_agent "com.aiws.bridge.openwebui" "$SOCAT TCP-LISTEN:$PORT_OPENWEBUI,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_OPENWEBUI"
  install_launch_agent "com.aiws.bridge.searxng"   "$SOCAT TCP-LISTEN:$PORT_SEARXNG,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_SEARXNG"
  install_launch_agent "com.aiws.bridge.langfuse"  "$SOCAT TCP-LISTEN:$PORT_LANGFUSE,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_LANGFUSE"
  ok "Gateway + dashboard + LAN bridges auto-start now and at login."
}

# =============================================================================
#  PHASE 8 — OPENCLAW (phone-driven agent) + Peekaboo (GUI control)
# =============================================================================
collect_chat_tokens() {
  log "Telegram token for your phone-driven agent"
  prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token" validate_telegram tut_telegram
}
openclaw_ok() { have openclaw && openclaw --version >/dev/null 2>&1; }
setup_openclaw() {
  log "OpenClaw — the agent you command from Telegram"
  have npm || { warn "npm missing; skipping OpenClaw."; return; }
  if openclaw_ok; then ok "OpenClaw present ($(openclaw --version 2>/dev/null))."
  else
    npm ls -g openclaw >/dev/null 2>&1 && opt npm uninstall -g openclaw
    SHARP_IGNORE_GLOBAL_LIBVIPS=1 npm install -g openclaw@latest \
      || opt npm install -g openclaw@latest
  fi
  openclaw_ok && ok "openclaw $(openclaw --version 2>/dev/null)" || { warn "OpenClaw install failed; run 'openclaw doctor' to diagnose, then re-run."; return; }
  cat <<TUT

${c_cyn}#############  FINISH OPENCLAW (interactive)  #############${c_reset}
 Run the official onboarding (the script can launch it for you below):
     ${c_grn}openclaw onboard --install-daemon${c_reset}
 In onboarding:
   - Provider: ollama   Model: qwen3.6:35b-a3b   Endpoint: http://localhost:$PORT_OLLAMA
   - Channel: Telegram -> paste TELEGRAM_BOT_TOKEN from $WORKDIR/.env
   - For Mac control, macOS will prompt for permissions. Grant ONLY what you want:
       System Settings > Privacy & Security > Accessibility   (enable OpenClaw)
       System Settings > Privacy & Security > Screen Recording (enable OpenClaw)

 SECURITY: keep the human-approval rule in agents/SOUL.orchestrator.md. Do NOT install
 third-party OpenClaw "skills" without reading their source.
${c_cyn}#########################################################${c_reset}
TUT
  printf "Run 'openclaw onboard --install-daemon' now? [Y/n] "
  read -r r; case "$r" in n|N|no) warn "Skipped — run it yourself when ready." ;; *) openclaw onboard --install-daemon || warn "Onboarding exited; re-run anytime." ;; esac
}
setup_peekaboo() {
  log "Peekaboo — lets the agent see the screen & control mouse/keyboard/apps (optional)"
  if brew list --cask peekaboo >/dev/null 2>&1 || [ -d "/Applications/Peekaboo.app" ] || have peekaboo; then
    ok "Peekaboo already installed. Skipping prompt."
    return 0
  fi
  printf "Install Peekaboo (needed for GUI/app control, goal #4)? [y/N] "
  read -r r; case "$r" in y|Y|yes) ;; *) ok "Skipped (agent can still do shell/file/code tasks)."; return ;; esac
  brew install steipete/tap/peekaboo 2>/dev/null && ok "Peekaboo installed." \
    || warn "Auto-install failed; try: brew install steipete/tap/peekaboo  (docs: https://peekaboo.boo)"
  warn "After install, grant Peekaboo/OpenClaw Accessibility + Screen Recording in System Settings."
}

# =============================================================================
#  PHASE 9 — WORKSPACE SCAFFOLD (agent SOUL files, web-search tool, README)
# =============================================================================
scaffold_workspace() {
  log "Workspace scaffold (agent rules, web-search helper, README)"
  local A="$WORKDIR/agents"; mkdir -p "$A"
  cat > "$A/SOUL.orchestrator.md" <<'MDEOF'
# SOUL: Orchestrator
You are the user's lead AI operator, running locally on their Mac. The user commands you
over Telegram. You plan, delegate to sub-agents, control the Mac when needed, and report back.

## HARD RULES (never break)
- LOCAL FIRST. Use the local models (via the gateway at http://localhost:4000) for every
  task by default. Only use a cloud/backup model (the openrouter-* aliases) when the user
  explicitly asks you to escalate. Never send data off-machine on your own initiative.
- BEFORE any high-impact action — running shell that changes the system, installing
  software, spinning up containers, deleting/overwriting files, controlling mouse/keyboard,
  opening apps, posting anything, or spending money — summarise the plan in ONE message and
  WAIT for the user to reply "yes"/"approve". Read-only analysis needs no approval.
- For anything "latest" or time-sensitive, USE the web-search tool (SearXNG at
  http://localhost:8888) — your built-in knowledge has a cutoff.
- Prefer real APIs and code over GUI clicking. Only fall back to mouse/keyboard control
  (Peekaboo) when there is no scriptable path. GUI control is brittle — verify with a
  screenshot after each step and stop if the screen isn't what you expected.

## Sub-agents (call by model alias on the gateway at http://localhost:4000)
- qwen3.6-coder    : qwen3.6:27b — writes web apps, scripts, services (primary coder)
- qwen3-coder-next : heavy agentic coding; multi-file, repo-level (run alone, ~46GB)
- devstral         : multi-file edits, runs tests, tool calls
- qwen2.5vl        : reads screenshots to locate UI elements before clicking
- reasoning         : qwen3.6:35b-a3b — planning / routing (orchestrator brain)

## Style: concise. State assumptions. Surface risks. Ask one question only when blocked.
## Accuracy over speed: prefer the most accurate model (qwen3.6-coder for code,
## reasoning for general reasoning) even if slower.
MDEOF
  cat > "$A/websearch.py" <<'PYEOF'
#!/usr/bin/env python3
"""Private web search via local SearXNG -> up-to-date answers for agents.
Usage: python websearch.py "your query" """
import sys, requests
q = " ".join(sys.argv[1:]) or "latest local LLM news"
try:
    r = requests.get("http://localhost:8888/search", params={"q": q, "format": "json"}, timeout=8)
    for it in r.json().get("results", [])[:8]:
        print("- " + it.get("title", "")); print("  " + it.get("url", "")); print("  " + it.get("content", "")[:200])
except Exception as e:
    print("search failed:", e)
PYEOF
  chmod +x "$A/websearch.py"
  cat > "$WORKDIR/README_AI.md" <<MDEOF
# Local AI workstation — quick reference
Everything lives in \`$WORKDIR\`. Secrets in \`.env\` (chmod 600).

## What does what (mapped to the five goals)
| Goal | Tool | How |
|---|---|---|
| 1. Build apps / scripts | Aider, Cline, Continue (VS Code) | local models write code |
| 2. Up-to-date answers | Open WebUI + SearXNG | chat that searches the live web |
| 3. Command from phone | OpenClaw + Telegram | DM your bot; it executes tasks |
| 4. Control the Mac/GUI | OpenClaw + Peekaboo | mouse/keyboard/app control (supervise!) |
| 5. Monitor everything | Live dashboard + Langfuse | health + agent activity |

## URLs (reachable on your Wi-Fi via your Mac's LAN IP)
- Dashboard      http://localhost:$PORT_DASHBOARD
- Open WebUI     http://localhost:$PORT_OPENWEBUI   (chat + web search)
- Langfuse       http://localhost:$PORT_LANGFUSE    (agent traces)
- SearXNG        http://localhost:$PORT_SEARXNG     (web search API)
- LiteLLM        http://localhost:$PORT_GATEWAY     (model routing)
- Ollama         http://localhost:$PORT_OLLAMA      (model server)

## Build code (goal 1)
- Aider (pair-programming):  \`cd <project> && aider --model ollama/qwen3.6:27b\`
- Cline (autonomous agent):  \`cd <project> && ollama launch cline --model qwen3.6:27b\`
- VS Code:   open the Continue panel; it uses your local models.
- Copilot CLI: run \`copilot\` in a repo, then \`/login\` (free tier has limited credits).

## Manage services
- Status:  \`bash setup_ai_workstation.sh --status\`
- Start | Stop | Restart | Update | Reset:  --start | --stop | --restart | --update | --reset
- Auto-start at login via launchd. Stopping preserves all data/models/configs.

## Notes & honest limits
- Local models are strong but won't fully match frontier cloud coders on hard agentic work.
- If an \`ollama pull\` failed, the tag drifted — check https://ollama.com/library and edit
  the MODELS array + litellm.config.yaml aliases.
- SECURITY: services bind 0.0.0.0 (reachable on your LAN). Safe on trusted home Wi-Fi only;
  on public networks, switch bindings back to 127.0.0.1 or add passwords.
MDEOF
  ok "Workspace ready at $WORKDIR/agents"
}

# =============================================================================
#  SERVICE CONTROL
# =============================================================================
LABELS="com.aiws.colima com.aiws.litellm com.aiws.dashboard com.aiws.bridge.openwebui com.aiws.bridge.searxng com.aiws.bridge.langfuse"
brew_env() { [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null; }
state()   { "$@" >/dev/null 2>&1 && echo up || echo down; }
svc_row() { if [ "$2" = up ]; then printf "  %-30s %sRUNNING%s\n" "$1" "$c_grn" "$c_reset"; else printf "  %-30s %sSTOPPED%s\n" "$1" "$c_red" "$c_reset"; fi; }
svc_status() {
  brew_env; log "Service status"
  svc_row "Docker engine (Colima)"        "$(state docker_up)"
  svc_row "Ollama        :$PORT_OLLAMA"    "$(state http_ok "http://localhost:$PORT_OLLAMA/api/tags")"
  svc_row "LiteLLM gw    :$PORT_GATEWAY"    "$(state http_ok "http://localhost:$PORT_GATEWAY/health/liveliness")"
  svc_row "Open WebUI    :$PORT_OPENWEBUI"  "$(state http_ok "http://localhost:$PORT_OPENWEBUI/")"
  svc_row "SearXNG       :$PORT_SEARXNG"    "$(state searxng_ok)"
  svc_row "Langfuse      :$PORT_LANGFUSE"   "$(state langfuse_ok)"
  svc_row "Live dashboard:$PORT_DASHBOARD"  "$(state http_ok "http://localhost:$PORT_DASHBOARD/")"
  printf "\n  Dashboard (Local): %shttp://localhost:%s%s\n" "$c_cyn" "$PORT_DASHBOARD" "$c_reset"
}
svc_start() {
  brew_env; log "Starting all services"
  opt colima start; for _ in $(seq 1 25); do docker_up && break; sleep 1; done
  opt brew services start ollama
  if docker_up; then
    docker start open-webui searxng >/dev/null 2>&1 || true
    [ -d "$WORKDIR/langfuse" ] && ( cd "$WORKDIR/langfuse" && opt dc up -d )
  fi
  for l in $LABELS; do launchctl load "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true; done
  ok "Start requested. Verify: bash setup_ai_workstation.sh --status"
}
svc_stop() {
  brew_env; log "Stopping all services (data/models/configs preserved)"
  for l in $LABELS; do launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true; done
  if docker_up; then
    [ -d "$WORKDIR/langfuse" ] && ( cd "$WORKDIR/langfuse" && dc stop >/dev/null 2>&1 || true )
    docker stop open-webui searxng >/dev/null 2>&1 || true
  fi
  opt brew services stop ollama; opt colima stop; ok "All services stopped."
}
do_reset() {
  printf "%sRemove containers, services, OpenClaw config, and %s? [y/N] %s" "$c_yel" "$WORKDIR" "$c_reset"
  read -r r; case "$r" in y|Y|yes) ;; *) echo "Aborted."; exit 0 ;; esac
  for l in $LABELS; do launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true; rm -f "$LAUNCH_DIR/$l.plist"; done
  if docker_up; then
    [ -d "$WORKDIR/langfuse" ] && ( cd "$WORKDIR/langfuse" && dc down -v >/dev/null 2>&1 || true )
    docker rm -f open-webui searxng >/dev/null 2>&1 || true
    docker volume rm open-webui >/dev/null 2>&1 || true
  fi
  npm ls -g openclaw >/dev/null 2>&1 && opt npm uninstall -g openclaw
  rm -rf "$HOME/.openclaw" "$WORKDIR"
  ok "Reset complete. Homebrew, Xcode CLT, GUI apps, Ollama models, and Colima left intact."
  echo "  (To also remove models/engine: brew services stop ollama; colima delete; brew uninstall ollama colima ...)"
  exit 0
}

do_update() {
  brew_env
  log "Updating everything to latest"
  if have brew; then
    log "Homebrew formulae + casks"
    opt brew update; opt brew upgrade; opt brew upgrade --cask
  fi
  log "Ollama models (re-pull = upgrade to newest tag)"
  if http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
    for entry in "${MODELS[@]}"; do
      local tag="${entry%%|*}"
      printf "    pulling latest %s\n" "$tag"
      ollama pull "$tag" || warn "skip $tag (tag may have drifted)"
    done
  else warn "Ollama not running; start it then re-run --update."; fi
  log "Python tools (gateway venv + isolated Aider)"
  [ -x "$WORKDIR/.venv/bin/python" ] && opt uv pip install --python "$WORKDIR/.venv/bin/python" --upgrade $GW_PKGS
  have uv && opt uv tool upgrade aider-chat
  log "Node CLIs (OpenClaw, Copilot, Cline)"
  have npm && { opt npm update -g openclaw; opt npm update -g @github/copilot; opt npm update -g cline; }
  log "Docker images (Open WebUI, SearXNG, Langfuse)"
  if docker_up; then
    opt docker pull ghcr.io/open-webui/open-webui:main
    opt docker pull searxng/searxng:latest
    docker rm -f open-webui searxng >/dev/null 2>&1 || true
    [ -d "$WORKDIR/langfuse" ] && ( cd "$WORKDIR/langfuse" && opt dc pull )
  fi
  ok "Update complete. Run --restart to relaunch with the new versions."
  exit 0
}

# =============================================================================
#  FINAL SUMMARY
# =============================================================================
summary() {
  load_env
  brew_env
  local tg; tg="$(get_env TELEGRAM_BOT_TOKEN)"; local tgs; [ -n "$tg" ] && tgs="configured" || tgs="(not set)"
  local ds; ds="$(get_env OPENROUTER_API_KEY)"; local dss; [ -n "$ds" ] && dss="configured (backup)" || dss="(not set)"
  local lfk; lfk="$(get_env LANGFUSE_PUBLIC_KEY)"; local lfs; [ -n "$lfk" ] && lfs="configured" || lfs="(not set)"
  up() { "$@" >/dev/null 2>&1 && printf "%sLIVE%s" "$c_grn" "$c_reset" || printf "%sdown%s" "$c_red" "$c_reset"; }

  cat <<SUM

${c_grn}===================================================================${c_reset}
${c_grn}              INSTALL COMPLETE — SUMMARY                            ${c_reset}
${c_grn}===================================================================${c_reset}
Workspace:  $WORKDIR
Reference:  $WORKDIR/README_AI.md   (full how-to)
Secrets:    $WORKDIR/.env           (chmod 600 — keep private)

${c_cyn}-------------------------------------------------------------------
 1) WHAT WAS INSTALLED
-------------------------------------------------------------------${c_reset}
 System & dev tools
   • Homebrew, Xcode Command Line Tools
   • node $(node -v 2>/dev/null), uv (python), git, jq, wget, lazydocker
   • Java (Temurin): $(java -version 2>&1 | head -n1)
   • IDEs: VS Code$( [ -d "/Applications/IntelliJ IDEA CE.app" ] && echo ", IntelliJ IDEA CE" || echo " (IntelliJ CE — see README if missing)")
 AI engine (LOCAL — your primary brain)
   • Ollama + models: $(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | paste -sd, - 2>/dev/null)
 Local coding tools
   • Aider (terminal pair-programmer, isolated via uv tool — run 'aider')
   • Cline (autonomous terminal agent — 'ollama launch cline')
   • Continue (VS Code extension, config at ~/.continue/config.yaml)
 Backup / cloud tools (used only when you choose them)
   • LM Studio (GUI), GitHub Copilot CLI $( have copilot && echo "(installed)" || echo "(see README)")
 Phone-driven agent & GUI control
   • OpenClaw $( openclaw_ok && echo "$(openclaw --version 2>/dev/null)" || echo "(re-run if it failed — 'openclaw doctor')")
   • Peekaboo $( have peekaboo && echo "(installed)" || echo "(optional; not installed)")
 Containers (via Colima/Docker)
   • Open WebUI, SearXNG, Langfuse

${c_cyn}-------------------------------------------------------------------
 2) WHAT WAS CONFIGURED
-------------------------------------------------------------------${c_reset}
   • LiteLLM gateway → model aliases (reasoning, qwen3.6-coder, qwen3-coder-next,
     devstral, codestral, qwen2.5vl, gemma4); logs every call to Langfuse.
   • OpenRouter cloud backup: $dss
   • Ollama capped at $OLLAMA_MAX_LOADED loaded model(s) to protect unified memory.
   • Continue + Aider pointed at local models (no data leaves the Mac).
   • Telegram bot: $tgs        Langfuse API keys: $lfs
   • Services bind 0.0.0.0 — reachable from other devices on your Wi-Fi by LAN IP.
   • Auto-start at login (launchd): Colima, LiteLLM gateway, dashboard.

${c_cyn}-------------------------------------------------------------------
 3) WHAT'S RUNNING RIGHT NOW   (also reachable via your Mac's LAN IP)
-------------------------------------------------------------------${c_reset}
   Live dashboard   http://localhost:$PORT_DASHBOARD    [$(up http_ok "http://localhost:$PORT_DASHBOARD/")]   services + hardware metrics + agent runs
   Open WebUI       http://localhost:$PORT_OPENWEBUI    [$(up http_ok "http://localhost:$PORT_OPENWEBUI/")]   chat with web search
   Langfuse         http://localhost:$PORT_LANGFUSE    [$(up langfuse_ok)]   agent traces / logs
   SearXNG          http://localhost:$PORT_SEARXNG    [$(up searxng_ok)]   private web search
   LiteLLM gateway  http://localhost:$PORT_GATEWAY    [$(up http_ok "http://localhost:$PORT_GATEWAY/health/liveliness")]   model routing
   Ollama           http://localhost:$PORT_OLLAMA   [$(up http_ok "http://localhost:$PORT_OLLAMA/api/tags")]   local models

${c_cyn}-------------------------------------------------------------------
 4) NEXT STEPS
-------------------------------------------------------------------${c_reset}
   1. Open the dashboard:  http://localhost:$PORT_DASHBOARD
   2. Open WebUI → Settings → enable Web Search → point at SearXNG (goal: fresh answers).
   3. If you skipped it:  openclaw onboard --install-daemon  (provider ollama, model qwen3.6:35b-a3b).
   4. For Mac/GUI control: System Settings → Privacy & Security → grant OpenClaw/Peekaboo
      Accessibility + Screen Recording (macOS-protected; can't be scripted).
   5. Code now:  cd <repo> && aider --model ollama/qwen3.6:27b

${c_cyn}-------------------------------------------------------------------
 5) MANAGING / RE-RUNNING
-------------------------------------------------------------------${c_reset}
   • Re-run this script anytime — healthy steps are skipped, broken ones are repaired.
   • Status:  bash setup_ai_workstation.sh --status
   • Start | Stop | Restart | Update | Reset:  --start | --stop | --restart | --update | --reset
   • --update pulls the latest of EVERYTHING (brew, models, Python tools, Docker images).

${c_yel}-------------------------------------------------------------------
 HONEST LIMITS
-------------------------------------------------------------------${c_reset}
   • GUI/app control (mouse, opening apps) is the least reliable part of any agent today
     and local models drive it worse than frontier cloud models — supervise; keep approvals on.
   • Services bind 0.0.0.0 (open on your Wi-Fi). Safe on trusted home networks; on public
     Wi-Fi, switch bindings back to 127.0.0.1 or add passwords.
${c_grn}===================================================================${c_reset}
SUM
}

# =============================================================================
#  MAIN
# =============================================================================
case "${1:-}" in
  --status)  svc_status; exit 0 ;;
  --start)   svc_start;  exit 0 ;;
  --stop)    svc_stop;   exit 0 ;;
  --restart) svc_stop; svc_start; exit 0 ;;
  --reset)   do_reset ;;
  --update)  do_update ;;
  -h|--help) sed -n '4,43p' "$0"; exit 0 ;;
esac
main() {
  preflight
  setup_xcode_clt
  setup_homebrew
  setup_core_tools
  setup_java
  setup_ides
  setup_gui_apps
  setup_ollama
  setup_python
  setup_colima
  setup_openwebui
  setup_searxng
  setup_langfuse
  setup_cloud_optional
  setup_litellm
  setup_continue
  write_dashboard
  setup_services
  collect_chat_tokens
  setup_openclaw
  setup_peekaboo
  scaffold_workspace
  summary
}
main "$@"
