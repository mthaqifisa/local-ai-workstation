#!/usr/bin/env bash
# =============================================================================
#  cleanup_v1.sh — Fully remove AI Workstation (v1 / v2 / v3 compatible)
# =============================================================================
#
#  REMOVES:
#    • LaunchAgents: com.aiws.*, com.openclaw.*
#    • Any process bound to the agent port (orphaned uvicorn / litellm)
#    • Docker containers: open-webui, searxng, langfuse-server, db
#    • Docker volumes:    open-webui, langfuse_db
#    • Python venv:       $WORKDIR/.venv/
#    • Agent server:      $WORKDIR/agents/
#    • Dashboard:         $WORKDIR/dashboard/
#    • Generated PDFs:    $WORKDIR/proposals/
#    • Wireframe images:  $WORKDIR/wireframes/
#    • Log files:         $WORKDIR/logs/
#    • Project data:      $WORKDIR/projects.json
#    • Configs:           litellm.config.yaml, start_gateway.sh
#    • Continue IDE:      ~/.continue/config.yaml
#    • OpenClaw (if installed via npm)
#
#  KEEPS — never touched:
#    • $WORKDIR/.env  (all your API keys, USER_EMAIL)
#    • All Ollama models (no re-download needed on next install)
#    • Colima + Docker installation and cached images
#    • Homebrew, Xcode CLT, and every other installed tool
#
#  After running:  bash setup_ai_team.sh
# =============================================================================
set -uo pipefail

WORKDIR="${WORKDIR:-$HOME/ai-workstation}"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
AGENT_PORT="${AGENT_PORT:-8000}"
LITELLM_PORT="${LITELLM_PORT:-4000}"

c_reset=$'\033[0m'; c_grn=$'\033[1;32m'; c_yel=$'\033[1;33m'
c_red=$'\033[1;31m'; c_cyn=$'\033[1;36m'; c_bold=$'\033[1m'
ok()   { printf "%s  ✓  %s%s\n" "$c_grn" "$*" "$c_reset"; }
warn() { printf "%s  !  %s%s\n" "$c_yel" "$*" "$c_reset"; }
info() { printf "%s  ▸  %s%s\n" "$c_cyn" "$*" "$c_reset"; }
hr()   { printf "%s%s%s\n" "$c_cyn" \
         "══════════════════════════════════════════════════" "$c_reset"; }

hr
printf "%s  🧹  AI Workstation — Full Cleanup (v1/v2/v3)%s\n" "$c_cyn" "$c_reset"
hr

cat << MANIFEST

${c_bold}REMOVES${c_reset}
  LaunchAgents      com.aiws.agent-server, com.aiws.gateway, com.openclaw.*
  Port processes    Any process on :${AGENT_PORT} (agent) and :${LITELLM_PORT} (gateway)
  Docker containers open-webui, searxng, langfuse-server, db
  Docker volumes    open-webui, langfuse_db  (Postgres trace history deleted)
  Python venv       $WORKDIR/.venv/
  Directories       agents/, dashboard/, proposals/, wireframes/, langfuse/, logs/
  Files             litellm.config.yaml, start_gateway.sh, projects.json
  Continue IDE      ~/.continue/config.yaml
  OpenClaw          if installed via npm

${c_bold}KEEPS${c_reset}
  $c_grn$WORKDIR/.env$c_reset     all API keys and USER_EMAIL — never touched
  ${c_grn}Ollama models$c_reset     no re-download needed on next install
  ${c_grn}Colima / Docker$c_reset   installation and cached images kept
  ${c_grn}Homebrew tools$c_reset    all packages intact

MANIFEST

printf "%s⚠️  Continue? This will delete containers, volumes, and all generated files.%s\n" \
       "$c_yel" "$c_reset"
printf "   Press Enter to proceed, or Ctrl+C to abort.\n"
read -r _
echo ""

# =============================================================================
#  1.  UNLOAD LAUNCHAGENTS
# =============================================================================
printf "── Step 1/7  Unloading LaunchAgents ──────────────────────────────────\n"
shopt -s nullglob
_removed=false
for plist in \
    "$LAUNCH_DIR"/com.aiws.*.plist \
    "$LAUNCH_DIR"/com.openclaw.*.plist; do
    label="$(basename "$plist" .plist)"
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    ok "Unloaded & removed: $label"
    _removed=true
done
shopt -u nullglob
"$_removed" || warn "No LaunchAgents found (already clean)"
echo ""

# =============================================================================
#  2.  KILL ORPHANED PORT PROCESSES
#      launchctl unload can leave processes running. Kill by port to be sure.
# =============================================================================
printf "── Step 2/7  Killing port processes ──────────────────────────────────\n"
for _port in "${AGENT_PORT}" "${LITELLM_PORT}"; do
    _pid=$(lsof -ti tcp:"${_port}" 2>/dev/null || true)
    if [ -n "$_pid" ]; then
        kill -9 $_pid 2>/dev/null || true
        ok "Killed process(es) on port ${_port} (PID: $_pid)"
    else
        ok "Port ${_port} already free"
    fi
done
echo ""

# =============================================================================
#  3.  STOP OLLAMA SERVICE
# =============================================================================
printf "── Step 3/7  Stopping Ollama ──────────────────────────────────────────\n"
brew services stop ollama 2>/dev/null \
    && ok "Ollama service stopped" \
    || warn "Ollama was not running via brew (skipping)"
echo ""

# =============================================================================
#  4.  REMOVE DOCKER CONTAINERS AND VOLUMES
#      Must run BEFORE stopping Colima so docker commands work.
# =============================================================================
printf "── Step 4/7  Removing Docker containers and volumes ──────────────────\n"

if docker info >/dev/null 2>&1; then
    ok "Docker daemon reachable"

    # ── Langfuse (compose) — -v removes named volumes including langfuse_db ──
    COMPOSE_FILE=""
    [ -f "$WORKDIR/langfuse/docker-compose.yml" ] && COMPOSE_FILE="$WORKDIR/langfuse/docker-compose.yml"
    [ -f "$WORKDIR/langfuse/compose.yml" ]         && COMPOSE_FILE="$WORKDIR/langfuse/compose.yml"

    if [ -n "$COMPOSE_FILE" ]; then
        (cd "$(dirname "$COMPOSE_FILE")" && docker compose down -v 2>/dev/null) \
            && ok "Langfuse: containers and volumes removed" \
            || warn "Langfuse compose down failed — attempting manual cleanup"
    fi

    # Remove Langfuse volume directly in case compose was already deleted or failed
    docker volume rm langfuse_db 2>/dev/null \
        && ok "Docker volume removed: langfuse_db" \
        || warn "langfuse_db volume not found (already clean)"

    # ── open-webui container + its named volume ───────────────────────────────
    if docker inspect open-webui >/dev/null 2>&1; then
        docker rm -f open-webui >/dev/null 2>&1 \
            && ok "Container removed: open-webui" \
            || warn "Could not remove container: open-webui"
    else
        warn "Container not found: open-webui (already clean)"
    fi
    docker volume rm open-webui 2>/dev/null \
        && ok "Docker volume removed: open-webui" \
        || warn "open-webui volume not found (already clean)"

    # ── searxng container ─────────────────────────────────────────────────────
    if docker inspect searxng >/dev/null 2>&1; then
        docker rm -f searxng >/dev/null 2>&1 \
            && ok "Container removed: searxng" \
            || warn "Could not remove container: searxng"
    else
        warn "Container not found: searxng (already clean)"
    fi

else
    warn "Docker not reachable — skipping container and volume removal"
    warn "If Colima is stopped: start it, run this script again, then stop it."
fi
echo ""

# =============================================================================
#  5.  STOP COLIMA
# =============================================================================
printf "── Step 5/7  Stopping Colima ──────────────────────────────────────────\n"
if command -v colima >/dev/null 2>&1; then
    colima stop 2>/dev/null \
        && ok "Colima stopped" \
        || warn "Colima was already stopped"
else
    warn "colima not found — skipping"
fi
echo ""

# =============================================================================
#  6.  REMOVE FILES AND DIRECTORIES
#      .env is explicitly skipped — it holds your API keys.
# =============================================================================
printf "── Step 6/7  Removing files and directories ──────────────────────────\n"

# Directories inside WORKDIR
for _dir in \
    "$WORKDIR/agents" \
    "$WORKDIR/dashboard" \
    "$WORKDIR/proposals" \
    "$WORKDIR/wireframes" \
    "$WORKDIR/langfuse" \
    "$WORKDIR/logs" \
    "$WORKDIR/.venv"; do
    if [ -d "$_dir" ]; then
        rm -rf "$_dir" \
            && ok "Removed: $_dir" \
            || warn "Could not remove: $_dir"
    fi
done

# Individual files
for _file in \
    "$WORKDIR/litellm.config.yaml" \
    "$WORKDIR/start_gateway.sh" \
    "$WORKDIR/projects.json"; do
    if [ -f "$_file" ]; then
        rm -f "$_file" \
            && ok "Removed: $_file" \
            || warn "Could not remove: $_file"
    fi
done

# Continue IDE — remove only the config file the setup script wrote.
# The ~/.continue/ directory may contain configs for other projects; leave it.
if [ -f "$HOME/.continue/config.yaml" ]; then
    rm -f "$HOME/.continue/config.yaml" \
        && ok "Removed: ~/.continue/config.yaml" \
        || warn "Could not remove: ~/.continue/config.yaml"
fi

# Verify .env was not touched
if [ -f "$WORKDIR/.env" ]; then
    ok ".env preserved — API keys intact"
else
    warn ".env not found (may not have been created yet)"
fi
echo ""

# =============================================================================
#  7.  REMOVE OPENCLAW (if installed)
# =============================================================================
printf "── Step 7/7  Checking for OpenClaw ───────────────────────────────────\n"
if npm ls -g openclaw >/dev/null 2>&1; then
    npm uninstall -g openclaw 2>/dev/null \
        && ok "OpenClaw uninstalled from npm" \
        || warn "OpenClaw npm uninstall failed"
    rm -rf "$HOME/.openclaw" 2>/dev/null || true
    ok "OpenClaw config directory removed"
else
    warn "OpenClaw not installed — skipping"
fi
echo ""

# =============================================================================
#  DONE
# =============================================================================
hr
printf "%s  ✅  Cleanup complete.%s\n" "$c_grn" "$c_reset"
hr

cat << DONE

  ${c_bold}What was preserved${c_reset}
  ──────────────────────────────────────────────────
  $([ -f "$WORKDIR/.env" ] && echo "✓" || echo "–")  $WORKDIR/.env  (API keys and USER_EMAIL)
  ✓  Ollama models — run 'ollama list' to confirm
  ✓  Homebrew packages, Colima, Docker images

  ${c_bold}Ready for a fresh install${c_reset}
  ──────────────────────────────────────────────────
  bash setup_ai_team.sh

DONE
