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
  screened-pool audited best-5/best-10 searches, fixed-vs-dynamic statistics,
  morphology clustering, selected artifacts, and final reports

## 2. Open Implementation Work (Current TODO)

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

Current autosweep note:

- `scripts/rank_autosweep_results.py` emits BPM-only `GOOD`, `MARGINAL`,
  `BAD`, `NO_SIGNAL`, `AMBIGUOUS_RIDGE`, and `MISSING_DATA` spill labels, plus
  config labels for ranking. These are offline ranking labels, not yet the
  production Rust `physics_usable` field.
- `scripts/build_elite_full_stage.py` filters the full Tier A stage to
  `spill_health.csv` usable spills and preserves rejected/flagged configs in
  diagnostics before full-data reruns.

## 3. Plot Usability Upgrades (Applies to Existing + New Plots)

Improve consistency across plot outputs:

- explicit axis labels and units where available
- subtitles with spill ID/target ms, window parameters, and BPM counts
- consistent naming conventions
- readable annotation density/resolution

## 4. Immediate Execution Order

1. aggregate median spectrogram
2. BPM subset consistency checks
3. best-BPM vs all-BPM spectrum comparison
4. FFT resolution metadata fields
5. optional frequency-axis mode
6. `physics_summary.md` and `physics_usable` flag integration
7. Spark Best-BPM full run execution and review of
   `reports/strong_bpm_analysis_summary.md`

SVD/PCA remains deferred for production Rust tune extraction. The standalone
poster analyzer can already produce opt-in representative-spill SVD/PCA
comparison plots for physics review.

## 5. Next Review Deliverables

Minimum for the next beam-physics review:

- `physics_summary.md`
- representative `spectrum_h/v.png`
- representative `tune_vs_time.png`
- representative `spectrogram_h/v.png`
- `median_spectrogram_h.png`
- `median_spectrogram_v.png`
- `subset_consistency_h.png`
- `subset_consistency_v.png`

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
  `statistics/bpm_global_statistics.csv`, `subset_search/best*/best*_results.csv`,
  and selected `artifacts/global/*.png`

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
guardrail. See `docs/USAGE.md` for command usage and
`docs/ISSUE_MAP_DAQ_SPLIT.md` for issue history.

The split guardrail is not a physics-quality acceptance criterion. It only
checks that the captured-bundle path preserves today's analysis behavior so
offline refinement can happen on stable raw artifacts.
