//! Deterministic in-process implementation of the published Best-N and ridge paths.
//!
//! Selection deliberately uses unnormalized, padded, half-precision cached power.
//! Ridge spectra deliberately use cropped-trace RMS normalization and no padding.
//! These are different measurements and must not share a preprocessing shortcut.

use std::collections::{BTreeMap, BTreeSet};

use half::f16;
use rayon::prelude::*;
use rustfft::{FftPlanner, num_complex::Complex};

use super::spec::*;
use super::types::*;

#[derive(Clone, Debug)]
struct SpectralCube {
    /// Channel-major, with contiguous window-major rows inside each channel.
    powers: Vec<Vec<f32>>,
    tune_axis: Vec<f32>,
    starts: Vec<usize>,
}

#[derive(Clone, Debug)]
struct SearchScore {
    members: Vec<usize>,
    q_hat: f64,
    score: f64,
    candidate_support: f64,
    peak_quality: f64,
    consensus_agreement: f64,
    window_stability: f64,
    diversity: f64,
    ambiguity: f64,
    visible_fraction: f64,
    visibility_duration_turns: f64,
}

#[derive(Clone, Debug, Default)]
struct AggregateMetrics {
    q_hat: Option<f64>,
    blind_q_hat: Option<f64>,
    peak_prominence: Option<f64>,
    p10_peak_prominence: Option<f64>,
    power_support: Option<f64>,
    visible_fraction: Option<f64>,
    spectral_entropy: Option<f64>,
}

#[derive(Clone, Copy, Debug)]
struct Peak {
    q: f64,
    confidence: f64,
}

fn finite(value: f64) -> Option<f64> {
    value.is_finite().then_some(value)
}

fn percentile(values: &[f64], fraction: f64) -> f64 {
    let mut clean: Vec<f64> = values.iter().copied().filter(|v| v.is_finite()).collect();
    if clean.is_empty() {
        return f64::NAN;
    }
    clean.sort_unstable_by(f64::total_cmp);
    let index = (clean.len() - 1) as f64 * fraction;
    let lo = index.floor() as usize;
    let hi = index.ceil() as usize;
    clean[lo] + (clean[hi] - clean[lo]) * (index - lo as f64)
}

fn median(values: &[f64]) -> f64 {
    let mut clean: Vec<f64> = values.iter().copied().filter(|v| v.is_finite()).collect();
    if clean.is_empty() {
        return f64::NAN;
    }
    let middle = clean.len() / 2;
    let even = clean.len().is_multiple_of(2);
    let (lower, upper, _) = clean.select_nth_unstable_by(middle, f64::total_cmp);
    if even {
        (lower.iter().copied().fold(f64::NEG_INFINITY, f64::max) + *upper) / 2.0
    } else {
        *upper
    }
}

fn argmax(values: &[f32]) -> usize {
    let mut best = 0;
    for index in 1..values.len() {
        if values[index] > values[best] {
            best = index;
        }
    }
    best
}

fn entropy(power: &[f32]) -> f64 {
    let total: f64 = power
        .iter()
        .filter(|p| p.is_finite() && **p > 0.0)
        .map(|p| f64::from(*p))
        .sum();
    if total == 0.0 {
        return 0.0;
    }
    -power
        .iter()
        .filter(|p| p.is_finite() && **p > 0.0)
        .map(|p| {
            let fraction = f64::from(*p) / total;
            fraction * fraction.ln()
        })
        .sum::<f64>()
        / (power.len().max(2) as f64).ln()
}

fn robust_prominence(power: &[f32], settings: &ScoreSettings) -> f64 {
    let logs: Vec<f64> = power
        .iter()
        .map(|p| (f64::from(*p) + settings.log_power_floor).log10())
        .collect();
    let center = median(&logs);
    let deviations: Vec<f64> = logs.iter().map(|v| (v - center).abs()).collect();
    (logs[argmax(power)] - center)
        / (median(&deviations) * settings.mad_scale).max(settings.mad_floor)
}

fn complete_windows(turn_count: usize, window: usize, stride: usize) -> Vec<usize> {
    if window == 0 || stride == 0 || window > turn_count {
        Vec::new()
    } else {
        (0..=turn_count - window).step_by(stride).collect()
    }
}

/// Exact fit-prefix reduction and overlap purge from `best_n.purged_window_split`.
fn purged_split(starts: &[usize], requested: usize, window: usize) -> (usize, usize) {
    if starts.len() <= 1 {
        return (starts.len(), starts.len());
    }
    let mut count = requested.min(starts.len() - 1).max(1);
    loop {
        let fit_end = starts[count - 1] + window;
        let test_start = (count..starts.len())
            .find(|index| starts[*index] >= fit_end)
            .unwrap_or(starts.len());
        if test_start < starts.len() || count == 1 {
            return (count, test_start);
        }
        count -= 1;
    }
}

fn hann(window: usize) -> Vec<f32> {
    if window <= 1 {
        return vec![1.0; window];
    }
    (0..window)
        .map(|index| {
            0.5 - 0.5 * (2.0 * std::f32::consts::PI * index as f32 / (window - 1) as f32).cos()
        })
        .collect()
}

fn taper(settings: &SpectralSettings, window: usize) -> Vec<f32> {
    match settings.window {
        WindowFunction::Hann => hann(window),
        WindowFunction::Rectangular => vec![1.0; window],
    }
}

fn trace_scale(trace: &[f32], settings: &SpectralSettings) -> f32 {
    if settings.normalization == Normalization::None {
        return 1.0;
    }
    let rms = (trace.iter().map(|v| v * v).sum::<f32>() / trace.len() as f32).sqrt();
    if rms.is_finite() && f64::from(rms) > settings.normalization_floor {
        rms
    } else {
        1.0
    }
}

fn cached_power(power: f32, settings: &SpectralSettings) -> f32 {
    match settings.cache_precision {
        CachePrecision::Float16 => f16::from_f32(power).to_f32(),
        CachePrecision::Float32 => power,
    }
}

fn selection_spectra(
    traces: &[&[f32]],
    starts: &[usize],
    settings: &SpectralSettings,
    band: [f64; 2],
) -> SpectralCube {
    let window = settings.window_turns;
    let nfft = window * settings.padding_factor;
    // NumPy rfftfreq is ascending in frequency; the upper-branch tune axis is
    // consequently descending. Its order is part of deterministic argmax ties.
    let bins: Vec<usize> = (0..=nfft / 2)
        .filter(|index| {
            let q = 1.0_f32 - *index as f32 / nfft as f32;
            q >= band[0] as f32 && q <= band[1] as f32
        })
        .collect();
    let axis = bins
        .iter()
        .map(|index| 1.0 - *index as f32 / nfft as f32)
        .collect();
    let taper = taper(settings, window);
    let norm = taper
        .iter()
        .map(|v| v * v)
        .sum::<f32>()
        .max(f32::MIN_POSITIVE);
    let fft = FftPlanner::<f32>::new().plan_fft_forward(nfft);
    let powers = traces
        .par_iter()
        .map(|trace| {
            let scale = trace_scale(trace, settings);
            let mut buffer = vec![Complex::default(); nfft];
            let mut scratch = vec![Complex::default(); fft.get_inplace_scratch_len()];
            let mut output = Vec::with_capacity(starts.len() * bins.len());
            for start in starts {
                let block = &trace[*start..*start + window];
                let center = if settings.mean_subtract {
                    block.iter().map(|v| v / scale).sum::<f32>() / window as f32
                } else {
                    0.0
                };
                buffer.fill(Complex::default());
                for ((value, sample), weight) in buffer.iter_mut().zip(block).zip(&taper) {
                    value.re = (*sample / scale - center) * weight;
                }
                fft.process_with_scratch(&mut buffer, &mut scratch);
                output.extend(bins.iter().map(|index| {
                    // The publication reads a float16 cache back into float32
                    // before scoring. Quantization is intentional here.
                    cached_power(buffer[*index].norm_sqr() / norm, settings)
                }));
            }
            output
        })
        .collect();
    SpectralCube {
        powers,
        tune_axis: axis,
        starts: starts.to_vec(),
    }
}

fn mean_spectrum(cube: &SpectralCube, members: &[usize], window_index: usize, output: &mut [f32]) {
    output.fill(0.0);
    let offset = window_index * output.len();
    for member in members {
        for (out, power) in output
            .iter_mut()
            .zip(&cube.powers[*member][offset..offset + cube.tune_axis.len()])
        {
            *out += *power;
        }
    }
    for out in output {
        *out /= members.len() as f32;
    }
}

fn training_candidates(cube: &SpectralCube, fit_count: usize) -> Vec<f64> {
    cube.powers
        .iter()
        .map(|powers| {
            let q: Vec<f64> = powers
                .chunks_exact(cube.tune_axis.len())
                .take(fit_count)
                .map(|window| f64::from(cube.tune_axis[argmax(window)]))
                .collect();
            // The reference stores the median back in float32.
            f64::from(median(&q) as f32)
        })
        .collect()
}

fn score_subset(
    cube: &SpectralCube,
    members: &[usize],
    fit_count: usize,
    candidates: &[f64],
    digitizers: &[&str],
    ring_orders: &[Option<f64>],
    window_turns: usize,
    search: &SearchSettings,
) -> SearchScore {
    let settings = &search.score;
    let mut combined = vec![0.0_f32; cube.tune_axis.len()];
    let mut q_windows = Vec::with_capacity(fit_count);
    let mut prominences = Vec::with_capacity(fit_count);
    let mut entropies = Vec::with_capacity(fit_count);
    let mut ratios = Vec::with_capacity(fit_count);
    for window in 0..fit_count {
        mean_spectrum(cube, members, window, &mut combined);
        let peak = argmax(&combined);
        q_windows.push(f64::from(cube.tune_axis[peak]));
        prominences.push(robust_prominence(&combined, settings));
        entropies.push(entropy(&combined));
        let second = combined
            .iter()
            .enumerate()
            .filter(|(index, _)| *index != peak)
            .map(|(_, power)| f64::from(*power))
            .fold(0.0_f64, f64::max);
        ratios.push(if combined.len() >= 2 {
            (second + settings.log_power_floor)
                / (f64::from(combined[peak]) + settings.log_power_floor)
        } else {
            1.0
        });
    }
    let q_hat = median(&q_windows);
    let tolerance =
        (search.tune_tolerance_bins / window_turns as f64).max(search.tune_tolerance_min);
    let support: Vec<bool> = candidates
        .iter()
        .map(|q| q.is_finite() && (q - q_hat).abs() <= tolerance)
        .collect();
    let all_support = support.iter().filter(|v| **v).count();
    let selected_support = members.iter().filter(|index| support[**index]).count();
    let candidate_support =
        (all_support - selected_support) as f64 / (cube.powers.len() - members.len()).max(1) as f64;
    let peak_quality = (median(&prominences) / settings.prominence_scale).clamp(0.0, 1.0)
        * settings.peak_quality_weights[0]
        + (1.0 - median(&entropies)).clamp(0.0, 1.0) * settings.peak_quality_weights[1];
    // The published Best-N fit explicitly passes consensus=None, so no
    // externally computed consensus (or later data) can leak into selection.
    let consensus_agreement = settings.consensus_neutral;
    let steps: Vec<f64> = q_windows
        .windows(2)
        .map(|pair| (pair[1] - pair[0]).abs())
        .collect();
    let step_median = if steps.is_empty() {
        0.0
    } else {
        median(&steps)
    };
    let window_stability = -(step_median / settings.stability_scale).min(1.0);
    let window_stability = window_stability.exp();
    let unique_digitizers = members
        .iter()
        .map(|index| digitizers[*index])
        .filter(|name| !name.is_empty())
        .collect::<BTreeSet<_>>()
        .len();
    let orders: Vec<f64> = members
        .iter()
        .filter_map(|index| ring_orders[*index])
        .filter(|v| v.is_finite())
        .collect();
    let ring_span = if orders.len() > 1 {
        (orders.iter().copied().fold(f64::NEG_INFINITY, f64::max)
            - orders.iter().copied().fold(f64::INFINITY, f64::min))
            / settings.ring_order_scale
    } else {
        0.5
    };
    let diversity = if members.len() == 1 {
        0.5
    } else {
        (settings.digitizer_weight * unique_digitizers as f64 / members.len() as f64
            + settings.ring_span_weight * ring_span)
            .clamp(0.0, 1.0)
    };
    let ambiguity = ((median(&ratios) - settings.ambiguity_ratio_start)
        * settings.ambiguity_weight)
        .clamp(0.0, settings.ambiguity_cap);
    let visible: Vec<usize> = prominences
        .iter()
        .enumerate()
        .filter(|(_, v)| v.is_finite() && **v >= settings.visibility_threshold)
        .map(|(i, _)| i)
        .collect();
    let visible_fraction = visible.len() as f64 / fit_count.max(1) as f64;
    let visibility_duration_turns = match (visible.first(), visible.last()) {
        (Some(first), Some(last)) => (cube.starts[*last] - cube.starts[*first]) as f64,
        _ => 0.0,
    };
    let window_scores: Vec<f64> = prominences
        .iter()
        .zip(entropies)
        .map(|(prom, ent)| {
            let local_peak = (prom / settings.prominence_scale).clamp(0.0, 1.0)
                * settings.local_peak_weights[0]
                + (1.0 - ent).clamp(0.0, 1.0) * settings.local_peak_weights[1];
            settings.window_weights[0] * candidate_support
                + settings.window_weights[1] * local_peak
                + settings.window_weights[2] * consensus_agreement
                + settings.window_weights[3] * visible_fraction
                + settings.window_weights[4] * diversity
                + settings.window_weights[5] * f64::from(prom.is_finite())
                - ambiguity
        })
        .collect();
    let score = settings.aggregate_weights[0] * median(&window_scores)
        + settings.aggregate_weights[1] * percentile(&window_scores, 0.10)
        + settings.aggregate_weights[2] * window_stability;
    SearchScore {
        members: members.to_vec(),
        q_hat,
        score,
        candidate_support,
        peak_quality,
        consensus_agreement,
        window_stability,
        diversity,
        ambiguity,
        visible_fraction,
        visibility_duration_turns,
    }
}

fn candidate_expansions(beam: &[SearchScore], count: usize) -> Vec<Vec<usize>> {
    let mut expanded = BTreeSet::new();
    for score in beam {
        for index in 0..count {
            if let Err(position) = score.members.binary_search(&index) {
                let mut members = score.members.clone();
                members.insert(position, index);
                expanded.insert(members);
            }
        }
    }
    expanded.into_iter().collect()
}

fn beam_search(
    cube: &SpectralCube,
    fit_count: usize,
    digitizers: &[&str],
    ring_orders: &[Option<f64>],
    window_turns: usize,
    requested_n: usize,
    search: &SearchSettings,
) -> Option<(SearchScore, Vec<usize>)> {
    if requested_n == 0
        || requested_n > cube.powers.len()
        || fit_count == 0
        || cube.tune_axis.is_empty()
    {
        return None;
    }
    let candidates = training_candidates(cube, fit_count);
    let mut beam = Vec::new();
    let mut scored_counts = Vec::new();
    for size in 1..=requested_n {
        let combinations = if size == 1 {
            (0..cube.powers.len()).map(|i| vec![i]).collect()
        } else {
            candidate_expansions(&beam, cube.powers.len())
        };
        scored_counts.push(combinations.len());
        let mut scored: Vec<SearchScore> = combinations
            .par_iter()
            .map(|members| {
                score_subset(
                    cube,
                    members,
                    fit_count,
                    &candidates,
                    digitizers,
                    ring_orders,
                    window_turns,
                    search,
                )
            })
            .collect();
        // Parallel evaluation preserves input order; an explicit lexical tie
        // break matches Python's stable sort of lexically ordered expansions.
        scored.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then_with(|| a.members.cmp(&b.members))
        });
        scored.truncate(search.beam_width.max(1));
        beam = scored;
    }
    beam.into_iter()
        .next()
        .map(|winner| (winner, scored_counts))
}

fn aggregate_metrics(
    cube: &SpectralCube,
    members: &[usize],
    test_start: usize,
    reference_q: f64,
    search: &SearchSettings,
) -> AggregateMetrics {
    if members.is_empty() || test_start >= cube.starts.len() || cube.tune_axis.is_empty() {
        return AggregateMetrics::default();
    }
    let half_width = search.later_support_half_width;
    let settings = &search.score;
    let axis: Vec<f64> = cube.tune_axis.iter().map(|q| f64::from(*q)).collect();
    let closest = axis
        .iter()
        .enumerate()
        .min_by(|(_, a), (_, b)| {
            (*a - reference_q)
                .abs()
                .total_cmp(&(*b - reference_q).abs())
        })
        .map(|(i, _)| i)
        .unwrap_or(0);
    let mask = |width: f64| {
        let mut indices: Vec<usize> = axis
            .iter()
            .enumerate()
            .filter(|(_, q)| !reference_q.is_finite() || (**q - reference_q).abs() <= width)
            .map(|(i, _)| i)
            .collect();
        if indices.is_empty() {
            indices.push(closest);
        }
        indices
    };
    let continuity = mask((half_width * 4.0).max(0.01));
    let signal_bins = mask(half_width);
    let mut background: Vec<usize> = axis
        .iter()
        .enumerate()
        .filter(|(i, q)| {
            (!reference_q.is_finite() || (**q - reference_q).abs() <= (half_width * 5.0).max(0.01))
                && !signal_bins.contains(i)
        })
        .map(|(i, _)| i)
        .collect();
    if background.is_empty() {
        background = (0..axis.len())
            .filter(|i| !signal_bins.contains(i))
            .collect();
    }
    let mut combined = vec![0.0_f32; axis.len()];
    let mut near_q = Vec::new();
    let mut blind_q = Vec::new();
    let mut prominence = Vec::new();
    let mut support = Vec::new();
    let mut entropies = Vec::new();
    for window in test_start..cube.starts.len() {
        mean_spectrum(cube, members, window, &mut combined);
        blind_q.push(axis[argmax(&combined)]);
        let local: Vec<f32> = continuity.iter().map(|i| combined[*i]).collect();
        near_q.push(axis[continuity[argmax(&local)]]);
        let signal = signal_bins
            .iter()
            .map(|i| f64::from(combined[*i]))
            .fold(0.0_f64, f64::max);
        let back: Vec<f64> = background.iter().map(|i| f64::from(combined[*i])).collect();
        support.push(if back.is_empty() {
            f64::NAN
        } else {
            signal / median(&back).max(settings.log_power_floor)
        });
        // The reference computes these logs on float32 combined powers.
        let logs: Vec<f64> = back
            .iter()
            .map(|p| f64::from((*p as f32 + settings.log_power_floor as f32).log10()))
            .collect();
        let center = median(&logs);
        let mad = median(&logs.iter().map(|v| (v - center).abs()).collect::<Vec<_>>())
            * settings.mad_scale;
        prominence.push(
            (f64::from((signal as f32 + settings.log_power_floor as f32).log10()) - center)
                / mad.max(settings.mad_floor),
        );
        entropies.push(entropy(&combined));
    }
    AggregateMetrics {
        q_hat: finite(median(&near_q)),
        blind_q_hat: finite(median(&blind_q)),
        peak_prominence: finite(median(&prominence)),
        p10_peak_prominence: finite(percentile(&prominence, 0.10)),
        power_support: finite(median(&support)),
        visible_fraction: Some(
            prominence
                .iter()
                .filter(|p| **p >= settings.visibility_threshold)
                .count() as f64
                / prominence.len() as f64,
        ),
        spectral_entropy: finite(median(&entropies)),
    }
}

fn ridge_spectra(
    traces: &[&[f32]],
    starts: &[usize],
    settings: &SpectralSettings,
) -> Vec<Vec<f32>> {
    let window = settings.window_turns;
    let nfft = window * settings.padding_factor;
    let taper = taper(settings, window);
    let fft = FftPlanner::<f32>::new().plan_fft_forward(nfft);
    let scales: Vec<f32> = traces
        .iter()
        .map(|trace| trace_scale(trace, settings))
        .collect();
    starts
        .par_iter()
        .map(|start| {
            let mut sum = vec![0.0_f32; nfft];
            let mut buffer = vec![Complex::default(); nfft];
            let mut scratch = vec![Complex::default(); fft.get_inplace_scratch_len()];
            for (trace, scale) in traces.iter().zip(&scales) {
                let block = &trace[*start..*start + window];
                let center = if settings.mean_subtract {
                    block.iter().map(|v| v / scale).sum::<f32>() / window as f32
                } else {
                    0.0
                };
                buffer.fill(Complex::default());
                for ((value, sample), weight) in buffer.iter_mut().zip(block).zip(&taper) {
                    *value = Complex::new((*sample / scale - center) * weight, 0.0);
                }
                fft.process_with_scratch(&mut buffer, &mut scratch);
                for (out, value) in sum.iter_mut().zip(&buffer) {
                    *out += value.norm_sqr();
                }
            }
            for out in &mut sum {
                *out = cached_power(*out / traces.len() as f32, settings);
            }
            sum[0] = 0.0;
            sum
        })
        .collect()
}

fn peak_in_band(spectrum: &[f32], band: [f64; 2]) -> Option<Peak> {
    let n = spectrum.len();
    if n < 8 {
        return None;
    }
    let start = ((band[0] * n as f64).floor() as usize).clamp(3, n - 2);
    let end = ((band[1] * n as f64).ceil() as usize).clamp(2, n - 1);
    if end <= start {
        return None;
    }
    let index = start + argmax(&spectrum[start..end]);
    let power = f64::from(spectrum[index]);
    if !power.is_finite() {
        return None;
    }
    let left = f64::from(spectrum[index - 1]);
    let right = f64::from(spectrum[index + 1]);
    let denom = left - 2.0 * power + right;
    let mut refined = index as f64;
    if denom.abs() > 1e-15 {
        let delta = 0.5 * (left - right) / denom;
        if delta.is_finite() && delta.abs() <= 1.0 {
            refined += delta;
        }
    }
    let mut sorted = spectrum[start..end].to_vec();
    sorted.sort_unstable_by(f32::total_cmp);
    // This is intentionally the upper middle value for even-length bands,
    // exactly as pick_peak_in_band, not the averaged statistical median.
    let median_power = f64::from(sorted[sorted.len() / 2]).max(1e-12);
    let confidence = power / median_power;
    confidence.is_finite().then_some(Peak {
        q: (refined / n as f64).clamp(0.0, 1.0),
        confidence,
    })
}

/// Analyze a complete acquisition without transport, clock, or publication state.
/// The returned raw candidates are independent of the operational display gates.
pub fn analyze(input: &SpillInput, spec: &MeasurementSpec) -> BTreeMap<String, PlaneResult> {
    let invalid = spec
        .validate()
        .err()
        .map(|error| format!("invalid_spec:{error}"))
        .or_else(|| (input.schema_version != 1).then(|| "unsupported_input_schema".into()));
    ["H", "V"]
        .into_iter()
        .map(|plane| {
            let plane_spec = spec.planes.get(plane).expect("known plane");
            let result = if let Some(reason) = &invalid {
                PlaneResult {
                    state: "unavailable".into(),
                    reasons: vec![reason.clone()],
                    ..PlaneResult::default()
                }
            } else {
                analyze_plane(input, spec, plane, plane_spec)
            };
            (plane.into(), result)
        })
        .collect()
}

fn analyze_plane(
    input: &SpillInput,
    spec: &MeasurementSpec,
    plane: &str,
    plane_spec: &PlaneSpec,
) -> PlaneResult {
    let requested_n = if plane_spec.mode == EnsembleMode::Static {
        plane_spec.members.len()
    } else {
        plane_spec.n
    };
    let mut result = PlaneResult {
        state: "unavailable".into(),
        mode: match plane_spec.mode {
            EnsembleMode::BestN => "best_n",
            EnsembleMode::Static => "static",
        }
        .into(),
        requested_n,
        ..PlaneResult::default()
    };
    if !plane_spec.enabled {
        result.reasons.push("disabled".into());
        return result;
    }
    let mut traces: Vec<&TraceInput> = input
        .traces
        .iter()
        .filter(|trace| trace.plane == plane)
        .collect();
    traces.sort_by(|a, b| {
        a.id.cmp(&b.id)
            .then_with(|| a.source_key.cmp(&b.source_key))
    });
    if traces.is_empty() {
        result.reasons.push("no_position_sources".into());
        return result;
    }
    let mut identities = BTreeMap::<&str, usize>::new();
    for trace in &traces {
        *identities.entry(&trace.id).or_default() += 1;
    }
    let mut static_invalid = false;
    if plane_spec.mode == EnsembleMode::Static {
        for member in &plane_spec.members {
            match identities.get(member.as_str()).copied().unwrap_or(0) {
                0 => {
                    result
                        .reasons
                        .push(format!("missing_static_member:{member}"));
                    static_invalid = true;
                }
                1 => (),
                _ => {
                    result
                        .reasons
                        .push(format!("ambiguous_static_member:{member}"));
                    static_invalid = true;
                }
            }
        }
        if static_invalid {
            result.available_n = traces.len();
            return result;
        }
    }
    traces.retain(|trace| {
        if identities[trace.id.as_str()] > 1 {
            result
                .reasons
                .push(format!("excluded_source:{}:duplicate_identity", trace.id));
            false
        } else {
            true
        }
    });
    let expected_turns = if plane_spec.mode == EnsembleMode::Static {
        let lengths: BTreeSet<usize> = traces
            .iter()
            .filter(|trace| plane_spec.members.contains(&trace.id))
            .map(|trace| trace.samples.len())
            .collect();
        if lengths.len() != 1 {
            result.reasons.push("static_member_length_mismatch".into());
            result.available_n = traces.len();
            return result;
        }
        *lengths.first().expect("nonempty static members")
    } else {
        let mut lengths = BTreeMap::<usize, usize>::new();
        for trace in &traces {
            *lengths.entry(trace.samples.len()).or_default() += 1;
        }
        let maximum_count = lengths.values().max().copied().unwrap_or(0);
        // Same modal-length rule as the source scripts, with deterministic
        // first-identity tie breaking independent of acquisition input order.
        traces
            .iter()
            .find(|trace| lengths[&trace.samples.len()] == maximum_count)
            .map_or(0, |trace| trace.samples.len())
    };
    result.available_turns = expected_turns;
    let begin = spec.analysis_start_turn;
    let end = spec
        .analysis_end_turn
        .unwrap_or(expected_turns)
        .min(expected_turns);
    result.used_turns = end.saturating_sub(begin);
    let selection_end = spec.selection_end_turn.unwrap_or(end).min(end);
    let starts = complete_windows(
        selection_end.saturating_sub(begin),
        spec.selection.window_turns,
        spec.selection.step_turns,
    );
    if starts.is_empty() {
        result
            .reasons
            .push("insufficient_turns_for_selection_window".into());
        return result;
    }
    traces.retain(|trace| {
        let reason = if trace.samples.len() != expected_turns {
            Some("sample_count_mismatch")
        } else if !trace.samples[begin..end]
            .iter()
            .all(|value| value.is_finite())
        {
            Some("nonfinite_position_samples")
        } else {
            None
        };
        if let Some(reason) = reason {
            result
                .reasons
                .push(format!("excluded_source:{}:{reason}", trace.id));
            if plane_spec.mode == EnsembleMode::Static && plane_spec.members.contains(&trace.id) {
                static_invalid = true;
            }
            false
        } else {
            true
        }
    });
    result.available_n = traces.len();
    if static_invalid {
        result.reasons.push("static_members_unavailable".into());
        return result;
    }
    if traces.len() < requested_n {
        result.reasons.push(format!(
            "insufficient_sources:{}/{requested_n}",
            traces.len()
        ));
        return result;
    }
    if let Err(error) = spec.validate_execution(plane, expected_turns, traces.len()) {
        result
            .reasons
            .push(format!("execution_budget_exceeded:{error}"));
        return result;
    }
    let cropped: Vec<&[f32]> = traces
        .iter()
        .map(|trace| &trace.samples[begin..selection_end])
        .collect();
    let mut cube = selection_spectra(
        &cropped,
        &starts,
        &spec.selection,
        plane_spec.selection_band,
    );
    if cube.tune_axis.is_empty() {
        result.reasons.push("empty_selection_band".into());
        return result;
    }
    // An overflowed half cache is not a usable candidate. Do not silently clip
    // it, which would change the paper score and could select invalid sources.
    let valid: Vec<bool> = cube
        .powers
        .iter()
        .map(|powers| powers.iter().all(|p| p.is_finite()))
        .collect();
    for (trace, valid) in traces.iter().zip(&valid) {
        if !valid {
            result.reasons.push(format!(
                "excluded_source:{}:nonfinite_selection_spectrum",
                trace.id
            ));
            if plane_spec.mode == EnsembleMode::Static && plane_spec.members.contains(&trace.id) {
                static_invalid = true;
            }
        }
    }
    let mut index = 0;
    traces.retain(|_| {
        let keep = valid[index];
        index += 1;
        keep
    });
    index = 0;
    cube.powers.retain(|_| {
        let keep = valid[index];
        index += 1;
        keep
    });
    result.available_n = traces.len();
    if static_invalid || traces.len() < requested_n {
        result.reasons.push(if static_invalid {
            "static_members_unavailable".into()
        } else {
            format!("insufficient_sources:{}/{requested_n}", traces.len())
        });
        return result;
    }
    let (fit_count, test_start) = purged_split(
        &starts,
        spec.search.fit_windows,
        spec.selection.window_turns,
    );
    result.fit_windows = fit_count;
    result.fit_end_turn =
        (fit_count > 0).then(|| begin + starts[fit_count - 1] + spec.selection.window_turns);
    result.test_start_turn = starts.get(test_start).map(|start| begin + start);
    if fit_count < spec.search.fit_windows {
        result.reasons.push(format!(
            "reduced_fit_windows:{fit_count}/{}",
            spec.search.fit_windows
        ));
    }
    if test_start == starts.len() {
        result.reasons.push("no_later_test_windows".into());
    }
    let digitizers: Vec<&str> = traces
        .iter()
        .map(|trace| trace.digitizer.as_str())
        .collect();
    let orders: Vec<Option<f64>> = traces
        .iter()
        .map(|trace| finite(trace.ring_order))
        .collect();
    if traces
        .iter()
        .any(|trace| trace.digitizer.is_empty() || !trace.ring_order.is_finite())
    {
        result.reasons.push("incomplete_diversity_metadata".into());
    }
    let selected = if plane_spec.mode == EnsembleMode::BestN {
        beam_search(
            &cube,
            fit_count,
            &digitizers,
            &orders,
            spec.selection.window_turns,
            requested_n,
            &spec.search,
        )
        .map(|(score, _)| score)
    } else {
        let members: Vec<usize> = traces
            .iter()
            .enumerate()
            .filter(|(_, trace)| plane_spec.members.contains(&trace.id))
            .map(|(i, _)| i)
            .collect();
        let candidates = training_candidates(&cube, fit_count);
        Some(score_subset(
            &cube,
            &members,
            fit_count,
            &candidates,
            &digitizers,
            &orders,
            spec.selection.window_turns,
            &spec.search,
        ))
    };
    let Some(score) = selected.filter(|score| {
        [
            score.score,
            score.q_hat,
            score.candidate_support,
            score.peak_quality,
            score.consensus_agreement,
            score.window_stability,
            score.diversity,
            score.ambiguity,
            score.visible_fraction,
            score.visibility_duration_turns,
        ]
        .iter()
        .all(|value| value.is_finite())
    }) else {
        result.reasons.push("no_finite_selection_score".into());
        return result;
    };
    result.selected_members = score
        .members
        .iter()
        .map(|i| traces[*i].id.clone())
        .collect();
    result.candidate_q = finite(score.q_hat);
    result.score = Some(ScoreComponents {
        subset_score: score.score,
        holdout_support: score.candidate_support,
        peak_quality: score.peak_quality,
        consensus_agreement: score.consensus_agreement,
        window_stability: score.window_stability,
        diversity_score: score.diversity,
        ambiguity_penalty: score.ambiguity,
        visible_fraction: score.visible_fraction,
        visibility_duration_turns: score.visibility_duration_turns,
    });
    let later = aggregate_metrics(&cube, &score.members, test_start, score.q_hat, &spec.search);
    result.later_support = Some(LaterSupport {
        window_count: starts.len() - test_start,
        q_hat: later.q_hat,
        blind_q_hat: later.blind_q_hat,
        peak_prominence: later.peak_prominence,
        p10_peak_prominence: later.p10_peak_prominence,
        power_support: later.power_support,
        visible_fraction: later.visible_fraction,
        spectral_entropy: later.spectral_entropy,
    });
    let selected_traces: Vec<&TraceInput> = score.members.iter().map(|i| traces[*i]).collect();
    let cropped: Vec<&[f32]> = selected_traces
        .iter()
        .map(|trace| &trace.samples[begin..end])
        .collect();
    let ridge_starts = complete_windows(
        result.used_turns,
        spec.trajectory.window_turns,
        spec.trajectory.step_turns,
    );
    if ridge_starts.is_empty() {
        result
            .reasons
            .push("insufficient_turns_for_trajectory_window".into());
        return result;
    }
    let spectra = ridge_spectra(&cropped, &ridge_starts, &spec.trajectory);
    let seed = if spec.tracking.injection_window_turns <= result.used_turns {
        if spec.tracking.injection_window_turns == spec.trajectory.window_turns {
            peak_in_band(&spectra[0], plane_spec.trajectory_band)
        } else {
            let mut seed_settings = spec.trajectory.clone();
            seed_settings.window_turns = spec.tracking.injection_window_turns;
            let seed_spectra = ridge_spectra(&cropped, &[0], &seed_settings);
            peak_in_band(&seed_spectra[0], plane_spec.trajectory_band)
        }
        .filter(|peak| peak.confidence >= spec.quality.min_peak_confidence)
    } else {
        None
    };
    if spec.tracking.enabled && seed.is_none() {
        result.reasons.push("no_trusted_injection_seed".into());
    }
    let nfft = spec.trajectory.window_turns * spec.trajectory.padding_factor;
    // Publish exactly the integer band searched by the ridge picker, including
    // its floor-rounded lower edge and exclusive ceil-rounded upper edge.
    let band_start =
        ((plane_spec.trajectory_band[0] * nfft as f64).floor() as usize).clamp(3, nfft - 2);
    let band_end =
        ((plane_spec.trajectory_band[1] * nfft as f64).ceil() as usize).clamp(2, nfft - 1);
    let bins: Vec<usize> = (band_start..band_end).collect();
    result.spectrum_tune = bins
        .iter()
        .map(|index| *index as f64 / nfft as f64)
        .collect();
    let mut trusted = seed.map(|peak| peak.q);
    for (start, spectrum) in ridge_starts.iter().zip(&spectra) {
        let raw = peak_in_band(spectrum, plane_spec.trajectory_band);
        let tracked = if spec.tracking.enabled {
            trusted.and_then(|q| {
                let band = [
                    plane_spec.trajectory_band[0].max(q - spec.tracking.half_width),
                    plane_spec.trajectory_band[1].min(q + spec.tracking.half_width),
                ];
                (band[1] > band[0])
                    .then(|| peak_in_band(spectrum, band))
                    .flatten()
            })
        } else {
            None
        };
        let tracked_passes =
            tracked.is_some_and(|peak| peak.confidence >= spec.quality.min_peak_confidence);
        let candidate = if tracked_passes { tracked } else { raw };
        let used_raw_fallback =
            spec.tracking.enabled && trusted.is_some() && !tracked_passes && raw.is_some();
        let suspicious_step = spec.tracking.enabled
            && trusted
                .zip(candidate)
                .is_some_and(|(previous, peak)| (peak.q - previous).abs() > spec.tracking.max_step);
        if spec.tracking.enabled && tracked_passes && !suspicious_step {
            trusted = tracked.map(|peak| peak.q);
        }
        let window_start = begin + start;
        let window_end = window_start + spec.trajectory.window_turns;
        let (beam_fraction, beam_reason) = beam_support(
            &selected_traces,
            window_start,
            window_end,
            spec.quality.beam_threshold_override,
        );
        let mut reasons = Vec::new();
        let mut gated = false;
        if candidate.is_none() {
            reasons.push("no_finite_peak".into());
        }
        if candidate.is_some_and(|peak| peak.confidence < spec.quality.min_peak_confidence) {
            reasons.push("low_peak_confidence".into());
            gated = true;
        }
        if let Some(reason) = beam_reason {
            reasons.push(reason);
            if spec.quality.require_intensity {
                gated = true;
            }
        }
        if beam_fraction.is_some_and(|fraction| fraction < spec.quality.min_beam_fraction) {
            reasons.push("insufficient_beam_support".into());
            gated = true;
        }
        if input.synthetic {
            reasons.push("synthetic_input".into());
            if spec.quality.suppress_synthetic {
                gated = true;
            }
        }
        if suspicious_step {
            reasons.push("suspicious_tune_step".into());
            gated = true;
        }
        if used_raw_fallback {
            reasons.push("raw_global_fallback".into());
        }
        if spec.tracking.enabled && seed.is_none() {
            reasons.push("no_trusted_injection_seed".into());
        }
        let q = if gated {
            None
        } else {
            candidate.map(|peak| peak.q)
        };
        result.windows.push(WindowResult {
            start_turn: window_start,
            end_turn_exclusive: window_end,
            center_turn: window_start + spec.trajectory.window_turns / 2,
            state: if candidate.is_none() {
                "unavailable"
            } else if gated {
                "suppressed"
            } else {
                "valid"
            }
            .into(),
            reasons,
            q,
            candidate_q: candidate.map(|peak| peak.q),
            raw_q: raw.map(|peak| peak.q),
            tracked_q: tracked.map(|peak| peak.q),
            confidence: candidate.map(|peak| peak.confidence),
            beam_fraction,
            suspicious_step,
            used_raw_fallback,
            spectrum_power: bins
                .iter()
                .map(|index| finite(f64::from(spectrum[*index])))
                .collect(),
        });
    }
    result.q = finite(median(
        &result
            .windows
            .iter()
            .filter_map(|window| window.q)
            .collect::<Vec<_>>(),
    ));
    result.state = if result.q.is_some() {
        "valid"
    } else if result
        .windows
        .iter()
        .any(|window| window.candidate_q.is_some())
    {
        "suppressed"
    } else {
        "unavailable"
    }
    .into();
    if result.q.is_none() {
        result.reasons.push("no_accepted_trajectory_windows".into());
    }
    result
}

fn beam_support(
    traces: &[&TraceInput],
    start: usize,
    end: usize,
    threshold_override: Option<f64>,
) -> (Option<f64>, Option<String>) {
    let mut fractions = Vec::with_capacity(traces.len());
    let mut missing = false;
    let mut invalid = false;
    for trace in traces {
        let Some(intensity) = &trace.intensity else {
            missing = true;
            continue;
        };
        if intensity.len() < end {
            missing = true;
            continue;
        }
        let threshold = threshold_override.unwrap_or(trace.beam_threshold);
        if !threshold.is_finite() || !intensity[start..end].iter().all(|value| value.is_finite()) {
            invalid = true;
            continue;
        }
        fractions.push(
            intensity[start..end]
                .iter()
                .filter(|value| f64::from(**value) >= threshold)
                .count() as f64
                / (end - start) as f64,
        );
    }
    // Do not take a median of a partially observed ensemble and imply that the
    // missing channels were beam-supported. The candidate remains available.
    if missing {
        (None, Some("missing_selected_intensity".into()))
    } else if invalid {
        (None, Some("invalid_selected_intensity".into()))
    } else {
        (finite(median(&fractions)), None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn trace(id: &str, turns: usize, q: f64) -> TraceInput {
        TraceInput {
            id: id.into(),
            plane: "H".into(),
            digitizer: format!("digitizer-{id}"),
            ring_order: id[2..].parse().unwrap(),
            source_key: format!("position:{id}"),
            source_stream_id: "1-0".into(),
            intensity_stream_id: Some("1-0".into()),
            beam_threshold: 200.0,
            samples: (0..turns)
                .map(|i| (2.0 * std::f64::consts::PI * q * i as f64).sin() as f32)
                .collect(),
            intensity: Some(vec![500.0; turns]),
        }
    }

    fn input(traces: Vec<TraceInput>) -> SpillInput {
        SpillInput {
            schema_version: 1,
            acquisition_id: "fixture".into(),
            source_time_ms: 1,
            captured_at_ms: 2,
            synthetic: false,
            traces,
            warnings: vec![],
        }
    }

    fn small_spec() -> MeasurementSpec {
        let mut spec = MeasurementSpec::default();
        spec.selection.window_turns = 128;
        spec.selection.step_turns = 64;
        spec.trajectory.window_turns = 128;
        spec.trajectory.step_turns = 64;
        spec.tracking.injection_window_turns = 128;
        spec.search.fit_windows = 3;
        spec.analysis_end_turn = None;
        spec.planes.h.n = 2;
        spec.planes.v.enabled = false;
        spec
    }

    #[test]
    fn paper_score_fixture_uses_joint_search_instead_of_top_singletons() {
        // Frozen from the repository's Python beam_search_curve on integer
        // power, so this checks the score and the search independently of FFT.
        let powers = [
            [
                24., 24., 23., 14., 18., 8., 26., 20., 32., 1., 32., 23., 5., 28., 16., 6., 23., 6.,
            ],
            [
                5., 27., 9., 12., 3., 28., 3., 23., 2., 14., 31., 28., 17., 10., 3., 31., 31., 11.,
            ],
            [
                30., 12., 30., 25., 7., 29., 19., 9., 9., 7., 10., 3., 8., 12., 1., 14., 14., 3.,
            ],
            [
                19., 25., 5., 25., 26., 21., 31., 28., 24., 29., 22., 33., 28., 11., 30., 23., 25.,
                15.,
            ],
            [
                33., 13., 19., 12., 10., 12., 10., 4., 28., 5., 21., 3., 8., 24., 22., 2., 17., 25.,
            ],
        ];
        let cube = SpectralCube {
            powers: powers.map(Vec::from).to_vec(),
            tune_axis: vec![0.67, 0.66, 0.65, 0.64, 0.63, 0.62],
            starts: vec![0, 16, 32],
        };
        let spec = MeasurementSpec::default();
        let digits = ["d0", "d1", "d2", "d0", "d1"];
        let orders = [Some(100.), Some(200.), Some(300.), Some(400.), Some(500.)];
        let (one, _) = beam_search(&cube, 3, &digits, &orders, 64, 1, &spec.search).unwrap();
        assert_eq!(one.members, [4]);
        assert!((one.score - 0.527443736246537).abs() < 1e-12);
        let (two, counts) = beam_search(&cube, 3, &digits, &orders, 64, 2, &spec.search).unwrap();
        assert_eq!(two.members, [0, 1]); // Python's top two singletons are [3,4].
        assert_eq!(counts, [5, 10]);
        assert!((two.score - 0.5297621753280461).abs() < 1e-12);
        assert!((two.peak_quality - 0.08632494120976113).abs() < 1e-12);
        let (three, _) = beam_search(&cube, 3, &digits, &orders, 64, 3, &spec.search).unwrap();
        assert_eq!(three.members, [0, 1, 2]);
        assert!((three.score - 0.5204203325863975).abs() < 1e-12);
        assert!((three.window_stability - 0.43459855389484).abs() < 1e-12);
    }

    #[test]
    fn purge_has_no_shared_turns_and_reports_short_data() {
        let starts = complete_windows(11037, 4096, 256);
        assert_eq!(purged_split(&starts, 8, 4096), (8, 23));
        let (fit, test) = purged_split(&complete_windows(8192, 4096, 256), 8, 4096);
        assert_eq!((fit, test), (1, 16));
        assert_eq!(purged_split(&[0, 256, 512], 8, 4096), (1, 3));
        assert_eq!(purged_split(&[0], 8, 4096), (1, 1));
        assert!(complete_windows(127, 128, 64).is_empty());
    }

    #[test]
    fn dynamic_lengths_drop_partial_windows_and_keep_upper_tune_branch() {
        let spec = small_spec();
        let result = analyze(
            &input(vec![trace("HP102", 555, 0.65), trace("HP104", 555, 0.65)]),
            &spec,
        );
        let h = &result["H"];
        assert_eq!(h.state, "valid");
        assert_eq!(h.available_turns, 555);
        assert_eq!(h.windows.len(), 7);
        assert_eq!(h.windows.last().unwrap().end_turn_exclusive, 512);
        assert!((h.q.unwrap() - 0.65).abs() < 0.002);
        assert!(
            h.windows
                .iter()
                .all(|window| window.spectrum_power.len() == h.spectrum_tune.len())
        );
        assert!(h.spectrum_tune.iter().all(|q| *q > 0.5));
        assert!(h.test_start_turn.unwrap() >= h.fit_end_turn.unwrap());
    }

    #[test]
    fn paper_selection_horizon_is_separate_from_fifty_thousand_turn_ridge() {
        let mut spec = MeasurementSpec::default();
        spec.planes.h.n = 1;
        spec.planes.v.enabled = false;
        let result = analyze(&input(vec![trace("HP102", 50123, 0.65)]), &spec);
        let h = &result["H"];
        assert_eq!(h.used_turns, 50000);
        assert_eq!(h.fit_windows, 8);
        assert_eq!(h.fit_end_turn, Some(5888));
        assert_eq!(h.test_start_turn, Some(5888));
        assert_eq!(h.later_support.as_ref().unwrap().window_count, 20);
        assert_eq!(h.windows.len(), 180);
        assert!(h.windows.last().unwrap().end_turn_exclusive <= 50000);
    }

    #[test]
    fn static_members_never_replace_missing_or_malformed_sources() {
        let mut spec = small_spec();
        spec.planes.h.mode = EnsembleMode::Static;
        spec.planes.h.members = vec!["HP102".into(), "HP104".into()];
        let missing = analyze(
            &input(vec![trace("HP102", 512, 0.65), trace("HP106", 512, 0.65)]),
            &spec,
        );
        assert_eq!(missing["H"].state, "unavailable");
        assert!(missing["H"].selected_members.is_empty());
        assert!(
            missing["H"]
                .reasons
                .iter()
                .any(|r| r == "missing_static_member:HP104")
        );
        let unequal = analyze(
            &input(vec![trace("HP102", 512, 0.65), trace("HP104", 511, 0.65)]),
            &spec,
        );
        assert!(
            unequal["H"]
                .reasons
                .iter()
                .any(|r| r == "static_member_length_mismatch")
        );
        let mut broken = trace("HP104", 512, 0.65);
        broken.samples[31] = f32::NAN;
        let invalid = analyze(
            &input(vec![
                trace("HP102", 512, 0.65),
                broken,
                trace("HP106", 512, 0.65),
            ]),
            &spec,
        );
        assert_eq!(invalid["H"].state, "unavailable");
        assert!(invalid["H"].selected_members.is_empty());
    }

    #[test]
    fn automatic_mode_never_reduces_requested_n() {
        let mut spec = small_spec();
        spec.planes.h.n = 3;
        let result = analyze(
            &input(vec![trace("HP102", 512, 0.65), trace("HP104", 512, 0.65)]),
            &spec,
        );
        assert_eq!(result["H"].state, "unavailable");
        assert_eq!(result["H"].available_n, 2);
        assert!(result["H"].selected_members.is_empty());
    }

    #[test]
    fn unsafe_plane_budget_is_rejected_before_spectra_without_blocking_other_plane() {
        let mut spec = small_spec();
        spec.selection.step_turns = 1;
        spec.trajectory.step_turns = 1;
        spec.planes.h.trajectory_band = [0.5, 1.0];
        spec.planes.v.enabled = true;
        spec.planes.v.mode = EnsembleMode::Static;
        spec.planes.v.members = vec!["VP102".into()];
        let mut vertical = trace("HP102", 7000, 0.72);
        vertical.id = "VP102".into();
        vertical.plane = "V".into();
        let result = analyze(
            &input(vec![
                trace("HP102", 7000, 0.65),
                trace("HP104", 7000, 0.65),
                vertical,
            ]),
            &spec,
        );
        let h = &result["H"];
        assert_eq!(h.state, "unavailable");
        assert!(
            h.reasons
                .iter()
                .any(|reason| reason.starts_with("execution_budget_exceeded:"))
        );
        assert!(h.selected_members.is_empty());
        assert!(h.score.is_none());
        assert!(h.windows.is_empty());
        assert_eq!(result["V"].state, "valid");
        assert_eq!(result["V"].selected_members, ["VP102"]);
    }

    #[test]
    fn display_gates_preserve_candidates_and_spectra() {
        let spec = small_spec();
        let mut spill = input(vec![trace("HP102", 512, 0.65), trace("HP104", 512, 0.65)]);
        let valid = analyze(&spill, &spec);
        spill.traces[0].intensity = None;
        let missing = analyze(&spill, &spec);
        assert_eq!(missing["H"].state, "suppressed");
        assert_eq!(missing["H"].candidate_q, valid["H"].candidate_q);
        assert_eq!(
            missing["H"].windows[0].candidate_q,
            valid["H"].windows[0].candidate_q
        );
        assert_eq!(
            missing["H"].windows[0].spectrum_power,
            valid["H"].windows[0].spectrum_power
        );
        assert!(
            missing["H"]
                .windows
                .iter()
                .all(|window| window.q.is_none() && window.beam_fraction.is_none())
        );
        spill.traces[0].intensity = Some(vec![500.; 512]);
        spill.synthetic = true;
        let synthetic = analyze(&spill, &spec);
        assert_eq!(synthetic["H"].state, "suppressed");
        assert_eq!(synthetic["H"].candidate_q, valid["H"].candidate_q);
        assert!(
            synthetic["H"].windows[0]
                .reasons
                .contains(&"synthetic_input".into())
        );
        spill.synthetic = false;
        for trace in &mut spill.traces {
            trace.intensity = Some(vec![0.; 512]);
        }
        let absent = analyze(&spill, &spec);
        assert_eq!(absent["H"].windows[0].beam_fraction, Some(0.));
        assert_eq!(
            absent["H"].windows[0].candidate_q,
            valid["H"].windows[0].candidate_q
        );
        assert!(absent["H"].windows[0].q.is_none());
    }

    #[test]
    fn beam_support_is_median_of_exact_selected_fractions() {
        let mut a = trace("HP102", 128, 0.65);
        let b = trace("HP104", 128, 0.65);
        let c = trace("HP106", 128, 0.65);
        a.intensity = Some(vec![0.; 128]);
        assert_eq!(beam_support(&[&a, &b, &c], 0, 128, None), (Some(1.), None));
        assert_eq!(beam_support(&[&a, &b], 0, 128, None), (Some(0.5), None));
        assert_eq!(
            beam_support(&[&b, &c], 0, 128, Some(600.)),
            (Some(0.), None)
        );
        a.intensity = None;
        assert_eq!(beam_support(&[&a, &b, &c], 0, 128, None).0, None);
    }

    #[test]
    fn rms_normalization_uses_only_the_cropped_analysis_range() {
        let mut spec = small_spec();
        spec.analysis_end_turn = Some(512);
        let mut spill = input(vec![trace("HP102", 900, 0.65), trace("HP104", 900, 0.65)]);
        let baseline = analyze(&spill, &spec);
        for trace in &mut spill.traces {
            for value in &mut trace.samples[512..] {
                *value *= 1e6;
            }
        }
        let changed = analyze(&spill, &spec);
        assert_eq!(
            serde_json::to_value(&baseline).unwrap(),
            serde_json::to_value(&changed).unwrap()
        );
    }

    #[test]
    fn fft_quantization_and_normalization_are_separate_paths() {
        let spec = small_spec();
        let a = trace("HP102", 512, 0.65);
        let mut b = a.clone();
        for value in &mut b.samples {
            *value *= 4.;
        }
        let cube = selection_spectra(
            &[&a.samples, &b.samples],
            &[0],
            &spec.selection,
            spec.planes.h.selection_band,
        );
        assert!(
            cube.powers
                .iter()
                .flatten()
                .all(|p| f16::from_f32(*p).to_f32() == *p)
        );
        let peak = argmax(&cube.powers[0]);
        assert!((cube.powers[1][peak] / cube.powers[0][peak] - 16.).abs() < 0.03);
        let one = ridge_spectra(&[&a.samples], &[0], &spec.trajectory);
        let two = ridge_spectra(&[&b.samples], &[0], &spec.trajectory);
        for (a, b) in one[0].iter().zip(&two[0]) {
            assert!((a - b).abs() < 1e-4);
        }
    }

    #[test]
    fn nonzero_start_is_reported_in_absolute_turn_coordinates() {
        let mut spec = small_spec();
        spec.analysis_start_turn = 23;
        let result = analyze(
            &input(vec![trace("HP102", 555, 0.65), trace("HP104", 555, 0.65)]),
            &spec,
        );
        assert_eq!(result["H"].used_turns, 532);
        assert_eq!(result["H"].windows[0].start_turn, 23);
        assert_eq!(result["H"].windows[0].center_turn, 87);
        assert_eq!(result["H"].windows.last().unwrap().end_turn_exclusive, 535);
    }
}
