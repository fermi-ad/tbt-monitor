# A0 Poster Source

The final poster uses Fermilab's A0 vertical May 2025 scientific-poster
template. The build preserves the template's master artwork and edits mapped
elements in place; direct OOXML mutation and parallel visual rebuilds are not
permitted.

## Paper-Frozen Boundary

The poster is a one-way downstream consumer of the accepted paper and evidence.
`evidence_gate.json` uses schema
`tbt-monitor.ibic2026-poster-evidence-gate/v3` and pins repo-relative paths plus
SHA-256 values for:

- `paper/ABSTRACT54.tex` and `paper/build/ABSTRACT54.pdf`;
- the frozen schema-v2 `results_payload.json`;
- the H and V leakage-controlled Best-N PNGs;
- the exact-paired H/V full-spill ridge PNG; and
- the contextual Muon Campus beamline map.

The gate also requires a structured `mapAttribution` record naming George
Deinlein, Fermilab staff, and recording full permission for this poster's
publication reuse. The poster-only input manifest must preserve that record
exactly, and finalization checks it against the visible credit.
Its `publicationRequirements` record also binds the assigned report number,
current acknowledgment, and official template identity and placement rules.
The generated
`tbt-monitor.ibic2026-poster-inputs/v2` manifest preserves this record exactly.

While this gate exists, `scripts/prepare_ibic2026_publication.py` intentionally
refuses to co-generate the paper and poster. Poster wording, layout, and context
must not modify the manuscript or paper PDF. Retire and replace the gate only
for a serious discrepancy in the paper or accepted evidence.

Materialize the poster inputs from the repository root:

```bash
PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache \
  python3 scripts/prepare_ibic2026_poster.py
```

The command validates every pinned hash and writes only under this directory:

- `content.json`: concise verifier-derived copy and structured evidence;
- `assets/best_n_validation_h.png`;
- `assets/best_n_validation_v.png`;
- `assets/ridge_density_comparison.png`;
- `assets/muon-campus-beamlines.png`; and
- `input_manifest.json`: gate/input/output hashes, PNG dimensions, contextual
  provenance, and before/after paper hashes.

Do not hand-edit `content.json` or manually replace the four bound assets. The
materializer requires H Best-5 and V Best-12; the exact 9.1%/8.9% and
26.3%/18.3% observed/null values; all 60 H and all 60 V Best-1 winner coverage;
a narrower selected H ridge; non-unique H/V sensitivity ranges; and at least
one selected-favored plus one all-training-favored comparison in each plane.
It preserves the structured primary-capture and ridge-coverage evidence used by
finalization.

The H/V frames show blind full-band selected-versus-held-out agreement on one
shared zero-based scale with the deterministic cross-spill null band.
Conditioned near-training agreement remains a review diagnostic and is not
overlaid as equivalent evidence. No intensity source role, sentence, evidence
field, or figure is permitted in poster materialization.

The beamline map is credited exactly as: "Beamline layout courtesy of George
Deinlein, Fermilab staff; used with permission." Full permission for this
poster's publication reuse was confirmed on 2026-08-19. The map was extracted
from slide 2 of the source deck recorded in the gate context. The external deck
need not be present to rebuild the poster; the copied PNG hash is the required
input. The map provides machine context only and is not an independent tune
measurement or calibration reference.

## Graphics-First Layout

The poster deliberately uses little prose and exactly four graphics:

1. the dominant 50,000-turn H/V ridge comparison;
2. the H held-out Best-N validation panel;
3. the V held-out Best-N validation panel; and
4. a secondary beamline map locating the Delivery Ring.

The visible narrative is carried by short questions and claims: there is no
single best BPM; choose early and test later; ask whether the candidate
persists; and distinguish useful operating points from universal optima. The
conclusion keeps the mixed all-training result explicit and identifies a
controlled quadrupole scan as the next calibration step. Secondary width,
H-loss, conditioned-agreement, sensitivity, and selected-spill graphics remain
in the paper or review gallery instead of being shrunk into the A0 poster.

`content.json` must provide the exact text fields enforced by
`build_poster.mjs`: `title`, `subtitle`, `author`, `reportNumber`,
`acknowledgment`, `mapCaption`, `mapCredit`, `methodHeading`, `methodBody`,
`bestNHCaption`, `bestNVCaption`, `ridgeHeading`, `conclusionHeading`, and
`conclusionBody`, plus the four asset roles and existing structured evidence.
The builder rejects missing/undersized images, unresolved copy, and nonnumeric
result text.

## Fermilab Publication Requirements

The [official Fermilab template page](https://www.fnal.gov/faw/designstandards/templates/index.html)
was rechecked on 2026-08-19. Its A0 vertical download still resolves to
[`FNAL_Scientific_Poster_A0_VRT_May25.potx`](https://www.fnal.gov/faw/designstandards/filesfordownload/FNAL_Scientific_Poster_A0_VRT_May25.potx),
the template already bound by hash in this package.

The visible poster must preserve both required placements:

- upper-right blue header: `FERMILAB-POSTER-26-0268-AD`;
- lower-left footer: "This manuscript has been authored by FermiForward
  Discovery Group, LLC under Contract No. 89243024CSC000002 with the U.S.
  Department of Energy, Office of Science, Office of High Energy Physics."

These are poster-only publication metadata. Updating or validating them does
not reopen the accepted manuscript, paper PDF, or scientific evidence gate.

## Build

Set `PRESENTATIONS_SKILL_DIR` to the installed Presentations skill,
`STARTER_PPTX` to the audited prepared template starter, and
`STARTER_LAYOUT_DIR` to its layout inventory. Set `TMP_DIR` to an external
scratch directory and `RUNTIME_NODE_MODULES` to the bundled Node packages
directory supplied by the workspace dependency runtime; the build links that
directory into its scratch workspace. No project-local dependency installation
is required.

```bash
PRESENTATIONS_SKILL_DIR="$PRESENTATIONS_SKILL_DIR" \
STARTER_PPTX="$TMP_DIR/template-starter.pptx" \
STARTER_LAYOUT_DIR="$TMP_DIR/template-starter-layout" \
RUNTIME_NODE_MODULES="$BUNDLED_NODE_MODULES" \
NODE="$BUNDLED_NODE" PYTHON="$BUNDLED_PYTHON" \
SOFFICE="$BUNDLED_BIN/soffice" \
PDFINFO="$BUNDLED_BIN/pdfinfo" PDFTOPPM="$BUNDLED_BIN/pdftoppm" \
PDFFONTS="$BUNDLED_BIN/pdffonts" \
WORK="$TMP_DIR/ibic2026-poster-build" \
OUT_DIR="$PWD/publication/ibic2026/poster/build" \
publication/ibic2026/poster/build_poster.sh
```

The builder emits an editable PPTX, one-page A0 PDF, full-size PNG copied from
the 150 dpi PDF raster, separate artifact-tool geometry preview, layout and
overflow inspection, font report, template-fidelity reports, portable
checksums, and a schema-v2 source manifest. That manifest binds
`evidence_gate.json`, `input_manifest.json`, `content.json`, all four assets,
the audited starter, and the primary editable/diagnostic outputs.

## Acceptance

Before delivery:

1. run `scripts/test_prepare_ibic2026_poster.py` and rematerialize the poster;
2. confirm `input_manifest.json` records identical before/after hashes for both
   the frozen paper source and PDF;
3. run `slides_test.py` with `--pad_px 10` because the default A0 padding can
   exceed PowerPoint's 56-inch limit;
4. run `check_template_fidelity.mjs` against the starter, final PPTX, frame map,
   and final layout;
5. inspect the PDF-derived full-size PNG at native scale for hierarchy, map
   credit, report number in the upper-right blue header, acknowledgment in the
   lower-left footer, axis/legend readability, clipping, and unintended overlap;
6. confirm the PDF is one A0 portrait page, every font is embedded/subset, the
   named PNG is byte-identical to the 150 dpi PDF raster, and the PPTX contains
   zero empty structural placeholders; and
7. run `scripts/finalize_ibic2026_publication.py` with explicit poster and paper
   visual-QA passes so it revalidates the gate, poster input/source manifests,
   payload-to-poster evidence, portable manifests, and all final artifacts.

The complete indexed review gallery remains separately packaged. It contains
the secondary diagnostics and legacy-selector audit without weakening the
four-graphic poster hierarchy.

## Publication status

The final poster is approved for technical publication and has been printed for
presentation at IBIC 2026.
