# A0 Poster Source

The final poster follows the user-supplied Fermilab A0 vertical May 2025
template. It is authored by exact template duplication and inherited-element
editing with `@oai/artifact-tool`; `python-pptx`, direct OOXML mutation, and a
parallel visual rebuild are not permitted.

## Inputs

- `template-frame-map.json`: validated source-slide and element map;
- `template-audit.txt`: inherited layout, brand, typography, and QA contract;
- `deviation-log.txt`: allowed departures from the source slide;
- `content.json`: final verifier-derived copy and relative paths to five PNGs;
- a prepared `template-starter.pptx` generated from the supplied POTX.

Do not edit `content.json` or copy the five final PNGs by hand. Generate them
with `scripts/prepare_ibic2026_publication.py`, which preserves independent H/V
Best-N choices and records every source hash.

`content.json` must provide the text and asset keys enforced by
`build_poster.mjs`. The builder rejects missing/undersized images, unresolved
copy, and nonnumeric result text. `build_poster.sh` runs that builder and the
complete delivery gate: editable PPTX, one-page A0 PDF, full-size PNG, PDF
raster, layout JSON, template-fidelity report, overflow check, embedded-font
check, and checksums.

## Build

Set `PRESENTATIONS_SKILL_DIR` to the installed Presentations skill and `TMP_DIR`
to an external scratch directory. Inspect the supplied template, validate this
map, and prepare the starter with the skill's template-following scripts. Then
initialize artifact-tool in the scratch directory and copy `build_poster.mjs`
there so its ES-module dependencies resolve.

The reproducible final build uses the bundled Node, Python, LibreOffice, and
Poppler tools. Supply the prepared starter and its layout inventory:

```bash
PRESENTATIONS_SKILL_DIR="$PRESENTATIONS_SKILL_DIR" \
STARTER_PPTX="$TMP_DIR/template-starter.pptx" \
STARTER_LAYOUT_DIR="$TMP_DIR/template-starter-layout" \
NODE="$BUNDLED_NODE" PYTHON="$BUNDLED_PYTHON" \
SOFFICE="$BUNDLED_BIN/soffice" \
PDFINFO="$BUNDLED_BIN/pdfinfo" PDFTOPPM="$BUNDLED_BIN/pdftoppm" \
OUT_DIR="$PWD/publication/ibic2026/poster/build" \
publication/ibic2026/poster/build_poster.sh
```

## Acceptance

Before delivery:

1. run `slides_test.py` with `--pad_px 10` because the default A0 padding can
   exceed PowerPoint's 56-inch limit;
2. run `check_template_fidelity.mjs` against the starter, final PPTX, frame map,
   and final layout;
3. inspect the full-size PNG for hierarchy, axis/legend readability, clipping,
   and unintended overlap;
4. inspect exported slide XML for empty structural placeholders;
5. render the PPTX to PDF and confirm one A0 portrait page with all fonts and
   authentic Fermilab branding preserved.

The complete review gallery is packaged separately. The poster itself uses five
evidence panels: H Best-N, V Best-N, the wide H/V ridge comparison, one selected
spill, and the data-derived H-loss diagnostic.
