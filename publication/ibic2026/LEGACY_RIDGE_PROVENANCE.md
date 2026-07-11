# Legacy Ridge-Density Provenance

This note binds the two archived `18d321dbd4fe` plots chosen as the visual
reference for the final adaptive-ensemble comparison.

## Immutable Local References

| Plane | Local file | SHA-256 |
| --- | --- | --- |
| H | `review-artifacts/poster_candidate_gallery/05_best_poster_18d321db_ridge_density_h.png` | `07413bd1995276de362b923e70cda6cb91e4bfd01c18049371b6a94d87a467bd` |
| V | `review-artifacts/poster_candidate_gallery/05_best_poster_18d321db_ridge_density_v.png` | `c48f26d3a30476ff8ed7b888db41ef9a56e9951a085dd7ec6b8ea960480f12fc` |

They were copied without modification from:

```text
/home/derekste/tbt-spills-2000-autosweep/elite-full/jobs/18d321dbd4fe/combined/ridge_density_h.png
/home/derekste/tbt-spills-2000-autosweep/elite-full/jobs/18d321dbd4fe/combined/ridge_density_v.png
```

The archived PNGs remain immutable historical outputs. Their old integer-cell
raster geometry is not silently rewritten; corrected future renders use the
full-axis proportional mapping required by the current verifier.

## Generation Protocol

`scripts/gpu_analyze_captured_spills.py` generated the plots from
`gpu_sliding_tune.csv` with:

```text
config hash: 18d321dbd4fe
turn range: 0-50000
window/stride: 4096/256 turns
window function: Hann
BPM mode: legacy normalized single
BPM normalization: RMS per BPM
detrend: mean subtraction
DC handling: zero DC bin
injection seed: 4096 turns
minimum peak confidence: 2.0
tracking half-width: 0.005
maximum tune step per window: 0.005
H tune band: 0.620-0.680
V tune band: 0.690-0.740
usable spills: 1988
sliding rows: 715680
```

For every accepted spill and window, the file contains one continuity-tracked
`selected_tune`. The PNG bins those picks by turn and tune. Color is spill
count; white curves are across-spill percentile/median tracks. It is not a
spectral-power heatmap.

## Selector Caveat

The historical `best_single_bpm` name overstates what this run selected. It
ranked trace RMS after `rms_per_bpm` normalization, when nominal RMS was one for
every channel, so floating-point residuals could choose the member. An evenly
spaced 400-manifest audit found agreement with the pre-normalization raw-RMS
leader in only 1.25% of H rows and 1.00% of V rows; median selected raw-RMS rank
was 29/60 in H and 31/60 in V. These panels must therefore be called the
**legacy normalized-single** reference, not the highest-RMS or optimized
single-BPM result.

## Fair Adaptive Comparison

The final full-buffer sidecar holds the archived windowing, tune bands,
tracking geometry, density bins, and exact common spill/window population
fixed. It renders three distinct comparisons:

1. legacy normalized single versus selected adaptive Best-N, which includes
   both selector repair and ensemble-size effects;
2. corrected Best-1 versus selected adaptive Best-N, which isolates the effect
   of using more than one corrected adaptive member;
3. legacy/corrected-Best-1/selected-Best-N together, which exposes both steps.

Standalone panels disclose their nonzero-P98 display clip. Subtractive panels
use a symmetric absolute-P99 display clip and exact common finite points. Red
and blue mean higher or lower column-normalized ridge-pick probability only.
Neither a narrower ridge nor a subtractive feature establishes physical noise
removal, absolute tune accuracy, or extraction onset.
