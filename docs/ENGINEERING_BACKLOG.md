# ENGINEERING_BACKLOG

Engineering backlog for fixes, QoL work, maintainability, and hardening.

## Workflow

1. Add new work only in `## Todo` using the item template below.
2. Move an item to `## In Progress` when implementation starts.
3. Move an item to `## Done` only after the doc sync gate in `AGENTS.md`
   is satisfied.
4. Keep `Docs:` explicit so updates are visible and auditable.

## Item Template

Copy this block for each new item:

```md
### [ID] Short title
- Status: todo | in_progress | done
- Owner: <name>
- Type: fix | qol | refactor | reliability | perf | docs
- Why: <problem being solved>
- Scope: <what will change>
- Acceptance: <testable done criteria>
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md
- Validation: cargo fmt; cargo test -- --nocapture
- Notes: <optional>
```

## Todo

None.

## In Progress

### [ENG-018] Best-BPM 2000-spill mining pipeline
- Status: in_progress
- Owner: codex
- Type: feature
- Why: the poster narrative needs a defensible BPM-only study of which individual and small BPM subsets most consistently recover tune information in the 2000 unlabeled Spark spills.
- Scope: add `scripts/bpm_mining/`, pass wrappers, default config, exact best-1/best-3 search, screened audited best-5/best-10 search, per-BPM consensus/features, global statistics, clustering, artifact selection, plots, final reports, and Spark parallel worker controls.
- Acceptance: the pipeline writes every required output group from `BEST_BPM_2000_SPILL_MINING_IMPLEMENTATION_PLAN.md`, passes synthetic unit/smoke tests, and completes a Spark run over the two Tier A collections using parallel workers.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/*.py scripts/test_best_bpm_mining.py; python3 scripts/test_best_bpm_mining.py (10 tests, parallel worker regressions skipped locally when process pools are sandbox-blocked); Spark smoke passed; full Spark run active at `/home/derekste/best_bpm_mining_20260624_full_v2`
- Notes: v1 keeps the full-buffer evolution schema but uses cached early rolling spectra unless a longer spectral-cache config is added.

## Done

### [ENG-017] Elite full-data autosweep stage
- Status: done
- Owner: codex
- Type: feature
- Why: the completed Spark pilot needs a focused full-data stage that reruns only explicit elite H/V/poster configurations over usable Tier A spills and generates heavy review artifacts.
- Scope: add elite full-stage selection and summary scripts, make full mode consume the supplied config list exactly, add BPM leaderboard and subset-consistency analyzer artifacts, and cover the flow with stdlib tests.
- Acceptance: pilot ranked outputs can produce filtered elite manifests/config lists, full-stage runs preserve rejected diagnostics, summaries identify best H/V/robust/poster configs, and heavy jobs emit BPM leaderboard and subset-consistency artifacts.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/build_elite_full_stage.py scripts/make_elite_full_summary.py scripts/run_autosweep.py scripts/gpu_analyze_captured_spills.py scripts/test_autosweep.py; python3 scripts/test_autosweep.py; python3 scripts/gpu_analyze_captured_spills.py --self-test
- Notes: Tier A usable-spill filtering comes from `spill_health.csv`; poster-safe summaries exclude `TOO_SLOW`, `UNSTABLE_H`, `UNSTABLE_V`, and `OVERFITS_BAND` by default.

### [ENG-016] Spark BPM autosweep ranking and classification
- Status: done
- Owner: codex
- Type: feature
- Why: the raw Spark position-only BPM dataset needs automated staged parameter exploration, candidate spill/config ranking, and classification without a naive full Cartesian sweep.
- Scope: add Stage 0 manifest/health/cache scripts, extend the raw captured-spill GPU analyzer with turn/plane/BPM-combination/preprocessing/ridge-anchor knobs, add deterministic autosweep orchestration, ranking/classification tables, initial summary generation, optional Spark venv bootstrap, and stdlib tests.
- Acceptance: Tier A raw position-only bundles can be inventoried, health-checked, swept in pilot/full modes, ranked with the required weighted score formula, classified with stable spill/config labels, and summarized into the required CSV/JSON/Markdown/PNG artifact set.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py scripts/build_collection_manifest.py scripts/validate_spill_integrity.py scripts/build_spill_cache.py scripts/run_autosweep.py scripts/rank_autosweep_results.py scripts/make_initial_analysis_summary.py scripts/test_autosweep.py; python3 scripts/test_autosweep.py; python3 scripts/gpu_analyze_captured_spills.py --self-test
- Notes: Tier B intensity/beam-loss support remains later-capable and does not block Tier A outputs. Autosweep scoring is BPM-only and should not be treated as Schottky/reference validation.

### [ENG-015] Offline tune-evolution poster upgrade
- Status: done
- Owner: codex
- Type: feature
- Why: `BPM_TUNE_EVOLUTION_ANALYSIS_UPGRADE_PLAN.md` requires cleaner and more physics-reviewable tune-evolution products than baseline FFT/stride traces alone.
- Scope: extend `scripts/gpu_analyze_captured_spills.py` with ridge-density plots, Hann/multitaper spectrogram options, dynamic-programming ridge extraction, representative ridge traces/overlays, optional SVD/PCA denoising products, and DGX benchmark markdown/PNG outputs while keeping CPU reproducibility and the existing baseline outputs.
- Acceptance: the analyzer exposes the requested CLI knobs; a CPU smoke run with `--spectrogram-method both --ridge-method dp --ridge-source-method multitaper --svd-denoise` produces all named upgrade artifacts; Spark can run the same upgraded path over `/home/derekste/tbt-spills-2000` with CuPy.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py scripts/bpm_dgx_poster.py; python3 scripts/gpu_analyze_captured_spills.py --self-test; CPU synthetic smoke with both spectrogram methods, DP ridge, and SVD enabled; remote Spark upgraded run over the copied 2000-spill dataset
- Notes: SVD/PCA remains opt-in and representative-spill only; it is not a production Rust tune-extraction default or a Schottky validation substitute.

### [ENG-014] Spark GPU raw captured-spill analysis
- Status: done
- Owner: codex
- Type: feature
- Why: the 2000-spill raw payload set is large enough that the poster/DGX phase needs a direct CuPy/CUDA analyzer instead of only summary-artifact synthesis.
- Scope: add `scripts/gpu_analyze_captured_spills.py` to load captured-spill `manifest.json` files and little-endian f32 payloads, run Hann-window FFT tune extraction with flash windows and local tracking on CuPy, keep NumPy CPU fallback/self-test, and emit GPU spill summaries, sliding/flash CSVs, tune/waterfall/spectrogram PNGs, and benchmark markdown.
- Acceptance: local and Spark self-tests pass; Spark can run CUDA smoke/full passes over the copied two-run 2000-spill dataset using `/home/derekste/venvs/cupy-spark-cu13`.
- Docs: README.md, AGENTS.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py; python3 scripts/gpu_analyze_captured_spills.py --self-test; remote Spark self-test with `/home/derekste/venvs/cupy-spark-cu13/bin/python`; Spark CUDA smoke/full run over `/home/derekste/tbt-spills-2000`
- Notes: CuPy/CUDA 13 on Spark is provided by `/home/derekste/venvs/cupy-spark-cu13`; use `ssh -K spark.fnal.gov` from `drbpm1` for restartable `rsync --partial` copies. The 2000-spill raw dataset was copied to `/home/derekste/tbt-spills-2000` on Spark. Full Spark outputs were generated at `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w2048` (2048-turn window, 1776 usable spills, 96000 sliding rows, 22.404 s elapsed) and `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w256` (256-turn true-128 flash windows, 1775 usable spills, 512000 sliding rows, 24.244 s elapsed).

### [ENG-013] BPM-only poster/DGX standalone artifact tool
- Status: done
- Owner: codex
- Type: feature
- Why: the conference-poster sprint needs a standalone offline tool that runs over the complete collected BPM artifact set on `drbpm1` or a DGX-mounted copy without changing the Rust runtime.
- Scope: add `scripts/bpm_dgx_poster.py` plus thin phase wrappers for manifest, baseline, flash, spectrogram/waterfall, subset, optional ML, benchmark, and poster-plot collection; support `candidate_spills.csv`, `spills_summary.csv`, and `capture_index.csv`; keep CPU fallback and optional CUDA/CuPy benchmarking; ignore generated Python cache and local poster-output directories.
- Acceptance: local self-test and review-artifact smoke run produce the poster-phase manifest, summaries, PNGs, model reports, benchmark report, and poster plot index; docs state that the full run should target `/home/derekste/out` on `drbpm1` or the DGX copy and that Schottky is excluded from this phase.
- Docs: README.md, AGENTS.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 scripts/bpm_dgx_poster.py --self-test; python3 scripts/bpm_dgx_poster.py run-all --input review-artifacts --out /private/tmp/tbt-monitor-poster-smoke --flashes 128 256 512 --device cpu; remote `drbpm1` self-test; remote `drbpm1` run-all over `/home/derekste/out`; remote `spark` run-all over copied `tune-curation`; cargo fmt --all; cargo test -- --nocapture
- Notes: full remote output was generated at `/home/derekste/out/bpm-dgx-poster-20260609`; Spark output was generated at `/home/derekste/bpm-dgx-poster-20260609-spark` after copying the 671 MB `tune-curation` tree. CuPy was installed into `/home/derekste/venvs/cupy-spark-cu13` using a wheelhouse downloaded on `adlinux3`; rerunning on Spark with that venv produced `/home/derekste/bpm-dgx-poster-20260609-spark-cu13` with CUDA benchmark availability.

### [ENG-012] Capture timestamp distribution reporting
- Status: done
- Owner: codex
- Type: reliability
- Why: operators need `120/120` captured streams to show the actual timestamp distribution instead of relying on ambiguous latest-poll `% aligned` wording.
- Scope: add captured-payload and latest-ID timestamp delta distributions to manifests, summaries, run-level JSON/Markdown, and a dedicated `capture_timestamp_distribution.csv`; clarify console warnings and docs.
- Acceptance: complete captures report captured-payload timestamp buckets separately from latest-ID snapshot buckets; run-level reports aggregate both distributions; tests cover distribution fields and output creation.
- Docs: README.md, docs/USAGE.md, docs/CONFIG_REFERENCE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements the first practical slice of GitHub issue #19; settle-after-wake polling remains a follow-up if latest-ID snapshots still need delayed classification.

### [ENG-011] README and usage-doc restructure
- Status: done
- Owner: codex
- Type: docs
- Why: the README had grown into a long combined feature inventory, user guide, artifact reference, Docker guide, and developer orientation.
- Scope: make `README.md` a concise project entry point, move command workflows into `docs/USAGE.md`, and trim repeated implemented-feature inventories across planning and physics docs.
- Acceptance: README introduces the project succinctly with links to feature guides; user workflows remain documented outside the README; internal docs consistently reference implemented commands, timing semantics, captured-spill artifacts, batch outputs, and remaining physics work.
- Docs: README.md, AGENTS.md, .github/pull_request_template.md, .github/ISSUE_TEMPLATE/feature_request.yml, docs/USAGE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/GITHUB_WORKFLOW.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo run --offline -- --help; cargo fmt --all; cargo test -- --nocapture
- Notes: tracked by GitHub issue #17; docs-only restructure with no behavior or schema changes.

### [ENG-010] Capture timing diagnostics and assess preflight
- Status: done
- Owner: codex
- Type: reliability
- Why: DAQ runs need first-class completeness and timing diagnostics so stale digitizers and timestamp distributions are visible before and during acquisition.
- Scope: add `same_spill_tolerance_ms`, capture manifest diagnostics, run-level capture CSV/JSON/Markdown reports, offline `diagnose-captures`, non-capturing `assess`, reason-code tests, and documentation.
- Acceptance: capture artifacts distinguish complete same-spill payloads from stale latest-poll observations; existing capture directories can regenerate reports; `assess` writes preflight stream/digitizer reports without payload capture; v1 remains annotate-only.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/CONFIG_REFERENCE.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: future strict-fail mode should reuse the same reason codes and enforce captured artifact quality first.

### [ENG-009] Online/offline split parity guardrail
- Status: done
- Owner: codex
- Type: reliability
- Why: the captured-bundle split needs a deterministic regression check that offline analysis preserves today's proof-of-concept behavior for the same raw spill data.
- Scope: add a no-Redis parity test that builds an online-style snapshot from decoded raw payload bytes, loads the same captured-spill bundle offline, and compares tune estimates, sliding medians, selected stream/quality fields, warnings, and quality flags with field-named failure messages.
- Acceptance: parity runs in normal `cargo test`; differences in key proof-of-concept outputs produce actionable field-specific failures; docs state this is a split regression guard, not physics certification of the current algorithm.
- Docs: docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issue #5. Post-split analysis refinement issue #9 remains open.

### [ENG-008] Offline captured-spill batch analysis
- Status: done
- Owner: codex
- Type: feature
- Why: captured raw spill bundles need batch-style analysis without Redis connectivity so acquisition-first runs can be reviewed and reprocessed offline.
- Scope: add captured-bundle discovery, duplicate-target suppression, offline snapshot reconstruction for multiple bundles, `analyze-captured-spills`, existing batch writer reuse, and focused offline batch artifact tests.
- Acceptance: a directory of captured-spill bundles can produce the current batch artifacts without Redis access; a single bundle directory or manifest path is also accepted; malformed bundles are skipped with explicit diagnostics when other usable bundles remain; batch records identify offline provenance with `trigger_source=captured-spill`.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issue #7. Minimal online/offline parity issue #5 remains open.

### [ENG-007] Offline captured-spill single analysis
- Status: done
- Owner: codex
- Type: feature
- Why: captured raw spill bundles need to be analyzable without Redis connectivity so acquisition and analysis are actually separated.
- Scope: add captured-spill manifest loading, safe payload resolution, checksum/size/sample validation, raw little-endian `f32` payload decoding, snapshot reconstruction, `analyze-captured-spill`, and focused offline artifact tests.
- Acceptance: a captured-spill bundle directory or manifest path can produce the current one-spill analysis artifacts without Redis access; unsupported schema/artifact types fail explicitly; incomplete/malformed captured streams emit warnings or errors instead of silent fallback.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issues #4 and #3. Offline multi-bundle issue #7 and parity issue #5 remain open.

### [ENG-006] Raw captured-spill acquisition commands
- Status: done
- Owner: codex
- Type: feature
- Why: acquisition must be separable from tune analysis so complete BPM spill data can be captured once and reanalyzed offline later.
- Scope: add `src/capture.rs`, `capture-spill`, `capture-spills --free-run [--count N]`, `schema_version=1` manifest writing, raw payload files, capture summaries, run-level `capture_index.csv`, and focused capture tests.
- Acceptance: one-shot capture writes a complete bundle without tune analysis; free-run capture writes one bundle per unique target and maintains a batch index; manifests include target/alignment metadata, full stream inventory, payload file paths, sizes, sample counts, and checksums; incomplete states emit warnings.
- Docs: README.md, AGENTS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issues #2, #6, and #8. Offline loader/analysis issues #3, #4, #7, and parity issue #5 remain open.

### [ENG-005] GitHub issue and PR workflow bootstrap
- Status: done
- Owner: codex
- Type: docs
- Why: project planning needs issue-first tracking, PR templates, and a durable map for the acquisition/offline-analysis split.
- Scope: add GitHub issue templates, a PR template, workflow guidance, and an acquisition/analysis issue map with PR-sized slices.
- Acceptance: templates exist under `.github/`, workflow docs define labels/branch/PR expectations, and the acquisition/analysis split is mapped into GitHub-ready issues.
- Docs: README.md, docs/GITHUB_WORKFLOW.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all --check; cargo test -- --nocapture
- Notes: completed on 2026-05-31; tracked by GitHub issue #1 and seeded split issues #2-#9.

### [ENG-004] Per-flash histogram batch artifacts
- Status: done
- Owner: codex
- Type: qol
- Why: flash-sampled runs need distribution views at each flash index, not only trend lines.
- Scope: add `tune_histogram_flash_XX.png` generation for flash-enabled batch outputs (including `analyze-spill --free-run --count` synthesis path) using existing sliding tune points.
- Acceptance: when `--flashes` is set and batch outputs are emitted, one `tune_histogram_flash_XX.png` is produced per available flash index alongside `tune_vs_spill_flash_XX.png`.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-003] Plot-axis domain toggle and tune-grid spacing config
- Status: done
- Owner: codex
- Type: qol
- Why: operators need turns as default axis domain, optional physical-time view in microseconds, and configurable tune-grid readability.
- Scope: add `plot_time_axes_in_us` config + CLI enable override (`--plot-time-axes-in-us`) across analysis commands, apply turn/us axis rendering consistently across per-spill and composite artifacts, and make `tune_vs_time` Y-grid spacing configurable (`tune_plot_y_tick_step`).
- Acceptance: default plots render turn-index time axes, enabling time-domain mode renders microseconds (`us`) using `turn_period_us`, and `tune_vs_time` horizontal grid spacing follows `tune_plot_y_tick_step`.
- Docs: README.md, docs/CONFIG_REFERENCE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-002] Flashpoint sampling mode and spill-trend expansion
- Status: done
- Owner: codex
- Type: qol
- Why: compare tune across spills at consistent in-spill checkpoints without relying only on injection or dense stride sampling.
- Scope: add `--flashes N|max` to tune-analysis commands, bound flash sampling by available turn depth, emit per-flash `tune_vs_spill_flash_XX.png`, and annotate per-spill `tune_vs_time` with flash turns and injection guides.
- Acceptance: `--flashes` overrides stride-based sliding placement, `--flashes max` resolves to per-spill maximum supported windows, flash mode uses `sliding_window_turns` for injection-path estimation, spill summaries warn when requested flashes are reduced by turn-depth bounds, and batch/per-spill artifacts render with flash data.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-001] Backlog framework bootstrap
- Status: done
- Owner: codex
- Type: docs
- Why: establish a durable process for keeping implementation and docs aligned.
- Scope: define backlog structure/template and add doc-sync DoD in `AGENTS.md`.
- Acceptance: template exists in this file and DoD gate exists in `AGENTS.md`.
- Docs: docs/ENGINEERING_BACKLOG.md, AGENTS.md
- Validation: docs-only change
- Notes: completed on 2026-03-09.
