# JACoW Paper Source

`ABSTRACT54.tex` is the working manuscript name until IBIC2026 assigns a
programme code. Rename the TeX and PDF together before submission.

The source uses the official JACoW class v3.01 dated 2026-03-11. The final
review package includes that class, every figure, a SHA-256 source manifest,
and the compiled four-page PDF. Build with Tectonic or an equivalent complete
TeX Live environment; the repository does not vendor TeX's general package
tree.

The paper is not final while it contains phrases such as "final manuscript
will report" or lacks both publication figures. Numerical Results, Abstract,
Conclusion, and captions must be regenerated from verifier-clean outputs in
the same pass. The final source must include:

- the H/V leakage-controlled Best-N figure;
- the exact-paired, shared-scale H/V legacy-versus-adaptive ridge figure;
- the data-derived horizontal tracking-loss diagnostic with no imposed onset;
- a verifier-derived `results_table.tex` with per-plane Best-N and paired-ridge
  estimates and intervals;
- the current Fermilab contract number `89243024CSC000002`;
- no claim of absolute tune calibration, measured physical noise removal, or
  a fixed extraction-onset turn;
- exactly four class-defined `595 x 792 bp` pages with no overfull boxes or
  missing references. This is the explicit JACoW v3.01 page geometry; do not
  replace it with ISO A4 or US Letter geometry.

The accepted abstract is intentionally quoted in the manuscript abstract until
the corrected results justify a narrower factual update.
