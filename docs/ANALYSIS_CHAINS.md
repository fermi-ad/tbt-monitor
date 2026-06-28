# Analysis Chains

This guide covers the Rust analysis paths that operate on live Redis data or
captured raw spill bundles. DAQ and capture-quality workflows are covered in
[DAQ Guide](DAQ.md). Spark/GPU downstream studies are covered in
[Spark Workflows](SPARK.md).

## Analysis Boundaries

The Rust analyzer is the operational proof-of-concept path:

- build a synchronized spill snapshot
- split BPM traces by plane
- estimate injection and sliding-window tunes
- write per-spill plots, CSVs, and quality summaries
- batch many spills into trend and distribution artifacts

Captured-bundle analysis is offline by design. It validates manifest schema,
payload paths, byte counts, checksums, payload shape, and little-endian `f32`
decode before reusing the same analysis path as live snapshots.

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

Per-spill outputs include:

- `spectrum_h.png`, `spectrum_v.png`
- `spectrogram_h.png`, `spectrogram_v.png`
- `tune_vs_time.png`
- `tune_validation.png`
- `sliding_tune.csv`
- text summary

Common analysis knobs:

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

`--flashes N` samples evenly spaced sliding-window centers across a spill.
`--flashes max` uses the maximum supported count for the available turn depth
and `sliding_window_turns`.

## Free-Run Analysis

Run continuous live one-spill analysis:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25
```

Each stream wake triggers a global snapshot. Duplicate physical spills are
suppressed using adjacent target tolerance. With `--count`, the command writes
batch-level summary/composite outputs at exit.

Per-spill free-run outputs are prefixed with `spill_<target_ms>_`, for example:

- `spill_<target_ms>_spectrum_h.png`
- `spill_<target_ms>_spectrogram_v.png`
- `spill_<target_ms>_tune_validation.png`
- `spill_<target_ms>_sliding_tune.csv`
- `spill_<target_ms>_summary.txt`

## Historical And No-Beam Analysis

Use no-beam mode when live waits are not appropriate:

```bash
cargo run --offline -- analyze-spill \
  --config config/monitor.cfg \
  --out-dir out \
  --no-beam \
  --stale-depth 100
```

`--free-run --no-beam` performs a finite historical sweep and exits. Add
`--count N` to stop after `N` successful analyses.

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
use `trigger_source=captured-spill` and `trigger_ms=target_ms`.

Batch outputs include:

- `spills_summary.csv`
- `spills_summary.jsonl`
- `tune_vs_spill.png`
- `confidence_vs_spill.png`
- `alignment_vs_spill.png`
- `tune_scatter_qx_qy.png`
- `tune_histogram.png`
- optional `tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png`
- `composite_waterfall_h.png`, `composite_waterfall_v.png`
- `batch_summary.md`
- optional `tune_residuals.png`
- `spill_<index>_<target_ms>_sliding_tune.csv`

Detailed artifact mode controls per-spill plot volume:

- `all`: save all detailed per-spill artifacts.
- `representative`: save first, highest-confidence, lowest-confidence,
  lowest-alignment, and bad spills.
- `none`: skip detailed per-spill artifacts.

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

Generated artifacts include:

- `tune_vs_window_start.png`
- `tune_vs_window_length.png`
- `bpm_quality_table.csv`
- `tune_by_bpm.png`
- `confidence_by_bpm.png`
- `method_comparison.png`
- `findings_summary.md`

## Physics Review Boundary

Current analysis products are evidence for BPM-derived tune behavior, not final
physics certification. Use [Physics](PHYSICS.md) for acceptance criteria and
[Analysis Checklist](ANALYSIS_CHECKLIST.md) for remaining validation work.
