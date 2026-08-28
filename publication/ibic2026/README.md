# IBIC 2026 Publication Package

This directory contains the finalized WEP014 proceedings paper and poster for
abstract 54,
*Turn-by-turn tune analysis using adaptive BPM ensembles in the Fermilab Mu2e
Delivery Ring*.

Both artifacts are approved for technical publication. The poster has been
printed for presentation at IBIC 2026.

## Final deliverables

| Deliverable | File |
| --- | --- |
| Proceedings paper | [`paper/build/WEP014.pdf`](paper/build/WEP014.pdf) |
| Proceedings source | [`paper/WEP014.tex`](paper/WEP014.tex) |
| Poster-frozen paper record | [`paper/build/ABSTRACT54.pdf`](paper/build/ABSTRACT54.pdf) |
| Poster PDF | [`poster/build/ibic2026-abstract54-poster.pdf`](poster/build/ibic2026-abstract54-poster.pdf) |
| Editable poster | [`poster/build/ibic2026-abstract54-poster.pptx`](poster/build/ibic2026-abstract54-poster.pptx) |
| Poster preview | [`poster/build/ibic2026-abstract54-poster-artifact-preview.png`](poster/build/ibic2026-abstract54-poster-artifact-preview.png) |
| Result payload | [`results_payload.json`](results_payload.json) |
| Compliance summary | [`compliance_report.md`](compliance_report.md) |
| File inventory | [`publication_manifest.csv`](publication_manifest.csv) |

The paper is exactly four JACoW pages. The poster is one A0 portrait page and
includes Fermilab report number `FERMILAB-POSTER-26-0268-AD` and the required
DOE contract acknowledgment.

## Result in brief

The analysis evaluates synchronized turn-by-turn position data from 2,000
Delivery Ring spills. Rather than selecting one permanently preferred BPM, it
chooses small adaptive ensembles and tests them on later, non-overlapping data
and held-out digitizers.

- H Best-5 and V Best-12 are the declared operating points.
- Vertical agreement is strongest across held-out digitizers.
- H Best-5 narrows the corrected-Best-1 ridge distribution.
- All-training mean and median aggregation remain competitive controls.
- All 60 H and all 60 V sources become Best-1 at least once, showing that
  observability moves around the ring.

The values are tune candidates supported by internal BPM reproducibility. They
are not an absolute tune calibration.

## Package structure

- `paper/`: JACoW source, generated tables/macros, canonical vector figures,
  build script, and final PDF.
- `poster/`: frozen evidence metadata, concise content, canonical assets,
  editable source, build script, and final deliverables.
- `reports/publication_figures/`: canonical paper and poster figure exports.
- `results_payload.json`: machine-readable accepted numerical content.
- `source_manifest.csv`: hashes connecting accepted analysis inputs to
  materialized outputs.
- `publication_manifest.csv`: size and SHA-256 inventory of the delivered
  package.
- `PREPARATION_REPORT.md` and `compliance_report.md`: concise materialization
  and final-artifact verification summaries.

The detailed Best-N review plots under `reports/` are supporting diagnostics;
the final scientific figures live in the paper and poster directories.

## Reproducibility boundary

Publication materialization is fail-closed:

1. `scripts/prepare_ibic2026_publication.py` accepts only verifier-clean
   analysis roots and generates the numerical payload, paper tables, and
   canonical figures.
2. Once the paper is accepted, `poster/evidence_gate.json` freezes its source,
   PDF, result payload, and scientific poster inputs by SHA-256.
3. `scripts/prepare_ibic2026_poster.py` may then update only poster content and
   assets while proving the paper hashes are unchanged.
4. `scripts/finalize_ibic2026_publication.py` verifies page geometry, fonts,
   manifests, numerical bindings, artifact hashes, and explicit visual QA.

The accepted external abstract and Fermilab poster template are not duplicated
in the repository. Their reference hashes are retained in the compliance
metadata. Raw accelerator captures and the full analysis workspace are also not
distributed; they require authorized Fermilab access.

Run the public CPU regression suites from the repository root:

```bash
python3 scripts/test_prepare_ibic2026_poster.py
python3 scripts/test_best_bpm_mining.py
cargo test --locked -- --nocapture
```

Paper and poster build details are documented in
[`paper/README.md`](paper/README.md) and [`poster/README.md`](poster/README.md).

## Claim boundary

The publication reports BPM-only tune candidates and internal reproducibility.
It does not claim:

- absolute or externally calibrated tune accuracy;
- physical noise removal from ridge-density differences;
- a universal Best-N advantage over all-training aggregation;
- a measured extraction-onset turn; or
- that unavailable ridge picks are zero-valued measurements.

H Best-5 only just clears its pointwise cross-spill null band, while V Best-12
is more clearly separated. Reduced-sample and gate-margin checks do not identify
a unique ensemble size. Score weights, spectral-window length, and tune-
agreement tolerance remain open sensitivities. A matched external reference or
controlled quadrupole scan is required for absolute validation.

The completed 200-spill intensity study is retained as a separately verified
technical sidecar. It contributes no paper or poster claim, macro, source role,
or figure.
