# PHYSICS
Beam Physics Validation Guide for BPM-Derived Tune Monitor

This document tracks the physics-validation work needed to establish that the
existing BPM tune pipeline is physically credible for Delivery Ring operations.

## 1. Implemented Baseline (Current Repository)

The current software baseline is documented in `docs/USAGE.md` and
`docs/ARCHITECTURE.md`. Physics validation should build on these implemented
capabilities rather than re-track them as new software tasks:

- synchronized multi-BPM spill snapshots and captured-spill bundles
- injection-window, sliding-window, flashpoint, robustness-study, and batch
  tune analysis
- per-spill and batch plots, CSV/JSONL records, markdown summaries, quality
  labels, explicit warnings, and timeliness metrics
- optional external reference matching in batch mode
- standalone BPM-only poster artifact synthesis through
  `scripts/bpm_dgx_poster.py`
- raw captured-spill GPU flash analysis through
  `scripts/gpu_analyze_captured_spills.py`, including ridge-density,
  multitaper, DP-ridge, optional SVD/PCA, and DGX benchmark products for the
  offline poster workflow
- staged Spark BPM autosweep/ranking through `scripts/run_autosweep.py`,
  `scripts/rank_autosweep_results.py`, and
  `scripts/make_initial_analysis_summary.py` for selecting candidate H/V/poster
  configurations and candidate spills from raw position bundles
- Best-BPM mining through `scripts/run_best_bpm_pipeline.py`, including
  per-BPM spectral features, within-spill consensus, exact best-1/best-3
  searches, screened-pool audited best-5/best-10 searches, global BPM
  statistics, morphology clustering, selected artifacts, and reports

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
- the current poster/DGX sprint is explicitly BPM-only and should not include
  Schottky labels or validation plots
- Spark GPU outputs are BPM-only tune estimates unless later joined to an
  independent reference table.
- Autosweep labels are BPM-only ranking labels. They help select candidate
  configs/spills for review, but do not prove true tune without independent
  reference comparison.
- Best-BPM mining labels and consensus tunes are BPM-only internal evidence.
  The method intentionally avoids using global tune distributions or neighboring
  spills as labels because the machine configuration changed during collection.
- Best-1 and best-3 subset searches can be described as globally exhaustive
  over valid BPMs. Best-5 and best-10 must be described as screened-pool exact
  searches with beam/random full-space audits unless a true global enumeration
  is ever run.
- `scripts/verify_best_bpm_outputs.py` is an artifact completeness and schema
  gate for Best-BPM runs. Passing it means the planned BPM-only outputs exist;
  it does not establish external tune truth or Schottky agreement.

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
- no SVD/PCA tune path in the Rust production flow yet; the standalone poster
  analyzer has opt-in SVD/PCA comparison plots that still need physics review
- autosweep scoring uses pragmatic proxy metrics until independent tune labels
  or richer per-BPM spectral products are available

## 7. Acceptance Criteria

The BPM tune monitor is successful when:

1. injection tune is stable over many spills in steady conditions
2. spectral peaks are repeatable and physically plausible
3. sliding tune evolution is smooth for accepted-quality spills
4. subset/coherence checks support ring-wide beam-motion interpretation
5. BPM-vs-reference residuals are operationally acceptable
