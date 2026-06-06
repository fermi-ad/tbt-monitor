# GitHub Workflow

This repository uses issue-first feature tracking and PR-first integration.

## 1. Source of Truth

- Feature/bug/analysis tasks start as GitHub Issues.
- Implementation lands via PRs linked to issues (`Closes #...`).
- `docs/ENGINEERING_BACKLOG.md` mirrors active engineering execution and doc-sync state.

## 2. Issue Types

Use the issue templates in `.github/ISSUE_TEMPLATE/`:

- `Feature Request`: behavior or capability changes.
- `Bug Report`: defect/regression tracking.
- `Analysis / Physics Task`: physics-validation work and dataset review tasks.

Minimum issue quality bar:

- explicit problem and scope
- testable acceptance criteria
- validation plan
- docs impacted

## 3. Branching and PRs

- Branch naming: `dev/<short-topic>`.
- One logical change per PR.
- PR description must include:
  - Summary and Why
  - Scope (in/out)
  - Validation evidence
  - Doc-sync checklist (AGENTS gate)
  - Linked issue(s)

## 4. Merge Readiness Gate

Before merge:

1. `cargo fmt --all`
2. `cargo test -- --nocapture`
3. User-visible behavior/docs updated (`docs/USAGE.md`, and `README.md` when
   the project overview or command map changes)
4. Architecture/rationale docs updated as needed
5. `docs/ENGINEERING_BACKLOG.md` item moved to `done` (or deferral note recorded)

## 5. Labels (recommended)

Baseline labels used for triage:

- Type: `type:feature`, `type:bug`, `type:analysis`, `type:docs`, `type:refactor`
- Area: `area:acquisition`, `area:analysis`, `area:plotting`, `area:config`, `area:docs`, `area:infra`
- Priority: `priority:P0`, `priority:P1`, `priority:P2`
- State: `state:blocked`, `state:needs-data`, `state:ready`, `state:in-progress`

The default GitHub labels may remain available, but new tracked work should use
the structured type/area/priority/state labels above.

## 6. Operating Cadence

- Weekly triage:
  - review new issues
  - assign priority/area labels
  - select next PR-sized slices
- Per-PR:
  - keep issue acceptance criteria updated if scope changes
- Post-merge:
  - close issue automatically via PR
  - mark backlog item `done`

## 7. Acquisition/Analysis Split Tracking

The initial roadmap for decoupling acquisition from offline analysis lives in
`docs/ISSUE_MAP_DAQ_SPLIT.md`. Keep that map aligned with GitHub issue numbers
until the epic is fully represented in GitHub.
