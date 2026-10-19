#!/usr/bin/env bash
# LAW v6 bootstrap. Requires only Homebrew and Python; installs everything else via `law install`.
# Safe to run any number of times: each run changes only what differs from settings/.
#
#   ./install.sh                     # install or update
#   ./install.sh --dry-run           # show what would change
#   ./install.sh --secrets FILE      # import Telegram/HF tokens from a file you keep privately
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAW_HOME="${LAW_HOME:-$HOME/LAW}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "LAW runs on macOS only."
[[ "$(uname -m)" == "arm64" ]]  || fail "LAW needs an Apple Silicon Mac."
[[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 15 ]] || fail "macOS 15 or newer is required."
[[ "$EUID" -ne 0 ]] || fail "Run as your normal user, not root."

BREW="$(command -v brew || true)"
[[ -n "$BREW" ]] || BREW=/opt/homebrew/bin/brew
[[ -x "$BREW" ]] || fail "Homebrew is required: https://brew.sh"
eval "$("$BREW" shellenv)"
command -v python3 >/dev/null || fail "Python 3 is required (brew install python)."

if ! command -v uv >/dev/null; then
  say "Installing uv"
  brew install uv
fi
if ! command -v git-lfs >/dev/null; then
  say "Installing git-lfs"
  brew install git-lfs
fi
git -C "$REPO" lfs install --local >/dev/null 2>&1 || true
git -C "$REPO" lfs pull >/dev/null 2>&1 || true

mkdir -p "$LAW_HOME"/{runtime,cache/uv,state,logs}
chmod 700 "$LAW_HOME/state"

export LAW_HOME LAW_REPO="$REPO"
export UV_PROJECT_ENVIRONMENT="$LAW_HOME/runtime/law-venv"
export UV_CACHE_DIR="$LAW_HOME/cache/uv"
export UV_PYTHON_INSTALL_DIR="$LAW_HOME/runtime/python"

say "Preparing the LAW control environment"
uv sync --project "$REPO" --locked --python 3.12 --quiet

say "Running law install $*"
exec "$UV_PROJECT_ENVIRONMENT/bin/law" install "$@"
