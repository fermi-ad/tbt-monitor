# AGENTS Guide for `tbt-monitor-tui`

This file helps coding assistants make safe, coherent changes in this repository.

## Fast Orientation

- Entry point: `src/main.rs`
- Config schema/parser: `src/config.rs`
- XML import path: `src/importer.rs`
- Live monitor runtime: `src/monitor.rs`
- Analysis/study/batch logic: `src/analyze.rs`
- User docs: `README.md`
- Architecture and rationale docs: `docs/ARCHITECTURE.md`, `docs/DESIGN_DECISIONS.md`, `PLAN.md`

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
- plan alignment (`PLAN.md`) if roadmap fit changes

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
