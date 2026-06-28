# Spark Workflows

This guide covers offline Spark/GPU work: direct raw captured-spill analysis,
poster/DGX artifact generation, staged autosweep/ranking, Best-BPM mining, and
GPU telemetry. These workflows are BPM-only unless a future plan explicitly
adds reference-monitor comparison.

## Runtime Assumptions

Current Spark-side examples use:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python
```

Tier A raw position-only inputs are:

```bash
/home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119
/home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330
```

Use `ssh -K` for host-to-host copies and remote commands. If direct Spark SSH
is unavailable from a given host, route through `drbpm1`.

## Direct Raw Captured-Spill GPU Analysis

Use `scripts/gpu_analyze_captured_spills.py` when the input is raw captured
bundle trees containing `spill_<target_ms>/manifest.json` and payload `.bin`
files.

CUDA smoke:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/gpu_analyze_captured_spills.py \
  --input /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-gpu-smoke \
  --device cuda \
  --limit 20 \
  --flashes 128
```

Full upgraded path:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/gpu_analyze_captured_spills.py \
  --input /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-gpu-upgrade \
  --device cuda \
  --turn-start 0 \
  --turn-end 50000 \
  --flashes 128 \
  --spectrogram-method both \
  --ridge-method dp \
  --ridge-source-method multitaper \
  --svd-denoise \
  --progress 25
```

Main outputs include:

- `gpu_spills_summary.csv`
- `gpu_sliding_tune.csv`
- `gpu_flash_summary_<N>.csv`
- `gpu_analysis_summary.md`
- ridge density, method comparison, single-spill spectrogram, SVD, and
  benchmark plots where requested

## Poster/DGX Artifact Layer

Use `scripts/bpm_dgx_poster.py` after data has already been collected and, when
available, analyzed or ranked:

```bash
python3 scripts/bpm_dgx_poster.py run-all \
  --input /home/derekste/out \
  --out poster-artifacts/drbpm1-poster \
  --flashes 128 256 512 \
  --device auto
```

This layer accepts `candidate_spills.csv`, `spills_summary.csv`, and
`capture_index.csv` trees and writes poster manifests, flash summaries,
spectrogram/waterfall products, optional weak-label model reports, benchmarks,
and a poster plot index. It is a reporting layer, not a replacement for DAQ or
captured-bundle analysis.

## Spark BPM Autosweep

Use autosweep when the question is which analysis configuration works best over
raw BPM bundles. The workflow avoids a naive full Cartesian sweep.

Stage 0 inventory and health:

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

Pilot:

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
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/pilot
python3 scripts/make_initial_analysis_summary.py \
  --ranking-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --top 10
```

Elite full-data stage:

```bash
python3 scripts/build_elite_full_stage.py \
  --pilot-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --health /home/derekste/tbt-spills-2000-autosweep/stage0/spill_health.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --expected-usable-spills 1988
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

`run_autosweep.py` is serial by default. Start with `--parallel-jobs 2` on
Spark and increase only after telemetry shows remaining headroom.

## Best-BPM Mining

Use Best-BPM mining when the question is which BPM subsets carry the strongest
within-spill tune evidence, not which analyzer configuration is best.

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --resume \
  --gpu-telemetry-interval-seconds 30
```

Verify a completed output package:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/verify_best_bpm_outputs.py \
  --root /home/derekste/best_bpm_mining
```

Output groups:

- `manifest/`: spill, BPM, channel, and rejection inventories.
- `cache/`: spectral cache index and per-spill spectra.
- `per_bpm/`: per-BPM peak/feature tables.
- `consensus/`: unsupervised within-spill consensus tune clusters.
- `subset_search/`: best-1, best-3, screened best-5/best-10, audits, progress.
- `evolution/`, `statistics/`, `clustering/`: downstream ranking and stability
  products.
- `artifact_selection/`, `artifacts/`, `reports/`: review plots and summaries.
- `logs/`: verification reports and optional GPU telemetry.

Best-1 and best-3 are globally exhaustive over valid BPMs. Best-5 and best-10
are exact searches inside screened pools with independent audit metadata.

The finalist re-evaluation part of `evolution/` can run in parallel. The full
pipeline passes `--workers` through to this stage and writes progress under
`evolution/progress/`. To rerun only the evolution stage from existing subset
search outputs:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/evaluate_best_subset_evolution.py \
  --config config/best_bpm_mining.yaml \
  --subsets /home/derekste/best_bpm_mining/subset_search \
  --cache /home/derekste/best_bpm_mining/cache \
  --features /home/derekste/best_bpm_mining/per_bpm \
  --manifest /home/derekste/best_bpm_mining/manifest \
  --workers 4 \
  --out /home/derekste/best_bpm_mining/evolution
```

## GPU Telemetry

Enable telemetry on autosweep or Best-BPM runs:

```bash
--gpu-telemetry-interval-seconds 30
```

This writes `logs/gpu_telemetry.csv`, `logs/gpu_telemetry_summary.json`, and
`logs/gpu_telemetry_summary.md` when the wrapper owns the telemetry lifecycle.
Summarize an existing CSV:

```bash
python3 scripts/gpu_run_telemetry.py summarize \
  --input /path/to/gpu_telemetry.csv \
  --summary-json /tmp/gpu_telemetry_summary.json \
  --summary-md /tmp/gpu_telemetry_summary.md
```

On Spark GB10, `nvidia-smi` may report `utilization.memory=0` while compute
processes still show allocated memory. Treat compute PIDs and GPU utilization
as the reliable occupancy signals.

## Validation

Run local synthetic/smoke checks before pushing substantial script changes:

```bash
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/best_bpm_mining
python3 scripts/gpu_run_telemetry.py summarize \
  --input /path/to/gpu_telemetry.csv \
  --summary-json /tmp/gpu_telemetry_summary.json
```

Use [Poster Analysis](POSTER_ANALYSIS.md) for the longer historical
phase-by-phase output inventory.
