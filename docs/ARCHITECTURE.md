# Architecture

## Scope

`tbt-monitor-tui` is a single Rust binary with five functional areas:
- config import and validation
- live stream monitoring (TUI)
- one-spill tune analysis
- robustness studies
- multi-spill batch analysis

The program reads Redis stream data from many BPM devices and converts it into synchronized spill-level artifacts and summary records.

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
- `src/analyze.rs`
  - Spill snapshot construction.
  - FFT-based tune extraction and sliding analysis.
  - Study and batch workflows.
  - Artifact and summary generation.

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
  - alignment fraction is low
  - complete poll is unavailable
  - near-target reads are incomplete
- Batch quality flags include `INCOMPLETE_TBT_POLL` and other confidence/alignment checks.
- Timeliness metrics (`obs_ms - target_ms`) are emitted per spill and aggregated in batch summary.

Rationale:
- Operational users need to distinguish "analysis failure" from "data quality degradation".
- Keeping marginal outputs supports post-run triage and reanalysis.

## Artifact Contract

Main artifact families:
- per-spill spectra and tune traces (`png`)
- per-spill top-down spectrogram heatmaps (`png`)
- per-spill 2x2 tune-validation composite (`png`)
- per-spill sliding samples (`csv`)
- per-spill summaries (`txt`)
- batch records (`csv`/`jsonl`)
- batch plots (including composite H/V waterfall, and optional per-flash
  `tune_vs_spill_flash_XX` and `tune_histogram_flash_XX`) and markdown summary

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
- `README.md`
- `docs/PLAN.md` (if plan alignment changes)
- any downstream analysis scripts expecting stable columns

## Extension Points

### Split acquisition from offline analysis

The planned split keeps Redis synchronization and target selection in the
acquisition path, then serializes a complete captured-spill bundle for later
analysis. The offline loader should reconstruct the same in-memory inputs that
the current `analyze-spill` path builds from Redis so tune extraction, quality
flags, plots, and batch summaries can stay shared.

Implementation slices are tracked in `docs/ISSUE_MAP_DAQ_SPLIT.md`.

### Add a new analysis metric

1. Compute metric in `src/analyze.rs` snapshot/plane paths.
2. Add to `SpillRecord` and batch serializers.
3. Add to summary outputs and tests.
4. Document in `README.md` and this file.

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
