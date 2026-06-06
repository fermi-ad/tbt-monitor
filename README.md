# tbt-monitor

`tbt-monitor-tui` is a Rust/Ratatui tool for MUON BPM turn-by-turn (TbT)
Redis streams. It monitors live stream arrivals, captures synchronized raw
spill bundles, and analyzes live or captured spills into tune-review artifacts.

The project goal is operational evidence: determine when BPM-derived `Qx/Qy`
tune estimates are synchronized, complete enough, and physically credible for
Delivery Ring studies.

## What It Provides

- ACNET XML import into validated monitor/analyzer config.
- Live Redis stream health monitoring with `XREAD BLOCK`.
- One-shot and continuous raw spill capture.
- Capture timing diagnostics and non-capturing preflight assessment.
- Offline reanalysis from captured-spill bundles without Redis connectivity.
- One-spill, multi-spill, no-beam, flashpoint, and robustness-study analysis.
- Batch records, quality flags, timeliness metrics, plots, and summaries for
  physics review.

## Start Here

| Need | Command family | Guide |
| --- | --- | --- |
| Generate or tune config | `import`, config keys | [Config Reference](docs/CONFIG_REFERENCE.md) |
| Watch live stream health | `monitor` | [Usage Guide](docs/USAGE.md#live-monitor) |
| Capture raw spills first | `capture-spill`, `capture-spills` | [Usage Guide](docs/USAGE.md#raw-spill-capture) |
| Check or regenerate DAQ timing diagnostics | `assess`, `diagnose-captures` | [Usage Guide](docs/USAGE.md#capture-diagnostics) |
| Analyze one live or captured spill | `analyze-spill`, `analyze-captured-spill` | [Usage Guide](docs/USAGE.md#one-spill-analysis) |
| Analyze many spills | `analyze-spills`, `analyze-captured-spills` | [Usage Guide](docs/USAGE.md#batch-analysis) |
| Study analysis robustness | `analyze-phase` | [Usage Guide](docs/USAGE.md#robustness-studies) |
| Review implementation and rationale | modules, timing, artifacts | [Architecture](docs/ARCHITECTURE.md), [Design Decisions](docs/DESIGN_DECISIONS.md) |
| Review physics status and remaining validation work | acceptance criteria, open tasks | [Physics](docs/PHYSICS.md), [Analysis Checklist](docs/ANALYSIS_CHECKLIST.md), [Plan](docs/PLAN.md) |

## Quick Start

Build or inspect the CLI:

```bash
cargo check --offline
cargo run --offline -- --help
```

Generate config from ACNET XML:

```bash
cargo run --offline -- import \
  --source /path/to/Config.xml \
  --output config/monitor.cfg
```

Run the live monitor:

```bash
cargo run --offline -- monitor --config config/monitor.cfg
```

Capture raw data for later offline analysis:

```bash
cargo run --offline -- capture-spills \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25
```

Analyze captured bundles offline:

```bash
cargo run --offline -- analyze-captured-spills \
  --config config/monitor.cfg \
  --bundles-dir out \
  --out-dir out/offline_batch \
  --count 25
```

## Documentation Map

- [Usage Guide](docs/USAGE.md): command workflows, options, outputs, Docker
  examples, and when to use each feature.
- [Config Reference](docs/CONFIG_REFERENCE.md): config keys and operational
  tuning guidance.
- [Architecture](docs/ARCHITECTURE.md): module boundaries, runtime data flow,
  synchronization policy, artifact contract, and tests.
- [Design Decisions](docs/DESIGN_DECISIONS.md): rationale and tradeoffs behind
  the current implementation.
- [Plan](docs/PLAN.md): implementation alignment with the tune-methodology
  plan and remaining roadmap.
- [Physics](docs/PHYSICS.md): physics-validation criteria and known limits.
- [Analysis Checklist](docs/ANALYSIS_CHECKLIST.md): remaining review artifacts
  and analysis-quality work.
- [Engineering Backlog](docs/ENGINEERING_BACKLOG.md): completed and active
  implementation tracking.
- [GitHub Workflow](docs/GITHUB_WORKFLOW.md): issue-first and PR workflow.
- [DAQ Split Issue Map](docs/ISSUE_MAP_DAQ_SPLIT.md): acquisition/offline
  analysis split history and follow-up issue mapping.
- [AGENTS.md](AGENTS.md): coding-assistant invariants for this repository.

## Repository Layout

- `src/main.rs`: CLI entrypoint and command dispatch.
- `src/config.rs`: config schema, parser, validation, serializer.
- `src/importer.rs`: ACNET XML import.
- `src/monitor.rs`: live TUI stream monitor runtime.
- `src/capture.rs`: raw synchronized spill capture and diagnostics.
- `src/analyze.rs`: live/offline tune analysis, studies, batch outputs.
- `config/monitor.cfg`: generated/example config.
- `docs/`: user, architecture, physics, planning, and workflow docs.

## Development Checks

Run from the repository root:

```bash
cargo fmt --all
cargo test -- --nocapture
```

For focused timing/selection checks:

```bash
cargo test choose_target_millisecond -- --nocapture
cargo test historical_candidate_ranking -- --nocapture
```
