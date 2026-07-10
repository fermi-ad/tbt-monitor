# IBIC 2026 Publication Package

This directory is the versioned source and final-deliverable home for abstract
54, "Turn-by-turn tune analysis using adaptive BPM ensembles in the Fermilab
Mu2e Delivery Ring."

Final numerical copy and figures are accepted only from roots that pass the
repository's primary, follow-up, Best-N, intensity, full-buffer ridge, and
corpus-wide raw-payload verifiers. June 2026 downstream figures may be used for
layout development but not as publication evidence.

## Authoritative Inputs

| Input | SHA-256 |
| --- | --- |
| Accepted abstract `abstract-54.pdf` | `e125b5889dbd28e35e17154297a0abb7abd2ce2ec26538a6c7d5301c67b8eea4` |
| Fermilab A0 vertical poster template, May 2025 | `ca9647b1db39860ebdc83854c432842f0dd09b0a7601c8f4af1bd2bf405468a9` |
| JACoW class v3.01, 2026-03-11 | `ad1f381eb56b22cac36f59b0947ae5ef20b8dcdc56f4867434754fb698347d0d` |

The accepted abstract and Fermilab template are user-supplied reference files
and are not duplicated here. The final editable poster inherits the supplied
template's master, header, footer, typography, and evidence frames.
Git normalizes the downloaded class's CRLF line endings and removes trailing
horizontal whitespace; the tracked form is semantically identical and has
SHA-256
`e902c3c4ff34a98604d17ba3dd44989b9ed6c042bfdd179eb4f1b700515f291c`.

`scripts/prepare_ibic2026_publication.py` is the numerical and figure
materialization gate. It accepts only verifier-clean analysis roots and writes
the exact poster JSON, paper table/figures, results payload, preparation report,
and source manifest used by the two build pipelines.
It also requires a passing first-50000-turn audit over the exact 2200-manifest,
263999-position-row, and 23999-paired-row publication corpus.
The paper figure set includes the selected-plane turn-resolved P10-P90 ridge-
width contrast copied from the same accepted ridge root; its source CSV is
unsmoothed and its rendered five-window smoothing is descriptive only.
Primary adaptive-score values and intensity-effect counts in manuscript prose
are generated into `results_macros.tex` from the same accepted roots. The macro
set also distinguishes 4000 full-curve spill-plane cases from 1000 stratified
validation cases evaluated across five digitizer folds.

## Deliverables

The completed directory contains:

- `poster/`: artifact-tool source, content, source manifest, editable A0 PPTX,
  PDF, and full-size PNG render;
- `paper/`: JACoW TeX source, exact figure files, source manifest, and a
  four-page PDF;
- `publication_manifest.csv`: SHA-256 inventory for every delivered source and
  rendered artifact;
- `compliance_report.md`: verifier state, page/size checks, placeholder and
  overlap audit, and visual-QA disposition.

Generate the last two files with
`scripts/finalize_ibic2026_publication.py` only after inspecting the final
poster and all four paper pages. The finalizer requires explicit `pass` values
for both visual reviews and rechecks the reference hashes, page geometry,
render dimensions, and results payload. `publication_manifest.csv` inventories
every file under this directory except itself.

The working paper name is `ABSTRACT54` until IBIC assigns a programme code.
Rename the TeX and PDF together when that code becomes available.

## Claim Boundary

The work reports BPM-only tune candidates and internal reproducibility. It does
not claim absolute tune calibration, physical noise removal, or a measured
extraction-onset turn. Ridge subtraction is exact-paired probability
redistribution. Intensity is retained as a tune weight only if the corrected
200-spill test passes every statistical, practical-effect, and tune-stability
gate.
