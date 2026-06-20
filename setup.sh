#!/usr/bin/env bash
# =============================================================================
#  script3.sh — Local AI Development Workstation Setup & Orchestrator
#  fresh-Mac bootstrap (Apple Silicon / Intel, macOS)
#  Combines OpenClaw & the Agile multi-agent team (Orion, Ada, Mira, Leo, Nova, Cipher, Vox)
# =============================================================================
#
#  Path mappings:
#    - Documents (proposals, reports, summaries) -> /Users/thaqifisa/OneDrive/AI-Agent/
#    - Source Code projects -> /Users/thaqifisa/SourceCode/
#
#  Control: --status | --start | --stop | --restart | --update | --reset | --uninstall | --help
# =============================================================================
set -uo pipefail
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

# ─────────────────────────────── CONFIGURATION ────────────────────────────────
WORKDIR="${HOME}/.local-ai-workstation"
ENV_FILE="$WORKDIR/.env"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

DOCS_WORKSPACE="/Users/thaqifisa/OneDrive/AI-Agent"
CODE_WORKSPACE="/Users/thaqifisa/SourceCode"
MASTER_EMAIL="mthaqifisa@pm.me"

COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEM="${COLIMA_MEM:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

OLLAMA_MAX_LOADED="${OLLAMA_MAX_LOADED:-2}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-3m}"

PORT_OLLAMA=11434; PORT_OPENWEBUI=3001; PORT_LANGFUSE=3000
PORT_SEARXNG=8888; PORT_GATEWAY=4000;  PORT_DASHBOARD=8800
PORT_PORTAINER=9001

VOX_HOUR="${VOX_HOUR:-7}";        VOX_MINUTE="${VOX_MINUTE:-0}"
VOX_HOUR_PM="${VOX_HOUR_PM:-18}"; VOX_MINUTE_PM="${VOX_MINUTE_PM:-0}"

MODELS=(
  "qwen3.6:35b-a3b|Orion — orchestrator, always loaded (~26 GB)"
  "qwen3.6:27b|Leo — coder model (~22 GB)"
  "gemma4:26b|Mira — UI/UX designer (~18 GB)"
  "qwen2.5:72b|Ada + Nova + Vox + Cipher — 72B reasoning (~44 GB)"
  "nomic-embed-text|Embeddings for RAG/search (~270 MB)"
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
    [ "$free_gb" -lt 60 ] && warn "Low disk: ${free_gb} GB free (need ~120 GB for models)" \
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

    # Install main utilities + WeasyPrint dependencies (cairo pango gdk-pixbuf libffi)
    for p in colima docker docker-compose node git jq wget lazydocker uv socat \
             cairo pango gdk-pixbuf libffi; do
        brew list "$p" >/dev/null 2>&1 && ok "$p present" || opt brew install "$p"
    done
    
    have node && ok "node $(node -v)"
    have uv   && ok "uv $(uv --version 2>/dev/null)"
    grep -q '.local/bin' "$HOME/.zprofile" 2>/dev/null || \
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"

    # Puppeteer E2E Testing (Nova)
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

# =============================================================================
#  PHASE 2 — OLLAMA MODELS
# =============================================================================
setup_ollama() {
    log "Ollama + local models"
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
}

# =============================================================================
#  PHASE 3 — PYTHON SETUP (VENV + REPAIR SYSTEM)
# =============================================================================
venv_ok() {
    [ -x "$WORKDIR/.venv/bin/python" ] && [ -x "$WORKDIR/.venv/bin/litellm" ] \
    && "$WORKDIR/.venv/bin/python" -c \
       "import litellm,flask,requests,psutil,telegram,yaml,playwright,weasyprint,markdown" >/dev/null 2>&1
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
        "python-telegram-bot>=21.0" \
        pyyaml \
        playwright \
        weasyprint \
        markdown \
        || { err "Failed to install Python requirements."; return; }

    log "Installing Playwright Chromium..."
    "$WORKDIR/.venv/bin/playwright" install chromium \
        && ok "Playwright Chromium ready." \
        || warn "Playwright Chromium setup failed."

    if venv_ok; then
        ok "Virtual environment setup complete."
        for svc in com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator; do
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
        && ok "Open WebUI running -> http://localhost:$PORT_OPENWEBUI" \
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
        && ok "SearXNG running -> http://localhost:$PORT_SEARXNG" \
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
  1. Open http://localhost:3000 → Create local account.
  2. Organization → Project → Settings → API Keys → Create.
  3. Copy PUBLIC (pk-lf-...) and SECRET (sk-lf-...).
########################################
LFEOF
    press_enter
    printf "Paste PUBLIC key (or 'skip'): "; read -r pk
    case "$pk" in skip|SKIP|"") warn "Skipping Langfuse tracking."; return ;; esac
    printf "Paste SECRET key: "; read -r sk
    set_env LANGFUSE_PUBLIC_KEY "$pk"; set_env LANGFUSE_SECRET_KEY "$sk"
    set_env LANGFUSE_HOST "http://localhost:$PORT_LANGFUSE"; ok "Langfuse configurations saved."
}

setup_portainer() {
    log "Portainer UI container"
    docker_up || { warn "Docker not active; skipping Portainer."; return; }
    opt docker volume create portainer_data
    docker rm -f portainer >/dev/null 2>&1 || true
    docker run -d --name portainer --restart unless-stopped \
        -p "127.0.0.1:$PORT_PORTAINER:9000" \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v portainer_data:/data \
        portainer/portainer-ce:latest \
        && ok "Portainer active -> http://localhost:$PORT_PORTAINER" \
        || warn "Failed to launch Portainer"
}

# =============================================================================
#  PHASE 5 — LiteLLM GATEWAY & REPAIR SERVICE
# =============================================================================
setup_litellm() {
    log "LiteLLM Gateway Setup"
    load_env
    local CFG="$WORKDIR/litellm.config.yaml"
    if [ -f "$CFG" ]; then
        ok "litellm.config.yaml already exists."
    else
        cat > "$CFG" <<'LLMEOF'
model_list:
  - model_name: orion
    litellm_params: { model: ollama/qwen3.6:35b-a3b,   api_base: http://127.0.0.1:11434 }
  - model_name: leo
    litellm_params: { model: ollama/qwen3.6:27b,       api_base: http://127.0.0.1:11434 }
  - model_name: cipher
    litellm_params: { model: ollama/devstral:24b,      api_base: http://127.0.0.1:11434 }
  - model_name: ada
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: nova
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: vox
    litellm_params: { model: ollama/qwen2.5:72b,      api_base: http://127.0.0.1:11434 }
  - model_name: mira
    litellm_params: { model: ollama/gemma4:26b,       api_base: http://127.0.0.1:11434 }
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

    # repair_venv.sh - self healing virtual environment script
    cat > "$WORKDIR/repair_venv.sh" <<'REPAIREOF'
#!/usr/bin/env bash
WORKDIR="${WORKDIR:-$HOME/.local-ai-workstation}"
VENV="$WORKDIR/.venv"
LOG="[repair_venv $(date '+%H:%M:%S')]"

venv_healthy() {
    [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/litellm" ] \
    && "$VENV/bin/python" -c \
       "import litellm,flask,requests,psutil,telegram,yaml,weasyprint,markdown" >/dev/null 2>&1
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
    requests rich psutil "python-telegram-bot>=21.0" pyyaml playwright \
    weasyprint markdown >&2 || exit 1
"$VENV/bin/playwright" install chromium >/dev/null 2>&1 || true
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

    cat > "$WORKDIR/start_orchestrator.sh" <<SHEOF
#!/usr/bin/env bash
WORKDIR="${WORKDIR}"
bash "\$WORKDIR/repair_venv.sh" || { sleep 60; exit 1; }
set -a; [ -f "\$WORKDIR/.env" ] && . "\$WORKDIR/.env"; set +a
exec "\$WORKDIR/.venv/bin/python" "\$WORKDIR/agents/orchestrator.py"
SHEOF
    chmod +x "$WORKDIR/start_orchestrator.sh"
    ok "Service launchers created."
}

# =============================================================================
#  PHASE 6 — CUSTOM AGENTS & ORCHESTRATOR
# =============================================================================
setup_agent_team() {
    log "Writing agent prompts and code files..."
    local AD="$WORKDIR/agents"
    mkdir -p "$AD"

    # 1. Write team.yaml defining agent roles
    cat > "$AD/team.yaml" <<'TEAMEOF'
roles:
  orion:
    name: Orion
    role: Chief of Staff & Systems Mind
    model: orion
    system_prompt: |
      You are ORION — the lead orchestrating intelligence running locally on this Mac.
      You plan project lifecycles, coordinate team members, run shell scripts (with approval),
      and communicate with your master via Telegram.
      
      HOW YOU OPERATE
      - Calm, concise, technical, and straightforward.
      - Never hallucinate file structures, configurations, or commands.
      - Save all project folders under `/Users/thaqifisa/SourceCode/<project_name>`.
      - Save all documents under `/Users/thaqifisa/OneDrive/AI-Agent/<sub-category>/<project_name>`.
      - When an idea arrives, reconstruct it into a brief for Ada (PM/PO) and generate a Title.
      
      YOUR AGENTS
      - Ada (ada): PM and analyst. Generates proposals as MD & PDF and does final reviews.
      - Mira (mira): UI/UX designer. Writes journeys, wireframe details, and draw.io XML files.
      - Leo (leo): Software Developer. Outputs code files and setup READMEs.
      - Nova (nova): QA engineer. Runs browser-driven E2E tests (Puppeteer) and files bug tickets.
      - Cipher (cipher): White-hat pentester. Tests code security.
      - Vox (vox): Opportunity scout. Daily trend analysis.

  ada:
    name: Ada
    role: Product Owner / Program Manager
    model: ada
    system_prompt: |
      You are ADA — Product Owner and PM. You parse the project brief and design an Agile plan.
      
      DELIVERABLES:
      - Create detailed proposals with user stories and acceptance criteria.
      - Output a Markdown document, which will be compiled into a PDF.
      - End proposals with the ticket block below:
      ---TICKETS---
      STORY|high|[Story title]|[One-sentence description]
      TASK|medium|[Task title]|[One-sentence description]

  mira:
    name: Mira
    role: Senior UI/UX Designer
    model: mira
    system_prompt: |
      You are MIRA — UI/UX designer. You deliver screen flows and layout wireframes.
      
      DELIVERABLES:
      - Plain text user journeys and design systems.
      - An editable draw.io wireframe for key screens inside a fenced XML block:
        ### [filename].drawio
        ```xml
        <mxfile host="app.diagrams.net">...</mxfile>
        ```

  leo:
    name: Leo
    role: Super-Senior Full-Stack Developer
    model: leo
    system_prompt: |
      You are LEO — Developer. You write high-quality source code.
      
      DELIVERABLES:
      - Working code files inside named fenced blocks, e.g.:
        ### src/main.py
        ```python
        # code here
        ```
      - Include a README.md file with build and run instructions.
      - When completely finished, output: DEPLOYMENT COMPLETE

  nova:
    name: Nova
    role: QA Tester
    model: nova
    system_prompt: |
      You are NOVA — QA Tester. You review Leo's deployment and run E2E scenarios.
      
      DELIVERABLES:
      - If everything works, output: ALL TESTS PASSED.
      - Otherwise, file detailed bug tickets using this layout:
        [BUG-001] [Bug Title] | [Severity] | [Reproduction Steps]
TEAMEOF

    # 2. Write trend_watcher.py
    cat > "$AD/trend_watcher.py" <<'TRENDEOF'
#!/usr/bin/env python3
"""Vox Opportunity Scout. Triggered twice daily."""
import os, sys, requests, yaml
from openai import OpenAI
from dotenv import load_dotenv

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/.local-ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))
TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

if not TOKEN or not CHAT_ID:
    sys.exit("Telegram credentials missing in .env")

with open(os.path.join(HOME, "agents", "team.yaml")) as f:
    VOX_SYS = yaml.safe_load(f)["roles"]["vox"]["system_prompt"]

client = OpenAI(base_url="http://localhost:4000/v1", api_key="local")

def get_trends():
    try:
        r = requests.get("http://localhost:8888/search",
            params={"q":"news today technology startup business opportunity","format":"json"}, timeout=10)
        return "\n".join(f"- {x.get('title','')}: {x.get('content','')[:200]}"
                         for x in r.json().get("results",[])[:8])
    except Exception as e: return f"Search failed: {e}"

def main():
    news = get_trends()
    try:
        resp = client.chat.completions.create(
            model="vox",
            messages=[{"role":"system","content":VOX_SYS},
                      {"role":"user","content":f"Latest news:\n{news}\n\nSuggest 3 startup project ideas."}],
            temperature=0.9, max_tokens=1000)
        text = resp.choices[0].message.content.strip()
    except Exception as e: text = f"Generation error: {e}"

    msg = f"📡 *Vox's Trend Insights:*\n\n{text}"
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=15)

if __name__ == "__main__": main()
TRENDEOF
    chmod +x "$AD/trend_watcher.py"

    # 3. Write orchestrator.py (Agile workflow manager)
    cat > "$AD/orchestrator.py" <<'ORCHEOF'
#!/usr/bin/env python3
"""AI Team Orchestrator. Direct Telegram interface and Agile manager."""
import os, sys, time, json, datetime, subprocess, re, logging, asyncio
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/.local-ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN","")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID","")

WORKSPACE_DOCS = "/Users/thaqifisa/OneDrive/AI-Agent"
WORKSPACE_CODE = "/Users/thaqifisa/SourceCode"

PF = os.path.join(HOME, "projects.json")
SF = os.path.join(HOME, "agent_status.json")
TF = os.path.join(HOME, "pending_actions.json")

client = OpenAI(base_url="http://localhost:4000/v1", api_key="local")
_exec = ThreadPoolExecutor(max_workers=5)

with open(os.path.join(HOME, "agents", "team.yaml")) as f:
    ROLES = yaml_data = __import__('yaml').safe_load(f)["roles"]

STATES = {
    "idle":              ("No active project",                "⬜"),
    "proposal_drafting": ("Ada & Mira writing proposal",      "安排"),
    "awaiting_approval": ("Awaiting your proposal approval",  "🔵"),
    "development":       ("Leo building",                     "安排"),
    "qa_running":        ("Nova testing",                     "安排"),
    "qa_bugs_found":     ("Bugs found — decision needed",     "🔴"),
    "final_review":      ("Ada doing final review",           "安排"),
    "awaiting_final":    ("Awaiting your final approval",     "🔵"),
    "completed":         ("Project complete",                 "🟢"),
    "paused":            ("Paused — manual IDE mode",         "⏸️"),
}

def load_projects():
    if not os.path.exists(PF): return {}
    try:
        with open(PF) as f: return json.load(f)
    except: return {}

def save_projects(data):
    with open(PF, "w") as f: json.dump(data, f, indent=2, default=str)
    os.chmod(PF, 0o600)

def update_project(cid, **kw):
    p = load_projects(); p.setdefault(cid, {}).update(kw); save_projects(p); return p[cid]

def get_project(cid): return load_projects().get(cid, {})

def write_status(upd):
    d = {}
    if os.path.exists(SF):
        try:
            with open(SF) as f: d = json.load(f)
        except: pass
    d.update(upd)
    with open(SF, "w") as f: json.dump(d, f)

def safe_name(text, max_len=45):
    n = re.sub(r'[^\w\s-]', '', str(text).lower())
    n = re.sub(r'\s+', '_', n.strip())
    return n[:max_len] or "untitled"

def _clean_title(raw, fallback="Untitled Project"):
    t = raw.strip().splitlines()[0] if raw.strip() else fallback
    t = re.sub(r'^(please\s+)?(can you\s+)?(build|create|make|develop|code|program|write)'
               r'(\s+me)?(\s+a|\s+an|\s+the)?\s+', '', t, flags=re.IGNORECASE).strip()
    t = t.strip(' .!,:;"\'')
    return t[:50] if t else fallback

def _ws_docs(pname, *parts):
    path = os.path.join(WORKSPACE_DOCS, *parts, pname)
    os.makedirs(path, exist_ok=True)
    return path

def _ws_code(pname):
    path = os.path.join(WORKSPACE_CODE, pname)
    os.makedirs(path, exist_ok=True)
    return path

def _save_code_files(response, output_dir):
    patterns = [
        r'[`*]{1,3}([^\s`*\n]+\.[a-zA-Z0-9]+)[`*]{0,3}\s*\n```[a-zA-Z]*\n(.*?)```',
        r'###\s*[`]?([^\n`]+\.[a-zA-Z0-9]+)[`]?\s*\n```[a-zA-Z]*\n(.*?)```',
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
            except: pass
    return saved

def _save_drawio_files(response, output_dir):
    saved = []
    pattern = r'(?:#{1,3}\s*[`]?([^\n`]+?\.drawio)[`]?\s*\n)?```xml\s*\n(.*?)```'
    idx = 0
    for m in re.finditer(pattern, response, re.DOTALL | re.IGNORECASE):
        xml = m.group(2).strip()
        if 'mxGraphModel' not in xml: continue
        idx += 1
        fname = (m.group(1) or f"wireframe_{idx}.drawio").strip().lstrip('/').lstrip('./')
        try:
            with open(os.path.join(output_dir, fname), 'w') as f: f.write(xml)
            saved.append(fname)
        except: pass
    return saved

def mt_render_pdf(md_text, out_path, title="Proposal"):
    try:
        import markdown as _md
        from weasyprint import HTML
        body = _md.markdown(md_text, extensions=["tables","fenced_code"])
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        html = ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "@page { size: A4; margin: 2cm; @bottom-right { content: counter(page); color:#888; } }"
            "body { font-family:sans-serif; color:#1a1a1a; line-height:1.5; }"
            "h1 { color:#1f3a5f; border-bottom:3px solid #1f3a5f; }"
            "table { border-collapse:collapse; width:100%; }"
            "th,td { border:1px solid #ccc; padding:8px; }"
            "pre { background:#f6f8fa; padding:10px; border-radius:6px; }"
            "</style></head><body>"
            f"<div style='text-align:center;margin-top:30%'><h1>{title}</h1>"
            f"<div>Prepared by Ada<br>{stamp}</div></div>"
            "<div style='page-break-before:always'></div>"
            f"{body}</body></html>")
        HTML(string=html).write_pdf(out_path)
        return (True, out_path)
    except Exception as e:
        logger.error(f"PDF fail: {e}")
        return (False, str(e))

def mt_send_email(subject, body, attachment_path=None):
    to = MASTER_EMAIL
    subj_e = subject.replace('"', '\\"')
    body_e = body.replace('"', '\\"')
    attach_clause = ""
    if attachment_path and os.path.isfile(attachment_path):
        posix_e = os.path.abspath(attachment_path).replace('"', '\\"')
        attach_clause = f'\n        make new attachment with properties {{file name:(POSIX file "{posix_e}")}} at after the last paragraph'
    script = (
        'tell application "Mail"\n'
        '    set newMsg to make new outgoing message with properties '
        f'{{subject:"{subj_e}", content:"{body_e}", visible:false}}\n'
        '    tell newMsg\n'
        f'        make new to recipient at end of to recipients with properties {{address:"{to}"}}'
        f'{attach_clause}\n'
        '    end tell\n'
        '    send newMsg\n'
        'end tell\n'
    )
    try:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as tf:
            tf.write(script)
            path = tf.name
        r = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=30)
        try: os.unlink(path)
        except: pass
        return r.returncode == 0
    except: return False

def _invoke_sync(name, msgs, temp=0.7):
    cfg = ROLES[name]
    full = [{"role":"system","content":cfg["system_prompt"]}] + msgs
    write_status({name: "working"})
    try:
        r = client.chat.completions.create(
            model=cfg["model"], messages=full, temperature=temp, max_tokens=2500)
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[{name}] API error: {e}")
        return f"⚠️ {name} error: {e}"
    finally:
        write_status({name: "idle"})

async def invoke(name, msgs, temp=0.7):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_exec, lambda: _invoke_sync(name, msgs, temp))

def _strip_think(text):
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

async def deliver_document(ctx, cid, file_path, subject, body, caption):
    # Sends document via Telegram + Emails it
    try:
        with open(file_path, "rb") as f:
            await ctx.bot.send_document(chat_id=int(cid), document=f, caption=caption)
    except Exception as e:
        logger.error(f"Telegram deliver failed: {e}")
    await asyncio.get_running_loop().run_in_executor(_exec, lambda: mt_send_email(subject, body, file_path))

async def keep_typing(bot, cid, stop):
    while not stop.is_set():
        try: await bot.send_chat_action(chat_id=int(cid), action="typing")
        except: pass
        await asyncio.sleep(4)

async def workflow(ctx, cid):
    proj   = get_project(cid)
    status = proj.get("status", "idle")
    idea   = proj.get("idea", "")
    brief  = proj.get("brief", idea)
    hist   = proj.get("history", [])
    pname  = safe_name(idea)

    if status == "proposal_drafting":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "🗂️ *Ada & Mira drafting proposal...*")
            ap = f"Brief:\n{brief}\n\nWrite a markdown proposal with architecture and milestones."
            proposal = await invoke("ada", hist + [{"role":"user","content":ap}])
            mp = f"Proposal:\n{proposal[:1500]}\n\nGenerate wireframe descriptions and a .drawio XML wireframe block."
            design = await invoke("mira", [{"role":"user","content":mp}])
        finally: stop.set()

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = os.path.join(_ws_docs(pname, "proposals"), f"proposal_{ts}.md")
        md_full = f"# {idea}\n\n## Ada's Proposal\n\n{proposal}\n\n## Mira's UX Design\n\n{design}"
        with open(fp, "w") as f: f.write(md_full)

        drawio_files = _save_drawio_files(design, _ws_docs(pname, "proposals"))
        pdf_path = os.path.join(_ws_docs(pname, "proposals"), f"proposal_{ts}.pdf")
        pdf_ok, pdf_res = mt_render_pdf(md_full, pdf_path, title=idea)

        await send(ctx, cid, "📎 _Delivering proposal to OneDrive + Email + Telegram..._")
        if pdf_ok:
            await deliver_document(ctx, cid, pdf_path, f"Proposal: {idea}", f"Proposal attached.\n\n— Ada", f"📄 Proposal: {idea}")
        else:
            await deliver_document(ctx, cid, fp, f"Proposal: {idea} (MD)", f"Proposal MD attached.\n\n— Ada", f"📄 Proposal MD: {idea}")

        update_project(cid, status="awaiting_approval", proposal_file=fp,
                       proposal_pdf=(pdf_path if pdf_ok else ""), history=hist + [{"role":"user","content":ap},{"role":"assistant","content":proposal}])
        
        await send(ctx, cid, f"📋 *Ada's proposal ready.* Approve below to begin code development.",
            mkb(("✅ Approve & Build", "approve_proposal"), ("❌ Reject", "reject_proposal")))

    elif status == "development":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "👨‍💻 *Leo building project...*")
            dp = "Build the approved plan. Output complete code in named fenced blocks. End with DEPLOYMENT COMPLETE"
            result = await invoke("leo", hist + [{"role":"user","content":dp}])
        finally: stop.set()

        nh = hist + [{"role":"user","content":dp},{"role":"assistant","content":result}]
        if "DEPLOYMENT COMPLETE" in result.upper():
            proj_dir = _ws_code(pname)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_md = os.path.join(_ws_docs(pname, "reports"), f"leo_output_{ts}.md")
            with open(out_md, "w") as f: f.write(result)

            saved_files = _save_code_files(result, proj_dir)
            update_project(cid, status="qa_running", history=nh, project_dir=proj_dir)
            await send(ctx, cid, f"✅ *Leo: Development Complete!*\nSaved {len(saved_files)} files to {proj_dir}.")
            await deliver_document(ctx, cid, out_md, f"Leo Build: {idea}", "Leo build log.", f"leo_output_{ts}.md")
            await send(ctx, cid, "🔎 *Activating Nova for E2E tests...*")
            await workflow(ctx, cid)
        else:
            update_project(cid, status="development", history=nh)
            await send(ctx, cid, f"👨‍💻 *Leo feedback Required:*\n\n{result[:1500]}",
                mkb(("✅ Force QA", "force_qa"), ("📝 More Instructions", "instruct_leo")))

    elif status == "qa_running":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "🔎 *Nova running tests...*")
            qp = "Test deployment. If works, end with ALL TESTS PASSED. Otherwise, list detailed bug tickets [BUG-NNN]."
            result = await invoke("nova", hist + [{"role":"user","content":qp}])
        finally: stop.set()

        nh = hist + [{"role":"user","content":qp},{"role":"assistant","content":result}]
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if "ALL TESTS PASSED" in result.upper():
            qa_path = os.path.join(_ws_docs(pname, "reports"), f"qa_report_{ts}.md")
            with open(qa_path, "w") as f: f.write(result)
            update_project(cid, status="final_review", history=nh)
            await send(ctx, cid, f"✅ *Nova: All tests passed!*")
            await deliver_document(ctx, cid, qa_path, f"QA Pass: {idea}", "All passed.", f"qa_report_{ts}.md")
            await workflow(ctx, cid)
        else:
            bug_path = os.path.join(_ws_docs(pname, "reports"), f"bug_report_{ts}.md")
            with open(bug_path, "w") as f: f.write(result)
            update_project(cid, status="qa_bugs_found", history=nh)
            await send(ctx, cid, f"🔴 *Nova found bugs:*\n\n{result[:1400]}")
            await deliver_document(ctx, cid, bug_path, f"Bugs found: {idea}", "Bugs list.", f"bug_report_{ts}.md")
            await send(ctx, cid, "Choose next step:", mkb(("🛠️ Fix bugs", "fix_bugs"), ("⚠️ Accept As-Is", "accept_bugs")))

    elif status == "final_review":
        stop = asyncio.Event()
        ctx.application.create_task(keep_typing(ctx.bot, cid, stop))
        try:
            await send(ctx, cid, "📋 *Ada conducting PM final review...*")
            rp = "Write project final review signoff."
            result = await invoke("ada", hist + [{"role":"user","content":rp}])
        finally: stop.set()
        
        update_project(cid, status="awaiting_final", history=hist + [{"role":"user","content":rp},{"role":"assistant","content":result}])
        await send(ctx, cid, f"📋 *Ada's Final Review:*\n\n{result[:1500]}",
            mkb(("🎉 Accept & Complete", "accept_final"), ("🔄 Refine", "more_changes")))

def mkb(*pairs):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=cb) for text, cb in pairs]])

async def send(ctx, cid, text, kb=None):
    try:
        await ctx.bot.send_message(chat_id=int(cid), text=text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Telegram send fail: {e}")

async def on_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not u.message or not u.message.text: return
    msg = u.message.text.strip(); cid = str(u.effective_chat.id)
    if cid != CHAT_ID: return
    
    proj = get_project(cid); status = proj.get("status", "idle")
    
    if any(s in msg.lower() for s in ["build ", "create ", "make ", "start project"]):
        title = _clean_title(msg)
        update_project(cid, status="proposal_drafting", idea=title, request=msg, created=str(datetime.datetime.now()), history=[])
        await send(ctx, cid, f"🚀 *Starting project: {title}...*")
        await workflow(ctx, cid)
    else:
        # standard general query logic
        resp = _strip_think(await invoke("orion", [{"role": "user", "content": msg}]))
        await send(ctx, cid, resp)

async def on_btn(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    data = q.data; cid = str(u.effective_chat.id)
    
    proj = get_project(cid)
    
    if data == "approve_proposal":
        update_project(cid, status="development")
        await send(ctx, cid, "✅ Proposal approved. Leo starting code development.")
        await workflow(ctx, cid)
    elif data == "reject_proposal":
        update_project(cid, status="idle")
        await send(ctx, cid, "❌ Proposal rejected.")
    elif data == "force_qa":
        update_project(cid, status="qa_running")
        await workflow(ctx, cid)
    elif data == "fix_bugs":
        update_project(cid, status="development")
        await send(ctx, cid, "🛠️ Re-routing to developer Leo to fix bugs.")
        await workflow(ctx, cid)
    elif data == "accept_bugs":
        update_project(cid, status="final_review")
        await workflow(ctx, cid)
    elif data == "accept_final":
        update_project(cid, status="completed", completed_at=str(datetime.datetime.now()))
        await send(ctx, cid, "🎉 Project accepted and marked as completed!")
    elif data == "more_changes":
        update_project(cid, status="development")
        await send(ctx, cid, "🔄 Re-routing for further modifications.")
        await workflow(ctx, cid)

async def action_poller(application):
    # Polls pending_actions.json to trigger events from the dashboard
    while True:
        if os.path.exists(TF):
            try:
                with open(TF) as f: actions = json.load(f)
                os.unlink(TF)
                for cid, act_data in actions.items():
                    act = act_data.get("action")
                    logger.info(f"Poller picked action {act} for project {cid}")
                    proj = get_project(cid)
                    if act == "approve_proposal":
                        update_project(cid, status="development")
                        await application.bot.send_message(chat_id=int(cid), text="✅ Proposal approved (via Dashboard). Development started.")
                        asyncio.create_task(workflow(None, cid))
                    elif act == "reject_proposal":
                        update_project(cid, status="idle")
                        await application.bot.send_message(chat_id=int(cid), text="❌ Proposal rejected (via Dashboard).")
                    elif act == "force_qa":
                        update_project(cid, status="qa_running")
                        asyncio.create_task(workflow(None, cid))
                    elif act == "fix_bugs":
                        update_project(cid, status="development")
                        await application.bot.send_message(chat_id=int(cid), text="🛠️ Developer Leo fixing bugs (via Dashboard).")
                        asyncio.create_task(workflow(None, cid))
                    elif act == "accept_bugs":
                        update_project(cid, status="final_review")
                        asyncio.create_task(workflow(None, cid))
                    elif act == "accept_final":
                        update_project(cid, status="completed", completed_at=str(datetime.datetime.now()))
                        await application.bot.send_message(chat_id=int(cid), text="🎉 Project completed (via Dashboard)!")
                    elif act == "more_changes":
                        update_project(cid, status="development")
                        asyncio.create_task(workflow(None, cid))
            except Exception as e:
                logger.error(f"Poller error: {e}")
        await asyncio.sleep(2)

async def post_init(application):
    application.create_task(action_poller(application))

def main():
    if not TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN set in environment.")
        sys.exit(1)
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", lambda u,c: send(c, str(u.effective_chat.id), "Welcome to AI Team Workstation orchestrator!")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_handler(CallbackQueryHandler(on_btn))
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__": main()
ORCHEOF
    chmod +x "$AD/orchestrator.py"
    ok "Agent prompts and scripts created."
}

# =============================================================================
#  PHASE 7 — LIVE DASHBOARD
# =============================================================================
write_dashboard() {
    log "Dashboard setup"
    local DD="$WORKDIR/dashboard"; mkdir -p "$DD"
    cat > "$DD/app.py" <<'DASHEOF'
#!/usr/bin/env python3
"""AI Team Dashboard Server. Premium Jira style."""
import os, json, datetime, subprocess, re
import requests, psutil
from flask import Flask, jsonify, request
from dotenv import load_dotenv

HOME = os.environ.get("AI_HOME", os.path.expanduser("~/.local-ai-workstation"))
load_dotenv(os.path.join(HOME, ".env"))

PF = os.path.join(HOME, "projects.json")
SF = os.path.join(HOME, "agent_status.json")
TF = os.path.join(HOME, "pending_actions.json")

WORKSPACE_DOCS = "/Users/thaqifisa/OneDrive/AI-Agent"
WORKSPACE_CODE = "/Users/thaqifisa/SourceCode"

LF_URL = os.environ.get("LANGFUSE_HOST","http://localhost:3000")
LF_PK  = os.environ.get("LANGFUSE_PUBLIC_KEY","")
LF_SK  = os.environ.get("LANGFUSE_SECRET_KEY","")

P_OLLAMA="11434"; P_GATEWAY="4000"; P_OPENWEBUI="3001"
P_SEARXNG="8888"; P_LANGFUSE="3000"; P_DASHBOARD="8800"
P_PORTAINER="9001"

app = Flask(__name__)

SERVICES = [
    ("Ollama", f"http://localhost:{P_OLLAMA}/api/tags", P_OLLAMA, "Model engine"),
    ("LiteLLM Gateway", f"http://localhost:{P_GATEWAY}/health/liveliness", P_GATEWAY, "LLM proxy"),
    ("Open WebUI", f"http://localhost:{P_OPENWEBUI}/", P_OPENWEBUI, "Chat application"),
    ("SearXNG", f"http://localhost:{P_SEARXNG}/", P_SEARXNG, "Search agent helper"),
    ("Langfuse", f"http://localhost:{P_LANGFUSE}/api/public/health", P_LANGFUSE, "Trace telemetry"),
    ("Portainer", f"http://localhost:{P_PORTAINER}/", P_PORTAINER, "Docker dashboard"),
    ("Dashboard", f"http://localhost:{P_DASHBOARD}/", P_DASHBOARD, "Control page"),
]

AGENTS = [
    ("orion","🤖","Chief of Staff","qwen3.6:35b-a3b","Team coordinator"),
    ("ada","📊","PM / Product Owner","qwen2.5:72b","PDF Proposals & review"),
    ("mira","🎨","Senior UI/UX","gemma4:26b","draw.io XML wireframes"),
    ("leo","💻","Superdev","qwen3.6:27b","Writes source code"),
    ("nova","🔎","QA Tester","qwen2.5:72b","E2E tests & bugs"),
    ("cipher","🛡️","Pentester","qwen2.5:72b","Vulnerability audit"),
    ("vox","📡","Trends","qwen2.5:72b","Daily trend search"),
]

KANBAN = [
    ("proposal_drafting","📋 Proposal"),("awaiting_approval","⏳ Awaiting Approval"),
    ("development","💻 Code Build"),("qa_running","🔎 QA Testing"),
    ("qa_bugs_found","🐛 Bugs"),("final_review","📊 Review"),
    ("awaiting_final","⏳ final Approval"),("completed","✅ Done"),
]

STATE_LABELS = {
    "idle":("No project","#6b7280"),"proposal_drafting":("Proposal Drafting","#f59e0b"),
    "awaiting_approval":("Awaiting Approval","#3b82f6"),"development":("Building","#f59e0b"),
    "qa_running":("QA Testing","#f59e0b"),"qa_bugs_found":("Bugs Found","#ef4444"),
    "final_review":("Reviewing","#f59e0b"),"awaiting_final":("Awaiting Signoff","#3b82f6"),
    "completed":("Completed ✅","#22c55e"),"paused":("Paused","#8b5cf6"),
}

STATE_ACTIONS = {
    "awaiting_approval":[("✅ Approve & Build","approve_proposal","green"),("❌ Reject","reject_proposal","red")],
    "development":[("✅ Move to QA","force_qa","green")],
    "qa_bugs_found":[("🛠️ Fix Bugs","fix_bugs","yellow"),("⚠️ Accept As-Is","accept_bugs","orange")],
    "awaiting_final":[("🎉 Sign Off","accept_final","green"),("🔄 Refine","more_changes","yellow")],
}

ACTION_STATUS = {
    "approve_proposal":"development","reject_proposal":"idle",
    "force_qa":"qa_running","fix_bugs":"development","accept_bugs":"final_review",
    "accept_final":"completed","more_changes":"development",
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
        if b: bat = {"percent":round(b.percent),"charging":bool(b.power_plugged)}
    except: pass
    return {
        "cpu":{"pct":round(psutil.cpu_percent(interval=None))},
        "ram":{"pct":round(vm.percent),"detail":f"{vm.used/1e9:.1f} / {vm.total/1e9:.1f} GB"},
        "storage":{"pct":round(disk.percent) if disk else 0, "detail":f"{disk.used/1e9:.1f} / {disk.total/1e9:.1f} GB" if disk else "?"},
        "battery":bat
    }

def get_models():
    try:
        r = requests.get(f"http://localhost:{P_OLLAMA}/api/tags", timeout=4); out = []
        for m in r.json().get("models",[]):
            name = m.get("name",""); size = m.get("size",0)
            out.append({"name":name,"size":f"{size/1e9:.1f} GB" if size else "?","group":"Local","desc":""})
        return out
    except: return []

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except: pass
    return default

def list_project_files(idea):
    pname = safe_name(idea); files = []
    folders_cats = [
        (os.path.join(WORKSPACE_DOCS, "proposals", pname), "Proposals & Design"),
        (os.path.join(WORKSPACE_DOCS, "reports", pname), "Reports & QA"),
        (os.path.join(WORKSPACE_CODE, pname), "Source Code")
    ]
    for folder, cat in folders_cats:
        if os.path.exists(folder):
            for root, dirs, filenames in os.walk(folder):
                for fname in sorted(filenames):
                    if fname.startswith('.') or 'node_modules' in root or '.venv' in root or '.git' in root: continue
                    fpath = os.path.join(root, fname)
                    if os.path.isfile(fpath):
                        st = os.stat(fpath)
                        rel_name = os.path.relpath(fpath, folder)
                        files.append({
                            "name": rel_name,
                            "category": cat,
                            "path": fpath,
                            "size": st.st_size,
                            "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                        })
    return files

def safe_name(text, max_len=45):
    n = re.sub(r'[^\w\s-]', '', str(text).lower())
    n = re.sub(r'\s+', '_', n.strip())
    return n[:max_len] or "untitled"

@app.route("/api/status")
def api_status():
    svcs = [{"name":n,"url":f"http://localhost:{p}/","port":int(p),"purpose":pu,"ok":probe(h)}
            for n,h,p,pu in SERVICES]
    ag_raw = load_json(SF, {})
    agents = [{"id":aid,"icon":ic,"role":role,"model":model,"desc":desc,"status":ag_raw.get(aid,"idle")}
            for aid,ic,role,model,desc in AGENTS]
    projects = load_json(PF, {})
    for cid, p in projects.items():
        st = p.get("status","idle")
        label, color = STATE_LABELS.get(st, (st, "#6b7280"))
        p["state_label"] = label; p["state_color"] = color
        p["actions"] = STATE_ACTIONS.get(st, []); p["cid"] = cid
    return jsonify({
        "services":svcs,"agents":agents,"projects":projects,
        "hardware":hardware_info(),"models":get_models(),
        "kanban":[{"status":s,"label":l} for s,l in KANBAN],
        "updated":datetime.datetime.now().strftime("%H:%M:%S")
    })

@app.route("/api/project/<cid>", methods=["DELETE"])
def api_delete_project(cid):
    import shutil
    projects = load_json(PF, {})
    if cid not in projects: return jsonify({"error":"Not found"}), 404
    proj = projects.pop(cid)
    with open(PF, "w") as f: json.dump(projects, f, indent=2, default=str)
    deleted = []; idea = proj.get("idea","")
    if idea:
        pname = safe_name(idea)
        folders = [
            os.path.join(WORKSPACE_DOCS, "proposals", pname),
            os.path.join(WORKSPACE_DOCS, "reports", pname),
            os.path.join(WORKSPACE_CODE, pname)
        ]
        for f in folders:
            if os.path.exists(f):
                shutil.rmtree(f); deleted.append(f)
    return jsonify({"ok":True,"deleted":deleted})

@app.route("/api/project/<cid>")
def api_project_detail(cid):
    projects = load_json(PF, {}); proj = projects.get(cid, {})
    if not proj: return jsonify({"error":"Not found"}), 404
    proj["files"] = list_project_files(proj.get("idea",""))
    return jsonify(proj)

@app.route("/api/project/<cid>/action", methods=["POST"])
def api_project_action(cid):
    data = request.get_json(silent=True) or {}; action = data.get("action","")
    new_status = ACTION_STATUS.get(action)
    if not new_status: return jsonify({"error":"Invalid action"}), 400
    projects = load_json(PF, {})
    if cid not in projects: return jsonify({"error":"Not found"}), 404
    proj = projects[cid]; proj["status"] = new_status
    if action == "accept_final": proj["completed_at"] = str(datetime.datetime.now())
    projects[cid] = proj
    with open(PF, "w") as f: json.dump(projects, f, indent=2, default=str)
    
    triggers = load_json(TF, {})
    triggers[cid] = {"action":action,"time":str(datetime.datetime.now())}
    with open(TF, "w") as f: json.dump(triggers, f)
    return jsonify({"ok":True,"new_status":new_status})

@app.route("/api/file")
def api_file():
    path = request.args.get("path","")
    allowed = [WORKSPACE_DOCS, WORKSPACE_CODE]
    if not any(path.startswith(prefix) for prefix in allowed):
        return jsonify({"error":"Access denied"}), 403
    if not os.path.isfile(path): return jsonify({"error":"File not found"}), 404
    try:
        with open(path, errors="replace") as f: content = f.read(100000)
        return jsonify({"content":content,"name":os.path.basename(path)})
    except Exception as e: return jsonify({"error":str(e)}), 500

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>AI Team — Mission Control</title>
<style>
  :root{--bg:#0a0e14;--panel:#131820;--panel2:#1a212c;--border:#252e3a;--text:#e6edf3;--dim:#8b98a5;--accent:#3b82f6;--green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--purple:#8b5cf6;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);font-size:14px;padding:20px}
  .wrap{max-width:1400px;margin:0 auto}
  header{display:flex;justify-content:space-between;margin-bottom:20px}
  .grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:16px}
  .hw{display:flex;gap:15px;justify-content:space-around}
  .svc{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)}
  .svc:last-child{border:none}
  .led{width:10px;height:10px;border-radius:50%;display:inline-block}
  .led.on{background:var(--green)}.led.off{background:var(--red)}
  .kanban{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
  .col{background:var(--panel2);border-radius:8px;padding:10px;min-height:120px}
  .col h3{font-size:11px;color:var(--dim);margin-bottom:8px;text-transform:uppercase}
  .tk{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:6px;cursor:pointer}
  .btn{padding:6px 12px;border:none;border-radius:6px;font-weight:600;cursor:pointer;margin-right:5px;color:#fff}
  .btn.green{background:var(--green)}.btn.yellow{background:var(--yellow)}.btn.red{background:var(--red)}.btn.orange{background:var(--purple)}
  .proj{background:var(--panel2);border-radius:8px;padding:12px;margin-bottom:10px}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:100;padding:20px}
  .modal.show{display:flex}
  .modal-box{background:var(--panel);border:1px solid var(--border);border-radius:12px;max-width:800px;width:100%;max-height:80vh;overflow-y:auto;padding:20px}
  pre{background:#0d1117;padding:12px;border-radius:8px;overflow:auto;font-family:monospace;white-space:pre-wrap}
  .close{float:right;cursor:pointer;font-size:20px}
  .del{background:none;border:1px solid var(--red);color:var(--red);padding:2px 8px;border-radius:4px;cursor:pointer;float:right}
</style></head><body>
<div class="wrap">
  <header><h1>● AI Team Workstation</h1><div style="color:var(--dim)">Auto-refresh 5s</div></header>
  <div class="grid">
    <div class="card"><h2>System status</h2><div class="hw" id="hw"></div></div>
    <div class="card"><h2>Workstation Services</h2><div id="svcs"></div></div>
  </div>
  <div class="card"><h2>Projects Queue</h2><div id="projects"></div></div>
  <div class="card"><h2>Kanban Board</h2><div class="kanban" id="kanban"></div></div>
  <div class="grid">
    <div class="card"><h2>Specialists</h2><div id="agents"></div></div>
    <div class="card"><h2>Ollama Models</h2><div id="models"></div></div>
  </div>
</div>
<div class="modal" id="modal"><div class="modal-box" id="modal-content"></div></div>
<script>
async function load(){
  try{
    const r=await fetch("/api/status");const d=await r.json();
    document.getElementById("svcs").innerHTML=d.services.map(s=>`
      <div class="svc"><span><span class="led ${s.ok?'on':'off'}"></span> <a href="${s.url}" target="_blank">${s.name}</a></span>
      <span style="color:var(--dim)">${s.purpose} (:${s.port})</span></div>`).join("");
    document.getElementById("hw").innerHTML=`
      <div>CPU: ${d.hardware.cpu.pct}%</div>
      <div>RAM: ${d.hardware.ram.pct}% (${d.hardware.ram.detail})</div>
      <div>Storage: ${d.hardware.storage.pct}% (${d.hardware.storage.detail})</div>`;
    document.getElementById("agents").innerHTML=d.agents.map(a=>`
      <div class="svc"><span>${a.icon} <b>${a.role}</b></span>
      <span style="color:${a.status==='working'?'var(--yellow)':'var(--dim)'}">${a.status}</span></div>`).join("");
    document.getElementById("models").innerHTML=d.models.map(m=>`
      <div class="svc"><span><code>${m.name}</code></span><span style="color:var(--dim)">${m.size}</span></div>`).join("");
    
    const pKeys=Object.keys(d.projects);
    if(!pKeys.length){
      document.getElementById("projects").innerHTML='<div style="color:var(--dim)">No projects running. Command your Telegram bot.</div>';
    } else {
      document.getElementById("projects").innerHTML=pKeys.map(k=>{
        const p=d.projects[k];
        const acts=p.actions.map(a=>`<button class="btn ${a[2]}" onclick="act('${k}','${a[1]}')">${a[0]}</button>`).join("");
        return `<div class="proj">
          <button class="del" onclick="del('${k}')">Delete</button>
          <h3>${p.idea}</h3>
          <div style="color:var(--dim);font-size:11px">Status: <span style="color:${p.state_color}">${p.state_label}</span></div>
          <div style="margin-top:8px">${acts} <button class="btn" style="background:#2d3748" onclick="view('${k}')">📁 Files</button></div>
        </div>`;
      }).join("");
    }
    
    document.getElementById("kanban").innerHTML=d.kanban.map(k=>{
      const items=pKeys.filter(x=>d.projects[x].status===k.status).map(x=>`
        <div class="tk"><b>${d.projects[x].idea}</b></div>`).join("") || '<div style="color:var(--dim)">—</div>';
      return `<div class="col"><h3>${k.label}</h3>${items}</div>`;
    }).join("");
  }catch(e){console.error(e);}
}
async function act(cid,action){
  await fetch(`/api/project/${cid}/action`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
  load();
}
async function del(cid){
  if(confirm("Delete project and generated OneDrive/SourceCode files?")){
    await fetch(`/api/project/${cid}`,{method:"DELETE"});
    load();
  }
}
async function view(cid){
  const r=await fetch(`/api/project/${cid}`);const p=await r.json();
  let html=`<span class="close" onclick="closeM()">×</span><h2>📁 ${p.idea} — Workspace Files</h2><br>`;
  (p.files||[]).forEach(f=>{
    html+=`<div style="padding:8px;background:#2d3748;margin-bottom:5px;border-radius:4px;cursor:pointer" onclick="viewF('${f.path}')">
      <b>${f.name}</b> (${f.category})<span style="float:right;color:var(--dim)">${f.size} bytes</span></div>`;
  });
  showM(html);
}
async function viewF(path){
  const r=await fetch(`/api/file?path=${encodeURIComponent(path)}`);const d=await r.json();
  showM(`<span class="close" onclick="closeM()">×</span><h2>${d.name}</h2><br><pre>${d.content}</pre>`);
}
function showM(html){document.getElementById("modal-content").innerHTML=html;document.getElementById("modal").classList.add("show");}
function closeM(){document.getElementById("modal").classList.remove("show");}
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
#  PHASE 8 — OPENCLAW INTEGRATION
# =============================================================================
openclaw_ok() { have openclaw && openclaw --version >/dev/null 2>&1; }
setup_openclaw() {
    log "OpenClaw Integrator (Stable Agent)"
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
    - Provider: ollama (http://localhost:11434)
    - Model: qwen3.6:35b-a3b
    - Channel: Telegram (paste your TELEGRAM_BOT_TOKEN)
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
#  PHASE 9 — LAUNCHD SERVICE PLIST BUILDER
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
    <key>TELEGRAM_BOT_TOKEN</key><string>$(get_env TELEGRAM_BOT_TOKEN)</string>
    <key>TELEGRAM_CHAT_ID</key><string>$(get_env TELEGRAM_CHAT_ID)</string>
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

write_vox_plist() {
    local label="com.aiws.vox"
    local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
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
    <key>AI_WORKSPACE</key><string>$DOCS_WORKSPACE</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
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
        && ok "Registered scheduled Vox updates" || warn "Failed registering Vox"
}

setup_services() {
    log "Registering launchd background services"
    write_plist "com.aiws.litellm"      "$WORKDIR/start_gateway.sh"
    write_plist "com.aiws.dashboard"    "$WORKDIR/start_dashboard.sh"
    write_plist "com.aiws.orchestrator" "$WORKDIR/start_orchestrator.sh"
    write_vox_plist
}

# =============================================================================
#  PHASE 10 — BOOTSTRAP / CREDENTIAL FLOWS
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
    hr
    cat <<SUMEOF
    🚀  LOCAL AI WORKSTATION SETUP COMPLETE

  Dashboard: http://localhost:$PORT_DASHBOARD
  Telegram:  Start bot on your phone via /start

  Folders:
    - Documents:  $DOCS_WORKSPACE
    - Source:     $CODE_WORKSPACE
    
  Run controls:
    $0 --status | --start | --stop | --restart | --uninstall
SUMEOF
    hr
}

# =============================================================================
#  CLI CONTROLLER & UNINSTALL RUNTIMES
# =============================================================================
SERVICE_LABELS=(com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator com.aiws.vox)

svc_status() {
    hr; log "Services status check:"
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
    for c in open-webui searxng portainer; do docker start "$c" >/dev/null 2>&1 || true; done
    [ -d "$WORKDIR/langfuse" ] && (cd "$WORKDIR/langfuse" && dc up -d >/dev/null 2>&1) || true
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
    if command_exists colima; then colima stop || true; fi
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

    log "Stopping active agents..."
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
    
    if command_exists colima; then
        colima stop >/dev/null 2>&1 || true
    fi

    log "Removing npm configurations..."
    if have npm; then
        npm uninstall -g openclaw >/dev/null 2>&1 || true
    fi

    log "Removing temporary files and local workspace..."
    rm -rf "$WORKDIR"
    rm -rf /tmp/model_*.lock || true

    printf "Remove directories (OneDrive & SourceCode outputs)? [y/N]: "
    read -r clean_w
    if [[ "$clean_w" =~ ^[Yy]$ ]]; then
        rm -rf "$DOCS_WORKSPACE" "$CODE_WORKSPACE"
        ok "Cleaned $DOCS_WORKSPACE and $CODE_WORKSPACE"
    fi

    printf "Uninstall Homebrew system packages installed by this script? (colima, docker, docker-compose, uv, socat, cairo, pango, gdk-pixbuf, libffi)? [y/N]: "
    read -r clean_brew
    if [[ "$clean_brew" =~ ^[Yy]$ ]]; then
        brew remove --force colima docker docker-compose uv socat cairo pango gdk-pixbuf libffi || true
    fi

    ok "Uninstall complete. Your system has been rolled back."
}

# =============================================================================
#  CLI PARSER
# =============================================================================
print_help() {
    cat <<USG
Usage: $0 [OPTION]
  --bootstrap : Run checks, configure tokens and workspaces
  --start     : Start Colima, Ollama, containers and orchestrator
  --stop      : Stop Colima, Ollama, containers and orchestrator
  --restart   : Stop then start services
  --status    : Display live server and services state
  --reset     : Reset orchestrator variables and configuration
  --uninstall : Complete rollback and removal of setup
  --help      : Display instructions
USG
}

case "${1:---help}" in
    --bootstrap)
        preflight
        setup_xcode_clt
        setup_homebrew
        setup_core_tools
        setup_workspaces
        collect_tokens
        setup_ollama
        setup_python
        setup_litellm
        write_dashboard
        setup_agent_team
        setup_openclaw
        setup_peekaboo
        setup_services
        print_summary
        ;;
    --start)
        svc_start
        ;;
    --stop)
        svc_stop
        ;;
    --restart)
        svc_stop
        svc_start
        ;;
    --status)
        svc_status
        ;;
    --reset)
        rm -f "$PF" "$SF" "$TF"
        ok "Kanban database reset."
        ;;
    --uninstall)
        uninstall_all
        ;;
    --help|-h)
        print_help
        ;;
    *)
        echo "Unknown option: $1"
        print_help
        exit 2
        ;;
esac
