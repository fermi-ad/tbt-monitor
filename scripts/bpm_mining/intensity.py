"""Intensity-assisted tune study over paired raw position/intensity captures."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from .best_n import beam_search_curve, bootstrap_ci, mean, median, percentile, purged_window_split, training_candidates
from .config import plane_band
from .contracts import (
    compatible_shard_contracts,
    ensure_run_contract,
    load_run_contract,
    manifest_inventory_sha256,
    object_sha256,
)
from .identity import channel_token
from .io import atomic_write_text, discover_manifests, ensure_dir, read_csv, safe_payload_path, write_csv
from .spectra import compute_spectra, window_starts
from .statistics import (
    benjamini_hochberg,
    block_bootstrap_interval,
    block_sign_permutation_p_value,
    rank_biserial_effect,
    stable_seed,
)


INTEGRITY_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "channel",
    "digitizer",
    "position_source_key",
    "intensity_source_key",
    "stream_identity_match",
    "position_sample_count",
    "intensity_sample_count",
    "position_payload_sample_count",
    "intensity_payload_sample_count",
    "sample_count",
    "finite_fraction",
    "plausible_fraction",
    "negative_fraction",
    "first_invalid_sample",
    "first_bad_block_turn",
    "analysis_range_plausible_fraction",
    "analysis_range_negative_fraction",
    "analysis_range_intensity_p01",
    "analysis_range_intensity_median",
    "analysis_range_intensity_p99",
    "quality_flags",
]

WINDOW_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "method",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "window_index",
    "center_turn",
    "is_fit_window",
    "window_role",
    "train_q_hat",
    "q_global",
    "q_near_train",
    "peak_prominence_global",
    "peak_prominence_at_train_q",
    "power_support_at_train_q",
    "spectral_entropy",
    "visible_at_train_q",
    "global_intensity_normalized",
    "selected_intensity_normalized",
    "effective_member_count",
    "weight_fallback",
    "valid_window",
]

SPILL_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "method",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "train_q_hat",
    "test_window_count",
    "valid_test_window_fraction",
    "median_test_q_near_train",
    "median_abs_q_delta_from_train",
    "q_mad",
    "median_peak_prominence_at_train_q",
    "p10_peak_prominence_at_train_q",
    "median_power_support_at_train_q",
    "visible_test_window_fraction",
    "median_spectral_entropy",
    "median_global_intensity_normalized",
    "median_effective_member_count",
    "weight_fallback_window_fraction",
]

LOSS_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "threshold_fraction",
    "intensity_crossing_turn",
    "power_support_loss_turn",
    "loss_minus_intensity_turn",
]

ERROR_FIELDS = ["collection", "spill_id", "stage", "detail"]

EFFECT_FIELDS = [
    "plane",
    "subset_size",
    "method",
    "metric",
    "spill_count",
    "beneficial_direction",
    "minimum_practical_effect",
    "median_paired_delta",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
    "permutation_p_value",
    "fdr_q_value",
    "rank_biserial_effect",
    "median_abs_q_shift_vs_unweighted",
    "q_shift_within_tolerance_fraction",
    "statistical_benefit_pass",
    "practical_effect_pass",
    "retain_method_for_tune_analysis",
    "bootstrap_block_spills",
]

CORRELATION_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "metric",
    "lag_windows",
    "sample_count",
    "spearman_rho",
]

CORRELATION_SUMMARY_FIELDS = [
    "plane",
    "subset_size",
    "metric",
    "lag_windows",
    "spill_count",
    "median_spearman_rho",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
    "bootstrap_block_spills",
]


@dataclass(frozen=True)
class PairedChannel:
    collection: str
    spill_id: str
    plane: str
    channel: str
    digitizer: str
    position_source_key: str
    intensity_source_key: str
    position_path: Path
    intensity_path: Path
    position_sample_count: int
    intensity_sample_count: int
    position_payload_sample_count: int
    intensity_payload_sample_count: int
    sample_count: int
    stream_identity_match: bool


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def has_weight_fallback(value: object) -> bool:
    return bool(str(value or "").strip())


def paired_channels(manifest_path: Path) -> list[PairedChannel]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle = manifest_path.parent
    collection = bundle.parent.name
    spill_id = bundle.name
    streams = {
        str(item.get("stream_key", "")): item
        for item in data.get("streams", [])
        if isinstance(item, dict) and item.get("stream_key")
    }
    out: list[PairedChannel] = []
    for position_key, position in streams.items():
        if "TBT_POSITION_RAW" not in position_key:
            continue
        intensity_key = position_key.replace("TBT_POSITION_RAW", "TBT_INTENSITY_RAW")
        intensity = streams.get(intensity_key)
        if not intensity:
            continue
        position_path = safe_payload_path(bundle, position.get("payload_file"))
        intensity_path = safe_payload_path(bundle, intensity.get("payload_file"))
        if position_path is None or intensity_path is None:
            continue
        position_count = int(position.get("sample_count") or int(position.get("payload_bytes") or 0) // 4)
        intensity_count = int(intensity.get("sample_count") or int(intensity.get("payload_bytes") or 0) // 4)
        position_payload_count = position_path.stat().st_size // 4
        intensity_payload_count = intensity_path.stat().st_size // 4
        stream_identity_match = (
            str(position.get("stream_id") or "") == str(intensity.get("stream_id") or "")
            and str(position.get("stream_ms") or "") == str(intensity.get("stream_ms") or "")
        )
        plane = str(position.get("plane") or "").upper()
        token = channel_token(position_key)
        out.append(
            PairedChannel(
                collection,
                spill_id,
                plane,
                token or position_key,
                str(position.get("bpm_ip") or position.get("digitizer") or ""),
                position_key,
                intensity_key,
                position_path,
                intensity_path,
                position_count,
                intensity_count,
                position_payload_count,
                intensity_payload_count,
                min(position_count, intensity_count, position_payload_count, intensity_payload_count),
                stream_identity_match,
            )
        )
    out.sort(key=lambda pair: (pair.plane, pair.channel, pair.position_source_key))
    return out


def first_sustained_bad_block(valid: np.ndarray, block_turns: int, minimum_fraction: float) -> int | None:
    if valid.size == 0:
        return 0
    for start in range(0, valid.size, block_turns):
        block = valid[start : start + block_turns]
        if block.size and float(np.mean(block)) < minimum_fraction:
            return start
    return None


def intensity_integrity_row(
    pair: PairedChannel,
    analysis_turns: int,
    max_abs_intensity: float,
    block_turns: int = 1024,
) -> dict[str, object]:
    data = np.memmap(pair.intensity_path, dtype="<f4", mode="r", shape=(pair.sample_count,))
    finite = np.isfinite(data)
    plausible = finite & (np.abs(data) <= max_abs_intensity)
    invalid_indices = np.flatnonzero(~plausible)
    first_invalid = int(invalid_indices[0]) if invalid_indices.size else None
    first_bad = first_sustained_bad_block(plausible, block_turns, 0.99)
    analysis = np.asarray(data[: min(analysis_turns, pair.sample_count)], dtype=np.float32)
    analysis_valid = np.isfinite(analysis) & (np.abs(analysis) <= max_abs_intensity)
    values = analysis[analysis_valid]
    flags: list[str] = []
    if first_bad is not None and first_bad < analysis_turns:
        flags.append("INVALID_WITHIN_ANALYSIS_RANGE")
    if not pair.stream_identity_match:
        flags.append("STREAM_IDENTITY_MISMATCH")
    if pair.position_sample_count != pair.intensity_sample_count:
        flags.append("SAMPLE_COUNT_MISMATCH")
    if (
        pair.position_sample_count != pair.position_payload_sample_count
        or pair.intensity_sample_count != pair.intensity_payload_sample_count
    ):
        flags.append("PAYLOAD_SIZE_MISMATCH")
    if float(np.mean(analysis_valid)) < 0.95 if analysis_valid.size else True:
        flags.append("LOW_PLAUSIBLE_FRACTION")
    return {
        "collection": pair.collection,
        "spill_id": pair.spill_id,
        "plane": pair.plane,
        "channel": pair.channel,
        "digitizer": pair.digitizer,
        "position_source_key": pair.position_source_key,
        "intensity_source_key": pair.intensity_source_key,
        "stream_identity_match": str(pair.stream_identity_match).lower(),
        "position_sample_count": pair.position_sample_count,
        "intensity_sample_count": pair.intensity_sample_count,
        "position_payload_sample_count": pair.position_payload_sample_count,
        "intensity_payload_sample_count": pair.intensity_payload_sample_count,
        "sample_count": pair.sample_count,
        "finite_fraction": _fmt(float(np.mean(finite))),
        "plausible_fraction": _fmt(float(np.mean(plausible))),
        "negative_fraction": _fmt(float(np.mean(data[plausible] < 0))) if np.any(plausible) else "",
        "first_invalid_sample": "" if first_invalid is None else first_invalid,
        "first_bad_block_turn": "" if first_bad is None else first_bad,
        "analysis_range_plausible_fraction": _fmt(float(np.mean(analysis_valid))) if analysis_valid.size else "",
        "analysis_range_negative_fraction": _fmt(float(np.mean(values < 0))) if values.size else "",
        "analysis_range_intensity_p01": _fmt(float(np.percentile(values, 1))) if values.size else "",
        "analysis_range_intensity_median": _fmt(float(np.median(values))) if values.size else "",
        "analysis_range_intensity_p99": _fmt(float(np.percentile(values, 99))) if values.size else "",
        "quality_flags": "|".join(flags),
    }


def intensity_window_features(
    traces: np.ndarray,
    starts: Sequence[int],
    window_turns: int,
    fit_count: int,
    max_abs_intensity: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    channels = traces.shape[0]
    medians = np.full((channels, len(starts)), np.nan, dtype=np.float32)
    valid_fraction = np.zeros_like(medians)
    for window_index, start in enumerate(starts):
        block = traces[:, start : start + window_turns]
        valid = np.isfinite(block) & (np.abs(block) <= max_abs_intensity)
        valid_fraction[:, window_index] = np.mean(valid, axis=1)
        for channel_index in range(channels):
            values = block[channel_index, valid[channel_index]]
            if values.size and valid_fraction[channel_index, window_index] >= 0.95:
                value = float(np.median(values))
                medians[channel_index, window_index] = max(0.0, value)
    early = medians[:, : max(1, fit_count)]
    reference = np.nanmedian(np.where(early > 0, early, np.nan), axis=1)
    normalized = medians / np.where(reference[:, None] > 0, reference[:, None], np.nan)
    global_envelope = np.nanmedian(normalized, axis=0)
    relative = normalized / np.where(global_envelope[None, :] > 0, global_envelope[None, :], np.nan)
    return medians, normalized, relative, global_envelope


def method_weights(relative_intensity: np.ndarray, method: str) -> np.ndarray:
    relative = np.asarray(relative_intensity, dtype=np.float32)
    finite = np.isfinite(relative) & (relative >= 0)
    if method == "unweighted":
        return np.ones(relative.shape, dtype=np.float32)
    if method == "sqrt_intensity":
        weights = np.where(finite, np.clip(np.sqrt(relative), 0.25, 4.0), 0.0).astype(np.float32)
        weights[:, ~np.any(finite, axis=0)] = 1.0
        return weights
    if method == "linear_intensity":
        weights = np.where(finite, np.clip(relative, 0.25, 4.0), 0.0).astype(np.float32)
        weights[:, ~np.any(finite, axis=0)] = 1.0
        return weights
    if method == "intensity_gate_50pct":
        weights = np.where(finite & (relative >= 0.5), 1.0, 0.0).astype(np.float32)
        empty_windows = np.flatnonzero((np.sum(weights, axis=0) == 0.0) & np.any(finite, axis=0))
        if empty_windows.size:
            strongest = np.argmax(np.where(finite[:, empty_windows], relative[:, empty_windows], -np.inf), axis=0)
            weights[strongest, empty_windows] = 1.0
        weights[:, ~np.any(finite, axis=0)] = 1.0
        return weights
    raise ValueError(f"unknown intensity method: {method}")


def method_weight_fallbacks(relative_intensity: np.ndarray, method: str) -> list[str]:
    relative = np.asarray(relative_intensity, dtype=np.float32)
    finite = np.isfinite(relative) & (relative >= 0)
    labels = ["" for _ in range(relative.shape[1])]
    if method == "unweighted":
        return labels
    no_usable = ~np.any(finite, axis=0)
    for index in np.flatnonzero(no_usable):
        labels[int(index)] = "NO_USABLE_INTENSITY_UNWEIGHTED"
    if method == "intensity_gate_50pct":
        gate_empty = np.any(finite, axis=0) & ~np.any(finite & (relative >= 0.5), axis=0)
        for index in np.flatnonzero(gate_empty):
            labels[int(index)] = "EMPTY_FINITE_GATE_STRONGEST"
    return labels


def combine_weighted_spectra(spectra: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if spectra.shape[:2] != weights.shape:
        raise ValueError(f"spectra/weight shape mismatch: {spectra.shape} vs {weights.shape}")
    clean_weights = np.where(np.isfinite(weights) & (weights > 0), weights, 0.0).astype(np.float32)
    if spectra.shape[0] == 1:
        valid = clean_weights[0] > 0
        combined = np.asarray(spectra[0], dtype=np.float32).copy()
        combined[~valid] = np.nan
        return combined, valid.astype(np.float32)
    denominator = np.sum(clean_weights, axis=0)
    combined = np.sum(spectra * clean_weights[:, :, None], axis=0) / np.maximum(denominator[:, None], 1e-24)
    combined[denominator <= 0] = np.nan
    effective = denominator * denominator / np.maximum(np.sum(clean_weights * clean_weights, axis=0), 1e-24)
    effective[denominator <= 0] = 0.0
    return np.asarray(combined, dtype=np.float32), np.asarray(effective, dtype=np.float32)


def combined_window_metrics(
    combined: np.ndarray,
    tune_axis: np.ndarray,
    train_q: float,
    tune_half_width: float,
) -> dict[str, np.ndarray]:
    windows = combined.shape[0]
    result = {name: np.full(windows, np.nan, dtype=np.float32) for name in (
        "q_global",
        "q_near_train",
        "peak_prominence_global",
        "peak_prominence_at_train_q",
        "power_support_at_train_q",
        "spectral_entropy",
    )}
    valid_windows = np.all(np.isfinite(combined), axis=1) & (np.sum(combined, axis=1) > 0)
    if not np.any(valid_windows):
        return result
    power = np.asarray(combined[valid_windows], dtype=np.float64)
    log_power = np.log10(power + 1e-24)
    global_indices = np.argmax(power, axis=1)
    result["q_global"][valid_windows] = tune_axis[global_indices]
    band_median = np.median(log_power, axis=1)
    band_mad = np.median(np.abs(log_power - band_median[:, None]), axis=1) * 1.4826
    global_log = log_power[np.arange(log_power.shape[0]), global_indices]
    result["peak_prominence_global"][valid_windows] = (global_log - band_median) / np.maximum(band_mad, 1e-9)

    continuity = np.abs(tune_axis - train_q) <= max(0.01, tune_half_width * 4.0)
    qmask = np.abs(tune_axis - train_q) <= tune_half_width
    background = (np.abs(tune_axis - train_q) <= max(0.01, tune_half_width * 5.0)) & ~qmask
    if not np.any(continuity):
        continuity[np.argmin(np.abs(tune_axis - train_q))] = True
    if not np.any(qmask):
        qmask[np.argmin(np.abs(tune_axis - train_q))] = True
    if not np.any(background):
        background = ~qmask
    continuity_indices = np.flatnonzero(continuity)
    local_indices = continuity_indices[np.argmax(power[:, continuity], axis=1)]
    result["q_near_train"][valid_windows] = tune_axis[local_indices]
    signal = np.max(power[:, qmask], axis=1)
    background_power = power[:, background]
    background_median = np.median(background_power, axis=1)
    result["power_support_at_train_q"][valid_windows] = signal / np.maximum(background_median, 1e-24)
    log_background = np.log10(background_power + 1e-24)
    log_median = np.median(log_background, axis=1)
    log_mad = np.median(np.abs(log_background - log_median[:, None]), axis=1) * 1.4826
    result["peak_prominence_at_train_q"][valid_windows] = (np.log10(signal + 1e-24) - log_median) / np.maximum(log_mad, 1e-9)
    fraction = power / np.maximum(np.sum(power, axis=1, keepdims=True), 1e-24)
    entropy = -np.sum(np.where(fraction > 0, fraction * np.log(np.where(fraction > 0, fraction, 1.0)), 0.0), axis=1)
    result["spectral_entropy"][valid_windows] = entropy / math.log(max(2, power.shape[1]))
    return result


def _identities(pairs: Sequence[PairedChannel], indices: Sequence[int]) -> dict[str, str]:
    selected = [pairs[int(index)] for index in indices]
    return {
        "bpm_indices": ",".join(str(index) for index in indices),
        "bpm_members": ",".join(pair.channel for pair in selected),
        "bpm_source_keys": ",".join(pair.position_source_key for pair in selected),
    }


def _metadata(pairs: Sequence[PairedChannel]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for index, pair in enumerate(pairs):
        digits = "".join(char for char in pair.channel if char.isdigit())
        out[index] = {
            "digitizer": pair.digitizer,
            "ring_order": digits,
            "source_key": pair.position_source_key,
        }
    return out


def _load_plane(pairs: Sequence[PairedChannel], analysis_turns: int) -> tuple[list[PairedChannel], np.ndarray, np.ndarray]:
    usable: list[PairedChannel] = []
    positions: list[np.ndarray] = []
    intensities: list[np.ndarray] = []
    for pair in pairs:
        if pair.sample_count < analysis_turns or not pair.stream_identity_match:
            continue
        position = np.fromfile(pair.position_path, dtype="<f4", count=analysis_turns)
        intensity = np.fromfile(pair.intensity_path, dtype="<f4", count=analysis_turns)
        if position.size != analysis_turns or intensity.size != analysis_turns or not np.all(np.isfinite(position)):
            continue
        usable.append(pair)
        positions.append(np.asarray(position, dtype=np.float32))
        intensities.append(np.asarray(intensity, dtype=np.float32))
    if not positions:
        return [], np.empty((0, analysis_turns), dtype=np.float32), np.empty((0, analysis_turns), dtype=np.float32)
    return usable, np.stack(positions), np.stack(intensities)


def sustained_crossing(
    centers: np.ndarray,
    values: np.ndarray,
    threshold: float,
    start_index: int,
    run_windows: int = 5,
) -> float:
    run = 0
    for index in range(start_index, len(values)):
        value = float(values[index])
        if math.isfinite(value) and value < threshold:
            run += 1
            if run >= run_windows:
                return float(centers[index - run_windows + 1])
        else:
            run = 0
    return math.nan


def analyze_spill(
    manifest_path: Path,
    cfg: dict[str, object],
    device: str,
    subset_sizes: Sequence[int],
    analysis_turns: int,
    window_turns: int,
    stride_turns: int,
    requested_fit_windows: int,
    beam_width: int,
    tune_half_width: float,
    max_abs_intensity: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    pairs_all = paired_channels(manifest_path)
    integrity = [intensity_integrity_row(pair, analysis_turns, max_abs_intensity) for pair in pairs_all]
    window_rows: list[dict[str, object]] = []
    spill_rows: list[dict[str, object]] = []
    loss_rows: list[dict[str, object]] = []
    for plane in ("H", "V"):
        pairs, position, intensity = _load_plane([pair for pair in pairs_all if pair.plane == plane], analysis_turns)
        if len(pairs) < max(3, min(subset_sizes)):
            continue
        spec = {
            "name": "intensity_50k",
            "turn_start": 0,
            "turn_end": analysis_turns,
            "window_turns": window_turns,
            "stride_turns": stride_turns,
        }
        spectra, tune_axis, centers = compute_spectra(position, spec, plane_band(cfg, plane), cfg, device)
        starts = window_starts(analysis_turns, spec)
        fit_count, test_start = purged_window_split(centers, requested_fit_windows, window_turns)
        _, normalized_intensity, relative_intensity, global_envelope = intensity_window_features(
            intensity,
            starts,
            window_turns,
            fit_count,
            max_abs_intensity,
        )
        bpm_indices = np.arange(len(pairs), dtype=np.int32)
        candidates = training_candidates(spectra[:, :fit_count, :], tune_axis)
        winners = beam_search_curve(
            spectra[:, :fit_count, :],
            tune_axis,
            centers[:fit_count],
            bpm_indices,
            candidates,
            _metadata(pairs),
            None,
            window_turns,
            max(subset_sizes),
            beam_width,
            int(cfg.get("subset_search", {}).get("subset_chunk_size", 4096)),
            device,
        )
        winner_by_size = {len(score.subset): score for score, _candidate_count in winners}
        for subset_size in subset_sizes:
            score = winner_by_size.get(int(subset_size))
            if score is None:
                continue
            selected_positions = [int(position) for position in score.subset]
            identities = _identities(pairs, selected_positions)
            selected_spectra = spectra[selected_positions]
            selected_normalized = normalized_intensity[selected_positions]
            selected_relative = relative_intensity[selected_positions]
            for method in ("unweighted", "sqrt_intensity", "linear_intensity", "intensity_gate_50pct"):
                weights = method_weights(selected_relative, method)
                fallback_labels = method_weight_fallbacks(selected_relative, method)
                combined, effective = combine_weighted_spectra(selected_spectra, weights)
                metrics = combined_window_metrics(combined, tune_axis, score.q_hat, tune_half_width)
                selected_envelope = np.nanmedian(selected_normalized, axis=0)
                for window_index, center in enumerate(centers):
                    valid_window = math.isfinite(float(metrics["q_near_train"][window_index]))
                    window_rows.append(
                        {
                            "collection": pairs[0].collection,
                            "spill_id": pairs[0].spill_id,
                            "plane": plane,
                            "subset_size": subset_size,
                            "method": method,
                            **identities,
                            "window_index": window_index,
                            "center_turn": _fmt(float(center)),
                            "is_fit_window": str(window_index < fit_count).lower(),
                            "window_role": "fit" if window_index < fit_count else "test" if window_index >= test_start else "purged_overlap",
                            "train_q_hat": _fmt(score.q_hat),
                            "q_global": _fmt(float(metrics["q_global"][window_index])),
                            "q_near_train": _fmt(float(metrics["q_near_train"][window_index])),
                            "peak_prominence_global": _fmt(float(metrics["peak_prominence_global"][window_index])),
                            "peak_prominence_at_train_q": _fmt(float(metrics["peak_prominence_at_train_q"][window_index])),
                            "power_support_at_train_q": _fmt(float(metrics["power_support_at_train_q"][window_index])),
                            "spectral_entropy": _fmt(float(metrics["spectral_entropy"][window_index])),
                            "visible_at_train_q": _fmt(float(metrics["peak_prominence_at_train_q"][window_index] >= 4.0)) if valid_window else "",
                            "global_intensity_normalized": _fmt(float(global_envelope[window_index])),
                            "selected_intensity_normalized": _fmt(float(selected_envelope[window_index])),
                            "effective_member_count": _fmt(float(effective[window_index])),
                            "weight_fallback": fallback_labels[window_index],
                            "valid_window": str(valid_window).lower(),
                        }
                    )
                test_indices = np.arange(test_start, len(centers))
                valid_test = [
                    int(index)
                    for index in test_indices
                    if math.isfinite(float(metrics["q_near_train"][index]))
                ]
                q_values = [float(metrics["q_near_train"][index]) for index in valid_test]
                prominence_values = [float(metrics["peak_prominence_at_train_q"][index]) for index in valid_test]
                power_values = [float(metrics["power_support_at_train_q"][index]) for index in valid_test]
                entropy_values = [float(metrics["spectral_entropy"][index]) for index in valid_test]
                spill_rows.append(
                    {
                        "collection": pairs[0].collection,
                        "spill_id": pairs[0].spill_id,
                        "plane": plane,
                        "subset_size": subset_size,
                        "method": method,
                        **identities,
                        "train_q_hat": _fmt(score.q_hat),
                        "test_window_count": len(test_indices),
                        "valid_test_window_fraction": _fmt(len(valid_test) / max(1, len(test_indices))),
                        "median_test_q_near_train": _fmt(median(q_values)),
                        "median_abs_q_delta_from_train": _fmt(median([abs(value - score.q_hat) for value in q_values])),
                        "q_mad": _fmt(median([abs(value - median(q_values)) for value in q_values]) * 1.4826),
                        "median_peak_prominence_at_train_q": _fmt(median(prominence_values)),
                        "p10_peak_prominence_at_train_q": _fmt(percentile(prominence_values, 0.10)),
                        "median_power_support_at_train_q": _fmt(median(power_values)),
                        "visible_test_window_fraction": _fmt(mean([float(value >= 4.0) for value in prominence_values])),
                        "median_spectral_entropy": _fmt(median(entropy_values)),
                        "median_global_intensity_normalized": _fmt(median([float(global_envelope[index]) for index in valid_test])),
                        "median_effective_member_count": _fmt(median([float(effective[index]) for index in valid_test])),
                        "weight_fallback_window_fraction": _fmt(
                            mean([float(bool(fallback_labels[index])) for index in test_indices])
                        ),
                    }
                )
                if method == "unweighted":
                    early_power = median([float(metrics["power_support_at_train_q"][index]) for index in range(min(fit_count, len(centers)))])
                    for threshold_fraction in (0.75, 0.50, 0.25):
                        intensity_turn = sustained_crossing(centers, global_envelope, threshold_fraction, test_start)
                        loss_turn = sustained_crossing(
                            centers,
                            metrics["power_support_at_train_q"],
                            early_power * threshold_fraction,
                            test_start,
                        )
                        loss_rows.append(
                            {
                                "collection": pairs[0].collection,
                                "spill_id": pairs[0].spill_id,
                                "plane": plane,
                                "subset_size": subset_size,
                                "threshold_fraction": _fmt(threshold_fraction),
                                "intensity_crossing_turn": _fmt(intensity_turn),
                                "power_support_loss_turn": _fmt(loss_turn),
                                "loss_minus_intensity_turn": _fmt(loss_turn - intensity_turn),
                            }
                        )
    return integrity, window_rows, spill_rows, loss_rows


def _rank(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    order = np.argsort(array, kind="mergesort")
    ranks = np.empty(array.size, dtype=np.float64)
    start = 0
    while start < array.size:
        end = start
        while end + 1 < array.size and array[order[end + 1]] == array[order[start]]:
            end += 1
        ranks[order[start : end + 1]] = 0.5 * (start + end) + 1.0
        start = end + 1
    return ranks


def spearman_values(left: Sequence[float], right: Sequence[float]) -> float:
    pairs = [(a, b) for a, b in zip(left, right) if math.isfinite(a) and math.isfinite(b)]
    if len(pairs) < 3:
        return math.nan
    a_rank = _rank([pair[0] for pair in pairs])
    b_rank = _rank([pair[1] for pair in pairs])
    if float(np.std(a_rank)) == 0.0 or float(np.std(b_rank)) == 0.0:
        return math.nan
    return float(np.corrcoef(a_rank, b_rank)[0, 1])


def method_effects(
    spill_rows: Sequence[Mapping[str, object]],
    tune_tolerance: float,
    bootstrap_samples: int,
    permutation_samples: int,
    bootstrap_block_spills: int = 20,
) -> list[dict[str, object]]:
    by_key: dict[tuple[str, str, str, int, str], Mapping[str, object]] = {}
    for row in spill_rows:
        by_key[(str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["subset_size"]), str(row["method"]))] = row
    metrics = (
        ("median_peak_prominence_at_train_q", 1.0, 0.10),
        ("median_power_support_at_train_q", 1.0, 0.05),
        ("visible_test_window_fraction", 1.0, 0.01),
        ("median_spectral_entropy", -1.0, 0.001),
        ("median_abs_q_delta_from_train", -1.0, 0.00025),
    )
    output: list[dict[str, object]] = []
    planes_sizes = sorted({(key[2], key[3]) for key in by_key})
    for plane, subset_size in planes_sizes:
        spill_keys = sorted({(key[0], key[1]) for key in by_key if key[2] == plane and key[3] == subset_size})
        for method in ("sqrt_intensity", "linear_intensity", "intensity_gate_50pct"):
            q_shifts: list[float] = []
            for collection, spill_id in spill_keys:
                baseline = by_key.get((collection, spill_id, plane, subset_size, "unweighted"))
                candidate = by_key.get((collection, spill_id, plane, subset_size, method))
                if baseline and candidate:
                    q_a = _f(baseline.get("median_test_q_near_train"))
                    q_b = _f(candidate.get("median_test_q_near_train"))
                    if math.isfinite(q_a) and math.isfinite(q_b):
                        q_shifts.append(abs(q_b - q_a))
            for metric, direction, practical_margin in metrics:
                series_by_collection: dict[str, list[float]] = defaultdict(list)
                for collection, spill_id in spill_keys:
                    baseline = by_key.get((collection, spill_id, plane, subset_size, "unweighted"))
                    candidate = by_key.get((collection, spill_id, plane, subset_size, method))
                    if not baseline or not candidate:
                        continue
                    base_value = _f(baseline.get(metric))
                    candidate_value = _f(candidate.get(metric))
                    if math.isfinite(base_value) and math.isfinite(candidate_value):
                        series_by_collection[collection].append(candidate_value - base_value)
                diffs = [
                    value
                    for collection in sorted(series_by_collection)
                    for value in series_by_collection[collection]
                ]
                lo, hi = block_bootstrap_interval(
                    series_by_collection,
                    bootstrap_samples,
                    stable_seed("intensity-effect", plane, subset_size, method, metric),
                    bootstrap_block_spills,
                )
                p_value = block_sign_permutation_p_value(
                    series_by_collection,
                    permutation_samples,
                    stable_seed("intensity-permutation", plane, subset_size, method, metric),
                    bootstrap_block_spills,
                )
                lo_value = _f(lo)
                hi_value = _f(hi)
                median_shift = median(q_shifts)
                shift_fraction = mean([float(value <= tune_tolerance) for value in q_shifts])
                beneficial_ci = lo_value > 0 if direction > 0 else hi_value < 0
                practical_ci = lo_value > practical_margin if direction > 0 else hi_value < -practical_margin
                tune_stability_pass = (
                    math.isfinite(median_shift)
                    and median_shift <= tune_tolerance
                    and math.isfinite(shift_fraction)
                    and shift_fraction >= 0.95
                )
                output.append(
                    {
                        "plane": plane,
                        "subset_size": subset_size,
                        "method": method,
                        "metric": metric,
                        "spill_count": len(diffs),
                        "beneficial_direction": "increase" if direction > 0 else "decrease",
                        "minimum_practical_effect": _fmt(practical_margin),
                        "median_paired_delta": _fmt(median(diffs)),
                        "bootstrap_ci_low": _fmt(lo_value),
                        "bootstrap_ci_high": _fmt(hi_value),
                        "permutation_p_value": _fmt(float(p_value)) if isinstance(p_value, float) else "",
                        "fdr_q_value": "",
                        "rank_biserial_effect": rank_biserial_effect(diffs),
                        "median_abs_q_shift_vs_unweighted": _fmt(median_shift),
                        "q_shift_within_tolerance_fraction": _fmt(shift_fraction),
                        "statistical_benefit_pass": str(beneficial_ci and tune_stability_pass).lower(),
                        "practical_effect_pass": str(practical_ci).lower(),
                        "retain_method_for_tune_analysis": "false",
                        "bootstrap_block_spills": bootstrap_block_spills,
                    }
                )
    benjamini_hochberg(output)
    for row in output:
        q_value = _f(row.get("fdr_q_value"))
        row["retain_method_for_tune_analysis"] = str(
            row["statistical_benefit_pass"] == "true"
            and row["practical_effect_pass"] == "true"
            and math.isfinite(q_value)
            and q_value <= 0.05
        ).lower()
    return output


def correlation_rows(window_rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, int], list[Mapping[str, object]]] = defaultdict(list)
    for row in window_rows:
        if row.get("method") == "unweighted" and str(row.get("window_role")) == "test":
            grouped[(str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["subset_size"]))].append(row)
    output: list[dict[str, object]] = []
    for (collection, spill_id, plane, subset_size), rows in sorted(grouped.items()):
        rows = sorted(rows, key=lambda row: int(float(str(row.get("window_index") or 0))))
        intensity = [_f(row.get("global_intensity_normalized")) for row in rows]
        metric_values = {
            "peak_prominence_at_train_q": [_f(row.get("peak_prominence_at_train_q")) for row in rows],
            "power_support_at_train_q": [_f(row.get("power_support_at_train_q")) for row in rows],
        }
        for metric, values in metric_values.items():
            for lag in range(-3, 4):
                if lag < 0:
                    left, right = intensity[-lag:], values[:lag]
                elif lag > 0:
                    left, right = intensity[:-lag], values[lag:]
                else:
                    left, right = intensity, values
                rho = spearman_values(left, right)
                output.append(
                    {
                        "collection": collection,
                        "spill_id": spill_id,
                        "plane": plane,
                        "subset_size": subset_size,
                        "metric": metric,
                        "lag_windows": lag,
                        "sample_count": sum(1 for a, b in zip(left, right) if math.isfinite(a) and math.isfinite(b)),
                        "spearman_rho": _fmt(rho),
                    }
                )
    return output


def correlation_summary(
    rows: Sequence[Mapping[str, object]],
    bootstrap_samples: int,
    bootstrap_block_spills: int = 20,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, int, str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[(str(row["plane"]), int(row["subset_size"]), str(row["metric"]), int(row["lag_windows"]))][
            str(row["collection"])
        ].append(_f(row.get("spearman_rho")))
    output: list[dict[str, object]] = []
    for (plane, subset_size, metric, lag), series_by_collection in sorted(grouped.items()):
        finite = [
            value
            for collection in sorted(series_by_collection)
            for value in series_by_collection[collection]
            if math.isfinite(value)
        ]
        lo, hi = block_bootstrap_interval(
            series_by_collection,
            bootstrap_samples,
            stable_seed("intensity-correlation", plane, subset_size, metric, lag),
            bootstrap_block_spills,
        )
        lo_value = _f(lo)
        hi_value = _f(hi)
        output.append(
            {
                "plane": plane,
                "subset_size": subset_size,
                "metric": metric,
                "lag_windows": lag,
                "spill_count": len(finite),
                "median_spearman_rho": _fmt(median(finite)),
                "bootstrap_ci_low": _fmt(lo_value),
                "bootstrap_ci_high": _fmt(hi_value),
                "bootstrap_block_spills": bootstrap_block_spills,
            }
        )
    return output


def analyze_intensity_capture(
    cfg: dict[str, object],
    capture_root: Path,
    out: Path,
    device: str = "cpu",
    subset_sizes: Sequence[int] = (1, 3, 5, 10),
    analysis_turns: int = 50_000,
    window_turns: int = 4096,
    stride_turns: int = 512,
    fit_windows: int = 8,
    beam_width: int = 32,
    tune_half_width: float = 0.0025,
    max_abs_intensity: float = 1e12,
    limit: int = 0,
    shard_index: int = 0,
    shard_count: int = 1,
) -> None:
    all_manifests = discover_manifests([capture_root])
    if limit:
        all_manifests = all_manifests[:limit]
    manifests = list(all_manifests)
    manifests = [path for index, path in enumerate(manifests) if index % max(1, shard_count) == shard_index]
    ensure_dir(out)
    ensure_run_contract(
        out / "run_contract.json",
        {
            "analysis": "intensity_tune_weighting",
            "config_sha256": object_sha256(cfg),
            "capture_root": str(capture_root.resolve()),
            "manifest_count": len(all_manifests),
            "manifest_inventory_sha256": manifest_inventory_sha256(all_manifests, capture_root),
            "device": device,
            "subset_sizes": sorted(set(int(size) for size in subset_sizes)),
            "analysis_turns": int(analysis_turns),
            "window_turns": int(window_turns),
            "stride_turns": int(stride_turns),
            "fit_windows": int(fit_windows),
            "beam_width": int(beam_width),
            "tune_half_width": float(tune_half_width),
            "max_abs_intensity": float(max_abs_intensity),
            "limit": int(limit),
            "shard_index": int(shard_index),
            "shard_count": int(shard_count),
        },
        (
            out / "intensity_payload_integrity.csv",
            out / "intensity_window_metrics.csv",
            out / "intensity_spill_metrics.csv",
        ),
    )
    integrity: list[dict[str, object]] = []
    windows: list[dict[str, object]] = []
    spills: list[dict[str, object]] = []
    losses: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    started = time.time()
    for index, manifest in enumerate(manifests, start=1):
        try:
            result = analyze_spill(
                manifest,
                cfg,
                device,
                sorted(set(int(size) for size in subset_sizes)),
                analysis_turns,
                window_turns,
                stride_turns,
                fit_windows,
                beam_width,
                tune_half_width,
                max_abs_intensity,
            )
            integrity.extend(result[0])
            windows.extend(result[1])
            spills.extend(result[2])
            losses.extend(result[3])
        except Exception as exc:
            errors.append(
                {
                    "collection": manifest.parent.parent.name,
                    "spill_id": manifest.parent.name,
                    "stage": "analyze_spill",
                    "detail": repr(exc),
                }
            )
        if index % 5 == 0 or index == len(manifests):
            write_csv(out / "intensity_payload_integrity.csv", integrity, INTEGRITY_FIELDS)
            write_csv(out / "intensity_window_metrics.csv", windows, WINDOW_FIELDS)
            write_csv(out / "intensity_spill_metrics.csv", spills, SPILL_FIELDS)
            write_csv(out / "intensity_loss_turns.csv", losses, LOSS_FIELDS)
            write_csv(out / "errors.csv", errors, ERROR_FIELDS)
            atomic_write_text(
                out / "progress.txt",
                f"spills={index}/{len(manifests)} window_rows={len(windows)} errors={len(errors)} elapsed_seconds={time.time() - started:.1f}\n",
            )
    atomic_write_text(
        out / "run_summary.md",
        "# Intensity Study Shard\n\n"
        f"- capture root: `{capture_root}`\n"
        f"- manifests analyzed: `{len(manifests)}`\n"
        f"- subset sizes: `{','.join(str(size) for size in subset_sizes)}`\n"
        f"- analysis turns: `{analysis_turns}`\n"
        f"- window/stride turns: `{window_turns}/{stride_turns}`\n"
        f"- fit windows: `{fit_windows}`\n"
        f"- beam width: `{beam_width}`\n"
        f"- maximum plausible absolute intensity: `{max_abs_intensity}`\n"
        f"- shard: `{shard_index + 1}/{shard_count}`\n"
        f"- errors: `{len(errors)}`\n"
        f"- elapsed seconds: `{time.time() - started:.1f}`\n",
    )


def _unique_rows(paths: Sequence[Path], key_fields: Sequence[str]) -> list[dict[str, object]]:
    rows: dict[tuple[str, ...], dict[str, object]] = {}
    for path in paths:
        if not path.exists():
            raise ValueError(f"missing intensity shard artifact: {path}")
        for row in read_csv(path):
            key = tuple(str(row.get(field, "")) for field in key_fields)
            if key in rows:
                raise ValueError(f"duplicate intensity key across shards for {path.name}: {key}")
            rows[key] = row
    return sorted(rows.values(), key=lambda row: tuple(str(row.get(field, "")) for field in key_fields))


def merge_intensity_shards(
    shards_root: Path,
    out: Path,
    tune_tolerance: float = 0.0025,
    bootstrap_samples: int = 1000,
    permutation_samples: int = 10_000,
    bootstrap_block_spills: int = 20,
) -> None:
    shards = sorted(path for path in shards_root.iterdir() if path.is_dir() and path.name.startswith("shard_"))
    if not shards:
        raise ValueError(f"no shard_* directories under {shards_root}")
    shard_contracts = [load_run_contract(shard / "run_contract.json") for shard in shards]
    source_contract, shard_indices = compatible_shard_contracts(shard_contracts)
    integrity = _unique_rows(
        [path / "intensity_payload_integrity.csv" for path in shards],
        ("collection", "spill_id", "plane", "position_source_key"),
    )
    windows = _unique_rows(
        [path / "intensity_window_metrics.csv" for path in shards],
        ("collection", "spill_id", "plane", "subset_size", "method", "window_index"),
    )
    spills = _unique_rows(
        [path / "intensity_spill_metrics.csv" for path in shards],
        ("collection", "spill_id", "plane", "subset_size", "method"),
    )
    losses = _unique_rows(
        [path / "intensity_loss_turns.csv" for path in shards],
        ("collection", "spill_id", "plane", "subset_size", "threshold_fraction"),
    )
    errors = _unique_rows(
        [path / "errors.csv" for path in shards],
        ("collection", "spill_id", "stage"),
    )
    effects = method_effects(
        spills,
        tune_tolerance,
        bootstrap_samples,
        permutation_samples,
        bootstrap_block_spills,
    )
    correlations = correlation_rows(windows)
    correlation_summaries = correlation_summary(correlations, bootstrap_samples, bootstrap_block_spills)
    ensure_dir(out)
    merged_contract = dict(source_contract)
    merged_contract.update(
        {
            "analysis": "intensity_tune_weighting_merged",
            "shard_index": "merged",
            "source_shard_indices": shard_indices,
            "tune_tolerance": float(tune_tolerance),
            "bootstrap_samples": int(bootstrap_samples),
            "permutation_samples": int(permutation_samples),
            "bootstrap_block_spills": int(bootstrap_block_spills),
        }
    )
    ensure_run_contract(
        out / "run_contract.json",
        merged_contract,
        (
            out / "intensity_payload_integrity.csv",
            out / "intensity_window_metrics.csv",
            out / "intensity_spill_metrics.csv",
        ),
    )
    write_csv(out / "intensity_payload_integrity.csv", integrity, INTEGRITY_FIELDS)
    write_csv(out / "intensity_window_metrics.csv", windows, WINDOW_FIELDS)
    write_csv(out / "intensity_spill_metrics.csv", spills, SPILL_FIELDS)
    write_csv(out / "intensity_loss_turns.csv", losses, LOSS_FIELDS)
    write_csv(out / "intensity_method_effects.csv", effects, EFFECT_FIELDS)
    write_csv(out / "intensity_visibility_correlations.csv", correlations, CORRELATION_FIELDS)
    write_csv(out / "intensity_visibility_correlation_summary.csv", correlation_summaries, CORRELATION_SUMMARY_FIELDS)
    write_csv(out / "errors.csv", errors, ERROR_FIELDS)
    statistically_beneficial = [
        row
        for row in effects
        if row.get("statistical_benefit_pass") == "true" and math.isfinite(_f(row.get("fdr_q_value"))) and _f(row.get("fdr_q_value")) <= 0.05
    ]
    retained = [row for row in effects if row.get("retain_method_for_tune_analysis") == "true"]
    bad_within_range = sum(1 for row in integrity if "INVALID_WITHIN_ANALYSIS_RANGE" in str(row.get("quality_flags", "")))
    fallback_windows = sum(1 for row in windows if has_weight_fallback(row.get("weight_fallback")))
    atomic_write_text(
        out / "intensity_study_summary.md",
        "# Intensity-Assisted Tune Study\n\n"
        f"- merged shards: `{len(shards)}`\n"
        f"- paired intensity payload rows: `{len(integrity)}`\n"
        f"- payloads with invalid data inside the analysis range: `{bad_within_range}`\n"
        f"- spill-method summaries: `{len(spills)}`\n"
        f"- paired method-effect tests: `{len(effects)}`\n"
        f"- FDR-significant directional effects within tune tolerance: `{len(statistically_beneficial)}`\n"
        f"- effects also exceeding minimum practical thresholds: `{len(retained)}`\n"
        f"- moving-bootstrap/sign-flip block length: `{bootstrap_block_spills}` spills within collection\n"
        f"- analysis errors: `{len(errors)}`\n\n"
        f"- explicitly labeled weighted-method fallback windows: `{fallback_windows}`\n\n"
        "An intensity method is retained only when its paired bootstrap interval clears a declared minimum practical effect, its sign-flip permutation test survives FDR correction, its median tune shift remains within tolerance, and at least 95% of paired spill tune shifts stay within that tolerance.\n",
    )
