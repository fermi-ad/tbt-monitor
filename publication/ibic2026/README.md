# IBIC 2026 Publication Package

This directory is the versioned source and final-deliverable home for abstract
54, "Turn-by-turn tune analysis using adaptive BPM ensembles in the Fermilab
Mu2e Delivery Ring."

Final numerical copy and figures are accepted only from roots that pass the
repository's primary, follow-up, Best-N, all-training, intensity, full-buffer ridge, and
corpus-wide raw-payload verifiers. June 2026 downstream figures may be used for
layout development but not as publication evidence.

## Authoritative Inputs

| Input | SHA-256 |
| --- | --- |
| Accepted abstract `abstract-54.pdf` | `e125b5889dbd28e35e17154297a0abb7abd2ce2ec26538a6c7d5301c67b8eea4` |
| Fermilab A0 vertical poster template, May 2025 | `ca9647b1db39860ebdc83854c432842f0dd09b0a7601c8f4af1bd2bf405468a9` |
| Audited one-slide poster starter | `b21f8c2e1d121f0d39ec1428576ae19d7ffdf1dd50a55b0a29df8e195ac8be60` |
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
and source manifest used by the two build pipelines. The manifest hashes the
exact numerical source tables and all 14 materialized outputs; finalization
parses its fixed schema and re-hashes the complete output inventory.
The all-training input must contain the accepted 10000 fold rows, 8000 exact
spill pairs, 16 method/metric comparisons, and 18 native review PNGs under the
same purge and held-out-digitizer protocol as Best-N. Materialization records
selected-favored, all-training-favored, and unresolved counts without requiring
a predetermined winner.
The materializer regenerates the H/V Best-N panels from the accepted summary:
the publication pair contains only blind held-out agreement on a common scale,
while conditioned near-training agreement and an exact criterion-by-N gate
matrix remain separate report assets.
All seven declared beam/fit/fold sensitivity runs must verify. At least four of
seven must produce an eligible recommendation in each plane. A structurally
valid run with no recommendation remains sensitivity evidence: its identity and
reason are preserved in the results payload and its unavailable count appears
in poster, paper, preparation, and compliance copy. The majority gate prevents
one weak reduced sample from being coerced into a false knee while still
blocking publication when a plane lacks reproducible recommendation coverage.
It also requires a passing first-50000-turn audit over the exact 2200-manifest,
263983-captured-position-row, and 23999-paired-row publication corpus, including
the hash-bound 17-row absent-stream inventory from 13 recorded partial captures.
For the two 1000-spill primary collections, generated copy separately reports
the nominal 60 H plus 60 V topology and its 16 source absences across 12 flagged
partial captures. The materializer also selects the accepted H Best-5 and V
Best-12 ridge coverage rows, verifies structural = finite + blank + edge
closure, and generates all eight counts into poster/paper payload and macros.
The paper figure set includes the selected-plane turn-resolved P10-P90 ridge-
width contrast against corrected adaptive Best-1, copied from the same accepted
ridge root. Its source CSV is unsmoothed, its rendered five-window smoothing is
descriptive only, and a zero Best-1 self-control plus every adaptive N-pair row
must pass ridge verification. The wide legacy comparison remains a historical
visual anchor and is not the ensemble-size estimator.
Primary adaptive-score values and intensity-effect counts in manuscript prose
are generated into `results_macros.tex` from the same accepted roots. The macro
set also distinguishes 4000 full-curve spill-plane cases from 1000 stratified
validation cases evaluated across five digitizer folds and binds the six H/V
all-training outcome counts plus selected H/V ridge-coverage counts quoted in
the manuscript.

## Deliverables

The completed directory contains:

- `poster/`: artifact-tool source, content, source manifest, editable A0 PPTX,
  PDF, PDF-derived full-size PNG render, and separate artifact-tool geometry
  preview;
- `paper/`: JACoW TeX source, exact figure files, source manifest, and a
  four-page PDF;
- `LEGACY_RIDGE_PROVENANCE.md`: hashes, origins, protocol, selector caveat, and
  claim boundary for the immutable H/V visual references;
- `publication_manifest.csv`: SHA-256 inventory for every delivered source and
  rendered artifact;
- `compliance_report.md`: verifier state, page/size checks, placeholder and
  overlap audit, and visual-QA disposition.

Generate the last two files with
`scripts/finalize_ibic2026_publication.py` only after inspecting the final
poster and all four paper pages. The finalizer requires explicit `pass` values
for both visual reviews and rechecks the reference hashes, page geometry,
render dimensions, results payload, and final PPTX slide XML. It rejects any
empty structural placeholder without rewriting the OOXML package and records
the accepted zero count in the compliance report. It also recomputes exact
package-relative poster/paper build manifests, the publication materialization
manifest, poster content/asset/output hashes and dimensions, primary/coverage
payload-to-poster/macro bindings, and the delivered
zero-issue template-fidelity report.
`publication_manifest.csv` inventories every file under this directory except
itself.

The separate final local review package must also include
`review-artifacts/poster_candidate_gallery/` as the
`legacy-candidate-gallery` component. That ignored 80-image directory contains
the two immutable H/V visual references named in
`LEGACY_RIDGE_PROVENANCE.md`; it is intentionally absent from the Git/source
archive and therefore cannot be supplied by the Spark-side package alone.
Include `review-artifacts/publication-run-handoff/` as a separate
`run-handoff` component so the accepted abstract, supplied POTX, audited poster
starter/layout, coherent offline Tectonic bundle, exact source archive, guarded
wrappers, and prepared issue/PR handoff text remain checksummed in the local
review package.

The working paper name is `ABSTRACT54` until IBIC assigns a programme code.
Rename the TeX and PDF together when that code becomes available.

## Claim Boundary

The work reports BPM-only tune candidates and internal reproducibility. It does
not claim absolute tune calibration, physical noise removal, or a measured
extraction-onset turn. Ridge subtraction is exact-paired probability
redistribution. Intensity is retained as a tune weight only if the corrected
200-spill test passes every statistical, practical-effect, tune-stability, and
bit-exact Best-1 zero-effect gate. Best-N improvement relative to adaptive Best-1 is reported separately
from the same-protocol all-training mean/median control. That control remains
internal reproducibility evidence and may favor either method or be unresolved.
