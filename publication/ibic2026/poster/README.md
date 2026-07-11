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
- the audited prepared `template-starter.pptx` generated from the supplied
  POTX, SHA-256
  `b21f8c2e1d121f0d39ec1428576ae19d7ffdf1dd50a55b0a29df8e195ac8be60`.

Do not edit `content.json` or copy the five final PNGs by hand. Generate them
with `scripts/prepare_ibic2026_publication.py`, which preserves independent H/V
Best-N choices, requires the exact corpus-wide raw-payload audit, and records
every source hash, including the manifest and 17-row absent-stream inventories.
The generated copy distinguishes the full Best-N curve
population from the smaller stratified held-out validation population. It also
prints the eligible-count and N range from seven verified sensitivity runs;
unavailable reduced-sample knees remain explicit rather than being assigned N.
Conclusion copy must separate Best-N improvement over adaptive Best-1 from the
same-protocol all-training control and report the H/V selected-favored and
all-training-favored counts. An unresolved or baseline-favored result is not
removed to simplify the poster story.
The H/V Best-N frames use blind full-band selected-versus-held-out agreement on
one shared zero-based scale. Do not substitute or overlay conditioned
near-training agreement in those frames.

`content.json` must provide the text and asset keys enforced by
`build_poster.mjs`. The builder rejects missing/undersized images, unresolved
copy, and nonnumeric result text. `build_poster.sh` runs that builder and the
complete delivery gate: editable PPTX, one-page A0 PDF, full-size PNG, PDF
raster, delivered layout JSON, zero-issue template-fidelity JSON/text reports,
PPTX overflow inspection, embedded-font report, and portable checksums.
The named full-size PNG is copied from the 150 dpi PDF raster and must remain
byte-identical to it. This preserves master-level Fermilab/DOE artwork that the
artifact-tool geometry preview does not render; the direct artifact preview is
retained separately as `*-artifact-preview.png` for layout diagnostics.

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
3. inspect the PDF-derived full-size PNG for hierarchy, authentic footer
   branding, axis/legend readability, clipping, and unintended overlap;
4. run the publication finalizer's read-only slide-XML gate and confirm the
   compliance report records zero empty structural placeholders;
5. confirm the finalizer recomputes the delivered source/deliverable manifests
   and zero-issue fidelity report, and validates every materialized output in
   the publication-level `source_manifest.csv`;
6. render the PPTX to PDF and confirm one A0 portrait page with all fonts and
   authentic Fermilab branding preserved.

The complete review gallery, including selected-spill examples, is packaged
separately. The poster itself uses five population-level or leakage-controlled
evidence panels: H Best-N, V Best-N, the wide H/V ridge comparison, the stacked
selected-H/V P10-P90 width contrast, and the data-derived H-loss diagnostic.
The ridge pipeline emits a dedicated 800x1250 portrait contrast for the
upper-right inherited frame; the paper uses the corresponding landscape image.
