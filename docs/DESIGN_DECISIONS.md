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
- Render `tune_vs_time` with horizontal `0.1`-spacing Y-grid lines.

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
- Use tune on X, time on Y (from `turn_period_us`), and normalized log spectral
  power for color intensity.
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

## Decision Update Rule

When changing one of these decisions, update:
1. this file,
2. `docs/ARCHITECTURE.md`,
3. `PLAN.md` (if plan alignment changes), and
4. `README.md` for user-visible behavior changes.
