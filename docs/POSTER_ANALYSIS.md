# BPM-Only Poster Analysis

This workflow implements the `BPM_DGX_POSTER_CODEX_PLAN.md` poster sprint as a
standalone tool layer around collected `tbt-monitor` artifacts. It is
complementary to the Rust acquisition and tune-analysis commands; it does not
replace live capture, online monitoring, or captured-bundle reanalysis.

For the current subsystem-level Spark entry point, including raw GPU analysis,
autosweep, Best-BPM mining, and telemetry, see [Spark Workflows](SPARK.md).

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
  --parallel-jobs 2 \
  --gpu-telemetry-interval-seconds 30 \
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
  --parallel-jobs 2 \
  --gpu-telemetry-interval-seconds 30 \
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

Parallel autosweep execution is opt-in with `--parallel-jobs`; 2 is the current
Spark ceiling and remains gated on the guarded two-job smoke. GPU utilization
alone is not permission to raise the count: any 3-4 job attempt requires a
separate unified-memory watchdog qualification. Each analyzer job writes into
its own config/view directory, and the
run log remains sorted by deterministic job id. `--gpu-telemetry-interval-seconds`
records run-level GPU samples and summaries under `logs/`.

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
- optional `logs/gpu_telemetry.csv`,
  `logs/gpu_telemetry_summary.json`, and
  `logs/gpu_telemetry_summary.md`
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

## Best-BPM 2000-Spill Mining

`BEST_BPM_2000_SPILL_MINING_IMPLEMENTATION_PLAN.md` is implemented as a
separate BPM-only mining layer under `scripts/bpm_mining/` with thin pass
wrappers. It is designed for the same two Tier A Spark position-only
collections and does not assume constant tune, chronological tune trend, or an
external truth label.

Run the full pipeline on Spark:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --resume
```

Individual passes are also available:

```bash
python3 scripts/build_best_bpm_manifest.py --config config/best_bpm_mining.yaml --out best_bpm_mining/manifest
python3 scripts/build_bpm_spectral_cache.py --config config/best_bpm_mining.yaml --manifest best_bpm_mining/manifest/spills.csv --out best_bpm_mining/cache --device cuda --workers 4 --resume
python3 scripts/extract_per_bpm_features.py --config config/best_bpm_mining.yaml --cache best_bpm_mining/cache --manifest best_bpm_mining/manifest --out best_bpm_mining/per_bpm --workers 12
python3 scripts/build_spill_tune_consensus.py --config config/best_bpm_mining.yaml --features best_bpm_mining/per_bpm --cache best_bpm_mining/cache --out best_bpm_mining/consensus --workers 12
python3 scripts/search_best_bpm_subsets.py --config config/best_bpm_mining.yaml --cache best_bpm_mining/cache --manifest best_bpm_mining/manifest --features best_bpm_mining/per_bpm --consensus best_bpm_mining/consensus --subset-sizes 1 3 5 --out best_bpm_mining/subset_search --workers 12 --resume
python3 scripts/evaluate_best_subset_evolution.py --config config/best_bpm_mining.yaml --subsets best_bpm_mining/subset_search --cache best_bpm_mining/cache --features best_bpm_mining/per_bpm --manifest best_bpm_mining/manifest --out best_bpm_mining/evolution
python3 scripts/aggregate_best_bpm_statistics.py --config config/best_bpm_mining.yaml --inputs best_bpm_mining --out best_bpm_mining/statistics
python3 scripts/cluster_spill_morphologies.py --config config/best_bpm_mining.yaml --inputs best_bpm_mining --out best_bpm_mining/clustering
python3 scripts/select_best_bpm_artifacts.py --config config/best_bpm_mining.yaml --inputs best_bpm_mining --out best_bpm_mining/artifact_selection
python3 scripts/make_best_bpm_artifacts.py --config config/best_bpm_mining.yaml --inputs best_bpm_mining --manifest best_bpm_mining/artifact_selection/artifact_manifest.csv --out best_bpm_mining/artifacts
python3 scripts/make_best_bpm_report.py --config config/best_bpm_mining.yaml --inputs best_bpm_mining --out best_bpm_mining/reports
python3 scripts/verify_best_bpm_outputs.py --root best_bpm_mining
```

The scope statement in the final report is intentional:

- best-1 and best-3 are globally exhaustive over valid BPMs in each
  spill/plane.
- best-5 is an exact search within a data-driven screened pool and runs an
  independent beam/random full-space audit that reports whether the pool was
  expanded.
- the historical screened Best-10 path is not used to choose the publication
  ensemble size; contiguous N is evaluated separately with fit/test purging and
  digitizer-disjoint validation.
- per-spill consensus is an internal unsupervised reference, not ground truth.
- expected H near `0.65` and V near `0.72` are soft priors only.
- finalist re-evaluation compares mean power, median power, trimmed mean, and
  static-quality-weighted mean on cached rolling spectra before report
  generation.

Required outputs are grouped under `best_bpm_mining/manifest`, `cache`,
`per_bpm`, `consensus`, `subset_search`, `evolution`, `statistics`,
`clustering`, `artifact_selection`, `artifacts`, `logs`, and `reports`.
Long subset-search runs also write `subset_search/progress/shard_*.json` and
`subset_search/progress/parent_status.json` so a monitor can distinguish
normal compute from a stall before final merged CSVs appear. The verifier
writes `logs/best_bpm_verification.json` and
`logs/best_bpm_verification_report.md` and exits nonzero when required outputs
are missing or structurally invalid.
`statistics/paired_method_tests.csv` includes paired bootstrap confidence
intervals, sign-flip permutation p-values, Benjamini-Hochberg q-values, and
matched-pairs rank-biserial effect sizes for subset-size comparisons.
Artifact selection is capped by plane and includes clean consensus, best-subset
improvement, fixed/dynamic agreement and disagreement, multimodal, low-signal,
and cluster-representative spill-plane examples.
The curated poster shortlist reserves scored H and V examples before
category-diverse fill; a stronger V score cannot consume the entire review cap.

For publication review, the canonical plot set is broader than the final poster
shortlist. It includes Best-N blind agreement and selected/held-out contrast
with block intervals, beam/fit/fold sensitivity, cross-collection transfer, the
same-protocol all-training mean/median control,
exact-point-paired full-buffer legacy comparisons, all meaningful requested-N
difference maps, H-loss diagnostics, and the block-aware intensity gallery.

The favorite archived `18d321dbd4fe` H/V images are tracked-tune density
plots, not spectral-power heatmaps. For each accepted spill and each 4096-turn
Hann window stepped by 256 turns, the legacy tool selected one continuity-
tracked `selected_tune` after a 4096-turn injection seed. It used
`best_single_bpm`, per-BPM RMS normalization, mean subtraction, DC-bin zeroing,
greedy tracking, a 0.005 half-width/maximum step, confidence threshold 2.0,
160 tune bins, and the configured H/V bands. Color is spill count across 1988
usable spills; white curves are across-spill median/percentile tracks. The
adaptive comparison must hold this geometry and visual grammar fixed while
changing only the declared fit-prefix Best-N aggregation.
Exact local hashes, Spark source paths, the legacy selector audit, and the
three-stage comparison contract are preserved in
`publication/ibic2026/LEGACY_RIDGE_PROVENANCE.md`.
The primary persistence candidate is the per-N four-panel H/V comparison:
legacy/adaptive columns, exact paired points, column-normalized pick
probability, one shared P98-clipped color scale, and white P10/median/P90
tracks. Keep its quantitative caption and the separate sample-fraction and
subtractive diagnostics in the review package; the composite alone cannot
distinguish true persistence from missing observations or establish physical
noise removal.
Because the historical normalized-single selector was defective, also review
the exact-paired corrected Best-1-versus-selected panel and the three-column
legacy/corrected-Best-1/selected-Best-N control. Only the corrected
Best-1-to-selected transition isolates the effect of ensemble size; the
legacy-to-Best-1 transition is selector repair.
The turn-resolved legacy contrast must retain its unsmoothed exact-paired CSV,
but it is historical context rather than the ensemble-size estimator. Review
its adaptive-minus-legacy IQR, P10-P90, peak-bin, entropy, and shared-mass
changes alongside the separate all-pairs adaptive tables. The latter include a
zero Best-1 self-control and provide the clean selected-Best-N-minus-corrected-
Best-1 contrast. Five-window-smoothed PNGs can locate intervals of changed
cross-spill pick concentration, but neither their sign nor a visual change
point establishes beam noise, absolute tune accuracy, or extraction onset.
Use the clean selected-H/V P10-P90 contrast, stacked with one shared y scale,
for publication. Keep its landscape form in the paper and its portrait twin in
the poster because two half-column native PNGs would make labels unreadable.
Use that all-spill contrast in the poster's upper-right evidence frame instead
of an anecdotal selected-spill panel. Keep every selected-spill example in the
separate exhaustive review gallery.
Use the generated 800x1250 portrait twin for that frame; contain-fitting the
landscape paper image would waste most of the inherited portrait area and make
its axes too small.
If leakage-controlled model selection chooses different H and V ensemble
sizes, use `--selected-h-n` and `--selected-v-n` to add a plane-selected H/V
composite and one clean concentration panel per selected plane. This is not a
cross-plane comparison: the common color scale supports method contrast, while
the different H/V tune-band widths still forbid comparing apparent thickness
between rows.
Every density raster maps tune bins proportionally onto the complete declared
axis. Integer-truncated cell heights are forbidden because they displace the
color field from the percentile tracks even when both come from the same rows.
The Best-N plot set is eligible only after strict verification of the declared
cache-row counts, contiguous N/fold coverage, exact memberships, purged timing,
finite metrics, detail/summary agreement, cross-collection products, native
plots, and the three-larger-N recommendation boundary. The beam/fit/fold matrix
uses seven unique sample runs with one shared baseline; it does not replace the
all-row primary curve.
The two poster-facing Best-N panels show only blind full-band selected-versus-
held-out agreement, with moving-block intervals and one shared zero-based H/V
scale. This preserves direct plane comparison and keeps the conditioned
near-training-tune curve in a separate review image rather than presenting it
as equivalent validation.
The review gallery also carries a pass/fail matrix for blind agreement, blind
tune difference, selected prominence/power, held-out prominence/power, and the
combined all-gates result at every N. It explains why the earliest eligible N
is selected even when a later point has a slightly higher agreement rate.
Keep the separate gate-margin matrix in the exhaustive review gallery. It
varies the blind-agreement and selected/held-out power non-inferiority margins
around the declared cell while retaining the tune-difference and prominence
rules. It is post-selection robustness evidence only: use it to distinguish a
stable low-to-mid-N region from an H-plane tradeoff, never to retune the
published selector after seeing the result.
Every matrix run must verify, but a reduced sample may legitimately have no
automatic knee when selected-power and prominence margins do not intersect.
Publication requires eligible knees from at least four of seven runs in each
plane. It preserves every unavailable run and reason and prints the available
count and N range; no unresolved case is silently assigned an N.
The same-metric direct-control gallery must also include all-BPM mean and median
beside adaptive and frozen N=1/3/5. All-BPM median currently scores higher in
both planes, and all-BPM mean does so vertically, under the reused-window
evolution metric. Keep that descriptive panel, then review the independent
all-training control under the exact Best-N purge and held-out folds. Its two H/V
scoreboards, eight raw-unit paired scatters, and eight favorable-delta CDFs must
remain in the exhaustive gallery whether selected Best-N wins, loses, or is
unresolved. Only this second control can support a same-protocol comparison.
`scripts/prepare_ibic2026_publication.py` is the final provenance gate. It
requires accepted primary/follow-up, Best-N 10/20/40-block, all-training,
intensity, and
ridge reports plus the exact 2200-manifest raw-payload audit; checks
cross-collection transfer and the seven-run matrix; and
copies the exact figures while writing the poster JSON, paper table, generated
all-training outcome macros, results
payload, and source manifest. The paper copy additionally binds the selected-H
and selected-V exact-paired P10-P90 width-contrast plots so the time-resolved
method comparison cannot drift from the ridge contract.
The source manifest includes the exact numerical source-table hashes and all 14
materialized outputs. Finalization must parse that manifest and re-hash every
declared output rather than treating the CSV's presence as provenance proof.
Poster and manuscript copy must distinguish the 4000 H/V spill-plane cases in
the full N curve from the evenly stratified 1000-case, five-fold held-out
validation sample. Those counts come from the accepted verifier and generated
macros, not manually maintained prose.
Every subtractive ridge caption must say that color represents probability-mass
redistribution, not measured physical noise. The primary density figures do not
show a fixed extraction onset; a broad 10000--20000-turn context band may appear
only in separately named exploratory variants.
Standalone and paired density rasters use a nonzero P98 display clip;
subtractive rasters use a symmetric absolute-P99 display clip. These clips must
be disclosed in captions and must never alter the exported quantitative rows.
The gallery must also pass strict spill/window coverage, selected-cardinality,
tune-band, exact legacy-pair, contrast-metric, warning, PNG, and caption checks
before a ridge panel enters the poster shortlist.
The intensity gallery is held to the same closure standard: the audited capture
counts, first-50000-turn integrity, complete method grids, exact Best-1
zero-effect control, all statistical/practical/tune-shift gates, and every PNG
with its claim guardrail must pass before an intensity panel is considered.
Its subtraction panels additionally require identical exact
collection/spill/plane/N/window/center keys and common finite in-band ridge
picks. Red and blue mean higher and lower column-normalized ridge-pick
probability versus unweighted aggregation, not physical noise added or removed;
the symmetric absolute-P99 clip changes display color only.
An exact-zero subtraction, including every valid Best-1 weighting control, is
annotated as no ridge-pick probability redistribution rather than presented as
an unexplained blank panel.
All intensity heatmaps use proportional inclusive raster bounds so color fills
the complete axis and remains registered to overlaid tracks. Standalone ridge
and binned relationship captions disclose their nonzero-P98 display clips.
Concentration is rendered twice: a common 0-1 scale for cross-panel context and
a zero-based scale at 110% of that panel's maximum for inspecting small
within-panel method separation. Only the common-scale version supports visual
amplitude comparison across N or plane.
Crossing-turn scatters likewise retain a common 0-50000-turn x/y view for
cross-panel context and a separately labeled observed-range detail view. They
omit absent crossings and remain association diagnostics, not extraction-onset
or causal measurements.
Lag correlations retain both the common -1 to 1 Spearman scale and a symmetric
panel-detail scale. The detail variant exposes small lag-shape changes but does
not change the overlapping-window, exploratory, noncausal interpretation.
The independent raw-payload audit also covers both position-only collections;
passing intensity-pair checks cannot waive a position source failure.
Held-out support captions and tables must state their evaluable numerator and
denominator. A finalist row without finite `q_hat` is retained as an explicitly
flagged unavailable observation; it contributes neither a zero candidate
fraction nor a finite support value.

Key poster candidates use the repository's deterministic native PNG renderer,
including BPM/tune deconstruction, selected-spectrum overlays, visibility
evolution, handoff state, and ridge-density panels. This removes a Matplotlib
runtime dependency without weakening the plot contract. Semantic verification
rejects reused placeholder metrics, blank scientific panels, invalid handoff
labels, incomplete Top-1/3/5/10 state coverage, missing global membership maps,
missing selected-spill composites, or a poster shortlist that omits either
plane. Handoff composites encode score, strict rank, and consensus tune
separately; none is an extraction-time marker.

The final A0 poster must be built from the supplied Fermilab vertical template,
preserve its master/header/footer, remain editable, and pass rendered visual QA.
The named full-size PNG is the 150 dpi PDF raster and must remain byte-identical
to it; the direct artifact-tool PNG is retained only as a geometry diagnostic
because it does not render master-level footer media.
Final closure also parses every exported slide XML member read-only and rejects
an empty title, body, or other structural placeholder while allowing
intentionally empty ordinary shapes.
The final build tree must retain the artifact-tool layout inventory,
`slides_test` inspection, font report, and zero-issue template-fidelity JSON and
text reports. Their package-relative checksum inventory is recomputed during
finalization together with every content, asset, output, and PNG-dimension entry
in the poster source manifest.
The poster should use four to six final evidence panels even though the complete
indexed review gallery is intentionally much larger.

## Validation

Run the built-in smoke test:

```bash
python3 scripts/bpm_dgx_poster.py --self-test
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/best_bpm_mining
python3 scripts/gpu_run_telemetry.py summarize --input /path/to/gpu_telemetry.csv --summary-json /tmp/gpu_telemetry_summary.json
```

The smoke test uses synthetic ranked artifacts and verifies that the full
standalone pipeline writes manifest, baseline, flash, waterfall, benchmark, and
poster-index outputs.
