#!/usr/bin/env bash
# Generate an image locally with mflux.
# Default model: Z-Image Turbo (non-gated, fast, commercial-friendly).
# Usage: mlx-image.sh "PROMPT" [OUT] [MODEL] [STEPS] [WIDTH] [HEIGHT] [SEED]
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

PROMPT="${1:-a friendly robot reading a book, flat vector illustration}"
OUT="${2:-$HOME/MLX-AI/documents/images/img_$(date +%s).png}"
MODEL="${3:-z-image-turbo}"
STEPS="${4:-}"
WIDTH="${5:-1024}"
HEIGHT="${6:-1024}"
SEED="${7:-}"

mkdir -p "$(dirname "$OUT")"

if [ -z "$STEPS" ] || [ "$STEPS" = "0" ]; then
  case "$MODEL" in
    z-image-turbo|z-image) STEPS=9 ;;
    dev)                   STEPS=20 ;;
    *)                     STEPS=4 ;;
  esac
fi

ARGS=(--steps "$STEPS" --width "$WIDTH" --height "$HEIGHT" --prompt "$PROMPT" --output "$OUT")
[ -n "$SEED" ] && ARGS+=(--seed "$SEED")

# Each model family has its own dedicated CLI command in mflux 0.18+
case "$MODEL" in
  z-image-turbo|zimage-turbo)
    CMD="mflux-generate-z-image-turbo"
    ;;
  z-image|zimage)
    CMD="mflux-generate-z-image"
    ;;
  dev|schnell|krea-dev)
    CMD="mflux-generate"
    ARGS+=(--model "$MODEL")
    case "$MODEL" in
      dev)     ARGS+=(--guidance 3.5) ;;
      schnell) ARGS+=(--guidance 0.0) ;;
    esac
    ;;
  *)
    CMD="mflux-generate"
    ARGS+=(--model "$MODEL")
    ;;
esac

uv tool run --from mflux "$CMD" "${ARGS[@]}"
echo "saved: $OUT"
