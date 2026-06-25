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
2. Preserve diagnostics even when data is incomplete (warn/flag instead of silent drop).
3. Provide timing observability so data freshness/jitter can be trended.
4. Keep outputs compatible with external reference validation workflows.

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
  with config hashes, manifest lists, resume/cached detection, and run logs.
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

Status: `In Progress`

`BEST_BPM_2000_SPILL_MINING_IMPLEMENTATION_PLAN.md` is being implemented as
`scripts/bpm_mining/` plus pass wrappers. The pipeline mines the two Tier A
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
- row-sharded screened-pool best-5 and best-10 enumeration with beam/random
  audit records, live per-shard progress JSON, and a bounded CUDA worker pool
- evolution/statistics/clustering/artifact/report outputs matching the plan
  layout, including cache-backed finalist re-evaluation across robust spectrum
  aggregators and paired bootstrap/permutation/FDR subset-size comparisons
- `verify_best_bpm_outputs.py` checks required output groups, CSV schemas,
  row counts where practical, global/per-spill artifacts, and final reports
  before a Spark run is treated as complete

Current documented implementation choice to revisit:

- Full-buffer rolling evolution is represented by the same output schema, but
  v1 uses cached early rolling spectra unless a longer raw/full-buffer path is
  explicitly run. This keeps the pass runnable on Spark while preserving a clear
  path to the more expensive full-buffer rerun.

## Next Milestones

Post-split analysis refinement:

1. Run the Best-BPM mining pipeline on Spark over the usable Tier A collections
   and verify the output directory with `scripts/verify_best_bpm_outputs.py`
   before reviewing `reports/strong_bpm_analysis_summary.md`.
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
captured-bundle analysis, and a minimal online/offline parity guardrail. See
`docs/USAGE.md` for command usage and `docs/ISSUE_MAP_DAQ_SPLIT.md` for the
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
