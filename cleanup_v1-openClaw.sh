#!/usr/bin/env bash
# =============================================================================
#  cleanup_v1.sh — Safely remove the v1 AI Workstation setup
# =============================================================================
#  REMOVES:  launchd agents, Docker containers, Python venv, old configs,
#            OpenClaw (if installed)
#  KEEPS:    .env secrets, all Ollama models, Colima, Homebrew, all packages,
#            Langfuse Postgres database volumes (your trace history)
#
#  Run this, then run: bash setup_ai_team.sh
# =============================================================================
set -uo pipefail

WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

c_reset=$'\033[0m'; c_grn=$'\033[1;32m'; c_yel=$'\033[1;33m'
c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'
ok()   { printf "%s  ✓  %s%s\n" "$c_grn" "$*" "$c_reset"; }
warn() { printf "%s  !  %s%s\n" "$c_yel" "$*" "$c_reset"; }
err()  { printf "%s  ✗  %s%s\n" "$c_red" "$*" "$c_reset" >&2; }
hr()   { printf "%s%s%s\n" "$c_cyn" \
         "══════════════════════════════════════════════════" "$c_reset"; }

hr
printf "%s  🧹  AI Workstation v1 — Safe Cleanup%s\n" "$c_cyn" "$c_reset"
hr

cat <<INFO

${c_red}REMOVES:${c_reset}
  • launchd agents: com.aiws.*, com.openclaw.*
  • Docker containers: open-webui, searxng
    (Langfuse containers stopped — database volumes kept)
  • Python venv: $WORKDIR/.venv
  • Scripts: start_gateway.sh, litellm.config.yaml
  • Directories: agents/, dashboard/
  • Continue config: ~/.continue/config.yaml
  • OpenClaw (if installed via npm)

${c_grn}KEEPS:${c_reset}
  • Your secrets: $WORKDIR/.env
  • All Ollama models (no re-download needed)
  • Colima + Docker installation and images
  • Langfuse Postgres volumes (your trace history)
  • Homebrew, Xcode CLT, all installed tools
  • The $WORKDIR folder itself

INFO
printf "%s⚠️  Press Enter to continue, or Ctrl+C to abort.%s\n" "$c_yel" "$c_reset"
read -r _
echo ""

# ── 1. Unload all launchd agents ─────────────────────────────────────────────
printf "Removing launchd agents...\n"
shopt -s nullglob
_any_removed=false
for plist in \
    "$LAUNCH_DIR"/com.aiws.*.plist \
    "$LAUNCH_DIR"/com.openclaw.*.plist; do
    label="$(basename "$plist" .plist)"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    ok "unloaded & removed: $label"
    _any_removed=true
done
shopt -u nullglob
"$_any_removed" || warn "No launchd agents found (already clean)"

# ── 2. Stop Ollama brew service ───────────────────────────────────────────────
printf "\nStopping Ollama...\n"
brew services stop ollama 2>/dev/null \
    && ok "ollama stopped" \
    || warn "ollama was not running via brew"

# ── 3. Stop Colima (keep installation) ───────────────────────────────────────
printf "\nStopping Colima...\n"
if command -v colima >/dev/null 2>&1; then
    colima stop 2>/dev/null \
        && ok "Colima stopped" \
        || warn "Colima was already stopped"
else
    warn "colima not found — skipping"
fi

# ── 4. Remove Docker containers (keep volumes) ───────────────────────────────
printf "\nHandling Docker containers...\n"
if docker info >/dev/null 2>&1; then
    for ctr in open-webui searxng; do
        if docker inspect "$ctr" >/dev/null 2>&1; then
            docker rm -f "$ctr" >/dev/null 2>&1 \
                && ok "removed container: $ctr" \
                || warn "could not remove container: $ctr"
        else
            warn "container not found: $ctr (skipping)"
        fi
    done
    # Stop Langfuse — NO -v flag so the Postgres volume is preserved
    if [ -d "$WORKDIR/langfuse" ] && \
       ([ -f "$WORKDIR/langfuse/docker-compose.yml" ] || \
        [ -f "$WORKDIR/langfuse/compose.yml" ]); then
        (cd "$WORKDIR/langfuse" && docker compose stop 2>/dev/null) \
            && ok "Langfuse containers stopped (database volumes preserved)" \
            || warn "Langfuse stop failed — may already be down"
    else
        warn "Langfuse directory not found — skipping"
    fi
else
    warn "Docker not running — skipping container removal"
    warn "(launchd agents already unloaded, so containers will not restart)"
fi

# ── 5. Remove old configs, scripts, and venv ─────────────────────────────────
printf "\nRemoving old configs and virtualenv...\n"
declare -a _paths=(
    "$WORKDIR/litellm.config.yaml"
    "$WORKDIR/start_gateway.sh"
    "$WORKDIR/agents"
    "$WORKDIR/dashboard"
    "$WORKDIR/.venv"
    "$WORKDIR/proposals"
    "$HOME/.continue/config.yaml"
)
for path in "${_paths[@]}"; do
    if [ -e "$path" ]; then
        rm -rf "$path" \
            && ok "removed: $path" \
            || warn "could not remove: $path"
    fi
done

# ── 6. Remove OpenClaw if installed ──────────────────────────────────────────
printf "\nChecking for OpenClaw...\n"
if npm ls -g openclaw >/dev/null 2>&1; then
    npm uninstall -g openclaw 2>/dev/null \
        && ok "OpenClaw uninstalled from npm" \
        || warn "OpenClaw npm uninstall failed"
    rm -rf "$HOME/.openclaw" 2>/dev/null || true
    ok "OpenClaw config dir removed"
else
    warn "OpenClaw not installed — skipping"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
printf "\n"
hr
printf "%s  ✅  Cleanup complete.%s\n" "$c_grn" "$c_reset"
hr
printf "\nOllama models are intact. All Homebrew packages intact.\n"
printf "Run the new setup:  %sbash setup_ai_team.sh%s\n\n" \
       "$c_cyn" "$c_reset"
