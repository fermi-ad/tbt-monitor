"""Scoring primitives for BPM subset search."""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

from .gpu import Backend


@dataclass
class SubsetScore:
    subset: tuple[int, ...]
    q_hat: float
    subset_score: float
    holdout_support: float
    peak_quality: float
    consensus_agreement: float
    window_stability: float
    diversity_score: float
    ambiguity_penalty: float
    visible_fraction: float
    visibility_duration_turns: float


def subset_mask(bpm_indices: Sequence[int]) -> int:
    mask = 0
    for idx in bpm_indices:
        if idx < 0 or idx >= 64:
            continue
        mask |= 1 << int(idx)
    return mask


def median(values: Iterable[float]) -> float:
    vals = [v for v in values if math.isfinite(v)]
    return statistics.median(vals) if vals else math.nan


def percentile(values: Iterable[float], pct: float) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def spectral_entropy(power: np.ndarray) -> np.ndarray:
    p = np.asarray(power, dtype=np.float64)
    p = np.where(np.isfinite(p) & (p > 0), p, 0.0)
    total = np.sum(p, axis=-1, keepdims=True)
    total = np.where(total > 0, total, 1.0)
    frac = p / total
    safe_frac = np.where(frac > 0, frac, 1.0)
    ent = -np.sum(np.where(frac > 0, frac * np.log(safe_frac), 0.0), axis=-1)
    return ent / math.log(max(2, p.shape[-1]))


def spectral_entropy_xp(xp, power):
    p = xp.asarray(power, dtype=xp.float64)
    p = xp.where(xp.isfinite(p) & (p > 0), p, 0.0)
    total = xp.sum(p, axis=-1, keepdims=True)
    total = xp.where(total > 0, total, 1.0)
    frac = p / total
    safe_frac = xp.where(frac > 0, frac, 1.0)
    ent = -xp.sum(xp.where(frac > 0, frac * xp.log(safe_frac), 0.0), axis=-1)
    return ent / math.log(max(2, int(power.shape[-1])))


def score_subset_chunk(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    window_centers: np.ndarray,
    candidate_tunes: np.ndarray,
    combos: np.ndarray,
    digitizers: Sequence[str],
    ring_orders: Sequence[float],
    consensus_tune: float | None,
    consensus_uncertainty: float | None,
    tune_tolerance: float,
    device: str = "cpu",
) -> list[SubsetScore]:
    if combos.size == 0:
        return []
    subset_size = combos.shape[1]
    backend = Backend(device)
    xp = backend.xp
    x_spectra = xp.asarray(spectra, dtype=xp.float32)
    x_combos = xp.asarray(combos, dtype=xp.int32)
    x_tune_axis = xp.asarray(tune_axis, dtype=xp.float32)
    combined = xp.mean(x_spectra[x_combos, :, :], axis=1, dtype=xp.float32)
    log_power = xp.log10(combined.astype(xp.float64) + 1e-24)
    peak_idx = xp.argmax(log_power, axis=2)
    q_windows = backend.to_numpy(x_tune_axis[peak_idx])
    peak_log = xp.take_along_axis(log_power, peak_idx[:, :, None], axis=2)[:, :, 0]
    band_median = xp.median(log_power, axis=2)
    band_mad = xp.median(xp.abs(log_power - band_median[:, :, None]), axis=2) * 1.4826
    prominence = backend.to_numpy((peak_log - band_median) / xp.maximum(band_mad, 1e-9))
    entropy = backend.to_numpy(spectral_entropy_xp(xp, combined))
    sorted_log = xp.sort(log_power, axis=2)
    if int(sorted_log.shape[2]) >= 2:
        second_ratio = backend.to_numpy(10 ** (sorted_log[:, :, -2] - sorted_log[:, :, -1]))
    else:
        second_ratio = np.ones_like(prominence)
    backend.synchronize()
    out: list[SubsetScore] = []
    total_bpms = spectra.shape[0]
    all_candidates = np.asarray(candidate_tunes, dtype=np.float64)
    for row_idx, combo in enumerate(combos):
        q_vals = [float(v) for v in q_windows[row_idx] if math.isfinite(float(v))]
        q_hat = median(q_vals)
        prom_vals = [float(v) for v in prominence[row_idx] if math.isfinite(float(v))]
        ent_vals = [float(v) for v in entropy[row_idx] if math.isfinite(float(v))]
        ratios = [float(v) for v in second_ratio[row_idx] if math.isfinite(float(v))]
        if not math.isfinite(q_hat):
            q_hat = math.nan
        selected_support = 0
        all_support = 0
        finite_cands = np.isfinite(all_candidates)
        if math.isfinite(q_hat):
            support = finite_cands & (np.abs(all_candidates - q_hat) <= tune_tolerance)
            all_support = int(np.sum(support))
            selected_support = int(np.sum(support[combo]))
        holdout_count = max(1, total_bpms - subset_size)
        candidate_support = max(0.0, (all_support - selected_support) / holdout_count)
        peak_quality = max(0.0, min(1.0, median(prom_vals) / 12.0)) * 0.7 + max(0.0, min(1.0, 1.0 - median(ent_vals))) * 0.3
        if consensus_tune is None or not math.isfinite(consensus_tune) or not math.isfinite(q_hat):
            consensus_agreement = 0.5
        else:
            sigma = max(consensus_uncertainty or 0.003, 0.003)
            consensus_agreement = math.exp(-0.5 * ((q_hat - consensus_tune) / sigma) ** 2)
        steps = [abs(b - a) for a, b in zip(q_vals, q_vals[1:])]
        step_med = median(steps) if steps else 0.0
        window_stability = math.exp(-min(1.0, step_med / 0.006))
        unique_digits = len({digitizers[int(idx)] for idx in combo if digitizers[int(idx)]})
        digitizer_fraction = unique_digits / max(1, subset_size)
        orders = [ring_orders[int(idx)] for idx in combo if math.isfinite(ring_orders[int(idx)])]
        ring_span = (max(orders) - min(orders)) / 1000.0 if len(orders) > 1 else 0.5
        diversity = 0.5 if subset_size == 1 else max(0.0, min(1.0, 0.65 * digitizer_fraction + 0.35 * ring_span))
        ambiguity = max(0.0, min(0.25, (median(ratios) - 0.6) * 0.5 if ratios else 0.0))
        visible = sum(1 for value in prom_vals if value >= 4.0) / max(1, len(prom_vals))
        duration = float(max(window_centers) - min(window_centers)) if visible and len(window_centers) > 1 else 0.0
        window_scores = []
        for prom, ent in zip(prominence[row_idx], entropy[row_idx]):
            local_peak = max(0.0, min(1.0, float(prom) / 12.0)) * 0.75 + max(0.0, min(1.0, 1.0 - float(ent))) * 0.25
            finite_quality = 1.0 if math.isfinite(float(prom)) else 0.0
            window_scores.append(
                0.35 * candidate_support
                + 0.20 * local_peak
                + 0.15 * consensus_agreement
                + 0.10 * visible
                + 0.10 * diversity
                + 0.10 * finite_quality
                - ambiguity
            )
        subset_score = 0.60 * median(window_scores) + 0.25 * percentile(window_scores, 0.10) + 0.15 * window_stability
        out.append(
            SubsetScore(
                subset=tuple(int(v) for v in combo),
                q_hat=q_hat,
                subset_score=float(subset_score),
                holdout_support=float(candidate_support),
                peak_quality=float(peak_quality),
                consensus_agreement=float(consensus_agreement),
                window_stability=float(window_stability),
                diversity_score=float(diversity),
                ambiguity_penalty=float(ambiguity),
                visible_fraction=float(visible),
                visibility_duration_turns=duration,
            )
        )
    return out


def combination_array(items: Sequence[int], size: int) -> np.ndarray:
    return np.asarray(list(itertools.combinations(items, size)), dtype=np.int16)
