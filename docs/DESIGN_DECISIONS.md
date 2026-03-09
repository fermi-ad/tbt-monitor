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

## Decision Update Rule

When changing one of these decisions, update:
1. this file,
2. `docs/ARCHITECTURE.md`,
3. `PLAN.md` (if plan alignment changes), and
4. `README.md` for user-visible behavior changes.
