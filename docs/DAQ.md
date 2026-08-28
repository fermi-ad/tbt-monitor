# DAQ Guide

This guide explains how `tbt-monitor` reads BPM data and saves one coherent
accelerator spill for later analysis. It covers configuration import, live
monitoring, raw capture, preflight checks, and capture-quality diagnostics. The
executable used for these commands is `tbt-monitor-tui`. Analysis workflows are
covered in [Analysis Chains](ANALYSIS_CHAINS.md).

## Responsibilities

The DAQ path answers acquisition questions before tune interpretation starts:

- Are all configured BPM streams readable?
- Did every configured stream provide a payload for the same machine event?
- Which streams or digitizers were missing, stale, ahead, malformed, or
  unreadable?
- What was the exact timestamp distribution relative to `target_ms`?

Captured raw bundles are the durable handoff between acquisition and analysis.
They should be kept complete enough that future analysis can be rerun without
Redis connectivity.

## Configuration

Generate monitor config from ACNET XML:

```bash
cargo run --offline -- import \
  --source /path/to/Config.xml \
  --output config/monitor.cfg
```

Useful import-time runtime overrides:

```bash
cargo run --offline -- import \
  --source /path/to/Config.xml \
  --output config/monitor.cfg \
  --xread-block-ms 1000 \
  --reconnect-initial-ms 2000 \
  --reconnect-max-ms 30000
```

`MonitorConfig::validate()` remains the runtime safety gate. Config keys and
defaults are documented in [Config Reference](CONFIG_REFERENCE.md).

## Live Monitoring

Run the TUI stream monitor:

```bash
cargo run --offline -- monitor --config config/monitor.cfg
```

The monitor uses Redis `XREAD BLOCK`, one worker per device, and reconnect
backoff. TUI controls are `q` to quit and `up/down` or `j/k` to select devices.

## Raw Spill Capture

Capture one synchronized spill:

```bash
cargo run --offline -- capture-spill \
  --config config/monitor.cfg \
  --out-dir out
```

Capture continuously and stop after `N` successful bundles:

```bash
cargo run --offline -- capture-spills \
  --config config/monitor.cfg \
  --out-dir out \
  --free-run \
  --count 25
```

Capture selects a Redis stream-ID millisecond `target_ms`, then reads entries
within `same_spill_tolerance_ms` (default `25 ms`). Exact timestamp offsets are
always recorded. A few milliseconds of spread is normal; a complete acquisition
means every configured stream has a payload inside the same-spill tolerance.

Each bundle contains:

- `spill_<target_ms>/manifest.json`
- `spill_<target_ms>/capture_summary.txt`
- `spill_<target_ms>/payloads/*.bin`

Free-run capture also writes run-level files:

- `capture_index.csv`
- `capture_spill_diagnostics.csv`
- `capture_stream_diagnostics.csv`
- `capture_timestamp_distribution.csv`
- `capture_digitizer_diagnostics.csv`
- `capture_quality_summary.json`
- `capture_quality_report.md`

## Capture Diagnostics

Run a non-capturing preflight check:

```bash
cargo run --offline -- assess \
  --config config/monitor.cfg \
  --out-dir out/assess \
  --events 1 \
  --same-spill-tolerance-ms 25
```

`assess` reads latest stream IDs, watches for one or more new machine events,
then re-reads latest IDs without writing payload bundles. It writes:

- `assess_streams.csv`
- `assess_digitizers.csv`
- `assess_summary.json`
- `assess_report.md`

Regenerate capture diagnostics from existing bundles without Redis:

```bash
cargo run --offline -- diagnose-captures \
  --bundles-dir out \
  --out-dir out \
  --same-spill-tolerance-ms 25
```

## Quality Semantics

Captured-payload quality and latest-ID poll diagnostics are intentionally
separate:

- `COMPLETE`: all configured streams had captured payloads within tolerance.
- `MISSING_CAPTURE`, `STALE_CAPTURE`, `AHEAD_CAPTURE`: captured payload was
  absent or outside the target bucket.
- `PAYLOAD_MISSING`, `PAYLOAD_MALFORMED`: a captured entry existed, but the raw
  payload could not be used as expected.
- `LATEST_STALE`, `LATEST_AHEAD`, `LATEST_MISSING`: latest-ID snapshot
  diagnostics only.
- `LATEST_STALE_BUT_CAPTURED_OK`: latest polling looked stale, but the
  near-target raw payload was captured correctly.
- `CONNECT_ERROR`, `READ_ERROR`: Redis access failed.

For tune studies, prefer captured-payload completeness over latest-poll
alignment. Latest-poll staleness is still valuable for identifying suspect
digitizers, but it should not reject a complete captured artifact by itself.

## Timestamp Distributions

`capture_timestamp_distribution.csv` has one row per spill/source/delta bucket:

- `source=captured_payload`: timestamps for entries written to payload files.
- `source=latest_id_snapshot`: latest Redis IDs observed during target
  selection.
- `delta_ms`: `stream_timestamp_ms - target_ms`.
- `stream_count`: number of streams in that bucket.

Machine events are nominally 15 seconds apart. A `delta_ms` near `-15000`
usually means that stream or latest-ID observation is one event stale, not
merely a few milliseconds misaligned.

## Operational Notes

When running in Docker, bind-mount both config and output paths. Avoid leaving
`RUN_DIR` empty; Docker interprets `-v ":/out"` as an invalid mount spec.

```bash
RUN_DIR="$HOME/out/tbt-capture-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
docker run -d --name tbt-capture \
  --pull always \
  --network host \
  -v "$RUN_DIR:/out" \
  -v "$HOME/tbt-monitor/config/monitor.cfg:/app/config/monitor.cfg:ro" \
  adregistry.fnal.gov/instrumentation/tbt-monitor-tui:amd64 \
  capture-spills --config /app/config/monitor.cfg --out-dir /out --free-run --count 500
```

Use [Operations](OPERATIONS.md) for Docker and host workflow details.
