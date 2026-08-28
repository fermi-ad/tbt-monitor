# Changelog

## [1.0.0] - 2026-08-28

This is the first stable public release of `tbt-monitor` and the software
release accompanying the IBIC 2026 study.

### Highlights

- Monitor synchronized turn-by-turn beam-position streams in a terminal.
- Capture raw same-spill data with explicit completeness, timing, and quality
  information.
- Analyze live or saved spills for horizontal and vertical tune candidates,
  sliding-window evolution, robustness studies, and batch summaries.
- Run the larger CPU/GPU Best-BPM, ensemble-validation, ridge-density, and
  reproducibility workflows used for the publication.
- Inspect the final WEP014 proceedings paper, printed poster, canonical figures,
  numerical result payload, and checksummed publication package.
- Use the project under the BSD 3-Clause License.

### Scientific scope

The release reports internally repeatable BPM-derived tune candidates. It does
not claim an absolute tune calibration, measured physical-noise removal, a
universal advantage over all-channel aggregation, or a fixed extraction-onset
turn. Raw accelerator captures are not distributed with the repository.

### Compatibility

- Executable: `tbt-monitor-tui`
- Minimum Rust version: 1.88
- Configuration and analysis-output schemas are unchanged from the finalized
  publication revision.

### Maintenance

- Updated the terminal UI stack to Ratatui 0.30.2 and Crossterm 0.29.
- Updated the transitive `lru` cache dependency to 0.18.3, resolving the
  low-severity advisory present in the pre-release dependency set.
