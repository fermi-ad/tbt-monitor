#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export LANG=C

HERE=$(cd "$(dirname "$0")" && pwd)
TECTONIC=${TECTONIC:-tectonic}
PDFINFO=${PDFINFO:-pdfinfo}
PDFTOPPM=${PDFTOPPM:-pdftoppm}
PDFFONTS=${PDFFONTS:-pdffonts}
SHASUM=${SHASUM:-shasum}
OUT=${OUT:-"$HERE/build"}
SOURCE=${SOURCE:-"$HERE/ABSTRACT54.tex"}
TECTONIC_ARGS=()
if [[ -n "${TECTONIC_FLAGS:-}" ]]; then
  read -r -a TECTONIC_ARGS <<<"$TECTONIC_FLAGS"
fi

for command in "$TECTONIC" "$PDFINFO" "$PDFTOPPM" "$PDFFONTS" "$SHASUM"; do
  command -v "$command" >/dev/null 2>&1 || test -x "$command" || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

for figure in \
  "$HERE/figures/best_n_validation_h.png" \
  "$HERE/figures/best_n_validation_v.png" \
  "$HERE/figures/ridge_density_comparison.png" \
  "$HERE/figures/ridge_width_contrast_hv.png" \
  "$HERE/figures/horizontal_loss_diagnostic.png"; do
  test -s "$figure" || {
    echo "missing required publication figure: $figure" >&2
    exit 1
  }
done
test -s "$HERE/results_table.tex" || {
  echo "missing verifier-derived results table: $HERE/results_table.tex" >&2
  exit 1
}
test -s "$HERE/results_macros.tex" || {
  echo "missing verifier-derived results macros: $HERE/results_macros.tex" >&2
  exit 1
}

for text_source in "$SOURCE" "$HERE/results_table.tex" "$HERE/results_macros.tex"; do
  if grep -Eiq 'final manuscript will report|(^|[^[:alnum:]_])(pending|provisional|tbd|todo)([^[:alnum:]_]|$)|\[[[:space:]]+\]' "$text_source"; then
    echo "paper source still contains unresolved or provisional copy: $text_source" >&2
    exit 1
  fi
done
grep -q '89243024CSC000002' "$SOURCE" || {
  echo "paper source is missing the current Fermilab contract number" >&2
  exit 1
}

mkdir -p "$OUT" "$OUT/rendered"
"$TECTONIC" "${TECTONIC_ARGS[@]}" --keep-logs --keep-intermediates --outdir "$OUT" "$SOURCE"

PDF="$OUT/$(basename "${SOURCE%.tex}").pdf"
test -s "$PDF"
pdf_info=$("$PDFINFO" "$PDF")
pages=$(awk '/^Pages:/ {print $2}' <<<"$pdf_info")
if [[ "$pages" != "4" ]]; then
  echo "expected exactly four paper pages, found ${pages:-unknown}" >&2
  exit 1
fi
page_width=$(awk '/^Page size:/ {print $3}' <<<"$pdf_info")
page_height=$(awk '/^Page size:/ {print $5}' <<<"$pdf_info")
if ! awk -v width="$page_width" -v height="$page_height" \
  'BEGIN {exit !(width >= 594 && width <= 596 && height >= 791 && height <= 793)}'; then
  echo "paper does not use the JACoW class-defined 595 x 792 bp page: ${page_width:-?} x ${page_height:-?}" >&2
  exit 1
fi
if grep -Eq 'Overfull \\[hv]box|undefined references|Citation .* undefined' "$OUT/$(basename "${SOURCE%.tex}").log"; then
  echo "paper log contains overflow or unresolved references" >&2
  exit 1
fi
"$PDFFONTS" "$PDF" >"$OUT/pdffonts.txt"
if awk 'NR > 2 && ($(NF-4) != "yes" || $(NF-3) != "yes" || $(NF-2) != "yes") {bad=1} END {exit bad}' "$OUT/pdffonts.txt"; then
  :
else
  echo "paper PDF contains a non-embedded or non-subset font" >&2
  exit 1
fi
"$PDFTOPPM" -png -r 150 "$PDF" "$OUT/rendered/page"
"$SHASUM" -a 256 \
  "$SOURCE" \
  "$HERE/jacow.cls" \
  "$HERE/results_table.tex" \
  "$HERE/results_macros.tex" \
  "$HERE/figures/best_n_validation_h.png" \
  "$HERE/figures/best_n_validation_v.png" \
  "$HERE/figures/ridge_density_comparison.png" \
  "$HERE/figures/ridge_width_contrast_hv.png" \
  "$HERE/figures/horizontal_loss_diagnostic.png" \
  "$PDF" >"$OUT/source_manifest.sha256"
printf 'paper=%s\npages=%s\npage_size=%sx%s\n' "$PDF" "$pages" "$page_width" "$page_height"
