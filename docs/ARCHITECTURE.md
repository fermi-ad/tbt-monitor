# Architecture

## Scope

`tbt-monitor-tui` is a single Rust binary with these functional areas:
- config import and validation
- live stream monitoring (TUI)
- raw spill capture and capture timing diagnostics
- live one-spill tune analysis
- offline captured-bundle analysis
- robustness studies
- multi-spill batch analysis
- standalone BPM-only poster/DGX artifact processing scripts
- staged BPM-only Spark autosweep, ranking, and classification scripts

The program reads Redis stream data from many BPM devices and converts it into synchronized spill-level artifacts and summary records.

User command workflows live in `docs/USAGE.md`; this file focuses on module
boundaries, data flow, synchronization policy, and artifact contracts.

## Module Map

- `src/main.rs`
  - CLI surface and command dispatch.
  - Applies CLI overrides to config before invoking feature modules.
- `src/config.rs`
  - Defines config schema and parser.
  - Enforces validation invariants.
  - Writes normalized config files.
- `src/importer.rs`
  - Converts ACNET XML into monitor config.
  - Selects relevant BPM stream/trigger keys.
- `src/monitor.rs`
  - Device worker runtime for live monitoring.
  - Uses stream-native `XREAD BLOCK` and reconnection backoff.
- `src/capture.rs`
  - Raw synchronized spill capture.
  - Writes captured-spill bundles without running tune analysis.
  - Emits manifest, raw payload files, summaries, and multi-spill indexes.
- `src/analyze.rs`
  - Spill snapshot construction.
  - Captured-spill manifest/payload loading for offline analysis.
  - FFT-based tune extraction and sliding analysis.
  - Study and batch workflows.
  - Artifact and summary generation.
- `scripts/bpm_dgx_poster.py`
  - Standalone poster-analysis entrypoint for already collected artifacts.
  - Consumes `candidate_spills.csv`, `spills_summary.csv`, and
    `capture_index.csv`.
  - Writes poster manifest, baseline/flash summaries, trace-density
    waterfalls, weak-label quality reports, and CPU/CUDA benchmark artifacts.
  - Does not connect to Redis and does not participate in runtime safety checks.
- `scripts/gpu_analyze_captured_spills.py`
  - Standalone raw captured-spill analyzer for `manifest.json` plus binary
    payload bundles.
  - Uses NumPy as the reproducibility baseline and CuPy/CUDA for batched Hann
    or multitaper FFT power, peak picking inputs, dynamic-programming ridge
    extraction inputs, and flash-mode processing on Spark.
  - Writes GPU spill summaries, sliding/flash CSVs, median tune plots, flash
    waterfalls, median band spectrograms, ridge-density plots, representative
    Hann/multitaper spectrogram overlays, DP ridge traces/overlays, optional
    SVD/PCA denoising products, and DGX benchmark markdown/plots.
  - Does not connect to Redis and does not replace `MonitorConfig::validate()`
    or runtime safety checks.
- `scripts/build_collection_manifest.py`, `scripts/validate_spill_integrity.py`,
  `scripts/build_spill_cache.py`
  - Stage 0 autosweep inventory and health tooling for raw captured bundles.
  - Discover manifest trees, classify collection tier/view, record waveform
    length and H/V availability, flag missing/constant/clipped/non-finite
    payloads, and write a lightweight metadata cache without FFT products.
- `scripts/run_autosweep.py`
  - Deterministic staged autosweep orchestrator.
  - Builds baseline, factor-screening, and capped pilot config grids using
    canonical JSON hashes for resume-safe job directories; full mode consumes
    a supplied elite config list exactly.
  - Invokes `gpu_analyze_captured_spills.py` through manifest-list files.
- `scripts/rank_autosweep_results.py`,
  `scripts/make_initial_analysis_summary.py`,
  `scripts/build_elite_full_stage.py`,
  `scripts/make_elite_full_summary.py`
  - Reduce analyzer outputs into spill/config/collection scores, labels,
    ranked tables, explicit elite full-stage handoff lists, lightweight plots,
    heavy-artifact galleries, and autosweep markdown summaries.

## Runtime Data Flow

### Monitor (`monitor` command)

1. Load and validate config.
2. Spawn one worker per device.
3. Each worker:
   - validates stream type/state
   - blocks on `XREAD`
   - updates latest IDs/arrival counters
   - emits `DeviceUpdate` to UI channel
4. TUI renders per-device stream health and arrival status.

### Analyze Spill (`analyze-spill`)

1. Collect latest TbT observations across configured streams.
2. Select `target_ms` using clustered timestamp buckets (adjacent tolerance).
3. Pull near-target entries per stream and decode payloads.
4. Split by plane, enforce consensus window/length validity.
5. Perform injection-window and sliding-window spectral analysis.
   - Optional flash mode (`--flashes N|max`) samples evenly spaced fixed centers and
     overrides stride-based sliding-window placement.
   - In flash mode, injection tune estimation uses `sliding_window_turns`.
6. Emit plots/CSV and a text summary with quality + timeliness diagnostics.
7. In `--free-run`, repeat until Ctrl-C or optional `--count` successful analyses.
8. In `--free-run --count`, synthesize batch summary/composite outputs for the
   collected spills at exit.

### Capture Spill (`capture-spill`)

1. Collect latest TbT observations across configured streams.
2. Select `target_ms` using same-spill clustering (`same_spill_tolerance_ms`,
   default `25 ms`).
3. Pull near-target Redis stream entries from every configured TbT stream.
4. Persist raw `_` payload bytes without decoding them for tune analysis.
5. Emit a captured-spill bundle with `manifest.json`, `capture_summary.txt`,
   `payloads/*.bin`, and per-spill capture diagnostics. The summary reports
   captured-payload timestamp distributions separately from latest-ID snapshot
   distributions.

### Capture Spills (`capture-spills --free-run`)

1. Spawn stream-watch workers to detect new arrivals.
2. Each wake triggers one global all-stream capture snapshot.
3. Suppress duplicate physical spills using same-spill tolerance.
4. Write one captured-spill bundle per unique target.
5. Maintain `capture_index.csv` plus stream, digitizer, JSON, and markdown
   quality diagnostics, including exact timestamp delta distributions.

### Assess (`assess`)

1. Collect an initial latest-ID snapshot across configured TbT streams.
2. Watch for the requested number of new machine events, defaulting to one.
3. Re-read latest IDs after each event without fetching raw payload data.
4. Emit stream/digitizer CSV, JSON, and markdown preflight diagnostics.

### Diagnose Captures (`diagnose-captures`)

1. Discover existing captured-spill manifests from one manifest, one bundle, or
   a directory of bundles.
2. Recompute capture timing diagnostics without Redis or payload reads.
3. Emit the same run-level capture diagnostic files used by live capture.

### Analyze Captured Spill (`analyze-captured-spill`)

1. Load `manifest.json` from a captured-spill bundle directory or explicit
   manifest path.
2. Validate `schema_version=1` and artifact type
   `tbt-monitor.captured-spill`.
3. Resolve payload paths relative to the bundle directory and reject unsafe
   absolute/parent-relative paths.
4. Verify byte counts and `fnv1a64` checksums when present.
5. Decode raw little-endian `f32` payload files into the same `StreamTrace`
   inputs used by live analysis.
6. Reconstruct `TbtObservation` entries from captured stream IDs and
   `target_ms`.
7. Reuse the existing one-spill analysis/output path. No Redis connection is
   opened.

### Analyze Captured Spills (`analyze-captured-spills`)

1. Discover captured-spill bundles from a bundle directory, explicit
   `manifest.json`, or an immediate-child bundle directory scan.
2. Validate each manifest with the same schema and payload safety checks used
   by `analyze-captured-spill`.
3. Sort candidates by `target_ms` and suppress duplicate physical spills using
   the configured adjacent-target tolerance.
4. Reconstruct one analysis snapshot per usable bundle without opening Redis.
5. Reuse existing batch record, quality, reference matching, plot, composite
   waterfall, detailed-artifact, and markdown summary writers.
6. Record `trigger_source=captured-spill`; the batch path does not read Redis
   trigger keys.

### Spark BPM Autosweep (`scripts/run_autosweep.py`)

1. Stage 0 discovers raw captured-spill manifests and records collection tier,
   view, stream counts, plane availability, waveform length, and payload health.
2. The runner builds a deterministic config grid using canonical sorted JSON
   and `sha256[:12]` config hashes.
3. Pilot mode runs baseline configs, factor screening, and a capped randomized
   interaction grid with seed `20260613`.
4. The elite builder filters Stage 0 to usable Tier A spills, selects explicit
   H/V/poster roles from combined-view pilot rankings, deduplicates effective
   configs, and preserves rejected/flagged rows in diagnostics.
5. Full mode runs exactly the supplied elite config list over the filtered
   usable-spill manifest.
6. Each job writes a manifest-list file and invokes the raw captured-spill GPU
   analyzer with explicit turn range, BPM-combination, normalization,
   detrending, DC-handling, tune-band, and ridge-anchor settings.
7. The ranker reads each job's `gpu_spills_summary.csv`, scores spill/config
   rows, assigns stable labels, and writes handoff CSVs for full-stage analysis.
8. The elite summary writer collates ranked tables and heavy GPU plots for the
   best H, best V, robust H/V, and poster candidates.

This workflow is offline and BPM-only. It does not connect to Redis, does not
use Schottky labels, and does not alter Rust runtime command safety checks.

### Analyze Phase (`analyze-phase`)

Builds the same synchronized snapshot, then runs sweeps and method-comparison artifacts for robustness studies.
In `--free-run`, it repeats until Ctrl-C or optional `--count` successful analyses.

### Analyze Spills (`analyze-spills`)

1. Repeat synchronized snapshot collection (live or historical/no-beam).
2. Suppress duplicate physical spills using target-ms tolerance logic.
3. Build per-spill records and quality labels.
4. Emit per-spill and aggregate artifacts (`csv/jsonl/png/md`).

## Timing and Synchronization Policy

The primary timestamp for synchronization is stream ID millisecond (`ms`):

- Target selection clusters adjacent buckets (currently up to `±1 ms`, additionally bounded by `align_tolerance_ms`).
- Historical no-beam candidate ranking merges adjacent buckets before coverage ranking.
- Duplicate suppression in live/batch free-run treats near-equal target ms as same physical spill.

Rationale:
- Real devices can land in neighboring milliseconds for the same spill.
- Raw mode-only selection creates split candidates (`96/24`, `100/20`) and artificial duplication.

## Quality and Diagnostics Model

Diagnostics are intentionally preserved instead of aggressively dropping imperfect spills.

- Warnings are attached to snapshots when:
  - latest-ID same-spill fraction is low
  - complete poll is unavailable
  - near-target reads are incomplete
- Batch quality flags include `INCOMPLETE_TBT_POLL` and other confidence/alignment checks.
- Timeliness metrics (`obs_ms - target_ms`) are emitted per spill and aggregated in batch summary.

Rationale:
- Operational users need to distinguish "analysis failure" from "data quality degradation".
- Keeping marginal outputs supports post-run triage and reanalysis.

## Artifact Contract

Main artifact families:
- captured-spill bundles (`manifest.json`, `capture_summary.txt`, raw
  `payloads/*.bin`)
- per-spill spectra and tune traces (`png`)
- per-spill top-down spectrogram heatmaps (`png`)
- per-spill 2x2 tune-validation composite (`png`)
- per-spill sliding samples (`csv`)
- per-spill summaries (`txt`)
- batch records (`csv`/`jsonl`)
- batch plots (including composite H/V waterfall, and optional per-flash
  `tune_vs_spill_flash_XX` and `tune_histogram_flash_XX`) and markdown summary
- standalone poster products (`dataset_manifest.csv`, baseline/flash summaries,
  trace-density waterfalls, optional weak-label ML reports, DGX benchmark
  summaries, and copied poster-plot index)
- raw captured-spill GPU/poster products (`gpu_spills_summary.csv`,
  `gpu_sliding_tune.csv`, `ridge_density_h/v.png`,
  `single_spill_spectrogram_h/v.png`, `spectrogram_*_{hann,multitaper}.png`,
  `ridge_trace_h/v.csv`, `ridge_overlay_h/v.png`, `bpm_leaderboard.csv`,
  `bpm_leaderboard_h/v.png`, `subset_consistency_h/v.png`, optional `svd_*`
  plots, and `dgx_benchmark.md/png`)
- Spark autosweep products (`dataset_manifest.csv`, `spill_health.csv`,
  `spill_cache_index.json`, `autosweep_config_grid.csv`,
  `autosweep_run_log.csv`, `autosweep_spill_scores.csv`,
  `autosweep_config_scores.csv`, `autosweep_collection_scores.csv`,
  `autosweep_ranked_configs.csv`, `autosweep_ranked_spills.csv`,
  `autosweep_rejected_configs.csv`, `top_configs_for_full.csv`,
  `initial_analysis_summary.md`, `elite_dataset_manifest.csv`,
  `elite_configs_h.csv`, `elite_configs_v.csv`,
  `elite_configs_for_full.csv`, `elite_config_sources.csv`,
  `elite_rejected_config_diagnostics.csv`, `elite_full_summary.md`,
  `elite_artifacts_manifest.csv`, lightweight plots, heavy-artifact galleries,
  and copied top-artifact manifests)

Captured-spill bundle schema:
- `schema_version=1`
- artifact type `tbt-monitor.captured-spill`
- `redis_timestamp_ms`, the selected Redis stream-ID millisecond used as the
  artifact timestamp (`target_ms` has the same value in schema v1)
- target/alignment metadata: `target_ms`, `align_tolerance_ms`,
  `same_spill_tolerance_ms`, `min_aligned_fraction`, latest-observation counts,
  captured stream counts
- stream inventory for all effective capture streams. In the current checked-in
  config this is 120 configured `TBT_POSITION_RAW` streams plus 120 derived
  `TBT_INTENSITY_RAW` streams.
- captured stream entries with BPM/device identity, plane, stream ID,
  stream-ID millisecond, payload file path, byte count, sample count, and
  `fnv1a64` checksum
- `capture_diagnostics` with per-stream reason codes, per-digitizer summaries,
  exact timestamp deltas, delta-count distributions, and complete/partial
  status
- warnings for incomplete target selection, incomplete near-target capture,
  low alignment, missing payload fields, or non-`f32`-sized payloads

Raw payload policy:
- Payload files store Redis stream `_` field bytes exactly as captured.
- Current TbT payload interpretation is little-endian `f32`; capture does not
  run FFT/tune analysis or otherwise transform samples.
- Position streams choose the capture target and feed offline tune analysis.
  Derived intensity streams are auxiliary preservation payloads and are skipped
  by offline tune analysis until intensity semantics are promoted explicitly.
- `capture-spills` writes `capture_index.csv` as the run-level bundle index,
  keyed by `redis_timestamp_ms` / `target_ms`, and also writes
  `capture_spill_diagnostics.csv`, `capture_stream_diagnostics.csv`,
  `capture_timestamp_distribution.csv`, `capture_digitizer_diagnostics.csv`,
  `capture_quality_summary.json`, and `capture_quality_report.md`.

Offline single-spill analysis policy:
- `analyze-captured-spill` consumes the captured-spill manifest/payload contract
  and emits the same per-spill analysis artifact set as `analyze-spill`.
- The command uses config for analysis parameters and stream-count expectations,
  but does not connect to Redis.
- Manifest/schema errors fail explicitly; incomplete captured data is preserved
  as warnings when enough payloads remain to analyze at least one plane.

Offline batch analysis policy:
- `analyze-captured-spills` consumes captured-spill bundles and emits the same
  batch artifact families as `analyze-spills`.
- Bundle discovery is immediate-child only for directory roots so unrelated
  files are ignored and nested historical outputs are not swept accidentally.
- Duplicate suppression uses the same `target_ms` tolerance as live/historical
  batch analysis.
- Redis trigger lookup is intentionally skipped; records use
  `trigger_ms=target_ms` and `trigger_source=captured-spill`.

Split parity guardrail:
- Normal unit tests include a deterministic same-spill comparison between an
  online-style snapshot built from decoded raw payload bytes and the
  captured-bundle loader path.
- The comparison covers `Qx/Qy`, sliding tune medians, selected stream/quality
  fields, warnings, and quality flags.
- This is a regression guard for the acquisition/analysis split, not a physics
  validation gate for the current tune algorithm.

Tune-valued plot scaling policy:
- Tune Y-axis bounds are config-driven via `tune_plot_y_min` and `tune_plot_y_max`.
- This keeps spill/batch/study tune visuals comparable across runs.
- `tune_vs_time` additionally renders horizontal grid lines at
  `tune_plot_y_tick_step` spacing within the configured range for quick manual
  readout.
- Composite waterfall plots use spill order as sequence axis and sliding-window
  center-turn as projected Z-axis depth (or microseconds when
  `plot_time_axes_in_us=true`).
- Per-spill spectrogram heatmaps use tune on X, and turns (default) or
  microseconds (`plot_time_axes_in_us=true`) on Y, with normalized log spectral
  power for color intensity.
- Spectrogram row semantics are discrete: one row per sliding-window FFT step
  in spill order.

Windowing policy:
- `injection_window_turns` serves only the single representative injection tune
  estimate path in non-flash mode.
- Default operations keep `injection_window_turns == sliding_window_turns`;
  divergence is reserved for intentional study runs.
- Flash sampling mode (`--flashes N|max`) places evenly spaced sliding-window
  centers across spill length and overrides stride-based placement.
- Flash count is runtime-bounded per plane/spill by available turn depth:
  `effective_flashes <= floor(consensus_turns / sliding_window_turns)`.

When changing artifact fields or meaning, update:
- `docs/USAGE.md`
- `README.md`
- `docs/PLAN.md` (if plan alignment changes)
- any downstream analysis scripts expecting stable columns

Poster/DGX script policy:
- The poster scripts are downstream consumers of collected artifacts, not part
  of online acquisition or Rust runtime dispatch.
- They keep CPU fallback as the reproducibility path and use CUDA/CuPy only
  for offline FFT benchmarks and raw captured-spill array-heavy products.
- They intentionally exclude Schottky validation for the BPM-only poster phase.
- `capture_index.csv` rows can enter the dataset manifest for inventory and
  completeness accounting even when tune-analysis labels are not yet present.
- Raw captured-spill GPU analysis reads payload bytes directly and keeps its
  output schema separate from Rust batch records.
- Spark autosweep scripts run on the raw captured-bundle GPU analyzer outputs
  and keep their ranking/classification schema separate from Rust batch
  records. Stage 0 may read enough payload data for health checks, but it does
  not cache FFT products.

## Extension Points

### Split acquisition from offline analysis

The acquisition side of the split now keeps Redis synchronization and target
selection in `src/capture.rs`, then serializes complete captured-spill bundles
for later analysis. Offline single-spill and batch commands reconstruct the same
in-memory inputs that the current Redis paths build so tune extraction, quality
flags, plots, and batch summaries stay shared.

Implementation slices are tracked in `docs/ISSUE_MAP_DAQ_SPLIT.md`.

### Add a new analysis metric

1. Compute metric in `src/analyze.rs` snapshot/plane paths.
2. Add to `SpillRecord` and batch serializers.
3. Add to summary outputs and tests.
4. Document in `docs/USAGE.md`, this file, and `README.md` only when the
   high-level feature map changes.

### Add a new data source or reference

1. Keep synchronized snapshot construction unchanged.
2. Add conversion/adaptation layer into existing record format.
3. Preserve `target_ms` semantics to maintain comparability.

### Add a new command

1. Define CLI in `src/main.rs`.
2. Validate/normalize config before execution.
3. Reuse module internals; avoid duplicating parser/Redis logic.

## Invariants

- Config validation must pass before runtime execution.
- Unknown config keys are rejected.
- `target_ms` semantics must stay explicit and documented.
- Batch records must remain parseable in both `csv` and `jsonl` modes.
- Every warning/quality flag must have a concrete operational meaning.

## Testing Strategy

Current tests are mostly unit-level in `src/analyze.rs` and `src/config.rs`.

High-value regression targets:
- timestamp clustering and dedupe behavior
- confidence gate behavior
- spectrum preprocessing assumptions
- summary and serialization field stability

Future recommended additions:
- golden-file tests for batch summary and record outputs
- integration tests against a deterministic Redis fixture

Standalone Python checks:
- `python3 scripts/gpu_analyze_captured_spills.py --self-test`
- `python3 scripts/test_autosweep.py`
