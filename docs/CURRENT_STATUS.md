# Current Status

Last updated: 2026-07-11.

## Executive Status

The IBIC 2026 analysis and publication artifacts are scientifically and
technically complete. The accepted source-bound Spark materialization selects
H Best-5 and V Best-12, the Fermilab-template A0 poster and four-page JACoW
paper are built from those exact outputs, and the final publication audit
passes.

The result is strong enough for the poster when stated as an internal BPM
reproducibility result. It does not establish absolute tune accuracy. The
vertical plane has the clearest digitizer-disjoint support. Horizontal evidence
is weaker and loses concentration earlier, but selected Best-5 produces a
narrower corrected-Best-1 ridge-pick distribution. The V Best-12 ridge width is
slightly broader than corrected Best-1, so the publication does not call the
ensemble a universal denoiser.

Repository presentation closeout is complete. The checksummed local review
bundle is under `review-artifacts/final-ibic2026-review-20260711`, PR #53 is the
scoped merge vehicle, and the superseded autosweep branch/worktree and preserved
handoff stash are removed only after their equivalence checks.

## Accepted Results

- Selected ensemble sizes: H Best-5; V Best-12.
- Full Best-N curve: 4,000 spill-plane cases through N=40.
- Digitizer-disjoint validation: 1,000 stratified cases across five folds.
- Cross-collection transfer: 4/4 rows accepted.
- Reduced-sample sensitivity: H eligible in 5/7 runs over N=2-13; V eligible
  in 6/7 over N=10-28. Unresolved runs remain visible evidence.
- H selected Best-5 minus corrected Best-1 ridge IQR:
  `-0.00243472`, 95% interval `[-0.00307726, -0.00192525]`.
- V selected Best-12 minus corrected Best-1 ridge IQR:
  `+0.00102365`, 95% interval `[0.00038709, 0.00165424]`.
- Same-protocol selected/all-training/unresolved comparisons: H `2/3/3`; V
  `3/3/2`. The all-training baseline remains explicit.
- Intensity study: 240 paired effects; 0 FDR-significant, 0 practically
  retained, and 0 weighting effects accepted.
- Horizontal tracking-loss candidates: 5,632/5,760, retained only as a
  turn-dependent noncausal diagnostic. No fixed extraction-onset turn is used.

## Data Coverage

- Primary captures: 2,000 spills, nominally 60 H plus 60 V channels.
- Primary completeness: 12 flagged partial captures and 16 source absences.
- Full payload audit: 2,200 manifests, 263,983 position rows, 23,999 exact
  position/intensity pairs, 13 partial captures, and 17 recorded absences.
- Every audited first-50,000-turn raw payload passes finite-data, plateau, and
  device-coded fallback checks.
- Selected full-buffer structural rows per plane: 360,000.
- H Best-5 ridge picks: 359,018 finite, 14 blank, 968 bounded edge exclusions.
- V Best-12 ridge picks: 289,210 finite, 69,684 blank, 1,106 bounded edge
  exclusions.
- No absent primary source intersects accepted selected membership.

## Publication Artifacts

Poster:

- Editable PPTX:
  `publication/ibic2026/poster/build/ibic2026-abstract54-poster.pptx`
- A0 PDF:
  `publication/ibic2026/poster/build/ibic2026-abstract54-poster.pdf`
- Full-size 150 dpi PNG:
  `publication/ibic2026/poster/build/ibic2026-abstract54-poster.png`
- Geometry: one `2383.26 x 3369.63 pt` page; PNG `4966 x 7021`.
- QA: zero overflow, zero template-fidelity issues, zero empty structural
  placeholders, exact portable source/deliverable manifests, embedded/subset
  fonts, and native-scale visual inspection passed.

Paper:

- Source: `publication/ibic2026/paper/ABSTRACT54.tex`
- Final PDF: `publication/ibic2026/paper/build/ABSTRACT54.pdf`
- Geometry: exactly four `595 x 792 bp` pages.
- QA: no overfull boxes or unresolved references; all fonts are embedded,
  subset, and Unicode-mapped; all four final renders passed visual inspection.

Binding and compliance:

- Machine-readable result payload:
  `publication/ibic2026/results_payload.json`
- Source materialization inventory:
  `publication/ibic2026/source_manifest.csv`
- Final compliance report:
  `publication/ibic2026/compliance_report.md`
- Complete 69-file publication inventory:
  `publication/ibic2026/publication_manifest.csv`
- Accepted abstract and Fermilab POTX hashes are rechecked by the finalizer.

## Visual Interpretation

The favorite legacy H/V ridge images are densities of one continuity-tracked
tune-ridge pick per spill and sliding window, not spectral-power heat maps.
Color is cross-spill ridge-pick count or normalized probability; white curves
are cross-spill percentile tracks. Their historical selector was effectively a
floating-point residual winner after per-BPM RMS normalization, so it is labeled
`legacy normalized-single`, not `best BPM`.

The wide legacy-versus-selected panel is useful as a visual anchor but combines
selector repair with ensemble size. The corrected-Best-1-versus-selected width
panel is the isolating ensemble-size comparison. Difference colors represent
exact-paired ridge-pick probability redistribution, not removed physical noise.

## Verification

- Exact e433 source: all 81 Best-BPM tests pass on Spark.
- Local Best-BPM suite: completes with six expected process-pool sandbox skips.
- Autosweep tests: 9/9 pass.
- Rust tests: 44/44 pass.
- GPU analyzer and poster/DGX self-tests: pass.
- Autosweep v2 acceptance smoke: 6/6 jobs; exactly two analyzer-bound PIDs;
  3.927 seconds of overlap; memory floor preserved.
- Spark publication preparation: selected H/V sizes `5/12`, 49 copied files,
  exact source hashes, and 14 required materialized outputs.
- Publication finalizer: 69 files verified and inventoried.
- `git diff --check`: pass before closeout.

## Claim Boundary

The accepted publication claim is:

> Adaptive BPM ensembles recover internally repeatable tune-like structure,
> with the strongest channel-disjoint evidence in V and a measurable H
> concentration gain relative to corrected Best-1.

Do not claim:

- absolute or externally calibrated tune accuracy,
- physical noise removal from subtractive density plots,
- a universal Best-N advantage over all-training aggregation,
- causal extraction timing or a fixed extraction-onset turn,
- that unavailable ridge picks are zero-valued measurements.

External Schottky/tune-meter comparison, controlled tune-knob scans, and
machine-state labels remain future validation work rather than blockers for the
BPM-only IBIC poster.

## Handoff State

- Publication branch: `dev/ibic2026-final-delivery` (merged through PR #53).
- Source-bound publication commit: `e43348318b6ff96b7570181e2eedda2737a4b3c9`.
- Publication PR: #53.
- Spark materialization archive SHA-256:
  `6cb27a5c36fa738861590f97884f3d1dfdf2dad8231b2c3e55b79892d2cffc4d`.
- Complete accepted Spark review package is local under
  `review-artifacts/spark-final-e5707035/` and independently verifies.
- Final local review package:
  `review-artifacts/final-ibic2026-review-20260711` plus its `.tar.gz` archive,
  archive SHA-256 sidecar, and package verification receipt.
- Final repository state: `main` synchronized with `origin/main`; superseded
  autosweep branch/worktree and the preserved Task-F stash removed after the
  documented equivalence audit.
