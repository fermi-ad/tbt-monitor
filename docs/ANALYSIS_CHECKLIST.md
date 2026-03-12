# ANALYSIS_CHECKLIST
Remaining Implementation Checklist for Physics Validation

This checklist has been trimmed to avoid re-tracking features that already
exist in the repository (`README.md`, `docs/PLAN.md`, `docs/ARCHITECTURE.md`).

## 1. Already Implemented (Removed from TODO)

The following are already available and should be treated as baseline:

- per-spill artifacts: `spectrum_h/v`, `tune_vs_time`, `sliding_tune.csv`
- per-spill tune-validation composite: `tune_validation.png`
- per-spill spectrograms: `spectrogram_h/v` (top-down tune heatmap vs time with normalized log-power color scale)
- optional flashpoint sampling mode (`--flashes N|max`) for fixed-count evenly
  spaced sliding windows (bounded by available turn depth)
- batch artifacts:
  - `tune_vs_spill.png` (contains both `Qx` and `Qy`)
  - optional `tune_vs_spill_flash_XX.png` series (flash-index trend across spills)
  - optional `tune_histogram_flash_XX.png` series (flash-index histograms across spills)
  - `confidence_vs_spill.png`
  - `alignment_vs_spill.png` (coverage/alignment trend)
  - `tune_scatter_qx_qy.png`
  - `tune_histogram.png` (contains both `Qx` and `Qy` histograms)
  - `batch_summary.md`
- quality labels and flags: `GOOD`, `MARGINAL`, `BAD`, plus explicit
  `quality_flags` (including edge and confidence checks)
- batch records (`spills_summary.csv` / `.jsonl`) with tune, confidence,
  alignment, stream counts, tracked-sliding statistics, and quality fields
- robustness-study artifacts from `analyze-phase`:
  - `tune_vs_window_start.png`, `tune_vs_window_length.png`
  - `bpm_quality_table.csv`, `tune_by_bpm.png`, `confidence_by_bpm.png`
  - `method_comparison.png`, `findings_summary.md`
- external reference matching and residual plotting in batch mode

## 2. Open Implementation Work (Current TODO)

### 2.1 Aggregate median spectrogram

Add:

- `median_spectrogram_h.png`
- `median_spectrogram_v.png`

Method:

- accumulate same-grid sliding spectra across accepted spills
- compute median power per `[window, tune_bin]`
- overlay median tracked tune path

### 2.2 BPM subset consistency checks

Add:

- `subset_consistency_h.png`
- `subset_consistency_v.png`

Modes:

- all BPMs
- odd/even split
- first/second half split
- random fixed-size subsets

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

SVD/PCA remains deferred until the above baseline physics-review artifacts are
in place.

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
