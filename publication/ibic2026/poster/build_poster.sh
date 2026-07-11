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
FINAL_LAYOUT_DIR="$WORK/final-layout"
FINAL_LAYOUT="$FINAL_LAYOUT_DIR/final-slide-01.layout.json"
RENDER_DIR="$OUT_DIR/rendered"

for file in \
  "$SKILL_DIR/container_tools/setup_artifact_tool_workspace.mjs" \
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
test -d "$STARTER_LAYOUT_DIR" || {
  echo "missing starter layout directory: $STARTER_LAYOUT_DIR" >&2
  exit 1
}
test -s "$CONTENT" || {
  echo "missing final poster content: $CONTENT" >&2
  exit 1
}

for command in "$NODE" "$PYTHON" "$SOFFICE" "$PDFINFO" "$PDFTOPPM" "$PDFFONTS" "$SHASUM"; do
  command -v "$command" >/dev/null 2>&1 || test -x "$command" || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

mkdir -p "$OUT_DIR" "$WORK" "$FINAL_LAYOUT_DIR" "$RENDER_DIR" \
  "$WORK/home" "$WORK/cache" "$WORK/lo-profile"
"$NODE" "$SKILL_DIR/container_tools/setup_artifact_tool_workspace.mjs" \
  --workspace "$WORK"
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
"$SHASUM" -a 256 "$PPTX" "$PDF" "$PREVIEW" "$ARTIFACT_PREVIEW" "$MANIFEST" \
  >"$OUT_DIR/deliverable-sha256.txt"

printf 'poster_pptx=%s\nposter_pdf=%s\nposter_preview=%s\nposter_artifact_preview=%s\n' \
  "$PPTX" "$PDF" "$PREVIEW" "$ARTIFACT_PREVIEW"
