# ENGINEERING_BACKLOG.md

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
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, PLAN.md, PHYSICS.md, ANALYSIS_CHECKLIST.md
- Validation: cargo fmt; cargo test -- --nocapture
- Notes: <optional>
```

## Todo

None.

## In Progress

None.

## Done

### [ENG-001] Backlog framework bootstrap
- Status: done
- Owner: codex
- Type: docs
- Why: establish a durable process for keeping implementation and docs aligned.
- Scope: define backlog structure/template and add doc-sync DoD in `AGENTS.md`.
- Acceptance: template exists in this file and DoD gate exists in `AGENTS.md`.
- Docs: ENGINEERING_BACKLOG.md, AGENTS.md
- Validation: docs-only change
- Notes: completed on 2026-03-09.
