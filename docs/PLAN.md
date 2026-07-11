# Tune Analysis Plan (Mapped to Implementation)

This document translates the methodology from:
- `ChatGPT - Synchrotron Tune Calculation plan.pdf`

into an implementation-facing roadmap and a gap report.

## Purpose

Use synchronized Delivery Ring BPM TbT position streams to estimate `Qx` and `Qy`:
- first in a robust injection window
- then across sliding windows versus time
- with quality/timeliness diagnostics
- and eventual comparison against Schottky measurements

## Plan Summary (from PDF)

1. Treat each spill as a synchronized multi-BPM snapshot.
2. Start with a configurable early-time window near injection.
3. Remove per-BPM closed-orbit offsets before spectral analysis.
4. Combine many BPMs to improve SNR (spectrum average, SVD/PCA, etc.).
5. Estimate tune by peak search in expected bands with confidence/uncertainty.
6. Extend to sliding windows for tune-vs-time.
7. Validate against Schottky in matched time slices.

## Plan vs Implementation

Legend:
- `Implemented`: in current code path and outputs.
- `Partial`: available but not fully aligned with PDF intent.
- `Not Yet`: not currently implemented.

### 1) Synchronized multi-BPM spill snapshot

Status: `Implemented`

What exists:
- Global spill snapshots across all configured streams.
- Stream-ID alignment logic using target millisecond selection.
- Adjacent-bucket tolerance (`±1 ms`) for both live and historical/no-beam paths.

Notes:
- This was added to reduce split-target artifacts (for example `96/24` across neighboring ms).

### 2) Configurable early-time injection window

Status: `Implemented`

What exists:
- Configurable `injection_start_turn` and `injection_window_turns`.
- CLI overrides for analysis commands.

### 3) Per-BPM mean removal / preprocessing

Status: `Implemented`

What exists:
- Mean subtraction per trace/window.
- Hann windowing before FFT.
- DC suppression (`bin 0 = 0`) and low-bin exclusion in peak search.

### 4) Multi-BPM combination strategy

Status: `Partial`

What exists:
- Multi-BPM averaging paths and per-BPM method comparison artifacts.
- Weighted/unweighted analysis options in study workflows.
- Optional representative-spill SVD/PCA denoising products in the standalone
  raw captured-spill GPU analyzer.

Divergence:
- SVD/PCA is not a production Rust tune-extraction path; it is an offline
  comparison/denoising product for poster analysis.
- Phase-aware lattice combination is not implemented.

### 5) Tune extraction + confidence/uncertainty

Status: `Partial`

What exists:
- Band-limited peak pick with confidence gate (`min_peak_confidence`).
- Per-plane confidence metrics and quality flags.

Divergence:
- No full uncertainty model yet (for example statistical confidence intervals).
- No dedicated peak-width metric exported as a first-class field.

### 6) Sliding-window tune vs time

Status: `Implemented`

What exists:
- Configurable sliding windows/stride.
- Optional flashpoint sampling mode (`--flashes N|max`) for evenly spaced
  fixed-count windows across spill depth, bounded by available turn count.
- In flash mode, injection tune uses `sliding_window_turns` (not
  `injection_window_turns`).
- Tracked local peak logic with fallback and suspicious-step diagnostics.
- Per-spill tune-vs-time plot and sliding CSV output.
- Batch flash-index trend and histogram plots
  (`tune_vs_spill_flash_XX.png`, `tune_histogram_flash_XX.png`) when flash
  sampling is enabled.

### 7) Validation against Schottky

Status: `Partial`

What exists:
- External reference matching in batch mode via reference files.
- Residual plot generation when references are present.

Divergence:
- No direct Schottky data ingestion/auto-sync pipeline in this repository.
- Matched-slice comparison requires external preprocessing and reference-file creation.

### 8) Signal quality checks requested in PDF

Status: `Partial`

What exists:
- Alignment fraction diagnostics.
- Incomplete-poll warnings and quality flag (`INCOMPLETE_TBT_POLL`).
- Timeliness statistics (`obs_ms - target_ms`) at spill and batch levels.

Divergence:
- Explicit clipping/saturation detection is not yet implemented.
- Spectral coherence metric across BPMs is not yet exported as a dedicated statistic.

### 9) Full-spill coverage assumption vs available TbT length

Status: `Partial`

What exists:
- The implementation analyzes whatever turn depth is present in payloads and reports window-consensus constraints.

Divergence:
- The PDF notes a potential mismatch between nominal spill duration and currently available TbT payload depth.
- The code does not yet enforce or report a dedicated "coverage of nominal spill duration" metric; it works on available turns.

## Current Priorities

1. Keep synchronized capture robust under real-world timestamp jitter.
2. Preserve least-transformed RAW position payloads for offline reanalysis.
3. Capture auxiliary RAW intensity payloads so later quality studies can compare
   beam/intensity behavior against position traces.
4. Preserve diagnostics even when data is incomplete (warn/flag instead of silent drop).
5. Provide timing observability so data freshness/jitter can be trended.
6. Keep outputs compatible with external reference validation workflows.

## BPM-Only Poster/DGX Sprint

Status: `Implemented as standalone scripts`

`BPM_DGX_POSTER_CODEX_PLAN.md` is handled by `scripts/bpm_dgx_poster.py` and
the thin phase wrappers in `scripts/`. This is a downstream poster-analysis
layer over already collected artifacts, with the complete data source expected
to be the `drbpm1` artifact tree (`/home/derekste/out`) or a DGX-mounted/copy
of that tree.

Implemented poster-phase products include:

- dataset manifest and summary from `candidate_spills.csv`,
  `spills_summary.csv`, and `capture_index.csv`
- baseline BPM tune summaries and reproducibility plots
- flash-mode summaries for requested flash counts
- selected-tune waterfall and median trace-density products
- conservative subset-consistency proxy outputs when per-BPM spectra are not
  present
- weak-label quality classifier reports
- tune/ridge model skip report when no independent labels are available
- synthetic CPU/CUDA FFT benchmark and poster plot index
- raw captured-spill CuPy/CUDA flash analysis directly from payload bundles,
  with GPU spill summaries, sliding/flash CSVs, waterfalls, median
  spectrograms, tune-ridge density plots, Hann/multitaper representative
  spectrogram overlays, dynamic-programming ridge traces, optional SVD/PCA
  denoising products, and DGX benchmark markdown/plots

This sprint intentionally excludes Schottky comparison and Schottky-derived
labels. Reference-monitor validation remains a separate roadmap item.

## Spark BPM Autosweep, Ranking, And Classification

Status: `Implemented as staged standalone scripts`

`SPARK_BPM_AUTOSWEEP_RANKING_AND_CLASSIFICATION_PLAN.md` is handled by the
Stage 0/autosweep/ranking scripts in `scripts/`. The implementation is a
BPM-only staged search over raw captured position bundles, not a full Cartesian
sweep.

Implemented pieces:

- `scripts/build_collection_manifest.py`: raw bundle discovery, collection
  tier/view assignment, stream counts, plane availability, waveform length, and
  completeness fields.
- `scripts/validate_spill_integrity.py`: payload health checks for missing,
  constant, clipped, non-finite, RMS/MAD, usable/reject reason fields.
- `scripts/build_spill_cache.py`: lightweight decoded metadata cache for
  manifests, BPM/plane counts, turn counts, health flags, and manifest lists;
  no FFT products are cached.
- `scripts/gpu_analyze_captured_spills.py`: additional offline knobs for turn
  range, plane, BPM combination, BPM normalization, detrending, DC handling,
  and H/V ridge-anchor priors.
- `scripts/run_autosweep.py`: deterministic baseline/factor/pilot/full runner
  with config hashes, manifest lists, resume/cached detection, run logs,
  opt-in parallel config/view execution, and optional GPU telemetry summaries.
- `scripts/rank_autosweep_results.py`: required weighted scores, spill/config
  labels, ranked spill/config/collection tables, rejected configs, and
  `top_configs_for_full.csv`.
- `scripts/make_initial_analysis_summary.py`: initial summary markdown,
  lightweight score plots, and top-artifact collation.
- `scripts/build_elite_full_stage.py`: usable Tier A manifest filtering,
  effective-config deduplication, explicit H/V/poster elite lists, and
  rejected/flagged diagnostic preservation.
- `scripts/make_elite_full_summary.py`: elite full-stage summary and heavy
  artifact collation for best H/V, robust H/V, and poster candidates.

Tier A inputs are the two Spark raw position-only collections under
`/home/derekste/tbt-spills-2000`. Tier B intensity/beam-loss support remains a
later-capable extension and should not block Tier A autosweep outputs.

The ranker uses the fixed score formula:

```text
0.25 injection + 0.25 ridge + 0.20 bpm_robustness
+ 0.15 spectrogram_quality + 0.10 usable_fraction
+ 0.05 compute_efficiency
```

Spill labels are `GOOD`, `MARGINAL`, `BAD`, `NO_SIGNAL`,
`AMBIGUOUS_RIDGE`, and `MISSING_DATA`. Config labels are `RECOMMENDED`,
`PROMISING`, `EXPLORATORY`, `REJECTED`, `TOO_SLOW`, `OVERFITS_BAND`,
`UNSTABLE_H`, and `UNSTABLE_V`.

## Best-BPM 2000-Spill Mining

Status: `Implemented; corrected publication rerun and validation sidecars active`

`BEST_BPM_2000_SPILL_MINING_IMPLEMENTATION_PLAN.md` is implemented as
`scripts/bpm_mining/` plus pass wrappers. The pipeline mined the two Tier A
Spark position-only collections without assuming a fixed tune, monotonic
chronological trend, or external tune truth.

Implemented structure:

- manifest/integrity pass with stable plane-local BPM indices and explicit
  channel rejection flags
- GPU/CPU spectral-cache pass with resumable per-spill `.npy` products and
  parallel workers
- sharded per-BPM peak feature extraction and cache-backed within-spill tune
  consensus clustering
- row-sharded exact best-1 and best-3 enumeration over valid BPMs
- row-sharded screened-pool best-5 enumeration with beam/random audit records,
  live per-shard progress JSON, and a bounded CUDA worker pool; the older
  screened best-10 capability remains available but is not the publication
  ensemble-size method
- evolution/statistics/clustering/artifact/report outputs matching the plan
  layout, including cache-backed finalist re-evaluation across robust spectrum
  aggregators and paired bootstrap/permutation/FDR subset-size comparisons
- follow-up sidecars for same-metric dynamic/fixed/all-BPM recomputation,
  held-out spectral support, curated poster-review PNGs, BPM
  handoff/visibility migration, and full-buffer Best-ensemble ridge-density
  comparison
- a narrowly scoped visibility-duration repair that reproduces the canonical
  cache exactly and changes only the previously overstated duration field;
  before/after row hashes make that mutation auditable
- exact identity normalization for same-digitizer sibling channels and channel
  token-derived ring order across every downstream reconstruction
- contiguous leakage-controlled Best-N selection with complete window purging,
  digitizer-disjoint later-window validation, non-circular moving-block
  intervals, declared-count sign-flip inference, beam/fit/fold/block-length
  sensitivity, and cross-collection
  global-N transfer, plus a reusable seven-run sensitivity matrix and a
  fail-closed output verifier. The matrix remains serial by default and exposes
  only the measured two-evaluator Spark mode, guarded by a sustained Linux
  `MemAvailable` floor and resumable per-run checkpoints.
- a 200-spill exact-pair intensity sidecar with block-aware paired inference,
  10/20/40-spill block-length sensitivity, practical-effect gates,
  payload-horizon auditing, and an indexed review gallery
- strict intensity closure over the audited 23999-pair capture, complete
  90-window 4096/512 spill grids, exact Best-1 weighting invariance, effect
  decisions, and all gallery assets
- verifier-bound IBIC materialization that carries independent H/V Best-N
  recommendations into a plane-selected ridge composite, poster copy, paper
  table, exact figure copies, results payload, and source-hash manifest
- `verify_best_bpm_outputs.py` checks required output groups, CSV schemas,
  row counts where practical, global/per-spill artifacts, and final reports
  before a Spark run is treated as complete
- the primary and follow-up semantic verifiers reconstruct source identities,
  result cardinalities, masks, fixed-set controls, held-out rows, handoff state
  transitions, plane-balanced poster selection, and every recommended PNG;
  file existence alone is not publication closure
- fixed and held-out sidecars preserve explicitly flagged no-visible/no-q rows
  as unavailable rather than zero-valued evidence, while summaries expose the
  fraction that is actually evaluable
- the handoff review renders strict Top-1/3/5/10 state changes, global per-turn
  membership maps, and every selected spill-plane score/rank/consensus
  composite without imposing an extraction onset
- `verify_best_n_outputs.py` separately checks exact N/fold coverage,
  memberships, purged timing, finite validation metrics, summary counts,
  recommendation boundaries, transfer rows, and figure products

Current publication interpretation:

- The publication ridge-density sidecar recomputes 0-50000 turn spectra
  from raw captured spills while holding fit-prefix Best-N memberships fixed.
  This is the intended persistence comparison to the older `18d321db` plots,
  not same-window dynamic reselection. Only the corrected rerun that passes the
  exact coverage/pairing verifier is final; the June gallery is provisional.
  Every requested N receives a shared-scale H/V-by-method composite in addition
  to its single-plane and subtractive diagnostics; no composite is interpreted
  without exact paired counts and sample-fraction checks.
  Plane-selected review also includes corrected Best-1 versus selected Best-N
  and a legacy/corrected-Best-1/selected three-column control. This prevents the
  historical selector defect from being presented as an ensemble-size gain.
  If H and V select different N, an additional contract-bound mixed composite
  uses the corresponding membership for each row and selected-N concentration
  panels keep the H-loss view legible.
  Corrected native rasterization fills the complete declared tune axis with
  proportional cells, keeping density colors and percentile overlays aligned.
  Disclosed P98 density and absolute-P99 subtraction clips affect only display
  contrast; visible legends and captions describe higher/lower pick
  probability, and exported quantitative rows remain unclipped.
  Exact-paired turn tables retain unsmoothed adaptive-minus-legacy width,
  entropy, peak-bin, and shared-ridge-mass contrasts; review PNGs apply only
  five-window visual smoothing. These locate changes in ridge-pick
  concentration without assigning a fixed extraction onset or claiming
  physical noise removal.
- Best-N, intensity, and full-buffer ridge passes write checksummed JSON run
  contracts before science output. Resumes reject parameter drift; merges
  require complete compatible shard sets and reject duplicate science keys.
- Paper prose values that depend on primary or intensity tables are generated
  as verifier-bound LaTeX macros; only explanatory scientific text remains
  static in the manuscript source.
- Final delivery is not inferred from build exit codes alone. A separate
  finalizer requires explicit poster and paper visual-QA passes, rechecks page
  geometry and payload closure, requires the named poster PNG to be the
  byte-identical PDF raster with inherited master artwork, rejects empty
  structural placeholders found by a read-only scan of final slide XML, and
  recomputes portable poster/paper build manifests and zero-issue template
  fidelity before inventorying every delivered publication file by SHA-256.
- Full-buffer galleries are publication-eligible only after their strict
  spill/window, exact-pair, metric, warning, PNG, and caption verifier passes.
- Publication materialization also requires the independent Delivery Ring raw
  payload audit over all 2200 manifests and first 50000 turns; no spectral
  result can waive a payload-integrity failure.
- The old ``Best-10 deferred'' note is historical, not an active missing run.
  The publication evaluates every N contiguously through at least N=20 (target
  N=30) under the separate leakage-controlled protocol. The bounded N=30 trial
  put the V knee at the upper boundary, so the definitive July pass extends
  contiguously through N=40 under the predeclared boundary rule.
- Intensity weighting is rejected unless block-aware FDR, practical effect,
  median tune-shift, and 95% spillwise tune-shift criteria all pass. Integrity
  and timing diagnostics remain useful even when weighting is rejected.
  Subtractive intensity figures require identical exact finite spill/window
  points and may claim only higher/lower column-normalized ridge-pick
  probability; their absolute-P99 color clip is display-only. All intensity
  heatmaps fill the complete axis with proportional cells, and count-density
  captions disclose nonzero-P98 display clipping.
  Concentration and crossing-turn diagnostics retain common-scale panels beside
  explicitly guarded detail-scale variants; neither detail scale supports
  cross-panel amplitude or extraction-timing claims.
  Lag correlation similarly retains common -1-to-1 and symmetric detail views;
  autoscaling does not make overlapping windows independent or causal.

## Next Milestones

Post-split analysis refinement:

1. Complete the corrected exact-identity Best-1/3/5 run, Best-N curve and
   sensitivities, exact-point-paired 50000-turn gallery, visibility-duration
   repair, corpus-wide raw-payload audit, final poster/paper, and publication
   artifact manifest plus post-transfer review-package verification; keep
   deficiencies tracked in GitHub issue #39.
2. Add explicit spectral-coherence and clipping diagnostics to production
   analysis summaries when the autosweep identifies stable criteria.
3. Export peak-width and uncertainty-oriented metrics in summaries/CSV.
4. Promote or reject the standalone SVD/PCA denoising findings after physics
   review; do not make SVD the default without that review.
5. Add a first-party Schottky reference ingestion path (or converter contract)
   to reduce manual matching.

The acquisition/offline-analysis split is tracked in
`docs/ISSUE_MAP_DAQ_SPLIT.md`.

Completed split work now covers versioned captured-spill bundles, live
one-shot/free-run capture, same-spill DAQ diagnostics with explicit timestamp
distributions, `assess`, `diagnose-captures`, offline single/batch
captured-bundle analysis, a minimal online/offline parity guardrail, and
auxiliary RAW intensity capture derived from configured RAW position streams.
See `docs/USAGE.md` for command usage and `docs/ISSUE_MAP_DAQ_SPLIT.md` for the
issue history. The parity guardrail is a split regression check, not physics
certification of the current algorithm.

## Open Physics Questions (still external)

1. Preferred injection-time window definition for operational reporting.
2. Final tune search bands by plane for production operation.
3. Whether passive coherent motion is sufficient in all operating regimes.
4. Expected tune drift scale versus window size, especially during extraction dynamics.

## Revision Discipline

When analysis behavior changes, update this file and classify impact as one of:
- `matches plan better`
- `acceptable divergence`
- `new divergence requiring review`
