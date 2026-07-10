# Current Publication Handoff

Last updated: 2026-07-10 03:12 CDT.

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

## Corrected Spark Run

```text
post-run code: /home/derekste/tbt-monitor-publication-code-20260710-final
root: /home/derekste/best_bpm_mining_20260709_corrected_best135
python: /home/derekste/venvs/cupy-spark-cu13/bin/python
data: two 1000-spill position-only collections, 120 exact channels
```

The active subset process was launched from the earlier staged code tree. The
corrected Best-1/3/5 subset phase is active on one NVIDIA GB10 with four
workers. Do not launch another GPU-heavy pass or alter this root until that phase
finishes. After completion, extract the staged code archive into the new empty
code directory, run the full
59-test Python suite on Spark, repair only the visibility-duration field, run
the canonical downstream phases explicitly, and then run the corrected
fixed/held-out/artifact/handoff sidecars and verifiers. Do not invoke the full
pipeline with `--resume`: that flag reuses caches but does not skip completed
subset search.

A bounded probe at 01:25 CDT showed all shards at 49.3-50.4% and advancing,
with 95% GPU utilization, 38.39 W power draw, and an observed-rate completion
estimate near 06:45 CDT.
The single-instance continuation watcher is active as PID `778510`; it waits for
the parent and every subset-search worker before running the corrected downstream
chain. The frozen source tree passed all 59 repository Python tests before
archive construction. The regenerated archive will be clean-extracted and
retested, then extracted into the new `20260710-final` code directory rather
than overlaid on the earlier tree. Its local and remote SHA-256 values must be
recorded in the transfer verification output before the watcher reaches the
continuation; an archive cannot contain its own stable checksum.
The exact 142 MB legacy `gpu_sliding_tune.csv` is
already on Spark under the `18d321db` combined output; `ssh -K drbpm1` remains
available only if another source artifact must be recovered.

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
    draws.

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
Python tests: 59 run, 53 passed, 6 process-pool tests skipped by local sandbox
Focused publication tests: 50 run, 44 passed, same 6 local sandbox skips
Rust tests: 44 passed
GPU analyzer self-test: passed
poster/DGX self-test: passed
git diff --check: passed
clean-extracted source archive: same 59 Python tests and 44 Rust tests passed
JACoW draft: unchanged official class compiles with Tectonic and official TeX Gyre Termes fonts; current text-only scaffold is 3 A4 pages and visually clean
```

The six process-pool probes must pass on Spark. No final physics claim, poster
panel, or paper number may come from a provisional June downstream artifact.
