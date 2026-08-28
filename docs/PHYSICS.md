# Physics Validation

This guide explains what the BPM measurements support scientifically, what they
do not yet establish, and what additional experiments would be needed for an
absolute tune calibration. The current result demonstrates repeatable
tune-like structure within the BPM data; it does not claim an independent
measurement of the machine tune.

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
- acquisition of RAW position payloads plus auxiliary RAW intensity payloads
  for later data-quality studies
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
  searches, screened-pool audited best-5 search, global BPM
  statistics, morphology clustering, selected artifacts, and reports
- Best-BPM follow-up sidecars for same-metric dynamic/fixed/all-BPM
  recomputation, held-out spectral support, handoff/visibility review, poster
  PNG generation, and full-buffer Best-ensemble ridge-density comparison
- an exact-cache repair for visibility duration and semantic verification that
  fixed/held-out/handoff/poster artifacts encode their named scientific rows
- strict ridge-gallery coverage and exact-pair verification before interpreting
  apparent concentration, redistribution, or H-plane loss behavior
- leakage-controlled Best-N curves with fit/test purging, digitizer-disjoint
  validation, collection-block intervals, sensitivity checks, and
  cross-collection global-N transfer; publication use requires the strict
  Best-N coverage/identity/timing/summary verifier, not only completed CSV files
- a same-protocol all-training mean/median control that preserves the accepted
  Best-N purge and held-out digitizers and reports paired blind/later-window
  outcomes without requiring adaptive selection to win
- an exact-pair intensity-study implementation with practical-effect gates and
  payload-horizon auditing; the June result is provisional until the corrected
  200-spill refresh passes strict closure, and intensity remains auxiliary to
  position tune extraction
- strict intensity closure requires exact Best-1 zero-effect behavior, complete
  first-50000-turn grids, and recomputation of every retain/reject gate; the
  singleton spectrum is passed through directly so float32 cancellation cannot
  masquerade as a physical weighting effect

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

Confirm that the tune candidate is not a one-channel artifact and that
independent digitizer groups recover compatible later-window structure.

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
- The full-buffer Best-ensemble ridge-density sidecar holds fit-prefix Best-N
  memberships fixed through the 50000-turn buffer. It tests persistence and is
  not same-window dynamic reselection.
- Best-1 and best-3 subset searches can be described as globally exhaustive
  over valid BPMs. Best-5 must be described as a screened-pool exact search with
  beam/random full-space audits. The historical screened Best-10 path is not the
  publication ensemble-size result; contiguous N is evaluated by beam search
  and disjoint validation.
- Blind full-band selected/held-out agreement is the ensemble-size guardrail.
  Near-training-tune support is useful but conditioned and cannot replace it.
  Publication plots therefore show blind agreement alone on a common H/V
  zero-based scale; a separate conditioned panel preserves the secondary check.
  A separate gate matrix exposes all six non-inferiority tests across N; the
  earliest all-gate pass, not the visually highest agreement point, is selected.
  A bounded gate-margin matrix is retained as post-selection robustness
  evidence. It may show that the V result occupies a stable low-to-mid-N region
  or that H trades selected power against prominence, but it cannot retune the
  declared protocol or turn internal agreement into absolute tune accuracy.
- A reduced-sample sensitivity run with no automatic knee is a physical-analysis
  limitation, not a missing-data value. All seven runs must verify, a strict
  majority per plane must yield eligible knees, and unavailable runs must retain
  their selected-power/prominence tradeoff reason. They are never assigned N.
- Same-metric reused-window controls show whether a dynamic small set beats a
  frozen small set, but they do not support a small-set-versus-all-BPM claim.
  The corrected all-BPM median is the strongest descriptive row in both planes,
  and all-BPM mean also leads vertically. These rows remain visible beside a
  separate leakage-controlled control that gives all training-side channels the
  exact Best-N fit prefix, purge, later windows, and held-out-digitizer folds.
  That control compares mean and median aggregation over blind agreement, blind
  tune error, later prominence, and later power. Baseline-favored and unresolved
  intervals are valid outcomes; none supplies external tune truth.
- Exact common spill/window pairing is required for ridge-density subtraction.
  Narrowing or probability-mass redistribution can be described as reduced
  diffuse ridge-pick probability, not physical noise removal or absolute tune
  accuracy. Display-only P98/P99 clipping does not apply to exported metrics.
  The shared-scale H/V composite uses column-normalized pick probability, so
  exact paired counts and sample-fraction diagnostics must accompany it.
  Because the legacy normalized-single selector was defective, corrected
  Best-1 must appear beside the selected Best-N before a visual change is
  attributed to ensemble size. Quantitative ensemble-size evidence likewise
  uses exact-paired selected Best-N minus corrected Best-1, with an exact-zero
  Best-1 self-control. Adaptive-minus-legacy intervals combine selector repair
  and ensemble size and remain historical context only.
- Ridge-density sample retention is itself a physics-quality diagnostic. Every
  spill/window row remains present, but no-confidence tunes stay blank and
  bounded parabolic edge refinements are excluded from the in-band density.
  Column normalization must therefore be interpreted with the exported sample
  fraction and exact paired counts, especially at larger N; visually stronger
  contrast cannot be credited to an ensemble when coverage has fallen.
- Intensity-weighting subtraction uses the same rule independently: methods
  must share exact collection/spill/plane/N/window/center keys, and only common
  finite in-band global ridge picks enter each column. Red/blue therefore means
  higher/lower ridge-pick probability versus unweighted aggregation, not
  physical signal or noise added/removed; absolute-P99 clipping is display-only.
  Proportional raster cells cover the complete tune/turn axes, while standalone
  count-density captions disclose their nonzero-P98 display clip.
  Concentration and crossing-turn galleries preserve common-scale context plus
  separately guarded detail views; absent crossings are omitted, and no panel
  establishes extraction timing or causation.
  Lag correlations likewise retain common and symmetric detail scales without
  converting overlapping-window associations into independent or causal data.
- `scripts/verify_best_bpm_outputs.py` is an artifact completeness and schema
  gate for Best-BPM runs. The associated semantic verifier also reconstructs
  identities, masks, fixed/held-out controls, handoff states, and poster PNGs.
  Passing both means the planned BPM-only outputs are internally coherent; it
  does not establish external tune truth or Schottky agreement.

Deliverables:

- residual statistics and trend plots for matched samples
- tolerance definition for acceptable BPM-vs-reference disagreement

### 3.6 Controlled Tune-Control Quadrupole Scan

Use a controlled quadrupole scan as the primary response test when a matched
reference monitor is unavailable. This test asks whether the BPM-derived
candidate moves with a known machine perturbation; it does not assume that the
unperturbed candidate is already calibrated.

Protocol:

- freeze the tune estimator, discovery bands, score definition, ensemble-size
  rule, and quality thresholds before inspecting scan results
- obtain safe current steps and optics-predicted `Delta Qx`/`Delta Qy` from
  Accelerator Physics, and preserve commanded plus read-back currents
- acquire repeated spills at multiple settings on both sides of nominal;
  interleave nominal returns or repeat the scan direction when practical to
  expose drift and hysteresis
- preserve orbit, intensity, extraction, RFKO, and other machine-state
  readbacks needed to identify confounding changes
- predeclare correct shift sign, monotonic response, measured-versus-predicted
  slope and uncertainty, residual scale, and no-reliable-tune rate as the main
  endpoints
- compare with matched Schottky or tune-meter values when available

This scan requires machine-coordinator approval and safe operating limits. It
is future calibration evidence, not part of the current BPM-only publication.

### 3.7 Analysis-Choice Robustness and Disagreement Taxonomy

Current Best-N sensitivity coverage is partial:

- 10/20/40-spill bootstrap blocks retain H Best-5 and V Best-12 on the full
  accepted rows
- seven reduced-sample beam-width, fit-window-count, and digitizer-fold checks
  resolve H in five runs over N=2-13 and V in six runs over N=10-28
- the post-selection blind-agreement margin moves the earliest eligible N;
  nearby selected/held-out-power floors do not move it at a fixed agreement
  margin
- score weights, the 4096-turn spectral-window length, and the
  `|Delta q| <= 0.0025` agreement tolerance have not been varied in the complete
  leakage-controlled protocol

Before using Best-5/Best-12 as production defaults, rerun the same time- and
digitizer-disjoint protocol over normalized score-weight perturbations and
component ablations, 2048/4096/8192-turn windows with comparable purge
geometry, and several tune-agreement tolerances. Report the eligible-N plateau
and held-out performance surface rather than only the earliest selected N.

The accepted rows also require a first-class disagreement taxonomy. Classify
missing candidates, low-prominence fitted lines, alternative full-band peaks,
band-edge selections, and selected/held-out splits by plane, collection, and
machine state. The current conditioned-versus-blind gap is consistent with
weak competing peaks and full-band peak switching, but it is not a complete
physical explanation.

## 4. Current Dataset Tune Priors

For the current 2000-spill Spark Tier A Best-BPM study, the most recent
analysis configuration and mining results use early-injection tune priors:

- H near `0.65`
- V near `0.72`

These values are dataset-specific soft priors for discovery, candidate
weighting, and ranking. They are not external truth labels, and they should not
be used to force interpretation when the spectral evidence is weak,
multimodal, or inconsistent across BPMs.

Older operational expectations near `Qx ~ 0.69` and `Qy ~ 0.71` should be
treated as historical machine-context notes unless they are tied to an
independent reference measurement for the data being reviewed. Machine settings
changed during the 2000-spill collection, so large tune shifts should trigger
data-quality and machine-state review before interpretation rather than being
scored purely by closeness to a fixed global expectation.

## 5. Canonical Quality Signals

Use repository field names when reviewing outputs:

- `aligned_fraction`
- `confidence_h`, `confidence_v`
- `sliding_fallback_count_h`, `sliding_fallback_count_v`
- `sliding_suspicious_count_h`, `sliding_suspicious_count_v`
- `quality_label` and `quality_flags`

`NO_VISIBLE_TUNE` and `NO_VALID_Q` mean that the associated prominence or
held-out support is unavailable. Those states must remain blank in numeric
support fields; encoding them as zero would confuse missing observability with
measured absence of support.

## 6. Known Limitations

- no direct Schottky ingestion/auto-sync path in this repository
- no controlled tune-control quadrupole scan or other known-response data in
  the accepted captures
- no full Best-N sensitivity pass over score weights, spectral-window length,
  or tune-agreement tolerance; the declared H Best-5 and V Best-12 values are
  operating points rather than unique optima
- no machine-state-linked disagreement taxonomy explaining the low absolute
  blind-agreement rates; H is only marginally separated from its pointwise
  cross-spill null, while V is more clearly separated
- the definitive same-protocol all-training control is complete, but its mixed
  H/V metric outcomes do not establish a universal Best-N advantage;
  descriptive all-BPM rankings still cannot substitute for this held-out test
- no dedicated cross-BPM coherence metric exported as first-class batch field
- no dedicated clipping/saturation diagnostic exported yet
- the final corrected block-aware intensity sidecar retains zero of 240 tested
  effects, so RAW intensity is not used in tune extraction; lag and crossing
  plots remain exploratory
- the corrected exact-pair 0-50000-turn ridge sidecar places the H sustained
  half-peak candidate near turn 5632 and a multimetric change near turn 5760.
  These locate loss of H tracking support in this data set, not a causal or
  fixed extraction onset
- the selected structural grids each contain 360000 rows, but finite coverage
  differs: H Best-5 retains 359018 finite picks, 14 blank-confidence rows, and
  968 bounded edge exclusions; V Best-12 retains 289210 finite picks, 69684
  blanks, and 1106 edge exclusions. Column-normalized density therefore permits
  shape comparison, not an equal-coverage claim
- live Redis payload depth remains a configuration-dependent acquisition issue;
  the reviewed preserved position collections contain 50000 clean turns, while
  the separate intensity capture advertises a longer array whose tail becomes
  structurally unreliable near turn 64000
- the checked-out Delivery Ring producer uses finite device-coded values below
  threshold in scaled streams. A same-ID live comparison found those values in
  scaled HP101 arrays but not either raw array, so raw captures are the correct
  analysis boundary. Source/runtime drift still requires the independent
  first-50000-turn corpus scan before publication. That completed scan found 17
  manifest-level absences across 13 captures already marked `Partial`, but no
  listed-payload corruption and no overlap between the 16 position-only
  absences and the accepted per-spill H Best-5/V Best-12 memberships
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
6. every listed publication raw payload passes the exact topology, count,
   finite-data, plateau, and fallback-pair audit through turn 50000, and every
   manifest-level absence is identity-enumerated and hash-bound without
   zero-filling
7. the all-training control passes exact fold, source-hash, paired-spill,
   interval, and native-PNG verification and its H/V outcomes are reported
8. final poster evidence, manuscript macros, and compliance copy all match the
   accepted primary capture and selected-ridge coverage payload
9. a controlled quadrupole scan or matched independent reference establishes
   the expected tune-response sign, slope, and acceptable residuals
10. score-weight, spectral-window, and agreement-tolerance sensitivity keeps
    the declared operating point on an acceptable held-out performance plateau,
    even when the earliest eligible N changes
