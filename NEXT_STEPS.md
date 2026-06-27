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

The immediate active work is the focused Spark Best-BPM mining run over the 2000-spill Tier A position-only dataset. This is not a generic autosweep anymore. It is a targeted search for tune-sensitive BPMs and small BPM subsets.

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

Do not kill, restart, or mutate the Spark run without explicit user approval.

## Current Spark Status Check

A lightweight status check at approximately 2026-06-27 11:31 CDT showed:

```text
phase: subset_search
progress: 531 / 4000 shard rows
fraction complete: 0.13275
GPU: NVIDIA GB10, ~96% utilization
workers: 4 active workers at ~100% CPU each
run root disk usage: 56K at this phase
```

The run looked healthy. Progress is tracked in:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2/subset_search/progress/shard_*.json
```

Use the current Spark status command from `docs/CURRENT_STATUS.md` if checking again. Keep checks bounded and read-only.

## Repo State Summary

Important files/docs already reviewed:

```text
README.md
docs/CURRENT_STATUS.md
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

Current estimated readiness after repo inspection:

```text
Platform / repo maturity:        85–90%
Data capture story:              90%
Spark mining implementation:     75–85%
Current best1/3/5 run:           in progress and healthy
Physics validation evidence:     55–70%
Poster-quality figures:          40–60%
Poster narrative:                80%
```

Overall:

```text
Current state: 70–80% poster-ready
If best1/3/5 results are sane: 80–85%
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

## First Actions When Current Run Completes

1. Verify output contract for the focused run:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/verify_best_bpm_outputs.py \
  --root /home/derekste/best_bpm_mining_20260627_best135_from_v2 \
  --subset-sizes 1 3 5
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

3. Decide if best5 saturates performance before considering best10.

4. Run or implement direct fixed-set evaluation from cached spectra.

5. Generate poster-grade figures from selected finalists.

## Suggested Follow-Up Implementation Tasks

### Task A: Reconcile tune anchors in docs

Status: addressed in `docs/PHYSICS.md` on 2026-06-27. The current 2000-spill
Best-BPM anchors H ≈ 0.65 and V ≈ 0.72 are now documented as dataset-specific
soft priors, and the older Qx/Qy values are explicitly historical unless tied
to an independent reference for the reviewed data.

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
