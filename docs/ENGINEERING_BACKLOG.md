# ENGINEERING_BACKLOG

Engineering backlog for fixes, QoL work, maintainability, and hardening.

## Workflow

1. Add new work only in `## Todo` using the item template below.
2. Move an item to `## In Progress` when implementation starts.
3. Move an item to `## Done` only after the doc sync gate in `AGENTS.md`
   is satisfied.
4. Keep `Docs:` explicit so updates are visible and auditable.

## Item Template

Copy this block for each new item:

```md
### [ID] Short title
- Status: todo | in_progress | done
- Owner: <name>
- Type: fix | qol | refactor | reliability | perf | docs
- Why: <problem being solved>
- Scope: <what will change>
- Acceptance: <testable done criteria>
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md
- Validation: cargo fmt; cargo test -- --nocapture
- Notes: <optional>
```

## Todo

None.

## In Progress

### [ENG-038] Delivery Ring producer and raw-payload publication integrity
- Status: in_progress
- Owner: project
- Type: reliability
- Why: the Delivery Ring producer can emit finite device-coded below-threshold values in scaled streams, its bind-mounted Python can drift from the running process, and a finite raw placeholder or plateau could pass ordinary plausibility checks while creating false spectral structure.
- Scope: document the dated read-only drbpm1/drbpm2 topology and live raw/scaled comparison; scan every publication raw payload through turn 50000 for topology, count, finite-data, long exact plateau, and device-coded fallback-pair integrity; bind the exact corpus report into publication preparation and finalization.
- Acceptance: synthetic tests detect paired exact plateaus and fallback runs; the Spark audit covers 2200 manifests, 263999 raw position rows, and 23999 exact raw pairs with zero blocking findings; final materialization rejects a missing, partial, or failed audit.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/DELIVERY_RING_SOURCE_AUDIT.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md, publication/ibic2026/README.md, publication/ibic2026/poster/README.md, publication/ibic2026/paper/README.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/payload_integrity.py scripts/audit_delivery_ring_payloads.py scripts/prepare_ibic2026_publication.py scripts/finalize_ibic2026_publication.py scripts/test_best_bpm_mining.py; PYTHONPATH=scripts PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; strict Spark payload audit and publication preparation pass
- Notes: a same-ID HP101 live event contained zero device-coded values in both raw arrays and 221215 in both scaled arrays, resolving the threshold-placeholder concern for the raw boundary. The loaded process predates the current bind-mounted source, so the exhaustive payload scan remains required evidence.

### [ENG-037] Verifier-bound IBIC publication materialization
- Status: in_progress
- Owner: project
- Type: reliability
- Why: the A0 poster and JACoW paper scaffolds rejected placeholders but had no code path proving that their copy, selected N, tables, and five final figures came from the same accepted primary, follow-up, Best-N, intensity, and ridge roots.
- Scope: render a plane-specific H/V ridge composite when H and V select different N; generate clean selected-N concentration panels and exact-paired per-turn width/entropy/peak/shared-mass contrasts; require the plane choices and turn-contrast grid in the ridge run contract and verifier; materialize poster `content.json`, paper `results_table.tex`, verifier-derived `results_macros.tex`, exact figure copies, a machine-readable results payload, and a checksummed source manifest from accepted roots; bind full-curve versus stratified-validation sample/fold counts from the accepted Best-N verifier; allow valid empty LaTeX option lists while still rejecting unresolved bracket placeholders; expose Tectonic cache/network flags without claiming a partial compile passed; preserve master artwork by deriving the named full-size poster PNG from the PDF raster; require explicit visual-QA acknowledgments before writing the final compliance report and full publication inventory.
- Acceptance: local tests cover mixed H/V rendering and plane-specific publication copy; corrected Spark ridge output contains the contract-bound mixed composite, selected-plane concentration panels, and selected-H/V turn-resolved width contrasts; poster materialization uses the population-level width contrast while retaining selected-spill examples in the review gallery; publication preparation rejects failed reports, mismatched N, any beam/fit/fold sensitivity run without an eligible H or V recommendation, unresolved block sensitivity, retained intensity weighting, missing cross-collection transfer, wrong Best-N sample/fold counts, or undersized images; finalization rejects missing sources, wrong reference hashes, wrong page/render geometry, a named poster PNG that differs from the PDF raster, unresolved payload state, or absent human QA; final A0 poster and four-page paper pass their layout/compliance gates.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md, publication/ibic2026/README.md, publication/ibic2026/poster/README.md, publication/ibic2026/paper/README.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/prepare_ibic2026_publication.py scripts/finalize_ibic2026_publication.py scripts/make_best_bpm_ridge_density.py scripts/bpm_mining/ridge_verification.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; strict Spark ridge and publication preparation pass; A0 PPTX/PDF/PNG visual QA; exact four-page JACoW PDF QA; final publication compliance/inventory pass
- Notes: plane-specific N is allowed because H and V are independent diagnostics. The mixed four-panel image still uses one shared P98-clipped color scale, but visual thickness is compared only within each plane because the tune-band widths differ. Per-turn contrast CSVs are unsmoothed; five-window plot smoothing is visual only, and no contrast metric is labeled as physical noise or extraction timing. Poster copy lists the 4000 full-curve cases, 1000 stratified validation cases, and five held-out-digitizer folds as separate design counts rather than multiplying the validation population by the fold count. The poster uses the author's full name from the accepted abstract; the JACoW manuscript retains its conventional initialized author form. Artifact-tool preserves the supplied master in the PPTX but omits master media from its direct PNG, so the branded PDF raster is the authoritative full-size PNG and the direct render remains a separate diagnostic.

### [ENG-036] Semantic publication artifacts and nonduplicating continuation
- Status: in_progress
- Owner: project
- Type: fix
- Why: subset visibility duration used the whole fit span after one visible window; compatibility plots reused unrelated data or mislabeled local index/N as ring order/compute cost; handoff top-five counts were constant even without visible channels; and the waiting `--resume` continuation would repeat the completed subset search.
- Scope: compute duration only between actually visible windows; repair the active result rows from cached spectra before statistics with hashes; render key poster candidates with the native PNG path; derive every named compatibility plot from its actual table; classify visible-set loss/recovery/empty/handoff states; retain nested strict Top-1/3/5/10 sets and every selected-spill composite; add semantic primary/follow-up verification; invoke only downstream stages after the active search.
- Acceptance: regression tests reproduce and repair duration, empty-set Jaccard is one, recommended poster files are real PNGs without Matplotlib, every required global/per-spill handoff asset exists, explicitly flagged no-visible/no-q rows remain auditable without being mistaken for zero support, corrupted exact identities and available-row metrics fail, cluster H/V medians are finite, and the Spark continuation contains no full-pipeline/subset-search call.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m compileall -q scripts; python3 -m unittest discover -s scripts -p 'test_*.py' -v; bash -n staged Spark scripts; strict corrected primary and follow-up Spark verifiers; rendered gallery review
- Notes: selection scores and memberships were unaffected by the duration-field defect; the repair changes only that descriptive field before dependent summaries are regenerated. Held-out summaries report both total and evaluable rows because missing `q_hat` is a scientific coverage result, not a numerical zero.

### [ENG-035] Fail-closed run provenance and shard merging
- Status: in_progress
- Owner: project
- Type: reliability
- Why: resumable Best-N rows were not bound to their original parameters, and the Best-N and intensity mergers silently replaced duplicate shard keys. Either behavior could produce structurally complete output with ambiguous provenance.
- Scope: write a checksummed JSON run contract before Best-N, intensity, and full-buffer ridge computation; reject parameter-changing output reuse; require compatible complete shard contracts and contiguous resume grids; fail on every duplicate or incomplete comparator key; carry merged statistical parameters into the merged contract; verify contract schema, identity, hashes, geometry, N, folds, tune tolerance, and block length.
- Acceptance: regression tests reject a changed Best-N resume and a duplicate Best-N shard key; every Spark Best-N, intensity, and ridge output contains a verifier-accepted `run_contract.json`; no merger silently deduplicates rows.
- Docs: NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/*.py scripts/*.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; strict Spark Best-N, intensity, and ridge verifiers
- Notes: source payloads remain read-only; contracts hash configuration and source inventories rather than copying or mutating raw data.

### [ENG-034] Strict intensity-study publication contract
- Status: in_progress
- Owner: project
- Type: reliability
- Why: the corrected gate and block-aware inference still need one closure check tying the audited raw capture shape, complete method grids, zero-effect control, decision rules, and broad gallery together.
- Scope: add an explicit unweighted fallback when no selected intensity is usable, verify the known 23999 exact payload pairs, no first-50000-turn corruption or shard errors, 12800 spill-method rows, complete exact 90-window 4096/512 grids, identical method spill keys and selected memberships, finite global ridge picks, exact selected cardinality, numerical Best-1 invariance across all methods, all effect decision gates, and every indexed gallery PNG/guardrail. Density differences use only identical exact finite spill/window points, and all heatmap bins use proportional inclusive raster bounds.
- Acceptance: an incomplete synthetic root fails; all fallback windows carry a reason and matching spill-level fraction; the corrected 200-spill block-20 merge and gallery pass; Best-1 has exact zero effects and its neutral subtraction maps are visibly labeled as no redistribution; 10/20/40-spill summaries preserve the same exact retained-effect identities, not only the same count; every raster fills its declared axes; count-density captions disclose nonzero P98 clipping; subtraction legends describe higher/lower ridge-pick probability, disclose the absolute-P99 display clip, and make no physical-noise claim.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/intensity_verification.py scripts/verify_intensity_outputs.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; corrected Spark intensity refresh, block-20 merge/gallery, strict verifier, and 10/20/40 sensitivity comparison
- Notes: intensity remains auxiliary; payload-horizon and lag diagnostics cannot establish beam-loss or extraction timing. Red/blue subtraction color is column-normalized ridge-pick probability redistribution, not measured noise removal.

### [ENG-033] Strict full-buffer ridge publication contract
- Status: in_progress
- Owner: project
- Type: reliability
- Why: the ridge renderer could exit successfully while only reporting an aggregate warning count, so missing memberships, incomplete exact pairing, or absent figure files could escape publication review.
- Scope: persist every generation warning and verify requested-N spill/window coverage, exact member cardinality, tune-band bounds, unique ridge keys, exact 2000-spill adaptive and 1988-spill legacy coverage, finite aggregate and per-turn contrast metrics, loss/role coverage, every per-N shared-scale H/V comparison and other manifest PNG/caption, and an exact match to the archived `18d321dbd4fe` tracking protocol.
- Acceptance: the verifier fails on an incomplete synthetic output root; proportional raster cells fill the full declared tune axis without gaps or overlay displacement; the full Spark 50000-turn gallery passes at the declared minimum spill and 180-center coverage; every remaining noncritical warning receives written disposition before a panel is used.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/ridge_verification.py scripts/make_best_bpm_ridge_density.py scripts/verify_ridge_density_outputs.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; full Spark ridge pass and strict verifier
- Notes: subtractive plots remain exact-pair probability redistribution diagnostics, not physical noise measurements. Visible legends use higher/lower pick probability, captions disclose standalone/pair P98 and subtractive absolute-P99 raster clipping, and exported metrics remain unclipped. The older standalone raster used floor-divided cell heights and could misalign density with percentile overlays; paired panels were unaffected, and all corrected panels use proportional inclusive cell bounds.

### [ENG-032] Non-circular block inference and matched-pairs effect size
- Status: in_progress
- Owner: project
- Type: fix
- Why: moving-block intervals wrapped nonadjacent collection/turn endpoints, and the exported rank-biserial field used only sign counts rather than ranked absolute paired differences.
- Scope: keep every resampled block inside the observed collection or turn ordering, compute the standard matched-pairs rank-biserial correlation, honor the configured permutation sample count without a hidden cap, and rerun affected primary, Best-N, intensity, and ridge summaries.
- Acceptance: regression tests cover the ranked effect calculation and exact configured draw count; local intensity decisions are compared at 10/20/40-spill non-circular blocks; corrected Spark outputs and captions identify moving-block inference without endpoint wraparound.
- Docs: NEXT_STEPS.md, docs/SPARK.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; corrected Spark statistics, Best-N block remerge, intensity block re-summary, and ridge comparison metrics
- Notes: block length remains a sensitivity parameter; no single block choice may determine a retained method or Best-N conclusion.

### [ENG-031] Plane-balanced curated poster examples
- Status: in_progress
- Owner: project
- Type: fix
- Why: the first eight-example poster shortlist filled entirely with higher-priority V rows even though H limitation examples existed in the full artifact manifest.
- Scope: deduplicate spill-plane rows, reserve scored examples from both planes, then fill by plane/category diversity and score without changing the exhaustive spill gallery.
- Acceptance: synthetic end-to-end tests require H and V in the curated manifest; corrected Spark poster artifacts contain both planes and the contact sheet renders their real cache-backed images.
- Docs: NEXT_STEPS.md, docs/POSTER_ANALYSIS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; corrected Spark artifact sidecar and rendered contact-sheet review
- Notes: this balances review coverage, not the physics conclusion; V may remain the strongest final panel.

### [ENG-030] Same-metric dynamic/fixed/all-BPM control recomputation
- Status: in_progress
- Owner: project
- Type: fix
- Why: the first fixed-set sidecar recomputed frozen and all-BPM rows with the evolution score but copied dynamic rows carrying the unrelated subset-search score, making plotted bar heights incomparable.
- Scope: resolve exact dynamic memberships per spill, recompute dynamic/fixed/all-BPM spectra from the same cache, score every method with the same evolution metric, fail on incomplete cardinality, and label the comparison descriptive because original dynamic memberships reuse selection windows.
- Acceptance: regression checks prove every output score is derived from the exported visibility and prominence fields; serial and parallel outputs match; the corrected Spark sidecar and verifier complete with exact cardinality.
- Docs: NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/fixed_sets.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; corrected Spark fixed-set sidecar and follow-up verifier
- Notes: do not reuse the June fixed-vs-dynamic plot or its numeric conclusion in a publication artifact.

### [ENG-028] Intensity-assisted tune-quality sidecar
- Status: in_progress
- Owner: project
- Type: feature
- Why: the 200-spill raw capture contains timestamp-matched position/intensity pairs, but intensity has never been tested as a quality covariate or ensemble weight.
- Scope: pair exact position/intensity channels, audit the valid payload horizon, select ensembles from position-only fit windows, compare unweighted/square-root/linear/gated spectral aggregation on later windows, run paired spill-level inference, estimate intensity/visibility lag associations and H-plane loss candidates, and generate a broad pure-PNG review gallery with claim guardrails.
- Acceptance: synthetic tests prove exact pairing and N=1 weighting invariance; a 200-spill Spark pass covers the first 50000 turns; intensity is retained only when paired confidence intervals and FDR-corrected tests improve, the median tune shift stays within tolerance, and at least 95% of spillwise shifts stay within tolerance; 10/20/40-spill block lengths agree on the decision; payload corruption is not interpreted as beam loss.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/intensity.py scripts/bpm_mining/intensity_plots.py scripts/analyze_intensity_assisted_tune.py scripts/merge_intensity_study.py scripts/make_intensity_study_plots.py scripts/compare_intensity_block_sensitivity.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; Spark one-spill smoke; Spark 200-spill four-shard run, 10/20/40-spill re-summary, sensitivity comparison, and gallery render
- Notes: raw intensity is never multiplied into position waveforms; it only changes per-window spectral aggregation weights or serves as a covariate. The publication run uses the explicit canonical N grid `1,3,5,7,10,12,15,20` plus each distinct accepted H/V recommendation outside that grid; analysis and verification must use the same union and corresponding spill-row count.

### [ENG-027] Time- and digitizer-disjoint Best-N model selection
- Status: in_progress
- Owner: project
- Type: feature
- Why: completed Best-1/3/5 runs do not establish the optimal ensemble size, and training score alone can improve with N without demonstrating later-window reproducibility.
- Scope: sweep contiguous N values with a beam search, derive candidate tunes only from fit windows, evaluate selected members on unseen later windows, compare against digitizer-disjoint channels, emit bootstrap intervals and explicit non-inferiority knees, support resumable Spark shards plus deterministic merging, execute a shared-baseline seven-run beam/fit/fold sensitivity matrix, and fail closed on coverage/identity/timing/summary/plot verification.
- Acceptance: synthetic tests cover complete N curves, two-shard merge, matrix deduplication, and strict verification; a Spark smoke produces non-saturated validation metrics; the full 2000-spill curve and disjoint validation quantify the knee through at least N=20 or document why a larger N is still required; beam/fit/fold and 10/20/40-spill block-length sensitivity do not reveal an unresolved recommendation; every full and sample output passes `verify_best_n_outputs.py`.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/best_n.py scripts/bpm_mining/best_n_sensitivity.py scripts/bpm_mining/best_n_verification.py scripts/evaluate_best_n_curve.py scripts/merge_best_n_shards.py scripts/run_best_n_sensitivity_matrix.py scripts/verify_best_n_outputs.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; Spark CuPy smoke; full Spark sharded run, block remerges, seven-run sensitivity matrix, and strict verification
- Notes: the automatic knee is a declared reproducibility/contrast non-inferiority rule; full metric curves remain the primary evidence. Four concurrent max-N=40 evaluators exceeded 115 GiB on Spark's single GB10 and forced a reboot. A later two-process qualification peaked near 83 GB (77 GiB) host use with about 44 GiB available and mostly 70-96% GPU utilization. Full-run shards and independent sensitivity configurations may therefore use at most two evaluators under a 32 GiB `MemAvailable` floor sampled every five seconds with three low samples before process-group termination; intensity and ridge remain serialized until separately qualified.

### [ENG-026] Exact BPM identity and ring-order provenance correction
- Status: in_progress
- Owner: project
- Type: fix
- Why: each digitizer contributes two same-plane channels, but legacy subset artifacts serialized only the digitizer label, and ring order was parsed from the first number in the IP address. Follow-up reconstruction could select the wrong channel and the ring-span diversity term was disabled.
- Scope: make plane-local indices, exact source keys, channel tokens, and digitizers explicit in every subset artifact; resolve legacy rows from the bit mask before names; derive ring order from the `HPnnn`/`VPnnn` token; update all downstream readers; regenerate affected results and figures.
- Acceptance: regression tests cover ambiguous same-digitizer labels and ring ordering; no corrected finalist has a selected-channel-count mismatch; publication figures and held-out/fixed/handoff results come only from corrected exact identities; legacy affected artifacts are labeled provisional.
- Docs: README.md, NEXT_STEPS.md, docs/USAGE.md, docs/SPARK.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/*.py scripts/gpu_analyze_captured_spills.py scripts/make_best_bpm_ridge_density.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; corrected Spark Best-1/3/5 and sidecar verification
- Notes: original Best-1/3/5 score rows remain useful diagnostics because masks preserve local indices, but downstream products that reconstructed channels from digitizer labels must be regenerated.

### [ENG-021] Autosweep parallel runner and GPU telemetry
- Status: in_progress
- Owner: project
- Type: perf
- Why: Spark autosweep and Best-BPM runs need better host/GPU utilization and first-class accounting for GPU wall time, utilized GPU-hours, and power draw.
- Scope: add `--parallel-jobs` to `scripts/run_autosweep.py`, keep serial default behavior, preserve deterministic run logs and timeout handling, add stdlib `scripts/gpu_run_telemetry.py`, and expose optional telemetry in autosweep and Best-BPM pipeline wrappers.
- Acceptance: local tests cover parallel dry-run scheduling, timeout handling, and telemetry summaries; a Spark smoke with 2 concurrent autosweep jobs completes cleanly before GitHub issue #30 is closed.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/gpu_run_telemetry.py scripts/run_autosweep.py scripts/bpm_mining/pipeline.py scripts/test_autosweep.py scripts/test_best_bpm_mining.py; python3 scripts/test_autosweep.py; python3 scripts/test_best_bpm_mining.py
- Notes: Spark two-job smoke is intentionally deferred until the current full Best-BPM run releases the GPU.

## Done

### [ENG-029] Checksummed publication review packaging
- Status: done
- Owner: project
- Type: feature
- Why: final paper, poster, reports, and large review galleries need one reproducible local handoff rather than an undocumented manual copy.
- Scope: add a repeatable `LABEL=PATH` packager that copies files and directories into a new review root and emits a source-path, byte-size, and SHA-256 manifest, a human-readable package index, and one searchable/lazy-loading HTML gallery over every packaged image.
- Acceptance: packaging refuses missing, duplicate, nested, or non-empty destinations; a smoke package contains copied inputs, `MANIFEST.csv`, `PACKAGE_INDEX.md`, and a filterable `index.html` with the copied figures.
- Docs: README.md, docs/USAGE.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/package_publication_review.py; python3 scripts/package_publication_review.py --component readme=README.md --component abstract=/Users/derekste/Downloads/abstract-54.pdf --out /tmp/tbt-publication-package-smoke-20260709
- Notes: generated review packages remain local artifacts; only the curated publication source/deliverables belong in version control.

### [ENG-025] Best-ensemble full-buffer ridge-density sidecar
- Status: done
- Owner: project
- Type: feature
- Why: the older `18d321db` ridge-density gallery plots were the strongest visual candidates, and the Best-BPM result needed a comparable full-buffer ensemble density view plus an explicit noise/concentration diagnostic.
- Scope: add `scripts/make_best_bpm_ridge_density.py` and a poster-artifact compatibility wrapper, reuse completed memberships over 0-50000 turn raw spectra, render H/V ridge-density PNGs, pairwise density-difference PNGs, turn-concentration plots, captions, metrics, a loss-candidate summary, and exact-paired corrected Best-1/selected plus legacy/corrected-Best-1/selected controls so selector repair is not misreported as ensemble-size gain.
- Acceptance: Spark smoke and full sidecar runs complete under `/home/derekste/best_bpm_mining_20260627_best135_from_v2/followups/next_steps_20260628/ridge_density_best_ensemble`; local review copies exist under `review-artifacts/best-bpm-ridge-density-20260628`; deficiencies are tracked in GitHub issue #39.
- Docs: NEXT_STEPS.md, docs/SPARK.md, docs/USAGE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/make_best_bpm_ridge_density.py scripts/gpu_analyze_captured_spills.py scripts/bpm_dgx_poster.py scripts/make_best_bpm_poster_artifacts.py; Spark CUDA full sidecar over both 1000-spill position-only collections
- Notes: the sidecar is not a full 50k dynamic subset search; it intentionally reuses early-window memberships and documents that limitation in captions and issue #39.

### [ENG-024] Best-BPM follow-up validation sidecar stack
- Status: done
- Owner: project
- Type: feature
- Why: the verifier-clean best1/3/5 Spark run needed direct fixed-set validation, stronger held-out spectral support, poster-grade cache-backed artifacts, and tune-visibility handoff analysis without mutating canonical outputs.
- Scope: add fixed-set, held-out, selected-artifact, and handoff passes with shared progress files, sidecar verification, Spark commands, and docs.
- Acceptance: local tests cover serial/parallel equality and sidecar verification; Spark smoke and full sidecar runs complete under `/home/derekste/best_bpm_mining_20260627_best135_from_v2/followups/next_steps_20260628`.
- Docs: NEXT_STEPS.md, docs/SPARK.md, docs/PHYSICS.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/*.py scripts/evaluate_fixed_bpm_sets.py scripts/evaluate_heldout_spectral_support.py scripts/run_bpm_handoff_analysis.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; Spark follow-up verifier passed for `/home/derekste/best_bpm_mining_20260627_best135_from_v2/followups/next_steps_20260628`
- Notes: canonical Spark run outputs stayed read-only; follow-up outputs were written sidecar-first.

### [ENG-023] Subsystem documentation rework
- Status: done
- Owner: project
- Type: docs
- Why: the README and detailed command docs had grown back into a mixed landing page, DAQ guide, analysis guide, Spark runbook, and operations reference.
- Scope: keep `README.md` concise, add subsystem guides for DAQ, Rust analysis chains, Spark workflows, and operations, and update cross-references so `docs/USAGE.md` remains the exact command reference.
- Acceptance: README is a short entry point; subsystem docs provide clear ownership for DAQ/capture, analysis chains, Spark/offline mining, and operations; existing detailed docs link to the new structure; markdown links validate locally.
- Docs: README.md, docs/DAQ.md, docs/ANALYSIS_CHAINS.md, docs/SPARK.md, docs/OPERATIONS.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 /tmp/check_tbt_doc_links.py
- Notes: docs-only change; no runtime behavior or output schemas changed.

### [ENG-022] Best-BPM 2000-spill mining pipeline
- Status: done
- Owner: project
- Type: feature
- Why: the poster narrative needs a defensible BPM-only study of which individual and small BPM subsets most consistently recover tune information in the 2000 unlabeled Spark spills.
- Scope: add `scripts/bpm_mining/`, pass wrappers, default config, exact best-1/best-3 search, screened audited best-5 search, per-BPM consensus/features, global statistics, clustering, artifact selection, plots, final reports, Spark parallel worker controls, live subset-search progress files, and output verification.
- Acceptance: the pipeline writes every required output group from `BEST_BPM_2000_SPILL_MINING_IMPLEMENTATION_PLAN.md`, passes synthetic unit/smoke tests, completes the focused best1/3/5 Spark run over the two Tier A collections using parallel workers, and passes `scripts/verify_best_bpm_outputs.py`.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 -m py_compile scripts/bpm_mining/*.py scripts/verify_best_bpm_outputs.py scripts/test_best_bpm_mining.py; PYTHONPYCACHEPREFIX=/tmp/tbt-monitor-pycache python3 scripts/test_best_bpm_mining.py; Spark verifier passed for `/home/derekste/best_bpm_mining_20260627_best135_from_v2`
- Notes: best-10 remains deferred; completed reports must not imply it was run.

### [ENG-019] Make captured-artifact quality the primary capture UX
- Status: done
- Owner: codex
- Type: qol
- Why: operators care first about complete same-spill acquisitions and bad digitizers in captured payloads; latest-poll snapshot staleness was too prominent in console and run reports.
- Scope: update capture summaries and quality reports to lead with artifact completeness and capture suspect digitizers, while keeping latest-poll-only suspects as explicitly advisory diagnostics.
- Acceptance: complete captures with stale latest-poll snapshots read as complete acquisitions; bad-digitizer alerts are tied to captured payload missing/stale/ahead/malformed reasons; latest-poll diagnostics remain available for troubleshooting.
- Docs: docs/USAGE.md, docs/DESIGN_DECISIONS.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: this is a reporting/triage priority change, not a captured bundle schema change.

### [ENG-018] RAW position plus auxiliary RAW intensity capture
- Status: done
- Owner: codex
- Type: feature
- Why: the next acquisition run should preserve least-transformed position payloads and collect matching intensity artifacts before analysis semantics are refined.
- Scope: switch checked-in monitor config to `TBT_POSITION_RAW`, add `capture_intensity_variant=raw`, derive matching `TBT_INTENSITY_RAW` streams during capture/assess, keep position streams as target-selection and tune-analysis inputs, and skip auxiliary intensity during offline tune analysis.
- Acceptance: default config captures 120 RAW position streams plus 120 derived RAW intensity streams; RAW position streams classify as H/V analysis inputs; intensity streams are captured as auxiliary payloads without being analyzed as position traces; docs describe the 240-stream default capture.
- Docs: README.md, docs/USAGE.md, docs/CONFIG_REFERENCE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: follows the RAW-vs-SCALED findings tracked in GitHub issue #25; intensity remains auxiliary until a later curation/analysis contract promotes it.

### [ENG-017] Elite full-data autosweep stage
- Status: done
- Owner: codex
- Type: feature
- Why: the completed Spark pilot needs a focused full-data stage that reruns only explicit elite H/V/poster configurations over usable Tier A spills and generates heavy review artifacts.
- Scope: add elite full-stage selection and summary scripts, make full mode consume the supplied config list exactly, add BPM leaderboard and subset-consistency analyzer artifacts, and cover the flow with stdlib tests.
- Acceptance: pilot ranked outputs can produce filtered elite manifests/config lists, full-stage runs preserve rejected diagnostics, summaries identify best H/V/robust/poster configs, and heavy jobs emit BPM leaderboard and subset-consistency artifacts.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/build_elite_full_stage.py scripts/make_elite_full_summary.py scripts/run_autosweep.py scripts/gpu_analyze_captured_spills.py scripts/test_autosweep.py; python3 scripts/test_autosweep.py; python3 scripts/gpu_analyze_captured_spills.py --self-test
- Notes: Tier A usable-spill filtering comes from `spill_health.csv`; poster-safe summaries exclude `TOO_SLOW`, `UNSTABLE_H`, `UNSTABLE_V`, and `OVERFITS_BAND` by default.

### [ENG-016] Spark BPM autosweep ranking and classification
- Status: done
- Owner: codex
- Type: feature
- Why: the raw Spark position-only BPM dataset needs automated staged parameter exploration, candidate spill/config ranking, and classification without a naive full Cartesian sweep.
- Scope: add Stage 0 manifest/health/cache scripts, extend the raw captured-spill GPU analyzer with turn/plane/BPM-combination/preprocessing/ridge-anchor knobs, add deterministic autosweep orchestration, ranking/classification tables, initial summary generation, optional Spark venv bootstrap, and stdlib tests.
- Acceptance: Tier A raw position-only bundles can be inventoried, health-checked, swept in pilot/full modes, ranked with the required weighted score formula, classified with stable spill/config labels, and summarized into the required CSV/JSON/Markdown/PNG artifact set.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py scripts/build_collection_manifest.py scripts/validate_spill_integrity.py scripts/build_spill_cache.py scripts/run_autosweep.py scripts/rank_autosweep_results.py scripts/make_initial_analysis_summary.py scripts/test_autosweep.py; python3 scripts/test_autosweep.py; python3 scripts/gpu_analyze_captured_spills.py --self-test
- Notes: Tier B intensity/beam-loss support remains later-capable and does not block Tier A outputs. Autosweep scoring is BPM-only and should not be treated as Schottky/reference validation.

### [ENG-015] Offline tune-evolution poster upgrade
- Status: done
- Owner: codex
- Type: feature
- Why: `BPM_TUNE_EVOLUTION_ANALYSIS_UPGRADE_PLAN.md` requires cleaner and more physics-reviewable tune-evolution products than baseline FFT/stride traces alone.
- Scope: extend `scripts/gpu_analyze_captured_spills.py` with ridge-density plots, Hann/multitaper spectrogram options, dynamic-programming ridge extraction, representative ridge traces/overlays, optional SVD/PCA denoising products, and DGX benchmark markdown/PNG outputs while keeping CPU reproducibility and the existing baseline outputs.
- Acceptance: the analyzer exposes the requested CLI knobs; a CPU smoke run with `--spectrogram-method both --ridge-method dp --ridge-source-method multitaper --svd-denoise` produces all named upgrade artifacts; Spark can run the same upgraded path over `/home/derekste/tbt-spills-2000` with CuPy.
- Docs: README.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py scripts/bpm_dgx_poster.py; python3 scripts/gpu_analyze_captured_spills.py --self-test; CPU synthetic smoke with both spectrogram methods, DP ridge, and SVD enabled; remote Spark upgraded run over the copied 2000-spill dataset
- Notes: SVD/PCA remains opt-in and representative-spill only; it is not a production Rust tune-extraction default or a Schottky validation substitute.

### [ENG-014] Spark GPU raw captured-spill analysis
- Status: done
- Owner: codex
- Type: feature
- Why: the 2000-spill raw payload set is large enough that the poster/DGX phase needs a direct CuPy/CUDA analyzer instead of only summary-artifact synthesis.
- Scope: add `scripts/gpu_analyze_captured_spills.py` to load captured-spill `manifest.json` files and little-endian f32 payloads, run Hann-window FFT tune extraction with flash windows and local tracking on CuPy, keep NumPy CPU fallback/self-test, and emit GPU spill summaries, sliding/flash CSVs, tune/waterfall/spectrogram PNGs, and benchmark markdown.
- Acceptance: local and Spark self-tests pass; Spark can run CUDA smoke/full passes over the copied two-run 2000-spill dataset using `/home/derekste/venvs/cupy-spark-cu13`.
- Docs: README.md, AGENTS.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 -m py_compile scripts/gpu_analyze_captured_spills.py; python3 scripts/gpu_analyze_captured_spills.py --self-test; remote Spark self-test with `/home/derekste/venvs/cupy-spark-cu13/bin/python`; Spark CUDA smoke/full run over `/home/derekste/tbt-spills-2000`
- Notes: CuPy/CUDA 13 on Spark is provided by `/home/derekste/venvs/cupy-spark-cu13`; use `ssh -K spark.fnal.gov` from `drbpm1` for restartable `rsync --partial` copies. The 2000-spill raw dataset was copied to `/home/derekste/tbt-spills-2000` on Spark. Full Spark outputs were generated at `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w2048` (2048-turn window, 1776 usable spills, 96000 sliding rows, 22.404 s elapsed) and `/home/derekste/tbt-spills-2000-gpu-20260609-flash128-w256` (256-turn true-128 flash windows, 1775 usable spills, 512000 sliding rows, 24.244 s elapsed).

### [ENG-013] BPM-only poster/DGX standalone artifact tool
- Status: done
- Owner: codex
- Type: feature
- Why: the conference-poster sprint needs a standalone offline tool that runs over the complete collected BPM artifact set on `drbpm1` or a DGX-mounted copy without changing the Rust runtime.
- Scope: add `scripts/bpm_dgx_poster.py` plus thin phase wrappers for manifest, baseline, flash, spectrogram/waterfall, subset, optional ML, benchmark, and poster-plot collection; support `candidate_spills.csv`, `spills_summary.csv`, and `capture_index.csv`; keep CPU fallback and optional CUDA/CuPy benchmarking; ignore generated Python cache and local poster-output directories.
- Acceptance: local self-test and review-artifact smoke run produce the poster-phase manifest, summaries, PNGs, model reports, benchmark report, and poster plot index; docs state that the full run should target `/home/derekste/out` on `drbpm1` or the DGX copy and that Schottky is excluded from this phase.
- Docs: README.md, AGENTS.md, docs/USAGE.md, docs/POSTER_ANALYSIS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: python3 scripts/bpm_dgx_poster.py --self-test; python3 scripts/bpm_dgx_poster.py run-all --input review-artifacts --out /private/tmp/tbt-monitor-poster-smoke --flashes 128 256 512 --device cpu; remote `drbpm1` self-test; remote `drbpm1` run-all over `/home/derekste/out`; remote `spark` run-all over copied `tune-curation`; cargo fmt --all; cargo test -- --nocapture
- Notes: full remote output was generated at `/home/derekste/out/bpm-dgx-poster-20260609`; Spark output was generated at `/home/derekste/bpm-dgx-poster-20260609-spark` after copying the 671 MB `tune-curation` tree. CuPy was installed into `/home/derekste/venvs/cupy-spark-cu13` using a wheelhouse downloaded on `adlinux3`; rerunning on Spark with that venv produced `/home/derekste/bpm-dgx-poster-20260609-spark-cu13` with CUDA benchmark availability.

### [ENG-012] Capture timestamp distribution reporting
- Status: done
- Owner: codex
- Type: reliability
- Why: operators need `120/120` captured streams to show the actual timestamp distribution instead of relying on ambiguous latest-poll `% aligned` wording.
- Scope: add captured-payload and latest-ID timestamp delta distributions to manifests, summaries, run-level JSON/Markdown, and a dedicated `capture_timestamp_distribution.csv`; clarify console warnings and docs.
- Acceptance: complete captures report captured-payload timestamp buckets separately from latest-ID snapshot buckets; run-level reports aggregate both distributions; tests cover distribution fields and output creation.
- Docs: README.md, docs/USAGE.md, docs/CONFIG_REFERENCE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements the first practical slice of GitHub issue #19; settle-after-wake polling remains a follow-up if latest-ID snapshots still need delayed classification.

### [ENG-011] README and usage-doc restructure
- Status: done
- Owner: codex
- Type: docs
- Why: the README had grown into a long combined feature inventory, user guide, artifact reference, Docker guide, and developer orientation.
- Scope: make `README.md` a concise project entry point, move command workflows into `docs/USAGE.md`, and trim repeated implemented-feature inventories across planning and physics docs.
- Acceptance: README introduces the project succinctly with links to feature guides; user workflows remain documented outside the README; internal docs consistently reference implemented commands, timing semantics, captured-spill artifacts, batch outputs, and remaining physics work.
- Docs: README.md, AGENTS.md, .github/pull_request_template.md, .github/ISSUE_TEMPLATE/feature_request.yml, docs/USAGE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/GITHUB_WORKFLOW.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo run --offline -- --help; cargo fmt --all; cargo test -- --nocapture
- Notes: tracked by GitHub issue #17; docs-only restructure with no behavior or schema changes.

### [ENG-010] Capture timing diagnostics and assess preflight
- Status: done
- Owner: codex
- Type: reliability
- Why: DAQ runs need first-class completeness and timing diagnostics so stale digitizers and timestamp distributions are visible before and during acquisition.
- Scope: add `same_spill_tolerance_ms`, capture manifest diagnostics, run-level capture CSV/JSON/Markdown reports, offline `diagnose-captures`, non-capturing `assess`, reason-code tests, and documentation.
- Acceptance: capture artifacts distinguish complete same-spill payloads from stale latest-poll observations; existing capture directories can regenerate reports; `assess` writes preflight stream/digitizer reports without payload capture; v1 remains annotate-only.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/CONFIG_REFERENCE.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: future strict-fail mode should reuse the same reason codes and enforce captured artifact quality first.

### [ENG-009] Online/offline split parity guardrail
- Status: done
- Owner: codex
- Type: reliability
- Why: the captured-bundle split needs a deterministic regression check that offline analysis preserves today's proof-of-concept behavior for the same raw spill data.
- Scope: add a no-Redis parity test that builds an online-style snapshot from decoded raw payload bytes, loads the same captured-spill bundle offline, and compares tune estimates, sliding medians, selected stream/quality fields, warnings, and quality flags with field-named failure messages.
- Acceptance: parity runs in normal `cargo test`; differences in key proof-of-concept outputs produce actionable field-specific failures; docs state this is a split regression guard, not physics certification of the current algorithm.
- Docs: docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issue #5. Post-split analysis refinement issue #9 remains open.

### [ENG-008] Offline captured-spill batch analysis
- Status: done
- Owner: codex
- Type: feature
- Why: captured raw spill bundles need batch-style analysis without Redis connectivity so acquisition-first runs can be reviewed and reprocessed offline.
- Scope: add captured-bundle discovery, duplicate-target suppression, offline snapshot reconstruction for multiple bundles, `analyze-captured-spills`, existing batch writer reuse, and focused offline batch artifact tests.
- Acceptance: a directory of captured-spill bundles can produce the current batch artifacts without Redis access; a single bundle directory or manifest path is also accepted; malformed bundles are skipped with explicit diagnostics when other usable bundles remain; batch records identify offline provenance with `trigger_source=captured-spill`.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issue #7. Minimal online/offline parity issue #5 remains open.

### [ENG-007] Offline captured-spill single analysis
- Status: done
- Owner: codex
- Type: feature
- Why: captured raw spill bundles need to be analyzable without Redis connectivity so acquisition and analysis are actually separated.
- Scope: add captured-spill manifest loading, safe payload resolution, checksum/size/sample validation, raw little-endian `f32` payload decoding, snapshot reconstruction, `analyze-captured-spill`, and focused offline artifact tests.
- Acceptance: a captured-spill bundle directory or manifest path can produce the current one-spill analysis artifacts without Redis access; unsupported schema/artifact types fail explicitly; incomplete/malformed captured streams emit warnings or errors instead of silent fallback.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issues #4 and #3. Offline multi-bundle issue #7 and parity issue #5 remain open.

### [ENG-006] Raw captured-spill acquisition commands
- Status: done
- Owner: codex
- Type: feature
- Why: acquisition must be separable from tune analysis so complete BPM spill data can be captured once and reanalyzed offline later.
- Scope: add `src/capture.rs`, `capture-spill`, `capture-spills --free-run [--count N]`, `schema_version=1` manifest writing, raw payload files, capture summaries, run-level `capture_index.csv`, and focused capture tests.
- Acceptance: one-shot capture writes a complete bundle without tune analysis; free-run capture writes one bundle per unique target and maintains a batch index; manifests include target/alignment metadata, full stream inventory, payload file paths, sizes, sample counts, and checksums; incomplete states emit warnings.
- Docs: README.md, AGENTS.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: implements GitHub issues #2, #6, and #8. Offline loader/analysis issues #3, #4, #7, and parity issue #5 remain open.

### [ENG-005] GitHub issue and PR workflow bootstrap
- Status: done
- Owner: codex
- Type: docs
- Why: project planning needs issue-first tracking, PR templates, and a durable map for the acquisition/offline-analysis split.
- Scope: add GitHub issue templates, a PR template, workflow guidance, and an acquisition/analysis issue map with PR-sized slices.
- Acceptance: templates exist under `.github/`, workflow docs define labels/branch/PR expectations, and the acquisition/analysis split is mapped into GitHub-ready issues.
- Docs: README.md, docs/GITHUB_WORKFLOW.md, docs/ISSUE_MAP_DAQ_SPLIT.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all --check; cargo test -- --nocapture
- Notes: completed on 2026-05-31; tracked by GitHub issue #1 and seeded split issues #2-#9.

### [ENG-004] Per-flash histogram batch artifacts
- Status: done
- Owner: codex
- Type: qol
- Why: flash-sampled runs need distribution views at each flash index, not only trend lines.
- Scope: add `tune_histogram_flash_XX.png` generation for flash-enabled batch outputs (including `analyze-spill --free-run --count` synthesis path) using existing sliding tune points.
- Acceptance: when `--flashes` is set and batch outputs are emitted, one `tune_histogram_flash_XX.png` is produced per available flash index alongside `tune_vs_spill_flash_XX.png`.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-003] Plot-axis domain toggle and tune-grid spacing config
- Status: done
- Owner: codex
- Type: qol
- Why: operators need turns as default axis domain, optional physical-time view in microseconds, and configurable tune-grid readability.
- Scope: add `plot_time_axes_in_us` config + CLI enable override (`--plot-time-axes-in-us`) across analysis commands, apply turn/us axis rendering consistently across per-spill and composite artifacts, and make `tune_vs_time` Y-grid spacing configurable (`tune_plot_y_tick_step`).
- Acceptance: default plots render turn-index time axes, enabling time-domain mode renders microseconds (`us`) using `turn_period_us`, and `tune_vs_time` horizontal grid spacing follows `tune_plot_y_tick_step`.
- Docs: README.md, docs/CONFIG_REFERENCE.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-002] Flashpoint sampling mode and spill-trend expansion
- Status: done
- Owner: codex
- Type: qol
- Why: compare tune across spills at consistent in-spill checkpoints without relying only on injection or dense stride sampling.
- Scope: add `--flashes N|max` to tune-analysis commands, bound flash sampling by available turn depth, emit per-flash `tune_vs_spill_flash_XX.png`, and annotate per-spill `tune_vs_time` with flash turns and injection guides.
- Acceptance: `--flashes` overrides stride-based sliding placement, `--flashes max` resolves to per-spill maximum supported windows, flash mode uses `sliding_window_turns` for injection-path estimation, spill summaries warn when requested flashes are reduced by turn-depth bounds, and batch/per-spill artifacts render with flash data.
- Docs: README.md, docs/ARCHITECTURE.md, docs/DESIGN_DECISIONS.md, docs/PLAN.md, docs/PHYSICS.md, docs/ANALYSIS_CHECKLIST.md, docs/ENGINEERING_BACKLOG.md
- Validation: cargo fmt --all; cargo test -- --nocapture
- Notes: completed on 2026-03-11.

### [ENG-001] Backlog framework bootstrap
- Status: done
- Owner: codex
- Type: docs
- Why: establish a durable process for keeping implementation and docs aligned.
- Scope: define backlog structure/template and add doc-sync DoD in `AGENTS.md`.
- Acceptance: template exists in this file and DoD gate exists in `AGENTS.md`.
- Docs: docs/ENGINEERING_BACKLOG.md, AGENTS.md
- Validation: docs-only change
- Notes: completed on 2026-03-09.
