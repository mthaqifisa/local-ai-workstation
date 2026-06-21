#!/usr/bin/env bash
# =============================================================================
#  script3.sh — Local AI Development Workstation Setup
#  fresh-Mac bootstrap (Apple Silicon / Intel, macOS)
#
#  This script provisions the INFRASTRUCTURE only:
#    - Ollama + local models (core team models + swappable alternates)
#    - LiteLLM gateway (OpenAI-compatible proxy in front of Ollama)
#    - Docker services: Open WebUI, SearXNG, Langfuse, Portainer
#    - A live status dashboard (system / services / models)
#    - IDEs: VS Code + IntelliJ IDEA
#    - OpenClaw (the phone-driven agent that actually runs your team)
#
#  The multi-agent team (Orion/Ada/Mira/Leo/Nova/Cipher/Vox) is NO LONGER
#  defined here. That team now lives inside OpenClaw. See README.md for the
#  copy-paste prompt that sets the team up inside OpenClaw.
#
#  Path mappings:
#    - Documents -> ~/OneDrive/AI-Agent/
#    - Source Code -> ~/SourceCode/
#
#  Control: --bootstrap | --status | --start | --stop | --restart
#           | --pull-models | --uninstall | --help
# =============================================================================
set -uo pipefail
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

# ─────────────────────────────── CONFIGURATION ────────────────────────────────
WORKDIR="${HOME}/.local-ai-workstation"
ENV_FILE="$WORKDIR/.env"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

DOCS_WORKSPACE="${HOME}/OneDrive/AI-Agent"
CODE_WORKSPACE="${HOME}/SourceCode"
MASTER_EMAIL="mthaqifisa@pm.me"

COLIMA_CPU="${COLIMA_CPU:-8}"
COLIMA_MEM="${COLIMA_MEM:-16}"
COLIMA_DISK="${COLIMA_DISK:-120}"

OLLAMA_MAX_LOADED="${OLLAMA_MAX_LOADED:-1}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-3m}"

PORT_OLLAMA=11434; PORT_OPENWEBUI=3001; PORT_LANGFUSE=3000
PORT_SEARXNG=8888; PORT_GATEWAY=4000;  PORT_DASHBOARD=8800
PORT_PORTAINER=9001; PORT_OPENCLAW=18789

# Core models pulled automatically during bootstrap.
# These are the models your OpenClaw team will reference.
MODELS=(
  "qwen3.6:35b-a3b|Orchestrator / coordinator, always loaded (~26 GB)"
  "qwen3-coder:30b-a3b-q4_K_M|Primary coder model (~18 GB)"
  "gemma4:26b|UI/UX + design + vision (~18 GB)"
  "qwen2.5:72b|Heavy reasoning: PM, QA, security, trends (~44 GB)"
  "nomic-embed-text|Embeddings for RAG/search (~270 MB)"
)

# Swappable models — NOT pulled automatically (would total ~200+ GB).
# Registered in litellm.config.yaml as alternates. Pull on demand with:
#   ollama pull <tag>     or     ./script3.sh --pull-models
SWAPPABLE_MODELS=(
  "qwen3-coder:30b-a3b-q4_K_M|Coding|Agentic coding, MoE 3.3B active, fast on 64GB (~18 GB)"
  "qwen3-coder:30b-a3b-q8_0|Coding|Same model, q8_0 quant — higher quality, more RAM (~32 GB)"
  "qwen2.5-coder:32b|Coding|Accuracy leader: GPT-4o-level HumanEval, tops EvalPlus/LiveCodeBench (~20 GB)"
  "qwen3.6:27b|Coding|Best dense coder on consumer HW, 77% SWE-bench (~22 GB)"
  "devstral:24b|Coding|Best agentic coder in class, 68% SWE-bench Verified, Apache 2.0 (~14 GB)"
  "deepseek-coder-v2:16b|Coding|Lightweight fast coder, great Python/JS (~9 GB)"
  "codestral:22b|Coding|Mistral coder, strong FIM autocomplete (~13 GB)"
  "mistral-small:24b|Reasoning|Trained to say 'I don't know' — lowest hallucination, great for RAG (~14 GB)"
  "deepseek-r1:14b|Reasoning|Chain-of-thought debugging & code analysis, MIT (~9 GB)"
  "deepseek-r1:32b|Reasoning|Larger CoT reasoner for harder algorithmic problems (~20 GB)"
  "deepseek-r1:70b|Reasoning|70B CoT reasoner (Llama3.3 distill), max reasoning, fits 64GB solo (~43 GB)"
  "phi4:14b|Reasoning|Microsoft Phi-4, best reasoning-per-GB, strong math/logic, MIT (~9 GB)"
  "glm-4.7-flash|Reasoning|GLM strongest in 30B class, lightweight agentic"
  "llama3.3:70b|General|Meta's flagship 70B, strong all-round chat/coding, fits 64GB solo (~43 GB)"
  "mistral:7b|General|Mistral 7B v0.3 — fast, low-hallucination lightweight general model (~4 GB)"
  "gemma4:31b|General|Google Gemma 4 dense 31B, vision + tool calling, reasoning (~20 GB)"
)

# ───────────────────────────────── LOGGING ────────────────────────────────────
c_reset=$'\033[0m'; c_blue=$'\033[1;34m'; c_grn=$'\033[1;32m'
c_yel=$'\033[1;33m'; c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'
log()  { printf "\n%s==>%s %s\n" "$c_blue" "$c_reset" "$*"; }
ok()   { printf "%s  ok%s  %s\n" "$c_grn" "$c_reset" "$*"; }
warn() { printf "%s   !%s  %s\n" "$c_yel" "$c_reset" "$*"; }
err()  { printf "%s   x%s  %s\n" "$c_red" "$c_reset" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
opt()  { "$@" || warn "non-fatal: $*"; }
hr()   { printf "%s%s%s\n" "$c_cyn" "────────────────────────────────────────────────────" "$c_reset"; }

# ─────────────────────────────── .env HELPERS ─────────────────────────────────
ensure_env_file() {
    mkdir -p "$WORKDIR"
    [ -f "$ENV_FILE" ] || : > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
}
get_env() { ensure_env_file; sed -n "s/^$1=//p" "$ENV_FILE" | head -n1; }
set_env() {
    ensure_env_file
    local key="$1" val="$2" tmp; tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"; export "$key=$val"
}
load_env() { ensure_env_file; set -a; . "$ENV_FILE"; set +a; }

# ─────────────────────────────── VALIDATORS ───────────────────────────────────
validate_telegram() {
    have curl || return 0
    curl -fsS "https://api.telegram.org/bot$1/getMe" 2>/dev/null | grep -q '"ok":true'
}
press_enter() { printf "\n%sPress Enter to continue…%s " "$c_yel" "$c_reset"; read -r _; }
prompt_secret() {
    local var="$1" title="$2" validator="$3" tut_fn="$4" current
    current="$(get_env "$var")"
    if [ -n "$current" ]; then
        if [ -z "$validator" ] || "$validator" "$current"; then
            ok "$title already configured."; return 0; fi
        warn "$title set but failed validation; re-enter."
    fi
    [ -n "$tut_fn" ] && { hr; "$tut_fn"; hr; }
    local tries=0 val=""
    while :; do
        printf "%sPaste %s (or 'skip'): %s" "$c_cyn" "$title" "$c_reset"; read -r val
        case "$val" in skip|SKIP) warn "Skipped $title."; return 0 ;; esac
        [ -z "$val" ] && { warn "Empty — try again."; continue; }
        if [ -z "$validator" ] || "$validator" "$val"; then break; fi
        tries=$((tries+1))
        [ "$tries" -ge 3 ] && { warn "Saving as-is."; break; }
        warn "Validation failed. Try again."
    done
    set_env "$var" "$val"; ok "$title saved."
}
tut_telegram() {
cat <<'TUTEOF'
####  TELEGRAM BOT SETUP  ####
  1. Open Telegram → search @BotFather (official, blue check).
  2. Send /newbot, name it, give username ending in 'bot'.
  3. Copy the token (123456789:ABCdef...).
  4. Also get your chat ID from @userinfobot.
##############################
TUTEOF
}

# ─────────────────────────────── HEALTH PROBES ────────────────────────────────
http_ok()          { have curl && curl -fsS -m 4 "$1" >/dev/null 2>&1; }
docker_up()        { have docker && docker info >/dev/null 2>&1; }
container_running(){ docker_up && [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
searxng_ok()       { http_ok "http://localhost:$PORT_SEARXNG/"; }
langfuse_ok()      { http_ok "http://localhost:$PORT_LANGFUSE/api/public/health"; }
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }

ollama_is_dmg()  { [ -d "/Applications/Ollama.app" ]; }
ollama_is_brew() { ! ollama_is_dmg && brew list ollama >/dev/null 2>&1; }
ollama_start() {
    if ollama_is_dmg; then
        open -a Ollama 2>/dev/null \
            && ok "Ollama.app launched." \
            || warn "Could not launch Ollama.app — open it manually."
    elif ollama_is_brew; then
        opt brew services start ollama
    else
        warn "Ollama not found. Install from https://ollama.com"
    fi
}
ollama_stop() {
    if ollama_is_dmg; then
        osascript -e 'quit app "Ollama"' 2>/dev/null \
            || pkill -x Ollama 2>/dev/null || true
    elif ollama_is_brew; then
        opt brew services stop ollama
    fi
}

# =============================================================================
#  WORKSPACE SETUP
# =============================================================================
setup_workspaces() {
    log "Verifying output folders..."
    mkdir -p "$DOCS_WORKSPACE" "$CODE_WORKSPACE"
    for subdir in proposals reports trends screenshots; do
        mkdir -p "$DOCS_WORKSPACE/$subdir"
    done
    set_env AI_WORKSPACE "$DOCS_WORKSPACE"
    set_env CODE_WORKSPACE "$CODE_WORKSPACE"
    ok "Workspace folders created."
    ok "Documents go to: $DOCS_WORKSPACE"
    ok "Source code goes to: $CODE_WORKSPACE"
}

# =============================================================================
#  PHASE 0 — PREFLIGHT
# =============================================================================
preflight() {
    log "Preflight Check"
    [ "$(uname -s)" = "Darwin" ] || { err "macOS only."; exit 1; }
    ok "macOS $(sw_vers -productVersion 2>/dev/null) on $(uname -m)"
    ensure_env_file
    local free_gb; free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}' || echo 999)
    [ "$free_gb" -lt 150 ] && warn "Low disk: ${free_gb} GB free (need ~150 GB for models + workspace)" \
                           || ok "Disk: ${free_gb} GB free"
    cat <<BANNEREOF

${c_yel}Building local AI Workstation inside: ${WORKDIR}
Outputs:
  - Generated documents: ${DOCS_WORKSPACE}
  - Generated source code: ${CODE_WORKSPACE}
BANNEREOF
    printf "Proceed? [y/N] "; read -r r
    case "$r" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
}

# =============================================================================
#  PHASE 1 — SYSTEM & DEV TOOLS
# =============================================================================
setup_xcode_clt() {
    log "Xcode Command Line Tools"
    if xcode-select -p >/dev/null 2>&1; then ok "Already installed."; return; fi
    warn "Installer popup will appear — click Install and wait."
    xcode-select --install >/dev/null 2>&1 || true
    printf "Waiting"; while ! xcode-select -p >/dev/null 2>&1; do printf "."; sleep 5; done
    printf "\n"; ok "Xcode CLT ready."
}

setup_homebrew() {
    log "Homebrew"
    if ! have brew; then
        log "Installing Homebrew non-interactively..."
        NONINTERACTIVE=1 /bin/bash -c \
            "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            || { err "Homebrew install failed."; exit 1; }
    fi
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"

    grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null || \
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
    ok "$(brew --version | head -n1)"
}

setup_core_tools() {
    log "Installing system packages..."
    if ollama_is_dmg; then ok "Ollama present (macOS application)"
    elif have ollama; then ok "Ollama present ($(ollama --version 2>/dev/null))"
    else opt brew install ollama; fi

    # CLI formulae — each checked before install (idempotent)
    for p in colima docker docker-compose node git jq wget lazydocker uv socat \
             cairo pango gdk-pixbuf libffi; do
        if brew list "$p" >/dev/null 2>&1; then ok "$p present"; else
            log "Installing $p..."; opt brew install "$p"
        fi
    done

    have node && ok "node $(node -v)"
    have uv   && ok "uv $(uv --version 2>/dev/null)"
    grep -q '.local/bin' "$HOME/.zprofile" 2>/dev/null || \
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"

    if have npm; then
        if ! npm ls -g puppeteer >/dev/null 2>&1; then
            log "Installing Puppeteer (E2E browser testing)..."
            opt npm install -g puppeteer
            npx --yes puppeteer browsers install chrome >/dev/null 2>&1 \
                && ok "Chrome for Testing installed" \
                || warn "Could not pre-download Chrome"
        else ok "puppeteer present"; fi
    fi
}

# ── IDEs: VS Code + IntelliJ IDEA (each checked before installing) ─────────────
setup_ides() {
    log "Developer IDEs (VS Code + IntelliJ IDEA)"
    have brew || { warn "Homebrew missing; skipping IDE install."; return; }

    # VS Code
    if [ -d "/Applications/Visual Studio Code.app" ] || brew list --cask visual-studio-code >/dev/null 2>&1; then
        ok "VS Code present."
    else
        log "Installing VS Code..."
        opt brew install --cask visual-studio-code
    fi
    # Register the 'code' CLI shim if the app exists
    if [ -d "/Applications/Visual Studio Code.app" ] && ! have code; then
        local code_bin="/Applications/Visual Studio Code.app/Contents/Resources/app/bin"
        grep -q "Visual Studio Code.app/Contents/Resources/app/bin" "$HOME/.zprofile" 2>/dev/null || \
            echo "export PATH=\"\$PATH:$code_bin\"" >> "$HOME/.zprofile"
        ok "VS Code 'code' CLI registered (restart shell to use)."
    fi

    # IntelliJ IDEA (Ultimate cask: intellij-idea)
    if [ -d "/Applications/IntelliJ IDEA.app" ] || brew list --cask intellij-idea >/dev/null 2>&1; then
        ok "IntelliJ IDEA present."
    else
        # Remove the old Community Edition if it was previously installed
        if brew list --cask intellij-idea-ce >/dev/null 2>&1 || [ -d "/Applications/IntelliJ IDEA CE.app" ]; then
            log "Removing old IntelliJ IDEA Community Edition..."
            opt brew uninstall --cask intellij-idea-ce
        fi
        log "Installing IntelliJ IDEA..."
        opt brew install --cask intellij-idea
    fi
}

# =============================================================================
#  PHASE 2 — OLLAMA MODELS
# =============================================================================
setup_ollama() {
    log "Ollama + local models"
    if ! grep -q OLLAMA_MAX_LOADED_MODELS "$HOME/.zprofile" 2>/dev/null; then
        { echo "export OLLAMA_MAX_LOADED_MODELS=$OLLAMA_MAX_LOADED"
          echo "export OLLAMA_KEEP_ALIVE=$OLLAMA_KEEP_ALIVE"
          echo "export OLLAMA_HOST=0.0.0.0:$PORT_OLLAMA"; } >> "$HOME/.zprofile"
    fi
    export OLLAMA_MAX_LOADED_MODELS="$OLLAMA_MAX_LOADED"
    export OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE"
    export OLLAMA_HOST="0.0.0.0:$PORT_OLLAMA"
    if ! http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
        ollama_start
        for _ in $(seq 1 20); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
    fi
    http_ok "http://localhost:$PORT_OLLAMA/api/tags" \
        && ok "Ollama up on :$PORT_OLLAMA (bound 0.0.0.0 for LAN access)" \
        || { warn "Ollama not responding. Run 'ollama serve' then re-run."; return; }

    local installed; installed="$(ollama list 2>/dev/null)"
    for entry in "${MODELS[@]}"; do
        local tag="${entry%%|*}" desc="${entry#*|}"
        if printf "%s" "$installed" | grep -q "^${tag%%:*}" && ollama show "$tag" >/dev/null 2>&1; then
            ok "model present: $tag"
        else
            printf "  pulling %s  (%s)\n" "$tag" "$desc"
            ollama pull "$tag" || warn "pull failed for '$tag' — verify at ollama.com/library"
        fi
    done
}

# Interactive puller for swappable alternates (not pulled during bootstrap)
pull_swappable_models() {
    log "Optional swappable models"
    if ! http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
        ollama_start
        for _ in $(seq 1 20); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
    fi
    http_ok "http://localhost:$PORT_OLLAMA/api/tags" \
        || { warn "Ollama not responding. Run 'ollama serve' then re-run."; return; }

    local installed; installed="$(ollama list 2>/dev/null)"
    hr
    echo "These are optional alternates. Each is large — pull only what you need."
    echo "They are already registered in litellm.config.yaml so your team can use them once pulled."
    echo "Note: model tags change over time. If a pull fails, check ollama.com/library for the current name."
    hr
    local i=1
    for entry in "${SWAPPABLE_MODELS[@]}"; do
        local tag="${entry%%|*}" rest="${entry#*|}" cat="${rest%%|*}" desc="${rest#*|}"
        local mark="  "
        printf "%s" "$installed" | grep -q "^${tag%%:*}" && mark="✓ "
        printf "  %s%2d) [%-9s] %-28s %s\n" "$mark" "$i" "$cat" "$tag" "$desc"
        i=$((i+1))
    done
    hr
    printf "Enter numbers to pull (space-separated), 'all', or Enter to skip: "
    read -r picks
    [ -z "$picks" ] && { ok "No optional models pulled."; return; }

    local idx=1
    for entry in "${SWAPPABLE_MODELS[@]}"; do
        local tag="${entry%%|*}"
        local want=0
        case "$picks" in
            all|ALL) want=1 ;;
            *) for n in $picks; do [ "$n" = "$idx" ] && want=1; done ;;
        esac
        if [ "$want" = "1" ]; then
            printf "  pulling %s ...\n" "$tag"
            ollama pull "$tag" || warn "pull failed for '$tag' — verify at ollama.com/library"
        fi
        idx=$((idx+1))
    done
    ok "Swappable model pulls complete."
}

# =============================================================================
#  PHASE 3 — PYTHON SETUP (for the dashboard + litellm gateway)
# =============================================================================
venv_ok() {
    [ -x "$WORKDIR/.venv/bin/python" ] && [ -x "$WORKDIR/.venv/bin/litellm" ] \
    && "$WORKDIR/.venv/bin/python" -c \
       "import litellm,flask,requests,psutil,yaml" >/dev/null 2>&1
}

setup_python() {
    log "Python virtualenv setup..."
    if venv_ok; then ok "Python virtualenv verified."; return; fi

    [ -d "$WORKDIR/.venv" ] && { warn "venv incomplete — rebuilding."; rm -rf "$WORKDIR/.venv"; }
    (cd "$WORKDIR" && uv venv --python 3.12 .venv) || { err "uv venv failed."; return; }

    local UV; UV="$(command -v uv 2>/dev/null || echo /opt/homebrew/bin/uv)"
    "$UV" pip install --python "$WORKDIR/.venv/bin/python" \
        "litellm[proxy]" \
        openai \
        "langfuse>=2.0,<3.0" \
        python-dotenv \
        flask \
        requests \
        rich \
        psutil \
        pyyaml \
        || { err "Failed to install Python requirements."; return; }

    if venv_ok; then
        ok "Virtual environment setup complete."
        for svc in com.aiws.litellm com.aiws.dashboard; do
            if [ -f "$LAUNCH_DIR/$svc.plist" ]; then
                launchctl unload "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
                launchctl load   "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
            fi
        done
    else
        err "Venv check failed after package installation."
    fi
}

# =============================================================================
#  PHASE 4 — DOCKER (Colima) & SERVICES
# =============================================================================
setup_colima() {
    log "Docker engine startup (Colima)"
    if docker_up; then ok "Docker already running."; return; fi
    opt colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"

    log "Waiting for Docker daemon to initialize..."
    for _ in $(seq 1 30); do
        docker_up && break
        sleep 1
    done
    docker_up && ok "Docker daemon active via Colima" || warn "Docker startup timed out."
}

setup_openwebui() {
    log "Open WebUI container"
    docker_up || { warn "Docker not active; skipping Open WebUI."; return; }
    docker rm -f open-webui >/dev/null 2>&1 || true
    opt docker volume create open-webui
    docker run -d --name open-webui --restart unless-stopped \
        -p "0.0.0.0:$PORT_OPENWEBUI:8080" \
        -e OLLAMA_BASE_URL="http://host.docker.internal:$PORT_OLLAMA" \
        --add-host=host.docker.internal:host-gateway \
        -v open-webui:/app/backend/data \
        ghcr.io/open-webui/open-webui:main \
        && ok "Open WebUI running -> http://0.0.0.0:$PORT_OPENWEBUI" \
        || warn "Failed to launch Open WebUI"
}

setup_searxng() {
    log "SearXNG private search container"
    docker_up || { warn "Docker not active; skipping SearXNG."; return; }
    local SX="$WORKDIR/searxng"; mkdir -p "$SX"
    if [ ! -f "$SX/settings.yml" ]; then
        local secret; secret="$(openssl rand -hex 24)"
        cat > "$SX/settings.yml" <<SXEOF
use_default_settings: true
server:
  secret_key: "$secret"
  bind_address: "0.0.0.0"
  limiter: false
search:
  formats: [html, json]
SXEOF
    fi
    docker rm -f searxng >/dev/null 2>&1 || true
    docker run -d --name searxng --restart unless-stopped \
        -p "0.0.0.0:$PORT_SEARXNG:8080" \
        -v "$SX:/etc/searxng" \
        searxng/searxng:latest \
        && ok "SearXNG running -> http://0.0.0.0:$PORT_SEARXNG" \
        || warn "Failed to launch SearXNG"
}

setup_langfuse() {
    log "Langfuse tracing server"
    docker_up || { warn "Docker not active; skipping Langfuse."; return; }
    langfuse_ok && { ok "Langfuse already running."; return; }
    local LF="$WORKDIR/langfuse"
    [ -d "$LF/.git" ] && (cd "$LF" && dc down >/dev/null 2>&1 || true)
    [ -d "$LF/.git" ] || opt git clone --depth=1 https://github.com/langfuse/langfuse.git "$LF"
    (cd "$LF" && opt dc up -d)

    printf "Waiting for Langfuse container"
    for _ in $(seq 1 60); do langfuse_ok && break; printf "."; sleep 2; done; printf "\n"
    langfuse_ok && ok "Langfuse active." || warn "Langfuse startup delayed."

    load_env
    local pk; pk="$(get_env LANGFUSE_PUBLIC_KEY)"
    [ -n "$pk" ] && { ok "Langfuse keys are set."; return; }
    cat <<'LFEOF'

####  LANGFUSE API KEYS SETUP (optional)  ####
  1. Open http://127.0.0.1:3000 → Create local account.
  2. Organization → Project → Settings → API Keys → Create.
  3. Copy PUBLIC (pk-lf-...) and SECRET (sk-lf-...).
########################################
LFEOF
    press_enter
    printf "Paste PUBLIC key (or 'skip'): "; read -r pk
    case "$pk" in skip|SKIP|"") warn "Skipping Langfuse tracking."; return ;; esac
    printf "Paste SECRET key: "; read -r sk
    set_env LANGFUSE_PUBLIC_KEY "$pk"; set_env LANGFUSE_SECRET_KEY "$sk"
    set_env LANGFUSE_HOST "http://0.0.0.0:$PORT_LANGFUSE"; ok "Langfuse configurations saved."
}

setup_portainer() {
    log "Portainer UI container"
    docker_up || { warn "Docker not active; skipping Portainer."; return; }
    opt docker volume create portainer_data
    docker rm -f portainer >/dev/null 2>&1 || true
    docker run -d --name portainer --restart unless-stopped \
        -p "0.0.0.0:$PORT_PORTAINER:9000" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v portainer_data:/data \
        portainer/portainer-ce:latest \
        && ok "Portainer active -> http://0.0.0.0:$PORT_PORTAINER" \
        || warn "Failed to launch Portainer"
}

# Brings up ALL Docker-backed services in order.
setup_docker_services() {
    log "Bringing up Docker-backed services"
    setup_colima
    docker_up || { warn "Docker did not start; skipping containers."; return; }
    setup_openwebui
    setup_searxng
    setup_langfuse
    setup_portainer
    ok "Docker services dispatched."
}

# =============================================================================
#  PHASE 5 — LiteLLM GATEWAY
# =============================================================================
setup_litellm() {
    log "LiteLLM Gateway Setup"
    load_env
    local CFG="$WORKDIR/litellm.config.yaml"
    if [ -f "$CFG" ]; then
        ok "litellm.config.yaml already exists."
    else
        cat > "$CFG" <<'LLMEOF'
# LiteLLM gateway config. Exposes an OpenAI-compatible endpoint at
# http://0.0.0.0:4000/v1 in front of your local Ollama models.
# Point OpenClaw (and any other agent/tool) at this gateway.
#
# The friendly model_name on the left is what you reference from OpenClaw.
model_list:
  # ── Recommended role aliases (map a job to a model) ────────────────────────
  - model_name: orchestrator
    litellm_params: { model: ollama/qwen3.6:35b-a3b,            api_base: http://0.0.0.0:11434 }
  - model_name: coder
    litellm_params: { model: ollama/qwen3-coder:30b-a3b-q4_K_M, api_base: http://0.0.0.0:11434 }
  - model_name: reasoner
    litellm_params: { model: ollama/qwen2.5:72b,                api_base: http://0.0.0.0:11434 }
  - model_name: designer
    litellm_params: { model: ollama/gemma4:26b,                 api_base: http://0.0.0.0:11434 }
  - model_name: embed
    litellm_params: { model: ollama/nomic-embed-text,           api_base: http://0.0.0.0:11434 }

  # ── Swappable alternates ───────────────────────────────────────────────────
  # Pull the model first ( ollama pull <tag>  or  ./script3.sh --pull-models )
  # then reference one of these names from OpenClaw to switch.
  - model_name: coder-qwen3-30b
    litellm_params: { model: ollama/qwen3-coder:30b-a3b-q4_K_M, api_base: http://0.0.0.0:11434 }
  - model_name: coder-qwen3-30b-q8
    litellm_params: { model: ollama/qwen3-coder:30b-a3b-q8_0,   api_base: http://0.0.0.0:11434 }
  - model_name: coder-qwen25-32b
    litellm_params: { model: ollama/qwen2.5-coder:32b,          api_base: http://0.0.0.0:11434 }
  - model_name: coder-qwen36-27b
    litellm_params: { model: ollama/qwen3.6:27b,                api_base: http://0.0.0.0:11434 }
  - model_name: coder-devstral-24b
    litellm_params: { model: ollama/devstral:24b,               api_base: http://0.0.0.0:11434 }
  - model_name: coder-deepseek-v2-16b
    litellm_params: { model: ollama/deepseek-coder-v2:16b,      api_base: http://0.0.0.0:11434 }
  - model_name: coder-codestral-22b
    litellm_params: { model: ollama/codestral:22b,              api_base: http://0.0.0.0:11434 }
  - model_name: reason-mistral-small-24b
    litellm_params: { model: ollama/mistral-small:24b,          api_base: http://0.0.0.0:11434 }
  - model_name: reason-deepseek-r1-14b
    litellm_params: { model: ollama/deepseek-r1:14b,            api_base: http://0.0.0.0:11434 }
  - model_name: reason-deepseek-r1-32b
    litellm_params: { model: ollama/deepseek-r1:32b,            api_base: http://0.0.0.0:11434 }
  - model_name: reason-deepseek-r1-70b
    litellm_params: { model: ollama/deepseek-r1:70b,            api_base: http://0.0.0.0:11434 }
  - model_name: reason-phi4-14b
    litellm_params: { model: ollama/phi4:14b,                   api_base: http://0.0.0.0:11434 }
  - model_name: reason-glm-47-flash
    litellm_params: { model: ollama/glm-4.7-flash,              api_base: http://0.0.0.0:11434 }
  - model_name: general-llama33-70b
    litellm_params: { model: ollama/llama3.3:70b,               api_base: http://0.0.0.0:11434 }
  - model_name: general-mistral-7b
    litellm_params: { model: ollama/mistral:7b,                 api_base: http://0.0.0.0:11434 }
  - model_name: general-gemma4-31b
    litellm_params: { model: ollama/gemma4:31b,                 api_base: http://0.0.0.0:11434 }
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  drop_params: true
  request_timeout: 1200
LLMEOF
        ok "litellm.config.yaml written."
    fi

    # repair_venv.sh - self healing virtual environment script
    cat > "$WORKDIR/repair_venv.sh" <<'REPAIREOF'
#!/usr/bin/env bash
WORKDIR="${WORKDIR:-$HOME/.local-ai-workstation}"
VENV="$WORKDIR/.venv"
LOG="[repair_venv $(date '+%H:%M:%S')]"

venv_healthy() {
    [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/litellm" ] \
    && "$VENV/bin/python" -c "import litellm,flask,requests,psutil,yaml" >/dev/null 2>&1
}

venv_healthy && exit 0
echo "$LOG virtualenv dependencies missing — rebuilding..." >&2
rm -rf "$VENV"
cd "$WORKDIR" || exit 1
UV="$(command -v uv 2>/dev/null || echo /opt/homebrew/bin/uv)"
if [ -z "$UV" ]; then
    echo "$LOG uv binary not found. install first." >&2; exit 1
fi
"$UV" venv --python 3.12 .venv >&2 || exit 1
"$UV" pip install --python "$VENV/bin/python" \
    "litellm[proxy]" openai "langfuse>=2.0,<3.0" python-dotenv flask \
    requests rich psutil pyyaml >&2 || exit 1
REPAIREOF
    chmod +x "$WORKDIR/repair_venv.sh"

    # Launchers with absolute paths
    cat > "$WORKDIR/start_gateway.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/litellm" --config "\$WORKDIR/litellm.config.yaml" --port $PORT_GATEWAY --host 0.0.0.0
SHEOF
    chmod +x "$WORKDIR/start_gateway.sh"

    cat > "$WORKDIR/start_dashboard.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/python" "\$WORKDIR/dashboard/app.py"
SHEOF
    chmod +x "$WORKDIR/start_dashboard.sh"
    ok "Service launchers created."
}

# =============================================================================
#  PHASE 6 — LIVE DASHBOARD (system / services / models)
# =============================================================================
write_dashboard() {
    log "Dashboard setup"
    local DD="$WORKDIR/dashboard"; mkdir -p "$DD"
    cat > "$DD/app.py" <<'DASHEOF'
#!/usr/bin/env python3
"""Local AI Workstation Dashboard — system status, services, and model catalog."""
import os, json, datetime, socket
import requests, psutil
from flask import Flask, jsonify
from dotenv import load_dotenv

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/.local-ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))

_home = os.path.expanduser("~")
WORKSPACE_DOCS = os.environ.get("AI_WORKSPACE",   os.path.join(_home, "OneDrive", "AI-Agent"))
WORKSPACE_CODE = os.environ.get("CODE_WORKSPACE", os.path.join(_home, "SourceCode"))

P_OLLAMA="11434"; P_GATEWAY="4000"; P_OPENWEBUI="3001"
P_SEARXNG="8888"; P_LANGFUSE="3000"; P_DASHBOARD="8800"
P_PORTAINER="9001"; P_OPENCLAW="18789"

def _lan_host():
    h = os.environ.get("LAN_HOST", "").strip()
    if h: return h
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "0.0.0.0"

LAN_HOST = _lan_host()
app = Flask(__name__)

# (display name, health-probe URL, port, purpose)
SERVICES = [
    ("Ollama",          f"http://127.0.0.1:{P_OLLAMA}/api/tags",            P_OLLAMA,    "Model engine"),
    ("LiteLLM Gateway", f"http://127.0.0.1:{P_GATEWAY}/health/liveliness",  P_GATEWAY,   "LLM proxy (point OpenClaw here)"),
    ("OpenClaw",        f"http://127.0.0.1:{P_OPENCLAW}/",                  P_OPENCLAW,  "Your agent team (phone-driven)"),
    ("Open WebUI",      f"http://127.0.0.1:{P_OPENWEBUI}/",                 P_OPENWEBUI, "Chat application"),
    ("SearXNG",         f"http://127.0.0.1:{P_SEARXNG}/",                   P_SEARXNG,   "Private search"),
    ("Langfuse",        f"http://127.0.0.1:{P_LANGFUSE}/api/public/health", P_LANGFUSE,  "Trace telemetry"),
    ("Portainer",       f"http://127.0.0.1:{P_PORTAINER}/",                 P_PORTAINER, "Docker dashboard"),
    ("Dashboard",       f"http://127.0.0.1:{P_DASHBOARD}/",                 P_DASHBOARD, "This page"),
]
OPENCLAW_PATH = "/chat?session=main"

# Every model the workstation can use, grouped by specialty, with a "when to use" note.
MODEL_CATALOG = {
    "Orchestration": [
        ("qwen3.6:35b-a3b", "Fast MoE coordinator. Use for planning, routing, and tool orchestration."),
    ],
    "Coding": [
        ("qwen3-coder:30b-a3b-q4_K_M", "Default coder. Best balance of speed + agentic coding on 64GB. Daily driver."),
        ("qwen3-coder:30b-a3b-q8_0",   "Same model, q8_0 quant. Higher quality, ~32 GB. Use when accuracy beats speed."),
        ("qwen2.5-coder:32b",          "Accuracy leader. GPT-4o-level HumanEval, tops EvalPlus/LiveCodeBench. Best for correctness."),
        ("qwen3.6:27b",                "Best dense coder on consumer HW (77% SWE-bench). One model for code + chat."),
        ("devstral:24b",               "Best agentic coder in its class (68% SWE-bench Verified). Apache 2.0, runs in 32GB."),
        ("deepseek-coder-v2:16b",      "Lightweight + fast. Great for quick Python/JS edits and autocomplete on low RAM."),
        ("codestral:22b",              "Mistral's coder. Excellent fill-in-the-middle (FIM) autocomplete in editors."),
    ],
    "Reasoning": [
        ("qwen2.5:72b",        "Deep reasoning for PM, QA analysis, security, and trend work."),
        ("mistral-small:24b",  "Lowest hallucination — trained to say 'I don't know'. Best for RAG / factual accuracy."),
        ("deepseek-r1:14b",    "Chain-of-thought debugging & code analysis. Shows its reasoning. MIT, fits 16GB."),
        ("deepseek-r1:32b",    "Larger CoT reasoner for harder algorithmic / math problems. MIT."),
        ("deepseek-r1:70b",    "70B CoT reasoner (Llama3.3 distill). Max reasoning depth; fits 64GB on its own."),
        ("phi4:14b",           "Best reasoning-per-GB. Strong math/logic/STEM, low footprint. MIT."),
        ("glm-4.7-flash",      "Strongest in the 30B class. Lightweight agentic, tool-use-focused reasoning."),
    ],
    "General": [
        ("llama3.3:70b",  "Meta's flagship 70B. Strong all-round chat + coding. Fits 64GB solo (~43 GB)."),
        ("mistral:7b",    "Mistral 7B v0.3. Fast, low-hallucination lightweight general/chat model."),
        ("gemma4:31b",    "Gemma 4 dense 31B. Vision + native tool calling + reasoning. Apache 2.0."),
    ],
    "Design": [
        ("gemma4:26b", "Strong visual/layout reasoning for wireframes and design systems."),
    ],
    "Embeddings": [
        ("nomic-embed-text", "Vector embeddings for RAG and SearXNG result ranking. Tiny, always available."),
    ],
}

def probe(url):
    try: return requests.get(url, timeout=3).status_code < 500
    except: return False

def hardware_info():
    vm = psutil.virtual_memory(); disk = None
    for p in ("/System/Volumes/Data", os.path.expanduser("~"), "/"):
        try: disk = psutil.disk_usage(p); break
        except: continue
    bat = None
    try:
        b = psutil.sensors_battery()
        if b: bat = {"percent": round(b.percent), "charging": bool(b.power_plugged)}
    except: pass
    return {
        "cpu":     {"pct": round(psutil.cpu_percent(interval=None))},
        "ram":     {"pct": round(vm.percent),
                    "detail": f"{vm.used/1e9:.1f} / {vm.total/1e9:.1f} GB"},
        "storage": {"pct": round(disk.percent) if disk else 0,
                    "detail": f"{disk.used/1e9:.1f} / {disk.total/1e9:.1f} GB" if disk else "?"},
        "battery": bat,
    }

def get_installed_models():
    out = {}
    try:
        r = requests.get(f"http://127.0.0.1:{P_OLLAMA}/api/tags", timeout=4)
        for m in r.json().get("models", []):
            name = m.get("name", ""); size = m.get("size", 0)
            out[name] = f"{size/1e9:.1f} GB" if size else "?"
    except: pass
    return out

def get_model_groups():
    installed = get_installed_models()
    used = set()
    groups = []
    for specialty, entries in MODEL_CATALOG.items():
        items = []
        for tag, when in entries:
            size = installed.get(tag)
            if not size:
                base = tag.split(":")[0]
                for inst in installed:
                    if inst.split(":")[0] == base:
                        size = installed[inst]; used.add(inst); break
            if tag in installed: used.add(tag)
            items.append({"name": tag, "size": size or "—",
                          "installed": bool(size), "when": when})
        groups.append({"specialty": specialty, "items": items})
    extras = [{"name": n, "size": s, "installed": True,
               "when": "Installed locally (not in the curated catalog)."}
              for n, s in installed.items() if n not in used]
    if extras:
        groups.append({"specialty": "Other / Installed", "items": extras})
    return groups

@app.route("/api/status")
def api_status():
    def _link(name, port):
        path = OPENCLAW_PATH if name == "OpenClaw" else "/"
        return f"http://{LAN_HOST}:{port}{path}"
    svcs = [{"name": n, "url": _link(n, p), "port": int(p),
             "purpose": pu, "ok": probe(h)}
            for n, h, p, pu in SERVICES]
    return jsonify({
        "services":     svcs,
        "hardware":     hardware_info(),
        "model_groups": get_model_groups(),
        "lan_host":     LAN_HOST,
        "updated":      datetime.datetime.now().strftime("%H:%M:%S"),
    })

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Local AI Workstation — Mission Control</title>
<style>
  :root{--bg:#0a0e14;--panel:#131820;--panel2:#1a212c;--border:#252e3a;--text:#e6edf3;--dim:#8b98a5;--accent:#3b82f6;--green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--purple:#8b5cf6;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);font-size:14px;padding:20px}
  .wrap{max-width:1200px;margin:0 auto}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
  .grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
  .card h2{font-size:15px;margin-bottom:12px}
  .hw{display:flex;gap:10px;justify-content:space-around;flex-wrap:wrap}
  .donut-wrap{display:flex;flex-direction:column;align-items:center;gap:6px;min-width:84px}
  .donut{position:relative;width:84px;height:84px}
  .donut svg{transform:rotate(-90deg)}
  .donut .ring-bg{stroke:var(--panel2)}
  .donut .ring-fg{stroke-linecap:round;transition:stroke-dashoffset .6s ease}
  .donut .label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}
  .donut-cap{font-size:11px;color:var(--dim);text-align:center}
  .donut-sub{font-size:10px;color:var(--dim);text-align:center}
  .svc{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)}
  .svc:last-child{border:none}
  .led{width:10px;height:10px;border-radius:50%;display:inline-block}
  .led.on{background:var(--green)}.led.off{background:var(--red)}
  .mgroup{margin-bottom:14px}
  .mgroup h3{font-size:12px;text-transform:uppercase;color:var(--accent);margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px}
  .mrow{display:flex;justify-content:space-between;align-items:flex-start;padding:6px 0;gap:10px}
  .mname{font-family:monospace;font-size:12px}
  .mwhen{color:var(--dim);font-size:11px;flex:1}
  .mtag{font-size:10px;padding:1px 7px;border-radius:8px;white-space:nowrap}
  .mtag.in{background:rgba(34,197,94,.18);color:var(--green)}
  .mtag.out{background:rgba(139,152,165,.12);color:var(--dim)}
  .hostbar{font-size:12px;color:var(--dim)}
  .hostbar code{color:var(--accent)}
</style></head><body>
<div class="wrap">
  <header>
    <h1>● Local AI Workstation</h1>
    <div class="hostbar">Access from any device: <code id="hosttxt">…</code> · Auto-refresh 5s</div>
  </header>
  <div class="grid">
    <div class="card"><h2>System Status</h2><div class="hw" id="hw"></div></div>
    <div class="card"><h2>Workstation Services</h2><div id="svcs"></div></div>
  </div>
  <div class="card"><h2>AI Models on This Machine</h2><div id="models"></div></div>
</div>
<script>
function donut(pct, caption, sub){
  pct = Math.max(0, Math.min(100, pct||0));
  const r=34, c=2*Math.PI*r, off=c*(1-pct/100);
  let col = 'var(--green)';
  if(pct>=85) col='var(--red)'; else if(pct>=60) col='var(--yellow)';
  return `<div class="donut-wrap">
    <div class="donut">
      <svg width="84" height="84">
        <circle class="ring-bg" cx="42" cy="42" r="${r}" fill="none" stroke-width="8"/>
        <circle class="ring-fg" cx="42" cy="42" r="${r}" fill="none" stroke-width="8"
          stroke="${col}" stroke-dasharray="${c}" stroke-dashoffset="${off}"/>
      </svg>
      <div class="label">${pct}%</div>
    </div>
    <div class="donut-cap">${caption}</div>
    <div class="donut-sub">${sub||''}</div>
  </div>`;
}
async function load(){
  try{
    const r=await fetch("/api/status");const d=await r.json();
    document.getElementById("hosttxt").textContent = `http://${d.lan_host}:8800`;
    const hw=d.hardware; let hwHtml='';
    hwHtml+=donut(hw.cpu.pct, 'CPU', '');
    hwHtml+=donut(hw.ram.pct, 'RAM', hw.ram.detail);
    hwHtml+=donut(hw.storage.pct, 'Storage', hw.storage.detail);
    if(hw.battery){
      const b=hw.battery;
      hwHtml+=donut(b.percent, 'Battery', b.charging?'⚡ charging':'on battery');
    }
    document.getElementById("hw").innerHTML=hwHtml;
    document.getElementById("svcs").innerHTML=d.services.map(s=>`
      <div class="svc">
        <span><span class="led ${s.ok?'on':'off'}"></span> <a href="${s.url}" target="_blank">${s.name}</a></span>
        <span style="color:var(--dim)">${s.purpose} (:${s.port})</span>
      </div>`).join("");
    document.getElementById("models").innerHTML=d.model_groups.map(g=>`
      <div class="mgroup">
        <h3>${g.specialty}</h3>
        ${g.items.map(m=>`
          <div class="mrow">
            <span class="mname">${m.name}</span>
            <span class="mwhen">${m.when}</span>
            <span class="mtag ${m.installed?'in':'out'}">${m.installed?('✓ '+m.size):'pull'}</span>
          </div>`).join("")}
      </div>`).join("");
  }catch(e){console.error(e);}
}
load();setInterval(load,5000);
</script></body></html>"""

@app.route("/")
def index():
    from flask import Response
    return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT_DASHBOARD", "8800"))
    app.run(host="0.0.0.0", port=port, debug=False)
DASHEOF
    ok "Dashboard script written."
}

# =============================================================================
#  PHASE 0.5 — CLEANUP OLD ARTIFACTS
# =============================================================================
cleanup_orchestrator() {
    log "Checking for legacy orchestrator artifacts..."
    local found=0

    # Check for standalone Python orchestrator script
    if [ -f "$WORKDIR/agents/orchestrator.py" ]; then
        warn "Found legacy orchestrator: $WORKDIR/agents/orchestrator.py"
        rm -rf "$WORKDIR/agents" \
            && ok "Removed $WORKDIR/agents/ (legacy orchestrator + any child agents)"
        found=1
    fi

    # Check for launch agent plist
    if [ -f "$LAUNCH_DIR/com.aiws.orchestrator.plist" ]; then
        warn "Found legacy launch agent: com.aiws.orchestrator"
        launchctl unload "$LAUNCH_DIR/com.aiws.orchestrator.plist" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_DIR/com.aiws.orchestrator.plist" \
            && ok "Removed com.aiws.orchestrator launch agent"
        found=1
    fi

    # Check for vox launch agent (was also standalone)
    if [ -f "$LAUNCH_DIR/com.aiws.vox.plist" ]; then
        warn "Found legacy vox launch agent: com.aiws.vox"
        launchctl unload "$LAUNCH_DIR/com.aiws.vox.plist" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_DIR/com.aiws.vox.plist" \
            && ok "Removed com.aiws.vox launch agent"
        found=1
    fi

    if [ "$found" -eq 0 ]; then
        ok "No legacy orchestrator artifacts found."
    else
        warn "Legacy artifacts cleaned. OpenClaw handles the team now."
    fi
}

# =============================================================================
#  PHASE 7 — OPENCLAW INTEGRATION (this is where your team lives)
# =============================================================================
openclaw_ok() { have openclaw && openclaw --version >/dev/null 2>&1; }
setup_openclaw() {
    log "OpenClaw Integrator (your agent team runtime)"
    have npm || { warn "npm missing; skipping OpenClaw install."; return; }

    if openclaw_ok; then
        ok "OpenClaw present ($(openclaw --version 2>/dev/null))."
    else
        log "Installing OpenClaw globally..."
        npm ls -g openclaw >/dev/null 2>&1 && opt npm uninstall -g openclaw
        SHARP_IGNORE_GLOBAL_LIBVIPS=1 npm install -g openclaw@latest \
            || opt npm install -g openclaw@latest
    fi

    if openclaw_ok; then
        ok "OpenClaw installed successfully."
        cat <<TUTEOF

####  OPENCLAW ONBOARDING  ####
  To complete setting up the phone-driven agent:
  Run:
      openclaw onboard --install-daemon

  Pick:
    - Provider: openai-compatible
    - Base URL: http://0.0.0.0:4000/v1   (your LiteLLM gateway)
    - API key:  local                    (any non-empty string)
    - Model:    orchestrator             (or any name from litellm.config.yaml)
    - Channel:  Telegram (paste your TELEGRAM_BOT_TOKEN)

  Once running, OpenClaw chat is at:
      http://0.0.0.0:18789/chat?session=main

  IMPORTANT: After onboarding, paste the team-setup prompt from README.md
  into the OpenClaw chat to create your Orion/Ada/Mira/Leo/Nova/Cipher/Vox team.
###############################
TUTEOF
        printf "Run 'openclaw onboard --install-daemon' now? [Y/n] "
        read -r r
        case "$r" in n|N|no) warn "Skipped. Run onboarding manually." ;;
            *) openclaw onboard --install-daemon || warn "Onboarding skipped." ;; esac
    else
        warn "Could not configure OpenClaw successfully."
    fi
}

setup_peekaboo() {
    log "Peekaboo GUI automation tap"
    if brew list --cask peekaboo >/dev/null 2>&1 || [ -d "/Applications/Peekaboo.app" ]; then
        ok "Peekaboo present."
        return 0
    fi
    printf "Install Peekaboo cask for GUI automation? [y/N] "
    read -r r
    case "$r" in y|Y|yes)
        brew install steipete/tap/peekaboo 2>/dev/null && ok "Peekaboo installed." \
            || warn "Peekaboo tap install failed. Run: brew install steipete/tap/peekaboo"
        ;;
        *) ok "Skipped Peekaboo." ;;
    esac
}

# =============================================================================
#  PHASE 8 — LAUNCHD SERVICE PLIST BUILDER
# =============================================================================
write_plist() {
    local label="$1" script="$2"
    local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$script</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WORKDIR</key><string>$WORKDIR</string>
    <key>AI_HOME</key><string>$WORKDIR</string>
    <key>AI_WORKSPACE</key><string>$DOCS_WORKSPACE</string>
    <key>CODE_WORKSPACE</key><string>$CODE_WORKSPACE</string>
    <key>MASTER_EMAIL</key><string>$MASTER_EMAIL</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err</string>
</dict></plist>
PLISTEOF
    launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl load   "$plist" >/dev/null 2>&1 \
        && ok "Registered: $label" || warn "Failed loading: $label"
}

setup_services() {
    log "Registering launchd background services"
    write_plist "com.aiws.litellm"   "$WORKDIR/start_gateway.sh"
    write_plist "com.aiws.dashboard" "$WORKDIR/start_dashboard.sh"
}

# =============================================================================
#  PHASE 9 — BOOTSTRAP / CREDENTIAL FLOWS
# =============================================================================
collect_tokens() {
    log "Configuring tokens..."
    load_env
    prompt_secret "TELEGRAM_BOT_TOKEN" "Telegram bot token" validate_telegram tut_telegram

    local chat; chat="$(get_env TELEGRAM_CHAT_ID)"
    if [ -n "$chat" ]; then ok "Telegram Chat ID present."; else
        cat <<'CHATEOF'

####  TELEGRAM CHAT ID  ####
  1. Open Telegram → Search @userinfobot → Start.
  2. Paste the numeric ID here.
###########################
CHATEOF
        printf "Paste Telegram Chat ID (or 'skip'): "; read -r chat
        case "$chat" in skip|SKIP|"") warn "Skipping chat ID." ;;
            *) set_env TELEGRAM_CHAT_ID "$chat"; ok "Chat ID saved." ;; esac
    fi
}

print_summary() {
    local ip; ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '0.0.0.0')"
    hr
    cat <<SUMEOF
    🚀  LOCAL AI WORKSTATION SETUP COMPLETE

  Dashboard (this Mac):  http://localhost:$PORT_DASHBOARD
  Dashboard (LAN/phone): http://$ip:$PORT_DASHBOARD
  LiteLLM gateway:       http://$ip:$PORT_GATEWAY/v1   (point OpenClaw here)
  OpenClaw chat:         http://$ip:$PORT_OPENCLAW/chat?session=main

  NEXT STEP — set up your agent team:
    Open README.md and copy the "OpenClaw Team Setup Prompt" into the
    OpenClaw chat. That creates Orion/Ada/Mira/Leo/Nova/Cipher/Vox inside
    OpenClaw (they are no longer defined by this script).

  All services bind 0.0.0.0 — reachable from other devices on your network.

  IDEs installed: VS Code + IntelliJ IDEA
  Swap models any time:  $0 --pull-models

  Folders:
    - Documents:  $DOCS_WORKSPACE
    - Source:     $CODE_WORKSPACE

  Run controls:
    $0 --status | --start | --stop | --restart | --pull-models | --uninstall
SUMEOF
    hr
}

# =============================================================================
#  CLI CONTROLLER & UNINSTALL RUNTIMES
# =============================================================================
SERVICE_LABELS=(com.aiws.litellm com.aiws.dashboard)

svc_status() {
    hr; log "Services status check:"
    for url_label in \
        "Ollama|http://127.0.0.1:$PORT_OLLAMA/api/tags" \
        "LiteLLM|http://127.0.0.1:$PORT_GATEWAY/health/liveliness" \
        "Dashboard|http://127.0.0.1:$PORT_DASHBOARD/" \
        "OpenClaw|http://127.0.0.1:$PORT_OPENCLAW/" \
        "Open WebUI|http://127.0.0.1:$PORT_OPENWEBUI/" \
        "SearXNG|http://127.0.0.1:$PORT_SEARXNG/" \
        "Langfuse|http://127.0.0.1:$PORT_LANGFUSE/api/public/health" \
        "Portainer|http://127.0.0.1:$PORT_PORTAINER/"; do
        local nm="${url_label%%|*}" url="${url_label#*|}"
        if http_ok "$url"; then ok "$nm — live"; else warn "$nm — down"; fi
    done
    echo; log "launchd status:"
    for l in "${SERVICE_LABELS[@]}"; do
        if launchctl list 2>/dev/null | grep -q "$l"; then ok "$l — active"; else warn "$l — inactive"; fi
    done
    echo; log "Docker status:"
    docker_up && docker ps --format '  {{.Names}} — {{.Status}}' 2>/dev/null || warn "Docker not active."
}

svc_start() {
    log "Booting Workstation services..."
    ollama_start
    setup_colima
    if docker_up; then
        for c in open-webui searxng portainer; do
            if docker inspect "$c" >/dev/null 2>&1; then
                docker start "$c" >/dev/null 2>&1 || true
            else
                case "$c" in
                    open-webui) setup_openwebui ;;
                    searxng)    setup_searxng ;;
                    portainer)  setup_portainer ;;
                esac
            fi
        done
        [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc up -d >/dev/null 2>&1) || setup_langfuse
    fi
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl load "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
    done
    ok "Startup triggers dispatched."
}

svc_stop() {
    log "Stopping Workstation services..."
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
    done
    for c in open-webui searxng portainer; do docker stop "$c" >/dev/null 2>&1 || true; done
    [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc stop >/dev/null 2>&1) || true
    ollama_stop
    if have colima; then colima stop >/dev/null 2>&1 || true; fi
    ok "Stop triggers dispatched."
}

uninstall_all() {
    hr
    log "UNINSTALL TRIGGERED — FULL ROLLBACK"
    printf "${c_red}Are you sure you want to stop and delete all setup local AI workspaces and configurations? [y/N]: ${c_reset}"
    read -r confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Uninstall aborted."
        exit 0
    fi

    log "Stopping services..."
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_DIR/$l.plist"
    done

    log "Downing Docker containers..."
    if docker_up; then
        docker rm -f open-webui searxng portainer >/dev/null 2>&1 || true
        docker volume rm open-webui portainer_data >/dev/null 2>&1 || true
        if [ -d "$WORKDIR/langfuse" ]; then
            (cd "$WORKDIR/langfuse" && dc down -v >/dev/null 2>&1 || true)
        fi
    fi

    if have colima; then
        colima stop >/dev/null 2>&1 || true
    fi

    log "Removing OpenClaw..."
    if have npm; then
        openclaw uninstall --daemon >/dev/null 2>&1 || true
        npm uninstall -g openclaw >/dev/null 2>&1 || true
    fi

    log "Removing temporary files and local workspace..."
    rm -rf "$WORKDIR"
    rm -rf /tmp/model_*.lock || true

    printf "Remove directories (OneDrive outputs)? [y/N]: "
    read -r clean_w
    if [[ "$clean_w" =~ ^[Yy]$ ]]; then
        rm -rf "$DOCS_WORKSPACE" "$CODE_WORKSPACE"
        ok "Cleaned $DOCS_WORKSPACE and $CODE_WORKSPACE"
    fi

    printf "Uninstall Homebrew system packages installed by this script? [y/N]: "
    read -r clean_brew
    if [[ "$clean_brew" =~ ^[Yy]$ ]]; then
        brew remove --force colima docker docker-compose uv socat cairo pango gdk-pixbuf libffi || true
    fi

    printf "Uninstall IDEs installed by this script (VS Code, IntelliJ IDEA)? [y/N]: "
    read -r clean_ide
    if [[ "$clean_ide" =~ ^[Yy]$ ]]; then
        brew uninstall --cask visual-studio-code intellij-idea intellij-idea-ce >/dev/null 2>&1 || true
        ok "IDEs removed."
    fi

    ok "Uninstall complete. Your system has been rolled back."
}

# =============================================================================
#  CLI PARSER
# =============================================================================
print_help() {
    cat <<USG
Usage: $0 [OPTION]
  --bootstrap   : Run checks, install tools, models, services, IDEs, OpenClaw
  --start       : Start Colima, Ollama, containers, gateway and dashboard
  --stop        : Stop Colima, Ollama, containers, gateway and dashboard
  --restart     : Stop then start services
  --status      : Display live server and services state
  --pull-models : Interactively pull optional swappable models
  --uninstall   : Complete rollback and removal of setup
  --help        : Display instructions
USG
}

case "${1:---help}" in
    --bootstrap)
        preflight
        cleanup_orchestrator
        setup_xcode_clt
        setup_homebrew
        setup_core_tools
        setup_ides
        setup_workspaces
        collect_tokens
        setup_ollama
        setup_python
        setup_litellm
        setup_docker_services
        write_dashboard
        setup_openclaw
        setup_peekaboo
        setup_services
        pull_swappable_models
        print_summary
        ;;
    --pull-models) pull_swappable_models ;;
    --start)       svc_start ;;
    --stop)        svc_stop ;;
    --restart)     svc_stop; svc_start ;;
    --status)      svc_status ;;
    --uninstall)   uninstall_all ;;
    --help|-h)     print_help ;;
    *)
        echo "Unknown option: $1"
        print_help
        exit 2
        ;;
esac
