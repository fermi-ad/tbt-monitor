# JACoW Paper Source

`WEP014` is the IBIC 2026 programme code for the paper. `WEP014.tex` is the
final proceedings source, and `build/WEP014.pdf` is the finalized four-page
paper. Its three `WEP014_f*.pdf` figures and numerical macros exactly match the
canonical checked inputs; `WEP014_results_table.tex` changes table-heading
formatting without changing the reported values.

`ABSTRACT54.tex` and `build/ABSTRACT54.pdf` are retained as the approved paper
revision frozen by `poster/evidence_gate.json` before poster production. The
WEP014 source preserves its scientific prose and replaces only programme
naming, JACoW presentation details, and local filenames. Poster copy, layout,
or contextual graphics must not rebuild or edit the frozen record.

The source uses the official JACoW class v3.01 dated 2026-03-11. The publication
package includes that class, every figure, a SHA-256 source manifest,
and the compiled four-page PDF. Build with Tectonic or an equivalent complete
TeX Live environment; the repository does not vendor TeX's general package
tree.
When Tectonic's bundle is already cached, set `TECTONIC_FLAGS=--only-cached`
to forbid resource downloads. Some Tectonic builds still initialize operating-
system proxy state in cached mode; that initialization must also succeed.
Leaving `TECTONIC_FLAGS` unset is supported on macOS Bash 3.2; the build avoids
expanding an empty array under `set -u`.
The synthetic layout smoke passed with Tectonic 0.16.9 and a coherent cached
bundle, including the exact page, reference, overflow, and font gates. It does
not substitute for rebuilding from the accepted real-data artifacts.

The final source preserves the following requirements. Numerical Results,
Abstract, Conclusion, and captions are generated from verifier-clean outputs
in the same pass:

- the H/V leakage-controlled Best-N figure, isolating blind full-band
  selected-versus-held-out agreement on one shared zero-based scale and showing
  its deterministic cross-spill null band;
- the exact-paired, shared-scale corrected-Best-1-versus-selected ridge figure
  with a complete turn scale and identical shared-population trace beneath each
  method panel;
- the exact-paired selected-H/V P10-P90 width-contrast composite with an explicit
  zero reference and non-noise/non-extraction guardrail;
- a verifier-derived `results_table.tex` with per-plane Best-N and paired-ridge
  estimates and intervals, using agreement percentages and $10^{-3}$ units for
  tune/IQR quantities, with no legacy-selector column;
- verifier-derived `results_macros.tex` for every primary-score, Best-1
  membership, sensitivity availability/range, and H/V all-training outcome
  count quoted in prose, plus the selected H/V structural, finite, blank, and
  bounded edge-excluded ridge counts;
- the current Fermilab contract number `89243024CSC000002`;
- no claim of absolute tune calibration, measured physical noise removal, or
  a fixed extraction-onset turn;
- no claim that a small adaptive set outperforms all-BPM aggregation from the
  reused-window score; the separate same-protocol all-training control must be
  reported exactly and may favor Best-N, all-training, or neither;
- exactly four class-defined `595 x 792 bp` pages with no overfull boxes or
  missing references. This is the explicit JACoW v3.01 page geometry; do not
  replace it with ISO A4 or US Letter geometry.
- every PDF font embedded, subset, and Unicode-mapped; the gate parses these
  status fields from the right because names such as `CID Type 0C` span several
  whitespace columns in `pdffonts` output.
- paper-specific line plots must remain vector PDF; density plots may rasterize
  only the heatmap field while preserving vector text, with 7.5--9 pt type at
  placed size.

The accepted abstract is intentionally quoted in the manuscript abstract and
must remain verbatim; corrected result disclosures belong in the body, table,
captions, and conclusion.

Claim calibration in the body must remain explicit:

- the selected H result is only marginally separated from its pointwise cross-
  spill null, while V is more clearly separated;
- conditioned support can diagnose full-band peak switching but cannot assign
  its physical cause;
- reduced-sample and gate-margin checks do not establish a unique N, and score
  weights, spectral-window length, and tune-agreement tolerance remain untested
  under the complete Best-N protocol;
- a controlled quadrupole scan comparing measured with optics-predicted tune
  shifts is required for calibration;
- the demonstrated workflow is offline/post-spill, not an intra-spill monitor.

Generate `results_table.tex`, `results_macros.tex`, and the three paper-specific
PDFs under
`figures/` with `scripts/prepare_ibic2026_publication.py`; the script rejects any mismatch
between the selected Best-N rows and the plane-specific full-buffer ridge
contract and requires the passing all-training and 2000-manifest position-only
raw-payload audit, including the exact 239984 captured position rows and hashed
16-row absent-stream inventory across 12 partial captures. The earlier
three-collection audit is retained with the intensity sidecar. Generated data
copy reports the primary completeness, while the ridge subsection reports selected
finite/blank/edge closure instead of implying every structural row has a tune.
No intensity source role, result key, macro, sentence, table row, or figure is
permitted in the paper materialization.
The data-derived horizontal tracking-loss diagnostic remains a required poster
and review-gallery asset, but it is deliberately not a manuscript figure: it
is a noncausal, plane-specific diagnostic and would displace references from
the four-page paper without strengthening the central inference.
The generated macro set also supplies the definitive full-curve case count,
stratified validation case count, fold count, and strict-majority sensitivity
summary used in manuscript prose. All seven sensitivity runs must verify, at
least four per plane must yield a knee, and every unavailable reason remains in
the results payload.
Final package closure is shared with the poster: the finalizer will not write
the inventory or compliance report until the poster PPTX contains zero empty
structural placeholders and both rendered artifacts have explicit visual-QA
passes. `build_paper.sh` writes package-relative logical labels in
`build/source_manifest.sha256`; finalization requires the exact expected entry
set and recomputes every digest. It independently validates the publication-
level materialization manifest covering this table, macro file, figures,
payload, and poster inputs. It also requires every primary-capture and selected-
ridge macro definition to equal the payload and poster evidence values.
