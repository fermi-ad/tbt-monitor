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
