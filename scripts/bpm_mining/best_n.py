"""Best-N beam sweep with time- and digitizer-disjoint validation."""

from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from .contracts import (
    compatible_shard_contracts,
    ensure_run_contract,
    file_sha256,
    load_run_contract,
    object_sha256,
)
from .identity import identity_fields, manifest_by_index
from .io import atomic_write_text, read_csv, write_csv
from .statistics import moving_block_resample, stable_seed
from .subset_search import metadata_for_bpms, score_combos


CURVE_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "subset_mask",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "q_hat",
    "subset_score",
    "holdout_support",
    "peak_quality",
    "consensus_agreement",
    "window_stability",
    "diversity_score",
    "ambiguity_penalty",
    "visible_fraction",
    "visibility_duration_turns",
    "fit_window_count",
    "test_window_count",
    "fit_end_turn",
    "test_start_turn",
    "test_q_hat_near_train",
    "test_abs_q_delta",
    "test_blind_q_hat",
    "test_blind_abs_q_delta",
    "test_peak_prominence_at_qhat",
    "test_p10_peak_prominence_at_qhat",
    "test_power_support_at_qhat",
    "test_visible_fraction_at_qhat",
    "test_spectral_entropy",
    "beam_width",
    "candidates_scored",
]

VALIDATION_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "fold",
    "subset_size",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "train_q_hat",
    "train_score",
    "train_visible_fraction",
    "test_q_hat_near_train",
    "heldout_q_hat_near_train",
    "selected_test_blind_q_hat",
    "heldout_blind_q_hat",
    "train_test_abs_q_delta",
    "selected_heldout_abs_q_delta",
    "blind_selected_heldout_abs_q_delta",
    "q_agreement_within_tolerance",
    "blind_q_agreement_within_tolerance",
    "test_peak_prominence_at_qhat",
    "test_p10_peak_prominence_at_qhat",
    "test_power_support_at_qhat",
    "test_visible_fraction_at_qhat",
    "test_spectral_entropy",
    "heldout_power_support_at_qhat",
    "heldout_prominence_at_qhat",
    "heldout_visible_fraction_at_qhat",
    "train_channel_count",
    "heldout_channel_count",
    "fit_window_count",
    "test_window_count",
    "fit_end_turn",
    "test_start_turn",
    "beam_width",
    "candidates_scored",
]

SUMMARY_FIELDS = [
    "plane",
    "subset_size",
    "curve_row_count",
    "median_subset_score",
    "median_score_gain_vs_previous_n",
    "median_visible_fraction",
    "median_curve_test_peak_prominence",
    "median_curve_test_power_support",
    "median_curve_test_visible_fraction",
    "validation_row_count",
    "validation_spill_count",
    "median_test_peak_prominence",
    "test_peak_prominence_ci_low",
    "test_peak_prominence_ci_high",
    "median_test_power_support",
    "test_power_support_ci_low",
    "test_power_support_ci_high",
    "median_heldout_power_support",
    "heldout_power_support_ci_low",
    "heldout_power_support_ci_high",
    "median_heldout_prominence",
    "heldout_prominence_ci_low",
    "heldout_prominence_ci_high",
    "median_heldout_visible_fraction",
    "median_test_visible_fraction",
    "q_agreement_rate",
    "q_agreement_ci_low",
    "q_agreement_ci_high",
    "blind_q_agreement_rate",
    "blind_q_agreement_ci_low",
    "blind_q_agreement_ci_high",
    "median_train_test_abs_q_delta",
    "median_selected_heldout_abs_q_delta",
    "median_blind_selected_heldout_abs_q_delta",
    "blind_selected_heldout_abs_q_delta_ci_low",
    "blind_selected_heldout_abs_q_delta_ci_high",
    "median_interfold_abs_q_delta",
    "median_candidates_scored",
    "bootstrap_block_spills",
]

SUMMARY_BY_COLLECTION_FIELDS = ["collection", *SUMMARY_FIELDS]

CROSS_COLLECTION_FIELDS = [
    "train_collection",
    "test_collection",
    "plane",
    "status",
    "selected_n",
    "test_collection_knee_n",
    "test_blind_q_agreement_rate",
    "test_median_blind_selected_heldout_abs_q_delta",
    "test_median_selected_power_support",
    "test_median_heldout_power_support",
    "test_median_selected_prominence",
    "test_median_heldout_prominence",
    "n1_blind_q_agreement_rate",
    "n1_median_blind_selected_heldout_abs_q_delta",
    "blind_agreement_gain_vs_n1",
    "blind_q_delta_reduction_vs_n1",
]

GATE_MARGIN_SENSITIVITY_FIELDS = [
    "plane",
    "blind_agreement_margin",
    "selected_power_fraction",
    "heldout_power_fraction",
    "recommended_n",
    "eligible_n",
    "status",
    "is_declared",
    "maximum_n",
]


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def median(values: Sequence[float]) -> float:
    vals = sorted(value for value in values if math.isfinite(value))
    if not vals:
        return math.nan
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def percentile(values: Sequence[float], fraction: float) -> float:
    vals = sorted(value for value in values if math.isfinite(value))
    if not vals:
        return math.nan
    position = min(1.0, max(0.0, fraction)) * (len(vals) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return vals[lower]
    return vals[lower] + (vals[upper] - vals[lower]) * (position - lower)


def mean(values: Sequence[float]) -> float:
    vals = [value for value in values if math.isfinite(value)]
    return sum(vals) / len(vals) if vals else math.nan


def bootstrap_ci(
    values: Sequence[float],
    samples: int,
    seed: int,
    statistic=median,
) -> tuple[float, float]:
    vals = [value for value in values if math.isfinite(value)]
    if not vals:
        return math.nan, math.nan
    rng = random.Random(seed)
    draws = []
    for _ in range(max(100, samples)):
        draws.append(statistic([rng.choice(vals) for _ in vals]))
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return lo, hi


def block_bootstrap_ci(
    series_by_collection: Mapping[str, Sequence[float]],
    samples: int,
    seed: int,
    block_spills: int,
    statistic=median,
) -> tuple[float, float]:
    """Non-circular moving-block bootstrap within each collection."""
    series = {
        collection: [value for value in values if math.isfinite(value)]
        for collection, values in series_by_collection.items()
    }
    series = {collection: values for collection, values in series.items() if values}
    if not series:
        return math.nan, math.nan
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(max(100, samples)):
        sample: list[float] = []
        for collection in sorted(series):
            values = series[collection]
            sample.extend(moving_block_resample(values, rng, block_spills))
        draws.append(statistic(sample))
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return lo, hi


def fit_window_count(total_windows: int, requested: int) -> int:
    if total_windows <= 1:
        return total_windows
    return max(1, min(int(requested), total_windows - 1))


def purged_window_split(
    centers: np.ndarray,
    requested_fit_windows: int,
    window_turns: int,
) -> tuple[int, int]:
    """Return fit count and first non-overlapping test-window index."""
    if centers.size <= 1:
        return int(centers.size), int(centers.size)
    fit_count = fit_window_count(int(centers.size), requested_fit_windows)
    half_window = float(window_turns) / 2.0
    while fit_count >= 1:
        fit_end = float(centers[fit_count - 1]) + half_window
        test_start = next(
            (
                index
                for index in range(fit_count, int(centers.size))
                if float(centers[index]) - half_window >= fit_end - 1e-6
            ),
            int(centers.size),
        )
        if test_start < int(centers.size) or fit_count == 1:
            return fit_count, test_start
        fit_count -= 1
    return 0, int(centers.size)


def training_candidates(spectra: np.ndarray, tune_axis: np.ndarray) -> np.ndarray:
    """Derive one candidate per BPM using only the fit-window spectra."""
    if spectra.size == 0:
        return np.full(spectra.shape[0], math.nan, dtype=np.float32)
    peak_indices = np.argmax(np.asarray(spectra, dtype=np.float32), axis=2)
    q_windows = tune_axis[peak_indices]
    return np.asarray(np.median(q_windows, axis=1), dtype=np.float32)


def _spectral_entropy(power: np.ndarray) -> np.ndarray:
    clean = np.asarray(power, dtype=np.float64)
    clean = np.where(np.isfinite(clean) & (clean > 0), clean, 0.0)
    total = np.sum(clean, axis=1, keepdims=True)
    fraction = clean / np.maximum(total, 1e-24)
    safe = np.where(fraction > 0, fraction, 1.0)
    entropy = -np.sum(np.where(fraction > 0, fraction * np.log(safe), 0.0), axis=1)
    return entropy / math.log(max(2, clean.shape[1]))


def aggregate_metrics(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    reference_q: float,
    tune_half_width: float,
    aggregator: str = "mean",
) -> dict[str, float]:
    """Evaluate an ensemble on windows that were not used for member selection."""
    if spectra.size == 0 or spectra.shape[1] == 0:
        return {
            "q_hat": math.nan,
            "blind_q_hat": math.nan,
            "peak_prominence": math.nan,
            "p10_peak_prominence": math.nan,
            "power_support": math.nan,
            "visible_fraction": math.nan,
            "spectral_entropy": math.nan,
        }
    clean = np.asarray(spectra, dtype=np.float32)
    if aggregator == "median":
        combined = np.median(clean, axis=0)
    else:
        combined = np.mean(clean, axis=0, dtype=np.float32)
    blind_peak_indices = np.argmax(combined, axis=1)
    blind_q_hat = median([float(value) for value in tune_axis[blind_peak_indices]])
    if math.isfinite(reference_q):
        continuity_width = max(0.01, tune_half_width * 4.0)
        continuity_mask = np.abs(tune_axis - reference_q) <= continuity_width
    else:
        continuity_mask = np.ones(tune_axis.shape, dtype=bool)
    if not np.any(continuity_mask):
        continuity_mask[np.argmin(np.abs(tune_axis - reference_q))] = True
    continuity_indices = np.flatnonzero(continuity_mask)
    local_peak = np.argmax(combined[:, continuity_mask], axis=1)
    peak_indices = continuity_indices[local_peak]
    q_windows = tune_axis[peak_indices]
    q_hat = median([float(value) for value in q_windows])

    qmask = np.abs(tune_axis - reference_q) <= tune_half_width if math.isfinite(reference_q) else continuity_mask
    if not np.any(qmask):
        qmask[np.argmin(np.abs(tune_axis - reference_q))] = True
    background_width = max(0.01, tune_half_width * 5.0)
    background_mask = (np.abs(tune_axis - reference_q) <= background_width) & ~qmask if math.isfinite(reference_q) else ~qmask
    if not np.any(background_mask):
        background_mask = ~qmask
    signal = np.max(combined[:, qmask], axis=1)
    background = combined[:, background_mask]
    background_median = np.median(background, axis=1)
    power_support = signal / np.maximum(background_median, 1e-24)
    log_background = np.log10(background + 1e-24)
    log_signal = np.log10(signal + 1e-24)
    log_median = np.median(log_background, axis=1)
    log_mad = np.median(np.abs(log_background - log_median[:, None]), axis=1) * 1.4826
    prominence = (log_signal - log_median) / np.maximum(log_mad, 1e-9)
    return {
        "q_hat": q_hat,
        "blind_q_hat": blind_q_hat,
        "peak_prominence": median([float(value) for value in prominence]),
        "p10_peak_prominence": percentile([float(value) for value in prominence], 0.10),
        "power_support": median([float(value) for value in power_support]),
        "visible_fraction": float(np.mean(prominence >= 4.0)),
        "spectral_entropy": median([float(value) for value in _spectral_entropy(combined)]),
    }


def cache_rows(root: Path, spectral_config: str) -> list[dict[str, str]]:
    rows = [
        row
        for row in read_csv(root / "cache" / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    ]
    rows.sort(key=lambda row: (row["collection"], row["spill_id"], row["plane"]))
    return rows


def stratified_limit(rows: Sequence[dict[str, str]], limit: int) -> list[dict[str, str]]:
    if limit <= 0 or len(rows) <= limit:
        return list(rows)
    strata: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strata[(row["collection"], row["plane"])].append(row)
    keys = sorted(strata)
    quotas = {
        key: min(len(strata[key]), limit // len(keys) + int(index < limit % len(keys)))
        for index, key in enumerate(keys)
    }
    remaining = limit - sum(quotas.values())
    while remaining > 0:
        progressed = False
        for key in keys:
            if quotas[key] < len(strata[key]):
                quotas[key] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:
            break

    selected: list[dict[str, str]] = []
    for key in keys:
        group = strata[key]
        count = quotas[key]
        if count <= 0:
            continue
        if count == 1:
            selected.append(group[len(group) // 2])
            continue
        indices = [round(index * (len(group) - 1) / (count - 1)) for index in range(count)]
        selected.extend(group[index] for index in indices)
    selected.sort(key=lambda row: (row["collection"], row["spill_id"], row["plane"]))
    return selected


def subset_bitmask(indices: Sequence[int]) -> int:
    mask = 0
    for idx in indices:
        if 0 <= int(idx) < 64:
            mask |= 1 << int(idx)
    return mask


def candidate_expansions(beam_subsets: Sequence[Sequence[int]], item_count: int) -> np.ndarray:
    expanded: set[tuple[int, ...]] = set()
    for subset in beam_subsets:
        members = set(int(idx) for idx in subset)
        for idx in range(item_count):
            if idx not in members:
                expanded.add(tuple(sorted((*members, idx))))
    if not expanded:
        return np.empty((0, 0), dtype=np.int16)
    return np.asarray(sorted(expanded), dtype=np.int16)


def beam_search_curve(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    centers: np.ndarray,
    bpm_indices: np.ndarray,
    candidate_tunes: np.ndarray,
    bpm_meta: Mapping[int, Mapping[str, str]],
    consensus: Mapping[str, str] | None,
    window_turns: int,
    max_n: int,
    beam_width: int,
    chunk_size: int,
    device: str,
) -> list[tuple[object, int]]:
    winners: list[tuple[object, int]] = []
    beam_subsets: list[tuple[int, ...]] = []
    for subset_size in range(1, min(max_n, spectra.shape[0]) + 1):
        if subset_size == 1:
            combos = np.arange(spectra.shape[0], dtype=np.int16)[:, None]
        else:
            combos = candidate_expansions(beam_subsets, spectra.shape[0])
        if combos.size == 0:
            break
        scored = score_combos(
            spectra,
            tune_axis,
            centers,
            bpm_indices,
            candidate_tunes,
            combos,
            dict(bpm_meta),
            dict(consensus) if consensus else None,
            window_turns,
            chunk_size,
            device,
        )
        if not scored:
            break
        winners.append((scored[0], int(combos.shape[0])))
        beam_subsets = [tuple(score.subset) for score in scored[: max(1, beam_width)]]
    return winners


def fold_by_digitizer(
    bpm_indices: Sequence[int],
    bpm_meta: Mapping[int, Mapping[str, str]],
    folds: int,
    seed: int,
) -> dict[int, int]:
    if folds < 2:
        raise ValueError("digitizer-disjoint validation requires at least two folds")
    missing = [int(idx) for idx in bpm_indices if not str(bpm_meta.get(int(idx), {}).get("digitizer", "")).strip()]
    if missing:
        raise ValueError(f"missing digitizer identity for BPM indices: {missing}")
    digitizers = sorted(
        {str(bpm_meta.get(int(idx), {}).get("digitizer", "")) for idx in bpm_indices},
        key=lambda name: stable_seed("best-n-fold", seed, name),
    )
    if len(digitizers) < folds:
        raise ValueError(f"requested {folds} folds but only {len(digitizers)} digitizers are available")
    digitizer_fold = {name: pos % folds for pos, name in enumerate(digitizers)}
    return {int(idx): digitizer_fold[str(bpm_meta.get(int(idx), {}).get("digitizer", ""))] for idx in bpm_indices}


def curve_rows_for_cache(
    cache: dict[str, str],
    cfg: dict[str, object],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
    max_n: int,
    beam_width: int,
    requested_fit_windows: int,
    tune_half_width: float,
    device: str,
) -> list[dict[str, object]]:
    spectra = np.asarray(np.load(cache["spectra_path"], mmap_mode="r"), dtype=np.float32)
    tune_axis = np.asarray(np.load(cache["tune_axis_path"]), dtype=np.float32)
    centers = np.asarray(np.load(cache["window_centers_path"]), dtype=np.float32)
    bpm_indices = np.asarray(np.load(cache["bpm_indices_path"]), dtype=np.int32)
    plane = cache["plane"]
    bpm_meta = metadata_for_bpms(Path(cfg["_manifest_dir"]), plane)
    fit_count, test_start = purged_window_split(centers, requested_fit_windows, int(cache.get("window_turns") or 4096))
    fit_spectra = spectra[:, :fit_count, :]
    test_spectra = spectra[:, test_start:, :]
    fit_centers = centers[:fit_count]
    candidates = training_candidates(fit_spectra, tune_axis)
    search_cfg = cfg.get("subset_search", {}) if isinstance(cfg.get("subset_search"), dict) else {}
    winners = beam_search_curve(
        fit_spectra,
        tune_axis,
        fit_centers,
        bpm_indices,
        candidates,
        bpm_meta,
        None,
        int(cache.get("window_turns") or 4096),
        max_n,
        beam_width,
        int(search_cfg.get("subset_chunk_size", 4096)),
        device,
    )
    rows: list[dict[str, object]] = []
    for score, candidate_count in winners:
        selected_positions = [int(pos) for pos in score.subset]
        actual = [int(bpm_indices[pos]) for pos in selected_positions]
        identities = identity_fields(plane, actual, meta_by_index)
        test = aggregate_metrics(test_spectra[selected_positions], tune_axis, score.q_hat, tune_half_width)
        rows.append(
            {
                "collection": cache["collection"],
                "spill_id": cache["spill_id"],
                "plane": plane,
                "subset_size": len(actual),
                "subset_mask": subset_bitmask(actual),
                **identities,
                "q_hat": _fmt(score.q_hat),
                "subset_score": _fmt(score.subset_score),
                "holdout_support": _fmt(score.holdout_support),
                "peak_quality": _fmt(score.peak_quality),
                "consensus_agreement": _fmt(score.consensus_agreement),
                "window_stability": _fmt(score.window_stability),
                "diversity_score": _fmt(score.diversity_score),
                "ambiguity_penalty": _fmt(score.ambiguity_penalty),
                "visible_fraction": _fmt(score.visible_fraction),
                "visibility_duration_turns": _fmt(score.visibility_duration_turns),
                "fit_window_count": fit_count,
                "test_window_count": test_spectra.shape[1],
                "fit_end_turn": _fmt(float(centers[fit_count - 1]) + int(cache.get("window_turns") or 4096) / 2.0),
                "test_start_turn": _fmt(float(centers[test_start]) - int(cache.get("window_turns") or 4096) / 2.0) if test_start < len(centers) else "",
                "test_q_hat_near_train": _fmt(test["q_hat"]),
                "test_abs_q_delta": _fmt(abs(test["q_hat"] - score.q_hat)),
                "test_blind_q_hat": _fmt(test["blind_q_hat"]),
                "test_blind_abs_q_delta": _fmt(abs(test["blind_q_hat"] - score.q_hat)),
                "test_peak_prominence_at_qhat": _fmt(test["peak_prominence"]),
                "test_p10_peak_prominence_at_qhat": _fmt(test["p10_peak_prominence"]),
                "test_power_support_at_qhat": _fmt(test["power_support"]),
                "test_visible_fraction_at_qhat": _fmt(test["visible_fraction"]),
                "test_spectral_entropy": _fmt(test["spectral_entropy"]),
                "beam_width": beam_width,
                "candidates_scored": candidate_count,
            }
        )
    return rows


def validation_rows_for_cache(
    cache: dict[str, str],
    cfg: dict[str, object],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
    max_n: int,
    beam_width: int,
    folds: int,
    fold_seed: int,
    requested_fit_windows: int,
    tune_half_width: float,
    device: str,
) -> list[dict[str, object]]:
    spectra = np.asarray(np.load(cache["spectra_path"], mmap_mode="r"), dtype=np.float32)
    tune_axis = np.asarray(np.load(cache["tune_axis_path"]), dtype=np.float32)
    centers = np.asarray(np.load(cache["window_centers_path"]), dtype=np.float32)
    bpm_indices = np.asarray(np.load(cache["bpm_indices_path"]), dtype=np.int32)
    plane = cache["plane"]
    bpm_meta = metadata_for_bpms(Path(cfg["_manifest_dir"]), plane)
    fit_count, test_start = purged_window_split(centers, requested_fit_windows, int(cache.get("window_turns") or 4096))
    assigned = fold_by_digitizer(bpm_indices, bpm_meta, folds, fold_seed)
    search_cfg = cfg.get("subset_search", {}) if isinstance(cfg.get("subset_search"), dict) else {}
    rows: list[dict[str, object]] = []
    for fold in range(folds):
        train_positions = [pos for pos, idx in enumerate(bpm_indices) if assigned[int(idx)] != fold]
        heldout_positions = [pos for pos, idx in enumerate(bpm_indices) if assigned[int(idx)] == fold]
        if not train_positions or not heldout_positions:
            continue
        train_fit_spectra = spectra[train_positions, :fit_count, :]
        train_test_spectra = spectra[train_positions, test_start:, :]
        heldout_test_spectra = spectra[heldout_positions, test_start:, :]
        train_indices = bpm_indices[train_positions]
        train_candidates = training_candidates(train_fit_spectra, tune_axis)
        winners = beam_search_curve(
            train_fit_spectra,
            tune_axis,
            centers[:fit_count],
            train_indices,
            train_candidates,
            bpm_meta,
            None,
            int(cache.get("window_turns") or 4096),
            max_n,
            beam_width,
            int(search_cfg.get("subset_chunk_size", 4096)),
            device,
        )
        for score, candidate_count in winners:
            selected_positions = [int(pos) for pos in score.subset]
            selected_test = aggregate_metrics(
                train_test_spectra[selected_positions],
                tune_axis,
                score.q_hat,
                tune_half_width,
                "mean",
            )
            heldout_test = aggregate_metrics(
                heldout_test_spectra,
                tune_axis,
                score.q_hat,
                tune_half_width,
                "median",
            )
            actual = [int(train_indices[pos]) for pos in selected_positions]
            identities = identity_fields(plane, actual, meta_by_index)
            train_test_delta = abs(selected_test["q_hat"] - score.q_hat)
            selected_heldout_delta = abs(selected_test["q_hat"] - heldout_test["q_hat"])
            blind_selected_heldout_delta = abs(selected_test["blind_q_hat"] - heldout_test["blind_q_hat"])
            rows.append(
                {
                    "collection": cache["collection"],
                    "spill_id": cache["spill_id"],
                    "plane": plane,
                    "fold": fold,
                    "subset_size": len(actual),
                    **identities,
                    "train_q_hat": _fmt(score.q_hat),
                    "train_score": _fmt(score.subset_score),
                    "train_visible_fraction": _fmt(score.visible_fraction),
                    "test_q_hat_near_train": _fmt(selected_test["q_hat"]),
                    "heldout_q_hat_near_train": _fmt(heldout_test["q_hat"]),
                    "selected_test_blind_q_hat": _fmt(selected_test["blind_q_hat"]),
                    "heldout_blind_q_hat": _fmt(heldout_test["blind_q_hat"]),
                    "train_test_abs_q_delta": _fmt(train_test_delta),
                    "selected_heldout_abs_q_delta": _fmt(selected_heldout_delta),
                    "blind_selected_heldout_abs_q_delta": _fmt(blind_selected_heldout_delta),
                    "q_agreement_within_tolerance": _fmt(float(selected_heldout_delta <= tune_half_width)) if math.isfinite(selected_heldout_delta) else "",
                    "blind_q_agreement_within_tolerance": _fmt(float(blind_selected_heldout_delta <= tune_half_width)) if math.isfinite(blind_selected_heldout_delta) else "",
                    "test_peak_prominence_at_qhat": _fmt(selected_test["peak_prominence"]),
                    "test_p10_peak_prominence_at_qhat": _fmt(selected_test["p10_peak_prominence"]),
                    "test_power_support_at_qhat": _fmt(selected_test["power_support"]),
                    "test_visible_fraction_at_qhat": _fmt(selected_test["visible_fraction"]),
                    "test_spectral_entropy": _fmt(selected_test["spectral_entropy"]),
                    "heldout_power_support_at_qhat": _fmt(heldout_test["power_support"]),
                    "heldout_prominence_at_qhat": _fmt(heldout_test["peak_prominence"]),
                    "heldout_visible_fraction_at_qhat": _fmt(heldout_test["visible_fraction"]),
                    "train_channel_count": len(train_positions),
                    "heldout_channel_count": len(heldout_positions),
                    "fit_window_count": fit_count,
                    "test_window_count": train_test_spectra.shape[1],
                    "fit_end_turn": _fmt(float(centers[fit_count - 1]) + int(cache.get("window_turns") or 4096) / 2.0),
                    "test_start_turn": _fmt(float(centers[test_start]) - int(cache.get("window_turns") or 4096) / 2.0) if test_start < len(centers) else "",
                    "beam_width": beam_width,
                    "candidates_scored": candidate_count,
                }
            )
    return rows


def interfold_deltas(rows: Sequence[dict[str, object]]) -> dict[tuple[str, int], list[float]]:
    grouped: dict[tuple[str, str, str, int], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["subset_size"]))].append(_f(row.get("test_q_hat_near_train")))
    out: dict[tuple[str, int], list[float]] = defaultdict(list)
    for (_collection, _spill, plane, subset_size), values in grouped.items():
        finite = [value for value in values if math.isfinite(value)]
        for left, right in combinations(finite, 2):
            out[(plane, subset_size)].append(abs(left - right))
    return out


def collapsed_validation_values(
    rows: Sequence[dict[str, object]],
    field: str,
    within_spill_statistic=median,
) -> list[float]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        value = _f(row.get(field))
        if math.isfinite(value):
            grouped[(str(row.get("collection", "")), str(row.get("spill_id", "")))].append(value)
    return [within_spill_statistic(values) for values in grouped.values() if values]


def collapsed_validation_series(
    rows: Sequence[dict[str, object]],
    field: str,
    within_spill_statistic=median,
) -> dict[str, list[float]]:
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        value = _f(row.get(field))
        if math.isfinite(value):
            grouped[(str(row.get("collection", "")), str(row.get("spill_id", "")))].append(value)

    def spill_key(spill_id: str) -> tuple[int, object]:
        try:
            return 0, int(spill_id)
        except ValueError:
            return 1, spill_id

    by_collection: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (collection, spill_id), values in grouped.items():
        if values:
            by_collection[collection].append((spill_id, within_spill_statistic(values)))
    return {
        collection: [value for _spill, value in sorted(values, key=lambda item: spill_key(item[0]))]
        for collection, values in by_collection.items()
    }


def summarize(
    curve_rows: Sequence[dict[str, object]],
    validation_rows: Sequence[dict[str, object]],
    bootstrap_samples: int,
    bootstrap_block_spills: int = 20,
) -> list[dict[str, object]]:
    curve_grouped: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    validation_grouped: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for row in curve_rows:
        curve_grouped[(str(row["plane"]), int(row["subset_size"]))].append(row)
    for row in validation_rows:
        validation_grouped[(str(row["plane"]), int(row["subset_size"]))].append(row)
    fold_delta = interfold_deltas(validation_rows)
    keys = sorted(set(curve_grouped) | set(validation_grouped))
    med_score = {
        key: median([_f(row.get("subset_score")) for row in rows])
        for key, rows in curve_grouped.items()
    }
    out: list[dict[str, object]] = []
    for plane, subset_size in keys:
        curve = curve_grouped.get((plane, subset_size), [])
        valid = validation_grouped.get((plane, subset_size), [])
        prominence_values = collapsed_validation_values(valid, "test_peak_prominence_at_qhat")
        support_values = collapsed_validation_values(valid, "test_power_support_at_qhat")
        heldout_support_values = collapsed_validation_values(valid, "heldout_power_support_at_qhat")
        heldout_prominence_values = collapsed_validation_values(valid, "heldout_prominence_at_qhat")
        heldout_visible_values = collapsed_validation_values(valid, "heldout_visible_fraction_at_qhat")
        agreement_values = collapsed_validation_values(valid, "q_agreement_within_tolerance", mean)
        blind_agreement_values = collapsed_validation_values(valid, "blind_q_agreement_within_tolerance", mean)
        visible_values = collapsed_validation_values(valid, "test_visible_fraction_at_qhat")
        train_test_delta_values = collapsed_validation_values(valid, "train_test_abs_q_delta")
        selected_heldout_delta_values = collapsed_validation_values(valid, "selected_heldout_abs_q_delta")
        blind_selected_heldout_delta_values = collapsed_validation_values(valid, "blind_selected_heldout_abs_q_delta")
        prominence_lo, prominence_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "test_peak_prominence_at_qhat"),
            bootstrap_samples,
            stable_seed("best-n-prominence", plane, subset_size),
            bootstrap_block_spills,
        )
        support_lo, support_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "test_power_support_at_qhat"),
            bootstrap_samples,
            stable_seed("best-n-support", plane, subset_size),
            bootstrap_block_spills,
        )
        heldout_support_lo, heldout_support_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "heldout_power_support_at_qhat"),
            bootstrap_samples,
            stable_seed("best-n-heldout-support", plane, subset_size),
            bootstrap_block_spills,
        )
        heldout_prominence_lo, heldout_prominence_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "heldout_prominence_at_qhat"),
            bootstrap_samples,
            stable_seed("best-n-heldout-prominence", plane, subset_size),
            bootstrap_block_spills,
        )
        agreement_lo, agreement_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "q_agreement_within_tolerance", mean),
            bootstrap_samples,
            stable_seed("best-n-agreement", plane, subset_size),
            bootstrap_block_spills,
            mean,
        )
        blind_agreement_lo, blind_agreement_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "blind_q_agreement_within_tolerance", mean),
            bootstrap_samples,
            stable_seed("best-n-blind-agreement", plane, subset_size),
            bootstrap_block_spills,
            mean,
        )
        blind_delta_lo, blind_delta_hi = block_bootstrap_ci(
            collapsed_validation_series(valid, "blind_selected_heldout_abs_q_delta"),
            bootstrap_samples,
            stable_seed("best-n-blind-q-delta", plane, subset_size),
            bootstrap_block_spills,
        )
        prior_score = med_score.get((plane, subset_size - 1), math.nan)
        current_score = med_score.get((plane, subset_size), math.nan)
        out.append(
            {
                "plane": plane,
                "subset_size": subset_size,
                "curve_row_count": len(curve),
                "median_subset_score": _fmt(current_score),
                "median_score_gain_vs_previous_n": _fmt(current_score - prior_score),
                "median_visible_fraction": _fmt(median([_f(row.get("visible_fraction")) for row in curve])),
                "median_curve_test_peak_prominence": _fmt(median([_f(row.get("test_peak_prominence_at_qhat")) for row in curve])),
                "median_curve_test_power_support": _fmt(median([_f(row.get("test_power_support_at_qhat")) for row in curve])),
                "median_curve_test_visible_fraction": _fmt(median([_f(row.get("test_visible_fraction_at_qhat")) for row in curve])),
                "validation_row_count": len(valid),
                "validation_spill_count": len({(str(row.get("collection", "")), str(row.get("spill_id", ""))) for row in valid}),
                "median_test_peak_prominence": _fmt(median(prominence_values)),
                "test_peak_prominence_ci_low": _fmt(prominence_lo),
                "test_peak_prominence_ci_high": _fmt(prominence_hi),
                "median_test_power_support": _fmt(median(support_values)),
                "test_power_support_ci_low": _fmt(support_lo),
                "test_power_support_ci_high": _fmt(support_hi),
                "median_heldout_power_support": _fmt(median(heldout_support_values)),
                "heldout_power_support_ci_low": _fmt(heldout_support_lo),
                "heldout_power_support_ci_high": _fmt(heldout_support_hi),
                "median_heldout_prominence": _fmt(median(heldout_prominence_values)),
                "heldout_prominence_ci_low": _fmt(heldout_prominence_lo),
                "heldout_prominence_ci_high": _fmt(heldout_prominence_hi),
                "median_heldout_visible_fraction": _fmt(median(heldout_visible_values)),
                "median_test_visible_fraction": _fmt(median(visible_values)),
                "q_agreement_rate": _fmt(mean(agreement_values)),
                "q_agreement_ci_low": _fmt(agreement_lo),
                "q_agreement_ci_high": _fmt(agreement_hi),
                "blind_q_agreement_rate": _fmt(mean(blind_agreement_values)),
                "blind_q_agreement_ci_low": _fmt(blind_agreement_lo),
                "blind_q_agreement_ci_high": _fmt(blind_agreement_hi),
                "median_train_test_abs_q_delta": _fmt(median(train_test_delta_values)),
                "median_selected_heldout_abs_q_delta": _fmt(median(selected_heldout_delta_values)),
                "median_blind_selected_heldout_abs_q_delta": _fmt(median(blind_selected_heldout_delta_values)),
                "blind_selected_heldout_abs_q_delta_ci_low": _fmt(blind_delta_lo),
                "blind_selected_heldout_abs_q_delta_ci_high": _fmt(blind_delta_hi),
                "median_interfold_abs_q_delta": _fmt(median(fold_delta.get((plane, subset_size), []))),
                "median_candidates_scored": _fmt(median([_f(row.get("candidates_scored")) for row in curve or valid])),
                "bootstrap_block_spills": bootstrap_block_spills,
            }
        )
    return out


def recommendation_gate_status(
    summary_rows: Sequence[dict[str, object]],
    plane: str,
    tune_half_width: float,
    *,
    blind_agreement_margin: float = 0.02,
    selected_power_fraction: float = 0.95,
    heldout_power_fraction: float = 0.90,
) -> tuple[list[dict[str, object]], dict[str, float], str]:
    if blind_agreement_margin < 0:
        raise ValueError("blind agreement margin must be nonnegative")
    if not 0 < selected_power_fraction <= 1 or not 0 < heldout_power_fraction <= 1:
        raise ValueError("power non-inferiority fractions must be in (0, 1]")
    rows = [
        row
        for row in summary_rows
        if row.get("plane") == plane and int(row.get("validation_row_count") or 0) > 0
    ]
    if not rows:
        return [], {}, "no validation rows"
    agreement_candidates = [_f(row.get("blind_q_agreement_rate")) for row in rows]
    q_delta_candidates = [_f(row.get("median_blind_selected_heldout_abs_q_delta")) for row in rows]
    agreement_candidates = [value for value in agreement_candidates if math.isfinite(value)]
    q_delta_candidates = [value for value in q_delta_candidates if math.isfinite(value)]
    if not agreement_candidates or not q_delta_candidates:
        return [], {}, "validation metrics are incomplete"
    best_agreement = max(agreement_candidates)
    best_q_delta = min(q_delta_candidates)
    stable = [
        row
        for row in rows
        if _f(row.get("blind_q_agreement_rate")) >= best_agreement - blind_agreement_margin
        and _f(row.get("median_blind_selected_heldout_abs_q_delta")) <= best_q_delta + tune_half_width
    ]
    if not stable:
        return [], {}, "no N met the declared tune-reproducibility thresholds"

    contrast_fields = (
        "median_test_peak_prominence",
        "median_test_power_support",
        "median_heldout_prominence",
        "median_heldout_power_support",
    )
    if any(not any(math.isfinite(_f(row.get(field))) for row in stable) for field in contrast_fields):
        return [], {}, "reproducible N values have incomplete selected/held-out contrast metrics"
    best_selected_prominence = max(_f(row.get("median_test_peak_prominence")) for row in stable)
    best_selected_support = max(_f(row.get("median_test_power_support")) for row in stable)
    best_heldout_prominence = max(_f(row.get("median_heldout_prominence")) for row in stable)
    best_heldout_support = max(_f(row.get("median_heldout_power_support")) for row in stable)
    selected_prominence_margin = max(0.25, 0.05 * abs(best_selected_prominence))
    heldout_prominence_margin = max(0.50, 0.10 * abs(best_heldout_prominence))
    statuses: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda item: int(item["subset_size"])):
        gates = {
            "blind_agreement": _f(row.get("blind_q_agreement_rate"))
            >= best_agreement - blind_agreement_margin,
            "blind_q_delta": _f(row.get("median_blind_selected_heldout_abs_q_delta"))
            <= best_q_delta + tune_half_width,
            "selected_prominence": _f(row.get("median_test_peak_prominence"))
            >= best_selected_prominence - selected_prominence_margin,
            "selected_power": _f(row.get("median_test_power_support"))
            >= selected_power_fraction * best_selected_support,
            "heldout_prominence": _f(row.get("median_heldout_prominence"))
            >= best_heldout_prominence - heldout_prominence_margin,
            "heldout_power": _f(row.get("median_heldout_power_support"))
            >= heldout_power_fraction * best_heldout_support,
        }
        statuses.append(
            {
                "row": row,
                "subset_size": int(row["subset_size"]),
                **gates,
                "all_gates": all(gates.values()),
            }
        )
    context = {
        "best_agreement": best_agreement,
        "best_q_delta": best_q_delta,
        "selected_prominence_margin": selected_prominence_margin,
        "heldout_prominence_margin": heldout_prominence_margin,
        "blind_agreement_margin": blind_agreement_margin,
        "selected_power_fraction": selected_power_fraction,
        "heldout_power_fraction": heldout_power_fraction,
    }
    return statuses, context, ""


def recommended_n(
    summary_rows: Sequence[dict[str, object]],
    plane: str,
    tune_half_width: float,
) -> tuple[dict[str, object] | None, str]:
    statuses, context, reason = recommendation_gate_status(summary_rows, plane, tune_half_width)
    if reason:
        return None, reason
    eligible = [status["row"] for status in statuses if status["all_gates"]]
    if not eligible:
        return None, "reproducible N values have an unresolved selected/held-out contrast tradeoff"
    chosen = min(eligible, key=lambda row: int(row["subset_size"]))
    if len(statuses) >= 10:
        larger = [status for status in statuses if int(status["subset_size"]) > int(chosen["subset_size"])]
        if len(larger) < 3:
            return None, (
                f"the provisional knee Best-{chosen['subset_size']} is boundary-limited; "
                "evaluate at least three larger contiguous N values"
            )
    rationale = (
        f"blind agreement is within {context['blind_agreement_margin']:.3g} of the best and blind channel-disjoint delta is within "
        f"{tune_half_width:.4g} of the minimum; selected later-window power is within 5% and "
        f"held-out power is within 10% of their best reproducible values; selected and held-out "
        f"prominence are within {context['selected_prominence_margin']:.3g} and "
        f"{context['heldout_prominence_margin']:.3g}, respectively"
    )
    return chosen, rationale


def recommendation_margin_sensitivity(
    summary_rows: Sequence[dict[str, object]],
    plane: str,
    tune_half_width: float,
    agreement_margins: Sequence[float] = (0.01, 0.02, 0.03),
    selected_power_fractions: Sequence[float] = (0.90, 0.95, 0.98),
    heldout_power_fractions: Sequence[float] = (0.85, 0.90, 0.95),
) -> list[dict[str, object]]:
    plane_rows = [row for row in summary_rows if row.get("plane") == plane]
    maximum_n = max((int(row.get("subset_size") or 0) for row in plane_rows), default=0)
    out: list[dict[str, object]] = []
    for agreement_margin in agreement_margins:
        for selected_power_fraction in selected_power_fractions:
            for heldout_power_fraction in heldout_power_fractions:
                statuses, _context, reason = recommendation_gate_status(
                    summary_rows,
                    plane,
                    tune_half_width,
                    blind_agreement_margin=agreement_margin,
                    selected_power_fraction=selected_power_fraction,
                    heldout_power_fraction=heldout_power_fraction,
                )
                eligible = [
                    int(status["subset_size"])
                    for status in statuses
                    if bool(status.get("all_gates"))
                ]
                recommended = min(eligible) if eligible else None
                out.append(
                    {
                        "plane": plane,
                        "blind_agreement_margin": _fmt(float(agreement_margin)),
                        "selected_power_fraction": _fmt(float(selected_power_fraction)),
                        "heldout_power_fraction": _fmt(float(heldout_power_fraction)),
                        "recommended_n": recommended if recommended is not None else "",
                        "eligible_n": ",".join(str(value) for value in eligible),
                        "status": "OK" if recommended is not None else reason or "UNRESOLVED_TRADEOFF",
                        "is_declared": (
                            "true"
                            if math.isclose(agreement_margin, 0.02)
                            and math.isclose(selected_power_fraction, 0.95)
                            and math.isclose(heldout_power_fraction, 0.90)
                            else "false"
                        ),
                        "maximum_n": maximum_n,
                    }
                )
    return out


def recommendation_text(summary_rows: Sequence[dict[str, object]], tune_half_width: float) -> str:
    lines = [
        "# Best-N Validation Summary",
        "",
        "The recommendation uses windows after member selection and a disjoint-digitizer reference; it does not use the adaptive training score alone.",
        "Channels sharing a digitizer stay in one fold. Candidate tunes are derived only from fit windows, and selected members are evaluated only on later windows.",
        "Blind agreement searches the full configured tune band on both selected and held-out channels. Conditioned agreement and power support are evaluated near the training tune and are reported separately.",
        "Confidence intervals use a moving-block bootstrap within each capture collection so adjacent spills are not treated as fully independent.",
        "",
    ]
    for plane in ("H", "V"):
        chosen, rationale = recommended_n(summary_rows, plane, tune_half_width)
        if chosen is None:
            lines.append(f"- {plane}: {rationale}; no automatic recommendation.")
        else:
            lines.append(
                f"- {plane}: provisional non-inferiority knee Best-{chosen['subset_size']}; {rationale}."
            )
    lines.extend(
        [
            "",
            "The margins are declared engineering non-inferiority thresholds, not a physical prior. The full curves and confidence intervals remain the primary evidence.",
            "This is a BPM-only model-selection result. It chooses ensemble size for internal spectral reproducibility; it does not establish absolute tune accuracy.",
        ]
    )
    return "\n".join(lines) + "\n"


def summarize_by_collection(
    curve_rows: Sequence[dict[str, object]],
    validation_rows: Sequence[dict[str, object]],
    bootstrap_samples: int,
    bootstrap_block_spills: int,
) -> list[dict[str, object]]:
    collections = sorted(
        {str(row.get("collection", "")) for row in (*curve_rows, *validation_rows) if row.get("collection")}
    )
    out: list[dict[str, object]] = []
    for collection in collections:
        summary = summarize(
            [row for row in curve_rows if str(row.get("collection", "")) == collection],
            [row for row in validation_rows if str(row.get("collection", "")) == collection],
            bootstrap_samples,
            bootstrap_block_spills,
        )
        out.extend({"collection": collection, **row} for row in summary)
    return out


def cross_collection_transfer(
    summary_rows: Sequence[dict[str, object]],
    tune_half_width: float,
) -> list[dict[str, object]]:
    by_collection: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in summary_rows:
        by_collection[str(row.get("collection", ""))].append(row)
    collections = sorted(collection for collection in by_collection if collection)
    out: list[dict[str, object]] = []
    for train_collection in collections:
        for test_collection in collections:
            if train_collection == test_collection:
                continue
            for plane in ("H", "V"):
                chosen, reason = recommended_n(by_collection[train_collection], plane, tune_half_width)
                test_knee, _test_reason = recommended_n(by_collection[test_collection], plane, tune_half_width)
                selected_n = int(chosen["subset_size"]) if chosen is not None else None
                test_row = next(
                    (
                        row
                        for row in by_collection[test_collection]
                        if row.get("plane") == plane and selected_n is not None and int(row.get("subset_size") or 0) == selected_n
                    ),
                    None,
                )
                n1_row = next(
                    (
                        row
                        for row in by_collection[test_collection]
                        if row.get("plane") == plane and int(row.get("subset_size") or 0) == 1
                    ),
                    None,
                )
                status = "OK"
                if chosen is None:
                    status = f"NO_TRAIN_RECOMMENDATION: {reason}"
                elif test_row is None:
                    status = "SELECTED_N_NOT_AVAILABLE_IN_TEST_COLLECTION"
                test_agreement = _f(test_row.get("blind_q_agreement_rate")) if test_row else math.nan
                test_delta = _f(test_row.get("median_blind_selected_heldout_abs_q_delta")) if test_row else math.nan
                n1_agreement = _f(n1_row.get("blind_q_agreement_rate")) if n1_row else math.nan
                n1_delta = _f(n1_row.get("median_blind_selected_heldout_abs_q_delta")) if n1_row else math.nan
                out.append(
                    {
                        "train_collection": train_collection,
                        "test_collection": test_collection,
                        "plane": plane,
                        "status": status,
                        "selected_n": selected_n if selected_n is not None else "",
                        "test_collection_knee_n": int(test_knee["subset_size"]) if test_knee is not None else "",
                        "test_blind_q_agreement_rate": _fmt(test_agreement),
                        "test_median_blind_selected_heldout_abs_q_delta": _fmt(test_delta),
                        "test_median_selected_power_support": _fmt(_f(test_row.get("median_test_power_support"))) if test_row else "",
                        "test_median_heldout_power_support": _fmt(_f(test_row.get("median_heldout_power_support"))) if test_row else "",
                        "test_median_selected_prominence": _fmt(_f(test_row.get("median_test_peak_prominence"))) if test_row else "",
                        "test_median_heldout_prominence": _fmt(_f(test_row.get("median_heldout_prominence"))) if test_row else "",
                        "n1_blind_q_agreement_rate": _fmt(n1_agreement),
                        "n1_median_blind_selected_heldout_abs_q_delta": _fmt(n1_delta),
                        "blind_agreement_gain_vs_n1": _fmt(test_agreement - n1_agreement),
                        "blind_q_delta_reduction_vs_n1": _fmt(n1_delta - test_delta),
                    }
                )
    return out


def cross_collection_text(rows: Sequence[dict[str, object]]) -> str:
    lines = [
        "# Cross-Collection Best-N Transfer",
        "",
        "Each row chooses N using one capture collection and reports that same N in the other collection. Per-spill members remain adaptive; only the global ensemble-size choice is transferred.",
        "",
    ]
    if not rows:
        lines.append("A cross-collection transfer check requires at least two collections.")
    for row in rows:
        if row.get("status") != "OK":
            lines.append(
                f"- {row.get('train_collection')} -> {row.get('test_collection')} {row.get('plane')}: {row.get('status')}."
            )
            continue
        lines.append(
            f"- {row.get('train_collection')} -> {row.get('test_collection')} {row.get('plane')}: "
            f"train-selected Best-{row.get('selected_n')}; test blind agreement {row.get('test_blind_q_agreement_rate')}, "
            f"blind median |delta q| {row.get('test_median_blind_selected_heldout_abs_q_delta')}, "
            f"agreement gain versus Best-1 {row.get('blind_agreement_gain_vs_n1')}."
        )
    lines.extend(
        [
            "",
            "This check guards the global N recommendation against collection-specific tuning. It does not provide an external tune reference.",
        ]
    )
    return "\n".join(lines) + "\n"


def _curve_plot_with_intervals(
    poster,
    path: Path,
    title: str,
    rows: Sequence[dict[str, object]],
    series: Sequence[tuple[str, str, str | None, str | None, tuple[int, int, int]]],
    y_label: str,
    knee_n: int | None,
    y_range: tuple[float, float] | None = None,
    reference_line: tuple[float, str] | None = None,
    note: str = "",
) -> None:
    clean_rows = sorted(rows, key=lambda row: int(row["subset_size"]))
    x_values = [float(row["subset_size"]) for row in clean_rows]
    plotted_values = [
        _f(row.get(field))
        for row in clean_rows
        for _label, center, low, high, _color in series
        for field in (center, low, high)
        if field
    ]
    plotted_values = [value for value in plotted_values if math.isfinite(value)]
    if not x_values or not plotted_values:
        poster.no_data_png(path, title)
        return
    xmin, xmax = min(x_values), max(x_values)
    if y_range is None:
        ymin, ymax = min(plotted_values), max(plotted_values)
        if reference_line is not None:
            ymin = min(ymin, reference_line[0])
            ymax = max(ymax, reference_line[0])
        pad = (ymax - ymin) * 0.10 or max(0.01, abs(ymax) * 0.10)
        ymin -= pad
        ymax += pad
        if reference_line is not None and min(plotted_values) >= 0:
            ymin = 0.0
        elif ymin > 0 and ymin < 0.15 * ymax:
            ymin = 0.0
    else:
        ymin, ymax = y_range

    width, height = 1400, 800
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "SUBSET SIZE N", y_label)
    if reference_line is not None and ymin <= reference_line[0] <= ymax:
        y = poster.scale_value(reference_line[0], ymin, ymax, y1, y0)
        poster.line(pixels, width, height, x0, y, x1, y, poster.RED)
    if knee_n is not None and xmin <= knee_n <= xmax:
        x = poster.scale_value(float(knee_n), xmin, xmax, x0, x1)
        for y in range(y0, y1 + 1, 8):
            poster.line(pixels, width, height, x, y, x, min(y + 4, y1), poster.INK)

    legend_x = x1 - 250
    legend_y = y0 + 14
    has_intervals = False
    for series_index, (label, center_field, low_field, high_field, color) in enumerate(series):
        points: list[tuple[float, float]] = []
        for row in clean_rows:
            x = float(row["subset_size"])
            center = _f(row.get(center_field))
            if not math.isfinite(center):
                continue
            px = poster.scale_value(x, xmin, xmax, x0, x1)
            py = poster.scale_value(center, ymin, ymax, y1, y0)
            low = _f(row.get(low_field)) if low_field else math.nan
            high = _f(row.get(high_field)) if high_field else math.nan
            if math.isfinite(low) and math.isfinite(high):
                has_intervals = True
                low_y = poster.scale_value(low, ymin, ymax, y1, y0)
                high_y = poster.scale_value(high, ymin, ymax, y1, y0)
                poster.line(pixels, width, height, px, low_y, px, high_y, color)
                poster.line(pixels, width, height, px - 4, low_y, px + 4, low_y, color)
                poster.line(pixels, width, height, px - 4, high_y, px + 4, high_y, color)
            poster.rect(pixels, width, height, px - 3, py - 3, px + 3, py + 3, color)
            points.append((x, center))
        for (xa, ya), (xb, yb) in zip(points, points[1:]):
            ax = poster.scale_value(xa, xmin, xmax, x0, x1)
            ay = poster.scale_value(ya, ymin, ymax, y1, y0)
            bx = poster.scale_value(xb, xmin, xmax, x0, x1)
            by = poster.scale_value(yb, ymin, ymax, y1, y0)
            poster.line(pixels, width, height, ax, ay, bx, by, color)
            poster.line(pixels, width, height, ax, ay + 1, bx, by + 1, color)
        poster.rect(
            pixels,
            width,
            height,
            legend_x,
            legend_y + series_index * 22,
            legend_x + 14,
            legend_y + 12 + series_index * 22,
            color,
        )
        poster.draw_text(pixels, width, height, legend_x + 22, legend_y + series_index * 22, label[:20], poster.MUTED, 2)

    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (xmin, xmax),
        (ymin, ymax),
        x_ticks=2,
    )
    tick_indices = sorted(
        index
        for index in {round(position * (len(x_values) - 1) / 5) for position in range(1, 5)}
        if 0 < index < len(x_values) - 1
    )
    for index in tick_indices:
        value = x_values[index]
        label = str(int(value))
        px = poster.scale_value(value, xmin, xmax, x0, x1)
        poster.draw_text(pixels, width, height, px - len(label) * 4, y1 + 8, label, poster.MUTED, 2)
    notes = []
    if has_intervals:
        notes.append("95 PCT BLOCK CI")
    if knee_n is not None:
        notes.append(f"DASH: KNEE N {knee_n}")
    if reference_line is not None:
        notes.append(f"RED: {reference_line[1]} {reference_line[0]:.4g}")
    if note:
        notes.append(note)
    if notes:
        poster.draw_text(pixels, width, height, x0, y0 - 28, "; ".join(notes)[:78], poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def _decision_gate_plot(
    poster,
    path: Path,
    plane: str,
    statuses: Sequence[dict[str, object]],
    knee_n: int | None,
) -> None:
    if not statuses:
        poster.no_data_png(path, f"BEST-N DECISION GATES {plane}")
        return
    criteria = (
        ("BLIND AGREEMENT", "blind_agreement"),
        ("BLIND Q DELTA", "blind_q_delta"),
        ("SELECTED PROM", "selected_prominence"),
        ("SELECTED POWER", "selected_power"),
        ("HELDOUT PROM", "heldout_prominence"),
        ("HELDOUT POWER", "heldout_power"),
        ("ALL GATES", "all_gates"),
    )
    width, height = 1400, 640
    pixels = poster.new_canvas(width, height)
    poster.draw_text(pixels, width, height, 34, 28, f"BEST-N DECISION GATES {plane}", poster.INK, 3)
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        68,
        "GREEN PASS  GRAY WITH RED MARK FAIL  BLUE ALL-GATE PASS  DASH SELECTED N",
        poster.MUTED,
        2,
    )
    x0, y0, x1, y1 = 250, 115, width - 45, height - 85
    row_height = (y1 - y0 + 1) // len(criteria)
    column_count = len(statuses)
    for row_index, (label, field) in enumerate(criteria):
        cy0 = y0 + row_index * row_height
        cy1 = y1 if row_index == len(criteria) - 1 else cy0 + row_height - 1
        background = (238, 241, 243) if row_index % 2 == 0 else (245, 247, 248)
        poster.rect(pixels, width, height, x0, cy0, x1, cy1, background)
        poster.draw_text(pixels, width, height, 24, cy0 + max(4, row_height // 2 - 7), label, poster.MUTED, 2)
        for column_index, status in enumerate(statuses):
            cx0 = x0 + round(column_index * (x1 - x0 + 1) / column_count)
            cx1 = x0 + round((column_index + 1) * (x1 - x0 + 1) / column_count) - 1
            passed = bool(status.get(field))
            if passed:
                color = poster.BLUE if field == "all_gates" else poster.GREEN
                poster.rect(pixels, width, height, cx0 + 1, cy0 + 2, cx1 - 1, cy1 - 2, color)
            else:
                marker_y = (cy0 + cy1) // 2
                poster.line(pixels, width, height, cx0 + 4, marker_y, cx1 - 4, marker_y, poster.RED)
        poster.line(pixels, width, height, x0, cy1, x1, cy1, poster.GRID)
    poster.line(pixels, width, height, x0, y0, x1, y0, poster.INK)
    poster.line(pixels, width, height, x0, y1, x1, y1, poster.INK)
    poster.line(pixels, width, height, x0, y0, x0, y1, poster.INK)
    poster.line(pixels, width, height, x1, y0, x1, y1, poster.INK)
    sizes = [int(status["subset_size"]) for status in statuses]
    tick_sizes = sorted({sizes[0], sizes[-1], *[size for size in sizes if size % 5 == 0]})
    for size in tick_sizes:
        index = sizes.index(size)
        center = x0 + round((index + 0.5) * (x1 - x0 + 1) / column_count)
        label = str(size)
        poster.draw_text(pixels, width, height, center - len(label) * 4, y1 + 10, label, poster.MUTED, 2)
    if knee_n is not None and knee_n in sizes:
        index = sizes.index(knee_n)
        center = x0 + round((index + 0.5) * (x1 - x0 + 1) / column_count)
        for y in range(y0, y1 + 1, 8):
            poster.line(pixels, width, height, center, y, center, min(y + 4, y1), poster.INK)
    poster.draw_text(pixels, width, height, (x0 + x1) // 2 - 55, height - 45, "SUBSET SIZE N", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def _gate_margin_sensitivity_plot(
    poster,
    path: Path,
    plane: str,
    rows: Sequence[dict[str, object]],
) -> None:
    if not rows:
        poster.no_data_png(path, f"BEST-N GATE MARGIN SENSITIVITY {plane}")
        return
    agreement_margins = sorted({_f(row.get("blind_agreement_margin")) for row in rows})
    power_pairs = sorted(
        {
            (
                _f(row.get("selected_power_fraction")),
                _f(row.get("heldout_power_fraction")),
            )
            for row in rows
        }
    )
    keyed = {
        (
            _f(row.get("blind_agreement_margin")),
            _f(row.get("selected_power_fraction")),
            _f(row.get("heldout_power_fraction")),
        ): row
        for row in rows
    }
    maximum_n = max((int(row.get("maximum_n") or 0) for row in rows), default=1)
    color_maximum_n = max(2, maximum_n)
    width, height = 1400, 720
    pixels = poster.new_canvas(width, height)
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        28,
        f"BEST-N GATE MARGIN SENSITIVITY {plane}",
        poster.INK,
        3,
    )
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        68,
        "POST-SELECTION ROBUSTNESS ONLY; CELL IS EARLIEST ALL-GATE N; GRAY UNRESOLVED",
        poster.MUTED,
        2,
    )
    x0, y0, x1, y1 = 250, 175, width - 45, height - 105
    column_count = len(power_pairs)
    row_count = len(agreement_margins)
    poster.draw_text(
        pixels,
        width,
        height,
        x0,
        112,
        "SELECTED/HELDOUT POWER FLOOR",
        poster.MUTED,
        2,
    )
    for column_index, (selected_fraction, heldout_fraction) in enumerate(power_pairs):
        cx0 = x0 + round(column_index * (x1 - x0 + 1) / column_count)
        cx1 = x0 + round((column_index + 1) * (x1 - x0 + 1) / column_count) - 1
        label = f"{selected_fraction * 100:.0f}/{heldout_fraction * 100:.0f}"
        center = (cx0 + cx1) // 2
        poster.draw_text(pixels, width, height, center - len(label) * 4, 142, label, poster.MUTED, 2)
    for row_index, agreement_margin in enumerate(agreement_margins):
        cy0 = y0 + round(row_index * (y1 - y0 + 1) / row_count)
        cy1 = y0 + round((row_index + 1) * (y1 - y0 + 1) / row_count) - 1
        row_label = f"AGREE {agreement_margin:.2f}"
        poster.draw_text(
            pixels,
            width,
            height,
            30,
            (cy0 + cy1) // 2 - 7,
            row_label,
            poster.MUTED,
            2,
        )
        for column_index, (selected_fraction, heldout_fraction) in enumerate(power_pairs):
            cx0 = x0 + round(column_index * (x1 - x0 + 1) / column_count)
            cx1 = x0 + round((column_index + 1) * (x1 - x0 + 1) / column_count) - 1
            row = keyed[(agreement_margin, selected_fraction, heldout_fraction)]
            recommended = int(row.get("recommended_n") or 0)
            if recommended > 0:
                color = poster.tune_color(float(recommended), 1.0, float(color_maximum_n))
                poster.rect(pixels, width, height, cx0 + 2, cy0 + 2, cx1 - 2, cy1 - 2, color)
                label = f"N{recommended}"
                poster.draw_text(
                    pixels,
                    width,
                    height,
                    (cx0 + cx1) // 2 - len(label) * 6,
                    (cy0 + cy1) // 2 - 11,
                    label,
                    poster.INK,
                    3,
                )
            else:
                poster.rect(
                    pixels,
                    width,
                    height,
                    cx0 + 2,
                    cy0 + 2,
                    cx1 - 2,
                    cy1 - 2,
                    (232, 235, 238),
                )
                poster.line(pixels, width, height, cx0 + 12, cy0 + 12, cx1 - 12, cy1 - 12, poster.RED)
                poster.line(pixels, width, height, cx0 + 12, cy1 - 12, cx1 - 12, cy0 + 12, poster.RED)
            if str(row.get("is_declared", "")).lower() == "true":
                for inset in (0, 1, 2):
                    poster.line(pixels, width, height, cx0 + inset, cy0 + inset, cx1 - inset, cy0 + inset, poster.INK)
                    poster.line(pixels, width, height, cx0 + inset, cy1 - inset, cx1 - inset, cy1 - inset, poster.INK)
                    poster.line(pixels, width, height, cx0 + inset, cy0 + inset, cx0 + inset, cy1 - inset, poster.INK)
                    poster.line(pixels, width, height, cx1 - inset, cy0 + inset, cx1 - inset, cy1 - inset, poster.INK)
        poster.line(pixels, width, height, x0, cy1, x1, cy1, poster.GRID)
    poster.line(pixels, width, height, x0, y0, x1, y0, poster.INK)
    poster.line(pixels, width, height, x0, y1, x1, y1, poster.INK)
    poster.line(pixels, width, height, x0, y0, x0, y1, poster.INK)
    poster.line(pixels, width, height, x1, y0, x1, y1, poster.INK)
    poster.draw_text(
        pixels,
        width,
        height,
        x0,
        height - 60,
        "BLACK BOX: DECLARED 0.02 AGREEMENT, 95/90 POWER FLOORS",
        poster.MUTED,
        2,
    )
    poster.write_png(path, width, height, pixels)


def write_plots(
    summary_rows: Sequence[dict[str, object]],
    out: Path,
    tune_half_width: float,
) -> None:
    try:
        import bpm_dgx_poster as poster
    except Exception:
        return
    out.mkdir(parents=True, exist_ok=True)
    margin_rows = [
        row
        for plane in ("H", "V")
        for row in recommendation_margin_sensitivity(
            summary_rows,
            plane,
            tune_half_width,
        )
    ]
    write_csv(
        out / "best_n_gate_margin_sensitivity.csv",
        margin_rows,
        GATE_MARGIN_SENSITIVITY_FIELDS,
    )
    blind_upper_values = [
        _f(row.get(field))
        for row in summary_rows
        for field in ("blind_q_agreement_rate", "blind_q_agreement_ci_high")
    ]
    blind_upper_values = [value for value in blind_upper_values if math.isfinite(value)]
    blind_upper = max(blind_upper_values, default=0.0)
    blind_ymax = max(0.4, math.ceil(blind_upper * 1.1 * 10.0) / 10.0)
    for plane in ("H", "V"):
        rows = sorted([row for row in summary_rows if row.get("plane") == plane], key=lambda row: int(row["subset_size"]))
        chosen, _reason = recommended_n(summary_rows, plane, tune_half_width)
        knee_n = int(chosen["subset_size"]) if chosen is not None else None
        gate_statuses, _gate_context, gate_reason = recommendation_gate_status(
            summary_rows,
            plane,
            tune_half_width,
        )
        n1_row = next((row for row in rows if int(row["subset_size"]) == 1), None)
        knee_row = next((row for row in rows if int(row["subset_size"]) == knee_n), None) if knee_n else None
        blind_note = ""
        if n1_row is not None and knee_row is not None:
            n1_agreement = _f(n1_row.get("blind_q_agreement_rate"))
            knee_agreement = _f(knee_row.get("blind_q_agreement_rate"))
            if math.isfinite(n1_agreement) and math.isfinite(knee_agreement):
                agreement_change = (knee_agreement - n1_agreement) * 100
                change_label = "GAIN" if agreement_change >= 0 else "LOSS"
                blind_note = (
                    f"N1 {n1_agreement:.3f} TO N{knee_n} {knee_agreement:.3f} "
                    f"({change_label} {abs(agreement_change):.1f} PTS)"
                )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_validation_{plane.lower()}.png",
            f"BLIND HELD-OUT AGREEMENT {plane}",
            rows,
            [
                ("SELECTED VS HELD OUT", "blind_q_agreement_rate", "blind_q_agreement_ci_low", "blind_q_agreement_ci_high", poster.BLUE),
            ],
            "AGREE RATE",
            knee_n,
            y_range=(0.0, blind_ymax),
            note=blind_note,
        )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_conditioned_agreement_{plane.lower()}.png",
            f"CONDITIONED NEAR-TRAIN AGREEMENT {plane}",
            rows,
            [
                ("NEAR TRAIN Q", "q_agreement_rate", "q_agreement_ci_low", "q_agreement_ci_high", poster.ORANGE),
            ],
            "AGREE RATE",
            knee_n,
            y_range=(0.0, 1.0),
        )
        gate_path = out / f"best_n_decision_gates_{plane.lower()}.png"
        _decision_gate_plot(poster, gate_path, plane, gate_statuses, knee_n)
        eligible_sizes = [
            int(status["subset_size"])
            for status in gate_statuses
            if bool(status.get("all_gates"))
        ]
        gate_detail = (
            f"Eligible N values: {', '.join(str(size) for size in eligible_sizes)}. "
            f"The earliest eligible value is the declared knee Best-{knee_n}."
            if knee_n is not None
            else (
                f"Gate-eligible N values {', '.join(str(size) for size in eligible_sizes)} do not yield a declared knee: "
                f"{gate_reason or _reason}."
                if eligible_sizes
                else f"No all-gate N is available: {gate_reason or _reason}."
            )
        )
        atomic_write_text(
            gate_path.with_name(f"{gate_path.stem}_caption.md"),
            f"# {plane} Best-N Decision Gates\n\n"
            "Each column is one contiguous ensemble size. Green cells pass the exact declared "
            "non-inferiority criterion; gray cells with a red mark fail. The blue final row "
            "passes only when blind agreement, blind channel-disjoint tune difference, selected "
            "power/prominence, and held-out power/prominence all pass. "
            f"{gate_detail} This is an internal reproducibility decision, not an external tune calibration.\n",
        )
        plane_margin_rows = [row for row in margin_rows if row.get("plane") == plane]
        margin_path = out / f"best_n_gate_margin_sensitivity_{plane.lower()}.png"
        _gate_margin_sensitivity_plot(poster, margin_path, plane, plane_margin_rows)
        outcome_counts = Counter(
            int(row["recommended_n"]) if row.get("recommended_n") not in (None, "") else None
            for row in plane_margin_rows
        )
        declared_row = next(
            row for row in plane_margin_rows if str(row.get("is_declared", "")).lower() == "true"
        )
        outcomes = ", ".join(
            f"{'unresolved' if value is None else f'Best-{value}'} in {count}/{len(plane_margin_rows)} cells"
            for value, count in sorted(
                outcome_counts.items(),
                key=lambda item: (-1 if item[0] is None else item[0]),
            )
        )
        atomic_write_text(
            margin_path.with_name(f"{margin_path.stem}_caption.md"),
            f"# {plane} Best-N Gate-Margin Sensitivity\n\n"
            "This post-selection diagnostic reruns the same earliest-all-gates rule over blind-agreement "
            "margins 0.01, 0.02, and 0.03; selected-channel power floors 90%, 95%, and 98%; and "
            "held-out-channel power floors 85%, 90%, and 95%. Prominence and tune-difference rules "
            "remain unchanged. The black cell is the declared 0.02/95%/90% protocol and selects "
            f"Best-{declared_row.get('recommended_n') or 'unresolved'}. Across the grid: {outcomes}. "
            "This characterizes criterion sensitivity after the declared analysis; it does not replace "
            "the published knee or provide external tune calibration.\n",
        )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_heldout_power_{plane.lower()}.png",
            f"BEST-N LATER POWER {plane}",
            rows,
            [
                ("SELECTED", "median_test_power_support", "test_power_support_ci_low", "test_power_support_ci_high", poster.ORANGE),
                ("HELD OUT DIGITIZERS", "median_heldout_power_support", "heldout_power_support_ci_low", "heldout_power_support_ci_high", poster.BLUE),
            ],
            "POWER RATIO",
            knee_n,
        )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_test_prominence_{plane.lower()}.png",
            f"BEST-N LATER PROMINENCE {plane}",
            rows,
            [
                ("SELECTED", "median_test_peak_prominence", "test_peak_prominence_ci_low", "test_peak_prominence_ci_high", poster.GREEN),
                ("HELD OUT DIGITIZERS", "median_heldout_prominence", "heldout_prominence_ci_low", "heldout_prominence_ci_high", poster.BLUE),
            ],
            "ROBUST Z",
            knee_n,
        )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_q_delta_{plane.lower()}.png",
            f"BEST-N DISJOINT Q DELTA {plane}",
            rows,
            [
                ("BLIND FULL BAND", "median_blind_selected_heldout_abs_q_delta", "blind_selected_heldout_abs_q_delta_ci_low", "blind_selected_heldout_abs_q_delta_ci_high", poster.BLUE),
                ("NEAR TRAIN Q", "median_selected_heldout_abs_q_delta", None, None, poster.ORANGE),
            ],
            "ABS Q DELTA",
            knee_n,
            reference_line=(tune_half_width, "TUNE TOLERANCE"),
        )
        _curve_plot_with_intervals(
            poster,
            out / f"best_n_score_{plane.lower()}.png",
            f"BEST-N ADAPTIVE SCORE {plane}",
            rows,
            [
                ("FIT SCORE", "median_subset_score", None, None, poster.BLUE),
                ("TEST VISIBLE", "median_curve_test_visible_fraction", None, None, poster.GREEN),
            ],
            "FRACTION/SCORE",
            knee_n,
            y_range=(0.0, 1.0),
        )


def write_summary_outputs(
    curve_rows: Sequence[dict[str, object]],
    validation_rows: Sequence[dict[str, object]],
    out: Path,
    bootstrap_samples: int,
    bootstrap_block_spills: int,
    tune_half_width: float,
) -> list[dict[str, object]]:
    summary_rows = summarize(curve_rows, validation_rows, bootstrap_samples, bootstrap_block_spills)
    write_csv(out / "best_n_summary.csv", summary_rows, SUMMARY_FIELDS)
    atomic_write_text(out / "best_n_summary.md", recommendation_text(summary_rows, tune_half_width))
    collection_rows = summarize_by_collection(
        curve_rows,
        validation_rows,
        bootstrap_samples,
        bootstrap_block_spills,
    )
    write_csv(out / "best_n_summary_by_collection.csv", collection_rows, SUMMARY_BY_COLLECTION_FIELDS)
    transfer_rows = cross_collection_transfer(collection_rows, tune_half_width)
    write_csv(out / "best_n_cross_collection_transfer.csv", transfer_rows, CROSS_COLLECTION_FIELDS)
    atomic_write_text(out / "best_n_cross_collection_transfer.md", cross_collection_text(transfer_rows))
    write_plots(summary_rows, out, tune_half_width)
    return summary_rows


def shard_rows(rows: Sequence[dict[str, str]], shard_index: int, shard_count: int) -> list[dict[str, str]]:
    if shard_count <= 1:
        return list(rows)
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(f"shard index {shard_index} is outside [0, {shard_count})")
    return [row for index, row in enumerate(rows) if index % shard_count == shard_index]


def cache_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    return str(row.get("collection", "")), str(row.get("spill_id", "")), str(row.get("plane", ""))


def completed_curve_keys(rows: Sequence[Mapping[str, object]], max_n: int) -> set[tuple[str, str, str]]:
    observed: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for row in rows:
        observed[cache_key(row)].append(int(row.get("subset_size") or 0))
    expected = list(range(1, max_n + 1))
    return {key for key, values in observed.items() if sorted(values) == expected}


def completed_validation_keys(
    rows: Sequence[Mapping[str, object]],
    max_n: int,
    folds: int,
) -> set[tuple[str, str, str]]:
    observed: dict[tuple[tuple[str, str, str], int], list[int]] = defaultdict(list)
    folds_by_key: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for row in rows:
        key = cache_key(row)
        fold = int(row.get("fold") or 0)
        observed[(key, fold)].append(int(row.get("subset_size") or 0))
        folds_by_key[key].add(fold)
    expected_n = list(range(1, max_n + 1))
    expected_folds = set(range(folds))
    return {
        key
        for key, observed_folds in folds_by_key.items()
        if observed_folds == expected_folds
        and all(sorted(observed.get((key, fold), [])) == expected_n for fold in expected_folds)
    }


def sorted_curve_rows(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rows, key=lambda row: (*cache_key(row), int(row.get("subset_size") or 0)))


def sorted_validation_rows(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rows, key=lambda row: (*cache_key(row), int(row.get("fold") or 0), int(row.get("subset_size") or 0)))


def merge_best_n_shards(
    shards_root: Path,
    out: Path,
    bootstrap_samples: int = 1000,
    tune_half_width: float = 0.0025,
    bootstrap_block_spills: int = 20,
) -> None:
    shard_dirs = sorted(path for path in shards_root.iterdir() if path.is_dir() and path.name.startswith("shard_"))
    if not shard_dirs:
        raise ValueError(f"no shard_* directories found under {shards_root}")
    shard_contracts = [load_run_contract(shard / "run_contract.json") for shard in shard_dirs]
    source_contract, shard_indices = compatible_shard_contracts(shard_contracts)
    curve_by_key: dict[tuple[str, str, str, int], dict[str, object]] = {}
    validation_by_key: dict[tuple[str, str, str, int, int], dict[str, object]] = {}
    for shard in shard_dirs:
        curve_path = shard / "best_n_curve_rows.csv"
        validation_path = shard / "best_n_disjoint_validation.csv"
        if not curve_path.exists() or not validation_path.exists():
            raise ValueError(f"incomplete Best-N shard: {shard}")
        for row in read_csv(curve_path):
            key = (*cache_key(row), int(row.get("subset_size") or 0))
            if key in curve_by_key:
                raise ValueError(f"duplicate Best-N curve key across shards: {key}")
            curve_by_key[key] = row
        for row in read_csv(validation_path):
            key = (*cache_key(row), int(row.get("fold") or 0), int(row.get("subset_size") or 0))
            if key in validation_by_key:
                raise ValueError(f"duplicate Best-N validation key across shards: {key}")
            validation_by_key[key] = row
    curve_rows = sorted_curve_rows(list(curve_by_key.values()))
    validation_rows = sorted_validation_rows(list(validation_by_key.values()))
    out.mkdir(parents=True, exist_ok=True)
    merged_contract = dict(source_contract)
    merged_contract.update(
        {
            "analysis": "best_n_merged",
            "shard_index": "merged",
            "source_shard_indices": shard_indices,
            "bootstrap_samples": int(bootstrap_samples),
            "bootstrap_block_spills": int(bootstrap_block_spills),
            "tune_half_width": float(tune_half_width),
        }
    )
    ensure_run_contract(
        out / "run_contract.json",
        merged_contract,
        (out / "best_n_curve_rows.csv", out / "best_n_disjoint_validation.csv"),
    )
    write_csv(out / "best_n_curve_rows.csv", curve_rows, CURVE_FIELDS)
    write_csv(out / "best_n_disjoint_validation.csv", validation_rows, VALIDATION_FIELDS)
    write_summary_outputs(
        curve_rows,
        validation_rows,
        out,
        bootstrap_samples,
        bootstrap_block_spills,
        tune_half_width,
    )
    atomic_write_text(
        out / "merge_summary.md",
        "# Best-N Shard Merge\n\n"
        f"- shard directories: `{len(shard_dirs)}`\n"
        f"- unique adaptive curve rows: `{len(curve_rows)}`\n"
        f"- unique disjoint validation rows: `{len(validation_rows)}`\n"
        f"- bootstrap samples: `{bootstrap_samples}`\n"
        f"- moving-bootstrap block length: `{bootstrap_block_spills}` spills within collection\n"
        f"- tune agreement tolerance: `{tune_half_width}`\n",
    )


def evaluate_best_n(
    cfg: dict[str, object],
    root: Path,
    out: Path,
    device: str = "cpu",
    spectral_config: str | None = None,
    max_n: int = 12,
    beam_width: int = 64,
    curve_limit: int = 0,
    validation_limit: int = 500,
    validation_beam_width: int = 32,
    folds: int = 3,
    fold_seed: int = 20260709,
    requested_fit_windows: int = 8,
    tune_half_width: float = 0.0025,
    bootstrap_samples: int = 500,
    bootstrap_block_spills: int = 20,
    progress_every: int = 25,
    shard_index: int = 0,
    shard_count: int = 1,
    resume: bool = False,
) -> None:
    spectral_config = spectral_config or str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256"))
    all_cache = cache_rows(root, spectral_config)
    curve_cache = shard_rows(stratified_limit(all_cache, curve_limit), shard_index, shard_count)
    validation_cache = shard_rows(stratified_limit(all_cache, validation_limit), shard_index, shard_count)
    manifest_dir = root / "manifest"
    cfg = dict(cfg)
    cfg["_manifest_dir"] = str(manifest_dir)
    meta_by_index = manifest_by_index(read_csv(manifest_dir / "bpm_index.csv"))
    out.mkdir(parents=True, exist_ok=True)
    curve_path = out / "best_n_curve_rows.csv"
    validation_path = out / "best_n_disjoint_validation.csv"
    contract_cfg = {key: value for key, value in cfg.items() if key != "_manifest_dir"}
    ensure_run_contract(
        out / "run_contract.json",
        {
            "analysis": "best_n",
            "config_sha256": object_sha256(contract_cfg),
            "inputs_root": str(root.resolve()),
            "bpm_index_sha256": file_sha256(manifest_dir / "bpm_index.csv"),
            "spectral_cache_index_sha256": file_sha256(root / "cache" / "index" / "spectral_cache.csv"),
            "spectral_config": spectral_config,
            "device": device,
            "max_n": int(max_n),
            "beam_width": int(beam_width),
            "curve_limit": int(curve_limit),
            "validation_limit": int(validation_limit),
            "validation_beam_width": int(validation_beam_width),
            "folds": int(folds),
            "fold_seed": int(fold_seed),
            "fit_windows": int(requested_fit_windows),
            "tune_half_width": float(tune_half_width),
            "bootstrap_samples": int(bootstrap_samples),
            "bootstrap_block_spills": int(bootstrap_block_spills),
            "shard_index": int(shard_index),
            "shard_count": int(shard_count),
        },
        (curve_path, validation_path),
    )
    curve_rows: list[dict[str, object]] = list(read_csv(curve_path)) if resume and curve_path.exists() else []
    curve_complete = completed_curve_keys(curve_rows, max_n)
    started = time.time()
    for idx, cache in enumerate(curve_cache, start=1):
        if cache_key(cache) not in curve_complete:
            curve_rows = [row for row in curve_rows if cache_key(row) != cache_key(cache)]
            curve_rows.extend(
                curve_rows_for_cache(
                    cache,
                    cfg,
                    meta_by_index,
                    max_n,
                    beam_width,
                    requested_fit_windows,
                    tune_half_width,
                    device,
                )
            )
        if progress_every and (idx % progress_every == 0 or idx == len(curve_cache)):
            write_csv(curve_path, sorted_curve_rows(curve_rows), CURVE_FIELDS)
            atomic_write_text(
                out / "progress.txt",
                f"curve {idx}/{len(curve_cache)} rows={len(curve_rows)} elapsed_seconds={time.time() - started:.1f}\n",
            )
    curve_rows = sorted_curve_rows(curve_rows)
    write_csv(curve_path, curve_rows, CURVE_FIELDS)

    validation_rows: list[dict[str, object]] = list(read_csv(validation_path)) if resume and validation_path.exists() else []
    validation_complete = completed_validation_keys(validation_rows, max_n, folds)
    for idx, cache in enumerate(validation_cache, start=1):
        if cache_key(cache) not in validation_complete:
            validation_rows = [row for row in validation_rows if cache_key(row) != cache_key(cache)]
            validation_rows.extend(
                validation_rows_for_cache(
                    cache,
                    cfg,
                    meta_by_index,
                    max_n,
                    validation_beam_width,
                    folds,
                    fold_seed,
                    requested_fit_windows,
                    tune_half_width,
                    device,
                )
            )
        if progress_every and (idx % progress_every == 0 or idx == len(validation_cache)):
            write_csv(validation_path, sorted_validation_rows(validation_rows), VALIDATION_FIELDS)
            atomic_write_text(
                out / "progress.txt",
                f"validation {idx}/{len(validation_cache)} rows={len(validation_rows)} elapsed_seconds={time.time() - started:.1f}\n",
            )
    validation_rows = sorted_validation_rows(validation_rows)
    write_csv(validation_path, validation_rows, VALIDATION_FIELDS)
    write_summary_outputs(
        curve_rows,
        validation_rows,
        out,
        bootstrap_samples,
        bootstrap_block_spills,
        tune_half_width,
    )
    atomic_write_text(
        out / "run_summary.md",
        "# Best-N Run Summary\n\n"
        f"- spectral config: `{spectral_config}`\n"
        f"- max N: `{max_n}`\n"
        f"- adaptive curve cache rows: `{len(curve_cache)}`\n"
        f"- adaptive beam width: `{beam_width}`\n"
        f"- disjoint validation cache rows: `{len(validation_cache)}`\n"
        f"- validation beam width: `{validation_beam_width}`\n"
        f"- digitizer folds: `{folds}`\n"
        f"- fit windows: `{requested_fit_windows}` (reduced only when a cache row has fewer windows)\n"
        f"- validation windows: all later windows after purging every window that overlaps the fit prefix\n"
        f"- tune agreement tolerance: `{tune_half_width}`\n"
        f"- confidence intervals: moving-block bootstrap, `{bootstrap_block_spills}` spills per block within collection\n"
        f"- shard: `{shard_index + 1}/{shard_count}`\n"
        f"- resumed: `{str(resume).lower()}`\n"
        f"- device: `{device}`\n"
        f"- elapsed seconds: `{time.time() - started:.1f}`\n",
    )
