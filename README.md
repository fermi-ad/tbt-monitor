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
- Capture timing diagnostics, timestamp distributions, and non-capturing
  preflight assessment.
- Offline reanalysis from captured-spill bundles without Redis connectivity.
- One-spill, multi-spill, no-beam, flashpoint, and robustness-study analysis.
- Batch records, quality flags, timeliness metrics, plots, and summaries for
  physics review.
- Standalone BPM-only poster/DGX analysis scripts for collected artifact sets,
  including a CuPy GPU path for raw captured-spill bundles on Spark with
  ridge-density, multitaper, dynamic-programming ridge, SVD/PCA, and benchmark
  poster products.
- Staged BPM autosweep scripts for Spark raw position-only data: manifest and
  health inventory, deterministic pilot/full config sweeps, elite config
  selection, ranking, classification, and summary/artifact packages.
- Best-BPM mining scripts for the 2000-spill Spark dataset: per-BPM spectra,
  within-spill consensus, exact best-1/best-3 searches, screened-pool audited
  best-5/best-10 searches, global statistics, clustering, and reports.

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
| Build poster-analysis products from collected artifacts | `scripts/bpm_dgx_poster.py` | [Poster Analysis](docs/POSTER_ANALYSIS.md) |
| Analyze raw captured spills on Spark/GPU | `scripts/gpu_analyze_captured_spills.py` | [Poster Analysis](docs/POSTER_ANALYSIS.md#raw-captured-spill-gpu-analysis) |
| Run staged Spark autosweep/ranking over raw BPM bundles | `scripts/run_autosweep.py` | [Poster Analysis](docs/POSTER_ANALYSIS.md#spark-bpm-autosweep) |
| Mine strongest BPM subsets from the 2000-spill Spark dataset | `scripts/run_best_bpm_pipeline.py` | [Poster Analysis](docs/POSTER_ANALYSIS.md#best-bpm-2000-spill-mining) |
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

Build BPM-only poster products from a collected artifact tree:

```bash
python3 scripts/bpm_dgx_poster.py run-all \
  --input /home/derekste/out \
  --out poster-artifacts/drbpm1-poster \
  --flashes 128 256 512 \
  --device auto
```

Run GPU-backed flash analysis directly over raw captured-spill bundles:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/gpu_analyze_captured_spills.py \
  --input /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-gpu-upgrade \
  --device cuda \
  --flashes 128 \
  --spectrogram-method both \
  --ridge-method dp \
  --ridge-source-method multitaper \
  --svd-denoise
```

Build and rank a staged BPM-only Spark autosweep package:

```bash
python3 scripts/build_collection_manifest.py \
  --roots /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-183119 \
          /home/derekste/tbt-spills-2000/tbt-capture-positiononly-1000-20260608-231330 \
  --out /home/derekste/tbt-spills-2000-autosweep/stage0
python3 scripts/validate_spill_integrity.py \
  --manifest /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/stage0 \
  --device cuda
python3 scripts/run_autosweep.py \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --mode pilot \
  --spills 200 \
  --max-configs 300 \
  --device cuda \
  --out /home/derekste/tbt-spills-2000-autosweep/pilot
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/pilot
python3 scripts/make_initial_analysis_summary.py \
  --ranking-dir /home/derekste/tbt-spills-2000-autosweep/pilot
python3 scripts/build_elite_full_stage.py \
  --pilot-dir /home/derekste/tbt-spills-2000-autosweep/pilot \
  --dataset /home/derekste/tbt-spills-2000-autosweep/stage0/dataset_manifest.csv \
  --health /home/derekste/tbt-spills-2000-autosweep/stage0/spill_health.csv \
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --expected-usable-spills 1988
python3 scripts/run_autosweep.py \
  --dataset /home/derekste/tbt-spills-2000-autosweep/elite-full/elite_dataset_manifest.csv \
  --mode full \
  --config-list /home/derekste/tbt-spills-2000-autosweep/elite-full/elite_configs_for_full.csv \
  --device cuda \
  --heavy-plots \
  --job-timeout-seconds 900 \
  --out /home/derekste/tbt-spills-2000-autosweep/elite-full
python3 scripts/rank_autosweep_results.py \
  --autosweep-dir /home/derekste/tbt-spills-2000-autosweep/elite-full \
  --min-spills 500
python3 scripts/make_elite_full_summary.py \
  --elite-dir /home/derekste/tbt-spills-2000-autosweep/elite-full
```

Run Best-BPM mining over the same Spark Tier A collections:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --resume
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
- [Poster Analysis](docs/POSTER_ANALYSIS.md): standalone BPM-only DGX/poster
  workflow for complete collected artifact sets.
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
- `scripts/`: standalone BPM-only poster/DGX artifact and raw-spill GPU
  processing helpers, Spark autosweep tooling, and Best-BPM mining passes.
- `config/monitor.cfg`: generated/example config.
- `docs/`: user, architecture, physics, planning, and workflow docs.

## Development Checks

Run from the repository root:

```bash
cargo fmt --all
cargo test -- --nocapture
python3 scripts/bpm_dgx_poster.py --self-test
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
python3 scripts/test_best_bpm_mining.py
```

For focused timing/selection checks:

```bash
cargo test choose_target_millisecond -- --nocapture
cargo test historical_candidate_ranking -- --nocapture
```
