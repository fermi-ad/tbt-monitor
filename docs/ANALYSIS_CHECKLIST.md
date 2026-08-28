# Analysis Checklist

This checklist records the completed publication gates and the remaining work
needed to promote the BPM-only result to a calibrated production measurement.
Implemented command usage lives in `docs/USAGE.md`; implementation status and
data flow live in `docs/PLAN.md` and `docs/ARCHITECTURE.md`.

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
  comparison artifacts; executive copy and the control-summary PNG must retain
  all-BPM mean/median even when they are stronger than the small-set methods
- exact-cache visibility-duration repair plus semantic verification of result
  identities, fixed/held-out controls, handoff transitions, poster balance,
  and recommended PNG payloads
- explicit no-visible/no-q quality states plus held-out evaluable-row coverage;
  unavailable support must never be encoded as a zero effect
- corrected exact-channel identity and ring-order reconstruction across the
  primary and every downstream sidecar
- contiguous leakage-controlled Best-N curves with purged later-window and
  digitizer-disjoint validation, block intervals, beam/fit/fold/block-length
  sensitivity comparisons, cross-collection global-N transfer, and a strict
  verifier for contiguous coverage, identities, timing, metrics, summaries,
  recommendation boundaries, and plots
- poster-facing Best-N blind-agreement H/V plots on one shared zero-based scale,
  with a deterministic cross-spill null band and the conditioned near-training
  diagnostic retained separately
- criterion-by-N Best-N gate matrices whose all-gates row matches the declared
  recommendation exactly
- post-selection Best-N gate-margin matrices over the bounded agreement and
  selected/held-out power floors, with the declared cell identified and an
  explicit prohibition on replacing the published knee
- a completed standalone exact-pair intensity weighting/covariate sidecar over 199 complete
  spills; the final block-aware result retains zero of 240 tested effects and
  keeps only integrity and exploratory timing diagnostics. Corrected all-zero
  gate fallback, no-usable-intensity fallback, and direct singleton spectrum
  pass-through preserve bit-exact N=1 invariance.
- strict intensity verification of the audited pair count, complete method/turn
  grids, payload horizon, bit-exact N=1 equality from direct singleton spectrum
  pass-through, effect decisions, and gallery assets; 10/20/40 blocks must
  retain the same exact effect identities
- verifier-bound publication materialization with independent H/V Best-N,
  deterministic null and Best-1 membership summaries, exact-paired corrected-
  Best-1 ridge comparison, selected-N H-loss panel, poster copy, paper table,
  verifier-derived position/finite-pick coverage, v2 payload, and source hashes;
  intensity is not a publication prerequisite or source role

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
- intensity subtraction panels are exact-key paired across methods, use only
  common finite in-band ridge picks, and describe red/blue as higher/lower
  column-normalized pick probability with an absolute-P99 display clip
- intensity heatmap bins cover the complete axes without truncation gaps, and
  standalone count-density captions disclose nonzero-P98 display clipping
- intensity concentration and crossing-turn diagnostics preserve common-scale
  views beside explicitly guarded panel-detail views; no absent crossing is
  zero-filled or interpreted as extraction timing
- lag correlations preserve common -1-to-1 Spearman and symmetric detail views;
  panel autoscaling does not remove overlap dependence or imply causation
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

### 2.9 Reviewer-driven tune calibration and robustness

These are required before promoting the BPM candidate to a calibrated
production tune measurement, but they are not blockers for the explicitly
BPM-only IBIC result:

- run a controlled tune-control quadrupole scan with frozen analysis settings,
  optics-predicted H/V tune shifts, repeated spills, current readbacks, and
  drift/hysteresis controls
- compare measured and predicted shift sign, slope, residuals, and
  no-reliable-tune rate; join a Schottky or tune-meter value when available
- extend the complete leakage-controlled Best-N matrix to normalized score-
  weight perturbations and component ablations
- repeat the Best-N analysis at 2048/4096/8192-turn spectral windows with
  explicit stride and fit/test-purge comparability
- vary the `|Delta q|` agreement tolerance independently from the existing
  blind-agreement non-inferiority margin
- export a disagreement taxonomy separating missing/weak candidates,
  alternative full-band peak selection, band-edge picks, and genuine
  selected/held-out splits by plane and machine state
- preserve H Best-5 and V Best-12 as declared operating points unless the full
  protocol is rerun; do not infer unique optima from the current reduced-sample
  ranges of H N=2-13 and V N=10-28
- target a quality-qualified result after each completed spill first; treat
  intra-spill updates as a separate incremental-transport and latency study

## 3. Plot Usability Gate (Current Publication)

Every final publication plot must satisfy:

- explicit axis labels and units where available
- subtitles with spill ID/target ms, window parameters, and BPM counts
- consistent naming conventions
- readable annotation density/resolution
- confidence intervals or an explicit descriptive-only label where inference
  is not supported
- exact support counts and point-pairing disclosure for comparisons
- disclosure of display-only color clipping, with unclipped exported metrics
- higher/lower ridge-pick probability language for subtractive maps; never a
  physical-noise or denoising label
- no extraction marker in the primary plot; broad review context may appear
  only in a separately named variant
- a verifier-accepted `run_contract.json` binding the figure rows to exact
  source inventories, scientific parameters, and complete compatible shards
- a semantic verifier proving that the depicted metric comes from its named
  table rather than a reused placeholder series; blank or constant scientific
  panels fail even when a PNG exists

## 4. Publication Acceptance Record

The approved paper and poster were accepted after the following gates passed:

1. verified the corrected exact-identity Best-1/3/5 run and every
   fixed/held-out/handoff/artifact/report sidecar, including the exact-cache
   visibility-duration repair
2. verified the contiguous Best-N curve, beam/fit/fold sensitivities, and
   cross-collection transfer; require the full and seven-run sample outputs to
   pass `verify_best_n_outputs.py`, retain every unavailable sensitivity reason,
   require eligible knees from at least four of seven runs per plane, and verify
   the 1000-draw cross-spill null plus exact Best-1 winner frequencies
3. passed the independent two-collection raw-payload audit over 2000 manifests and
   all 239984 captured position rows through turn 50000; retain the hashed
   16-row absent-stream inventory for the 12 recorded partial captures
4. ran and verified the CPU/cache-only all-training mean/median control over the
   exact accepted Best-N validation keys; retain all 10,000 fold rows, 8,000
   spill pairs, 16 comparison rows, and 18 native PNGs
5. rendered the exact-point-paired 50000-turn legacy/Best-N ridge gallery and
   inspect H-loss diagnostics without forcing an extraction onset; require the
   strict spill/window/pair/figure verifier to pass
6. froze the claim boundary and reviewer-driven scientific caveats
7. built and visually verified the Fermilab-template A0 poster and four-page JACoW
   paper
8. verified the exhaustive evidence gallery, curated publication bundle,
   portable manifests, and final deliverables after transfer

SVD/PCA remains deferred for production Rust tune extraction. The standalone
poster analyzer can already produce opt-in representative-spill SVD/PCA
comparison plots for physics review.

## 5. Retained Publication Evidence

The accepted analysis retains:

- corrected verifier and follow-up-verifier reports
- Best-N summary, sensitivity, and cross-collection transfer tables/plots
- all-training fold detail, exact spill pairs, comparison table/report, strict
  verification receipt, H/V scoreboards, paired scatters, and favorable-delta
  CDFs
- exact-point-paired corrected-Best-1-versus-selected H/V publication panels,
  one shared-scale audit comparison for every requested N, and subtractive
  redistribution maps
- exact-point-paired corrected Best-1-versus-selected H/V and
  legacy/corrected-Best-1/selected-Best-N control composites, with only the
  corrected Best-1 transition interpreted as ensemble-size evidence
- unsmoothed exact-paired per-turn width, entropy, peak-bin, and shared-ridge-
  mass contrasts for every adaptive N pair plus legacy, including an exact-zero
  Best-1 self-control, with zero-referenced five-window-smoothed review plots
- shared-scale selected-Best-N-minus-corrected-Best-1 H/V landscape and portrait
  composites for all five metrics, with the clean P10-P90 pair bound to the
  paper/poster and explicit non-noise/non-extraction guardrails
- full-axis density raster coverage with percentile and median overlays on the
  identical tune-to-pixel mapping
- exact 2,000-spill by 180-center structural-grid closure for every requested N
  and plane, with finite in-band, blank-confidence, and bounded edge-excluded
  counts reported separately; adaptive aggregate/per-turn rows must equal the
  reconstructed finite-point intersections
- selected publication coverage stated numerically: H Best-5 has 359018 finite,
  14 blank-confidence, and 968 edge-excluded rows; V Best-12 has 289210 finite,
  69684 blank-confidence, and 1106 edge-excluded rows, each from 360000
  structural rows
- primary nominal 60 H plus 60 V topology accompanied by the 16 explicit source
  absences across 12 flagged partial captures; no absent channel is fabricated
  or zero-filled
- H-plane concentration, width, entropy, confidence, fallback, and data-derived
  loss-candidate diagnostics
- deterministic cross-spill-null and diversity-independent Best-1 membership
  tables, strict verification receipts, and source hashes
- editable A0 poster, poster PDF/render, four-page paper source/PDF, source data,
  generated numerical macros, checksums, commands, captions, and compliance
  notes
- byte-identical named poster PNG and 150 dpi PDF raster, with authentic
  master-level Fermilab/DOE artwork visible in the reviewed render
- delivered poster layout inventory, PPTX overflow inspection, and zero-issue
  template-fidelity reports, with exact portable poster/paper checksum manifests
- publication source manifest with exact numerical source hashes and a
  finalizer-verified exact materialized-output inventory
- final `compliance_report.md` and `publication_manifest.csv` produced only
  after explicit visual-QA pass flags, page/payload/hash verification, and a
  read-only final-PPTX scan reporting zero empty structural placeholders; the
  report must repeat the payload-bound primary and selected-ridge coverage

Optional but useful:

- the completed block-aware intensity result tables and indexed gallery,
  packaged as a standalone sidecar rather than an IBIC dependency

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

Live data acquisition and offline tune analysis are separated by the captured-
spill artifact contract. Captured bundles can be reanalyzed without Redis
connectivity while preserving the existing analysis inputs and quality state.

This split is intentionally ahead of deeper analysis refinement. Treat the
current pipeline as a proof of concept to preserve through the split, then use
offline captured bundles to iterate on stronger physics-quality checks.

Current split status:

The split is implemented for live capture, DAQ diagnostics, offline
single/batch captured-bundle analysis, and a minimal online/offline parity
guardrail. The checked-in capture config now preserves RAW position payloads
and derived RAW intensity payloads. The completed sidecar rejects intensity as
a tune weight but retains it as an auxiliary integrity/timing channel. See
`docs/USAGE.md` for command usage.

The split guardrail is not a physics-quality acceptance criterion. It only
checks that the captured-bundle path preserves today's analysis behavior so
offline refinement can happen on stable raw artifacts.
