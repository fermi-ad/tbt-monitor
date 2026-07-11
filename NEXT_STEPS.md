# Next Steps For Best-BPM Mining And IBIC Poster

Last updated: 2026-07-11.

This file is a handoff for the next Codex/DevSpace pass. It consolidates the current repo review, Spark run status, physics assumptions, validation gaps, and the most important next analysis questions for the Delivery Ring BPM tune-tracking work.

## 2026-07-09 Publication Audit And Completion Gate

The June 28 interpretation below is retained as run history, but its downstream
fixed-set, held-out, handoff, selected-artifact, and full-buffer figures are
provisional pending a corrected rerun. Three provenance defects were found during
the publication audit:

1. Each digitizer contributes two exact same-plane channels, while legacy
   `bpm_members` rows stored only the shared digitizer label. The primary subset
   masks retained exact plane-local indices, but downstream readers could map a
   selected member to the other channel on the same digitizer.
2. `ring_order` was parsed from the first number in the digitizer IP, assigning
   every channel the value `10`. This disabled the ring-span component of the
   Best-3/Best-5 diversity score.
3. The favorite legacy `18d321db` run selected `best_single_bpm` after
   `rms_per_bpm` normalization. All normalized channel RMS values were nominally
   one, so the selected channel was effectively determined by floating-point
   residuals rather than highest raw RMS. In an 800-plane-row audit, the legacy
   channel matched the raw-RMS leader only 1.25% of H rows and 1.00% of V rows;
   its median raw-RMS rank was 29/60 in H and 31/60 in V. Those images are now
   labeled `legacy normalized-single`, not `best BPM`.
4. The first fixed-set sidecar recomputed fixed/all-BPM rows with the evolution
   visibility-prominence score but copied dynamic rows with the unrelated
   subset-search score. Its fixed-vs-dynamic bar heights and conclusions are
   therefore provisional. Corrected code now rescales dynamic memberships,
   frozen memberships, and all-BPM controls from the same cached spectra with
   the same evolution metric; the leakage-controlled Best-N pass remains the
   inferential test because this descriptive control reuses selection windows.
5. The first curated poster shortlist exhausted its eight-example cap on
   higher-priority V rows before considering H. The full artifact manifest did
   contain H examples, but the shortlist was not plane-balanced. Corrected
   selection reserves scored examples from both planes before category-diverse
   fill, so the H limitation cannot disappear from review.
6. The first block bootstrap wrapped each collection or turn series end back to
   its beginning, even though those endpoints are not adjacent. It also labeled
   a sign-count balance as rank-biserial effect. Corrected inference uses
   non-circular blocks and the matched-pairs rank-biserial correlation based on
   ranked absolute differences.
7. `visibility_duration_turns` in the subset-search rows used the entire fit
   span whenever any one window crossed the visibility threshold. The score and
   selected membership did not use this field, but downstream BPM-duration
   summaries would be overstated. Corrected code spans the first through last
   actually visible window, and the active run is repaired from cached spectra
   before statistics with before/after hashes and a row-level audit.
8. Several compatibility plots had authoritative names but reused the same BPM
   inclusion bars, cluster H/V scores were blank, and the handoff top-5 count
   was constant because five channels were ranked even when none were visible.
   Corrected artifacts are data-specific, key poster candidates use the native
   PNG renderer, and handoff sets now contain only strict visible channels with
   explicit loss, recovery, empty-set, and persistent-handoff states.
9. The first continuation script called the full pipeline with `--resume`, but
   that flag only resumes spectral-cache files and would have repeated the
   completed multi-hour subset search. The continuation now invokes only the
   repair and downstream evolution/statistics/clustering/artifact/report stages.
10. Primary paired statistics read the configured 10,000 permutation samples
    but silently capped execution at 5,000. The corrected block sign-flip test
    honors the declared count exactly (with only the existing 100-draw floor),
    so the method text and executed Monte Carlo sample size cannot diverge.
11. Standalone ridge-density and density-difference PNGs divided the plot
    height by the tune-bin count with integer truncation. The unused remainder
    left the heatmap short of the declared tune axis while percentile tracks
    used the full axis. Paired H/V panels already used proportional cell bounds;
    both the current full-buffer sidecar and the original
    `gpu_analyze_captured_spills.py` density renderer now apply those bounds to
    every density panel, disclose P98 display clipping, and distinguish count
    from normalized-fraction color. The archived favorite PNGs remain immutable
    historical outputs; regression tests require complete, gap-free axes for
    future renders.
12. The first strict follow-up verifier treated an unavailable tune as corrupt
    even when the row correctly represented no visible tune: 19,004 fixed or
    dynamic control rows had `visible_fraction=0`, `score=0`, and no finite
    prominence, while 504,074 held-out rows had exact selected cardinality but
    no finite finalist `q_hat`. The held-out producer also converted an empty
    comparison (`NaN >= 3`) into a misleading candidate fraction of zero.
    Corrected rows retain explicit `NO_VISIBLE_TUNE`/`NO_VALID_Q` flags, leave
    every unavailable metric blank, and report evaluable counts/fractions.
    Verification still requires exact identities and finite support metrics
    whenever `q_hat` exists.
13. The publication scaffold rejected placeholder copy but did not bind final
    H/V ensemble sizes, numerical text, table rows, and exact figure files to
    one set of accepted analysis roots. It could therefore be populated by a
    stale or single-N image without violating the layout gate. Corrected
    materialization requires every primary/follow-up/Best-N/intensity/ridge
    report, preserves independent H/V N in a mixed four-panel ridge image, and
    writes a machine-readable results payload plus source hashes.
14. The paper build's unresolved-placeholder regex also rejected the valid
    JACoW declaration `\documentclass[]{jacow}` because it treated every empty
    LaTeX option list as a placeholder. The corrected gate still rejects
    bracket placeholders containing whitespace but permits syntactically valid
    empty option lists.
15. Tectonic's default client attempted network initialization even with a
    complete local TeX bundle and could fail in macOS system configuration.
    The paper build now accepts `TECTONIC_FLAGS`, including `--only-cached`, so
    resource-download policy is explicit. In a permitted context, Tectonic
    0.16.9 using its coherent cached bundle passed the complete layout smoke:
    exactly four `595 x 792 bp` JACoW pages, no overfull boxes or unresolved
    references, and every font embedded, subset, and Unicode-mapped. The final
    compile remains pending only because it must consume the accepted real-data
    outputs; the synthetic smoke is layout proof, not a scientific deliverable.
16. The manuscript prose still embedded corrected primary-score values and the
    first intensity-study row count as literals. Expanding the final intensity
    N grid could therefore leave a verifier-clean table beside stale prose.
    Publication materialization now generates `results_macros.tex` directly
    from the accepted primary and intensity rows, and the paper build requires
    and hashes that file together with the table and four bound figures.
17. The publication README promised a complete manifest and compliance report,
    but no command generated them or required a human visual-QA disposition.
    `finalize_ibic2026_publication.py` now verifies the immutable references,
    page geometry, render sizes, required sources, selected-N/sensitivity/
    transfer payload, zero retained intensity effects, and unresolved copy;
    only explicit poster and paper visual-QA passes can produce final closure.
18. The PDF font gate read fixed whitespace columns from `pdffonts`. A valid
    embedded `CID Type 0C` font contains spaces in its type name, shifting the
    `emb/sub/uni` fields and causing a false failure. Both paper and poster gates
    now read those three status fields relative to the right edge, where the
    trailing object and generation numbers have stable positions.
19. The first definitive N=1-40 launch ran all four logical shards concurrently
    on Spark's single GB10. The evaluators consumed about 105 GiB initially and
    climbed above roughly 115 GiB of unified memory, making the host
    unresponsive after only 10/1000 curve keys per shard and forcing a reboot.
    Those partial rows are resume checkpoints, not accepted evidence. Recovery
    keeps four logical shards for deterministic coverage and began with one CUDA
    evaluator at a time. A later controlled two-evaluator qualification held
    GPU utilization mostly near 70-96%, peaked near 83 GB (77 GiB) host use
    with about 44 GiB still available, and never crossed its 32 GiB
    `MemAvailable` floor.
    The definitive recovery therefore permits at most two max-N=40 evaluators
    in one process group with three-sample/5-second memory-watchdog termination
    and resumable checkpoints. Four-way execution remains prohibited. The same
    bounded mode may run two independent Best-N sensitivity evaluators; the
    intensity and ridge stages remain serialized because their memory profiles
    have not been qualified for overlap.
20. A legacy-versus-selected Best-N density panel alone would combine two
    effects: repair of the flawed normalized-single selector and the benefit of
    using more than one corrected adaptive BPM. The final ridge gallery now
    requires an exact-paired corrected Best-1-versus-selected H/V panel and a
    legacy/corrected-Best-1/selected-Best-N three-column panel on one probability
    scale. Only the Best-1-to-selected transition isolates ensemble-size gain.
21. The Delivery Ring producer source can substitute device-coded values such
    as HP101 position `1.01` and intensity `1101` below threshold, and its
    bind-mounted Python changed after the representative dataflow process
    started. A 2026-07-10 live audit therefore checked data behavior rather
    than trusting the checkout: one exact HP101 event had zero fallback values
    in both `TBT_*_RAW` arrays and 221,215 fallback values in both scaled
    arrays, with one shared Redis ID. Active dataflow30 logs repeatedly report
    the HP303/VP304 one-turn correction. Publication closure now additionally
    requires an exhaustive first-50000-turn scan of all 263,983 captured raw position
    rows and 23,999 paired raw intensity rows; any nonfinite sample, long exact
    plateau, or repeated device-coded raw fallback pair is fatal.
22. The poster and manuscript identified 2,000 primary spills and five folds
    but did not distinguish the 4,000 H/V spill-plane cases used for the full
    N curve from the 1,000 evenly stratified cases used for digitizer-disjoint
    validation. Publication materialization now reads those counts and the
    160,000/200,000 curve/validation evaluation-row totals from the accepted
    block-20 verifier, generates manuscript macros, writes them into the result
    payload, and requires the finalizer to recheck the exact study design.
23. The subtractive ridge math was exact-paired probability redistribution, but
    one visible legend said `suppressed vs base` and one caption described
    suppressing diffuse noise. That language could make a visually attractive
    map sound like a physical denoising measurement. Corrected legends now say
    higher/lower ridge-pick probability, captions state the standalone P98 and
    subtractive absolute-P99 color clips, and exported counts/metrics remain
    unclipped. A regression keeps subtractive copy on ridge-pick probability
    and explicitly rejects physical-noise language.
24. The intensity-weighting subtraction renderer normalized each turn column
    but only checked that the two methods exposed the same center-turn list. The
    producer emits all four methods in one loop, yet the figure code itself
    could still compare different spill/window populations if an upstream table
    were damaged. The corrected renderer requires identical exact
    collection/spill/plane/N/window/center keys, keeps only common finite in-band
    pairs, and fails instead of drawing a mismatched panel. Its visible legend
    now says higher/lower ridge-pick probability rather than `adds`/`suppresses`;
    the manifest discloses the symmetric absolute-P99 display clip and states
    that the map does not isolate physical noise. Exact-zero fields, including
    the required Best-1 controls, are visibly labeled as no ridge-pick
    probability redistribution rather than left as apparently broken blank
    panels. The strict verifier also binds
    every method to the same spill keys, memberships, contracted window centers,
    and finite global ridge picks.
25. The intensity ridge, subtraction, and binned relationship heatmaps still
    floor-divided plot width and height by the raster-bin count. As with the
    earlier standalone ridge defect, a non-divisible axis could leave an
    unpainted strip and misregister color against percentile or median tracks.
    Every intensity raster now maps bins to proportional inclusive pixel bounds
    that cover the complete declared axes without gaps. Standalone ridge and
    binned relationship captions and visible legends disclose their nonzero-P98
    display clip; subtractive captions retain the absolute-P99 disclosure. A
    regression checks complete coverage in both normal and reversed axes, and
    strict gallery verification rejects missing clip disclosures.
26. The poster acceptance checklist required inspection of exported slide XML
    for empty structural placeholders, but final closure depended on a one-off
    manual ZIP/XML scan. A later rebuild could therefore reintroduce a blank
    title or body placeholder after visual review without failing the
    finalizer. `finalize_ibic2026_publication.py` now parses every
    `ppt/slides/slide*.xml` member read-only, rejects any `p:sp` carrying a
    `p:ph` whose DrawingML text is empty or whitespace, and records a zero
    count in the compliance report. A synthetic PPTX regression distinguishes
    empty placeholders from intentionally empty ordinary shapes.
27. The poster build ran overflow and template-fidelity checks in scratch but
    did not preserve their layout, inspection, or report files in the delivered
    build tree. Poster and paper checksum manifests were required to exist but
    finalization never recomputed their entries, and their absolute source
    paths were not portable to the review bundle. The poster build now delivers
    its layout inventory, `slides_test` inspection, zero-issue fidelity JSON and
    text reports, font report, and a portable exact checksum inventory. The
    paper build likewise writes logical package-relative checksum labels.
    Finalization recomputes both exact inventories, verifies every poster
    source-manifest content/asset/output hash and recorded PNG dimension, and
    rejects a stale or nonzero-issue fidelity report. The template-derived
    smoke passes all of these gates.
28. The paper build initialized `TECTONIC_ARGS` as an empty Bash array and then
    expanded it under `set -u`. macOS Bash 3.2 treats that empty-array expansion
    as an unbound variable, so the documented default invocation failed before
    Tectonic unless `TECTONIC_FLAGS` happened to be set. The build now branches
    explicitly between flagged and unflagged invocations. A non-compiling
    harness over the already verified four-page smoke PDF exercises both paths
    and the portable manifest writer; a real cached Tectonic rebuild remains a
    separate final-paper gate.
29. The review packager emitted per-file SHA-256 values but had no independent
    verification mode after archive transfer. A missing, extra, truncated, or
    same-size modified file could therefore survive until a reviewer noticed it
    manually, and there was no proof that every copied image appeared in the
    scrollable gallery. Packaging now recomputes the complete copied-file set,
    safe component paths, sizes, hashes, image count, and one-card-per-image
    gallery coverage before writing `PACKAGE_VERIFICATION.json`.
    `--verify-only PATH` repeats those checks read-only after copy-back and also
    verifies that the receipt matches current state. The CLI smoke passes over
    the complete template-derived poster build, and a regression detects
    same-size content tampering.
30. The local favorite-gallery provenance note still ended by permitting the
    phrase `suppression of diffuse ridge picks`, after the publication audit had
    narrowed subtraction to exact-paired ridge-pick probability redistribution.
    Because that ignored note will be included in the final visual package, its
    stale sentence could contradict the poster and paper. The local note now
    uses the same no-noise/no-accuracy/no-onset boundary, and the repository
    carries `publication/ibic2026/LEGACY_RIDGE_PROVENANCE.md` with exact local
    PNG hashes, Spark origins, archived protocol, selector-defect audit, and the
    three required fair-comparison stages. Historical PNG bytes remain
    immutable.
31. Directory packaging copied host metadata and runtime caches such as
    `.DS_Store` and `__pycache__`. The exact source snapshot would therefore
    acquire non-source bytecode merely because analysis scripts ran before the
    handoff, and legacy local galleries already contained Finder metadata.
    Copying now excludes only generated host/test/type-check/bytecode cache
    names while retaining hidden repository files and every scientific output.
    Regression coverage proves these files do not enter the manifest or copied
    component.
32. The corrected Best-1-versus-selected ridge panel isolated ensemble size
    visually, but the exported intervals and publication width figure still
    compared selected Best-N only with the defective legacy selector. The
    ensemble-size claim therefore had no matching quantitative full-buffer
    control. The ridge pass now exports an exact-paired metric row and complete
    turn grid for every adaptive N pair plus an identically zero Best-1
    self-control. The selected H/V IQR and P10-P90 figures and the paper/poster
    width panel use selected Best-N minus corrected Best-1; the wider
    legacy-versus-selected composite remains a historical visual anchor and is
    not used to estimate ensemble-size gain.
33. Publication preparation wrote `source_manifest.csv`, but finalization only
    required the file to be nonempty; it did not parse the schema, require the
    exact materialized-output inventory, or re-hash those outputs. A stale or
    modified figure, content JSON, table, macro file, payload, or preparation
    report could therefore survive this provenance gate. Preparation now records
    the exact numerical source tables as source-only hashes, and finalization
    rejects unsafe/duplicate paths, malformed hashes, missing or extra outputs,
    symlinks, and any source/output/current-byte mismatch for all 14 materialized
    publication outputs.
34. The detached publication tail trusted unversioned `RIDGE/COMPLETE` and
    `ANALYSIS_COMPLETE` marker files. If an older source revision completed
    first, the latest wrapper could skip the newly required adaptive-pair ridge
    outputs or exit before rematerializing the package. The final wrapper now
    reuses a ridge root only after the current strict verifier passes, preserves
    incompatible ridge and publication trees under timestamped `.incomplete`
    names, rebuilds publication materialization from a clean directory, and
    treats analysis completion as valid only when the marker contains the exact
    source commit.
35. The first five completed reduced-sample Best-N sensitivity runs exposed a
    real H-plane tradeoff: one structurally valid baseline run had stable N
    candidates, but low N preserved selected later-window power while larger N
    improved prominence, so no single N cleared both non-inferiority margins.
    The publication gate incorrectly treated any such unresolved run as corrupt
    output and would have encouraged a false recommendation. All seven runs must
    still verify, but final materialization now requires a strict majority
    (`4/7`) of eligible recommendations per plane, records every run's N or
    unavailable reason, and prints availability plus the observed N range in
    the poster, paper, preparation report, payload, and compliance report. A
    plane with fewer than four eligible runs still blocks publication.
36. The corrected same-metric control table included all-BPM mean and median
    rows, but the executive summary and control PNG considered only adaptive
    and frozen N=1/3/5 methods. That omission hid the strongest descriptive
    control: all-BPM aggregation scores above the small-set methods in both
    planes. The final report and a new native-PNG summary now show adaptive,
    frozen, and all-BPM methods together. Publication copy is narrowed to
    improvement over adaptive Best-1 and frozen small sets; all-BPM aggregation
    remains an explicit baseline pending a leakage-controlled head-to-head test.
37. The first poster-facing Best-N validation PNG overlaid blind full-band
    agreement with agreement conditioned near the training tune on a fixed
    0-1 axis. That mixed the primary digitizer-disjoint result with a secondary
    conditioned diagnostic and visually compressed the H/V difference. The
    final publication panel shows only blind selected-versus-held-out agreement
    with block intervals on one shared zero-based H/V scale; the conditioned
    curve remains a separate gallery diagnostic. A criterion-by-N gate matrix
    shows every exact pass/fail decision and the earliest all-gate N.
    Publication preparation regenerates the set directly from the accepted
    summary CSV.
38. Adding the final sensitivity disclosure and all-BPM limitation pushed the
    JACoW smoke manuscript onto a fifth page containing only two references.
    The horizontal-loss plot was the least inferential paper figure: it is a
    noncausal, plane-specific diagnostic, while the Best-N validation, paired
    ridge density, and paired width contrast carry the central results. The
    paper now leaves that diagnostic in the poster and review gallery, requires
    and hashes only the four PNGs it actually references, and again renders as
    exactly four `595 x 792 bp` pages. A regression binds the manuscript figure
    references to the paper build manifest so unused or missing figures cannot
    silently change the submission dependency set.
39. The declared Best-N gate exposed every pass/fail decision but did not show
    whether the exact knee was stable to nearby non-inferiority margins. The
    accepted-summary renderer now exports a 54-row post-selection criterion
    grid and native H/V matrices over blind-agreement margins 0.01/0.02/0.03,
    selected-power floors 90/95/98%, and held-out-power floors 85/90/95% while
    retaining the tune-difference and prominence rules. The declared
    0.02/95/90 cell remains boxed and authoritative. On the accepted full curve,
    H yields Best-13, Best-5, or unresolved as the agreement margin changes;
    V remains finite at Best-18, Best-12, or Best-10 across all nine power-floor
    pairs. This strengthens the V low-to-mid-teen region and confirms that H is
    criterion-sensitive; the diagnostic is not permission to retune the
    published selector.
40. The corrected reused-window control showed all-BPM aggregation above the
    adaptive small sets, but that comparison did not use the Best-N purge or
    held-out digitizers. A definitive head-to-head is now required: for every
    accepted block-20 Best-N spill-plane/fold, aggregate every training-side
    channel by mean and median, retain the held-out digitizers as the independent
    reference, and compare blind agreement, blind absolute tune difference,
    later prominence, and later power on fold-collapsed exact spill pairs. The
    implemented CPU/cache-only pass fail-closes on the accepted Best-N verifier
    and source hashes and must produce 10,000 detail rows, 8,000 paired-spill
    rows, 16 comparison rows, four summary rows, and 18 verifier-bound native
    PNGs. Selected-favored, all-training-favored, and unresolved intervals are
    all acceptable results, but no result may be omitted or used to retune the
    declared Best-N selector. Publication preparation, poster copy, manuscript
    macros, finalization, and the review package now require this control.
41. The final local review package could omit the complete ignored
    `poster_candidate_gallery`, including the two immutable favorite legacy
    ridge-density references, even when every tracked publication artifact was
    present. The packaging gate now requires all 80 legacy gallery images and
    verifies the favorite H/V hashes separately from the current generated
    gallery.
42. All seven reduced-sample Best-N sensitivity configurations completed and
    passed their strict per-run verifiers, but the matrix coordinator failed in
    its final beam-width comparison. It forwarded internal labels such as
    `beam16=/path` to a documented `WIDTH=/path` interface that parses the key as
    an integer. The runner now strips the internal `beam` prefix only for that
    comparer, with a regression test over the exact three-run command. Recovery
    must use `--resume`, preserve all seven verified run directories, regenerate
    only the comparison/gallery/index products, and write `COMPLETE` only after
    the full matrix command exits successfully.
43. The first corpus-wide payload wrapper assumed that only the intensity
    collection contained an incomplete manifest and therefore required 263,999
    position rows. The read-only scan found the exact corpus instead contains
    263,983 captured position rows: 17 manifest-level absences across 13
    explicitly `Partial` captures (6/10/1 missing rows across the two
    position-only collections and intensity collection). Every listed payload
    passes the first-50,000-turn finite/count/plateau/fallback checks, and none
    of the 16 position-only absences intersects the accepted per-spill H Best-5
    or V Best-12 membership. `compare_payload_absences_to_best_n.py` preserves
    that exact-cardinality identity join as CSV/JSON/Markdown evidence. The audit now writes and hashes an exact
    `missing_position_streams.csv`, binds the immutable 2,200-manifest inventory
    hash and per-collection incomplete topology, and rejects any drift from the
    observed 263,983/23,999 contract. This is capture completeness evidence, not
    permission to fabricate or zero-fill absent channels.
44. The first full corrected intensity refresh completed all four waveform
    shards, all 1,152,000 window rows, all three block summaries, and the
    369-image gallery, but its strict verifier rejected 4,105 Best-1 weighted
    rows. A read-only exact-field audit found no tune-bin, visibility,
    intensity, or member-count change: only nonlinear spectral-shape metrics
    moved after multiplying and dividing the sole float32 spectrum (2,574
    linear-weight rows and 1,531 square-root-weight rows; maximum absolute
    delta `7.6e-6`). The singleton combiner now copies the usable spectrum
    directly and tests require bit-exact equality. The rejected root remains
    immutable evidence; publication closure requires a clean source-bound
    four-shard rerun, all three block summaries, gallery regeneration, and a
    passing strict verifier. Loosening the tolerance or editing CSV rows in
    place is prohibited.
45. The first full 50,000-turn ridge pass completed 2,000 manifests, all nine
    requested N values, both planes, 200 PNGs, and 200 captions, but its strict
    verifier reported 1,796,222 false errors. It had equated every structural
    spill/window row with a finite ridge pick, required constant full-source
    counts for exact finite intersections, rejected the peak picker's bounded
    parabolic edge refinements, and applied landscape width/height minima to
    valid 800x1250 portrait figures. A read-only reconstruction found every
    sliding table has exactly 360,000 rows per plane on the 2,000-spill,
    180-center grid and that all tested adaptive aggregate/per-turn counts match
    their finite in-band intersections exactly, including H Best-1/Best-5
    `357943`, V Best-1/Best-12 `288457`, and H Best-30/Best-40 `59208` points.
    The corrected verifier reconstructs compact per-center spill masks, keeps
    blank and edge-excluded picks visible, enforces the algorithmic two-FFT-bin
    refinement bound, reconciles variable per-turn pair counts, and accepts
    minimum figure dimensions independent of orientation. Malformed grids,
    identities, cardinalities, encoded nonfinite values, impossible legacy
    counts, and larger band excursions still fail. The preserved completed
    root passes the corrected verifier diagnostically with zero errors and zero
    warnings; source-bound closure and final marker regeneration remain
    required before publication use.
46. The first autosweep parallel-smoke receipt counted every visible GPU
    application. Spark already hosted an unrelated `pip2va` service, so the
    service plus one analyzer could satisfy a superficial two-process check.
    The corrected receipt is schema v2 and identifies only analyzer PIDs whose
    command line is bound to the smoke root; it also proves overlap from their
    completed run intervals while preserving the unrelated process in total-GPU
    telemetry.
47. The corrected autosweep smoke initially expected two result rows even
    though two configurations crossed with two collections plus their combined
    view produce six deterministic jobs. The final gate derives this product
    from the submitted grid, requires all six rows to be `ok`, and records two
    exact analyzer PIDs overlapping for 3.927 seconds. Spark remained above the
    declared memory floor throughout the bounded two-worker run.
48. Final publication-copy review found that the poster and manuscript treated
    the nominal 60 H plus 60 V topology as complete in every primary spill,
    omitted selected-ridge finite coverage, quoted a positive legacy-to-selected
    V shared-mass point estimate despite an unresolved interval, and described
    improvement without naming the blind-agreement/tune-delta result or its
    power-support tradeoff. Materialization now derives the 16 source absences
    across 12 flagged partial primary captures and selected H Best-5/V Best-12
    finite, blank, and bounded edge-excluded counts from accepted verifier
    reports. Generated poster copy reports finite coverage, uses exact paired
    support counts for the legacy visual, labels selector repair, and narrows
    the conclusion to internal blind agreement and selected/held-out tune delta.
    The accepted abstract remains byte-for-byte unchanged.

Measured legacy member retention against the exact subset masks was about 48%
for Best-1/3/5. Best-1 had 2056 of 4000 rows with zero exact-member retention;
Best-3 had 581; Best-5 had 155. These values diagnose artifact reconstruction,
not the primary mask-backed score rows. Corrected code now serializes
`bpm_indices`, exact `HPnnn`/`VPnnn` tokens, full source keys, and digitizers,
and all legacy normalization resolves the bit mask before labels.

The active publication protocol adds three independent tests:

- Best-N: select members from a fit-window prefix, purge every overlapping
  window, evaluate on later windows, and compare the inferred tune with channels
  from disjoint digitizers. Confidence intervals bootstrap spills after folding
  repeated digitizer partitions within each spill. Limited samples are evenly
  spaced within collection/plane, blind agreement searches the full tune band,
  conditioned support near the training tune is reported separately for both
  selected and held-out channels, and confidence intervals use a moving-block
  bootstrap within each collection. The final N must also transfer when chosen
  on either acquisition collection and evaluated on the other. Beam-width,
  fit-window-count, digitizer-fold-seed, and 10/20/40-spill bootstrap-block
  sensitivity checks are required.
- Best-N outputs are accepted only after `verify_best_n_outputs.py` confirms
  exact contiguous N and fold coverage, selected-member cardinality and masks,
  finite validation metrics, nonoverlapping fit/test timing, summary/detail
  counts, cross-collection products, plot products, and at least three evaluated
  larger N values above any recommended knee. The seven unique beam/fit/fold
  sample configurations are generated by
  `run_best_n_sensitivity_matrix.py`; its shared baseline is evaluated once.
- Every Best-N, intensity, and full-buffer ridge pass writes a checksummed
  `run_contract.json` before science rows. Resume parameter drift, incomplete
  or incompatible shard contracts, and duplicate cross-shard science keys are
  fatal rather than silently deduplicated.
- Resume completeness requires exactly one contiguous row for every requested
  N and fold; beam and sensitivity comparisons require identical full key sets
  rather than silently taking intersections.
- All-training: reuse the exact accepted block-20 validation keys and fold
  assignment, but replace selected membership with every training-side channel.
  Mean and median aggregation each receive their own train-only tune estimate;
  both are evaluated on the same later windows and held-out digitizers as
  selected Best-N. Collapse folds within spill before paired moving-block
  intervals. This is the maximum fair all-channel baseline under the internal
  validation design, not a literal all-60 result and not external calibration.
- Intensity: use the 200-spill capture with exact raw position/intensity pairs;
  never multiply the position waveform by intensity; compare unweighted,
  square-root, linear, and gated spectral aggregation on purged later windows.
  Analyze the canonical N grid `1,3,5,7,10,12,15,20` plus each distinct
  accepted H/V Best-N recommendation when it is outside that grid; every shard,
  merge, verifier, and gallery must use the same explicit union.
  Select ensembles from position-only fit windows, treat collection and spill
  ordering as part of the sampling design, and use a moving-block bootstrap plus
  block sign-flip test within collection. The N=1 result is a required
  zero-effect control. Retention requires FDR-corrected directional evidence, a
  predeclared minimum practical effect, a median tune shift within tolerance,
  and at least 95% of spillwise tune shifts within tolerance. Re-summary at
  10, 20, and 40-spill block lengths must not reverse the decision.
  Every intensity density-difference panel must compare the same exact finite
  spill/window points and describe color only as column-normalized ridge-pick
  probability redistribution, with absolute-P99 clipping limited to display.
  Preserve both the common 0-1 concentration scale and a zero-based
  panel-autoscaled detail view; the latter is for within-panel method separation
  only and cannot support apparent-amplitude comparisons across N or plane.
  Preserve crossing-turn scatter on common 0-50000 x/y axes plus a separately
  labeled observed-range detail view; neither may define an extraction onset or
  imply causation, and absent crossings remain omitted rather than zero-filled.
  Preserve lag correlations on the common -1 to 1 Spearman scale plus a
  symmetric panel-detail scale; the detail view reveals shape only and does not
  change the overlapping-window, exploratory, noncausal status.

The first payload audit found that the intensity arrays advertise 250000 samples
but become structurally unreliable near turn 64000. The first 50000 turns are
clean in the smoke test and are the only range used for tune/intensity inference.
The invalid tail is a payload-integrity finding and must not be labeled beam loss.
The corrected integrity table also records advertised and on-disk sample counts
for both members of every pair; any mismatch is fatal to publication use.

The favorite `18d321dbd4fe` ridge images are not spectral-power heatmaps. They
bin one continuity-tracked `selected_tune` pick per spill and 4096/256-turn
window, so color is spill count and white curves are across-spill percentiles.
The archived command used 0-50000 turns, Hann windows, RMS-per-BPM
normalization, mean subtraction, DC-bin zeroing, a 4096-turn injection seed,
confidence threshold 2.0, tracking half-width 0.005, maximum step 0.005, and the
H 0.620-0.680 / V 0.690-0.740 bands. The paired adaptive run is verifier-bound
to that protocol; channel aggregation is the intended method difference.
Every adaptive N pair is also compared on exact common spill/window keys. The
clean publication contrast is selected Best-N minus corrected adaptive Best-1,
with an explicit zero-valued Best-1 self-control. Legacy-minus-adaptive metrics
remain useful for provenance and visual comparison but combine selector repair
with ensemble size and therefore cannot quantify the latter alone.

The completed block-aware intensity reanalysis covered 199 complete spills,
`1,152,000` purged-window rows, `12,800` spill-method summaries, and 240 paired
method-effect tests. With 20-spill moving blocks, zero directional effects were
FDR-significant within tune tolerance and zero exceeded the practical-effect
threshold. Intensity weighting is therefore rejected for the tune estimator.
Intensity remains useful only for payload-integrity and exploratory timing views;
crossing-turn and lag plots do not establish extraction timing or causation.
Publication audit then found that the 50% gate could zero every selected weight
in a window, including the sole channel at N=1. The corrected gate retains the
strongest finite channel when finite values exist. Every weighted method falls
back explicitly to unweighted aggregation when a window has no usable selected
intensity at all, restoring the declared N=1 invariance without dropping the
position spectrum.
The continuous-weight results and block-aware re-summary remain valid, but the
gate rows and final gallery require a short waveform refresh before closure.

Completion status:

| Deliverable | Status | Completion evidence |
| --- | --- | --- |
| Exact identity/ring-order implementation and regression tests | complete | local 73-test Best-BPM suite (67 pass, 6 expected process-pool sandbox skips), 9 autosweep tests, 44 Rust tests, and Python byte compilation pass; staged-source Spark rerun remains part of final handoff |
| Corrected Best-1/3/5 primary and downstream rerun | complete | primary and follow-up strict verifiers both report zero failures and zero warnings; every required fixed, held-out, artifact, and handoff product is present |
| Best-N curve through at least N=20 | in progress | full N=1-40 run and 10/20/40-block merges complete; H Best-5 and V Best-12 on the accepted block-20 curve; five of seven reduced-sample sensitivity runs verified before the clock correction, with two bounded evaluators still active at the last network-confirmed probe |
| Leakage-controlled Best-N versus all-training control | pending | implementation, strict verifier, 18-image native gallery, publication binding, and exact four-page paper smoke pass locally; full 1000-spill-plane CPU/cache run waits for the active Spark publication chain to become idle |
| 200-spill intensity hypothesis test | in progress | all four corrected waveform shards completed and still found 0 FDR-significant/0 practical effects, but strict closure exposed singleton float32 scale/divide roundoff in 4,105 Best-1 rows; bit-exact pass-through passes locally and a clean source-bound rerun is required |
| Delivery Ring producer and raw-payload integrity audit | in progress | live raw/scaled separation and HP303/VP304 roll behavior confirmed read-only on drbpm2; exhaustive first-50000-turn scan over all three publication collections pending on Spark |
| Corrected 50000-turn ridge/difference/concentration gallery | pending | exact-source-key full-buffer sidecar plus strict spill/window/pair/metric/PNG/caption verifier |
| All required handoff, fixed, held-out, artifact, and report tasks below | complete | corrected follow-up verifier passes 32,000 fixed, 800,000 held-out, 201,240 visibility, 13,104 handoff-event, 4,680 summary rows, and every required artifact |
| Fermilab-template A0 poster and visual QA | pending | current all-training-copy stress smoke passes editable PPTX, one-page A0 PDF, PDF-derived full-size PNG, zero overflow, zero-issue template fidelity, and embedded/subset fonts; verifier-bound real-data rebuild and final visual QA remain |
| JACoW-compliant four-page paper and visual QA | pending | current all-training-copy offline smoke passes exactly four pages with embedded/subset/Unicode fonts and no overfull boxes or unresolved references; verifier-bound real-data rebuild and final visual QA remain |
| Scoped commits/PRs, merged docs, and clean repository | pending | merged PR URLs and clean `git status` |

No poster or paper claim may use a provisional legacy downstream figure. The
goal remains open until every pending row above and every required task in this
document is complete or is explicitly reclassified with a written scientific
reason.
The final local review package must include the complete 80-image
`review-artifacts/poster_candidate_gallery` component. Git and the Spark source
archive intentionally exclude that directory, so the two hash-bound favorite
H/V ridge references otherwise would be missing from the delivered gallery.
The same verified local package must include
`review-artifacts/publication-run-handoff`, which preserves the source archive,
both marker-gated wrappers, accepted abstract, supplied POTX, audited poster
starter/layout, coherent offline Tectonic bundle, prepared GitHub text, and
their checksum manifest outside ephemeral temp storage.

## 2026-06-28 Final Review Update

Local review inputs inspected:

```text
review-artifacts/best135-20260627
review-artifacts/best-bpm-final-review-20260628
```

The important pivot is that the follow-up bundle already completed the major items that were previously listed as next work: direct fixed-set recomputation, held-out spectral support, handoff/window visibility analysis, and review PNG generation.

Current bottom line:

- The strongest claim is dynamic per-spill Best-3/Best-5 selection, especially in V.
- The supported tune priors for this dataset are H near `0.653-0.654` and V near `0.721-0.724`.
- The older H-around-`0.69` context is not supported by these 2000 spills unless a separate machine-state reference says otherwise.
- Frozen fixed BPM sets are not validated. Direct recomputation collapses H fixed top1/top3/top5 medians to zero and leaves V fixed sets far below dynamic Best-3/Best-5.
- BPM `10.200.22.62` is a recurring strong contributor, not a standalone tune monitor.
- Held-out spectral support is strong for V Best-3/Best-5 and weak or threshold-limited for H.
- The handoff pass is useful qualitatively, but the v1 strict `VISIBLE_TUNE` threshold is too severe: almost everything becomes `WEAK_TUNE` or `NO_RELIABLE_TUNE`.
- The poster PNGs in `review-artifacts/best-bpm-final-review-20260628/followups/artifacts/poster/` are review-ready, not yet final physics-publication figures.

Use this as the go-forward story for the IBIC poster:

```text
Dynamic small-BPM ensembles recover repeatable tune-like vertical-plane structure near q ≈ 0.722 more consistently than one BPM, frozen BPM sets, or all-BPM averages in this unlabeled BPM-only dataset. Horizontal-plane results improve by score but remain weak by strict visibility, so H should be treated as a limitation and future-work path.
```

## Scope Split: IBIC Poster Vs Thesis Validation

Keep the IBIC poster and the SDR/Schottky thesis work separate.

### IBIC poster scope

The IBIC poster should be a BPM-only adaptive-ensemble result:

```text
Synchronized distributed BPM turn-by-turn streams can be mined to identify adaptive small-BPM ensembles that recover repeatable tune-like spectral structure, especially in the vertical plane.
```

Primary poster claims:

1. Coherent spill snapshots can be built from distributed BPM data streams.
2. Tune observability is not concentrated in one globally best BPM.
3. Dynamic Best-3/Best-5 BPM ensembles outperform Best-1.
4. Frozen fixed BPM sets do not reproduce dynamic per-spill performance.
5. The vertical plane is the cleanest poster result; the horizontal plane is a limitation and future-work case.

Do not frame the IBIC poster as a Schottky replacement, SDR comparison, or absolute tune-truth validation result. Use language such as `tune-like spectral structure`, `tune candidate`, and `BPM-only internal consistency` unless an independent reference is actually included in the analysis.

### Thesis / SDR / Schottky validation scope

Keep the following as separate thesis or future-validation work, not required IBIC poster scope:

1. Compare BPM-derived tune candidates against Schottky or SDR references.
2. Add controlled tune-knob or machine-state scans.
3. Validate absolute tune accuracy against an independent reference.
4. Convert the BPM-only candidate into a calibrated operational tune measurement.
5. Study whether SDR/Schottky and adaptive BPM ensembles can provide complementary online diagnostics.

## Current Context

The project is now organized around a clear instrumentation workflow:

```text
Redis BPM streams
→ synchronized raw spill capture
→ offline Rust analysis
→ Spark/GPU autosweep
→ Best-BPM mining
→ selected reports/artifacts
→ physics review and poster figures
```

The immediate active data product is the focused Spark Best-BPM mining run over the 2000-spill Tier A position-only dataset. This is not a generic autosweep anymore. It is a targeted search for tune-sensitive BPMs and small BPM subsets.

Current run root:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

The run intentionally searches only subset sizes:

```text
1, 3, 5
```

Best-10 is deferred because the earlier broader run was too slow.

The restart reuses expensive outputs from the earlier run:

```text
/home/derekste/best_bpm_mining_20260624_full_v2/cache
/home/derekste/best_bpm_mining_20260624_full_v2/manifest
/home/derekste/best_bpm_mining_20260624_full_v2/per_bpm
/home/derekste/best_bpm_mining_20260624_full_v2/consensus
```

Do not delete, overwrite, or broadly mutate Spark run outputs without explicit user approval. Follow-up passes should write sidecar outputs first unless the user asks to update the canonical run tree.

## Current Spark Completion Check

A bounded read-only Spark check on 2026-06-27 21:04 CDT showed that the focused run completed and passed verification.

Run root:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

Progress summary:

```text
subset_search: ok, about 33908 s
shard progress: 4000 / 4000 rows complete
evolution: ok, about 727 s
statistics: ok
clustering: ok
artifact_selection: ok
artifacts: ok
report: ok
verify: ok
```

Verification summary:

```text
status: ok
failures: 0
warnings: 0
subset sizes: 1, 3, 5
spills inventoried: 2000
usable spills: 2000
BPM index rows: 120
subset rows: best1=4000, best3=4000, best5=4000
finalist reevaluation rows: 799988
selected poster-review spill-plane artifacts: 79
run root size: about 1.1 GiB
```

Important output locations:

```text
reports/strong_bpm_executive_summary.md
reports/strong_bpm_analysis_summary.md
logs/best_bpm_verification_report.md
statistics/paired_method_tests.csv
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
statistics/subset_size_pareto.csv
evolution/finalist_reevaluation.csv
artifact_selection/artifact_manifest.csv
artifacts/global/
artifacts/spills/
```

The run is no longer the bottleneck. The next phase is interpretation, physics validation, and poster-grade figure generation.

## Latest Results Interpretation

The completed best1/3/5 run gives a clear direction.

### Subset size result

Paired method tests show strong improvements from adding BPMs:

```text
H best1 → best3: median score improvement ≈ 0.0392, effect size ≈ 0.999
H best3 → best5: median score improvement ≈ 0.0101, effect size ≈ 0.753
V best1 → best3: median score improvement ≈ 0.0542, effect size ≈ 1.000
V best3 → best5: median score improvement ≈ 0.0285, effect size ≈ 0.930
```

Interpretation:

- best3 clearly beats best1 in both planes,
- best5 still improves over best3, especially in V,
- the poster should emphasize **small tune-sensitive BPM ensembles**, not just one magic BPM,
- best10 should remain deferred until best5 results are understood and properly visualized.

### Plane asymmetry

The subset-size Pareto table shows:

```text
H median visible_fraction: 0.0 for best1, best3, best5
V median visible_fraction: 0.0 for best1, 0.3125 for best3, 0.625 for best5
```

Interpretation:

- V is much more ready for a strong physics/poster claim,
- H still has useful ranking structure but weak visibility under the current thresholds,
- do not force H and V into the same conclusion,
- H likely needs better visibility thresholds, stronger held-out spectral validation, or tuned deconstruction review.

### BPM rank stability

Collection-to-collection rank stability is encouraging:

```text
H Spearman ≈ 0.752, Kendall ≈ 0.582
V Spearman ≈ 0.904, Kendall ≈ 0.763
```

Interpretation:

- BPM quality is not random,
- V BPM ranking is especially stable,
- stable BPM pools are useful as priors, QA checks, and seeded dynamic candidates,
- the 2026-06-28 direct recomputation shows that frozen fixed sets should not be promoted as validated operational sets under the strict current metric.

### Leading BPMs

The same BPM appears near the top for both planes:

```text
acsys_DeliveryRingBPM 10.200.22.62
```

Top-1 frequency is modest, roughly 6%, which means no single BPM dominates the full dataset. That supports the ensemble story: tune visibility appears distributed across a population of useful BPMs, not controlled by one channel.

### Consensus quality

Within-spill consensus class counts were:

```text
CLEAN_CONSENSUS: 1606
MULTIMODAL: 2101
WEAK_CONSENSUS: 252
NO_CONSENSUS: 41
```

Interpretation:

- there is substantial BPM tune structure, but many spill-plane/window cases are multimodal,
- this supports the need for deconstruction plots, handoff analysis, and selected artifact review,
- avoid any claim that every spill has a single simple tune ridge.

### Artifact caveat

The verifier passed, but the existing generated artifacts are mostly contract/smoke artifacts. Several per-spill artifacts are `.txt` fallbacks rather than poster-quality plots.

Do not treat the current `artifacts/` tree as the final poster figure set.

## Updated Immediate Direction

The project should now move from computation to review-quality interpretation.

Recommended priority order:

1. Revise the IBIC abstract/title around adaptive BPM ensembles rather than generic turn-by-turn tune measurement.
2. Select 4-6 poster figures: global Best-1/3/5 performance, V-plane BPM inclusion, one strong V-plane subset spectra overlay, the matching V-plane BPM deconstruction, an optional H-plane or multimodal caution example, and a simple workflow diagram.
3. Reproduce the older favorite ridge-density visual grammar with the Best-ensemble method, using a targeted 50k full-buffer sidecar rather than a broad new search.
4. Polish poster PNGs for presentation: clear tune-axis labels, fewer decimal places, explicit plane labels, and clear `BPM-only tune candidate` wording.
5. Retune language around visibility. Avoid overclaiming `measured machine tune`; use `tune-like spectral structure`, `tune candidate`, or `BPM-only internal consistency`.
6. Clean up report semantics so Best-10 is not mentioned in Best-1/3/5-only runs and old fixed-set overlap is not mistaken for direct fixed-set performance.
7. Keep external tune-reference work, Schottky comparison, SDR validation, and controlled tune scans in the separate thesis/future-validation track.

Do not run another broad search yet. Best-10 is not the next bottleneck. The IBIC poster story is already supported by Best-1/3/5; the immediate bottleneck is framing and figure polish.

If checking Spark again, keep probes bounded and read-only and verify the live
host state directly.

## 2026-06-28 Implementation Completion Update

All Phase 0-5 tasks below have now been implemented or verified as completed
against the Best-1/3/5 run and follow-up sidecar outputs. Best-10 remains
deferred.

New completed ridge-density comparison:

```text
Spark:
/home/derekste/best_bpm_mining_20260627_best135_from_v2/followups/next_steps_20260628/ridge_density_best_ensemble

Local:
review-artifacts/best-bpm-ridge-density-20260628
```

The full-buffer ridge-density sidecar reuses the completed early-window
Best-1/Best-3/Best-5 memberships, recomputes `0-50000` turn raw-spill spectra,
and writes Best-1/3/5 H/V density heatmaps, pairwise density-difference maps,
turn-concentration plots, metrics, captions, and loss-candidate summaries. It
is a fair first comparison to the old `18d321db` visual grammar, not a full
50k dynamic subset search.

Key full-buffer ridge-density findings:

```text
V Best-5 improves median IQR width from 0.0186 to 0.0168 and peak-bin fraction
from 0.0165 to 0.0175 relative to V Best-1.

H Best-5 improves median IQR width from 0.0249 to 0.0233 and peak-bin fraction
from 0.0135 to 0.0140 relative to H Best-1.

H concentration peaks near turn 4096 and falls below half peak by about turn
5632; this is earlier than the 10000-20000 turn extraction-review band, so do
not claim that extraction timing has been identified as the cause.
```

Known deficiencies are tracked in GitHub issue #39:

```text
https://github.com/fermi-ad/tbt-monitor/issues/39
```

## Repo State Summary

Important files/docs already reviewed:

```text
README.md
docs/PHYSICS.md
docs/ANALYSIS_CHECKLIST.md
docs/SPARK.md
docs/ANALYSIS_CHAINS.md
config/best_bpm_mining.yaml
scripts/run_best_bpm_pipeline.py
scripts/bpm_mining/pipeline.py
scripts/bpm_mining/spectra.py
scripts/bpm_mining/peaks.py
scripts/bpm_mining/consensus.py
scripts/bpm_mining/subset_score.py
scripts/bpm_mining/subset_search.py
scripts/bpm_mining/evolution.py
scripts/bpm_mining/statistics.py
scripts/bpm_mining/artifact_selection.py
scripts/bpm_mining/plots.py
scripts/bpm_mining/report.py
scripts/bpm_mining/verification.py
```

The repo is mature enough to support an IBIC poster. The remaining IBIC work is turning the completed BPM-only mining output into a focused adaptive-ensemble story with poster-grade figures.

## Strong Current Assumptions

### 1. BPM turn-by-turn data contains real tune information near injection

This is the strongest current result. The analysis repeatedly finds tune-visible spectral structure near the expected early-injection regions.

Poster-safe claim:

```text
BPM TBT data contains reproducible tune-sensitive spectral evidence near injection.
```

Avoid claiming full-spill tune tracking unless later outputs prove it.

### 2. Expected tune anchors are soft priors, not labels

Current Best-BPM config uses:

```text
H expected tune: 0.65
V expected tune: 0.72
H discovery band: 0.60–0.70
V discovery band: 0.67–0.75
```

However, `docs/PHYSICS.md` still says:

```text
Qx ~ 0.69
Qy ~ 0.71
```

This mismatch should be reconciled before the poster or physics review. Based on the current study context, prefer wording like:

```text
For this data set, observed early-injection tune clusters are near H ≈ 0.65 and V ≈ 0.72.
```

Do not use the anchors as hard truth labels because machine settings changed during data acquisition.

### 3. Machine settings varied during the 2000-spill collection

The data was captured asynchronously while machine parameters were being adjusted. Therefore:

- do not assume chronological tune trends,
- do not assume spill-to-spill continuity,
- do not score methods by closeness to neighboring spills,
- do not average all spills as if they were the same machine state.

Primary scoring should be within-spill:

- BPM consensus,
- held-out BPM support,
- peak quality,
- subset stability,
- visibility classification,
- collection-to-collection BPM rank stability.

### 4. Dynamic best-BPM selection has look-elsewhere bias

The autosweep and current mining logic can identify strong single BPMs, but searching 60 BPMs means one BPM can look good by chance. Dynamic best-BPM results must be defended with:

- held-out BPM support,
- best-1 vs best-3 vs best-5 paired comparisons,
- fixed-set cross-validation,
- per-spill BPM tune deconstruction plots.

### 5. Best-1 and best-3 are globally exhaustive; best-5 is not

The current subset search is:

```text
best1: globally exhaustive over valid BPMs
best3: globally exhaustive over valid BPMs
best5: exact within a screened pool, with audit metadata
```

Do not describe best-5 as globally exhaustive over all BPMs.

## Key Implementation Findings

### Spectral cache

`scripts/bpm_mining/spectra.py` computes cached per-BPM spectra for the configured windows. Current cache configs are early/injection focused:

```text
injection_2048
injection_4096
early_2048_256, 0–10000 turns
early_4096_256, 0–15000 turns
```

This means the current mining run primarily supports early-spill tune evidence. It does not prove full 100 ms tune tracking.

### Per-BPM features

`scripts/bpm_mining/peaks.py` extracts up to three local spectral candidates per BPM/window and records:

- peak tune,
- peak power,
- prominence,
- local background ratio,
- peak width,
- second-peak ratio,
- entropy,
- distance to band edge,
- distance to expected anchor.

This is the right feature basis for Best-BPM mining.

### Within-spill consensus

`scripts/bpm_mining/consensus.py` clusters per-BPM tune candidates into internal consensus labels:

```text
CLEAN_CONSENSUS
WEAK_CONSENSUS
MULTIMODAL
NO_CONSENSUS
```

This is an internal BPM-only pseudo-reference. It must not be described as ground truth.

### Subset scoring

`scripts/bpm_mining/subset_score.py` scores candidate subsets using:

- held-out support,
- peak quality,
- consensus agreement,
- window stability,
- diversity score,
- ambiguity penalty,
- visible fraction.

The score is directionally good. However, the current `holdout_support` uses per-BPM median candidate tunes from `per_bpm_spill_summary.csv`, not a full per-window held-out spectral-power check. Treat it as useful ranking evidence, not final physical validation.

### Evolution pass

`scripts/bpm_mining/evolution.py` has two layers:

1. a summary derived from subset-search rows, and
2. finalist re-evaluation using cached rolling spectra and several aggregators.

The finalist re-evaluation is more trustworthy than the raw `visibility_duration_turns` from subset search. The raw search duration can overstate true visibility because it may represent the span of the cached search windows rather than a robust continuous visible interval.

### Statistics pass

`scripts/bpm_mining/statistics.py` writes many useful tables, but one important limitation was identified:

The current fixed-set cross-fit logic does not truly recompute spectra for a frozen fixed BPM set on the held-out collection. It mostly measures overlap between dynamic winners and fixed member lists. A rigorous fixed-set evaluation should recompute combined spectra for the frozen set on every test spill.

### Artifact generation

`scripts/bpm_mining/plots.py` currently satisfies output contracts, but several plots are placeholder-style rather than poster-grade:

- `bpm_tune_deconstruction` is currently a selected-membership bar chart, not a BPM-vs-tune spectral heatmap.
- `subset_spectra` is currently a score scatter, not an overlaid spectrum comparison.
- `subset_evolution` is currently a point plot, not a visible-window tune evolution plot.
- Some named global plots reuse the same top-k bar chart style.

The tables may be useful, but poster figures still need a follow-up plotting pass.

## Missing Validation Evidence

### 1. Direct fixed-set evaluation

Need a follow-up pass that actually evaluates frozen fixed BPM sets from cached spectra:

```text
rank fixed top-N on collection A
combine exactly those BPMs on every collection B spill
score resulting spectra
reverse A/B
```

This is stronger than dynamic/fixed overlap.

### 2. Stronger held-out spectral support

For finalists, compute held-out support from actual held-out BPM spectra in the same windows, not only per-BPM summary candidate tunes.

Useful finalist metric:

```text
At q_hat, what fraction of non-selected BPMs have above-background spectral power or local candidates within tolerance?
```

### 3. Poster-grade per-spill deconstruction plots

Need real plots showing:

- x-axis: tune,
- y-axis: BPM index or ring order,
- color: row-normalized log spectral power,
- markers: per-BPM peak candidates,
- vertical line: within-spill consensus tune,
- highlighted best1/best3/best5 members.

These will be the most convincing human-review plots.

### 4. Overlaid subset spectra

For selected spills, overlay spectra for:

```text
best1
best3
best5
all-BPM mean
all-BPM median
fixed top-N if available
```

Mark q_hat and within-spill consensus. This should replace the placeholder `subset_spectra` artifact for poster use.

### 5. Visibility duration with visible-window masking

Use finalist re-evaluation or a dedicated pass to show tune only in visible windows. Do not draw continuous tune traces through `NO_RELIABLE_TUNE` regions.

## Next Five Analysis Questions

### 1. Does best-3 or best-5 beat best-1 in paired comparisons?

Inspect after run completion:

```text
statistics/paired_method_tests.csv
statistics/subset_size_pareto.csv
evolution/subset_size_comparison.csv
evolution/finalist_reevaluation.csv
```

Decision:

- If best-3 or best-5 improves clearly, the poster should emphasize small BPM ensembles.
- If best-1 dominates, the poster should emphasize identifying tune-sensitive BPMs and avoiding all-BPM dilution.

### 2. Are the top BPMs stable across the two 1000-spill collections?

Inspect:

```text
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
```

Decision:

- Stable rankings imply fixed operational BPM subsets are plausible.
- Unstable rankings imply adaptive per-spill selection is required.

### 3. Are high-scoring subsets supported by held-out BPMs?

Inspect:

```text
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
```

Plot or summarize:

```text
subset_score vs holdout_support
subset_size vs holdout_support
q_hat - consensus_tune
holdout_support distribution by plane and subset size
```

This is the defense against overfitting noise.

### 4. Which spills and subsets are best for physics review?

Start from:

```text
artifact_selection/artifact_manifest.csv
reports/strong_bpm_analysis_summary.md
```

Then generate real poster-grade plots for only a small number of selected spills:

- clean consensus examples,
- best1 examples,
- best3/best5 improvement examples,
- dynamic-vs-fixed agreement/disagreement examples,
- multimodal/failure examples.

### 5. Can a frozen fixed BPM set be evaluated directly?

This is the highest-value follow-up job after the current run finishes.

Implement a cache-based pass that evaluates fixed top-N sets directly, rather than using dynamic-overlap proxies.

Compare:

```text
dynamic best1/3/5
fixed top1/3/5 trained on collection A and tested on B
fixed top1/3/5 trained on collection B and tested on A
all-BPM mean
all-BPM median
```

This is likely more valuable than immediately running best10.

## IBIC Poster Readiness

Current estimated readiness after repo inspection and focused-run completion:

```text
Platform / repo maturity:        85–90%
Data capture story:              90%
Spark mining implementation:     80–90%
Current best1/3/5 run:           complete and verifier-clean
Physics validation evidence:     65–75%
Poster-quality figures:          40–60%
Poster narrative:                80%
```

Overall:

```text
Current state: 80–85% poster-ready
If true fixed-set evaluation and real deconstruction plots are added: 90%+
```

Recommended poster framing:

```text
Mining Delivery Ring BPM Turn-by-Turn Data for Tune-Sensitive BPM Subsets
```

Recommended main claim:

```text
We captured and mined 2000 full-ring BPM turn-by-turn spills using GPU-assisted spectral analysis to identify BPMs and small BPM subsets that provide reproducible within-spill tune evidence near injection.
```

Recommended secondary claim:

```text
The method quantifies when BPM tune evidence is visible and avoids forcing tune estimates in low-confidence regions.
```

Do not frame this yet as a completed replacement tune monitor.

## First Actions After Focused Run Completion

1. Keep the completed run immutable until review artifacts are copied or sidecar passes are verified:

```text
/home/derekste/best_bpm_mining_20260627_best135_from_v2
```

2. Inspect these first:

```text
reports/strong_bpm_executive_summary.md
reports/strong_bpm_analysis_summary.md
statistics/paired_method_tests.csv
statistics/bpm_global_statistics.csv
statistics/bpm_rank_stability.csv
evolution/finalist_reevaluation.csv
artifact_selection/artifact_manifest.csv
logs/best_bpm_verification_report.md
```

3. Treat best-3 and best-5 paired improvements as the starting point for poster interpretation, but do not overstate best-5 search completeness.

4. Run or implement direct fixed-set evaluation from cached spectra.

5. Generate poster-grade figures from selected finalists.

## Implementation, Progress, Parallelism, And Deployment Plan

The follow-up work should be split into small passes that can be run against the completed Spark output without rerunning expensive cache or subset-search stages. Each pass should support serial mode, deterministic parallel mode, resume, bounded selected-spill mode, and sidecar output directories.

### Phase 0: Normalize handoff and docs

Goal:

```text
Make the repo branch, NEXT_STEPS.md, physics docs, and PR branch tell the same story.
```

Work:

- keep this `NEXT_STEPS.md` file on the active Best-BPM branch with Task F included,
- reconcile `docs/PHYSICS.md` so the current 2000-spill H ≈ 0.65 / V ≈ 0.72 anchors are dataset-specific soft priors, while older `Qx ~ 0.69`, `Qy ~ 0.71` language is explicitly historical or operational-context dependent,
- update `docs/SPARK.md` after new commands exist,
- keep local operational handoffs out of permanent design truth unless they are
  scrubbed and intentionally promoted into tracked docs.

Validation:

```bash
git diff --check
```

### Phase 1: Direct fixed-set evaluation

Goal:

```text
Replace dynamic/fixed overlap proxies with actual frozen BPM-set spectral evaluation.
```

Implementation:

- add `scripts/bpm_mining/fixed_sets.py`,
- add wrapper `scripts/evaluate_fixed_bpm_sets.py`,
- optionally add pipeline command `fixed-sets`,
- train fixed top-N sets on collection A and test exact frozen members on collection B, then reverse,
- compare dynamic best1/best3/best5, fixed top1/top3/top5, all-BPM mean, and all-BPM median.

Inputs:

```text
cache/index/spectral_cache.csv
manifest/bpm_index.csv
statistics/bpm_global_statistics.csv
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
```

Outputs:

```text
statistics/fixed_set_direct_evaluation.csv
statistics/fixed_vs_dynamic_direct_summary.csv
artifacts/global/fixed_vs_dynamic_direct_h.png
artifacts/global/fixed_vs_dynamic_direct_v.png
```

Progress:

```text
statistics/fixed_set_progress/parent_status.json
statistics/fixed_set_progress/shard_<n>.json
logs/progress.csv pass=fixed_set_direct_evaluation
```

Parallelism:

- shard by `(train_collection, test_collection, plane, subset_size, method, spill_chunk)`,
- load spectral arrays with `np.load(..., mmap_mode="r")`,
- have workers write chunk CSV fragments or return rows, then merge in stable sorted order,
- prove serial and parallel output equality in tests.

Spark deploy:

```text
deploy code to a new scratch directory on Spark
run sidecar output first, e.g. /home/derekste/best_bpm_mining_20260627_best135_from_v2/fixed_set_eval_probe
promote or copy into canonical statistics/artifacts only after review
```

### Phase 2: Stronger held-out spectral support

Goal:

```text
Defend dynamic subset winners against look-elsewhere bias using actual non-selected BPM spectra at q_hat.
```

Implementation:

- add `scripts/bpm_mining/heldout.py`,
- add wrapper `scripts/evaluate_heldout_spectral_support.py`,
- for finalist rows, compute held-out support near the selected q_hat from the same cached spectra and windows,
- keep H/V separate.

Output:

```text
evolution/finalist_heldout_spectral_support.csv
```

Fields:

```text
heldout_candidate_fraction
heldout_power_support
heldout_prominence_at_qhat
selected_vs_heldout_delta
heldout_bpm_count
quality_flags
```

Progress:

```text
evolution/heldout_progress/parent_status.json
evolution/heldout_progress/shard_<n>.json
logs/progress.csv pass=heldout_spectral_support
```

Parallelism:

- shard by finalist-row chunks,
- group cache loads by `(collection, spill_id, plane, spectral_config)` where possible,
- merge output deterministically by source finalist order plus aggregator.

### Phase 3: Poster-grade selected artifacts

Goal:

```text
Replace placeholder-style plots with physics-review figures for only the best selected spill-plane rows.
```

The June plan originally limited generation to a small curated set. That limit
is superseded for the publication audit: generate an exhaustive review gallery
covering all scientifically relevant Best-N, control, difference, sensitivity,
full-spill, and failure-mode views. The final poster still needs a disciplined
4-6 figure shortlist that explains the result quickly.

Selection rule:

```text
Use artifact_selection/artifact_manifest.csv plus ranking tables to select roughly:
  - 2 best V clean-consensus examples
  - 1 best H clean-consensus or H-improvement example
  - 1 best3/best5 improvement example
  - 1 dynamic/fixed disagreement or multimodal caution example
```

Final-poster cap (not a review-gallery cap):

```text
no more than 8 spill-plane examples should be recommended for the final poster;
additional review examples remain required and must be indexed rather than hidden
```

Implementation:

- extend `scripts/bpm_mining/plots.py` or add `scripts/bpm_mining/poster_plots.py`,
- add wrapper `scripts/make_best_bpm_poster_artifacts.py`,
- read cached spectra directly rather than relying on placeholder artifact rows,
- generate real BPM-vs-tune spectral heatmaps for selected rows,
- generate overlaid subset spectra for best1, best3, best5, all-BPM mean, all-BPM median, and fixed top-N when Phase 1 exists,
- generate one or two global summary plots from statistics tables,
- preserve existing artifact names where possible and add explicit `_poster` or `_overlay` files when replacing would risk confusion,
- write a small poster figure index with caption drafts and suggested poster placement.

Color/artifact ideas:

1. **BPM tune deconstruction heatmap**

   ```text
   x-axis: tune
   y-axis: BPM index or ring order
   color: row-normalized log spectral power
   overlays: per-BPM peak markers, consensus tune line, best1/best3/best5 membership badges
   ```

   This should be the main “convince the physicist” plot. Use a perceptually strong colormap such as `viridis`, `magma`, or `turbo` if acceptable. Keep H and V panels separate unless both are clean.

2. **Subset spectrum overlay**

   ```text
   x-axis: tune
   y-axis: normalized spectral power or log power
   curves: best1, best3, best5, all-BPM mean, all-BPM median
   overlays: q_hat, consensus tune, expected tune anchor
   ```

   This plot should show why small ensembles beat all-BPM averaging. Use distinct line styles and a compact legend. This is likely a poster panel.

3. **Top-N performance curve**

   ```text
   x-axis: subset size, 1/3/5
   y-axis: median subset score or held-out support
   markers: H and V separately
   optional second y/panel: visible fraction
   ```

   This should be one clean global result plot. It can be built from `statistics/paired_method_tests.csv`, `statistics/subset_size_pareto.csv`, and `evolution/subset_size_comparison.csv`.

4. **BPM inclusion/rank stability map**

   ```text
   x-axis: BPM/ring order
   y-axis or stacked bars: top1/top3/top5 inclusion frequency
   color/group: plane or collection
   ```

   This plot should show that BPM quality is not random and that V rankings are especially stable.

5. **Visibility / handoff preview plot**

   ```text
   x-axis: turn window
   y-axis: BPM index/ring order
   color: visibility score or support at consensus tune
   overlays: top1/top3/top5 membership through time
   ```

   Only generate this for the best one or two selected spills at first. Treat it as exploratory unless the handoff pass is implemented.

6. **Poster contact sheet**

   Create one image or markdown index showing thumbnails of the curated artifacts with one-line captions. This helps quickly choose final poster panels.

7. **Best-ensemble ridge-density comparison**

   The older favorite gallery plots:

   ```text
   review-artifacts/poster_candidate_gallery/05_best_poster_18d321db_ridge_density_h.png
   review-artifacts/poster_candidate_gallery/05_best_poster_18d321db_ridge_density_v.png
   ```

   came from:

   ```text
   /home/derekste/tbt-spills-2000-autosweep/elite-full/jobs/18d321dbd4fe/combined/ridge_density_h.png
   /home/derekste/tbt-spills-2000-autosweep/elite-full/jobs/18d321dbd4fe/combined/ridge_density_v.png
   ```

   They were generated by `scripts/gpu_analyze_captured_spills.py` from
   `gpu_sliding_tune.csv`, not from a median spectral-power image. The plot
   bins one continuity-tracked `selected_tune` per accepted spill/window into
   `(center_turn, tune_bin)` density. Tracking is seeded by the injection-window
   global spectral peak and then searches a local tune band; the configured
   H/V ridge anchors affected the separate DP/greedy ridge-overlay product, not
   this greedy density image. Color is spill count. The white overlays are
   per-window median and percentile curves. The source config was:

   ```text
   config hash: 18d321dbd4fe
   role: best_poster / best_v
   turn range: 0-50000
   window/stride: 4096 / 256
   BPM combination: best_single_bpm
   H band: 0.620-0.680
   V band: 0.690-0.740
   usable spills: 1988
   ```

   To compare the new adaptive-ensemble method fairly, the first pass should
   generate the same density visual grammar over the same full turn range:

   ```text
   add full-buffer cache config:
     name: poster_4096_256_full_50000
     turn_start: 0
     turn_end: 50000
     window_turns: 4096
     stride_turns: 256
   ```

   Do not start with a full exhaustive 50k subset search. First generate a
   targeted sidecar:

   1. Reuse existing dynamic Best-1/Best-3/Best-5 memberships from the completed
      Best-1/3/5 run.
   2. Combine the selected BPM spectra over the new 50k cache for each
      spill/plane/window.
   3. Pick the ridge tune within the same poster bands used by the older plot.
   4. Bin ridge tunes by center turn and tune bin.
   5. Render comparable density plots for Best-1, Best-3, Best-5, and optionally
      all-BPM mean/median baselines.
   6. Write captions explaining whether the ensemble density is narrower,
      higher contrast, or merely different.

   For every requested N, also render the primary four-panel comparison
   `ridge_density_legacy_single_vs_best<N>_hv.png`: H/V rows, legacy/adaptive
   columns, exact common spill/window points, column-normalized pick
   probability, one shared color scale, and P10/median/P90 overlays. The shared
   scale makes concentration contrast visually comparable across all four
   panels. It does not turn diffuse-pick suppression into a measurement of
   physical noise; paired counts, sample-fraction diagnostics, and quantitative
   width/entropy/mass tables remain required alongside it.
   When the accepted H and V recommendations differ, also render the
   contract-bound plane-selected comparison and selected-N concentration
   panels. Do not use the vertical recommendation as an unlabeled horizontal
   default.

   Export the exact-paired legacy/adaptive contrast at every turn center in
   `ridge_density_legacy_comparison_by_turn.csv`. Render IQR and P10-P90 width
   deltas, peak-bin-fraction gain, normalized-entropy delta, and shared-ridge-
   mass gain for all requested N and for each selected plane. The CSV remains
   unsmoothed; the review curves use only five-window visual smoothing and a
   labeled zero reference. These plots characterize cross-spill ridge-pick
   concentration and probability redistribution. They are not measurements of
   physical noise removal, extraction onset, or absolute tune accuracy.
   For every metric, also render one stacked selected-H/V composite with a
   shared y scale in both landscape and template-matched portrait forms. The
   P10-P90 landscape composite is a bound paper figure and its portrait twin
   fills the poster evidence frame; the remaining variants stay in the review
   gallery.

   Separately export every exact-paired adaptive N-to-N contrast in
   `ridge_density_adaptive_pair_comparison_metrics.csv` and
   `ridge_density_adaptive_pair_comparison_by_turn.csv`, including an exact-zero
   Best-1 self-control. Render the five selected-Best-N-minus-corrected-Best-1
   metric families as shared-scale H/V landscape and portrait figures. Use the
   clean P10-P90 landscape/portrait pair for the paper/poster width contrast;
   keep the adaptive-minus-legacy versions in the exhaustive review gallery.

   Actual completed outputs:

   ```text
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best1_h.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best3_h.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best5_h.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best1_v.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best3_v.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best5_v.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_legacy_single_vs_best{N}_hv.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best1_vs_selected_h{H}_v{V}_hv.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_legacy_vs_best1_vs_selected_h{H}_v{V}_hv.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best{3,5}_minus_best1_{h,v}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_concentration_vs_turn_{h,v}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_{iqr,p10_p90,entropy}_delta_vs_turn_{h,v}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_{peak_bin,shared_mass}_gain_vs_turn_{h,v}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_legacy_comparison_by_turn.csv
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_adaptive_pair_comparison_metrics.csv
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_adaptive_pair_comparison_by_turn.csv
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_{iqr,p10_p90,entropy}_delta_vs_turn_best1_to_selected_h{H}_v{V}_hv{,_poster}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_{peak_bin,shared_mass}_gain_vs_turn_best1_to_selected_h{H}_v{V}_hv{,_poster}.png
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_best_ensemble_index.md
   followups/next_steps_20260628/ridge_density_best_ensemble/ridge_density_*_caption.md
   ```

   Escalate to a full 50k dynamic subset search only if the targeted sidecar
   shows that the Best-ensemble ridge density is promising but the fixed
   early-window membership appears to miss late-window structure.

Required outputs:

```text
artifacts/poster/selected_poster_artifacts.csv
artifacts/poster/poster_artifact_index.md
artifacts/poster/poster_contact_sheet.png
artifacts/poster/global_topn_performance_hv.png
artifacts/poster/global_bpm_inclusion_h.png
artifacts/poster/global_bpm_inclusion_v.png
artifacts/poster/spill_<id>_<plane>_bpm_tune_deconstruction_poster.png
artifacts/poster/spill_<id>_<plane>_subset_spectra_overlay_poster.png
artifacts/poster/spill_<id>_<plane>_visible_window_tune_evolution_poster.png
```

Existing or compatibility outputs:

```text
artifacts/spills/spill_<id>_<plane>_bpm_tune_deconstruction.png
artifacts/spills/spill_<id>_<plane>_subset_spectra_overlay.png
artifacts/spills/spill_<id>_<plane>_visible_window_tune_evolution.png
artifacts/global/poster_artifact_index.md
```

Poster artifact style requirements:

- large labels readable on a poster,
- explicit plane, spill ID, window length, stride, and tune band in subtitle or caption,
- colorbar labels with clear units such as `row-normalized log power` or `visibility score`,
- H/V color distinction should be consistent across global plots,
- do not overplot 2000 spills,
- do not use rainbow clutter for line plots; use color primarily for heatmaps and density plots,
- every poster artifact should have a caption draft in `poster_artifact_index.md`,
- no more than 4-6 final figure candidates should be recommended, while the
  complete review gallery remains packaged for manual visual selection.

Recommended caption angle:

```text
Small BPM ensembles improve tune-visible evidence relative to single-BPM and all-BPM summaries. The selected BPMs are supported by a broader within-spill consensus, not merely by one isolated channel.
```

Progress:

```text
artifacts/progress/parent_status.json
artifacts/progress/shard_<n>.json
logs/progress.csv pass=poster_artifacts
```

Parallelism:

- shard by artifact manifest rows,
- workers generate files into per-shard temporary directories,
- parent writes final manifest and summary after all files exist.

### Phase 4: BPM handoff / tune-visibility migration analysis

Goal:

```text
Test whether tune observability migrates across BPMs or BPM ensembles over turn windows.
```

Implementation:

- add `scripts/bpm_mining/handoff.py`,
- add wrapper `scripts/run_bpm_handoff_analysis.py`,
- start standalone before pipeline integration,
- use existing `early_4096_256` cache and selected artifact rows first,
- keep intensity as optional future covariate; do not block position-only v1.

Outputs:

```text
handoff/bpm_window_visibility.csv
handoff/bpm_handoff_events.csv
handoff/bpm_visibility_summary.csv
handoff/handoff_summary.md
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/visible_bpm_fraction_vs_turn_h.png
handoff/visible_bpm_fraction_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
handoff/top_bpm_membership_vs_turn_h.png
handoff/top_bpm_membership_vs_turn_v.png
handoff/spill_<id>_<plane>_bpm_visibility_handoff.png
handoff/spill_<id>_<plane>_top_sets_vs_turn.png
```

Progress:

```text
handoff/progress/parent_status.json
handoff/progress/shard_<n>.json
logs/progress.csv pass=bpm_handoff
```

Parallelism:

- shard by selected `(collection, spill_id, plane)` groups,
- compute window/BPM visibility independently,
- aggregate global handoff-rate and visibility-fraction plots after worker merge.

### Phase 5: Report and verifier integration

Goal:

```text
Make new outputs first-class enough to rerun and review without private chat context.
```

Implementation:

- update `scripts/bpm_mining/report.py` to mention fixed-set direct evaluation, held-out spectral support, and handoff outputs when present,
- update `scripts/bpm_mining/verification.py` to treat new passes as optional sections unless the corresponding output directory exists,
- add concise doc commands in `docs/SPARK.md`.

Validation:

```bash
python3 -m py_compile scripts/bpm_mining/*.py scripts/evaluate_fixed_bpm_sets.py scripts/evaluate_heldout_spectral_support.py scripts/run_bpm_handoff_analysis.py scripts/test_best_bpm_mining.py
python3 scripts/test_best_bpm_mining.py
python3 scripts/verify_best_bpm_outputs.py --root /path/to/smoke --subset-sizes 1 3 5
```

Spark validation sequence:

1. deploy changed files to a new scratch code directory,
2. run fixed-set direct evaluation on 20 selected spills,
3. run held-out spectral support on 1000 finalist rows,
4. run poster artifact generation on 4 artifact-manifest rows,
5. run handoff on 4 selected spill-plane rows,
6. then run the full sidecar passes against `/home/derekste/best_bpm_mining_20260627_best135_from_v2`.

Priority order:

```text
1. Direct fixed-set evaluation
2. Stronger held-out spectral support
3. Poster-grade selected artifacts
4. BPM handoff / tune-visibility migration analysis
5. Optional full-buffer handoff cache only if selected-spill handoff is promising
```

## Suggested Follow-Up Implementation Tasks

### Task A: Reconcile tune anchors in docs

Status: done. `docs/PHYSICS.md` now treats H near `0.65` and V near `0.72` as current 2000-spill dataset-specific soft priors, while older `Qx ~ 0.69`, `Qy ~ 0.71` language is explicitly historical or operational-context dependent.

Update `docs/PHYSICS.md` so the expected tune region matches current dataset assumptions or clearly distinguishes older operational expectations from current observed early-injection clusters.

Suggested wording:

```text
For the current 2000-spill Spark Tier A study, early-injection BPM spectra cluster near H ≈ 0.65 and V ≈ 0.72. These are used as soft priors for discovery and ranking, not external truth labels.
```

### Task B: Implement direct fixed-set evaluation

Status: done. Implemented in `scripts/bpm_mining/fixed_sets.py` and `scripts/evaluate_fixed_bpm_sets.py`; full sidecar outputs are present under `followups/statistics/`.

Add a script or pipeline pass such as:

```text
scripts/evaluate_fixed_bpm_sets.py
```

or add to `scripts/bpm_mining/statistics.py` / `evolution.py`.

Inputs:

```text
cache/index/spectral_cache.csv
manifest/bpm_index.csv
statistics/bpm_global_statistics.csv
subset_search/best*/best*_results.csv
```

Outputs:

```text
statistics/fixed_set_direct_evaluation.csv
statistics/fixed_vs_dynamic_direct_summary.csv
artifacts/global/fixed_vs_dynamic_direct_h.png
artifacts/global/fixed_vs_dynamic_direct_v.png
```

### Task C: Implement real per-spill BPM tune deconstruction plots

Status: done for poster review. Cache-backed poster PNGs are emitted under `followups/artifacts/poster/`; compatibility `artifacts/spills/` entries may still include text fallbacks and are tracked as a deficiency in issue #39.

Replace or supplement placeholder artifacts with:

```text
spill_<id>_<plane>_bpm_tune_deconstruction.png
```

Plot content:

- BPM/ring order vs tune spectral-power image,
- primary candidate markers,
- consensus tune line,
- best1/best3/best5 membership annotations.

### Task D: Implement overlaid subset spectra for finalists

Status: done for poster review. Cache-backed subset overlay PNGs are emitted under `followups/artifacts/poster/` for the selected poster examples.

Add:

```text
spill_<id>_<plane>_subset_spectra_overlay.png
```

Compare:

- best1,
- best3,
- best5,
- all-BPM mean,
- all-BPM median,
- fixed top-N if available.

### Task E: Add stronger held-out spectral validation for finalists

Status: done. Implemented in `scripts/bpm_mining/heldout.py` and `scripts/evaluate_heldout_spectral_support.py`; full sidecar output `evolution/finalist_heldout_spectral_support.csv` is present in the follow-up bundle.

For finalist subsets, compute held-out spectral support in the same windows using actual held-out BPM spectra.

Output:

```text
evolution/finalist_heldout_spectral_support.csv
```

Include:

```text
heldout_candidate_fraction
heldout_power_support
heldout_prominence_at_qhat
selected_vs_heldout_delta
```

### Task F: Add BPM handoff / tune-visibility migration analysis

Status: done. The corrected Spark sidecar and semantic verifier pass over the
complete fixed, held-out, visibility, handoff-event, summary, and artifact
outputs; the strict thresholds remain a qualitative limitation rather than an
unfinished implementation task.
Implemented in `scripts/bpm_mining/handoff.py` and
`scripts/run_bpm_handoff_analysis.py`. Current thresholds are useful for
qualitative review but too strict for primary poster claims, and this
deficiency is tracked in issue #39. The current implementation emits every
selected spill-plane panel, strict Top-1/3/5/10 transitions, and all global
plots listed below.

#### Motivation

A physicist suggested that the intensity waxing/waning seen on BPM channels may reflect bunch decoherence/recoherence. If coherent transverse signal evolves during the spill, the BPMs that provide the clearest tune evidence may also change with turn number.

The tune itself is not moving from BPM to BPM. The hypothesis is that the **observability of the tune line** migrates between BPMs or BPM ensembles as coherent beam motion damps, recoheres, changes phase-space structure, or changes relative to local BPM noise/electronics.

This could explain several observed behaviors:

- all-BPM averaging can wash out useful tune evidence,
- dynamic best-BPM methods can outperform all-BPM methods,
- different BPMs may dominate at different turn ranges,
- intensity-envelope features may correlate with tune visibility,
- a static “best BPM” set may be insufficient if the useful ensemble changes through the spill.

The analysis question is:

```text
Which BPMs provide usable spectral evidence for the common tune at each turn/window, and does that set change coherently over time?
```

#### Conceptual output

For each spill, plane, BPM, and rolling window, compute:

```text
peak_tune
peak_prominence
power at within-spill consensus tune
local spectral background
second_peak_ratio
visibility_flag
optional intensity metrics
```

Then build a matrix:

```text
BPM × turn-window
```

where color represents tune visibility or support for the consensus tune.

This should produce a new class of diagnostic plot:

```text
x-axis: turn or time
y-axis: BPM index / ring order
color: tune visibility or spectral support
markers: best1 / best3 / best5 membership
overlay: consensus tune visibility / intensity envelope where available
```

Potential filenames:

```text
handoff/spill_<id>_h_bpm_visibility_handoff.png
handoff/spill_<id>_v_bpm_visibility_handoff.png
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
```

#### Proposed implementation location

Add a new module:

```text
scripts/bpm_mining/handoff.py
```

and an entry point:

```text
scripts/run_bpm_handoff_analysis.py
```

The handoff pass should also be callable from the Best-BPM pipeline after `evolution` and before `artifact_selection`, but it can start as a standalone script.

#### Inputs

Use existing Best-BPM mining outputs where possible:

```text
cache/index/spectral_cache.csv
per_bpm/per_bpm_window_features.csv
consensus/spill_consensus_windows.csv
consensus/spill_consensus_summary.csv
subset_search/best1/best1_results.csv
subset_search/best3/best3_results.csv
subset_search/best5/best5_results.csv
artifact_selection/artifact_manifest.csv
```

Optional future inputs:

```text
intensity feature tables
position+intensity captured bundles
```

Do not block the first implementation on intensity. Start with position-derived tune visibility.

#### Per-window BPM visibility metric

For each BPM/window, compute a score such as:

```text
visibility_score =
    0.40 * normalized_peak_prominence
  + 0.25 * support_at_consensus_tune
  + 0.15 * inverse_second_peak_ratio
  + 0.10 * inverse_spectral_entropy
  + 0.10 * band_edge_safety
```

where:

```text
support_at_consensus_tune =
  spectral power near q_consensus relative to local background
```

Use the within-spill consensus tune from `spill_consensus_windows.csv`.

Visibility classes:

```text
VISIBLE_TUNE
WEAK_TUNE
NO_RELIABLE_TUNE
```

Suggested initial thresholds:

```text
VISIBLE_TUNE:
  visibility_score >= 0.65
  and peak_prominence_z >= 4.0
  and distance_to_band_edge >= 0.003

WEAK_TUNE:
  visibility_score >= 0.35
  or peak_prominence_z >= 3.0

NO_RELIABLE_TUNE:
  otherwise
```

Keep thresholds configurable.

#### Handoff metrics

For each spill/plane/window, determine:

```text
top1_visible_bpm
top3_visible_set
top5_visible_set
top10_visible_set
visible_bpm_fraction
visible_bpm_count
dominant_digitizer
dominant_ring_sector
consensus_tune
consensus_label
```

Track changes over windows:

```text
top3_jaccard_vs_previous
top5_jaccard_vs_previous
top3_jaccard_vs_injection
top5_jaccard_vs_injection
handoff_score = 1 - Jaccard(topK_current, topK_previous)
```

Add persistence:

```text
handoff_persistence =
  number of consecutive windows for which the new dominant set remains stable
```

Flag likely real handoffs:

```text
PERSISTENT_HANDOFF:
  handoff_score >= 0.6
  and handoff_persistence >= 3 windows
  and consensus tune remains continuous
```

Flag likely noise flicker:

```text
FLICKER:
  high handoff_score
  but low persistence
  or consensus tune jumps
  or visibility is weak
```

#### Intensity-aware extension

Use intensity only as a covariate or quality metric.

Do not multiply position waveforms by intensity:

```text
do not use: position(t) *= intensity(t)
```

For intensity-capable captures, compute per BPM/window:

```text
intensity_median
intensity_rms
intensity_std_over_mean
intensity_envelope
intensity_drop_flag
```

Then compare to tune visibility:

```text
corr(intensity_median, visibility_score)
corr(intensity_envelope, visibility_score)
lagged_corr(intensity_envelope, visibility_score)
```

Outputs:

```text
handoff/intensity_visibility_correlation.csv
handoff/intensity_visibility_correlation_by_bpm.png
handoff/spill_<id>_<plane>_intensity_visibility_overlay.png
```

Keep intensity weighting only if it improves visibility/held-out support without shifting the selected tune.

#### Required output tables

Create:

```text
handoff/bpm_window_visibility.csv
handoff/bpm_handoff_events.csv
handoff/bpm_visibility_summary.csv
handoff/handoff_summary.md
```

`bpm_window_visibility.csv` schema:

```text
collection
spill_id
plane
spectral_config
window_index
center_turn
bpm_index
bpm_name
digitizer
consensus_tune
consensus_label
peak_tune
peak_prominence_z
power_at_consensus
local_background_at_consensus
support_at_consensus
second_peak_ratio
spectral_entropy
visibility_score
visibility_class
is_top1_visible
is_top3_visible
is_top5_visible
is_top10_visible
quality_flags
```

`bpm_handoff_events.csv` schema:

```text
collection
spill_id
plane
subset_size
window_index
center_turn
previous_members
current_members
jaccard_vs_previous
handoff_score
handoff_persistence
consensus_tune
consensus_delta
event_label
quality_flags
```

`bpm_visibility_summary.csv` schema:

```text
collection
spill_id
plane
bpm_index
bpm_name
digitizer
visible_window_fraction
first_visible_turn
last_visible_turn
visibility_duration_turns
median_visibility_score
median_support_at_consensus
top1_window_fraction
top3_window_fraction
top5_window_fraction
top10_window_fraction
handoff_event_count
```

#### Required plots

Global plots:

```text
handoff/handoff_rate_vs_turn_h.png
handoff/handoff_rate_vs_turn_v.png
handoff/visible_bpm_fraction_vs_turn_h.png
handoff/visible_bpm_fraction_vs_turn_v.png
handoff/bpm_visibility_cluster_map_h.png
handoff/bpm_visibility_cluster_map_v.png
handoff/top_bpm_membership_vs_turn_h.png
handoff/top_bpm_membership_vs_turn_v.png
```

Selected per-spill plots:

```text
handoff/spill_<id>_h_bpm_visibility_handoff.png
handoff/spill_<id>_v_bpm_visibility_handoff.png
handoff/spill_<id>_h_top_sets_vs_turn.png
handoff/spill_<id>_v_top_sets_vs_turn.png
```

Per-spill handoff plot should show:

1. BPM/ring order vs turn heatmap of visibility score.
2. Top1/top3/top5 membership markers.
3. Consensus tune and consensus quality as a lower panel.
4. Optional intensity envelope overlay when intensity data exists.

The native composite implements items 1-3 directly: score color is separate
from strict Top-1/3/5 rank markers, and the consensus-tune trace occupies its
own lower panel. Intensity remains a separate exploratory sidecar rather than
being overlaid when no exact paired payload is available.

#### Interpretation rules

Strong evidence for real BPM handoff requires:

1. A stable within-spill consensus tune exists.
2. One BPM group supports that tune early.
3. A different BPM group supports the same or smoothly evolving consensus tune later.
4. The transition persists across multiple adjacent windows.
5. The transition is not dominated by band-edge locking.
6. The phenomenon repeats across multiple spills or morphology clusters.
7. Optional: visibility changes correlate with intensity-envelope changes.

Weak evidence / likely artifact:

```text
best BPM jumps randomly
q_hat jumps with the selected BPM set
no clean consensus tune exists
handoff disappears under different window/stride
handoff is dominated by one noisy BPM
visibility is weak or band-edge locked
```

#### Recommended initial scope

Do not run this over every possible artifact immediately.

First pass:

```text
use current early_4096_256 cache
turn range: 0–15000
subset sizes: 1, 3, 5
planes: H, V
selected spills only:
  - clean consensus examples
  - best3/best5 improvement examples
  - dynamic/fixed disagreement examples
  - multimodal/failure examples
```

Second pass, only if first pass is promising:

```text
add full-buffer cache config:
  name: handoff_4096_256
  turn_start: 0
  turn_end: 50000
  window_turns: 4096
  stride_turns: 256
```

Optional high-time-resolution pass:

```text
handoff_2048_128
```

Use 4096/256 first because it is more spectrally stable.

#### Poster relevance

This analysis could become a strong poster result if it shows:

```text
Tune evidence is not uniformly distributed across BPMs. As coherent beam motion evolves, different BPM ensembles can provide useful channel-resolved visibility. Dynamic subset selection improves on a single or frozen small set in the strongest vertical results, while all-BPM aggregation remains a strong explicit baseline.
```

This would explain why Best-BPM mining is physically meaningful rather than merely a numerical optimization.

## Guardrails

- Stay BPM-only unless explicitly adding external reference data.
- Do not call within-spill consensus “truth.”
- Do not claim true turn-by-turn tune.
- Do not claim full-spill tune tracking from early-window cached analysis.
- The corrected Best-1/3/5, leakage-controlled Best-N, sensitivity, and targeted
  50000-turn ridge passes are approved by the current publication goal. Do not
  expand beyond those evidence questions or mutate source data without approval.
- Do not delete or mutate Spark data without approval.
- Generate the exhaustive indexed review gallery requested for this publication
  pass; keep only the strongest 4-6 panels in the poster itself.
- Keep H and V conclusions separate.
- Treat best poster-looking output as a candidate, not proof.

## Bottom Line

The repo and current run are ready to support an IBIC poster as a BPM-only adaptive-ensemble study. The main remaining work is not broad new computation or Schottky/SDR validation. It is selecting and polishing the clearest evidence from the completed output:

```text
best1 vs best3 vs best5 comparison
BPM rank stability and recurring useful BPM pools
held-out support as internal consistency evidence
direct frozen-small-set weakness plus the stronger descriptive all-BPM control
poster-grade V-plane deconstruction and subset spectra examples
one H-plane or multimodal caution example
```

The poster story is strong if it stays scoped correctly: distributed BPM data contains repeatable tune-like spectral structure, but observability is condition-dependent and requires adaptive BPM/subset selection. Absolute tune validation belongs to the separate thesis/future-validation track.
