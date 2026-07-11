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

For acquisition quality, treat captured payload completeness as the source of
truth. `capture_suspect_digitizers` and captured statuses such as
`MISSING_CAPTURE`, `STALE_CAPTURE`, `AHEAD_CAPTURE`, `PAYLOAD_MISSING`, and
`PAYLOAD_MALFORMED` are the primary bad-digitizer signals. Latest-poll-only
suspects are advisory and should not reject an otherwise complete captured
artifact.

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
subsets when explicitly requested, computes global statistics, clusters spill
morphologies, selects review artifacts, and writes final reports. The old
screened Best-10 path remains available but is not the publication ensemble-size
study; contiguous leakage-controlled Best-N validation below answers that
question.

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --gpu-telemetry-interval-seconds 30
```

`--resume` is a spectral-cache reuse switch, not a whole-pipeline checkpoint:
later stages, including subset search, still execute. After a separately
completed subset search, run the stage-specific evolution, statistics,
clustering, selection, artifact, and report wrappers instead of calling the
full pipeline again.

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

`statistics/bpm_global_statistics.csv` retains both the plane-local
`bpm_index` and the channel-token-derived `ring_order`. Ring-quality plots use
`ring_order`; subset-size Pareto plots use the exported `compute_cost` rather
than treating N itself as compute cost.

Follow-up validation and poster-review passes can run as sidecars against a
completed Best-BPM output tree:

```bash
python3 scripts/evaluate_fixed_bpm_sets.py --inputs /path/to/best_bpm --out /path/to/followups
python3 scripts/evaluate_heldout_spectral_support.py --inputs /path/to/best_bpm --out /path/to/followups
python3 scripts/make_best_bpm_poster_artifacts.py --inputs /path/to/best_bpm --manifest /path/to/best_bpm/artifact_selection/artifact_manifest.csv --out /path/to/followups/artifacts
python3 scripts/run_bpm_handoff_analysis.py --inputs /path/to/best_bpm --out /path/to/followups
```

For runs produced before the corrected visibility-duration primitive, repair
only that descriptive field from the exact cached spectra before statistics:

```bash
python3 scripts/repair_best_bpm_visibility_duration.py \
  --config config/best_bpm_mining.yaml \
  --root /path/to/best_bpm \
  --out /path/to/followups/visibility_duration_repair \
  --subset-sizes 1 3 5
```

The repair refuses nonidentical subset/cache coverage or a reproduced visible
fraction mismatch, changes no score or membership, and records row-level old
and corrected durations plus input/output hashes.

The fixed-set sidecar resolves exact per-spill dynamic memberships and frozen
cross-collection memberships, then recomputes dynamic, fixed, and all-BPM
controls from the same spectral cache with one evolution score. It fails on a
cardinality mismatch. This comparison is descriptive because the original
dynamic memberships reuse selection windows; use the leakage-controlled
Best-N study for inferential claims.
Rows with no visible tune retain score zero and explicit `NO_VISIBLE_TUNE` /
`NO_VALID_Q` flags; an unavailable prominence is not fabricated as zero.
Held-out rows likewise preserve exact selected membership when the finalist has
no finite `q_hat`, leave every support metric blank, and report evaluable row
counts/fractions in the summary. A finite `q_hat` still requires every support
metric to be finite for verification.
The handoff sidecar ranks channels but places only strict `VISIBLE_TUNE`
channels in Top-1/3/5/10 sets. Empty-to-empty windows are `NO_VISIBLE_SET` with
Jaccard one; transitions are labeled `VISIBILITY_LOSS`,
`VISIBILITY_RECOVERY`, `PERSISTENT_HANDOFF`, `FLICKER`, or `STABLE` as
appropriate. Native-PNG plots report visible-BPM fraction, spill support,
loss/recovery/handoff fractions, per-turn Top-5 membership frequency, and an
uncapped set of selected-spill visibility/consensus composites. Score color and
strict-rank markers are encoded separately; the plots do not assign an
extraction onset.

### Leakage-Controlled Best-N Study

Use the cached corrected run to sweep contiguous ensemble sizes. Member search
uses only the fit-window prefix; every overlapping window is purged before
later-window evaluation. Complete digitizers, including sibling channels, stay
on one side of each disjoint validation fold.

```bash
python3 scripts/evaluate_best_n_curve.py \
  --config config/best_bpm_mining.yaml \
  --inputs /path/to/corrected-best-bpm \
  --out /path/to/best-n-shards/shard_0 \
  --device cuda \
  --max-n 40 \
  --beam-width 64 \
  --validation-beam-width 64 \
  --folds 5 \
  --fold-seed 20260709 \
  --fit-windows 8 \
  --bootstrap-block-spills 20 \
  --shard-index 0 \
  --shard-count 4 \
  --resume

python3 scripts/merge_best_n_shards.py \
  --shards /path/to/best-n-shards \
  --out /path/to/best-n-merged \
  --bootstrap-samples 1000 \
  --bootstrap-block-spills 20

python3 scripts/verify_best_n_outputs.py \
  --root /path/to/best-n-merged \
  --max-n 40 \
  --curve-cache-rows 4000 \
  --validation-cache-rows 1000 \
  --folds 5
```

Repeat the evaluator for every shard index. `--curve-limit 0` and
`--validation-limit 0` use every cache row; positive limits are stratified and
evenly spaced within collection and plane, not taken from the start of the run.
The merge writes full curves, selected/held-out later-window contrast,
blind full-band channel-disjoint agreement, confidence intervals, a provisional
non-inferiority knee, per-collection summaries, and cross-collection global-N
transfer. The conditioned near-training-tune metrics are never substituted for
the blind agreement result.

The verifier is fail-closed: expected cache-row counts are explicit, every
spill-plane and fold must contain exactly one row for every contiguous N,
membership cardinality and masks must agree, fit/test supports must not overlap,
critical metrics must be finite, detailed and summary counts must match, and all
cross-collection and native-PNG products must exist. A scientifically honest
"no recommendation" is a warning rather than a structural failure; any actual
recommendation must have at least three evaluated larger N values.
Each shard writes `run_contract.json` before science rows. `--resume` is
accepted only when that contract still matches the configuration, source
indexes, N range, beam widths, folds, fit prefix, tolerance, block length,
device, and shard identity. The merger requires exactly one compatible
contract for every declared shard and fails on duplicate curve or validation
keys instead of deduplicating them. Resume skips a spill-plane only when it has
exactly one contiguous row for every N and fold; a row at the maximum N alone
is not treated as completion. Sensitivity comparators require identical full
key sets and never take silent intersections.

Run the declared beam-width, fit-prefix, and fold-seed sample matrix with one
shared baseline:

```bash
python3 scripts/run_best_n_sensitivity_matrix.py \
  --inputs /path/to/corrected-best-bpm \
  --out /path/to/best-n-sensitivity \
  --device cuda \
  --max-n 40 \
  --curve-limit 400 \
  --validation-limit 200 \
  --folds 5 \
  --beam-widths 16 32 64 \
  --fit-windows 4 8 16 \
  --fold-seeds 20260709 20260710 20260711 \
  --parallel-runs 2 \
  --minimum-available-memory-gib 32 \
  --memory-check-seconds 5 \
  --low-memory-samples 3 \
  --resume
```

This executes seven unique runs, verifies each one, compares summary curves for
all three dimensions, performs exact membership/score/tune convergence checks
for beam width, and writes an indexed native-PNG gallery plus the complete
command manifest. Serial execution is the default. The optional two-run mode is
the maximum setting qualified on Spark's single GB10; it requires Linux
`MemAvailable` and terminates both evaluators after three consecutive
five-second samples below the declared 32 GiB floor. Checkpoint files remain
resumable, `memory_guard_abort.json` records any guard trip, and
`execution_controls.json` records the operational settings without changing
the scientific run contracts.
The sensitivity gallery includes central curves and their interval endpoints;
block-length comparisons are expected to change uncertainty even when central
estimates and the recommended N are unchanged.
Configured permutation sample counts are executed as declared; the primary
block sign-flip path does not apply an undocumented upper cap.
The intensity and full-buffer ridge sidecars use the same fail-closed contract
rule. Their verifiers check the merged block length or ridge window geometry
and the SHA-256 source inventory before accepting plots.
`scripts/analyze_next_steps_outputs.py` also refuses supplied primary,
follow-up, Best-N, ridge, or intensity roots unless their JSON verifier reports
are accepted; the executive interpretation cannot silently consume a
provisional tree. A supplied sensitivity root must contain exactly seven unique
verified runs; the report discovers all nested beam/fit/seed recommendation
tables and recomputes the full-run H/V recommendation with the contract-bound
tune tolerance.

Compare fit-window, fold-seed, beam-width, or other completed sensitivity runs:

```bash
python3 scripts/compare_best_n_sensitivity.py \
  --dimension fit_windows \
  --run fit4=/path/to/fit4 \
  --run fit8=/path/to/fit8 \
  --run fit16=/path/to/fit16 \
  --reference-label fit8 \
  --out /path/to/fit-window-sensitivity
```

Remerge the same completed shards at 10, 20, and 40-spill bootstrap blocks and
compare those summaries as an inference sensitivity; no GPU reselection is
needed. Blocks stay inside each collection and do not wrap its endpoints.
Apply the same three block lengths with
`scripts/resummarize_intensity_study.py` and require one retain/reject decision.

Summarize and render the intensity block-length decision:

```bash
python3 scripts/compare_intensity_block_sensitivity.py \
  --run block10=/path/to/intensity-block10 \
  --run block20=/path/to/intensity-block20 \
  --run block40=/path/to/intensity-block40 \
  --out /path/to/intensity-block-sensitivity
```

The count plot separates FDR-significant directional effects from effects whose
confidence intervals clear a predeclared minimum practical effect. The second
plot shows the strongest directional confidence bound divided by that practical
threshold; retention requires the ratio to exceed one plus all tune-stability
gates. The comparator fails unless the exact retained-effect identities agree
at all block lengths and every Best-1 paired effect remains zero.

### Intensity-Assisted Sidecar

Intensity is an auxiliary covariate and spectral aggregation weight; it is
never multiplied into the position waveform. Run sharded waveform analysis,
then merge with collection-aware moving blocks:

```bash
python3 scripts/analyze_intensity_assisted_tune.py \
  --config config/best_bpm_mining.yaml \
  --capture-root /path/to/intensity-capture \
  --out /path/to/intensity-shards/shard_0 \
  --device cuda \
  --analysis-turns 50000 \
  --window-turns 4096 \
  --stride-turns 512 \
  --fit-windows 8 \
  --subset-sizes 1,3,5,7,10,12,15,20 \
  --shard-index 0 \
  --shard-count 4

python3 scripts/merge_intensity_study.py \
  --shards /path/to/intensity-shards \
  --out /path/to/intensity-merged \
  --bootstrap-block-spills 20

python3 scripts/make_intensity_study_plots.py \
  --inputs /path/to/intensity-merged \
  --out /path/to/intensity-gallery

python3 scripts/verify_intensity_outputs.py \
  --root /path/to/intensity-merged \
  --gallery /path/to/intensity-gallery \
  --subset-sizes 1 3 5 7 10 12 15 20 \
  --expected-paired-payload-rows 23999 \
  --expected-spill-rows 12800 \
  --expected-centers 90 \
  --minimum-spills-per-group 199 \
  --expected-block-spills 20
```

Pass the canonical `1,3,5,7,10,12,15,20` grid explicitly to every waveform
shard. If the accepted horizontal or vertical Best-N recommendation is outside
that grid, append each distinct selected N to the comma-separated analysis
list and pass the same union as space-separated values to the verifier. The
expected spill-row count is `200 manifests x 2 planes x 4 methods x number of
distinct N values` (12,800 rows for the canonical eight-size grid); do not
verify an expanded run with the canonical count.

`scripts/resummarize_intensity_study.py` can recompute block-aware inference
from an existing merged study without rereading waveforms. Retain a weighting
method only if the FDR-corrected directional test, minimum practical effect,
median tune-shift tolerance, and 95% spillwise tune-shift tolerance all pass.
Payload corruption after the declared analysis horizon is an integrity
finding, not beam-loss evidence.
The 4096/512 full-buffer geometry contains 90 windows. The strict verifier
checks the audited capture shape, exact source identities,
identical method spill populations and memberships, exact contracted
spill/window center grids, finite global ridge picks, zero errors or invalid first-50000-turn payloads,
equal advertised and on-disk position/intensity sample counts, member
cardinality, every effect decision gate, exact Best-1 weighting
invariance, and every indexed gallery PNG/claim guardrail.
If no selected channel has usable intensity in a window, every weighted method
uses an explicit unweighted fallback. When finite values exist but the 50%
gate would be empty, only the strongest finite selected channel is retained.
`intensity_window_metrics.csv` records the reason in `weight_fallback`, and
`intensity_spill_metrics.csv` records `weight_fallback_window_fraction`; the
merged summary reports the total fallback-window count.
Density-difference plots fail unless unweighted and weighted rows have identical
exact collection/spill/plane/N/window/center keys. They retain only common
finite in-band picks, show higher/lower column-normalized ridge-pick probability
versus unweighted aggregation, and use a disclosed symmetric absolute-P99
display clip; they do not measure physical noise removal.
Exact-zero subtraction fields, including Best-1 controls, carry an explicit
no-redistribution annotation instead of appearing as unexplained blank panels.
All intensity heatmaps use proportional inclusive raster cells so the color
field fills the declared axes. Standalone ridge and binned relationship figures
disclose their nonzero-P98 count-color clip in the visible/indexed copy.
The gallery includes concentration on a common 0-1 scale and a separate
zero-based detail scale extending to 110% of each panel maximum. Use the detail
view only to inspect method separation within that panel; its apparent amplitude
is not comparable across N or plane.
Crossing-turn plots also come in a common 0-50000-turn x/y view and a separate
observed-range detail view. Missing crossings are omitted; both views are
association diagnostics and do not locate extraction onset or establish
causation.
Lag-correlation plots likewise retain a common -1-to-1 Spearman view and a
symmetric panel-detail view. The detail scale varies by panel and is only for
lag-shape inspection; overlapping windows remain exploratory and noncausal.

Build a filterable, lazy-loading HTML index for any generated review directory:

```bash
python3 scripts/build_image_gallery.py \
  --root /path/to/review-gallery \
  --title "Best-N Publication Figure Review"
```

The index reads the intensity and ridge figure manifests when present, exposes
their claim guardrails next to each thumbnail, and otherwise indexes images by
directory. It links assets in place and does not duplicate the gallery payload.

`scripts/make_best_bpm_ridge_density.py` is a targeted poster sidecar for
recreating the older ridge-density visual grammar with exact corrected Best-N
memberships over a 0-50000 turn raw-spill recomputation. Pass a merged Best-N
membership CSV when rendering sizes other than the canonical Best-1/3/5 rows.
With `--legacy-sliding-csv`, paired panels and subtractive maps use only exact
common spill/window ridge points. They quantify ridge-pick redistribution, not
physical noise removal. Primary figures are unmarked; optional
`--extraction-context-variants` add a separately named broad review band that
is never used by the data-derived loss heuristic.
Standalone densities clip nonzero cells above P98 only for color rendering;
subtractive maps symmetrically clip absolute differences above P99. Exported
counts, probabilities, percentiles, and contrast metrics remain unclipped, and
the on-image subtraction legend describes higher/lower pick probability rather
than suppression or noise removal.
For each requested N the sidecar also writes
`ridge_density_legacy_single_vs_best<N>_hv.png`, a primary H/V-by-method
comparison whose four panels use column-normalized pick probability and one
shared P98-clipped color scale. Its caption reports exact paired counts and
warns that visual narrowing must agree with sample-fraction, width, entropy,
and shared-ridge-mass diagnostics.
The exact-paired turn table
`ridge_density_legacy_comparison_by_turn.csv` preserves the unsmoothed values
behind five additional contrast families: adaptive-minus-legacy IQR and
P10-P90 width, peak-bin-fraction gain, normalized-entropy delta, and gain in
mass within `+/-0.0025` tune of the shared method center. Global and selected-N
PNGs use five-window visual smoothing and a zero reference; they describe
where the adaptive picks become more or less concentrated, not a physical
noise spectrum or an extraction-time measurement.
When selected H/V sizes are present, each metric also receives a full-width
stacked H/V composite with one shared y scale plus an 800x1250 portrait twin.
The landscape form avoids unreadable half-width paper panels; the portrait form
fills the inherited A0 evidence frame without cropping.
After the Best-N verifier selects H and V, pass both `--selected-h-n` and
`--selected-v-n` and include both values in `--subset-sizes`. The run then
writes `ridge_density_legacy_single_vs_best_h<H>_v<V>_hv.png` plus one clean
selected-N concentration panel per plane. The ridge verifier requires these
assets whenever `selected_plane_sizes` is present in the run contract.
It also writes two selector-defect controls on exact common points:
`ridge_density_best1_vs_selected_h<H>_v<V>_hv.png` directly isolates the
corrected ensemble-size contrast, while
`ridge_density_legacy_vs_best1_vs_selected_h<H>_v<V>_hv.png` shows the legacy
selector, corrected Best-1, and selected Best-N as separate columns on one
probability scale. Do not attribute the full legacy-to-Best-N contrast solely
to adding BPMs.

The favorite `18d321dbd4fe` images bin one tracked `selected_tune` per spill
and window; they are not spectral-power heatmaps. For an exact paired
comparison, use the archived protocol: 4096/256 Hann windows, a 4096-turn
injection seed, RMS-per-BPM normalization, mean subtraction, zeroed DC bin,
confidence 2.0, tracking half-width and maximum step 0.005, and H/V bands
0.620-0.680 / 0.690-0.740. The publication verifier rejects drift from those
settings. Color in standalone panels is spill count; paired and subtractive
panels are explicitly column-normalized probabilities.
All density renderers use proportional inclusive cell bounds so the heatmap,
declared tune limits, and percentile or median overlays share one pixel mapping.

Verify the primary gallery before using any panel:

```bash
python3 scripts/verify_ridge_density_outputs.py \
  --root /path/to/ridge-gallery \
  --subset-sizes 1 3 5 10 15 20 30 40 \
  --minimum-spills 1900 \
  --expected-centers 180
```

The verifier requires all 2000 adaptive spill-planes and all 1988 legacy
spill-planes at the exact 180-center grid. It rejects duplicate memberships or
points, wrong selected-member cardinality, out-of-band tune picks, incomplete exact legacy
pairing or contrast metrics, unresolved membership/payload warnings, and any
missing, invalid, undersized, or uncaptioned manifest figure. Other data-quality
warnings remain visible and require written review.

Materialize final poster and paper inputs only after every analysis gate passes.
First audit the complete raw publication corpus independently of the spectral
pipelines:

```bash
python3 scripts/audit_delivery_ring_payloads.py \
  --capture-root /path/to/position-collection-a \
  --capture-root /path/to/position-collection-b \
  --capture-root /path/to/intensity-capture \
  --out /path/to/delivery-ring-payload-audit \
  --analysis-turns 50000 \
  --plateau-turns 128
```

Publication acceptance requires 2200 manifests, 263999 position rows, 23999
paired position/intensity rows, the 120-channel/30-digitizer union topology in
each collection, and no first-50000-turn corruption, long exact plateau, or
repeated device-coded raw fallback pair. Then bind every accepted root:

```bash
python3 scripts/prepare_ibic2026_publication.py \
  --primary-root /path/to/corrected-best135 \
  --followup-root /path/to/corrected-best135/followups/publication \
  --best-n-root /path/to/best-n-full \
  --ridge-root /path/to/ridge-50000 \
  --intensity-root /path/to/intensity-refresh \
  --payload-audit-root /path/to/delivery-ring-payload-audit \
  --publication-root publication/ibic2026
```

This command requires accepted 10/20/40-block Best-N outputs, four OK
cross-collection transfer rows, seven verified beam/fit/fold sensitivity runs
with an eligible H and V recommendation in every run,
an accepted mixed-N ridge contract, zero retained intensity effects, and the
exact corpus-bound raw-payload audit. It
writes `poster/content.json`, `paper/results_table.tex`, verifier-derived
`paper/results_macros.tex`, exact figure copies, `results_payload.json`,
`PREPARATION_REPORT.md`, and `source_manifest.csv`. The macros bind primary
Best-1/3/5 scores and intensity effect counts to the accepted tables instead of
preserving literals from an earlier run. They also bind 4000 full-curve
spill-plane cases, 1000 stratified validation cases, and five digitizer folds
from the accepted block-20 Best-N verifier; materialization rejects any other
study design.

Package final publication sources, rendered deliverables, reports, and broad
review galleries into one local handoff directory:

```bash
python3 scripts/package_publication_review.py \
  --component publication=publication/ibic2026 \
  --component report=/path/to/final-analysis-report \
  --component best-n-gallery=/path/to/best-n-gallery \
  --component ridge-gallery=/path/to/ridge-gallery \
  --component intensity-gallery=/path/to/intensity-gallery \
  --component payload-audit=/path/to/delivery-ring-payload-audit \
  --out review-artifacts/ibic2026-final-review-YYYYMMDD
```

Each `LABEL=PATH` component is copied. `MANIFEST.csv` records every packaged
file's original path, byte size, and SHA-256 checksum, while
`PACKAGE_INDEX.md` summarizes the package. The generated `index.html` is a
self-contained, lazy-loading gallery with text and category filters across
every packaged image; use `--title` to set its heading. The output must be new
or empty so an older review bundle cannot be silently overwritten.

After visually inspecting the final poster and all four paper pages, close the
publication directory with the explicit human-QA gate:

```bash
python3 scripts/finalize_ibic2026_publication.py \
  --root publication/ibic2026 \
  --abstract /path/to/abstract-54.pdf \
  --poster-template /path/to/FNAL_Scientific_Poster_A0_VRT_May25.potx \
  --poster-visual-qa pass \
  --paper-visual-qa pass
```

The finalizer verifies immutable reference hashes, required source and render
files, A0 poster and four-page paper geometry, PNG dimensions, byte identity
between the named poster PNG and its 150 dpi PDF raster, the selected H/V
payload, seven sensitivity runs, four OK transfer rows, zero retained intensity
effects, the exact raw-payload corpus, unresolved-copy gates, and every final
slide XML placeholder. It reads the PPTX as a ZIP package without modifying it
and rejects a placeholder shape whose DrawingML text is empty or whitespace.
It also recomputes the exact portable poster and paper checksum inventories,
the poster builder's recorded content/asset/output hashes and dimensions, and
the delivered zero-issue template-fidelity report. It writes
`compliance_report.md` and `publication_manifest.csv`; the manifest inventories
every publication file except itself.

`TECTONIC_FLAGS` is optional. Leaving it unset uses the ordinary Tectonic
command, while `TECTONIC_FLAGS=--only-cached` explicitly forbids resource
downloads; both shell paths are valid under the system Bash 3.2 runtime.

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
