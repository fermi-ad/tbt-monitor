# PHYSICS.md
Beam Physics Validation Checklist for BPM-Derived Tune Monitor

This document defines the **beam physics questions and validation tests**
required to determine whether BPM turn-by-turn data can reliably measure
betatron tune in the Mu2e Delivery Ring.

The software framework (Redis ingestion, spill capture, FFT analysis,
tracked peak extraction, batch validation) already exists in this repository.

This file defines the **physics checks** required to confirm that the results
are physically meaningful and operationally useful.

---

# 1. Core Physics Question

Can synchronized BPM turn-by-turn data be used to reliably measure the
horizontal and vertical betatron tune during the Delivery Ring spill?

The answer requires confirming:

- the extracted spectral peaks correspond to real betatron motion
- the measured tune values are stable and physically consistent
- spill evolution matches known machine behavior
- BPM-derived measurements agree with the existing Schottky tune monitor

---

# 2. Required Physics Validation Tests

## 2.1 Injection Tune Stability

Compute injection-window tune for **~100 spills**.

Expected behavior:

- Tune should cluster tightly spill-to-spill.

Typical expectation:


σ(Qx) ≈ 0.001–0.01
σ(Qy) ≈ 0.001–0.01


Deliverables:

- histogram of Qx injection
- histogram of Qy injection
- median + stddev summary

Purpose:

Confirm that the peak corresponds to a stable machine parameter.

---

## 2.2 Tune Evolution Through Spill

Plot sliding-window tune vs time.

Expected behavior:

- tune evolution should be **smooth**
- tune may drift toward resonance during extraction
- motion should be physically continuous (no bin-hopping)

Deliverables:

- tune_vs_time plots
- median tune trajectory across many spills
- envelope of tune variation

Purpose:

Verify that observed motion reflects machine dynamics rather than analysis artifacts.

---

## 2.3 Spectral Peak Validation

Inspect BPM-averaged spectra.

A valid tune signal should exhibit:

- a **distinct narrow spectral peak**
- consistent location across spills
- separation from noise floor

Deliverables:

- horizontal spectrum plots
- vertical spectrum plots
- zoomed view around tune band

Purpose:

Confirm the algorithm is selecting a real spectral line.

---

## 2.4 Revolution Harmonic Cross-Check

The Delivery Ring revolution frequency is approximately:


f_rev ≈ 590.08 kHz


FFT frequency scaling should produce visible harmonic structure
at integer multiples of the revolution frequency.

Deliverables:

- annotated spectrum showing revolution harmonics
- verification that FFT frequency axis is correctly scaled

Purpose:

Validate the frequency calibration of the analysis.

---

## 2.5 BPM Coherence Test

True betatron motion should appear coherently across BPMs.

Test:

- compute tune using subsets of BPMs
- verify consistent peak location

Optional metrics:

- cross-BPM spectral coherence
- SVD/PCA mode extraction

Deliverables:

- tune estimates from multiple BPM subsets
- comparison plot

Purpose:

Confirm signal is beam motion rather than local noise.

---

## 2.6 Comparison with Existing Tune Monitor

Compare BPM-derived tune to the Schottky system.

Possible approaches:

- spill-averaged comparison
- time-slice comparison within spill

Deliverables:

- Qx_BPM vs Qx_Schottky
- Qy_BPM vs Qy_Schottky
- residual statistics

Purpose:

Establish credibility of BPM-derived measurement.

---

# 3. Expected Operational Tune Range

Approximate Delivery Ring tune region:


Qx ≈ ~0.69
Qy ≈ ~0.71


Observed tune values should remain within reasonable proximity
to these ranges unless machine settings change.

Large spill-to-spill variation (> ~0.05) likely indicates analysis errors.

---

# 4. Signal Quality Metrics

The analysis pipeline should report the following diagnostics:

| Metric | Purpose |
|------|------|
| alignment_fraction | confirm BPM synchronization |
| spectral_confidence | peak significance |
| fallback_count | peak-tracking robustness |
| suspicious_step_count | detect bin hopping |

Spills failing quality criteria should be flagged as:


GOOD
MARGINAL
BAD


---

# 5. Known Limitations

The BPM-derived tune monitor differs from the Schottky system:

Schottky monitor advantages:

- resonant pickup
- higher SNR
- dedicated frequency measurement

BPM system advantages:

- existing instrumentation
- distributed measurement across ring
- flexible analysis

The BPM approach should be considered a **complementary diagnostic**
unless proven equivalent in performance.

---

# 6. Future Physics Improvements

Potential enhancements:

### SVD/PCA Orbit Decomposition
Extract dominant coherent betatron mode.

### Multi-spill Averaging
Improve spectral SNR.

### Window Optimization
Increase FFT resolution for tune tracking.

### Beam Excitation Studies
Introduce controlled oscillations to verify response.

---

# 7. Acceptance Criteria

The BPM tune monitor is considered successful if:

1. Injection tune clusters tightly across spills.
2. Spectra show clear, repeatable tune peaks.
3. Sliding tune traces are smooth and physically plausible.
4. BPM subset analysis yields consistent tune values.
5. Results agree reasonably with the Schottky system.

---

# 8. Responsibilities

Software (Codex / developers):

- data ingestion
- signal processing
- plotting
- batch analysis

Beam physicists:

- interpret tune evolution
- confirm expected machine behavior
- validate results against operational measurements

---

# 9. Summary

This repository provides a full pipeline for BPM-based tune measurement.

The remaining task is **physics validation**, not software development.

Successful validation would demonstrate that BPM TbT data can provide a
useful complementary tune diagnostic for Delivery Ring operations.
