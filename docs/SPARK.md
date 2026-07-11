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

The local `spark` SSH alias reaches `spark.fnal.gov` through the configured
`outland.fnal.gov` jump host. From Spark, delegated Kerberos access to the raw
capture host is available with `ssh -K drbpm1`; use that route for authoritative
source-data checks or staged copies. Spark does not have unrestricted package
internet access. Dependency downloads must use the established outland proxy or
be staged on outland and copied to Spark; do not point `pip` directly at the
public index and assume a timeout is a missing package.

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

`run_autosweep.py` is serial by default. Two jobs are the current maximum on
Spark's single unified-memory GB10 and remain gated on the guarded parallel
smoke. Do not infer that 3-4 jobs are safe from GPU utilization alone; higher
concurrency requires a separate memory-watchdog qualification.

## Best-BPM Mining

Use Best-BPM mining when the question is which BPM subsets carry the strongest
within-spill tune evidence, not which analyzer configuration is best.

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --gpu-telemetry-interval-seconds 30
```

`--resume` reuses spectral-cache arrays but does not checkpoint or skip later
pipeline stages. Never call the full pipeline to continue after an externally
completed subset search. Repair any legacy duration rows, then invoke the
evolution, statistics, clustering, artifact-selection, artifact, and report
wrappers explicitly. This avoids repeating the multi-hour search.

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
- `subset_search/`: best-1, best-3, screened best-5, optional legacy best-10,
  audits, and progress.
- `evolution/`, `statistics/`, `clustering/`: downstream ranking and stability
  products.
- `artifact_selection/`, `artifacts/`, `reports/`: review plots and summaries.
- `logs/`: verification reports and optional GPU telemetry.

Best-1 and best-3 are globally exhaustive over valid BPMs. Best-5 is exact
inside a screened pool with independent audit metadata. The publication run
does not use the older screened Best-10 phase; contiguous ensemble-size choice
is handled by the leakage-controlled Best-N sidecar below.

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

### Best-BPM Follow-Up Sidecar Passes

Run follow-up validation and poster-review passes against a completed Best-BPM
run without overwriting the canonical output tree:

```bash
ROOT=/home/derekste/best_bpm_mining_20260627_best135_from_v2
OUT="$ROOT/followups/next_steps_20260628"
BESTN=/home/derekste/best_n_20260709
PY=/home/derekste/venvs/cupy-spark-cu13/bin/python

"$PY" scripts/evaluate_fixed_bpm_sets.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --out "$OUT" \
  --workers 4 \
  --subset-sizes 1 3 5

"$PY" scripts/evaluate_heldout_spectral_support.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --out "$OUT" \
  --workers 4 \
  --tune-half-width 0.0025

"$PY" scripts/make_best_bpm_poster_artifacts.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --manifest "$ROOT/artifact_selection/artifact_manifest.csv" \
  --out "$OUT/artifacts" \
  --workers 4

"$PY" scripts/run_bpm_handoff_analysis.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --out "$OUT" \
  --workers 4

"$PY" scripts/verify_best_bpm_outputs.py \
  --root "$OUT" \
  --followups-only

"$PY" scripts/analyze_next_steps_outputs.py \
  --root "$ROOT" \
  --followup "$OUT" \
  --best-n "$BESTN/merged" \
  --all-training "$BESTN/all_training" \
  --ridge "$OUT/ridge_density_best_ensemble" \
  --intensity /home/derekste/tbt-intensity-study-20260709/merged \
  --sensitivity "$BESTN/sensitivity" \
  --out "$OUT/analysis/next_steps_output_analysis.md"
```

The optional Best-N, all-training, ridge, intensity, and sensitivity-matrix arguments make
the final report data-derived. A supplied sensitivity root must contain the
seven unique verified beam/fit/seed runs and nested comparison outputs. Omit
only an input that was intentionally not run, in which case the report records
the missing evidence rather than substituting a stale conclusion.
When an analysis root is supplied, the report generator requires its accepted
JSON verifier result and refuses to summarize a failed, missing, or provisional
tree.

Smoke-test the sidecar commands first with `--limit` before running the full
follow-up stack. The fixed-set and held-out passes write shard progress under
their output directories, while `logs/progress.csv` records top-level command
status when launched through the wrappers.

The corrected fixed-set pass recomputes dynamic, frozen, and all-BPM controls
from the same cache with the same evolution score. Treat it as a descriptive
control because the original dynamic memberships reuse their selection
windows; later-window digitizer-disjoint Best-N validation is the publication
inference.
The control summary and executive report must include all-BPM mean/median. The
all-BPM median currently exceeds the small-set scores in both planes, and mean
does so vertically, so the publication claim is Best-N versus Best-1 and frozen
small sets until the same-protocol all-training control below is complete.
No-visible fixed/control rows and no-`q_hat` held-out rows are retained with
explicit quality flags and blank unavailable metrics. The held-out summary
reports evaluable coverage; the semantic verifier accepts those states only
when identity/cardinality remain exact and rejects any missing metric for a
finite `q_hat`.

The artifact pass keeps compatibility outputs under `artifacts/global/` and
`artifacts/spills/`, and writes the curated poster-review set under
`artifacts/poster/` with `selected_poster_artifacts.csv`,
`poster_artifact_index.md`, `poster_contact_sheet.png`,
`global_topn_performance_hv.png`, and `global_bpm_inclusion_{h,v}.png`.
`scripts/make_best_bpm_artifacts.py` remains an equivalent legacy wrapper for
the poster-artifact command.

### Leakage-Controlled Best-N Pass

Run this only after the corrected exact-identity cache is verifier-clean. Use
one output directory per shard and merge after every shard completes:

```bash
BESTN=/home/derekste/best_n_20260709
"$PY" scripts/evaluate_best_n_curve.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --out "$BESTN/shards/shard_0" \
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

"$PY" scripts/merge_best_n_shards.py \
  --shards "$BESTN/shards" \
  --out "$BESTN/merged" \
  --bootstrap-samples 1000 \
  --bootstrap-block-spills 20

"$PY" scripts/verify_best_n_outputs.py \
  --root "$BESTN/merged" \
  --max-n 40 \
  --curve-cache-rows 4000 \
  --validation-cache-rows 1000 \
  --folds 5
```

Repeat the evaluator for shard indices 1 through 3 before merging. Four
logical shards are required for deterministic coverage, while execution
concurrency is an independent operational control. A measured four-process,
max-N=40 launch rose from about 105 GiB to more than 115 GiB of unified memory
and made the host unresponsive; four-way CUDA concurrency is prohibited. A
subsequent two-process run was qualified under the same max-N=40 contract: GPU
utilization was mostly 70-96%, host use peaked near 83 GB (77 GiB), and
approximately 44 GiB remained available. Permit at most two evaluators, keep them and their
coordinator in one process group, and terminate the group after three
consecutive five-second samples below 32 GiB `MemAvailable`. Preserve per-shard
resume files, and do not overlap the full Best-N run with sensitivity,
intensity, or ridge jobs. Positive curve/validation limits are
evenly stratified across collection and plane. Record any limit in the report
and use the full curve for the final global-N recommendation.
Every shard must contain `run_contract.json`. A resume with changed scientific
parameters or source hashes is rejected. Merge requires the complete declared
shard index set with compatible contracts and no duplicate science keys; do
not remove the contracts to force reuse of an old output directory. A resumed
spill-plane is complete only when every N and every declared fold is present
exactly once. Sensitivity comparisons likewise require identical full key sets.

Required sensitivity dimensions are beam width, fit-window count, and
digitizer-fold seed. Also remerge the same completed shards with 10, 20, and 40
spill bootstrap blocks; this block-length check repeats only summary inference,
not GPU selection. Blocks are non-circular and never join collection endpoints.
Configured sign-flip sample counts are executed without an undocumented cap.
Merge each variant separately, then compare completed runs with
`scripts/compare_best_n_beam_widths.py` or
`scripts/compare_best_n_sensitivity.py`. The bounded N=30 benchmark put the V
recommendation on the upper boundary, so the definitive run extends through
N=40. Extend again only if the blind agreement and selected/held-out contrast
curves remain boundary-limited;
five-fold validation cannot select more channels than remain in a training
fold.

The accepted summary can regenerate publication and diagnostic plots without
waveform access. `best_n_validation_h/v.png` contains blind full-band agreement
only on a shared H/V scale; `best_n_conditioned_agreement_h/v.png` retains the
near-training diagnostic separately, and `best_n_decision_gates_h/v.png`
records the exact criterion-by-N pass/fail decision.

The reproducible sample sensitivity matrix is:

```bash
"$PY" scripts/run_best_n_sensitivity_matrix.py \
  --inputs "$ROOT" \
  --out "$BESTN/sensitivity" \
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

It runs seven unique configurations because the beam-32/fit-8/seed-20260709
baseline is shared. Each run is verified before comparison. Keep this sample
matrix separate from the all-4000-row primary curve; it tests numerical and
hyperparameter stability rather than replacing full-run inference.
The matrix uses descriptive internal labels such as `beam16`, but the standalone
beam-width comparer accepts numeric `WIDTH=/path` keys. The runner converts those
three labels to `16/32/64` at that subprocess boundary. A post-evaluator failure
in comparison or gallery generation must be resumed with the same command and
contracts. In `--resume` mode the coordinator strictly verifies each existing
run before scheduling and does not spawn an evaluator for a passing directory;
do not delete or recompute already verified run directories.
An otherwise valid sample run may report no knee when its selected-power and
prominence margins do not intersect. Do not relabel that as a failed run or
choose N manually. Publication requires all seven verified outputs and eligible
knees from at least four runs per plane; unavailable reasons remain in the
final payload and report.
Serial execution remains the default. `--parallel-runs 2` is the maximum
qualified Spark setting and requires readable Linux `MemAvailable`; a sustained
floor breach terminates both evaluators and leaves their ten-case checkpoints
resumable. A trip writes `memory_guard_abort.json`; the active controls are
written to `execution_controls.json` and do not alter any per-run scientific
contract.
The generated sensitivity gallery includes confidence-interval endpoints, so
10/20/40-spill block comparisons expose uncertainty changes even when central
curves are identical.

### Leakage-Controlled All-Training Control

Run this CPU/cache-only control after the Best-N block-20 verifier has selected
both planes and after the GPU publication chain is idle:

```bash
ALL_TRAINING=/home/derekste/best_n_20260709_all_training
"$PY" scripts/evaluate_best_n_all_training.py \
  --config config/best_bpm_mining.yaml \
  --inputs "$ROOT" \
  --best-n-root "$BESTN/merged_block20" \
  --out "$ALL_TRAINING"

"$PY" scripts/verify_best_n_all_training.py --root "$ALL_TRAINING"
```

This is not a literal all-60 result because each fold keeps complete digitizers
held out. It is the leakage-controlled counterpart: every available
training-side channel is aggregated by mean and median, while fit/test purge,
fold identity, later windows, and held-out reference exactly match the accepted
selected Best-N rows. The run contract hashes the accepted Best-N contract,
validation, summary, verifier, BPM index, and spectral-cache index.

The definitive 1000-spill-plane validation population produces 10,000 detail
rows, 8,000 exact fold-collapsed spill pairs, 16 paired method/metric intervals,
and 18 native PNGs. The verifier requires those exact counts, source hashes,
fold/timing/cardinality coverage, finite intervals, and complete PNG geometry.
Do not require Best-N to win: a baseline-favored or unresolved interval is a
scientific result. Require every result to remain visible in the publication
payload, manuscript macros, and review gallery.

### Intensity Sidecar

The 200-spill intensity study is independent of the position-only primary run.
Use `scripts/analyze_intensity_assisted_tune.py` in shards, merge with
`scripts/merge_intensity_study.py --bootstrap-block-spills 20`, and generate
the broad review gallery with `scripts/make_intensity_study_plots.py`.
Every waveform shard must receive the same explicit comma-separated canonical
grid `--subset-sizes 1,3,5,7,10,12,15,20`. Append each distinct accepted H/V
Best-N recommendation when it falls outside that grid, and use the identical
union for the block re-summaries, gallery, and verifier. The verifier takes
space-separated N values; its expected spill rows are
`200 x 2 planes x 4 methods x number of distinct N values`.
`scripts/resummarize_intensity_study.py` can apply corrected block-aware
statistics to an existing merged run without repeating GPU waveform work.
Run it at 10, 20, and 40-spill block lengths and require the retain/reject
decision to agree across all three. The comparator checks exact effect
identities, not only retained counts, and fails any nonzero Best-1 effect.
Only the first 50000 turns are eligible for inference; structural corruption in
the advertised tail is a payload-integrity result, not a beam-loss time.
Advertised and on-disk position/intensity sample counts must also agree for
every exact pair.
After the corrected block-20 merge and gallery render, require:

```bash
"$PY" scripts/verify_intensity_outputs.py \
  --root "$INTENSITY/merged_block20" \
  --gallery "$INTENSITY/gallery" \
  --subset-sizes 1 3 5 7 10 12 15 20 \
  --expected-paired-payload-rows 23999 \
  --expected-spill-rows 12800 \
  --expected-centers 90 \
  --minimum-spills-per-group 199 \
  --expected-block-spills 20
```

This also proves the corrected all-zero gate fallback by requiring every Best-1
window to be numerically invariant across all four weighting methods.
No-usable-intensity windows explicitly use unweighted aggregation for every
method; a finite-but-below-threshold gate keeps the strongest finite member.
The window/spill CSVs and merged summary expose fallback reasons and frequency;
there is no silent intensity fallback.
The verifier also binds all four methods to identical exact spill keys,
selected memberships, and contracted 4096/512 center grids with finite global
ridge picks. Intensity subtraction then joins exact
collection/spill/plane/N/window/center keys, retains common finite in-band
points, and labels red/blue only as higher/lower column-normalized ridge-pick
probability. Its symmetric absolute-P99 clip is display-only, not denoising.
All intensity rasters use proportional inclusive cell bounds; count-density
captions and legends disclose their nonzero-P98 display clip.
The gallery emits both common 0-1 and zero-based panel-detail concentration
plots; apparent amplitude in the autoscaled detail plots is not comparable
across N or plane.
Crossing-turn plots retain common 0-50000-turn axes plus an observed-range
detail view; absent crossings are omitted and neither view defines extraction
timing or causation.
Lag correlations retain common -1-to-1 and symmetric panel-detail views; the
detail limits vary and do not make overlapping windows independent or causal.

Before interpreting either the intensity or 50000-turn ridge products, scan
the complete publication corpus independently of the FFT paths:

```bash
"$PY" scripts/audit_delivery_ring_payloads.py \
  --capture-root /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
  --capture-root /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --capture-root "$INTENSITY_CAPTURE" \
  --out "$OUT/delivery_ring_payload_audit" \
  --analysis-turns 50000 \
  --plateau-turns 128
```

The audit must cover 2200 manifests, 263999 raw position rows, and 23999 exact
raw position/intensity pairs. It fails on first-50000-turn nonfinite samples,
advertised/on-disk count differences, exact plateaus of 128 turns or more, and
repeated device-coded threshold fallback pairs. The one known incomplete
120-channel intensity manifest remains counted and visible; it is not silently
dropped. See `docs/DELIVERY_RING_SOURCE_AUDIT.md` for the upstream live audit.

To reproduce the old `18d321db` ridge-density visual grammar with corrected
Best-N memberships, run the full-buffer ridge-density sidecar:

```bash
"$PY" scripts/make_best_bpm_ridge_density.py \
  --best-root "$ROOT" \
  --membership-csv "$BESTN/merged_block20/best_n_curve_rows.csv" \
  --legacy-sliding-csv /path/to/18d321db/gpu_sliding_tune.csv \
  --input \
    /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
    /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out "$OUT/ridge_density_best_ensemble" \
  --device cuda \
  --turn-start 0 \
  --turn-end 50000 \
  --window-turns 4096 \
  --stride-turns 256 \
  --bpm-normalization rms_per_bpm \
  --detrend mean_subtract \
  --dc-handling zero_dc_bin \
  --injection-window-turns 4096 \
  --min-peak-confidence 2.0 \
  --track-half-width 0.005 \
  --max-tune-step-per-window 0.005 \
  --subset-sizes 1 3 5 10 15 20 30 40 \
  --selected-h-n "$H_N" \
  --selected-v-n "$V_N" \
  --comparison-bootstrap-samples 1000 \
  --extraction-context-variants \
  --progress 100

"$PY" scripts/verify_ridge_density_outputs.py \
  --root "$OUT/ridge_density_best_ensemble" \
  --subset-sizes 1 3 5 10 15 20 30 40 \
  --minimum-spills 1900 \
  --expected-centers 180
```

This writes H/V ridge-density heatmaps, all pairwise requested-N difference
maps, exact-point-paired legacy comparisons, a shared-scale four-panel H/V
legacy-versus-adaptive comparison for every requested N, turn-concentration and
H-loss diagnostics, exact-paired per-turn width/entropy/peak/shared-mass
legacy contrasts, every adaptive N-pair metric and turn grid, an exact-zero
Best-1 self-control, moving-turn-block contrast intervals, captions, and an
indexed gallery. The per-turn CSVs are unsmoothed; their PNGs use five-window
visual smoothing and a zero reference. They diagnose ridge-pick probability
redistribution rather than physical noise or extraction timing. Selected-H/V
composites stack the two planes on one shared y scale; the P10-P90-width
landscape composite is copied into the paper source and its portrait twin into
the poster source. Primary density figures omit the
broad extraction-review marker;
separately named context variants may show it. The method applies members chosen
from early fit windows through the 0-50000-turn buffer. It tests persistence and
does not perform same-window dynamic reselection.
P98 standalone/pair color clipping and absolute-P99 subtractive clipping affect
only raster contrast; exported counts, probabilities, and metrics are not
clipped. Visible difference legends use higher/lower pick probability wording.
The plane-selected outputs also include a corrected Best-1-versus-selected H/V
comparison, five clean selected-minus-Best-1 H/V turn contrasts in landscape
and portrait form, and a legacy/corrected-Best-1/selected-Best-N three-column
control. The clean P10-P90 pair is copied into the paper/poster; the legacy
contrast remains a visual anchor that includes selector repair.
Set `H_N` and `V_N` from the accepted block-20 Best-N recommendations. The
additional mixed H/V composite uses the selected membership for each plane,
and selected-N concentration panels avoid presenting every requested N in the
final H-loss frame. Both values are stored in `run_contract.json` and required
by the verifier.
The legacy table contains one tracked tune pick per spill/window, not spectral
power. The explicit settings above match archived job `18d321dbd4fe`; the
verifier rejects protocol drift and requires all 2000 adaptive and all 1988
legacy spill-planes on the exact 180-center grid, every per-N combined H/V
comparison, every other manifest PNG/caption, and machine-readable disposition
of generation warnings.
The native renderer proportionally maps every tune bin over the complete plot
height; this keeps standalone and subtractive heatmaps aligned with their tune
axis and percentile overlays when the pixel height is not divisible by the bin
count.

After the raw-payload, all-training, intensity, and ridge verifiers pass, run
`scripts/prepare_ibic2026_publication.py` with the corrected primary/follow-up,
Best-N parent, all-training, ridge, intensity parent, and payload-audit roots.
Run it on Spark before
copy-back when the large ridge CSVs remain in place; only the generated
publication tree and review galleries need transfer to the local checkout.
The generated paper tree includes `results_table.tex`, `results_macros.tex`,
the selected-H/V turn-width contrast, and the other four contract-bound PNGs;
the final paper build rejects a missing macro or figure file. The generated
`source_manifest.csv` includes exact numerical source hashes and the complete
14-output materialization inventory; finalization re-hashes that inventory
after copy-back.

If an original artifact must be revisited, Spark can reach the acquisition host
with forwarded credentials via `ssh -K drbpm1`; copy or package the smallest
complete source artifact rather than rerunning acquisition.

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
