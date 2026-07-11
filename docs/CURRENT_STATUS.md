# Current Publication Handoff

Last updated: 2026-07-11 after the local clock correction.

This file records the live publication run state. Permanent behavior and
rationale remain in `docs/ARCHITECTURE.md`, `docs/DESIGN_DECISIONS.md`, and
`docs/PHYSICS.md`.

## Active Objective

Complete every publication requirement in `NEXT_STEPS.md`, then deliver and
locally package:

- a corrected, verifier-clean Best-1/3/5 analysis and all downstream sidecars,
- a leakage-controlled contiguous Best-N study with convergence and sensitivity
  evidence,
- a same-protocol Best-N versus all-training mean/median control,
- the corrected 200-spill intensity test,
- the exact-paired 50000-turn legacy-versus-adaptive ridge gallery,
- an editable Fermilab-template A0 poster and rendered PDF/PNG,
- a four-page JACoW IBIC2026 paper and rendered PDF,
- scoped merged PRs and a clean repository.

## Accepted Primary And Active Best-N Run

```text
accepted primary root: /home/derekste/best_bpm_mining_20260709_corrected_best135
active Best-N root: /home/derekste/best_n_20260710_full40
active continuation code: /home/derekste/tbt-monitor-publication-code-25c41237
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

Connected checkpoint after the system clock correction:

```text
full-run COMPLETE marker: present
sensitivity COMPLETE marker: absent
sensitivity matrix: all seven evaluator outputs verified; final coordinator exited 1
failure: beam-width comparer received beam16/beam32/beam64 labels instead of integers
active CUDA evaluators: none
MemAvailable at the connected probe: 119928056 KiB
payload-audit/intensity/ridge/ANALYSIS_COMPLETE markers: absent
memory watchdog: clear
```

This is an orchestration failure after the expensive work, not a failed science
run. `sensitivity_run_manifest.csv` contains exactly seven `verified` rows and
there is no memory-abort marker. The local matrix runner now translates only the
beam-width comparer labels to `16/32/64`. Recovery stages a new source hash and
runs the same matrix with `--resume`; no evaluator may be recomputed unless its
existing parameter contract or strict verification fails.

The machine clock was corrected by roughly 80 minutes during this run. Hashes,
row counts, and Linux process elapsed time remain valid; wall-clock log stamps
and evaluator `elapsed_seconds` fields that span the correction must not be used
for rate or duration claims.

The accepted same-metric fixed-control CSV also exposed a publication-reporting
omission: all-BPM mean/median were present in the table but absent from the
executive comparison and summary PNG. They are the strongest descriptive
controls in both planes. The current local branch restores those rows and
narrows the claim to Best-N versus adaptive Best-1 and frozen small sets. The
Spark continuation remains correctly pinned to `25c41237` because no waveform
or GPU science path changed; after transfer, regenerate the control PNG and
final report/poster/paper locally from the accepted CSV with the newer branch.

The local publication renderer now also regenerates poster/paper Best-N panels
from that accepted summary. Those panels isolate blind full-band agreement on a
shared zero-based H/V scale; conditioned near-training agreement remains in a
separate diagnostic PNG. A criterion-by-N matrix renders the six exact gates
and all-gates result used by the recommendation. This is reporting-only and
does not change the Spark selection, validation rows, recommendation, or active
continuation.

A second reporting-only matrix now probes nearby gate margins after the
declared analysis. Across the 27 agreement/power-floor combinations per plane,
H selects Best-13 at a 0.01 blind-agreement margin, Best-5 at the declared 0.02,
and is unresolved at 0.03; selected/held-out power-floor variation does not
change those outcomes. V remains finite at Best-18, Best-12, and Best-10 for
the same three agreement margins across all nine power-floor pairs. This
supports a V low-to-mid-teen region and records H criterion sensitivity; the
boxed 0.02/95%/90% cell remains the publication selector.

The local branch now implements the final all-BPM question as a separate
CPU/cache-only control. It reuses the accepted block-20 validation cache keys,
fit/test purge, digitizer folds, tune tolerance, and block length; all
training-side channels are aggregated by mean and median while held-out
digitizers remain independent. The strict contract requires 10000 detail rows,
8000 fold-collapsed spill pairs, 16 method/metric comparisons, four summary
rows, and 18 native PNGs. Publication preparation and finalization require the
accepted control but do not require Best-N to win. The synthetic end-to-end
test, PNG inspection, publication-binding test, and exact four-page offline
paper smoke pass. Do not start the full control until the current GPU chain is
idle.

The current template-derived A0 poster also passes an offline layout stress
build using deliberately long current copy for the sensitivity, full-buffer,
all-training, intensity, and H-loss statements. The editable PPTX has no
reported overflow, the template-fidelity check reports zero issues, and the
single-page A0 PDF has embedded/subset fonts. Full-size inspection confirms the
expanded conclusion fits cleanly. This remains layout proof only: the final
poster must be rebuilt and visually reviewed with verifier-bound real-data
figures and exact accepted counts.

The immutable post-chain handoff is prepared locally:

```text
source commit: cf43cb1d9277443a6bece1d310d692f8f59c4467
source archive: review-artifacts/publication-run-handoff/tbt-monitor-publication-code-cf43cb1d.tar.gz
source archive SHA-256: c07e3e2da4689ade5d0e25baf7a8b0f0d44863a2ff8f89243bc14bf798259b7c
post-chain wrapper: review-artifacts/publication-run-handoff/spark_all_training_post_chain_cf43cb1d.sh
post-chain wrapper SHA-256: b2d47c9467382a7b410b58c2d95e5cb686b2626be61cec705661472774871772
```

The wrapper waits for `ANALYSIS_COMPLETE` containing exact prerequisite commit
`25c41237`, verifies and extracts the archive, reruns the local gates on Spark,
waits for at least 48 GiB available memory, executes one resumable evaluator
under a three-sample 32 GiB abort floor, verifies the 24-file result inventory,
regenerates the executive report and publication materialization, rebuilds and
verifies the source-side review package, and writes commit-bound
`FINAL_ANALYSIS_COMPLETE`. It does not overlap the active GPU stages.

The separate deferred autosweep acceptance smoke is also prepared locally:

```text
wrapper: review-artifacts/publication-run-handoff/spark_autosweep_parallel_smoke_cf43cb1d.sh
wrapper SHA-256: 059258579881d539911033c7b5b54c0d704d6ddec67ae12540b16a8084901886
```

It waits for the exact `FINAL_ANALYSIS_COMPLETE` marker from `cf43cb1d`, verifies
the autosweep, telemetry, and analyzer file hashes in that staged source,
requires three consecutive 48 GiB `MemAvailable` preflight samples, and aborts
the two-job process group after three consecutive samples below 32 GiB. The
receipt must contain exactly two successful run rows and telemetry observing at
least two compute processes before ENG-021 and issue #30 can close. It must not
overlap the all-training control merely because the GPU appears idle.

Connectivity and forwarded Kerberos credentials are restored. A bounded
`ssh -K spark` probe found the payload-audit coordinator and publication tail
alive in separate sessions, both waiting on the missing sensitivity marker. The
GPU was idle and no evaluator, intensity, ridge, all-training, or autosweep
process was active. Resume the corrected comparison first; the existing marker
chain then remains authoritative. Continue to reconcile by marker, source hash,
strict verifier, and row count rather than wall-clock stamps.

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
4. `/home/derekste/spark_publication_tail_25c41237.sh` holds the final launch
   lock and waits for the payload-audit marker. It requires passing
   10/20/40-block Best-N reports, four OK transfer rows, all seven verified
   sensitivity runs, and eligible H/V recommendations in a strict majority of
   those runs. Every unavailable run and reason remains publication evidence.
   It then propagates the accepted full-data plane-specific N values into the
   exact intensity and ridge unions, runs all four intensity shards sequentially,
   verifies the three intensity block summaries, runs the full 50000-turn ridge
   gallery serially, materializes the publication inputs, and creates a
   source-side review archive. The 32 GiB three-sample memory watchdog remains
   active throughout the GPU stages.
5. A post-chain all-training launch is intentionally not attached yet because
   the implementation did not exist in source commit `25c41237` and local
   connectivity is absent. Its watcher must wait for the exact commit-bound
   `ANALYSIS_COMPLETE`, use the newly staged source hash, run serially, verify
   every table and PNG, and rematerialize/repackage publication inputs with the
   new required `--all-training-root`.

The final continuation is detached as PID/PGID/SID `869490` in its own session
and process group. Its script SHA-256 is
`eebb60e4dbab0604cc9f92f9d062a29ab88a3bce394e56e6a61b8bd12a7728ef`.
It uses source commit `25c41237`, extracted only after the archive matched
SHA-256 `04c48280432947f27325d518aee5c7e7fc800f36909ec847835a29866564a018`.
The exact staged source passed all 71 Best-BPM tests, all nine autosweep tests,
and the poster self-test on Spark. The immutable stage receipt records all
three source/archive/wrapper identities. The watcher log records the same
source and archive identities and is waiting for the payload-audit marker.
No intensity or ridge computation can begin merely because the earlier marker
appears; the selected-N and sensitivity preflight must pass first.

The latest publication source is commit `25c41237`. Its prepared archive
SHA-256 is
`04c48280432947f27325d518aee5c7e7fc800f36909ec847835a29866564a018`,
and its prepared continuation-wrapper SHA-256 is
`eebb60e4dbab0604cc9f92f9d062a29ab88a3bce394e56e6a61b8bd12a7728ef`.
The archive and wrapper plus the guarded stage/swap helpers match those local
bytes on Spark. The exact archive locally passes all 71 Best-BPM tests, all nine
autosweep tests, and the poster self-test. The same tests pass in the extracted
Spark source, and the hash-bound stage receipt enabled the guarded watcher swap.
This version retains the earlier exact-paired metrics and complete turn grids
for every adaptive N pair plus the zero Best-1 self-control, binds the
paper/poster width contrast to selected Best-N minus corrected Best-1, and
strictly verifies the new rows and figures. It also includes exact-zero intensity control labels,
common/detail gallery scales, citation-order polish, shorter ridge diagnostic
labels, the read-only final-PPTX empty-placeholder gate, preserved poster
layout/overflow/template-fidelity evidence, portable poster/paper build
manifests, and both flagged and unflagged macOS Bash 3.2 Tectonic invocation
paths. Publication preparation now records exact numerical source-table hashes,
and finalization re-hashes the fixed 14-file materialized-output inventory.
The wrapper accepts an existing ridge root only after the current strict
verifier passes, preserves any incompatible ridge/publication tree under a
timestamped `.incomplete` name, and trusts `ANALYSIS_COMPLETE` only when it
contains the exact source commit.
It also treats a verified sensitivity run without an automatic knee as an
explicit result: at least four of seven recommendations per plane are required,
and payload/poster/paper/compliance copy carries every unavailable reason and
the observed N range.
The wrapper now also reruns the exact review-package verifier against every
copied path, hash, and gallery image before it creates the source-side tarball.
That package includes the exact analysis source tree/archive and continuation
script, corrected primary/follow-up/handoff visual directories, and all new
Best-N, sensitivity, ridge, intensity, payload, report, and publication roots.
The final local repack must additionally include the complete 80-image
`review-artifacts/poster_candidate_gallery` component. It is intentionally
ignored by Git and absent from the Spark source archive; its two selected H/V
ridge references match the immutable hashes recorded in
`publication/ibic2026/LEGACY_RIDGE_PROVENANCE.md`.
The required `review-artifacts/publication-run-handoff` component also preserves
exact copies of the accepted abstract, supplied Fermilab POTX, audited poster
starter/layout/preview, coherent offline Tectonic bundle, source archive,
guarded wrappers, and prepared GitHub text under one checksum manifest.
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
    weighted signal `adds`/`suppresses`, and
12. an executive summary and summary PNG that omitted stronger all-BPM controls
    already present in the corrected same-metric table.

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
All-BPM mean/median now remain beside adaptive and frozen N=1/3/5 in both the
executive text and a detached, explicitly descriptive native-PNG panel.

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

- At the latest post-correction local check, DNS still did not resolve either
  `api.github.com` or `outland.fnal.gov`, and `klist` reported a credential-cache
  I/O failure after the clock correction. Do not infer a Spark failure from
  local connectivity. Renew Kerberos only after network service returns, then
  reconcile the detached chain from markers, process identity/elapsed time,
  hashes, and row counts rather than cross-correction wall-clock stamps.
- Local to Spark: use the configured `spark` alias with bounded SSH options and
  `ClearAllForwardings=yes`; the alias uses `ProxyJump outland.fnal.gov`.
- Spark to raw capture host: delegated Kerberos works with `ssh -K drbpm1`.
- Spark internet/package downloads must use the established outland proxy or be
  staged through outland; do not point `pip` directly at the internet.
- Package artifacts on their source host first, then copy the simplest complete
  directory/archive locally.

## Current Local Validation

```text
Best-BPM Python tests: 73 run, 67 passed, 6 process-pool tests skipped by local sandbox
Autosweep Python tests: 9 passed
Rust tests: 44 passed
GPU analyzer self-test: passed
poster/DGX self-test: passed
git diff --check: passed
current A0 template frame map: 0 validation issues; full A0 smoke passes overflow, fidelity, PDF/font, and branded PNG identity gates
template-derived smoke PPTX: 0 empty structural placeholders under the finalizer's read-only slide-XML gate
poster build provenance smoke: portable 10-entry manifest, audited starter hash, delivered layout/inspection, 0 fidelity issues
paper build routing smoke: flagged and unflagged shell paths plus portable 9-entry manifest pass
review-package transfer smoke: 13 files, 3 images, exact manifest/gallery/receipt verification passes; same-size tampering is detected
JACoW layout smoke: the current manuscript is exactly four 595 x 792 bp pages after keeping the noncausal H-loss diagnostic in the poster/gallery; no overfull boxes or unresolved references, and all fonts are embedded/subset/Unicode-mapped
accepted references: abstract and poster-template hashes reverified; manuscript title and abstract match abstract 54
local branch: `dev/ibic2026-final-delivery` contains unpushed reporting, layout, and provenance commits; inspect `git log origin/dev/ibic2026-final-delivery..HEAD` after reconnecting
accepted-summary gate-margin CSV and H/V native PNGs: rendered and visually inspected; declared cell and post-selection guardrail present
```

The six process-pool probes passed on Spark in the accepted staged source tree.
The stricter intensity-pairing gates were also streamed over the completed
199-spill tables: all four methods had identical hashes for 288000 window keys
and 3200 spill keys apiece, with zero nonfinite global picks, zero center-grid
errors, and zero membership mismatches.
The poster and paper smoke outputs prove layout only; final real-data builds and
full-size visual QA remain required. No final physics claim, poster panel, or
paper number may come from a provisional June downstream artifact.
