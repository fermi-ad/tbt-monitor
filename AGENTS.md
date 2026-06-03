# AGENTS Guide for `tbt-monitor-tui`

This file helps coding assistants make safe, coherent changes in this repository.

## Fast Orientation

- Entry point: `src/main.rs`
- Config schema/parser: `src/config.rs`
- XML import path: `src/importer.rs`
- Live monitor runtime: `src/monitor.rs`
- Raw spill capture path: `src/capture.rs`
- Analysis/study/batch logic: `src/analyze.rs`
- User docs: `README.md`
- Architecture and rationale docs: `docs/ARCHITECTURE.md`, `docs/DESIGN_DECISIONS.md`, `docs/PLAN.md`

## Core Invariants

1. `MonitorConfig::validate()` must remain authoritative for runtime safety checks.
2. `target_ms` meaning must remain explicit and consistent across analysis paths.
3. Adjacent-bucket tolerance behavior (currently `±1 ms`) must stay documented when changed.
4. Incomplete poll states must emit warnings/quality flags, not fail silently.
5. Batch output schema changes must be reflected in docs and tests.

## Change Discipline

When behavior changes, update all of:
- implementation (`src/*.rs`)
- user docs (`README.md`)
- architecture/rationale docs (`docs/*.md`)
- plan alignment (`docs/PLAN.md`) if roadmap fit changes

## Definition of Done (Doc Sync Gate)

No feature/fix is complete until code, tests, and docs are updated in the
same change.

Required completion checks:

1. Backlog item status is moved to `done` in `docs/ENGINEERING_BACKLOG.md`.
2. Affected docs listed in the backlog item's `Docs:` line are updated.
3. `README.md` is updated for user-visible behavior/CLI/artifact changes.
4. `docs/ARCHITECTURE.md` and `docs/DESIGN_DECISIONS.md` are updated when
   module boundaries, data flow, or rationale/tradeoffs change.
5. `docs/PLAN.md` is updated when plan alignment/divergence changes.
6. `docs/PHYSICS.md` and/or `docs/ANALYSIS_CHECKLIST.md` are updated when physics
   validation scope, acceptance criteria, or remaining analysis tasks change.
7. Tests are added or updated for behavior changes.

If any required doc update is intentionally deferred, record a short reason in
the same backlog item under `Notes:`.

## Common Tasks

### Add an analysis metric

1. Compute in `src/analyze.rs`.
2. Thread into spill summary and batch summary if applicable.
3. Add tests in `src/analyze.rs`.
4. Document in `README.md` and architecture docs.

### Add CLI option

1. Add flag in `src/main.rs` command struct.
2. Apply override before dispatch.
3. Validate through config and command-specific checks.
4. Update CLI examples in `README.md`.

### Modify synchronization/timing behavior

1. Update timing helpers (`target selection`, `dedupe`, `timeliness`).
2. Add/adjust tests for adjacent-ms behavior and edge cases.
3. Document operational impact and rationale in docs.

## Validation Commands

Run from repo root:

```bash
cargo fmt
cargo test -- --nocapture
```

For focused checks:

```bash
cargo test choose_target_millisecond -- --nocapture
cargo test historical_candidate_ranking -- --nocapture
```

## Pitfalls to Avoid

- Do not introduce silent fallback behavior without warnings.
- Do not change output field meaning without documenting migration impact.
- Do not assume one stream timestamp exactly equals all others during real operations.
