# Current Status

Last updated: 2026-08-28.

## Publication status

The IBIC 2026 paper and poster are finalized and approved for technical
publication. The poster has been printed for presentation. The accepted title
is *Turn-by-turn tune analysis using adaptive BPM ensembles in the Fermilab
Mu2e Delivery Ring*.

Final deliverables:

- [Proceedings paper WEP014](../publication/ibic2026/paper/build/WEP014.pdf)
- [Poster PDF](../publication/ibic2026/poster/build/ibic2026-abstract54-poster.pdf)
- [Editable poster](../publication/ibic2026/poster/build/ibic2026-abstract54-poster.pptx)
- [Publication package](../publication/ibic2026/README.md)

The completed intensity study remains a separately verified technical sidecar
and is not part of the paper or poster claim.

## Accepted results

- Selected operating points: H Best-5 and V Best-12.
- Full Best-N curve: 4,000 spill-plane cases through N=40.
- Digitizer-disjoint validation: 1,000 stratified cases across five folds.
- Cross-collection transfer: 4/4 rows accepted.
- Reduced-sample sensitivity: H produced an eligible choice in 5/7 runs over
  N=2-13; V did so in 6/7 runs over N=10-28.
- H Best-5 minus corrected Best-1 ridge IQR: `-0.00243472`, with 95% interval
  `[-0.00307726, -0.00192525]`.
- V Best-12 minus corrected Best-1 ridge IQR: `+0.00102365`, with 95% interval
  `[0.00038709, 0.00165424]`.
- Same-protocol selected/all-training/unresolved comparisons: H `2/3/3`; V
  `3/3/2`.
- All 60 sources in each plane win Best-1 at least once. The largest winner
  frequencies are 3.7% H and 5.7% V.
- The standalone intensity sidecar contains 240 paired effects, with no
  FDR-significant or practically retained weighting effects.

H Best-5 and V Best-12 are useful operating points on broad performance
plateaus, not unique or universal optima. The vertical plane has the clearest
digitizer-disjoint support. Horizontal evidence is weaker, but H Best-5
produces a narrower corrected-Best-1 ridge distribution. All-training
aggregation remains a competitive control.

## Data coverage

- Primary captures: 2,000 spills, nominally 60 H plus 60 V channels.
- Primary completeness: 12 partial captures and 16 recorded source absences.
- Audited position rows: 239,984 across two complete 30-digitizer topologies.
- Every audited first-50,000-turn raw payload passes finite-data, plateau, and
  device-coded fallback checks.
- Selected full-buffer structural rows per plane: 360,000.
- H Best-5: 359,018 finite ridge picks, 14 blank-confidence rows, and 968
  bounded edge exclusions.
- V Best-12: 289,210 finite ridge picks, 69,684 blank-confidence rows, and
  1,106 bounded edge exclusions.
- No absent primary source intersects accepted selected membership.

Missing channels remain missing; they are never fabricated or zero-filled.
Finite, blank-confidence, and edge-excluded ridge states remain distinct.

## Interpretation

The supported claim is:

> Adaptive BPM ensembles recover internally repeatable tune-like structure,
> with the strongest channel-disjoint evidence in V and a measurable H
> concentration gain relative to corrected Best-1.

The analysis does not establish:

- absolute or externally calibrated tune accuracy;
- physical noise removal from ridge-density differences;
- a universal Best-N advantage over all-training aggregation;
- a causal extraction time or fixed extraction-onset turn; or
- a finite tune value for unavailable ridge picks.

H Best-5 only just clears its pointwise cross-spill null band, while V Best-12
is clearly separated. Ensemble size also changes under reduced samples and gate
margin choices. Score weights, spectral-window length, and agreement tolerance
remain open sensitivities. A matched external tune reference or controlled
quadrupole scan is required for absolute validation.

## Verification

- Rust formatting and all 44 Rust tests pass.
- The Best-BPM suite passes 89 tests with six expected process-pool sandbox
  skips in the local environment.
- All eight poster-materializer tests pass.
- Publication preparation binds the selected sizes, position audit, null and
  membership controls, ridge coverage, tables, and figures to the accepted
  result payload.
- Finalization verifies the four-page paper, one-page A0 poster, embedded fonts,
  manifests, artifact dimensions, source hashes, and explicit visual QA.

See the [publication README](../publication/ibic2026/README.md) for deliverable
and reproducibility details.
