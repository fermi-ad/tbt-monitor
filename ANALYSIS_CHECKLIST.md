# ANALYSIS_CHECKLIST.md
Implementation checklist for physics validation of BPM-derived tune analysis

This file defines the next analysis steps for the BPM tune-monitor project.
The goal is to move from “it runs” to “it is physically credible.”

Scope of this phase:

- validate tune extraction over many spills
- produce plots a beam physicist expects to see
- improve interpretability of results
- prepare for later SVD / modal analysis
- do not replace the existing collection path

---

# 1. Primary goals

The code should answer these questions:

1. Are the extracted Qx and Qy values stable across spills?
2. Does tune evolve smoothly through the spill?
3. Do the spectra show physically meaningful narrow peaks?
4. Are the measured peaks consistent with expected machine tune?
5. Can we produce beam-physics-friendly plots for review?
6. Is the current result limited by analysis choices, or by actual signal quality?

---

# 2. Batch analysis outputs to add

For a run over many spills, generate the following outputs.

## 2.1 Injection tune distributions
Create:

- `qx_injection_hist.png`
- `qy_injection_hist.png`

Use only spills that pass quality cuts.

Plot:

- histogram of injection-window Qx
- histogram of injection-window Qy
- annotate median and standard deviation

Also write into batch summary:

- median Qx
- median Qy
- stddev Qx
- stddev Qy
- count of accepted spills

Purpose:

- quantify whether spill-to-spill variation is physically reasonable

---

## 2.2 Spill-to-spill tune trend
Create:

- `qx_vs_spill.png`
- `qy_vs_spill.png`

Plot:

- spill index on x-axis
- injection-window tune on y-axis
- overlay quality color or marker style:
  - GOOD
  - MARGINAL
  - BAD

Purpose:

- see whether tune is clustered or wandering
- identify obvious outlier spills

---

## 2.3 Confidence and coverage trend
Create:

- `confidence_vs_spill.png`
- `coverage_vs_spill.png`

Plot:

- spill index vs confidence
- spill index vs BPM coverage / aligned streams / usable streams

Purpose:

- determine whether bad tune variation correlates with weak data quality

---

# 3. Per-spill detailed plots

For representative spills, generate the following.

## 3.1 Injection spectrum plots
Create:

- `spill_<id>_spectrum_h.png`
- `spill_<id>_spectrum_v.png`

Requirements:

- x-axis labeled as tune (0 to 1)
- y-axis labeled as averaged spectral power or arbitrary units
- show configured tune search band
- mark selected peak
- include plot subtitle with:
  - spill id / target ms
  - plane
  - Q value
  - confidence
  - BPM count used
  - FFT window length

Purpose:

- make peak-picking auditable

---

## 3.2 Tune-vs-time trace
Create:

- `spill_<id>_tune_vs_time.png`

Requirements:

- x-axis = turn number or time after injection
- y-axis = tune
- show H and V traces
- title/subtitle should include:
  - spill id
  - sliding window length
  - stride
  - tracking enabled/disabled

Purpose:

- show whether tracked tune is smooth or obviously hopping

---

## 3.3 Spectrogram / waterfall plot
Create:

- `spill_<id>_spectrogram_h.png`
- `spill_<id>_spectrogram_v.png`

This should look like the attached example.

Requirements:

- x-axis = tune
- y-axis = time after injection or sliding-window center
- color = normalized spectral power
- overlay tracked tune path as a line
- optionally overlay raw peak path as a second line
- optionally mark injection-window peak
- include colorbar

Recommended title format:

- `Qx tune spectrogram`
- `Qy tune spectrogram`

Recommended axis labels:

- x-axis: `Tune`
- y-axis: `Time after injection [ms]` or `Window center [turn]`
- colorbar: `Normalized spectral power [arb.]`

Implementation notes:

- each sliding window already produces a spectrum
- stack these spectra row-by-row into a 2D matrix
- use image/heatmap rendering
- normalize consistently so weak structure remains visible
- preserve enough resolution that narrow lines stay narrow

Purpose:

- this is one of the strongest physics-review plots
- shows whether tune line is real, continuous, and separable from background

---

# 4. Aggregate physics-review plots

These are the next-tier plots for physicist review.

## 4.1 Median spectrogram across many spills
Create:

- `median_spectrogram_h.png`
- `median_spectrogram_v.png`

Method:

- collect same-sized sliding spectra across accepted spills
- compute median spectral power at each [window, tune_bin]

Requirements:

- same style as per-spill spectrogram
- overlay median tracked tune path

Purpose:

- suppress spill-specific noise
- reveal persistent tune structure

---

## 4.2 Tune band zoom panel
Create:

- `tune_band_zoom_h.png`
- `tune_band_zoom_v.png`

Method:

- select representative GOOD spills
- plot spectra zoomed tightly around expected tune region

Purpose:

- show line sharpness and peak repeatability

---

## 4.3 Tune scatter plot
Create:

- `tune_scatter_qx_qy.png`

Plot:

- x-axis = Qx injection
- y-axis = Qy injection
- one point per spill
- color by confidence or quality class

Purpose:

- identify clustering and correlated outliers

---

# 5. Required physics cross-checks

## 5.1 FFT resolution check
For every run, compute and report:

- FFT length
- tune-bin spacing = 1 / N_window if tune axis is normalized by turn frequency
- expected raw resolution
- refined peak estimate method used

Add to summary:

- `fft_points`
- `tune_bin_spacing`
- `peak_refinement_method`

Purpose:

- answer the physicist’s question about expected tune resolution

---

## 5.2 Revolution frequency / machine line cross-check
Add optional frequency-axis mode.

Create optional plots:

- `spill_<id>_spectrum_h_freq.png`
- `spill_<id>_spectrum_v_freq.png`

If revolution frequency is provided in config, annotate expected machine lines:

- n * f_rev

Config additions to consider:

- `revolution_frequency_hz`
- `plot_frequency_axis`

Purpose:

- verify axis scaling
- check for known revolution harmonics

---

## 5.3 Best-BPM vs all-BPM comparison
For representative spills, compare:

- best single BPM spectrum
- median BPM spectrum
- averaged power spectrum across all BPMs

Create:

- `spill_<id>_spectrum_compare_h.png`
- `spill_<id>_spectrum_compare_v.png`

Purpose:

- determine whether the all-BPM combination is helping or washing things out

---

## 5.4 Plane consistency / BPM subset test
Add analysis mode to recompute tune using BPM subsets:

- all BPMs
- odd/even BPM split
- first half / second half
- random subsets of fixed size

Create:

- `subset_consistency_h.png`
- `subset_consistency_v.png`

Purpose:

- confirm tune is coherent across the ring
- identify dependence on a small number of BPMs

---

# 6. Quality cuts to implement for physics validation

Current confidence values near 1.0 are probably too weak to call “good.”
Implement explicit filtering for physics summary plots.

Suggested initial accepted-spill requirements:

- aligned fraction >= configured threshold
- usable BPM count per plane >= 8
- injection peak not at search-band edge
- confidence above threshold
- no decode failure
- no missing plane

Add separate concepts:

- `analysis_status`: did code run successfully
- `physics_usable`: suitable for tune summary plots

Purpose:

- stop marginal spills from dominating interpretation

---

# 7. Summary tables to write

## 7.1 Batch CSV / JSONL
Ensure batch summary rows include:

- spill index
- target ms
- qx injection
- qy injection
- confidence h
- confidence v
- coverage
- aligned fraction
- usable bpm count h
- usable bpm count v
- quality label
- physics usable flag
- sliding median qx tracked
- sliding median qy tracked
- sliding std qx tracked
- sliding std qy tracked

## 7.2 Physics summary markdown
Create:

- `physics_summary.md`

Include:

- total spills analyzed
- spills passing physics cuts
- injection Qx/Qy medians and stddev
- representative spill ids
- worst outliers
- config parameters used
- notes about tracking/fallback behavior

Purpose:

- quick artifact a physicist can read without opening every plot

---

# 8. Immediate next implementation order

Implement in this order:

1. injection histograms
2. qx/qy vs spill plots
3. per-spill spectrogram / waterfall plots
4. median spectrogram across accepted spills
5. best-BPM vs all-BPM comparison
6. BPM subset consistency checks
7. optional frequency-axis / revolution-line annotation
8. SVD branch after the above is working

Do not start SVD first.
First make the current FFT pipeline fully inspectable.

---

# 9. Plot styling requirements

For all plots:

- add axis labels
- add units where known
- put spill id / plane / BPM count / window length in subtitle
- save high enough resolution to read annotations
- avoid unlabeled “mystery plots”
- use consistent naming and axis conventions across outputs

Standard conventions:

- tune axis: `Tune`
- turn axis: `Turn`
- time axis: `Time after injection [ms]` if conversion exists
- spectrum axis: `Spectral power [arb.]`
- coverage axis: `Usable BPM channels`
- confidence axis: `Peak / median-band power`

---

# 10. Questions these plots should answer

The final artifact set should let a physicist answer:

1. Is the tune peak real?
2. Is it where we expect it?
3. Is it stable spill-to-spill?
4. Does it move smoothly through the spill?
5. Are the results driven by a few BPMs or by coherent beam motion?
6. Does the BPM method look credible enough to compare against Schottky?

If those questions cannot be answered from the plots, add the missing plot.

---

# 11. Deferred work

Do not do these yet unless the above looks good:

- SVD-based mode extraction
- phase-advance-based coherent summation
- model-based fitting
- Schottky residual study
- automatic tune correction / online feedback

These are phase-2 items.

---

# 12. Deliverables for the next review

Minimum desired artifact set for next beam-physics review:

- `physics_summary.md`
- `qx_injection_hist.png`
- `qy_injection_hist.png`
- `qx_vs_spill.png`
- `qy_vs_spill.png`
- representative `spectrum_h/v.png`
- representative `tune_vs_time.png`
- representative `spectrogram_h/v.png`
- `median_spectrogram_h.png`
- `median_spectrogram_v.png`

If possible also include:

- `subset_consistency_h.png`
- `subset_consistency_v.png`
- `spectrum_compare_h.png`
- `spectrum_compare_v.png`
