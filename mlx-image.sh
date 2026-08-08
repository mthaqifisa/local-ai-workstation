#!/usr/bin/env bash
# Generate an image locally with mflux 0.18+.
# Best-quality default: Qwen-Image 20.4B @ 4-bit (public, no HF auth).
# Usage: mlx-image.sh "PROMPT" [OUT] [MODEL] [STEPS] [WIDTH] [HEIGHT] [SEED]
#
# MODEL values:
#   qwen          — Qwen/Qwen-Image 20.4B 4-bit (best quality, ~10GB, default)
#   ernie         — ERNIE-Image 8B (high quality, ~4GB, fast)
#   z-image-turbo — Z-Image Turbo (fastest, already cached)
#   schnell       — FLUX.1-schnell (needs download)
#   dev           — FLUX.1-dev (needs download)
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

PROMPT="${1:-a friendly robot reading a book, flat vector illustration}"
OUT="${2:-$HOME/MLX-AI/documents/images/img_$(date +%s).png}"
MODEL="${3:-qwen}"
STEPS="${4:-}"
WIDTH="${5:-1024}"
HEIGHT="${6:-1024}"
SEED="${7:-}"

mkdir -p "$(dirname "$OUT")"

if [ -z "$STEPS" ] || [ "$STEPS" = "0" ]; then
  case "$MODEL" in
    qwen|qwen-image)         STEPS=20 ;;
    ernie|ernie-image)       STEPS=20 ;;
    z-image-turbo|z-image)   STEPS=9  ;;
    dev)                     STEPS=20 ;;
    *)                       STEPS=4  ;;
  esac
fi

ARGS=(--steps "$STEPS" --width "$WIDTH" --height "$HEIGHT" --prompt "$PROMPT" --output "$OUT")
[ -n "$SEED" ] && ARGS+=(--seed "$SEED")

case "$MODEL" in
  qwen|qwen-image)
    CMD="mflux-generate-qwen"
    ARGS+=(--quantize 4)
    ;;
  ernie|ernie-image)
    CMD="mflux-generate-ernie-image"
    ARGS+=(--quantize 4 --guidance 3.5)
    ;;
  z-image-turbo|zimage-turbo)
    CMD="mflux-generate-z-image-turbo"
    ;;
  z-image|zimage)
    CMD="mflux-generate-z-image"
    ;;
  edit|img2img)
    CMD="mflux-generate-qwen"
    ARGS+=(--quantize 4)
    [ -n "${8:-}" ] && ARGS+=(--image-path "$8" --image-strength "${9:-0.65}")
    ;;
  dev|schnell|krea-dev)
    CMD="mflux-generate"
    ARGS+=(--base-model "$MODEL")
    case "$MODEL" in
      dev)     ARGS+=(--guidance 3.5) ;;
      schnell) ;;
    esac
    ;;
  *)
    CMD="mflux-generate"
    ARGS+=(--model "$MODEL")
    ;;
esac

"$CMD" "${ARGS[@]}"
echo "saved: $OUT"
