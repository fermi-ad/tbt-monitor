# JACoW Paper Source

`ABSTRACT54.tex` is the working manuscript name until IBIC2026 assigns a
programme code. Rename the TeX and PDF together before submission.

The source uses the official JACoW class v3.01 dated 2026-03-11. The final
review package includes that class, every figure, a SHA-256 source manifest,
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

The paper is not final while it contains phrases such as "final manuscript
will report" or lacks any required publication figure. Numerical Results, Abstract,
Conclusion, and captions must be regenerated from verifier-clean outputs in
the same pass. The final source must include:

- the H/V leakage-controlled Best-N figure;
- the exact-paired, shared-scale H/V legacy-versus-adaptive ridge figure;
- the exact-paired selected-H/V P10-P90 width-contrast composite with an explicit
  zero reference and non-noise/non-extraction guardrail;
- the data-derived horizontal tracking-loss diagnostic with no imposed onset;
- a verifier-derived `results_table.tex` with per-plane Best-N and paired-ridge
  estimates and intervals;
- verifier-derived `results_macros.tex` for every primary-score, intensity-
  effect count, and sensitivity availability/range quoted in prose;
- the current Fermilab contract number `89243024CSC000002`;
- no claim of absolute tune calibration, measured physical noise removal, or
  a fixed extraction-onset turn;
- exactly four class-defined `595 x 792 bp` pages with no overfull boxes or
  missing references. This is the explicit JACoW v3.01 page geometry; do not
  replace it with ISO A4 or US Letter geometry.
- every PDF font embedded, subset, and Unicode-mapped; the gate parses these
  status fields from the right because names such as `CID Type 0C` span several
  whitespace columns in `pdffonts` output.

The accepted abstract is intentionally quoted in the manuscript abstract until
the corrected results justify a narrower factual update.

Generate `results_table.tex`, `results_macros.tex`, and the five files under `figures/` with
`scripts/prepare_ibic2026_publication.py`; the script rejects any mismatch
between the selected Best-N rows and the plane-specific full-buffer ridge
contract and requires the passing 2200-manifest raw-payload audit.
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
payload, and poster inputs.
