#!/usr/bin/env bash
# =============================================================================
#  Local AI Workstation v2 — single entry point
#
#    ./install.sh               check EVERYTHING: install what's missing, repair what drifted,
#                               remove what's no longer needed (safe to run any number of times)
#    ./install.sh --dry-run     show what would change, change nothing
#    ./install.sh --upgrade     also re-resolve Python packages and pull newer Docker images
#    ./install.sh --keep-v1     don't retire the old v1 stack (mlx-setup.sh) this run
#    ./install.sh --purge       remove v2 (keeps secrets, backups, models, Docker volumes, ~/MLX-AI)
#
#  One-liner (fresh Mac):
#    curl -fsSL https://raw.githubusercontent.com/mthaqifisa/local-ai-workstation/v5-enhanced-mlx/install.sh | bash
#
#  This script only does what bash must do (Xcode CLT, Homebrew, uv, Python venv),
#  then hands over to the Python control plane:  law install
# =============================================================================
set -Eeuo pipefail

LAW_HOME="${LAW_HOME:-$HOME/.law}"
LAW_REPO="${LAW_REPO:-https://github.com/mthaqifisa/local-ai-workstation.git}"
LAW_BRANCH="${LAW_BRANCH:-v5-enhanced-mlx}"
LAW_PY="${LAW_PY:-3.12}"
CTL_VENV="$LAW_HOME/control/.venv"
BIN_DIR="$HOME/.local/bin"
LOG_DIR="$LAW_HOME/logs"
LOCK_DIR="$LAW_HOME/run/install.lock.d"

c_b=$'\033[1m'; c_g=$'\033[32m'; c_y=$'\033[33m'; c_r=$'\033[31m'; c_c=$'\033[36m'; c_0=$'\033[0m'
[ -t 1 ] || { c_b=; c_g=; c_y=; c_r=; c_c=; c_0=; }
log()  { printf '%s==>%s %s\n' "$c_c" "$c_0" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$c_g" "$c_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_y" "$c_0" "$*" >&2; }
die()  { printf '%s✗ %s%s\n' "$c_r" "$*" "$c_0" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

on_err() {
    local rc=$? line=${1:-?}
    printf '%s✗ install.sh failed at line %s (exit %s).%s\n' "$c_r" "$line" "$rc" "$c_0" >&2
    printf '  Re-run ./install.sh — finished steps are skipped, it resumes where it stopped.\n' >&2
}
trap 'on_err $LINENO' ERR

# ── args: install.sh only consumes --purge/--help; everything else goes to `law install`
PASS_ARGS=()
PURGE=0
for a in "$@"; do
    case "$a" in
        --purge) PURGE=1 ;;
        -h|--help)
            sed -n '2,17p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) PASS_ARGS+=("$a") ;;
    esac
done

# ── 1. preflight ────────────────────────────────────────────────────────────
log "${c_b}Local AI Workstation v2${c_0}"
[ "$(uname -s)" = "Darwin" ] || die "macOS only."
[ "$(uname -m)" = "arm64" ]  || die "Apple Silicon (arm64) only. If you're on Rosetta, open a native Terminal."
[ "$(id -u)" -ne 0 ]         || die "Do not run as root/sudo. Run as your normal user."
MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MACOS_MAJOR" -ge 14 ] || die "macOS 14 (Sonoma) or newer required; found $(sw_vers -productVersion)."
RAM_GB=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
CHIP="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo 'Apple Silicon')"
ok "$CHIP · ${RAM_GB} GB · macOS $(sw_vers -productVersion)"
[ "$RAM_GB" -ge 32 ] || warn "Less than 32 GB RAM — large models will be refused by the RAM guard."

# ── 2. single-instance lock (mkdir is atomic) ───────────────────────────────
mkdir -p "$LAW_HOME/run" "$LOG_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    oldpid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
        die "Another install is running (pid $oldpid). Wait for it to finish."
    fi
    warn "Removing stale install lock (previous run was interrupted)."
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# ── 3. Xcode Command Line Tools ─────────────────────────────────────────────
log "Xcode Command Line Tools"
if xcode-select -p >/dev/null 2>&1; then
    ok "present"
else
    warn "Installing — a macOS dialog will appear. Click Install and wait."
    xcode-select --install >/dev/null 2>&1 || true
    for _ in $(seq 1 180); do xcode-select -p >/dev/null 2>&1 && break; sleep 10; done
    xcode-select -p >/dev/null 2>&1 || die "Command Line Tools not installed. Finish the dialog, then re-run."
    ok "installed"
fi

# ── 4. Homebrew ─────────────────────────────────────────────────────────────
log "Homebrew"
if [ ! -x /opt/homebrew/bin/brew ]; then
    warn "Installing Homebrew (it will ask for your Mac password once)."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
        || die "Homebrew install failed."
fi
eval "$(/opt/homebrew/bin/brew shellenv)"
touch "$HOME/.zprofile"
grep -qF 'brew shellenv' "$HOME/.zprofile" || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
ok "$(brew --version | head -n1)"

# ── 5. uv + git ─────────────────────────────────────────────────────────────
log "uv + git"
for p in uv git; do
    brew list --formula "$p" >/dev/null 2>&1 || brew install "$p"
done
ok "$(uv --version)"

# ── 6. source: use this checkout, or clone/update ~/.law/src ────────────────
log "Source"
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pyproject.toml" ] && [ -d "$SELF_DIR/law" ]; then
    SRC="$SELF_DIR"
    ok "using local checkout: $SRC"
else
    SRC="$LAW_HOME/src"
    if [ -d "$SRC/.git" ]; then
        git -C "$SRC" fetch --depth=1 origin "$LAW_BRANCH" >/dev/null 2>&1 \
            && git -C "$SRC" reset --hard "origin/$LAW_BRANCH" >/dev/null \
            && ok "updated $SRC ($LAW_BRANCH)" \
            || warn "could not update $SRC — continuing with the existing copy"
    else
        rm -rf "$SRC"
        git clone --depth=1 --branch "$LAW_BRANCH" "$LAW_REPO" "$SRC" >/dev/null 2>&1 \
            || die "git clone failed: $LAW_REPO ($LAW_BRANCH)"
        ok "cloned $LAW_BRANCH → $SRC"
    fi
fi

# ── 7. purge (explicit only) ────────────────────────────────────────────────
if [ "$PURGE" = "1" ]; then
    if [ -x "$CTL_VENV/bin/law" ]; then
        "$CTL_VENV/bin/law" purge --yes || true
    fi
    rm -f "$BIN_DIR/law"
    rm -rf "$LAW_HOME/control" "$LAW_HOME/state" "$LAW_HOME/run"
    ok "v2 removed."
    exit 0
fi

# ── 8. control-plane venv (Python 3.12, managed by uv) ──────────────────────
log "Control plane (Python $LAW_PY)"
mkdir -p "$LAW_HOME/control"
if [ ! -x "$CTL_VENV/bin/python" ] || ! "$CTL_VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)' 2>/dev/null; then
    rm -rf "$CTL_VENV"
    uv venv --python "$LAW_PY" "$CTL_VENV" >/dev/null || die "uv venv failed"
fi
uv pip install --python "$CTL_VENV/bin/python" --quiet -e "$SRC" || die "installing the law package failed"
ok "law $("$CTL_VENV/bin/law" --version)"

# ── 9. `law` on PATH (atomic shim) ──────────────────────────────────────────
mkdir -p "$BIN_DIR"
tmp="$(mktemp "$BIN_DIR/.law.XXXXXX")"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$CTL_VENV/bin/law" > "$tmp"
chmod 755 "$tmp"
if ! cmp -s "$tmp" "$BIN_DIR/law" 2>/dev/null; then mv -f "$tmp" "$BIN_DIR/law"; else rm -f "$tmp"; fi
grep -qF '.local/bin' "$HOME/.zprofile" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zprofile"

# ── 10. hand over ───────────────────────────────────────────────────────────
log "Handing over to: law install ${PASS_ARGS[*]+${PASS_ARGS[*]}}"
rm -rf "$LOCK_DIR"; trap - EXIT    # the Python side takes its own lock
exec "$CTL_VENV/bin/law" install --src "$SRC" ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}
