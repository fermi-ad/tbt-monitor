# tbt-monitor

`tbt-monitor-tui` monitors MUON BPM turn-by-turn Redis streams, captures raw
spill artifacts, and analyzes captured or live spills into tune-review products.

The project is organized around one operational goal: collect complete,
same-spill BPM data first, then evaluate which analysis chain and BPM subsets
produce physically credible `Qx/Qy` evidence for Delivery Ring studies.

## Main Workflows

| Need | Start here |
| --- | --- |
| Configure or operate live DAQ/capture | [DAQ Guide](docs/DAQ.md) |
| Analyze live or captured spills with the Rust tool | [Analysis Chains](docs/ANALYSIS_CHAINS.md) |
| Run Spark/GPU offline analysis, autosweep, or Best-BPM mining | [Spark Workflows](docs/SPARK.md) |
| Build, run in Docker, validate outputs, or work with GitHub issues/PRs | [Operations](docs/OPERATIONS.md) |
| Look up exact CLI flags and command examples | [Command Reference](docs/USAGE.md) |
| Tune config keys | [Config Reference](docs/CONFIG_REFERENCE.md) |
| Understand module boundaries and data contracts | [Architecture](docs/ARCHITECTURE.md) |
| Review physics status and open analysis questions | [Physics](docs/PHYSICS.md), [Analysis Checklist](docs/ANALYSIS_CHECKLIST.md) |
| Review the Delivery Ring raw-stream provenance audit | [Producer And Payload Audit](docs/DELIVERY_RING_SOURCE_AUDIT.md) |

Current capture defaults preserve `TBT_POSITION_RAW` and can derive matching
`TBT_INTENSITY_RAW` payloads for offline study. Spark/GPU workflows include raw
captured-spill analysis, staged autosweep ranking, exact-identity Best-BPM
subset mining, leakage-controlled contiguous Best-N validation, an optional
same-protocol all-training-channel control, a position/intensity sidecar,
full-buffer ridge-density review galleries, and fail-closed publication
verifiers for the primary, Best-N, all-training, intensity, and ridge outputs. A
separate raw-payload gate scans every publication stream through turn
50000 for nonfinite data, sample-count drift, long exact plateaus, and
device-coded threshold fallback pairs.
Publication artifacts use exact channel identities, semantic verifiers, and a
deterministic native PNG renderer for the key deconstruction, handoff,
intensity, Best-N, and ridge figures. `prepare_ibic2026_publication.py` binds
the final plane-specific N, numerical copy, tables, and figure files to the
same accepted analysis roots before the poster or paper can be built. It also
binds the full-curve and stratified-validation sample counts so the two Best-N
populations cannot be conflated in publication copy. All seven reduced-sample
sensitivity runs must verify; at least four per plane must yield an eligible
knee, while every unavailable run and reason remains visible in the payload and
publication copy.
Poster-facing Best-N panels isolate blind full-band selected-versus-held-out
agreement on one shared H/V scale. Near-training-tune agreement remains a
separate conditioned diagnostic and is never overlaid as equivalent evidence.
The reused-window direct-control gallery keeps all-BPM mean/median beside
adaptive and frozen small sets. A separate leakage-controlled control aggregates
every training-side channel by mean and median under the exact Best-N purge and
held-out-digitizer folds. Its outcome may favor either method or remain
unresolved, but it must be reported before materialization.
Subtractive intensity and ridge panels require exact common spill/window
points and describe only ridge-pick probability redistribution; display color
clipping is disclosed and does not alter exported metrics. Native heatmap cells
cover their complete declared axes without floor-division gaps.
`finalize_ibic2026_publication.py` then requires explicit visual-QA passes and
rechecks immutable references, page geometry, payload closure, checksums, and
the final PPTX slide XML for empty structural placeholders before writing the
final compliance report and publication inventory. Build provenance remains in
the delivered tree: portable source/deliverable checksum manifests, poster
layout and overflow inspection, and the zero-issue template-fidelity reports.
The final review packager verifies every copied path, size, hash, and gallery
image both when it is created and again after transfer with `--verify-only`.

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

Capture raw spill bundles for offline analysis:

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

Run the current Spark Best-BPM pipeline:

```bash
/home/derekste/venvs/cupy-spark-cu13/bin/python scripts/run_best_bpm_pipeline.py \
  --config config/best_bpm_mining.yaml \
  --out /home/derekste/best_bpm_mining \
  --device cuda \
  --workers 12 \
  --gpu-telemetry-interval-seconds 30
```

`--resume` reuses completed spectral-cache arrays; it is not a whole-pipeline
stage checkpoint and does not skip subset search. Use the stage-specific
commands in `docs/USAGE.md` when continuing after a completed search.

## Repository Layout

- `src/main.rs`: CLI entry point and command dispatch.
- `src/config.rs`: config schema, parser, validation, and serialization.
- `src/importer.rs`: ACNET XML import.
- `src/monitor.rs`: live Redis stream monitor.
- `src/capture.rs`: raw synchronized spill capture and capture diagnostics.
- `src/analyze.rs`: live/offline tune analysis, studies, and batch outputs.
- `scripts/`: poster/DGX tooling, Spark autosweep, Best-BPM mining,
  verification helpers, verifier-bound IBIC materialization, and browsable,
  checksummed publication-review packaging.
- `config/`: example/generated runtime config.
- `docs/`: subsystem guides, command reference, architecture, physics notes,
  backlog, and workflow docs.

## Development Checks

Run the normal Rust checks from the repository root:

```bash
cargo fmt --all
cargo test -- --nocapture
```

Python analysis helpers also have focused smoke tests:

```bash
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/best_bpm_mining
```

For coding-agent invariants and doc-sync rules, see [AGENTS.md](AGENTS.md).
