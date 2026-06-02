# Issue Map: Acquisition/Analysis Separation

Objective: decouple data acquisition from analysis so complete spill artifacts can be captured once and analyzed offline repeatedly.

Planning stance: split first, refine analysis second. The current tune-analysis
pipeline is a useful proof of concept, not a physics-final contract. The split
should preserve today's behavior well enough for reproducibility, but deeper
analysis validation and quality refinement should not block the captured-spill
artifact work.

Tracking labels:
- `type:feature`
- `area:acquisition`
- `area:analysis`
- `area:infra`
- `priority:P1`
- `state:ready`

GitHub issue numbers should be recorded beside each slice once seeded.

## Epic A: Spill Capture Artifact Contract

### A1. Define spill artifact schema and manifest
- GitHub: #2
- Type: feature
- Area: acquisition,analysis
- Labels: `type:feature`, `area:acquisition`, `area:analysis`, `priority:P1`, `state:ready`
- Acceptance:
  1. Versioned manifest format defined (`schema_version`, run context, target/alignment metadata, stream inventory).
  2. Raw payload storage format documented (per-stream samples + stream IDs + timestamps).
  3. Integrity fields included (checksums/file sizes).

### A2. Add `capture-spill` command (live one-shot)
- GitHub: #6
- Type: feature
- Area: acquisition
- Labels: `type:feature`, `area:acquisition`, `priority:P1`, `state:ready`
- Acceptance:
  1. Captures a synchronized spill from all configured BPM streams.
  2. Writes complete artifact bundle without running tune analysis.
  3. Writes summary metadata with alignment/timeliness warnings.

### A3. Add `capture-spills` command (live free-run/count)
- GitHub: #8
- Type: feature
- Area: acquisition
- Labels: `type:feature`, `area:acquisition`, `priority:P1`, `state:ready`
- Acceptance:
  1. Supports `--free-run` and optional `--count`.
  2. Writes one artifact bundle per unique spill target.
  3. Emits batch capture index file.

## Epic B: Offline Analysis Input Path

### B1. Add offline loader for captured spill bundle
- GitHub: #4
- Type: feature
- Area: analysis
- Labels: `type:feature`, `area:analysis`, `priority:P1`, `state:ready`
- Acceptance:
  1. Loader reconstructs in-memory snapshot from artifact bundle.
  2. Handles malformed/missing fields with explicit warnings/errors.

### B2. Add `analyze-captured-spill` command
- GitHub: #3
- Type: feature
- Area: analysis
- Labels: `type:feature`, `area:analysis`, `priority:P1`, `state:ready`
- Acceptance:
  1. Produces same per-spill analysis outputs currently emitted by `analyze-spill`.
  2. No Redis connectivity required.

### B3. Add `analyze-captured-spills` command
- GitHub: #7
- Type: feature
- Area: analysis
- Labels: `type:feature`, `area:analysis`, `priority:P1`, `state:ready`
- Acceptance:
  1. Batch analysis over captured bundle directories.
  2. Produces current batch artifacts (including flash/waterfall outputs).

## Epic C: Split Guardrails and Post-Split Refinement

### C1. Minimal online-vs-offline parity guardrail
- GitHub: #5
- Type: feature
- Area: analysis,infra
- Labels: `type:feature`, `area:analysis`, `area:infra`, `priority:P1`, `state:ready`
- Acceptance:
  1. Given the same captured spill, online/offline paths match within tolerance
     for key proof-of-concept outputs (`Qx/Qy`, sliding medians, quality flags).
  2. The parity check is a regression guard for the split, not a physics
     validation gate for the current algorithm.

### C2. Post-split coverage-aware quality flagging
- GitHub: #9
- Type: feature
- Area: analysis
- Labels: `type:feature`, `area:analysis`, `priority:P2`, `state:needs-data`
- Acceptance:
  1. Add sliding coverage metrics (valid windows / total windows by plane).
  2. Add quality flag when late-spill tune availability is poor despite good injection quality.
  3. Do after the acquisition/offline-analysis split is usable.

## PR Slice Plan

1. PR-1: Schema + manifest + docs (`A1`).
2. PR-2: `capture-spill` one-shot (`A2`).
3. PR-3: `capture-spills` free-run/count (`A3`).
4. PR-4: Offline loader + `analyze-captured-spill` (`B1`,`B2`).
5. PR-5: Offline batch command (`B3`).
6. PR-6: Minimal online/offline parity guardrail (`C1`).

Post-split refinement:

7. PR-7+: Coverage-aware quality gate and broader analysis refinements (`C2`
   and follow-up analysis issues).

Each PR should close its linked issue and update `docs/ENGINEERING_BACKLOG.md`.
