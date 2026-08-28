#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

HERE=$(cd "$(dirname "$0")" && pwd)
SKILL_DIR=${PRESENTATIONS_SKILL_DIR:?set PRESENTATIONS_SKILL_DIR to the installed Presentations skill}
STARTER_PPTX=${STARTER_PPTX:?set STARTER_PPTX to the prepared template starter}
STARTER_LAYOUT_DIR=${STARTER_LAYOUT_DIR:?set STARTER_LAYOUT_DIR to the starter layout directory}
CONTENT=${CONTENT:-"$HERE/content.json"}
OUT_DIR=${OUT_DIR:-"$HERE/build"}
WORK=${WORK:-"${TMPDIR:-/tmp}/ibic2026-poster-build"}

NODE=${NODE:-node}
PYTHON=${PYTHON:-python3}
SOFFICE=${SOFFICE:-soffice}
PDFINFO=${PDFINFO:-pdfinfo}
PDFTOPPM=${PDFTOPPM:-pdftoppm}
PDFFONTS=${PDFFONTS:-pdffonts}
SHASUM=${SHASUM:-shasum}

BASE=ibic2026-abstract54-poster
PPTX="$OUT_DIR/$BASE.pptx"
PDF="$OUT_DIR/$BASE.pdf"
PREVIEW="$OUT_DIR/$BASE.png"
ARTIFACT_PREVIEW="$OUT_DIR/$BASE-artifact-preview.png"
MANIFEST="$OUT_DIR/source_manifest.json"
FINAL_LAYOUT_DIR="$OUT_DIR/layout"
FINAL_LAYOUT="$FINAL_LAYOUT_DIR/final-slide-01.layout.json"
RENDER_DIR="$OUT_DIR/rendered"
QA_DIR="$OUT_DIR/qa"
INSPECT="$PPTX.inspect.ndjson"

for file in \
  "$SKILL_DIR/container_tools/slides_test.py" \
  "$SKILL_DIR/template_following_scripts/check_template_fidelity.mjs" \
  "$HERE/build_poster.mjs" \
  "$HERE/template-frame-map.json" \
  "$STARTER_PPTX"; do
  test -s "$file" || {
    echo "missing required poster input: $file" >&2
    exit 1
  }
done
RUNTIME_NODE_MODULES=${RUNTIME_NODE_MODULES:?set RUNTIME_NODE_MODULES to the bundled Node.js packages directory}
test -d "$RUNTIME_NODE_MODULES" || {
  echo "missing bundled Node.js packages directory: $RUNTIME_NODE_MODULES" >&2
  exit 1
}
test -d "$STARTER_LAYOUT_DIR" || {
  echo "missing starter layout directory: $STARTER_LAYOUT_DIR" >&2
  exit 1
}
test -s "$CONTENT" || {
  echo "missing final poster content: $CONTENT" >&2
  exit 1
}
for poster_input in "$HERE/evidence_gate.json" "$HERE/input_manifest.json"; do
  test -s "$poster_input" || {
    echo "missing paper-gated poster input: $poster_input" >&2
    exit 1
  }
done

for command in "$NODE" "$PYTHON" "$SOFFICE" "$PDFINFO" "$PDFTOPPM" "$PDFFONTS" "$SHASUM"; do
  command -v "$command" >/dev/null 2>&1 || test -x "$command" || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

mkdir -p "$OUT_DIR" "$WORK" "$FINAL_LAYOUT_DIR" "$RENDER_DIR" "$QA_DIR" \
  "$WORK/home" "$WORK/cache" "$WORK/lo-profile"
if [[ -L "$WORK/node_modules" ]]; then
  [[ "$(readlink "$WORK/node_modules")" == "$RUNTIME_NODE_MODULES" ]] || {
    echo "scratch node_modules points at the wrong runtime" >&2
    exit 1
  }
elif [[ -e "$WORK/node_modules" ]]; then
  echo "scratch node_modules exists and is not a symlink: $WORK/node_modules" >&2
  exit 1
else
  ln -s "$RUNTIME_NODE_MODULES" "$WORK/node_modules"
fi
cp "$HERE/build_poster.mjs" "$WORK/build_poster.mjs"

"$NODE" "$WORK/build_poster.mjs" \
  --starter "$STARTER_PPTX" \
  --content "$CONTENT" \
  --out "$PPTX" \
  --preview "$ARTIFACT_PREVIEW" \
  --layout "$FINAL_LAYOUT" \
  --manifest "$MANIFEST"

"$PYTHON" "$SKILL_DIR/container_tools/slides_test.py" "$PPTX" --pad_px 10
"$NODE" "$SKILL_DIR/template_following_scripts/check_template_fidelity.mjs" \
  --workspace "$WORK" \
  --starter-pptx "$STARTER_PPTX" \
  --final-pptx "$PPTX" \
  --map "$HERE/template-frame-map.json" \
  --starter-layout-dir "$STARTER_LAYOUT_DIR" \
  --final-layout-dir "$FINAL_LAYOUT_DIR" \
  --edit-dir "$WORK"
for report in template-fidelity-check.json template-fidelity-check.txt; do
  test -s "$WORK/qa/$report" || {
    echo "template-fidelity check did not produce $report" >&2
    exit 1
  }
  cp "$WORK/qa/$report" "$QA_DIR/$report"
done
"$PYTHON" -c '
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
report = json.loads(path.read_text(encoding="utf-8"))
report.update({
    "workspace": "ephemeral/poster-build",
    "finalPptx": "publication/ibic2026/poster/build/ibic2026-abstract54-poster.pptx",
    "starterPptx": "external/template-starter.pptx",
    "mapPath": "publication/ibic2026/poster/template-frame-map.json",
    "starterLayoutDir": "external/template-starter-layout",
    "finalLayoutDir": "publication/ibic2026/poster/build/layout",
    "editDir": "ephemeral/poster-build",
})
path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
' "$QA_DIR/template-fidelity-check.json"
test -s "$FINAL_LAYOUT" || {
  echo "poster build did not preserve the final layout inventory: $FINAL_LAYOUT" >&2
  exit 1
}
test -s "$INSPECT" || {
  echo "slides_test did not preserve the PPTX inspection record: $INSPECT" >&2
  exit 1
}

rm -f "$PDF"
profile_uri="file://${WORK// /%20}/lo-profile"
HOME="$WORK/home" XDG_CACHE_HOME="$WORK/cache" \
  "$SOFFICE" --headless "-env:UserInstallation=$profile_uri" \
  --convert-to pdf --outdir "$OUT_DIR" "$PPTX"
test -s "$PDF" || {
  echo "LibreOffice did not produce the poster PDF: $PDF" >&2
  exit 1
}

pdf_info=$($PDFINFO "$PDF")
pages=$(awk '/^Pages:/ {print $2}' <<<"$pdf_info")
if [[ "$pages" != "1" ]]; then
  echo "expected one poster page, found ${pages:-unknown}" >&2
  exit 1
fi
if ! grep -Eq '^Page size:.*\(A0\)$' <<<"$pdf_info"; then
  echo "poster PDF is not reported as A0:" >&2
  grep '^Page size:' <<<"$pdf_info" >&2 || true
  exit 1
fi

"$PDFFONTS" "$PDF" >"$OUT_DIR/pdffonts.txt"
if awk 'NR > 2 && ($(NF-4) != "yes" || $(NF-3) != "yes" || $(NF-2) != "yes") {bad=1} END {exit bad}' \
  "$OUT_DIR/pdffonts.txt"; then
  :
else
  echo "poster PDF contains a non-embedded or non-subset font" >&2
  exit 1
fi

rm -f "$RENDER_DIR"/poster-*.png "$PREVIEW"
"$PDFTOPPM" -png -r 150 "$PDF" "$RENDER_DIR/poster"
test -s "$RENDER_DIR/poster-1.png" || {
  echo "PDF rasterizer did not produce the poster PNG" >&2
  exit 1
}
cp "$RENDER_DIR/poster-1.png" "$PREVIEW"
cmp -s "$PREVIEW" "$RENDER_DIR/poster-1.png" || {
  echo "poster preview does not match the authoritative PDF raster" >&2
  exit 1
}
write_checksum() {
  local source=$1
  local label=$2
  local digest
  digest=$("$SHASUM" -a 256 "$source" | awk '{print $1}')
  printf '%s  %s\n' "$digest" "$label"
}
{
  write_checksum "$PPTX" "$BASE.pptx"
  write_checksum "$PDF" "$BASE.pdf"
  write_checksum "$PREVIEW" "$BASE.png"
  write_checksum "$ARTIFACT_PREVIEW" "$BASE-artifact-preview.png"
  write_checksum "$MANIFEST" "source_manifest.json"
  write_checksum "$FINAL_LAYOUT" "layout/final-slide-01.layout.json"
  write_checksum "$INSPECT" "$BASE.pptx.inspect.ndjson"
  write_checksum "$QA_DIR/template-fidelity-check.json" "qa/template-fidelity-check.json"
  write_checksum "$QA_DIR/template-fidelity-check.txt" "qa/template-fidelity-check.txt"
  write_checksum "$OUT_DIR/pdffonts.txt" "pdffonts.txt"
} >"$OUT_DIR/deliverable-sha256.txt"

printf 'poster_pptx=%s\nposter_pdf=%s\nposter_preview=%s\nposter_artifact_preview=%s\n' \
  "$PPTX" "$PDF" "$PREVIEW" "$ARTIFACT_PREVIEW"
