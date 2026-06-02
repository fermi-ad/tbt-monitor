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

None.

## Done

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
