#!/usr/bin/env bash
# =============================================================================
#  pre-mlx-cleanup.sh — Fresh-slate teardown for the MLX migration
#
#  Reverses the ORIGINAL Ollama/OpenClaw workstation (script3.sh):
#    - launchd services      (com.aiws.*)
#    - Docker containers      (open-webui, searxng, portainer, langfuse) + volumes
#    - Colima VM
#    - OpenClaw + Peekaboo
#    - Ollama app/service AND every downloaded Ollama model
#    - (optional) Hugging Face model cache
#    - (optional) Homebrew packages the original installed
#    - (optional) IDEs (VS Code, IntelliJ)
#    - (optional) generated workspace folders
#    - .zprofile lines the original appended (with a backup)
#
#  Safe by design: idempotent, skips what isn't present, and prompts before
#  every destructive or shared-tool action. Nothing is removed silently.
#
#  Usage:
#    chmod +x pre-mlx-cleanup.sh
#    ./pre-mlx-cleanup.sh              # interactive
#    ./pre-mlx-cleanup.sh --yes-all    # accept every optional removal (careful!)
# =============================================================================
set -uo pipefail
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi

# ─────────────────────────── config: what we target ───────────────────────────
WORKDIR="${HOME}/.local-ai-workstation"          # original script's working dir
LAUNCH_DIR="${HOME}/Library/LaunchAgents"
DOCS_WORKSPACE="${HOME}/OneDrive/AI-Agent"
CODE_WORKSPACE="${HOME}/SourceCode"

LAUNCHD_LABELS=(com.aiws.litellm com.aiws.dashboard com.aiws.orchestrator com.aiws.vox)
DOCKER_CONTAINERS=(open-webui searxng portainer langfuse)
DOCKER_VOLUMES=(open-webui portainer_data)

# Homebrew packages the original script installed (formulae then casks).
BREW_WORKSTATION_FORMULAE=(colima docker docker-compose socat lazydocker cairo pango gdk-pixbuf libffi)
BREW_SHARED_FORMULAE=(node git jq wget uv)          # commonly used elsewhere — off by default
BREW_CASKS=(visual-studio-code intellij-idea intellij-idea-ce)

AUTO_YES=0
[ "${1:-}" = "--yes-all" ] && AUTO_YES=1

# ───────────────────────────────── logging ────────────────────────────────────
c_reset=$'\033[0m'; c_blue=$'\033[1;34m'; c_grn=$'\033[1;32m'
c_yel=$'\033[1;33m'; c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'
log()  { printf "\n%s==>%s %s\n" "$c_blue" "$c_reset" "$*"; }
ok()   { printf "%s  ok%s  %s\n" "$c_grn" "$c_reset" "$*"; }
warn() { printf "%s   !%s  %s\n" "$c_yel" "$c_reset" "$*"; }
err()  { printf "%s   x%s  %s\n" "$c_red" "$c_reset" "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
hr()   { printf "%s%s%s\n" "$c_cyn" "────────────────────────────────────────────────────" "$c_reset"; }

# ask "question" -> returns 0 for yes. Honors --yes-all.
ask() {
    [ "$AUTO_YES" = "1" ] && { echo "  (auto-yes) $1"; return 0; }
    printf "%s? %s%s [y/N] " "$c_yel" "$1" "$c_reset"
    read -r reply
    case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

dc() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
docker_up() { have docker && docker info >/dev/null 2>&1; }

# ─────────────────────────────── preflight ────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || { err "macOS only."; exit 1; }

cat <<BANNER
${c_red}
  ╭──────────────────────────────────────────────────────────╮
  │   PRE-MLX CLEANUP — this removes the old local-AI stack    │
  ╰──────────────────────────────────────────────────────────╯
${c_reset}
This will tear down the ORIGINAL Ollama/OpenClaw workstation so the MLX
setup starts from a clean slate. Destructive steps are confirmed one by one.
BANNER
ask "Continue with cleanup" || { echo "Aborted."; exit 0; }

# =============================================================================
#  1) launchd background services
# =============================================================================
log "Stopping launchd services"
for label in "${LAUNCHD_LABELS[@]}"; do
    plist="$LAUNCH_DIR/$label.plist"
    if launchctl list 2>/dev/null | grep -q "$label" || [ -f "$plist" ]; then
        launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || \
            launchctl unload "$plist" >/dev/null 2>&1 || true
        rm -f "$plist" && ok "removed $label" || warn "could not remove $plist"
    else
        ok "$label not present"
    fi
done

# =============================================================================
#  2) OpenClaw agent runtime
# =============================================================================
log "OpenClaw agent runtime"
if have openclaw; then
    openclaw uninstall --daemon >/dev/null 2>&1 || true
    if have npm; then npm uninstall -g openclaw >/dev/null 2>&1 || true; fi
    ok "OpenClaw removed"
else
    ok "OpenClaw not present"
fi
# OpenClaw state/config
for d in "$HOME/.openclaw" "$HOME/.config/openclaw"; do
    [ -d "$d" ] && { rm -rf "$d" && ok "removed $d"; }
done

# =============================================================================
#  3) Docker containers, volumes, Langfuse compose, Colima
# =============================================================================
log "Docker services"
if docker_up; then
    for c in "${DOCKER_CONTAINERS[@]}"; do
        if docker inspect "$c" >/dev/null 2>&1; then
            docker rm -f "$c" >/dev/null 2>&1 && ok "removed container $c"
        fi
    done
    if [ -d "$WORKDIR/langfuse/.git" ]; then
        ( cd "$WORKDIR/langfuse" && dc down -v >/dev/null 2>&1 ) && ok "langfuse compose down"
    fi
    for v in "${DOCKER_VOLUMES[@]}"; do
        docker volume inspect "$v" >/dev/null 2>&1 && \
            docker volume rm "$v" >/dev/null 2>&1 && ok "removed volume $v"
    done
else
    warn "Docker daemon not running — skipping container/volume removal"
fi

log "Colima VM"
if have colima; then
    colima stop >/dev/null 2>&1 || true
    if ask "Delete the Colima VM entirely (frees disk, removes all its images)"; then
        colima delete -f >/dev/null 2>&1 && ok "Colima VM deleted" || warn "colima delete failed"
    else
        ok "Colima stopped (VM kept)"
    fi
else
    ok "Colima not present"
fi

# =============================================================================
#  4) Ollama + ALL downloaded Ollama models
# =============================================================================
log "Ollama runtime + models"
# stop the service/app first
if [ -d "/Applications/Ollama.app" ]; then
    osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || pkill -x Ollama 2>/dev/null || true
fi
if have brew && brew services list 2>/dev/null | grep -q '^ollama'; then
    brew services stop ollama >/dev/null 2>&1 || true
fi
if [ -d "$HOME/.ollama" ]; then
    size=$(du -sh "$HOME/.ollama" 2>/dev/null | awk '{print $1}')
    if ask "Delete ALL Ollama models and data at ~/.ollama (${size:-unknown})"; then
        rm -rf "$HOME/.ollama" && ok "Ollama models + data deleted"
    else
        warn "Kept ~/.ollama"
    fi
else
    ok "No ~/.ollama directory"
fi
# uninstall the Ollama binary/app itself
if [ -d "/Applications/Ollama.app" ]; then
    if ask "Remove the Ollama.app application"; then
        rm -rf "/Applications/Ollama.app" && ok "Ollama.app removed"
    fi
elif have brew && brew list ollama >/dev/null 2>&1; then
    if ask "Uninstall Ollama (Homebrew)"; then
        brew uninstall ollama >/dev/null 2>&1 && ok "Ollama uninstalled"
    fi
fi

# =============================================================================
#  5) Hugging Face model cache (may already hold GGUF/MLX weights)
# =============================================================================
log "Hugging Face model cache"
HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}"
if [ -d "$HF_CACHE" ]; then
    size=$(du -sh "$HF_CACHE" 2>/dev/null | awk '{print $1}')
    warn "Found $HF_CACHE (${size:-unknown}) — this holds any already-downloaded models."
    if ask "Delete the entire Hugging Face cache to start fresh"; then
        rm -rf "$HF_CACHE" && ok "HF cache deleted"
    else
        ok "Kept HF cache (MLX setup will reuse anything valid)"
    fi
else
    ok "No Hugging Face cache present"
fi

# =============================================================================
#  6) Peekaboo GUI-automation cask
# =============================================================================
log "Peekaboo"
if [ -d "/Applications/Peekaboo.app" ] || (have brew && brew list --cask peekaboo >/dev/null 2>&1); then
    if ask "Uninstall Peekaboo"; then
        brew uninstall --cask peekaboo >/dev/null 2>&1 || rm -rf "/Applications/Peekaboo.app" 2>/dev/null || true
        ok "Peekaboo removed"
    fi
else
    ok "Peekaboo not present"
fi

# =============================================================================
#  7) Puppeteer / Chrome-for-Testing (npm global, installed by original)
# =============================================================================
log "Puppeteer (npm global)"
if have npm && npm ls -g puppeteer >/dev/null 2>&1; then
    if ask "Remove global puppeteer + its downloaded Chrome"; then
        npm uninstall -g puppeteer >/dev/null 2>&1 || true
        rm -rf "$HOME/.cache/puppeteer" 2>/dev/null || true
        ok "puppeteer removed"
    fi
else
    ok "puppeteer not present"
fi

# =============================================================================
#  8) Working directory, logs, lock files
# =============================================================================
log "Workstation working directory"
if [ -d "$WORKDIR" ]; then
    rm -rf "$WORKDIR" && ok "removed $WORKDIR"
else
    ok "$WORKDIR not present"
fi
rm -f /tmp/model_*.lock 2>/dev/null || true

# =============================================================================
#  9) Generated workspace folders (potentially your real work — extra caution)
# =============================================================================
log "Generated workspace folders"
for d in "$DOCS_WORKSPACE" "$CODE_WORKSPACE"; do
    if [ -d "$d" ]; then
        warn "$d exists and MAY contain real work."
        if ask "Delete $d (only if it holds nothing you need)"; then
            rm -rf "$d" && ok "removed $d"
        else
            ok "Kept $d"
        fi
    fi
done

# =============================================================================
# 10) Homebrew packages installed by the original script
# =============================================================================
log "Homebrew packages"
if have brew; then
    echo "  The MLX setup will REINSTALL whatever it needs (uv, node, colima, docker),"
    echo "  so removing these now is optional and mainly for a truly clean machine."
    if ask "Uninstall the workstation-only formulae (${BREW_WORKSTATION_FORMULAE[*]})"; then
        for p in "${BREW_WORKSTATION_FORMULAE[@]}"; do
            brew list "$p" >/dev/null 2>&1 && brew uninstall --ignore-dependencies "$p" >/dev/null 2>&1 \
                && ok "uninstalled $p" || true
        done
    fi
    if ask "Also uninstall SHARED dev tools (${BREW_SHARED_FORMULAE[*]}) — skip if you use them elsewhere"; then
        for p in "${BREW_SHARED_FORMULAE[@]}"; do
            brew list "$p" >/dev/null 2>&1 && brew uninstall --ignore-dependencies "$p" >/dev/null 2>&1 \
                && ok "uninstalled $p" || true
        done
    fi
    if ask "Uninstall the IDEs the original installed (VS Code, IntelliJ)"; then
        for c in "${BREW_CASKS[@]}"; do
            brew list --cask "$c" >/dev/null 2>&1 && brew uninstall --cask "$c" >/dev/null 2>&1 \
                && ok "uninstalled $c" || true
        done
    fi
    brew cleanup >/dev/null 2>&1 || true
else
    ok "Homebrew not present — nothing to uninstall"
fi

# =============================================================================
# 11) .zprofile lines the original appended (backed up first)
# =============================================================================
log "Shell profile cleanup (~/.zprofile)"
ZP="$HOME/.zprofile"
if [ -f "$ZP" ] && grep -Eq 'OLLAMA_|local-ai-workstation|Visual Studio Code.app/Contents/Resources/app/bin' "$ZP"; then
    cp "$ZP" "$ZP.bak.$(date +%Y%m%d%H%M%S)" && ok "backed up $ZP"
    tmp="$(mktemp)"
    grep -Ev 'OLLAMA_MAX_LOADED_MODELS|OLLAMA_KEEP_ALIVE|OLLAMA_HOST|local-ai-workstation|Visual Studio Code.app/Contents/Resources/app/bin' "$ZP" > "$tmp"
    mv "$tmp" "$ZP"
    ok "removed OLLAMA_* / VS Code CLI / workstation lines (brew + .local/bin left intact)"
else
    ok "No matching lines in ~/.zprofile"
fi

# =============================================================================
#  done
# =============================================================================
hr
cat <<DONE
${c_grn}  Cleanup complete.${c_reset}

  What's gone:   old launchd services, Docker containers/volumes, OpenClaw,
                 Ollama runtime, and (if confirmed) all downloaded models.
  What's next:   open a NEW terminal (so shell changes take effect), then run
                 ${c_cyn}./mlx-setup.sh --bootstrap${c_reset}

  A backup of ~/.zprofile was saved if it was modified.
DONE
hr
