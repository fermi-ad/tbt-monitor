# Design Decisions

This file captures major design choices, why they were made, and what tradeoffs they imply.

## DD-001: Stream-native ingestion with `XREAD BLOCK`

Decision:
- Use Redis stream blocking reads (`XREAD BLOCK`) instead of fixed-interval polling loops.

Why:
- Lower idle overhead.
- Better event timing fidelity from stream IDs.
- Natural fit for multi-device wake-driven workflows.

Tradeoffs:
- Requires robust reconnect handling.
- Debugging can be less intuitive than periodic polling.

## DD-002: Global synchronized spill snapshots

Decision:
- Any device wake triggers a full all-device snapshot for analysis commands.

Why:
- Tune extraction quality depends on coherent multi-BPM context.
- Prevents local-device wake bias in tune estimates.

Tradeoffs:
- More per-spill Redis work than single-device reads.
- Snapshot incompleteness must be surfaced as warnings/flags.

## DD-003: Adjacent timestamp bucket clustering (±1 ms)

Decision:
- Treat near-adjacent stream-id milliseconds as one physical target bucket.

Why:
- Real systems often split one spill across neighboring milliseconds.
- Prevents coverage splits and duplicate spill records.

Tradeoffs:
- Slight risk of collapsing distinct events if events are truly 1 ms apart.
- Mitigated by keeping tolerance small and bounded by alignment tolerance.

## DD-004: Keep partial/incomplete snapshots with explicit warnings

Decision:
- Do not hard-fail every incomplete poll; carry warnings and quality flags.

Why:
- Operational debugging needs visibility into degraded states.
- Hard drops hide intermittent infrastructure issues.

Tradeoffs:
- More marginal outputs to interpret.
- Requires clear quality semantics (`INCOMPLETE_TBT_POLL`, low alignment, low confidence).

## DD-005: Confidence-gated peak extraction in expected tune bands

Decision:
- Restrict search to configured tune bands and require minimum confidence.

Why:
- Reduces false positives in low-SNR windows.
- Keeps output physically plausible under noisy conditions.

Tradeoffs:
- Weak but real signals may be marked missing.
- Requires tuning of confidence thresholds by operation mode.

## DD-006: Sliding-window tracking with conservative state updates

Decision:
- Use local-band tracking around prior trusted tune; fallback windows do not reseed tracker.

Why:
- Avoid drift from noisy outliers.
- Preserve smooth physical tune evolution unless evidence is strong.

Tradeoffs:
- Can become conservative during abrupt true tune changes.
- Requires explicit diagnostics for fallback/suspicious windows.

## DD-007: Explicit timeliness metrics as first-class diagnostics

Decision:
- Record and summarize `obs_ms - target_ms` deltas per spill and batch.

Why:
- Synchronization quality should be observable, not inferred.
- Supports longitudinal monitoring of capture jitter/freshness.

Tradeoffs:
- Adds more numbers for users to interpret.
- Requires docs to explain signed vs absolute timing deltas.

## DD-008: Batch outputs in both machine and human formats

Decision:
- Keep `csv/jsonl` records and human-readable markdown/plots.

Why:
- Supports both automated downstream analysis and operator review.
- Simplifies ad-hoc debugging and reproducibility.

Tradeoffs:
- Wider compatibility surface when fields change.
- Requires discipline around output schema evolution.

## DD-009: Standardized tune-plot Y-axis bounds

Decision:
- Use config-defined fixed tune Y-axis bounds (`tune_plot_y_min/max`) for
  tune-valued trend/comparison plots instead of per-plot autoscaling.
- Render `tune_vs_time` with configurable horizontal Y-grid spacing via
  `tune_plot_y_tick_step` (default `0.01`).

Why:
- Visual comparisons across spills/runs are unreliable when each plot autoscales.
- Fixed scaling makes drift/outlier interpretation more consistent for physics review.
- Grid lines improve quick manual readout during operations.

Tradeoffs:
- Out-of-range tune values can clip at plot edges if bounds are too tight.
- Operators must keep configured bounds aligned with current machine regime.

## DD-010: Batch-end composite waterfall generation for `analyze-spills`

Decision:
- Always emit composite horizontal/vertical waterfall plots at the end of
  `analyze-spills` (`--count` successful spills).

Why:
- Physics review needs a single cross-spill view of tune-vs-time evolution.
- Batch-end synthesis reduces manual plot stitching and improves run-to-run review speed.

Tradeoffs:
- Additional plot generation time at batch completion.
- 3D-style projection is a visualization aid, not a substitute for raw CSV records.

## DD-011: Per-spill top-down spectrograms with normalized heat scale

Decision:
- Emit per-spill `spectrogram_h.png` and `spectrogram_v.png` heatmaps.
- Use tune on X, and turns by default on Y; optionally use microseconds when
  `plot_time_axes_in_us=true` (converted via `turn_period_us`).
- Use normalized log spectral power for color intensity.
- Map rows discretely to sliding-window FFT steps (one row per step).

Why:
- Provides a physics-review view of tune evolution without perspective distortion.
- Normalized heat scaling keeps weak/strong structures readable within a spill.

Tradeoffs:
- Heat colors are normalized per plot, so absolute color intensity is not directly
  comparable between different spills without raw-spectrum reference.

## DD-012: Optional success-count stop condition for free-run analysis modes

Decision:
- `analyze-spill --free-run` and `analyze-phase --free-run` accept optional
  `--count N` and stop after `N` successful analyses.
- If `--count` is omitted, free-run remains unbounded (Ctrl-C stop).
- In `--no-beam --free-run`, count targets successful analyses across discovered
  historical candidates; if exhaustion occurs before `N`, the command exits with
  an explicit error.
- In `analyze-spill --free-run --count`, collected spills are also synthesized
  into batch-level summary/composite outputs at exit.

Why:
- Operators need both long-running capture and bounded capture without switching
  command families.
- Using successful analyses (not wake count) keeps stop semantics aligned with
  produced artifacts and downstream batch-style review.

Tradeoffs:
- Historical free-run with strict count can fail when stale depth is insufficient.
- Additional CLI surface requires clear docs to avoid confusion with
  `analyze-spills --count` (which is always required).

## DD-013: Per-spill tune-validation composite artifact

Decision:
- Emit one per-spill composite figure (`tune_validation.png`) combining:
  H/V spectrograms and H/V tune-vs-time panels in a 2x2 layout.
- Spectrogram panels overlay both tracked (`selected_tune`) and raw global tune
  trajectories, with row registration marks by sliding-window step.
- Tune-vs-time panels overlay tracked and raw traces and annotate suspicious-step
  and fallback windows.

Why:
- Physics review needs an immediate visual check that tracked tune follows the
  dominant spectral ridge without opening multiple files.
- Side-by-side H/V and spectrogram/trace views reduce false confidence from
  single-plot inspection.

Tradeoffs:
- Additional per-spill artifact generation cost and output file volume.
- Composite readability depends on balanced panel scaling and label layout.

## DD-014: Optional flashpoint sampling mode for sliding tune extraction

Decision:
- Add `--flashes N|max` to `analyze-spill` and `analyze-spills`.
- When set, sliding-window placement switches from fixed stride to `N`
  evenly spaced centers across spill depth (window size still governed by
  `sliding_window_turns`).
- `--flashes max` automatically resolves to the per-spill maximum supported
  by available turns and `sliding_window_turns`.
- In flash mode, injection tune estimation also uses `sliding_window_turns`
  (ignoring `injection_window_turns`).
- Batch outputs add one tune-trend plot and one histogram per flash index:
  `tune_vs_spill_flash_XX.png` and `tune_histogram_flash_XX.png`.

Why:
- Physics review often needs spill-to-spill comparison at consistent in-spill
  phases (early/mid/late) without over-emphasizing dense sliding trajectories.
- Fixed-count flash sampling provides predictable per-spill sampling while
  preserving existing injection and tracked-sliding analysis logic.

Tradeoffs:
- High requested flash counts can exceed available turn depth for short spills;
  implementation applies per-spill runtime bounds and emits warnings when
  reduced.
- Flash-index plots are indexed by sampling order; center turn can vary slightly
  when spill turn depth varies between captures.

## DD-015: Captured-spill artifact boundary

Decision:
- Separate data acquisition from tune analysis by writing complete, versioned
  captured-spill bundles before offline analysis.
- Keep Redis stream synchronization, target-ms selection, and raw payload
  capture in the acquisition path.
- Keep FFT/tune extraction, quality classification, and artifact rendering out
  of `capture-spill` / `capture-spills`.
- Store Redis stream `_` payload bytes exactly as captured, with a
  `schema_version=1` manifest, stream inventory, stream IDs, sample counts,
  byte counts, and `fnv1a64` checksums.
- Let `analyze-captured-spill` reconstruct the same one-spill analysis snapshot
  from a captured bundle and then reuse the current analysis/output path without
  Redis connectivity.
- Let `analyze-captured-spills` reconstruct snapshots from captured bundle
  directories and reuse the current batch records, quality labels, reference
  matching, plots, waterfall, and markdown summary writers.
- Record offline batch trigger provenance as `trigger_source=captured-spill`
  with `trigger_ms=target_ms`, because Redis trigger keys are not available or
  needed for offline bundle analysis.
- Keep a deterministic online/offline parity guardrail in normal `cargo test`
  that compares proof-of-concept tune outputs, sliding medians, warnings, and
  quality flags for the same raw spill data.

Why:
- Complete spill bundles make analysis reproducible without requiring live Redis
  access or repeated beam-time capture.
- Offline reanalysis supports algorithm and physics-validation iteration on the
  same raw data.
- A manifest boundary makes schema/version changes explicit.
- The split can land before the proof-of-concept tune analysis is physics-final;
  parity checks should guard against accidental behavior drift, not certify the
  current algorithm as final.

Tradeoffs:
- Adds a durable artifact schema that must be versioned and tested.
- Increases disk usage because raw payloads are preserved in addition to plots
  and summaries.
- Requires focused parity tests so online and offline analysis paths do not
  diverge during the split.
- The current implementation duplicates some synchronization helpers from
  `src/analyze.rs`; later parity/refactor work should consolidate shared
  acquisition primitives where it reduces drift without blurring the acquisition
  boundary.
- Offline analysis depends on the manifest schema remaining explicit and
  versioned; schema changes must include loader/test/docs updates.

## DD-016: Same-spill timing diagnostics

Decision:
- Treat capture completeness as a same-spill artifact question, not exact
  millisecond equality.
- Add `same_spill_tolerance_ms` with default `25 ms` for capture lookup,
  duplicate suppression, and DAQ diagnostics.
- Preserve exact timestamp deltas in per-spill manifests and run-level
  diagnostics so jitter distributions can be trended.
- Separate captured artifact quality from latest-poll timing diagnostics.
  `LATEST_STALE_BUT_CAPTURED_OK` is diagnostic context, not a partial capture.
- Add `assess` as a non-capturing preflight and `diagnose-captures` as an
  offline report regenerator.

Why:
- Machine events arrive about every 15 seconds, so millisecond-level timestamp
  spread should be measured instead of treated as missing data.
- Operators need to identify stale digitizers before and during DAQ runs.
- Stable reason codes allow a future strict-fail mode without changing the
  diagnostic schema.

Tradeoffs:
- Manifests and run directories contain more diagnostic metadata.
- There are now two timing concepts: legacy strict `align_tolerance_ms` and
  capture-oriented `same_spill_tolerance_ms`.

## Decision Update Rule

When changing one of these decisions, update:
1. this file,
2. `docs/ARCHITECTURE.md`,
3. `docs/PLAN.md` (if plan alignment changes), and
4. `docs/USAGE.md` for user-visible behavior changes.

Update `README.md` only when the project overview, command map, or documentation
routing changes.
