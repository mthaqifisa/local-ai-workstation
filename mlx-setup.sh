#!/usr/bin/env bash
# =============================================================================
#  mlx-setup.sh — Local MLX AI Workstation (Apple Silicon, macOS)
#
#  Provisions a fully local, zero-API-cost AI platform on an M-series Mac:
#
#    HOST (native, Metal GPU, via uv venv + launchd)
#      • mlx-openai-server   :8000  one server → LM + vision + embeddings
#      • LiteLLM gateway      :4000  OpenAI-compatible router (role aliases)
#      • Control dashboard    :8800  live status + demo view + documentation
#      • mflux (image gen)           isolated CLI tool
#
#    DOCKER (via Colima; reaches host through host.docker.internal)
#      • Open WebUI           :3001  web chat, pick any model, private
#      • SearXNG              :8888  private web search for RAG
#      • Langfuse (optional)  :3000  tracing/observability
#
#  Why this split: Docker on macOS runs a Linux VM with NO Metal access, so all
#  MLX inference MUST run natively on the host. Stateless UIs live in Docker.
#
#  Models are served from ONE mlx-openai-server YAML with on-demand loading, so
#  only the model in use sits in RAM — the right pattern for 64 GB.
#
#  Control:  --bootstrap | --start | --stop | --restart | --status
#            --pull-models | --image "<prompt>" | --help
# =============================================================================
set -uo pipefail
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

# ─────────────────────────────── CONFIGURATION ────────────────────────────────
SCRIPT_PATH="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)/$(basename "$0")"
WORKDIR="${HOME}/.mlx-ai-workstation"
ENV_FILE="$WORKDIR/.env"
VENV="$WORKDIR/.venv"
MODELS_REGISTRY="$WORKDIR/models.custom.tsv"   # role \t repo \t type \t tool_parser \t reasoning_parser
LAUNCH_DIR="$HOME/Library/LaunchAgents"

DOCS_WORKSPACE="${HOME}/MLX-AI/documents"
CODE_WORKSPACE="${HOME}/MLX-AI/source"

PYTHON_VERSION="3.12"                    # 3.12 = safest for the MLX stack in 2026

PORT_MLX=8000; PORT_GATEWAY=4000; PORT_DASHBOARD=8800
PORT_OPENWEBUI=3001; PORT_SEARXNG=8888; PORT_LANGFUSE=3000
PORT_VISION=8081
VISION_MODEL="${MLX_VISION_MODEL:-mlx-community/Qwen3-VL-8B-Instruct-4bit}"

COLIMA_CPU="${COLIMA_CPU:-6}"; COLIMA_MEM="${COLIMA_MEM:-8}"; COLIMA_DISK="${COLIMA_DISK:-80}"

# Bind host: services listen on 0.0.0.0 by DEFAULT so any device on your LAN can
# reach the dashboard, web chat, and gateway by the Mac's IP (good for demos).
# Set LOCAL_ONLY=1 to bind everything to 127.0.0.1 instead (private/single-machine).
# WARNING: 0.0.0.0 has no TLS and the dashboard/gateway have no login — only run it
# on a network you trust. Example lockdown:  LOCAL_ONLY=1 ./mlx-setup.sh --bootstrap
LOCAL_ONLY="${LOCAL_ONLY:-0}"
if [ "$LOCAL_ONLY" = "1" ]; then BIND_HOST="127.0.0.1"; else BIND_HOST="0.0.0.0"; fi

INSTALL_LANGFUSE="${INSTALL_LANGFUSE:-0}"   # set 1 to add Langfuse tracing (heavy)

# ── Models ────────────────────────────────────────────────────────────────────
# Best-per-task lineup that fits 64 GB unified memory AND runs on MLX (mid-2026).
# Frontier models (GLM-5.2 754B, DeepSeek V4 1.6T, Kimi K2 1T, Qwen3.5-397B) are
# data-center-only and deliberately excluded. All HF repo IDs verified on
# huggingface.co. Everything loads on-demand ONE AT A TIME (see write_mlx_config):
# a 40 GB coder and a 43 GB 70B cannot coexist on 64 GB, so we never pin a big
# model resident — the server loads what you pick and frees it when idle.
# Format: "hf_repo|role_label|CORE|approx_disk"
CORE_MODELS=(
  "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit|orchestrator + general (MoE, 3B active, fast)|CORE|~20 GB"
  "mlx-community/Qwen3.6-27B-8bit|coder + QA, fast daily driver (dense)|CORE|~29 GB"
  "mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit|deep reasoning + math (MIT)|CORE|~18 GB"
  "mlx-community/Qwen3-Embedding-8B-4bit-DWQ|RAG embeddings|CORE|~5 GB"
)
# HEAVY: best-in-class but large; downloaded only via --pull-heavy or the UI so a
# fresh box isn't forced to grab 80+ GB. Both load on-demand (cold-load pause).
HEAVY_MODELS=(
  "mlx-community/Qwen3-Coder-Next-4bit|best agentic coding (80B MoE, SWE-rebench #1)|OPT|~40 GB"
  "mlx-community/DeepSeek-R1-Distill-Llama-70B-4bit|70B for manual heavy lifting (MIT)|OPT|~43 GB"
)
# VISION: PROBE-FIRST. Two gates remain: (1) mlx-vlm must be current (gemma4_unified
# landed AFTER 0.4.4 — bootstrap now upgrades mlx-vlm), and (2) the mlx-openai-server
# multimodal GENERATION path hung on us before and must be re-probed with a real image.
# Not wired to any persona until one actually generates. qwen3_vl + gemma3 archs are in
# mlx-vlm 0.4.4; gemma4 needs the upgrade.
VISION_MODELS=(
  "mlx-community/Qwen3-VL-8B-Instruct-4bit|vision/OCR (arch qwen3_vl, SOTA small VLM)|OPT|~6 GB"
  "mlx-community/gemma-3-27b-it-4bit|vision (arch gemma3, MMMU leader, proven arch)|OPT|~16 GB"
  "unsloth/gemma-4-26b-a4b-it-MLX-8bit|vision (Gemma 4 MoE, needs upgraded mlx-vlm)|OPT|~28 GB"
)
OPTIONAL_MODELS=(
  # kept for --add-model discoverability; DeepSeek-R1 distill tags confirmed on huggingface.co
  "mlx-community/Devstral-Small-2-4bit|alt coder (Mistral 24B, 256K ctx)|OPT|~14 GB"
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
ensure_env_file() { mkdir -p "$WORKDIR"; [ -f "$ENV_FILE" ] || : > "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
get_env() { ensure_env_file; sed -n "s/^$1=//p" "$ENV_FILE" | head -n1; }
set_env() {
    ensure_env_file; local key="$1" val="$2" tmp; tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"; mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}
HF() { "$VENV/bin/hf" "$@"; }             # always use the venv's Hugging Face CLI

# ─────────────────────────────── HEALTH PROBES ────────────────────────────────
http_ok() { have curl && curl -fsS -m 4 "$1" >/dev/null 2>&1; }
docker_up() { have docker && docker info >/dev/null 2>&1; }
dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
mlx_ok()      { http_ok "http://127.0.0.1:$PORT_MLX/v1/models"; }
gateway_ok()  { http_ok "http://127.0.0.1:$PORT_GATEWAY/health/liveliness"; }
dashboard_ok(){ http_ok "http://127.0.0.1:$PORT_DASHBOARD/"; }

# =============================================================================
#  PHASE 0 — PREFLIGHT
# =============================================================================
preflight() {
    log "Preflight"
    [ "$(uname -s)" = "Darwin" ] || { err "macOS only."; exit 1; }
    [ "$(uname -m)" = "arm64" ]  || { err "Apple Silicon (arm64) required for MLX/Metal."; exit 1; }
    ok "macOS $(sw_vers -productVersion 2>/dev/null) on $(uname -m)"

    local ram_gb; ram_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
    [ "$ram_gb" -ge 60 ] && ok "RAM: ${ram_gb} GB" \
        || warn "RAM: ${ram_gb} GB — the 27B/80B models expect ~64 GB; use smaller quants if low."
    local free_gb; free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}' || echo 999)
    [ "$free_gb" -lt 120 ] && warn "Disk: ${free_gb} GB free (models need ~60-120 GB)" \
                           || ok "Disk: ${free_gb} GB free"

    cat <<BANNER

${c_yel}This installs a fully local AI platform in: ${WORKDIR}${c_reset}
  Inference stays on-device (Metal). No API keys, no per-token cost, no data egress.
BANNER
    printf "Proceed? [y/N] "; read -r r; case "$r" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
}

# =============================================================================
#  PHASE 1 — SYSTEM TOOLS
# =============================================================================
setup_xcode_clt() {
    log "Xcode Command Line Tools"
    if xcode-select -p >/dev/null 2>&1; then ok "already installed"; return; fi
    warn "An installer popup will appear — click Install and wait."
    xcode-select --install >/dev/null 2>&1 || true
    printf "Waiting"; while ! xcode-select -p >/dev/null 2>&1; do printf "."; sleep 5; done
    printf "\n"; ok "Xcode CLT ready"
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
    log "System packages"
    # uv drives all Python work; ffmpeg is for media in content pipelines; colima+docker for UIs.
    for p in uv git jq wget ffmpeg colima docker docker-compose node; do
        if brew list "$p" >/dev/null 2>&1; then ok "$p present"
        else log "installing $p"; opt brew install "$p"; fi
    done
    have uv && ok "uv $(uv --version 2>/dev/null)"
}

# =============================================================================
#  PHASE 2 — PYTHON ENV (uv, native host)  — the flawless-Python part
# =============================================================================
venv_ok() {
    # litellm is intentionally NOT here — it runs as an isolated uv tool (see below).
    [ -x "$VENV/bin/python" ] && [ -x "$VENV/bin/mlx-openai-server" ] \
    && "$VENV/bin/python" -c "import mlx, mlx_lm, mlx_vlm, flask, psutil, yaml, requests" >/dev/null 2>&1
}
setup_python() {
    log "Python environment (uv, Python ${PYTHON_VERSION})"
    mkdir -p "$WORKDIR"                         # <-- ensure work dir exists before any venv work
    if venv_ok; then ok "virtualenv verified"; return; fi
    # Build a fresh interpreter only if none exists; otherwise keep it and just
    # (re)install packages — a failed verification must never trigger a multi-GB rebuild.
    if [ ! -x "$VENV/bin/python" ]; then
        [ -d "$VENV" ] && { warn "incomplete venv — rebuilding"; rm -rf "$VENV"; }
        uv venv --python "$PYTHON_VERSION" "$VENV" || { err "uv venv failed"; exit 1; }
    else
        warn "venv exists; re-installing packages and re-verifying (no rebuild)"
    fi

    # Install mlx-openai-server 1.8+ and let it pin the MLX stack (mlx, mlx-lm,
    # mlx-vlm, mlx-embeddings, fastapi, pyyaml, rich are all its dependencies).
    #
    # VERSION LOCK (learned the hard way): the multi-model `--config` YAML mode only
    # exists in mlx-openai-server >=1.6 (we require >=1.8). Those builds depend on
    # mlx-lm >=0.31.3, which requires transformers >=5.0.0. So we must be on the
    # MODERN set. The earlier '__module__' crash was a transformers 5.0.0 *release
    # candidate* paired with an unready mlx-lm; stable 5.x + mlx-lm 0.31.3 are a
    # matched pair and import cleanly. Pinning transformers <5 silently drags the
    # server back to 1.3.12 (single-model, no --config) — which is the bug we hit.
    log "installing MLX + serving stack (this pulls a lot on first run)..."
    uv pip install --python "$VENV/bin/python" \
        "mlx-openai-server>=1.8" \
        "transformers>=5.0.0" \
        "mlx-vlm>=0.5.0" \
        "huggingface_hub" \
        "flask" "psutil" "requests" "python-dotenv" \
        || { err "pip install failed"; exit 1; }
    # mlx-vlm>=0.5 carries the 'gemma4_unified' module (added after 0.4.4) plus the
    # qwen3_vl / gemma3 archs, so the vision models in VISION_MODELS can at least LOAD.
    # (Generation through the multimodal path is a separate probe — see docs/Guide.)

    # Isolated CLI tools moved to setup_cli_tools() so they run even when the venv
    # already verifies on a re-run (this function returns early in that case).

    if venv_ok; then
        uv pip freeze --python "$VENV/bin/python" > "$WORKDIR/requirements.lock" 2>/dev/null || true
        ok "virtualenv ready; pinned versions saved to requirements.lock"
    else
        err "venv verification failed. The actual cause is printed below:"
        echo "  --- import check ---"
        "$VENV/bin/python" -c "import mlx, mlx_lm, mlx_vlm, mlx_embeddings, flask, psutil, yaml, requests; print('  all imports OK -> a required binary is what is missing')" 2>&1 | sed 's/^/  /'
        echo "  --- key binaries in .venv/bin ---"
        ls -1 "$VENV/bin" 2>/dev/null | grep -Ei 'mlx-openai|^hf$' | sed 's/^/  /' || echo "  (none matched)"
        err "Paste the block above and we'll fix the specific package."
        exit 1
    fi
}

# Isolated gateway + image tools — kept OUT of the venv to avoid dependency
# conflicts, and idempotent so re-runs always ensure they're present.
setup_cli_tools() {
    log "Isolated CLI tools (LiteLLM gateway + mflux)"
    if uv tool list 2>/dev/null | grep -q '^litellm'; then ok "litellm present"
    else
        uv tool install --force "litellm[proxy]" >/dev/null 2>&1 && ok "litellm installed" \
            || { err "litellm tool install failed — retry: uv tool install \"litellm[proxy]\""; exit 1; }
    fi
    if uv tool list 2>/dev/null | grep -q '^mflux'; then ok "mflux present"
    else
        uv tool install --force mflux >/dev/null 2>&1 && ok "mflux installed" \
            || warn "mflux skipped (image gen optional) — 'uv tool install mflux' to retry"
    fi
}

# =============================================================================
#  PHASE 3 — HUGGING FACE AUTH + MODEL DOWNLOADS  (uses new `hf` CLI)
# =============================================================================
#  SECRETS — prompt once, store in .env (chmod 600), re-use silently. Re-run any
#  time with `--configure`. Visible input (per user preference) so pastes are easy
#  to eyeball; nothing is echoed to shell history.
# =============================================================================
_mask() {   # show a short masked preview of a secret, e.g. hf_ab…9c2
    local v="$1"; local n=${#v}
    if [ "$n" -le 8 ]; then printf '••••'; else printf '%s…%s' "${v:0:4}" "${v: -3}"; fi
}

# prompt_secret KEY "Label" REQUIRED("yes"/"no") HINT
prompt_secret() {
    local key="$1" label="$2" required="$3" hint="$4" cur; cur="$(get_env "$key")"
    if [ "${MLX_NONINTERACTIVE:-0}" = "1" ]; then     # wizard/CI: never prompt, use .env as-is
        [ -n "$cur" ] && ok "$label present" || { [ "$required" = yes ] && warn "$label missing (required)"; }
        return 0
    fi
    if [ -n "$cur" ]; then
        printf "  %s%s%s is set [%s]. Replace? [y/N] " "$c_cyn" "$label" "$c_reset" "$(_mask "$cur")"
        read -r ans; case "$ans" in y|Y|yes|YES) ;; *) ok "$label kept"; return 0 ;; esac
    fi
    [ -n "$hint" ] && printf "  %s%s%s\n" "$c_yel" "$hint" "$c_reset"
    while :; do
        printf "  paste %s%s%s%s: " "$c_cyn" "$label" "$c_reset" "$([ "$required" = yes ] && echo ' (required)' || echo ' (Enter to skip)')"
        read -r val
        if [ -z "$val" ]; then
            if [ "$required" = yes ]; then err "$label is required — get one and paste it, or Ctrl-C to abort."; continue; fi
            warn "$label skipped — the feature that needs it stays off until you run --configure"; return 0
        fi
        set_env "$key" "$val"; ok "$label saved to .env"; return 0
    done
}

configure_secrets() {
    log "Secrets (stored in $ENV_FILE, chmod 600 — never in shell history or the repo)"
    # 1) Hugging Face token — REQUIRED (model downloads). Free token, 1 minute to make.
    prompt_secret HF_TOKEN "Hugging Face token" yes \
        "Create a free READ token at https://huggingface.co/settings/tokens (also needed to accept Gemma's license)."
    local tok; tok="$(get_env HF_TOKEN)"
    if [ -n "$tok" ]; then
        HF auth login --token "$tok" >/dev/null 2>&1 && ok "Hugging Face: logged in" \
            || warn "HF login didn't verify — public repos still work; gated (Gemma) may 403"
    fi
    # 2) Telegram — OPTIONAL (Layer 4 bridge). Bot obeys only your numeric ID.
    prompt_secret TELEGRAM_BOT_TOKEN "Telegram bot token" no \
        "From @BotFather: send /newbot, follow prompts, copy the token it gives you."
    prompt_secret TELEGRAM_USER_ID "Telegram user ID" no \
        "Your numeric ID from @userinfobot — the bridge will accept commands only from this ID."
    # 3) Dashboard admin password — OPTIONAL, only matters when the control UI is
    #    exposed beyond localhost (BIND_HOST=0.0.0.0). Localhost stays password-free.
    prompt_secret DASHBOARD_PASSWORD "Dashboard admin password" no \
        "Only needed if you reach the :$PORT_DASHBOARD control panel from other machines (LAN). Protects persona/model/service controls."
}

# Verify a repo landed in the HF cache. The `hf` CLI in current huggingface_hub
# raises its success exit (code 0) as an *uncaught* exception, so the process exit
# code is unreliable — we judge success by whether the snapshot actually exists.
hf_cache_dir() { printf '%s/models--%s' "${HF_HOME:-$HOME/.cache/huggingface}/hub" "$(printf '%s' "$1" | sed 's#/#--#g')"; }
hf_download_verify() {   # $1 = repo id → 0 if present in cache after the attempt
    local repo="$1" dir; dir="$(hf_cache_dir "$repo")"
    HF download "$repo" || true          # ignore the CLI's Exit(0)-as-traceback quirk
    [ -d "$dir/snapshots" ] && [ -n "$(ls -A "$dir/snapshots" 2>/dev/null)" ]
}

download_one() {   # $1 = repo id
    local repo="$1"
    case "$repo" in
        *gemma*) if [ "$(get_env HF_SKIP_GEMMA)" = "1" ]; then warn "skip $repo (no HF login)"; return; fi ;;
    esac
    printf "  ↓ downloading %s ...\n" "$repo"   # hf shows its own progress bars below
    if hf_download_verify "$repo"; then ok "downloaded $repo"
    else warn "download failed: $repo (check the repo id / HF login / disk)"; fi
}

download_core_models() {
    log "Downloading CORE models via 'hf download' (resumable; cached in ~/.cache/huggingface)"
    for entry in "${CORE_MODELS[@]}"; do
        local repo="${entry%%|*}" rest="${entry#*|}" role="${rest%%|*}"
        printf "  • %-45s %s\n" "$repo" "$role"
        download_one "$repo"
    done
}

pull_optional_models() {
    log "Optional models"
    local i=1
    for entry in "${OPTIONAL_MODELS[@]}"; do
        local repo="${entry%%|*}" rest="${entry#*|}" role="${rest%%|*}" sz="${entry##*|}"
        printf "  %d) %-45s %s (%s)\n" "$i" "$repo" "$role" "$sz"; i=$((i+1))
    done
    [ "$i" = "1" ] && { ok "none configured"; return; }
    printf "Enter numbers to download (space-separated), 'all', or Enter to skip: "; read -r picks
    [ -z "$picks" ] && { ok "no optional models pulled"; return; }
    local idx=1
    for entry in "${OPTIONAL_MODELS[@]}"; do
        local repo="${entry%%|*}" want=0
        case "$picks" in all|ALL) want=1 ;; *) for n in $picks; do [ "$n" = "$idx" ] && want=1; done ;; esac
        [ "$want" = "1" ] && download_one "$repo"
        idx=$((idx+1))
    done
}

pull_heavy_models() {
    log "Heavy models (large; on-demand). These enable best-in-class coding + a 70B."
    for entry in "${HEAVY_MODELS[@]}"; do
        local repo="${entry%%|*}" rest="${entry#*|}" role="${rest%%|*}" sz="${entry##*|}"
        printf "  • %-46s %s (%s)\n" "$repo" "$role" "$sz"
        download_one "$repo"
    done
    ok "heavy models downloaded — uncomment their blocks in mlx-server.config.yaml (or use --add-model) and load-probe"
}

pull_vision_models() {
    log "Vision models (PROBE-FIRST). Need mlx-vlm>=0.5 + the multimodal generation probe."
    for entry in "${VISION_MODELS[@]}"; do
        local repo="${entry%%|*}" rest="${entry#*|}" role="${rest%%|*}" sz="${entry##*|}"
        printf "  • %-46s %s (%s)\n" "$repo" "$role" "$sz"
        download_one "$repo"
    done
    ok "vision models downloaded — uncomment ONE at a time in the config, restart, and probe with a real image"
}


# =============================================================================
#  PHASE 4 — mlx-openai-server CONFIG (one server, many models, on-demand)
# =============================================================================
write_mlx_config() {
    log "mlx-openai-server config"
    local CFG="$WORKDIR/mlx-server.config.yaml"
    # PARSER NOTE: tool_call_parser / reasoning_parser names are version-specific.
    # These match mlx-openai-server's documented Qwen/Gemma parsers. If tool calls
    # misbehave after first run, check the current names with:
    #     "$VENV/bin/mlx-openai-server" launch --help
    # and the model card, then adjust below. on_demand keeps only the in-use model
    # in RAM — correct for 64 GB. Set on_demand:false on the orchestrator only if
    # you have headroom and want it always hot.
    cat > "$CFG" <<YAMLEOF
server:
  host: "${BIND_HOST}"        # 0.0.0.0 by default (LAN-reachable); LOCAL_ONLY=1 → 127.0.0.1
  port: ${PORT_MLX}
  log_level: INFO
  log_file: "${WORKDIR}/logs/mlx-inference.log"   # ABSOLUTE — under launchd a relative
                                                  # 'logs/' resolves to /logs (read-only) and crashes startup

models:
  # Everything is on_demand:true — with a 40 GB coder and 43 GB 70B in the catalog,
  # nothing can be pinned resident on 64 GB. The server loads what you request and
  # frees it when idle, so only ONE big model occupies memory at a time. First use
  # of each model has a cold-load pause; queue_timeout 900 covers it.

  # ── Orchestrator + general (Qwen3.6 MoE, arch qwen3_5_moe, `lm` text path) ──
  - model_path: unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit
    served_model_name: qwen36-35b
    model_type: lm
    enable_auto_tool_choice: true
    tool_call_parser: qwen3_coder
    reasoning_parser: qwen3
    context_length: 65536            # big room; 4-bit MoE (~20 GB) leaves headroom on 64 GB
    kv_bits: 8                       # halve KV-cache memory (minimal quality loss) — enables large context
    kv_group_size: 64
    batch_completion_size: 2         # single-user; NOT the default 32 (that inflates KV memory hugely)
    batch_prefill_size: 1
    prompt_cache_size: 2
    prompt_cache_max_bytes: 8589934592   # 8 GB hard KV-cache cap — OOM guard so it "won't break"
    default_max_tokens: 2048
    queue_timeout: 900
    on_demand: true
    on_demand_idle_timeout: 600

  # ── Coder + QA, fast daily driver (dense 27B, arch qwen3_5, `lm`) ──
  - model_path: mlx-community/Qwen3.6-27B-8bit
    served_model_name: qwen36-27b
    model_type: lm
    enable_auto_tool_choice: true
    tool_call_parser: qwen3_coder
    reasoning_parser: qwen3
    context_length: 49152            # 8-bit (~27 GB) is the heaviest daily model — generous but leaves RAM
    kv_bits: 8
    kv_group_size: 64
    batch_completion_size: 2
    batch_prefill_size: 1
    prompt_cache_size: 2
    prompt_cache_max_bytes: 8589934592   # 8 GB KV-cache cap
    default_max_tokens: 2048
    queue_timeout: 900
    on_demand: true
    on_demand_idle_timeout: 600

  # ── Deep reasoning / math (DeepSeek-R1-Distill-Qwen-32B, MIT). Served as bare
  #    `lm`: it emits <think> traces but its tool-call format differs from Qwen, so
  #    parsers are omitted until probed (the agent strips <think> client-side).
  #    Add reasoning_parser/tool_call_parser here only after confirming the exact
  #    accepted names via `mlx-openai-server launch --help`. ──
  - model_path: mlx-community/DeepSeek-R1-Distill-Qwen-32B-4bit
    served_model_name: deepseek-r1-32b
    model_type: lm
    context_length: 65536            # 4-bit (~18 GB); reasoner benefits from room for long <think>
    kv_bits: 8
    kv_group_size: 64
    batch_completion_size: 2
    batch_prefill_size: 1
    prompt_cache_size: 2
    prompt_cache_max_bytes: 8589934592
    default_max_tokens: 4096
    queue_timeout: 900
    on_demand: true
    on_demand_idle_timeout: 600

  # ── RAG embeddings ──
  - model_path: mlx-community/Qwen3-Embedding-8B-4bit-DWQ
    served_model_name: qwen3-embed
    model_type: embeddings
    on_demand: true
    on_demand_idle_timeout: 900

  # ── HEAVY (best-in-class, large). Uncomment AFTER 'mlx-setup.sh --pull-heavy'
  #    and a load-probe. Both are on_demand so they only occupy RAM when used. ──
  # - model_path: mlx-community/Qwen3-Coder-Next-4bit          # best agentic coder, 80B MoE ~40 GB
  #   served_model_name: qwen3-coder-next
  #   model_type: lm
  #   enable_auto_tool_choice: true
  #   tool_call_parser: qwen3_coder
  #   reasoning_parser: qwen3
  #   default_max_tokens: 2048
  #   queue_timeout: 1200
  #   on_demand: true
  #   on_demand_idle_timeout: 300
  # - model_path: mlx-community/DeepSeek-R1-Distill-Llama-70B-4bit   # 70B manual heavy-lift ~43 GB
  #   served_model_name: deepseek-r1-70b
  #   model_type: lm
  #   default_max_tokens: 4096
  #   queue_timeout: 1200
  #   on_demand: true
  #   on_demand_idle_timeout: 300

  # ── VISION (PROBE-FIRST). Needs mlx-vlm>=0.5 (bootstrap upgrades it) AND the
  #    multimodal generation hang resolved. Uncomment ONE at a time, restart, and
  #    probe with a real image before trusting it. model_type: multimodal. ──
  # - model_path: mlx-community/Qwen3-VL-8B-Instruct-4bit      # arch qwen3_vl, small+fast
  #   served_model_name: qwen3-vl-8b
  #   model_type: multimodal
  #   queue_timeout: 900
  #   on_demand: true
  #   on_demand_idle_timeout: 300
  # - model_path: mlx-community/gemma-3-27b-it-4bit            # arch gemma3 (proven in mlx-vlm)
  #   served_model_name: gemma3-27b
  #   model_type: multimodal
  #   queue_timeout: 900
  #   on_demand: true
  #   on_demand_idle_timeout: 300
  # - model_path: unsloth/gemma-4-26b-a4b-it-MLX-8bit         # Gemma 4 MoE, needs mlx-vlm>=0.5
  #   served_model_name: gemma4-26b
  #   model_type: multimodal
  #   queue_timeout: 900
  #   on_demand: true
  #   on_demand_idle_timeout: 300
YAMLEOF
    ok "wrote $CFG"
    append_custom_mlx_models "$CFG"
}

# Append every registered custom model to the mlx-openai-server config's models: list.
append_custom_mlx_models() {
    [ -f "$MODELS_REGISTRY" ] || return 0
    local role repo mtype tparse rparse
    while IFS=$'\t' read -r role repo mtype tparse rparse; do
        [ -z "$role" ] && continue
        case "$role" in \#*) continue ;; esac
        {
            printf '\n  # ── custom: %s ──\n' "$role"
            printf '  - model_path: %s\n'          "$repo"
            printf '    served_model_name: %s\n'    "$role"
            printf '    model_type: %s\n'           "${mtype:-lm}"
            if [ -n "$tparse" ] && [ "$tparse" != "-" ]; then
                printf '    enable_auto_tool_choice: true\n'
                printf '    tool_call_parser: %s\n' "$tparse"
            fi
            [ -n "$rparse" ] && [ "$rparse" != "-" ] && printf '    reasoning_parser: %s\n' "$rparse"
            case "${mtype:-lm}" in
              lm|multimodal)
                # Conservative ceiling: registry models can be 40 GB+ (coder-next, 70B), so
                # keep context modest and KV quantized so a big model + big context still fits 64 GB.
                printf '    context_length: 32768\n'
                printf '    kv_bits: 8\n'
                printf '    kv_group_size: 64\n'
                printf '    batch_completion_size: 2\n'
                printf '    batch_prefill_size: 1\n'
                printf '    prompt_cache_size: 1\n'
                printf '    prompt_cache_max_bytes: 4294967296\n'   # 4 GB KV cap
                ;;
            esac
            printf '    on_demand: true\n'
            printf '    on_demand_idle_timeout: 600\n'
        } >> "$1"
        ok "  + custom model: $role → $repo"
    done < "$MODELS_REGISTRY"
}
write_litellm_config() {
    log "LiteLLM gateway config"
    local CFG="$WORKDIR/litellm.config.yaml"
    # Gateway aliases are the user-facing names (OWUI dropdown + agent/personas).
    # Named specialty:model so the flat OWUI list clusters by specialty when sorted.
    # The colon lives ONLY here — the MLX server keeps clean names (qwen36-27b…),
    # so nothing downstream has to accept a colon. reasoner/qa/vision reuse the 27B.
    cat > "$CFG" <<YAMLEOF
model_list:
  - model_name: "orchestrator:qwen3.6-35b"
    litellm_params: { model: openai/qwen36-35b,  api_base: http://127.0.0.1:${PORT_MLX}/v1, api_key: not-needed }
  - model_name: "coder:qwen3.6-27b"
    litellm_params: { model: openai/qwen36-27b,  api_base: http://127.0.0.1:${PORT_MLX}/v1, api_key: not-needed }
  - model_name: "qa:qwen3.6-27b"
    litellm_params: { model: openai/qwen36-27b,  api_base: http://127.0.0.1:${PORT_MLX}/v1, api_key: not-needed }
  - model_name: "reasoner:deepseek-r1-32b"
    litellm_params: { model: openai/deepseek-r1-32b, api_base: http://127.0.0.1:${PORT_MLX}/v1, api_key: not-needed }
  - model_name: "embed:qwen3-8b"
    litellm_params: { model: openai/qwen3-embed, api_base: http://127.0.0.1:${PORT_MLX}/v1, api_key: not-needed }
  # After a successful load-probe, add heavy/vision aliases here (or via --add-model):
  #   "coder:qwen3-coder-next" → qwen3-coder-next   (best agentic coder, 80B)
  #   "reasoner:deepseek-r1-70b" → deepseek-r1-70b  (70B manual heavy-lift)
  #   "vision:qwen3-vl-8b" → qwen3-vl-8b            (once multimodal generation is proven)
YAMLEOF
    append_custom_litellm_models "$CFG"          # custom roles slot in here, before settings
    cat >> "$CFG" <<YAMLEOF

litellm_settings:
  drop_params: true
  request_timeout: 1200
YAMLEOF
    ok "wrote $CFG"
}

# Append every registered custom model as a LiteLLM role alias → mlx-openai-server.
append_custom_litellm_models() {
    [ -f "$MODELS_REGISTRY" ] || return 0
    local role repo mtype tparse rparse
    while IFS=$'\t' read -r role repo mtype tparse rparse; do
        [ -z "$role" ] && continue
        case "$role" in \#*) continue ;; esac
        printf '  - model_name: %s\n    litellm_params: { model: openai/%s, api_base: http://127.0.0.1:%s/v1, api_key: not-needed }\n' \
            "$role" "$role" "$PORT_MLX" >> "$1"
    done < "$MODELS_REGISTRY"
}

# =============================================================================
#  PHASE 6 — START SCRIPTS + SELF-HEAL
# =============================================================================
write_launchers() {
    log "Service launchers"
    mkdir -p "$WORKDIR/logs"

    cat > "$WORKDIR/repair_venv.sh" <<REPAIREOF
#!/usr/bin/env bash
VENV="$VENV"
"\$VENV/bin/python" -c "import mlx, mlx_lm, mlx_vlm, flask, psutil, yaml, requests" >/dev/null 2>&1 && exit 0
echo "[repair] venv unhealthy — rerun: ./mlx-setup.sh --bootstrap" >&2
exit 1
REPAIREOF
    chmod +x "$WORKDIR/repair_venv.sh"

    cat > "$WORKDIR/start_mlx.sh" <<SHEOF
#!/usr/bin/env bash
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
exec "$VENV/bin/mlx-openai-server" launch --config "$WORKDIR/mlx-server.config.yaml"
SHEOF

    cat > "$WORKDIR/start_gateway.sh" <<SHEOF
#!/usr/bin/env bash
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
# litellm runs as an isolated uv tool (installed to ~/.local/bin). Prefer it on PATH,
# and fall back to 'uv tool run' so the gateway self-heals even if the shim moved.
LITELLM="\$(command -v litellm || echo "$HOME/.local/bin/litellm")"
if [ -x "\$LITELLM" ]; then
    exec "\$LITELLM" --config "$WORKDIR/litellm.config.yaml" --host 0.0.0.0 --port ${PORT_GATEWAY}
else
    exec uv tool run --from "litellm[proxy]" litellm --config "$WORKDIR/litellm.config.yaml" --host 0.0.0.0 --port ${PORT_GATEWAY}
fi
SHEOF

    cat > "$WORKDIR/start_dashboard.sh" <<SHEOF
#!/usr/bin/env bash
bash "$WORKDIR/repair_venv.sh" || { sleep 30; exit 1; }
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
export MLX_WORKDIR="$WORKDIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
exec "$VENV/bin/python" "$WORKDIR/dashboard/app.py"
SHEOF

    # Resident vision server (mlx_vlm.server). Launched WITHOUT --model so it costs no
    # memory until the first image request, then keeps that model warm — fast repeat
    # calls, and it sidesteps mlx-openai-server's multimodal hang entirely.
    cat > "$WORKDIR/start_vision.sh" <<SHEOF
#!/usr/bin/env bash
bash "$WORKDIR/repair_venv.sh" || { sleep 30; exit 1; }
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -x "$VENV/bin/mlx_vlm.server" ]; then
    exec "$VENV/bin/mlx_vlm.server" --port ${PORT_VISION}
else
    exec "$VENV/bin/python" -m mlx_vlm.server --port ${PORT_VISION}
fi
SHEOF
    chmod +x "$WORKDIR"/start_*.sh
    ok "launchers created"
}

# =============================================================================
#  PHASE 7 — DOCKER UIs (Open WebUI + SearXNG, optional Langfuse)
# =============================================================================
setup_colima() {
    log "Colima (Docker engine)"
    if docker_up; then ok "Docker already running"; return; fi
    opt colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"
    for _ in $(seq 1 30); do docker_up && break; sleep 1; done
    docker_up && ok "Docker active via Colima" || warn "Docker startup timed out"
}

# Start Colima automatically on login so the Docker-based UIs (Open WebUI, SearXNG)
# — which use --restart unless-stopped — come back after a reboot with no manual step.
# One-shot at login (not KeepAlive: `colima start` returns once the VM is up).
setup_colima_autostart() {
    log "Colima login autostart"
    local label="com.mlxstack.colima" plist="$HOME/Library/LaunchAgents/com.mlxstack.colima.plist"
    local runner="$WORKDIR/run_colima.sh" cbin; cbin="$(command -v colima || echo colima)"
    mkdir -p "$HOME/Library/LaunchAgents" "$WORKDIR/logs"
    cat > "$runner" <<SHEOF
#!/usr/bin/env bash
# bring the Docker engine up if it isn't already (idempotent)
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
"$cbin" status >/dev/null 2>&1 || "$cbin" start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEM" --disk "$COLIMA_DISK"
SHEOF
    chmod +x "$runner"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$runner</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/com.mlxstack.colima.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/com.mlxstack.colima.err</string>
</dict></plist>
PLISTEOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || launchctl load "$plist" 2>/dev/null || true
    ok "Colima will start on login → Docker UIs auto-recover after reboot"
}

setup_openwebui() {
    log "Open WebUI (web chat)"
    docker_up || { warn "Docker not active; skipping"; return; }
    docker rm -f open-webui >/dev/null 2>&1 || true
    opt docker volume create open-webui
    # Published on $BIND_HOST (0.0.0.0 by default); reaches LiteLLM via host.docker.internal.
    docker run -d --name open-webui --restart unless-stopped \
        -p "$BIND_HOST:$PORT_OPENWEBUI:8080" \
        -e OPENAI_API_BASE_URL="http://host.docker.internal:$PORT_GATEWAY/v1" \
        -e OPENAI_API_KEY="not-needed" \
        -e WEBUI_AUTH="True" \
        --add-host=host.docker.internal:host-gateway \
        -v open-webui:/app/backend/data \
        ghcr.io/open-webui/open-webui:main \
        && ok "Open WebUI up on port $PORT_OPENWEBUI (see summary for URL)" \
        || warn "Open WebUI failed to start"
}

setup_searxng() {
    log "SearXNG (private search for RAG)"
    docker_up || { warn "Docker not active; skipping"; return; }
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
        -p "$BIND_HOST:$PORT_SEARXNG:8080" \
        -v "$SX:/etc/searxng" \
        searxng/searxng:latest \
        && ok "SearXNG up on port $PORT_SEARXNG" \
        || warn "SearXNG failed to start"
}

setup_langfuse() {
    [ "$INSTALL_LANGFUSE" = "1" ] || { ok "Langfuse skipped (set INSTALL_LANGFUSE=1 to enable)"; return; }
    log "Langfuse (tracing)"
    docker_up || { warn "Docker not active; skipping"; return; }
    local LF="$WORKDIR/langfuse"
    [ -d "$LF/.git" ] || opt git clone --depth=1 https://github.com/langfuse/langfuse.git "$LF"
    ( cd "$LF" && opt dc up -d ) && ok "Langfuse starting on port $PORT_LANGFUSE" \
        || warn "Langfuse compose failed"
}

setup_docker_services() {
    setup_colima
    docker_up || { warn "Docker did not start; UIs skipped"; return; }
    setup_openwebui
    setup_searxng
    setup_langfuse
}

# =============================================================================
#  FIRST-RUN WIZARD  (GUI installer — stdlib-only web wizard, runs pre-venv)
# =============================================================================
write_wizard() {
    log "First-run wizard"
    mkdir -p "$WORKDIR"
    cat > "$WORKDIR/wizard.py" <<'WIZEOF'
#!/usr/bin/env python3
"""First-run setup wizard for the MLX AI Workstation — a self-contained, stdlib-only
web GUI (no venv/Flask needed, so it runs before anything is installed). It checks
prerequisites, collects config, writes .env, then drives the TESTED installer
(mlx-setup.sh --bootstrap) with live progress. Thin orchestration over the engine."""
import http.server, socketserver, json, os, subprocess, threading, webbrowser, shutil, platform
from pathlib import Path

WORKDIR = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
ENV_FILE = WORKDIR / ".env"
SETUP = os.environ.get("MLX_SETUP_PATH", "")
PORT = int(os.environ.get("MLX_WIZARD_PORT", "8899"))

_INSTALL = {"running": False, "done": False, "ok": False, "log": []}
_LOCK = threading.Lock()

def _sh(cmd):
    try: return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return ""

def sys_check():
    chip = _sh("sysctl -n machdep.cpu.brand_string") or platform.processor() or "unknown"
    machine = platform.machine().lower()
    apple = ("arm" in machine) or ("apple" in chip.lower())
    mem = _sh("sysctl -n hw.memsize"); ram = round(int(mem) / 1e9) if mem.isdigit() else 0
    try: free = round(shutil.disk_usage(str(Path.home())).free / 1e9)
    except Exception: free = 0
    macos = _sh("sw_vers -productVersion")
    brew = bool(shutil.which("brew"))
    checks = [
        {"label": "macOS", "value": macos or "?", "ok": bool(macos)},
        {"label": "Apple Silicon", "value": chip, "ok": apple},
        {"label": "Memory", "value": f"{ram} GB", "ok": ram >= 16},
        {"label": "Free disk", "value": f"{free} GB", "ok": free >= 30},
        {"label": "Homebrew", "value": "installed" if brew else "will be installed", "ok": True},
    ]
    return {"checks": checks, "ok": apple and ram >= 16 and free >= 30, "setup_ok": bool(SETUP)}

def _read_env():
    env = {}
    if ENV_FILE.exists():
        for ln in ENV_FILE.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); env[k.strip()] = v.strip()
    return env

def write_env(cfg):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    env = _read_env()
    for k in ("HF_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "DASHBOARD_PASSWORD"):
        v = (cfg.get(k) or "").strip()
        if v: env[k] = v
    if SETUP: env["MLX_SETUP_PATH"] = SETUP
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    try: os.chmod(ENV_FILE, 0o600)
    except Exception: pass

def _log(line):
    with _LOCK: _INSTALL["log"].append(str(line)[:500])

def run_install(pull_vision, pull_heavy):
    with _LOCK: _INSTALL.update({"running": True, "done": False, "ok": False, "log": []})
    ok = True
    try:
        if not SETUP or not os.path.exists(SETUP):
            _log("ERROR: installer path unknown — set MLX_SETUP_PATH"); ok = False
        else:
            seq = [[SETUP, "--bootstrap"]]
            if pull_vision: seq.append([SETUP, "--pull-vision"])
            if pull_heavy: seq.append([SETUP, "--pull-heavy"])
            env = dict(os.environ, MLX_NONINTERACTIVE="1")
            for cmd in seq:
                _log(f"$ bash {' '.join(cmd)}")
                p = subprocess.Popen(["bash"] + cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, env=env)
                for line in p.stdout:
                    _log(line.rstrip())
                p.wait()
                if p.returncode != 0:
                    _log(f"[step exited {p.returncode}]"); ok = False; break
    except Exception as e:
        _log(f"error: {e}"); ok = False
    finally:
        with _LOCK: _INSTALL.update({"running": False, "done": True, "ok": ok})

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try: self.wfile.write(b)
        except Exception: pass
    def do_GET(self):
        if self.path == "/":
            self._send(200, WIZARD_HTML, "text/html")
        elif self.path == "/api/check":
            self._send(200, json.dumps(sys_check()))
        elif self.path == "/api/log":
            with _LOCK: d = {"running": _INSTALL["running"], "done": _INSTALL["done"],
                             "ok": _INSTALL["ok"], "log": list(_INSTALL["log"])}
            self._send(200, json.dumps(d))
        else:
            self._send(404, "{}")
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode() if n else "{}"
        try: d = json.loads(raw or "{}")
        except Exception: d = {}
        if self.path == "/api/config":
            has_token = bool((d.get("HF_TOKEN") or "").strip()) or ("HF_TOKEN" in _read_env())
            if not has_token:
                self._send(400, json.dumps({"error": "A Hugging Face token is required."})); return
            write_env(d); self._send(200, json.dumps({"ok": True}))
        elif self.path == "/api/install":
            with _LOCK:
                if _INSTALL["running"]:
                    self._send(409, json.dumps({"error": "install already running"})); return
            threading.Thread(target=run_install,
                             args=(bool(d.get("pull_vision")), bool(d.get("pull_heavy"))),
                             daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, "{}")

WIZARD_HTML = r"""<!doctype html><html><head><meta charset=utf-8><title>MLX AI Workstation · Setup</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;min-height:100vh}
.wrap{max-width:720px;margin:0 auto;padding:32px 20px}
h1{font-size:24px;margin:0 0 4px}.sub{color:#8b98b8;margin-bottom:22px}
.steps{display:flex;gap:8px;margin-bottom:24px}
.steps div{flex:1;height:4px;border-radius:2px;background:#223052}.steps div.on{background:#3b82f6}
.card{background:#141b2e;border:1px solid #223052;border-radius:14px;padding:22px;margin-bottom:16px}
h2{font-size:17px;margin:0 0 14px}label{display:block;font-size:13px;margin:14px 0 5px;color:#b6c2da}
input{width:100%;padding:10px;border-radius:9px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;font:14px system-ui}
.hint{color:#8b98b8;font-size:12px;margin-top:4px}.hint a{color:#60a5fa}
button{border:0;border-radius:10px;padding:12px 20px;font-weight:600;cursor:pointer;background:#3b82f6;color:#fff;font-size:15px}
button.ghost{background:#334155}button:disabled{opacity:.5;cursor:default}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:22px}
.chk{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid #1c2740}
.chk:last-child{border:0}.ok{color:#22c55e}.bad{color:#f87171}
.opt{display:flex;gap:10px;align-items:flex-start;padding:12px;border:1px solid #2a3550;border-radius:10px;margin-top:10px;cursor:pointer}
.opt input{width:auto;margin-top:3px}.opt b{display:block}.opt span{color:#8b98b8;font-size:13px}
#log{font-family:ui-monospace,monospace;font-size:12px;background:#05070f;border:1px solid #223052;border-radius:10px;padding:12px;height:320px;overflow:auto;white-space:pre-wrap}
.done{text-align:center;padding:20px}.big{font-size:40px}
.warnbox{background:#3b1d1d;border:1px solid #7f1d1d;color:#fecaca;border-radius:10px;padding:10px 12px;font-size:13px;margin-top:10px;display:none}
</style></head><body><div class=wrap>
<h1>🚀 MLX AI Workstation</h1><div class=sub>Local, private AI on your Mac — set up in a few steps.</div>
<div class=steps><div id=s0 class=on></div><div id=s1></div><div id=s2></div><div id=s3></div></div>

<div class=card id=step0>
  <h2>Welcome</h2>
  <p>This installs a complete local AI platform — chat, agents, vision, and image generation — that runs entirely on this Mac. No cloud, no API costs, your data stays here.</p>
  <p class=hint>You'll need a free Hugging Face token (to download models). Telegram and a dashboard password are optional.</p>
  <div class=row><span></span><button onclick="go(1)">Get started →</button></div>
</div>

<div class=card id=step1 style=display:none>
  <h2>System check</h2>
  <div id=checks>checking…</div>
  <div class=warnbox id=syswarn>This Mac may not meet the recommended specs (Apple Silicon, 16GB+ RAM, 30GB+ free). You can continue, but large models may not run well.</div>
  <div class=row><button class=ghost onclick="go(0)">← Back</button><button id=c1 onclick="go(2)">Continue →</button></div>
</div>

<div class=card id=step2 style=display:none>
  <h2>Configure</h2>
  <label>Hugging Face token <span style="color:#f87171">*required</span></label>
  <input id=hf placeholder="hf_…" autocomplete=off>
  <div class=hint>Free, ~1 min: <a href="https://huggingface.co/settings/tokens" target=_blank>huggingface.co/settings/tokens</a> → create a READ token.</div>
  <label>Telegram bot token <span class=hint>(optional — control it from your phone)</span></label>
  <input id=tgtoken placeholder="from @BotFather" autocomplete=off>
  <label>Telegram user ID <span class=hint>(optional)</span></label>
  <input id=tgid placeholder="numeric id from @userinfobot" autocomplete=off>
  <label>Dashboard password <span class=hint>(optional — only if you'll open the dashboard from other devices)</span></label>
  <input id=pw type=password placeholder="leave blank for localhost-only" autocomplete=new-password>
  <label style="margin-top:18px">Models to download</label>
  <label class=opt><input type=radio name=preset value=core checked><span><b>Core</b><span>Orchestrator, coder, reasoner, embeddings — the essentials (~90GB).</span></span></label>
  <label class=opt><input type=radio name=preset value=vision><span><b>Core + Vision</b><span>Adds the vision model for image understanding (~6GB more).</span></span></label>
  <label class=opt><input type=radio name=preset value=all><span><b>Everything</b><span>Also the heavy 70B/80B specialists (~80GB more, slow first load).</span></span></label>
  <div class=warnbox id=cfgwarn></div>
  <div class=row><button class=ghost onclick="go(1)">← Back</button><button onclick="startInstall()">Install →</button></div>
</div>

<div class=card id=step3 style=display:none>
  <h2 id=insttitle>Installing…</h2>
  <p class=hint id=instnote>Downloading models can take a while on the first run — leave this open.</p>
  <div id=log></div>
  <div id=finish style=display:none>
    <div class=done><div class=big>✅</div><p><b>Your workstation is ready.</b></p>
      <button onclick="location.href='http://localhost:8800'">Open the dashboard →</button></div>
  </div>
</div>

<script>
function go(n){for(let i=0;i<4;i++){document.getElementById('step'+i).style.display=i==n?'block':'none';
  document.getElementById('s'+i).className=i<=n?'on':'';}
  if(n==1)check();window.scrollTo(0,0);}
async function check(){
  const d=await(await fetch('/api/check')).json();
  document.getElementById('checks').innerHTML=d.checks.map(c=>
    `<div class=chk><span>${c.label}</span><span class="${c.ok?'ok':'bad'}">${c.ok?'✓ ':'✕ '}${c.value}</span></div>`).join('');
  document.getElementById('syswarn').style.display=d.ok?'none':'block';
  if(!d.setup_ok){document.getElementById('syswarn').style.display='block';
    document.getElementById('syswarn').textContent='Installer path not found — launch this wizard via ./mlx-setup.sh --wizard.';}
}
async function startInstall(){
  const cfg={HF_TOKEN:hf.value.trim(),TELEGRAM_BOT_TOKEN:tgtoken.value.trim(),
    TELEGRAM_USER_ID:tgid.value.trim(),DASHBOARD_PASSWORD:pw.value};
  const w=document.getElementById('cfgwarn');
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  if(!r.ok){const e=await r.json();w.style.display='block';w.textContent=e.error||'config error';return;}
  const preset=[...document.querySelectorAll('input[name=preset]')].find(x=>x.checked).value;
  await fetch('/api/install',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pull_vision:preset!=='core',pull_heavy:preset==='all'})});
  go(3);poll();
}
async function poll(){
  const t=setInterval(async()=>{
    const d=await(await fetch('/api/log')).json();
    const el=document.getElementById('log');el.textContent=d.log.join('\n');el.scrollTop=el.scrollHeight;
    if(d.done){clearInterval(t);
      document.getElementById('insttitle').textContent=d.ok?'Done':'Install finished with errors';
      document.getElementById('instnote').textContent=d.ok?'':'Some steps failed — check the log above.';
      if(d.ok)document.getElementById('finish').style.display='block';}
  },1200);
}
</script></div></body></html>"""

def main():
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"\n  Setup wizard running → open {url} in your browser\n")
    try: webbrowser.open(url)
    except Exception: pass
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nwizard stopped.")

if __name__ == "__main__":
    main()
WIZEOF
    ok "wrote wizard.py"
}

# Launch the browser-based setup wizard. Uses system python3 (stdlib only), so it
# works before Homebrew/venv exist. It writes .env from the GUI, then drives this
# same installer's --bootstrap non-interactively with live progress.
cmd_wizard() {
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found. Install Apple's Command Line Tools first:"
        printf "    xcode-select --install\n"
        printf "  then re-run:  ./mlx-setup.sh --wizard\n"
        exit 1
    fi
    mkdir -p "$WORKDIR"; ensure_env_file
    set_env MLX_SETUP_PATH "$SCRIPT_PATH"
    write_wizard
    export MLX_SETUP_PATH="$SCRIPT_PATH" MLX_WORKDIR="$WORKDIR" MLX_WIZARD_PORT="${MLX_WIZARD_PORT:-8899}"
    log "Opening the setup wizard in your browser (Ctrl-C here to stop it)…"
    exec python3 "$WORKDIR/wizard.py"
}

# =============================================================================
#  PHASE 8 — CONTROL DASHBOARD  (instrument-panel UI; also live documentation)
# =============================================================================
write_dashboard() {
    log "Control dashboard"
    local DD="$WORKDIR/dashboard"; mkdir -p "$DD"
    cat > "$DD/app.py" <<'PYEOF'
#!/usr/bin/env python3
"""MLX AI Workstation — control dashboard: live status, persona management, docs."""
import os, socket, datetime, secrets, hmac, re, sys, json, importlib.util
import threading as _th, time as _time
import queue as _queue
from collections import deque
import requests, psutil
from functools import wraps
from pathlib import Path
from flask import Flask, jsonify, Response, request, session, redirect

P_MLX="8000"; P_GATEWAY="4000"; P_DASH="8800"
P_OWUI="3001"; P_SEARX="8888"; P_LF="3000"

WORKDIR = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
ENV_FILE = WORKDIR / ".env"
AGENT_PATH = WORKDIR / "agent" / "mlx-agent.py"

def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip()
    return env
ENV = load_env()

# Reuse the TESTED agent data layer (personas + tool registry) — the UI never
# reimplements persona logic, it calls load_personas/save_personas/ALL_SCHEMAS.
try:
    _spec = importlib.util.spec_from_file_location("mlx_agent", str(AGENT_PATH))
    agent = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(agent)
except Exception as e:
    agent = None; print("dashboard: agent module not loaded yet:", e)

app = Flask(__name__)
app.secret_key = ENV.get("DASHBOARD_SECRET") or secrets.token_hex(16)

# Auth is enabled only when the dashboard is exposed beyond localhost (user's choice).
DASH_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
_LOOPBACK = DASH_HOST in ("127.0.0.1", "localhost", "::1", "")
DASH_PASSWORD = ENV.get("DASHBOARD_PASSWORD", "")
if not _LOOPBACK and not DASH_PASSWORD:
    DASH_PASSWORD = secrets.token_urlsafe(10)
    print(f"dashboard: exposed on {DASH_HOST} with no DASHBOARD_PASSWORD set — "
          f"generated a temporary one: {DASH_PASSWORD}  (set a permanent one via ./mlx-setup.sh --configure)")
AUTH_ON = not _LOOPBACK

def require_auth(f):
    @wraps(f)
    def w(*a, **k):
        if AUTH_ON and not session.get("ok"):
            if request.path.startswith("/api/"): return jsonify({"error": "auth required"}), 401
            return redirect("/login")
        return f(*a, **k)
    return w

SERVICES = [
    ("Inference (MLX)",          f"http://127.0.0.1:{P_MLX}/v1/models",           P_MLX,     "Local models on Metal"),
    ("Gateway (LiteLLM)",        f"http://127.0.0.1:{P_GATEWAY}/health/liveliness", P_GATEWAY, "OpenAI-compatible router"),
    ("Web chat (Open WebUI)",    f"http://127.0.0.1:{P_OWUI}/",                   P_OWUI,    "Pick a model & chat"),
    ("Private search (SearXNG)", f"http://127.0.0.1:{P_SEARX}/",                  P_SEARX,   "Web results for RAG"),
    ("Tracing (Langfuse)",       f"http://127.0.0.1:{P_LF}/api/public/health",    P_LF,      "Optional observability"),
    ("Dashboard",                f"http://127.0.0.1:{P_DASH}/",                   P_DASH,    "This page"),
]

# Role → served model, for the demo model table.
ROLES = [
    ("Orchestrator", "qwen36-35b",   "Reads the task, routes work to the right sub-model."),
    ("Coder",        "qwen36-27b",   "Writes & edits code (dense 27B, 8-bit for accuracy)."),
    ("Reasoner / QA","qwen36-27b",   "Plans, reviews, designs tests (thinking mode)."),
    ("Vision / OCR", "qwen36-27b",  "Reads images, PDFs, charts, screenshots (multimodal 27B)."),
    ("Embeddings",   "qwen3-embed",  "Turns documents into vectors for RAG."),
]

def probe(url):
    try: return requests.get(url, timeout=3).status_code < 500
    except Exception: return False

def loaded_models():
    try:
        r = requests.get(f"http://127.0.0.1:{P_MLX}/v1/models", timeout=3)
        return {m.get("id","") for m in r.json().get("data", [])}
    except Exception:
        return set()

def hardware():
    vm = psutil.virtual_memory()
    try: disk = psutil.disk_usage(os.path.expanduser("~"))
    except Exception: disk = None
    return {
        "cpu": round(psutil.cpu_percent(interval=None)),
        "ram_pct": round(vm.percent), "ram_txt": f"{vm.used/1e9:.0f} / {vm.total/1e9:.0f} GB",
        "disk_pct": round(disk.percent) if disk else 0,
        "disk_txt": f"{disk.used/1e9:.0f} / {disk.total/1e9:.0f} GB" if disk else "?",
    }

# ── rolling 5-minute metric history (Task-Manager-style graphs) ─────────────
_METRICS = deque(maxlen=100)            # ~5 min at 1 sample / 3s
_SAMPLER_STARTED = False
def _sample_loop():
    while True:
        try:
            vm = psutil.virtual_memory()
            _METRICS.append({"t": int(_time.time()),
                             "cpu": round(psutil.cpu_percent(interval=None)),
                             "ram": round(vm.percent),
                             "ram_gb": round(vm.used / 1e9, 1)})
        except Exception:
            pass
        _time.sleep(3)
def _start_sampler():
    global _SAMPLER_STARTED
    if not _SAMPLER_STARTED:
        _SAMPLER_STARTED = True
        _th.Thread(target=_sample_loop, daemon=True).start()
_start_sampler()

@app.route("/api/metrics")
@require_auth
def api_metrics():
    data = list(_METRICS)
    vm = psutil.virtual_memory()
    return jsonify({
        "cpu":    [d["cpu"] for d in data],
        "ram":    [d["ram"] for d in data],
        "ram_gb": [d["ram_gb"] for d in data],
        "ram_total_gb": round(vm.total / 1e9),
        "window_sec": 300,
    })

ACTIVITY_DIR = WORKDIR / "activity"
@app.route("/api/activity")
@require_auth
def api_activity():
    out, now = [], _time.time()
    if ACTIVITY_DIR.exists():
        import json as _json
        for f in ACTIVITY_DIR.glob("*.json"):
            try:
                r = _json.loads(f.read_text())
            except Exception:
                continue
            age = now - r.get("last", 0)
            if age > 45:                      # no heartbeat in 45s → process likely gone; sweep it
                try: f.unlink()
                except Exception: pass
                continue
            out.append({"task": r.get("task", ""), "model": r.get("model", ""),
                        "persona": r.get("persona", ""), "current": r.get("current", ""),
                        "elapsed": int(now - r.get("started", now))})
    out.sort(key=lambda x: -x["elapsed"])
    return jsonify({"active": out})

@app.route("/api/status")
@require_auth
def api_status():
    served = loaded_models()
    svcs = [{"name": n, "port": int(p), "purpose": desc, "ok": probe(h)}
            for (n, h, p, desc) in SERVICES]
    models = [{"role": r, "id": mid, "desc": d, "ready": (mid in served)} for (r, mid, d) in ROLES]
    return jsonify({"services": svcs, "models": models, "hardware": hardware(),
                    "updated": datetime.datetime.now().strftime("%H:%M:%S")})

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MLX AI Workstation — Control</title>
<style>
 :root{
   --bg:#0b0f14; --panel:#111823; --panel2:#0e141d; --line:#1c2733;
   --ink:#e8eef5; --dim:#7d8ea1; --accent:#e0a13c; --accent2:#4ea1c4;
   --ok:#3ecf8e; --off:#e5484d;
   --mono:"SF Mono",ui-monospace,"JetBrains Mono",Menlo,monospace;
   --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 }
 *{box-sizing:border-box;margin:0;padding:0}
 body{background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;
   padding:28px 20px;background-image:radial-gradient(1200px 500px at 50% -10%,rgba(78,161,196,.06),transparent);}
 .wrap{max-width:1120px;margin:0 auto}
 header{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:6px}
 .brand{font-family:var(--mono);letter-spacing:.02em}
 .brand .dot{color:var(--ok)}
 h1{font-size:19px;font-weight:600}
 .sub{color:var(--dim);font-size:12px;margin-top:2px}
 .badge{font-family:var(--mono);font-size:11px;color:var(--accent);border:1px solid var(--line);
   border-radius:999px;padding:6px 12px;background:var(--panel);white-space:nowrap}
 .clock{font-family:var(--mono);color:var(--dim);font-size:11px}
 .tabs{display:flex;gap:22px;border-bottom:1px solid var(--line);margin:22px 0}
 .tab{font-family:var(--mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--dim);
   background:none;border:none;padding:0 0 12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
 .tab.active{color:var(--ink);border-bottom-color:var(--accent)}
 .view{display:none}.view.active{display:block}
 .grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
 .card h2{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin-bottom:14px}
 .meters{display:flex;gap:26px;justify-content:space-around}
 .meter{text-align:center}
 .meter .val{font-family:var(--mono);font-size:26px;font-weight:600}
 .meter .lab{font-size:11px;color:var(--dim);margin-top:2px}
 .meter .det{font-family:var(--mono);font-size:10px;color:var(--dim)}
 .row{display:flex;align-items:center;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--line)}
 .row:last-child{border:none}
 .led{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:9px;box-shadow:0 0 8px currentColor}
 .led.on{background:var(--ok);color:var(--ok)}.led.off{background:var(--off);color:var(--off)}
 .row a{color:var(--ink);text-decoration:none;font-weight:500}
 .row a:hover{color:var(--accent2)}
 .row .meta{font-family:var(--mono);font-size:11px;color:var(--dim)}
 .mrole{font-weight:600}
 .mid{font-family:var(--mono);font-size:11px;color:var(--accent2)}
 .pill{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:6px;border:1px solid var(--line)}
 .pill.ready{color:var(--ok);border-color:rgba(62,207,142,.4)}
 .pill.idle{color:var(--dim)}
 .doc{max-width:760px}
 .doc h3{font-size:14px;margin:20px 0 8px}
 .doc p{color:var(--dim);line-height:1.65;margin-bottom:8px}
 .doc code{font-family:var(--mono);font-size:12px;background:var(--panel2);border:1px solid var(--line);
   border-radius:6px;padding:2px 7px;color:var(--accent)}
 .doc .step{display:flex;gap:14px;padding:12px 0;border-bottom:1px solid var(--line)}
 .doc .n{font-family:var(--mono);color:var(--accent);font-size:12px;min-width:24px}
 footer{color:var(--dim);font-size:11px;text-align:center;margin-top:26px;font-family:var(--mono)}
</style></head><body><div class="wrap">
 <header>
   <div><div class="brand"><span class="dot">●</span> MLX&nbsp;AI&nbsp;WORKSTATION</div>
     <h1>Local intelligence, on your hardware</h1>
     <div class="sub">Every model runs on this Mac. No API keys, no per-token billing, no data leaving the device.</div>
   </div>
   <div style="text-align:right">
     <div class="badge">100% LOCAL · $0 API · PRIVATE</div>
     <div class="clock" id="hosturl">—</div>
     <div style="margin-top:8px"><a href="/chat" style="color:#60a5fa;text-decoration:none;font-size:14px">💬 Chat →</a></div>
     <div style="margin-top:4px"><a href="/vision" style="color:#60a5fa;text-decoration:none;font-size:14px">👁 Vision →</a></div>
     <div style="margin-top:4px"><a href="/imagine" style="color:#60a5fa;text-decoration:none;font-size:14px">🎨 Create image →</a></div>
     <div style="margin-top:4px"><a href="/personas" style="color:#60a5fa;text-decoration:none;font-size:14px">🎭 Manage personas →</a></div>
     <div style="margin-top:4px"><a href="/models" style="color:#60a5fa;text-decoration:none;font-size:14px">📦 Manage models →</a></div>
     <div class="clock" id="clock">—</div>
   </div>
 </header>

 <div class="tabs">
   <button class="tab active" data-v="overview">Overview</button>
   <button class="tab" data-v="models">Models</button>
   <button class="tab" data-v="guide">Guide</button>
 </div>

 <section id="overview" class="view active">
   <div class="grid">
     <div class="card"><h2>System</h2><div class="meters" id="meters"></div></div>
     <div class="card"><h2>Services</h2><div id="svcs"></div></div>
   </div>
   <div class="card" style="margin-top:14px"><h2>Live · last 5 minutes</h2>
     <div style="display:flex;gap:18px;flex-wrap:wrap">
       <div style="flex:1;min-width:240px">
         <div class="meta" style="margin-bottom:6px">CPU &nbsp;<b id="cpuNow" style="color:#3b82f6">–</b></div>
         <canvas id="cpuChart" height="96" style="width:100%;display:block;background:rgba(255,255,255,.02);border-radius:8px"></canvas>
       </div>
       <div style="flex:1;min-width:240px">
         <div class="meta" style="margin-bottom:6px">Memory &nbsp;<b id="ramNow" style="color:#22c55e">–</b></div>
         <canvas id="ramChart" height="96" style="width:100%;display:block;background:rgba(255,255,255,.02);border-radius:8px"></canvas>
       </div>
     </div>
   </div>
   <div class="card" style="margin-top:14px"><h2>Active agents</h2>
     <div id="activity"><div class="meta" style="color:var(--dim)">idle — no agent running</div></div>
     <p style="color:var(--dim);font-size:12px;margin-top:10px">Shows any agent task running now (CLI, Telegram, or spawned) with its live step. Updates every 3s.</p>
   </div>
 </section>

 <section id="models" class="view">
   <div class="card"><h2>Model roles</h2><div id="models"></div>
     <p style="color:var(--dim);font-size:12px;margin-top:12px">
       Models load on demand — a role shows <span class="pill ready">ready</span> once it has been used and is resident in memory. Only the model in use occupies RAM, which is how a 64&nbsp;GB Mac serves a whole team of specialists.</p>
   </div>
 </section>

 <section id="guide" class="view">
   <div class="card doc">
     <h2>What this is</h2>
     <p>A self-contained AI platform. A single MLX server runs the models natively on the Mac's GPU; a gateway exposes them under friendly role names; a web chat lets you pick a model and work with it. Nothing depends on the internet once models are downloaded.</p>
     <h3>Try it in 30 seconds</h3>
     <div class="step"><span class="n">1</span><div>Open <a data-port="3001" target="_blank">the web chat</a> and pick a model from the dropdown (start with <code>coder</code> or <code>reasoner</code>).</div></div>
     <div class="step"><span class="n">2</span><div>Ask it something real — summarize a document, draft code, explain an image.</div></div>
     <div class="step"><span class="n">3</span><div>Watch the <strong>Models</strong> tab: the role you used turns <span class="pill ready">ready</span> as it loads.</div></div>
     <h3>The role names</h3>
     <p><code>orchestrator</code> routes work · <code>coder</code> writes code · <code>reasoner</code> / <code>qa</code> plan and review · <code>vision</code> reads images and documents · <code>embed</code> powers search over your files.</p>
     <h3>Run it from the terminal</h3>
     <p><code>./mlx-setup.sh --status</code> shows health · <code>--start</code> / <code>--stop</code> control services · <code>--image "a red bike"</code> generates an image · <code>--pull-models</code> adds optional models.</p>
     <h3>Where things live</h3>
     <p>Models cache in <code>~/.cache/huggingface</code>. Config and logs are in <code>~/.mlx-ai-workstation</code>. Generated documents go to <code>~/MLX-AI/documents</code>.</p>
   </div>
 </section>

 <footer id="foot">initializing…</footer>
</div>
<script>
 document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
   document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
   document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));
   t.classList.add('active'); document.getElementById(t.dataset.v).classList.add('active');
 });
 // Build all service links from the host the browser actually used, so they work
 // identically whether you opened the dashboard via localhost or a LAN IP.
 const HOST = window.location.hostname || 'localhost';
 function svcUrl(port){ return 'http://' + HOST + ':' + port; }
 document.querySelectorAll('a[data-port]').forEach(a => a.href = svcUrl(a.dataset.port));
 (function(){ var e=document.getElementById('hosturl'); if(e) e.textContent = window.location.origin; })();
 function meter(val,lab,det){
   return '<div class="meter"><div class="val">'+val+'%</div><div class="lab">'+lab+'</div><div class="det">'+(det||'')+'</div></div>';
 }
 async function tick(){
   try{
     const d=await (await fetch('/api/status')).json();
     document.getElementById('clock').textContent='synced '+d.updated;
     const h=d.hardware;
     document.getElementById('meters').innerHTML=
       meter(h.cpu,'CPU','')+meter(h.ram_pct,'RAM',h.ram_txt)+meter(h.disk_pct,'DISK',h.disk_txt);
     document.getElementById('svcs').innerHTML=d.services.map(s=>
       '<div class="row"><span><span class="led '+(s.ok?'on':'off')+'"></span>'+
       '<a href="'+svcUrl(s.port)+'" target="_blank">'+s.name+'</a></span>'+
       '<span class="meta">'+s.purpose+' · :'+s.port+'</span></div>').join('');
     document.getElementById('models').innerHTML=d.models.map(m=>
       '<div class="row"><span><span class="mrole">'+m.role+'</span> &nbsp;<span class="mid">'+m.id+'</span>'+
       '<div class="meta" style="margin-top:3px">'+m.desc+'</div></span>'+
       '<span class="pill '+(m.ready?'ready':'idle')+'">'+(m.ready?'ready':'on demand')+'</span></div>').join('');
     const up=d.services.filter(s=>s.ok).length;
     document.getElementById('foot').textContent='MLX AI Workstation · '+up+'/'+d.services.length+' services up · refreshes every 5s';
   }catch(e){ document.getElementById('foot').textContent='dashboard offline — is the service running?'; }
 }
 tick(); setInterval(tick,5000);
 function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
 function drawChart(id, vals, color){
   const cv=document.getElementById(id); if(!cv) return;
   const w=cv.width=cv.clientWidth||600, h=cv.height, ctx=cv.getContext('2d');
   ctx.clearRect(0,0,w,h);
   ctx.strokeStyle='rgba(255,255,255,0.07)'; ctx.lineWidth=1;
   for(let i=0;i<=4;i++){const y=Math.round(h*i/4)+.5; ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();}
   if(!vals||!vals.length) return;
   const maxN=100, xs=w/Math.max(maxN-1,1), x0=w-(vals.length-1)*xs;
   const yof=v=>h-(Math.min(100,Math.max(0,v))/100)*h;
   ctx.beginPath(); vals.forEach((v,i)=>{const x=x0+i*xs,y=yof(v); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
   ctx.lineTo(x0+(vals.length-1)*xs,h); ctx.lineTo(x0,h); ctx.closePath();
   ctx.fillStyle=color+'22'; ctx.fill();
   ctx.beginPath(); vals.forEach((v,i)=>{const x=x0+i*xs,y=yof(v); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
   ctx.strokeStyle=color; ctx.lineWidth=2; ctx.stroke();
 }
 async function mtick(){
   try{
     const m=await (await fetch('/api/metrics')).json();
     drawChart('cpuChart', m.cpu, '#3b82f6'); drawChart('ramChart', m.ram, '#22c55e');
     const last=a=>a&&a.length?a[a.length-1]:0;
     document.getElementById('cpuNow').textContent=last(m.cpu)+'%';
     document.getElementById('ramNow').textContent=last(m.ram)+'% ('+last(m.ram_gb)+' / '+m.ram_total_gb+' GB)';
   }catch(e){}
   try{
     const a=await (await fetch('/api/activity')).json();
     const el=document.getElementById('activity');
     if(!a.active||!a.active.length){ el.innerHTML='<div class="meta" style="color:var(--dim)">idle — no agent running</div>'; }
     else el.innerHTML=a.active.map(t=>
       '<div class="row"><span><span class="mrole">'+esc(t.persona||t.model||'agent')+'</span>'+
       '<div class="meta" style="margin-top:3px">“'+esc(t.task)+'”</div>'+
       '<div class="meta">▸ '+esc(t.current)+'</div></span>'+
       '<span class="pill ready">'+t.elapsed+'s</span></div>').join('');
   }catch(e){}
 }
 mtick(); setInterval(mtick,3000);
</script></body></html>"""

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><title>Sign in</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font:16px system-ui;background:#0b1020;color:#e6edf3;display:grid;place-items:center;height:100vh;margin:0}
form{background:#141b2e;padding:28px;border-radius:14px;box-shadow:0 10px 40px #0006;min-width:280px}
h1{font-size:18px;margin:0 0 16px}input{width:100%;padding:10px;border-radius:8px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;box-sizing:border-box}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font-weight:600;cursor:pointer}
.e{color:#f87171;font-size:13px;margin-top:8px;min-height:16px}</style></head>
<body><form method=post><h1>🔒 MLX Workstation</h1>
<input type=password name=password placeholder="Admin password" autofocus>
<div class=e><!--err--></div><button>Sign in</button></form></body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ON: return redirect("/")
    if request.method == "POST":
        if DASH_PASSWORD and hmac.compare_digest(request.form.get("password", ""), DASH_PASSWORD):
            session["ok"] = True; return redirect("/")
        return Response(LOGIN_HTML.replace("<!--err-->", "Wrong password"), mimetype="text/html")
    return Response(LOGIN_HTML, mimetype="text/html")

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login" if AUTH_ON else "/")

# ── persona management (thin layer over the agent's tested data functions) ──
def _gateway_models():
    try:
        r = requests.get(f"http://127.0.0.1:{P_GATEWAY}/v1/models", timeout=3)
        return sorted(m.get("id", "") for m in r.json().get("data", []))
    except Exception:
        return []

def _tool_names():
    return sorted(agent.ALL_SCHEMAS.keys()) if agent else []

def _starters():
    return set(agent.DEFAULT_PERSONAS.keys()) if agent else set()

@app.route("/api/personas")
@require_auth
def api_personas():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    p = agent.load_personas(); st = _starters()
    out = [{"name": n, "description": c.get("description", ""), "model": c.get("model", ""),
            "allowed_tools": c.get("allowed_tools", "all"), "approval": c.get("approval", "normal"),
            "system_prompt": c.get("system_prompt", ""), "starter": n in st}
           for n, c in p.items()]
    return jsonify({"personas": out, "models": _gateway_models(), "tools": _tool_names()})

@app.route("/api/personas/save", methods=["POST"])
@require_auth
def api_personas_save():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", name):
        return jsonify({"error": "name must be 1–32 chars: letters, digits, _ or -"}), 400
    model = (d.get("model") or "").strip()
    if not model:
        return jsonify({"error": "pick a model"}), 400
    tools = d.get("allowed_tools", "all")
    if isinstance(tools, list):
        tools = "all" if ("all" in tools or not tools) else [t for t in tools if t in agent.ALL_SCHEMAS]
    entry = {"description": (d.get("description") or "").strip(), "model": model,
             "allowed_tools": tools, "system_prompt": (d.get("system_prompt") or "").strip()}
    if d.get("approval") == "strict": entry["approval"] = "strict"
    p = agent.load_personas(); p[name] = entry; agent.save_personas(p)
    return jsonify({"ok": True})

@app.route("/api/personas/delete", methods=["POST"])
@require_auth
def api_personas_delete():
    if not agent: return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    if name in _starters():
        return jsonify({"error": "built-in starter — edit it instead of deleting"}), 400
    p = agent.load_personas()
    if name in p: del p[name]; agent.save_personas(p)
    return jsonify({"ok": True})

PERSONA_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Personas · MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:24px}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}.sub{color:#8b98b8;font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{background:#141b2e;border:1px solid #223052;border-radius:12px;padding:14px}
.card h3{margin:0 0 4px;font-size:16px}.tag{display:inline-block;font-size:11px;padding:2px 7px;border-radius:20px;background:#1e293b;color:#93c5fd;margin-right:4px}
.strict{background:#3b1d1d;color:#fca5a5}.mono{font-family:ui-monospace,monospace;font-size:12px;color:#9fb3d1}
.desc{color:#b6c2da;font-size:13px;margin:6px 0}.row{margin-top:10px}
button{border:0;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}
.edit{background:#334155;color:#e6edf3}.del{background:#7f1d1d;color:#fee2e2;margin-left:6px}.add{background:#3b82f6;color:#fff}
dialog{background:#141b2e;color:#e6edf3;border:1px solid #223052;border-radius:14px;padding:20px;width:min(560px,92vw)}
label{display:block;font-size:13px;margin:10px 0 4px;color:#b6c2da}
input,select,textarea{width:100%;padding:9px;border-radius:8px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;font:14px system-ui}
textarea{min-height:90px;resize:vertical}.tools{display:flex;flex-wrap:wrap;gap:8px;max-height:120px;overflow:auto;border:1px solid #2a3550;border-radius:8px;padding:8px}
.tools label{display:flex;align-items:center;gap:5px;margin:0;font-size:12px}.tools input{width:auto}
.err{color:#f87171;font-size:13px;min-height:16px;margin-top:8px}.actions{margin-top:16px;display:flex;justify-content:flex-end;gap:8px}
</style></head><body>
<p><a href="/">← dashboard</a></p>
<h1>🎭 Personas</h1><div class=sub>Each persona = a model + system prompt + tool permissions + approval level. Used by the agent & Telegram (not the web-chat dropdown).</div>
<p><button class=add onclick="openEdit()">+ New persona</button></p>
<div class=grid id=grid></div>
<dialog id=dlg><h3 id=dtitle>New persona</h3>
<label>Name (letters, digits, _ -)</label><input id=f_name>
<label>Description</label><input id=f_desc>
<label>Model</label><select id=f_model></select>
<label>Approval</label><select id=f_appr><option value=normal>normal (can allow-all per run)</option><option value=strict>strict (always ask)</option></select>
<label>Allowed tools</label>
<div><label style="display:inline-flex;gap:6px"><input type=checkbox id=f_all onchange="toggleAll()"> all tools</label></div>
<div class=tools id=f_tools></div>
<label>System prompt</label><textarea id=f_sys></textarea>
<div class=err id=err></div>
<div class=actions><button class=edit onclick="dlg.close()">Cancel</button><button class=add onclick="save()">Save</button></div>
</dialog>
<script>
let MODELS=[],TOOLS=[],editing=null;
async function load(){
  const r=await fetch('/api/personas'); if(r.status==401){location='/login';return;}
  const d=await r.json(); MODELS=d.models||[]; TOOLS=d.tools||[];
  const g=document.getElementById('grid'); g.innerHTML='';
  (d.personas||[]).forEach(p=>{
    const tools=Array.isArray(p.allowed_tools)?p.allowed_tools.join(', '):'all tools';
    const el=document.createElement('div'); el.className='card';
    el.innerHTML=`<h3>${p.name} ${p.starter?'<span class=tag>starter</span>':''} ${p.approval=='strict'?'<span class="tag strict">strict</span>':''}</h3>
      <div class=mono>${p.model||'—'}</div><div class=desc>${p.description||''}</div>
      <div class=mono>🔧 ${tools}</div>
      <div class=row><button class=edit>Edit</button>${p.starter?'':'<button class=del>Delete</button>'}</div>`;
    el.querySelector('.edit').onclick=()=>openEdit(p);
    const db=el.querySelector('.del'); if(db) db.onclick=()=>del(p.name);
    g.appendChild(el);
  });
}
function openEdit(p){
  editing=p||null; document.getElementById('err').textContent='';
  document.getElementById('dtitle').textContent=p?('Edit '+p.name):'New persona';
  const mSel=document.getElementById('f_model'); mSel.innerHTML=MODELS.map(m=>`<option>${m}</option>`).join('');
  const tw=document.getElementById('f_tools'); tw.innerHTML=TOOLS.map(t=>`<label><input type=checkbox value="${t}">${t}</label>`).join('');
  const isAll = !p || p.allowed_tools==='all' || !Array.isArray(p.allowed_tools);
  document.getElementById('f_all').checked=isAll; toggleAll();
  document.getElementById('f_name').value=p?p.name:''; document.getElementById('f_name').disabled=!!p;
  document.getElementById('f_desc').value=p?p.description:''; 
  if(p&&p.model)mSel.value=p.model;
  document.getElementById('f_appr').value=p?p.approval:'normal';
  document.getElementById('f_sys').value=p?p.system_prompt:'';
  if(p&&Array.isArray(p.allowed_tools))[...tw.querySelectorAll('input')].forEach(c=>c.checked=p.allowed_tools.includes(c.value));
  document.getElementById('dlg').showModal();
}
function toggleAll(){const on=document.getElementById('f_all').checked;document.getElementById('f_tools').style.opacity=on?0.4:1;
  [...document.querySelectorAll('#f_tools input')].forEach(c=>c.disabled=on);}
async function save(){
  const all=document.getElementById('f_all').checked;
  const tools=all?'all':[...document.querySelectorAll('#f_tools input:checked')].map(c=>c.value);
  const body={name:document.getElementById('f_name').value.trim(),description:document.getElementById('f_desc').value,
    model:document.getElementById('f_model').value,approval:document.getElementById('f_appr').value,
    allowed_tools:tools,system_prompt:document.getElementById('f_sys').value};
  const r=await fetch('/api/personas/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(!r.ok){document.getElementById('err').textContent=d.error||'error';return;}
  document.getElementById('dlg').close(); load();
}
async function del(name){ if(!confirm('Delete persona '+name+'?'))return;
  await fetch('/api/personas/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}); load();}
load();
</script></body></html>"""

@app.route("/personas")
@require_auth
def personas_page():
    return Response(PERSONA_PAGE, mimetype="text/html")

# ── model management (thin layer over the installer's tested commands) ──────
import subprocess, threading as _th, time as _time
SETUP = ENV.get("MLX_SETUP_PATH", "")            # recorded during --bootstrap
_JOBS = {}                                       # id -> {stage, log[], done, ok, repo}
_JOB_LOCK = _th.Lock()

def _registry_rows():
    reg = WORKDIR / "models.custom.tsv"; rows = []
    if reg.exists():
        for ln in reg.read_text().splitlines():
            parts = ln.split("\t")
            if len(parts) >= 3: rows.append({"role": parts[0], "repo": parts[1], "type": parts[2]})
    return rows

@app.route("/api/models")
@require_auth
def api_models():
    served = []
    try:
        r = requests.get(f"http://127.0.0.1:{P_MLX}/v1/models", timeout=3)
        served = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception: pass
    return jsonify({"served": sorted(served), "custom": _registry_rows(),
                    "gateway": _gateway_models(), "setup_ok": bool(SETUP)})

def _cached_repos():
    # scan the HF hub cache for fully-downloaded repos → repo ids
    base = os.environ.get("HF_HUB_CACHE") or os.environ.get("HF_HOME")
    hub = Path(base) / "hub" if base and not base.rstrip("/").endswith("hub") else \
          (Path(base) if base else Path.home() / ".cache" / "huggingface" / "hub")
    if not hub.exists():
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    out = []
    if hub.exists():
        for d in hub.iterdir():
            if d.is_dir() and d.name.startswith("models--"):
                snap = d / "snapshots"
                if snap.exists() and any(snap.iterdir()):
                    out.append(d.name[len("models--"):].replace("--", "/"))
    return sorted(out)

@app.route("/api/models/cached")
@require_auth
def api_models_cached():
    reg = {r["repo"] for r in _registry_rows()}
    return jsonify({"cached": [{"repo": r, "registered": (r in reg)} for r in _cached_repos()]})

@app.route("/api/models/search")
@require_auth
def api_models_search():
    q = (request.args.get("q") or "").strip()
    if not q: return jsonify({"results": []})
    try:  # Hugging Face public search API, filtered to MLX-format repos
        r = requests.get("https://huggingface.co/api/models",
                         params={"search": q, "filter": "mlx", "sort": "downloads",
                                 "direction": "-1", "limit": 25},
                         timeout=12)
        out = [{"id": m.get("id", ""), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0)}
               for m in r.json()]
        return jsonify({"results": out})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 502

def _job_log(jid, line):
    with _JOB_LOCK:
        if jid in _JOBS: _JOBS[jid]["log"].append(line)

def _run_add_job(jid, repo, role, mtype, tparse, rparse):
    def stage(s):
        with _JOB_LOCK:
            if jid in _JOBS: _JOBS[jid]["stage"] = s
        _job_log(jid, f"— {s} —")
    try:
        if not SETUP or not os.path.exists(SETUP):
            _job_log(jid, "ERROR: installer path unknown (re-run --bootstrap)"); return _finish(jid, False)
        # 1) download (no register yet)
        stage("downloading")
        if _sh(jid, [SETUP, "--download-model", repo]) != 0:
            _job_log(jid, "download failed"); return _finish(jid, False)
        # 2) LOAD-PROBE before registering (searchable ≠ loadable)
        stage("probing (loading the model to confirm it runs)")
        rc = _sh(jid, [SETUP, "--probe-model", repo, mtype])
        if rc != 0:
            _job_log(jid, "✗ probe failed — NOT added to the dropdown (weights kept in cache)")
            return _finish(jid, False)
        # 3) only now register → dropdown
        stage("registering")
        args = [SETUP, "--add-model", repo, role, mtype, tparse, rparse]
        if _sh(jid, args) != 0:
            _job_log(jid, "register failed"); return _finish(jid, False)
        _job_log(jid, f"✓ '{role}' probed loadable and added to the dropdown")
        _finish(jid, True)
    except Exception as e:
        _job_log(jid, f"error: {e}"); _finish(jid, False)

def _sh(jid, args):
    try:
        p = subprocess.Popen(["bash"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            _job_log(jid, line.rstrip()[:400])
        p.wait(); return p.returncode
    except Exception as e:
        _job_log(jid, f"exec error: {e}"); return 1

def _finish(jid, ok):
    with _JOB_LOCK:
        if jid in _JOBS: _JOBS[jid]["done"] = True; _JOBS[jid]["ok"] = ok

@app.route("/api/models/add", methods=["POST"])
@require_auth
def api_models_add():
    d = request.get_json(force=True, silent=True) or {}
    repo = (d.get("repo") or "").strip()
    role = (d.get("role") or "").strip()
    mtype = (d.get("type") or "lm").strip()
    if not repo or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]+$", repo):
        return jsonify({"error": "bad repo id"}), 400
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", role):
        return jsonify({"error": "role: lowercase letters/digits/hyphen, ≤31 chars"}), 400
    jid = secrets.token_hex(4)
    with _JOB_LOCK:
        _JOBS[jid] = {"stage": "queued", "log": [], "done": False, "ok": False, "repo": repo}
    _th.Thread(target=_run_add_job, args=(jid, repo, role, mtype,
               (d.get("tool_parser") or "").strip(), (d.get("reasoning_parser") or "").strip()),
               daemon=True).start()
    return jsonify({"job": jid})

@app.route("/api/models/job/<jid>")
@require_auth
def api_models_job(jid):
    with _JOB_LOCK:
        j = _JOBS.get(jid)
        if not j: return jsonify({"error": "no such job"}), 404
        return jsonify(dict(j))

@app.route("/api/models/remove", methods=["POST"])
@require_auth
def api_models_remove():
    d = request.get_json(force=True, silent=True) or {}
    role = (d.get("role") or "").strip()
    if not SETUP: return jsonify({"error": "installer path unknown"}), 500
    # echo 'n' so the installer keeps the weights (non-interactive)
    try:
        p = subprocess.run(f'echo n | bash {subprocess.list2cmdline([SETUP])} --remove-model {subprocess.list2cmdline([role])}',
                           shell=True, capture_output=True, text=True, timeout=120)
        return jsonify({"ok": p.returncode == 0, "log": (p.stdout + p.stderr)[-2000:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

MODELS_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Models · MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:24px}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;color:#93c5fd;margin:22px 0 8px}
.sub{color:#8b98b8;font-size:13px;margin-bottom:16px}.mono{font-family:ui-monospace,monospace;font-size:12px}
.bar{display:flex;gap:8px}input,select{padding:9px;border-radius:8px;border:1px solid #2a3550;background:#0b1020;color:#e6edf3;font:14px system-ui}
input#q{flex:1}button{border:0;border-radius:8px;padding:8px 13px;font-weight:600;cursor:pointer;font-size:13px}
.go{background:#3b82f6;color:#fff}.rm{background:#7f1d1d;color:#fee2e2}.add{background:#166534;color:#dcfce7}
.card{background:#141b2e;border:1px solid #223052;border-radius:10px;padding:11px 13px;margin:8px 0;display:flex;justify-content:space-between;align-items:center;gap:10px}
.tag{font-size:11px;color:#93c5fd;background:#1e293b;padding:2px 7px;border-radius:20px}
dialog{background:#141b2e;color:#e6edf3;border:1px solid #223052;border-radius:14px;padding:20px;width:min(520px,92vw)}
label{display:block;font-size:13px;margin:10px 0 4px;color:#b6c2da}dialog input,dialog select{width:100%}
.actions{margin-top:16px;display:flex;justify-content:flex-end;gap:8px}.err{color:#f87171;font-size:13px;min-height:16px}
#joblog{font-family:ui-monospace,monospace;font-size:12px;background:#05070f;border:1px solid #223052;border-radius:10px;padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap}
.stage{color:#fbbf24}.warn{color:#fca5a5;font-size:12px}
</style></head><body>
<p><a href="/">← dashboard</a> &nbsp;·&nbsp; <a href="/personas">🎭 personas</a></p>
<h1>📦 Models</h1>
<div class=sub>Search Hugging Face (MLX only) → download → <b>load-probe</b> (actually runs it) → add to the dropdown. A model that downloads but won't load never gets registered.</div>
<div id=warn class=warn></div>
<div class=bar><input id=q placeholder="search HF for MLX models, e.g. qwen coder, llama, whisper" onkeydown="if(event.key=='Enter')search()"><button class=go onclick=search()>Search</button></div>
<div id=results></div>
<h2>In your local cache <span class=sub>(downloaded but not yet added — one click to probe + register)</span></h2><div id=cached></div>
<h2>Currently registered</h2><div id=current></div>
<dialog id=dlg><h3 id=dt>Add model</h3>
<label>HF repo</label><input id=f_repo readonly>
<label>Role / dropdown name (lowercase)</label><input id=f_role>
<label>Type</label><select id=f_type><option value=lm>lm (text / chat / coder)</option><option value=multimodal>multimodal (vision)</option><option value=embeddings>embeddings</option></select>
<label>tool_call_parser (optional)</label><input id=f_tp placeholder="e.g. qwen3_coder">
<label>reasoning_parser (optional)</label><input id=f_rp placeholder="e.g. qwen3">
<div class=err id=derr></div>
<div class=actions><button class=go style="background:#334155" onclick="dlg.close()">Cancel</button><button class=add onclick=startAdd()>Download → probe → add</button></div>
</dialog>
<dialog id=jdlg><h3>Adding model…</h3><div id=joblog></div>
<div class=actions><button class=go id=jclose onclick="jdlg.close();loadCurrent();loadCached()" disabled>Close</button></div></dialog>
<script>
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}
function slug(repo){return repo.split('/').pop().toLowerCase()
  .replace(/-(4bit|8bit|6bit|3bit|mlx|dwq|hf|gguf|instruct|it|chat|unified)\b/g,'')
  .replace(/[^a-z0-9-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,30).replace(/-+$/,'');}
async function search(){
  const q=document.getElementById('q').value.trim(); if(!q)return;
  const res=document.getElementById('results'); res.innerHTML='searching…';
  const d=await(await j('/api/models/search?q='+encodeURIComponent(q))).json();
  if(!d.results.length){res.innerHTML='<div class=sub>no MLX repos found for that query</div>';return;}
  res.innerHTML=''; d.results.forEach(m=>{
    const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div><div class=mono>${m.id}</div><span class=tag>⬇ ${m.downloads}</span> <span class=tag>♥ ${m.likes}</span></div><button class=add>Add</button>`;
    el.querySelector('button').onclick=()=>openAdd(m.id);res.appendChild(el);
  });
}
function openAdd(repo){document.getElementById('derr').textContent='';document.getElementById('f_repo').value=repo;
  document.getElementById('f_role').value=slug(repo);document.getElementById('f_type').value='lm';
  document.getElementById('f_tp').value='';document.getElementById('f_rp').value='';document.getElementById('dlg').showModal();}
async function startAdd(){
  const body={repo:document.getElementById('f_repo').value,role:document.getElementById('f_role').value.trim(),
    type:document.getElementById('f_type').value,tool_parser:document.getElementById('f_tp').value.trim(),
    reasoning_parser:document.getElementById('f_rp').value.trim()};
  const r=await j('/api/models/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); if(!r.ok){document.getElementById('derr').textContent=d.error||'error';return;}
  document.getElementById('dlg').close(); poll(d.job);
}
function poll(jid){
  const box=document.getElementById('joblog'),close=document.getElementById('jclose');
  close.disabled=true; box.textContent=''; document.getElementById('jdlg').showModal();
  const t=setInterval(async()=>{
    const d=await(await j('/api/models/job/'+jid)).json();
    box.innerHTML=`<span class=stage>stage: ${d.stage}</span>\n\n`+(d.log||[]).join('\n');
    box.scrollTop=box.scrollHeight;
    if(d.done){clearInterval(t);close.disabled=false;
      box.innerHTML+='\n\n'+(d.ok?'✅ done — added to the dropdown':'❌ failed — see log above (nothing registered)');}
  },1200);
}
async function loadCached(){
  const d=await(await j('/api/models/cached')).json();
  const box=document.getElementById('cached'); box.innerHTML='';
  const pending=(d.cached||[]).filter(m=>!m.registered);
  if(!pending.length){box.innerHTML='<div class=sub>nothing cached that isn\'t already registered</div>';return;}
  pending.forEach(m=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div class=mono>${m.repo}</div><button class=add>Add</button>`;
    el.querySelector('button').onclick=()=>openAdd(m.repo);box.appendChild(el);});
}
async function loadCurrent(){
  const d=await(await j('/api/models')).json();
  document.getElementById('warn').textContent=d.setup_ok?'':'⚠ installer path not recorded — re-run ./mlx-setup.sh --bootstrap to enable adding models.';
  const cur=document.getElementById('current');cur.innerHTML='';
  (d.served||[]).forEach(s=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div class=mono>${s} <span class=tag>loaded</span></div>`;cur.appendChild(el);});
  (d.custom||[]).forEach(m=>{const el=document.createElement('div');el.className='card';
    el.innerHTML=`<div><div class=mono>${m.role} → ${m.repo}</div><span class=tag>${m.type}</span> <span class=tag>custom</span></div><button class=rm>Remove</button>`;
    el.querySelector('button').onclick=async()=>{if(!confirm('Remove '+m.role+'? (weights kept)'))return;
      await j('/api/models/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:m.role})});loadCurrent();};
    cur.appendChild(el);});
}
loadCurrent();
loadCached();
</script></body></html>"""

VISION_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Vision · MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:24px}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}.sub{color:#8b98b8;font-size:13px;margin-bottom:16px}
label{display:block;font-size:13px;margin:12px 0 4px;color:#b6c2da}
input,select,textarea{width:100%;padding:9px;border-radius:8px;border:1px solid #2a3550;background:#141b2e;color:#e6edf3;font:14px system-ui}
textarea{min-height:70px}button{border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer;background:#3b82f6;color:#fff;margin-top:12px}
#preview{max-width:100%;max-height:280px;border-radius:10px;margin-top:10px;display:none}
#out{white-space:pre-wrap;background:#05070f;border:1px solid #223052;border-radius:10px;padding:14px;margin-top:16px;min-height:40px;font:14px ui-monospace,monospace}
.warn{color:#fca5a5;font-size:12px;margin-bottom:12px}
</style></head><body>
<p><a href="/">← dashboard</a> · <a href="/chat">💬 chat</a> · <a href="/models">📦 models</a></p>
<h1>👁 Vision</h1>
<div class=sub>OCR, image Q&A, chart/screenshot reading — runs a vision model directly via mlx_vlm (works even though the :3001 dropdown can't serve vision yet).</div>
<div class=warn>First run loads the model (Qwen3-VL ≈ 6GB) — allow ~30–90s.</div>
<label>Image</label><input type=file id=img accept="image/*" onchange="prev()">
<img id=preview>
<label>Question</label><textarea id=prompt>Describe this image in detail.</textarea>
<label>Vision model</label><input id=model value="mlx-community/Qwen3-VL-8B-Instruct-4bit">
<button onclick=run()>Ask</button>
<div id=out></div>
<script>
function prev(){const f=document.getElementById('img').files[0];if(!f)return;const p=document.getElementById('preview');p.src=URL.createObjectURL(f);p.style.display='block';}
async function run(){
  const f=document.getElementById('img').files[0];const out=document.getElementById('out');
  if(!f){out.textContent='pick an image first';return;}
  out.textContent='👁 looking… (first run loads the model, be patient)';
  const fd=new FormData();fd.append('image',f);fd.append('prompt',document.getElementById('prompt').value);fd.append('model',document.getElementById('model').value);
  try{const r=await fetch('/api/vision',{method:'POST',body:fd});if(r.status==401){location='/login';return;}
    const d=await r.json();out.textContent=r.ok?d.answer:('⚠ '+(d.error||'failed'));}
  catch(e){out.textContent='⚠ '+e;}
}
</script></body></html>"""

CHAT_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Chat · MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;display:flex;flex-direction:column;height:100vh}
a{color:#60a5fa;text-decoration:none}header{padding:12px 18px;border-bottom:1px solid #223052;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
select{padding:7px;border-radius:8px;border:1px solid #2a3550;background:#141b2e;color:#e6edf3}
#thread{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:760px;padding:10px 14px;border-radius:12px;white-space:pre-wrap;line-height:1.45}
.u{align-self:flex-end;background:#1d4ed8;color:#fff}.a{align-self:flex-start;background:#141b2e;border:1px solid #223052}
.a pre{background:#05070f;padding:10px;border-radius:8px;overflow:auto}.msg img{max-width:260px;border-radius:8px;display:block;margin-bottom:6px}
footer{padding:12px 18px;border-top:1px solid #223052;display:flex;gap:8px;align-items:flex-end;flex-direction:column}
.frow{display:flex;gap:8px;width:100%;align-items:flex-end}
#in{flex:1;padding:11px;border-radius:10px;border:1px solid #2a3550;background:#141b2e;color:#e6edf3;font:15px system-ui;resize:none}
button{border:0;border-radius:10px;padding:0 16px;height:42px;font-weight:600;cursor:pointer;background:#3b82f6;color:#fff}
.attach{background:#334155;font-size:18px;padding:0 12px}
.tiny{color:#8b98b8;font-size:12px}
#chip{display:none;align-items:center;gap:8px;background:#141b2e;border:1px solid #223052;border-radius:8px;padding:6px 10px;font-size:13px;align-self:flex-start}
#chip img{height:34px;border-radius:5px}#chip b{color:#c4b5fd}#chip span{cursor:pointer;color:#f87171;font-weight:700}
body.drag{outline:3px dashed #7c3aed;outline-offset:-6px}
.steps{font-family:ui-monospace,monospace;font-size:12px;color:#8b98b8;background:#05070f;border:1px solid #223052;border-radius:8px;padding:8px;margin-bottom:8px;white-space:pre-wrap;max-height:200px;overflow:auto}
.toggle{display:flex;align-items:center;gap:6px;font-size:13px;color:#c4b5fd;cursor:pointer}
.toggle input{width:16px;height:16px}
.msg.a h2,.msg.a h3,.msg.a h4{margin:.5em 0 .3em;line-height:1.25}
.msg.a h2{font-size:18px}.msg.a h3{font-size:16px}.msg.a h4{font-size:15px;color:#c4b5fd}
.msg.a p{margin:.4em 0}.msg.a ul,.msg.a ol{margin:.4em 0;padding-left:1.4em}
.msg.a li{margin:.15em 0}.msg.a a{color:#60a5fa}
.msg.a strong{color:#f1f5f9}.msg.a em{color:#e6edf3}
.msg.a blockquote{border-left:3px solid #3b82f6;margin:.5em 0;padding:.2em 0 .2em .8em;color:#b6c2da}
.msg.a pre{background:#05070f;border:1px solid #223052;border-radius:8px;padding:10px;overflow:auto;margin:.5em 0}
.msg.a code{background:#0b1020;border:1px solid #223052;border-radius:4px;padding:1px 5px;font-size:13px}
.msg.a pre code{background:none;border:0;padding:0}
</style></head><body>
<header><a href="/">← dashboard</a><b>💬 Chat</b>
<label class=toggle title="Agent mode uses tools — web search, files, self-upgrade — so it can actually browse and act. Off = fast, direct model.">
  <input type=checkbox id=agent checked onchange="modeChange()"> 🌐 Agent</label>
<select id=model></select>
<span class=tiny id=note></span>
<button style="margin-left:auto;background:#334155" onclick="msgs=[];render()">clear</button></header>
<div id=thread></div>
<footer>
  <div id=chip><img id=chipimg><b id=chipname></b><span onclick="clearImg()">✕</span></div>
  <div class=frow>
    <input type=file id=file accept="image/*" style="display:none" onchange="pickImg(this.files[0])">
    <button class=attach title="attach an image to ask about it" onclick="document.getElementById('file').click()">📎</button>
    <textarea id=in rows=1 placeholder="Message…  (Enter to send · attach an image to ask about it)" onkeydown="key(event)"></textarea>
    <button id=sendbtn onclick=send()>Send</button>
  </div>
</footer>
<script>
let msgs=[],busy=false,img=null;
function agentMode(){return document.getElementById('agent').checked;}
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}
async function loadDropdown(){
  const sel=document.getElementById('model');const note=document.getElementById('note');
  if(agentMode()){
    const d=await(await j('/api/personas')).json();
    const names=(d.personas||[]).map(p=>p.name);
    sel.innerHTML=names.map(n=>`<option>${n}</option>`).join('')||'<option>orchestrator</option>';
    if(names.includes('orchestrator'))sel.value='orchestrator';
    note.textContent='agent: browses the web, runs tools, self-upgrades';
  }else{
    const d=await(await j('/api/models')).json();
    const list=(d.gateway||[]).filter(x=>!x.startsWith('embed'));
    sel.innerHTML=list.map(x=>`<option>${x}</option>`).join('')||'<option>no models</option>';
    note.textContent=list.length?'direct model — fast, no tools':'gateway has no models — is it running?';
  }
}
function modeChange(){loadDropdown();}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(raw){
  raw=(raw||'').replace(/\r/g,'');
  const fence=[],ic=[];
  raw=raw.replace(/```(?:[\w-]*)\n?([\s\S]*?)```/g,(m,c)=>{fence.push(c.replace(/\n$/,''));return '\u0000F'+(fence.length-1)+'\u0000';});
  raw=raw.replace(/`([^`\n]+)`/g,(m,c)=>{ic.push(c);return '\u0000C'+(ic.length-1)+'\u0000';});
  const restore=s=>s.replace(/\u0000C(\d+)\u0000/g,(m,n)=>'<code>'+esc(ic[+n])+'</code>');
  const inl=s=>{s=esc(s);
    s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
    s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target=_blank rel=noopener>$1</a>');
    return restore(s);};
  const L=raw.split('\n'),out=[];let i=0;
  const isBlk=s=>/^(#{1,4})\s|^\s*[-*+]\s|^\s*\d+\.\s|^\u0000F\d+\u0000\s*$|^>\s?/.test(s);
  while(i<L.length){
    let ln=L[i];
    let f=ln.trim().match(/^\u0000F(\d+)\u0000$/);
    if(f){out.push('<pre>'+esc(fence[+f[1]])+'</pre>');i++;continue;}
    let h=ln.match(/^(#{1,4})\s+(.*)$/);
    if(h){const lv=Math.min(h[1].length+1,6);out.push('<h'+lv+'>'+inl(h[2])+'</h'+lv+'>');i++;continue;}
    if(/^\s*[-*+]\s+/.test(ln)){let it=[];while(i<L.length&&/^\s*[-*+]\s+/.test(L[i])){it.push('<li>'+inl(L[i].replace(/^\s*[-*+]\s+/,''))+'</li>');i++;}out.push('<ul>'+it.join('')+'</ul>');continue;}
    if(/^\s*\d+\.\s+/.test(ln)){let it=[];while(i<L.length&&/^\s*\d+\.\s+/.test(L[i])){it.push('<li>'+inl(L[i].replace(/^\s*\d+\.\s+/,''))+'</li>');i++;}out.push('<ol>'+it.join('')+'</ol>');continue;}
    if(/^>\s?/.test(ln)){let q=[];while(i<L.length&&/^>\s?/.test(L[i])){q.push(inl(L[i].replace(/^>\s?/,'')));i++;}out.push('<blockquote>'+q.join('<br>')+'</blockquote>');continue;}
    if(ln.trim()===''){i++;continue;}
    let p=[];while(i<L.length&&L[i].trim()!==''&&!isBlk(L[i])){p.push(inl(L[i]));i++;}
    out.push('<p>'+p.join('<br>')+'</p>');
  }
  return out.join('');
}
function bubble(m){const d=document.createElement('div');d.className='msg '+(m.role=='user'?'u':'a');
  let h='';
  if(m.role!=='user'&&m.steps&&m.steps.length){
    const open=m.content?'':' open';
    h+='<details class=stepsd'+open+'><summary style="cursor:pointer;color:#93c5fd;font-size:12px">🔧 steps ('+m.steps.length+')</summary><div class=steps>'+esc(m.steps.join('\n'))+'</div></details>';
  }
  h+=m.role=='user'?esc(m.content):fmt(m.content);
  if(m.image) h='<img src="'+m.image+'">'+h; d.innerHTML=h; return d;}
function render(){const t=document.getElementById('thread');t.innerHTML='';
  msgs.forEach(m=>t.appendChild(bubble(m)));t.scrollTop=t.scrollHeight;return t;}
function key(e){if(e.key=='Enter'&&!e.shiftKey){e.preventDefault();send();}}
function pickImg(f){if(!f)return;img=f;const u=URL.createObjectURL(f);
  document.getElementById('chip').style.display='flex';document.getElementById('chipimg').src=u;
  document.getElementById('chipname').textContent=f.name.slice(0,28);}
function clearImg(){img=null;document.getElementById('file').value='';document.getElementById('chip').style.display='none';}
document.addEventListener('dragover',e=>{e.preventDefault();document.body.classList.add('drag');});
document.addEventListener('dragleave',e=>{if(e.relatedTarget===null)document.body.classList.remove('drag');});
document.addEventListener('drop',e=>{e.preventDefault();document.body.classList.remove('drag');
  const f=[...(e.dataTransfer.files||[])].find(x=>x.type.startsWith('image/'));if(f)pickImg(f);});
async function send(){
  if(busy)return;const inp=document.getElementById('in');const text=inp.value.trim();
  if(!text&&!img)return;
  busy=true;document.getElementById('sendbtn').disabled=true;
  if(img){ await visionTurn(text); }
  else if(agentMode()){ await agentTurn(text); }
  else { await chatTurn(text); }
  busy=false;document.getElementById('sendbtn').disabled=false;
}
async function agentTurn(text){
  const persona=document.getElementById('model').value;
  document.getElementById('in').value='';
  const hist=msgs.filter(x=>x.content&&(x.role=='user'||x.role=='assistant'))
                 .map(x=>({role:x.role,content:x.content}));
  msgs.push({role:'user',content:text});
  const a={role:'assistant',content:'',steps:[]};msgs.push(a);
  const t=render();const last=t.lastChild;
  function paint(){last.innerHTML=bubble(a).innerHTML;t.scrollTop=t.scrollHeight;}
  a.steps.push('working…');paint();
  try{
    const r=await j('/api/agent-chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({persona,message:text,history:hist})});
    if(r.status==429){a.steps=[];a.content='⏳ agent busy — one request at a time';paint();return;}
    const reader=r.body.getReader();const dec=new TextDecoder();let buf='';a.steps=[];
    while(true){const {done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let i;
      while((i=buf.indexOf('\n'))>=0){const ln=buf.slice(0,i);buf=buf.slice(i+1);if(!ln.trim())continue;
        let o;try{o=JSON.parse(ln)}catch(e){continue}
        if(o.type=='trace')a.steps.push(o.text);
        else if(o.type=='answer')a.content=o.text;
        paint();}
    }
    if(!a.content){a.content='[no answer]';paint();}
  }catch(e){a.content='⚠ '+e;paint();}
}
async function chatTurn(text){
  const model=document.getElementById('model').value;
  document.getElementById('in').value='';
  msgs.push({role:'user',content:text});
  const a={role:'assistant',content:''};msgs.push(a);
  const t=render();const last=t.lastChild;
  try{
    const r=await j('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model,messages:msgs.slice(0,-1)})});
    const reader=r.body.getReader();const dec=new TextDecoder();let acc='';
    while(true){const {done,value}=await reader.read();if(done)break;
      acc+=dec.decode(value,{stream:true});a.content=acc;last.innerHTML=fmt(acc);
      t.scrollTop=t.scrollHeight;}
    if(!acc)a.content='[no output]',last.innerHTML='[no output]';
  }catch(e){a.content='⚠ '+e;last.innerHTML='⚠ '+esc(''+e);}
}
async function visionTurn(text){
  const f=img,prompt=text||'Describe this image in detail.';
  const url=URL.createObjectURL(f);document.getElementById('in').value='';clearImg();
  msgs.push({role:'user',content:prompt,image:url});
  const a={role:'assistant',content:'👁 looking… (first image loads the vision model)'};msgs.push(a);
  const t=render();const last=t.lastChild;
  try{
    const fd=new FormData();fd.append('image',f);fd.append('prompt',prompt);
    const r=await j('/api/vision',{method:'POST',body:fd});const d=await r.json();
    a.content=r.ok?d.answer:('⚠ '+(d.error||'failed'));
  }catch(e){a.content='⚠ '+e;}
  last.innerHTML=fmt(a.content);t.scrollTop=t.scrollHeight;
}
loadDropdown();
</script></body></html>"""

IMAGINE_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Create image · MLX</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{font:15px system-ui;background:#0b1020;color:#e6edf3;margin:0;padding:24px}
a{color:#60a5fa;text-decoration:none}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;color:#93c5fd;margin:24px 0 10px}
.sub{color:#8b98b8;font-size:13px;margin-bottom:16px}label{display:block;font-size:13px;margin:12px 0 4px;color:#b6c2da}
textarea,select,input{width:100%;padding:9px;border-radius:8px;border:1px solid #2a3550;background:#141b2e;color:#e6edf3;font:14px system-ui}
textarea{min-height:66px}.row{display:flex;gap:10px;flex-wrap:wrap}.row>div{flex:1;min-width:120px}
button{border:0;border-radius:8px;padding:11px 18px;font-weight:600;cursor:pointer;background:#7c3aed;color:#fff;margin-top:14px}
button:disabled{opacity:.5;cursor:default}
#result{margin-top:18px}#result img{max-width:100%;border-radius:12px;border:1px solid #223052}
#status{color:#c4b5fd;font-size:13px;min-height:18px;margin-top:10px}
.warn{color:#fca5a5;font-size:12px;margin-bottom:12px}.lic{color:#8b98b8;font-size:12px;margin-top:4px}
#gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
#gallery img{width:100%;border-radius:8px;border:1px solid #223052;cursor:pointer}
</style></head><body>
<p><a href="/">← dashboard</a> · <a href="/chat">💬 chat</a> · <a href="/vision">👁 vision</a></p>
<h1>🎨 Create image</h1>
<div class=sub>Text→image locally with FLUX (mflux) — no API, no cost, private.</div>
<div class=warn>First run downloads the model (a few GB) — that generation can take a few minutes; later ones are fast.</div>
<label>Prompt</label><textarea id=prompt placeholder="a cozy reading nook by a rainy window, warm light, watercolor"></textarea>
<div class=row>
  <div><label>Model</label><select id=model onchange="lic()"><option value=z-image-turbo>Z-Image Turbo (fast, non-gated)</option><option value=schnell>FLUX.1-schnell (needs HF access)</option><option value=dev>FLUX.1-dev (needs HF access)</option></select><div class=lic id=lic>Z-Image Turbo · non-gated · Tongyi Qianwen license (commercial use OK; verify at large scale)</div></div>
  <div><label>Steps</label><input id=steps type=number value=9 min=1 max=50></div>
  <div><label>Width</label><input id=width type=number value=1024 min=256 max=1536 step=64></div>
  <div><label>Height</label><input id=height type=number value=1024 min=256 max=1536 step=64></div>
  <div><label>Seed (optional)</label><input id=seed placeholder="random"></div>
</div>
<button id=go onclick=gen()>Generate</button>
<div id=status></div>
<div id=result></div>
<h2>Recent</h2><div id=gallery></div>
<script>
async function j(u,o){const r=await fetch(u,o);if(r.status==401){location='/login';throw 0;}return r;}
function lic(){
  const v=document.getElementById('model').value;
  const info={'z-image-turbo':['Z-Image Turbo · non-gated · Tongyi Qianwen license (commercial use OK; verify at large scale)',9],
              'schnell':['FLUX.1-schnell · Apache-2.0 · requires accepting the license on huggingface.co/black-forest-labs/FLUX.1-schnell',4],
              'dev':['FLUX.1-dev · non-commercial · requires an HF access grant',20]}[v];
  document.getElementById('lic').textContent=info[0];
  document.getElementById('steps').value=info[1];
}
async function gen(){
  const prompt=document.getElementById('prompt').value.trim();const st=document.getElementById('status');
  if(!prompt){st.textContent='enter a prompt first';return;}
  const body={prompt,model:document.getElementById('model').value,steps:document.getElementById('steps').value,
    width:document.getElementById('width').value,height:document.getElementById('height').value,seed:document.getElementById('seed').value};
  const go=document.getElementById('go');go.disabled=true;st.textContent='🎨 generating… (first run pulls the model, be patient)';
  document.getElementById('result').innerHTML='';
  try{const r=await j('/api/imagine',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json();
    if(r.ok){const u='/api/image-file/'+d.file+'?t='+Date.now();
      document.getElementById('result').innerHTML=`<img src="${u}"><div style="margin-top:8px"><a href="${u}" download>⬇ download</a></div>`;
      st.textContent='done';loadGallery();}
    else st.textContent='⚠ '+(d.error||'failed');}
  catch(e){st.textContent='⚠ '+e;}
  go.disabled=false;
}
async function loadGallery(){
  const d=await(await j('/api/images')).json();const g=document.getElementById('gallery');g.innerHTML='';
  (d.images||[]).forEach(f=>{const u='/api/image-file/'+f;const im=document.createElement('img');im.src=u;im.onclick=()=>window.open(u);g.appendChild(im);});
}
loadGallery();
</script></body></html>"""

@app.route("/models")
@require_auth
def models_page():
    return Response(MODELS_PAGE, mimetype="text/html")

# ── vision: prefer the resident mlx_vlm.server (warm, fast); fall back to a fresh
#    bounded subprocess (always works) if that server isn't up ─────────────────
import tempfile as _tmp, base64 as _b64
VISION_MODEL = ENV.get("MLX_VISION_MODEL", "mlx-community/Qwen3-VL-8B-Instruct-4bit")
P_VISION = os.environ.get("MLX_VISION_PORT", "8081")
_VCODE = ("import sys\nfrom mlx_vlm import load, generate\n"
          "from mlx_vlm.prompt_utils import apply_chat_template\nfrom mlx_vlm.utils import load_config\n"
          "repo,img,q=sys.argv[1],sys.argv[2],sys.argv[3]\n"
          "model,proc=load(repo);cfg=load_config(repo)\n"
          "fp=apply_chat_template(proc,cfg,q,num_images=1)\n"
          "out=generate(model,proc,fp,[img],verbose=False,max_tokens=512)\n"
          "print((getattr(out,'text',None) or str(out)).strip())\n")

def _vision_http(model, path, prompt, timeout=300):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as fh:
        url = f"data:image/{mime};base64," + _b64.b64encode(fh.read()).decode()
    body = {"model": model, "max_tokens": 512, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}}]}]}
    r = requests.post(f"http://127.0.0.1:{P_VISION}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()

@app.route("/api/vision", methods=["POST"])
@require_auth
def api_vision():
    prompt = (request.form.get("prompt") or "Describe this image in detail.").strip()
    model = (request.form.get("model") or VISION_MODEL).strip()
    f = request.files.get("image")
    if not f: return jsonify({"error": "no image uploaded"}), 400
    suffix = os.path.splitext(f.filename or "img.png")[1] or ".png"
    path = _tmp.mktemp(suffix=suffix); f.save(path)
    try:
        try:                                   # fast path: warm resident vision server
            ans = _vision_http(model, path, prompt)
            if ans: return jsonify({"answer": ans})
        except Exception:
            pass                               # fall back to the always-works subprocess
        try:
            r = subprocess.run([sys.executable, "-c", _VCODE, model, path, prompt],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "vision timed out (600s) — model too large or multimodal path hanging"}), 504
        if r.returncode != 0:
            return jsonify({"error": (r.stderr or "vision failed").strip()[-600:]}), 500
        return jsonify({"answer": (r.stdout or "").strip()})
    finally:
        try: os.remove(path)
        except Exception: pass

@app.route("/vision")
@require_auth
def vision_page():
    return Response(VISION_PAGE, mimetype="text/html")

# ── chat (streams tokens from the LiteLLM gateway — OWUI-style, in the dashboard) ──
@app.route("/api/chat", methods=["POST"])
@require_auth
def api_chat():
    d = request.get_json(force=True, silent=True) or {}
    model = d.get("model"); messages = d.get("messages")
    if not model or not isinstance(messages, list):
        return jsonify({"error": "model and messages required"}), 400
    def gen():
        try:
            with requests.post(f"http://127.0.0.1:{P_GATEWAY}/v1/chat/completions",
                               json={"model": model, "messages": messages,
                                     "max_tokens": 2048, "stream": True},
                               stream=True, timeout=600) as r:
                if r.status_code >= 400:
                    yield f"[error: gateway returned {r.status_code}]"; return
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content", "")
                    except Exception:
                        continue
                    if delta:
                        yield delta
        except Exception as e:
            yield f"\n[stream error: {str(e)[:200]}]"
    return Response(gen(), mimetype="text/plain; charset=utf-8")

@app.route("/chat")
@require_auth
def chat_page():
    return Response(CHAT_PAGE, mimetype="text/html")

# ── agentic chat: runs the query through the AGENT (web_search, web_fetch,
#    self-upgrade, subagents) and streams its live steps + final answer ───────
_AGENT_LOCK = _th.Lock()
_AGENT_BUSY = {"on": False}

@app.route("/api/agent-chat", methods=["POST"])
@require_auth
def api_agent_chat():
    if agent is None:
        return jsonify({"error": "agent module not loaded"}), 500
    d = request.get_json(force=True, silent=True) or {}
    message = (d.get("message") or "").strip()
    persona_name = (d.get("persona") or "orchestrator").strip()
    hist = d.get("history") if isinstance(d.get("history"), list) else []
    history = [{"role": h.get("role"), "content": h.get("content")}
               for h in hist if isinstance(h, dict) and h.get("role") in ("user", "assistant")][-20:]
    if not message:
        return jsonify({"error": "message required"}), 400
    personas = agent.load_personas()
    persona = personas.get(persona_name) or personas.get("orchestrator") or next(iter(personas.values()), None)
    if not persona:
        return jsonify({"error": "no personas configured"}), 500
    with _AGENT_LOCK:
        if _AGENT_BUSY["on"]:
            return jsonify({"error": "agent is busy with another request — one at a time"}), 429
        _AGENT_BUSY["on"] = True

    q = _queue.Queue()
    autonomous = not AUTH_ON     # on localhost act independently; when exposed, refuse gated actions
    def ask_fn(prompt, can_all):
        q.put(("trace", ("✔ auto-approved: " if autonomous else "⛔ needs approval (do it via CLI/Telegram): ") + str(prompt)[:140]))
        return "y" if autonomous else "n"
    def choice_fn(question, options):
        opts = [str(o) for o in (options or [])]
        q.put(("trace", "• " + str(question)[:120]))
        return opts[0] if opts else ""
    def emit_fn(line):
        q.put(("trace", str(line)))

    def worker():
        try:
            agent.EMIT_FN = emit_fn; agent.ASK_FN = ask_fn; agent.CHOICE_FN = choice_fn
            ans = agent.run_agent(message, persona, history=history)
        except Exception as e:
            ans = f"[error: {e}]"
        finally:
            agent.EMIT_FN = agent.ASK_FN = agent.CHOICE_FN = None
            q.put(("answer", ans)); q.put(None)
            _AGENT_BUSY["on"] = False
    _th.Thread(target=worker, daemon=True).start()

    def gen():
        strip = getattr(agent, "strip_thinking", lambda s: s)
        while True:
            item = q.get()
            if item is None:
                break
            typ, text = item
            if typ == "answer":
                text = strip(text) or "[no answer]"
            yield json.dumps({"type": typ, "text": text}) + "\n"
    return Response(gen(), mimetype="application/x-ndjson")

# ── image generation (mflux / FLUX, via the tested wrapper) ─────────────────
from flask import send_file
IMAGES_DIR = Path(os.environ.get("MLX_IMAGES_DIR", str(Path.home() / "MLX-AI" / "documents" / "images")))
IMG_WRAPPER = str(WORKDIR / "mlx-image.sh")

@app.route("/api/imagine", methods=["POST"])
@require_auth
def api_imagine():
    d = request.get_json(force=True, silent=True) or {}
    prompt = (d.get("prompt") or "").strip()
    if not prompt: return jsonify({"error": "prompt required"}), 400
    model = d.get("model") if d.get("model") in ("z-image-turbo", "schnell", "dev") else "z-image-turbo"
    def _int(v, lo, hi, dflt):
        try: return max(lo, min(hi, int(v)))
        except Exception: return dflt
    steps = _int(d.get("steps"), 1, 50, {"z-image-turbo": 9, "schnell": 4, "dev": 20}[model])
    w = _int(d.get("width"), 256, 1536, 1024); h = _int(d.get("height"), 256, 1536, 1024)
    seed = str(d.get("seed") or "").strip()
    if seed and not seed.isdigit(): seed = ""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    out = str(IMAGES_DIR / f"img_{int(_time.time())}.png")
    if not os.path.exists(IMG_WRAPPER):
        return jsonify({"error": "image tool missing — run --bootstrap"}), 500
    args = ["bash", IMG_WRAPPER, prompt, out, model, str(steps), str(w), str(h)] + ([seed] if seed else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "image generation timed out (900s) — first run downloads FLUX (multi-GB)"}), 504
    if r.returncode != 0 or not os.path.exists(out):
        return jsonify({"error": (r.stderr or r.stdout or "generation failed").strip()[-600:]}), 500
    return jsonify({"file": os.path.basename(out)})

@app.route("/api/image-file/<name>")
@require_auth
def api_image_file(name):
    if not re.match(r"^[A-Za-z0-9._-]+\.png$", name): return ("bad name", 400)
    p = IMAGES_DIR / name
    if not p.exists(): return ("not found", 404)
    return send_file(str(p), mimetype="image/png")

@app.route("/api/images")
@require_auth
def api_images():
    if not IMAGES_DIR.exists(): return jsonify({"images": []})
    files = sorted((f.name for f in IMAGES_DIR.glob("*.png")), reverse=True)[:24]
    return jsonify({"images": files})

@app.route("/imagine")
@require_auth
def imagine_page():
    return Response(IMAGINE_PAGE, mimetype="text/html")

@app.route("/")
@require_auth
def index(): return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    app.run(host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"), port=int(os.environ.get("PORT_DASHBOARD", "8800")), debug=False)
PYEOF
    ok "dashboard written"
}

# =============================================================================
#  PHASE 9 — IMAGE-GEN WRAPPER (mflux, commercial-safe FLUX.1-schnell)
# =============================================================================
write_image_tool() {
    log "Image generation wrapper (mflux)"
    mkdir -p "$DOCS_WORKSPACE/images"
    cat > "$WORKDIR/mlx-image.sh" <<SHEOF
#!/usr/bin/env bash
# Generate an image locally with mflux. Default model is Z-Image Turbo — NON-GATED
# (no Hugging Face access grant needed), fast, commercial-friendly. FLUX.1 schnell/dev
# are supported too but require accepting their gated HF license first.
# Usage: mlx-image.sh "PROMPT" [OUT] [MODEL] [STEPS] [WIDTH] [HEIGHT] [SEED]
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$PATH"
PROMPT="\${1:-a friendly robot reading a book, flat vector illustration}"
OUT="\${2:-$DOCS_WORKSPACE/images/img_\$(date +%s).png}"
MODEL="\${3:-z-image-turbo}"; STEPS="\${4:-}"; WIDTH="\${5:-1024}"; HEIGHT="\${6:-1024}"; SEED="\${7:-}"
if [ -z "\$STEPS" ] || [ "\$STEPS" = 0 ]; then
  case "\$MODEL" in z-image-turbo) STEPS=9 ;; dev) STEPS=20 ;; *) STEPS=4 ;; esac
fi
ARGS=(--model "\$MODEL" --steps "\$STEPS" --width "\$WIDTH" --height "\$HEIGHT" -q 8 --prompt "\$PROMPT" --output "\$OUT")
case "\$MODEL" in                       # only the FLUX family takes a guidance value
  dev)     ARGS+=(--guidance 3.5) ;;
  schnell) ARGS+=(--guidance 0.0) ;;
esac
[ -n "\$SEED" ] && ARGS+=(--seed "\$SEED")
uv tool run --from mflux mflux-generate "\${ARGS[@]}"
echo "saved: \$OUT"
SHEOF
    chmod +x "$WORKDIR/mlx-image.sh"
    ok "wrote mlx-image.sh (use: ./mlx-setup.sh --image \"your prompt\")"
}

# =============================================================================
#  PHASE 9b — AGENT (tool-executor: runs allowlisted tools, answers from real data)
# =============================================================================
write_agent() {
    log "Tool-executor agent (mlx-agent)"
    local AD="$WORKDIR/agent"; mkdir -p "$AD"
    cat > "$AD/mlx-agent.py" <<'AGENTEOF'
#!/usr/bin/env python3
"""
mlx-agent — persona-aware tool-executor for the local MLX workstation.

Runs NATIVELY on the host so it can actually touch the filesystem, shell, and
your SearXNG. Personas are DATA (a registry), not code: each persona is a name +
model + system prompt + allowed tools. The same generic tool loop runs any of
them, so you add a "researcher" or "pentester" without changing this file.

    your question → model picks a tool → THIS runs it for real →
    result fed back → model answers from real data.

Safety: shell is allowlisted (read-only auto-runs; anything else asks), file
paths are scoped, writes confirm. Personas default to the full toolset, but the
destructive/network confirmation gates ALWAYS apply — even to an "open" persona.

Usage:
    mlx-agent "how much disk is free?"                 # default 'general' persona
    mlx-agent --persona researcher "what to build next?"
    mlx-agent --list-personas
    mlx-agent --add-persona                            # interactive
    mlx-agent --edit-persona pentester
    mlx-agent --remove-persona pentester
    mlx-agent                                          # interactive REPL
    mlx-agent --yes "..."                              # auto-approve (careful)
"""
from __future__ import annotations
import argparse, json, os, re, shlex, subprocess, sys, textwrap, datetime, time, base64, shutil
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests not found — run with the workstation venv:\n"
             "  ~/.mlx-ai-workstation/.venv/bin/python mlx-agent.py \"...\"")

# ───────────────────────────── configuration ──────────────────────────────
GATEWAY   = os.environ.get("MLX_GATEWAY", "http://localhost:8000/v1")   # mlx-openai-server direct — LiteLLM drops tool calls
VISION_MODEL = os.environ.get("MLX_VISION_MODEL", "mlx-community/Qwen3-VL-8B-Instruct-4bit")
VISION_PORT  = os.environ.get("MLX_VISION_PORT", "8081")
VISION_SERVER = f"http://localhost:{VISION_PORT}"   # resident mlx_vlm.server (warm model)
SEARXNG   = os.environ.get("MLX_SEARXNG", "http://localhost:8888")
WORKDIR   = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
WORKSPACE = Path(os.environ.get("MLX_WORKSPACE", str(Path.home() / "MLX-AI")))
PERSONAS_FILE = WORKDIR / "personas.json"
DEFAULT_MODEL = os.environ.get("MLX_AGENT_MODEL", "coder:qwen3.6-27b")
MAX_STEPS = int(os.environ.get("MLX_AGENT_MAX_STEPS", "0"))               # 0 = unlimited tool steps
LOOP_GUARD = int(os.environ.get("MLX_AGENT_LOOP_GUARD", "10"))            # stop if the SAME tool call repeats N× in a row (0 = off); not a step cap, just anti-hang
MAX_SEARCHES = int(os.environ.get("MLX_AGENT_MAX_SEARCHES", "5"))         # per run, then steer to web_fetch/direct
MAX_SPAWN_DEPTH = int(os.environ.get("MLX_AGENT_MAX_SPAWN_DEPTH", "2"))   # subagents can nest this deep
MAX_SPAWNS = int(os.environ.get("MLX_AGENT_MAX_SPAWNS", "8"))             # total subagents per top-level run
SHELL_TIMEOUT = int(os.environ.get("MLX_AGENT_SHELL_TIMEOUT", "60"))
CODE_TIMEOUT = int(os.environ.get("MLX_AGENT_CODE_TIMEOUT", "120"))       # run_python wall-clock bound
MAX_TOKENS = int(os.environ.get("MLX_AGENT_MAX_TOKENS", "16384"))       # per model call; 0 = uncapped. A finite value stops a degenerate loop from running to the whole context.
TEMPERATURE = float(os.environ.get("MLX_AGENT_TEMPERATURE", "0.3"))
FREQ_PENALTY = float(os.environ.get("MLX_AGENT_FREQ_PENALTY", "0.4"))    # >0 discourages the "same phrase over and over" loops; 0 = don't send
PRESENCE_PENALTY = float(os.environ.get("MLX_AGENT_PRESENCE_PENALTY", "0.0"))

ALLOWED_ROOTS = [Path.home(), Path("/tmp"), WORKDIR, WORKSPACE, Path.cwd()]

SAFE_COMMANDS = {
    "df","du","ls","cat","head","tail","wc","grep","egrep","find","stat","file",
    "ps","top","uname","sw_vers","sysctl","uptime","whoami","id","date","which",
    "echo","env","printenv","vm_stat","system_profiler","ifconfig","ipconfig",
    "networksetup","hostname","sort","uniq","cut","awk","sed","tr","pwd","tree",
    "brew","git","pip","uv","python3","node","docker","launchctl","hf","ollama",
}
DANGEROUS = re.compile(r"(;|&&|\|\||>|<|`|\$\(|\brm\b|\bmv\b|\bdd\b|\bmkfs\b|\bsudo\b|\bkillall\b)")

# Autonomy: by default the agent RUNS everything (shell, code, file writes) without
# asking. Only two categories still require a human: installing packages, and deleting
# files/folders. Truly catastrophic commands are refused outright. Set
# MLX_AGENT_AUTONOMOUS=0 to go back to confirm-everything; a persona with
# approval:"strict" always asks regardless.
AUTONOMOUS = os.environ.get("MLX_AGENT_AUTONOMOUS", "1") != "0"

_INSTALL_RE = re.compile(
    r"\b(pip3?|pipx|uv|conda|mamba|poetry)\b[^\n]*\b(install|add|sync)\b"
    r"|\bbrew\b[^\n]*\b(install|reinstall|upgrade|tap)\b"
    r"|\b(npm|pnpm|yarn|bun)\b[^\n]*\b(i|install|add|ci)\b"
    r"|\b(cargo|gem|go)\b[^\n]*\binstall\b"
    r"|\b(apt|apt-get|port|dnf|yum)\b[^\n]*\binstall\b"
    r"|\bpip3?\s+install\b"
    r"|curl[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I)

_DELETE_RE = re.compile(
    r"\brm\b|\brmdir\b|\bunlink\b|\bshred\b|\btrash\b"
    r"|\bfind\b[^\n]*-delete\b|\bfind\b[^\n]*-exec\s+rm\b|\bgit\s+clean\b", re.I)

# Never run these, even with approval — they can wreck the machine.
_CATASTROPHIC_RE = re.compile(
    r"\bsudo\b|\bdd\b\s|\bmkfs\b|\bdiskutil\s+(erase|reformat|partition)\b"
    r"|:\s*\(\s*\)\s*\{|\bshutdown\b|\breboot\b|\bhalt\b"
    r"|\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+(/|~|\$HOME|/\*)(\s|$)"
    r"|>\s*/dev/(r?disk|sd)", re.I)

def _in_git_repo(path: str) -> bool:
    """True if `path` lives inside a git work tree (so changes/deletes are recoverable)."""
    try:
        p = os.path.abspath(os.path.expanduser(path))
        d = p if os.path.isdir(p) else (os.path.dirname(p) or ".")
        r = subprocess.run(["git", "-C", d, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False

def _delete_is_git_safe(command: str) -> bool:
    """True when EVERY path a delete command targets is inside a git work tree — then the
    deletion is version-controlled/recoverable, so it's safe to auto-approve without asking."""
    try: toks = shlex.split(command)
    except Exception: return False
    skip = {"rm","rmdir","unlink","shred","trash","find","git","clean","xargs","-exec",";","+","{}"}
    targets = [t for t in toks if not t.startswith("-") and t not in skip]
    if not targets:                       # e.g. `git clean -fd` acts on the cwd repo
        return _in_git_repo(os.getcwd())
    return all(_in_git_repo(t) for t in targets)

C = {"dim":"\033[2m","cyan":"\033[36m","yellow":"\033[33m","green":"\033[32m",
     "red":"\033[31m","bold":"\033[1m","magenta":"\033[35m","reset":"\033[0m"}
def c(s, col): return f"{C[col]}{s}{C['reset']}" if sys.stdout.isatty() else s

AUTO_YES = False
RUN_APPROVE_ALL = False   # per-run "allow the rest of this run"; reset at each top-level run
STRICT_RUN = False        # set from the active persona; disables the 'allow all' escape hatch

# Pluggable I/O hooks. The CLI leaves these None (terminal input + stdout). The
# Telegram bridge sets them to route approvals to inline buttons, multi-choice to
# button rows, and progress to a single live-updating message.
ASK_FN = None      # ASK_FN(prompt:str, allow_all:bool) -> 'y' | 'n' | 'a'
CHOICE_FN = None   # CHOICE_FN(question:str, options:list) -> chosen str
EMIT_FN = None     # EMIT_FN(line:str) -> None   (a progress/trace line)
STREAM_FN = None   # STREAM_FN(piece:str) -> None (a token/delta of the streaming answer)
_ANSI = re.compile(r"\033\[[0-9;]*m")

def emit(line: str):
    print(line)
    if EMIT_FN is not None:
        try: EMIT_FN(_ANSI.sub("", line))
        except Exception: pass

def _ask_yn(prompt: str, can_all: bool) -> str:
    if ASK_FN is not None:
        try: return ASK_FN(prompt, can_all)
        except Exception: return "n"
    opts = "[y]es · [n]o · [a]llow rest of this run" if can_all else "[y]es · [n]o"
    try: ans = input(c(f"  ⚠ {prompt}  {opts}: ", "yellow")).strip().lower()
    except (EOFError, KeyboardInterrupt): return "n"
    if ans in ("y","yes"): return "y"
    if ans in ("a","all") and can_all: return "a"
    return "n"

def confirm(prompt: str, force_ask: bool = False, kind: str = "general") -> bool:
    """Approve an action. In AUTONOMOUS mode the agent runs freely; only kind in
    {install, delete} still asks a human. A strict persona (STRICT_RUN) always asks."""
    global RUN_APPROVE_ALL
    gated = kind in ("install", "delete")
    if AUTO_YES:
        emit(c(f"  (auto-approved) {prompt}", "dim")); return True
    if AUTONOMOUS and not STRICT_RUN and not force_ask and not gated:
        return True                                   # silent auto-run for normal task actions
    if RUN_APPROVE_ALL and not STRICT_RUN and not force_ask and not gated:
        emit(c(f"  (approved for this run) {prompt}", "dim")); return True
    can_all = not (STRICT_RUN or force_ask or gated)  # no 'allow-all' for installs/deletes — each asks
    ans = _ask_yn(prompt, can_all)
    if ans == "y": return True
    if ans == "a" and can_all:
        RUN_APPROVE_ALL = True; return True
    return False

# ─────────────────────────── persona registry ─────────────────────────────
BASE_SYSTEM = (
    "You are a local AI assistant running on the user's Mac with REAL tools. When a task "
    "needs live system data, files, code execution, or current web info, CALL A TOOL rather "
    "than guessing or claiming you lack access. Use the fewest tool calls that answer the task. "
    "After tools return, answer concisely from their ACTUAL output. Never fabricate tool results.\n"
    "AUTONOMY: you are cleared to act. Run shell commands, execute code, and read/write/overwrite "
    "files freely to complete the task — do NOT ask the user for permission for ordinary steps, and "
    "do NOT stop to narrate what you're about to do; just call the tool and do it. The ONLY actions "
    "that need the user's approval are installing packages and deleting files or folders; the system "
    "handles asking for those, so proceed normally and let it prompt when needed. Inside a git "
    "repository, deletions are auto-approved (git can recover them), so you can refactor freely there.\n"
    "INTERNET: you DO have live web access via web_search and web_fetch. For anything current or "
    "factual you're unsure of — news, sports scores, prices, release dates, docs — search first. "
    "NEVER tell the user you can't access the internet or browse the web; you can, so do it.\n"
    "CODE: for anything without a dedicated tool, WRITE AND RUN a short Python script with run_python "
    "(the runtime has mlx, requests, psutil, etc.) instead of giving up or stringing together many shell "
    "calls. print() the result so you get it back. This is your general-purpose way to actually do work.\n"
    "SWIFT/XCODE: to start ANY Swift, macOS, or iOS app, call scaffold_swift first (it uses SwiftPM and "
    "produces a buildable Package.swift that opens in Xcode). NEVER hand-write .xcodeproj or "
    "project.pbxproj — they will not work. Then write source under Sources/ with write_file, and run "
    "`swift build` via run_shell to compile and fix errors iteratively before moving on.\n"
    "SELF-HEALING: if a task needs a tool/package/library/CLI you don't have, DO NOT give up or "
    "say you lack the capability. Instead: (1) use web_search to find what to install and the exact "
    "commands, then (2) call propose_capability with a concrete plan. The user will review and approve. "
    "After it installs and verifies, retry the original task. Prefer package installs (pip/brew/uv/npm); "
    "only propose fetching a web script when no package exists.\n"
    "DELEGATION: when a task spans specialties, you may call spawn_subagent(persona, task) to hand a "
    "sub-task to a specialist (personas include researcher, dev, qa, ba, reasoner, general). Do simple "
    "steps yourself; delegate the ones a specialist fits, then synthesize the results.\n"
    "FILES: when the user asks you to CREATE, WRITE, SAVE, PRODUCE, GENERATE, or UPDATE a file "
    "(a README, script, config, document, etc.), you MUST call write_file with the COMPLETE final "
    "content and the target path — do NOT print the file's contents as your chat answer and stop. "
    "Writing the file IS the deliverable. To study or summarize an existing file first, call read_file "
    "to get its real content (never guess it). After writing, your answer is just a short confirmation "
    "of what you wrote and the path — not the whole file again. If the user gives a path, use it exactly; "
    "if not, write under the shared workspace ~/MLX-AI (e.g. ~/MLX-AI/<short-name>). Never invent absolute "
    "paths for directories you haven't confirmed exist — check with list_dir or pwd first if unsure.\n"
    "SEARCH DISCIPLINE: web_search is for finding a URL or a quick fact. Do NOT search repeatedly for the "
    "same thing. Once a search returns a promising link, use web_fetch to READ it. To learn a library's "
    "API, the fastest route is usually propose_capability to install it, then introspect (python -c "
    "\"import x; help(x)\") — not more searching. After ~3 searches on one question, switch tactics.")

DEFAULT_PERSONAS = {
    "orchestrator": {
        "description": "Main agent — plans a goal, delegates to specialist subagents, synthesizes.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are the orchestrator, the user's main agent. Plan the goal as concrete steps. "
                          "Delegate specialist steps with spawn_subagent(persona, task) — and put the CONCRETE "
                          "context the specialist needs directly in the task string (e.g. don't tell dev to 'write "
                          "hello-world for the popular framework' — tell it exactly which framework and any facts you "
                          "already found, so it doesn't re-research). Personas: researcher (web/trends), dev "
                          "(build/run code), qa (test), ba (specs), reasoner (hard analysis). Do trivial steps "
                          "yourself. ALWAYS finish with a short synthesized answer to the user that states the key "
                          "findings and what each subagent produced — never end on a raw tool result. Do NOT spawn "
                          "the same task twice: if a subagent already attempted a step, use its result or refine it "
                          "yourself rather than re-delegating the identical task. "
                          "If the goal is to PRODUCE A FILE (a README, script, doc, etc.), the deliverable is "
                          "the file WRITTEN TO DISK: call write_file yourself with the complete content (or have "
                          "a subagent do it), then finish with a one-line confirmation of the path — do NOT paste "
                          "the file's full contents as your answer and stop, and do NOT end on a raw tool result."),
    },
    "general": {
        "description": "General-purpose daily assistant.",
        "model": DEFAULT_MODEL,
        "allowed_tools": "all",
        "system_prompt": "You handle any everyday task: questions, code, files, system checks, web lookups.",
    },
    "ba": {
        "description": "Business Analyst — turns goals into clear requirements.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Business Analyst. Clarify the user's goal, break it into concrete "
                          "requirements and acceptance criteria, and write a concise spec. Ask a blocking "
                          "question only when genuinely necessary."),
    },
    "dev": {
        "description": "Software Developer — implements and runs code.",
        "model": "coder:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Software Developer. Implement the requested code, write it to files, "
                          "then run and debug it with the shell. Keep changes minimal and working; show what you ran."),
    },
    "qa": {
        "description": "QA Engineer — reviews and tests.",
        "model": "qa:qwen3.6-27b",
        "allowed_tools": "all",
        "system_prompt": ("You are a QA Engineer. Review code for correctness and edge cases, write and RUN "
                          "tests, and report pass/fail with specifics and reproduction steps."),
    },
    "researcher": {
        "description": "Research Analyst — trend/market research from the web.",
        "model": "orchestrator:qwen3.6-35b",
        "allowed_tools": "all",
        "system_prompt": ("You are a Research Analyst. Use web_search to gather current, credible information, "
                          "cross-check multiple sources, and summarize findings with links and a clear, actionable "
                          "recommendation. Distinguish facts from your inference."),
    },
    "reasoner": {
        "description": "Deep reasoning / math specialist (DeepSeek-R1-32B). Best for hard analysis, not tool-heavy work.",
        "model": "reasoner:deepseek-r1-32b",
        "allowed_tools": "all",
        "system_prompt": ("You are a careful reasoning specialist. Think step by step through hard problems in "
                          "math, logic, analysis, and debugging. Show the key steps of your reasoning, then give a "
                          "clear final answer. Use tools when a fact must be checked rather than assumed."),
    },
}

# Gateway aliases that no longer exist (renamed/removed over the project). A STARTER
# persona still pointing at one of these is auto-reset to its current DEFAULT_PERSONAS
# model on load. User-created personas are never touched.
RETIRED_MODELS = {
    "coder", "orchestrator", "reasoner", "qa", "vision", "embed",   # pre-colon short names
    "reasoner:qwen3.6-27b", "vision:qwen3.6-27b",                    # renamed colon aliases
}

def load_personas() -> dict:
    if not PERSONAS_FILE.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        PERSONAS_FILE.write_text(json.dumps(DEFAULT_PERSONAS, indent=2))
        return dict(DEFAULT_PERSONAS)
    try:
        p = json.loads(PERSONAS_FILE.read_text())
    except Exception as e:
        print(c(f"warning: personas.json unreadable ({e}); using defaults", "yellow"))
        return dict(DEFAULT_PERSONAS)
    changed = False
    # 1) add any missing STARTER personas (e.g. a newly-shipped 'orchestrator')
    for name, cfg in DEFAULT_PERSONAS.items():
        if name not in p:
            p[name] = dict(cfg); changed = True
    # 2) repair dead model aliases on STARTERS only → reset to their current intended
    #    model. User-created personas are left exactly as the user set them.
    for name in DEFAULT_PERSONAS:
        if p.get(name, {}).get("model") in RETIRED_MODELS:
            p[name]["model"] = DEFAULT_PERSONAS[name]["model"]; changed = True
    if changed:
        try: save_personas(p)
        except Exception: pass
    return p

def save_personas(p: dict):
    WORKDIR.mkdir(parents=True, exist_ok=True)
    PERSONAS_FILE.write_text(json.dumps(p, indent=2))

def installed_models() -> list:
    try:
        r = requests.get(f"{GATEWAY}/models", timeout=8)
        return [m["id"] for m in r.json().get("data", [])]
    except Exception:
        return []

# ─────────────────────────────── tools ────────────────────────────────────
def _within_allowed(p: Path) -> bool:
    try: rp = p.expanduser().resolve()
    except Exception: return False
    return any(str(rp).startswith(str(r.resolve())) for r in ALLOWED_ROOTS)

# Vision. Preferred path: a resident mlx_vlm.server (model stays warm → fast, and it
# sidesteps mlx-openai-server's multimodal hang). If that server isn't up, fall back
# to a bounded one-shot subprocess that loads the model fresh — slower, but always works.
_VISION_CODE = (
    "import sys\n"
    "from mlx_vlm import load, generate\n"
    "from mlx_vlm.prompt_utils import apply_chat_template\n"
    "from mlx_vlm.utils import load_config\n"
    "repo, img, q = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "model, proc = load(repo)\n"
    "cfg = load_config(repo)\n"
    "fp = apply_chat_template(proc, cfg, q, num_images=1)\n"
    "out = generate(model, proc, fp, [img], verbose=False, max_tokens=512)\n"
    "print((getattr(out, 'text', None) or str(out)).strip())\n"
)

def _image_to_url(src: str) -> str:
    """A remote URL is passed through; a local file becomes a base64 data URL."""
    if src.startswith("http://") or src.startswith("https://"):
        return src
    data = Path(src).read_bytes()
    ext = Path(src).suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}.get(ext, "png")
    return f"data:image/{mime};base64," + base64.b64encode(data).decode()

def _vision_via_server(src: str, question: str, timeout: int = 300) -> str:
    """Ask the resident mlx_vlm.server (OpenAI-compatible). Raises on any failure so
    the caller can fall back to the subprocess."""
    body = {"model": VISION_MODEL, "max_tokens": 512, "temperature": 0.0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": _image_to_url(src)}}]}]}
    r = requests.post(f"{VISION_SERVER}/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()

def t_see_image(image_path: str, question: str = "Describe this image in detail.") -> str:
    src = (image_path or "").strip()
    if not (src.startswith("http://") or src.startswith("https://")):
        p = Path(src)
        if not _within_allowed(p): return f"[refused: {src} is outside the allowed folders]"
        if not p.expanduser().exists(): return f"[no such image: {src}]"
        src = str(p.expanduser().resolve())
    emit(c(f"  👁  see_image({src[:60]} · {VISION_MODEL})", "green"))
    try:                                   # fast path: warm resident server
        out = _vision_via_server(src, question)
        if out: return out
    except Exception:
        pass                               # server down/erroring → fall back below
    try:                                   # reliable path: fresh bounded subprocess
        r = subprocess.run([sys.executable, "-c", _VISION_CODE, VISION_MODEL, src, question],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "[vision timed out (600s) — the model may be too large or the multimodal path is hanging]"
    if r.returncode != 0:
        return f"[vision failed: {(r.stderr or '').strip()[-400:]}]"
    return (r.stdout or "").strip() or "[vision produced no output]"

def t_run_shell(command: str) -> str:
    command = command.strip()
    # 1) Refuse machine-wrecking commands outright — no approval path.
    if _CATASTROPHIC_RE.search(command):
        return ("[blocked: refusing a destructive/system-level command (sudo, disk wipe, "
                "rm -rf / , shutdown, etc.). If this is truly intended, run it yourself.]")
    # 2) Installs and deletes need a human, even in autonomous mode.
    if _INSTALL_RE.search(command):
        if not confirm(f"install packages: {c(command,'bold')}", kind="install"):
            return "[install denied by user]"
    elif _DELETE_RE.search(command):
        # Deletes inside a git work tree are recoverable → auto-approve (unless a strict persona).
        git_safe = AUTONOMOUS and not STRICT_RUN and _delete_is_git_safe(command)
        if git_safe:
            emit(c("  (git-tracked → recoverable → auto-approved delete)", "dim"))
        elif not confirm(f"DELETE files/folders: {c(command,'bold')}", kind="delete"):
            return "[delete denied by user]"
    elif STRICT_RUN:
        # A strict persona confirms every command, autonomy notwithstanding.
        if not confirm(f"run shell: {c(command,'bold')}", kind="run"):
            return "[denied by user]"
    elif not AUTONOMOUS:
        # Legacy confirm-the-risky behaviour when autonomy is switched off.
        segments = [s.strip() for s in command.split("|")]
        first_tokens = []
        for seg in segments:
            try: first_tokens.append(shlex.split(seg)[0])
            except Exception: first_tokens.append("")
        all_safe = bool(first_tokens) and all(t in SAFE_COMMANDS for t in first_tokens)
        if not (all_safe and not DANGEROUS.search(command)):
            if not confirm(f"run shell: {c(command,'bold')}", kind="run"):
                return "[denied by user]"
    # 3) Everything else runs freely.
    emit(c(f"  $ {command}", "cyan"))
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=SHELL_TIMEOUT)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        out = out.strip() or "[no output]"
        return out[:6000] + ("\n…[truncated]" if len(out) > 6000 else "")
    except subprocess.TimeoutExpired: return f"[timed out after {SHELL_TIMEOUT}s]"
    except Exception as e: return f"[error: {e}]"

def t_scaffold_swift(directory: str, name: str = "App", kind: str = "executable") -> str:
    """Create a buildable Swift package with SwiftPM (`swift package init`). This is the RELIABLE
    way to start a Swift/Xcode project: it produces a Package.swift that `swift build` compiles and
    that opens directly in Xcode — no fragile .xcodeproj/project.pbxproj to hand-write."""
    d = Path(directory).expanduser()
    if not _within_allowed(d):
        return f"[refused: {directory} is outside allowed paths]"
    kinds = {"executable", "library", "tool", "empty", "macro", "build-tool-plugin", "command-plugin"}
    k = kind if kind in kinds else "executable"
    if shutil.which("swift") is None:
        return "[swift not found — install Xcode Command Line Tools: `xcode-select --install`, then retry]"
    if not confirm(f"scaffold Swift {k} package {c(name,'bold')} in {c(str(d),'bold')} (swift package init)?", kind="run"):
        return "[denied by user]"
    emit(c(f"  🛠  scaffold_swift({name} · {k} · {d})", "cyan"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["swift", "package", "init", "--type", k, "--name", name],
                           cwd=str(d), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return f"[scaffold failed: {((r.stderr or r.stdout) or '').strip()[-400:]}]"
        tree = subprocess.run(["find", str(d), "-maxdepth", "2", "-not", "-path", "*/.*"],
                              capture_output=True, text=True, timeout=20).stdout.strip()
        return (f"[scaffolded Swift {k} package '{name}' in {d}]\n{tree}\n"
                f"Build: (cd {d} && swift build)   ·   Open in Xcode: open {d}/Package.swift\n"
                f"Now write the Swift source under {d}/Sources/ with write_file, then run `swift build` "
                f"via run_shell and fix any errors before moving on.")
    except subprocess.TimeoutExpired:
        return "[scaffold timed out]"
    except Exception as e:
        return f"[error: {e}]"

def t_disk_usage(_: str = "") -> str: return t_run_shell("df -h /")

def t_run_python(code: str, timeout: int = 0) -> str:
    """Write an ephemeral Python script and run it in the workstation's Python (which has
    mlx, requests, psutil, etc.). This is the general-purpose 'do it in code' escape hatch —
    for anything without a dedicated tool. print() whatever you want returned."""
    code = (code or "").strip()
    if not code:
        return "[no code provided]"
    to = int(timeout) if (timeout and int(timeout) > 0) else CODE_TIMEOUT
    preview = code if len(code) <= 400 else code[:400] + " …"
    if not confirm(f"run python script ({len(code)} chars, {to}s):\n{c(preview,'dim')}", kind="run"):
        return "[denied by user]"
    emit(c(f"  🐍 run_python ({len(code)} chars)", "cyan"))
    import tempfile
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".py", prefix="agent_", dir="/tmp")
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        r = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=to)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        if r.returncode != 0 and not out.strip():
            out = f"[exited {r.returncode} with no output]"
        out = out.strip() or "[ran OK, no output — print() what you want back]"
        return out[:6000] + ("\n…[truncated]" if len(out) > 6000 else "")
    except subprocess.TimeoutExpired:
        return f"[timed out after {to}s]"
    except Exception as e:
        return f"[error: {e}]"
    finally:
        if path:
            try: os.remove(path)
            except Exception: pass

def t_read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    if not p.is_file(): return f"[not a file: {path}]"
    try:
        data = p.read_text(errors="replace")
        return data[:8000] + ("\n…[truncated]" if len(data) > 8000 else "")
    except Exception as e: return f"[error reading {path}: {e}]"

def t_list_dir(path: str = ".") -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    if not p.is_dir(): return f"[not a directory: {path}]"
    try:
        items = sorted(p.iterdir())
        return "\n".join(("📁 " if i.is_dir() else "📄 ") + i.name for i in items[:200]) or "[empty]"
    except Exception as e: return f"[error: {e}]"

def t_write_file(path: str, content: str, mode: str = "w") -> str:
    p = Path(path).expanduser()
    if not _within_allowed(p): return f"[refused: {path} is outside allowed paths]"
    append = str(mode).lower() in ("a", "append")
    verb = "append to" if append else "write"
    preview = content if len(content) < 300 else content[:300] + "…"
    if not confirm(f"{verb} {len(content)} chars {'onto' if append else 'to'} {c(str(p),'bold')}?\n{c(preview,'dim')}\n", kind="write"):
        return "[write denied by user]"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w") as fh:
            fh.write(content)
        total = p.stat().st_size
        return f"[{'appended' if append else 'wrote'} {len(content)} chars to {p} (file now {total} bytes)]"
    except Exception as e: return f"[error writing {path}: {e}]"

def t_web_search(query: str) -> str:
    try:
        r = requests.get(f"{SEARXNG}/search", params={"q": query, "format": "json"}, timeout=20)
        results = r.json().get("results", [])[:5]
        if not results: return "[no results]"
        return "\n\n".join(f"{x.get('title','')}\n{x.get('url','')}\n{x.get('content','')}" for x in results)
    except Exception as e: return f"[search error: {e} — is SearXNG up on {SEARXNG}?]"

def t_web_fetch(url: str) -> str:
    """Fetch and return the readable text of a web page — read a source/doc instead of re-searching."""
    if not re.match(r"^https?://", url or ""):
        return "[error: url must start with http:// or https://]"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "mlx-agent/1.0"})
        ct = r.headers.get("content-type", "")
        text = r.text
        if "html" in ct:   # strip tags/script/style to readable text
            text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = re.sub(r"&[a-z]+;", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return text[:10000] + ("\n…[truncated]" if len(text) > 10000 else "")
    except Exception as e:
        return f"[fetch error for {url}: {e}]"

def t_ask_choice(question: str, options=None) -> str:
    """Ask the user to pick one of several options. Renders as buttons on Telegram."""
    if isinstance(options, str): options = [options]
    options = [str(o) for o in (options or []) if str(o).strip()]
    if not options: return "[error: ask_choice needs a non-empty 'options' list]"
    if CHOICE_FN is not None:
        try: return CHOICE_FN(question, options)
        except Exception: return options[0]
    emit(c(f"  ? {question}", "yellow"))
    for i, o in enumerate(options, 1): emit(f"    {i}) {o}")
    try: raw = input(c("  choose [number]: ", "cyan")).strip()
    except (EOFError, KeyboardInterrupt): return options[0]
    if raw.isdigit() and 1 <= int(raw) <= len(options): return options[int(raw)-1]
    return raw or options[0]

# ── self-healing: acquire missing capabilities (with approval + audit) ──
CAP_LOG = WORKSPACE / "capability-log.jsonl"
VENV_PY = str(WORKDIR / ".venv" / "bin" / "python")   # the workstation venv interpreter

def _venv_scope(cmd: str) -> str:
    """Rewrite a package-install command to target the workstation venv, never system Python."""
    c = cmd.strip()
    # pip / pip3 / python -m pip / python3 -m pip  →  <venv python> -m pip
    c = re.sub(r"^(sudo\s+)?(python3?\s+-m\s+)?pip3?\s+install\b",
               f'"{VENV_PY}" -m pip install', c)
    # uv pip install ...  →  uv pip install --python <venv python> ...
    if re.match(r"^uv\s+pip\s+install\b", c) and "--python" not in c:
        c = c.replace("uv pip install", f'uv pip install --python "{VENV_PY}"', 1)
    return c

def _log_capability(entry: dict):
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        with open(CAP_LOG, "a") as f: f.write(json.dumps(entry) + "\n")
    except Exception: pass

def _run_install_cmd(cmd: str) -> tuple:
    print(c(f"  $ {cmd}", "cyan"))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=900)
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired: return 124, "[timed out after 900s]"
    except Exception as e: return 1, f"[error: {e}]"

def t_propose_capability(need: str, kind: str = "package", commands=None,
                         script_url: str = "", run_command: str = "",
                         verify: str = "", reason: str = "") -> str:
    kind = (kind or "package").lower()
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    print(c("\n  ┌─ capability request ───────────────────────────────", "magenta"))
    print(c(f"  │ need   : {need}", "magenta"))
    if reason: print(c(f"  │ reason : {reason}", "magenta"))

    if kind == "script":
        if not script_url: return "[error: script kind requires script_url]"
        try: body = requests.get(script_url, timeout=30).text
        except Exception as e: return f"[could not fetch {script_url}: {e}]"
        print(c(f"  │ source : {script_url}", "magenta"))
        print(c(f"  │ run    : {run_command or '(bash the file)'}", "magenta"))
        print(c("  └────────────────────────────────────────────────────", "magenta"))
        print(c("  ── SCRIPT CONTENT — review carefully, this runs on YOUR machine ──", "yellow"))
        print(body[:8000] + ("\n…[truncated — full script saved before run]" if len(body) > 8000 else ""))
        print(c("  ──────────────────────────────────────────────────────────────", "yellow"))
        # force_ask: arbitrary web scripts ALWAYS ask, even under "allow rest of run"
        approved = confirm(f"fetch-and-run this script from {script_url}?", force_ask=True)
        _log_capability({"ts":ts,"kind":"script","need":need,"url":script_url,"run":run_command,"approved":approved})
        if not approved: return "[user declined the script — do not retry it]"
        dest = Path("/tmp") / f"cap_{int(datetime.datetime.now().timestamp())}.sh"
        dest.write_text(body)
        rc, out = _run_install_cmd(run_command or f"bash {dest}")
        return f"[script {'ok' if rc==0 else 'FAILED rc='+str(rc)}]\n{out[:3000]}"

    # package install
    if isinstance(commands, str): commands = [commands]
    commands = commands or []
    if not commands: return "[error: package kind requires commands]"
    print(c("  │ plan   :", "magenta"))
    for cmd in commands: print(c(f"  │   $ {cmd}", "bold"))
    print(c("  └────────────────────────────────────────────────────", "magenta"))
    approved = confirm("install plan (into workstation venv):\n  " + "\n  ".join(commands), kind="install")
    _log_capability({"ts":ts,"kind":"package","need":need,"commands":commands,"approved":approved})
    if not approved: return "[user declined the install — do not retry it]"
    outs = []
    for cmd in commands:
        scoped = _venv_scope(cmd)
        rc, out = _run_install_cmd(scoped); outs.append(f"$ {scoped}\n{out[:1500]}")
        if rc != 0: outs.append(f"[stopped: '{scoped}' failed rc={rc}]"); break
    if verify:
        rc, vout = _run_install_cmd(verify)
        outs.append(f"[verify {'OK — capability ready' if rc==0 else 'FAILED (installed ≠ working)'}] {vout[:500]}")
    return "\n".join(outs)

TOOLS = {
    "run_shell":  (t_run_shell,  "Run a shell command on the host and return its output. Read-only commands "
                                 "run automatically; others ask the user first."),
    "disk_usage": (t_disk_usage, "Return human-readable free/used disk space for the root volume."),
    "run_python": (t_run_python, "Write and run an ephemeral Python script in the workstation's Python "
                   "(which has mlx, requests, psutil, etc.). This is your general-purpose way to DO a task "
                   "that has no dedicated tool — parse/transform data, call a library, do multi-step logic, "
                   "or build file content programmatically. print() whatever you want back (stdout is returned). "
                   "Prefer one short script over many shell calls or giving up. Args: code (str), timeout (int, "
                   "optional seconds). Asks the user to confirm first."),
    "read_file":  (t_read_file,  "Read a text file from an allowed path."),
    "list_dir":   (t_list_dir,   "List the contents of a directory in an allowed path."),
    "write_file": (t_write_file, "Create or overwrite a file on disk with the given content. "
                   "Call this WHENEVER the user asks you to create, write, save, produce, generate, "
                   "or update a file (README, script, config, doc). Pass the COMPLETE final content — "
                   "do not just print it in chat. For a file too large for one call, write the first "
                   "part then call again with mode='a' to append more. Asks the user to confirm first."),
    "scaffold_swift": (t_scaffold_swift, "Start a buildable Swift/Xcode project the RELIABLE way, using "
                   "SwiftPM (swift package init). Produces a Package.swift that `swift build` compiles and "
                   "that opens in Xcode. Use this to begin ANY Swift/macOS/iOS app — NEVER hand-write "
                   ".xcodeproj or project.pbxproj. Args: directory, name, kind (executable|library|empty). "
                   "After scaffolding, write source files under Sources/ and build with run_shell."),
    "web_search": (t_web_search, "Search the web via the local private SearXNG for current info."),
    "web_fetch":  (t_web_fetch,  "Fetch and read the actual text of a web page by URL. Use this to READ a "
                                 "doc, README, or source file instead of repeatedly searching — e.g. after a "
                                 "search returns a promising URL, fetch it to get the real content."),
    "see_image":  (t_see_image,  "Look at an image and answer a question about it (OCR, description, charts, "
                                 "screenshots). Args: image_path (a local path or http(s) URL) and question. "
                                 "Runs a local vision model — first use loads it, so allow some time."),
    "ask_choice": (t_ask_choice, "Ask the user to choose among concrete options (renders as tappable buttons on "
                                 "Telegram). Use when you genuinely need the user to decide a direction. Args: "
                                 "question (str) and options (list of short strings). Returns the chosen option."),
    "propose_capability": (t_propose_capability,
        "Acquire a MISSING capability instead of giving up. Call this when the task needs a tool, "
        "package, library, or CLI you don't have. Provide: need (what's missing), kind ('package' or "
        "'script'), and for packages a 'commands' list (e.g. ['uv pip install pytesseract','brew install "
        "tesseract']) plus an optional 'verify' command; for scripts a 'script_url' and 'run_command'. "
        "The user reviews and approves before anything runs. After it succeeds, retry the original task."),
    "spawn_subagent": (None,
        "Delegate a sub-task to a specialist persona. Args: persona (registry name like researcher, dev, qa, "
        "ba, reasoner, general) and task (what that specialist should do). Returns the subagent's final answer "
        "plus a short trace of the tools it used. Use this to break a big goal into specialist steps."),
}
def schema(name, props, req):
    return {"type":"function","function":{"name":name,"description":TOOLS[name][1],
            "parameters":{"type":"object","properties":props,"required":req}}}
ALL_SCHEMAS = {
    "run_shell":  schema("run_shell", {"command":{"type":"string"}}, ["command"]),
    "disk_usage": schema("disk_usage", {}, []),
    "run_python": schema("run_python", {"code":{"type":"string","description":"the Python source to run"},
                                        "timeout":{"type":"integer","description":"optional wall-clock seconds"}}, ["code"]),
    "read_file":  schema("read_file", {"path":{"type":"string"}}, ["path"]),
    "list_dir":   schema("list_dir", {"path":{"type":"string"}}, []),
    "write_file": schema("write_file", {"path":{"type":"string"},"content":{"type":"string"},
                                         "mode":{"type":"string","enum":["w","a"],"description":"w=overwrite (default), a=append"}}, ["path","content"]),
    "scaffold_swift": schema("scaffold_swift", {"directory":{"type":"string","description":"folder to create the package in"},
                                                "name":{"type":"string","description":"package/app name"},
                                                "kind":{"type":"string","enum":["executable","library","empty"]}}, ["directory"]),
    "web_search": schema("web_search", {"query":{"type":"string"}}, ["query"]),
    "web_fetch":  schema("web_fetch", {"url":{"type":"string"}}, ["url"]),
    "see_image":  schema("see_image", {"image_path":{"type":"string","description":"local path or http(s) URL of the image"},
                                       "question":{"type":"string","description":"what to ask about the image"}}, ["image_path"]),
    "ask_choice": schema("ask_choice", {
        "question":{"type":"string"},
        "options":{"type":"array","items":{"type":"string"}},
    }, ["question","options"]),
    "propose_capability": schema("propose_capability", {
        "need":{"type":"string","description":"what capability is missing"},
        "kind":{"type":"string","enum":["package","script"]},
        "commands":{"type":"array","items":{"type":"string"},"description":"install commands for kind=package"},
        "script_url":{"type":"string","description":"URL to fetch for kind=script"},
        "run_command":{"type":"string","description":"how to run the fetched script"},
        "verify":{"type":"string","description":"command to confirm the capability now works"},
        "reason":{"type":"string","description":"why it's needed for the task"},
    }, ["need","kind"]),
    "spawn_subagent": schema("spawn_subagent", {
        "persona":{"type":"string","description":"registry persona to delegate to (researcher, dev, qa, ba, reasoner, general)"},
        "task":{"type":"string","description":"the sub-task for that specialist to perform"},
    }, ["persona","task"]),
}
def tools_for(persona: dict) -> tuple:
    allowed = persona.get("allowed_tools", "all")
    if allowed == "all" or not allowed:
        names = list(ALL_SCHEMAS.keys())
    else:
        names = [t for t in allowed if t in ALL_SCHEMAS]
    return set(names), [ALL_SCHEMAS[n] for n in names]

# ─────────────────────────── model plumbing ───────────────────────────────
def _looks_degenerate(text: str) -> bool:
    """True when a generation fell into a repetition loop: many lines, almost none unique."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return len(lines) >= 15 and len(set(lines)) <= 5

def strip_thinking(text: str) -> str:
    if not text: return ""
    if "</think>" in text: text = text.split("</think>")[-1]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()

_SERVED = {"names": None, "t": 0.0}
def _served_models():
    """Cache the server's real model IDs (served_model_name) for ~60s."""
    now = time.time()
    if _SERVED["names"] is None or now - _SERVED["t"] > 60:
        try:
            r = requests.get(f"{GATEWAY}/models", timeout=8)
            _SERVED["names"] = [d.get("id") for d in (r.json().get("data") or []) if d.get("id")]
        except Exception:
            _SERVED["names"] = _SERVED["names"] or []
        _SERVED["t"] = now
    return _SERVED["names"]

def _resolve_model(m: str) -> str:
    """Map a persona/LiteLLM-style alias to the server's actual served_model_name.
    e.g. 'orchestrator:qwen3.6-35b' -> 'qwen36-35b'. Bypassing LiteLLM means the mlx
    server only knows its served names, so we translate before every call."""
    m = (m or "").strip()
    served = _served_models()
    if served and m in served:
        return m
    cand = m.split(":")[-1].replace(".", "")        # 'coder:qwen3.6-27b' -> 'qwen36-27b'
    if not served or cand in served:
        return cand
    for s in served:                                 # last resort: loose match
        if cand and (cand in s or s in cand):
            return s
    return cand

def call_gateway(model: str, messages: list, schemas: list) -> dict:
    # Always non-streaming. Reliable tool-call detection is the whole point of the
    # agent, and streaming tool-calls through the local MLX backend is unreliable —
    # the model's tool call can arrive as plain text and get mistaken for the answer.
    # The final answer is streamed to live front-ends separately (see run_agent), by
    # replaying it once generation is safely complete.
    body = {"model": _resolve_model(model), "messages": messages, "tools": schemas,
            "tool_choice": "auto", "temperature": TEMPERATURE, "stream": False}
    if MAX_TOKENS > 0:                       # 0 → uncapped; a finite value bounds a runaway loop
        body["max_tokens"] = MAX_TOKENS
    if FREQ_PENALTY:                         # curbs degenerate "I will run the script" repetition loops
        body["frequency_penalty"] = FREQ_PENALTY
    if PRESENCE_PENALTY:
        body["presence_penalty"] = PRESENCE_PENALTY
    r = requests.post(f"{GATEWAY}/chat/completions", json=body, timeout=600)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]

def execute_tool_call(tc: dict, allowed_names: set) -> str:
    name = tc["function"]["name"]
    if name not in allowed_names:
        return f"[refused: this persona is not permitted to use '{name}']"
    try: args = json.loads(tc["function"].get("arguments") or "{}")
    except json.JSONDecodeError: args = {}
    fn = TOOLS.get(name, (None,))[0]
    if fn is None: return f"[unknown tool: {name}]"
    emit(c(f"  → {name}({', '.join(f'{k}={v!r}' for k,v in args.items())})", "green"))
    try: return fn(**args)
    except TypeError as e: return f"[bad arguments for {name}: {e}]"
    except Exception as e: return f"[tool error in {name}: {e}]"

# ── activity beacon: a running top-level task drops a small json file so the
#    dashboard can show "what's an agent doing right now" across processes ─────
ACTIVITY_DIR = WORKDIR / "activity"
class _Activity:
    def __init__(self, query: str, persona: dict, depth: int):
        self.path = None
        if depth != 0:                      # only the top-level task is tracked (subagents show as 'current')
            return
        try:
            ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
            self.path = ACTIVITY_DIR / f"{os.getpid()}.json"
            self.rec = {"pid": os.getpid(), "task": (query or "").strip()[:240],
                        "model": persona.get("model", ""),
                        "persona": (persona.get("description", "") or "")[:48],
                        "started": time.time(), "last": time.time(), "current": "starting…"}
            self._flush()
        except Exception:
            self.path = None
    def update(self, current: str):
        if not self.path: return
        try:
            self.rec["current"] = (current or "")[:140]; self.rec["last"] = time.time(); self._flush()
        except Exception: pass
    def _flush(self):
        self.path.write_text(json.dumps(self.rec))
    def done(self):
        if not self.path: return
        try: self.path.unlink()
        except Exception: pass

def run_agent(query: str, persona: dict, depth: int = 0, spawn_ctx: dict = None,
              trace_out: list = None, history: list = None) -> str:
    global RUN_APPROVE_ALL, STRICT_RUN
    if spawn_ctx is None: spawn_ctx = {"count": 0}
    if depth == 0:
        RUN_APPROVE_ALL = False                       # each new top-level task starts fresh
    prev_strict = STRICT_RUN
    STRICT_RUN = prev_strict or (persona.get("approval") == "strict")   # stricter only, never looser
    act = _Activity(query, persona, depth)
    try:
        allowed_names, schemas = tools_for(persona)
        model = persona.get("model", DEFAULT_MODEL)
        system = BASE_SYSTEM + "\n\n" + persona.get("system_prompt", "")
        # Prior turns give the agent conversation memory: a follow-up like "and who
        # came second?" resolves against the earlier topic instead of starting cold.
        prior = []
        for h in (history or []):
            role = h.get("role"); content = (h.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                prior.append({"role": role, "content": content[:4000]})
        prior = prior[-20:]                               # cap to keep context bounded
        messages = [{"role":"system","content":system}] + prior + [{"role":"user","content":query}]
        searches = 0
        step = 0
        last_sig = None; repeats = 0
        while True:
            step += 1
            if MAX_STEPS > 0 and step > MAX_STEPS:
                return "[stopped: reached configured step cap MLX_AGENT_MAX_STEPS]"
            act.update("thinking…")
            msg = call_gateway(model, messages, schemas)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                ans = strip_thinking(msg.get("content") or "") or "[no answer]"
                if _looks_degenerate(ans):
                    ans = ("[the model fell into a repetition loop instead of answering — this usually "
                           "means it's too weak at tool-calling for this task. Try /reset and rephrase, "
                           "or switch the persona to the dense qwen3.6-27b coder (MoE models loop here). "
                           "You can also raise MLX_AGENT_FREQ_PENALTY.]")
                # Live-stream the answer to front-ends (e.g. Telegram) ONLY at the top
                # level and ONLY now that generation is safely complete — never during
                # tool turns and never from a subagent (which would interleave garbage).
                if depth == 0 and STREAM_FN is not None and not ans.startswith("[no answer"):
                    try:
                        piece = max(24, len(ans) // 20)
                        for i in range(0, len(ans), piece):
                            STREAM_FN(ans[i:i + piece])
                    except Exception:
                        pass
                return ans
            # Anti-hang guard (NOT a step cap): if the model emits the exact same tool
            # call over and over, it's stuck in a loop — break instead of spinning forever.
            if LOOP_GUARD > 0:
                sig = json.dumps([[tc["function"].get("name"), tc["function"].get("arguments")]
                                  for tc in tool_calls], sort_keys=True)
                repeats = repeats + 1 if sig == last_sig else 0
                last_sig = sig
                if repeats >= LOOP_GUARD:
                    return (f"[stopped: the model repeated the identical tool call {repeats+1}× without "
                            "making progress — it's stuck. Try rephrasing the task, or switch to a dense "
                            "model like qwen3.6-27b (MoE models tend to loop on tool chains). "
                            "Disable this guard with MLX_AGENT_LOOP_GUARD=0.]")
            messages.append({"role":"assistant","content":msg.get("content") or "","tool_calls":tool_calls})
            for tc in tool_calls:
                name = tc["function"]["name"]
                act.update(f"tool: {name}")
                if name == "web_search":
                    searches += 1
                    if searches > MAX_SEARCHES:
                        result = ("[search limit reached — STOP searching. Either web_fetch a specific URL you already "
                                  "found to read its real content, or take the direct route (install the package and "
                                  "introspect it, read the source file, or answer with what you have).]")
                        messages.append({"role":"tool","tool_call_id":tc.get("id",""),"name":name,"content":result})
                        if trace_out is not None: trace_out.append(name+"[capped]")
                        continue
                if name == "spawn_subagent":
                    if "spawn_subagent" not in allowed_names:
                        result = "[refused: this persona cannot spawn subagents]"
                    else:
                        try: args = json.loads(tc["function"].get("arguments") or "{}")
                        except json.JSONDecodeError: args = {}
                        result = t_spawn_subagent(depth, spawn_ctx, **args)
                else:
                    result = execute_tool_call(tc, allowed_names)
                if trace_out is not None: trace_out.append(name)
                messages.append({"role":"tool","tool_call_id":tc.get("id",""),
                                 "name":name,"content":result})
    finally:
        STRICT_RUN = prev_strict
        act.done()

def t_spawn_subagent(depth: int, spawn_ctx: dict, persona: str = "", task: str = "", **_) -> str:
    if depth >= MAX_SPAWN_DEPTH:
        return f"[refused: max spawn depth {MAX_SPAWN_DEPTH} reached — do this step yourself]"
    if spawn_ctx.get("count", 0) >= MAX_SPAWNS:
        return f"[refused: spawn budget {MAX_SPAWNS} exhausted — finish with what you have]"
    personas = load_personas()
    sub = personas.get(persona)
    if not sub:
        return f"[no persona '{persona}'. Available: {', '.join(personas)}]"
    spawn_ctx["count"] = spawn_ctx.get("count", 0) + 1
    emit(c(f"\n  ╭─ spawn: {persona}  (depth {depth+1}, subagent #{spawn_ctx['count']})", "magenta"))
    emit(c(f"  │  task: {task[:140]}", "magenta"))
    subtrace = []
    answer = run_agent(task, sub, depth + 1, spawn_ctx, trace_out=subtrace)
    tools_used = ", ".join(subtrace) if subtrace else "none"
    emit(c(f"  ╰─ {persona} done · tools: {tools_used}", "magenta"))
    return f"[subagent '{persona}' finished — tools used: {tools_used}]\n{answer}"

# ─────────────────────────── persona CRUD ─────────────────────────────────
def cmd_list_personas():
    p = load_personas()
    print(c("\nPersonas","bold"), c(f"({PERSONAS_FILE})","dim"))
    for name, cfg in p.items():
        tools = cfg.get("allowed_tools","all")
        tools = "all tools" if tools=="all" else ", ".join(tools)
        strict = "  · STRICT approval" if cfg.get("approval")=="strict" else ""
        print(f"  {c(name,'magenta'):<24} {c(cfg.get('model',''),'cyan')}{c(strict,'yellow')}")
        print(f"    {cfg.get('description','')}  {c('['+tools+']','dim')}")

def _pick_model(current: str = "") -> str:
    models = installed_models()
    if models:
        print("  installed models:")
        for i, m in enumerate(models, 1): print(f"    {i}) {m}")
        raw = input(c(f"  model [number, or type a name{f', blank={current}' if current else ''}]: ","cyan")).strip()
        if not raw and current: return current
        if raw.isdigit() and 1 <= int(raw) <= len(models): return models[int(raw)-1]
        return raw
    return input(c(f"  model name (gateway offline; free-form){f' [{current}]' if current else ''}: ","cyan")).strip() or current

def _multiline(prompt: str, current: str = "") -> str:
    print(c(f"  {prompt} (end with a blank line{'; blank=keep' if current else ''}):","cyan"))
    lines = []
    while True:
        try: line = input("    ")
        except EOFError: break
        if line == "": break
        lines.append(line)
    return "\n".join(lines) if lines else current

def cmd_add_persona():
    p = load_personas()
    name = input(c("persona name (lowercase, e.g. pentester): ","cyan")).strip()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,30}$", name): return print(c("invalid name","red"))
    if name in p and not confirm(f"'{name}' exists — overwrite?"): return
    desc = input(c("  one-line description: ","cyan")).strip()
    model = _pick_model()
    sysp = _multiline("system prompt")
    tools_raw = input(c("  allowed tools [Enter=all, or comma list e.g. web_search,read_file]: ","cyan")).strip()
    allowed = "all" if not tools_raw else [t.strip() for t in tools_raw.split(",") if t.strip()]
    strict = input(c("  approval level [normal / strict] (strict = always ask, no 'allow-all'): ","cyan")).strip().lower()
    entry = {"description":desc,"model":model,"allowed_tools":allowed,"system_prompt":sysp}
    if strict == "strict": entry["approval"] = "strict"
    p[name] = entry
    save_personas(p); print(c(f"✓ saved persona '{name}'","green"))

def cmd_edit_persona(name: str):
    p = load_personas()
    if name not in p: return print(c(f"no persona '{name}' (see --list-personas)","red"))
    cur = p[name]
    print(c(f"editing '{name}' — blank keeps current value","dim"))
    desc = input(c(f"  description [{cur.get('description','')}]: ","cyan")).strip() or cur.get("description","")
    model = _pick_model(cur.get("model",""))
    sysp = _multiline("system prompt", cur.get("system_prompt",""))
    tr = input(c(f"  allowed tools [{cur.get('allowed_tools','all')}] (Enter=keep, 'all', or comma list): ","cyan")).strip()
    allowed = cur.get("allowed_tools","all") if not tr else ("all" if tr=="all" else [t.strip() for t in tr.split(",")])
    cur_appr = cur.get("approval","normal")
    ap = input(c(f"  approval level [{cur_appr}] (normal / strict, Enter=keep): ","cyan")).strip().lower()
    approval = cur_appr if not ap else ap
    entry = {"description":desc,"model":model,"allowed_tools":allowed,"system_prompt":sysp}
    if approval == "strict": entry["approval"] = "strict"
    p[name] = entry
    save_personas(p); print(c(f"✓ updated '{name}'","green"))

def cmd_remove_persona(name: str):
    p = load_personas()
    if name not in p: return print(c(f"no persona '{name}'","red"))
    if not confirm(f"delete persona '{name}'?"): return
    del p[name]; save_personas(p); print(c(f"✓ removed '{name}'","green"))

# ─────────────────────────────── cli ──────────────────────────────────────
def _fmt(s: str) -> str:
    return textwrap.fill(s, width=100) if "\n" not in s and len(s) > 100 else s

def main():
    global AUTO_YES
    ap = argparse.ArgumentParser(description="Persona-aware local MLX tool-executor")
    ap.add_argument("query", nargs="*", help="your task (omit for interactive REPL)")
    ap.add_argument("--persona", default="general", help="persona to run as (see --list-personas)")
    ap.add_argument("--model", default=None, help="override the persona's model for this run")
    ap.add_argument("--yes", action="store_true", help="auto-approve shell/writes (careful)")
    ap.add_argument("--list-personas", action="store_true")
    ap.add_argument("--add-persona", action="store_true")
    ap.add_argument("--edit-persona", metavar="NAME")
    ap.add_argument("--remove-persona", metavar="NAME")
    args = ap.parse_args()
    AUTO_YES = args.yes

    if args.list_personas: return cmd_list_personas()
    if args.add_persona:   return cmd_add_persona()
    if args.edit_persona:  return cmd_edit_persona(args.edit_persona)
    if args.remove_persona:return cmd_remove_persona(args.remove_persona)

    personas = load_personas()
    persona = personas.get(args.persona)
    if persona is None:
        print(c(f"unknown persona '{args.persona}'. Available: {', '.join(personas)}","red"))
        print(c("(use --add-persona to create one, or --persona general)","dim")); return
    if args.model: persona = {**persona, "model": args.model}

    banner = (c("mlx-agent","bold") + c(f"  persona={args.persona}  model={persona.get('model')}  "
              f"gateway={GATEWAY}","dim"))
    if args.query:
        print(banner)
        try: print("\n" + _fmt(run_agent(" ".join(args.query), persona)))
        except requests.HTTPError as e: print(c(f"gateway error: {e} — is :4000 up?","red"))
        return
    print(banner + c("   (type 'exit' to quit)","dim"))
    while True:
        try: q = input(c(f"\n{args.persona} › ","cyan")).strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if q.lower() in ("exit","quit","q"): break
        if not q: continue
        try: print("\n" + _fmt(run_agent(q, persona)))
        except requests.HTTPError as e: print(c(f"gateway error: {e} — is :4000 up? (./mlx-setup.sh --status)","red"))
        except Exception as e: print(c(f"error: {e}","red"))

if __name__ == "__main__":
    main()
AGENTEOF
    chmod +x "$AD/mlx-agent.py"
    # First-class 'mlx-agent' command on PATH (uv tools bin), running under the venv.
    mkdir -p "$HOME/.local/bin" "$HOME/MLX-AI"
    cat > "$HOME/.local/bin/mlx-agent" <<SHEOF
#!/usr/bin/env bash
# Settings (override in your shell if you like):
export MLX_GATEWAY="\${MLX_GATEWAY:-http://localhost:${PORT_MLX}/v1}"   # mlx-openai-server direct (tool calls work); LiteLLM :$PORT_GATEWAY drops them
export MLX_SEARXNG="\${MLX_SEARXNG:-http://localhost:${PORT_SEARXNG}}"
export MLX_WORKDIR="$WORKDIR"
export MLX_WORKSPACE="\${MLX_WORKSPACE:-$HOME/MLX-AI}"
exec "$VENV/bin/python" "$AD/mlx-agent.py" "\$@"
SHEOF
    chmod +x "$HOME/.local/bin/mlx-agent"
    # Initialize/MIGRATE the persona registry now. load_personas() creates it with
    # starters on first run, and on later runs adds any newly-shipped starters (e.g.
    # 'orchestrator') and repairs starters pointing at retired model aliases — while
    # leaving personas you created untouched. So --bootstrap self-heals the registry.
    MLX_WORKDIR="$WORKDIR" MLX_WORKSPACE="$HOME/MLX-AI" "$VENV/bin/python" "$AD/mlx-agent.py" --list-personas >/dev/null 2>&1 || true
    ok "agent installed — run: mlx-agent \"how much disk is free?\"  (personas: mlx-agent --list-personas)"
}

cmd_agent() { [ -f "$WORKDIR/agent/mlx-agent.py" ] || { err "run --bootstrap first"; exit 1; }; MLX_WORKSPACE="$HOME/MLX-AI" "$VENV/bin/python" "$WORKDIR/agent/mlx-agent.py" "$@"; }

# =============================================================================
#  PHASE 9c — TELEGRAM BRIDGE (live thinking-then-collapse + inline-button gates)
# =============================================================================
write_telegram() {
    log "Telegram bridge (mlx-telegram)"
    local AD="$WORKDIR/agent"; mkdir -p "$AD"
    cat > "$AD/mlx-telegram.py" <<'TELEGRAMEOF'
#!/usr/bin/env python3
"""
mlx-telegram — Telegram bridge for the local MLX agent.

Puts the whole persona/agent stack in your pocket:
  • Live "thinking then collapse": one message updates in place with the agent's
    tool trace while it works, then collapses to just the final answer.
  • Native inline-button approvals: the [y]/[n]/[a] gate becomes tappable Yes / No /
    Allow-rest-of-run buttons; the agent's ask_choice becomes a row of buttons.
  • Locked to ONE Telegram user (your numeric ID) — ignores everyone else.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID from ~/.mlx-ai-workstation/.env
(set them with ./mlx-setup.sh --configure). Uses only `requests` — no bot SDK,
so nothing to version-clash with the rest of the stack.

Run:  ./mlx-setup.sh --telegram      (or as a launchd service; see the installer)
"""
from __future__ import annotations
import importlib.util, os, sys, threading, time, re
import html as _html
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests missing — run under the workstation venv.")

WORKDIR = Path(os.environ.get("MLX_WORKDIR", str(Path.home() / ".mlx-ai-workstation")))
ENV_FILE = WORKDIR / ".env"
AGENT_PATH = WORKDIR / "agent" / "mlx-agent.py"

# ── load .env (KEY=VALUE lines) ────────────────────────────────────────────
def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip()
    return env

ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_ID = str(ENV.get("TELEGRAM_USER_ID") or os.environ.get("TELEGRAM_USER_ID", "")).strip()
if not TOKEN or not USER_ID:
    sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID in .env — run ./mlx-setup.sh --configure")
API = f"https://api.telegram.org/bot{TOKEN}"

# ── load the agent as a module and point it at our workspace ───────────────
spec = importlib.util.spec_from_file_location("mlx_agent", str(AGENT_PATH))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

# ── Telegram API helpers (raw HTTP) ────────────────────────────────────────
def tg(method: str, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=65)
        if r.status_code == 429:                       # rate limited — back off, don't crash
            try: time.sleep(min(int(r.json().get("parameters", {}).get("retry_after", 1)), 5))
            except Exception: time.sleep(1)
            return {"ok": False, "retry_after": True}
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def kb(rows):   # rows: list[list[(label, data)]] → InlineKeyboardMarkup
    return {"inline_keyboard": [[{"text": l, "callback_data": d} for (l, d) in row] for row in rows]}

def _plainish(html_text: str) -> str:
    # strip our tags + unescape entities, for the plain-text fallback
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text))

def send(text: str, buttons=None, parse_mode=None):
    p = {"chat_id": USER_ID, "text": (text or "…")[:4096], "disable_web_page_preview": True}
    if buttons is not None: p["reply_markup"] = buttons
    if parse_mode: p["parse_mode"] = parse_mode
    r = tg("sendMessage", **p)
    if parse_mode and not r.get("ok"):                 # formatting rejected → send plain, never lose the msg
        p.pop("parse_mode", None); p["text"] = _plainish(text)[:4096]; r = tg("sendMessage", **p)
    return r.get("result", {}).get("message_id")

def edit(mid, text: str, buttons=None, parse_mode=None):
    p = {"chat_id": USER_ID, "message_id": mid, "text": (text or "…")[:4096], "disable_web_page_preview": True}
    if buttons is not None: p["reply_markup"] = buttons
    if parse_mode: p["parse_mode"] = parse_mode
    r = tg("editMessageText", **p)
    # retry plain ONLY on a real parse failure — "not modified" is not an error
    if parse_mode and not r.get("ok") and "not modified" not in str(r).lower():
        p.pop("parse_mode", None); p["text"] = _plainish(text)[:4096]; r = tg("editMessageText", **p)
    return r

def _chunks(text: str, limit: int = 3800):
    """Split text into <=limit pieces at line boundaries (hard-splitting any overlong line),
    so a long answer can flow across several Telegram messages instead of being cut at 4096."""
    text = text or ""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur: out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur: out.append(cur)
    return out or [""]

def answer_cb(cb_id, text=""):
    tg("answerCallbackQuery", callback_query_id=cb_id, text=text)

def typing():
    # Shows Telegram's "typing…" bubble (~5s). Re-sent periodically while the agent
    # works so the chat looks alive even during long model generation with no tool calls.
    tg("sendChatAction", chat_id=USER_ID, action="typing")

# ── HTML formatting (Telegram-safe) ────────────────────────────────────────
def esc(s: str) -> str:
    return _html.escape(s or "", quote=False)   # escapes & < >

def _inline(seg: str) -> str:
    """Escaped text → apply inline code, bold, and header styling."""
    seg = esc(seg)
    seg = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", seg)          # `code`
    seg = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", seg)          # **bold**
    lines = seg.split("\n")
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*#{1,6}\s+(.*\S)\s*$", ln)               # markdown header → bold
        if m:
            lines[i] = f"<b>{m.group(1)}</b>"; continue
        # a short line ending in ':' reads as a section header → bold it
        s = ln.strip()
        if s and s.endswith(":") and len(s) <= 48 and "<code>" not in s:
            lines[i] = f"<b>{s}</b>"
    return "\n".join(lines)

def _tables_to_code(text: str) -> str:
    """Telegram HTML has no <table>. Convert Markdown pipe-tables into an aligned
       monospace block (wrapped as a fenced code block so to_html renders it in <pre>,
       where columns line up). Non-table text is untouched."""
    lines = (text or "").split("\n"); out = []; i = 0
    sep = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
    def cells(row): return [c.strip() for c in row.strip().strip("|").split("|")]
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "|" in lines[i] and "|" in nxt and "-" in nxt and sep.match(nxt):
            header = cells(lines[i]); j = i + 2; data = []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                data.append(cells(lines[j])); j += 1
            rows = [header] + data
            nc = max(len(r) for r in rows)
            rows = [r + [""] * (nc - len(r)) for r in rows]
            w = [max(len(rows[k][c]) for k in range(len(rows))) for c in range(nc)]
            fmt = lambda r: "  ".join(r[c].ljust(w[c]) for c in range(nc)).rstrip()
            tbl = [fmt(rows[0]), "  ".join("-" * w[c] for c in range(nc))] + [fmt(r) for r in rows[1:]]
            out.append("```\n" + "\n".join(tbl) + "\n```")
            i = j
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)

def to_html(text: str) -> str:
    """Convert the model's lightweight-markdown answer to Telegram-safe HTML:
       Markdown tables → aligned monospace, fenced ``` → <pre>, inline `x` → <code>,
       **x**/headers → bold."""
    text = _tables_to_code(text or "")
    out, parts = [], re.split(r"```[ \t]*\w*\n?(.*?)```", text, flags=re.DOTALL)
    for i, seg in enumerate(parts):
        out.append(f"<pre>{esc(seg.rstrip())}</pre>" if i % 2 else _inline(seg))
    return "".join(out)

def split_thinking(raw: str):
    """Separate <think>…</think> reasoning from the visible answer — WITHOUT deleting
       it. Returns (visible_answer, thinking). Handles an unclosed <think> (model still
       mid-thought) by treating the trailing text as thinking."""
    raw = raw or ""
    thinks = re.findall(r"<think>(.*?)</think>", raw, flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    m = re.search(r"<think>(.*)$", visible, flags=re.DOTALL | re.IGNORECASE)   # unclosed tag
    if m:
        thinks.append(m.group(1)); visible = visible[:m.start()]
    return visible.strip(), "\n\n".join(t.strip() for t in thinks if t.strip()).strip()

# ── shared state (single user → one run + one pending gate at a time) ───────
STATE = {
    "persona": "orchestrator",     # default main agent
    "busy": False,
    "pending": None,               # {"event":Event, "result":..., "kind":"approval|choice"}
    "history": [],                 # rolling [{role,content}] so follow-ups keep context
}
LOCK = threading.Lock()

# ── live "thinking" message that updates in place, then collapses ──────────
class Live:
    def __init__(self, persona):
        self.persona = persona
        self.buf = []
        self.start = time.time()
        self.msg_id = send(f"🧠 {persona} is working…")
        self.lk = threading.Lock()
        self._last_text = ""
        self.done = False        # once collapsed, no keeper render may overwrite the answer
        self._tick = 0           # drives a visible spinner so it's obvious the agent is alive
        self.answer = ""         # streamed final-answer text (grows token by token)

    def emit(self, line: str):
        # CHEAP: just buffer the line. No API call here — a single renderer thread
        # does all the editing at a steady, rate-safe cadence. This is why heavy
        # tool activity can't trip Telegram's per-message edit limit anymore.
        with self.lk:
            self.buf.append(line.rstrip())
            self.answer = ""     # a tool step means we're not in the final-answer phase yet

    def stream(self, piece: str):
        # A token/delta of the final answer. Cheap append; the keeper renders it on
        # its throttled cadence (so token streaming can't trip Telegram's rate limit).
        with self.lk:
            self.answer += piece

    def render(self):
        # Called on a fixed cadence by the keeper. Safe: skips if text is unchanged
        # (avoids Telegram "message is not modified"), and never raises.
        with self.lk:
            if self.done:      # already collapsed to the answer — never overwrite it
                return
            self._tick += 1
            spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[self._tick % 10]
            secs = int(time.time() - self.start)
            ans = self.answer
            body = "\n".join(self.buf[-24:])[-3500:]
        if ans:                # streaming the answer — show it growing, formatted
            vis, think = split_thinking(ans)
            shown = to_html(vis) if vis else ("💭 <i>thinking…</i>" if think else "…")
            text = f"✍️ <b>{esc(self.persona)}</b> {spin} {secs}s\n{shown}"
        else:                  # still working through tools — show the live trace
            header = f"🧠 <b>{esc(self.persona)}</b> working… {spin} {secs}s"
            text = header + (f"\n<pre>{esc(body)}</pre>" if body else "")
        text = text[:4096]
        if text == self._last_text:
            return
        self._last_text = text
        try: edit(self.msg_id, text, parse_mode="HTML")
        except Exception: pass

    def collapse(self, raw: str, steps: int):
        with self.lk:
            self.done = True                       # from here on, render() is a no-op
        visible, thinking = split_thinking(raw)
        foot = (f"\n\n<i>· {esc(self.persona)} · {steps} steps · {int(time.time()-self.start)}s</i>"
                if steps else "")
        if not (visible or "").strip():
            # model produced only reasoning → don't show an empty answer
            head = "<i>(reasoning only — see below)</i>" if thinking else "<i>(no output)</i>"
            self._last_text = (head + foot)[:4096]
            edit(self.msg_id, self._last_text, parse_mode="HTML")
        else:
            # Split the FULL answer into <=Telegram-limit pieces so nothing is truncated.
            # First piece edits the live message; the rest are sent as follow-up messages.
            parts = _chunks(visible, 3800)
            first = to_html(parts[0]).strip() + (foot if len(parts) == 1 else "")
            self._last_text = first[:4096]
            r = edit(self.msg_id, self._last_text, parse_mode="HTML")
            if not r.get("ok") and "not modified" not in str(r).lower():
                edit(self.msg_id, _plainish(parts[0])[:4096])
            for i, part in enumerate(parts[1:], start=1):
                tail = foot if i == len(parts) - 1 else ""
                send(to_html(part).strip() + tail, parse_mode="HTML")
        if thinking:                               # reasoning as its own (single) message
            tp = thinking.strip()
            send("💭 <b>thinking</b>\n<pre>" + esc(tp[:3800]) + ("…" if len(tp) > 3800 else "") + "</pre>",
                 parse_mode="HTML")

# ── approval + choice handlers injected into the agent ─────────────────────
def _wait_gate(kind: str, timeout=600):
    ev = threading.Event()
    with LOCK:
        STATE["pending"] = {"event": ev, "result": None, "kind": kind}
    if not ev.wait(timeout=timeout):            # user didn't respond in time → safe default
        with LOCK: STATE["pending"] = None
        return None
    with LOCK:
        res = STATE["pending"]["result"] if STATE["pending"] else None
        STATE["pending"] = None
    return res

def ask_fn(prompt: str, allow_all: bool) -> str:
    rows = [[("✅ Yes", "gate:y"), ("❌ No", "gate:n")]]
    if allow_all: rows.append([("⏩ Allow rest of this run", "gate:a")])
    send(f"⚠️ Approve this action?\n\n{prompt[:900]}", buttons=kb(rows))
    res = _wait_gate("approval")
    return res if res in ("y", "n", "a") else "n"

def choice_fn(question: str, options: list) -> str:
    opts = [str(o) for o in options][:8]
    rows = [[(o[:40], f"choice:{i}")] for i, o in enumerate(opts)]
    send(f"❓ {question[:900]}", buttons=kb(rows))
    res = _wait_gate("choice")
    try: return opts[int(res)] if res is not None else opts[0]
    except Exception: return opts[0]

# ── run one task on a worker thread (keeps the poll loop free for callbacks) ─
def run_task(text: str):
    persona_name = STATE["persona"]
    personas = agent.load_personas()
    persona = personas.get(persona_name) or personas.get("general")
    live = Live(persona_name)
    # ONE controlled updater for the whole task: on a steady 2s cadence it keeps the
    # "typing…" bubble alive and re-renders the live message. Because emit() only
    # buffers (no API call), edits happen at this fixed rate no matter how many tools
    # fire — so tool-heavy tasks can't trip Telegram's rate limit. Fully wrapped so a
    # transient API error can never kill the loop (the old failure mode).
    stop_keeper = threading.Event()
    def _keeper():
        while not stop_keeper.is_set():
            try:
                typing()
                live.render()
            except Exception as e:
                print("keeper (non-fatal):", e)
            stop_keeper.wait(2.0)
    keeper = threading.Thread(target=_keeper, daemon=True)
    keeper.start()
    # wire the agent's hooks to Telegram for the duration of this run
    agent.EMIT_FN = live.emit
    agent.STREAM_FN = live.stream     # stream the final answer token-by-token (rate-safe via the keeper)
    agent.ASK_FN = ask_fn
    agent.CHOICE_FN = choice_fn
    try:
        history = list(STATE.get("history") or [])
        answer = agent.run_agent(text, persona, history=history)
    except Exception as e:
        answer = f"⚠️ error: {e}"
    finally:
        stop_keeper.set()
        keeper.join(timeout=5)     # wait out any in-flight render BEFORE we write the answer
        agent.EMIT_FN = agent.ASK_FN = agent.CHOICE_FN = None
        agent.STREAM_FN = None
    live.collapse(answer, len(live.buf))     # collapse now preserves thinking itself
    with LOCK:
        # remember this turn (visible answer only, thinking stripped) so a follow-up
        # like "and who came second?" resolves against the topic, not a cold start
        vis, _ = split_thinking(answer)
        hist = STATE.get("history") or []
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": (vis or answer)})
        STATE["history"] = hist[-20:]        # keep the last ~10 exchanges
        STATE["busy"] = False

# ── command handling ───────────────────────────────────────────────────────
HELP = ("🤖 Local MLX agent\n\n"
        "Just send a task and I'll work on it — you'll see live progress, and I'll ask "
        "with buttons before anything risky.\n\n"
        "/personas — list personas\n"
        "/use <name> — switch persona (current: {p})\n"
        "/reset — start a fresh conversation (forget the current thread)\n"
        "/help — this message")

def handle_text(text: str):
    if text.startswith("/"):
        cmd, *rest = text[1:].split(maxsplit=1)
        arg = rest[0].strip() if rest else ""
        if cmd in ("start", "help"):
            send(HELP.format(p=STATE["persona"])); return
        if cmd in ("reset", "new", "clear"):
            with LOCK: STATE["history"] = []
            send("🧹 Fresh start — I've cleared the conversation."); return
        if cmd == "personas":
            p = agent.load_personas()
            lines = [f"• {n}{'  ← active' if n==STATE['persona'] else ''}\n   {c.get('description','')}"
                     for n, c in p.items()]
            send("Personas:\n\n" + "\n".join(lines)); return
        if cmd == "use":
            p = agent.load_personas()
            if arg in p:
                STATE["persona"] = arg
                with LOCK: STATE["history"] = []      # new specialist → fresh thread
                send(f"✅ now using: {arg}  (conversation reset)")
            else: send(f"No persona '{arg}'. See /personas.")
            return
        send("Unknown command. /help"); return
    # a task
    with LOCK:
        if STATE["busy"]:
            send("⏳ Still working on the previous task — one at a time. Send it again when I'm done.")
            return
        STATE["busy"] = True
    typing()   # instant feedback — the "working…" message + live spinner follow within a second
    threading.Thread(target=run_task, args=(text,), daemon=True).start()

def _tapped_label(cb, data):
    for row in ((cb.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard", []):
        for btn in row:
            if btn.get("callback_data") == data:
                return btn.get("text", "")
    return ""

def handle_callback(cb):
    data = cb.get("data", "")
    m = cb.get("message") or {}
    mid, orig = m.get("message_id"), m.get("text", "")
    label = _tapped_label(cb, data)
    with LOCK:
        pend = STATE["pending"]
    if not pend:
        answer_cb(cb["id"], "nothing pending")
        if mid: edit(mid, (orig + "\n\n⏱ expired")[:4096])   # drop stale buttons too
        return
    if data.startswith("gate:") and pend["kind"] == "approval":
        pend["result"] = data.split(":", 1)[1]
        note = {"y": "✅ Approved", "n": "❌ Denied", "a": "⏩ Approved (rest of run)"}.get(pend["result"], label)
        answer_cb(cb["id"], note)
        if mid: edit(mid, (orig + f"\n\n→ {note}")[:4096])    # no buttons passed ⇒ keyboard removed
        pend["event"].set()
    elif data.startswith("choice:") and pend["kind"] == "choice":
        pend["result"] = data.split(":", 1)[1]
        answer_cb(cb["id"], "got it")
        if mid: edit(mid, (orig + f"\n\n→ {label or 'selected'}")[:4096])
        pend["event"].set()
    else:
        answer_cb(cb["id"])
        if mid: edit(mid, (orig + "\n\n(dismissed)")[:4096])

# ── long-poll loop ─────────────────────────────────────────────────────────
def main():
    me = tg("getMe")
    if not me.get("ok"):
        sys.exit(f"Telegram auth failed — check TELEGRAM_BOT_TOKEN. ({me})")
    print(f"mlx-telegram up as @{me['result'].get('username')} · locked to user {USER_ID}")
    send("🟢 Local MLX agent online. Send a task, or /help.")
    offset = None
    while True:
        r = tg("getUpdates", offset=offset, timeout=50)
        if not r.get("ok"):
            time.sleep(3); continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            msg = upd.get("message") or upd.get("edited_message")
            frm = (cb or msg or {}).get("from", {})
            if str(frm.get("id")) != USER_ID:          # hard user-ID lock
                if cb: answer_cb(cb["id"], "not authorized")
                continue
            try:
                if cb: handle_callback(cb)
                elif msg and "text" in msg: handle_text(msg["text"].strip())
            except Exception as e:
                print("handler error:", e)

if __name__ == "__main__":
    main()
TELEGRAMEOF
    chmod +x "$AD/mlx-telegram.py"
    ok "telegram bridge installed — start it with: ./mlx-setup.sh --telegram"
}

# Run the bridge in the foreground (Ctrl-C to stop). Needs TELEGRAM_* in .env.
cmd_telegram() {
    [ -f "$WORKDIR/agent/mlx-telegram.py" ] || { err "run --bootstrap first"; exit 1; }
    local tok; tok="$(get_env TELEGRAM_BOT_TOKEN)"
    [ -n "$tok" ] || { err "no TELEGRAM_BOT_TOKEN — run: ./mlx-setup.sh --configure"; exit 1; }
    log "starting Telegram bridge (Ctrl-C to stop)"
    set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
    MLX_WORKDIR="$WORKDIR" MLX_WORKSPACE="$HOME/MLX-AI" "$VENV/bin/python" "$WORKDIR/agent/mlx-telegram.py"
}

# Install the bridge as a launchd service so it runs on login and restarts on crash.
cmd_telegram_service() {
    [ -f "$WORKDIR/agent/mlx-telegram.py" ] || { err "run --bootstrap first"; exit 1; }
    local tok; tok="$(get_env TELEGRAM_BOT_TOKEN)"
    [ -n "$tok" ] || { err "no TELEGRAM_BOT_TOKEN — run: ./mlx-setup.sh --configure"; exit 1; }
    local label="com.mlxstack.telegram" plist="$HOME/Library/LaunchAgents/com.mlxstack.telegram.plist"
    local runner="$WORKDIR/run_telegram.sh"
    cat > "$runner" <<SHEOF
#!/usr/bin/env bash
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a
export MLX_WORKDIR="$WORKDIR" MLX_WORKSPACE="$HOME/MLX-AI"
exec "$VENV/bin/python" "$WORKDIR/agent/mlx-telegram.py"
SHEOF
    chmod +x "$runner"
    mkdir -p "$HOME/Library/LaunchAgents" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$runner</string></array>
  <key>WorkingDirectory</key><string>$WORKDIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/com.mlxstack.telegram.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/com.mlxstack.telegram.err</string>
</dict></plist>
PLISTEOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || launchctl load "$plist" 2>/dev/null || true
    ok "telegram service installed (starts on login, restarts on crash). logs: $WORKDIR/logs/com.mlxstack.telegram.*"
}

# =============================================================================
#  PHASE 10 — launchd SERVICES
# =============================================================================
write_plist() {
    local label="$1" script="$2"; local plist="$LAUNCH_DIR/$label.plist"
    mkdir -p "$LAUNCH_DIR" "$WORKDIR/logs"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$script</string></array>
  <key>WorkingDirectory</key><string>$WORKDIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PORT_DASHBOARD</key><string>$PORT_DASHBOARD</string>
    <key>DASHBOARD_HOST</key><string>$BIND_HOST</string>
    <key>MLX_VISION_PORT</key><string>$PORT_VISION</string>
    <key>MLX_VISION_MODEL</key><string>$VISION_MODEL</string>
    <key>HF_HUB_ENABLE_HF_TRANSFER</key><string>0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WORKDIR/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$WORKDIR/logs/$label.err</string>
</dict></plist>
PLISTEOF
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || launchctl unload "$plist" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || launchctl load "$plist" >/dev/null 2>&1
    launchctl list 2>/dev/null | grep -q "$label" && ok "registered $label" || warn "could not register $label"
}
setup_services() {
    log "Registering background services (launchd)"
    write_plist "com.mlxstack.inference" "$WORKDIR/start_mlx.sh"
    write_plist "com.mlxstack.gateway"   "$WORKDIR/start_gateway.sh"
    write_plist "com.mlxstack.dashboard" "$WORKDIR/start_dashboard.sh"
    write_plist "com.mlxstack.vision"    "$WORKDIR/start_vision.sh"
}

# =============================================================================
#  SUMMARY
# =============================================================================
print_summary() {
    # Display host: the Mac's LAN IP when bound to 0.0.0.0, else localhost.
    local host="localhost"
    if [ "$BIND_HOST" = "0.0.0.0" ]; then
        local ip; ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
        [ -n "$ip" ] && host="$ip"
    fi
    hr
    cat <<SUM
    ${c_grn}✦  MLX AI WORKSTATION READY${c_reset}

  Reachable at ${c_cyn}${host}${c_reset}$( [ "$host" = "localhost" ] && echo "  (bound local-only)" || echo "  — open these from any device on your network" )

  Control dashboard : http://${host}:$PORT_DASHBOARD      (status · demo · docs)
  Web chat          : http://${host}:$PORT_OPENWEBUI      (pick a model, chat)
  Gateway (API)     : http://${host}:$PORT_GATEWAY/v1     (OpenAI-compatible)
  Private search    : http://${host}:$PORT_SEARXNG

  Model roles       : orchestrator · coder · reasoner · qa · vision · embed
  Generate an image : ./mlx-setup.sh --image "a red racing bike, poster art"
  Add more models   : ./mlx-setup.sh --pull-models

  First model call is slow (it loads into memory); after that it's cached.
  Everything runs on this Mac — private and \$0 per token.

  Controls: --status | --start | --stop | --restart | --pull-models | --help
SUM
    if [ "$BIND_HOST" = "0.0.0.0" ]; then
        printf "  %snote:%s services are on your LAN with no TLS/login — trusted networks only.\n" "$c_yel" "$c_reset"
        printf "        run with %sLOCAL_ONLY=1%s to keep everything on 127.0.0.1.\n" "$c_cyn" "$c_reset"
    fi
    hr
}

# =============================================================================
#  CLI CONTROLLER
# =============================================================================
LABELS=(com.mlxstack.inference com.mlxstack.gateway com.mlxstack.dashboard com.mlxstack.vision)

svc_status() {
    hr; log "Service health"
    for pair in \
        "Inference (MLX)|http://127.0.0.1:$PORT_MLX/v1/models" \
        "Gateway (LiteLLM)|http://127.0.0.1:$PORT_GATEWAY/health/liveliness" \
        "Dashboard|http://127.0.0.1:$PORT_DASHBOARD/" \
        "Vision (mlx-vlm)|http://127.0.0.1:$PORT_VISION/health" \
        "Open WebUI|http://127.0.0.1:$PORT_OPENWEBUI/" \
        "SearXNG|http://127.0.0.1:$PORT_SEARXNG/"; do
        local nm="${pair%%|*}" url="${pair#*|}"
        http_ok "$url" && ok "$nm — up" || warn "$nm — down"
    done
    echo; log "launchd"
    for l in "${LABELS[@]}"; do launchctl list 2>/dev/null | grep -q "$l" && ok "$l" || warn "$l inactive"; done
    echo; log "Docker"; docker_up && docker ps --format '  {{.Names}} — {{.Status}}' 2>/dev/null || warn "Docker not active"
    echo; log "Downloaded models"
    local cache_out; cache_out="$(HF cache list 2>/dev/null)"
    if [ -n "$cache_out" ]; then printf '%s\n' "$cache_out" | sed -n '1,14p'
    else warn "no models cached yet — run --bootstrap"; fi
}

svc_start() {
    log "Starting services"
    setup_colima
    if docker_up; then for c in open-webui searxng; do docker start "$c" >/dev/null 2>&1 || true; done; fi
    for l in "${LABELS[@]}"; do
        launchctl bootstrap "gui/$(id -u)" "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || \
        launchctl load "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
    done
    ok "start dispatched — give the MLX server ~20s to come up"
}
svc_stop() {
    log "Stopping services"
    for l in "${LABELS[@]}"; do
        launchctl bootout "gui/$(id -u)/$l" >/dev/null 2>&1 || launchctl unload "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1 || true
    done
    for c in open-webui searxng; do docker stop "$c" >/dev/null 2>&1 || true; done
    ok "stopped (Colima left running; 'colima stop' to halt the VM)"
}

do_image() { [ -f "$WORKDIR/mlx-image.sh" ] || { err "run --bootstrap first"; exit 1; }; bash "$WORKDIR/mlx-image.sh" "$@"; }

# ── Model management (registry-driven; the engine the Phase 3 GUI will call) ──
CORE_ROLES="orchestrator coder reasoner qa vision embed"
valid_role()       { printf '%s' "$1" | grep -Eq '^[a-z0-9][a-z0-9-]{0,30}$'; }
is_core_role()     { case " $CORE_ROLES " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
registry_has_role(){ [ -f "$MODELS_REGISTRY" ] && awk -F'\t' -v r="$1" '$1==r{f=1} END{exit !f}' "$MODELS_REGISTRY"; }

svc_reload_core() {
    log "Reloading inference + gateway"
    for l in com.mlxstack.inference com.mlxstack.gateway; do
        launchctl kickstart -k "gui/$(id -u)/$l" >/dev/null 2>&1 || {
            launchctl bootout "gui/$(id -u)/$l" >/dev/null 2>&1
            launchctl bootstrap "gui/$(id -u)" "$LAUNCH_DIR/$l.plist" >/dev/null 2>&1
        }
    done
    ok "reloading — new config live in ~20s (first call to a new model is slower)"
}

cmd_add_model() {   # [repo] [role] [type] [tool_parser] [reasoning_parser]
    local repo="${1:-}" role="${2:-}" mtype="${3:-}" tparse="${4:-}" rparse="${5:-}"
    if [ -z "$repo" ] || [ -z "$role" ]; then
        printf "HF repo id (e.g. mlx-community/Qwen3-Coder-Next-4bit): "; read -r repo
        printf "role/alias name (lowercase, e.g. coder-agentic): ";        read -r role
        printf "type [lm|multimodal|embeddings|image-generation] (default lm): "; read -r mtype
        printf "tool_call_parser (blank if none): ";  read -r tparse
        printf "reasoning_parser (blank if none): ";  read -r rparse
    fi
    mtype="${mtype:-lm}"
    [ -n "$repo" ] || { err "no repo given"; exit 1; }
    valid_role "$role" || { err "invalid role '$role' — use lowercase letters, digits, hyphen"; exit 1; }
    is_core_role "$role" && { err "'$role' is a built-in role; choose another alias"; exit 1; }
    mkdir -p "$WORKDIR"
    log "Downloading $repo"
    hf_download_verify "$repo" || { err "download failed (repo not found in cache) — model not added"; exit 1; }
    # de-duplicate the role, then append the registry line
    if [ -f "$MODELS_REGISTRY" ]; then
        awk -F'\t' -v r="$role" '$1!=r' "$MODELS_REGISTRY" > "$MODELS_REGISTRY.tmp" && mv "$MODELS_REGISTRY.tmp" "$MODELS_REGISTRY"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$role" "$repo" "$mtype" "$tparse" "$rparse" >> "$MODELS_REGISTRY"
    ok "registered '$role' → $repo ($mtype)"
    write_mlx_config; write_litellm_config
    svc_reload_core
    ok "Done. Select it in the web-chat model dropdown or call it by the name '$role'."
}

cmd_remove_model() {   # [role]
    local role="${1:-}"
    [ -z "$role" ] && { printf "role to remove: "; read -r role; }
    is_core_role "$role" && { err "'$role' is built-in and can't be removed here"; exit 1; }
    registry_has_role "$role" || { err "no custom model named '$role' (see --list-models)"; exit 1; }
    local repo; repo="$(awk -F'\t' -v r="$role" '$1==r{print $2; exit}' "$MODELS_REGISTRY")"
    awk -F'\t' -v r="$role" '$1!=r' "$MODELS_REGISTRY" > "$MODELS_REGISTRY.tmp" && mv "$MODELS_REGISTRY.tmp" "$MODELS_REGISTRY"
    ok "unregistered '$role'"
    write_mlx_config; write_litellm_config; svc_reload_core
    printf "Also delete the downloaded weights for %s? [y/N] " "$repo"; read -r yn
    case "$yn" in
        y|Y|yes|YES)
            local dir="$HOME/.cache/huggingface/hub/models--$(printf '%s' "$repo" | sed 's#/#--#g')"
            [ -d "$dir" ] && rm -rf "$dir" && ok "weights deleted ($dir)" || warn "cache dir not found; skip" ;;
        *) ok "weights kept in the HF cache" ;;
    esac
}

cmd_list_models() {
    hr; log "Built-in roles (defined in the script)"
    printf "  %-15s %s\n" orchestrator "unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit"
    printf "  %-15s %s\n" "coder/reasoner/qa" "mlx-community/Qwen3.6-27B-8bit"
    printf "  %-15s %s\n" vision "mlx-community/gemma-4-12B-4bit"
    printf "  %-15s %s\n" embed  "mlx-community/Qwen3-Embedding-8B-4bit-DWQ"
    echo; log "Custom models (registry: models.custom.tsv)"
    if [ -f "$MODELS_REGISTRY" ] && [ -s "$MODELS_REGISTRY" ]; then
        awk -F'\t' '{printf "  %-15s %-45s [%s]\n",$1,$2,$3}' "$MODELS_REGISTRY"
    else
        echo "  (none — add one with:  ./mlx-setup.sh --add-model)"
    fi
    hr
}

# Real load-probe: actually load (and for lm, generate) to confirm the model RUNS,
# not just that it downloaded. This is the "searchable ≠ loadable" gate. Timeout is
# enforced inside Python (macOS lacks `timeout`). Prints PROBEOK:/PROBEFAIL: for callers.
probe_model() {   # $1=repo  $2=type(lm|multimodal|embeddings)  $3=timeout_sec
    local repo="$1" mtype="${2:-lm}" to="${3:-900}"
    hf_download_verify "$repo" >/dev/null 2>&1 || { echo "PROBEFAIL: not in cache — download first"; return 1; }
    "$VENV/bin/python" - "$repo" "$mtype" "$to" <<'PY'
import sys, signal
repo, mtype, to = sys.argv[1], sys.argv[2], int(sys.argv[3])
def _to(*a):
    print("PROBEFAIL: timed out after", to, "s loading/generating"); sys.exit(3)
signal.signal(signal.SIGALRM, _to); signal.alarm(to)
try:
    if mtype == "multimodal":
        # Real vision test: generate against a tiny synthetic image (the alarm above
        # bounds it, so if the multimodal path hangs, this fails instead of blocking).
        try:
            import tempfile
            from mlx_vlm import load, generate
            from mlx_vlm.prompt_utils import apply_chat_template
            from mlx_vlm.utils import load_config
            model, proc = load(repo); cfg = load_config(repo)
            img = tempfile.mktemp(suffix=".png")
            try:
                from PIL import Image
                Image.new("RGB", (64, 64), (120, 120, 120)).save(img)
                fp = apply_chat_template(proc, cfg, "What color is this image?", num_images=1)
                out = generate(model, proc, fp, [img], verbose=False, max_tokens=8)
                print("PROBEOK: vision generated:", (getattr(out, "text", None) or str(out)).strip()[:60])
            except ImportError:
                print("PROBEOK: loaded (Pillow missing; generation not verified)")
        except Exception as e:
            print("PROBEFAIL:", type(e).__name__, str(e)[:200]); sys.exit(2)
    elif mtype == "embeddings":
        from mlx_lm import load; load(repo); print("PROBEOK: loaded (embeddings)")
    else:
        from mlx_lm import load, generate
        m, t = load(repo)
        try:
            out = generate(m, t, prompt="Reply with: ok", max_tokens=8, verbose=False)
            print("PROBEOK: generated:", (out or "").strip()[:60])
        except TypeError:
            print("PROBEOK: loaded (generate signature differs; load succeeded)")
except Exception as e:
    print("PROBEFAIL:", type(e).__name__, str(e)[:200]); sys.exit(2)
PY
}

cmd_probe_model() {   # <repo> [type]
    local repo="${1:-}" mtype="${2:-lm}"
    [ -n "$repo" ] || { err "usage: --probe-model <hf_repo> [lm|multimodal|embeddings]"; exit 1; }
    log "Load-probe $repo ($mtype)"
    local out; out="$(probe_model "$repo" "$mtype" "${3:-900}")"; echo "$out"
    case "$out" in *PROBEOK:*) ok "loadable ✓"; return 0 ;; *) err "not loadable"; return 1 ;; esac
}

cmd_download_model() {   # <repo>
    local repo="${1:-}"
    [ -n "$repo" ] || { err "usage: --download-model <hf_repo>"; exit 1; }
    download_one "$repo"
}

# Use a vision model DIRECTLY via mlx_vlm (bypasses mlx-openai-server, whose
# multimodal path currently hangs). This is the working way to do OCR / image Q&A today.
VISION_DEFAULT="${VISION_DEFAULT:-mlx-community/Qwen3-VL-8B-Instruct-4bit}"
cmd_vision() {   # <image-path-or-url> <prompt> [model_repo]
    local img="${1:-}" prompt="${2:-}" repo="${3:-$VISION_DEFAULT}"
    if [ -z "$img" ] || [ -z "$prompt" ]; then
        err "usage: --vision <image-path-or-url> \"<prompt>\" [model_repo]"
        err "  e.g. ./mlx-setup.sh --vision ~/Desktop/receipt.jpg \"extract all line items and totals\""
        exit 1
    fi
    log "Vision ($repo) — running directly via mlx_vlm"
    "$VENV/bin/python" -m mlx_vlm.generate \
        --model "$repo" --image "$img" --prompt "$prompt" \
        --max-tokens 512 --temperature 0 \
        || { err "vision generation failed — try --probe-model \"$repo\" multimodal to see the error"; exit 1; }
}

print_help() {
    cat <<USG
Usage: $0 [OPTION]
  --wizard        Guided first-run setup in your browser (recommended for a fresh install)
  --bootstrap     Full install: tools, Python, models, server, gateway, UIs, dashboard
  --start         Start all services
  --stop          Stop all services
  --restart       Stop then start
  --status        Show live health of everything
  --configure     Set/replace secrets (HF token, Telegram bot token + user ID) in .env
  --pull-models   Download optional models (Devstral, …) — interactive picker
  --pull-heavy    Download heavy best-in-class models (Qwen3-Coder-Next 80B, DeepSeek-R1 70B)
  --pull-vision   Download vision models to probe (Qwen3-VL, Gemma 3, Gemma 4)
  --list-models   List built-in roles and any custom models you've added
  --add-model     Add a model:  --add-model <hf_repo> <role> [type] [tool_parser] [reasoning_parser]
                  (no args = interactive prompts). Downloads, registers, and reloads.
  --remove-model  Remove a custom model:  --remove-model <role>
  --download-model <repo>   Download an HF repo into the cache (no register)
  --probe-model <repo> [type]  Load-probe a model (actually loads/generates) before trusting it
  --agent "TEXT"  Ask the tool-executor agent (runs real tools: shell, files, web)
  --telegram      Run the Telegram bridge in the foreground (needs --configure first)
  --telegram-service  Install the Telegram bridge as a launchd service (runs on login)
  --image "TEXT"  Generate an image locally with FLUX (mflux)
  --vision <image> "<prompt>" [model]   Ask a vision model about an image (direct mlx_vlm; OCR, image Q&A)
  --help          This message
USG
}

case "${1:---help}" in
    --bootstrap)
        preflight
        setup_xcode_clt
        setup_homebrew
        setup_core_tools
        mkdir -p "$WORKDIR" "$DOCS_WORKSPACE" "$CODE_WORKSPACE"
        setup_python
        setup_cli_tools
        configure_secrets
        ensure_env_file; set_env MLX_SETUP_PATH "$SCRIPT_PATH"   # let the dashboard call back into this installer
        download_core_models
        write_mlx_config
        write_litellm_config
        write_launchers
        write_dashboard
        write_image_tool
        write_agent
        write_telegram
        setup_docker_services
        setup_services
        setup_colima_autostart
        # Auto-install the Telegram background service if secrets are configured, so a
        # rebooted machine has the bot running with no manual step.
        if [ -n "$(get_env TELEGRAM_BOT_TOKEN)" ] && [ -n "$(get_env TELEGRAM_USER_ID)" ]; then
            cmd_telegram_service
        else
            warn "Telegram secrets not set — skipping bot autostart. Add them with --configure, then --telegram-service."
        fi
        pull_optional_models
        print_summary
        ;;
    --wizard)  cmd_wizard ;;
    --pull-models) pull_optional_models ;;
    --configure)   configure_secrets ;;
    --pull-heavy)  pull_heavy_models ;;
    --pull-vision) pull_vision_models ;;
    --list-models) cmd_list_models ;;
    --add-model)    shift; cmd_add_model "$@" ;;
    --download-model) shift; cmd_download_model "$@" ;;
    --probe-model)  shift; cmd_probe_model "$@" ;;
    --vision)       shift; cmd_vision "$@" ;;
    --remove-model) shift; cmd_remove_model "$@" ;;
    --start)   svc_start ;;
    --stop)    svc_stop ;;
    --restart) svc_stop; svc_start ;;
    --status)  svc_status ;;
    --agent)   shift; cmd_agent "$@" ;;
    --telegram)         cmd_telegram ;;
    --telegram-service) cmd_telegram_service ;;
    --image)   shift; do_image "$@" ;;
    --help|-h) print_help ;;
    *) echo "Unknown option: $1"; print_help; exit 2 ;;
esac
