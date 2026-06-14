# BPM-Only Poster Analysis

This workflow implements the `BPM_DGX_POSTER_CODEX_PLAN.md` poster sprint as a
standalone tool layer around collected `tbt-monitor` artifacts. It is
complementary to the Rust acquisition and tune-analysis commands; it does not
replace live capture, online monitoring, or captured-bundle reanalysis.

This phase is BPM-only. Do not add Schottky comparison, Schottky labels, or
Schottky validation products to these poster outputs.

## Inputs

Use the complete collected artifact set on `drbpm1`, not only the local copied
review subset. The manifest builder accepts any mix of:

- `candidate_spills.csv` from curation/ranking runs
- `spills_summary.csv` from `analyze-spills` or `analyze-captured-spills`
- `capture_index.csv` from raw capture runs

For the current collected-data layout, the intended source root is:

```bash
/home/derekste/out
```

If the DGX Spark has the `drbpm1` output tree mounted or copied, point `--input`
at that mounted/copied path and use `--device cuda` or `--device auto`.

Spark currently has a user CuPy environment at:

```bash
/home/derekste/venvs/cupy-spark-cu13
```

Use that Python when running GPU-backed poster benchmarks on Spark:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/bpm_dgx_poster.py run-all \
  --input /home/derekste/bpm-dgx-poster-20260609-spark-input/tune-curation \
  --out /home/derekste/bpm-dgx-poster-20260609-spark-cu13 \
  --flashes 128 256 512 \
  --device auto
```

## Raw Captured-Spill GPU Analysis

Use `scripts/gpu_analyze_captured_spills.py` when the input is the raw
`capture-spills` bundle tree (`spill_<target_ms>/manifest.json` plus payload
`.bin` files). This path performs the FFT/window analysis directly from raw
payload bytes and uses CuPy for the array-heavy FFT batches.

Copy the two 1000-spill position-only runs from `drbpm1` to Spark:

```bash
ssh drbpm1
rsync -a --partial -e 'ssh -K' \
  /home/derekste/out/tbt-capture-positiononly-1000-20260608-183119 \
  /home/derekste/out/tbt-capture-positiononly-1000-20260608-231330 \
  spark.fnal.gov:/home/derekste/tbt-spills-2000/
```

Run a small CUDA smoke test first:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/gpu_analyze_captured_spills.py \
  --input /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-gpu-smoke \
  --device cuda \
  --limit 20 \
  --flashes 128
```

Then run the full 2000-spill pass:

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
  --svd-denoise \
  --progress 25
```

The analyzer mirrors the Rust tune path's important numerical choices:
mean-subtracted Hann windows, full FFT power, median-band confidence,
parabolic peak refinement, flash window placement, and local tune tracking
seeded from the injection estimate. It also adds the tune-evolution upgrade
products from `BPM_TUNE_EVOLUTION_ANALYSIS_UPGRADE_PLAN.md`:

- ridge-density plots across accepted spills:
  `ridge_density_h.png`, `ridge_density_v.png`
- representative Hann and multitaper spectrogram overlays:
  `spectrogram_h_hann.png`, `spectrogram_v_hann.png`,
  `spectrogram_h_multitaper.png`, `spectrogram_v_multitaper.png`
- method comparisons and single-spill poster spectrograms:
  `method_comparison_h.png`, `method_comparison_v.png`,
  `single_spill_spectrogram_h.png`, `single_spill_spectrogram_v.png`
- dynamic-programming ridge traces and overlays:
  `ridge_trace_h.csv`, `ridge_trace_v.csv`,
  `ridge_overlay_h.png`, `ridge_overlay_v.png`
- optional SVD/PCA denoising products when `--svd-denoise` is set:
  `svd_singular_values_h/v.png`, `svd_spectrogram_*_modes_<N>.png`,
  `svd_ridge_compare_h/v.png`, and `svd_method_comparison.png`
- DGX benchmark products:
  `dgx_benchmark.md`, `dgx_benchmark.png`, and
  `dgx_processing_benchmark.png`

CPU mode is available for reproducibility:

```bash
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/gpu_analyze_captured_spills.py --input <captured-run-dir> --out <out-dir> --device cpu --limit 2
```

## Full Pipeline

Run from a checkout containing `scripts/bpm_dgx_poster.py`:

```bash
python3 scripts/bpm_dgx_poster.py run-all \
  --input /home/derekste/out \
  --out poster-artifacts/drbpm1-poster \
  --flashes 128 256 512 \
  --device auto
```

The local copied review subset can be used only as a smoke target:

```bash
python3 scripts/bpm_dgx_poster.py run-all \
  --input review-artifacts \
  --out /private/tmp/tbt-monitor-poster-smoke \
  --flashes 128 256 512 \
  --device cpu
```

## Script Layout

The single entrypoint has subcommands matching the poster plan, and thin wrapper
scripts are available for the same phases:

```bash
python3 scripts/build_manifest.py --input /home/derekste/out --out processed/
python3 scripts/run_baseline_batch.py --manifest processed/dataset_manifest.csv --out processed/
python3 scripts/run_flash_batch.py --manifest processed/dataset_manifest.csv --flashes 128 256 512 --out processed/
python3 scripts/build_spectrograms.py --manifest processed/dataset_manifest.csv --device auto --out plots/
python3 scripts/run_bpm_subset_checks.py --manifest processed/dataset_manifest.csv --out processed/
python3 scripts/train_quality_model.py --features processed/features.csv --out models/
python3 scripts/train_tune_model.py --features processed/features.csv --out models/
python3 scripts/benchmark_dgx_processing.py --manifest processed/dataset_manifest.csv --out benchmarks/
python3 scripts/make_poster_plots.py --input processed/ plots/ models/ benchmarks/ --out poster_plots/
```

Every command is CPU-reproducible. CUDA acceleration is used only where CuPy is
available; otherwise the benchmark reports GPU unavailability instead of
silently fabricating a DGX result.

## Outputs

`run-all` writes these output groups:

- `processed/dataset_manifest.csv`, `dataset_summary.md`, `dataset_overview.png`
- `processed/baseline_summary.csv`, `baseline_quality_summary.md`, baseline
  histograms/trends, and `features.csv`
- `processed/flash_summary_128.csv`, `flash_summary_256.csv`,
  `flash_summary_512.csv`, flash waterfalls, and flash comparison plots
- `plots/representative_spectrogram_h/v.png`,
  `composite_waterfall_h/v.png`, and `median_spectrogram_h/v.png`
- `processed/bpm_subset_summary.csv` and subset consistency plots
- `models/quality_model_metrics.md` and optional weak-label quality plots
- `models/tune_model_metrics.md`; tune/ridge training is skipped unless
  independent `label_qx`/`label_qy` columns are provided
- `benchmarks/dgx_benchmark.md` and `dgx_processing_rate.png`
- `poster_plots/poster_plot_index.md` with the high-priority poster plot set

The raw captured-spill GPU analyzer writes:

- `gpu_spills_summary.csv`
- `gpu_sliding_tune.csv`
- `gpu_flash_summary_<N>.csv`
- `gpu_analysis_summary.md` and `gpu_benchmark.md`
- `gpu_median_tune_vs_spill.png`
- `gpu_flash_waterfall_h.png`, `gpu_flash_waterfall_v.png`
- `gpu_median_spectrogram_h.png`, `gpu_median_spectrogram_v.png`
- `injection_tune_reproducibility.png`
- `ridge_density_h.png`, `ridge_density_v.png`
- `single_spill_spectrogram_h.png`, `single_spill_spectrogram_v.png`
- `spectrogram_h_hann.png`, `spectrogram_v_hann.png`, and multitaper
  companions when requested
- `spectrogram_method_compare_h.png`, `spectrogram_method_compare_v.png`
- `method_comparison_h.png`, `method_comparison_v.png`
- `ridge_trace_h.csv`, `ridge_trace_v.csv`
- `ridge_overlay_h.png`, `ridge_overlay_v.png`
- optional `svd_*` denoising plots when `--svd-denoise` is set
- `dgx_benchmark.md`, `dgx_benchmark.png`, and
  `dgx_processing_benchmark.png`

Current Spark outputs for the 2000-spill position-only dataset:

- `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w2048`
  - 2048-turn window, requested `--flashes 128`, effective 24 windows per
    50,000-turn plane under the Rust-compatible flash cap.
  - 2000 spills processed, 1776 usable, 96000 sliding rows.
- `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w256`
  - 256-turn window, true 128 flash windows per plane.
  - 2000 spills processed, 1775 usable, 512000 sliding rows.

When only ranked summary artifacts are present, median spectrograms and subset
checks are conservative proxies based on selected tune traces and summary fields.
Use full offline batch outputs or per-BPM study artifacts for final physics
claims about spectral power medians or BPM subset robustness.

## Spark BPM Autosweep

`SPARK_BPM_AUTOSWEEP_RANKING_AND_CLASSIFICATION_PLAN.md` is implemented as a
staged raw-bundle workflow under `scripts/`. It is BPM-only, uses Tier A
position-only Spark data first, and intentionally avoids a naive full
Cartesian sweep.

Tier A inputs are the two raw position-only collections:

```bash
/home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119
/home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330
```

Stage 0 writes the dataset inventory, payload health table, and lightweight
metadata cache:

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
interaction grid:

```bash
python3 scripts/run_autosweep.py \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --mode pilot \
  --spills 200 \
  --max-configs 300 \
  --device cuda \
  --out /home/derekste/tbt-spills-2000-autosweep/pilot
```

Then rank and summarize:

```bash
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/pilot
python3 scripts/make_initial_analysis_summary.py \
  --ranking-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --top 10
```

The elite full-data stage turns the pilot ranking into explicit H/V/poster
handoff lists and filters the Stage 0 manifest to usable Tier A spills from
`spill_health.csv`:

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
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --min-spills 500
python3 scripts/make_elite_full_summary.py \
  --elite-dir /home/derekste/tbt-spills-2000-autosweep/elite-full
```

The elite builder deduplicates effective configs, includes explicit H and V
roles (`top_physics`, `top10_robust`, `median_or_trimmed`,
`baseline_mean`), adds a poster-safe best-poster config, and preserves
rejected/flagged pilot rows in diagnostics. Full mode consumes the supplied
config list exactly.

The score formula is fixed:

```text
0.25 injection + 0.25 ridge + 0.20 bpm_robustness
+ 0.15 spectrogram_quality + 0.10 usable_fraction
+ 0.05 compute_efficiency
```

The ranker also emits `poster_score`, `physics_score`, and `compute_score`;
spill labels are `GOOD`, `MARGINAL`, `BAD`, `NO_SIGNAL`,
`AMBIGUOUS_RIDGE`, or `MISSING_DATA`; config labels are `RECOMMENDED`,
`PROMISING`, `EXPLORATORY`, `REJECTED`, `TOO_SLOW`, `OVERFITS_BAND`,
`UNSTABLE_H`, or `UNSTABLE_V`.

Required autosweep outputs are:

- `dataset_manifest.csv`, `spill_health.csv`, `spill_cache_index.json`,
  `dataset_summary.md`
- `autosweep_config_grid.csv`, `autosweep_run_log.csv`
- `autosweep_spill_scores.csv`, `autosweep_config_scores.csv`,
  `autosweep_collection_scores.csv`
- `autosweep_ranked_configs.csv`, `autosweep_ranked_spills.csv`,
  `autosweep_rejected_configs.csv`, `top_configs_for_full.csv`
- `initial_analysis_summary.md`, `plots/*.png`, and
  `top_artifacts_manifest.csv`
- elite full-stage files: `elite_dataset_manifest.csv`,
  `elite_configs_h.csv`, `elite_configs_v.csv`,
  `elite_configs_for_full.csv`, `elite_config_sources.csv`,
  `elite_rejected_config_diagnostics.csv`, `elite_full_summary.md`,
  `elite_artifacts_manifest.csv`, and `poster_candidate_gallery/`

## Validation

Run the built-in smoke test:

```bash
python3 scripts/bpm_dgx_poster.py --self-test
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
```

The smoke test uses synthetic ranked artifacts and verifies that the full
standalone pipeline writes manifest, baseline, flash, waterfall, benchmark, and
poster-index outputs.
