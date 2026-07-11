# Design Decisions

This file captures major design choices, why they were made, and what tradeoffs they imply.

## DD-001: Stream-native ingestion with `XREAD BLOCK`

Decision:
- Use Redis stream blocking reads (`XREAD BLOCK`) instead of fixed-interval polling loops.

Why:
- Lower idle overhead.
- Better event timing fidelity from stream IDs.
- Natural fit for multi-device wake-driven workflows.

Tradeoffs:
- Requires robust reconnect handling.
- Debugging can be less intuitive than periodic polling.

## DD-002: Global synchronized spill snapshots

Decision:
- Any device wake triggers a full all-device snapshot for analysis commands.

Why:
- Tune extraction quality depends on coherent multi-BPM context.
- Prevents local-device wake bias in tune estimates.

Tradeoffs:
- More per-spill Redis work than single-device reads.
- Snapshot incompleteness must be surfaced as warnings/flags.

## DD-003: Adjacent timestamp bucket clustering (±1 ms)

Decision:
- Treat near-adjacent stream-id milliseconds as one physical target bucket.

Why:
- Real systems often split one spill across neighboring milliseconds.
- Prevents coverage splits and duplicate spill records.

Tradeoffs:
- Slight risk of collapsing distinct events if events are truly 1 ms apart.
- Mitigated by keeping tolerance small and bounded by alignment tolerance.

## DD-004: Keep partial/incomplete snapshots with explicit warnings

Decision:
- Do not hard-fail every incomplete poll; carry warnings and quality flags.

Why:
- Operational debugging needs visibility into degraded states.
- Hard drops hide intermittent infrastructure issues.

Tradeoffs:
- More marginal outputs to interpret.
- Requires clear quality semantics (`INCOMPLETE_TBT_POLL`, low alignment, low confidence).

## DD-005: Confidence-gated peak extraction in expected tune bands

Decision:
- Restrict search to configured tune bands and require minimum confidence.

Why:
- Reduces false positives in low-SNR windows.
- Keeps output physically plausible under noisy conditions.

Tradeoffs:
- Weak but real signals may be marked missing.
- Requires tuning of confidence thresholds by operation mode.

## DD-006: Sliding-window tracking with conservative state updates

Decision:
- Use local-band tracking around prior trusted tune; fallback windows do not reseed tracker.

Why:
- Avoid drift from noisy outliers.
- Preserve smooth physical tune evolution unless evidence is strong.

Tradeoffs:
- Can become conservative during abrupt true tune changes.
- Requires explicit diagnostics for fallback/suspicious windows.

## DD-007: Explicit timeliness metrics as first-class diagnostics

Decision:
- Record and summarize `obs_ms - target_ms` deltas per spill and batch.

Why:
- Synchronization quality should be observable, not inferred.
- Supports longitudinal monitoring of capture jitter/freshness.

Tradeoffs:
- Adds more numbers for users to interpret.
- Requires docs to explain signed vs absolute timing deltas.

## DD-008: Batch outputs in both machine and human formats

Decision:
- Keep `csv/jsonl` records and human-readable markdown/plots.

Why:
- Supports both automated downstream analysis and operator review.
- Simplifies ad-hoc debugging and reproducibility.

Tradeoffs:
- Wider compatibility surface when fields change.
- Requires discipline around output schema evolution.

## DD-009: Standardized tune-plot Y-axis bounds

Decision:
- Use config-defined fixed tune Y-axis bounds (`tune_plot_y_min/max`) for
  tune-valued trend/comparison plots instead of per-plot autoscaling.
- Render `tune_vs_time` with configurable horizontal Y-grid spacing via
  `tune_plot_y_tick_step` (default `0.01`).

Why:
- Visual comparisons across spills/runs are unreliable when each plot autoscales.
- Fixed scaling makes drift/outlier interpretation more consistent for physics review.
- Grid lines improve quick manual readout during operations.

Tradeoffs:
- Out-of-range tune values can clip at plot edges if bounds are too tight.
- Operators must keep configured bounds aligned with current machine regime.

## DD-010: Batch-end composite waterfall generation for `analyze-spills`

Decision:
- Always emit composite horizontal/vertical waterfall plots at the end of
  `analyze-spills` (`--count` successful spills).

Why:
- Physics review needs a single cross-spill view of tune-vs-time evolution.
- Batch-end synthesis reduces manual plot stitching and improves run-to-run review speed.

Tradeoffs:
- Additional plot generation time at batch completion.
- 3D-style projection is a visualization aid, not a substitute for raw CSV records.

## DD-011: Per-spill top-down spectrograms with normalized heat scale

Decision:
- Emit per-spill `spectrogram_h.png` and `spectrogram_v.png` heatmaps.
- Use tune on X, and turns by default on Y; optionally use microseconds when
  `plot_time_axes_in_us=true` (converted via `turn_period_us`).
- Use normalized log spectral power for color intensity.
- Map rows discretely to sliding-window FFT steps (one row per step).

Why:
- Provides a physics-review view of tune evolution without perspective distortion.
- Normalized heat scaling keeps weak/strong structures readable within a spill.

Tradeoffs:
- Heat colors are normalized per plot, so absolute color intensity is not directly
  comparable between different spills without raw-spectrum reference.

## DD-012: Optional success-count stop condition for free-run analysis modes

Decision:
- `analyze-spill --free-run` and `analyze-phase --free-run` accept optional
  `--count N` and stop after `N` successful analyses.
- If `--count` is omitted, free-run remains unbounded (Ctrl-C stop).
- In `--no-beam --free-run`, count targets successful analyses across discovered
  historical candidates; if exhaustion occurs before `N`, the command exits with
  an explicit error.
- In `analyze-spill --free-run --count`, collected spills are also synthesized
  into batch-level summary/composite outputs at exit.

Why:
- Operators need both long-running capture and bounded capture without switching
  command families.
- Using successful analyses (not wake count) keeps stop semantics aligned with
  produced artifacts and downstream batch-style review.

Tradeoffs:
- Historical free-run with strict count can fail when stale depth is insufficient.
- Additional CLI surface requires clear docs to avoid confusion with
  `analyze-spills --count` (which is always required).

## DD-013: Per-spill tune-validation composite artifact

Decision:
- Emit one per-spill composite figure (`tune_validation.png`) combining:
  H/V spectrograms and H/V tune-vs-time panels in a 2x2 layout.
- Spectrogram panels overlay both tracked (`selected_tune`) and raw global tune
  trajectories, with row registration marks by sliding-window step.
- Tune-vs-time panels overlay tracked and raw traces and annotate suspicious-step
  and fallback windows.

Why:
- Physics review needs an immediate visual check that tracked tune follows the
  dominant spectral ridge without opening multiple files.
- Side-by-side H/V and spectrogram/trace views reduce false confidence from
  single-plot inspection.

Tradeoffs:
- Additional per-spill artifact generation cost and output file volume.
- Composite readability depends on balanced panel scaling and label layout.

## DD-014: Optional flashpoint sampling mode for sliding tune extraction

Decision:
- Add `--flashes N|max` to `analyze-spill` and `analyze-spills`.
- When set, sliding-window placement switches from fixed stride to `N`
  evenly spaced centers across spill depth (window size still governed by
  `sliding_window_turns`).
- `--flashes max` automatically resolves to the per-spill maximum supported
  by available turns and `sliding_window_turns`.
- In flash mode, injection tune estimation also uses `sliding_window_turns`
  (ignoring `injection_window_turns`).
- Batch outputs add one tune-trend plot and one histogram per flash index:
  `tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png`.

Why:
- Physics review often needs spill-to-spill comparison at consistent in-spill
  phases (early/mid/late) without over-emphasizing dense sliding trajectories.
- Fixed-count flash sampling provides predictable per-spill sampling while
  preserving existing injection and tracked-sliding analysis logic.

Tradeoffs:
- High requested flash counts can exceed available turn depth for short spills;
  implementation applies per-spill runtime bounds and emits warnings when
  reduced.
- Flash-index plots are indexed by sampling order; center turn can vary slightly
  when spill turn depth varies between captures.

## DD-015: Captured-spill artifact boundary

Decision:
- Separate data acquisition from tune analysis by writing complete, versioned
  captured-spill bundles before offline analysis.
- Keep Redis stream synchronization, target-ms selection, and raw payload
  capture in the acquisition path.
- Keep FFT/tune extraction, quality classification, and artifact rendering out
  of `capture-spill` / `capture-spills`.
- Store Redis stream `_` payload bytes exactly as captured, with a
  `schema_version=1` manifest, stream inventory, stream IDs, sample counts,
  byte counts, and `fnv1a64` checksums.
- Let `analyze-captured-spill` reconstruct the same one-spill analysis snapshot
  from a captured bundle and then reuse the current analysis/output path without
  Redis connectivity.
- Let `analyze-captured-spills` reconstruct snapshots from captured bundle
  directories and reuse the current batch records, quality labels, reference
  matching, plots, waterfall, and markdown summary writers.
- Record offline batch trigger provenance as `trigger_source=captured-spill`
  with `trigger_ms=target_ms`, because Redis trigger keys are not available or
  needed for offline bundle analysis.
- Keep a deterministic online/offline parity guardrail in normal `cargo test`
  that compares proof-of-concept tune outputs, sliding medians, warnings, and
  quality flags for the same raw spill data.

Why:
- Complete spill bundles make analysis reproducible without requiring live Redis
  access or repeated beam-time capture.
- Offline reanalysis supports algorithm and physics-validation iteration on the
  same raw data.
- A manifest boundary makes schema/version changes explicit.
- The split can land before the proof-of-concept tune analysis is physics-final;
  parity checks should guard against accidental behavior drift, not certify the
  current algorithm as final.

Tradeoffs:
- Adds a durable artifact schema that must be versioned and tested.
- Increases disk usage because raw payloads are preserved in addition to plots
  and summaries.
- Requires focused parity tests so online and offline analysis paths do not
  diverge during the split.
- The current implementation duplicates some synchronization helpers from
  `src/analyze.rs`; later parity/refactor work should consolidate shared
  acquisition primitives where it reduces drift without blurring the acquisition
  boundary.
- Offline analysis depends on the manifest schema remaining explicit and
  versioned; schema changes must include loader/test/docs updates.

## DD-016: Same-spill timing diagnostics

Decision:
- Treat capture completeness as a same-spill artifact question, not exact
  millisecond equality.
- Add `same_spill_tolerance_ms` with default `25 ms` for capture lookup,
  duplicate suppression, and DAQ diagnostics.
- Preserve exact timestamp deltas in per-spill manifests and run-level
  diagnostics so jitter distributions can be trended.
- Report captured-payload timestamp distributions separately from latest-ID
  snapshot distributions. A latest-ID snapshot can be one event stale while the
  captured payload for the same target is complete.
- Separate captured artifact quality from latest-poll timing diagnostics.
  `LATEST_STALE_BUT_CAPTURED_OK` is diagnostic context, not a partial capture.
- Make operator-facing summaries lead with captured artifact completeness and
  capture suspect digitizers. Latest-poll-only suspects remain available for
  troubleshooting, but are explicitly secondary to captured payload quality.
- Add `assess` as a non-capturing preflight and `diagnose-captures` as an
  offline report regenerator.

Why:
- Machine events arrive about every 15 seconds, so millisecond-level timestamp
  spread should be measured instead of treated as missing data.
- Operators need to identify stale digitizers before and during DAQ runs.
- Stable reason codes allow a future strict-fail mode without changing the
  diagnostic schema.

Tradeoffs:
- Manifests and run directories contain more diagnostic metadata.
- There are now two timing concepts: legacy strict `align_tolerance_ms` and
  capture-oriented `same_spill_tolerance_ms`.

## DD-017: Standalone BPM-only poster analysis layer

Decision:
- Implement the DGX/poster sprint as Python scripts under `scripts/` rather
  than as new Rust runtime commands.
- Consume existing collected artifacts (`candidate_spills.csv`,
  `spills_summary.csv`, and `capture_index.csv`) and write poster-phase
  products into a separate output tree.
- Add a second standalone script for raw captured-spill bundles when Spark/GPU
  processing should run directly from payload bytes instead of summary CSVs.
- Keep Schottky comparison out of this phase.
- Use CPU execution as the reproducibility baseline and CUDA/CuPy only as an
  optional DGX acceleration path.

Why:
- The poster work is offline evidence synthesis over already collected data,
  not an online deployment or control-room feedback feature.
- Keeping it standalone avoids expanding the runtime safety surface while still
  letting the complete `drbpm1` artifact tree feed the poster manifest.
- CPU fallback makes local review and regression checks possible even when DGX
  access is unavailable.
- Keeping the raw payload GPU analyzer outside the Rust runtime lets Spark
  process the 2000-spill dataset without expanding the online monitoring or
  acquisition safety surface.

Tradeoffs:
- The scripts must be kept in sync with stable artifact schemas.
- When only summary/ranking artifacts are available, median spectrogram and
  BPM-subset products are conservative proxies until raw spectral/per-BPM study
  artifacts are provided.

## DD-018: Offline tune-evolution upgrade products

Decision:
- Add ridge-density, multitaper spectrogram, dynamic-programming ridge, optional
  SVD/PCA denoising, and DGX benchmark products to
  `scripts/gpu_analyze_captured_spills.py`.
- Keep the default backend as CPU and make CUDA an explicit/offline acceleration
  choice.
- Keep the current all-BPM averaged Hann spectrum as the baseline, then write
  comparison plots instead of silently replacing the baseline tune trace.
- Use SVD/PCA only as an opt-in representative-spill denoising experiment, not
  as the default physics answer.

Why:
- The 2000-spill captured dataset is large enough that clean composite
  tune-evolution plots require density/ridge aggregation rather than thousands
  of individual traces.
- Multitaper and DP ridge extraction improve poster-review readability while
  preserving side-by-side comparison against the existing FFT/stride baseline.
- SVD/PCA can clarify coherent motion, but betatron motion may occupy mode
  pairs; it needs comparison plots before becoming a production default.

Tradeoffs:
- The raw-spill analyzer has a larger Python artifact surface to document and
  regression-test.
- DP ridge penalties and SVD mode counts are method choices that require
  physics review before being treated as operational defaults.
- These plots are BPM-only offline evidence products; they are not Schottky
  validation, online inference, or feedback-loop controls.

## DD-019: Staged BPM autosweep instead of full Cartesian search

Decision:
- Implement Spark tune-tracking parameter exploration as a staged autosweep:
  Stage 0 inventory/health/cache, baseline configs, factor screening, capped
  interaction pilot, and elite full-data reruns.
- Use canonical sorted JSON plus `sha256[:12]` config hashes for deterministic
  config identity, resume-safe output directories, and reproducible handoff
  lists.
- Keep v1 BPM-only over Tier A raw position bundles; Tier B intensity/beam-loss
  paths remain later-capable but do not block Tier A outputs.
- Score configs with fixed component weights:
  `0.25 injection + 0.25 ridge + 0.20 bpm_robustness +
  0.15 spectrogram_quality + 0.10 usable_fraction +
  0.05 compute_efficiency`.
- Classify spill/config rows with explicit labels so later strict filters can
  be applied to ranked tables rather than rerunning acquisition.
- Keep autosweep serial by default, with `--parallel-jobs` as an opt-in Spark
  throughput control for overlapping independent config/view analyzer jobs.
- Add lightweight `nvidia-smi` telemetry as an optional wrapper-level concern
  rather than embedding GPU accounting inside the physics analyzer.

Why:
- The parameter space is too large for a naive Cartesian sweep over all spills.
- The first need is to identify robust candidate tune-tracking configurations
  and top candidate spills for review, not to exhaustively generate artifacts
  for every combination.
- Deterministic hashes and CSV logs make Spark jobs restartable and auditable.
- Isolated config/view job directories make bounded parallel execution safe
  without introducing shared-writer races.
- Keeping autosweep as Python scripts avoids adding offline research controls
  to the Rust runtime safety surface.

Tradeoffs:
- Pilot selection can miss interactions that a full Cartesian search would
  reveal; full-stage reruns should therefore include baseline and top H/V/poster
  configs.
- Parallel autosweep jobs improve host/GPU utilization but can increase
  contention for a single GPU. The older autosweep may start with 2 concurrent
  jobs and use telemetry before raising that to 3-4; this does not apply to the
  max-N=40 evaluator.
- Best-N max-N=40 evaluators run at no more than two processes on Spark's
  single GB10. Four measured processes exceeded 115 GiB of unified memory and
  made the host unresponsive. A watchdog-bounded two-process qualification
  peaked near 83 GB (77 GiB) host use with about 44 GiB available and kept GPU
  utilization mostly near 70-96%. Two-way execution therefore requires a
  32 GiB `MemAvailable` floor sampled every five seconds, three low samples
  before process-group termination, and resumable checkpoints; logical
  sharding and execution concurrency remain separate controls.
- Telemetry-derived GPU-hours and watt-hours are estimates from sampled
  `nvidia-smi` utilization and power, not scheduler-grade accounting.
- Full-stage execution uses an explicit elite builder rather than implicit
  baseline injection inside the runner, so the config list is auditable and the
  full run never expands beyond the selected effective configs.
- Some scoring components are pragmatic proxies until independent reference
  labels or richer per-BPM spectral products are available.
- The ranking schema is a downstream analysis contract and must be versioned
  through docs/tests when field meanings change.

## DD-020: RAW position plus auxiliary RAW intensity capture

Decision:
- Use `TBT_POSITION_RAW` as the checked-in position capture stream variant for
  the next preservation run.
- Add `capture_intensity_variant` as an optional config-level derived capture
  control. `capture_intensity_variant=raw` derives one `TBT_INTENSITY_RAW`
  stream from each configured position stream.
- Keep position streams responsible for free-run wake watching, target
  selection, and offline tune analysis.
- Capture and diagnose derived intensity streams as auxiliary raw payloads, but
  do not promote them into tune-analysis semantics yet.

Why:
- RAW position preserves the least-transformed position artifact while RAW vs
  SCALED conventions are still being investigated.
- RAW intensity appears available at the same 5000-sample full-resolution shape
  and may help later quality/beam-present studies.
- Keeping intensity auxiliary avoids accidentally treating intensity waveforms as
  H/V position traces in the proof-of-concept tune chain.

Tradeoffs:
- A complete default capture now expects 240 streams instead of 120, roughly
  doubling full-resolution payload bytes per spill.
- Offline tune analysis must intentionally skip auxiliary intensity entries
  until an intensity analysis or quality-gating contract is defined.

## DD-021: Best-BPM mining uses within-spill evidence, not global tune labels

Decision:
- Add a separate Best-BPM mining pipeline for the 2000-spill Spark Tier A
  position-only dataset.
- Score BPM subsets using within-spill spectral quality, held-out BPM support,
  consensus agreement, visibility, diversity, and ambiguity penalties.
- Keep expected H/V tunes as soft priors only.
- Make best-1 and best-3 globally exhaustive over the valid plane-local BPM
  set; make best-5 exact only inside a screened pool with independent
  beam/random full-space audits. Keep the older screened best-10 capability as
  an optional historical path, not the publication ensemble-size decision.
- Keep full-buffer ridge-density comparison as a sidecar that reuses completed
  memberships before considering any full 50k dynamic subset search.
- Render one shared-scale H/V-by-method composite per requested N, but retain
  the per-plane, subtraction, sample-fraction, and quantitative tables needed
  to prevent a visually narrow ridge from becoming a stand-alone claim.
- When leakage-controlled selection recommends different N for H and V, render
  one additional plane-selected composite rather than silently applying one
  plane's knee to both. Bind those two choices into the ridge run contract and
  keep a selected-N concentration panel for each plane.

Why:
- The machine configuration changed during acquisition and the spills are
  unlabeled, so global tune continuity or closeness to neighboring spills would
  create false confidence.
- Exact global best-10 over roughly 60 BPMs is computationally unrealistic.
  Reporting screened-pool scope plus audit outcomes is more defensible than
  implying a global exhaustive result; the publication question is handled by
  a contiguous beam-search Best-N curve with independent validation.
- Plane-local BPM indices preserve the 64-bit subset-mask contract while
  keeping H and V searches independent.
- Spark execution uses CuPy for FFT cache and subset scoring, while the
  Python-heavy per-BPM peak pass is sharded across CPU workers.

Tradeoffs:
- Final reports must say that per-spill consensus is an internal unsupervised
  reference, not ground truth.
- Dynamic per-spill selection is useful evidence but carries look-elsewhere
  bias; fixed-set cross-fitting and held-out support are required for the poster
  narrative.
- Fixed, dynamic, and all-BPM control rows must be recomputed from the same
  cached spectra with the same evolution score. That direct comparison remains
  descriptive because the original dynamic memberships reuse their selection
  windows; leakage-controlled Best-N validation carries the publication
  inference.
- Preserve no-reliable-tune states as missing measurements, not numerical
  zeros. Fixed/control rows may have zero score with unavailable prominence
  only when explicitly flagged `NO_VISIBLE_TUNE`; held-out support is
  unevaluable without `q_hat`, so every support field stays blank and summary
  tables report both total and evaluable coverage.
- Full-buffer evolution can be regenerated by adding a longer spectral-cache
  config; the v1 implementation keeps the schema and visibility classes in
  place while using cached early rolling spectra and a finalist re-evaluation
  table for mean, median, trimmed-mean, and static-quality-weighted aggregators.
- Long subset-search passes write per-shard JSON progress rather than
  provisional science CSVs. The progress files are operational telemetry for
  stall/ETA assessment; the merged `best*/` CSVs remain the authoritative
  analysis outputs.
- The 0-50000 turn ridge-density sidecar is visually comparable to the older
  autosweep gallery, but it should be captioned as a membership-reuse
  recomputation rather than a new exhaustive search.
- Never use legacy normalized-single versus selected Best-N as the only visual
  method contrast. Add corrected adaptive Best-1 on the same exact points and
  probability scale so selector repair and ensemble-size gain remain separate.
- The verifier checks structural completeness without forcing line counts over
  very large CSVs by default. Large tables are checked for existence, size, and
  headers unless `--count-large-csv` is requested.
- The per-BPM feature pass writes shard CSVs and merges them deterministically;
  this uses more scratch disk during the pass but avoids leaving Spark mostly
  idle on one Python process.

## DD-022: Ensemble size requires leakage-controlled, disjoint validation

Decision:
- Select per-spill Best-N members only from a fit-window prefix and purge every
  later window whose 4096-turn support overlaps that prefix.
- Keep same-digitizer sibling channels in one validation fold. Evaluate the
  selected training-digitizer ensemble and a median-power held-out-digitizer
  reference on the same later windows.
- Report blind full-band tune agreement separately from support conditioned
  near the training tune. Choose the smallest non-inferior N only after blind
  agreement, channel-disjoint tune difference, selected contrast, and held-out
  contrast all pass declared margins.
- Collapse repeated folds within each spill before a moving-block bootstrap
  within acquisition collection. Require beam-width, fit-window, fold-seed,
  bootstrap-block-length, and cross-collection global-N sensitivity checks.
- Execute beam 16/32/64, fit-prefix 4/8/16, and three fold seeds as seven unique
  stratified sample runs with one shared baseline. Keep this convergence matrix
  distinct from the all-row primary curve.
- Keep a no-recommendation sensitivity run as a valid analysis outcome, but do
  not materialize a poster or paper that claims a selected N while any declared
  beam/fit/fold run lacks an eligible recommendation in either plane.
- Fail closed on the declared full/sample cache-row counts, contiguous N and
  fold coverage, exact member cardinality and masks, purged timing, finite
  metrics, detail/summary agreement, cross-collection products, native plots,
  and the three-larger-N recommendation boundary. No automatic recommendation
  remains a reportable scientific outcome rather than a structural failure.
- Bind every resumable or sharded pass to a machine-readable, checksummed run
  contract. A changed parameter, incomplete shard-index set, incompatible
  shard contract, or duplicate science key is an error; mergers may not hide a
  partitioning defect by retaining one duplicate row. Resume and comparison
  completeness require the exact contiguous N/fold key grid, not only a row at
  the requested maximum N or an intersection between runs.
- Apply exact selected memberships through 50000 turns for persistence plots.
  Pair every subtractive legacy comparison by exact collection/spill/window
  key and refuse any row whose selected cardinality is smaller than N.
- Preserve unsmoothed per-turn paired width, entropy, peak-bin, and shared-ridge-
  mass contrasts as data, while limiting smoothing to labeled review curves.
  Treat all five as pick-distribution diagnostics rather than physical-noise or
  extraction-onset measurements.
- Stack selected H/V turn contrasts at full publication width with one shared y
  scale; do not shrink native axis labels into separate half-column panels.
- Persist every full-buffer generation warning and reject the ridge gallery if
  spill/window coverage, exact legacy pairing, tune-band bounds, selected
  cardinality, paired metrics, or any manifest PNG/caption is incomplete.
- Map density bins to proportional inclusive pixel bounds. Floor-dividing plot
  height by tune-bin count can leave an unfilled band and make percentile
  overlays appear at the wrong tune even when the underlying rows are correct.
- Treat intensity as a separately tested covariate or spectral aggregation
  weight. Retain it only after FDR-corrected block-aware evidence, a minimum
  practical effect, a median tune shift within tolerance, and at least 95% of
  spillwise tune shifts within tolerance; never multiply it into position.
- Preserve the position-only estimator when intensity is unusable: all weighted
  methods fall back to unweighted aggregation for an all-nonfinite window, while
  a finite but empty 50% gate retains its strongest finite selected member.
  Export both the per-window reason and per-spill fallback fraction.
- Join every intensity density subtraction on identical exact
  collection/spill/plane/N/window/center keys and use only common finite in-band
  picks. Describe red/blue as higher/lower column-normalized ridge-pick
  probability versus unweighted aggregation; absolute-P99 clipping is a
  display choice, not evidence of physical denoising.
- Map intensity ridge, subtraction, and binned relationship rasters with
  proportional inclusive pixel bounds. Disclose nonzero-P98 count clipping and
  absolute-P99 subtraction clipping in both visible or indexed figure copy.
- Require advertised and on-disk sample counts to agree for both members of
  every position/intensity pair; shorter-member truncation is not a valid way
  to hide a payload-length mismatch.
- Require the known 23999-pair capture shape, zero first-50000-turn corruption
  or analysis errors, complete 180-window method grids, exact Best-1 equality
  under all weighting methods, reproducible effect-gate decisions, and every
  indexed gallery asset before closing the intensity question.
- Compare exact retained-effect identities across 10/20/40-spill summaries;
  equal counts with different retained methods are a failed sensitivity gate.
- Materialize poster copy, paper tables, and final figure filenames from the
  accepted roots in one command. Placeholder rejection alone does not prove
  that a manually copied image and a reported number share provenance.

Why:
- Adaptive training score alone is vulnerable to look-elsewhere bias and can
  improve monotonically with N without improving reproducibility.
- Conditioned support can appear good even when two independent channel groups
  choose different full-band peaks. The blind metric makes that failure visible.
- Adjacent spills and heavily overlapping turn windows are not independent.
- Collection and turn-series endpoints are not adjacent, so block resampling is
  non-circular. Full non-wrapping blocks are drawn and only the assembled draw
  is truncated to the observed length.
- Monte Carlo sign-flip tests execute the configured sample count exactly; a
  hidden runtime cap would make the method record disagree with the inference
  actually performed.
- Exact point pairing prevents missing difficult windows from masquerading as
  contrast improvement in a density-difference image.
- Binding all intensity methods to the same spill population, selected members,
  and contracted center grid makes that exact-pair statement independently
  verifiable rather than an assumption about producer loop order.

Tradeoffs:
- The held-out median-power pool is a conservative internal reference, not an
  external tune label.
- Beam search is approximate above N=1; convergence checks are part of the
  result and no automatic knee is reported when the curves remain unresolved.
- The 50000-turn plot holds early-selected members fixed. It tests persistence,
  not same-window dynamic reselection.
- Ridge concentration and subtractive maps can support a claim of reduced
  diffuse ridge-pick probability, but not physical noise removal or absolute
  tune accuracy. P98/P99 clipping is a disclosed raster-only display choice;
  exported values remain unclipped.
- The intensity sidecar can produce useful integrity and timing diagnostics
  even when weighting is rejected; lag and crossing plots remain exploratory.

## DD-023: Publication figures and continuations must be semantically verified

Decision:
- A named figure must be generated from the table or array named by its caption;
  compatibility placeholders may not reuse unrelated BPM-inclusion bars under
  rank, synergy, cluster, duration, or fixed-set filenames. Ring plots use the
  source-token ring order, and Pareto plots use the exported compute cost rather
  than relabeling subset size.
- Render key poster deconstruction, visible-set, Best-N, intensity, and ridge
  candidates with the deterministic native PNG path so an optional plotting
  package cannot silently replace them with text files.
- Put only strict `VISIBLE_TUNE` channels in handoff sets. Treat empty-to-empty
  Jaccard as one, and distinguish visibility loss, recovery, no-visible-set,
  flicker, stable membership, and persistent replacement. Preserve nested
  Top-1/3/5/10 memberships and render all selected spill-plane composites;
  score color, rank markers, and consensus tune must remain distinct encodings.
- Correct historical whole-span `visibility_duration_turns` values from the
  exact cache before dependent statistics. Preserve scores and memberships and
  record row-level changes plus before/after hashes.
- Treat full-pipeline `--resume` as cache reuse only. After an externally
  completed subset search, continuation scripts must invoke downstream stages
  explicitly and may not rerun the search.
- Verification must check primary cross-table identity and follow-up semantics,
  including shared score formulas, held-out cardinality/finite metrics, state
  transitions, plane balance, and every recommended poster PNG.
- Best-N publication copy must state the full-curve and stratified-validation
  case counts separately. Generate those values from the accepted verifier and
  reject a final payload whose case, fold, N, or evaluation-row counts differ
  from the definitive design.
- Keep the artifact-tool direct PNG as a geometry diagnostic. The named poster
  PNG must instead be the byte-identical 150 dpi PDF raster so inherited
  master-level Fermilab/DOE artwork is present in both rendered deliverables.

Why:
- Correct filenames and valid CSV headers do not prove that a figure visualizes
  the claimed quantity.
- A constant top-five count and an empty-set Jaccard of zero manufacture visual
  handoff structure where no channel passed the visibility threshold.
- Repeating a multi-hour search after completion wastes scarce GPU time and can
  overwrite a validated result with a parameter-drifted rerun.
- Artifact-tool preserves master media in the editable PPTX but does not render
  that media in its direct slide PNG. A PDF-derived final PNG avoids presenting
  a brand-incomplete preview as the poster deliverable.

Tradeoffs:
- Semantic verification reads more small/medium tables and intentionally fails
  environments that cannot produce required PNGs.
- Strict visibility may leave sparse H-plane handoff plots. That sparsity is a
  result; weak/no-reliable windows remain available in CSV diagnostics without
  being promoted into visible sets.

## DD-024: Raw publication payloads require an independent producer-integrity gate

Decision:
- Treat `TBT_POSITION_RAW` and `TBT_INTENSITY_RAW` as the publication source
  boundary, not the scaled orbit products. The checked-out producer inserts
  device-coded below-threshold values only in scaled arrays; a same-ID live
  raw/scaled comparison confirmed that separation despite source/runtime drift.
- Scan every publication raw stream through turn 50000 independently of the
  spectral analysis. Reject nonfinite samples, byte/sample-count drift, exact
  plateaus of at least 128 turns, and repeated device-coded fallback pairs.
- Require the exact 2200-manifest, 263999-position-row, 23999-paired-row report
  in publication materialization and finalization. Report the known incomplete
  intensity manifest rather than silently normalizing it away.

Why:
- Finite sentinels can evade ordinary numerical plausibility checks and create
  step spectra or false late-spill structure. A clean FFT output is not proof
  that its source waveform was semantically raw.
- Bind-mounted source changed after the representative Python process started,
  so current files alone cannot establish historical runtime behavior.

Tradeoffs:
- The audit rereads roughly 264000 first-50000-turn payload slices and adds a
  serial I/O pass. It does not alter source data and is cheaper than accepting
  an untraceable publication artifact.

## Decision Update Rule

When changing one of these decisions, update:
1. this file,
2. `docs/ARCHITECTURE.md`,
3. `docs/PLAN.md` (if plan alignment changes), and
4. `docs/USAGE.md` for user-visible behavior changes.

Update `README.md` only when the project overview, command map, or documentation
routing changes.
