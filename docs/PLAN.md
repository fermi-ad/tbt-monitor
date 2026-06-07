# Tune Analysis Plan (Mapped to Implementation)

This document translates the methodology from:
- `ChatGPT - Synchrotron Tune Calculation plan.pdf`

into an implementation-facing roadmap and a gap report.

## Purpose

Use synchronized Delivery Ring BPM TbT position streams to estimate `Qx` and `Qy`:
- first in a robust injection window
- then across sliding windows versus time
- with quality/timeliness diagnostics
- and eventual comparison against Schottky measurements

## Plan Summary (from PDF)

1. Treat each spill as a synchronized multi-BPM snapshot.
2. Start with a configurable early-time window near injection.
3. Remove per-BPM closed-orbit offsets before spectral analysis.
4. Combine many BPMs to improve SNR (spectrum average, SVD/PCA, etc.).
5. Estimate tune by peak search in expected bands with confidence/uncertainty.
6. Extend to sliding windows for tune-vs-time.
7. Validate against Schottky in matched time slices.

## Plan vs Implementation

Legend:
- `Implemented`: in current code path and outputs.
- `Partial`: available but not fully aligned with PDF intent.
- `Not Yet`: not currently implemented.

### 1) Synchronized multi-BPM spill snapshot

Status: `Implemented`

What exists:
- Global spill snapshots across all configured streams.
- Stream-ID alignment logic using target millisecond selection.
- Adjacent-bucket tolerance (`±1 ms`) for both live and historical/no-beam paths.

Notes:
- This was added to reduce split-target artifacts (for example `96/24` across neighboring ms).

### 2) Configurable early-time injection window

Status: `Implemented`

What exists:
- Configurable `injection_start_turn` and `injection_window_turns`.
- CLI overrides for analysis commands.

### 3) Per-BPM mean removal / preprocessing

Status: `Implemented`

What exists:
- Mean subtraction per trace/window.
- Hann windowing before FFT.
- DC suppression (`bin 0 = 0`) and low-bin exclusion in peak search.

### 4) Multi-BPM combination strategy

Status: `Partial`

What exists:
- Multi-BPM averaging paths and per-BPM method comparison artifacts.
- Weighted/unweighted analysis options in study workflows.

Divergence:
- SVD/PCA path is explicitly deferred in current implementation.
- Phase-aware lattice combination is not implemented.

### 5) Tune extraction + confidence/uncertainty

Status: `Partial`

What exists:
- Band-limited peak pick with confidence gate (`min_peak_confidence`).
- Per-plane confidence metrics and quality flags.

Divergence:
- No full uncertainty model yet (for example statistical confidence intervals).
- No dedicated peak-width metric exported as a first-class field.

### 6) Sliding-window tune vs time

Status: `Implemented`

What exists:
- Configurable sliding windows/stride.
- Optional flashpoint sampling mode (`--flashes N|max`) for evenly spaced
  fixed-count windows across spill depth, bounded by available turn count.
- In flash mode, injection tune uses `sliding_window_turns` (not
  `injection_window_turns`).
- Tracked local peak logic with fallback and suspicious-step diagnostics.
- Per-spill tune-vs-time plot and sliding CSV output.
- Batch flash-index trend and histogram plots
  (`tune_vs_spill_flash_XX.png`, `tune_histogram_flash_XX.png`) when flash
  sampling is enabled.

### 7) Validation against Schottky

Status: `Partial`

What exists:
- External reference matching in batch mode via reference files.
- Residual plot generation when references are present.

Divergence:
- No direct Schottky data ingestion/auto-sync pipeline in this repository.
- Matched-slice comparison requires external preprocessing and reference-file creation.

### 8) Signal quality checks requested in PDF

Status: `Partial`

What exists:
- Alignment fraction diagnostics.
- Incomplete-poll warnings and quality flag (`INCOMPLETE_TBT_POLL`).
- Timeliness statistics (`obs_ms - target_ms`) at spill and batch levels.

Divergence:
- Explicit clipping/saturation detection is not yet implemented.
- Spectral coherence metric across BPMs is not yet exported as a dedicated statistic.

### 9) Full-spill coverage assumption vs available TbT length

Status: `Partial`

What exists:
- The implementation analyzes whatever turn depth is present in payloads and reports window-consensus constraints.

Divergence:
- The PDF notes a potential mismatch between nominal spill duration and currently available TbT payload depth.
- The code does not yet enforce or report a dedicated "coverage of nominal spill duration" metric; it works on available turns.

## Current Priorities

1. Keep synchronized capture robust under real-world timestamp jitter.
2. Preserve least-transformed RAW position payloads for offline reanalysis.
3. Capture auxiliary RAW intensity payloads so later quality studies can compare
   beam/intensity behavior against position traces.
4. Preserve diagnostics even when data is incomplete (warn/flag instead of silent drop).
5. Provide timing observability so data freshness/jitter can be trended.
6. Keep outputs compatible with external reference validation workflows.

## Next Milestones

Post-split analysis refinement:

1. Add explicit spectral-coherence and clipping diagnostics.
2. Export peak-width and uncertainty-oriented metrics in summaries/CSV.
3. Add optional SVD/PCA-based tune extraction path for side-by-side comparison.
4. Add a first-party Schottky reference ingestion path (or converter contract) to reduce manual matching.

The acquisition/offline-analysis split is tracked in
`docs/ISSUE_MAP_DAQ_SPLIT.md`.

Completed split work now covers versioned captured-spill bundles, live
one-shot/free-run capture, same-spill DAQ diagnostics with explicit timestamp
distributions, `assess`, `diagnose-captures`, offline single/batch
captured-bundle analysis, a minimal online/offline parity guardrail, and
auxiliary RAW intensity capture derived from configured RAW position streams.
See `docs/USAGE.md` for command usage and `docs/ISSUE_MAP_DAQ_SPLIT.md` for the
issue history. The parity guardrail is a split regression check, not physics
certification of the current algorithm.

## Open Physics Questions (still external)

1. Preferred injection-time window definition for operational reporting.
2. Final tune search bands by plane for production operation.
3. Whether passive coherent motion is sufficient in all operating regimes.
4. Expected tune drift scale versus window size, especially during extraction dynamics.

## Revision Discipline

When analysis behavior changes, update this file and classify impact as one of:
- `matches plan better`
- `acceptable divergence`
- `new divergence requiring review`
