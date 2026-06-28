# Next Steps For Best-BPM Mining And IBIC Poster

Last updated: 2026-06-27.

This file is a handoff for the next Codex/DevSpace pass. It consolidates the current repo review, Spark run status, physics assumptions, validation gaps, and the most important next analysis questions for the Delivery Ring BPM tune-tracking work.

## Current Context

The project is now organized around a clear instrumentation workflow:

```text
Redis BPM streams
→ synchronized raw spill capture
→ offline Rust analysis
→ Spark/GPU autosweep
→ Best-BPM mining
→ selected reports/artifacts
→ physics review and poster figures
```

The immediate active data product is the focused Spark Best-BPM mining run over the 2000-spill Tier A position-only dataset. This is not a generic autosweep anymore. It is a targeted search for tune-sensitive BPMs and small BPM subsets.

Current run root:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

The run intentionally searches only subset sizes:

```text
1, 3, 5
```

Best-10 is deferred because the earlier broader run was too slow.

The restart reuses expensive outputs from the earlier run:

```text
/home/derekste/best_bpm_mining_20260624_full_v2/cache
/home/derekste/best_bpm_mining_20260624_full_v2/manifest
/home/derekste/best_bpm_mining_20260624_full_v2/per_bpm
/home/derekste/best_bpm_mining_20260624_full_v2/consensus
```

Do not delete, overwrite, or broadly mutate Spark run outputs without explicit user approval. Follow-up passes should write sidecar outputs first unless the user asks to update the canonical run tree.

## Current Spark Completion Check

A bounded read-only Spark check on 2026-06-27 21:04 CDT showed that the focused run completed and passed verification.

Run root:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

Progress summary:

```text
subset_search: ok, about 33908 s
shard progress: 4000 / 4000 rows complete
evolution: ok, about 727 s
statistics: ok
clustering: ok
artifact_selection: ok
artifacts: ok
report: ok
verify: ok
```

Verification summary:

```text
status: ok
failures: 0
warnings: 0
subset sizes: 1, 3, 5
spills inventoried: 2000
usable spills: 2000
BPM index rows: 120
subset rows: best1=4000, best3=4000, best5=4000
finalist reevaluation rows: 799988
selected poster-review spill-plane artifacts: 79
run root size: about 1.1 GiB
```

Important output locations:

```text
reports/strong_bpm_executive_summary.md
reports/strong_bpm_analysis_summary.md
logs/best_bpm_verification_report.md
statistics/paired_method_tests.csv
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
statistics/subset_size_pareto.csv
evolution/finalist_reevaluation.csv
artifact_selection/artifact_manifest.csv
artifacts/global/
artifacts/spills/
```

The run is no longer the bottleneck. The next phase is interpretation, physics validation, and poster-grade figure generation.

## Latest Results Interpretation

The completed best1/3/5 run gives a clear direction.

### Subset size result

Paired method tests show strong improvements from adding BPMs:

```text
H best1 → best3: median score improvement ≈ 0.0392, effect size ≈ 0.999
H best3 → best5: median score improvement ≈ 0.0101, effect size ≈ 0.753
V best1 → best3: median score improvement ≈ 0.0542, effect size ≈ 1.000
V best3 → best5: median score improvement ≈ 0.0285, effect size ≈ 0.930
```

Interpretation:

- best3 clearly beats best1 in both planes,
- best5 still improves over best3, especially in V,
- the poster should emphasize **small tune-sensitive BPM ensembles**, not just one magic BPM,
- best10 should remain deferred until best5 results are understood and properly visualized.

### Plane asymmetry

The subset-size Pareto table shows:

```text
H median visible_fraction: 0.0 for best1, best3, best5
V median visible_fraction: 0.0 for best1, 0.3125 for best3, 0.625 for best5
```

Interpretation:

- V is much more ready for a strong physics/poster claim,
- H still has useful ranking structure but weak visibility under the current thresholds,
- do not force H and V into the same conclusion,
- H likely needs better visibility thresholds, stronger held-out spectral validation, or tuned deconstruction review.

### BPM rank stability

Collection-to-collection rank stability is encouraging:

```text
H Spearman ≈ 0.752, Kendall ≈ 0.582
V Spearman ≈ 0.904, Kendall ≈ 0.763
```

Interpretation:

- BPM quality is not random,
- V BPM ranking is especially stable,
- fixed or semi-fixed tune-sensitive BPM sets are plausible,
- direct fixed-set spectral evaluation is now the highest-value follow-up.

### Leading BPMs

The same BPM appears near the top for both planes:

```text
acsys_DeliveryRingBPM 10.200.22.62
```

Top-1 frequency is modest, roughly 6%, which means no single BPM dominates the full dataset. That supports the ensemble story: tune visibility appears distributed across a population of useful BPMs, not controlled by one channel.

### Consensus quality

Within-spill consensus class counts were:

```text
CLEAN_CONSENSUS: 1606
MULTIMODAL: 2101
WEAK_CONSENSUS: 252
NO_CONSENSUS: 41
```

Interpretation:

- there is substantial BPM tune structure, but many spill-plane/window cases are multimodal,
- this supports the need for deconstruction plots, handoff analysis, and selected artifact review,
- avoid any claim that every spill has a single simple tune ridge.

### Artifact caveat

The verifier passed, but the existing generated artifacts are mostly contract/smoke artifacts. Several per-spill artifacts are `.txt` fallbacks rather than poster-quality plots.

Do not treat the current `artifacts/` tree as the final poster figure set.

## Updated Immediate Direction

The project should now move from computation to review-quality interpretation.

Recommended priority order:

1. Package the completed summary/report/artifact tree from Spark for local review.
2. Build a compact human-review table of the top H and V results from the completed run.
3. Implement direct fixed-set evaluation from cached spectra.
4. Generate real poster-grade deconstruction and subset spectrum overlay plots for selected spills.
5. Add the BPM handoff / tune-visibility migration pass using current early-window cache.
6. Only then decide whether best10 is worth a follow-up run.

Do not run another broad search yet.

If checking Spark again, keep probes bounded and read-only and verify the live
host state directly.

## Repo State Summary

Important files/docs already reviewed:

```text
README.md
docs/PHYSICS.md
docs/ANALYSIS_CHECKLIST.md
docs/SPARK.md
docs/ANALYSIS_CHAINS.md
config/best_bpm_mining.yaml
scripts/run_best_bpm_pipeline.py
scripts/bpm_mining/pipeline.py
scripts/bpm_mining/spectra.py
scripts/bpm_mining/peaks.py
scripts/bpm_mining/consensus.py
scripts/bpm_mining/subset_score.py
scripts/bpm_mining/subset_search.py
scripts/bpm_mining/evolution.py
scripts/bpm_mining/statistics.py
scripts/bpm_mining/artifact_selection.py
scripts/bpm_mining/plots.py
scripts/bpm_mining/report.py
scripts/bpm_mining/verification.py
```

The repo is mature enough to support an IBIC poster, but the remaining work is turning mining output into physics-defensible evidence and poster-grade figures.

## Strong Current Assumptions

### 1. BPM turn-by-turn data contains real tune information near injection

This is the strongest current result. The analysis repeatedly finds tune-visible spectral structure near the expected early-injection regions.

Poster-safe claim:

```text
BPM TBT data contains reproducible tune-sensitive spectral evidence near injection.
```

Avoid claiming full-spill tune tracking unless later outputs prove it.

### 2. Expected tune anchors are soft priors, not labels

Current Best-BPM config uses:

```text
H expected tune: 0.65
V expected tune: 0.72
H discovery band: 0.60–0.70
V discovery band: 0.67–0.75
```

However, `docs/PHYSICS.md` still says:

```text
Qx ~ 0.69
Qy ~ 0.71
```

This mismatch should be reconciled before the poster or physics review. Based on the current study context, prefer wording like:

```text
For this data set, observed early-injection tune clusters are near H ≈ 0.65 and V ≈ 0.72.
```

Do not use the anchors as hard truth labels because machine settings changed during data acquisition.

### 3. Machine settings varied during the 2000-spill collection

The data was captured asynchronously while machine parameters were being adjusted. Therefore:

- do not assume chronological tune trends,
- do not assume spill-to-spill continuity,
- do not score methods by closeness to neighboring spills,
- do not average all spills as if they were the same machine state.

Primary scoring should be within-spill:

- BPM consensus,
- held-out BPM support,
- peak quality,
- subset stability,
- visibility classification,
- collection-to-collection BPM rank stability.

### 4. Dynamic best-BPM selection has look-elsewhere bias

The autosweep and current mining logic can identify strong single BPMs, but searching 60 BPMs means one BPM can look good by chance. Dynamic best-BPM results must be defended with:

- held-out BPM support,
- best-1 vs best-3 vs best-5 paired comparisons,
- fixed-set cross-validation,
- per-spill BPM tune deconstruction plots.

### 5. Best-1 and best-3 are globally exhaustive; best-5 is not

The current subset search is:

```text
best1: globally exhaustive over valid BPMs
best3: globally exhaustive over valid BPMs
best5: exact within a screened pool, with audit metadata
```

Do not describe best-5 as globally exhaustive over all BPMs.

## Key Implementation Findings

### Spectral cache

`scripts/bpm_mining/spectra.py` computes cached per-BPM spectra for the configured windows. Current cache configs are early/injection focused:

```text
injection_2048
injection_4096
early_2048_256, 0–10000 turns
early_4096_256, 0–15000 turns
```

This means the current mining run primarily supports early-spill tune evidence. It does not prove full 100 ms tune tracking.

### Per-BPM features

`scripts/bpm_mining/peaks.py` extracts up to three local spectral candidates per BPM/window and records:

- peak tune,
- peak power,
- prominence,
- local background ratio,
- peak width,
- second-peak ratio,
- entropy,
- distance to band edge,
- distance to expected anchor.

This is the right feature basis for Best-BPM mining.

### Within-spill consensus

`scripts/bpm_mining/consensus.py` clusters per-BPM tune candidates into internal consensus labels:

```text
CLEAN_CONSENSUS
WEAK_CONSENSUS
MULTIMODAL
NO_CONSENSUS
```

This is an internal BPM-only pseudo-reference. It must not be described as ground truth.

### Subset scoring

`scripts/bpm_mining/subset_score.py` scores candidate subsets using:

- held-out support,
- peak quality,
- consensus agreement,
- window stability,
- diversity score,
- ambiguity penalty,
- visible fraction.

The score is directionally good. However, the current `holdout_support` uses per-BPM median candidate tunes from `per_bpm_spill_summary.csv`, not a full per-window held-out spectral-power check. Treat it as useful ranking evidence, not final physical validation.

### Evolution pass

`scripts/bpm_mining/evolution.py` has two layers:

1. a summary derived from subset-search rows, and
2. finalist re-evaluation using cached rolling spectra and several aggregators.

The finalist re-evaluation is more trustworthy than the raw `visibility_duration_turns` from subset search. The raw search duration can overstate true visibility because it may represent the span of the cached search windows rather than a robust continuous visible interval.

### Statistics pass

`scripts/bpm_mining/statistics.py` writes many useful tables, but one important limitation was identified:

The current fixed-set cross-fit logic does not truly recompute spectra for a frozen fixed BPM set on the held-out collection. It mostly measures overlap between dynamic winners and fixed member lists. A rigorous fixed-set evaluation should recompute combined spectra for the frozen set on every test spill.

### Artifact generation

`scripts/bpm_mining/plots.py` currently satisfies output contracts, but several plots are placeholder-style rather than poster-grade:

- `bpm_tune_deconstruction` is currently a selected-membership bar chart, not a BPM-vs-tune spectral heatmap.
- `subset_spectra` is currently a score scatter, not an overlaid spectrum comparison.
- `subset_evolution` is currently a point plot, not a visible-window tune evolution plot.
- Some named global plots reuse the same top-k bar chart style.

The tables may be useful, but poster figures still need a follow-up plotting pass.

## Missing Validation Evidence

### 1. Direct fixed-set evaluation

Need a follow-up pass that actually evaluates frozen fixed BPM sets from cached spectra:

```text
rank fixed top-N on collection A
combine exactly those BPMs on every collection B spill
score resulting spectra
reverse A/B
```

This is stronger than dynamic/fixed overlap.

### 2. Stronger held-out spectral support

For finalists, compute held-out support from actual held-out BPM spectra in the same windows, not only per-BPM summary candidate tunes.

Useful finalist metric:

```text
At q_hat, what fraction of non-selected BPMs have above-background spectral power or local candidates within tolerance?
```

### 3. Poster-grade per-spill deconstruction plots

Need real plots showing:

- x-axis: tune,
- y-axis: BPM index or ring order,
- color: row-normalized log spectral power,
- markers: per-BPM peak candidates,
- vertical line: within-spill consensus tune,
- highlighted best1/best3/best5 members.

These will be the most convincing human-review plots.

### 4. Overlaid subset spectra

For selected spills, overlay spectra for:

```text
best1
best3
best5
all-BPM mean
all-BPM median
fixed top-N if available
```

Mark q_hat and within-spill consensus. This should replace the placeholder `subset_spectra` artifact for poster use.

### 5. Visibility duration with visible-window masking

Use finalist re-evaluation or a dedicated pass to show tune only in visible windows. Do not draw continuous tune traces through `NO_RELIABLE_TUNE` regions.

## Next Five Analysis Questions

### 1. Does best-3 or best-5 beat best-1 in paired comparisons?

Inspect after run completion:

```text
statistics/paired_method_tests.csv
statistics/subset_size_pareto.csv
evolution/subset_size_comparison.csv
evolution/finalist_reevaluation.csv
```

Decision:

- If best-3 or best-5 improves clearly, the poster should emphasize small BPM ensembles.
- If best-1 dominates, the poster should emphasize identifying tune-sensitive BPMs and avoiding all-BPM dilution.

### 2. Are the top BPMs stable across the two 1000-spill collections?

Inspect:

```text
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
```

Decision:

- Stable rankings imply fixed operational BPM subsets are plausible.
- Unstable rankings imply adaptive per-spill selection is required.

### 3. Are high-scoring subsets supported by held-out BPMs?

Inspect:

```text
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
```

Plot or summarize:

```text
subset_score vs holdout_support
subset_size vs holdout_support
q_hat - consensus_tune
holdout_support distribution by plane and subset size
```

This is the defense against overfitting noise.

### 4. Which spills and subsets are best for physics review?

Start from:

```text
artifact_selection/artifact_manifest.csv
reports/strong_bpm_analysis_summary.md
```

Then generate real poster-grade plots for only a small number of selected spills:

- clean consensus examples,
- best1 examples,
- best3/best5 improvement examples,
- dynamic-vs-fixed agreement/disagreement examples,
- multimodal/failure examples.

### 5. Can a frozen fixed BPM set be evaluated directly?

This is the highest-value follow-up job after the current run finishes.

Implement a cache-based pass that evaluates fixed top-N sets directly, rather than using dynamic-overlap proxies.

Compare:

```text
dynamic best1/3/5
fixed top1/3/5 trained on collection A and tested on B
fixed top1/3/5 trained on collection B and tested on A
all-BPM mean
all-BPM median
```

This is likely more valuable than immediately running best10.

## IBIC Poster Readiness

Current estimated readiness after repo inspection and focused-run completion:

```text
Platform / repo maturity:        85–90%
Data capture story:              90%
Spark mining implementation:     80–90%
Current best1/3/5 run:           complete and verifier-clean
Physics validation evidence:     65–75%
Poster-quality figures:          40–60%
Poster narrative:                80%
```

Overall:

```text
Current state: 80–85% poster-ready
If true fixed-set evaluation and real deconstruction plots are added: 90%+
```

Recommended poster framing:

```text
Mining Delivery Ring BPM Turn-by-Turn Data for Tune-Sensitive BPM Subsets
```

Recommended main claim:

```text
We captured and mined 2000 full-ring BPM turn-by-turn spills using GPU-assisted spectral analysis to identify BPMs and small BPM subsets that provide reproducible within-spill tune evidence near injection.
```

Recommended secondary claim:

```text
The method quantifies when BPM tune evidence is visible and avoids forcing tune estimates in low-confidence regions.
```

Do not frame this yet as a completed replacement tune monitor.

## First Actions After Focused Run Completion

1. Keep the completed run immutable until review artifacts are copied or sidecar passes are verified:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

2. Inspect these first:

```text
reports/strong_bpm_executive_summary.md
reports/strong_bpm_analysis_summary.md
statistics/paired_method_tests.csv
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
evolution/finalist_reevaluation.csv
artifact_selection/artifact_manifest.csv
logs/best_bpm_verification_report.md
```

3. Treat best-3 and best-5 paired improvements as the starting point for poster interpretation, but do not overstate best-5 search completeness.

4. Run or implement direct fixed-set evaluation from cached spectra.

5. Generate poster-grade figures from selected finalists.

## Implementation, Progress, Parallelism, And Deployment Plan

The follow-up work should be split into small passes that can be run against the completed Spark output without rerunning expensive cache or subset-search stages. Each pass should support serial mode, deterministic parallel mode, resume, bounded selected-spill mode, and sidecar output directories.

### Phase 0: Normalize handoff and docs

Goal:

```text
Make the repo branch, NEXT_STEPS.md, physics docs, and PR branch tell the same story.
```

Work:

- keep this `NEXT_STEPS.md` file on the active Best-BPM branch with Task F included,
- reconcile `docs/PHYSICS.md` so the current 2000-spill H ≈ 0.65 / V ≈ 0.72 anchors are dataset-specific soft priors, while older `Qx ~ 0.69`, `Qy ~ 0.71` language is explicitly historical or operational-context dependent,
- update `docs/SPARK.md` after new commands exist,
- keep local operational handoffs out of permanent design truth unless they are
  scrubbed and intentionally promoted into tracked docs.

Validation:

```bash
git diff --check
```

### Phase 1: Direct fixed-set evaluation

Goal:

```text
Replace dynamic/fixed overlap proxies with actual frozen BPM-set spectral evaluation.
```

Implementation:

- add `scripts/bpm_mining/fixed_sets.py`,
- add wrapper `scripts/evaluate_fixed_bpm_sets.py`,
- optionally add pipeline command `fixed-sets`,
- train fixed top-N sets on collection A and test exact frozen members on collection B, then reverse,
- compare dynamic best1/best3/best5, fixed top1/top3/top5, all-BPM mean, and all-BPM median.

Inputs:

```text
cache/index/spectral_cache.csv
manifest/bpm_index.csv
statistics/bpm_global_statistics.csv
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
```

Outputs:

```text
statistics/fixed_set_direct_evaluation.csv
statistics/fixed_vs_dynamic_direct_summary.csv
artifacts/global/fixed_vs_dynamic_direct_h.png
artifacts/global/fixed_vs_dynamic_direct_v.png
```

Progress:

```text
statistics/fixed_set_progress/parent_status.json
statistics/fixed_set_progress/shard_<n>.json
logs/progress.csv pass=fixed_set_direct_evaluation
```

Parallelism:

- shard by `(train_collection, test_collection, plane, subset_size, method, spill_chunk)`,
- load spectral arrays with `np.load(..., mmap_mode="r")`,
- have workers write chunk CSV fragments or return rows, then merge in stable sorted order,
- prove serial and parallel output equality in tests.

Spark deploy:

```text
deploy code to a new scratch directory on Spark
run sidecar output first, e.g. /home/derekste/best_bpm_mining_20260627_best135_from_v2/fixed_set_eval_probe
promote or copy into canonical statistics/artifacts only after review
```

### Phase 2: Stronger held-out spectral support

Goal:

```text
Defend dynamic subset winners against look-elsewhere bias using actual non-selected BPM spectra at q_hat.
```

Implementation:

- add `scripts/bpm_mining/heldout.py`,
- add wrapper `scripts/evaluate_heldout_spectral_support.py`,
- for finalist rows, compute held-out support near the selected q_hat from the same cached spectra and windows,
- keep H/V separate.

Output:

```text
evolution/finalist_heldout_spectral_support.csv
```

Fields:

```text
heldout_candidate_fraction
heldout_power_support
heldout_prominence_at_qhat
selected_vs_heldout_delta
heldout_bpm_count
quality_flags
```

Progress:

```text
evolution/heldout_progress/parent_status.json
evolution/heldout_progress/shard_<n>.json
logs/progress.csv pass=heldout_spectral_support
```

Parallelism:

- shard by finalist-row chunks,
- group cache loads by `(collection, spill_id, plane, spectral_config)` where possible,
- merge output deterministically by source finalist order plus aggregator.

### Phase 3: Poster-grade selected artifacts

Goal:

```text
Replace placeholder-style plots with physics-review figures for only the best selected spill-plane rows.
```

This phase should generate a small number of colorful, visually strong artifacts for the poster and beam-physics discussion. Do not make hundreds of plots. The target is a curated figure set that explains the result quickly.

Selection rule:

```text
Use artifact_selection/artifact_manifest.csv plus ranking tables to select roughly:
  - 2 best V clean-consensus examples
  - 1 best H clean-consensus or H-improvement example
  - 1 best3/best5 improvement example
  - 1 dynamic/fixed disagreement or multimodal caution example
```

Hard cap:

```text
no more than 8 spill-plane examples for poster-grade rendering unless the user asks
```

Implementation:

- extend `scripts/bpm_mining/plots.py` or add `scripts/bpm_mining/poster_plots.py`,
- add wrapper `scripts/make_best_bpm_poster_artifacts.py`,
- read cached spectra directly rather than relying on placeholder artifact rows,
- generate real BPM-vs-tune spectral heatmaps for selected rows,
- generate overlaid subset spectra for best1, best3, best5, all-BPM mean, all-BPM median, and fixed top-N when Phase 1 exists,
- generate one or two global summary plots from statistics tables,
- preserve existing artifact names where possible and add explicit `_poster` or `_overlay` files when replacing would risk confusion,
- write a small poster figure index with caption drafts and suggested poster placement.

Color/artifact ideas:

1. **BPM tune deconstruction heatmap**

   ```text
   x-axis: tune
   y-axis: BPM index or ring order
   color: row-normalized log spectral power
   overlays: per-BPM peak markers, consensus tune line, best1/best3/best5 membership badges
   ```

   This should be the main “convince the physicist” plot. Use a perceptually strong colormap such as `viridis`, `magma`, or `turbo` if acceptable. Keep H and V panels separate unless both are clean.

2. **Subset spectrum overlay**

   ```text
   x-axis: tune
   y-axis: normalized spectral power or log power
   curves: best1, best3, best5, all-BPM mean, all-BPM median
   overlays: q_hat, consensus tune, expected tune anchor
   ```

   This plot should show why small ensembles beat all-BPM averaging. Use distinct line styles and a compact legend. This is likely a poster panel.

3. **Top-N performance curve**

   ```text
   x-axis: subset size, 1/3/5
   y-axis: median subset score or held-out support
   markers: H and V separately
   optional second y/panel: visible fraction
   ```

   This should be one clean global result plot. It can be built from `statistics/paired_method_tests.csv`, `statistics/subset_size_pareto.csv`, and `evolution/subset_size_comparison.csv`.

4. **BPM inclusion/rank stability map**

   ```text
   x-axis: BPM/ring order
   y-axis or stacked bars: top1/top3/top5 inclusion frequency
   color/group: plane or collection
   ```

   This plot should show that BPM quality is not random and that V rankings are especially stable.

5. **Visibility / handoff preview plot**

   ```text
   x-axis: turn window
   y-axis: BPM index/ring order
   color: visibility score or support at consensus tune
   overlays: top1/top3/top5 membership through time
   ```

   Only generate this for the best one or two selected spills at first. Treat it as exploratory unless the handoff pass is implemented.

6. **Poster contact sheet**

   Create one image or markdown index showing thumbnails of the curated artifacts with one-line captions. This helps quickly choose final poster panels.

Required outputs:

```text
artifacts/poster/selected_poster_artifacts.csv
artifacts/poster/poster_artifact_index.md
artifacts/poster/poster_contact_sheet.png
artifacts/poster/global_topn_performance_hv.png
artifacts/poster/global_bpm_inclusion_h.png
artifacts/poster/global_bpm_inclusion_v.png
artifacts/poster/spill_<id>_<plane>_bpm_tune_deconstruction_poster.png
artifacts/poster/spill_<id>_<plane>_subset_spectra_overlay_poster.png
artifacts/poster/spill_<id>_<plane>_visible_window_tune_evolution_poster.png
```

Existing or compatibility outputs:

```text
artifacts/spills/spill_<id>_<plane>_bpm_tune_deconstruction.png
artifacts/spills/spill_<id>_<plane>_subset_spectra_overlay.png
artifacts/spills/spill_<id>_<plane>_visible_window_tune_evolution.png
artifacts/global/poster_artifact_index.md
```

Poster artifact style requirements:

- large labels readable on a poster,
- explicit plane, spill ID, window length, stride, and tune band in subtitle or caption,
- colorbar labels with clear units such as `row-normalized log power` or `visibility score`,
- H/V color distinction should be consistent across global plots,
- do not overplot 2000 spills,
- do not use rainbow clutter for line plots; use color primarily for heatmaps and density plots,
- every poster artifact should have a caption draft in `poster_artifact_index.md`,
- no more than 4–6 final figure candidates should be recommended.

Recommended caption angle:

```text
Small BPM ensembles improve tune-visible evidence relative to single-BPM and all-BPM summaries. The selected BPMs are supported by a broader within-spill consensus, not merely by one isolated channel.
```

Progress:

```text
artifacts/progress/parent_status.json
artifacts/progress/shard_<n>.json
logs/progress.csv pass=poster_artifacts
```

Parallelism:

- shard by artifact manifest rows,
- workers generate files into per-shard temporary directories,
- parent writes final manifest and summary after all files exist.

### Phase 4: BPM handoff / tune-visibility migration analysis

Goal:

```text
Test whether tune observability migrates across BPMs or BPM ensembles over turn windows.
```

Implementation:

- add `scripts/bpm_mining/handoff.py`,
- add wrapper `scripts/run_bpm_handoff_analysis.py`,
- start standalone before pipeline integration,
- use existing `early_4096_256` cache and selected artifact rows first,
- keep intensity as optional future covariate; do not block position-only v1.

Outputs:

```text
handoff/bpm_window_visibility.csv
handoff/bpm_handoff_events.csv
handoff/bpm_visibility_summary.csv
handoff/handoff_summary.md
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/visible_bpm_fraction_vs_turn_h.png
handoff/visible_bpm_fraction_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
handoff/top_bpm_membership_vs_turn_h.png
handoff/top_bpm_membership_vs_turn_v.png
handoff/spill_<id>_<plane>_bpm_visibility_handoff.png
handoff/spill_<id>_<plane>_top_sets_vs_turn.png
```

Progress:

```text
handoff/progress/parent_status.json
handoff/progress/shard_<n>.json
logs/progress.csv pass=bpm_handoff
```

Parallelism:

- shard by selected `(collection, spill_id, plane)` groups,
- compute window/BPM visibility independently,
- aggregate global handoff-rate and visibility-fraction plots after worker merge.

### Phase 5: Report and verifier integration

Goal:

```text
Make new outputs first-class enough to rerun and review without private chat context.
```

Implementation:

- update `scripts/bpm_mining/report.py` to mention fixed-set direct evaluation, held-out spectral support, and handoff outputs when present,
- update `scripts/bpm_mining/verification.py` to treat new passes as optional sections unless the corresponding output directory exists,
- add concise doc commands in `docs/SPARK.md`.

Validation:

```bash
python3 -m py_compile scripts/bpm_mining/*.py scripts/evaluate_fixed_bpm_sets.py scripts/evaluate_heldout_spectral_support.py scripts/run_bpm_handoff_analysis.py scripts/test_best_bpm_mining.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/smoke --subset-sizes 1 3 5
```

Spark validation sequence:

1. deploy changed files to a new scratch code directory,
2. run fixed-set direct evaluation on 20 selected spills,
3. run held-out spectral support on 1000 finalist rows,
4. run poster artifact generation on 4 artifact-manifest rows,
5. run handoff on 4 selected spill-plane rows,
6. then run the full sidecar passes against `/home/derekste/best_bpm_mining_20260627_best135_from_v2`.

Priority order:

```text
1. Direct fixed-set evaluation
2. Stronger held-out spectral support
3. Poster-grade selected artifacts
4. BPM handoff / tune-visibility migration analysis
5. Optional full-buffer handoff cache only if selected-spill handoff is promising
```

## Suggested Follow-Up Implementation Tasks

### Task A: Reconcile tune anchors in docs

Status: still needed on this branch. A prior main-branch note said this was addressed, but the active `dev/best-bpm-2000-mining` branch still carries older `Qx ~ 0.69`, `Qy ~ 0.71` operational tune wording in `docs/PHYSICS.md`.

Update `docs/PHYSICS.md` so the expected tune region matches current dataset assumptions or clearly distinguishes older operational expectations from current observed early-injection clusters.

Suggested wording:

```text
For the current 2000-spill Spark Tier A study, early-injection BPM spectra cluster near H ≈ 0.65 and V ≈ 0.72. These are used as soft priors for discovery and ranking, not external truth labels.
```

### Task B: Implement direct fixed-set evaluation

Add a script or pipeline pass such as:

```text
scripts/evaluate_fixed_bpm_sets.py
```

or add to `scripts/bpm_mining/statistics.py` / `evolution.py`.

Inputs:

```text
cache/index/spectral_cache.csv
manifest/bpm_index.csv
statistics/bpm_global_statistics.csv
subset_search/best*/best*_results.csv
```

Outputs:

```text
statistics/fixed_set_direct_evaluation.csv
statistics/fixed_vs_dynamic_direct_summary.csv
artifacts/global/fixed_vs_dynamic_direct_h.png
artifacts/global/fixed_vs_dynamic_direct_v.png
```

### Task C: Implement real per-spill BPM tune deconstruction plots

Replace or supplement placeholder artifacts with:

```text
spill_<id>_<plane>_bpm_tune_deconstruction.png
```

Plot content:

- BPM/ring order vs tune spectral-power image,
- primary candidate markers,
- consensus tune line,
- best1/best3/best5 membership annotations.

### Task D: Implement overlaid subset spectra for finalists

Add:

```text
spill_<id>_<plane>_subset_spectra_overlay.png
```

Compare:

- best1,
- best3,
- best5,
- all-BPM mean,
- all-BPM median,
- fixed top-N if available.

### Task E: Add stronger held-out spectral validation for finalists

For finalist subsets, compute held-out spectral support in the same windows using actual held-out BPM spectra.

Output:

```text
evolution/finalist_heldout_spectral_support.csv
```

Include:

```text
heldout_candidate_fraction
heldout_power_support
heldout_prominence_at_qhat
selected_vs_heldout_delta
```

### Task F: Add BPM handoff / tune-visibility migration analysis

#### Motivation

A physicist suggested that the intensity waxing/waning seen on BPM channels may reflect bunch decoherence/recoherence. If coherent transverse signal evolves during the spill, the BPMs that provide the clearest tune evidence may also change with turn number.

The tune itself is not moving from BPM to BPM. The hypothesis is that the **observability of the tune line** migrates between BPMs or BPM ensembles as coherent beam motion damps, recoheres, changes phase-space structure, or changes relative to local BPM noise/electronics.

This could explain several observed behaviors:

- all-BPM averaging can wash out useful tune evidence,
- dynamic best-BPM methods can outperform all-BPM methods,
- different BPMs may dominate at different turn ranges,
- intensity-envelope features may correlate with tune visibility,
- a static “best BPM” set may be insufficient if the useful ensemble changes through the spill.

The analysis question is:

```text
Which BPMs provide usable spectral evidence for the common tune at each turn/window, and does that set change coherently over time?
```

#### Conceptual output

For each spill, plane, BPM, and rolling window, compute:

```text
peak_tune
peak_prominence
power at within-spill consensus tune
local spectral background
second_peak_ratio
visibility_flag
optional intensity metrics
```

Then build a matrix:

```text
BPM × turn-window
```

where color represents tune visibility or support for the consensus tune.

This should produce a new class of diagnostic plot:

```text
x-axis: turn or time
y-axis: BPM index / ring order
color: tune visibility or spectral support
markers: best1 / best3 / best5 membership
overlay: consensus tune visibility / intensity envelope where available
```

Potential filenames:

```text
handoff/spill_<id>_h_bpm_visibility_handoff.png
handoff/spill_<id>_v_bpm_visibility_handoff.png
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
```

#### Proposed implementation location

Add a new module:

```text
scripts/bpm_mining/handoff.py
```

and an entry point:

```text
scripts/run_bpm_handoff_analysis.py
```

The handoff pass should also be callable from the Best-BPM pipeline after `evolution` and before `artifact_selection`, but it can start as a standalone script.

#### Inputs

Use existing Best-BPM mining outputs where possible:

```text
cache/index/spectral_cache.csv
per_bpm/per_bpm_window_features.csv
consensus/spill_consensus_windows.csv
consensus/spill_consensus_summary.csv
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
artifact_selection/artifact_manifest.csv
```

Optional future inputs:

```text
intensity feature tables
position+intensity captured bundles
```

Do not block the first implementation on intensity. Start with position-derived tune visibility.

#### Per-window BPM visibility metric

For each BPM/window, compute a score such as:

```text
visibility_score =
    0.40 * normalized_peak_prominence
  + 0.25 * support_at_consensus_tune
  + 0.15 * inverse_second_peak_ratio
  + 0.10 * inverse_spectral_entropy
  + 0.10 * band_edge_safety
```

where:

```text
support_at_consensus_tune =
  spectral power near q_consensus relative to local background
```

Use the within-spill consensus tune from `spill_consensus_windows.csv`.

Visibility classes:

```text
VISIBLE_TUNE
WEAK_TUNE
NO_RELIABLE_TUNE
```

Suggested initial thresholds:

```text
VISIBLE_TUNE:
  visibility_score >= 0.65
  and peak_prominence_z >= 4.0
  and distance_to_band_edge >= 0.003

WEAK_TUNE:
  visibility_score >= 0.35
  or peak_prominence_z >= 3.0

NO_RELIABLE_TUNE:
  otherwise
```

Keep thresholds configurable.

#### Handoff metrics

For each spill/plane/window, determine:

```text
top1_visible_bpm
top3_visible_set
top5_visible_set
top10_visible_set
visible_bpm_fraction
visible_bpm_count
dominant_digitizer
dominant_ring_sector
consensus_tune
consensus_label
```

Track changes over windows:

```text
top3_jaccard_vs_previous
top5_jaccard_vs_previous
top3_jaccard_vs_injection
top5_jaccard_vs_injection
handoff_score = 1 - Jaccard(topK_current, topK_previous)
```

Add persistence:

```text
handoff_persistence =
  number of consecutive windows for which the new dominant set remains stable
```

Flag likely real handoffs:

```text
PERSISTENT_HANDOFF:
  handoff_score >= 0.6
  and handoff_persistence >= 3 windows
  and consensus tune remains continuous
```

Flag likely noise flicker:

```text
FLICKER:
  high handoff_score
  but low persistence
  or consensus tune jumps
  or visibility is weak
```

#### Intensity-aware extension

Use intensity only as a covariate or quality metric.

Do not multiply position waveforms by intensity:

```text
do not use: position(t) *= intensity(t)
```

For intensity-capable captures, compute per BPM/window:

```text
intensity_median
intensity_rms
intensity_std_over_mean
intensity_envelope
intensity_drop_flag
```

Then compare to tune visibility:

```text
corr(intensity_median, visibility_score)
corr(intensity_envelope, visibility_score)
lagged_corr(intensity_envelope, visibility_score)
```

Outputs:

```text
handoff/intensity_visibility_correlation.csv
handoff/intensity_visibility_correlation_by_bpm.png
handoff/spill_<id>_<plane>_intensity_visibility_overlay.png
```

Keep intensity weighting only if it improves visibility/held-out support without shifting the selected tune.

#### Required output tables

Create:

```text
handoff/bpm_window_visibility.csv
handoff/bpm_handoff_events.csv
handoff/bpm_visibility_summary.csv
handoff/handoff_summary.md
```

`bpm_window_visibility.csv` schema:

```text
collection
spill_id
plane
spectral_config
window_index
center_turn
bpm_index
bpm_name
digitizer
consensus_tune
consensus_label
peak_tune
peak_prominence_z
power_at_consensus
local_background_at_consensus
support_at_consensus
second_peak_ratio
spectral_entropy
visibility_score
visibility_class
is_top1_visible
is_top3_visible
is_top5_visible
quality_flags
```

`bpm_handoff_events.csv` schema:

```text
collection
spill_id
plane
subset_size
window_index
center_turn
previous_members
current_members
jaccard_vs_previous
handoff_score
handoff_persistence
consensus_tune
consensus_delta
event_label
quality_flags
```

`bpm_visibility_summary.csv` schema:

```text
collection
spill_id
plane
bpm_index
bpm_name
digitizer
visible_window_fraction
first_visible_turn
last_visible_turn
visibility_duration_turns
median_visibility_score
median_support_at_consensus
top1_window_fraction
top3_window_fraction
top5_window_fraction
handoff_event_count
```

#### Required plots

Global plots:

```text
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/visible_bpm_fraction_vs_turn_h.png
handoff/visible_bpm_fraction_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
handoff/top_bpm_membership_vs_turn_h.png
handoff/top_bpm_membership_vs_turn_v.png
```

Selected per-spill plots:

```text
handoff/spill_<id>_h_bpm_visibility_handoff.png
handoff/spill_<id>_v_bpm_visibility_handoff.png
handoff/spill_<id>_h_top_sets_vs_turn.png
handoff/spill_<id>_v_top_sets_vs_turn.png
```

Per-spill handoff plot should show:

1. BPM/ring order vs turn heatmap of visibility score.
2. Top1/top3/top5 membership markers.
3. Consensus tune and consensus quality as a lower panel.
4. Optional intensity envelope overlay when intensity data exists.

#### Interpretation rules

Strong evidence for real BPM handoff requires:

1. A stable within-spill consensus tune exists.
2. One BPM group supports that tune early.
3. A different BPM group supports the same or smoothly evolving consensus tune later.
4. The transition persists across multiple adjacent windows.
5. The transition is not dominated by band-edge locking.
6. The phenomenon repeats across multiple spills or morphology clusters.
7. Optional: visibility changes correlate with intensity-envelope changes.

Weak evidence / likely artifact:

```text
best BPM jumps randomly
q_hat jumps with the selected BPM set
no clean consensus tune exists
handoff disappears under different window/stride
handoff is dominated by one noisy BPM
visibility is weak or band-edge locked
```

#### Recommended initial scope

Do not run this over every possible artifact immediately.

First pass:

```text
use current early_4096_256 cache
turn range: 0–15000
subset sizes: 1, 3, 5
planes: H, V
selected spills only:
  - clean consensus examples
  - best3/best5 improvement examples
  - dynamic/fixed disagreement examples
  - multimodal/failure examples
```

Second pass, only if first pass is promising:

```text
add full-buffer cache config:
  name: handoff_4096_256
  turn_start: 0
  turn_end: 50000
  window_turns: 4096
  stride_turns: 256
```

Optional high-time-resolution pass:

```text
handoff_2048_128
```

Use 4096/256 first because it is more spectrally stable.

#### Poster relevance

This analysis could become a strong poster result if it shows:

```text
Tune evidence is not uniformly distributed across BPMs. As coherent beam motion evolves, different BPM ensembles provide the strongest tune visibility. Dynamic or visibility-weighted BPM subset selection can recover tune evidence more reliably than all-BPM averaging.
```

This would explain why Best-BPM mining is physically meaningful rather than merely a numerical optimization.

## Guardrails

- Stay BPM-only unless explicitly adding external reference data.
- Do not call within-spill consensus “truth.”
- Do not claim true turn-by-turn tune.
- Do not claim full-spill tune tracking from early-window cached analysis.
- Do not run heavy follow-up searches without approval.
- Do not delete or mutate Spark data without approval.
- Prefer selected artifact generation over plotting everything.
- Keep H and V conclusions separate.
- Treat best poster-looking output as a candidate, not proof.

## Bottom Line

The repo and current run are close to an IBIC poster. The main remaining work is not broad new computation. It is producing physics-defensible evidence from the current mining output:

```text
best1 vs best3 vs best5 comparison
BPM rank stability
held-out support
direct fixed-set evaluation
poster-grade deconstruction and subset spectra plots
```

If those results are clean, the poster story is strong. If not, the poster still works as an exploratory result showing that BPM tune evidence is present but condition-dependent and requires adaptive BPM/subset selection.
