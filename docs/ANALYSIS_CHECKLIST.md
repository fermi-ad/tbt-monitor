# ANALYSIS_CHECKLIST
Remaining Implementation Checklist for Physics Validation

This checklist tracks remaining analysis-quality work. Implemented command
usage lives in `docs/USAGE.md`; implementation status and data flow live in
`docs/PLAN.md` and `docs/ARCHITECTURE.md`.

## 1. Already Implemented (Removed from TODO)

Treat these as baseline rather than open TODOs:

- per-spill tune artifacts, spectrograms, validation composite, and
  `sliding_tune.csv`
- flashpoint sampling (`--flashes N|max`) and per-flash batch trend/histogram
  artifacts
- batch records, plots, composite waterfalls, summaries, quality labels, and
  explicit `quality_flags`
- robustness-study artifacts from `analyze-phase`
- external reference matching and residual plotting in batch mode
- standalone BPM-only poster/DGX manifest, baseline, flash, trace-density,
  weak-label quality, benchmark, and poster-index products
- raw captured-spill CuPy/CUDA analysis from payload bundles, including
  same-grid flash summaries, median band spectrograms, ridge-density plots,
  representative Hann/multitaper spectrogram overlays, dynamic-programming
  ridge traces, optional SVD/PCA denoising plots, and DGX benchmarks
- staged Spark BPM autosweep over raw position bundles, including Stage 0
  manifest/health/cache outputs, deterministic pilot/full config grids,
  weighted ranking, spill/config labels, top full-stage config lists, and
  `initial_analysis_summary.md`
- Best-BPM mining pass over the 2000-spill Spark dataset, including per-BPM
  spectra/features, within-spill consensus, exact best-1/best-3 searches,
  screened-pool audited best-5 search, fixed-vs-dynamic statistics,
  morphology clustering, selected artifacts, and final reports
- Best-BPM follow-up sidecars for same-metric dynamic/fixed/all-BPM
  recomputation, held-out spectral support, handoff/visibility review, curated
  plane-balanced poster PNGs, and full-buffer Best-1/3/5 ridge-density
  comparison artifacts
- exact-cache visibility-duration repair plus semantic verification of result
  identities, fixed/held-out controls, handoff transitions, poster balance,
  and recommended PNG payloads
- corrected exact-channel identity and ring-order reconstruction across the
  primary and every downstream sidecar
- contiguous leakage-controlled Best-N curves with purged later-window and
  digitizer-disjoint validation, block intervals, beam/fit/fold/block-length
  sensitivity comparisons, cross-collection global-N transfer, and a strict
  verifier for contiguous coverage, identities, timing, metrics, summaries,
  recommendation boundaries, and plots
- an initial exact-pair intensity weighting/covariate study over 199 complete
  spills; that provisional block-aware result rejects intensity weighting and retains only
  integrity and exploratory timing diagnostics. A corrected all-zero gate
  fallback plus an explicit no-usable-intensity unweighted fallback restore N=1
  invariance and require one final 200-spill refresh.
- strict intensity verification of the audited pair count, complete method/turn
  grids, payload horizon, exact N=1 equality, effect decisions, and gallery
  assets; 10/20/40 blocks must retain the same exact effect identities

## 2. Deferred Production Enhancements (Not Publication TODO)

The following items remain useful production/Rust enhancements, but they are
not missing tasks for the current BPM-only IBIC publication. The publication
uses the implemented Spark sidecars and records the absence of an external tune
reference as a limitation rather than silently treating these as complete.

### 2.1 Aggregate median spectrogram

Add:

- `median_spectrogram_h.png`
- `median_spectrogram_v.png`

Method:

- accumulate same-grid sliding spectra across accepted spills
- compute median power per `[window, tune_bin]`
- overlay median tracked tune path

Current poster helper note:

- `scripts/bpm_dgx_poster.py` can emit `median_spectrogram_h/v.png` as
  selected-tune trace-density products from existing summaries.
- `scripts/gpu_analyze_captured_spills.py` emits median band-spectrogram PNGs
  from raw payload FFT power for same-grid flash runs.
- Full median spectral-power products still require same-grid spectral arrays
  from offline analysis outputs.

### 2.2 BPM subset consistency checks

Add:

- `subset_consistency_h.png`
- `subset_consistency_v.png`

Modes:

- all BPMs
- odd/even split
- first/second half split
- random fixed-size subsets

Current poster helper note:

- `scripts/bpm_dgx_poster.py` emits conservative spill-subset proxy outputs
  when only summary/ranking artifacts are present.
- True per-BPM subset checks still require per-BPM spectra or `analyze-phase`
  study artifacts.

### 2.3 Best-BPM vs all-BPM spectrum comparison plot

Add:

- `spill_<id>_spectrum_compare_h.png`
- `spill_<id>_spectrum_compare_v.png`

Compare:

- best single BPM
- all-BPM unweighted average
- all-BPM weighted average

### 2.4 FFT resolution metadata in summaries

Add summary fields:

- `fft_points`
- `tune_bin_spacing`
- `peak_refinement_method`

### 2.5 Optional frequency-axis cross-check mode

Add optional outputs:

- `spill_<id>_spectrum_h_freq.png`
- `spill_<id>_spectrum_v_freq.png`

Config keys to consider:

- `revolution_frequency_hz`
- `plot_frequency_axis`

### 2.6 Physics-specific summary artifact

Add:

- `physics_summary.md`

Include:

- total spills analyzed
- spills passing stricter physics cuts
- `Qx/Qy` injection medians and stddev
- representative spill IDs and key outliers
- tracking fallback/suspicious-step notes

### 2.7 Dedicated physics-usable classification

Keep existing `status` and `quality_label`, and add optional:

- `physics_usable` flag (stricter than generic analysis success)

### 2.8 Intensity-assisted quality study (provisional baseline)

The June sidecar used exact `TBT_POSITION_RAW`/`TBT_INTENSITY_RAW` pairs,
position-only member selection, purged later windows, and collection-aware
moving blocks. Its numbers below are a diagnostic baseline until the corrected
200-spill refresh passes the strict verifier.

Resolved findings:

- the first 50000 turns are structurally valid for inference; the advertised
  tail becomes unreliable near turn 64000 and is an integrity finding
- 240 paired method-effect tests yield zero FDR-significant directional effects
  within tune tolerance and zero practical effects with 20-spill blocks
- square-root, linear, and gated intensity weighting are rejected for tune
  extraction
- lag and crossing-turn plots remain exploratory and do not identify a fixed
  extraction boundary or establish causation

Current autosweep note:

- `scripts/rank_autosweep_results.py` emits BPM-only `GOOD`, `MARGINAL`,
  `BAD`, `NO_SIGNAL`, `AMBIGUOUS_RIDGE`, and `MISSING_DATA` spill labels, plus
  config labels for ranking. These are offline ranking labels, not yet the
  production Rust `physics_usable` field.
- `scripts/build_elite_full_stage.py` filters the full Tier A stage to
  `spill_health.csv` usable spills and preserves rejected/flagged configs in
  diagnostics before full-data reruns.

## 3. Plot Usability Gate (Current Publication)

Every final publication plot must satisfy:

- explicit axis labels and units where available
- subtitles with spill ID/target ms, window parameters, and BPM counts
- consistent naming conventions
- readable annotation density/resolution
- confidence intervals or an explicit descriptive-only label where inference
  is not supported
- exact support counts and point-pairing disclosure for comparisons
- no extraction marker in the primary plot; broad review context may appear
  only in a separately named variant
- a verifier-accepted `run_contract.json` binding the figure rows to exact
  source inventories, scientific parameters, and complete compatible shards
- a semantic verifier proving that the depicted metric comes from its named
  table rather than a reused placeholder series; blank or constant scientific
  panels fail even when a PNG exists

## 4. Current Publication Execution Order

1. complete and verify the corrected exact-identity Best-1/3/5 run and every
   fixed/held-out/handoff/artifact/report sidecar, including the exact-cache
   visibility-duration repair
2. complete the contiguous Best-N curve, beam/fit/fold sensitivities, and
   cross-collection transfer; require the full and seven-run sample outputs to
   pass `verify_best_n_outputs.py`
3. render the exact-point-paired 50000-turn legacy/Best-N ridge gallery and
   inspect H-loss diagnostics without forcing an extraction onset; require the
   strict spill/window/pair/figure verifier to pass
4. freeze the executive interpretation and issue #39 deficiency disposition
5. build and visually verify the Fermilab-template A0 poster and four-page JACoW
   paper
6. package the exhaustive review gallery and curated publication source bundle,
   then merge scoped PRs and leave a clean repository

SVD/PCA remains deferred for production Rust tune extraction. The standalone
poster analyzer can already produce opt-in representative-spill SVD/PCA
comparison plots for physics review.

## 5. Current Review Deliverables

Required for the publication review:

- corrected verifier and follow-up-verifier reports
- Best-N summary, sensitivity, and cross-collection transfer tables/plots
- exact-point-paired legacy-versus-adaptive H/V ridge panels, one shared-scale
  four-panel H/V comparison for every requested N, and subtractive
  redistribution maps
- H-plane concentration, width, entropy, confidence, fallback, and data-derived
  loss-candidate diagnostics
- block-aware intensity result tables and indexed review gallery
- editable A0 poster, poster PDF/render, four-page paper source/PDF, source data,
  checksums, commands, captions, and compliance notes

Optional but useful:

- `spectrum_compare_h.png`
- `spectrum_compare_v.png`
- `*_freq.png` spectra with revolution-line annotations
- standalone poster-analysis `ridge_density_h/v.png`,
  `method_comparison_h/v.png`, and optional `svd_*` products from raw captured
  bundles
- autosweep `initial_analysis_summary.md`, `autosweep_ranked_configs.csv`,
  `autosweep_ranked_spills.csv`, `elite_full_summary.md`,
  `bpm_leaderboard.csv`, `bpm_leaderboard_h/v.png`, and elite poster candidate
  plots/artifacts
- Best-BPM mining `reports/strong_bpm_analysis_summary.md`,
  `logs/best_bpm_verification_report.md`,
  `statistics/bpm_global_statistics.csv`, `subset_search/best*/best*_results.csv`,
  `subset_search/progress/*.json`, selected `artifacts/global/*.png`,
  `followups/artifacts/poster/*.png`, and
  `followups/next_steps_20260628/ridge_density_best_ensemble/*.png`

## 6. Related Acquisition/Analysis Split

Separating live data acquisition from offline tune analysis is tracked in
`docs/ISSUE_MAP_DAQ_SPLIT.md`. That work should preserve the existing analysis
artifact contract while adding captured-spill bundles that can be reanalyzed
without Redis connectivity.

This split is intentionally ahead of deeper analysis refinement. Treat the
current pipeline as a proof of concept to preserve through the split, then use
offline captured bundles to iterate on stronger physics-quality checks.

Current split status:

The split is implemented for live capture, DAQ diagnostics, offline
single/batch captured-bundle analysis, and a minimal online/offline parity
guardrail. The checked-in capture config now preserves RAW position payloads
and derived RAW intensity payloads. The completed sidecar rejects intensity as
a tune weight but retains it as an auxiliary integrity/timing channel. See
`docs/USAGE.md` for command usage and
`docs/ISSUE_MAP_DAQ_SPLIT.md` for issue history.

The split guardrail is not a physics-quality acceptance criterion. It only
checks that the captured-bundle path preserves today's analysis behavior so
offline refinement can happen on stable raw artifacts.
