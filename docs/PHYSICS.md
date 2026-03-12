# PHYSICS
Beam Physics Validation Guide for BPM-Derived Tune Monitor

This document tracks the physics-validation work needed to establish that the
existing BPM tune pipeline is physically credible for Delivery Ring operations.

## 1. Implemented Baseline (Current Repository)

The software pipeline already provides:

- synchronized multi-BPM spill capture with adjacent-ms clustering (`±1 ms`)
- injection-window and sliding-window tune extraction (`Qx`, `Qy`)
- tracked sliding peaks with fallback/suspicious-step diagnostics
- per-spill artifacts:
  - `spectrum_h.png`, `spectrum_v.png`, `spectrogram_h.png`, `spectrogram_v.png`,
    `tune_vs_time.png`, `tune_validation.png`, `sliding_tune.csv`
- optional flashpoint sampling mode (`--flashes N|max`) for fixed-count evenly
  spaced in-spill tune checkpoints (bounded by available turns and window size)
- batch artifacts:
  - `tune_vs_spill.png`, `confidence_vs_spill.png`, `alignment_vs_spill.png`,
    `tune_scatter_qx_qy.png`, `tune_histogram.png`,
    optional `tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png`
    (one per flash index),
    `composite_waterfall_h.png`, `composite_waterfall_v.png`, `batch_summary.md`
- quality semantics and labels (`GOOD`, `MARGINAL`, `BAD`) with explicit flags
  (for example `INCOMPLETE_TBT_POLL`, `LOW_ALIGNMENT_FRACTION`, low-confidence
  and band-edge flags)
- optional reference matching in batch mode (`reference-file`) with residual
  outputs (`tune_residuals.png`)

Physics validation should build on this baseline instead of redefining these as
new software tasks.

## 2. Core Physics Question

Can synchronized BPM turn-by-turn data reliably measure horizontal and vertical
betatron tune during the spill in a way that is operationally useful?

This requires confirming:

- extracted peaks correspond to true betatron motion
- measured tune is stable and physically consistent spill-to-spill
- in-spill tune evolution reflects machine behavior, not analysis artifacts
- BPM-derived values agree reasonably with reference tune measurements

## 3. Required Physics Validation Tests

### 3.1 Injection Tune Stability

Run at least ~100 spills and evaluate injection-window tune spread.

Expected behavior:

- tune clusters tightly in steady machine state
- representative range for spread is order `1e-3` to `1e-2` (machine-dependent)

Deliverables:

- injection-tune histogram evidence (existing `tune_histogram.png` is acceptable)
- median and standard deviation for `Qx` and `Qy`
- accepted-spill count and quality breakdown

### 3.2 Tune Evolution Through Spill

Use sliding-window outputs to assess continuity.

Expected behavior:

- smooth evolution absent abrupt machine changes
- limited fallback/suspicious-step rates for accepted spills

Deliverables:

- representative `tune_vs_time` traces
- median trajectory and spread across accepted spills
- fallback and suspicious-step summary statistics

### 3.3 Spectral Peak Validation

Confirm peak morphology is physically plausible.

Expected behavior:

- distinct narrow peak in configured tune bands
- repeatable location across accepted spills
- clear separation from noise floor

Deliverables:

- representative `spectrum_h` and `spectrum_v`
- tune-band zooms for accepted spills

### 3.4 BPM Coherence Test

Confirm tune is not dominated by a small BPM subset.

Deliverables:

- subset-consistency results (all BPMs vs splits/subsets)
- cross-BPM coherence evidence (or equivalent consistency metric)

### 3.5 Reference Monitor Comparison

Compare BPM tune against Schottky-equivalent reference values.

Notes:

- current repo supports external reference-file matching, not direct Schottky
  ingestion

Deliverables:

- residual statistics and trend plots for matched samples
- tolerance definition for acceptable BPM-vs-reference disagreement

## 4. Expected Operational Tune Region

Working expectations (subject to machine state and optics settings):

- `Qx ~ 0.69`
- `Qy ~ 0.71`

Operationally large unexplained shifts should trigger data-quality and
machine-state review before interpretation.

## 5. Canonical Quality Signals

Use repository field names when reviewing outputs:

- `aligned_fraction`
- `confidence_h`, `confidence_v`
- `sliding_fallback_count_h`, `sliding_fallback_count_v`
- `sliding_suspicious_count_h`, `sliding_suspicious_count_v`
- `quality_label` and `quality_flags`

## 6. Known Limitations

- no direct Schottky ingestion/auto-sync path in this repository
- no dedicated cross-BPM coherence metric exported as first-class batch field
- no dedicated clipping/saturation diagnostic exported yet
- no SVD/PCA tune path in production flow yet

## 7. Acceptance Criteria

The BPM tune monitor is successful when:

1. injection tune is stable over many spills in steady conditions
2. spectral peaks are repeatable and physically plausible
3. sliding tune evolution is smooth for accepted-quality spills
4. subset/coherence checks support ring-wide beam-motion interpretation
5. BPM-vs-reference residuals are operationally acceptable
