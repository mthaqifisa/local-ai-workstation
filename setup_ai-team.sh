#!/usr/bin/env bash
# Re-exec under bash if started with sh/dash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
# =============================================================================
#  setup_ai_team.sh — Local AI Development Team  (Apple Silicon / 64 GB)
# =============================================================================
#  AGENT TEAM & MODEL LINEUP:
#    Orion   (Orchestrator) — qwen3.6:35b-a3b   ~26 GB always loaded
#    Leo     (Developer)    — qwen2.5-coder:72b ~44 GB on demand
#    Cipher  (Pentester)    — qwen2.5:72b       ~44 GB on demand
#    Ada     (PM/PO)        — qwen2.5:72b       ~44 GB on demand (rich docs)
#    Nova    (QA)           — qwen2.5:72b       shares Ada's slot
#    Vox     (Trends)       — qwen2.5:72b       shares Ada's slot
#    Mira    (UI/UX)        — gemma4:26b        ~18 GB on demand (multimodal)
#    IDE     (manual)       — qwen3.6:27b       ~22 GB only during /pause
#
#  MEMORY: Orion+Ada peak = ~52 GB.  OS+Docker overhead ~8 GB.  Total ~60/64 GB.
#
#  WORKFLOW:
#    You → idea → Ada+Mira proposal → Your approval
#    → Leo builds → Nova tests → bugs? → Leo fixes (loop)
#    → Ada final review → Your approval → Done ✅
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

# All agent-generated output (code, images, reports, proposals) goes here.
# Each task gets its own subfolder. Change this if you want a different location.
AI_WORKSPACE="${AI_WORKSPACE:-$HOME/AI}"

COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

# Orion (14B ~8GB) + one specialist loaded at once.
# Ada/Nova/Vox use 72B (~44GB) so peak is ~52GB — tight but fits on 64GB.
OLLAMA_MAX_LOADED="${OLLAMA_MAX_LOADED:-2}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-3m}"

PORT_OLLAMA=11434; PORT_OPENWEBUI=3001; PORT_LANGFUSE=3000
PORT_SEARXNG=8888; PORT_GATEWAY=4000;  PORT_DASHBOARD=8800
PORT_PORTAINER=9001

VOX_HOUR="${VOX_HOUR:-7}";  VOX_MINUTE="${VOX_MINUTE:-0}"

MODELS=(
  "qwen3.6:35b-a3b|Orion — orchestrator, always loaded (~26 GB)"
  "qwen2.5-coder:72b|Leo — 72B coding specialist (~44 GB)"
  "qwen2.5:72b|Ada + Nova + Vox + Cipher — 72B reasoning (~44 GB)"
  "qwen2.5:72b|Ada + Nova + Vox — 72B for rich proposals, QA, analysis (~44 GB)"
  "gemma4:26b|Mira — multimodal UI/UX designer, can analyse images (~18 GB)"
  "qwen3.6:35b-a3b|Orion orchestrator (~26 GB)"
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
# Priority: DMG app (/Applications/Ollama.app) beats brew formula.
# If both somehow exist, prefer DMG since brew services won't manage a DMG install.
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
#  WORKSPACE — all agent-generated files live here
# =============================================================================
setup_workspace() {
    log "AI Workspace: $AI_WORKSPACE"
    for subdir in screenshots proposals projects reports trends; do
        mkdir -p "$AI_WORKSPACE/$subdir"
    done
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

MODEL LINEUP (hybrid — quality where it matters):
  Orion  (orchestrator)  qwen3.6:35b-a3b  ~26 GB always on
  Leo    (developer)     qwen2.5-coder:72b ~44 GB 72B coder
  Cipher (pentester)     qwen2.5:72b      ~44 GB 72B pentest
  Ada    (PM/PO)         qwen2.5:72b      ~44 GB richest reasoning
  Nova   (QA)            qwen2.5:72b      same slot as Ada
  Vox    (trends)        qwen2.5:72b      same slot as Ada
  Mira   (UI/UX)         gemma4:26b       ~18 GB multimodal (sees images)

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
    for p in colima docker docker-compose node git jq wget lazydocker uv socat; do
        brew list "$p" >/dev/null 2>&1 && ok "$p present" || opt brew install "$p"
    done
    have node && ok "node $(node -v)"
    have uv   && ok "uv $(uv --version 2>/dev/null)"
    grep -q '.local/bin' "$HOME/.zprofile" 2>/dev/null || \
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"
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
    # MLX check
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
       "import litellm,flask,requests,psutil,telegram,yaml,playwright" >/dev/null 2>&1
}
setup_python() {
    log "Python virtualenv (gateway + dashboard + orchestrator)"

    if venv_ok; then ok "Venv healthy — all packages verified."; return; fi

    # Wipe any partial/broken venv before rebuilding
    if [ -d "$WORKDIR/.venv" ]; then
        warn "Incomplete venv detected — wiping and rebuilding from scratch."
        rm -rf "$WORKDIR/.venv"
    fi

    # Create fresh venv with Python 3.12
    (cd "$WORKDIR" && uv venv --python 3.12 .venv) \
        || { err "uv venv failed. Try: brew install python@3.12"; return; }
    ok "Venv created. Installing packages…"

    # Use uv pip install (uv venv does not include pip by default)
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
        || { err "Package install failed — check errors above."; return; }

    # Install headless Chromium for URL screenshots (one-time ~300 MB download)
    ok "Installing headless Chromium for URL screenshots…"
    "$WORKDIR/.venv/bin/playwright" install chromium \
        && ok "Chromium installed — URL screenshots enabled." \
        || warn "Chromium install failed — full-screen screenshots still work."

    # Verify every import that the app needs
    if venv_ok; then
        ok "Venv ready — all packages verified."
        # If services were already registered, reload them to pick up the new venv
        for svc in com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator; do
            if [ -f "$LAUNCH_DIR/$svc.plist" ]; then
                launchctl unload "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
                launchctl load   "$LAUNCH_DIR/$svc.plist" >/dev/null 2>&1 || true
                ok "reloaded: $svc"
            fi
        done
    else
        err "Venv still incomplete after install."
        warn "Run manually: $WORKDIR/.venv/bin/pip install litellm flask requests psutil python-telegram-bot pyyaml"
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
    # Always stop and recreate — applies any config changes.
    # Volume (data) is preserved; only the container is replaced.
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
    # Generate config only if it doesn't exist (preserve the secret key)
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
    # Always recreate container to apply any config changes
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
    # Always recreate container; volume (admin password + settings) is preserved.
    # Use /var/run/docker.sock — Portainer runs inside the Colima VM where this
    # is the correct path. The macOS host socket (~/.colima/.../docker.sock) must
    # NOT be used here; that path is for the macOS Docker CLI, not containers.
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
#  PHASE 5 — LiteLLM GATEWAY
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

    # ── repair_venv.sh — auto-heals the venv; called by every service launcher ──
    cat > "$WORKDIR/repair_venv.sh" <<'REPAIREOF'
#!/usr/bin/env bash
# repair_venv.sh — rebuilds the Python venv if packages are missing.
# Safe to call on every service start; exits immediately if venv is healthy.
WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
VENV="$WORKDIR/.venv"
LOG="[repair_venv $(date '+%H:%M:%S')]"

venv_healthy() {
    [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/litellm" ] \
    && "$VENV/bin/python" -c \
       "import litellm,flask,requests,psutil,telegram,yaml" >/dev/null 2>&1
}

venv_healthy && exit 0   # Already healthy — nothing to do

echo "$LOG Venv broken or missing — rebuilding…" >&2
rm -rf "$VENV"
cd "$WORKDIR" || exit 1

# Locate uv (Homebrew or standalone install)
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
    requests rich psutil "python-telegram-bot>=21.0" pyyaml playwright >&2 \
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

    # ── start_gateway.sh ──────────────────────────────────────────────────────
    cat > "$WORKDIR/start_gateway.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
# Auto-repair venv before starting — if repair fails, wait 60s then retry
bash "\$WORKDIR/repair_venv.sh" || { echo "Venv repair failed; retrying in 60s" >&2; sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/litellm" \\
    --config "\$WORKDIR/litellm.config.yaml" \\
    --port $PORT_GATEWAY --host 0.0.0.0
SHEOF
    chmod +x "$WORKDIR/start_gateway.sh"

    # ── start_dashboard.sh ────────────────────────────────────────────────────
    cat > "$WORKDIR/start_dashboard.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { echo "Venv repair failed; retrying in 60s" >&2; sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/python" "\$WORKDIR/dashboard/app.py"
SHEOF
    chmod +x "$WORKDIR/start_dashboard.sh"

    # ── start_orchestrator.sh ─────────────────────────────────────────────────
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

    # ── team.yaml — keep if exists (user may have customised prompts) ──────────
    [ -f "$AD/team.yaml" ] && { ok "team.yaml exists — keeping."; } || {
    cat > "$AD/team.yaml" <<'TEAMEOF'
roles:
  orion:
    name: Orion
    role: Main Orchestrator
    model: orion
    system_prompt: |
      You are ORION, lead AI orchestrator and personal assistant running on this Mac.
      You talk to your owner via Telegram and help with anything they need.

      YOUR JOBS:
      1. Answer questions directly — you are smart, knowledgeable, and helpful.
      2. Classify build requests: when the user wants to make something, start the workflow.
      3. Control this Mac when asked: open apps, run commands, search the web, take screenshots.
      4. Delegate project work to the specialist team following the workflow.
      5. Keep replies concise. One message per turn.

      YOUR TEAM (use them for project work):
      - Ada  (qwen2.5:72b)       — product proposals, planning, final reviews
      - Mira (gemma4:26b)        — UI/UX design, can analyse images
      - Leo  (qwen2.5-coder:72b) — writes code in any language or stack
      - Nova (qwen2.5:72b)       — QA testing and bug reports
      - Cipher (qwen2.5:72b)     — security audits (always confirm with user first)
      - Vox  (qwen2.5:72b)       — daily tech trends and project inspiration

      WORKFLOW: Idea → Ada+Mira proposal → User approval → Leo builds
        → Nova tests → bugs? → Leo fixes → Ada final review → User approval → Done.

      RULES:
      - Never start heavy project work without user approval via Telegram buttons.
      - Never run a pentest without explicit user confirmation.
      - You run locally on this Mac — no cloud, no data leaves this machine.
      - Be direct and natural. Answer questions yourself before delegating.

  ada:
    name: Ada
    role: Product Owner / PM
    model: ada
    system_prompt: |
      You are ADA, Product Owner. You write detailed project proposals and perform
      final reviews. Always use Markdown: ## headings, bullet lists, tables.
      User stories in Given/When/Then format. Be honest about risks and blockers.
      Proposal structure: Executive Summary, User Stories, Tech Scope, Stack, Milestones, Risks.

      IMPORTANT — end every proposal with EXACTLY this ticket block (no exceptions):
      ---TICKETS---
      STORY|high|[Story title]|[One-sentence description]
      STORY|medium|[Story title]|[One-sentence description]
      TASK|high|[Task title]|[One-sentence description]
      TASK|medium|[Task title]|[One-sentence description]
      (Add as many STORY and TASK lines as the project needs — minimum 3 stories)

  mira:
    name: Mira
    role: UI/UX Designer
    model: mira
    system_prompt: |
      You are MIRA, UI/UX Designer. You can analyse images and screenshots.
      Produce detailed design briefs: user journeys, wireframe descriptions,
      component lists, colour palette, typography, accessibility notes.
      Describe layouts precisely enough for a developer to implement without ambiguity.

  leo:
    name: Leo
    role: Developer
    model: leo
    system_prompt: |
      You are LEO, senior full-stack developer. You write production-ready code in any
      language or framework. Include a README.md with setup/run instructions.
      When deployment is complete, end your response with exactly: DEPLOYMENT COMPLETE
      followed by a summary of what was built and how to access it.

  nova:
    name: Nova
    role: QA / Tester
    model: nova
    system_prompt: |
      You are NOVA, QA engineer. Write thorough tests (Given/When/Then format).
      Cover: happy paths, edge cases, error handling, security basics, performance.
      For each bug: [BUG-NNN] Title | Severity: Critical/High/Medium/Low | Steps to reproduce.
      If everything passes, end with exactly: ALL TESTS PASSED

  cipher:
    name: Cipher
    role: Security Pentester
    model: cipher
    system_prompt: |
      You are CIPHER, grey-hat security pentester. Only act when explicitly invoked.
      Audit code and systems for vulnerabilities. Report as:
        [VULN-NNN] Title | Severity | CVSS | Description | Exploit vector | Remediation
      Always remind: only test systems you own or have written permission to test.
      Never assist with illegal activity.

  vox:
    name: Vox
    role: Trend Watcher
    model: vox
    system_prompt: |
      You are VOX, tech trend analyst. Suggest 3 buildable project ideas per session.
      For each: bold Title | 2-sentence description | why timely now |
      tech stack | complexity (Simple/Medium/Complex).
      Focus on ideas buildable locally with open-source tools.
TEAMEOF
    ok "team.yaml written."; }

    # ── orchestrator.py — always regenerate to apply latest fixes ────────────
    {
    cat > "$AD/orchestrator.py" <<'ORCHEOF'
#!/usr/bin/env python3
"""Orion — Multi-Agent Telegram Orchestrator.
Fixes vs v1: send() uses bot.send_message (works from callbacks too),
file locking on projects.json, Cipher gate, async agent calls, typing indicator.
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
WORKSPACE = os.environ.get("AI_WORKSPACE", os.path.join(os.path.expanduser("~"), "AI"))
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

def _ws(*parts):
    """Create dirs and return path under WORKSPACE."""
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

# ── Ticket helpers ─────────────────────────────────────────────────────────────
def _prefix(idea):
    """Short uppercase ticket prefix from project idea, e.g. 'FIT' from 'fitness tracker'."""
    words = re.sub(r'[^\w\s]','',idea.lower()).split()
    return ''.join(w[0].upper() for w in words[:3]) or "PRJ"

def _parse_tickets(text, idea, existing=None):
    """Parse ---TICKETS--- section from LLM response into ticket dicts."""
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
        assignee = {'STORY':'leo','TASK':'leo','BUG':'leo','SUBTASK':'leo'}.get(ttype,'leo')
        tickets.append({
            'id':       f"{prefix}-{start_idx + len(tickets):03d}",
            'type':     ttype.lower(),
            'title':    title,
            'desc':     desc,
            'priority': priority,
            'status':   'todo',
            'assignee': assignee,
            'created':  str(datetime.datetime.now())[:19],
            'updated':  str(datetime.datetime.now())[:19],
        })
    return tickets

def _parse_bugs(qa_text, idea, existing_count=0):
    """Extract [BUG-NNN] entries from Nova's QA output and create bug tickets."""
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
            'id':       f"{prefix}-BUG-{bid}",
            'type':     'bug',
            'title':    m.group(2).strip(),
            'desc':     (m.group(4) or '').strip(),
            'priority': priority,
            'status':   'todo',
            'assignee': 'leo',
            'created':  str(datetime.datetime.now())[:19],
            'updated':  str(datetime.datetime.now())[:19],
        })
    return bugs

# ── File sending tool ─────────────────────────────────────────────────────────
def mt_list_workspace_files(cid):
    """List all files across ~/AI/ for this user's projects."""
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
        files = sorted(os.listdir(shots))[-5:]  # last 5
        for f in files:
            lines.append(f"• [Screenshots] `{f}`")
    if not lines:
        return "No files generated yet. Start a project first."
    return "📁 *Your workspace files:*\n" + "\n".join(lines)

def mt_find_file(query, cid):
    """Find and return (photo/file, path, name) for a natural language file request."""
    projects = load_projects()
    proj = projects.get(cid, {})
    idea = proj.get("idea","")
    if not idea:
        return ("text","No active project found. Start a project first.","")
    pname = _safe_name(idea)
    ml = query.lower()
    # Screenshots
    if "screenshot" in ml:
        shots = os.path.join(WORKSPACE, "screenshots")
        if os.path.exists(shots):
            files = [f for f in sorted(os.listdir(shots)) if f.endswith('.png')]
            if files:
                path = os.path.join(shots, files[-1])
                return ("photo", path, f"📸 {files[-1]}")
        return ("text","No screenshots found. Use /screenshot first.","")
    # Proposal
    for kw in ["proposal","brief","design"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "proposals", pname)
            if os.path.exists(folder):
                files = [f for f in os.listdir(folder) if f.endswith('.md')]
                if files:
                    path = os.path.join(folder, sorted(files)[-1])
                    return ("file", path, os.path.basename(path))
    # QA / bug report
    for kw in ["qa","test","bug","report"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "projects", pname)
            if os.path.exists(folder):
                for f in sorted(os.listdir(folder), reverse=True):
                    if any(x in f for x in ["qa_","bug_"]):
                        return ("file", os.path.join(folder, f), f)
    # Code / implementation
    for kw in ["code","implementation","leo","project"]:
        if kw in ml:
            folder = os.path.join(WORKSPACE, "projects", pname)
            if os.path.exists(folder):
                for f in sorted(os.listdir(folder), reverse=True):
                    if "leo_output" in f:
                        return ("file", os.path.join(folder, f), f)
    return ("text", f"Couldn't find that file. Use `/files` to see all available files.","")

def mt_send_specific_file(path):
    """Send a specific file from the workspace."""
    if not os.path.isfile(path):
        return ("text", f"File not found: {path}","")
    name = os.path.basename(path)
    if name.lower().endswith(('.png','.jpg','.jpeg','.gif','.webp')):
        return ("photo", path, f"📸 {name}")
    return ("file", path, name)

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

# =============================================================================
# MACHINE TOOLS — Orion runs on the Mac so it has full system access.
# Safe tools execute immediately. Controlled tools require your approval.
# =============================================================================
import psutil, requests as _req

def _run(cmd, timeout=30):
    """Execute a shell command, return (ok, output)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out or "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Command timed out."
    except Exception as e:
        return False, str(e)

def mt_list_models():
    """List all Ollama models installed on this machine."""
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
    """Get CPU, RAM, disk, and OS info."""
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
    """List files in a directory."""
    ok, out = _run(f"ls -lah {path} 2>&1 | head -40")
    return f"📂 `{path}`:\n```\n{out}\n```"

def mt_running_services():
    """Show status of AI workstation services."""
    checks = {
        "Ollama":   "http://localhost:11434/api/tags",
        "LiteLLM":  "http://localhost:4000/health/liveliness",
        "Dashboard":"http://localhost:8800/",
        "Open WebUI":"http://localhost:3001/",
        "SearXNG":  "http://localhost:8888/",
        "Langfuse": "http://localhost:3000/api/public/health",
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
    """Run an arbitrary shell command and return output."""
    ok, out = _run(cmd, timeout=60)
    status = "✅" if ok else "❌"
    return f"{status} `{cmd}`\n```\n{out[:3000]}\n```"

def mt_open_app(app_name):
    """Open a macOS application."""
    ok, out = _run(f'open -a "{app_name}"')
    return f"✅ Opened *{app_name}*." if ok else f"❌ Could not open {app_name}: {out}"

def mt_music(action="play", query=""):
    """Control music via AppleScript. action: play/pause/stop/next."""
    scripts = {
        "pause": 'tell application "Music" to pause',
        "stop":  'tell application "Music" to stop',
        "next":  'tell application "Music" to next track',
        "prev":  'tell application "Music" to previous track',
        "play":  f'tell application "Music" to play' if not query else
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
    """Pull the search term from natural language like 'search for X' or 'look up "X"'."""
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
    """Convert a natural language navigation request to a URL."""
    ml = msg.lower()
    # Direct URL in message
    u = re.search(r'https?://\S+', msg)
    if u: return u.group(0).rstrip('.,)')
    # YouTube search
    if "youtube" in ml:
        q = _extract_query(msg)
        if not q:
            m = re.search(r'for\s+["\u201c]?([^"\u201d,\.\n]+)', msg, re.IGNORECASE)
            q = m.group(1).strip() if m else None
        return (f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
                if q else "https://www.youtube.com")
    # Google search
    if "google" in ml and any(w in ml for w in ["search","look up","find"]):
        q = _extract_query(msg)
        return (f"https://www.google.com/search?q={urllib.parse.quote(q)}"
                if q else "https://www.google.com")
    # Named site
    for site, url in _SITE_MAP.items():
        if site in ml: return url
    return None

def mt_browse(msg):
    """Open URL parsed from natural language instruction in the best available browser."""
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
    """Full Mac screen screenshot using built-in screencapture."""
    path = os.path.join(_ws("screenshots"), f"screen_{int(time.time())}.png")
    ok, out = _run(f"screencapture -x {path}")
    if ok and os.path.exists(path):
        return ("photo", path, "📸 Full screen screenshot")
    return ("text", f"❌ Screenshot failed: {out}", "")

def mt_screenshot_url(url, label=""):
    """Screenshot of a localhost URL using playwright (headless Chromium)."""
    safe_label = re.sub(r'[^\w]', '_', label or url)[:30]
    path = os.path.join(_ws("screenshots"), f"{safe_label}_{int(time.time())}.png")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            pg = browser.new_context(
                viewport={"width": 1400, "height": 900},
                ignore_https_errors=True,
            ).new_page()
            pg.goto(url, wait_until="networkidle", timeout=20000)
            time.sleep(1)
            pg.screenshot(path=path, full_page=False)
            browser.close()
        return ("photo", path, f"📸 {label or url}")
    except ImportError:
        return ("text",
            "❌ Playwright not installed.\n"
            f"Run: `{HOME}/.venv/bin/pip install playwright && "
            f"{HOME}/.venv/bin/playwright install chromium`", "")
    except Exception as e:
        return ("text", f"❌ Screenshot of {url} failed: {e}", "")

# ── URL label lookup ──────────────────────────────────────────────────────────
_URL_SHORTCUTS = {
    "dashboard":  ("Dashboard",  "http://localhost:8800"),
    "portainer":  ("Portainer",  "http://localhost:9001"),
    "open webui": ("Open WebUI", "http://localhost:3001"),
    "langfuse":   ("Langfuse",   "http://localhost:3000"),
    "searxng":    ("SearXNG",    "http://localhost:8888"),
    "ollama":     ("Ollama",     "http://localhost:11434"),
    "gateway":    ("LiteLLM",    "http://localhost:4000"),
}

def _parse_url_target(msg):
    """Extract (label, url) from a message, or (None, None)."""
    ml = msg.lower()
    pm = re.search(r'localhost:(\d+)', ml) or re.search(r'port (\d+)', ml)
    if pm:
        port = pm.group(1)
        return f"localhost:{port}", f"http://localhost:{port}"
    for key, (label, url) in _URL_SHORTCUTS.items():
        if key in ml:
            return label, url
    return None, None

# ── Tool dispatch — maps message to (description, pre-bound call) ─────────────
# Every entry that changes machine state REQUIRES approval.
# Read-only queries execute immediately.
def _build_call(msg):
    """
    Returns (description, call_lambda, needs_approval) or (None, None, None).
    The call_lambda returns either a string OR a ("photo", path, caption) tuple.
    """
    ml = msg.lower()

    # ── READ-ONLY — no approval ───────────────────────────────────────────────
    if any(w in ml for w in ["list model","what model","which model","models on",
                              "installed model","available model","ollama model",
                              "do i have model"]):
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

    # ── SCREENSHOTS — require approval ────────────────────────────────────────
    if any(w in ml for w in ["screenshot","snap my","take a photo","capture screen",
                              "show me my","show me the dashboard","show me the",
                              "what does my screen"]):
        label, url = _parse_url_target(msg)
        if url:
            return f"Screenshot of {label} ({url})", \
                   lambda u=url, l=label: mt_screenshot_url(u, l), True
        # Full screen fallback
        return "Full screen screenshot", mt_screenshot_screen, True

    # ── MUSIC CONTROL — require approval ─────────────────────────────────────
    if any(w in ml for w in ["pause music","pause song","stop music","stop playing"]):
        return "Pause music", lambda: mt_music("pause"), True
    if any(w in ml for w in ["next song","skip song","next track","skip track"]):
        return "Skip to next track", lambda: mt_music("next"), True
    if any(w in ml for w in ["previous song","prev song","previous track","go back"]):
        return "Previous track", lambda: mt_music("prev"), True
    if any(w in ml for w in ["play music","resume music","play song","play spotify","open music"]):
        return "Play music", lambda: mt_music("play"), True

    # ── FILE SENDING — no approval (read-only) ────────────────────────────────
    if any(w in ml for w in ["list my files","list files","my files","workspace files",
                              "what files","files in my workspace"]):
        return "List workspace files", lambda: mt_list_workspace_files(""), False

    if any(w in ml for w in ["send me the proposal","send proposal","share proposal",
                              "send me the code","send the code","send leo",
                              "send qa report","send qa","send bug report",
                              "send me the report","send screenshot","send me screenshot",
                              "send latest screenshot"]):
        return f"Send file: {msg[:50]}", lambda q=msg: ("text","__FILE_REQUEST__",""), False

    # ── WEB SEARCH — safe, no approval ───────────────────────────────────────
    if any(kw in ml for kw in [
        "search for ","search the web","look up ","search online",
        "google ","find info about","find information about","research ",
    ]):
        query = re.sub(r'^(search for|search the web for|look up|google|search online for|'
                       r'find info(rmation)? (about|on)|research)\s+', '', ml, flags=re.IGNORECASE).strip()
        if not query: query = msg
        return f"Web search: {query}", lambda q=query: (
            "text",
            "🔍 *Search results for:* _" + q + "_\n\n" + _search_web_sync(q, max_results=5),
            ""
        ), False

    # ── BROWSER NAVIGATION — require approval (catches before generic open_app) ─
    # Triggers on any request to visit a URL, search a site, or navigate somewhere.
    _nav_kw = [
        "go to youtube","open youtube","navigate to youtube","youtube search",
        "search youtube","search on youtube","look up on youtube","find on youtube",
        "navigate to","go to http","open http","browse to","open website",
        "navigate to google","search on google","open google and",
    ]
    _nav_compound = re.search(
        r'open\s+(chrome|browser|safari|firefox)\s+(and|to|then)', ml)
    if any(kw in ml for kw in _nav_kw) or _nav_compound:
        url = _build_nav_url(msg)
        desc = f"Open browser → {url}" if url else f"Browse: {msg[:60]}"
        return desc, lambda m=msg: mt_browse(m), True

    # Also catch bare site names when navigating is clearly implied
    if any(site in ml for site in _SITE_MAP) and any(
            w in ml for w in ["go to","open","navigate","visit","browse","take me"]):
        url = _build_nav_url(msg)
        if url:
            return f"Open browser → {url}", lambda m=msg: mt_browse(m), True

    # ── OPEN APP — require approval ───────────────────────────────────────────
    if "open " in ml or "launch " in ml:
        for app in ["Spotify","Terminal","Safari","Chrome","Firefox","Finder",
                    "VS Code","Xcode","Notes","Calendar","Messages","Mail","Slack"]:
            if app.lower() in ml:
                return f"Open {app}", lambda a=app: mt_open_app(a), True
        # Generic: extract word after "open"
        m = re.search(r'(?:open|launch)\s+([\w\s]+)', ml)
        if m:
            app = m.group(1).strip().title()
            return f"Open {app}", lambda a=app: mt_open_app(a), True

    return None, None, None

def detect_tool(msg):
    """Returns (description, call_lambda, needs_approval) or (None, None, None)."""
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
    full = [{"role": "system", "content": cfg["system_prompt"]}] + msgs
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
    """Remove <think>…</think> blocks that qwen3 extended-thinking emits."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

async def invoke_think(name, msgs, temp=0.7):
    """Like invoke() but enables qwen3 extended thinking (/think prefix) and strips the block."""
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1] + [{"role":"user","content":"/think " + msgs[-1]["content"]}]
    raw = await invoke(name, msgs, temp)
    return _strip_think(raw)

# Keywords that mean "consult Ada" instead of answering directly
_ADA_CONSULT_KW = [
    "how should i build","best way to build","best approach to","recommend a stack",
    "what tech stack","which framework should","architecture for","how to design",
    "should i use","what database","microservice or monolith","help me plan",
    "how would you architect","api design","what's the best approach",
]
# Keywords that trigger extended thinking
_THINK_KW = [
    "explain how","why does","what is the difference between","analyze",
    "compare","trade-off","deep dive","how exactly","walk me through","break down",
]

def _needs_ada(msg): return any(k in msg.lower() for k in _ADA_CONSULT_KW)
def _needs_think(msg): return len(msg) > 60 and any(k in msg.lower() for k in _THINK_KW)

# ── Web search for ReAct loop ─────────────────────────────────────────────────
def _search_web_sync(query, max_results=4):
    """Query SearXNG for solutions. Runs in thread pool."""
    try:
        r = _req.get(SX, params={"q": query, "format": "json"}, timeout=8)
        results = r.json().get("results", [])[:max_results]
        if not results:
            return "No results found."
        return "\n\n".join(
            f"[{res.get('title','')}] {res.get('content','')[:250]}"
            for res in results
        )
    except Exception as e:
        return f"Search unavailable: {e}"

# ── ReAct execution loop ──────────────────────────────────────────────────────
# Reason → Act → Observe → if failed → Search → Reason → Act again
# Shows visible thinking steps in Telegram via message edits.
async def react_tool(ctx, cid, desc, call_fn):
    """Run a tool with visible thinking and auto-retry via web search on failure."""
    loop  = asyncio.get_running_loop()

    # Live status message — edited at each step so user sees reasoning
    live  = await ctx.bot.send_message(
                chat_id=int(cid), text=f"💭 _Planning: {desc}_", parse_mode="Markdown")

    async def upd(text):
        try: await live.edit_text(text, parse_mode="Markdown")
        except Exception: pass

    def _looks_failed(r):
        if isinstance(r, tuple):
            return r[0] == "text" and any(
                w in (r[1] or "").lower()
                for w in ["❌","error","failed","could not","not found","unable"])
        return isinstance(r, str) and any(
            w in r.lower() for w in ["❌","error","failed","could not","not found"])

    # ── Attempt 1 ──────────────────────────────────────────────────────────────
    await upd(f"⚙️ _Executing: {desc}_")
    result = await loop.run_in_executor(_exec, call_fn)

    if not _looks_failed(result):
        try: await live.delete()
        except Exception: pass
        await send_result(ctx, cid, result)
        return

    err = result[1] if isinstance(result, tuple) else str(result)

    # ── Step 2: Search for fix ─────────────────────────────────────────────────
    await upd(f"❌ _Failed:_ `{err[:100]}`\n\n🔍 _Searching web for a fix…_")
    search_q = f"macOS {desc} {err[:120]} fix solution 2024"
    search_res = await loop.run_in_executor(_exec, lambda: _search_web_sync(search_q))

    # ── Step 3: Reason about fix ───────────────────────────────────────────────
    await upd(f"💭 _Analysing search results, deriving fix…_")
    fix_prompt = (
        f"Task: {desc}\n"
        f"Error: {err[:400]}\n"
        f"Web search results:\n{search_res[:1200]}\n\n"
        "Based on the above, what is the correct single shell command to fix this on macOS? "
        "Reply with ONLY the shell command — no explanation."
    )
    fix_cmd = await invoke("orion", [{"role":"user","content":fix_prompt}])
    fix_cmd = fix_cmd.strip().strip('`').split('\n')[0].strip()

    # ── Step 4: Retry with fix ─────────────────────────────────────────────────
    await upd(f"🔄 _Retrying with fix:_\n`{fix_cmd}`")
    ok2, out2 = await loop.run_in_executor(_exec, lambda: _run(fix_cmd, timeout=30))

    try: await live.delete()
    except Exception: pass

    if ok2:
        await send(ctx, cid,
            f"✅ *Fixed!*\n\n"
            f"*Original error:* `{err[:120]}`\n"
            f"*Solution applied:* `{fix_cmd}`"
            + (f"\n\n```\n{out2[:400]}\n```" if out2.strip() else ""))
    else:
        await send(ctx, cid,
            f"❌ *Still failing after web search + retry.*\n\n"
            f"Tried: `{fix_cmd}`\n"
            f"Error: `{out2[:300]}`\n\n"
            f"Try `/run {fix_cmd}` manually or check if the app is installed.")

# ── Project intent detection — keyword-first, no LLM required ─────────────────
_PROJECT_SIGNALS = [
    "build me ","build a ","build an ","create me ","create a ","create an ",
    "make me an ","make me a ","make an app","make a website","make a tool",
    "i want to build","i want to create","i want to make an","i want to make a",
    "let's build","let us build","let's create","start a project","new project",
    "develop a ","develop an ","write me a program","write me a script",
    "code me a","code a ","program a ","i need you to build","i need you to create",
    "can you build","can you create","can you make me",
]

def _is_clear_project(msg):
    ml = msg.lower()
    return any(s in ml for s in _PROJECT_SIGNALS)

# Hardcoded identity answer — returned any time Orion tries to deflect
_ORION_ID = (
    "I'm *Orion* — your AI team running locally on this Mac.\n\n"
    "🧠 *Me:* `qwen3.6:35b-a3b` via Ollama\n\n"
    "*My team:*\n"
    "• 📊 *Ada* `qwen2.5:72b` — proposals, planning, final reviews\n"
    "• 🎨 *Mira* `gemma4:26b` — UI/UX, wireframes, image analysis\n"
    "• 💻 *Leo* `qwen2.5-coder:72b` — writes & ships code in any language\n"
    "• 🔎 *Nova* `qwen2.5:72b` — QA, testing, bug reports\n"
    "• 🛡️ *Cipher* `qwen2.5:72b` — security audits (confirmation required)\n"
    "• 📡 *Vox* `qwen2.5:72b` — tech trends & project ideas\n\n"
    "*What I can do:*\n"
    "• Answer any question — code, tech, general knowledge\n"
    "• Control this Mac — open apps, run commands, play music\n"
    "• Navigate the web — open URLs, YouTube search, etc.\n"
    "• Take screenshots of any app or localhost URL\n"
    "• Build software end-to-end with my team\n"
    "• Send files — proposals, reports, code via Telegram\n"
    "• Add new skills — /upgrade <capability>\n\n"
    "_Everything local. No cloud. No data leaves this machine._"
)

# Phrases indicating Orion is deflecting — catches the trained refusal behaviour
_DEFLECT_PHRASES = [
    "not disclose","cannot disclose","not able to share","not able to reveal",
    "cannot reveal","i don't have access to information about my","not authorized",
    "not provided with","policy","designed not to","not aware of the specific model",
    "cannot share information about my","i am not able to provide",
    "my training does not","not comfortable sharing","prefer not to share",
    "i cannot tell you what model","cannot confirm","unable to confirm",
    "i'm just an ai","just an assistant","not able to confirm",
    "cannot navigate to","i cannot navigate","unable to navigate",
    "cannot browse","i cannot browse","i don't have the ability to browse",
    "i cannot access the internet","cannot access websites","i cannot open",
    "i don't have access to external","cannot interact with websites",
    # Specific phrases from this complaint
    "do not have direct access to your system",
    "my role is to manage workflows",
    "i do not have direct access",
    "manage workflows via telegram",
    "delegate tasks to specialists",
    "enforce project processes",
    "i don't have real-time access",
    "i cannot perform actions on your",
    "i am unable to access your",
    "don't have the ability to interact with your",
    "not equipped to directly access",
    "i only process text",
    "i cannot execute",
    "i don't execute",
    # Internet search deflections
    "cannot perform internet searches",
    "cannot access external information",
    "don't have internet access",
    "cannot browse the internet",
    "cannot access real-time information",
    "no internet access",
    "i cannot search the internet",
    "unable to search the web",
    "i cannot access the web",
    "i don't have the ability to search",
    "cannot retrieve information from the internet",
    "my knowledge cutoff",
    "i cannot look up",
    "cannot look up real",
    "i have no access to the internet",
    # Corporate-speak that means the model is being rigid
    "predefined workflow constraints",
    "my operations remain bound",
    "workflow constraints",
    "i acknowledge your assertion",
    "delegate to specialists as required",
    "please specify agent names",
    "confirm compatibility",
    "predefined constraints",
    "bound to the",
    "operations remain",
]

def _is_deflecting(text):
    ml = text.lower()
    return any(p in ml for p in _DEFLECT_PHRASES)

# Keywords that mean the user is asking about Orion himself
# Any deflected response to these → return _ORION_ID, not self-upgrade offer
_ABOUT_ORION_KW = [
    "agent","team","your model","your capab","your tool","your skill",
    "what can you","what do you","can you do","are you able","you able to",
    "your system","your hardware","your memory","real-time","real time",
    "access to","who are you","what are you","how many","how much do you",
    "your agents","your team","list your","show me your","tell me your",
    "your status","your version","your name","your features",
    "count your","count the","number of","how do you work",
]

def _is_about_orion(msg):
    """True if message is asking about Orion's identity, team, or capabilities."""
    ml = msg.lower()
    return any(k in ml for k in _ABOUT_ORION_KW)

# ── Web search intent ─────────────────────────────────────────────────────────
_SEARCH_KW = [
    "search for","look up","find info","latest news","current price","news about",
    "search the web","search online","find on the internet","what happened",
    "what is happening","find me information","research this","look for",
    "what is the latest","recent news","today's","this week's","right now",
    "current","up to date","live price","stock price","weather in",
    "who won","what time","when does","is it open","near me",
]

def _needs_web_search(msg):
    ml = msg.lower()
    return any(kw in ml for kw in _SEARCH_KW)

# ── Plugin system ─────────────────────────────────────────────────────────────
PLUGINS_DIR = os.path.join(HOME, "agents", "plugins")
_PLUGINS: dict = {}   # fn_name → callable, populated by _load_plugins()

def _load_plugins():
    """Import all .py files from the plugins directory into global tool registry."""
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    count = 0
    for fname in sorted(os.listdir(PLUGINS_DIR)):
        if not fname.endswith(".py"): continue
        fpath = os.path.join(PLUGINS_DIR, fname)
        try:
            ns: dict = {}
            exec(open(fpath).read(), ns)
            for name, fn in ns.items():
                if name.startswith("mt_") and callable(fn):
                    _PLUGINS[name] = fn
                    logger.info(f"Plugin loaded: {name} ← {fname}")
            count += 1
        except Exception as e:
            logger.error(f"Plugin {fname} failed: {e}")
    return count

# ── Self-restart ──────────────────────────────────────────────────────────────
ORCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.aiws.orchestrator.plist")

async def self_restart(ctx, cid, reason="capability upgrade"):
    """Gracefully restart the orchestrator — spawns a delayed shell then exits."""
    await send(ctx, cid, f"🔄 _Restarting ({reason})… back in ~10 seconds._")
    # Spawn background shell that reloads us after we exit
    subprocess.Popen([
        "bash", "-c",
        f"sleep 3 && launchctl unload '{ORCH_PLIST}'; "
        f"sleep 2 && launchctl load '{ORCH_PLIST}'"
    ])
    await asyncio.sleep(1)
    import signal as _signal
    os.kill(os.getpid(), _signal.SIGTERM)

# ── Self-upgrade ──────────────────────────────────────────────────────────────
async def orion_self_upgrade(ctx, cid, capability):
    """
    Orion adds a missing capability:
    1. Searches web for how to implement it
    2. Asks LLM to write the Python tool function + required package
    3. Shows user the code for approval before installing
    """
    live = await ctx.bot.send_message(
        chat_id=int(cid),
        text=f"🔬 _Researching how to add: {capability}_",
        parse_mode="Markdown")

    async def upd(t):
        try: await live.edit_text(t, parse_mode="Markdown")
        except Exception: pass

    loop = asyncio.get_running_loop()

    # 1. Search for implementation
    await upd(f"🔍 _Searching: Python {capability} implementation…_")
    search_res = await loop.run_in_executor(
        _exec, lambda: _search_web_sync(f"Python {capability} library code example"))

    # 2. Generate plugin code + package name
    await upd(f"💭 _Writing plugin code…_")
    code_prompt = (
        f"Write a Python tool function called `mt_{re.sub(r'[^\\w]','_',capability.lower()[:30])}` "
        f"that implements: {capability}\n"
        f"Context from web:\n{search_res[:1000]}\n\n"
        "Requirements:\n"
        "- Use stdlib or a single pip-installable package\n"
        "- First line comment: # requires: <package_name> (or 'stdlib' if no package needed)\n"
        "- Return a string result OR a ('photo'|'file', path, caption) tuple\n"
        "- Handle errors with try/except — never raise\n"
        "- No top-level imports — put imports inside the function\n"
        "Output ONLY the Python function code."
    )
    code_raw = await invoke("orion", [{"role":"user","content":code_prompt}])
    code = re.sub(r'```python\n?|```\n?', '', code_raw).strip()

    # Extract required package from first-line comment
    pkg_match = re.search(r'#\s*requires:\s*([\w\-\[\]>=<.,\s]+)', code)
    package = (pkg_match.group(1).strip() if pkg_match else "").strip()
    fn_name_m = re.search(r'def\s+(mt_\w+)', code)
    fn_name = fn_name_m.group(1) if fn_name_m else "mt_new_tool"

    try: await live.delete()
    except Exception: pass

    # Store for approval
    ctx.user_data["pending_plugin"] = {
        "capability": capability,
        "package": package,
        "fn_name": fn_name,
        "code": code,
    }
    pkg_line = f"*Package to install:* `{package}`\n" if package and package != "stdlib" else ""
    await send(ctx, cid,
        f"🔬 *New capability ready to install*\n\n"
        f"*Capability:* {capability}\n"
        f"{pkg_line}"
        f"*Function:* `{fn_name}`\n\n"
        f"```python\n{code[:800]}{'…' if len(code)>800 else ''}\n```\n\n"
        "Install this and restart Orion?",
        mkb(("✅ Install & restart", "plugin_yes"),
            ("❌ Cancel",           "plugin_no")))

async def keep_typing(bot, cid, stop):
    while not stop.is_set():
        try: await bot.send_chat_action(chat_id=int(cid), action="typing")
        except Exception: pass
        await asyncio.sleep(4)

# ── Messaging — always via bot.send_message so it works in callbacks too ──────
TRIGGER_FILE = os.path.join(HOME, "pending_actions.json")
_app = None   # set in main(); used by dashboard action poller

# ── Per-user conversation history ─────────────────────────────────────────────
# Keeps multi-turn context so Orion can follow a conversation thread.
# Persisted to disk so it survives Orion restarts (e.g. after self-upgrades).
CONV_DIR  = os.path.join(HOME, "conversations")
_CONV: dict = {}    # cid → [{"role":…,"content":…}]
_CONV_MAX = 30      # max messages to keep (15 exchanges)

def _conv_path(cid):
    os.makedirs(CONV_DIR, exist_ok=True)
    return os.path.join(CONV_DIR, f"{cid}.json")

def _conv_load(cid):
    """Load conversation from disk on first access."""
    if cid in _CONV:
        return
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
    _CONV[cid].append({"role": role, "content": str(content)[:4000]})
    if len(_CONV[cid]) > _CONV_MAX:
        # Keep pairs: drop oldest user+assistant pair together
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
    """Send message. If Markdown parsing fails, retries as plain text.
    This ensures reply_markup (buttons) always arrive even when content
    contains unescaped Markdown characters from LLM output."""
    chunks = [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]
    for i, chunk in enumerate(chunks):
        markup = kb if i == len(chunks)-1 else None
        for mode in [pm, None]:          # try Markdown first, fall back to plain
            try:
                await ctx.bot.send_message(
                    chat_id=int(cid), text=chunk,
                    reply_markup=markup, parse_mode=mode)
                break
            except Exception as e:
                err_str = str(e).lower()
                if mode is not None and ("can't parse" in err_str
                                         or "parse entities" in err_str
                                         or "parsing" in err_str):
                    logger.warning(f"Markdown parse failed for cid={cid}, retrying as plain text.")
                    continue
                logger.error(f"send(): {e}")
                break

async def send_result(ctx, cid, result, kb=None):
    """Send a tool result — handles text, photo tuples, and file tuples."""
    if isinstance(result, tuple) and result[0] == "photo":
        _, path, caption = result
        try:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(chat_id=int(cid), photo=f,
                                         caption=caption, reply_markup=kb)
        except Exception as e:
            await send(ctx, cid, f"❌ Could not send photo: {e}")
    elif isinstance(result, tuple) and result[0] == "file":
        _, path, name = result
        try:
            with open(path, "rb") as f:
                await ctx.bot.send_document(chat_id=int(cid), document=f,
                                             filename=name, reply_markup=kb)
        except Exception as e:
            await send(ctx, cid, f"❌ Could not send file: {e}")
    else:
        text = result[1] if isinstance(result, tuple) else result
        await send(ctx, cid, str(text), kb)

def mkb(*pairs):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(l, callback_data=d)] for l, d in pairs])

# ── State machine ─────────────────────────────────────────────────────────────
async def workflow(ctx, cid):
    proj   = get_project(cid)
    status = proj.get("status", "idle")
    idea   = proj.get("idea", "")
    hist   = proj.get("history", [])

    if status == "proposal_drafting":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "🗂️ *Ada & Mira drafting your proposal…* _(~2 min)_")
            ap = (f"Project: {idea}\nWrite a full proposal (Markdown):\n"
                  "## 1. Executive Summary\n## 2. User Stories (Given/When/Then)\n"
                  "## 3. Tech Scope & Architecture\n## 4. Tech Stack\n"
                  "## 5. Milestones\n## 6. Risks & Mitigations")
            proposal = await invoke("ada", hist + [{"role":"user","content":ap}])
            mp = (f"Based on:\n\n{proposal[:2000]}\n\nWrite a UI/UX design brief:\n"
                  "## 1. User Journey\n## 2. Key Screens (wireframe descriptions)\n"
                  "## 3. Visual Principles\n## 4. Component List\n## 5. Accessibility")
            design = await invoke("mira", [{"role":"user","content":mp}])
        finally: stop.set()
        ts     = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        pname  = _safe_name(idea)
        fp     = os.path.join(_ws("proposals", pname), f"proposal_{ts}.md")
        with open(fp, "w") as f:
            f.write(f"# {idea}\n\n## Ada's Proposal\n\n{proposal}\n\n")
            f.write(f"## Mira's Design Brief\n\n{design}\n")
        # Auto-send proposal document to Telegram
        await send(ctx, cid, "📎 _Sending proposal document…_")
        await send_result(ctx, cid, ("file", fp, os.path.basename(fp)))
        # Extract Jira-style tickets from Ada's structured output
        tickets = _parse_tickets(proposal, idea)
        if not tickets:
            # Fallback: create a default story if Ada didn't output the block
            tickets = [{"id":f"{_prefix(idea)}-001","type":"story","title":idea,
                        "desc":"Main project story","priority":"high","status":"todo",
                        "assignee":"leo","created":ts,"updated":ts}]
        preview = (f"*📄 Proposal: {idea}*\n\n"
                   f"**Ada (excerpt)**\n{proposal[:800]}…\n\n"
                   f"**Mira (excerpt)**\n{design[:400]}…\n\n"
                   f"🎫 *{len(tickets)} tickets created*\n"
                   f"_📁 `~/AI/proposals/{pname}/proposal_{ts}.md`_")
        update_project(cid, status="awaiting_approval", proposal_file=fp,
            tickets=tickets,
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
                  "Write complete, production-ready code with a README.md.\n"
                  "End with: DEPLOYMENT COMPLETE\nThen summarise what was built.")
            result = await invoke("leo", hist + [{"role":"user","content":dp}])
        finally: stop.set()
        nh = hist + [{"role":"user","content":dp},{"role":"assistant","content":result}]
        if "DEPLOYMENT COMPLETE" in result.upper():
            # Save Leo's code to ~/AI/projects/<project_name>/
            pname   = _safe_name(idea)
            proj_dir= _ws("projects", pname)
            ts      = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            # Save full output as markdown
            with open(os.path.join(proj_dir, f"leo_output_{ts}.md"), "w") as f:
                f.write(f"# {idea}\n_Generated: {ts}_\n\n{result}\n")
            # Extract individual code files from Leo's response
            saved_files = _save_code_files(result, proj_dir)
            files_msg = (f"\n\n📁 *Saved to:* `~/AI/projects/{pname}/`"
                         + (f"\n_Files extracted: {', '.join(saved_files)}_" if saved_files else ""))
            update_project(cid, status="qa_running", history=nh,
                           project_dir=proj_dir)
            await send(ctx, cid,
                f"✅ *Leo: Deployment complete!*\n\n{result[:1400]}{files_msg}")
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
            await send(ctx, cid, "🔎 *Nova running tests…*")
            qp = ("Test the project thoroughly.\n"
                  "For each test: Given [pre] / When [action] / Then [expected] — PASS/FAIL\n"
                  "Also check error handling, edge cases, security basics.\n"
                  "All pass → end with: ALL TESTS PASSED\n"
                  "Bugs → list as [BUG-NNN] Title | Severity | Steps to reproduce")
            result = await invoke("nova", hist + [{"role":"user","content":qp}])
        finally: stop.set()
        nh = hist + [{"role":"user","content":qp},{"role":"assistant","content":result}]
        if "ALL TESTS PASSED" in result.upper():
            # Save QA report
            pname = _safe_name(idea)
            ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            qa_path = os.path.join(_ws("projects", pname), f"qa_report_{ts}.md")
            with open(qa_path, "w") as f:
                f.write(f"# QA Report: {idea}\n_Date: {ts}_\n\n{result}\n")
            await send(ctx, cid, "📎 _Sending QA report…_")
            await send_result(ctx, cid, ("file", qa_path, f"qa_report_{ts}.md"))
            update_project(cid, status="final_review", history=nh)
            await send(ctx, cid,
                f"✅ *Nova: All tests passed!*\n\n{result[:1400]}"
                f"\n\n📁 Report saved: `~/AI/projects/{pname}/qa_report_{ts}.md`")
            await send(ctx, cid, "📋 Activating *Ada* for final review…")
            await workflow(ctx, cid)
        else:
            pname = _safe_name(idea)
            ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            bug_path = os.path.join(_ws("projects", pname), f"bug_report_{ts}.md")
            with open(bug_path, "w") as f:
                f.write(f"# Bug Report: {idea}\n_Date: {ts}_\n\n{result}\n")
            await send(ctx, cid, "📎 _Sending bug report…_")
            await send_result(ctx, cid, ("file", bug_path, f"bug_report_{ts}.md"))
            # Extract bug tickets from Nova's report
            existing = proj.get("tickets", [])
            new_bugs = _parse_bugs(result, idea, len(existing))
            all_tickets = existing + new_bugs
            update_project(cid, status="qa_bugs_found", history=nh, tickets=all_tickets)
            await send(ctx, cid,
                f"🔴 *Nova found issues:*\n\n{result[:1800]}"
                f"\n\n🎫 *{len(new_bugs)} bug tickets created*"
                f"\n📁 Bug report: `~/AI/projects/{pname}/bug_report_{ts}.md`",
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
        r = req.get(SX, params={"q":"emerging tech trends software startup 2025 2026",
                    "format":"json"}, timeout=10)
        snippets = "\n".join(f"- {x.get('title','')}: {x.get('content','')[:200]}"
                             for x in r.json().get("results",[])[:6])
    except Exception as e: snippets = f"Search unavailable: {e}"
    stop = asyncio.Event()
    ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
    try:
        result = await invoke("vox",[{"role":"user","content":
            f"Tech news:\n{snippets}\n\nSuggest 3 buildable project ideas. "
            "Bold title, 2-sentence description, why timely, stack, complexity."}], temp=0.9)
    finally: stop.set()
    await send(ctx, cid, f"📡 *Vox's Picks:*\n\n{result}",
        mkb(("🚀 Start a project from these", "start_from_trend"),
            ("👍 Just browsing, thanks",       "dismiss_trend")))
    ctx.user_data["last_vox"] = result

# ── Command handlers ──────────────────────────────────────────────────────────
async def cmd_start(u, ctx):
    cid = str(u.effective_chat.id)
    _conv_clear(cid)   # fresh slate on /start
    await send(ctx, cid,
        "👋 *I'm Orion, your AI team lead.*\n\n"
        "🤖 *The team:*\n"
        "📊 Ada — Product Owner / PM _(qwen2.5:72b)_\n"
        "🎨 Mira — UI/UX Designer _(gemma4:26b, multimodal)_\n"
        "💻 Leo — Developer _(qwen2.5-coder:72b)_\n"
        "🔎 Nova — QA Tester _(qwen2.5:72b)_\n"
        "🛡️ Cipher — Pentester _(on-demand, requires confirmation)_\n"
        "📡 Vox — Trend Watcher _(daily + on-demand)_\n\n"
        "💡 *Send me a project idea to start!*\n"
        "/status · /projects · /trends · /help · /clear")

async def cmd_clear(u, ctx):
    """/clear — wipe conversation history and start fresh"""
    cid = str(u.effective_chat.id)
    _conv_clear(cid)
    await send(ctx, cid, "🧹 Conversation cleared — fresh start!")

async def cmd_status(u, ctx):
    cid  = str(u.effective_chat.id)
    proj = get_project(cid); sts = read_status()
    desc, icon = STATES.get(proj.get("status","idle"), ("No project","⬜"))
    lines = [f"{icon} *Project:* {desc}"]
    if proj.get("idea"): lines.append(f"💡 *Idea:* {proj['idea'][:60]}")
    lines.append("\n*Agents:*")
    for nm, ic in [("orion","🤖"),("ada","📊"),("mira","🎨"),("leo","💻"),
                   ("nova","🔎"),("cipher","🛡️"),("vox","📡")]:
        s = sts.get(nm,"idle")
        lines.append(f"{ic} {nm.capitalize()}: {'🟡 *working*' if s=='working' else '🟢 idle'}")
    await send(ctx, cid, "\n".join(lines))

async def cmd_projects(u, ctx):
    cid  = str(u.effective_chat.id)
    mine = [v for v in load_projects().values() if str(v.get("chat_id",""))==cid]
    if not mine: await send(ctx, cid, "No projects yet — send me an idea!"); return
    lines = []
    for p in sorted(mine, key=lambda x: x.get("created",""), reverse=True):
        d, ic = STATES.get(p.get("status","idle"), ("?","❓"))
        lines.append(f"{ic} *{(p.get('idea') or '?')[:50]}*\n  _{d}_ · {(p.get('created') or '')[:10]}")
    await send(ctx, cid, "*Your Projects*\n\n" + "\n\n".join(lines))

async def cmd_trends(u, ctx):
    cid = str(u.effective_chat.id)
    await send(ctx, cid, "📡 *Vox searching for trends…*")
    await run_vox(ctx, cid)

async def cmd_pause(u, ctx):
    cid = str(u.effective_chat.id); proj = get_project(cid)
    curr = proj.get("status","idle")
    if curr not in ("idle","completed","paused"):
        update_project(cid, status="paused", prev_status=curr)
        await send(ctx, cid, "⏸️ *Agents paused.*\n\nThe 72B/30B slot is free.\n"
            "VS Code → Continue → select *Leo Manual (qwen3.6:27b)*.\n\n"
            "Send /resume when done to continue the workflow.")
    else:
        ctx.user_data["manually_paused"] = True
        await send(ctx, cid, "⏸️ *Paused* (no active project).\n"
            "Connect Continue in VS Code.\nSend /resume when done.")

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
        "/start · /status · /projects · /trends · /pause · /resume · /help · /run · /screenshot\n\n"
        "*Project:* just send an idea\n"
        "*Machine (read):* 'list models', 'system info', 'service status'\n"
        "*Machine (control — needs approval):* 'open Spotify', 'play music', 'pause music'\n"
        "*Screenshot:*\n"
        "  `/screenshot` — full Mac screen\n"
        "  `/screenshot 8800` — dashboard\n"
        "  `/screenshot dashboard` — same\n"
        "  Or just say _'screenshot of the dashboard'_\n"
        "*Shell:* `/run <command>` e.g. `/run ollama list`\n"
        "*Pentest:* say 'pentest [target]' (requires confirmation)\n"
        "*Manual IDE:* /pause → VS Code + Continue → /resume")

async def cmd_screenshot(u, ctx):
    """Take a screenshot — /screenshot [url or port or 'screen']"""
    cid  = str(u.effective_chat.id)
    arg  = u.message.text.partition(" ")[2].strip()
    if not arg or arg.lower() in ("screen","full","mac"):
        desc, call = "Full screen screenshot", mt_screenshot_screen
    else:
        label, url = _parse_url_target(arg)
        if not url:
            if arg.isdigit():
                url   = f"http://localhost:{arg}"
                label = f"localhost:{arg}"
            else:
                url   = arg if arg.startswith("http") else f"http://{arg}"
                label = arg
        desc  = f"Screenshot of {label or url}"
        call  = lambda u=url, l=label: mt_screenshot_url(u, l)
    ctx.user_data["pending_tool"] = {"desc": desc, "call": call}
    await send(ctx, cid,
        f"📸 *Screenshot requested*\n\n_{desc}_\n\nProceed?",
        mkb(("✅ Yes, take it", "tool_yes"),
            ("❌ Cancel",       "tool_no")))

async def cmd_run(u, ctx):
    """Execute a shell command after user approval — /run <command>"""
    cid = str(u.effective_chat.id)
    cmd = u.message.text.partition(" ")[2].strip()
    if not cmd:
        await send(ctx, cid,
            "Usage: `/run <shell command>`\n"
            "Example: `/run ls ~/ai-workstation`\n"
            "Example: `/run ollama list`")
        return
    ctx.user_data["pending_shell"] = cmd
    await send(ctx, cid,
        f"🖥️ *Shell command requested:*\n```\n{cmd}\n```\n\nRun this on your Mac?",
        mkb(("✅ Yes, run it", "shell_yes"),
            ("❌ Cancel",      "shell_no")))

async def cmd_files(u, ctx):
    """List and offer to send files from the workspace — /files"""
    cid = str(u.effective_chat.id)
    loop = asyncio.get_running_loop()
    listing = await loop.run_in_executor(_exec, lambda: mt_list_workspace_files(cid))
    await send(ctx, cid,
        listing + "\n\n_Ask me to send any of them:_\n"
        "`send me the proposal`\n"
        "`send me the QA report`\n"
        "`send me the latest screenshot`")

async def cmd_upgrade(u, ctx):
    """/upgrade [capability] — teach Orion a new skill."""
    cid = str(u.effective_chat.id)
    capability = u.message.text.partition(" ")[2].strip()
    if not capability:
        plugins = list(_PLUGINS.keys()) or ["none yet"]
        await send(ctx, cid,
            "🔬 *Orion self-upgrade*\n\n"
            f"*Installed plugins:* {', '.join(f'`{p}`' for p in plugins)}\n\n"
            "Tell me what capability to add:\n"
            "`/upgrade web scraping`\n"
            "`/upgrade send email via smtp`\n"
            "`/upgrade read pdf files`\n"
            "`/upgrade control spotify via api`")
        return
    await orion_self_upgrade(ctx, cid, capability)

# Phrases indicating Orion acknowledges a capability gap — triggers self-upgrade offer
_CAP_GAP_PHRASES = [
    "i don't have the ability to","i cannot perform","i lack the capability",
    "this is beyond","i'm not equipped","requires tools i don't have",
    "i don't have a tool","i can't do that","i am unable to","unable to perform",
    "don't have access to that","not something i can","outside my capabilities",
]

def _has_capability_gap(text):
    ml = text.lower()
    return any(p in ml for p in _CAP_GAP_PHRASES)

# ── Main message handler ───────────────────────────────────────────────────────
async def on_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not u.message or not u.message.text: return
    msg = u.message.text.strip(); cid = str(u.effective_chat.id)
    # Send typing immediately so user sees response is coming
    try: await ctx.bot.send_chat_action(chat_id=int(cid), action="typing")
    except Exception: pass
    if ctx.user_data.get("manually_paused"):
        await send(ctx, cid, "⏸️ Agents paused. /resume to re-enable."); return
    proj = get_project(cid); status = proj.get("status","idle")
    if status == "paused":
        await send(ctx, cid, "⏸️ Project paused. /resume to continue."); return

    # Identity questions — answer directly, bypass model to avoid trained deflection
    identity_kw = [
        # Model / identity
        "which model","what model","what are you","who are you","what version",
        "running on","your model","what llm","which llm","are you gpt",
        "are you claude","are you gemini","are you chatgpt","are you openai",
        "are you alibaba","are you qwen","what ai are you","which ai",
        "what technology","what is your name","tell me about yourself",
        "what are you based on","which company made you","how were you trained",
        "what base model","which language model","what language model",
        "are you an ai","are you a bot","what kind of ai","built on what",
        "powered by","underlying model","your architecture",
        # Team / agents / capabilities
        "your team","my team","your agents","my agents","list your agents",
        "who are your agents","what agents do you have","who is on your team",
        "what can you do","what are your capabilities","your capabilities",
        "what do you have access to","what tools do you have",
        "what are your features","what can orion do","your skills",
        "tell me about your team","introduce your team","who helps you",
        "your specialists","your team members","the team",
        # System access
        "your system","do you have access","direct access","hardware access",
        "can you control my","can you access my","access to my mac",
        "access to my machine","access to my computer",
    ]
    if any(kw in msg.lower() for kw in identity_kw):
        await send(ctx, cid, _ORION_ID)
        return

    # File send requests — needs cid context so handled before generic tool detection
    file_request_kws = ["send me the proposal","send proposal","share proposal",
                        "send me the code","send the code","send leo",
                        "send qa report","send qa","send bug report",
                        "send me the report","send screenshot","send me screenshot",
                        "send latest screenshot","send me the latest"]
    if any(kw in msg.lower() for kw in file_request_kws):
        await send(ctx, cid, "📎 _Fetching file…_")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_exec, lambda: mt_find_file(msg, cid))
        await send_result(ctx, cid, result)
        return

    # Machine tool detection — runs before LLM, uses subprocess on this Mac
    tool_desc, tool_call, needs_approval = detect_tool(msg)
    if tool_call is not None:
        if not needs_approval:
            await send(ctx, cid, "⚙️ _Checking your machine…_")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(_exec, tool_call)
            await send_result(ctx, cid, result)
        else:
            ctx.user_data["pending_tool"] = {"desc": tool_desc, "call": tool_call}
            await send(ctx, cid,
                f"🖥️ *Machine action requested*\n\n_{tool_desc}_\n\n"
                "Run this on your Mac?",
                mkb(("✅ Yes, do it", "tool_yes"),
                    ("❌ Cancel",     "tool_no")))
        return
    if any(w in msg.lower() for w in ["pentest","hack ","security audit",
                                       "vulnerability scan","ctf ","bug bounty"]):
        ctx.user_data["pending_pentest"] = msg
        await send(ctx, cid,
            f"🛡️ *Cipher requested*\n\nTask: _{msg}_\n\n"
            "⚠️ Only test systems you *own* or have *written permission* to test.",
            mkb(("⚠️ Yes — I have permission, proceed", "cipher_yes"),
                ("❌ Cancel",                            "cipher_no"))); return

    # Trend keywords at idle
    if any(w in msg.lower() for w in ["trend","what should i build","inspire",
                                       "suggest project","what to build"]) \
            and status in ("idle","completed"):
        await cmd_trends(u, ctx); return

    # Active project — Orion handles mid-workflow messages but DON'T start new project
    if status not in ("idle","completed"):
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            proj = get_project(cid)
            history = _conv_get(cid)
            r = await invoke("orion", history + [{"role":"user","content":
                f"Current project: {proj.get('idea','?')} (status: {status})\n"
                f"User: {msg}\n\n"
                "Answer helpfully. If they're asking a general question, answer it directly. "
                "If they want to continue the project, guide them on next steps."}])
            r = _strip_think(r)
            _conv_add(cid, "user", msg)
            _conv_add(cid, "assistant", r)
            await send(ctx, cid, r)
        finally:
            stop.set()
        return

    # ── Routing — keyword-first, never routes to PROJECT accidentally ─────────
    # Only start a project when the user CLEARLY says "build me / create / make me X"
    proj_summary = "; ".join(
        f"{p.get('idea','?')} ({p.get('status','?')})"
        for p in load_projects().values()
    ) or "none"

    if _is_clear_project(msg):
        # Confirmed explicit build request — start workflow
        update_project(cid, status="proposal_drafting", idea=msg,
            created=str(datetime.datetime.now()), history=[], chat_id=cid)
        await send(ctx, cid, f"🚀 *Starting project:* _{msg}_\n\nAda & Mira drafting proposal…")
        await workflow(ctx, cid)
        return

    # Everything else → smart Q&A (Orion answers, consults Ada, or uses thinking)
    stop = asyncio.Event()
    ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
    try:
        history = _conv_get(cid)          # full conversation so far
        ctx_prefix = (f"[Active projects: {proj_summary}]\n" if proj_summary != "none" else "")

        if _needs_ada(msg):
            await send(ctx, cid, "🤔 _Let me get Ada's expert take on this…_")
            # Give Ada the conversation context too
            ada_msgs = history + [{"role":"user","content":
                f"{ctx_prefix}User asks: {msg}\n\n"
                "Answer directly as a senior product manager and technical advisor. "
                "Be specific and practical."}]
            ada_ans = await invoke("ada", ada_msgs)
            _conv_add(cid, "user", msg)
            _conv_add(cid, "assistant", ada_ans)
            await send(ctx, cid, f"📊 *Ada:*\n\n{ada_ans}")

        elif _needs_think(msg):
            msgs = history + [{"role":"user","content": ctx_prefix + msg}]
            ans = await invoke_think("orion", msgs)
            ans = _strip_think(ans)
            _conv_add(cid, "user", msg)
            _conv_add(cid, "assistant", ans)
            await send(ctx, cid, _ORION_ID if _is_deflecting(ans) else ans)

        else:
            msgs = history + [{"role":"user","content": ctx_prefix + msg}]

            # Web search — do it BEFORE calling Orion so he has real data
            if _needs_web_search(msg):
                live = await ctx.bot.send_message(
                    chat_id=int(cid), text="🔍 _Searching the web…_", parse_mode="Markdown")
                search_results = await asyncio.get_running_loop().run_in_executor(
                    _exec, lambda: _search_web_sync(msg, max_results=5))
                try: await live.delete()
                except Exception: pass
                msgs = history + [{"role":"user","content":
                    f"{ctx_prefix}[Web search results for: {msg}]\n\n"
                    f"{search_results}\n\n"
                    f"User question: {msg}\n\n"
                    "Answer using the search results above. Be specific and cite "
                    "sources (site name or URL) where helpful."}]

            ans = await invoke("orion", msgs)
            ans = _strip_think(ans)
            if _is_deflecting(ans):
                # LLM deflected — check if it was an internet search refusal
                if any(p in ans.lower() for p in [
                    "cannot perform internet","cannot access external",
                    "cannot search","no internet","cannot browse",
                    "cannot retrieve","cannot look up"
                ]):
                    # Do the search ourselves and answer with results
                    live = await ctx.bot.send_message(
                        chat_id=int(cid), text="🔍 _Searching the web…_", parse_mode="Markdown")
                    results = await asyncio.get_running_loop().run_in_executor(
                        _exec, lambda: _search_web_sync(msg, max_results=5))
                    try: await live.delete()
                    except Exception: pass
                    search_msgs = history + [{"role":"user","content":
                        f"[Web search results for: {msg}]\n\n{results}\n\n"
                        f"Answer the user's question: {msg}"}]
                    ans2 = _strip_think(await invoke("orion", search_msgs))
                    _conv_add(cid, "user", msg)
                    _conv_add(cid, "assistant", ans2)
                    await send(ctx, cid, ans2)
                # LLM deflected about identity/capabilities
                elif _is_about_orion(msg):
                    _conv_add(cid, "user", msg)
                    _conv_add(cid, "assistant", _ORION_ID)
                    await send(ctx, cid, _ORION_ID)
                else:
                    # Genuine external capability gap — offer to self-upgrade
                    _conv_add(cid, "user", msg)
                    _conv_add(cid, "assistant", ans)
                    await send(ctx, cid,
                        f"{ans}\n\n"
                        "🔬 _I can research and add this capability myself. Want me to?_",
                        mkb(("✅ Yes, learn it",  "self_upgrade_yes"),
                            ("❌ No thanks",      "self_upgrade_no")))
                    ctx.user_data["pending_upgrade_msg"] = msg
            else:
                _conv_add(cid, "user", msg)
                _conv_add(cid, "assistant", ans)
                await send(ctx, cid, ans)
    finally:
        stop.set()

# ── Button/callback handler ────────────────────────────────────────────────────
async def on_btn(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    cid = str(q.message.chat.id); data = q.data
    try: await q.edit_message_reply_markup(None)
    except Exception: pass

    if   data == "approve_proposal":
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
        update_project(cid, status="completed",
                        completed_at=str(datetime.datetime.now()))
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
        await send(ctx, cid,
            f"🛡️ *Cipher's Report:*\n\n{result[:1200]}"
            f"\n\n📁 Saved: `~/AI/reports/pentest_{ts}.md`")
        await send(ctx, cid, "📎 _Sending full pentest report…_")
        await send_result(ctx, cid, ("file", rpath, f"pentest_{ts}.md"))
    elif data == "cipher_no":
        ctx.user_data.pop("pending_pentest", None)
        await send(ctx, cid, "🛡️ Pentest cancelled.")

    # Machine tool approvals
    elif data == "tool_yes":
        pending = ctx.user_data.pop("pending_tool", {})
        call = pending.get("call")
        desc = pending.get("desc", "action")
        if call:
            await react_tool(ctx, cid, desc, call)
        else:
            await send(ctx, cid, "⚠️ Nothing to run — try again.")
    elif data == "tool_no":
        ctx.user_data.pop("pending_tool", None)
        await send(ctx, cid, "❌ Cancelled.")

    # Shell command approvals (/run)
    elif data == "shell_yes":
        cmd = ctx.user_data.pop("pending_shell", "")
        if not cmd:
            await send(ctx, cid, "No command found."); return
        await react_tool(ctx, cid, f"shell: {cmd}", lambda c=cmd: _run(c, timeout=60))
    elif data == "shell_no":
        ctx.user_data.pop("pending_shell", None)
        await send(ctx, cid, "❌ Command cancelled.")
    elif data == "start_from_trend":
        await send(ctx, cid, "Which idea do you want to build? Just describe it.")
    elif data == "dismiss_trend":
        await send(ctx, cid, "👍 No problem. Send an idea whenever you're ready!")

    # Plugin install approvals
    elif data == "plugin_yes":
        p = ctx.user_data.pop("pending_plugin", {})
        if not p:
            await send(ctx, cid, "Nothing pending."); return
        loop = asyncio.get_running_loop()
        live = await ctx.bot.send_message(chat_id=int(cid),
            text="📦 _Installing…_", parse_mode="Markdown")
        async def upd(t):
            try: await live.edit_text(t, parse_mode="Markdown")
            except: pass
        # Install package if needed
        pkg = p.get("package","")
        if pkg and pkg.lower() not in ("stdlib","none",""):
            await upd(f"📦 _Installing `{pkg}`…_")
            ok, out = await loop.run_in_executor(
                _exec, lambda: _run(
                    f"uv pip install --python {HOME}/.venv/bin/python {pkg}"))
            if not ok:
                await live.delete()
                await send(ctx, cid, f"❌ Package install failed:\n```\n{out[:400]}\n```")
                return
        # Save plugin file
        slug = re.sub(r'[^\w]','_',p.get("capability","plugin").lower())[:35]
        plugin_path = os.path.join(PLUGINS_DIR, f"{slug}.py")
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        with open(plugin_path, "w") as pf:
            pf.write(f"# Plugin: {p.get('capability')}\n"
                     f"# Installed: {datetime.datetime.now()}\n\n"
                     f"{p['code']}\n")
        await upd(f"💾 _Saved plugin, restarting…_")
        await asyncio.sleep(1)
        await live.delete()
        await self_restart(ctx, cid, f"added {p.get('fn_name','plugin')}")

    elif data == "plugin_no":
        ctx.user_data.pop("pending_plugin", None)
        await send(ctx, cid, "❌ Plugin install cancelled.")

    elif data == "self_upgrade_yes":
        msg_orig = ctx.user_data.pop("pending_upgrade_msg", "")
        if msg_orig:
            await orion_self_upgrade(ctx, cid, msg_orig)
        else:
            await send(ctx, cid, "Use `/upgrade <skill>` to teach me a specific capability.")
    elif data == "self_upgrade_no":
        ctx.user_data.pop("pending_upgrade_msg", None)
        await send(ctx, cid, "No problem. Use `/upgrade <capability>` anytime to add new skills.")
    else:
        await send(ctx, cid, f"Unknown action: {data}")

async def on_err(u, ctx): logger.error(f"Error: {ctx.error}", exc_info=True)

# ── Dashboard action poller ────────────────────────────────────────────────────
# Checks pending_actions.json every 3 seconds for actions triggered from the
# web dashboard (approve/reject buttons), then continues the workflow.
class _DashCtx:
    """Minimal context-like object for workflow() calls from the poller."""
    def __init__(self, application):
        self.bot         = application.bot
        self.application = application
        self.user_data   = {}

async def action_poller():
    global _app
    while True:
        await asyncio.sleep(3)
        try:
            if not os.path.exists(TRIGGER_FILE): continue
            with open(TRIGGER_FILE) as f: triggers = json.load(f)
            if not triggers: continue
            with open(TRIGGER_FILE, "w") as f: json.dump({}, f)  # clear immediately
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
    chmod +x "$AD/orchestrator.py"; ok "orchestrator.py written (all bugs fixed)."; }

    # ── trend_watcher.py — always regenerate to apply latest fixes ───────────
    {
    cat > "$AD/trend_watcher.py" <<'TRENDEOF'
#!/usr/bin/env python3
"""Vox — Daily Trend Watcher. Launched by launchd at configured time."""
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
            params={"q":"emerging tech trends software startup ideas 2026","format":"json"},
            timeout=10)
        return "\n".join(f"- {x.get('title','')}: {x.get('content','')[:200]}"
                         for x in r.json().get("results",[])[:6])
    except Exception as e: return f"Search unavailable: {e}"

def main():
    snippets = get_snippets()
    try:
        resp = client.chat.completions.create(
            model="vox",
            messages=[{"role":"system","content":VOX_SYS},
                      {"role":"user","content":
                       f"Today's tech news:\n{snippets}\n\n"
                       "Suggest 3 buildable ideas. Bold title, 2-sentence description, "
                       "why timely now, tech stack, complexity."}],
            temperature=0.9, max_tokens=800)
        text = resp.choices[0].message.content.strip()
    except Exception as e: text = f"Trend analysis failed: {e}"

    msg = f"📡 *Good morning! Vox's Daily Trends:*\n\n{text}"
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=15)
        print("Daily trend sent.")
    except Exception as e: print(f"Send failed: {e}", file=sys.stderr)

if __name__ == "__main__": main()
TRENDEOF
    chmod +x "$AD/trend_watcher.py"; ok "trend_watcher.py written."; }
}

# =============================================================================
#  PHASE 7 — LIVE DASHBOARD (Jira-style with per-project Kanban)
# =============================================================================
write_dashboard() {
    log "Live dashboard (:$PORT_DASHBOARD)"
    local DD="$WORKDIR/dashboard"; mkdir -p "$DD"
    cat > "$DD/app.py" <<'DASHEOF'
#!/usr/bin/env python3
"""AI Team Mission Control Dashboard — Jira-style with project detail, actions, and file browser."""
import os, json, datetime, subprocess, re, urllib.parse
import requests, psutil
from flask import Flask, jsonify, request
from dotenv import load_dotenv

HOME      = os.environ.get("AI_HOME",      os.path.expanduser("~/ai-workstation"))
WORKSPACE = os.environ.get("AI_WORKSPACE", os.path.join(os.path.expanduser("~"), "AI"))
load_dotenv(os.path.join(HOME, ".env"))

PF   = os.path.join(HOME, "projects.json")
SF   = os.path.join(HOME, "agent_status.json")
TF   = os.path.join(HOME, "pending_actions.json")   # trigger file for orchestrator
LF_URL = os.environ.get("LANGFUSE_HOST","http://localhost:3000")
LF_PK  = os.environ.get("LANGFUSE_PUBLIC_KEY","")
LF_SK  = os.environ.get("LANGFUSE_SECRET_KEY","")
P_OLLAMA    = os.environ.get("PORT_OLLAMA","11434")
P_GATEWAY   = os.environ.get("PORT_GATEWAY","4000")
P_OPENWEBUI = os.environ.get("PORT_OPENWEBUI","3001")
P_SEARXNG   = os.environ.get("PORT_SEARXNG","8888")
P_LANGFUSE  = os.environ.get("PORT_LANGFUSE","3000")
P_DASHBOARD = os.environ.get("PORT_DASHBOARD","8800")
P_PORTAINER = os.environ.get("PORT_PORTAINER","9001")

app = Flask(__name__)

# ── Static data ────────────────────────────────────────────────────────────────
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
    ("orion","🤖","Main Orchestrator","qwen3.6:35b-a3b","Orchestrator, AI advisor, self-upgrading. Delegates to team when needed."),
    ("ada","📊","PM / Product Owner","qwen2.5:72b","Proposals, user stories, final sign-off."),
    ("mira","🎨","UI/UX Designer","gemma4:26b","Multimodal — analyses images & wireframes."),
    ("leo","💻","Developer","qwen2.5-coder:72b","72B coding specialist. Any language, any stack."),
    ("nova","🔎","QA Tester","qwen2.5:72b","Writes & runs comprehensive tests."),
    ("cipher","🛡️","Pentester","qwen2.5:72b","72B security specialist. On-demand only."),
    ("vox","📡","Trend Watcher","qwen2.5:72b","Daily tech trends & project ideas."),
]
KANBAN = [
    ("proposal_drafting","📋 Proposal"),
    ("awaiting_approval","⏳ Pending Approval"),
    ("development","💻 Development"),
    ("qa_running","🔎 QA"),
    ("qa_bugs_found","🐛 Bugs Found"),
    ("final_review","📊 Final Review"),
    ("awaiting_final","⏳ Final Approval"),
    ("completed","✅ Done"),
]
STATE_LABELS = {
    "idle":              ("No project","#6b7280"),
    "proposal_drafting": ("Drafting proposal","#f59e0b"),
    "awaiting_approval": ("Awaiting approval","#3b82f6"),
    "development":       ("In development","#f59e0b"),
    "qa_running":        ("QA running","#f59e0b"),
    "qa_bugs_found":     ("Bugs found","#ef4444"),
    "final_review":      ("Final review","#f59e0b"),
    "awaiting_final":    ("Awaiting final approval","#3b82f6"),
    "completed":         ("Completed ✅","#22c55e"),
    "paused":            ("Paused","#8b5cf6"),
}
# Actions available per state: (label, action_key, color)
STATE_ACTIONS = {
    "awaiting_approval": [
        ("✅ Approve — start development","approve_proposal","green"),
        ("🔄 Request changes","revise_proposal","yellow"),
        ("❌ Reject idea","reject_proposal","red"),
    ],
    "development": [
        ("✅ Mark deployed → QA","force_qa","green"),
    ],
    "qa_bugs_found": [
        ("🛠️ Fix bugs — reactivate Leo","fix_bugs","yellow"),
        ("⚠️ Accept as-is","accept_bugs","orange"),
    ],
    "awaiting_final": [
        ("🎉 Accept & complete project","accept_final","green"),
        ("🔄 More changes needed","more_changes","yellow"),
    ],
}
ACTION_STATUS = {
    "approve_proposal":"development","revise_proposal":"idle","reject_proposal":"idle",
    "force_qa":"qa_running","fix_bugs":"development","accept_bugs":"final_review",
    "accept_final":"completed","more_changes":"qa_running",
}
MODEL_META = [
    ("qwen3.6:35b-a3b","Orchestration","Orion. Smart orchestrator, self-upgrading, MoE 35B/3.5B active (~26 GB)."),
    ("qwen2.5-coder:72b","Coding","Leo. 72B coding specialist. Best for complex code (~44 GB)."),
    ("qwen3-coder","Coding","Leo + Cipher. Specialized coding & pentesting."),
    ("qwen2.5:72b","Reasoning","Ada + Nova + Vox. Richest reasoning & proposals (~44 GB)."),
    ("qwen2.5","Reasoning","Strong reasoning and document writing."),
    ("qwen3.6:27b","Coding","Leo Manual. Dense coder for VS Code + Continue (~22 GB)."),
    ("qwen3.6","Coding","Dense coder for VS Code + Continue."),
    ("qwen3","Reasoning","General reasoning and Q&A."),
    ("gemma4:26b","Vision/Design","Mira. Multimodal — analyses images & wireframes (~18 GB)."),
    ("gemma4","Vision/Design","Mira. Multimodal vision model."),
    ("nomic-embed-text","Embeddings","RAG, semantic search, memory. Not for chat (~270 MB)."),
    ("nomic-embed","Embeddings","Embeddings for RAG and semantic search."),
]
GROUP_ORDER = {"Orchestration":0,"Coding":1,"Reasoning":2,"Vision/Design":3,"Embeddings":4,"Other":5}
PROJECT_PORTS = {11434,4000,8800,3001,8888,3000,9001}
WELL_KNOWN_PORTS = {
    22:"SSH",53:"DNS",80:"HTTP",443:"HTTPS",548:"AFP File Sharing",
    631:"CUPS Printing",3306:"MySQL",5000:"Flask / AirPlay",5001:"AirPlay",
    5432:"PostgreSQL",5900:"VNC",6379:"Redis",7000:"AirPlay / Plex",
    8080:"HTTP Alt",8123:"ClickHouse HTTP",8443:"HTTPS Alt",
    9000:"ClickHouse Native",9090:"Prometheus",27017:"MongoDB",
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def probe(url):
    try: return requests.get(url,timeout=3).status_code < 500
    except: return False

def fmt_gb(gb):
    return f"{gb/1000:.1f} TB" if gb>=1000 else f"{gb:.0f} GB"

def hardware_info():
    vm = psutil.virtual_memory()
    disk = None
    for p in ("/System/Volumes/Data",os.path.expanduser("~"),"/"):
        try: disk=psutil.disk_usage(p); break
        except: continue
    bat=None
    try:
        b=psutil.sensors_battery()
        if b: bat={"percent":round(b.percent),"charging":bool(b.power_plugged)}
    except: pass
    return {
        "cpu":{"pct":round(psutil.cpu_percent(interval=None))},
        "ram":{"pct":round(vm.percent),"detail":f"{fmt_gb(vm.used/1e9)} / {fmt_gb(vm.total/1e9)}"},
        "storage":{"pct":round(disk.percent) if disk else 0,
                   "detail":f"{fmt_gb(disk.used/1e9)} / {fmt_gb(disk.total/1e9)}" if disk else "?"},
        "battery":bat,
    }

def get_models():
    try:
        r=requests.get(f"http://localhost:{P_OLLAMA}/api/tags",timeout=4)
        out=[]
        for m in r.json().get("models",[]):
            name=m.get("name",""); size=m.get("size",0)
            group,desc="Other","General purpose model."
            for prefix,g,d in MODEL_META:
                if name==prefix or name.startswith(prefix.split(":")[0]+":") \
                        or (":"  not in prefix and name.startswith(prefix)):
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
            svc=WELL_KNOWN_PORTS.get(port,f"Unknown — {parts[0]}")
            extra.append({"port":port,"service":svc,"process":parts[0]})
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
        return [{"name":t.get("name") or "(trace)",
                 "time":(t.get("timestamp") or "")[:19].replace("T"," "),
                 "latency":round(t.get("latency") or 0,2)}
                for t in r.json().get("data",[])]
    except: return []

def safe_name(text,max_len=45):
    n=re.sub(r'[^\w\s-]','',str(text).lower())
    n=re.sub(r'\s+','_',n.strip())
    return n[:max_len] or "untitled"

def list_project_files(idea):
    pname=safe_name(idea)
    files=[]
    for folder,cat in [
        (os.path.join(WORKSPACE,"proposals",pname),"Proposals & Design"),
        (os.path.join(WORKSPACE,"projects",pname),"Code & Reports"),
    ]:
        if os.path.exists(folder):
            for fname in sorted(os.listdir(folder)):
                fpath=os.path.join(folder,fname)
                if os.path.isfile(fpath):
                    st=os.stat(fpath)
                    files.append({"name":fname,"category":cat,"path":fpath,
                        "size":st.st_size,
                        "modified":datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return files

def enrich_project(cid,proj):
    st=proj.get("status","idle")
    label,color=STATE_LABELS.get(st,(st,"#6b7280"))
    proj["state_label"]=label; proj["state_color"]=color
    proj["actions"]=STATE_ACTIONS.get(st,[])
    proj["cid"]=cid
    return proj

# ── API endpoints ──────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    svcs=[{"name":n,"url":f"http://localhost:{p}/","port":int(p),"purpose":pu,"ok":probe(h)}
          for n,h,p,pu in SERVICES]
    ag_raw=load_json(SF,{})
    agents=[{"id":aid,"icon":ic,"role":role,"model":model,"desc":desc,"status":ag_raw.get(aid,"idle")}
            for aid,ic,role,model,desc in AGENTS]
    projects=load_json(PF,{})
    for cid,p in projects.items(): enrich_project(cid,p)
    return jsonify({
        "services":svcs,"agents":agents,"projects":projects,
        "hardware":hardware_info(),"models":get_models(),
        "extra_ports":scan_ports(),"traces":get_traces(),
        "kanban":[{"status":s,"label":l} for s,l in KANBAN],
        "updated":datetime.datetime.now().strftime("%H:%M:%S"),
    })

@app.route("/api/project/<cid>", methods=["DELETE"])
def api_delete_project(cid):
    import shutil
    projects=load_json(PF,{})
    if cid not in projects:
        return jsonify({"error":"Not found"}),404
    proj=projects.pop(cid)
    with open(PF,"w") as f: json.dump(projects,f,indent=2,default=str)
    os.chmod(PF,0o600)
    # Delete all generated files from workspace
    deleted=[]
    idea=proj.get("idea","")
    if idea:
        pname=safe_name(idea)
        for folder in [
            os.path.join(WORKSPACE,"proposals",pname),
            os.path.join(WORKSPACE,"projects",pname),
        ]:
            if os.path.exists(folder):
                shutil.rmtree(folder); deleted.append(folder)
    return jsonify({"ok":True,"deleted":deleted})

@app.route("/api/project/<cid>")
def api_project_detail(cid):
    projects=load_json(PF,{})
    proj=projects.get(cid,{})
    if not proj: return jsonify({"error":"Not found"}),404
    enrich_project(cid,proj)
    proj["files"]=list_project_files(proj.get("idea",""))
    # Build activity from history
    activity=[]
    for i,h in enumerate(proj.get("history",[])):
        if h.get("role")=="assistant":
            activity.append({"idx":i,"content":h["content"][:300]+"…" if len(h["content"])>300 else h["content"]})
    proj["activity"]=activity
    return jsonify(proj)

@app.route("/api/project/<cid>/action",methods=["POST"])
def api_project_action(cid):
    data=request.get_json(silent=True) or {}
    action=data.get("action","")
    new_status=ACTION_STATUS.get(action)
    if not new_status:
        return jsonify({"error":f"Unknown action: {action}"}),400
    projects=load_json(PF,{})
    if cid not in projects:
        return jsonify({"error":"Project not found"}),404
    proj=projects[cid]
    proj["status"]=new_status
    if action=="accept_final":
        proj["completed_at"]=str(datetime.datetime.now())
    proj.setdefault("events",[]).append({
        "time":str(datetime.datetime.now())[:19],
        "source":"dashboard","action":action,"new_status":new_status,
    })
    projects[cid]=proj
    with open(PF,"w") as f: json.dump(projects,f,indent=2,default=str)
    os.chmod(PF,0o600)
    # Signal orchestrator to continue workflow
    try:
        triggers=load_json(TF,{})
        triggers[cid]={"action":action,"time":str(datetime.datetime.now())}
        with open(TF,"w") as f: json.dump(triggers,f)
    except Exception as e:
        app.logger.warning(f"Could not write trigger: {e}")
    return jsonify({"ok":True,"new_status":new_status})

@app.route("/api/file")
def api_file():
    path=request.args.get("path","")
    if not path.startswith(WORKSPACE):
        return jsonify({"error":"Access denied"}),403
    if not os.path.isfile(path):
        return jsonify({"error":"File not found"}),404
    try:
        with open(path,errors="replace") as f: content=f.read(100000)
        return jsonify({"content":content,"name":os.path.basename(path)})
    except Exception as e:
        return jsonify({"error":str(e)}),500

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Team — Mission Control</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#21262d;--text:#e6edf3;--muted:#8b949e;
     --accent:#4c8dff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#a371f7;
     --orange:#f97316}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;height:100vh;display:flex;flex-direction:column}
a{color:var(--accent);text-decoration:none}
code{font-family:ui-monospace,monospace;font-size:12px}
/* Topbar */
.topbar{background:var(--panel);border-bottom:1px solid var(--border);padding:0 20px;height:52px;display:flex;align-items:center;justify-content:space-between;flex:none}
.logo{font-weight:700;font-size:18px;display:flex;align-items:center;gap:8px}
.logo span{color:var(--accent)}
.topbar-r{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:12px}
.live{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
/* Tabs */
.tabs{display:flex;padding:0 20px;background:var(--panel);border-bottom:1px solid var(--border);flex:none}
.tab{padding:12px 16px;cursor:pointer;color:var(--muted);font-size:13px;border-bottom:2px solid transparent;transition:.2s}
.tab:hover{color:var(--text)}.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
/* Pages */
.pg{display:none;flex:1;overflow:hidden}.pg.active{display:flex}
/* Overview/Agents/Activity pages */
.pg-scroll{display:none;padding:20px;max-width:1400px;margin:0 auto;width:100%;overflow-y:auto}
.pg-scroll.active{display:block}
/* Projects page — split layout */
.pg-projects{display:none;flex:1;overflow:hidden}
.pg-projects.active{display:flex}
.proj-sidebar{width:280px;border-right:1px solid var(--border);display:flex;flex-direction:column;flex:none;overflow:hidden}
.proj-sidebar-hd{padding:14px 16px;border-bottom:1px solid var(--border);font-weight:600;font-size:13px;display:flex;align-items:center;justify-content:space-between}
.proj-search{padding:10px 12px;border-bottom:1px solid var(--border)}
.proj-search input{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:12px;outline:none}
.proj-search input:focus{border-color:var(--accent)}
.proj-list{flex:1;overflow-y:auto}
.proj-item{padding:12px 16px;cursor:pointer;border-bottom:1px solid var(--border);transition:.15s}
.proj-item:hover{background:rgba(255,255,255,.03)}
.proj-item.active{background:rgba(76,141,255,.1);border-left:3px solid var(--accent)}
.proj-item-title{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.proj-item-meta{font-size:11px;color:var(--muted);margin-top:3px;display:flex;align-items:center;gap:6px}
.status-dot{width:7px;height:7px;border-radius:50%;flex:none}
/* Project detail */
.proj-detail{flex:1;display:flex;flex-direction:column;overflow:hidden}
.proj-detail-empty{flex:1;display:flex;align-items:center;justify-content:center;color:var(--muted);flex-direction:column;gap:12px}
.proj-detail-hd{padding:16px 20px;border-bottom:1px solid var(--border);flex:none}
.proj-detail-title{font-size:20px;font-weight:700;margin-bottom:6px}
.proj-detail-meta{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--muted);flex-wrap:wrap}
.status-badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
/* Detail tabs */
.detail-tabs{display:flex;padding:0 20px;border-bottom:1px solid var(--border);flex:none;background:var(--panel)}
.detail-tab{padding:10px 14px;cursor:pointer;color:var(--muted);font-size:13px;border-bottom:2px solid transparent}
.detail-tab:hover{color:var(--text)}.detail-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.detail-content{flex:1;overflow-y:auto;padding:20px}
/* Action buttons */
.action-bar{padding:12px 20px;border-top:1px solid var(--border);background:var(--panel);display:flex;gap:10px;flex-wrap:wrap;flex:none}
.btn{padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:600;transition:.2s}
.btn-green{background:rgba(63,185,80,.15);color:var(--green);border:1px solid rgba(63,185,80,.3)}
.btn-green:hover{background:rgba(63,185,80,.25)}
.btn-yellow{background:rgba(210,153,34,.15);color:var(--yellow);border:1px solid rgba(210,153,34,.3)}
.btn-yellow:hover{background:rgba(210,153,34,.25)}
.btn-red{background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.3)}
.btn-red:hover{background:rgba(248,81,73,.25)}
.btn-orange{background:rgba(249,115,22,.15);color:var(--orange);border:1px solid rgba(249,115,22,.3)}
.btn-orange:hover{background:rgba(249,115,22,.25)}
/* Kanban */
.kanban{display:flex;gap:10px;padding-bottom:10px}
.kol{flex:0 0 165px;background:rgba(255,255,255,.02);border-radius:8px;padding:10px;border:1px solid var(--border)}
.kol.active-col{border-color:var(--yellow)}.kol-hd{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px}
.kol.active-col .kol-hd{color:var(--yellow)}
.kcard{background:var(--panel);border:1px solid var(--border);border-radius:5px;padding:8px;font-size:12px}
.kcard-t{font-weight:600;margin-bottom:3px}
/* Timeline */
.timeline{position:relative;padding-left:20px}
.timeline::before{content:'';position:absolute;left:6px;top:0;bottom:0;width:2px;background:var(--border)}
.tl-item{position:relative;margin-bottom:16px;padding-left:16px}
.tl-dot{position:absolute;left:-14px;top:4px;width:10px;height:10px;border-radius:50%;background:var(--accent);border:2px solid var(--bg)}
.tl-time{font-size:10px;color:var(--muted);margin-bottom:3px}
.tl-text{font-size:12px;line-height:1.5;color:var(--text)}
/* Files */
.file-list{display:flex;flex-direction:column;gap:6px}
.file-item{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--panel);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:.15s}
.file-item:hover{border-color:var(--accent)}
.file-icon{font-size:18px;flex:none}
.file-name{font-weight:600;font-size:13px}
.file-meta{font-size:11px;color:var(--muted);margin-top:2px}
.file-cat{font-size:10px;padding:2px 6px;border-radius:10px;background:rgba(76,141,255,.15);color:var(--accent)}
/* File viewer */
.file-viewer{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;font-family:ui-monospace,monospace;font-size:12px;line-height:1.6;white-space:pre-wrap;overflow:auto;max-height:500px}
/* General */
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
.ga{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
.sec{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin:20px 0 12px}
.hw-lbl{font-size:12px;color:var(--muted)}.hw-val{font-size:24px;font-weight:700;margin:4px 0 2px}
.hw-detail{font-size:11px;color:var(--muted);margin-bottom:6px}
.bar{height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.bf{height:100%;border-radius:2px;transition:.5s}
.bg{background:var(--green)}.by{background:var(--yellow)}.br{background:var(--red)}
.svc-row{display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.dot-up{background:var(--green);box-shadow:0 0 6px var(--green)}.dot-dn{background:var(--red)}
.svc-nm{font-weight:600;font-size:13px}
.port{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:1px 5px;margin-left:4px}
.svc-pu{font-size:11px;color:var(--muted);margin-top:2px}
.sst{margin-left:auto;font-size:11px;font-weight:600;text-transform:uppercase}
.up{color:var(--green)}.dn{color:var(--red)}
.ag-card{display:flex;flex-direction:column;align-items:center;text-align:center;padding:20px 12px;position:relative}
.av{width:52px;height:52px;border-radius:50%;background:var(--bg);border:2px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:24px;margin-bottom:10px;transition:.3s}
.av.working{border-color:var(--yellow);animation:glow 1s infinite alternate}
@keyframes glow{from{box-shadow:0 0 4px var(--yellow)}to{box-shadow:0 0 16px var(--yellow)}}
.ag-name{font-weight:700;font-size:15px}.ag-role{font-size:11px;color:var(--muted);margin:2px 0 5px}
.ag-model{font-family:ui-monospace,monospace;font-size:10px;color:var(--muted);background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:2px 6px;margin-bottom:6px}
.ag-desc{font-size:11px;color:var(--muted);line-height:1.4}
.pill{position:absolute;top:10px;right:10px;padding:3px 8px;border-radius:20px;font-size:10px;font-weight:600;text-transform:uppercase}
.p-idle{background:rgba(63,185,80,.1);color:var(--green)}
.p-work{background:rgba(210,153,34,.15);color:var(--yellow)}
.group-hd td{background:rgba(76,141,255,.08);color:var(--accent);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.7px;padding:6px 12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--border)}
th{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
tr:last-child td{border:none}
.empty{color:var(--muted);padding:20px;text-align:center;font-size:13px}
.toast{position:fixed;bottom:20px;right:20px;background:#1e2d3d;border:1px solid var(--accent);border-radius:8px;padding:12px 16px;font-size:13px;z-index:999;display:none;animation:fadeIn .3s}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:768px){.g4{grid-template-columns:repeat(2,1fr)}.proj-sidebar{width:220px}}
</style></head><body>
<div class="topbar">
  <div class="logo">🤖 <span>AI</span> Team — Mission Control</div>
  <div class="topbar-r"><span class="live"></span><span id="upd">—</span></div>
</div>
<div class="tabs">
  <div class="tab active" onclick="showPg('overview',this)">Overview</div>
  <div class="tab" onclick="showPg('agents',this)">Agents</div>
  <div class="tab" onclick="showPg('projects',this)">Projects</div>
  <div class="tab" onclick="showPg('activity',this)">Activity</div>
</div>
<div class="toast" id="toast"></div>

<!-- OVERVIEW -->
<div id="pg-overview" class="pg-scroll active">
  <p class="sec">Hardware</p>
  <div class="g4">
    <div class="card"><div class="hw-lbl">CPU</div><div class="hw-val" id="hw-cpu">—</div><div class="hw-detail" id="hw-cpu-d">All cores</div><div class="bar"><div class="bf" id="b-cpu" style="width:0%"></div></div></div>
    <div class="card"><div class="hw-lbl">RAM</div><div class="hw-val" id="hw-ram">—</div><div class="hw-detail" id="hw-ram-d">—</div><div class="bar"><div class="bf" id="b-ram" style="width:0%"></div></div></div>
    <div class="card"><div class="hw-lbl">Storage</div><div class="hw-val" id="hw-sto">—</div><div class="hw-detail" id="hw-sto-d">—</div><div class="bar"><div class="bf" id="b-sto" style="width:0%"></div></div></div>
    <div class="card"><div class="hw-lbl">Battery</div><div class="hw-val" id="hw-bat">—</div><div class="hw-detail" id="hw-bat-d">—</div><div class="bar"><div class="bf" id="b-bat" style="width:0%"></div></div></div>
  </div>
  <p class="sec">Services</p>
  <div class="g3" id="svcs"></div>
  <p class="sec">Local Models — grouped by specialty</p>
  <div class="card" style="padding:0;overflow:hidden">
    <table><thead><tr><th>Model</th><th>Size</th><th>Description</th></tr></thead>
    <tbody id="models-body"><tr><td colspan="3" class="empty">Loading…</td></tr></tbody></table>
  </div>
  <p class="sec">Additional Open Ports</p>
  <div class="card" style="padding:0;overflow:hidden">
    <table><thead><tr><th>Port</th><th>Process</th><th>Likely Service</th></tr></thead>
    <tbody id="ports-body"><tr><td colspan="3" class="empty">Scanning…</td></tr></tbody></table>
  </div>
</div>

<!-- AGENTS -->
<div id="pg-agents" class="pg-scroll">
  <p class="sec">AI Team Members</p>
  <div class="ga" id="agents"></div>
</div>

<!-- PROJECTS — Jira-style split layout -->
<div id="pg-projects" class="pg-projects">
  <div class="proj-sidebar">
    <div class="proj-sidebar-hd">
      <span>Projects</span>
      <span id="proj-count" style="font-size:11px;color:var(--muted)"></span>
    </div>
    <div class="proj-search"><input id="proj-filter" placeholder="🔍 Filter projects…" oninput="filterProjects()"></div>
    <div class="proj-list" id="proj-list"><div class="empty">No projects yet.<br>Send an idea to Telegram.</div></div>
  </div>
  <div class="proj-detail" id="proj-detail">
    <div class="proj-detail-empty">
      <div style="font-size:40px">📋</div>
      <div style="font-size:16px;font-weight:600">Select a project</div>
      <div style="color:var(--muted)">Choose a project from the list to see details</div>
    </div>
  </div>
</div>

<!-- ACTIVITY -->
<div id="pg-activity" class="pg-scroll">
  <p class="sec">Recent Agent Calls (Langfuse)</p>
  <div class="card" style="padding:0;overflow:hidden">
    <table><thead><tr><th>Time</th><th>Agent / Call</th><th>Latency (s)</th></tr></thead>
    <tbody id="traces"></tbody></table>
  </div>
</div>

<script>
let _d={}, _selCid=null, _selTab='overview', _allProjects={}, _selDetailTab='overview';

function showPg(id,el){
  document.querySelectorAll('.pg-scroll,.pg-projects').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('pg-'+id).classList.add('active');
  el.classList.add('active');
  if(id==='projects') renderProjectsList();
}

function barcls(p){return p<60?'bg':p<85?'by':'br';}

async function tick(){
  try{
    _d=await(await fetch('/api/status')).json();
    document.getElementById('upd').textContent='Updated '+(_d.updated||'');
    _allProjects=_d.projects||{};
    renderHardware(_d.hardware);
    renderServices(_d.services);
    renderModels(_d.models);
    renderPorts(_d.extra_ports);
    renderAgents(_d.agents);
    renderProjectsList();
    renderTraces(_d.traces);
    // Refresh selected project if open
    if(_selCid) renderDetail(_selCid);
  }catch(e){console.warn('tick',e);}
}

function renderHardware(hw){
  if(!hw) return;
  const cpu=hw.cpu||{},ram=hw.ram||{},sto=hw.storage||{};
  document.getElementById('hw-cpu').textContent=(cpu.pct||0)+'%';
  setBar('b-cpu',cpu.pct||0);
  document.getElementById('hw-ram').textContent=(ram.pct||0)+'%';
  document.getElementById('hw-ram-d').textContent=ram.detail||'';
  setBar('b-ram',ram.pct||0);
  document.getElementById('hw-sto').textContent=(sto.pct||0)+'%';
  document.getElementById('hw-sto-d').textContent=sto.detail||'';
  setBar('b-sto',sto.pct||0);
  const bat=hw.battery;
  document.getElementById('hw-bat').textContent=bat?(bat.percent+'%'+(bat.charging?' ⚡':'')):'N/A';
  document.getElementById('hw-bat-d').textContent=bat?(bat.charging?'Charging':'On battery'):'No sensor';
  if(bat)setBar('b-bat',bat.percent);
}
function setBar(id,p){const b=document.getElementById(id);if(b){b.style.width=p+'%';b.className='bf '+barcls(p);}}

function renderServices(svcs){
  if(!svcs)return;
  const host=window.location.hostname;
  document.getElementById('svcs').innerHTML=svcs.map(s=>`
    <div class="card"><div class="svc-row">
      <div class="dot ${s.ok?'dot-up':'dot-dn'}"></div>
      <div style="flex:1"><div><span class="svc-nm">${s.name}</span><span class="port">:${s.port}</span></div>
        <div class="svc-pu">${s.purpose}</div>
        <div style="margin-top:5px"><a href="http://${host}:${s.port}/" target="_blank">Open ↗</a></div>
      </div>
      <div class="sst ${s.ok?'up':'dn'}">${s.ok?'LIVE':'DOWN'}</div>
    </div></div>`).join('');
}

function renderModels(models){
  const el=document.getElementById('models-body');
  if(!models||!models.length){el.innerHTML='<tr><td colspan="3" class="empty">Ollama not reachable.</td></tr>';return;}
  const GC={'Orchestration':'#4c8dff','Coding':'#3fb950','Reasoning':'#a371f7','Vision/Design':'#f59e0b','Embeddings':'#8b949e'};
  let html='',last='';
  models.forEach(m=>{
    if(m.group!==last){html+=`<tr class="group-hd"><td colspan="3">${m.group}</td></tr>`;last=m.group;}
    html+=`<tr><td><code>${m.name}</code></td><td style="color:var(--muted);white-space:nowrap">${m.size}</td><td style="color:var(--muted)">${m.desc}</td></tr>`;
  });
  el.innerHTML=html;
}

function renderPorts(ports){
  const el=document.getElementById('ports-body');
  if(!ports||!ports.length){el.innerHTML='<tr><td colspan="3" class="empty">No additional ports detected.</td></tr>';return;}
  el.innerHTML=ports.map(p=>`<tr><td><code>${p.port}</code></td><td style="color:var(--muted)">${p.process}</td><td style="color:var(--muted)">${p.service}</td></tr>`).join('');
}

function renderAgents(agents){
  if(!agents)return;
  document.getElementById('agents').innerHTML=agents.map(a=>`
    <div class="card ag-card">
      <span class="pill ${a.status==='working'?'p-work':'p-idle'}">${a.status}</span>
      <div class="av ${a.status==='working'?'working':''}">${a.icon}</div>
      <div class="ag-name">${a.id.charAt(0).toUpperCase()+a.id.slice(1)}</div>
      <div class="ag-role">${a.role}</div>
      <div class="ag-model"><code>${a.model}</code></div>
      <div class="ag-desc">${a.desc}</div>
    </div>`).join('');
}

// ── Projects ─────────────────────────────────────────────────────────────────
function filterProjects(){
  const q=document.getElementById('proj-filter').value.toLowerCase();
  document.querySelectorAll('.proj-item').forEach(el=>{
    el.style.display=el.dataset.idea.includes(q)?'':'none';
  });
}

function renderProjectsList(){
  const list=document.getElementById('proj-list');
  const projs=Object.entries(_allProjects);
  document.getElementById('proj-count').textContent=projs.length;
  if(!projs.length){list.innerHTML='<div class="empty">No projects yet.<br>Send an idea to Telegram.</div>';return;}
  list.innerHTML=projs
    .sort((a,b)=>(b[1].created||'').localeCompare(a[1].created||''))
    .map(([cid,p])=>{
      const color=p.state_color||'#6b7280';
      return `<div class="proj-item ${cid===_selCid?'active':''}" 
        data-cid="${cid}" data-idea="${(p.idea||'').toLowerCase()}"
        onclick="selectProject('${cid}')">
        <div class="proj-item-title">${p.idea||'Unnamed project'}</div>
        <div class="proj-item-meta">
          <span class="status-dot" style="background:${color}"></span>
          <span>${p.state_label||p.status||'unknown'}</span>
          <span>·</span>
          <span>${(p.created||'').substring(0,10)}</span>
        </div>
      </div>`;
    }).join('');
}

async function selectProject(cid){
  _selCid=cid;
  _selDetailTab='overview';  // reset to overview when switching projects
  renderProjectsList();
  await renderDetail(cid);
}

async function deleteProject(cid,title){
  if(!confirm(`Delete "${title}"?\n\nThis will permanently remove:\n• The project record\n• All files in ~/AI/proposals and ~/AI/projects\n\nThis cannot be undone.`))return;
  try{
    const r=await fetch(`/api/project/${cid}`,{method:'DELETE'});
    const d=await r.json();
    if(d.ok){
      showToast('🗑️ Project deleted','green');
      _selCid=null;
      _selDetailTab='overview';
      document.getElementById('proj-detail').innerHTML=`
        <div class="proj-detail-empty">
          <div style="font-size:40px">📋</div>
          <div style="font-size:16px;font-weight:600">Select a project</div>
          <div style="color:var(--muted)">Choose a project from the list to see details</div>
        </div>`;
      await tick();
    }else{
      showToast('Error: '+(d.error||'unknown'),'red');
    }
  }catch(e){showToast('Delete failed: '+e,'red');}
}

async function renderDetail(cid){
  const container=document.getElementById('proj-detail');
  try{
    const proj=await(await fetch(`/api/project/${cid}`)).json();
    const actions=proj.actions||[];
    const actionBtns=actions.map(([label,act,cls])=>
      `<button class="btn btn-${cls}" onclick="doAction('${cid}','${act}','${label}')">${label}</button>`
    ).join('');

    // Build tab bar — restore whichever tab was active before refresh
    const tabs=[['overview','Overview'],['activity','Activity'],['documents','Documents'],['board','Board']];
    const tabHtml=tabs.map(([t,l])=>
      `<div class="detail-tab ${t===_selDetailTab?'active':''}" onclick="showDetailTab('${t}',this,event)">${l}</div>`
    ).join('');

    // Render the correct tab content (not always overview)
    const tabRenders={
      overview:()=>renderDetailOverview(proj),
      activity:()=>renderDetailActivity(proj),
      documents:()=>renderDetailDocuments(proj),
      board:()=>renderDetailBoard(proj),
    };
    const bodyHtml=(tabRenders[_selDetailTab]||tabRenders.overview)();

    container.innerHTML=`
      <div class="proj-detail-hd">
        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px">
          <div>
            <div class="proj-detail-title">${proj.idea||'Unknown project'}</div>
            <div class="proj-detail-meta">
              <span class="status-badge" style="background:${proj.state_color}22;color:${proj.state_color}">${proj.state_label}</span>
              <span>📅 Created ${(proj.created||'').substring(0,10)}</span>
              ${proj.completed_at?`<span>✅ Completed ${(proj.completed_at||'').substring(0,10)}</span>`:''}
            </div>
          </div>
          <button class="btn btn-red" style="flex:none;margin-top:4px" onclick="deleteProject('${cid}','${(proj.idea||'').replace(/'/g,'\\\'').substring(0,40)}')">🗑️ Delete</button>
        </div>
      </div>
      <div class="detail-tabs">${tabHtml}</div>
      <div class="detail-content" id="detail-body">${bodyHtml}</div>
      ${actions.length?`<div class="action-bar">${actionBtns}</div>`:''}
    `;
    container.dataset.proj=JSON.stringify(proj);
  }catch(e){
    container.innerHTML=`<div class="proj-detail-empty"><div>⚠️ Could not load project</div><div style="color:var(--muted)">${e}</div></div>`;
  }
}

function renderDetailOverview(proj){
  const kanban=(_d.kanban||[]).map(col=>{
    const active=col.status===proj.status;
    return `<div class="kol ${active?'active-col':''}">
      <div class="kol-hd">${col.label}</div>
      ${active?`<div class="kcard"><div class="kcard-t">${(proj.idea||'').substring(0,30)}</div></div>`:''}
    </div>`;
  }).join('');
  const hist=proj.history||[];
  const summary=hist.length?`${hist.length} messages exchanged`:'No activity yet';
  return `
    <div style="margin-bottom:16px">
      <div class="sec" style="margin-top:0">Workflow Position</div>
      <div style="overflow-x:auto"><div class="kanban">${kanban}</div></div>
    </div>
    <div class="g3" style="margin-top:16px">
      <div class="card"><div style="font-size:11px;color:var(--muted)">Messages</div><div style="font-size:22px;font-weight:700;margin-top:4px">${hist.length}</div></div>
      <div class="card"><div style="font-size:11px;color:var(--muted)">Status</div><div style="font-size:14px;font-weight:600;margin-top:4px;color:${proj.state_color}">${proj.state_label}</div></div>
      <div class="card"><div style="font-size:11px;color:var(--muted)">Started</div><div style="font-size:13px;font-weight:600;margin-top:4px">${(proj.created||'').substring(0,16)}</div></div>
    </div>`;
}

function renderDetailActivity(proj){
  const hist=proj.history||[];
  if(!hist.length) return '<div class="empty">No activity yet.</div>';
  const agentIcons={ada:'📊',mira:'🎨',leo:'💻',nova:'🔎',cipher:'🛡️',vox:'📡',orion:'🤖',user:'👤'};
  const items=hist.map((h,i)=>{
    const role=h.role==='assistant'?'🤖 Agent':'👤 You';
    const preview=(h.content||'').substring(0,200)+(h.content&&h.content.length>200?'…':'');
    return `<div class="tl-item">
      <div class="tl-dot"></div>
      <div class="tl-time">${role} · message ${i+1}</div>
      <div class="tl-text">${preview.replace(/</g,'&lt;')}</div>
    </div>`;
  }).join('');
  return `<div class="timeline">${items}</div>`;
}

function renderDetailDocuments(proj){
  const files=proj.files||[];
  if(!files.length) return '<div class="empty">No files generated yet.<br>Files appear here after Leo builds the project.</div>';
  const cats=[...new Set(files.map(f=>f.category))];
  return cats.map(cat=>`
    <div class="sec">${cat}</div>
    <div class="file-list">
      ${files.filter(f=>f.category===cat).map(f=>`
        <div class="file-item" onclick="viewFile('${encodeURIComponent(f.path)}','${f.name}')">
          <div class="file-icon">${f.name.endsWith('.md')?'📄':f.name.endsWith('.py')?'🐍':f.name.endsWith('.js')?'📜':f.name.endsWith('.png')||f.name.endsWith('.jpg')?'🖼️':'📁'}</div>
          <div style="flex:1">
            <div class="file-name">${f.name}</div>
            <div class="file-meta">${(f.size/1024).toFixed(1)} KB · ${f.modified}</div>
          </div>
          <span class="file-cat">${f.category}</span>
        </div>`).join('')}
    </div>`).join('');
}

function renderDetailBoard(proj){
  const tickets=proj.tickets||[];
  const cols=[
    {status:'todo',      label:'📋 To Do',      color:'#6b7280'},
    {status:'in_progress',label:'🔄 In Progress',color:'#f59e0b'},
    {status:'in_review', label:'👀 In Review',   color:'#3b82f6'},
    {status:'done',      label:'✅ Done',         color:'#22c55e'},
    {status:'blocked',   label:'🚫 Blocked',     color:'#ef4444'},
  ];
  const typeIcon={story:'📖',task:'✏️',bug:'🐛',subtask:'↳'};
  const prioColor={critical:'#ef4444',high:'#f97316',medium:'#f59e0b',low:'#6b7280'};
  if(!tickets.length) return `<div class="empty">No tickets yet.<br>Tickets are created when Ada drafts the proposal and Nova runs QA.</div>`;
  const cols_html=cols.map(col=>{
    const cards=tickets.filter(t=>t.status===col.status);
    const cardsHtml=cards.map(t=>`
      <div style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:8px;cursor:pointer" onclick="showTicket(${JSON.stringify(t).replace(/"/g,'&quot;')})">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
          <span>${typeIcon[t.type]||'•'}</span>
          <span style="font-family:monospace;font-size:10px;color:var(--muted)">${t.id}</span>
          <span style="margin-left:auto;width:8px;height:8px;border-radius:50%;background:${prioColor[t.priority]||'#6b7280'};flex:none" title="${t.priority}"></span>
        </div>
        <div style="font-size:12px;font-weight:600;line-height:1.4">${t.title}</div>
        <div style="font-size:10px;color:var(--muted);margin-top:5px">👤 ${t.assignee||'unassigned'}</div>
      </div>`).join('');
    return `
      <div style="flex:0 0 200px;background:rgba(255,255,255,.02);border-radius:8px;padding:10px;border:1px solid var(--border)">
        <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:${col.color};margin-bottom:10px;display:flex;align-items:center;justify-content:space-between">
          <span>${col.label}</span>
          <span style="background:rgba(255,255,255,.08);border-radius:10px;padding:1px 7px;font-size:10px">${cards.length}</span>
        </div>
        ${cardsHtml||'<div style="font-size:11px;color:var(--muted);text-align:center;padding:10px">Empty</div>'}
      </div>`;
  }).join('');
  const bugCount=tickets.filter(t=>t.type==='bug').length;
  const doneCount=tickets.filter(t=>t.status==='done').length;
  const pct=tickets.length?Math.round(doneCount/tickets.length*100):0;
  return `
    <div style="margin-bottom:14px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
      <div class="card" style="padding:10px 16px;display:flex;gap:20px;flex:none">
        <div><div style="font-size:10px;color:var(--muted)">TOTAL</div><div style="font-size:20px;font-weight:700">${tickets.length}</div></div>
        <div><div style="font-size:10px;color:var(--muted)">DONE</div><div style="font-size:20px;font-weight:700;color:var(--green)">${doneCount}</div></div>
        <div><div style="font-size:10px;color:var(--muted)">BUGS</div><div style="font-size:20px;font-weight:700;color:var(--red)">${bugCount}</div></div>
        <div><div style="font-size:10px;color:var(--muted)">PROGRESS</div><div style="font-size:20px;font-weight:700;color:var(--accent)">${pct}%</div></div>
      </div>
      <div style="flex:1;min-width:200px"><div class="bar" style="height:8px"><div class="bf bg" style="width:${pct}%"></div></div></div>
    </div>
    <div style="overflow-x:auto"><div style="display:flex;gap:10px;min-width:1100px;padding-bottom:8px">${cols_html}</div></div>
    <div id="ticket-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;display:none;align-items:center;justify-content:center" onclick="this.style.display='none'">
      <div id="ticket-body" style="background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:480px;width:90%;position:relative" onclick="event.stopPropagation()">
        <button onclick="document.getElementById('ticket-modal').style.display='none'" style="position:absolute;top:12px;right:12px;background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer">✕</button>
        <div id="ticket-detail"></div>
      </div>
    </div>`;
}

function showDetailTab(tab,el,ev){
  ev.stopPropagation();
  _selDetailTab=tab;   // persist so refresh restores correct tab
  document.querySelectorAll('.detail-tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  const projStr=document.getElementById('proj-detail').dataset.proj;
  if(!projStr)return;
  const proj=JSON.parse(projStr);
  const body=document.getElementById('detail-body');
  const renders={overview:renderDetailOverview,activity:renderDetailActivity,
                 documents:renderDetailDocuments,board:renderDetailBoard};
  body.innerHTML=(renders[tab]||renders.overview)(proj);
}

async function viewFile(encodedPath,name){
  try{
    const r=await fetch(`/api/file?path=${encodedPath}`);
    const d=await r.json();
    if(d.error){showToast('Error: '+d.error,'red');return;}
    const body=document.getElementById('detail-body');
    const isImage=name.match(/\.(png|jpg|jpeg|gif|webp)$/i);
    if(isImage){
      body.innerHTML=`<button onclick="history.back()" style="margin-bottom:12px;background:none;border:1px solid var(--border);border-radius:6px;padding:6px 12px;color:var(--text);cursor:pointer">← Back</button>
        <div class="sec">${name}</div><img src="/api/file?path=${encodedPath}&raw=1" style="max-width:100%;border-radius:8px">`;
    }else{
      body.innerHTML=`<button onclick="showDetailTab('documents',document.querySelector('.detail-tab:nth-child(3)'),{stopPropagation:()=>{}})" style="margin-bottom:12px;background:none;border:1px solid var(--border);border-radius:6px;padding:6px 12px;color:var(--text);cursor:pointer">← Back</button>
        <div class="sec">${name}</div>
        <div class="file-viewer">${(d.content||'').replace(/</g,'&lt;')}</div>`;
    }
  }catch(e){showToast('Could not load file','red');}
}

async function doAction(cid,action,label){
  if(!confirm(`Confirm: ${label}?`))return;
  try{
    const r=await fetch(`/api/project/${cid}/action`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action})
    });
    const d=await r.json();
    if(d.ok){
      showToast(`✅ ${label} — orchestrator notified`,'green');
      await tick();
      if(_selCid) await renderDetail(_selCid);
    }else{
      showToast('Error: '+(d.error||'unknown'),'red');
    }
  }catch(e){showToast('Request failed: '+e,'red');}
}

function showTicket(t){
  const prioColor={critical:'#ef4444',high:'#f97316',medium:'#f59e0b',low:'#6b7280'};
  const typeIcon={story:'📖 Story',task:'✏️ Task',bug:'🐛 Bug',subtask:'↳ Sub-task'};
  const statusLabel={todo:'To Do',in_progress:'In Progress',in_review:'In Review',
                     done:'Done',blocked:'Blocked'};
  document.getElementById('ticket-detail').innerHTML=`
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
      <span>${typeIcon[t.type]||t.type}</span>
      <code style="color:var(--muted);font-size:12px">${t.id}</code>
    </div>
    <div style="font-size:18px;font-weight:700;margin-bottom:12px">${t.title}</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">
      <div class="card" style="padding:10px">
        <div style="font-size:10px;color:var(--muted)">STATUS</div>
        <div style="font-weight:600;margin-top:3px">${statusLabel[t.status]||t.status}</div>
      </div>
      <div class="card" style="padding:10px">
        <div style="font-size:10px;color:var(--muted)">PRIORITY</div>
        <div style="font-weight:600;margin-top:3px;color:${prioColor[t.priority]||'var(--text)'}">${t.priority||'—'}</div>
      </div>
      <div class="card" style="padding:10px">
        <div style="font-size:10px;color:var(--muted)">ASSIGNEE</div>
        <div style="font-weight:600;margin-top:3px">👤 ${t.assignee||'Unassigned'}</div>
      </div>
      <div class="card" style="padding:10px">
        <div style="font-size:10px;color:var(--muted)">CREATED</div>
        <div style="font-weight:600;margin-top:3px;font-size:12px">${(t.created||'').substring(0,10)}</div>
      </div>
    </div>
    ${t.desc?`<div style="font-size:13px;color:var(--muted);line-height:1.6;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">${t.desc}</div>`:''}
  `;
  const modal=document.getElementById('ticket-modal');
  modal.style.display='flex';
}

function showToast(msg,color){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.style.display='block';
  t.style.borderColor=color==='green'?'var(--green)':'var(--red)';
  setTimeout(()=>{t.style.display='none';},3000);
}

function renderTraces(traces){
  const el=document.getElementById('traces');
  if(!traces||!traces.length){el.innerHTML='<tr><td colspan="3" class="empty">No traces yet — configure Langfuse API keys to see agent call history.</td></tr>';return;}
  el.innerHTML=traces.map(t=>`<tr><td><code>${t.time||'—'}</code></td><td>${t.name}</td><td>${t.latency}</td></tr>`).join('');
}

tick(); setInterval(tick,5000);
</script></body></html>"""

@app.route("/")
def home(): return PAGE

if __name__ == "__main__":
    app.run(host="0.0.0.0",
            port=int(os.environ.get("PORT_DASHBOARD","8800")),threaded=True)
DASHEOF
    ok "dashboard/app.py written (Jira-style with project detail, approve/reject, file browser)."
}
# =============================================================================
#  PHASE 8 — CONTINUE (VS Code manual IDE)
# =============================================================================
setup_continue() {
    log "Continue VS Code extension"
    have code && {
        code --install-extension continue.continue >/dev/null 2>&1 \
            && ok "Continue extension installed." \
            || warn "Could not auto-install — in VS Code: Extensions → search 'Continue'."
    } || warn "'code' CLI not on PATH. In VS Code: Cmd+Shift+P → 'Install code command in PATH'."
    mkdir -p "$HOME/.continue"
    [ -f "$HOME/.continue/config.yaml" ] && { ok "Continue config exists — keeping."; return; }
    cat > "$HOME/.continue/config.yaml" <<'CONTEOF'
name: Local AI Team
version: 1.0.0
models:
  - name: Leo Manual (qwen3.6:27b — activate during /pause)
    provider: ollama
    model: qwen3.6:27b
    roles: [chat, edit, apply]
  - name: Leo Heavy (qwen2.5-coder:72b — 72B coding specialist)
    provider: ollama
    model: qwen2.5-coder:72b
    roles: [chat, edit, apply]
  - name: Autocomplete
    provider: ollama
    model: qwen3-coder:30b
    roles: [autocomplete]
  - name: Embeddings
    provider: ollama
    model: nomic-embed-text
    roles: [embed]
CONTEOF
    ok "Continue config written (~/.continue/config.yaml)."
}

# =============================================================================
#  PHASE 9 — ALWAYS-ON SERVICES (launchd)
# =============================================================================
install_launch_agent() {
    local label="${1:-}" prog="${2:-}"
    [ -z "$label" ] || [ -z "$prog" ] && { warn "install_launch_agent: missing args."; return; }
    local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR"
    launchctl unload "$plist" >/dev/null 2>&1 || true
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-lc</string><string>$prog</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>AI_HOME</key><string>$WORKDIR</string>
        <key>AI_WORKSPACE</key><string>$AI_WORKSPACE</string>
        <key>PORT_GATEWAY</key><string>$PORT_GATEWAY</string>
        <key>PORT_SEARXNG</key><string>$PORT_SEARXNG</string>
        <key>PORT_DASHBOARD</key><string>$PORT_DASHBOARD</string>
        <key>PORT_PORTAINER</key><string>$PORT_PORTAINER</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.out.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err.log</string>
</dict></plist>
PLISTEOF
    launchctl load "$plist" >/dev/null 2>&1 \
        && ok "service loaded: $label" \
        || warn "could not load $label"
}

setup_services() {
    log "Registering always-on launchd services"
    mkdir -p "$WORKDIR/logs"
    install_launch_agent "com.aiws.colima" \
        "/opt/homebrew/bin/colima start || true; while true; do sleep 86400; done"
    install_launch_agent "com.aiws.litellm"      "$WORKDIR/start_gateway.sh"
    install_launch_agent "com.aiws.dashboard"    "$WORKDIR/start_dashboard.sh"
    install_launch_agent "com.aiws.orchestrator" "$WORKDIR/start_orchestrator.sh"

    # Daily trend watcher via StartCalendarInterval
    local TW_PLIST="$LAUNCH_DIR/com.aiws.trendwatcher.plist"
    launchctl unload "$TW_PLIST" >/dev/null 2>&1 || true
    cat > "$TW_PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.aiws.trendwatcher</string>
  <key>ProgramArguments</key>
  <array><string>$WORKDIR/.venv/bin/python</string>
         <string>$WORKDIR/agents/trend_watcher.py</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$VOX_HOUR</integer>
        <key>Minute</key><integer>$VOX_MINUTE</integer></dict>
  <key>EnvironmentVariables</key>
  <dict><key>AI_HOME</key><string>$WORKDIR</string>
        <key>PORT_GATEWAY</key><string>$PORT_GATEWAY</string>
        <key>PORT_SEARXNG</key><string>$PORT_SEARXNG</string></dict>
  <key>StandardOutPath</key><string>$WORKDIR/logs/trendwatcher.out.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/trendwatcher.err.log</string>
</dict></plist>
PLISTEOF
    launchctl load "$TW_PLIST" >/dev/null 2>&1 \
        && ok "Vox daily trend scheduled at ${VOX_HOUR}:$(printf "%02d" $VOX_MINUTE)" \
        || warn "Trend watcher scheduling failed"

    # LAN bridges (socat forwards Docker's localhost ports to all interfaces)
    local SOCAT; SOCAT="$(command -v socat || echo /opt/homebrew/bin/socat)"
    install_launch_agent "com.aiws.bridge.openwebui" \
        "$SOCAT TCP-LISTEN:$PORT_OPENWEBUI,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_OPENWEBUI"
    install_launch_agent "com.aiws.bridge.searxng" \
        "$SOCAT TCP-LISTEN:$PORT_SEARXNG,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_SEARXNG"
    install_launch_agent "com.aiws.bridge.langfuse" \
        "$SOCAT TCP-LISTEN:$PORT_LANGFUSE,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:$PORT_LANGFUSE"
    ok "All services registered — auto-start at login."
}

# =============================================================================
#  PHASE 10 — COLLECT TOKENS
# =============================================================================
collect_tokens() {
    log "Telegram bot credentials"
    prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token" validate_telegram tut_telegram
    if [ -z "$(get_env TELEGRAM_CHAT_ID)" ]; then
        printf "Paste your Telegram numeric chat ID (from @userinfobot, or 'skip'): "
        read -r val
        case "$val" in skip|SKIP|"") warn "Skipped TELEGRAM_CHAT_ID (daily Vox trend won't send)." ;;
        *) set_env TELEGRAM_CHAT_ID "$val" && ok "Chat ID saved." ;; esac
    else ok "Chat ID already set."; fi
}

# =============================================================================
#  PHASE 11 — SUMMARY
# =============================================================================
summary() {
    load_env; brew_env
    local tg; tg="$(get_env TELEGRAM_BOT_TOKEN)"
    local tgs; [ -n "$tg" ] && tgs="configured" || tgs="⚠️  not set"
    local cid; cid="$(get_env TELEGRAM_CHAT_ID)"
    local cids; [ -n "$cid" ] && cids="configured" || cids="⚠️  not set (Vox daily won't send)"
    _up() { "$@" >/dev/null 2>&1 && printf "%sLIVE%s" "$c_grn" "$c_reset" || printf "%sdown%s" "$c_red" "$c_reset"; }
    cat <<SUMEOF

${c_grn}══════════════════════════════════════════════════════${c_reset}
${c_grn}      AI DEVELOPMENT TEAM — SETUP COMPLETE           ${c_reset}
${c_grn}══════════════════════════════════════════════════════${c_reset}

${c_cyn}AI WORKSPACE (all generated files)${c_reset}
  $AI_WORKSPACE/
  ├── projects/     ← Leo's code, per project subfolder
  ├── proposals/    ← Ada+Mira proposals, per project subfolder
  ├── screenshots/  ← all screenshots sent via Telegram
  ├── reports/      ← Cipher pentest reports
  └── trends/       ← (future) Vox saved suggestions

${c_cyn}YOUR AI TEAM${c_reset}
  🤖 Orion   Orchestrator      qwen3.6:27b      ~22 GB (always on)
  📊 Ada     PM / PO           qwen2.5:72b     ~44 GB  (on demand)
  🎨 Mira    UI/UX Designer    gemma4:26b      ~18 GB  (on demand, multimodal)
  💻 Leo     Developer         qwen2.5-coder:72b ~44 GB (on demand)
  🔎 Nova    QA Tester         qwen2.5:72b     ~44 GB  (shares Ada's slot)
  🛡️  Cipher  Pentester         qwen2.5:72b      ~44 GB (on-demand, confirmation req.)
  📡 Vox     Trend Watcher     qwen2.5:72b     ~44 GB  (daily ${VOX_HOUR}:$(printf "%02d" $VOX_MINUTE) + on-demand)

${c_cyn}MEMORY BUDGET${c_reset}
  Orion + Leo/Cipher:  ~30 GB  ✅ comfortable
  Orion + Ada/Nova/Vox: ~52 GB  ⚠️  tight but fits (Apple Silicon handles it)
  OS + Docker overhead: ~8 GB
  Total peak:          ~60 GB / 64 GB

${c_cyn}WORKFLOW${c_reset}
  You (Telegram) → idea → Ada+Mira proposal → Your approval
  → Leo builds → Nova tests → bugs? → Leo fixes (loop)
  → Ada final review → Your approval → ✅ Done

${c_cyn}SERVICES${c_reset}
  Portainer    http://localhost:$PORT_PORTAINER  [$(_up http_ok "http://localhost:$PORT_PORTAINER/")]
  Dashboard   http://localhost:$PORT_DASHBOARD  [$(_up http_ok "http://localhost:$PORT_DASHBOARD/")]
  Open WebUI  http://localhost:$PORT_OPENWEBUI  [$(_up http_ok "http://localhost:$PORT_OPENWEBUI/")]
  Langfuse    http://localhost:$PORT_LANGFUSE   [$(_up langfuse_ok)]
  SearXNG     http://localhost:$PORT_SEARXNG    [$(_up searxng_ok)]
  LiteLLM     http://localhost:$PORT_GATEWAY    [$(_up http_ok "http://localhost:$PORT_GATEWAY/health/liveliness")]
  Ollama      http://localhost:$PORT_OLLAMA     [$(_up http_ok "http://localhost:$PORT_OLLAMA/api/tags")]

${c_cyn}CREDENTIALS${c_reset}
  Telegram bot: $tgs
  Chat ID:      $cids

${c_cyn}NEXT STEPS${c_reset}
  1. Open dashboard:   http://localhost:$PORT_DASHBOARD
  2. DM your bot:      "build me a [project idea]"
  3. Manual IDE:       /pause → VS Code + Continue → Leo Manual → /resume
  4. On-demand trends: say "what should I build?" in Telegram
  5. Pentest:          say "pentest [target]" — Cipher asks for confirmation

${c_cyn}CONTROL${c_reset}
  bash setup_ai_team.sh  --status | --start | --stop | --restart | --update | --reset
${c_grn}══════════════════════════════════════════════════════${c_reset}
SUMEOF
}

# =============================================================================
#  SERVICE CONTROL  (--status / --start / --stop / --restart / --update / --reset)
# =============================================================================
LABELS="com.aiws.colima com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator \
        com.aiws.bridge.openwebui com.aiws.bridge.searxng com.aiws.bridge.langfuse"

brew_env() {
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
}
state()   { "$@" >/dev/null 2>&1 && echo "up" || echo "down"; }
svc_row() {
    local lbl="$1" st="$2"
    if [ "$st" = "up" ]; then
        printf "  %-34s %sRUNNING%s\n" "$lbl" "$c_grn" "$c_reset"
    else
        printf "  %-34s %sSTOPPED%s\n" "$lbl" "$c_red" "$c_reset"
    fi
}

svc_status() {
    brew_env; log "Service status"
    svc_row "Docker / Colima"                "$(state docker_up)"
    svc_row "Ollama           :$PORT_OLLAMA"  "$(state http_ok "http://localhost:$PORT_OLLAMA/api/tags")"
    svc_row "LiteLLM gateway  :$PORT_GATEWAY" "$(state http_ok "http://localhost:$PORT_GATEWAY/health/liveliness")"
    svc_row "Open WebUI       :$PORT_OPENWEBUI" "$(state http_ok "http://localhost:$PORT_OPENWEBUI/")"
    svc_row "SearXNG          :$PORT_SEARXNG"  "$(state searxng_ok)"
    svc_row "Langfuse         :$PORT_LANGFUSE" "$(state langfuse_ok)"
    svc_row "Portainer        :$PORT_PORTAINER" "$(state http_ok "http://localhost:$PORT_PORTAINER/")"
    svc_row "Dashboard        :$PORT_DASHBOARD" "$(state http_ok "http://localhost:$PORT_DASHBOARD/")"
    printf "\n  Dashboard: %shttp://localhost:%s%s\n" "$c_cyn" "$PORT_DASHBOARD" "$c_reset"
}

svc_start() {
    brew_env; log "Starting all services"
    opt colima start
    for _ in $(seq 1 25); do docker_up && break; sleep 1; done
    ollama_start
    if docker_up; then
        docker start open-webui searxng portainer >/dev/null 2>&1 || true
        [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && opt dc up -d)
    fi
    for l in $LABELS; do launchctl load "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true; done
    launchctl load "$LAUNCH_DIR/com.aiws.trendwatcher.plist" >/dev/null 2>&1 || true
    ok "Start requested. Verify with: bash setup_ai_team.sh --status"
}

svc_stop() {
    brew_env; log "Stopping all services (data and models preserved)"
    for l in $LABELS; do launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true; done
    launchctl unload "$LAUNCH_DIR/com.aiws.trendwatcher.plist" >/dev/null 2>&1 || true
    if docker_up; then
        [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc stop >/dev/null 2>&1 || true)
        docker stop open-webui searxng portainer >/dev/null 2>&1 || true
    fi
    ollama_stop
    opt colima stop
    ok "All services stopped."
}

do_reset() {
    printf "%sThis removes all configs, containers, and services (models & .env kept). [y/N] %s" \
           "$c_yel" "$c_reset"
    read -r r; case "$r" in y|Y|yes) ;; *) echo "Aborted."; exit 0 ;; esac
    brew_env
    for l in $LABELS; do
        launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_DIR/$l.plist"
    done
    launchctl unload "$LAUNCH_DIR/com.aiws.trendwatcher.plist" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_DIR/com.aiws.trendwatcher.plist"
    if docker_up; then
        [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc down -v >/dev/null 2>&1 || true)
        docker rm -f open-webui searxng portainer >/dev/null 2>&1 || true
        docker volume rm open-webui portainer_data >/dev/null 2>&1 || true
    fi
    ollama_stop; opt colima stop
    rm -rf "$WORKDIR/.venv" "$WORKDIR/agents" "$WORKDIR/dashboard" \
           "$WORKDIR/proposals" "$WORKDIR/litellm.config.yaml" \
           "$WORKDIR/start_gateway.sh" "$WORKDIR/start_dashboard.sh" \
           "$WORKDIR/start_orchestrator.sh" "$WORKDIR/repair_venv.sh" \
           "$HOME/.continue/config.yaml"
    ok "Reset complete. Ollama models and .env are intact."
    echo "Re-run: bash setup_ai_team.sh"
    exit 0
}

do_update() {
    brew_env; log "Updating everything to latest"
    opt brew update; opt brew upgrade
    log "Ollama"
    if ollama_is_brew; then
        opt brew upgrade ollama
    else
        warn "Ollama installed via DMG — to update, download latest from https://ollama.com"
    fi
    log "Ollama models (re-pull = latest version)"
    if http_ok "http://localhost:$PORT_OLLAMA/api/tags"; then
        for entry in "${MODELS[@]}"; do
            local tag="${entry%%|*}"
            printf "  pulling latest %s\n" "$tag"
            ollama pull "$tag" || warn "skip $tag"
        done
    else warn "Ollama not running — start it then re-run --update."; fi
    log "Python packages"
    if [ -x "$WORKDIR/.venv/bin/python" ]; then
        local UV; UV="$(command -v uv 2>/dev/null || echo /opt/homebrew/bin/uv)"
        "$UV" pip install --python "$WORKDIR/.venv/bin/python" --upgrade \
            "litellm[proxy]" openai "langfuse>=2.0,<3.0" python-dotenv flask \
            requests rich psutil "python-telegram-bot>=21.0" pyyaml playwright
    fi
    log "Docker images"
    if docker_up; then
        opt docker pull ghcr.io/open-webui/open-webui:main
        opt docker pull searxng/searxng:latest
        opt docker pull portainer/portainer-ce:latest
        docker rm -f open-webui searxng portainer >/dev/null 2>&1 || true
        [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && opt dc pull)
    fi
    ok "Update complete. Run --restart to relaunch with new versions."
    exit 0
}

# =============================================================================
#  MAIN
# =============================================================================
case "${1:-}" in
    --status)  svc_status; exit 0 ;;
    --start)   svc_start;  exit 0 ;;
    --stop)    svc_stop;   exit 0 ;;
    --restart) svc_stop; sleep 2; svc_start; exit 0 ;;
    --reset)   do_reset ;;
    --update)  do_update ;;
    -h|--help) head -n 32 "$0" | tail -n 30; exit 0 ;;
esac

main() {
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
    setup_services
    collect_tokens
    summary
}
main "$@"
