# Current Publication Handoff

Last updated: 2026-07-10 21:32 CDT.

This file records the live publication run state. Permanent behavior and
rationale remain in `docs/ARCHITECTURE.md`, `docs/DESIGN_DECISIONS.md`, and
`docs/PHYSICS.md`.

## Active Objective

Complete every publication requirement in `NEXT_STEPS.md`, then deliver and
locally package:

- a corrected, verifier-clean Best-1/3/5 analysis and all downstream sidecars,
- a leakage-controlled contiguous Best-N study with convergence and sensitivity
  evidence,
- the corrected 200-spill intensity test,
- the exact-paired 50000-turn legacy-versus-adaptive ridge gallery,
- an editable Fermilab-template A0 poster and rendered PDF/PNG,
- a four-page JACoW IBIC2026 paper and rendered PDF,
- scoped merged PRs and a clean repository.

## Accepted Primary And Active Best-N Run

```text
accepted primary root: /home/derekste/best_bpm_mining_20260709_corrected_best135
active Best-N root: /home/derekste/best_n_20260710_full40
full-run science code: /home/derekste/tbt-monitor-publication-code-20260710-final
post-run code: /home/derekste/tbt-monitor-publication-code-3832ee84
python: /home/derekste/venvs/cupy-spark-cu13/bin/python
data: two 1000-spill position-only collections, 120 exact channels
```

The corrected Best-1/3/5 primary and every required fixed, held-out, artifact,
handoff, and report sidecar are complete. Both strict verifiers report zero
failures and zero warnings.

The definitive Best-N study evaluates every N from 1 through 40 over four
logical shards. Four-way CUDA execution exceeded unified memory and forced a
host reboot. The recovery therefore permits at most two evaluators in one
process group under a 32 GiB `MemAvailable` floor with termination after three
consecutive five-second low-memory samples. The two-worker qualification peaked
near 83 GB (77 GiB) host use with about 44 GiB available and mostly 70-96% GPU
utilization.

Checkpoint after the system clock correction:

```text
shard 0: validation 250/250, complete
shard 1: validation 250/250, complete
shard 2: validation 250/250, complete
shard 3: validation 200/250, 40000/50000 rows
memory watchdog: clear
full-run COMPLETE marker: absent
```

The machine clock was corrected by roughly 80 minutes during this run. Hashes,
row counts, and Linux process elapsed time remain valid; wall-clock log stamps
and evaluator `elapsed_seconds` fields that span the correction must not be used
for rate or duration claims.

The full-run source archive SHA-256 is
`588705e83934ffa3a379eaf6b9ab746fb12d8cb5ef2620469eaff71198486a30`.
The post-run source archive is commit `3832ee84`, with SHA-256
`e5557d6512154da0ad4e079cc966ee4186f8eae782d4d744abb7d74f28a01c46`;
the local and Spark copies matched before extraction. Pull request #52 changed
the sensitivity scheduler and its tests/docs, not the full-run evaluator,
merger, verifier, or Best-N science implementation.

## Autonomous Continuation

The active chain is marker-gated and survives loss of the client SSH session:

1. `/home/derekste/spark_bestn_full_v4_two_way.sh` finishes shards 2/3,
   remerges the same rows at 10/20/40-spill block lengths, verifies each merge,
   compares block sensitivity, builds the gallery, and writes the full-run
   `COMPLETE` marker.
2. `/home/derekste/spark_bestn_post_full_3832ee84.sh` holds an advisory launch
   lock, waits for that marker, rejects a watchdog-aborted state, and starts the
   seven-run beam/fit/fold sensitivity matrix with at most two evaluators under
   the same 32 GiB memory floor.
3. `/home/derekste/spark_post_sensitivity_payload_audit_3832ee84.sh` holds a
   separate lock and waits for sensitivity completion before scanning all 2200
   manifests through turn 50000. It writes only under
   `/home/derekste/tbt-publication-20260710/delivery_ring_payload_audit` and
   requires the exact 263999-position-row/23999-paired-row contract before its
   own `COMPLETE` marker.
4. `/home/derekste/spark_publication_tail_c651ed5d.sh` holds the final launch
   lock and waits for the payload-audit marker. Before using the GPU it requires
   passing 10/20/40-block Best-N reports, four OK transfer rows, seven verified
   sensitivity runs, and eligible H/V recommendations in every sensitivity
   run. It then propagates the accepted plane-specific N values into the exact
   intensity and ridge unions, runs all four intensity shards sequentially,
   verifies the three intensity block summaries, runs the full 50000-turn ridge
   gallery serially, materializes the publication inputs, and creates a
   source-side review archive. The 32 GiB three-sample memory watchdog remains
   active throughout the GPU stages.

The final continuation is detached as PID `645280` in its own session and
process group. Its script SHA-256 is
`0b9919b2e1dcfbfde7de343024a201cb44a1784dd20f71ba333ba7ab8ec3934a`.
It uses source commit `c651ed5d`, extracted only after the archive matched
SHA-256 `1754858edbbaf3b0a2437e9fa1163385476d26ae2da2406bcfa71aaa8d9c63d4`.
No intensity or ridge computation can begin merely because the earlier marker
appears; the selected-N and sensitivity preflight must pass first.

The latest local publication source is commit `a323b19c`. Its prepared archive
SHA-256 is
`0d8f3b28d6bd8ae1de9c88b7b4681d7afcb31ed883461b97e3f1b783bf64646a`,
and its prepared continuation-wrapper SHA-256 is
`5de819588eefa86c598c5cd36f2d8d718c83c12a6b797d82f7b2f6709350cdf1`.
That version includes exact-zero control labels, common/detail intensity
gallery scales, citation-order polish, shorter ridge diagnostic labels, and a
read-only final-PPTX empty-placeholder gate. Staging is temporarily deferred by
the local Codex remote-execution approval window until 23:45 CDT, not by Spark
or SSH.
If the older wrapper reaches intensity first, its waveform rows remain reusable;
the expanded gallery and strict verifier can be rerun deterministically without
another GPU waveform pass.

The exact 142 MB legacy `gpu_sliding_tune.csv` is already on Spark under the
`18d321db` combined output. `ssh -K drbpm1` remains available only if another
source artifact must be recovered.

## Publication Audit Findings

The June downstream figures are provisional because the audit found:

1. ambiguous same-digitizer sibling-channel labels in downstream reconstruction,
2. ring order parsed from the first IP-address number, disabling ring span,
3. a legacy normalized-single selector decided by floating-point residuals after
   RMS normalization,
4. a fixed-vs-dynamic plot that mixed two unrelated score definitions,
5. a curated eight-example poster cap filled entirely by V before H was
   considered,
6. an intensity gate that could zero every selected member in a window,
7. visibility duration exported as the complete fit span whenever any fit
   window was visible,
8. several global plots that reused an unrelated inclusion-bar series, a
   cluster-score panel that could be blank, and Pareto/ring axes sourced from
   N/local index instead of compute cost/token ring order, and
9. a continuation command that would rerun subset search despite `--resume`,
   and
10. a 10,000-draw permutation configuration silently capped at 5,000 executed
    draws, and
11. an intensity subtraction renderer that assumed common spill/window rows
    after checking only center turns and labeled probability redistribution as
    weighted signal `adds`/`suppresses`.

Corrected code uses exact source keys and masks, token-derived ring order,
same-metric direct controls, a plane-balanced shortlist, and a nonempty gate
fallback. All weighted methods also use an explicit unweighted fallback when a
window has no usable selected intensity. Block inference is non-circular within
each acquisition collection, and configured permutation draw counts are
executed without a hidden upper cap. The visibility repair exactly reproduces the
canonical cache and changes only duration, with before/after hashes. Scientific
plots now use their named source tables and deterministic native PNG rendering;
semantic verification rejects placeholder, blank, invalid-transition, or
single-plane poster artifacts.
Intensity subtraction now requires exact common finite spill/window keys,
verifies identical method populations and memberships, and labels only
higher/lower column-normalized ridge-pick probability with a display-only
absolute-P99 clip.

## Evidence Protocol

- Best-N selects on a fit-window prefix, purges every overlap, tests later, and
  validates against complete held-out digitizers.
- The primary agreement metric is blind full-band selected-versus-held-out tune
  agreement. Conditioned support is reported separately.
- Beam width, fit-window count, fold seed, bootstrap block length, and
  cross-collection global-N transfer are required sensitivity checks.
- A seven-run shared-baseline sample matrix and strict Best-N verifier are now
  implemented locally. The verifier requires exact N/fold coverage, identities,
  purged timing, finite metrics, matching summaries, transfer/plot products,
  and three larger N values beyond any recommendation.
- Best-N, intensity, and ridge passes write checksummed run contracts before
  science rows. Parameter-changing resume, incomplete or incompatible shard
  sets, and duplicate cross-shard keys fail closed.
- Primary and follow-up verification reconstruct exact result identities,
  memberships, fixed/held-out controls, handoff transitions, and poster PNG
  payloads rather than treating file existence as sufficient evidence.
- Intensity is never multiplied into position. Retention requires FDR evidence,
  a minimum practical effect, median tune stability, 95% spillwise tune
  stability, and agreement at 10/20/40-spill block lengths.
- Intensity density subtraction uses identical exact finite spill/window points;
  red/blue is ridge-pick probability redistribution, not physical denoising.
- Ridge subtraction is exact-point-paired probability redistribution, not
  physical noise removal. H-loss diagnostics do not impose an extraction-onset
  turn.
- Every requested N receives a wide, shared-scale H/V-by-method ridge composite
  sized for the inherited A0 poster frame and two-column paper, while all
  single-plane, subtraction, count, and sample-fraction diagnostics remain in
  the review gallery.

## Access And Handoff

- Local to Spark: use the configured `spark` alias with bounded SSH options and
  `ClearAllForwardings=yes`; the alias uses `ProxyJump outland.fnal.gov`.
- Spark to raw capture host: delegated Kerberos works with `ssh -K drbpm1`.
- Spark internet/package downloads must use the established outland proxy or be
  staged through outland; do not point `pip` directly at the internet.
- Package artifacts on their source host first, then copy the simplest complete
  directory/archive locally.

## Current Local Validation

```text
Best-BPM Python tests: 68 run, 62 passed, 6 process-pool tests skipped by local sandbox
Autosweep Python tests: 9 passed
Rust tests: 44 passed
GPU analyzer self-test: passed
poster/DGX self-test: passed
git diff --check: passed
current A0 template frame map: 0 validation issues; full A0 smoke passes overflow, fidelity, PDF/font, and branded PNG identity gates
template-derived smoke PPTX: 0 empty structural placeholders under the finalizer's read-only slide-XML gate
JACoW layout smoke: exactly four 595 x 792 bp pages, no overfull boxes or unresolved references, all fonts embedded/subset/Unicode-mapped
```

The six process-pool probes passed on Spark in the accepted staged source tree.
The stricter intensity-pairing gates were also streamed over the completed
199-spill tables: all four methods had identical hashes for 288000 window keys
and 3200 spill keys apiece, with zero nonfinite global picks, zero center-grid
errors, and zero membership mismatches.
The poster and paper smoke outputs prove layout only; final real-data builds and
full-size visual QA remain required. No final physics claim, poster panel, or
paper number may come from a provisional June downstream artifact.
