# Usage Guide

This is the command reference for `tbt-monitor-tui`. For task-oriented guides,
start with [DAQ Guide](DAQ.md), [Analysis Chains](ANALYSIS_CHAINS.md),
[Spark Workflows](SPARK.md), or [Operations](OPERATIONS.md). Use
[Config Reference](CONFIG_REFERENCE.md) for config keys and
[Architecture](ARCHITECTURE.md) for data flow, synchronization policy, and
artifact schema details.

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

When captured payloads are `120/120`, use the captured-payload timestamp
distribution to understand how those 120 streams were bucketed. Latest-ID
snapshot staleness is diagnostic context and can be one machine event old even
when the captured payload is complete.

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

## Standalone Poster/DGX Analysis

Use `scripts/bpm_dgx_poster.py` after data has already been collected and, where
available, analyzed or ranked. This is a BPM-only poster workflow; it does not
perform Schottky comparison or use Schottky-derived labels.

The complete collected artifact set lives on `drbpm1`, so the full poster run
should target that tree or a DGX-mounted/copy of it:

```bash
python3 scripts/bpm_dgx_poster.py run-all \
  --input /home/derekste/out \
  --out poster-artifacts/drbpm1-poster \
  --flashes 128 256 512 \
  --device auto
```

`--input` accepts directories or files containing `candidate_spills.csv`,
`spills_summary.csv`, and `capture_index.csv`. The command writes manifest,
baseline, flash, spectrogram/waterfall, subset, optional ML, benchmark, and
poster-plot products. Use `--device cuda` on DGX Spark when CuPy is available;
CPU mode remains the reproducible fallback.

For the raw 2000-spill captured payload set, copy the two capture directories
to Spark and run the GPU analyzer directly over `manifest.json`/payload bundles:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/gpu_analyze_captured_spills.py \
  --input /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-gpu-upgrade \
  --device cuda \
  --flashes 128 \
  --spectrogram-method both \
  --ridge-method dp \
  --ridge-source-method multitaper \
  --svd-denoise
```

This emits `gpu_spills_summary.csv`, `gpu_sliding_tune.csv`,
`gpu_flash_summary_<N>.csv`, median tune trends, flash waterfalls, median
band-spectrogram PNGs, and a benchmark/summary markdown pair. The upgraded
tune-evolution path also writes:

- `ridge_density_h.png` and `ridge_density_v.png`
- representative Hann/multitaper spectrogram overlays and method comparisons
- `ridge_trace_h.csv`, `ridge_trace_v.csv`, and `ridge_overlay_h/v.png`
- optional `svd_*` denoising plots when `--svd-denoise` is set
- `dgx_benchmark.md`, `dgx_benchmark.png`, and
  `dgx_processing_benchmark.png`

Key raw-spill GPU-analysis options:

- `--device cpu|cuda|auto`: select NumPy CPU, CuPy CUDA, or best available
  backend; default is CPU for reproducibility.
- `--turn-start N --turn-end N`: analyze a bounded turn interval from each
  waveform, useful when buffers exceed the first 50,000 turns needed for the
  current study.
- `--plane H|V|both`: analyze one plane or both planes.
- `--spectrogram-method hann|multitaper|both`: select Hann, multitaper, or both
  for comparison plots.
- `--multitaper-nw 2.5 --multitaper-k 4`: set DPSS/Slepian taper parameters.
- `--bpm-combination mean|median|trimmed_mean_10pct|best_single_bpm|top10_by_confidence|top20_by_confidence|odd_even|first_second_half`:
  select how BPM spectra are combined for a plane.
- `--bpm-normalization none|rms_per_bpm|mad_per_bpm|injection_rms_per_bpm`:
  normalize each BPM trace before spectral combination.
- `--detrend none|mean_subtract|linear|polynomial_order_2` and
  `--dc-handling keep|zero_dc_bin|ignore_low_bins`: control window
  preprocessing.
- `--ridge-method greedy|dp`: select local peak tracking or
  dynamic-programming ridge extraction.
- `--ridge-jump-penalty`, `--ridge-jump2-penalty`, `--ridge-max-step`, and
  `--ridge-normalize row|global|none`: tune DP smoothness behavior.
- `--ridge-anchor-enabled true|false`, `--ridge-anchor-h`, `--ridge-anchor-v`,
  and `--ridge-anchor-half-width`: apply H/V tune-anchor priors to DP ridge
  selection; defaults are H `0.65`, V `0.72`, half-width `0.02`.
- `--svd-denoise --svd-modes 1,2,4 --svd-normalize-bpm true|false`: create
  representative SVD/PCA denoising products.

Use `--limit` for Spark smoke tests before the full pass.

## Spark BPM Autosweep

Use the autosweep scripts when you want to explore tune-tracking parameter
space over raw captured BPM bundles without running a full Cartesian sweep.
The v1 path is BPM-only and is intended for the two Tier A Spark position-only
collections under `/home/derekste/tbt-spills-2000`.

Stage 0 inventories raw captures, checks payload health, and writes a metadata
cache:

```bash
python3 scripts/build_collection_manifest.py \
  --roots /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-autosweep/stage0

python3 scripts/validate_spill_integrity.py \
  --manifest /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/stage0 \
  --device cuda

python3 scripts/build_spill_cache.py \
  --manifest /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --health /home/derekste/tbt-spills-2000-autosweep/stage0/spill_health.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/stage0 \
  --device cuda
```

Pilot mode runs baseline configs, factor screening, and a deterministic capped
interaction grid (`seed=20260613`, default `max-configs=300`):

```bash
python3 scripts/run_autosweep.py \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --mode pilot \
  --spills 200 \
  --max-configs 300 \
  --device cuda \
  --parallel-jobs 2 \
  --gpu-telemetry-interval-seconds 30 \
  --out /home/derekste/tbt-spills-2000-autosweep/pilot
```

Rank the pilot and make the initial summary package:

```bash
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/pilot

python3 scripts/make_initial_analysis_summary.py \
  --ranking-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --top 10
```

Build an explicit elite full-data package from the pilot rankings and Stage 0
health table:

```bash
python3 scripts/build_elite_full_stage.py \
  --pilot-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --health /home/derekste/tbt-spills-2000-autosweep/stage0/spill_health.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --expected-usable-spills 1988
```

Full mode consumes the supplied elite config list exactly; the builder owns
baseline inclusion and usable-spill filtering:

```bash
python3 scripts/run_autosweep.py \
  --dataset /home/derekste/tbt-spills-2000-autosweep/elite-full/elite_dataset_manifest.csv \
  --mode full \
  --config-list /home/derekste/tbt-spills-2000-autosweep/elite-full/elite_configs_for_full.csv \
  --device cuda \
  --heavy-plots \
  --job-timeout-seconds 900 \
  --parallel-jobs 2 \
  --gpu-telemetry-interval-seconds 30 \
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --min-spills 500
python3 scripts/make_elite_full_summary.py \
  --elite-dir /home/derekste/tbt-spills-2000-autosweep/elite-full
```

Autosweep outputs include `dataset_manifest.csv`, `spill_health.csv`,
`spill_cache_index.json`, `autosweep_config_grid.csv`,
`autosweep_run_log.csv`, `autosweep_spill_scores.csv`,
`autosweep_config_scores.csv`, `autosweep_collection_scores.csv`,
`autosweep_ranked_configs.csv`, `autosweep_ranked_spills.csv`,
`autosweep_rejected_configs.csv`, `top_configs_for_full.csv`, and
`initial_analysis_summary.md`. The elite full stage adds
`elite_dataset_manifest.csv`, `elite_configs_h.csv`, `elite_configs_v.csv`,
`elite_configs_for_full.csv`, `elite_config_sources.csv`,
`elite_rejected_config_diagnostics.csv`, `elite_full_summary.md`,
`elite_artifacts_manifest.csv`, and `poster_candidate_gallery/`.

`run_autosweep.py` runs serially by default. Use `--parallel-jobs 2` to start
conservatively on Spark, then try 3-4 if GPU telemetry shows the device is still
underused. The scheduler runs independent config/view jobs concurrently while
keeping each job in its isolated `jobs/<config_hash>/<view>/` directory. It also
keeps at most one active view per config so a timed-out config can still mark
later views as `prior_view_too_slow`.

Use `--gpu-telemetry-interval-seconds 30` to record run-level GPU utilization,
active compute PIDs, and power draw to `logs/gpu_telemetry.csv`. The runner
writes `logs/gpu_telemetry_summary.json` and
`logs/gpu_telemetry_summary.md` after the run. The same CSV can be summarized
later with:

```bash
python3 scripts/gpu_run_telemetry.py summarize \
  --input /home/derekste/tbt-spills-2000-autosweep/pilot/logs/gpu_telemetry.csv \
  --summary-json /home/derekste/tbt-spills-2000-autosweep/pilot/logs/gpu_telemetry_summary.json \
  --summary-md /home/derekste/tbt-spills-2000-autosweep/pilot/logs/gpu_telemetry_summary.md
```

`scripts/bootstrap_spark_env.sh` can create a minimal optional venv, but the
scripts intentionally avoid pandas, scipy, pyarrow, and zarr. NumPy is required;
CuPy is optional for GPU execution.

See `docs/POSTER_ANALYSIS.md` for the phase-by-phase script layout and output
inventory.

## Best-BPM Mining

Use the Best-BPM mining pipeline when the question is not which analyzer config
is best, but which BPM subsets carry the most defensible within-spill tune
evidence. The pipeline consumes raw captured position bundles, caches per-BPM
spectra, builds within-spill consensus clusters, searches best 1/3/5/10 BPM
subsets, computes global/fixed/dynamic statistics, clusters spill morphologies,
selects review artifacts, and writes final reports.

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --resume \
  --gpu-telemetry-interval-seconds 30
```

Verify that a completed output directory satisfies the expected artifact
contract:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/verify_best_bpm_outputs.py \
  --root /home/derekste/best_bpm_mining
```

Use `--limit` for a Spark smoke test and `--workers` to fan out per-spill
manifest/integrity checks, sharded per-BPM peak extraction, and cache-backed
consensus clustering. Subset search can also shard spill/plane rows, with the
CUDA path capped by `subset_search.cuda_workers` to avoid oversubscribing one
GPU. During subset search, each worker writes
`subset_search/progress/shard_*.json` and parent merge status in
`subset_search/progress/parent_status.json`; these files are live progress
telemetry, not physics outputs. Single-GPU
FFT/cache construction intentionally uses one CuPy worker to avoid multiple
independent CUDA contexts, and subset scoring still uses the CUDA path. The
default config is JSON-compatible YAML so the Spark runtime only needs stdlib
plus NumPy/CuPy.

Main outputs:

- `manifest/spills.csv`, `manifest/bpm_index.csv`,
  `manifest/channels.csv`, `manifest/rejections.csv`
- `cache/index/spectral_cache.csv` and per-spill `.npy` spectra
- `per_bpm/per_bpm_*features.csv`
- `consensus/spill_consensus_*.csv`
- `subset_search/best1`, `best3`, `best5`, `best10`, and
  `subset_search/audit_results.csv`
- `subset_search/progress/*.json` for long-run progress/merge visibility
- `evolution/subset_evolution_*.csv` and
  `evolution/finalist_reevaluation.csv`
- `statistics/*.csv`, `clustering/*.csv`,
  `artifact_selection/artifact_manifest.csv`
- `artifacts/global/*.png`, selected per-spill plots, and
  `reports/strong_bpm_analysis_summary.md`
- `logs/best_bpm_verification.json` and
  `logs/best_bpm_verification_report.md` when the verifier is run
- `logs/gpu_telemetry.csv`, `logs/gpu_telemetry_summary.json`, and
  `logs/gpu_telemetry_summary.md` when GPU telemetry is enabled

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
