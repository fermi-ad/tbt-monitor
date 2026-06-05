# tbt-monitor

Rust/Ratatui platform for Synchrotron BPM turn-by-turn (TbT) tune analysis
across multiple Redis digitizers.

The project is aimed at physics validation and operations support, not just
stream monitoring. Current scope includes:

- ACNET XML import into validated monitor/analyzer config
- live stream health monitoring via Redis Streams (`XREAD BLOCK`)
- synchronized global spill capture with same-spill timestamp tolerance
- raw captured-spill bundles for acquisition-first/offline-analysis workflows
- capture timing diagnostics and non-capturing stream preflight assessment
- offline one-spill reanalysis from captured-spill bundles
- offline multi-spill batch analysis from captured-spill bundles
- injection-window and sliding-window tune extraction (`Qx/Qy`) with confidence gating
- tracked sliding tune diagnostics (fallback/suspicious-step visibility)
- robustness studies (`analyze-phase`) for window sensitivity and BPM/method comparison
- multi-spill batch validation (`analyze-spills`) with quality flags, timeliness metrics,
  and optional external reference residuals

Primary objective: determine when BPM-derived tune estimates are physically
credible and operationally useful for Delivery Ring studies.

## Documentation Map

- User and operations guide: this `README.md`
- Implementation architecture: `docs/ARCHITECTURE.md`
- Design rationale and tradeoffs: `docs/DESIGN_DECISIONS.md`
- Configuration semantics: `docs/CONFIG_REFERENCE.md`
- Physics/engineering roadmap mapped from the methodology PDF: `docs/PLAN.md`
- Physics-validation guide and acceptance framing: `docs/PHYSICS.md`
- Remaining implementation work for physics review artifacts: `docs/ANALYSIS_CHECKLIST.md`
- Engineering backlog and implementation tracking: `docs/ENGINEERING_BACKLOG.md`
- GitHub issue/PR workflow: `docs/GITHUB_WORKFLOW.md`
- Acquisition/analysis split issue map: `docs/ISSUE_MAP_DAQ_SPLIT.md`
- Coding-assistant orientation for this repository: `AGENTS.md`

`docs/PLAN.md` includes an explicit "plan vs implementation" divergence matrix.

## Repository Layout

- `src/main.rs`: CLI entrypoint and command dispatch.
- `src/config.rs`: config schema, parser, validation, serializer.
- `src/importer.rs`: ACNET XML import into monitor config.
- `src/monitor.rs`: stream-driven live monitor runtime for the TUI.
- `src/capture.rs`: raw synchronized spill capture and bundle writing.
- `src/analyze.rs`: synchronized spill analysis, studies, and batch processing.
- `config/monitor.cfg`: generated/example config with device and stream definitions.

## Build

```bash
cd /Users/derekste/Dev/codex/tbt-monitor
cargo check --offline
```

## Generate Config from XML

```bash
cargo run --offline -- import \
  --source /Users/derekste/Downloads/Config.xml \
  --output /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg
```

Optional stream/reconnect tuning while importing:

```bash
cargo run --offline -- import \
  --source /Users/derekste/Downloads/Config.xml \
  --output /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --xread-block-ms 1000 \
  --reconnect-initial-ms 2000 \
  --reconnect-max-ms 30000
```

## Run TUI Monitor

```bash
cargo run --offline -- monitor \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg
```

Optional runtime overrides:

```bash
cargo run --offline -- monitor \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --xread-block-ms 1000 \
  --reconnect-initial-ms 2000 \
  --reconnect-max-ms 30000
```

`xread_block_ms=0` means `XREAD BLOCK 0` (wait indefinitely for the next entry).

Controls:

- `q` quit
- `up/down` or `j/k` change selected device

## Capture Raw Spill Bundles

Use `capture-spill` to collect one synchronized spill from all configured BPM
`TBT_POSITION_SCALED` streams without running tune analysis:

```bash
cargo run --offline -- capture-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out
```

The command selects a stream-ID millisecond target and captures every configured
stream within the configured same-spill window (`same_spill_tolerance_ms`,
default `25 ms`). Exact timestamp offsets are measured and reported; a few
milliseconds of spread is normal DAQ timing information, not an automatic
failure. It writes:

- `out/spill_<target_ms>/manifest.json`
- `out/spill_<target_ms>/capture_summary.txt`
- `out/spill_<target_ms>/payloads/*.bin`

The raw payload files contain the Redis stream `_` field bytes exactly as
captured. For current BPM TbT streams this is little-endian `f32` sample data.
The manifest records:

- `schema_version=1` and artifact type `tbt-monitor.captured-spill`
- `redis_timestamp_ms` (the selected Redis stream-ID millisecond, equal to
  `target_ms` for schema v1)
- target, same-spill tolerance, capture diagnostics, and timeliness observations
- full configured stream inventory
- captured stream IDs and stream milliseconds
- payload file paths, byte counts, sample counts, and `fnv1a64` checksums
- warnings for incomplete polls, low alignment, missing payloads, or malformed
  payload lengths

Use `capture-spills --free-run` to keep capturing one bundle per unique spill
target. Add `--count N` for a bounded run:

```bash
cargo run --offline -- capture-spills \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --free-run \
  --count 25
```

`capture-spills` starts stream-watch workers only to detect new arrivals. Each
wake triggers a full global snapshot over all configured streams. Duplicate
physical spills are suppressed using the same-spill tolerance. The command
writes run-level diagnostics alongside the per-spill bundle directories:

- `capture_index.csv`
- `capture_spill_diagnostics.csv`
- `capture_stream_diagnostics.csv`
- `capture_digitizer_diagnostics.csv`
- `capture_quality_summary.json`
- `capture_quality_report.md`

Capture quality and latest-poll timing are intentionally separate. A bundle is
`Complete` when every configured stream has a same-spill payload within
`±same_spill_tolerance_ms`. If the latest-ID polling pass saw stale values but
the near-target capture still found complete payloads, the stream is annotated
as `LATEST_STALE_BUT_CAPTURED_OK` rather than bad captured data.

Regenerate diagnostics for an existing capture directory without Redis:

```bash
cargo run --offline -- diagnose-captures \
  --bundles-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --same-spill-tolerance-ms 25
```

Assess stream timing before collecting payload artifacts:

```bash
cargo run --offline -- assess \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out/assess \
  --events 1 \
  --same-spill-tolerance-ms 25
```

`assess` reads current latest stream IDs, watches one new event by default, and
writes `assess_streams.csv`, `assess_digitizers.csv`, `assess_summary.json`,
and `assess_report.md`. It does not collect raw payload bundles.

## Analyze A Captured Spill Offline

Use `analyze-captured-spill` to run the existing one-spill tune-analysis path
from a captured-spill bundle without Redis connectivity:

```bash
cargo run --offline -- analyze-captured-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --bundle /Users/derekste/Dev/codex/tbt-monitor/out/spill_<target_ms> \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out/offline_<target_ms>
```

`--bundle` may point at either the bundle directory or its `manifest.json`. The
command loads config for analysis parameters and stream-count expectations, then
reconstructs the analysis snapshot from `manifest.json` plus `payloads/*.bin`.
It does not connect to Redis. Malformed manifests, unsupported schema versions,
unsafe payload paths, checksum mismatches, missing payloads, or malformed
little-endian `f32` payloads are reported as explicit errors or warnings.

Outputs match the current one-spill analysis artifact set:

- `spectrum_h.png`
- `spectrum_v.png`
- `spectrogram_h.png`
- `spectrogram_v.png`
- `tune_vs_time.png`
- `tune_validation.png`
- `sliding_tune.csv`

## Analyze Captured Spill Bundles Offline

Use `analyze-captured-spills` to run the existing batch-analysis path across
captured-spill bundles without Redis connectivity:

```bash
cargo run --offline -- analyze-captured-spills \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --bundles-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out/offline_batch \
  --count 25
```

`--bundles-dir` may point at a directory containing `spill_<target_ms>/`
bundle directories, one bundle directory, or one `manifest.json`. Directory
discovery scans immediate child bundle directories and orders them by
`target_ms`. Duplicate physical spills are suppressed using the same adjacent
target tolerance policy as live batch analysis. Batch records use
`trigger_source=captured-spill` because no Redis trigger read is performed.

The command supports the same batch analysis knobs as `analyze-spills`,
including `--flashes`, `--record-format`, `--detailed-artifacts`,
`--reference-file`, and reference matching options. Outputs match the current
batch artifact set: `spills_summary.csv/jsonl`, batch plots, composite
waterfalls, `batch_summary.md`, and per-spill sliding CSV files.

## Run One-Shot Tune Analysis

This command reads one spill snapshot, computes injection-window `Qx/Qy`, always computes sliding-window tune trace, and writes:

- `spectrum_h.png`
- `spectrum_v.png`
- `spectrogram_h.png` (top-down tune heatmap vs time)
- `spectrogram_v.png` (top-down tune heatmap vs time)
- `tune_vs_time.png`
- `tune_validation.png` (2x2 validation figure: H/V spectrogram + H/V tune traces)
- `sliding_tune.csv`

Summary output for each spill also includes:

- alignment diagnostics across streams/digitizers
- timeliness diagnostics (`obs_ms - target_ms` min/max/median absolute delta)
- explicit warnings when a complete stream poll is not available

Alignment for `analyze-spill` is based on the millisecond field of latest
`TBT_POSITION_SCALED` stream IDs (not trigger keys). Target selection clusters
adjacent timestamp buckets (currently `±1 ms`) before selecting the representative
target, which makes synchronized capture resilient to small cross-device jitter.

```bash
cargo run --offline -- analyze-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out
```

Optional overrides:

```bash
cargo run --offline -- analyze-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --align-tolerance-ms 1 \
  --min-aligned-fraction 0.70 \
  --injection-start-turn 0 \
  --injection-window-turns 2048 \
  --sliding-window-turns 2048 \
  --sliding-stride-turns 256 \
  --flashes 5 \
  --min-peak-confidence 1.5 \
  --qx-band-min 0.58 \
  --qx-band-max 0.74 \
  --qy-band-min 0.58 \
  --qy-band-max 0.74 \
  --free-run \
  --count 25
```

No-beam historical mode (no wait for new triggers, scans stream buffers immediately):

```bash
cargo run --offline -- analyze-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --no-beam \
  --stale-depth 100
```

`--free-run --no-beam` runs a finite historical sweep (newest to oldest candidates) and exits.
Add `--count N` to stop after `N` successful analyses; without `--count`, it scans all discovered candidates.
With `--count`, the command also writes batch-level summary/composite outputs for the collected spills before exit.

No-beam historical candidate discovery also merges adjacent timestamp buckets
(`±1 ms`) before ranking candidates, so split coverage patterns (for example
`96/24` across neighboring milliseconds) are analyzed as one physical spill target.

Flashpoint sampling mode:

- `--flashes N` samples `N` evenly spaced sliding-window centers across each spill.
- `--flashes max` automatically uses the maximum flash count allowed by available turns and `sliding_window_turns`.
- This overrides `sliding_stride_turns` for tune extraction windows.
- Runtime bounds are enforced per plane/spill: `effective_flashes <= floor(consensus_turns / sliding_window_turns)`.
  If reduced, a warning is emitted in spill summaries.
- In flash mode, injection tune estimation uses `sliding_window_turns` (so `injection_window_turns` is ignored).

Sliding-window tracking knobs are mostly config-driven:

- `enable_peak_tracking=true`
- `qx_track_half_width=0.005`
- `qy_track_half_width=0.005`
- `max_tune_step_per_window=0.005`
- `turn_period_us=1.6`
- `plot_time_axes_in_us=false`
- `tune_plot_y_min=0.58`
- `tune_plot_y_max=0.74`
- `tune_plot_y_tick_step=0.01`

When tracking is enabled, `tune_vs_time.png` uses the tracked `selected_tune` curve, and raw global-band peaks are still computed for diagnostics/fallback.
With `--flashes`, `tune_vs_time.png` also marks sampled flash points and overlays injection-tune guides.
Tune-valued Y axes are fixed to `tune_plot_y_min/max` (including batch/study tune trend plots) for cross-run visual comparability.
`tune_vs_time.png` draws horizontal Y-grid lines at `tune_plot_y_tick_step` intervals within the configured range.
Time-domain plot axes default to turn index. Set `plot_time_axes_in_us=true` (or pass `--plot-time-axes-in-us` on analysis commands) to render time axes in microseconds.
`spectrogram_h/v.png` are top-down heatmaps with tune on X and turn/time on Y; each row corresponds to one sliding-window FFT step, and heat intensity uses normalized log spectral power.
`injection_window_turns` is used only for the single representative injection tune estimate in non-flash mode; in flash mode, `sliding_window_turns` is used for both injection and sliding paths.
Fallback-picked windows and suspicious large-step windows are kept in outputs but never reseed the tracker state.
All tune peak picks use a configurable confidence gate (`min_peak_confidence`, default `2.0`), so weak windows are reported as missing tune.
Use `--min-peak-confidence` on analysis commands for per-run overrides.
FFT preprocessing also suppresses DC by zeroing bin 0 and ignoring the first few bins during peak search.

## Free-Run Continuous Capture

Use `--free-run` to keep collecting spill analyses continuously with the same global all-stream alignment logic as one-shot `analyze-spill`.
Add optional `--count N` to stop automatically after `N` successful spill analyses.
When `--count` is set, `analyze-spill` also synthesizes batch-level outputs at exit:
`spills_summary.csv/jsonl`, `tune_vs_spill.png`, `confidence_vs_spill.png`,
`alignment_vs_spill.png`, `tune_scatter_qx_qy.png`, `tune_histogram.png`,
`composite_waterfall_h.png`, `composite_waterfall_v.png`, and `batch_summary.md`.
If `--flashes` is set, synthesized batch outputs also include
`tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png` (one per flash index).

The process starts stream-watch workers (one per device) only to detect new arrivals. Each arrival wakes a full global snapshot over all configured streams, then writes one timestamped artifact set.

Free-run duplicate suppression is also tolerant to adjacent target milliseconds
(`±1 ms`) to avoid double-writing the same physical spill.

```bash
cargo run --offline -- analyze-spill \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --free-run \
  --count 25
```

Output layout in free-run mode:

- Per-spill files in the output directory:
  - `spill_<target_ms>_spectrum_h.png`
  - `spill_<target_ms>_spectrum_v.png`
  - `spill_<target_ms>_spectrogram_h.png`
  - `spill_<target_ms>_spectrogram_v.png`
  - `spill_<target_ms>_tune_vs_time.png`
  - `spill_<target_ms>_tune_validation.png`
  - `spill_<target_ms>_sliding_tune.csv`
  - `spill_<target_ms>_summary.txt` (same metadata/summary text printed to console)

Example:

- `out/spill_1772830005123_spectrum_h.png`

## Stream-Driven Behavior

For each device, the monitor:

1. Validates configured `stream_key` types.
2. Uses `XREAD BLOCK` across all eligible stream keys (no client-side socket read timeout).
3. Tracks per-stream latest entry ID, `_` payload byte length, and arrival counts.
4. On connection/read failure, reconnects with slow exponential backoff (`reconnect_initial_ms` to `reconnect_max_ms`).

No fixed-rate polling loop is required for stream arrivals.

## Docker (Mac M1 -> linux/amd64)

Build locally for `x86_64` Linux from Apple Silicon:

```bash
cd /Users/derekste/Dev/codex/tbt-monitor
docker build --platform linux/amd64 -t tbt-monitor:amd64 .
```

Push directly to a registry:

```bash
docker buildx build \
  --platform linux/amd64 \
  -t <your-registry>/tbt-monitor:amd64 \
  --push \
  .
```

Run interactively (for TUI):

```bash
docker run --rm -it \
  --name tbt-monitor \
  <your-registry>/tbt-monitor:amd64
```

Override config at runtime:

```bash
docker run --rm -it \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  <your-registry>/tbt-monitor:amd64 \
  monitor --config /app/config/monitor.cfg
```

## Docker Artifact Export (PNG Files)

Recommended: bind mount host output directory to `/out` in container.

```bash
mkdir -p "$PWD/out"
docker run -it \
  --name tbt-tune \
  --network host \
  -v "$PWD/out:/out" \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  <your-registry>/tbt-monitor:amd64 \
  analyze-spill --config /app/config/monitor.cfg --out-dir /out
```

This writes PNGs directly to host `./out`, so stopping the container does not lose files.

Fallback when no bind mount is used:

```bash
docker run -it \
  --name tbt-tune \
  --network host \
  <your-registry>/tbt-monitor:amd64 \
  analyze-spill --config /app/config/monitor.cfg --out-dir /out

docker cp tbt-tune:/out ./out
```

Use no `--rm` for analysis runs if you may need post-stop file extraction.

Continuous collection (free-run) in Docker:

```bash
mkdir -p "$PWD/out"
docker run -it \
  --name tbt-tune-free \
  --network host \
  -v "$PWD/out:/out" \
  <your-registry>/tbt-monitor:amd64 \
  analyze-spill --config /app/config/monitor.cfg --out-dir /out --free-run
```

No-beam in Docker:

```bash
docker run -it \
  --name tbt-tune-nobeam \
  --network host \
  -v "$PWD/out:/out" \
  <your-registry>/tbt-monitor:amd64 \
  analyze-spill --config /app/config/monitor.cfg --out-dir /out --no-beam --stale-depth 100
```

## Run Robustness Study (Phase 1-3)

This command runs the next analysis phase up through:

1. Window sensitivity studies
2. BPM-by-BPM quality metrics/ranking
3. Simple multi-BPM method comparison

SVD is intentionally deferred in this phase.

```bash
cargo run --offline -- analyze-phase \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out
```

Continuous robustness-study capture:

```bash
cargo run --offline -- analyze-phase \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --free-run \
  --count 25
```

No-beam historical analyze-phase:

```bash
cargo run --offline -- analyze-phase \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --no-beam \
  --stale-depth 100
```

`--free-run --no-beam` performs a finite historical sweep for analyze-phase artifacts, then exits.
Add `--count N` to stop after `N` successful analyses; without `--count`, it scans all discovered candidates.

In `--free-run`, each synchronized spill gets timestamped files:

- `spill_<target_ms>_tune_vs_window_start.png`
- `spill_<target_ms>_tune_vs_window_length.png`
- `spill_<target_ms>_bpm_quality_table.csv`
- `spill_<target_ms>_tune_by_bpm.png`
- `spill_<target_ms>_confidence_by_bpm.png`
- `spill_<target_ms>_method_comparison.png`
- `spill_<target_ms>_findings_summary.md` (or prefixed custom `--summary-file`)
- `spill_<target_ms>_analyze_phase_summary.txt` (console metadata capture)

Configurable study options:

```bash
cargo run --offline -- analyze-phase \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --window-start-min 0 \
  --window-start-max 2048 \
  --window-start-step 128 \
  --window-length-min 512 \
  --window-length-max 2048 \
  --window-length-step 256 \
  --reference-start 0 \
  --reference-length 1024 \
  --summary-file findings_summary.md
```

Generated artifacts:

- `tune_vs_window_start.png`
- `tune_vs_window_length.png`
- `bpm_quality_table.csv`
- `tune_by_bpm.png`
- `confidence_by_bpm.png`
- `method_comparison.png`
- `findings_summary.md`

## Run Multi-Spill Batch Validation

`analyze-spills` collects many synchronized spills using the same wake/snapshot pipeline as `analyze-spill`, then writes spill-to-spill validation outputs.
At completion (`--count` successful spills), it also writes composite horizontal/vertical waterfall plots across the batch.

```bash
cargo run --offline -- analyze-spills \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --count 50
```

No-beam historical batch mode:

```bash
cargo run --offline -- analyze-spills \
  --config /Users/derekste/Dev/codex/tbt-monitor/config/monitor.cfg \
  --out-dir /Users/derekste/Dev/codex/tbt-monitor/out \
  --count 50 \
  --no-beam \
  --stale-depth 100
```

Docker example:

```bash
mkdir -p "$PWD/out"
docker run -it \
  --name tbt-batch \
  --network host \
  -v "$PWD/out:/out" \
  <your-registry>/tbt-monitor:amd64 \
  analyze-spills --config /app/config/monitor.cfg --out-dir /out --count 50
```

Offline captured-bundle equivalent:

```bash
docker run -it \
  --name tbt-offline-batch \
  -v "$PWD/out:/out" \
  <your-registry>/tbt-monitor:amd64 \
  analyze-captured-spills --config /app/config/monitor.cfg --bundles-dir /out --out-dir /out/offline_batch --count 50
```

Main options:

- `--min-confidence 1.5`
- `--min-aligned-bpm-count 4`
- `--min-per-plane-bpm 1`
- `--peak-edge-margin 0.005`
- `--min-peak-confidence <f64>`
- `--flashes <N|max>`
- `--record-format both|csv|jsonl`
- `--detailed-artifacts all|representative|none`
- `--reference-file <path>`
- `--reference-key target_ms|spill_index`
- `--reference-match-tolerance-ms 1`
- `--no-beam`
- `--stale-depth 100`

Batch quality semantics:

- `INCOMPLETE_TBT_POLL` is emitted when the snapshot observed fewer streams than configured.
- Timeliness fields are printed per spill in console output:
  - `timeliness_med_abs_ms`
  - `timeliness_max_abs_ms`
- Batch summary aggregates include:
  - median per-spill median absolute timing delta
  - median per-spill max absolute timing delta
  - worst observed max absolute timing delta

Batch outputs:

- `spills_summary.csv`
- `spills_summary.jsonl` (unless CSV-only mode)
- `tune_vs_spill.png` (injection tune trend)
- `tune_vs_spill_flash_XX.png` (one per flash index when `--flashes` is set)
- `tune_histogram_flash_XX.png` (one per flash index when `--flashes` is set)
- `confidence_vs_spill.png`
- `alignment_vs_spill.png`
- `tune_scatter_qx_qy.png`
- `tune_histogram.png`
- `composite_waterfall_h.png` (3D-style composite; time/turn on Z)
- `composite_waterfall_v.png` (3D-style composite; time/turn on Z)
- `batch_summary.md`
- `tune_residuals.png` (only when reference matches exist)
- `spill_<index>_<target_ms>_sliding_tune.csv` for every analyzed spill

Detailed artifact mode:

- `all`: saves per-spill `spectrum_h/spectrum_v/spectrogram_h/spectrogram_v/tune_vs_time/tune_validation/sliding_tune.csv` + summary text for every analyzed spill.
- `representative`: saves first, highest-confidence, lowest-confidence, lowest-alignment, and BAD spills.
- `none`: skips per-spill detailed artifacts.

## Developer Notes

For implementation-level details and design rationale, use:

- `docs/ARCHITECTURE.md` for module boundaries and runtime data flow.
- `docs/DESIGN_DECISIONS.md` for explicit tradeoffs and justification.
- `docs/CONFIG_REFERENCE.md` for key-by-key config behavior.
- `docs/PLAN.md` for roadmap alignment with the tune-methodology plan PDF.
- `docs/GITHUB_WORKFLOW.md` for issue labels, templates, and PR expectations.
- `docs/ISSUE_MAP_DAQ_SPLIT.md` for the planned acquisition/offline-analysis split.
- `AGENTS.md` for assistant-oriented repository guidance and invariants.
