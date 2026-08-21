# Operations

This guide collects build, Docker, validation, host, and GitHub workflow notes.
Subsystem-specific commands live in [DAQ Guide](DAQ.md),
[Analysis Chains](ANALYSIS_CHAINS.md), and [Spark Workflows](SPARK.md).

## Local Build And Help

```bash
cargo check --offline
cargo run --offline -- --help
cargo run --offline -- <command> --help
```

Focused Rust checks:

```bash
cargo fmt --all
cargo test -- --nocapture
cargo test choose_target_millisecond -- --nocapture
cargo test historical_candidate_ranking -- --nocapture
```

Python checks:

```bash
python3 scripts/bpm_dgx_poster.py --self-test
python3 scripts/gpu_analyze_captured_spills.py --self-test
python3 scripts/test_autosweep.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/best_bpm_mining
```

## Docker

Build for linux/amd64 from Apple Silicon:

```bash
docker build --platform linux/amd64 -t tbt-monitor:amd64 .
```

Run the TUI:

```bash
docker run --rm -it \
  --name tbt-monitor \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  tbt-monitor:amd64 \
  monitor --config /app/config/monitor.cfg
```

Bind-mount output for capture or analysis:

```bash
RUN_DIR="$PWD/out"
mkdir -p "$RUN_DIR"
docker run -it \
  --name tbt-capture \
  --network host \
  -v "$RUN_DIR:/out" \
  -v /path/to/monitor.cfg:/app/config/monitor.cfg:ro \
  tbt-monitor:amd64 \
  capture-spills --config /app/config/monitor.cfg --out-dir /out --free-run --count 25
```

Use no `--rm` if you might need `docker cp` fallback extraction after the
container exits.

## Host Workflow Notes

- Use `ssh -K` for Fermilab host-to-host access.
- If direct access to `spark.fnal.gov` fails from the local machine, route via
  `drbpm1`.
- Keep long Spark jobs resumable with explicit output directories and
  `--resume`.
- Do not interrupt active acquisition or Spark runs unless the process is
  clearly failed, stalled, or the operator asks for it.
- For live DAQ collection, create and check `RUN_DIR` before starting Docker so
  the bind mount cannot collapse to `:/out`.

## Output Verification

Best-BPM output packages have a structural verifier:

```bash
python3 scripts/verify_best_bpm_outputs.py --root /path/to/best_bpm_mining
```

GPU telemetry CSVs can be summarized after a run:

```bash
python3 scripts/gpu_run_telemetry.py summarize \
  --input /path/to/gpu_telemetry.csv \
  --summary-json /tmp/gpu_telemetry_summary.json \
  --summary-md /tmp/gpu_telemetry_summary.md
```

Capture directories can regenerate timing diagnostics offline:

```bash
cargo run --offline -- diagnose-captures \
  --bundles-dir /path/to/capture-run \
  --out-dir /path/to/capture-run \
  --same-spill-tolerance-ms 25
```

## Contributing

Use issue-first tracking for substantive work and keep pull requests focused.
Run the relevant Rust and Python checks, update affected documentation, and keep
generated or review-only artifacts out of commits unless they are intentional
deliverables. See [CONTRIBUTING.md](../CONTRIBUTING.md).
