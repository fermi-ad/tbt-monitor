# Usage Guide

This guide covers the command workflows for `tbt-monitor-tui`. Use
`docs/CONFIG_REFERENCE.md` for config keys and `docs/ARCHITECTURE.md` for data
flow, synchronization policy, and artifact schema details.

## Command Matrix

| Command | Purpose | Redis required |
| --- | --- | --- |
| `import` | Convert ACNET XML into `config/monitor.cfg`. | No |
| `monitor` | Run the live TUI stream-health monitor. | Yes |
| `capture-spill` | Capture one synchronized raw spill bundle. | Yes |
| `capture-spills` | Capture one bundle per unique spill in free-run mode. | Yes |
| `assess` | Check latest-ID timing without writing payload bundles. | Yes |
| `diagnose-captures` | Regenerate timing diagnostics from existing bundles. | No |
| `analyze-spill` | Analyze one live spill, free-run, or no-beam historical data. | Yes |
| `analyze-captured-spill` | Analyze one captured bundle offline. | No |
| `analyze-captured-spills` | Batch-analyze captured bundles offline. | No |
| `analyze-phase` | Run robustness and method-comparison studies. | Yes |
| `analyze-spills` | Batch-analyze many live or historical spills. | Yes |

## Build And Help

```bash
cargo check --offline
cargo run --offline -- --help
cargo run --offline -- <command> --help
```

Most examples below use `config/monitor.cfg` and `out/`. Replace those paths for
deployment or container runs.

## Configuration

Generate native config from an ACNET XML device file:

```bash
cargo run --offline -- import \
  --source /path/to/Config.xml \
  --output config/monitor.cfg
```

Import-time runtime tuning is available:

```bash
cargo run --offline -- import \
  --source /path/to/Config.xml \
  --output config/monitor.cfg \
  --xread-block-ms 1000 \
  --reconnect-initial-ms 2000 \
  --reconnect-max-ms 30000
```

`MonitorConfig::validate()` is the runtime safety gate. Unknown config keys are
rejected, and user-visible config semantics are documented in
`docs/CONFIG_REFERENCE.md`.

## Live Monitor

Run the TUI monitor:

```bash
cargo run --offline -- monitor --config config/monitor.cfg
```

Useful overrides:

```bash
cargo run --offline -- monitor \
  --config config/monitor.cfg \
  --xread-block-ms 1000 \
  --reconnect-initial-ms 2000 \
  --reconnect-max-ms 30000
```

`xread_block_ms=0` means `XREAD BLOCK 0`, which waits indefinitely for the next
stream entry. TUI controls are `q` to quit and `up/down` or `j/k` to change the
selected device.

## Raw Spill Capture

Use capture commands when acquisition should be separated from tune analysis.
Captured bundles store raw Redis stream `_` field bytes exactly as collected.
Current BPM TbT payloads are interpreted later as little-endian `f32` samples.

The checked-in `config/monitor.cfg` is set up for the next preservation run:
the primary configured streams are `TBT_POSITION_RAW`, and
`capture_intensity_variant=raw` derives matching `TBT_INTENSITY_RAW` streams
for the same plates. Position streams still drive target selection and offline
tune analysis; intensity streams are captured and diagnosed as auxiliary raw
payloads for later study.

Capture one synchronized spill:

```bash
cargo run --offline -- capture-spill \
  --config config/monitor.cfg \
  --out-dir out
```

Capture continuously and stop after `N` successful bundles:

```bash
cargo run --offline -- capture-spills \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25
```

Capture selects a stream-ID millisecond `target_ms`, then reads configured
streams within `same_spill_tolerance_ms` (default `25 ms`). Exact timestamp
deltas are recorded; millisecond spread is diagnostic information, not automatic
failure. Live duplicate suppression uses the same same-spill tolerance.

Capture reports distinguish two timestamp populations:

- captured payload timestamps: the raw entries actually written to the bundle
- latest-ID snapshot timestamps: what each stream reported as its latest Redis
  stream ID during target selection

With RAW position plus RAW intensity enabled, a complete capture is `240/240`:
120 position payloads and 120 derived intensity payloads. Use the
captured-payload timestamp distribution to understand how those streams were
bucketed. Latest-ID snapshot staleness is diagnostic context and can be one
machine event old even when the captured payload is complete.

Each bundle is written as:

- `spill_<target_ms>/manifest.json`
- `spill_<target_ms>/capture_summary.txt`
- `spill_<target_ms>/payloads/*.bin`

Free-run capture also writes run-level files:

- `capture_index.csv`
- `capture_spill_diagnostics.csv`
- `capture_stream_diagnostics.csv`
- `capture_timestamp_distribution.csv`
- `capture_digitizer_diagnostics.csv`
- `capture_quality_summary.json`
- `capture_quality_report.md`

## Capture Diagnostics

Assess stream timing before writing raw payload bundles:

```bash
cargo run --offline -- assess \
  --config config/monitor.cfg \
  --out-dir out/assess \
  --events 1 \
  --same-spill-tolerance-ms 25
```

`assess` writes `assess_streams.csv`, `assess_digitizers.csv`,
`assess_summary.json`, and `assess_report.md`.

Regenerate capture timing reports from existing bundles without Redis:

```bash
cargo run --offline -- diagnose-captures \
  --bundles-dir out \
  --out-dir out \
  --same-spill-tolerance-ms 25
```

Capture quality and latest-poll timing are separate. For example,
`LATEST_STALE_BUT_CAPTURED_OK` means a latest-ID observation looked stale, but
the near-target raw payload was found and captured.

`capture_timestamp_distribution.csv` has one row per spill/source/delta bucket:

- `source=captured_payload` describes timestamps for entries written to
  payload files
- `source=latest_id_snapshot` describes the latest Redis IDs observed during
  target selection
- `delta_ms` is `stream_timestamp_ms - target_ms`
- `stream_count` is how many streams landed in that timestamp bucket

## One-Spill Analysis

Analyze one live spill:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out
```

Analyze one captured bundle offline:

```bash
cargo run --offline -- analyze-captured-spill \
  --config config/monitor.cfg \
  --bundle out/spill_<target_ms> \
  --out-dir out/offline_<target_ms>
```

`--bundle` may point at a bundle directory or its `manifest.json`. Offline
analysis validates schema, artifact type, payload paths, byte counts, checksums,
and payload shape before reconstructing the same analysis snapshot used by live
analysis.

Per-spill analysis emits:

- `spectrum_h.png`, `spectrum_v.png`
- `spectrogram_h.png`, `spectrogram_v.png`
- `tune_vs_time.png`
- `tune_validation.png`
- `sliding_tune.csv`
- console or prefixed text summary

Common knobs:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out \
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
  --qy-band-max 0.74
```

`--flashes N` samples `N` evenly spaced sliding-window centers across a spill.
`--flashes max` uses the maximum supported count for the available turn depth
and `sliding_window_turns`. In flash mode, injection tune estimation uses
`sliding_window_turns`, so `injection_window_turns` is ignored.

Use historical no-beam mode when live waits are not appropriate:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out \
  --no-beam \
  --stale-depth 100
```

`--free-run --no-beam` performs a finite historical sweep and exits. Add
`--count N` to stop after `N` successful analyses; without `--count`, it scans
all discovered candidates.

## Free-Run Analysis

Run continuous live one-spill analysis:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25
```

Each stream wake triggers a full all-stream snapshot. Duplicate physical spills
are suppressed with adjacent target tolerance. When `--count` is set,
`analyze-spill` also emits batch-level summary/composite outputs at exit.

Per-spill free-run outputs are prefixed with `spill_<target_ms>_`, for example:

- `spill_<target_ms>_spectrum_h.png`
- `spill_<target_ms>_spectrogram_v.png`
- `spill_<target_ms>_tune_validation.png`
- `spill_<target_ms>_sliding_tune.csv`
- `spill_<target_ms>_summary.txt`

## Robustness Studies

Run window-sensitivity, BPM-quality, and method-comparison artifacts:

```bash
cargo run --offline -- analyze-phase \
  --config config/monitor.cfg \
  --out-dir out
```

Continuous and no-beam modes are supported:

```bash
cargo run --offline -- analyze-phase \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25

cargo run --offline -- analyze-phase \
  --config config/monitor.cfg \
  --out-dir out \
  --no-beam \
  --stale-depth 100
```

Study options include `--window-start-min/max/step`,
`--window-length-min/max/step`, `--reference-start`, `--reference-length`,
`--min-peak-confidence`, `--plot-time-axes-in-us`, and `--summary-file`.

Generated artifacts include:

- `tune_vs_window_start.png`
- `tune_vs_window_length.png`
- `bpm_quality_table.csv`
- `tune_by_bpm.png`
- `confidence_by_bpm.png`
- `method_comparison.png`
- `findings_summary.md`

SVD/PCA remains deferred; see `docs/PLAN.md` and
`docs/ANALYSIS_CHECKLIST.md`.

## Batch Analysis

Analyze multiple live or historical spills:

```bash
cargo run --offline -- analyze-spills \
  --config config/monitor.cfg \
  --out-dir out \
  --count 50
```

Analyze captured bundles offline:

```bash
cargo run --offline -- analyze-captured-spills \
  --config config/monitor.cfg \
  --bundles-dir out \
  --out-dir out/offline_batch \
  --count 50
```

`--bundles-dir` may point at one manifest, one bundle directory, or a directory
containing immediate-child `spill_<target_ms>/` bundles. Offline batch records
use `trigger_source=captured-spill` and `trigger_ms=target_ms` because Redis
trigger keys are not read.

Main batch options:

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

Batch outputs include:

- `spills_summary.csv`
- `spills_summary.jsonl` unless CSV-only mode is requested
- `tune_vs_spill.png`
- `confidence_vs_spill.png`
- `alignment_vs_spill.png`
- `tune_scatter_qx_qy.png`
- `tune_histogram.png`
- optional `tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png`
- `composite_waterfall_h.png`, `composite_waterfall_v.png`
- `batch_summary.md`
- optional `tune_residuals.png` when reference matches exist
- `spill_<index>_<target_ms>_sliding_tune.csv`

Detailed artifact mode controls per-spill plot volume:

- `all`: save all detailed per-spill artifacts.
- `representative`: save first, highest-confidence, lowest-confidence,
  lowest-alignment, and bad spills.
- `none`: skip detailed per-spill artifacts.

## Timing Semantics

The primary synchronization timestamp is the Redis stream-ID millisecond. The
selected representative spill timestamp is `target_ms`.

- Live/historical target selection clusters adjacent timestamp buckets
  (currently `+/-1 ms`) before selecting the representative target.
- Capture uses `same_spill_tolerance_ms` to decide whether streams belong to
  the selected same-spill target.
- Batch and free-run duplicate suppression use target-ms tolerance so one
  physical spill is not written twice because of small cross-device jitter.
- Incomplete states emit warnings or quality flags rather than disappearing.

## Docker

Build for linux/amd64 from Apple Silicon:

```bash
docker build --platform linux/amd64 -t tbt-monitor:amd64 .
```

Run the TUI:

```bash
docker run --rm -it \
  --name tbt-monitor \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  tbt-monitor:amd64 \
  monitor --config /app/config/monitor.cfg
```

Bind-mount output for analysis or capture:

```bash
mkdir -p "$PWD/out"
docker run -it \
  --name tbt-tune \
  --network host \
  -v "$PWD/out:/out" \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  tbt-monitor:amd64 \
  analyze-spill --config /app/config/monitor.cfg --out-dir /out
```

Use no `--rm` if you might need `docker cp` fallback extraction after the
container exits.
