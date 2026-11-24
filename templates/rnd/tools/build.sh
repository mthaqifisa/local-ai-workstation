#!/usr/bin/env bash
# Build the book from its one source: book.md + chapters/*.md -> book.pdf (A4, Typst) and book.html (one self-contained file).
#   bash tools/build.sh
# Diagrams: diagrams/<name>.mmd (Mermaid) or diagrams/<name>.dot (Graphviz) are rendered to diagrams/<name>.png
# (3x scale, so text stays sharp) whenever the source is newer. Reference them in book.md as
#   ![Caption that says what the figure shows](diagrams/<name>.png){#fig:<name>}
# LAW's book kit (templates/rnd). tools/verify.py, run by .law/check.sh, checks what this builds.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .law/env.sh ] && . .law/env.sh
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PUPPETEER="${LAW_PUPPETEER_CONFIG:-$HOME/LAW/config/puppeteer.json}"
mkdir -p build

shopt -s nullglob
for src in diagrams/*.mmd; do
  out="${src%.mmd}.png"
  if [ ! -f "$out" ] || [ "$src" -nt "$out" ]; then
    echo "==> diagram $src"
    # the SVG records the diagram's natural size (mmdc quietly shrinks a PNG wider than 800 px to fit, which
    # hides unreadable text); the PNG is what the book shows
    mkdir -p build/diagrams
    mmdc -p "$PUPPETEER" -i "$src" -o "build/${src%.mmd}.svg" -b white -q \
      && mmdc -p "$PUPPETEER" -i "$src" -o "$out" -s 3 -b white -q \
      || { echo "build: Mermaid could not render $src (fix its syntax)" >&2; exit 1; }
  fi
done
for src in diagrams/*.dot; do
  out="${src%.dot}.png"
  if [ ! -f "$out" ] || [ "$src" -nt "$out" ]; then
    echo "==> diagram $src"
    dot -Tpng -Gdpi=216 -o "$out" "$src" || { echo "build: Graphviz could not render $src" >&2; exit 1; }
  fi
done

PYTHONDONTWRITEBYTECODE=1 python3 tools/prepare.py . build/book.render.md pdf
PYTHONDONTWRITEBYTECODE=1 python3 tools/prepare.py . build/book.html.md html

echo "==> book.pdf (A4)"
pandoc build/book.render.md -f markdown-citations -o book.pdf --pdf-engine=typst \
  --template=tools/book.typst --resource-path=. 2> build/pdf.log || { cat build/pdf.log >&2; exit 1; }
grep -i "warning" build/pdf.log >&2 || true

echo "==> book.html"
pandoc build/book.html.md -f markdown-citations -s --toc --toc-depth=2 --number-sections \
  --embed-resources --css=tools/book.css --resource-path=. -o book.html

echo "==> built book.pdf ($(pdfinfo book.pdf | awk '/^Pages:/ {print $2}') pages) and book.html"
