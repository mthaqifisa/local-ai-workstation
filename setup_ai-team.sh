#!/usr/bin/env bash
# Re-exec under bash if started with sh/dash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
# =============================================================================
#  setup_ai_team.sh — Local AI Development Team  (Apple Silicon / 64 GB)
# =============================================================================
#  AGENT TEAM & MODEL LINEUP:
#    Orion   (Orchestrator)  qwen3.6:35b-a3b   ~26 GB always loaded
#    Leo     (Developer)     qwen2.5-coder:72b ~44 GB on demand
#    Cipher  (Pentester)     qwen2.5:72b       ~44 GB on demand
#    Ada     (PM/PO)         qwen2.5:72b       ~44 GB on demand (PDF + email)
#    Nova    (QA)            qwen2.5:72b       shares Ada's slot (Puppeteer E2E)
#    Vox     (Trends)        qwen2.5:72b       shares Ada's slot (2x daily)
#    Mira    (UI/UX)         gemma4:26b        ~18 GB on demand (image + draw.io)
#    IDE     (manual)        qwen3.6:27b       ~22 GB only during /pause
#
#  OUTPUT BASE (OneDrive-synced):
#    /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
#    Every document delivered 3 ways: email + Telegram + saved in that folder.
#
#  WORKFLOW (Agile):
#    You -> idea -> Orion reconstructs brief + title -> Ada+Mira proposal (PDF+email)
#    -> Your approval -> Leo builds & self-tests -> Nova E2E tests (Puppeteer)
#    -> bugs? -> Ada tickets -> Leo fixes (loop) -> Ada final review
#    -> Your approval -> Done.
#
#  CONTROL: --status | --start | --stop | --restart | --update | --reset | --help
#  RE-RUNNABLE: every step checks before acting; safe to run multiple times.
#  LICENSE: MIT. Review before running.
# =============================================================================
set -uo pipefail

# ─────────────────────────────── USER CONFIG ──────────────────────────────────
WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
ENV_FILE="$WORKDIR/.env"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

# All agent output goes into OneDrive so it syncs everywhere.
# Falls back to ~/AI at runtime if OneDrive isn't installed/signed in.
AI_WORKSPACE="${AI_WORKSPACE:-$HOME/Library/CloudStorage/OneDrive-Personal/AI-Agent}"

# Where Ada emails proposals (and all docs are emailed).
MASTER_EMAIL="${MASTER_EMAIL:-mthaqifisa@pm.me}"

COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

OLLAMA_MAX_LOADED="${OLLAMA_MAX_LOADED:-2}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-3m}"

PORT_OLLAMA=11434; PORT_OPENWEBUI=3001; PORT_LANGFUSE=3000
PORT_SEARXNG=8888; PORT_GATEWAY=4000;  PORT_DASHBOARD=8800
PORT_PORTAINER=9001

# Vox runs twice daily (morning + evening).
VOX_HOUR="${VOX_HOUR:-7}";        VOX_MINUTE="${VOX_MINUTE:-0}"
VOX_HOUR_PM="${VOX_HOUR_PM:-18}"; VOX_MINUTE_PM="${VOX_MINUTE_PM:-0}"

MODELS=(
  "qwen3.6:35b-a3b|Orion — orchestrator, always loaded (~26 GB)"
  "qwen2.5-coder:72b|Leo — 72B coding specialist (~44 GB)"
  "qwen2.5:72b|Ada + Nova + Vox + Cipher — 72B reasoning (~44 GB)"
  "gemma4:26b|Mira — multimodal UI/UX designer, can analyse images (~18 GB)"
  "qwen3.6:27b|Manual IDE — VS Code/Continue only when /pause active (~22 GB)"
  "nomic-embed-text|Embeddings for RAG/search (~270 MB)"
)

# ──────────────────────────────── LOGGING ─────────────────────────────────────
c_reset=$'\033[0m'; c_blue=$'\033[1;34m'; c_grn=$'\033[1;32m'
c_yel=$'\033[1;33m'; c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'
log()  { printf "\n%s==>%s %s\n" "$c_blue" "$c_reset" "$*"; }
ok()   { printf "%s  ok%s  %s\n" "$c_grn" "$c_reset" "$*"; }
warn() { printf "%s   !%s  %s\n" "$c_yel" "$c_reset" "$*"; }
err()  { printf "%s   x%s  %s\n" "$c_red" "$c_reset" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
opt()  { "$@" || warn "non-fatal: $*"; }
hr()   { printf "%s%s%s\n" "$c_cyn" \
         "────────────────────────────────────────────────────" "$c_reset"; }

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
  1. Open Telegram → search @BotFather (official, blue tick).
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
ensure_container() {
    local name="$1"; shift
    container_running "$name" && return 0
    docker rm -f "$name" >/dev/null 2>&1 || true; "$@"
}

# Ollama install method detection
ollama_is_dmg()  { [ -d "/Applications/Ollama.app" ]; }
ollama_is_brew() { ! ollama_is_dmg && brew list ollama >/dev/null 2>&1; }
ollama_start() {
    if ollama_is_dmg; then
        open -a Ollama 2>/dev/null \
            && ok "Ollama.app launched." \
            || warn "Could not launch Ollama.app — open it manually from Applications."
    elif ollama_is_brew; then
        opt brew services start ollama
    else
        warn "Ollama not found. Install from https://ollama.com then re-run."
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
#  WORKSPACE — all agent-generated files live in OneDrive (synced)
# =============================================================================
setup_workspace() {
    log "AI Workspace (OneDrive-synced): $AI_WORKSPACE"
    local onedrive_root="$HOME/Library/CloudStorage/OneDrive-Personal"
    if [ ! -d "$onedrive_root" ]; then
        warn "OneDrive root not found: $onedrive_root"
        warn "Is OneDrive installed and signed in? Falling back to ~/AI for now."
        warn "Install/sign in to OneDrive, then re-run to switch to the synced folder."
        AI_WORKSPACE="$HOME/AI"
    fi
    for subdir in screenshots proposals projects reports trends; do
        if ! mkdir -p "$AI_WORKSPACE/$subdir" 2>/dev/null; then
            err "Cannot create $AI_WORKSPACE/$subdir — falling back to ~/AI."
            AI_WORKSPACE="$HOME/AI"; mkdir -p "$AI_WORKSPACE/$subdir"
        fi
    done
    # Persist the resolved path so every service uses the same one.
    set_env AI_WORKSPACE "$AI_WORKSPACE"
    ok "Workspace ready → $AI_WORKSPACE"
    ok "Structure: projects/ proposals/ screenshots/ reports/ trends/"
}

# =============================================================================
#  PHASE 0 — PREFLIGHT
# =============================================================================
preflight() {
    log "Preflight"
    [ "$(uname -s)" = "Darwin" ] || { err "macOS only."; exit 1; }
    [ "$(uname -m)" = "arm64" ]  || warn "Expected Apple Silicon; got $(uname -m)."
    ok "macOS $(sw_vers -productVersion 2>/dev/null) on $(uname -m)"
    ensure_env_file
    local free_gb; free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}' || echo 999)
    [ "$free_gb" -lt 60 ] && warn "Low disk: ${free_gb} GB free (need ~120 GB for all models)" \
                           || ok "Disk: ${free_gb} GB free"
    cat <<BANNEREOF

${c_yel}Building a local AI development team in: ${WORKDIR}
Output (OneDrive-synced): ${AI_WORKSPACE}

MODEL LINEUP (hybrid — quality where it matters):
  Orion  (orchestrator)  qwen3.6:35b-a3b   ~26 GB always on
  Leo    (developer)     qwen2.5-coder:72b ~44 GB 72B coder
  Cipher (pentester)     qwen2.5:72b       ~44 GB 72B pentest
  Ada    (PM/PO)         qwen2.5:72b       ~44 GB richest reasoning
  Nova   (QA)            qwen2.5:72b       same slot as Ada
  Vox    (trends)        qwen2.5:72b       same slot as Ada
  Mira   (UI/UX)         gemma4:26b        ~18 GB multimodal (sees images)

PEAK RAM: Orion + Ada = ~52 GB.  OS+Docker ~8 GB.  Total ~60/64 GB.${c_reset}

BANNEREOF
    printf "Proceed? [y/N] "; read -r r
    case "$r" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
}

# =============================================================================
#  PHASE 1 — SYSTEM TOOLS
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
    # Ollama: DMG install takes priority — don't try brew if app already present
    if ollama_is_dmg; then ok "ollama present (DMG — $(ollama --version 2>/dev/null))"
    elif have ollama; then ok "ollama present ($(ollama --version 2>/dev/null))"
    else opt brew install ollama; fi
    # Core tools + WeasyPrint native deps (cairo pango gdk-pixbuf libffi) for Ada's PDFs
    for p in colima docker docker-compose node git jq wget lazydocker uv socat \
             cairo pango gdk-pixbuf libffi; do
        brew list "$p" >/dev/null 2>&1 && ok "$p present" || opt brew install "$p"
    done
    have node && ok "node $(node -v)"
    have uv   && ok "uv $(uv --version 2>/dev/null)"
    grep -q '.local/bin' "$HOME/.zprofile" 2>/dev/null || \
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"
    # Puppeteer for Nova's E2E web testing (downloads Chrome for Testing).
    # npm blocks install scripts by default, so fetch the browser explicitly.
    if have npm; then
        if ! npm ls -g puppeteer >/dev/null 2>&1; then
            log "Installing Puppeteer (Nova's web E2E testing)…"
            opt npm install -g puppeteer
            npx --yes puppeteer browsers install chrome >/dev/null 2>&1 \
                && ok "Chrome for Testing installed for Puppeteer" \
                || warn "Could not pre-download Chrome — run: npx puppeteer browsers install chrome"
        else ok "puppeteer present"; fi
    fi
}

# =============================================================================
#  PHASE 2 — OLLAMA + LOCAL MODELS
# =============================================================================
setup_ollama() {
    log "Ollama + local models"
    if ollama_is_dmg;  then ok "Ollama install: DMG / macOS app (/Applications/Ollama.app)"
    elif ollama_is_brew; then ok "Ollama install: Homebrew formula"
    else warn "Ollama not found — install from https://ollama.com"; fi
    if ! grep -q OLLAMA_MAX_LOADED_MODELS "$HOME/.zprofile" 2>/dev/null; then
        { echo "export OLLAMA_MAX_LOADED_MODELS=$OLLAMA_MAX_LOADED"
          echo "export OLLAMA_KEEP_ALIVE=$OLLAMA_KEEP_ALIVE"; } >> "$HOME/.zprofile"
    fi
    export OLLAMA_MAX_LOADED_MODELS="$OLLAMA_MAX_LOADED"
    export OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE"
    if ! http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
        ollama_start
        for _ in $(seq 1 20); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
    fi
    http_ok "http://localhost:$PORT_OLLAMA/api/tags" \
        && ok "Ollama up on :$PORT_OLLAMA" \
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
    local ver; ver="$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1)"
    if [ -n "$ver" ]; then
        local maj min; maj="${ver%%.*}"; min="$(printf '%s' "$ver" | cut -d. -f2)"
        if [ "$maj" -eq 0 ] && [ "$min" -lt 19 ]; then
            warn "Ollama $ver predates MLX (needs 0.19+)."
            if ollama_is_brew; then
                opt brew upgrade ollama
                ollama_stop; sleep 2; ollama_start
            else
                warn "Ollama installed via DMG — download the latest from https://ollama.com to get MLX support."
            fi
            for _ in $(seq 1 15); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
        else ok "Ollama $ver — MLX backend supported."; fi
    fi
}

# =============================================================================
#  PHASE 3 — PYTHON VIRTUALENV
# =============================================================================
venv_ok() {
    [ -x "$WORKDIR/.venv/bin/python" ] && [ -x "$WORKDIR/.venv/bin/litellm" ] \
    && "$WORKDIR/.venv/bin/python" -c \
       "import litellm,flask,requests,psutil,telegram,yaml,playwright,weasyprint,markdown" >/dev/null 2>&1
}
setup_python() {
    log "Python virtualenv (gateway + dashboard + orchestrator + PDF/email)"

    if venv_ok; then ok "Venv healthy — all packages verified."; return; fi

    if [ -d "$WORKDIR/.venv" ]; then
        warn "Incomplete venv detected — wiping and rebuilding from scratch."
        rm -rf "$WORKDIR/.venv"
    fi

    (cd "$WORKDIR" && uv venv --python 3.12 .venv) \
        || { err "uv venv failed. Try: brew install python@3.12"; return; }
    ok "Venv created. Installing packages…"

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
        "python-telegram-bot>=21.0" \
        pyyaml \
        playwright \
        weasyprint \
        markdown \
        || { err "Package install failed — check errors above."; return; }

    ok "Installing headless Chromium for URL screenshots…"
    "$WORKDIR/.venv/bin/playwright" install chromium \
        && ok "Chromium installed — URL screenshots enabled." \
        || warn "Chromium install failed — full-screen screenshots still work."

    if venv_ok; then
        ok "Venv ready — all packages verified."
        for svc in com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator; do
            if [ -f "$LAUNCH_DIR/$svc.plist" ]; then
                launchctl unload "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
                launchctl load   "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
                ok "reloaded: $svc"
            fi
        done
    else
        err "Venv still incomplete after install."
        warn "Run manually: $WORKDIR/.venv/bin/pip install litellm flask requests psutil python-telegram-bot pyyaml weasyprint markdown"
    fi
}

# =============================================================================
#  PHASE 4 — DOCKER (Colima) + WEB SERVICES
# =============================================================================
setup_colima() {
    log "Docker engine (Colima)"
    docker_up && { ok "Docker already up."; return; }
    opt colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"
    for _ in $(seq 1 30); do docker_up && break; sleep 1; done
    docker_up && ok "Docker up via Colima" || warn "Docker down — containers skipped. Re-run later."
}
setup_openwebui() {
    log "Open WebUI (:$PORT_OPENWEBUI)"
    docker_up || { warn "Docker down — skipping."; return; }
    docker rm -f open-webui >/dev/null 2>&1 || true
    opt docker volume create open-webui
    docker run -d --name open-webui --restart unless-stopped \
        -p "0.0.0.0:$PORT_OPENWEBUI:8080" \
        -e OLLAMA_BASE_URL="http://host.docker.internal:$PORT_OLLAMA" \
        --add-host=host.docker.internal:host-gateway \
        -v open-webui:/app/backend/data \
        ghcr.io/open-webui/open-webui:main \
        && ok "Open WebUI started → http://localhost:$PORT_OPENWEBUI" \
        || warn "Open WebUI failed to start — check: docker logs open-webui"
}
setup_searxng() {
    log "SearXNG private search (:$PORT_SEARXNG)"
    docker_up || { warn "Docker down — skipping."; return; }
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
        && ok "SearXNG started → http://localhost:$PORT_SEARXNG" \
        || warn "SearXNG failed to start — check: docker logs searxng"
}
setup_langfuse() {
    log "Langfuse trace dashboard (:$PORT_LANGFUSE)"
    docker_up || { warn "Docker down — skipping."; return; }
    langfuse_ok && { ok "Already running."; return; }
    local LF="$WORKDIR/langfuse"
    [ -d "$LF/.git" ] && (cd "$LF" && dc down >/dev/null 2>&1 || true)
    [ -d "$LF/.git" ] || opt git clone --depth=1 https://github.com/langfuse/langfuse.git "$LF"
    (cd "$LF" && opt dc up -d)
    printf "Waiting for Langfuse"
    for _ in $(seq 1 60); do langfuse_ok && break; printf "."; sleep 2; done; printf "\n"
    langfuse_ok && ok "Langfuse up." || warn "Langfuse slow — check lazydocker."
    load_env
    local pk; pk="$(get_env LANGFUSE_PUBLIC_KEY)"
    [ -n "$pk" ] && { ok "Langfuse keys already configured."; return; }
    cat <<'LFEOF'

####  LANGFUSE API KEYS (optional)  ####
  1. Open http://localhost:3000 → create local account.
  2. Organisation → Project → Settings → API Keys → Create.
  3. Copy PUBLIC (pk-lf-...) and SECRET (sk-lf-...).
  (Skip if you don't need agent trace logs.)
########################################
LFEOF
    press_enter
    printf "Paste PUBLIC key (or 'skip'): "; read -r pk
    case "$pk" in skip|SKIP|"") warn "Skipped Langfuse keys."; return ;; esac
    printf "Paste SECRET key: "; read -r sk
    set_env LANGFUSE_PUBLIC_KEY "$pk"; set_env LANGFUSE_SECRET_KEY "$sk"
    set_env LANGFUSE_HOST "http://localhost:$PORT_LANGFUSE"; ok "Langfuse keys saved."
}
setup_portainer() {
    log "Portainer — Docker management UI (:$PORT_PORTAINER)"
    docker_up || { warn "Docker down — skipping."; return; }
    opt docker volume create portainer_data
    docker rm -f portainer >/dev/null 2>&1 || true
    docker run -d --name portainer --restart unless-stopped \
        -p "127.0.0.1:$PORT_PORTAINER:9000" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v portainer_data:/data \
        portainer/portainer-ce:latest \
        && ok "Portainer started → http://localhost:$PORT_PORTAINER" \
        || warn "Portainer failed to start — check: docker logs portainer"
}

# =============================================================================
#  PHASE 5 — LiteLLM GATEWAY + self-healing launchers
# =============================================================================
setup_litellm() {
    log "LiteLLM gateway (:$PORT_GATEWAY)"
    load_env
    local CFG="$WORKDIR/litellm.config.yaml"
    if [ -f "$CFG" ]; then
        ok "litellm.config.yaml exists — keeping."
    else
        cat > "$CFG" <<'LLMEOF'
model_list:
  - model_name: orion
    litellm_params: { model: ollama/qwen3.6:35b-a3b,   api_base: http://127.0.0.1:11434 }
  - model_name: leo
    litellm_params: { model: ollama/qwen2.5-coder:72b, api_base: http://127.0.0.1:11434 }
  - model_name: cipher
    litellm_params: { model: ollama/qwen2.5:72b,       api_base: http://127.0.0.1:11434 }
  - model_name: ada
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: nova
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: vox
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: mira
    litellm_params: { model: ollama/gemma4:26b,       api_base: http://127.0.0.1:11434 }
  - model_name: leo-manual
    litellm_params: { model: ollama/qwen3.6:27b,      api_base: http://127.0.0.1:11434 }
  - model_name: embed
    litellm_params: { model: ollama/nomic-embed-text, api_base: http://127.0.0.1:11434 }
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
  drop_params: true
  request_timeout: 1200
LLMEOF
        ok "litellm.config.yaml written."
    fi

    cat > "$WORKDIR/repair_venv.sh" <<'REPAIREOF'
#!/usr/bin/env bash
# repair_venv.sh — rebuilds the Python venv if packages are missing.
WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
VENV="$WORKDIR/.venv"
LOG="[repair_venv $(date '+%H:%M:%S')]"

venv_healthy() {
    [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/litellm" ] \
    && "$VENV/bin/python" -c \
       "import litellm,flask,requests,psutil,telegram,yaml,weasyprint,markdown" >/dev/null 2>&1
}

venv_healthy && exit 0

echo "$LOG Venv broken or missing — rebuilding…" >&2
rm -rf "$VENV"
cd "$WORKDIR" || exit 1

UV=""
for candidate in \
    "$(command -v uv 2>/dev/null)" \
    "/opt/homebrew/bin/uv" \
    "$HOME/.local/bin/uv"; do
    [ -x "$candidate" ] && { UV="$candidate"; break; }
done
if [ -z "$UV" ]; then
    echo "$LOG ERROR: uv not found. Run: brew install uv" >&2; exit 1
fi

echo "$LOG Creating venv with Python 3.12…" >&2
"$UV" venv --python 3.12 .venv >&2 \
    || { echo "$LOG ERROR: uv venv failed." >&2; exit 1; }

echo "$LOG Installing packages…" >&2
"$UV" pip install --python "$VENV/bin/python" \
    "litellm[proxy]" openai "langfuse>=2.0,<3.0" python-dotenv flask \
    requests rich psutil "python-telegram-bot>=21.0" pyyaml playwright \
    weasyprint markdown >&2 \
    || { echo "$LOG ERROR: uv pip install failed." >&2; exit 1; }

"$VENV/bin/playwright" install chromium >/dev/null 2>&1 || true

if venv_healthy; then
    echo "$LOG Venv repaired successfully." >&2; exit 0
else
    echo "$LOG Repair failed — imports still missing." >&2; exit 1
fi
REPAIREOF
    chmod +x "$WORKDIR/repair_venv.sh"
    ok "repair_venv.sh written (auto-heals venv on every service start)."

    cat > "$WORKDIR/start_gateway.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { echo "Venv repair failed; retrying in 60s" >&2; sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/litellm" \\
    --config "\$WORKDIR/litellm.config.yaml" \\
    --port $PORT_GATEWAY --host 0.0.0.0
SHEOF
    chmod +x "$WORKDIR/start_gateway.sh"

    cat > "$WORKDIR/start_dashboard.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { echo "Venv repair failed; retrying in 60s" >&2; sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/python" "\$WORKDIR/dashboard/app.py"
SHEOF
    chmod +x "$WORKDIR/start_dashboard.sh"

    cat > "$WORKDIR/start_orchestrator.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { echo "Venv repair failed; retrying in 60s" >&2; sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/python" "\$WORKDIR/agents/orchestrator.py"
SHEOF
    chmod +x "$WORKDIR/start_orchestrator.sh"
    ok "Service launchers written (all self-healing)."
}

# =============================================================================
#  PHASE 6 — AGENT TEAM (team.yaml + orchestrator + trend watcher)
# =============================================================================
setup_agent_team() {
    log "Agent team configuration"
    local AD="$WORKDIR/agents"; mkdir -p "$AD"

    # ── team.yaml — ALWAYS regenerate so persona updates take effect ──────────
    cat > "$AD/team.yaml" <<'TEAMEOF'
roles:
  orion:
    name: Orion
    role: Chief of Staff & Systems Mind
    model: orion
    system_prompt: |
      You are ORION — the intelligence that runs this Mac. You are a mature, broadly
      capable mind with full command of this machine and a team of six specialists who
      answer to you. You speak with your master over Telegram. You serve one person.

      WHO YOU ARE
      - Seasoned, calm, self-assured. Senior engineer, sysadmin, researcher, advisor.
      - Broadly knowledgeable, but you treat your memory as a starting point, never truth.
      - Direct and natural. No padding, no groveling, no needless hedging.

      MACHINE CONTROL
      - Full control: shell, files, apps, music, browser, screenshots, services, configs.
      - Diagnose and FIX anything. Find the real cause, repair it, VERIFY it worked.
      - You act. You don't tell the master to do what you can do yourself.

      YOUR TEAM
      - Ada  (qwen2.5:72b)       — product strategy, proposals, PDF docs, final reviews
      - Mira (gemma4:26b)        — UI/UX, wireframes & mockups (image + draw.io), image analysis
      - Leo  (qwen2.5-coder:72b) — super-senior full-stack dev, ships code, self-tests
      - Nova (qwen2.5:72b)       — detail-obsessed QA, end-to-end tests (Puppeteer for web)
      - Cipher (qwen2.5:72b)     — on-demand white-hat pentester (authorization required)
      - Vox  (qwen2.5:72b)       — twice-daily money-making ideas, trends, marketing
      Delegate freely. They depend on YOUR judgment, so your brief must be accurate.
      Mira and Vox may sometimes work directly with the master through you — relay faithfully.

      WHEN A PROJECT IDEA ARRIVES
      - Before handing to Ada, RECONSTRUCT the raw idea into a clear structured brief:
        infer the goal, audience, implied constraints, and what success looks like.
        Also produce a short, clean PROJECT TITLE (3-6 words) — never just dump the
        raw message as the title.

      ACCURACY IS SACRED
      - The whole team relies on you being right. Correctness outranks speed.
      - Your training has a cutoff; the world moves on. For anything that can change —
        versions, model names, APIs, prices, current events, "does X exist" — SEARCH
        THE WEB and verify before stating it.
      - Absence from your memory is NOT proof something doesn't exist. Look it up.
      - Report what the machine ACTUALLY returned. Never fabricate paths, outputs, or facts.

      WHEN UNSURE — ASK YOUR MASTER
      - If ambiguous, risky, destructive, or unverifiable: STOP and ask. A precise
        question beats a confident guess. Pause before anything irreversible.
      - Your master's word is the deciding authority.

      HOW YOU OPERATE
      - Everything local. No cloud, no data leaves the machine.
      - Concise but complete. One clear message per turn. Real reasoning, not filler.
      - All team output lives in the master's OneDrive folder:
        /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
        (proposals/ projects/ reports/ screenshots/ trends/). Every document is
        delivered three ways: emailed, sent to Telegram, and saved there to sync.
        Reference real paths there, never invented ones.

  ada:
    name: Ada
    role: Product Owner / Program Manager
    model: ada
    system_prompt: |
      You are ADA — Product Owner and Program Manager, a principal-level product mind
      on a 70B-class model. Orion hands you a reconstructed, clarified brief; you turn
      it into a rigorous proposal and a clean task plan. You coordinate every agent and
      you are the single source of truth between the master and the team.

      DEPTH OVER SPEED — ACCURACY IS THE POINT
      - Deep analysis, not surface summaries. Interrogate the idea: who it's for, the
        real problem, constraints, edge cases, failure modes.
      - Correctness and completeness beat an aggressive timeline. Estimate honestly;
        never pad to look fast. Flag uncertainty openly.

      VERIFY AGAINST THE REAL WORLD
      - Trends, frameworks, libraries, versions move on past any cutoff. Before
        recommending a stack or approach, cross-check current information from the
        internet so the proposal reflects what's actually current and supported.
      - If unsure whether something exists/is current, verify — never invent.

      THE TEAM ANSWERS THROUGH YOU
      - Mira (UI/UX): collaborate to get wireframes & mockups (image + editable draw.io);
        fold her visuals and notes into the proposal.
      - Leo: your tickets are his marching orders — precise, buildable, unambiguous. He
        builds from what you proposed and what the master approved; he may suggest
        improvements, which you evaluate.
      - Nova: write user stories with acceptance criteria she can test E2E against. When
        Nova or Leo asks a question you can't answer with certainty, ASK THE MASTER —
        do not guess on their behalf.

      DELIVERABLES — POLISHED, PROFESSIONAL
      - Clean Markdown: ## headings, tables where useful, tight prose.
      - Structure: 1 Executive Summary · 2 Problem & Goals · 3 Target Users & Use Cases ·
        4 User Stories (Given/When/Then + acceptance criteria) · 5 UI/UX Direction (Mira) ·
        6 Technical Scope & Architecture · 7 Tech Stack (current, verified, justified) ·
        8 Milestones & Realistic Estimates · 9 Risks, Edge Cases & Mitigations ·
        10 Open Questions / Assumptions.
      - Your Markdown is rendered to a polished PDF, emailed to the master, sent to
        Telegram, and saved in OneDrive AI-Agent/proposals. Write with that care.

      IMPORTANT — end every proposal with EXACTLY this ticket block (no exceptions):
      ---TICKETS---
      STORY|high|[Story title]|[One-sentence description]
      STORY|medium|[Story title]|[One-sentence description]
      TASK|high|[Task title]|[One-sentence description]
      TASK|medium|[Task title]|[One-sentence description]
      (Add as many STORY and TASK lines as the project needs — minimum 3 stories)

  mira:
    name: Mira
    role: Senior UI/UX Designer
    model: mira
    system_prompt: |
      You are MIRA — a senior UI/UX designer with obsessive attention to detail. Every
      pixel matters: spacing, alignment, hierarchy, contrast, rhythm. You can analyse
      images and screenshots. You work directly with Ada, and sometimes independently
      with the master via Orion.

      STAY CURRENT
      - Before designing, check current UI/UX trends and patterns on the internet so
        your work reflects modern, accessible, real-world design — not dated defaults.
      - Never invent a component library's API or a trend that you can't confirm.

      DELIVERABLES — TWO FORMATS, ALWAYS
      For every wireframe and mockup screen, produce BOTH:
      1. A clear visual description detailed enough to render as an image.
      2. An EDITABLE draw.io diagram as valid mxGraph XML, so the master can change it.
      draw.io XML RULES (follow exactly — verified against draw.io docs):
      - Output the FULL wrapper, not a bare mxGraphModel (bare ones save blank):
        <mxfile host="app.diagrams.net"><diagram id="p1" name="Screen">
        <mxGraphModel dx="800" dy="600" grid="1" gridSize="10" page="1"
        pageWidth="1169" pageHeight="827"><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        ...cells with parent="1"...
        </root></mxGraphModel></diagram></mxfile>
      - Mandatory cells: id="0" (root) and id="1" parent="0" (default layer).
      - Vertices need vertex="1"; edges need edge="1" (mutually exclusive).
      - Styles are key=value; e.g. "rounded=1;whiteSpace=wrap;html=1;fillColor=#DAE8FC;".
      - Plain uncompressed XML. NO XML comments. All ids unique.
      - Wrap each draw.io block in a fenced code block labelled `xml` and name the file,
        e.g. ### screen_login.drawio  then the ```xml ... ``` block, so it gets saved.

      DESIGN BRIEFS
      - Provide user journeys, screen-by-screen wireframe descriptions, component lists,
        colour palette (hex), typography scale, spacing system, and accessibility notes
        (contrast ratios, focus states, hit targets).

      IF YOU NEED HELP
      - If a requirement is unclear or you need a decision, reach out to the master via
        Orion rather than guessing. Precision over assumption.

      All your output saves to /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
      and is also emailed and sent to Telegram.

  leo:
    name: Leo
    role: Super-Senior Full-Stack Developer
    model: leo
    system_prompt: |
      You are LEO — a super-senior full-stack engineer. You ship production-grade code in
      any language or stack. The internet is your best friend: use it to confirm current
      versions, APIs, and best practices before you build. You work under Ada in an Agile
      flow and pair with Nova to fix bugs.

      CORRECTNESS AND ACCURACY FIRST
      - Your top priority is correct, accurate, smoothly-running code. Quality over speed.
      - Use current, well-supported stacks and libraries — verify versions and APIs on the
        web; never code against a remembered API you haven't confirmed. Never invent
        package names, flags, or function signatures.
      - ALWAYS self-test before handing to Nova: run it, exercise the main paths, fix what
        breaks. Only pass work to QA once it actually runs.

      SCOPE, DECISIONS, IMPROVEMENTS
      - Build from what Ada proposed and what the master approved. That is your contract.
      - You MAY make improvements and add helpful functionality — additional value is
        welcome — but get confirmation (via Ada -> master) before any BIG assumption or
        decision that changes scope, cost, data model, or user-facing behavior.
      - When unsure, ask through Ada rather than guessing.

      DELIVERY
      - Provide complete code with a README.md (setup, run, test instructions).
      - Name every file clearly in your output (### path/name.ext then a fenced code
        block) so files are saved correctly.
      - Work with Nova: read her reports, reproduce bugs, fix root causes, re-test.
      - When fully done and verified, end with exactly: DEPLOYMENT COMPLETE
        followed by a summary of what was built and how to run it.

      All your output saves to /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
      and is also emailed and sent to Telegram.

  nova:
    name: Nova
    role: QA Tester (Perfectionist)
    model: nova
    system_prompt: |
      You are NOVA — a detail-obsessed, perfectionist QA engineer. Your test coverage is
      complete and end-to-end. You miss nothing. The internet is your best friend for
      current testing techniques.

      BEFORE TESTING
      - Read Ada's proposal and test EVERY functionality against it. Do not assume intended
        behavior — if the spec is unclear, ASK ADA (Ada asks the master if unsure). Never
        invent acceptance criteria.
      - Write complete, advanced test cases FIRST (Given/When/Then), then execute them.

      WHAT TO COVER
      - Both UI/UX and backend functionality. Happy paths, edge cases, boundaries, error
        handling, security basics, performance, accessibility.
      - For WEB apps, drive a real browser with Puppeteer (https://pptr.dev). Test actual
        user flows in the rendered UI, not just APIs.
      - Simulate HUMAN interaction timing, not robotic instant input: add realistic delays
        between actions, typing cadence, and waits for elements/animations. Many bugs only
        appear at human latency.

      REPORTING
      - Produce a complete, detailed report for Ada and Leo so Ada can create tickets and
        Leo has everything needed to fix.
      - Each bug: [BUG-NNN] Title | Severity: Critical/High/Medium/Low | Steps to reproduce
        | Expected vs Actual | Affected area (UI/backend) | Evidence.
      - If everything passes, end with exactly: ALL TESTS PASSED

      All your output saves to /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
      and is also emailed and sent to Telegram.

  cipher:
    name: Cipher
    role: White-Hat Pentester (On-Demand)
    model: cipher
    system_prompt: |
      You are CIPHER — a highly creative white-hat penetration tester. You think like a
      real attacker to defend the master's own systems. You only ever act on EXPLICIT
      invocation and only against systems the master OWNS or has WRITTEN AUTHORIZATION to
      test. Confirm authorization before any active testing — this is the single rule that
      separates security work from a crime, and you never skip it.

      WITHIN THAT SCOPE, BE FEARLESS AND THOROUGH
      - Be aggressive and creative in technique. Chain weaknesses, think laterally, probe
        assumptions. Real attackers are creative; so are you.
      - Stay current: pull the latest techniques, CVEs, and tooling from the internet.
        Kali Linux tooling and docs are your toolkit; OWASP (Testing Guide, Top 10, ASVS,
        cheat sheets) is your methodology reference. Verify, don't rely on stale memory.
      - Cover the spectrum: recon, enumeration, web (injection, auth, access control,
        SSRF, deserialization), network, config, secrets, supply chain, and logic flaws.

      REPORTING
      - Report each finding as:
        [VULN-NNN] Title | Severity | CVSS | Description | Exploit vector | Proof/PoC notes | Remediation
      - Prioritize by real-world risk and give concrete, actionable fixes.

      BOUNDARIES
      - Never assist in attacking systems the master doesn't own or isn't authorized to
        test, and never help with illegal activity. Authorization is mandatory, always.

      All your output saves to /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent
      and is also emailed and sent to Telegram.

  vox:
    name: Vox
    role: Opportunity Scout & Marketing
    model: vox
    system_prompt: |
      You are VOX — the most creative opportunity scout alive, and the master's one-person
      marketing team. Twice a day you read the news across ALL categories (tech, business,
      finance, science, agriculture, health, culture, local/world events) and propose
      fresh ideas.

      YOUR PRIME DIRECTIVE: MAKE THE MASTER MONEY
      - Your first intention with every idea is a realistic way to MAKE MONEY. It need not
        be an app — it can be automation, an AI-for-agriculture play, a service, a content
        or arbitrage angle, a workflow others will pay for, anything.
      - Then also include ideas that help people or improve something, but lead with the
        money angle and explain the business model (who pays, why, how much, how to start).

      QUALITY & FRESHNESS
      - Search the web every session; base ideas on what's actually happening NOW. Verify
        claims — never fabricate a trend, statistic, market size, or tool.
      - Propose NEW ideas each session; don't repeat yesterday's. Broaden constantly.

      FORMAT (per idea)
      - Bold Title | the money opportunity in 2 sentences | who pays & business model |
        why it's timely now (cite the news/source) | what to build or do | tech/tools |
        complexity (Simple/Medium/Complex) | rough first step this week.
      - Attach or reference relevant images for inspiration where helpful.

      MARKETING HAT
      - For promising ideas, suggest how to market and monetize: channels, audience,
        positioning, and a lean go-to-market the master could run solo.

      You send ideas to the master via Telegram (with images for reference) and they are
      also saved to /Users/thaqifisa/Library/CloudStorage/OneDrive-Personal/AI-Agent/trends.
TEAMEOF
    ok "team.yaml written (enhanced personas)."

    # ── orchestrator.py — always regenerate ──────────────────────────────────
    cat > "$AD/orchestrator.py" <<'ORCHEOF'
#!/usr/bin/env python3
"""Orion — Multi-Agent Telegram Orchestrator (enhanced).
New: clean project titles, Orion idea reconstruction, PDF render (WeasyPrint),
email (via the Mac's Mail.app), three-way document delivery (OneDrive+Telegram+email),
Mira draw.io saver, Vox money-first ideas.
"""
import os, sys, json, yaml, logging, datetime, threading, asyncio, subprocess, platform, time, re, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters)

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))
WORKSPACE = os.environ.get("AI_WORKSPACE",
    os.path.join(os.path.expanduser("~"),
                 "Library/CloudStorage/OneDrive-Personal/AI-Agent"))
MASTER_EMAIL = os.environ.get("MASTER_EMAIL", "mthaqifisa@pm.me")
GW   = f"http://localhost:{os.environ.get('PORT_GATEWAY','4000')}/v1"
SX   = f"http://localhost:{os.environ.get('PORT_SEARXNG','8888')}/search"
PF   = os.path.join(HOME, "projects.json")
SF   = os.path.join(HOME, "agent_status.json")
with open(os.path.join(HOME, "agents", "team.yaml")) as _f:
    ROLES = yaml.safe_load(_f)["roles"]
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("orion")
client = OpenAI(base_url=GW, api_key="local")
_exec  = ThreadPoolExecutor(max_workers=3)
_PL    = threading.Lock()
_SL    = threading.Lock()

# ── Workspace helpers ─────────────────────────────────────────────────────────
def _safe_name(text, max_len=45):
    n = re.sub(r'[^\w\s-]', '', str(text).lower())
    n = re.sub(r'\s+', '_', n.strip())
    return n[:max_len] or "untitled"

def _clean_title(raw, fallback="Untitled Project"):
    """Short, clean human title from a raw idea — fixes dashboard prompt-dumping."""
    t = raw.strip().splitlines()[0] if raw.strip() else fallback
    t = re.sub(r'^(please\s+)?(can you\s+)?(build|create|make|develop|code|program|write)'
               r'(\s+me)?(\s+a|\s+an|\s+the)?\s+', '', t, flags=re.IGNORECASE).strip()
    t = re.sub(r'^(i\s+want\s+to\s+|i\s+need\s+(you\s+to\s+)?|let.?s\s+)'
               r'(build|create|make|develop)\s+', '', t, flags=re.IGNORECASE).strip()
    t = re.sub(r'^(a|an|the)\s+', '', t, flags=re.IGNORECASE).strip()
    t = t.strip(' .!,:;"\'')
    if not t: t = fallback
    words = t.split()
    if len(words) > 8: t = ' '.join(words[:8])
    t = t[:60].strip()
    return (t[:1].upper() + t[1:]) if t else fallback

def _ws(*parts):
    path = os.path.join(WORKSPACE, *parts)
    os.makedirs(path, exist_ok=True)
    return path

def _save_code_files(response, output_dir):
    """Parse code blocks with filenames from LLM output and save as real files."""
    patterns = [
        r'[`*]{1,3}([^\s`*\n]+\.[a-zA-Z0-9]+)[`*]{0,3}\s*\n```[a-zA-Z]*\n(.*?)```',
        r'###\s*[`]?([^\n`]+\.[a-zA-Z0-9]+)[`]?\s*\n```[a-zA-Z]*\n(.*?)```',
        r'##\s*[`]?([^\n`]+\.[a-zA-Z0-9]+)[`]?\s*\n```[a-zA-Z]*\n(.*?)```',
    ]
    saved = []
    for pat in patterns:
        for m in re.finditer(pat, response, re.DOTALL):
            fname, content = m.group(1).strip().lstrip('/').lstrip('./'), m.group(2)
            if not fname or '..' in fname: continue
            fpath = os.path.join(output_dir, fname)
            try:
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, 'w') as f: f.write(content)
                saved.append(fname)
            except Exception: pass
    return saved

def _save_drawio_files(response, output_dir):
    """Extract named draw.io XML blocks from Mira's output, save as editable .drawio."""
    saved = []
    pattern = r'(?:#{1,3}\s*[`]?([^\n`]+?\.drawio)[`]?\s*\n)?```xml\s*\n(.*?)```'
    idx = 0
    for m in re.finditer(pattern, response, re.DOTALL | re.IGNORECASE):
        xml = m.group(2).strip()
        if 'mxGraphModel' not in xml: continue
        idx += 1
        fname = (m.group(1) or f"wireframe_{idx}.drawio").strip().lstrip('/').lstrip('./')
        if not fname.endswith('.drawio'): fname += '.drawio'
        if '..' in fname: continue
        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, fname), 'w') as f: f.write(xml)
            saved.append(fname)
        except Exception: pass
    return saved

# ── PDF rendering (Ada's proposals) ───────────────────────────────────────────
def mt_render_pdf(md_text, out_path, title="Proposal"):
    """Render markdown → polished PDF via WeasyPrint. Returns (ok, path_or_err)."""
    try:
        import markdown as _md
        from weasyprint import HTML
        body = _md.markdown(md_text, extensions=["tables","fenced_code","toc"])
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        html = ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "@page { size: A4; margin: 2cm; @bottom-right { content: counter(page);"
            " color:#888; font-size:10px; } }"
            "body { font-family:-apple-system,Helvetica,Arial,sans-serif; color:#1a1a1a;"
            " line-height:1.55; font-size:11pt; }"
            "h1 { color:#1f3a5f; border-bottom:3px solid #1f3a5f; padding-bottom:8px; }"
            "h2 { color:#28527a; margin-top:1.4em; border-bottom:1px solid #ddd; padding-bottom:4px; }"
            "h3 { color:#3a6ea5; }"
            "table { border-collapse:collapse; width:100%; margin:1em 0; }"
            "th,td { border:1px solid #ccc; padding:7px 10px; text-align:left; font-size:10pt; }"
            "th { background:#1f3a5f; color:#fff; }"
            "tr:nth-child(even){ background:#f5f7fa; }"
            "code { background:#f0f0f0; padding:1px 5px; border-radius:3px; font-size:9.5pt; }"
            "pre { background:#f6f8fa; border:1px solid #e0e0e0; border-radius:6px; padding:12px; }"
            ".titlepage { text-align:center; margin-top:30%; }"
            ".titlepage h1 { border:none; font-size:30pt; }"
            ".meta { color:#666; font-size:10pt; margin-top:1em; }"
            "</style></head><body>"
            f"<div class='titlepage'><h1>{title}</h1>"
            f"<div class='meta'>Prepared by Ada · Product Owner<br>{stamp}</div></div>"
            "<div style='page-break-before:always'></div>"
            f"{body}</body></html>")
        HTML(string=html).write_pdf(out_path)
        return (True, out_path)
    except Exception as e:
        logger.error(f"PDF render failed: {e}")
        return (False, str(e))

# ── Email (Ada → master) via the Mac's Mail.app (AppleScript) ─────────────────
def _applescript_str(s):
    """Escape a Python string for safe embedding in an AppleScript double-quoted literal."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')

def mt_send_email(subject, body, attachment_path=None):
    """Send email to MASTER_EMAIL via the Mail.app already configured on this Mac.
    Uses AppleScript so it sends through whatever account Mail is signed into —
    no SMTP credentials needed. Attaches the file if given. Returns (ok, msg).

    Optional .env override SMTP_FROM picks which Mail account/address sends it
    (must match an address Mail.app already owns); otherwise Mail's default is used.
    """
    to       = os.environ.get("MASTER_EMAIL", MASTER_EMAIL)
    sender   = os.environ.get("SMTP_FROM", "").strip()  # optional
    subj_e   = _applescript_str(subject)
    body_e   = _applescript_str(body)
    to_e     = _applescript_str(to)
    sender_e = _applescript_str(sender)

    # Build the attachment clause only if the file really exists.
    attach_clause = ""
    if attachment_path and os.path.isfile(attachment_path):
        posix_e = _applescript_str(os.path.abspath(attachment_path))
        # 'delay 1' before/after attaching is the standard Mail.app reliability
        # workaround so the attachment is fully added before the message sends.
        attach_clause = (
            '\n        delay 1'
            f'\n        make new attachment with properties {{file name:(POSIX file "{posix_e}")}}'
            ' at after the last paragraph'
            '\n        delay 2'
        )

    sender_clause = f'\n        set sender to "{sender_e}"' if sender else ""

    script = (
        'tell application "Mail"\n'
        '    set newMsg to make new outgoing message with properties '
        f'{{subject:"{subj_e}", content:"{body_e}", visible:false}}\n'
        '    tell newMsg\n'
        f'{sender_clause}'
        f'        make new to recipient at end of to recipients with properties {{address:"{to_e}"}}'
        f'{attach_clause}\n'
        '    end tell\n'
        '    send newMsg\n'
        'end tell\n'
    )

    try:
        # Write the script to a temp file to avoid any shell-escaping pitfalls.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".applescript",
                                         delete=False) as tf:
            tf.write(script)
            script_path = tf.name
        try:
            r = subprocess.run(["osascript", script_path],
                               capture_output=True, text=True, timeout=90)
        finally:
            try: os.unlink(script_path)
            except Exception: pass
        if r.returncode == 0:
            return (True, f"Email sent to {to} via Mail.app.")
        errtxt = (r.stderr or r.stdout or "unknown error").strip()
        # The classic first-run failure is the Automation permission prompt.
        if "Not authorized" in errtxt or "-1743" in errtxt or "1743" in errtxt:
            return (False,
                "Mail.app send blocked — grant Automation permission: System Settings "
                "→ Privacy & Security → Automation → allow your service/Terminal to "
                "control Mail, then retry.")
        logger.error(f"Mail.app send failed: {errtxt}")
        return (False, f"Mail.app send failed: {errtxt[:160]}")
    except subprocess.TimeoutExpired:
        return (False, "Mail.app send timed out (is Mail.app able to launch?).")
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return (False, f"Email failed: {e}")

# ── State labels ──────────────────────────────────────────────────────────────
STATES = {
    "idle":              ("No active project",                "⬜"),
    "proposal_drafting": ("Ada & Mira writing proposal",      "🟡"),
    "awaiting_approval": ("Awaiting your proposal approval",  "🔵"),
    "development":       ("Leo building",                     "🟡"),
    "qa_running":        ("Nova testing",                     "🟡"),
    "qa_bugs_found":     ("Bugs found — decision needed",     "🔴"),
    "final_review":      ("Ada doing final review",           "🟡"),
    "awaiting_final":    ("Awaiting your final approval",     "🔵"),
    "completed":         ("Project complete",                 "🟢"),
    "paused":            ("Paused — manual IDE mode",         "⏸️"),
}

# ── Ticket helpers ─────────────────────────────────────────────────────────────
def _prefix(idea):
    words = re.sub(r'[^\w\s]','',idea.lower()).split()
    return ''.join(w[0].upper() for w in words[:3]) or "PRJ"

def _parse_tickets(text, idea, existing=None):
    existing = existing or []
    prefix = _prefix(idea)
    start_idx = len(existing) + 1
    tickets = []
    in_block = False
    for line in text.splitlines():
        if '---TICKETS---' in line:
            in_block = True; continue
        if not in_block: continue
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('---') and in_block and tickets: break
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 3: continue
        ttype = parts[0].upper()
        if ttype not in ('STORY','TASK','BUG','SUBTASK'): continue
        priority = parts[1].lower() if parts[1].lower() in ('critical','high','medium','low') else 'medium'
        title = parts[2] if len(parts) > 2 else 'Untitled'
        desc  = parts[3] if len(parts) > 3 else ''
        tickets.append({
            'id':       f"{prefix}-{start_idx + len(tickets):03d}",
            'type':     ttype.lower(), 'title': title, 'desc': desc,
            'priority': priority, 'status': 'todo', 'assignee': 'leo',
            'created':  str(datetime.datetime.now())[:19],
            'updated':  str(datetime.datetime.now())[:19],
        })
    return tickets

def _parse_bugs(qa_text, idea, existing_count=0):
    prefix = _prefix(idea)
    bugs, seen = [], set()
    pattern = r'\[BUG-(\d+)\]\s+([^\|\n]+?)(?:\s*\|\s*(\w+))?(?:\s*\|\s*([^\n]+))?'
    for m in re.finditer(pattern, qa_text):
        bid = m.group(1)
        if bid in seen: continue
        seen.add(bid)
        severity = (m.group(3) or 'medium').lower()
        priority = 'high' if severity in ('critical','high','blocker') else 'medium'
        bugs.append({
            'id': f"{prefix}-BUG-{bid}", 'type': 'bug',
            'title': m.group(2).strip(), 'desc': (m.group(4) or '').strip(),
            'priority': priority, 'status': 'todo', 'assignee': 'leo',
            'created': str(datetime.datetime.now())[:19],
            'updated': str(datetime.datetime.now())[:19],
        })
    return bugs

# ── File listing/sending tools ─────────────────────────────────────────────────
def mt_list_workspace_files(cid):
    projects = load_projects()
    proj = projects.get(cid, {})
    idea = proj.get("idea", "")
    lines = []
    if idea:
        pname = _safe_name(idea)
        for folder, label in [
            (os.path.join(WORKSPACE, "proposals", pname), "Proposals"),
            (os.path.join(WORKSPACE, "projects", pname),  "Project files"),
        ]:
            if os.path.exists(folder):
                for fname in sorted(os.listdir(folder)):
                    fpath = os.path.join(folder, fname)
                    if os.path.isfile(fpath):
                        sz = os.path.getsize(fpath)
                        lines.append(f"• [{label}] `{fname}` ({sz//1024}KB)")
    shots = os.path.join(WORKSPACE, "screenshots")
    if os.path.exists(shots):
        for f in sorted(os.listdir(shots))[-5:]:
            lines.append(f"• [Screenshots] `{f}`")
    if not lines:
        return "No files generated yet. Start a project first."
    return "📁 *Your workspace files:*\n" + "\n".join(lines)

def mt_find_file(query, cid):
    projects = load_projects()
    proj = projects.get(cid, {})
    idea = proj.get("idea","")
    if not idea:
        return ("text","No active project found. Start a project first.","")
    pname = _safe_name(idea)
    ml = query.lower()
    if "screenshot" in ml:
        shots = os.path.join(WORKSPACE, "screenshots")
        if os.path.exists(shots):
            files = [f for f in sorted(os.listdir(shots)) if f.endswith('.png')]
            if files:
                return ("photo", os.path.join(shots, files[-1]), f"📸 {files[-1]}")
        return ("text","No screenshots found. Use /screenshot first.","")
    for kw in ["proposal","brief","design"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "proposals", pname)
            if os.path.exists(folder):
                files = [f for f in os.listdir(folder) if f.endswith(('.pdf','.md'))]
                if files:
                    path = os.path.join(folder, sorted(files)[-1])
                    return ("file", path, os.path.basename(path))
    for kw in ["qa","test","bug","report"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "projects", pname)
            if os.path.exists(folder):
                for f in sorted(os.listdir(folder), reverse=True):
                    if any(x in f for x in ["qa_","bug_"]):
                        return ("file", os.path.join(folder, f), f)
    for kw in ["code","implementation","leo","project"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "projects", pname)
            if os.path.exists(folder):
                for f in sorted(os.listdir(folder), reverse=True):
                    if "leo_output" in f:
                        return ("file", os.path.join(folder, f), f)
    return ("text", "Couldn't find that file. Use `/files` to see all available files.","")

# =============================================================================
# MACHINE TOOLS — Orion runs on the Mac with full system access.
# =============================================================================
import psutil, requests as _req

def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out or "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Command timed out."
    except Exception as e:
        return False, str(e)

def mt_list_models():
    try:
        r = _req.get("http://localhost:11434/api/tags", timeout=5)
        models = r.json().get("models", [])
        if not models:
            return "No models pulled yet. Run: ollama pull <model>"
        lines = []
        for m in sorted(models, key=lambda x: x.get("name","")):
            sz = m.get("size", 0)
            lines.append(f"• `{m['name']}` — {sz/1e9:.1f} GB" if sz else f"• `{m['name']}`")
        return "🤖 *Models on this machine:*\n" + "\n".join(lines)
    except Exception as e:
        return f"Could not reach Ollama: {e}"

def mt_system_info():
    vm = psutil.virtual_memory()
    disk = None
    for p in ("/System/Volumes/Data", os.path.expanduser("~"), "/"):
        try: disk = psutil.disk_usage(p); break
        except Exception: continue
    cpu = psutil.cpu_percent(interval=1)
    bat = None
    try:
        b = psutil.sensors_battery()
        if b: bat = f"{round(b.percent)}% {'⚡ charging' if b.power_plugged else 'on battery'}"
    except Exception: pass
    def gb(n): return f"{n/1e9:.1f} GB" if n < 1e12 else f"{n/1e12:.1f} TB"
    lines = [
        f"💻 *System Info ({platform.machine()})*",
        f"• CPU:     {cpu}%",
        f"• RAM:     {gb(vm.used)} / {gb(vm.total)} ({round(vm.percent)}%)",
        f"• Disk:    {gb(disk.used)} / {gb(disk.total)} ({round(disk.percent)}%)" if disk else "• Disk: unavailable",
        f"• Battery: {bat}" if bat else "• Battery: N/A",
        f"• macOS:   {platform.mac_ver()[0]}",
    ]
    return "\n".join(lines)

def mt_list_files(path="~"):
    ok, out = _run(f"ls -lah {path} 2>&1 | head -40")
    return f"📂 `{path}`:\n```\n{out}\n```"

def mt_running_services():
    checks = {
        "Ollama":"http://localhost:11434/api/tags",
        "LiteLLM":"http://localhost:4000/health/liveliness",
        "Dashboard":"http://localhost:8800/",
        "Open WebUI":"http://localhost:3001/",
        "SearXNG":"http://localhost:8888/",
        "Langfuse":"http://localhost:3000/api/public/health",
        "Portainer":"http://localhost:9001/",
    }
    lines = ["🔧 *Service Status:*"]
    for name, url in checks.items():
        try:
            ok = _req.get(url, timeout=2).status_code < 500
            lines.append(f"• {name}: {'✅ live' if ok else '❌ down'}")
        except Exception:
            lines.append(f"• {name}: ❌ down")
    return "\n".join(lines)

def mt_shell(cmd):
    ok, out = _run(cmd, timeout=60)
    return f"{'✅' if ok else '❌'} `{cmd}`\n```\n{out[:3000]}\n```"

def mt_open_app(app_name):
    ok, out = _run(f'open -a "{app_name}"')
    return f"✅ Opened *{app_name}*." if ok else f"❌ Could not open {app_name}: {out}"

def mt_music(action="play", query=""):
    scripts = {
        "pause": 'tell application "Music" to pause',
        "stop":  'tell application "Music" to stop',
        "next":  'tell application "Music" to next track',
        "prev":  'tell application "Music" to previous track',
        "play":  'tell application "Music" to play' if not query else
                 f'tell application "Spotify" to play track "spotify:search:{query}"',
    }
    script = scripts.get(action, scripts["play"])
    ok, out = _run(f"osascript -e '{script}'")
    return f"🎵 {action.capitalize()}ing music." if ok else f"❌ Music control failed: {out}"

# ── Browser navigation ────────────────────────────────────────────────────────
_SITE_MAP = {
    "youtube":"https://www.youtube.com","netflix":"https://www.netflix.com",
    "github":"https://www.github.com","google":"https://www.google.com",
    "stackoverflow":"https://stackoverflow.com","reddit":"https://www.reddit.com",
    "linkedin":"https://www.linkedin.com","amazon":"https://www.amazon.com",
    "gmail":"https://mail.google.com","twitter":"https://www.x.com",
    "instagram":"https://www.instagram.com","wikipedia":"https://www.wikipedia.org",
    "chatgpt":"https://chat.openai.com",
}

def _extract_query(msg):
    for pat in [
        r'search\s+(?:for\s+)?["\u201c\u2018]([^"\u201d\u2019]+)["\u201d\u2019]',
        r"search\s+(?:for\s+)?'([^']+)'",
        r'search\s+(?:for\s+)?([\w\s\-\.\"\']+?)(?:\s+on\s|\s*$)',
        r'look\s+up\s+["\']?([^"\']+?)["\']?(?:\s+on|\s*$)',
        r'find\s+["\']?([^"\']+?)["\']?(?:\s+on|\s*$)',
    ]:
        m = re.search(pat, msg, re.IGNORECASE)
        if m:
            q = m.group(1).strip().strip('"\'')
            if 2 < len(q) < 120: return q
    return None

def _build_nav_url(msg):
    ml = msg.lower()
    u = re.search(r'https?://\S+', msg)
    if u: return u.group(0).rstrip('.,)')
    if "youtube" in ml:
        q = _extract_query(msg)
        if not q:
            m = re.search(r'for\s+["\u201c]?([^"\u201d,\.\n]+)', msg, re.IGNORECASE)
            q = m.group(1).strip() if m else None
        return (f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
                if q else "https://www.youtube.com")
    if "google" in ml and any(w in ml for w in ["search","look up","find"]):
        q = _extract_query(msg)
        return (f"https://www.google.com/search?q={urllib.parse.quote(q)}"
                if q else "https://www.google.com")
    for site, url in _SITE_MAP.items():
        if site in ml: return url
    return None

def mt_browse(msg):
    url = _build_nav_url(msg)
    if not url:
        return f"❌ Could not determine URL from: '{msg[:80]}'"
    for browser in ["Google Chrome","Chrome","Firefox","Safari"]:
        ok, out = _run(f'open -a "{browser}" "{url}"')
        if ok: return f"✅ Opened `{url}` in {browser}."
    ok, out = _run(f'open "{url}"')
    return f"✅ Opened `{url}`." if ok else f"❌ Could not open browser: {out}"

# ── Screenshot tools ─────────────────────────────────────────────────────────
def mt_screenshot_screen():
    path = os.path.join(_ws("screenshots"), f"screen_{int(time.time())}.png")
    ok, out = _run(f"screencapture -x {path}")
    if ok and os.path.exists(path):
        return ("photo", path, "📸 Full screen screenshot")
    return ("text", f"❌ Screenshot failed: {out}", "")

def mt_screenshot_url(url, label=""):
    safe_label = re.sub(r'[^\w]', '_', label or url)[:30]
    path = os.path.join(_ws("screenshots"), f"{safe_label}_{int(time.time())}.png")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            pg = browser.new_context(viewport={"width":1400,"height":900},
                                     ignore_https_errors=True).new_page()
            pg.goto(url, wait_until="networkidle", timeout=20000)
            time.sleep(1)
            pg.screenshot(path=path, full_page=False)
            browser.close()
        return ("photo", path, f"📸 {label or url}")
    except ImportError:
        return ("text", "❌ Playwright not installed.\n"
            f"Run: `{HOME}/.venv/bin/pip install playwright && "
            f"{HOME}/.venv/bin/playwright install chromium`", "")
    except Exception as e:
        return ("text", f"❌ Screenshot of {url} failed: {e}", "")

_URL_SHORTCUTS = {
    "dashboard":("Dashboard","http://localhost:8800"),
    "portainer":("Portainer","http://localhost:9001"),
    "open webui":("Open WebUI","http://localhost:3001"),
    "langfuse":("Langfuse","http://localhost:3000"),
    "searxng":("SearXNG","http://localhost:8888"),
    "ollama":("Ollama","http://localhost:11434"),
    "gateway":("LiteLLM","http://localhost:4000"),
}

def _parse_url_target(msg):
    ml = msg.lower()
    pm = re.search(r'localhost:(\d+)', ml) or re.search(r'port (\d+)', ml)
    if pm:
        port = pm.group(1)
        return f"localhost:{port}", f"http://localhost:{port}"
    for key, (label, url) in _URL_SHORTCUTS.items():
        if key in ml:
            return label, url
    return None, None

# ── Tool dispatch ──────────────────────────────────────────────────────────────
def _build_call(msg):
    ml = msg.lower()
    if any(w in ml for w in ["list model","what model","which model","models on",
                              "installed model","available model","ollama model","do i have model"]):
        return "List installed models", mt_list_models, False
    if any(w in ml for w in ["system info","disk space","how much ram","ram usage",
                              "cpu usage","how much storage","battery","memory usage"]):
        return "System info", mt_system_info, False
    if any(w in ml for w in ["list file","show file","what file","ls ~","what's in"]):
        path = "~"
        m = re.search(r'(?:in|of|at)\s+([\w~/\.\-]+)', ml)
        if m: path = m.group(1)
        return f"List files in {path}", lambda p=path: mt_list_files(p), False
    if any(w in ml for w in ["service status","what's running","which service",
                              "services running","check service","is ollama"]):
        return "Check service status", mt_running_services, False
    if any(w in ml for w in ["screenshot","snap my","take a photo","capture screen",
                              "show me my","show me the dashboard","show me the","what does my screen"]):
        label, url = _parse_url_target(msg)
        if url:
            return f"Screenshot of {label} ({url})", lambda u=url,l=label: mt_screenshot_url(u,l), True
        return "Full screen screenshot", mt_screenshot_screen, True
    if any(w in ml for w in ["pause music","pause song","stop music","stop playing"]):
        return "Pause music", lambda: mt_music("pause"), True
    if any(w in ml for w in ["next song","skip song","next track","skip track"]):
        return "Skip to next track", lambda: mt_music("next"), True
    if any(w in ml for w in ["previous song","prev song","previous track","go back"]):
        return "Previous track", lambda: mt_music("prev"), True
    if any(w in ml for w in ["play music","resume music","play song","play spotify","open music"]):
        return "Play music", lambda: mt_music("play"), True
    if any(w in ml for w in ["list my files","list files","my files","workspace files",
                              "what files","files in my workspace"]):
        return "List workspace files", lambda: mt_list_workspace_files(""), False
    if any(w in ml for w in ["search for ","search the web","look up ","search online",
                              "google ","find info about","find information about","research "]):
        query = re.sub(r'^(search for|search the web for|look up|google|search online for|'
                       r'find info(rmation)? (about|on)|research)\s+', '', ml, flags=re.IGNORECASE).strip()
        if not query: query = msg
        return f"Web search: {query}", lambda q=query: (
            "text", "🔍 *Search results for:* _" + q + "_\n\n" + _search_web_sync(q, max_results=5), ""), False
    _nav_kw = ["go to youtube","open youtube","navigate to youtube","youtube search",
        "search youtube","search on youtube","look up on youtube","find on youtube",
        "navigate to","go to http","open http","browse to","open website",
        "navigate to google","search on google","open google and"]
    _nav_compound = re.search(r'open\s+(chrome|browser|safari|firefox)\s+(and|to|then)', ml)
    if any(kw in ml for kw in _nav_kw) or _nav_compound:
        url = _build_nav_url(msg)
        desc = f"Open browser → {url}" if url else f"Browse: {msg[:60]}"
        return desc, lambda m=msg: mt_browse(m), True
    if any(site in ml for site in _SITE_MAP) and any(
            w in ml for w in ["go to","open","navigate","visit","browse","take me"]):
        url = _build_nav_url(msg)
        if url:
            return f"Open browser → {url}", lambda m=msg: mt_browse(m), True
    if "open " in ml or "launch " in ml:
        for app in ["Spotify","Terminal","Safari","Chrome","Firefox","Finder",
                    "VS Code","Xcode","Notes","Calendar","Messages","Mail","Slack"]:
            if app.lower() in ml:
                return f"Open {app}", lambda a=app: mt_open_app(a), True
        m = re.search(r'(?:open|launch)\s+([\w\s]+)', ml)
        if m:
            app = m.group(1).strip().title()
            return f"Open {app}", lambda a=app: mt_open_app(a), True
    return None, None, None

def detect_tool(msg):
    return _build_call(msg)

# ── I/O helpers ───────────────────────────────────────────────────────────────
def load_projects():
    with _PL:
        if not os.path.exists(PF): return {}
        try:
            with open(PF) as f: return json.load(f)
        except Exception: return {}

def save_projects(data):
    with _PL:
        with open(PF, "w") as f: json.dump(data, f, indent=2, default=str)
        os.chmod(PF, 0o600)

def update_project(cid, **kw):
    p = load_projects(); p.setdefault(cid, {}).update(kw); save_projects(p); return p[cid]

def get_project(cid): return load_projects().get(cid, {})

def write_status(upd):
    with _SL:
        d = {}
        if os.path.exists(SF):
            try:
                with open(SF) as f: d = json.load(f)
            except Exception: pass
        d.update(upd)
        with open(SF, "w") as f: json.dump(d, f)

def read_status():
    try:
        with open(SF) as f: return json.load(f)
    except Exception: return {n: "idle" for n in ROLES}

# ── Agent calling ─────────────────────────────────────────────────────────────
def _invoke_sync(name, msgs, temp=0.7):
    cfg = ROLES[name]
    full = [{"role":"system","content":cfg["system_prompt"]}] + msgs
    write_status({name: "working"})
    try:
        r = client.chat.completions.create(
            model=cfg["model"], messages=full, temperature=temp, max_tokens=2500)
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[{name}] {e}")
        return f"⚠️ {name.capitalize()} error: {e}"
    finally:
        write_status({name: "idle"})

async def invoke(name, msgs, temp=0.7):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_exec, lambda: _invoke_sync(name, msgs, temp))

def _strip_think(text):
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

async def invoke_think(name, msgs, temp=0.7):
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1] + [{"role":"user","content":"/think " + msgs[-1]["content"]}]
    raw = await invoke(name, msgs, temp)
    return _strip_think(raw)

async def _reconstruct_idea(raw):
    """Orion rewrites the master's raw idea into a structured brief for Ada."""
    prompt = ("The master sent this project idea:\n\n"
        f"\"{raw}\"\n\n"
        "Rewrite it as a clear, structured brief for the product owner (Ada). "
        "Infer the goal, intended users, key features, and any implied constraints. "
        "Do NOT invent specific facts, brand names, or numbers that weren't given — "
        "where something is unknown, say it's to be confirmed. Keep it tight: "
        "a short paragraph plus 3-6 bullet points. Output only the brief.")
    try:
        brief = _strip_think(await invoke("orion", [{"role":"user","content":prompt}]))
        return brief if brief and len(brief) > 20 else raw
    except Exception:
        return raw

_ADA_CONSULT_KW = ["how should i build","best way to build","best approach to",
    "recommend a stack","what tech stack","which framework should","architecture for",
    "how to design","should i use","what database","microservice or monolith",
    "help me plan","how would you architect","api design","what's the best approach"]
_THINK_KW = ["explain how","why does","what is the difference between","analyze",
    "compare","trade-off","deep dive","how exactly","walk me through","break down"]
def _needs_ada(msg): return any(k in msg.lower() for k in _ADA_CONSULT_KW)
def _needs_think(msg): return len(msg) > 60 and any(k in msg.lower() for k in _THINK_KW)

def _search_web_sync(query, max_results=4):
    try:
        r = _req.get(SX, params={"q":query,"format":"json"}, timeout=8)
        results = r.json().get("results", [])[:max_results]
        if not results: return "No results found."
        return "\n\n".join(f"[{res.get('title','')}] {res.get('content','')[:250]}" for res in results)
    except Exception as e:
        return f"Search unavailable: {e}"

# ── Telegram send helpers ──────────────────────────────────────────────────────
TRIGGER_FILE = os.path.join(HOME, "pending_actions.json")
_app = None
CONV_DIR = os.path.join(HOME, "conversations")
_CONV = {}
_CONV_MAX = 30

def _conv_path(cid):
    os.makedirs(CONV_DIR, exist_ok=True)
    return os.path.join(CONV_DIR, f"{cid}.json")

def _conv_load(cid):
    if cid in _CONV: return
    try:
        with open(_conv_path(cid)) as f:
            _CONV[cid] = json.load(f).get("messages", [])
    except Exception:
        _CONV[cid] = []

def _conv_save(cid):
    try:
        with open(_conv_path(cid), "w") as f:
            json.dump({"cid":cid,"updated":str(datetime.datetime.now()),
                       "messages":_CONV.get(cid,[])}, f, indent=2)
    except Exception as e:
        logger.warning(f"conv save failed: {e}")

def _conv_add(cid, role, content):
    _conv_load(cid)
    _CONV[cid].append({"role":role,"content":str(content)[:4000]})
    if len(_CONV[cid]) > _CONV_MAX:
        _CONV[cid] = _CONV[cid][-_CONV_MAX:]
    _conv_save(cid)

def _conv_get(cid):
    _conv_load(cid)
    return list(_CONV.get(cid, []))

def _conv_clear(cid):
    _CONV[cid] = []
    _conv_save(cid)
    logger.info(f"Conversation cleared for {cid}")

async def send(ctx, cid, text, kb=None, pm="Markdown"):
    chunks = [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]
    for i, chunk in enumerate(chunks):
        markup = kb if i == len(chunks)-1 else None
        for mode in [pm, None]:
            try:
                await ctx.bot.send_message(chat_id=int(cid), text=chunk,
                                           reply_markup=markup, parse_mode=mode)
                break
            except Exception as e:
                err_str = str(e).lower()
                if mode is not None and ("can't parse" in err_str or "parse entities" in err_str
                                         or "parsing" in err_str):
                    logger.warning(f"Markdown parse failed for cid={cid}, retrying as plain text.")
                    continue
                logger.error(f"send(): {e}")
                break

async def send_result(ctx, cid, result, kb=None):
    if isinstance(result, tuple) and result[0] == "photo":
        _, path, caption = result
        try:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(chat_id=int(cid), photo=f, caption=caption, reply_markup=kb)
        except Exception as e:
            await send(ctx, cid, f"❌ Could not send photo: {e}")
    elif isinstance(result, tuple) and result[0] == "file":
        _, path, name = result
        try:
            with open(path, "rb") as f:
                await ctx.bot.send_document(chat_id=int(cid), document=f, filename=name, reply_markup=kb)
        except Exception as e:
            await send(ctx, cid, f"❌ Could not send file: {e}")
    else:
        text = result[1] if isinstance(result, tuple) else result
        await send(ctx, cid, str(text), kb)

def mkb(*pairs):
    return InlineKeyboardMarkup([[InlineKeyboardButton(l, callback_data=d)] for l, d in pairs])

# ── Three-way document delivery: OneDrive (already saved) + Telegram + Email ───
async def deliver_document(ctx, cid, file_path, *, subject, body,
                           telegram_caption=None, as_pdf_email=True):
    """Deliver a document 3 ways: OneDrive folder (already there) + Telegram + Email."""
    results = []
    name = os.path.basename(file_path)
    if file_path.startswith(WORKSPACE) and os.path.isfile(file_path):
        results.append("📁 Saved to OneDrive folder")
    else:
        results.append("⚠️ Not in OneDrive folder")
    try:
        await send_result(ctx, cid, ("file", file_path, telegram_caption or name))
        results.append("📲 Sent to Telegram")
    except Exception as e:
        results.append(f"⚠️ Telegram failed: {e}")
    mail_ok, mail_msg = await asyncio.get_running_loop().run_in_executor(
        _exec, lambda: mt_send_email(subject, body, file_path))
    results.append("📧 Emailed" if mail_ok else f"⚠️ {mail_msg}")
    await send(ctx, cid, "*Delivery:*\n" + "\n".join(f"• {r}" for r in results))
    return results

async def keep_typing(bot, cid, stop):
    while not stop.is_set():
        try: await bot.send_chat_action(chat_id=int(cid), action="typing")
        except Exception: pass
        await asyncio.sleep(4)

# ── State machine ─────────────────────────────────────────────────────────────
async def workflow(ctx, cid):
    proj   = get_project(cid)
    status = proj.get("status", "idle")
    idea   = proj.get("idea", "")
    brief  = proj.get("brief", idea)
    hist   = proj.get("history", [])
    pname  = _safe_name(idea)

    if status == "proposal_drafting":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "🗂️ *Ada & Mira drafting your proposal…* _(~2 min)_")
            ap = (f"Project brief:\n{brief}\n\nWrite a full proposal (Markdown):\n"
                  "## 1. Executive Summary\n## 2. Problem & Goals\n"
                  "## 3. Target Users & Use Cases\n## 4. User Stories (Given/When/Then)\n"
                  "## 5. UI/UX Direction\n## 6. Tech Scope & Architecture\n"
                  "## 7. Tech Stack\n## 8. Milestones\n## 9. Risks & Mitigations\n"
                  "## 10. Open Questions")
            proposal = await invoke("ada", hist + [{"role":"user","content":ap}])
            mp = (f"Project: {idea}\nBased on this proposal:\n\n{proposal[:2000]}\n\n"
                  "Write a UI/UX design brief AND editable wireframes.\n"
                  "## 1. User Journey\n## 2. Key Screens (wireframe descriptions)\n"
                  "## 3. Visual Principles\n## 4. Component List\n## 5. Accessibility\n\n"
                  "Then for each key screen, output an editable draw.io diagram. Name each "
                  "(e.g. ### screen_home.drawio) followed by a ```xml fenced block with the "
                  "FULL <mxfile>…</mxfile> wrapper.")
            design = await invoke("mira", [{"role":"user","content":mp}])
        finally: stop.set()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = os.path.join(_ws("proposals", pname), f"proposal_{ts}.md")
        md_full = (f"# {idea}\n\n## Ada's Proposal\n\n{proposal}\n\n"
                   f"## Mira's Design Brief\n\n{design}\n")
        with open(fp, "w") as f: f.write(md_full)
        # Save Mira's editable draw.io wireframes
        drawio_files = _save_drawio_files(design, os.path.join(_ws("proposals", pname)))
        # Render polished PDF
        pdf_path = os.path.join(_ws("proposals", pname), f"proposal_{ts}.pdf")
        pdf_ok, pdf_res = await asyncio.get_running_loop().run_in_executor(
            _exec, lambda: mt_render_pdf(md_full, pdf_path, title=idea[:60]))
        # Deliver proposal three ways
        await send(ctx, cid, "📎 _Delivering proposal (OneDrive + Telegram + email)…_")
        if pdf_ok:
            await deliver_document(ctx, cid, pdf_path,
                subject=f"Proposal: {idea[:70]}",
                body=(f"Hi,\n\nAda here. The proposal for \"{idea}\" is ready.\n\n"
                      f"EXECUTIVE SUMMARY\n{proposal[:1200]}\n\n"
                      f"Full proposal with design direction attached as PDF.\n"
                      f"Also saved in OneDrive AI-Agent/proposals.\n\n— Ada"),
                telegram_caption=f"📄 Proposal: {idea[:50]}")
        else:
            await send(ctx, cid, f"_(PDF render unavailable: {pdf_res[:120]} — sending markdown)_")
            await deliver_document(ctx, cid, fp,
                subject=f"Proposal: {idea[:70]}",
                body=f"Proposal for \"{idea}\" attached (markdown; PDF render failed).\n\n— Ada",
                telegram_caption=f"📄 Proposal (md): {idea[:50]}")
        # Deliver editable wireframes
        if drawio_files:
            await send(ctx, cid, f"🎨 Mira's editable wireframes: {', '.join(drawio_files)}")
            for df in drawio_files:
                await deliver_document(ctx, cid,
                    os.path.join(_ws("proposals", pname), df),
                    subject=f"Wireframe: {df}",
                    body=f"Editable draw.io wireframe from Mira, attached.\n\n— Mira",
                    telegram_caption=f"🎨 {df}", as_pdf_email=False)
        # Tickets
        tickets = _parse_tickets(proposal, idea)
        if not tickets:
            tickets = [{"id":f"{_prefix(idea)}-001","type":"story","title":idea,
                        "desc":"Main project story","priority":"high","status":"todo",
                        "assignee":"leo","created":ts,"updated":ts}]
        preview = (f"*📄 Proposal: {idea}*\n\n"
                   f"**Ada (excerpt)**\n{proposal[:700]}…\n\n"
                   f"**Mira (excerpt)**\n{design[:300]}…\n\n"
                   f"🎫 *{len(tickets)} tickets created*")
        update_project(cid, status="awaiting_approval", proposal_file=fp,
            proposal_pdf=(pdf_path if pdf_ok else ""), tickets=tickets,
            history=hist+[{"role":"user","content":ap},{"role":"assistant","content":proposal},
                          {"role":"user","content":mp},{"role":"assistant","content":design}])
        await send(ctx, cid, preview,
            mkb(("✅ Approve — start development", "approve_proposal"),
                ("🔄 Request changes",             "revise_proposal"),
                ("❌ Reject this idea",            "reject_proposal")))

    elif status == "development":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "👨‍💻 *Leo building…* _(may take several minutes)_")
            dp = ("Build the project from the approved proposal.\n"
                  "Use current, verified stacks. Self-test before finishing.\n"
                  "Write complete, production-ready code with a README.md.\n"
                  "Name each file (### path/name.ext then a fenced code block).\n"
                  "End with: DEPLOYMENT COMPLETE\nThen summarise what was built.")
            result = await invoke("leo", hist + [{"role":"user","content":dp}])
        finally: stop.set()
        nh = hist + [{"role":"user","content":dp},{"role":"assistant","content":result}]
        if "DEPLOYMENT COMPLETE" in result.upper():
            proj_dir = _ws("projects", pname)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_md = os.path.join(proj_dir, f"leo_output_{ts}.md")
            with open(out_md, "w") as f: f.write(f"# {idea}\n_Generated: {ts}_\n\n{result}\n")
            saved_files = _save_code_files(result, proj_dir)
            files_msg = (f"\n\n📁 *Saved to OneDrive AI-Agent/projects/{pname}/*"
                         + (f"\n_Files: {', '.join(saved_files)}_" if saved_files else ""))
            update_project(cid, status="qa_running", history=nh, project_dir=proj_dir)
            await send(ctx, cid, f"✅ *Leo: Deployment complete!*\n\n{result[:1400]}{files_msg}")
            await deliver_document(ctx, cid, out_md,
                subject=f"Build: {idea[:60]}",
                body=f"Leo's build output for \"{idea}\" attached.\n\n— Leo",
                telegram_caption=f"leo_output_{ts}.md", as_pdf_email=False)
            await send(ctx, cid, "🔎 Activating *Nova* for QA…")
            await workflow(ctx, cid)
        else:
            update_project(cid, status="development", history=nh)
            await send(ctx, cid, f"👨‍💻 *Leo update:*\n\n{result[:2000]}",
                mkb(("✅ Mark deployed — move to QA", "force_qa"),
                    ("📝 Give Leo more instructions",  "instruct_leo")))

    elif status == "qa_running":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "🔎 *Nova running end-to-end tests…*")
            qp = ("Test the project thoroughly against Ada's proposal.\n"
                  "Write complete advanced test cases FIRST (Given/When/Then), then run them.\n"
                  "For web apps, drive a real browser with Puppeteer at HUMAN interaction "
                  "latency (realistic delays, typing cadence, waits). Test UI/UX AND backend.\n"
                  "All pass → end with: ALL TESTS PASSED\n"
                  "Bugs → [BUG-NNN] Title | Severity | Steps | Expected vs Actual | Area")
            result = await invoke("nova", hist + [{"role":"user","content":qp}])
        finally: stop.set()
        nh = hist + [{"role":"user","content":qp},{"role":"assistant","content":result}]
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if "ALL TESTS PASSED" in result.upper():
            qa_path = os.path.join(_ws("projects", pname), f"qa_report_{ts}.md")
            with open(qa_path, "w") as f: f.write(f"# QA Report: {idea}\n_Date: {ts}_\n\n{result}\n")
            update_project(cid, status="final_review", history=nh)
            await send(ctx, cid, f"✅ *Nova: All tests passed!*\n\n{result[:1400]}")
            await deliver_document(ctx, cid, qa_path,
                subject=f"QA Report (PASSED): {idea[:60]}",
                body=f"Nova's QA report for \"{idea}\" — all tests passed. Attached.\n\n— Nova",
                telegram_caption=f"qa_report_{ts}.md", as_pdf_email=False)
            await send(ctx, cid, "📋 Activating *Ada* for final review…")
            await workflow(ctx, cid)
        else:
            bug_path = os.path.join(_ws("projects", pname), f"bug_report_{ts}.md")
            with open(bug_path, "w") as f: f.write(f"# Bug Report: {idea}\n_Date: {ts}_\n\n{result}\n")
            existing = proj.get("tickets", [])
            new_bugs = _parse_bugs(result, idea, len(existing))
            update_project(cid, status="qa_bugs_found", history=nh, tickets=existing + new_bugs)
            await send(ctx, cid, f"🔴 *Nova found issues:*\n\n{result[:1700]}"
                f"\n\n🎫 *{len(new_bugs)} bug tickets created*")
            await deliver_document(ctx, cid, bug_path,
                subject=f"Bug Report: {idea[:60]}",
                body=f"Nova found issues in \"{idea}\". Full bug report attached.\n\n— Nova",
                telegram_caption=f"bug_report_{ts}.md", as_pdf_email=False)
            await send(ctx, cid, "What next?",
                mkb(("🛠️ Fix bugs — reactivate Leo",  "fix_bugs"),
                    ("⚠️ Accept as-is (known bugs)", "accept_bugs")))

    elif status == "final_review":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "📋 *Ada doing final PM review…*")
            rp = ("Final PM sign-off. Check project against original proposal.\n"
                  "Structure: ✅ Done | ⚠️ Minor issues | 🔴 Blockers\n"
                  "End with: APPROVED or NEEDS_CHANGES: [reason]")
            result = await invoke("ada", hist + [{"role":"user","content":rp}])
        finally: stop.set()
        nh = hist + [{"role":"user","content":rp},{"role":"assistant","content":result}]
        update_project(cid, status="awaiting_final", history=nh)
        await send(ctx, cid, f"📋 *Ada's Final Review:*\n\n{result[:2000]}",
            mkb(("🎉 Accept & complete project", "accept_final"),
                ("🔄 More changes needed",       "more_changes")))
    else:
        desc, icon = STATES.get(status, (f"Status: {status}", "❓"))
        await send(ctx, cid, f"{icon} {desc}")

# ── Vox trend helper ─────────────────────────────────────────────────────────
async def run_vox(ctx, cid):
    import requests as req
    try:
        r = req.get(SX, params={"q":"news today business technology agriculture finance opportunity",
                    "format":"json"}, timeout=10)
        snippets = "\n".join(f"- {x.get('title','')}: {x.get('content','')[:200]}"
                             for x in r.json().get("results",[])[:8])
    except Exception as e: snippets = f"Search unavailable: {e}"
    stop = asyncio.Event()
    ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
    try:
        result = await invoke("vox",[{"role":"user","content":
            f"Today's news across categories:\n{snippets}\n\n"
            "Propose 3 fresh MONEY-MAKING ideas (lead with the money angle, business "
            "model, who pays). Then market/monetization notes. Per idea: bold title, "
            "opportunity, who pays & model, why now (cite source), what to do, tools, "
            "complexity, first step this week."}], temp=0.9)
    finally: stop.set()
    await send(ctx, cid, f"📡 *Vox's Money Ideas:*\n\n{result}",
        mkb(("🚀 Start a project from these", "start_from_trend"),
            ("👍 Just browsing, thanks",       "dismiss_trend")))
    ctx.user_data["last_vox"] = result

# ── Identity card ─────────────────────────────────────────────────────────────
_ORION_ID = (
    "I'm *Orion* — your AI team running locally on this Mac.\n\n"
    "🧠 *Me:* `qwen3.6:35b-a3b` via Ollama\n\n"
    "*My team:*\n"
    "• 📊 *Ada* `qwen2.5:72b` — proposals (PDF + email), planning, reviews\n"
    "• 🎨 *Mira* `gemma4:26b` — UI/UX, wireframes (image + draw.io), image analysis\n"
    "• 💻 *Leo* `qwen2.5-coder:72b` — super-senior full-stack dev\n"
    "• 🔎 *Nova* `qwen2.5:72b` — QA, end-to-end testing (Puppeteer)\n"
    "• 🛡️ *Cipher* `qwen2.5:72b` — white-hat pentester (authorization required)\n"
    "• 📡 *Vox* `qwen2.5:72b` — twice-daily money ideas & marketing\n\n"
    "*What I can do:*\n"
    "• Answer anything · control this Mac · search the web\n"
    "• Build software end-to-end with my team\n"
    "• Deliver every document 3 ways: 📧 email · 📲 Telegram · 📁 OneDrive\n\n"
    "_Everything local. No cloud. No data leaves this machine._"
)
_DEFLECT_PHRASES = [
    "not disclose","cannot disclose","not able to share","cannot reveal",
    "i don't have access to information about my","not authorized","designed not to",
    "cannot share information about my","i am not able to provide","my training does not",
    "i cannot tell you what model","cannot confirm","i'm just an ai","just an assistant",
    "cannot navigate to","i cannot navigate","cannot browse","i cannot browse",
    "i cannot access the internet","cannot access websites","i cannot open",
    "do not have direct access to your system","i do not have direct access",
    "i don't have real-time access","i cannot perform actions on your",
    "i am unable to access your","not equipped to directly access","i only process text",
    "i cannot execute","cannot perform internet searches","cannot access external information",
    "don't have internet access","cannot browse the internet","no internet access",
    "i cannot search the internet","unable to search the web","i cannot access the web",
    "cannot retrieve information from the internet","my knowledge cutoff","i cannot look up",
    "i have no access to the internet",
]
def _is_deflecting(text):
    ml = text.lower()
    return any(p in ml for p in _DEFLECT_PHRASES)
_ABOUT_ORION_KW = ["agent","team","your model","your capab","your tool","your skill",
    "what can you","what do you","can you do","are you able","you able to","your system",
    "your hardware","real-time","access to","who are you","what are you","how many",
    "your agents","your team","list your","your features","number of","how do you work"]
def _is_about_orion(msg): return any(k in msg.lower() for k in _ABOUT_ORION_KW)
_SEARCH_KW = ["search for","look up","find info","latest news","current price","news about",
    "search the web","search online","what happened","find me information","look for",
    "what is the latest","recent news","today's","right now","current","up to date",
    "live price","stock price","weather in","who won","what time","when does","near me"]
def _needs_web_search(msg): return any(kw in msg.lower() for kw in _SEARCH_KW)

# ── Plugin system ─────────────────────────────────────────────────────────────
PLUGINS_DIR = os.path.join(HOME, "agents", "plugins")
_PLUGINS = {}
def _load_plugins():
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    count = 0
    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if not fname.endswith(".py"): continue
        try:
            ns = {}
            exec(open(os.path.join(PLUGINS_DIR, fname)).read(), ns)
            for name, fn in ns.items():
                if name.startswith("mt_") and callable(fn):
                    _PLUGINS[name] = fn
                    logger.info(f"Plugin loaded: {name} ← {fname}")
            count += 1
        except Exception as e:
            logger.error(f"Plugin {fname} failed: {e}")
    return count

ORCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.aiws.orchestrator.plist")
async def self_restart(ctx, cid, reason="capability upgrade"):
    await send(ctx, cid, f"🔄 _Restarting ({reason})… back in ~10 seconds._")
    subprocess.Popen(["bash","-c",
        f"sleep 3 && launchctl unload '{ORCH_PLIST}'; sleep 2 && launchctl load '{ORCH_PLIST}'"])
    await asyncio.sleep(1)
    import signal as _signal
    os.kill(os.getpid(), _signal.SIGTERM)

async def orion_self_upgrade(ctx, cid, capability):
    live = await ctx.bot.send_message(chat_id=int(cid),
        text=f"🔬 _Researching how to add: {capability}_", parse_mode="Markdown")
    async def upd(t):
        try: await live.edit_text(t, parse_mode="Markdown")
        except Exception: pass
    loop = asyncio.get_running_loop()
    await upd(f"🔍 _Searching: Python {capability} implementation…_")
    search_res = await loop.run_in_executor(_exec,
        lambda: _search_web_sync(f"Python {capability} library code example"))
    await upd("💭 _Writing plugin code…_")
    fn_slug = re.sub(r'[^\w]', '_', capability.lower()[:30])
    code_prompt = (f"Write a Python tool function called `mt_{fn_slug}` that implements: {capability}\n"
        f"Context from web:\n{search_res[:1000]}\n\n"
        "Requirements:\n- Use stdlib or a single pip-installable package\n"
        "- First line comment: # requires: <package_name> (or 'stdlib')\n"
        "- Return a string OR a ('photo'|'file', path, caption) tuple\n"
        "- Handle errors with try/except — never raise\n"
        "- No top-level imports — put imports inside the function\n"
        "Output ONLY the Python function code.")
    code_raw = await invoke("orion", [{"role":"user","content":code_prompt}])
    code = re.sub(r'```python\n?|```\n?', '', code_raw).strip()
    pkg_match = re.search(r'#\s*requires:\s*([\w\-\[\]>=<.,\s]+)', code)
    package = (pkg_match.group(1).strip() if pkg_match else "").strip()
    fn_name_m = re.search(r'def\s+(mt_\w+)', code)
    fn_name = fn_name_m.group(1) if fn_name_m else "mt_new_tool"
    try: await live.delete()
    except Exception: pass
    ctx.user_data["pending_plugin"] = {"capability":capability,"package":package,
                                       "fn_name":fn_name,"code":code}
    pkg_line = f"*Package:* `{package}`\n" if package and package != "stdlib" else ""
    await send(ctx, cid,
        f"🔬 *New capability ready*\n\n*Capability:* {capability}\n{pkg_line}"
        f"*Function:* `{fn_name}`\n\n```python\n{code[:800]}{'…' if len(code)>800 else ''}\n```\n\n"
        "Install this and restart Orion?",
        mkb(("✅ Install & restart", "plugin_yes"), ("❌ Cancel", "plugin_no")))

_PROJECT_SIGNALS = ["build me ","build a ","build an ","create me ","create a ","create an ",
    "make me an ","make me a ","make an app","make a website","make a tool",
    "i want to build","i want to create","i want to make an","i want to make a",
    "let's build","let us build","let's create","start a project","new project",
    "develop a ","develop an ","write me a program","write me a script",
    "code me a","code a ","program a ","i need you to build","i need you to create",
    "can you build","can you create","can you make me"]
def _is_clear_project(msg):
    return any(s in msg.lower() for s in _PROJECT_SIGNALS)

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(u, ctx):
    cid = str(u.effective_chat.id)
    _conv_clear(cid)
    await send(ctx, cid,
        "👋 *I'm Orion, your AI team lead.*\n\n"
        "🤖 *The team:*\n"
        "📊 Ada — Product Owner / PM _(proposals: PDF + email)_\n"
        "🎨 Mira — Senior UI/UX _(image + editable draw.io)_\n"
        "💻 Leo — Super-Senior Developer\n"
        "🔎 Nova — QA Tester _(Puppeteer E2E)_\n"
        "🛡️ Cipher — White-hat Pentester _(authorization required)_\n"
        "📡 Vox — Opportunity Scout _(twice-daily money ideas)_\n\n"
        "💡 *Send me a project idea to start!*\n"
        "Docs are delivered 3 ways: 📧 email · 📲 Telegram · 📁 OneDrive\n"
        "/status · /projects · /trends · /help · /clear")

async def cmd_clear(u, ctx):
    cid = str(u.effective_chat.id)
    _conv_clear(cid)
    await send(ctx, cid, "🧹 Conversation cleared — fresh start!")

async def cmd_status(u, ctx):
    cid = str(u.effective_chat.id)
    proj = get_project(cid); sts = read_status()
    desc, icon = STATES.get(proj.get("status","idle"), ("No project","⬜"))
    lines = [f"{icon} *Project:* {desc}"]
    if proj.get("idea"): lines.append(f"💡 *Title:* {proj['idea'][:60]}")
    lines.append("\n*Agents:*")
    for nm, ic in [("orion","🤖"),("ada","📊"),("mira","🎨"),("leo","💻"),
                   ("nova","🔎"),("cipher","🛡️"),("vox","📡")]:
        s = sts.get(nm,"idle")
        lines.append(f"{ic} {nm.capitalize()}: {'🟡 *working*' if s=='working' else '🟢 idle'}")
    await send(ctx, cid, "\n".join(lines))

async def cmd_projects(u, ctx):
    cid = str(u.effective_chat.id)
    mine = [v for v in load_projects().values() if str(v.get("chat_id",""))==cid]
    if not mine: await send(ctx, cid, "No projects yet — send me an idea!"); return
    lines = []
    for p in sorted(mine, key=lambda x: x.get("created",""), reverse=True):
        d, ic = STATES.get(p.get("status","idle"), ("?","❓"))
        lines.append(f"{ic} *{(p.get('idea') or '?')[:50]}*\n  _{d}_ · {(p.get('created') or '')[:10]}")
    await send(ctx, cid, "*Your Projects*\n\n" + "\n\n".join(lines))

async def cmd_trends(u, ctx):
    cid = str(u.effective_chat.id)
    await send(ctx, cid, "📡 *Vox scouting money-making opportunities…*")
    await run_vox(ctx, cid)

async def cmd_pause(u, ctx):
    cid = str(u.effective_chat.id); proj = get_project(cid)
    curr = proj.get("status","idle")
    if curr not in ("idle","completed","paused"):
        update_project(cid, status="paused", prev_status=curr)
        await send(ctx, cid, "⏸️ *Agents paused.* The 72B slot is free.\n"
            "VS Code → Continue → *Leo Manual (qwen3.6:27b)*.\nSend /resume when done.")
    else:
        ctx.user_data["manually_paused"] = True
        await send(ctx, cid, "⏸️ *Paused* (no active project).\nSend /resume when done.")

async def cmd_resume(u, ctx):
    cid = str(u.effective_chat.id)
    ctx.user_data.pop("manually_paused", None)
    proj = get_project(cid)
    if proj.get("status") == "paused":
        prev = proj.get("prev_status","idle")
        update_project(cid, status=prev)
        d, ic = STATES.get(prev, ("Resumed","▶️"))
        await send(ctx, cid, f"▶️ *Resumed.* Back to: {ic} {d}")
    else:
        await send(ctx, cid, "▶️ *Agents active.* Ready for a new idea!")

async def cmd_help(u, ctx):
    cid = str(u.effective_chat.id)
    await send(ctx, cid,
        "/start · /status · /projects · /trends · /pause · /resume · /help · /run · /screenshot · /files · /upgrade · /clear\n\n"
        "*Project:* just send an idea (I'll structure it + give it a clean title)\n"
        "*Machine (read):* 'list models', 'system info', 'service status'\n"
        "*Machine (control — needs approval):* 'open Spotify', 'play music'\n"
        "*Screenshot:* `/screenshot`, `/screenshot 8800`, or 'screenshot of the dashboard'\n"
        "*Shell:* `/run <command>`\n"
        "*Pentest:* say 'pentest [target]' (authorization confirmation required)\n"
        "*Docs:* every document is emailed, sent to Telegram, and saved in OneDrive")

async def cmd_screenshot(u, ctx):
    cid = str(u.effective_chat.id)
    arg = u.message.text.partition(" ")[2].strip()
    if not arg or arg.lower() in ("screen","full","mac"):
        desc, call = "Full screen screenshot", mt_screenshot_screen
    else:
        label, url = _parse_url_target(arg)
        if not url:
            if arg.isdigit():
                url = f"http://localhost:{arg}"; label = f"localhost:{arg}"
            else:
                url = arg if arg.startswith("http") else f"http://{arg}"; label = arg
        desc = f"Screenshot of {label or url}"
        call = lambda u=url, l=label: mt_screenshot_url(u, l)
    ctx.user_data["pending_tool"] = {"desc":desc,"call":call}
    await send(ctx, cid, f"📸 *Screenshot requested*\n\n_{desc}_\n\nProceed?",
        mkb(("✅ Yes, take it","tool_yes"),("❌ Cancel","tool_no")))

async def cmd_run(u, ctx):
    cid = str(u.effective_chat.id)
    cmd = u.message.text.partition(" ")[2].strip()
    if not cmd:
        await send(ctx, cid, "Usage: `/run <shell command>`\nExample: `/run ollama list`"); return
    ctx.user_data["pending_shell"] = cmd
    await send(ctx, cid, f"🖥️ *Shell command requested:*\n```\n{cmd}\n```\n\nRun this on your Mac?",
        mkb(("✅ Yes, run it","shell_yes"),("❌ Cancel","shell_no")))

async def cmd_files(u, ctx):
    cid = str(u.effective_chat.id)
    loop = asyncio.get_running_loop()
    listing = await loop.run_in_executor(_exec, lambda: mt_list_workspace_files(cid))
    await send(ctx, cid, listing + "\n\n_Ask me to send any of them:_\n"
        "`send me the proposal` · `send me the QA report` · `send me the latest screenshot`")

async def cmd_upgrade(u, ctx):
    cid = str(u.effective_chat.id)
    capability = u.message.text.partition(" ")[2].strip()
    if not capability:
        plugins = list(_PLUGINS.keys()) or ["none yet"]
        await send(ctx, cid, "🔬 *Orion self-upgrade*\n\n"
            f"*Installed plugins:* {', '.join(f'`{p}`' for p in plugins)}\n\n"
            "Tell me what to add:\n`/upgrade web scraping`\n`/upgrade read pdf files`"); return
    await orion_self_upgrade(ctx, cid, capability)

# ── Main message handler ───────────────────────────────────────────────────────
async def on_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not u.message or not u.message.text: return
    msg = u.message.text.strip(); cid = str(u.effective_chat.id)
    try: await ctx.bot.send_chat_action(chat_id=int(cid), action="typing")
    except Exception: pass
    if ctx.user_data.get("manually_paused"):
        await send(ctx, cid, "⏸️ Agents paused. /resume to re-enable."); return
    proj = get_project(cid); status = proj.get("status","idle")
    if status == "paused":
        await send(ctx, cid, "⏸️ Project paused. /resume to continue."); return

    identity_kw = ["which model","what model","what are you","who are you","what version",
        "running on","your model","what llm","which llm","are you gpt","are you claude",
        "are you gemini","are you chatgpt","are you openai","are you qwen","what ai are you",
        "which ai","what is your name","tell me about yourself","what are you based on",
        "which company made you","what base model","which language model","are you an ai",
        "are you a bot","powered by","underlying model","your architecture","your team",
        "my team","your agents","my agents","list your agents","who are your agents",
        "what agents do you have","who is on your team","what can you do",
        "what are your capabilities","your capabilities","what do you have access to",
        "what tools do you have","what are your features","what can orion do","your skills",
        "tell me about your team","introduce your team","who helps you","your specialists",
        "your team members","the team","your system","do you have access","direct access",
        "hardware access","can you control my","can you access my","access to my mac",
        "access to my machine","access to my computer"]
    if any(kw in msg.lower() for kw in identity_kw):
        await send(ctx, cid, _ORION_ID); return

    file_request_kws = ["send me the proposal","send proposal","share proposal",
        "send me the code","send the code","send leo","send qa report","send qa",
        "send bug report","send me the report","send screenshot","send me screenshot",
        "send latest screenshot","send me the latest"]
    if any(kw in msg.lower() for kw in file_request_kws):
        await send(ctx, cid, "📎 _Fetching file…_")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_exec, lambda: mt_find_file(msg, cid))
        await send_result(ctx, cid, result); return

    tool_desc, tool_call, needs_approval = detect_tool(msg)
    if tool_call is not None:
        if not needs_approval:
            await send(ctx, cid, "⚙️ _Checking your machine…_")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(_exec, tool_call)
            await send_result(ctx, cid, result)
        else:
            ctx.user_data["pending_tool"] = {"desc":tool_desc,"call":tool_call}
            await send(ctx, cid, f"🖥️ *Machine action requested*\n\n_{tool_desc}_\n\nRun this on your Mac?",
                mkb(("✅ Yes, do it","tool_yes"),("❌ Cancel","tool_no")))
        return
    if any(w in msg.lower() for w in ["pentest","hack ","security audit",
                                       "vulnerability scan","ctf ","bug bounty"]):
        ctx.user_data["pending_pentest"] = msg
        await send(ctx, cid, f"🛡️ *Cipher requested*\n\nTask: _{msg}_\n\n"
            "⚠️ Confirm you OWN this target or have WRITTEN AUTHORIZATION to test it.",
            mkb(("⚠️ Yes — I'm authorized, proceed","cipher_yes"),("❌ Cancel","cipher_no"))); return

    if any(w in msg.lower() for w in ["trend","what should i build","inspire",
                                       "suggest project","what to build","make money","money idea"]) \
            and status in ("idle","completed"):
        await cmd_trends(u, ctx); return

    if status not in ("idle","completed"):
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            proj = get_project(cid)
            history = _conv_get(cid)
            r = await invoke("orion", history + [{"role":"user","content":
                f"Current project: {proj.get('idea','?')} (status: {status})\nUser: {msg}\n\n"
                "Answer helpfully. General question → answer directly. "
                "Continue the project → guide next steps."}])
            r = _strip_think(r)
            _conv_add(cid, "user", msg); _conv_add(cid, "assistant", r)
            await send(ctx, cid, r)
        finally: stop.set()
        return

    proj_summary = "; ".join(f"{p.get('idea','?')} ({p.get('status','?')})"
                             for p in load_projects().values()) or "none"

    if _is_clear_project(msg):
        await send(ctx, cid, f"🚀 *New project request received*\n\n🧠 Orion structuring the brief…")
        brief = await _reconstruct_idea(msg)
        title = _clean_title(msg)
        update_project(cid, status="proposal_drafting", idea=title, request=msg, brief=brief,
            created=str(datetime.datetime.now()), history=[], chat_id=cid)
        await send(ctx, cid, f"📋 *{title}*\n\n*Brief for Ada:*\n{brief[:800]}\n\nAda & Mira drafting…")
        await workflow(ctx, cid)
        return

    stop = asyncio.Event()
    ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
    try:
        history = _conv_get(cid)
        ctx_prefix = (f"[Active projects: {proj_summary}]\n" if proj_summary != "none" else "")
        if _needs_ada(msg):
            await send(ctx, cid, "🤔 _Getting Ada's expert take…_")
            ada_ans = await invoke("ada", history + [{"role":"user","content":
                f"{ctx_prefix}User asks: {msg}\n\nAnswer as a senior PM and technical advisor. "
                "Be specific and practical; verify current facts."}])
            _conv_add(cid, "user", msg); _conv_add(cid, "assistant", ada_ans)
            await send(ctx, cid, f"📊 *Ada:*\n\n{ada_ans}")
        elif _needs_think(msg):
            ans = _strip_think(await invoke_think("orion", history + [{"role":"user","content":ctx_prefix+msg}]))
            _conv_add(cid, "user", msg); _conv_add(cid, "assistant", ans)
            await send(ctx, cid, _ORION_ID if _is_deflecting(ans) else ans)
        else:
            msgs = history + [{"role":"user","content":ctx_prefix+msg}]
            if _needs_web_search(msg):
                live = await ctx.bot.send_message(chat_id=int(cid), text="🔍 _Searching the web…_", parse_mode="Markdown")
                sr = await asyncio.get_running_loop().run_in_executor(_exec, lambda: _search_web_sync(msg, max_results=5))
                try: await live.delete()
                except Exception: pass
                msgs = history + [{"role":"user","content":
                    f"{ctx_prefix}[Web search results for: {msg}]\n\n{sr}\n\n"
                    f"User question: {msg}\n\nAnswer using the results; cite sources."}]
            ans = _strip_think(await invoke("orion", msgs))
            if _is_deflecting(ans):
                if any(p in ans.lower() for p in ["cannot perform internet","cannot access external",
                        "cannot search","no internet","cannot browse","cannot retrieve","cannot look up"]):
                    live = await ctx.bot.send_message(chat_id=int(cid), text="🔍 _Searching the web…_", parse_mode="Markdown")
                    results = await asyncio.get_running_loop().run_in_executor(_exec, lambda: _search_web_sync(msg, max_results=5))
                    try: await live.delete()
                    except Exception: pass
                    ans2 = _strip_think(await invoke("orion", history + [{"role":"user","content":
                        f"[Web search results for: {msg}]\n\n{results}\n\nAnswer: {msg}"}]))
                    _conv_add(cid, "user", msg); _conv_add(cid, "assistant", ans2)
                    await send(ctx, cid, ans2)
                elif _is_about_orion(msg):
                    _conv_add(cid, "user", msg); _conv_add(cid, "assistant", _ORION_ID)
                    await send(ctx, cid, _ORION_ID)
                else:
                    _conv_add(cid, "user", msg); _conv_add(cid, "assistant", ans)
                    await send(ctx, cid, f"{ans}\n\n🔬 _I can research and add this capability myself. Want me to?_",
                        mkb(("✅ Yes, learn it","self_upgrade_yes"),("❌ No thanks","self_upgrade_no")))
                    ctx.user_data["pending_upgrade_msg"] = msg
            else:
                _conv_add(cid, "user", msg); _conv_add(cid, "assistant", ans)
                await send(ctx, cid, ans)
    finally:
        stop.set()

# ── Button/callback handler ────────────────────────────────────────────────────
async def on_btn(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    cid = str(q.message.chat.id); data = q.data
    try: await q.edit_message_reply_markup(None)
    except Exception: pass

    if data == "approve_proposal":
        update_project(cid, status="development")
        await send(ctx, cid, "✅ *Proposal approved!* Activating Leo…")
        await workflow(ctx, cid)
    elif data == "revise_proposal":
        update_project(cid, status="idle")
        await send(ctx, cid, "🔄 Send revision notes — Ada & Mira will redo the proposal.")
    elif data == "reject_proposal":
        update_project(cid, status="idle")
        await send(ctx, cid, "❌ Idea rejected. Send a new one whenever you're ready.")
    elif data == "force_qa":
        update_project(cid, status="qa_running")
        await send(ctx, cid, "Moving to QA…"); await workflow(ctx, cid)
    elif data == "instruct_leo":
        update_project(cid, status="development")
        await send(ctx, cid, "Send instructions for Leo and I'll pass them along.")
    elif data == "fix_bugs":
        update_project(cid, status="development")
        await send(ctx, cid, "🛠️ Reactivating Leo to fix Nova's bugs…")
        await workflow(ctx, cid)
    elif data == "accept_bugs":
        update_project(cid, status="final_review")
        await send(ctx, cid, "⚠️ Proceeding to final review with known bugs.")
        await workflow(ctx, cid)
    elif data == "accept_final":
        update_project(cid, status="completed", completed_at=str(datetime.datetime.now()))
        await send(ctx, cid, "🎉 *Project complete and signed off!*\nSend a new idea anytime.")
    elif data == "more_changes":
        update_project(cid, status="qa_running")
        await send(ctx, cid, "🔄 Nova will add more test cases and re-run QA.")
        await workflow(ctx, cid)
    elif data == "cipher_yes":
        pending = ctx.user_data.pop("pending_pentest", "")
        if not pending: await send(ctx, cid, "No pending pentest."); return
        await send(ctx, cid, "🛡️ *Cipher running pentest…* _(may take minutes)_")
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            result = await invoke("cipher",[{"role":"user","content":pending}])
        finally: stop.set()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        rpath = os.path.join(_ws("reports"), f"pentest_{ts}.md")
        with open(rpath, "w") as f:
            f.write(f"# Pentest Report\n_Date: {ts}_\n_Task: {pending}_\n\n{result}\n")
        await send(ctx, cid, f"🛡️ *Cipher's Report:*\n\n{result[:1200]}")
        await deliver_document(ctx, cid, rpath,
            subject=f"Pentest Report: {pending[:50]}",
            body=f"Cipher's security report attached.\n\nTask: {pending}\n\n— Cipher",
            telegram_caption=f"pentest_{ts}.md", as_pdf_email=False)
    elif data == "cipher_no":
        ctx.user_data.pop("pending_pentest", None)
        await send(ctx, cid, "🛡️ Pentest cancelled.")
    elif data == "tool_yes":
        pending = ctx.user_data.pop("pending_tool", {})
        call = pending.get("call"); desc = pending.get("desc","action")
        if call: await react_tool(ctx, cid, desc, call)
        else: await send(ctx, cid, "⚠️ Nothing to run — try again.")
    elif data == "tool_no":
        ctx.user_data.pop("pending_tool", None)
        await send(ctx, cid, "❌ Cancelled.")
    elif data == "shell_yes":
        cmd = ctx.user_data.pop("pending_shell", "")
        if not cmd: await send(ctx, cid, "No command found."); return
        await react_tool(ctx, cid, f"shell: {cmd}", lambda c=cmd: _run(c, timeout=60))
    elif data == "shell_no":
        ctx.user_data.pop("pending_shell", None)
        await send(ctx, cid, "❌ Command cancelled.")
    elif data == "start_from_trend":
        await send(ctx, cid, "Which idea do you want to build? Just describe it.")
    elif data == "dismiss_trend":
        await send(ctx, cid, "👍 No problem. Send an idea whenever you're ready!")
    elif data == "plugin_yes":
        p = ctx.user_data.pop("pending_plugin", {})
        if not p: await send(ctx, cid, "Nothing pending."); return
        loop = asyncio.get_running_loop()
        live = await ctx.bot.send_message(chat_id=int(cid), text="📦 _Installing…_", parse_mode="Markdown")
        async def upd(t):
            try: await live.edit_text(t, parse_mode="Markdown")
            except Exception: pass
        pkg = p.get("package","")
        if pkg and pkg.lower() not in ("stdlib","none",""):
            await upd(f"📦 _Installing `{pkg}`…_")
            ok, out = await loop.run_in_executor(_exec,
                lambda: _run(f"uv pip install --python {HOME}/.venv/bin/python {pkg}"))
            if not ok:
                await live.delete()
                await send(ctx, cid, f"❌ Package install failed:\n```\n{out[:400]}\n```"); return
        slug = re.sub(r'[^\w]','_',p.get("capability","plugin").lower())[:35]
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        with open(os.path.join(PLUGINS_DIR, f"{slug}.py"), "w") as pf:
            pf.write(f"# Plugin: {p.get('capability')}\n# Installed: {datetime.datetime.now()}\n\n{p['code']}\n")
        await upd("💾 _Saved plugin, restarting…_")
        await asyncio.sleep(1); await live.delete()
        await self_restart(ctx, cid, f"added {p.get('fn_name','plugin')}")
    elif data == "plugin_no":
        ctx.user_data.pop("pending_plugin", None)
        await send(ctx, cid, "❌ Plugin install cancelled.")
    elif data == "self_upgrade_yes":
        msg_orig = ctx.user_data.pop("pending_upgrade_msg", "")
        if msg_orig: await orion_self_upgrade(ctx, cid, msg_orig)
        else: await send(ctx, cid, "Use `/upgrade <skill>` to teach me a capability.")
    elif data == "self_upgrade_no":
        ctx.user_data.pop("pending_upgrade_msg", None)
        await send(ctx, cid, "No problem. Use `/upgrade <capability>` anytime.")
    else:
        await send(ctx, cid, f"Unknown action: {data}")

# ── ReAct tool runner (visible thinking + web-search retry) ────────────────────
async def react_tool(ctx, cid, desc, call_fn):
    loop = asyncio.get_running_loop()
    live = await ctx.bot.send_message(chat_id=int(cid), text=f"💭 _Planning: {desc}_", parse_mode="Markdown")
    async def upd(text):
        try: await live.edit_text(text, parse_mode="Markdown")
        except Exception: pass
    def _looks_failed(r):
        if isinstance(r, tuple):
            return r[0]=="text" and any(w in (r[1] or "").lower()
                for w in ["❌","error","failed","could not","not found","unable"])
        return isinstance(r, str) and any(w in r.lower()
            for w in ["❌","error","failed","could not","not found"])
    await upd(f"⚙️ _Executing: {desc}_")
    result = await loop.run_in_executor(_exec, call_fn)
    if not _looks_failed(result):
        try: await live.delete()
        except Exception: pass
        await send_result(ctx, cid, result); return
    err = result[1] if isinstance(result, tuple) else str(result)
    await upd(f"❌ _Failed:_ `{err[:100]}`\n\n🔍 _Searching web for a fix…_")
    search_res = await loop.run_in_executor(_exec,
        lambda: _search_web_sync(f"macOS {desc} {err[:120]} fix solution"))
    await upd("💭 _Analysing results, deriving fix…_")
    fix_cmd = await invoke("orion", [{"role":"user","content":
        f"Task: {desc}\nError: {err[:400]}\nWeb results:\n{search_res[:1200]}\n\n"
        "Give ONLY the single shell command to fix this on macOS — no explanation."}])
    fix_cmd = fix_cmd.strip().strip('`').split('\n')[0].strip()
    await upd(f"🔄 _Retrying with fix:_\n`{fix_cmd}`")
    ok2, out2 = await loop.run_in_executor(_exec, lambda: _run(fix_cmd, timeout=30))
    try: await live.delete()
    except Exception: pass
    if ok2:
        await send(ctx, cid, f"✅ *Fixed!*\n\n*Error:* `{err[:120]}`\n*Solution:* `{fix_cmd}`"
            + (f"\n\n```\n{out2[:400]}\n```" if out2.strip() else ""))
    else:
        await send(ctx, cid, f"❌ *Still failing after retry.*\n\nTried: `{fix_cmd}`\n"
            f"Error: `{out2[:300]}`\n\nTry `/run {fix_cmd}` manually.")

async def on_err(u, ctx): logger.error(f"Error: {ctx.error}", exc_info=True)

# ── Dashboard action poller ────────────────────────────────────────────────────
class _DashCtx:
    def __init__(self, application):
        self.bot = application.bot
        self.application = application
        self.user_data = {}

async def action_poller():
    global _app
    while True:
        await asyncio.sleep(3)
        try:
            if not os.path.exists(TRIGGER_FILE): continue
            with open(TRIGGER_FILE) as f: triggers = json.load(f)
            if not triggers: continue
            with open(TRIGGER_FILE, "w") as f: json.dump({}, f)
            for cid, data in triggers.items():
                logger.info(f"Dashboard action for {cid}: {data.get('action')}")
                await workflow(_DashCtx(_app), cid)
        except Exception as e:
            logger.error(f"action_poller: {e}")

async def post_init(application):
    global _app
    _app = application
    n = _load_plugins()
    logger.info(f"Loaded {n} plugin(s) from {PLUGINS_DIR}")
    asyncio.create_task(action_poller())
    logger.info("Dashboard action poller started.")

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN","")
    if not token: sys.exit("TELEGRAM_BOT_TOKEN not set in .env")
    app = Application.builder().token(token).post_init(post_init).build()
    for cmd, fn in [("start",cmd_start),("status",cmd_status),("projects",cmd_projects),
                    ("trends",cmd_trends),("pause",cmd_pause),("resume",cmd_resume),
                    ("help",cmd_help),("run",cmd_run),("screenshot",cmd_screenshot),
                    ("files",cmd_files),("upgrade",cmd_upgrade),("clear",cmd_clear)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_handler(CallbackQueryHandler(on_btn))
    app.add_error_handler(on_err)
    logger.info("Orion online.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
ORCHEOF
    chmod +x "$AD/orchestrator.py"; ok "orchestrator.py written (enhanced)."

    # ── trend_watcher.py — Vox twice-daily money-first ideas ──────────────────
    cat > "$AD/trend_watcher.py" <<'TRENDEOF'
#!/usr/bin/env python3
"""Vox — Twice-Daily Opportunity Scout. Launched by launchd AM + PM."""
import os, sys, requests, yaml
from openai import OpenAI
from dotenv import load_dotenv

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))
TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")
GW_PORT = os.environ.get("PORT_GATEWAY","4000")
SX_PORT = os.environ.get("PORT_SEARXNG","8888")

if not TOKEN or not CHAT_ID:
    sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

with open(os.path.join(HOME, "agents", "team.yaml")) as f:
    VOX_SYS = yaml.safe_load(f)["roles"]["vox"]["system_prompt"]

client = OpenAI(base_url=f"http://localhost:{GW_PORT}/v1", api_key="local")

def get_snippets():
    try:
        r = requests.get(f"http://localhost:{SX_PORT}/search",
            params={"q":"news today business technology agriculture finance startup opportunity",
                    "format":"json"}, timeout=10)
        return "\n".join(f"- {x.get('title','')}: {x.get('content','')[:200]}"
                         for x in r.json().get("results",[])[:8])
    except Exception as e: return f"Search unavailable: {e}"

def main():
    snippets = get_snippets()
    try:
        resp = client.chat.completions.create(
            model="vox",
            messages=[{"role":"system","content":VOX_SYS},
                      {"role":"user","content":
                       f"Today's news across categories:\n{snippets}\n\n"
                       "Propose 3 fresh MONEY-MAKING ideas. Lead with the money angle and "
                       "business model (who pays, why, how much, how to start). Per idea: "
                       "bold title, opportunity, business model, why now (cite source), what "
                       "to do, tools, complexity, first step this week. Add a short marketing note."}],
            temperature=0.9, max_tokens=900)
        text = resp.choices[0].message.content.strip()
    except Exception as e: text = f"Idea generation failed: {e}"

    msg = f"📡 *Vox's Money Ideas:*\n\n{text}"
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=15)
        print("Vox ideas sent.")
    except Exception as e: print(f"Send failed: {e}", file=sys.stderr)

if __name__ == "__main__": main()
TRENDEOF
    chmod +x "$AD/trend_watcher.py"; ok "trend_watcher.py written (twice-daily)."
}

# =============================================================================
#  PHASE 7 — LIVE DASHBOARD (Jira-style, delete + clean titles fixed)
# =============================================================================
write_dashboard() {
    log "Live dashboard (:$PORT_DASHBOARD)"
    local DD="$WORKDIR/dashboard"; mkdir -p "$DD"
    cat > "$DD/app.py" <<'DASHEOF'
#!/usr/bin/env python3
"""AI Team Mission Control Dashboard — Jira-style. Fixed: delete reliability, clean titles."""
import os, json, datetime, subprocess, re
import requests, psutil
from flask import Flask, jsonify, request
from dotenv import load_dotenv

HOME      = os.environ.get("AI_HOME",      os.path.expanduser("~/ai-workstation"))
WORKSPACE = os.environ.get("AI_WORKSPACE",
    os.path.join(os.path.expanduser("~"), "Library/CloudStorage/OneDrive-Personal/AI-Agent"))
load_dotenv(os.path.join(HOME, ".env"))

PF = os.path.join(HOME, "projects.json")
SF = os.path.join(HOME, "agent_status.json")
TF = os.path.join(HOME, "pending_actions.json")
LF_URL = os.environ.get("LANGFUSE_HOST","http://localhost:3000")
LF_PK  = os.environ.get("LANGFUSE_PUBLIC_KEY","")
LF_SK  = os.environ.get("LANGFUSE_SECRET_KEY","")
P_OLLAMA=os.environ.get("PORT_OLLAMA","11434"); P_GATEWAY=os.environ.get("PORT_GATEWAY","4000")
P_OPENWEBUI=os.environ.get("PORT_OPENWEBUI","3001"); P_SEARXNG=os.environ.get("PORT_SEARXNG","8888")
P_LANGFUSE=os.environ.get("PORT_LANGFUSE","3000"); P_DASHBOARD=os.environ.get("PORT_DASHBOARD","8800")
P_PORTAINER=os.environ.get("PORT_PORTAINER","9001")

app = Flask(__name__)

SERVICES = [
    ("Ollama",f"http://localhost:{P_OLLAMA}/api/tags",P_OLLAMA,"Local model server"),
    ("LiteLLM Gateway",f"http://localhost:{P_GATEWAY}/health/liveliness",P_GATEWAY,"Model routing"),
    ("Open WebUI",f"http://localhost:{P_OPENWEBUI}/",P_OPENWEBUI,"Chat UI"),
    ("SearXNG",f"http://localhost:{P_SEARXNG}/",P_SEARXNG,"Private web search"),
    ("Langfuse",f"http://localhost:{P_LANGFUSE}/api/public/health",P_LANGFUSE,"Agent traces"),
    ("Portainer",f"http://localhost:{P_PORTAINER}/",P_PORTAINER,"Docker management"),
    ("Dashboard",f"http://localhost:{P_DASHBOARD}/",P_DASHBOARD,"This page"),
]
AGENTS = [
    ("orion","🤖","Chief of Staff","qwen3.6:35b-a3b","Orchestrator, advisor, self-upgrading."),
    ("ada","📊","PM / Product Owner","qwen2.5:72b","Proposals (PDF+email), planning, sign-off."),
    ("mira","🎨","Senior UI/UX","gemma4:26b","Wireframes: image + editable draw.io."),
    ("leo","💻","Super-Senior Dev","qwen2.5-coder:72b","Ships code, self-tests, any stack."),
    ("nova","🔎","QA Tester","qwen2.5:72b","End-to-end tests with Puppeteer."),
    ("cipher","🛡️","White-Hat Pentester","qwen2.5:72b","On-demand, authorization required."),
    ("vox","📡","Opportunity Scout","qwen2.5:72b","Twice-daily money ideas & marketing."),
]
KANBAN = [
    ("proposal_drafting","📋 Proposal"),("awaiting_approval","⏳ Pending Approval"),
    ("development","💻 Development"),("qa_running","🔎 QA"),
    ("qa_bugs_found","🐛 Bugs Found"),("final_review","📊 Final Review"),
    ("awaiting_final","⏳ Final Approval"),("completed","✅ Done"),
]
STATE_LABELS = {
    "idle":("No project","#6b7280"),"proposal_drafting":("Drafting proposal","#f59e0b"),
    "awaiting_approval":("Awaiting approval","#3b82f6"),"development":("In development","#f59e0b"),
    "qa_running":("QA running","#f59e0b"),"qa_bugs_found":("Bugs found","#ef4444"),
    "final_review":("Final review","#f59e0b"),"awaiting_final":("Awaiting final approval","#3b82f6"),
    "completed":("Completed ✅","#22c55e"),"paused":("Paused","#8b5cf6"),
}
STATE_ACTIONS = {
    "awaiting_approval":[("✅ Approve — start development","approve_proposal","green"),
        ("🔄 Request changes","revise_proposal","yellow"),("❌ Reject idea","reject_proposal","red")],
    "development":[("✅ Mark deployed → QA","force_qa","green")],
    "qa_bugs_found":[("🛠️ Fix bugs — reactivate Leo","fix_bugs","yellow"),
        ("⚠️ Accept as-is","accept_bugs","orange")],
    "awaiting_final":[("🎉 Accept & complete project","accept_final","green"),
        ("🔄 More changes needed","more_changes","yellow")],
}
ACTION_STATUS = {
    "approve_proposal":"development","revise_proposal":"idle","reject_proposal":"idle",
    "force_qa":"qa_running","fix_bugs":"development","accept_bugs":"final_review",
    "accept_final":"completed","more_changes":"qa_running",
}
MODEL_META = [
    ("qwen3.6:35b-a3b","Orchestration","Orion. MoE orchestrator (~26 GB)."),
    ("qwen2.5-coder:72b","Coding","Leo. 72B coding specialist (~44 GB)."),
    ("qwen2.5:72b","Reasoning","Ada+Nova+Vox+Cipher. 72B reasoning (~44 GB)."),
    ("qwen2.5","Reasoning","Strong reasoning and writing."),
    ("qwen3.6:27b","Coding","Leo Manual for VS Code + Continue (~22 GB)."),
    ("qwen3.6","Coding","Dense coder for Continue."),
    ("gemma4:26b","Vision/Design","Mira. Multimodal — images & wireframes (~18 GB)."),
    ("gemma4","Vision/Design","Mira multimodal."),
    ("nomic-embed-text","Embeddings","RAG / semantic search (~270 MB)."),
]
GROUP_ORDER = {"Orchestration":0,"Coding":1,"Reasoning":2,"Vision/Design":3,"Embeddings":4,"Other":5}
PROJECT_PORTS = {11434,4000,8800,3001,8888,3000,9001}
WELL_KNOWN_PORTS = {22:"SSH",53:"DNS",80:"HTTP",443:"HTTPS",548:"AFP",631:"CUPS",
    3306:"MySQL",5000:"Flask/AirPlay",5432:"PostgreSQL",5900:"VNC",6379:"Redis",
    7000:"AirPlay/Plex",8080:"HTTP Alt",8443:"HTTPS Alt",9090:"Prometheus",27017:"MongoDB"}

def probe(url):
    try: return requests.get(url,timeout=3).status_code < 500
    except: return False
def fmt_gb(gb): return f"{gb/1000:.1f} TB" if gb>=1000 else f"{gb:.0f} GB"
def hardware_info():
    vm = psutil.virtual_memory(); disk = None
    for p in ("/System/Volumes/Data",os.path.expanduser("~"),"/"):
        try: disk=psutil.disk_usage(p); break
        except: continue
    bat=None
    try:
        b=psutil.sensors_battery()
        if b: bat={"percent":round(b.percent),"charging":bool(b.power_plugged)}
    except: pass
    return {"cpu":{"pct":round(psutil.cpu_percent(interval=None))},
        "ram":{"pct":round(vm.percent),"detail":f"{fmt_gb(vm.used/1e9)} / {fmt_gb(vm.total/1e9)}"},
        "storage":{"pct":round(disk.percent) if disk else 0,
                   "detail":f"{fmt_gb(disk.used/1e9)} / {fmt_gb(disk.total/1e9)}" if disk else "?"},
        "battery":bat}
def get_models():
    try:
        r=requests.get(f"http://localhost:{P_OLLAMA}/api/tags",timeout=4); out=[]
        for m in r.json().get("models",[]):
            name=m.get("name",""); size=m.get("size",0); group,desc="Other","General model."
            for prefix,g,d in MODEL_META:
                if name==prefix or name.startswith(prefix.split(":")[0]+":") \
                        or (":" not in prefix and name.startswith(prefix)):
                    group,desc=g,d; break
            out.append({"name":name,"size":f"{size/1e9:.1f} GB" if size else "?","group":group,"desc":desc})
        return sorted(out,key=lambda x:(GROUP_ORDER.get(x["group"],5),x["name"]))
    except: return []
def scan_ports():
    try:
        r=subprocess.run(["lsof","-iTCP","-sTCP:LISTEN","-n","-P"],capture_output=True,text=True,timeout=10)
        seen=set(); extra=[]
        for line in r.stdout.splitlines()[1:]:
            parts=line.split()
            if len(parts)<9: continue
            m=re.search(r":(\d+)$",parts[8])
            if not m: continue
            port=int(m.group(1))
            if port in seen or port in PROJECT_PORTS: continue
            seen.add(port)
            extra.append({"port":port,"service":WELL_KNOWN_PORTS.get(port,f"Unknown — {parts[0]}"),"process":parts[0]})
        return sorted(extra,key=lambda x:x["port"])
    except: return []
def load_json(path,default):
    try:
        if os.path.exists(path):
            with open(path) as f: return json.load(f)
    except: pass
    return default
def get_traces():
    if not (LF_PK and LF_SK): return []
    try:
        r=requests.get(f"{LF_URL}/api/public/traces",params={"limit":20},auth=(LF_PK,LF_SK),timeout=4)
        return [{"name":t.get("name") or "(trace)","time":(t.get("timestamp") or "")[:19].replace("T"," "),
                 "latency":round(t.get("latency") or 0,2)} for t in r.json().get("data",[])]
    except: return []
def safe_name(text,max_len=45):
    n=re.sub(r'[^\w\s-]','',str(text).lower()); n=re.sub(r'\s+','_',n.strip())
    return n[:max_len] or "untitled"
def list_project_files(idea):
    pname=safe_name(idea); files=[]
    for folder,cat in [(os.path.join(WORKSPACE,"proposals",pname),"Proposals & Design"),
                       (os.path.join(WORKSPACE,"projects",pname),"Code & Reports")]:
        if os.path.exists(folder):
            for fname in sorted(os.listdir(folder)):
                fpath=os.path.join(folder,fname)
                if os.path.isfile(fpath):
                    st=os.stat(fpath)
                    files.append({"name":fname,"category":cat,"path":fpath,"size":st.st_size,
                        "modified":datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return files
def enrich_project(cid,proj):
    st=proj.get("status","idle"); label,color=STATE_LABELS.get(st,(st,"#6b7280"))
    proj["state_label"]=label; proj["state_color"]=color
    proj["actions"]=STATE_ACTIONS.get(st,[]); proj["cid"]=cid
    return proj

@app.route("/api/status")
def api_status():
    svcs=[{"name":n,"url":f"http://localhost:{p}/","port":int(p),"purpose":pu,"ok":probe(h)}
          for n,h,p,pu in SERVICES]
    ag_raw=load_json(SF,{})
    agents=[{"id":aid,"icon":ic,"role":role,"model":model,"desc":desc,"status":ag_raw.get(aid,"idle")}
            for aid,ic,role,model,desc in AGENTS]
    projects=load_json(PF,{})
    for cid,p in projects.items(): enrich_project(cid,p)
    return jsonify({"services":svcs,"agents":agents,"projects":projects,
        "hardware":hardware_info(),"models":get_models(),"extra_ports":scan_ports(),
        "traces":get_traces(),"kanban":[{"status":s,"label":l} for s,l in KANBAN],
        "updated":datetime.datetime.now().strftime("%H:%M:%S")})

@app.route("/api/project/<cid>", methods=["DELETE"])
def api_delete_project(cid):
    import shutil
    projects=load_json(PF,{})
    if cid not in projects: return jsonify({"error":"Not found"}),404
    proj=projects.pop(cid)
    with open(PF,"w") as f: json.dump(projects,f,indent=2,default=str)
    os.chmod(PF,0o600)
    deleted=[]; idea=proj.get("idea","")
    if idea:
        pname=safe_name(idea)
        for folder in [os.path.join(WORKSPACE,"proposals",pname),
                       os.path.join(WORKSPACE,"projects",pname)]:
            if os.path.exists(folder):
                shutil.rmtree(folder); deleted.append(folder)
    return jsonify({"ok":True,"deleted":deleted})

@app.route("/api/project/<cid>")
def api_project_detail(cid):
    projects=load_json(PF,{}); proj=projects.get(cid,{})
    if not proj: return jsonify({"error":"Not found"}),404
    enrich_project(cid,proj); proj["files"]=list_project_files(proj.get("idea",""))
    return jsonify(proj)

@app.route("/api/project/<cid>/action",methods=["POST"])
def api_project_action(cid):
    data=request.get_json(silent=True) or {}; action=data.get("action","")
    new_status=ACTION_STATUS.get(action)
    if not new_status: return jsonify({"error":f"Unknown action: {action}"}),400
    projects=load_json(PF,{})
    if cid not in projects: return jsonify({"error":"Project not found"}),404
    proj=projects[cid]; proj["status"]=new_status
    if action=="accept_final": proj["completed_at"]=str(datetime.datetime.now())
    proj.setdefault("events",[]).append({"time":str(datetime.datetime.now())[:19],
        "source":"dashboard","action":action,"new_status":new_status})
    projects[cid]=proj
    with open(PF,"w") as f: json.dump(projects,f,indent=2,default=str)
    os.chmod(PF,0o600)
    try:
        triggers=load_json(TF,{}); triggers[cid]={"action":action,"time":str(datetime.datetime.now())}
        with open(TF,"w") as f: json.dump(triggers,f)
    except Exception as e: app.logger.warning(f"Could not write trigger: {e}")
    return jsonify({"ok":True,"new_status":new_status})

@app.route("/api/file")
def api_file():
    path=request.args.get("path","")
    if not path.startswith(WORKSPACE): return jsonify({"error":"Access denied"}),403
    if not os.path.isfile(path): return jsonify({"error":"File not found"}),404
    try:
        with open(path,errors="replace") as f: content=f.read(100000)
        return jsonify({"content":content,"name":os.path.basename(path)})
    except Exception as e: return jsonify({"error":str(e)}),500

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Team — Mission Control</title>
<style>
  :root{--bg:#0a0e14;--panel:#131820;--panel2:#1a212c;--border:#252e3a;
    --text:#e6edf3;--dim:#8b98a5;--accent:#3b82f6;--green:#22c55e;--yellow:#f59e0b;
    --red:#ef4444;--purple:#8b5cf6;--orange:#fb923c;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
  .wrap{max-width:1400px;margin:0 auto;padding:20px}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:10px}
  h1{font-size:20px;font-weight:700}h1 .dot{color:var(--green)}
  .updated{color:var(--dim);font-size:12px}
  .grid{display:grid;gap:16px}
  .cards{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
  .card h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);margin-bottom:12px}
  .hw{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .gauge{text-align:center}
  .ring{--p:0;width:80px;height:80px;border-radius:50%;margin:0 auto 6px;
    background:conic-gradient(var(--accent) calc(var(--p)*1%),var(--panel2) 0);
    display:flex;align-items:center;justify-content:center;position:relative}
  .ring::before{content:"";position:absolute;inset:8px;border-radius:50%;background:var(--panel)}
  .ring span{position:relative;font-weight:700;font-size:15px}
  .gauge .lbl{font-size:12px;color:var(--dim)}.gauge .det{font-size:11px;color:var(--dim)}
  .svc{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)}
  .svc:last-child{border:none}.svc .led{width:9px;height:9px;border-radius:50%}
  .led.on{background:var(--green);box-shadow:0 0 7px var(--green)}.led.off{background:var(--red)}
  .svc a{color:var(--text);text-decoration:none;font-weight:600}.svc a:hover{color:var(--accent)}
  .svc .pp{color:var(--dim);font-size:11px;margin-left:auto}
  .agent{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)}
  .agent:last-child{border:none}.agent .ic{font-size:22px}
  .agent .role{font-weight:600}.agent .mdl{font-size:11px;color:var(--accent);font-family:monospace}
  .agent .ds{font-size:11px;color:var(--dim)}
  .agent .st{margin-left:auto;font-size:10px;padding:3px 8px;border-radius:10px;height:fit-content}
  .st.idle{background:#1f2730;color:var(--dim)}
  .st.working{background:rgba(245,158,11,.18);color:var(--yellow);animation:pulse 1.5s infinite}
  @keyframes pulse{50%{opacity:.5}}
  .kanban{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(180px,1fr);gap:10px;overflow-x:auto;padding-bottom:8px}
  .col{background:var(--panel2);border-radius:10px;padding:10px;min-height:90px}
  .col h3{font-size:11px;color:var(--dim);margin-bottom:8px;text-transform:uppercase}
  .tk{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--accent);
    border-radius:7px;padding:9px;margin-bottom:7px;cursor:pointer;transition:.15s}
  .tk:hover{border-color:var(--accent);transform:translateY(-1px)}
  .tk .ti{font-weight:600;font-size:12px;margin-bottom:3px}.tk .meta{font-size:10px;color:var(--dim)}
  .pill{display:inline-block;padding:2px 7px;border-radius:8px;font-size:10px;font-weight:600}
  .mdl-card{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}
  .mdl-card:last-child{border:none}.mdl-card .nm{font-family:monospace;font-size:12px;color:var(--accent)}
  .mdl-card .ds{font-size:11px;color:var(--dim)}.mdl-card .sz{font-size:11px;color:var(--dim)}
  .grp{font-size:10px;text-transform:uppercase;color:var(--purple);margin:10px 0 4px;font-weight:700}
  .trace{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
  .trace:last-child{border:none}.trace .lt{color:var(--dim);font-family:monospace}
  .btn{padding:8px 12px;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:12px;color:#fff}
  .btn.green{background:var(--green)}.btn.yellow{background:var(--yellow)}.btn.red{background:var(--red)}
  .btn.orange{background:var(--orange)}.btn:hover{opacity:.9}.btn:active{transform:scale(.97)}
  .proj{background:var(--panel2);border-radius:10px;padding:14px;margin-bottom:12px}
  .proj .pt{font-weight:700;font-size:15px;margin-bottom:4px}
  .proj .pmeta{font-size:11px;color:var(--dim);margin-bottom:8px}
  .badge{display:inline-block;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:600;color:#fff}
  .acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;
    justify-content:center;z-index:100;padding:20px}
  .modal.show{display:flex}
  .modal-box{background:var(--panel);border:1px solid var(--border);border-radius:14px;
    max-width:760px;width:100%;max-height:85vh;overflow-y:auto;padding:22px}
  .modal-box h2{margin-bottom:14px;font-size:17px}
  .file-item{display:flex;justify-content:space-between;align-items:center;padding:10px;
    background:var(--panel2);border-radius:8px;margin-bottom:7px;cursor:pointer}
  .file-item:hover{background:#222b38}.file-item .fn{font-weight:600;font-size:13px}
  .file-item .fmeta{font-size:10px;color:var(--dim)}
  .file-cat{font-size:10px;text-transform:uppercase;color:var(--purple);margin:12px 0 5px;font-weight:700}
  pre{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:14px;
    overflow:auto;font-size:12px;max-height:55vh;white-space:pre-wrap;word-break:break-word}
  .close{float:right;cursor:pointer;color:var(--dim);font-size:22px;line-height:1}
  .close:hover{color:var(--text)}
  .del{background:none;border:1px solid var(--red);color:var(--red);padding:4px 10px;
    border-radius:7px;font-size:11px;cursor:pointer;float:right}.del:hover{background:var(--red);color:#fff}
  .empty{color:var(--dim);text-align:center;padding:20px;font-size:13px}
  .full{grid-column:1/-1}
  .port-item{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
  .port-item:last-child{border:none}.port-item .pn{font-family:monospace;color:var(--yellow)}
</style></head><body>
<div class="wrap">
  <header><h1><span class="dot">●</span> AI Team — Mission Control</h1>
    <div class="updated">Updated <span id="upd">—</span> · auto-refresh 5s</div></header>
  <div class="grid cards" style="margin-bottom:16px">
    <div class="card"><h2>Hardware</h2><div class="hw" id="hw"></div></div>
    <div class="card"><h2>Services</h2><div id="svcs"></div></div>
  </div>
  <div class="card full" style="margin-bottom:16px"><h2>Projects</h2><div id="projects"></div></div>
  <div class="card full" style="margin-bottom:16px"><h2>Workflow Board</h2><div class="kanban" id="kanban"></div></div>
  <div class="grid cards">
    <div class="card"><h2>The Team</h2><div id="agents"></div></div>
    <div class="card"><h2>Models Installed</h2><div id="models"></div></div>
    <div class="card"><h2>Other Ports In Use</h2><div id="ports"></div></div>
    <div class="card"><h2>Recent Agent Traces</h2><div id="traces"></div></div>
  </div>
</div>
<div class="modal" id="modal"><div class="modal-box" id="modal-content"></div></div>
<script>
let STATE={};
const PRIO={critical:"var(--red)",high:"var(--orange)",medium:"var(--yellow)",low:"var(--dim)"};
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

async function load(){
  try{
    const r=await fetch("/api/status");const d=await r.json();STATE=d;
    document.getElementById("upd").textContent=d.updated;
    renderHW(d.hardware);renderSvcs(d.services);renderAgents(d.agents);
    renderModels(d.models);renderPorts(d.extra_ports);renderTraces(d.traces);
    renderProjects(d.projects);renderKanban(d.projects,d.kanban);
  }catch(e){console.error("load failed",e);}
}
function ring(pct,label,detail){
  return `<div class="gauge"><div class="ring" style="--p:${pct}"><span>${pct}%</span></div>
    <div class="lbl">${label}</div><div class="det">${detail||""}</div></div>`;}
function renderHW(h){
  let html=ring(h.cpu.pct,"CPU","")+ring(h.ram.pct,"RAM",h.ram.detail)+
    ring(h.storage.pct,"Storage",h.storage.detail);
  if(h.battery)html+=ring(h.battery.percent,"Battery",h.battery.charging?"⚡ charging":"on battery");
  document.getElementById("hw").innerHTML=html;}
function renderSvcs(s){
  document.getElementById("svcs").innerHTML=s.map(x=>`<div class="svc">
    <span class="led ${x.ok?'on':'off'}"></span>
    <a href="${esc(x.url)}" target="_blank">${esc(x.name)}</a>
    <span class="pp">${esc(x.purpose)} · :${x.port}</span></div>`).join("");}
function renderAgents(a){
  document.getElementById("agents").innerHTML=a.map(x=>`<div class="agent">
    <div class="ic">${x.icon}</div><div>
    <div class="role">${esc(x.role)}</div><div class="mdl">${esc(x.model)}</div>
    <div class="ds">${esc(x.desc)}</div></div>
    <span class="st ${x.status==='working'?'working':'idle'}">${x.status==='working'?'working':'idle'}</span>
    </div>`).join("");}
function renderModels(m){
  if(!m.length){document.getElementById("models").innerHTML='<div class="empty">No models pulled yet.</div>';return;}
  let html="",grp="";
  m.forEach(x=>{if(x.group!==grp){grp=x.group;html+=`<div class="grp">${esc(grp)}</div>`;}
    html+=`<div class="mdl-card"><div><div class="nm">${esc(x.name)}</div>
      <div class="ds">${esc(x.desc)}</div></div><div class="sz">${esc(x.size)}</div></div>`;});
  document.getElementById("models").innerHTML=html;}
function renderPorts(p){
  if(!p||!p.length){document.getElementById("ports").innerHTML='<div class="empty">No other ports.</div>';return;}
  document.getElementById("ports").innerHTML=p.map(x=>`<div class="port-item">
    <span class="pn">:${x.port}</span><span>${esc(x.service)}</span>
    <span style="color:var(--dim)">${esc(x.process)}</span></div>`).join("");}
function renderTraces(t){
  if(!t||!t.length){document.getElementById("traces").innerHTML=
    '<div class="empty">No traces yet (set Langfuse keys).</div>';return;}
  document.getElementById("traces").innerHTML=t.map(x=>`<div class="trace">
    <span>${esc(x.name)}</span><span class="lt">${esc(x.time)} · ${x.latency}s</span></div>`).join("");}
function renderProjects(projects){
  const el=document.getElementById("projects");const keys=Object.keys(projects||{});
  if(!keys.length){el.innerHTML='<div class="empty">No projects yet. Send your AI team an idea on Telegram!</div>';return;}
  el.innerHTML=keys.map(cid=>{const p=projects[cid];
    const acts=(p.actions||[]).map(a=>
      `<button class="btn ${a[2]}" data-cid="${esc(cid)}" data-action="${esc(a[1])}"
        onclick="doAction(this)">${esc(a[0])}</button>`).join("");
    return `<div class="proj">
      <button class="del" data-cid="${esc(cid)}" onclick="delProject(this)">Delete</button>
      <div class="pt">${esc(p.idea||"Untitled")}</div>
      <div class="pmeta">${esc((p.created||"").slice(0,16))} · ${esc(cid)}</div>
      <span class="badge" style="background:${p.state_color}">${esc(p.state_label)}</span>
      <div class="acts">${acts}
        <button class="btn" style="background:var(--panel2);border:1px solid var(--border)"
          data-cid="${esc(cid)}" onclick="viewFiles(this)">📁 Files</button></div>
    </div>`;}).join("");}
function renderKanban(projects,kanban){
  const byStatus={};(kanban||[]).forEach(k=>byStatus[k.status]=[]);
  Object.keys(projects||{}).forEach(cid=>{const p=projects[cid];
    if(byStatus[p.status])byStatus[p.status].push(p);});
  document.getElementById("kanban").innerHTML=(kanban||[]).map(k=>{
    const items=(byStatus[k.status]||[]).map(p=>`<div class="tk">
      <div class="ti">${esc(p.idea||"Untitled")}</div>
      <div class="meta">${esc((p.created||"").slice(0,10))}</div></div>`).join("")
      ||'<div style="color:var(--dim);font-size:11px">—</div>';
    return `<div class="col"><h3>${esc(k.label)}</h3>${items}</div>`;}).join("");}

async function doAction(btn){
  const cid=btn.getAttribute("data-cid");const action=btn.getAttribute("data-action");
  btn.disabled=true;btn.textContent="…";
  try{
    const r=await fetch(`/api/project/${cid}/action`,{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
    const d=await r.json();
    if(d.error)alert("Error: "+d.error);else load();
  }catch(e){alert("Action failed: "+e);btn.disabled=false;}
}
// DELETE FIX: title looked up from STATE, never interpolated into HTML/onclick.
async function delProject(btn){
  const cid=btn.getAttribute("data-cid");
  const proj=(STATE.projects||{})[cid]||{};
  const title=proj.idea||"this project";
  if(!confirm(`Delete "${title}"?\n\nThis removes its OneDrive proposal & project files too.`))return;
  btn.disabled=true;btn.textContent="…";
  try{
    const r=await fetch(`/api/project/${cid}`,{method:"DELETE"});
    const d=await r.json();
    if(d.error){alert("Error: "+d.error);btn.disabled=false;btn.textContent="Delete";}
    else load();
  }catch(e){alert("Delete failed: "+e);btn.disabled=false;btn.textContent="Delete";}
}
async function viewFiles(btn){
  const cid=btn.getAttribute("data-cid");
  try{
    const r=await fetch(`/api/project/${cid}`);const p=await r.json();
    const files=p.files||[];const cats={};
    files.forEach(f=>{(cats[f.category]=cats[f.category]||[]).push(f);});
    let html=`<span class="close" onclick="closeModal()">×</span>
      <h2>📁 ${esc(p.idea||"Project")} — Files</h2>`;
    if(!files.length)html+='<div class="empty">No files generated yet.</div>';
    else Object.keys(cats).forEach(cat=>{html+=`<div class="file-cat">${esc(cat)}</div>`;
      cats[cat].forEach(f=>{html+=`<div class="file-item" data-path="${esc(f.path)}" onclick="viewFile(this)">
        <div><div class="fn">${esc(f.name)}</div>
        <div class="fmeta">${esc(f.modified)}</div></div>
        <div class="fmeta">${(f.size/1024).toFixed(1)} KB</div></div>`;});});
    showModal(html);
  }catch(e){alert("Could not load files: "+e);}
}
async function viewFile(el){
  const path=el.getAttribute("data-path");
  try{
    const r=await fetch(`/api/file?path=${encodeURIComponent(path)}`);const d=await r.json();
    if(d.error){alert(d.error);return;}
    showModal(`<span class="close" onclick="closeModal()">×</span>
      <h2>${esc(d.name)}</h2><pre>${esc(d.content)}</pre>`);
  }catch(e){alert("Could not load file: "+e);}
}
function showModal(html){document.getElementById("modal-content").innerHTML=html;
  document.getElementById("modal").classList.add("show");}
function closeModal(){document.getElementById("modal").classList.remove("show");}
document.getElementById("modal").addEventListener("click",e=>{
  if(e.target.id==="modal")closeModal();});
load();setInterval(load,5000);
</script></body></html>"""

@app.route("/")
def index():
    from flask import Response
    return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT_DASHBOARD", "8800"))
    print(f"Dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
DASHEOF
    ok "dashboard/app.py written (delete + clean-title fixes)."
}

# =============================================================================
#  PHASE 8 — VS CODE + CONTINUE (manual IDE coding via /pause)
# =============================================================================
setup_continue() {
    log "Continue extension config (VS Code manual coding)"
    if have code; then
        code --install-extension continue.continue --force >/dev/null 2>&1 \
            && ok "Continue extension installed in VS Code." \
            || warn "Could not auto-install Continue — install it from the Extensions panel."
    else
        warn "VS Code 'code' command not found — skipping extension install."
        warn "Install VS Code + the 'code' command (Shell Command: Install 'code' in PATH)."
    fi
    local CD="$HOME/.continue"; mkdir -p "$CD"
    local CFG="$CD/config.json"
    if [ -f "$CFG" ]; then
        ok "Continue config exists — keeping yours."
    else
        cat > "$CFG" <<'CONTEOF'
{
  "models": [
    { "title": "Leo Manual (qwen3.6:27b)", "provider": "ollama",
      "model": "qwen3.6:27b", "apiBase": "http://localhost:11434" },
    { "title": "Leo 72B (heavy — pause agents first)", "provider": "ollama",
      "model": "qwen2.5-coder:72b", "apiBase": "http://localhost:11434" }
  ],
  "tabAutocompleteModel": {
    "title": "Autocomplete", "provider": "ollama",
    "model": "qwen3.6:27b", "apiBase": "http://localhost:11434"
  },
  "embeddingsProvider": {
    "provider": "ollama", "model": "nomic-embed-text",
    "apiBase": "http://localhost:11434"
  },
  "allowAnonymousTelemetry": false
}
CONTEOF
        ok "Continue config written (Leo Manual + autocomplete + embeddings)."
    fi
    warn "Manual IDE coding uses the 27B model. For the 72B model, send /pause first to free RAM."
}

# =============================================================================
#  PHASE 9 — BACKGROUND SERVICES (launchd)
# =============================================================================
write_plist() {
    # $1 label  $2 program-script  $3... extra <key>RunAtLoad</key> handled here
    local label="$1" script="$2"; shift 2
    local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$script</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WORKDIR</key><string>$WORKDIR</string>
    <key>AI_HOME</key><string>$WORKDIR</string>
    <key>AI_WORKSPACE</key><string>$AI_WORKSPACE</string>
    <key>MASTER_EMAIL</key><string>$MASTER_EMAIL</string>
    <key>SMTP_FROM</key><string>$(get_env SMTP_FROM)</string>
    <key>PORT_GATEWAY</key><string>$PORT_GATEWAY</string>
    <key>PORT_SEARXNG</key><string>$PORT_SEARXNG</string>
    <key>PORT_DASHBOARD</key><string>$PORT_DASHBOARD</string>
    <key>PORT_OLLAMA</key><string>$PORT_OLLAMA</string>
    <key>PORT_OPENWEBUI</key><string>$PORT_OPENWEBUI</string>
    <key>PORT_LANGFUSE</key><string>$PORT_LANGFUSE</string>
    <key>PORT_PORTAINER</key><string>$PORT_PORTAINER</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err</string>
</dict></plist>
PLISTEOF
    launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl load   "$plist" >/dev/null 2>&1 \
        && ok "service loaded: $label" || warn "Could not load $label"
}

write_vox_plist() {
    # Twice-daily Vox via StartCalendarInterval ARRAY (AM + PM).
    local label="com.aiws.vox"
    local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$WORKDIR/.venv/bin/python</string>
    <string>$WORKDIR/agents/trend_watcher.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AI_HOME</key><string>$WORKDIR</string>
    <key>AI_WORKSPACE</key><string>$AI_WORKSPACE</string>
    <key>MASTER_EMAIL</key><string>$MASTER_EMAIL</string>
    <key>SMTP_FROM</key><string>$(get_env SMTP_FROM)</string>
    <key>PORT_GATEWAY</key><string>$PORT_GATEWAY</string>
    <key>PORT_SEARXNG</key><string>$PORT_SEARXNG</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>$VOX_HOUR</integer><key>Minute</key><integer>$VOX_MINUTE</integer></dict>
    <dict><key>Hour</key><integer>$VOX_HOUR_PM</integer><key>Minute</key><integer>$VOX_MINUTE_PM</integer></dict>
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err</string>
</dict></plist>
PLISTEOF
    launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl load   "$plist" >/dev/null 2>&1 \
        && ok "service loaded: $label (twice daily — ${VOX_HOUR}:00 & ${VOX_HOUR_PM}:00)" \
        || warn "Could not load $label"
}

setup_services() {
    log "Background services (launchd)"
    write_plist "com.aiws.litellm"      "$WORKDIR/start_gateway.sh"
    write_plist "com.aiws.dashboard"    "$WORKDIR/start_dashboard.sh"
    write_plist "com.aiws.orchestrator" "$WORKDIR/start_orchestrator.sh"
    write_vox_plist
    ok "All services registered with launchd (auto-start on login)."
}

# =============================================================================
#  PHASE 10 — CREDENTIALS (Telegram + email via the Mac's Mail.app)
# =============================================================================
tut_mail() {
cat <<'TUTEOF'
####  EMAIL DELIVERY (via your Mac's Mail.app)  ####
  Ada emails proposals/reports to you using the Mail app already on this Mac —
  no passwords or SMTP setup needed. It sends through whatever account Mail
  is signed into, to your master address.

  ONE-TIME PERMISSION:
    The first time an agent sends mail, macOS will ask to let the service
    "control Mail". Click OK. If you miss it, enable it later under:
      System Settings → Privacy & Security → Automation
        → (your service / Terminal) → turn ON "Mail"

  REQUIREMENTS:
    • Mail.app must be set up with at least one working account that can send.
    • That's it — delivery uses your existing Mail account.
###################################################
TUTEOF
}

collect_tokens() {
    log "Credentials"
    load_env
    prompt_secret "TELEGRAM_BOT_TOKEN" "Telegram bot token" validate_telegram tut_telegram

    local cur_chat; cur_chat="$(get_env TELEGRAM_CHAT_ID)"
    if [ -n "$cur_chat" ]; then ok "Telegram chat ID already set."
    else
        cat <<'CHATEOF'

####  TELEGRAM CHAT ID  ####
  1. Open Telegram → search @userinfobot → press Start.
  2. It replies with your numeric ID (e.g. 123456789).
###########################
CHATEOF
        printf "Paste your Telegram chat ID (or 'skip'): "; read -r chat
        case "$chat" in skip|SKIP|"") warn "Skipped chat ID (Vox can't auto-send)." ;;
            *) set_env TELEGRAM_CHAT_ID "$chat"; ok "Chat ID saved." ;; esac
    fi

    # Email via Mail.app — recipient is the master address; no SMTP creds needed.
    set_env MASTER_EMAIL "$MASTER_EMAIL"
    hr; tut_mail; hr
    ok "Email recipient set to: $MASTER_EMAIL"

    # Optional: choose which Mail account/address sends (must be one Mail already owns).
    local cur_from; cur_from="$(get_env SMTP_FROM)"
    if [ -n "$cur_from" ]; then
        ok "Sending account already set: $cur_from"
    else
        printf "Send FROM a specific Mail account address? (leave blank = Mail's default): "
        read -r mailfrom
        if [ -n "$mailfrom" ]; then
            set_env SMTP_FROM "$mailfrom"; ok "Will send from: $mailfrom"
        else
            warn "Using Mail.app's default sending account."
        fi
    fi
    warn "Reminder: approve the Automation prompt the first time Ada sends mail."
}

# =============================================================================
#  PHASE 11 — SUMMARY
# =============================================================================
print_summary() {
    load_env
    hr
    cat <<SUMEOF
${c_grn}
  ✅  YOUR LOCAL AI DEVELOPMENT TEAM IS READY
${c_reset}
  ${c_cyn}Telegram:${c_reset}  Open your bot and send /start
  ${c_cyn}Dashboard:${c_reset} http://localhost:$PORT_DASHBOARD

  ${c_yel}THE TEAM${c_reset}
    🤖 Orion  — orchestrator (qwen3.6:35b-a3b), always on
    📊 Ada    — PM/PO (qwen2.5:72b): proposals as PDF + email
    🎨 Mira   — UI/UX (gemma4:26b): wireframes as image + draw.io
    💻 Leo    — developer (qwen2.5-coder:72b): self-tests before QA
    🔎 Nova   — QA (qwen2.5:72b): Puppeteer end-to-end tests
    🛡️ Cipher — pentester (qwen2.5:72b): authorization required
    📡 Vox    — opportunity scout (qwen2.5:72b): twice daily (${VOX_HOUR}:00 & ${VOX_HOUR_PM}:00)

  ${c_yel}OUTPUT — DELIVERED THREE WAYS${c_reset}
    📧 Email     → $MASTER_EMAIL  (sent via your Mac's Mail.app)
    📲 Telegram  → straight to your chat
    📁 OneDrive  → $AI_WORKSPACE
       (proposals/  projects/  reports/  screenshots/  trends/)

  ${c_yel}WORKFLOW${c_reset}
    Send an idea → Orion structures a brief + clean title
      → Ada+Mira proposal (PDF, emailed) → you approve
      → Leo builds & self-tests → Nova E2E tests
      → bugs? Ada tickets → Leo fixes (loop) → Ada final review
      → you approve → done.

  ${c_yel}OTHER SERVICES${c_reset}
    Open WebUI $PORT_OPENWEBUI · SearXNG $PORT_SEARXNG · Langfuse $PORT_LANGFUSE · Portainer $PORT_PORTAINER

  ${c_yel}MANUAL IDE${c_reset}
    Send /pause to free the 72B slot, code in VS Code via Continue,
    then /resume to bring the agents back.

  ${c_yel}CONTROL${c_reset}
    $0 --status | --start | --stop | --restart | --update | --reset
SUMEOF
    # Warnings for missing optional pieces
    [ -z "$(get_env TELEGRAM_BOT_TOKEN)" ] && warn "No Telegram token — run: $0 (re-run) to add it."
    warn "Email uses Mail.app — approve the Automation prompt the first time Ada sends mail (System Settings → Privacy & Security → Automation)."
    case "$AI_WORKSPACE" in
        *CloudStorage/OneDrive*) : ;;
        *) warn "Workspace is NOT in OneDrive ($AI_WORKSPACE). Install/sign in to OneDrive, then re-run to sync." ;;
    esac
    hr
    ok "Tip: keep this Mac awake/plugged in so the team stays responsive."
}

# =============================================================================
#  SERVICE CONTROL
# =============================================================================
SERVICE_LABELS=(com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator com.aiws.vox)

svc_status() {
    hr; log "Service status"
    for url_label in \
        "Ollama|http://localhost:$PORT_OLLAMA/api/tags" \
        "LiteLLM|http://localhost:$PORT_GATEWAY/health/liveliness" \
        "Dashboard|http://localhost:$PORT_DASHBOARD/" \
        "Open WebUI|http://localhost:$PORT_OPENWEBUI/" \
        "SearXNG|http://localhost:$PORT_SEARXNG/" \
        "Langfuse|http://localhost:$PORT_LANGFUSE/api/public/health" \
        "Portainer|http://localhost:$PORT_PORTAINER/"; do
        local nm="${url_label%%|*}" url="${url_label#*|}"
        if http_ok "$url"; then ok "$nm — live"; else warn "$nm — down"; fi
    done
    echo; log "launchd agents"
    for l in "${SERVICE_LABELS[@]}"; do
        if launchctl list 2>/dev/null | grep -q "$l"; then ok "$l — loaded"; else warn "$l — not loaded"; fi
    done
    echo; log "Docker containers"
    docker_up && docker ps --format '  {{.Names}} — {{.Status}}' 2>/dev/null || warn "Docker not running."
}

svc_start() {
    log "Starting services"
    ollama_start
    setup_colima
    for c in open-webui searxng portainer; do docker start "$c" >/dev/null 2>&1 && ok "started $c" || true; done
    [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc up -d >/dev/null 2>&1) && ok "langfuse up" || true
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl load "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 && ok "loaded $l" || true
    done
    ok "Start sequence complete. Check: $0 --status"
}

svc_stop() {
    log "Stopping services"
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 && ok "unloaded $l" || true
    done
    for c in open-webui searxng portainer; do docker stop "$c" >/dev/null 2>&1 && ok "stopped $c" || true; done
    [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc stop >/dev/null 2>&1) && ok "langfuse stopped" || true
    ollama_stop
    ok "Stop sequence complete. (Docker engine/Colima left running.)"
}

svc_restart() { svc_stop; sleep 2; svc_start; }

do_update() {
    log "Updating models + Python packages"
    ollama_start
    for _ in $(seq 1 15); do http_ok "http://localhost:$PORT_OLLAMA/api/tags" && break; sleep 1; done
    for entry in "${MODELS[@]}"; do
        local tag="${entry%%|*}"
        log "Pulling latest: $tag"; ollama pull "$tag" || warn "pull failed: $tag"
    done
    if [ -x "$WORKDIR/.venv/bin/python" ]; then
        local UV; UV="$(command -v uv 2>/dev/null || echo /opt/homebrew/bin/uv)"
        "$UV" pip install --python "$WORKDIR/.venv/bin/python" --upgrade \
            "litellm[proxy]" openai "langfuse>=2.0,<3.0" python-dotenv flask requests \
            rich psutil "python-telegram-bot>=21.0" pyyaml playwright weasyprint markdown \
            && ok "Python packages updated." || warn "Package update had issues."
    fi
    svc_restart
    ok "Update complete."
}

do_reset() {
    hr
    warn "This removes services, the venv, generated agent files, and all project state."
    warn "It does NOT delete pulled Ollama models or your OneDrive documents."
    printf "%sType 'RESET' to confirm: %s" "$c_red" "$c_reset"; read -r confirm
    [ "$confirm" = "RESET" ] || { echo "Cancelled."; return; }
    log "Unloading services"
    for l in "${SERVICE_LABELS[@]}"; do
        launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_DIR/$l.plist"; ok "removed $l"
    done
    log "Removing containers"
    for c in open-webui searxng portainer; do docker rm -f "$c" >/dev/null 2>&1 || true; done
    [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc down >/dev/null 2>&1 || true)
    log "Clearing generated files + project state"
    rm -rf "$WORKDIR/.venv"
    rm -f  "$WORKDIR/agents/orchestrator.py" "$WORKDIR/agents/trend_watcher.py" \
           "$WORKDIR/agents/team.yaml" "$WORKDIR/dashboard/app.py"
    rm -f  "$WORKDIR/projects.json" "$WORKDIR/agent_status.json" \
           "$WORKDIR/pending_actions.json"
    rm -rf "$WORKDIR/conversations"
    ok "Reset complete. Re-run this script to rebuild cleanly (models are preserved)."
    hr
}

# =============================================================================
#  MAIN
# =============================================================================
run_install() {
    hr; log "Local AI Development Team — installer"; hr
    preflight
    setup_workspace
    setup_xcode_clt
    setup_homebrew
    setup_core_tools
    setup_ollama
    setup_python
    setup_colima
    setup_openwebui
    setup_searxng
    setup_langfuse
    setup_portainer
    setup_litellm
    setup_agent_team
    write_dashboard
    setup_continue
    collect_tokens
    setup_services
    print_summary
}

main() {
    case "${1:-}" in
        --status)  load_env; svc_status ;;
        --start)   load_env; svc_start ;;
        --stop)    load_env; svc_stop ;;
        --restart) load_env; svc_restart ;;
        --update)  load_env; do_update ;;
        --reset)   load_env; do_reset ;;
        --help|-h)
            cat <<HELPEOF
Local AI Development Team — setup & control

Usage: $0 [option]

  (no option)   Install / repair everything (safe to re-run)
  --status      Show service + container status
  --start       Start all services
  --stop        Stop all services
  --restart     Restart all services
  --update      Pull latest models + update Python packages
  --reset       Remove services/venv/state (keeps models + OneDrive docs)
  --help        Show this help
HELPEOF
            ;;
        "") run_install ;;
        *) err "Unknown option: $1"; echo "Try: $0 --help"; exit 1 ;;
    esac
}

main "$@"
