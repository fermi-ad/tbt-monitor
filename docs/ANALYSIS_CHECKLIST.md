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

### 2.8 Intensity-assisted quality study

Use newly captured auxiliary `TBT_INTENSITY_RAW` payloads to test whether
intensity helps identify usable spills or bad BPM/digitizer data.

Questions:

- whether intensity RAW is more useful than SCALED for quality metrics
- whether sample index 0 should be treated as metadata rather than waveform
- whether intensity dropouts correlate with bad position traces or tune failures
- whether the observed 5000-sample payload depth is sufficient for the intended
  physics review or points to another acquisition-depth setting

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
6. intensity-assisted quality study
7. `physics_summary.md` and `physics_usable` flag integration

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
and derived RAW intensity payloads; intensity remains auxiliary until quality
metrics are defined. See `docs/USAGE.md` for command usage and
`docs/ISSUE_MAP_DAQ_SPLIT.md` for issue history.

The split guardrail is not a physics-quality acceptance criterion. It only
checks that the captured-bundle path preserves today's analysis behavior so
offline refinement can happen on stable raw artifacts.
