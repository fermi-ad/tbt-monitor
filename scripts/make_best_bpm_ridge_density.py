#!/usr/bin/env python3
"""Render Best-BPM ensemble ridge-density comparisons from raw captured spills.

This is a targeted sidecar for poster review. It reuses completed adaptive
membership tables, recomputes full-buffer sliding spectra from raw captured-
spill bundles, and renders the same ridge-density visual grammar as the older
elite autosweep gallery.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import time
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

import bpm_dgx_poster as poster
from bpm_mining.contracts import (
    ensure_run_contract,
    file_sha256,
    manifest_inventory_sha256,
    object_sha256,
)
from bpm_mining.identity import manifest_by_index, subset_indices
from bpm_mining.io import read_csv as read_mining_csv
from bpm_mining.statistics import moving_block_resample, stable_seed
from gpu_analyze_captured_spills import (
    FftBackend,
    analyze_plane,
    color_ramp,
    discover_manifests,
    draw_scaled_polyline,
    load_bundle,
    load_plane_traces,
)


RIDGE_METRIC_FIELDS = [
    "plane",
    "subset_size",
    "spill_count",
    "ridge_points",
    "valid_center_count",
    "median_iqr_width",
    "median_p10_p90_width",
    "median_peak_bin_fraction",
    "median_tune",
    "tune_std",
]

FIGURE_FIELDS = ["figure", "caption_file", "plane", "subset_size", "role", "source"]
WARNING_FIELDS = ["warning"]

SERIES_COLORS = (
    (44, 123, 182),
    (38, 153, 112),
    (222, 137, 56),
    (118, 91, 176),
    (190, 72, 72),
    (0, 137, 148),
    (166, 118, 29),
    (191, 91, 137),
    (91, 126, 42),
    (32, 36, 40),
)

CENTER_METRIC_FIELDS = [
    "plane",
    "subset_size",
    "center_turn",
    "sample_count",
    "sample_fraction",
    "median_tune",
    "iqr_width",
    "p10_p90_width",
    "peak_bin_fraction",
    "density_entropy",
    "median_selected_confidence",
    "global_fallback_fraction",
    "suspicious_step_fraction",
    "median_abs_step_delta",
]

LOSS_SUMMARY_FIELDS = [
    "plane",
    "subset_size",
    "peak_concentration_turn",
    "peak_concentration",
    "half_peak_threshold",
    "first_sustained_half_peak_loss_turn",
    "most_likely_change_turn",
    "pre_change_peak_fraction",
    "post_change_peak_fraction",
    "relative_peak_fraction_drop",
    "pre_change_iqr_width",
    "post_change_iqr_width",
    "relative_iqr_width_increase",
    "pre_change_sample_fraction",
    "post_change_sample_fraction",
    "relative_sample_fraction_drop",
    "change_score",
    "fraction_at_extraction_start",
    "fraction_at_extraction_end",
    "median_fraction_inside_extraction_marker",
]

LEGACY_COMPARISON_FIELDS = [
    "plane",
    "subset_size",
    "common_spill_count",
    "common_ridge_point_count",
    "common_center_count",
    "legacy_median_iqr_width",
    "ensemble_median_iqr_width",
    "median_iqr_delta_ensemble_minus_legacy",
    "median_iqr_delta_ci_low",
    "median_iqr_delta_ci_high",
    "fraction_centers_with_narrower_iqr",
    "legacy_median_p10_p90_width",
    "ensemble_median_p10_p90_width",
    "legacy_median_peak_bin_fraction",
    "ensemble_median_peak_bin_fraction",
    "median_peak_bin_fraction_gain",
    "median_peak_bin_fraction_gain_ci_low",
    "median_peak_bin_fraction_gain_ci_high",
    "legacy_median_density_entropy",
    "ensemble_median_density_entropy",
    "median_density_entropy_delta",
    "median_density_entropy_delta_ci_low",
    "median_density_entropy_delta_ci_high",
    "median_shared_ridge_mass_gain",
    "median_shared_ridge_mass_gain_ci_low",
    "median_shared_ridge_mass_gain_ci_high",
    "turn_block_windows",
    "turn_block_bootstrap_samples",
]

LEGACY_TURN_COMPARISON_FIELDS = [
    "plane",
    "subset_size",
    "center_turn",
    "paired_ridge_count",
    "shared_ridge_center",
    "legacy_iqr_width",
    "ensemble_iqr_width",
    "iqr_delta_ensemble_minus_legacy",
    "legacy_p10_p90_width",
    "ensemble_p10_p90_width",
    "p10_p90_delta_ensemble_minus_legacy",
    "legacy_peak_bin_fraction",
    "ensemble_peak_bin_fraction",
    "peak_bin_fraction_gain",
    "legacy_density_entropy",
    "ensemble_density_entropy",
    "density_entropy_delta",
    "legacy_shared_ridge_mass",
    "ensemble_shared_ridge_mass",
    "shared_ridge_mass_gain",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    poster.ensure_dir(path.parent)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_text(path: Path, text: str) -> None:
    poster.ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def f(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def fmt(value: float, digits: int = 6) -> str:
    return f"{value:.{digits}g}" if math.isfinite(value) else ""


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def short_bpm(value: str) -> str:
    return value.replace("acsys_DeliveryRingBPM ", "").strip()


def load_memberships(
    best_root: Path,
    subset_sizes: Sequence[str],
    membership_csv: Path | None = None,
) -> dict[tuple[str, str, str, str], set[str]]:
    memberships: dict[tuple[str, str, str, str], set[str]] = {}
    meta_by_index = manifest_by_index(read_mining_csv(best_root / "manifest" / "bpm_index.csv"))
    rows_by_size: dict[str, list[dict[str, str]]] = defaultdict(list)
    if membership_csv is not None:
        for row in read_csv(membership_csv):
            size = str(row.get("subset_size", ""))
            if size in subset_sizes:
                rows_by_size[size].append(row)
    else:
        for size in subset_sizes:
            candidates = (
                best_root / "subset_search" / f"best{size}" / f"best{size}_results.csv",
                best_root / "subset_search" / f"best{size}_results.csv",
            )
            path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
            rows_by_size[size].extend(read_csv(path))
    for size in subset_sizes:
        for row in rows_by_size.get(size, []):
            key = (row["collection"], row["spill_id"], row["plane"], size)
            indices = subset_indices(row, row["plane"], meta_by_index)
            current = {
                meta_by_index[(row["plane"], idx)].get("source_key", "")
                for idx in indices
                if (row["plane"], idx) in meta_by_index
            }
            current.discard("")
            if key in memberships:
                raise ValueError(f"duplicate membership row: {key}")
            memberships[key] = current
    return memberships


def selected_trace_indices(labels: Sequence[str], wanted_source_keys: set[str]) -> list[int]:
    return [idx for idx, label in enumerate(labels) if label in wanted_source_keys]


def percentile(values: Sequence[float], pct: float) -> float:
    vals = sorted(value for value in values if math.isfinite(value))
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def stdev(values: Sequence[float]) -> float:
    vals = [value for value in values if math.isfinite(value)]
    if len(vals) < 2:
        return math.nan
    mean = sum(vals) / len(vals)
    return math.sqrt(sum((value - mean) ** 2 for value in vals) / (len(vals) - 1))


def mean(values: Sequence[float]) -> float:
    vals = [value for value in values if math.isfinite(value)]
    return sum(vals) / len(vals) if vals else math.nan


def moving_block_interval(
    values: Sequence[float],
    samples: int,
    block_windows: int,
    seed: int,
) -> tuple[float, float]:
    """Non-circular block bootstrap over ordered, overlapping turn windows."""
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return math.nan, math.nan
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(max(100, int(samples))):
        sample = moving_block_resample(clean, rng, block_windows)
        draws.append(percentile(sample, 0.50))
    return percentile(draws, 0.025), percentile(draws, 0.975)


def build_density(
    rows: Sequence[dict[str, object]],
    plane: str,
    band: tuple[float, float],
    accepted_spills: set[int],
    tune_bins: int,
    accepted_spill_keys: set[tuple[str, str]] | None = None,
) -> dict[str, object]:
    filtered = [
        row
        for row in rows
        if row.get("plane") == plane
        and int(row.get("spill_index", -1)) in accepted_spills
        and (
            accepted_spill_keys is None
            or (str(row.get("run_name", "")), str(row.get("target_ms", ""))) in accepted_spill_keys
        )
        and math.isfinite(f(row.get("selected_tune")))
    ]
    centers = sorted({int(row.get("center_turn", 0)) for row in filtered})
    center_index = {center: idx for idx, center in enumerate(centers)}
    density = np.zeros((tune_bins, len(centers)), dtype=np.float32)
    grouped: dict[int, list[float]] = {center: [] for center in centers}
    for row in filtered:
        value = f(row.get("selected_tune"))
        if value < band[0] or value > band[1]:
            continue
        center = int(row.get("center_turn", 0))
        x_idx = center_index[center]
        y_idx = int((value - band[0]) / (band[1] - band[0]) * tune_bins)
        y_idx = max(0, min(tune_bins - 1, y_idx))
        density[y_idx, x_idx] += 1.0
        grouped[center].append(value)
    spill_keys = {
        (str(row.get("run_name", "")), str(row.get("target_ms", "")))
        for row in filtered
    }
    return {"centers": centers, "density": density, "grouped": grouped, "spill_keys": spill_keys}


def load_legacy_points(path: Path) -> dict[str, list[tuple[str, str, int, float]]]:
    """Load only the fields needed to reproduce the legacy ridge density."""
    points: dict[str, list[tuple[str, str, int, float]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            plane = str(row.get("plane", ""))
            tune = f(row.get("selected_tune"))
            if plane not in {"H", "V"} or not math.isfinite(tune):
                continue
            points[plane].append(
                (
                    str(row.get("run_name", "")),
                    str(row.get("target_ms", "")),
                    int(float(str(row.get("center_turn") or 0))),
                    tune,
                )
            )
    return points


def density_from_legacy_points(
    points: Sequence[tuple[str, str, int, float]],
    band: tuple[float, float],
    tune_bins: int,
    accepted_spill_keys: set[tuple[str, str]],
) -> dict[str, object]:
    grouped: dict[int, list[float]] = defaultdict(list)
    spill_keys: set[tuple[str, str]] = set()
    for run_name, target_ms, center, tune in points:
        key = (run_name, target_ms)
        if key not in accepted_spill_keys or tune < band[0] or tune > band[1]:
            continue
        spill_keys.add(key)
        grouped[center].append(tune)
    centers = sorted(grouped)
    density = np.zeros((tune_bins, len(centers)), dtype=np.float32)
    for col, center in enumerate(centers):
        for tune in grouped[center]:
            row = int((tune - band[0]) / (band[1] - band[0]) * tune_bins)
            density[max(0, min(tune_bins - 1, row)), col] += 1.0
    return {"centers": centers, "density": density, "grouped": grouped, "spill_keys": spill_keys}


def keyed_ensemble_points(
    rows: Sequence[dict[str, object]],
    plane: str,
    band: tuple[float, float],
) -> dict[tuple[str, str, int], float]:
    points: dict[tuple[str, str, int], float] = {}
    for row in rows:
        tune = f(row.get("selected_tune"))
        if row.get("plane") != plane or not math.isfinite(tune) or not band[0] <= tune <= band[1]:
            continue
        key = (
            str(row.get("run_name", "")),
            str(row.get("target_ms", "")),
            int(row.get("center_turn", 0)),
        )
        if key in points:
            raise ValueError(f"duplicate ensemble ridge point: {key}")
        points[key] = tune
    return points


def keyed_legacy_points(
    points: Sequence[tuple[str, str, int, float]],
    band: tuple[float, float],
) -> dict[tuple[str, str, int], float]:
    keyed: dict[tuple[str, str, int], float] = {}
    for run_name, target_ms, center, tune in points:
        if not math.isfinite(tune) or not band[0] <= tune <= band[1]:
            continue
        key = (run_name, target_ms, int(center))
        if key in keyed:
            raise ValueError(f"duplicate legacy ridge point: {key}")
        keyed[key] = tune
    return keyed


def density_from_keyed_points(
    points: dict[tuple[str, str, int], float],
    band: tuple[float, float],
    tune_bins: int,
) -> dict[str, object]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for (_run_name, _target_ms, center), tune in sorted(points.items()):
        grouped[center].append(tune)
    centers = sorted(grouped)
    density = np.zeros((tune_bins, len(centers)), dtype=np.float32)
    for col, center in enumerate(centers):
        for tune in grouped[center]:
            row = int((tune - band[0]) / (band[1] - band[0]) * tune_bins)
            density[max(0, min(tune_bins - 1, row)), col] += 1.0
    return {
        "centers": centers,
        "density": density,
        "grouped": grouped,
        "spill_keys": {(run_name, target_ms) for run_name, target_ms, _center in points},
        "point_keys": set(points),
    }


def exact_paired_density_results(
    baseline_points: dict[tuple[str, str, int], float],
    ensemble_points: dict[tuple[str, str, int], float],
    band: tuple[float, float],
    tune_bins: int,
) -> tuple[dict[str, object], dict[str, object]]:
    common = set(baseline_points) & set(ensemble_points)
    return (
        density_from_keyed_points({key: baseline_points[key] for key in common}, band, tune_bins),
        density_from_keyed_points({key: ensemble_points[key] for key in common}, band, tune_bins),
    )


def exact_paired_density_results_many(
    point_sets: Mapping[str, dict[tuple[str, str, int], float]],
    band: tuple[float, float],
    tune_bins: int,
) -> dict[str, dict[str, object]]:
    if not point_sets:
        return {}
    common = set.intersection(*(set(points) for points in point_sets.values()))
    return {
        label: density_from_keyed_points(
            {key: points[key] for key in common},
            band,
            tune_bins,
        )
        for label, points in point_sets.items()
    }


def normalized_columns(density: np.ndarray) -> np.ndarray:
    if density.size == 0:
        return density.copy()
    out = density.astype(np.float32, copy=True)
    col_sum = out.sum(axis=0, keepdims=True)
    col_sum[col_sum == 0.0] = 1.0
    return out / col_sum


def raster_cell_bounds(index: int, count: int, start: int, end: int, *, reverse: bool = False) -> tuple[int, int]:
    """Map one raster bin onto an inclusive pixel interval without truncation gaps."""
    if count <= 0 or index < 0 or index >= count or end < start:
        raise ValueError("invalid raster cell geometry")
    span = end - start + 1
    if reverse:
        low = end - int((index + 1) * span / count) + 1
        high = end - int(index * span / count)
    else:
        low = start + int(index * span / count)
        high = start + int((index + 1) * span / count) - 1
    return low, high


def median_points(grouped: dict[int, list[float]], pct: float) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for center in sorted(grouped):
        values = grouped[center]
        if values:
            points.append((float(center), percentile(values, pct)))
    return points


def overlay_extraction_context(
    pixels: bytearray,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
    centers: Sequence[int],
    start_turn: int,
    end_turn: int,
) -> None:
    if not centers or start_turn <= 0 or end_turn <= start_turn:
        return
    x0, y0, x1, y1 = area
    xmin = float(min(centers))
    xmax = float(max(centers) or min(centers) + 1)
    left = poster.scale_value(float(start_turn), xmin, xmax, x0, x1)
    right = poster.scale_value(float(end_turn), xmin, xmax, x0, x1)
    for x in (left, right):
        for y in range(y0, y1 + 1, 8):
            poster.line(pixels, width, height, x, y, x, min(y + 4, y1), poster.ORANGE)
    label_x = max(x0, min(x1 - 310, left + 6))
    poster.draw_text(pixels, width, height, label_x, y0 + 8, "EXTRACTION REVIEW BAND", poster.ORANGE, 2)


def ridge_density_plot(
    path: Path,
    title: str,
    rows: Sequence[dict[str, object]],
    plane: str,
    band: tuple[float, float],
    accepted_spills: set[int],
    args: argparse.Namespace,
) -> dict[str, object]:
    result = build_density(rows, plane, band, accepted_spills, args.ridge_density_tune_bins)
    centers = result["centers"]
    density = result["density"]
    grouped = result["grouped"]
    if not centers or density.size == 0:
        poster.no_data_png(path, title)
        return result
    if args.ridge_density_normalize:
        density = normalized_columns(density)
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    finite_vals = density[density > 0]
    vmax = float(np.percentile(finite_vals, 98)) if finite_vals.size else 1.0
    vmax = max(vmax, 1.0 / max(1, len(accepted_spills)) if args.ridge_density_normalize else 1.0)
    for col in range(len(centers)):
        cx0, cx1 = raster_cell_bounds(col, len(centers), x0, x1)
        for row_idx in range(args.ridge_density_tune_bins):
            value = float(density[row_idx, col])
            frac = value / vmax if vmax else 0.0
            color = color_ramp(frac) if value > 0 else (245, 247, 248)
            cy0, cy1 = raster_cell_bounds(
                row_idx,
                args.ridge_density_tune_bins,
                y0,
                y1,
                reverse=True,
            )
            poster.rect(pixels, width, height, cx0, cy0, cx1, cy1, color)
    area = (x0, y0, x1, y1)
    if args.mark_extraction_context:
        overlay_extraction_context(
            pixels,
            width,
            height,
            area,
            centers,
            args.extraction_range_start_turn,
            args.extraction_range_end_turn,
        )
    x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
    y_range = band
    draw_scaled_polyline(pixels, width, height, median_points(grouped, 0.10), x_range, y_range, area, (255, 255, 255), 1)
    draw_scaled_polyline(pixels, width, height, median_points(grouped, 0.90), x_range, y_range, area, (255, 255, 255), 1)
    draw_scaled_polyline(pixels, width, height, median_points(grouped, 0.25), x_range, y_range, area, (235, 237, 240), 1)
    draw_scaled_polyline(pixels, width, height, median_points(grouped, 0.75), x_range, y_range, area, (235, 237, 240), 1)
    draw_scaled_polyline(pixels, width, height, median_points(grouped, 0.50), x_range, y_range, area, (255, 255, 255), 3)
    poster.draw_text(pixels, width, height, x0, y1 + 8, str(min(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 110, y1 + 8, str(max(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x0, y0 - 28, f"{plane} {band[0]:.3f}-{band[1]:.3f} N={len(accepted_spills)}", poster.MUTED, 2)
    color_label = "COLOR: RIDGE FRACTION" if args.ridge_density_normalize else "COLOR: SPILL COUNT"
    poster.draw_text(pixels, width, height, x1 - 310, y0 - 28, f"{color_label}, WHITE: MED/PCT", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)
    return result


def center_metric_rows(
    rows: Sequence[dict[str, object]],
    plane: str,
    subset_size: str,
    band: tuple[float, float],
    tune_bins: int,
    spill_count: int,
) -> list[dict[str, object]]:
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if row.get("plane") != plane:
            continue
        value = f(row.get("selected_tune"))
        if not math.isfinite(value) or value < band[0] or value > band[1]:
            continue
        grouped[int(row.get("center_turn", 0))].append(row)
    out: list[dict[str, object]] = []
    for center, center_group in sorted(grouped.items()):
        values = [f(row.get("selected_tune")) for row in center_group]
        counts = [0 for _ in range(tune_bins)]
        for value in values:
            idx = int((value - band[0]) / (band[1] - band[0]) * tune_bins)
            idx = max(0, min(tune_bins - 1, idx))
            counts[idx] += 1
        probabilities = [count / max(1, len(values)) for count in counts if count > 0]
        entropy = -sum(probability * math.log(probability) for probability in probabilities) / math.log(max(2, tune_bins))
        confidences = [f(row.get("selected_confidence")) for row in center_group]
        step_deltas = [abs(f(row.get("step_delta"))) for row in center_group]
        out.append(
            {
                "plane": plane,
                "subset_size": subset_size,
                "center_turn": center,
                "sample_count": len(values),
                "sample_fraction": fmt(len(values) / max(1, spill_count)),
                "median_tune": fmt(percentile(values, 0.50)),
                "iqr_width": fmt(percentile(values, 0.75) - percentile(values, 0.25)),
                "p10_p90_width": fmt(percentile(values, 0.90) - percentile(values, 0.10)),
                "peak_bin_fraction": fmt(max(counts) / max(1, len(values))),
                "density_entropy": fmt(entropy),
                "median_selected_confidence": fmt(percentile(confidences, 0.50)),
                "global_fallback_fraction": fmt(mean([float(truthy(row.get("used_global_fallback"))) for row in center_group])),
                "suspicious_step_fraction": fmt(mean([float(truthy(row.get("suspicious_step"))) for row in center_group])),
                "median_abs_step_delta": fmt(percentile(step_deltas, 0.50)),
            }
        )
    return out


def ridge_metrics(
    rows: Sequence[dict[str, object]],
    plane: str,
    subset_size: str,
    band: tuple[float, float],
    tune_bins: int,
    spill_count: int,
) -> dict[str, object]:
    grouped: dict[int, list[float]] = defaultdict(list)
    all_values: list[float] = []
    for row in rows:
        if row.get("plane") != plane:
            continue
        value = f(row.get("selected_tune"))
        if not math.isfinite(value) or value < band[0] or value > band[1]:
            continue
        center = int(row.get("center_turn", 0))
        grouped[center].append(value)
        all_values.append(value)
    iqr_widths: list[float] = []
    p80_widths: list[float] = []
    peak_fracs: list[float] = []
    for values in grouped.values():
        if not values:
            continue
        iqr_widths.append(percentile(values, 0.75) - percentile(values, 0.25))
        p80_widths.append(percentile(values, 0.90) - percentile(values, 0.10))
        counts = [0 for _ in range(tune_bins)]
        for value in values:
            idx = int((value - band[0]) / (band[1] - band[0]) * tune_bins)
            idx = max(0, min(tune_bins - 1, idx))
            counts[idx] += 1
        peak_fracs.append(max(counts) / max(1, len(values)))
    return {
        "plane": plane,
        "subset_size": subset_size,
        "spill_count": spill_count,
        "ridge_points": len(all_values),
        "valid_center_count": len(grouped),
        "median_iqr_width": fmt(percentile(iqr_widths, 0.50)),
        "median_p10_p90_width": fmt(percentile(p80_widths, 0.50)),
        "median_peak_bin_fraction": fmt(percentile(peak_fracs, 0.50)),
        "median_tune": fmt(percentile(all_values, 0.50)),
        "tune_std": fmt(stdev(all_values)),
    }


def draw_comparison(path: Path, plane: str, rows: Sequence[dict[str, object]], subset_sizes: Sequence[str]) -> None:
    subset_rows = [row for row in rows if row.get("plane") == plane and str(row.get("subset_size")) in set(subset_sizes)]
    subset_rows.sort(key=lambda row: int(str(row["subset_size"])))
    if not subset_rows:
        poster.no_data_png(path, f"RIDGE DENSITY {plane}", "NO METRICS")
        return
    width, height = 1400, 820
    pixels = poster.new_canvas(width, height)
    poster.draw_text(pixels, width, height, 34, 28, f"RIDGE DENSITY CONTRAST {plane}", poster.INK, 3)
    poster.draw_text(
        pixels,
        width,
        height,
        95,
        68,
        "LEFT: LOWER IS TIGHTER   RIGHT: HIGHER IS TIGHTER   DESCRIPTIVE ACROSS TURN CENTERS",
        poster.MUTED,
        2,
    )

    panels = (
        ((95, 125, 675, 720), "RIDGE IQR WIDTH", "median_iqr_width", poster.BLUE),
        ((770, 125, 1350, 720), "PEAK BIN FRACTION", "median_peak_bin_fraction", poster.ORANGE),
    )
    for (x0, y0, x1, y1), title, field, color in panels:
        values = [f(row.get(field)) for row in subset_rows]
        finite = [value for value in values if math.isfinite(value)]
        if not finite:
            continue
        ymin, ymax = min(finite), max(finite)
        pad = 0.10 * (ymax - ymin) or max(1e-6, 0.05 * abs(ymax))
        ymin = max(0.0, ymin - pad)
        ymax += pad
        poster.draw_text(pixels, width, height, x0, y0 - 30, title, color, 2)
        poster.rect(pixels, width, height, x0, y0, x1, y1, (245, 247, 248))
        for tick in range(6):
            x = x0 + int((x1 - x0) * tick / 5)
            y = y0 + int((y1 - y0) * tick / 5)
            poster.line(pixels, width, height, x, y0, x, y1, poster.GRID)
            poster.line(pixels, width, height, x0, y, x1, y, poster.GRID)
        poster.line(pixels, width, height, x0, y1, x1, y1, poster.INK)
        poster.line(pixels, width, height, x0, y0, x0, y1, poster.INK)
        label_values = ((ymax, y0 - 7), ((ymin + ymax) / 2.0, (y0 + y1) // 2 - 7), (ymin, y1 - 7))
        for value, y in label_values:
            label = poster.format_axis_value(value, ymax - ymin)
            poster.draw_text(pixels, width, height, max(2, x0 - len(label) * 8 - 7), y, label, poster.MUTED, 2)
        points: list[tuple[int, int]] = []
        for index, (row, value) in enumerate(zip(subset_rows, values)):
            fraction = index / max(1, len(subset_rows) - 1)
            x = x0 + int(round((x1 - x0) * fraction))
            y = poster.scale_value(value, ymin, ymax, y1, y0)
            points.append((x, y))
            poster.rect(pixels, width, height, x - 4, y - 4, x + 4, y + 4, color)
            label = str(row["subset_size"])
            poster.draw_text(pixels, width, height, x - len(label) * 4, y1 + 10, label, poster.MUTED, 2)
        for (xa, ya), (xb, yb) in zip(points, points[1:]):
            poster.line(pixels, width, height, xa, ya, xb, yb, color)
            poster.line(pixels, width, height, xa, ya + 1, xb, yb + 1, color)
        poster.draw_text(pixels, width, height, (x0 + x1) // 2 - 52, y1 + 38, "SUBSET SIZE N", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def diverging_color(frac: float) -> tuple[int, int, int]:
    frac = max(-1.0, min(1.0, frac))
    if frac >= 0.0:
        t = frac
        return (
            int(248 + (190 - 248) * t),
            int(248 + (72 - 248) * t),
            int(246 + (72 - 246) * t),
        )
    t = -frac
    return (
        int(248 + (44 - 248) * t),
        int(248 + (123 - 248) * t),
        int(246 + (182 - 246) * t),
    )


def aligned_density(result: dict[str, object], centers: Sequence[int]) -> np.ndarray:
    source_centers = list(result["centers"])
    source_density = normalized_columns(result["density"])
    if source_density.size == 0:
        return np.zeros((0, len(centers)), dtype=np.float32)
    source_index = {center: idx for idx, center in enumerate(source_centers)}
    out = np.zeros((source_density.shape[0], len(centers)), dtype=np.float32)
    for col, center in enumerate(centers):
        src = source_index.get(center)
        if src is not None:
            out[:, col] = source_density[:, src]
    return out


def draw_panel_axes(
    pixels: bytearray,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
) -> None:
    x0, y0, x1, y1 = area
    poster.rect(pixels, width, height, x0, y0, x1, y1, (245, 247, 248))
    for tick in range(6):
        x = x0 + int((x1 - x0) * tick / 5)
        y = y0 + int((y1 - y0) * tick / 5)
        poster.line(pixels, width, height, x, y0, x, y1, poster.GRID)
        poster.line(pixels, width, height, x0, y, x1, y, poster.GRID)
    poster.line(pixels, width, height, x0, y1, x1, y1, poster.INK)
    poster.line(pixels, width, height, x0, y0, x0, y1, poster.INK)


def draw_density_in_area(
    pixels: bytearray,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
    density: np.ndarray,
    vmax: float,
) -> None:
    x0, y0, x1, y1 = area
    if density.size == 0:
        return
    columns = density.shape[1]
    rows = density.shape[0]
    for col in range(columns):
        cx0, cx1 = raster_cell_bounds(col, columns, x0, x1)
        for row in range(rows):
            value = float(density[row, col])
            cy0, cy1 = raster_cell_bounds(row, rows, y0, y1, reverse=True)
            color = color_ramp(value / vmax) if value > 0 else (245, 247, 248)
            poster.rect(pixels, width, height, cx0, cy0, cx1, cy1, color)


def draw_legacy_pair(
    path: Path,
    plane: str,
    subset_size: str,
    legacy: dict[str, object],
    ensemble: dict[str, object],
    band: tuple[float, float],
) -> None:
    centers = sorted(set(legacy["centers"]) | set(ensemble["centers"]))
    if not centers:
        poster.no_data_png(path, f"LEGACY VS BEST{subset_size} {plane}", "NO COMMON DENSITY")
        return
    legacy_density = aligned_density(legacy, centers)
    ensemble_density = aligned_density(ensemble, centers)
    positive = np.concatenate((legacy_density[legacy_density > 0], ensemble_density[ensemble_density > 0]))
    vmax = float(np.percentile(positive, 98)) if positive.size else 1.0
    vmax = max(vmax, 1e-6)
    width, height = 1800, 900
    pixels = poster.new_canvas(width, height)
    poster.draw_text(pixels, width, height, 34, 28, f"LEGACY NORMALIZED-SINGLE VS ADAPTIVE BEST{subset_size} {plane}", poster.INK, 3)
    common = len(set(legacy.get("spill_keys", set())) & set(ensemble.get("spill_keys", set())))
    common_points = len(set(legacy.get("point_keys", set())) & set(ensemble.get("point_keys", set())))
    poster.draw_text(
        pixels,
        width,
        height,
        90,
        72,
        f"PAIRED {common_points} RIDGES / {common} SPILLS, COLUMN NORMALIZED, SHARED SCALE, WHITE P10 MED P90",
        poster.MUTED,
        2,
    )
    left = (90, 125, 855, 790)
    right = (945, 125, 1710, 790)
    for area in (left, right):
        draw_panel_axes(pixels, width, height, area)
    draw_density_in_area(pixels, width, height, left, legacy_density, vmax)
    draw_density_in_area(pixels, width, height, right, ensemble_density, vmax)
    x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
    for result, area in ((legacy, left), (ensemble, right)):
        draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.10), x_range, band, area, (255, 255, 255), 1)
        draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.90), x_range, band, area, (255, 255, 255), 1)
        draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.50), x_range, band, area, (255, 255, 255), 3)
    poster.draw_text(pixels, width, height, 90, 99, "LEGACY NORMALIZED-SINGLE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 945, 99, f"ADAPTIVE BEST{subset_size} POWER ENSEMBLE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 18, 440, "TUNE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 834, 835, "TURN", poster.MUTED, 2)
    for area in (left, right):
        x0, y0, x1, y1 = area
        poster.draw_text(pixels, width, height, x0, y0 - 24, f"{band[1]:.3f}", poster.MUTED, 2)
        poster.draw_text(pixels, width, height, x0, y1 + 8, f"{band[0]:.3f}", poster.MUTED, 2)
        poster.draw_text(pixels, width, height, x0, y1 + 32, str(min(centers)), poster.MUTED, 2)
        poster.draw_text(pixels, width, height, x1 - 95, y1 + 32, str(max(centers)), poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def draw_legacy_pair_hv(
    path: Path,
    subset_size: str,
    paired: dict[str, tuple[dict[str, object], dict[str, object], tuple[float, float]]],
) -> None:
    draw_legacy_pair_hv_selected(
        path,
        {"H": str(subset_size), "V": str(subset_size)},
        paired,
    )


def draw_legacy_pair_hv_selected(
    path: Path,
    subset_sizes: dict[str, str],
    paired: dict[str, tuple[dict[str, object], dict[str, object], tuple[float, float]]],
) -> None:
    required = {"H", "V"}
    if set(paired) != required or set(subset_sizes) != required:
        poster.no_data_png(path, "LEGACY VS PLANE-SELECTED BEST-N H/V", "MISSING PLANE")
        return

    h_size = str(subset_sizes["H"])
    v_size = str(subset_sizes["V"])
    selected_label = f"BEST{h_size}" if h_size == v_size else f"H BEST{h_size} / V BEST{v_size}"

    aligned: dict[str, tuple[list[int], np.ndarray, np.ndarray]] = {}
    positives: list[np.ndarray] = []
    for plane in ("H", "V"):
        legacy, ensemble, _band = paired[plane]
        centers = sorted(set(legacy["centers"]) | set(ensemble["centers"]))
        if not centers:
            poster.no_data_png(path, f"LEGACY VS {selected_label} H/V", f"NO COMMON {plane} DENSITY")
            return
        old_density = aligned_density(legacy, centers)
        new_density = aligned_density(ensemble, centers)
        aligned[plane] = (centers, old_density, new_density)
        positives.extend((old_density[old_density > 0], new_density[new_density > 0]))
    positive = np.concatenate([values for values in positives if values.size]) if any(values.size for values in positives) else np.empty(0)
    vmax = max(float(np.percentile(positive, 98)) if positive.size else 1.0, 1e-6)

    width, height = 2400, 900
    pixels = poster.new_canvas(width, height)
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        24,
        f"FULL-SPILL RIDGE DENSITY: LEGACY VS ADAPTIVE {selected_label}",
        poster.INK,
        3,
    )
    poster.draw_text(
        pixels,
        width,
        height,
        110,
        68,
        "ROWS H/V; COLOR: COLUMN PICK PROBABILITY, SHARED P98 CLIP; WHITE: P10 MED P90",
        poster.MUTED,
        2,
    )
    poster.draw_text(pixels, width, height, 110, 96, "LEGACY NORMALIZED-SINGLE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 1260, 96, f"ADAPTIVE {selected_label} POWER ENSEMBLE", poster.MUTED, 2)

    areas = {
        ("H", "legacy"): (110, 140, 1160, 400),
        ("H", "ensemble"): (1260, 140, 2310, 400),
        ("V", "legacy"): (110, 500, 1160, 760),
        ("V", "ensemble"): (1260, 500, 2310, 760),
    }
    for area in areas.values():
        draw_panel_axes(pixels, width, height, area)

    for plane in ("H", "V"):
        legacy, ensemble, band = paired[plane]
        centers, old_density, new_density = aligned[plane]
        left = areas[(plane, "legacy")]
        right = areas[(plane, "ensemble")]
        draw_density_in_area(pixels, width, height, left, old_density, vmax)
        draw_density_in_area(pixels, width, height, right, new_density, vmax)
        x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
        for result, area in ((legacy, left), (ensemble, right)):
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.10), x_range, band, area, poster.WHITE, 1)
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.90), x_range, band, area, poster.WHITE, 1)
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.50), x_range, band, area, poster.WHITE, 3)
            x0, y0, x1, y1 = area
            poster.draw_text(pixels, width, height, x0, y0 - 24, f"{band[1]:.3f}", poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x0, y1 + 7, f"{band[0]:.3f}", poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x0, y1 + 31, str(min(centers)), poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x1 - 95, y1 + 31, str(max(centers)), poster.MUTED, 2)
        common_spills = len(set(legacy.get("spill_keys", set())) & set(ensemble.get("spill_keys", set())))
        common_points = len(set(legacy.get("point_keys", set())) & set(ensemble.get("point_keys", set())))
        row_y = 250 if plane == "H" else 610
        poster.draw_text(pixels, width, height, 22, row_y, plane, poster.INK, 4)
        poster.draw_text(
            pixels,
            width,
            height,
            110,
            450 if plane == "H" else 810,
            f"{plane} PAIRED: {common_points} RIDGE PICKS / {common_spills} SPILLS",
            poster.MUTED,
            2,
        )
    poster.draw_text(pixels, width, height, 22, 430, "TUNE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 1130, height - 34, "TURN", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def draw_paired_density_grid_hv(
    path: Path,
    title: str,
    columns: Sequence[tuple[str, str]],
    paired: Mapping[str, tuple[Mapping[str, dict[str, object]], tuple[float, float]]],
) -> None:
    required_planes = {"H", "V"}
    column_keys = [key for key, _label in columns]
    if set(paired) != required_planes or len(columns) < 2:
        poster.no_data_png(path, title, "MISSING PLANE OR METHOD")
        return
    if any(set(results) != set(column_keys) for results, _band in paired.values()):
        poster.no_data_png(path, title, "MISSING PAIRED METHOD")
        return

    aligned: dict[tuple[str, str], tuple[list[int], np.ndarray]] = {}
    positives: list[np.ndarray] = []
    for plane in ("H", "V"):
        results, _band = paired[plane]
        centers = sorted({center for result in results.values() for center in result["centers"]})
        if not centers:
            poster.no_data_png(path, title, f"NO COMMON {plane} DENSITY")
            return
        for key in column_keys:
            density = aligned_density(results[key], centers)
            aligned[(plane, key)] = (centers, density)
            if np.any(density > 0):
                positives.append(density[density > 0])
    positive = np.concatenate(positives) if positives else np.empty(0)
    vmax = max(float(np.percentile(positive, 98)) if positive.size else 1.0, 1e-6)

    width = 2400 if len(columns) == 2 else 3000
    height = 900
    left_margin = 110
    right_margin = 90
    gap = 80
    panel_width = (width - left_margin - right_margin - gap * (len(columns) - 1)) // len(columns)
    pixels = poster.new_canvas(width, height)
    poster.draw_text(pixels, width, height, 34, 24, title, poster.INK, 3)
    poster.draw_text(
        pixels,
        width,
        height,
        left_margin,
        68,
        "ROWS H/V; EXACT COMMON POINTS; COLUMN PICK PROBABILITY; SHARED P98 CLIP; WHITE P10 MED P90",
        poster.MUTED,
        2,
    )

    areas: dict[tuple[str, str], tuple[int, int, int, int]] = {}
    for column_index, (key, label) in enumerate(columns):
        x0 = left_margin + column_index * (panel_width + gap)
        x1 = x0 + panel_width
        poster.draw_text(pixels, width, height, x0, 96, label, poster.MUTED, 2)
        areas[("H", key)] = (x0, 140, x1, 400)
        areas[("V", key)] = (x0, 500, x1, 760)
    for area in areas.values():
        draw_panel_axes(pixels, width, height, area)

    for plane in ("H", "V"):
        results, band = paired[plane]
        for key in column_keys:
            centers, density = aligned[(plane, key)]
            result = results[key]
            area = areas[(plane, key)]
            draw_density_in_area(pixels, width, height, area, density, vmax)
            x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.10), x_range, band, area, poster.WHITE, 1)
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.90), x_range, band, area, poster.WHITE, 1)
            draw_scaled_polyline(pixels, width, height, median_points(result["grouped"], 0.50), x_range, band, area, poster.WHITE, 3)
            x0, y0, x1, y1 = area
            poster.draw_text(pixels, width, height, x0, y0 - 24, f"{band[1]:.3f}", poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x0, y1 + 7, f"{band[0]:.3f}", poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x0, y1 + 31, str(min(centers)), poster.MUTED, 2)
            poster.draw_text(pixels, width, height, x1 - 95, y1 + 31, str(max(centers)), poster.MUTED, 2)
        point_sets = [set(results[key].get("point_keys", set())) for key in column_keys]
        spill_sets = [set(results[key].get("spill_keys", set())) for key in column_keys]
        common_points = len(set.intersection(*point_sets)) if point_sets else 0
        common_spills = len(set.intersection(*spill_sets)) if spill_sets else 0
        poster.draw_text(pixels, width, height, 22, 250 if plane == "H" else 610, plane, poster.INK, 4)
        poster.draw_text(
            pixels,
            width,
            height,
            left_margin,
            450 if plane == "H" else 810,
            f"{plane} PAIRED: {common_points} RIDGE PICKS / {common_spills} SPILLS",
            poster.MUTED,
            2,
        )
    poster.draw_text(pixels, width, height, 22, 430, "TUNE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, width // 2 - 40, height - 34, "TURN", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def distribution_metrics(values: Sequence[float], band: tuple[float, float], tune_bins: int) -> dict[str, float]:
    clean = [value for value in values if math.isfinite(value) and band[0] <= value <= band[1]]
    if not clean:
        return {"iqr": math.nan, "p80": math.nan, "peak": math.nan, "entropy": math.nan}
    counts = np.zeros(tune_bins, dtype=np.float64)
    for value in clean:
        idx = int((value - band[0]) / (band[1] - band[0]) * tune_bins)
        counts[max(0, min(tune_bins - 1, idx))] += 1.0
    probability = counts / max(1.0, float(np.sum(counts)))
    positive = probability[probability > 0]
    entropy = -float(np.sum(positive * np.log(positive))) / math.log(max(2, tune_bins))
    return {
        "iqr": percentile(clean, 0.75) - percentile(clean, 0.25),
        "p80": percentile(clean, 0.90) - percentile(clean, 0.10),
        "peak": float(np.max(probability)),
        "entropy": entropy,
    }


def legacy_comparison_by_turn_rows(
    plane: str,
    subset_size: str,
    legacy: dict[str, object],
    ensemble: dict[str, object],
    band: tuple[float, float],
    tune_bins: int,
    ridge_half_width: float = 0.0025,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    centers = sorted(set(legacy["grouped"]) & set(ensemble["grouped"]))
    for center in centers:
        legacy_values = legacy["grouped"][center]
        ensemble_values = ensemble["grouped"][center]
        old = distribution_metrics(legacy_values, band, tune_bins)
        new = distribution_metrics(ensemble_values, band, tune_bins)
        shared_center = percentile([*legacy_values, *ensemble_values], 0.50)
        old_mass = mean([float(abs(value - shared_center) <= ridge_half_width) for value in legacy_values])
        new_mass = mean([float(abs(value - shared_center) <= ridge_half_width) for value in ensemble_values])
        rows.append(
            {
                "plane": plane,
                "subset_size": subset_size,
                "center_turn": int(center),
                "paired_ridge_count": len(legacy_values),
                "shared_ridge_center": fmt(shared_center),
                "legacy_iqr_width": fmt(old["iqr"]),
                "ensemble_iqr_width": fmt(new["iqr"]),
                "iqr_delta_ensemble_minus_legacy": fmt(new["iqr"] - old["iqr"]),
                "legacy_p10_p90_width": fmt(old["p80"]),
                "ensemble_p10_p90_width": fmt(new["p80"]),
                "p10_p90_delta_ensemble_minus_legacy": fmt(new["p80"] - old["p80"]),
                "legacy_peak_bin_fraction": fmt(old["peak"]),
                "ensemble_peak_bin_fraction": fmt(new["peak"]),
                "peak_bin_fraction_gain": fmt(new["peak"] - old["peak"]),
                "legacy_density_entropy": fmt(old["entropy"]),
                "ensemble_density_entropy": fmt(new["entropy"]),
                "density_entropy_delta": fmt(new["entropy"] - old["entropy"]),
                "legacy_shared_ridge_mass": fmt(old_mass),
                "ensemble_shared_ridge_mass": fmt(new_mass),
                "shared_ridge_mass_gain": fmt(new_mass - old_mass),
            }
        )
    return rows


def legacy_comparison_metrics(
    plane: str,
    subset_size: str,
    legacy: dict[str, object],
    ensemble: dict[str, object],
    band: tuple[float, float],
    tune_bins: int,
    ridge_half_width: float = 0.0025,
    turn_block_windows: int = 16,
    bootstrap_samples: int = 500,
) -> dict[str, object]:
    centers = sorted(set(legacy["grouped"]) & set(ensemble["grouped"]))
    rows: list[tuple[dict[str, float], dict[str, float], float]] = []
    for center in centers:
        legacy_values = legacy["grouped"][center]
        ensemble_values = ensemble["grouped"][center]
        old_metrics = distribution_metrics(legacy_values, band, tune_bins)
        new_metrics = distribution_metrics(ensemble_values, band, tune_bins)
        shared_center = percentile([*legacy_values, *ensemble_values], 0.50)
        old_mass = mean([float(abs(value - shared_center) <= ridge_half_width) for value in legacy_values])
        new_mass = mean([float(abs(value - shared_center) <= ridge_half_width) for value in ensemble_values])
        rows.append((old_metrics, new_metrics, new_mass - old_mass))
    old_iqr = [row[0]["iqr"] for row in rows]
    new_iqr = [row[1]["iqr"] for row in rows]
    old_p80 = [row[0]["p80"] for row in rows]
    new_p80 = [row[1]["p80"] for row in rows]
    old_peak = [row[0]["peak"] for row in rows]
    new_peak = [row[1]["peak"] for row in rows]
    old_entropy = [row[0]["entropy"] for row in rows]
    new_entropy = [row[1]["entropy"] for row in rows]
    iqr_delta = [new - old for old, new in zip(old_iqr, new_iqr)]
    peak_gain = [new - old for old, new in zip(old_peak, new_peak)]
    entropy_delta = [new - old for old, new in zip(old_entropy, new_entropy)]
    mass_gain = [row[2] for row in rows]
    interval_inputs = {
        "iqr": iqr_delta,
        "peak": peak_gain,
        "entropy": entropy_delta,
        "mass": mass_gain,
    }
    intervals = {
        name: moving_block_interval(
            values,
            bootstrap_samples,
            turn_block_windows,
            stable_seed("ridge-legacy-turn-block", plane, subset_size, name),
        )
        for name, values in interval_inputs.items()
    }
    return {
        "plane": plane,
        "subset_size": subset_size,
        "common_spill_count": len(set(legacy.get("spill_keys", set())) & set(ensemble.get("spill_keys", set()))),
        "common_ridge_point_count": len(set(legacy.get("point_keys", set())) & set(ensemble.get("point_keys", set()))),
        "common_center_count": len(centers),
        "legacy_median_iqr_width": fmt(percentile(old_iqr, 0.50)),
        "ensemble_median_iqr_width": fmt(percentile(new_iqr, 0.50)),
        "median_iqr_delta_ensemble_minus_legacy": fmt(percentile(iqr_delta, 0.50)),
        "median_iqr_delta_ci_low": fmt(intervals["iqr"][0]),
        "median_iqr_delta_ci_high": fmt(intervals["iqr"][1]),
        "fraction_centers_with_narrower_iqr": fmt(mean([float(new < old) for old, new in zip(old_iqr, new_iqr)])),
        "legacy_median_p10_p90_width": fmt(percentile(old_p80, 0.50)),
        "ensemble_median_p10_p90_width": fmt(percentile(new_p80, 0.50)),
        "legacy_median_peak_bin_fraction": fmt(percentile(old_peak, 0.50)),
        "ensemble_median_peak_bin_fraction": fmt(percentile(new_peak, 0.50)),
        "median_peak_bin_fraction_gain": fmt(percentile(peak_gain, 0.50)),
        "median_peak_bin_fraction_gain_ci_low": fmt(intervals["peak"][0]),
        "median_peak_bin_fraction_gain_ci_high": fmt(intervals["peak"][1]),
        "legacy_median_density_entropy": fmt(percentile(old_entropy, 0.50)),
        "ensemble_median_density_entropy": fmt(percentile(new_entropy, 0.50)),
        "median_density_entropy_delta": fmt(percentile(entropy_delta, 0.50)),
        "median_density_entropy_delta_ci_low": fmt(intervals["entropy"][0]),
        "median_density_entropy_delta_ci_high": fmt(intervals["entropy"][1]),
        "median_shared_ridge_mass_gain": fmt(percentile(mass_gain, 0.50)),
        "median_shared_ridge_mass_gain_ci_low": fmt(intervals["mass"][0]),
        "median_shared_ridge_mass_gain_ci_high": fmt(intervals["mass"][1]),
        "turn_block_windows": turn_block_windows,
        "turn_block_bootstrap_samples": bootstrap_samples,
    }


def draw_density_difference(
    path: Path,
    plane: str,
    baseline_size: str,
    ensemble_size: str,
    baseline: dict[str, object],
    ensemble: dict[str, object],
    band: tuple[float, float],
    args: argparse.Namespace,
) -> None:
    centers = sorted(set(baseline["centers"]) | set(ensemble["centers"]))
    if not centers:
        poster.no_data_png(path, f"BEST{ensemble_size} MINUS BEST{baseline_size} {plane}", "NO DENSITY")
        return
    base_density = aligned_density(baseline, centers)
    ensemble_density = aligned_density(ensemble, centers)
    if base_density.size == 0 or ensemble_density.size == 0:
        poster.no_data_png(path, f"BEST{ensemble_size} MINUS BEST{baseline_size} {plane}", "NO DENSITY")
        return
    diff = ensemble_density - base_density
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    title = (
        f"ADAPTIVE BEST{ensemble_size} MINUS LEGACY NORMALIZED-SINGLE {plane}"
        if baseline_size == "LEGACY"
        else f"RIDGE DENSITY BEST{ensemble_size}-BEST{baseline_size} {plane}"
    )
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    finite_vals = np.abs(diff[np.isfinite(diff)])
    vmax = float(np.percentile(finite_vals, 99)) if finite_vals.size else 1.0
    vmax = max(vmax, 1e-6)
    for col in range(len(centers)):
        cx0, cx1 = raster_cell_bounds(col, len(centers), x0, x1)
        for row_idx in range(diff.shape[0]):
            value = float(diff[row_idx, col])
            color = diverging_color(value / vmax)
            cy0, cy1 = raster_cell_bounds(row_idx, diff.shape[0], y0, y1, reverse=True)
            poster.rect(pixels, width, height, cx0, cy0, cx1, cy1, color)
    area = (x0, y0, x1, y1)
    if args.mark_extraction_context:
        overlay_extraction_context(
            pixels,
            width,
            height,
            area,
            centers,
            args.extraction_range_start_turn,
            args.extraction_range_end_turn,
        )
    x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
    draw_scaled_polyline(
        pixels,
        width,
        height,
        median_points(ensemble["grouped"], 0.50),
        x_range,
        band,
        area,
        (255, 255, 255),
        3,
    )
    draw_scaled_polyline(
        pixels,
        width,
        height,
        median_points(baseline["grouped"], 0.50),
        x_range,
        band,
        area,
        poster.INK,
        2,
    )
    poster.draw_text(pixels, width, height, x0, y1 + 8, str(min(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 110, y1 + 8, str(max(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 22, y0 - 8, f"{band[1]:.3f}", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, 22, y1 - 10, f"{band[0]:.3f}", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x0, y0 - 28, "RED: HIGHER PICK PROBABILITY  BLUE: LOWER VS BASE", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 330, y0 - 28, "WHITE: ENSEMBLE MED, DARK: BASE MED", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def smooth_pairs(points: Sequence[tuple[float, float]], radius: int = 2) -> list[tuple[float, float]]:
    clean = [(x, y) for x, y in points if math.isfinite(y)]
    out: list[tuple[float, float]] = []
    for idx, (x, _y) in enumerate(clean):
        lo = max(0, idx - radius)
        hi = min(len(clean), idx + radius + 1)
        vals = [value for _turn, value in clean[lo:hi]]
        out.append((x, sum(vals) / len(vals)))
    return out


def subset_color_map(subset_sizes: Sequence[str]) -> dict[str, tuple[int, int, int]]:
    return {
        size: SERIES_COLORS[index % len(SERIES_COLORS)]
        for index, size in enumerate(subset_sizes)
    }


def draw_subset_legend(
    pixels: bytearray,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
    subset_sizes: Sequence[str],
    colors: dict[str, tuple[int, int, int]],
) -> None:
    x0, y0, x1, _y1 = area
    columns = 2 if len(subset_sizes) > 5 else 1
    rows = int(math.ceil(len(subset_sizes) / columns))
    column_width = 138
    legend_width = columns * column_width + 12
    legend_x = max(x0 + 10, x1 - legend_width - 10)
    legend_y = y0 + 10
    poster.rect(
        pixels,
        width,
        height,
        legend_x - 8,
        legend_y - 7,
        x1 - 6,
        legend_y + rows * 23 + 3,
        (245, 247, 248),
    )
    for index, size in enumerate(subset_sizes):
        column = index // rows
        row = index % rows
        x = legend_x + column * column_width
        y = legend_y + row * 23
        poster.rect(pixels, width, height, x, y, x + 14, y + 14, colors[size])
        poster.draw_text(pixels, width, height, x + 22, y, f"BEST{size}", poster.MUTED, 2)


def draw_concentration_plot(
    path: Path,
    plane: str,
    center_rows: Sequence[dict[str, object]],
    subset_sizes: Sequence[str],
    args: argparse.Namespace,
) -> None:
    rows = [row for row in center_rows if row.get("plane") == plane]
    if not rows:
        poster.no_data_png(path, f"RIDGE CONCENTRATION {plane}", "NO CENTER METRICS")
        return
    width, height = 1400, 780
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, f"RIDGE CONCENTRATION {plane}", "TURN", "PEAK BIN FRACTION")
    xs = [f(row.get("center_turn")) for row in rows]
    xmin, xmax = min(xs), max(xs)
    ymax = max([f(row.get("peak_bin_fraction")) for row in rows if math.isfinite(f(row.get("peak_bin_fraction")))] + [0.05])
    ymax = min(1.0, max(0.05, ymax * 1.08))
    area = (x0, y0, x1, y1)
    if args.mark_extraction_context:
        overlay_extraction_context(pixels, width, height, area, [int(x) for x in xs], args.extraction_range_start_turn, args.extraction_range_end_turn)
    colors = subset_color_map(subset_sizes)
    for size in subset_sizes:
        pts = [
            (f(row.get("center_turn")), f(row.get("peak_bin_fraction")))
            for row in rows
            if str(row.get("subset_size")) == size
        ]
        pts.sort()
        draw_scaled_polyline(pixels, width, height, smooth_pairs(pts), (xmin, xmax), (0.0, ymax), area, colors[size], 3)
    draw_subset_legend(pixels, width, height, area, subset_sizes, colors)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        area,
        (xmin, xmax),
        (0.0, ymax),
        x_ticks=2,
    )
    poster.draw_text(pixels, width, height, x0, y0 - 28, f"SMOOTHED 5-WINDOW PEAK FRACTION; HIGHER IS NARROWER {plane} DENSITY", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def draw_turn_metric_plot(
    path: Path,
    plane: str,
    center_rows: Sequence[dict[str, object]],
    subset_sizes: Sequence[str],
    metric: str,
    title: str,
    y_label: str,
    fraction_scale: bool = False,
    zero_reference: bool = False,
) -> None:
    rows = [row for row in center_rows if row.get("plane") == plane]
    values = [f(row.get(metric)) for row in rows]
    finite = [value for value in values if math.isfinite(value)]
    if not rows or not finite:
        poster.no_data_png(path, f"{title} {plane}", "NO CENTER METRICS")
        return
    width, height = 1400, 780
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, f"{title} {plane}", "TURN", y_label)
    xs = [f(row.get("center_turn")) for row in rows]
    xmin, xmax = min(xs), max(xs)
    if fraction_scale:
        ymin, ymax = 0.0, 1.0
    else:
        ymin = min(0.0, min(finite))
        ymax = max(0.0, max(finite)) if zero_reference else max(finite)
        if ymax <= ymin:
            ymax = ymin + 1.0
        else:
            ymax = ymin + 1.10 * (ymax - ymin)
    area = (x0, y0, x1, y1)
    colors = subset_color_map(subset_sizes)
    for size in subset_sizes:
        points = [
            (f(row.get("center_turn")), f(row.get(metric)))
            for row in rows
            if str(row.get("subset_size")) == size
        ]
        points.sort()
        draw_scaled_polyline(pixels, width, height, smooth_pairs(points), (xmin, xmax), (ymin, ymax), area, colors[size], 3)
    if zero_reference and ymin < 0.0 < ymax:
        zero_y = poster.scale_value(0.0, ymin, ymax, y1, y0)
        poster.line(pixels, width, height, x0, zero_y, x1, zero_y, poster.MUTED)
    draw_subset_legend(pixels, width, height, area, subset_sizes, colors)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        area,
        (xmin, xmax),
        (ymin, ymax),
        x_ticks=2,
    )
    poster.draw_text(pixels, width, height, x0, y0 - 28, "SMOOTHED 5-WINDOW DESCRIPTIVE DIAGNOSTIC", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def draw_selected_turn_contrast_hv(
    path: Path,
    center_rows: Sequence[dict[str, object]],
    selected_sizes: Mapping[str, str],
    metric: str,
    title: str,
    y_label: str,
    portrait: bool = False,
) -> None:
    selected_rows = {
        plane: [
            row
            for row in center_rows
            if row.get("plane") == plane
            and str(row.get("subset_size")) == str(selected_sizes[plane])
        ]
        for plane in ("H", "V")
    }
    finite = [
        f(row.get(metric))
        for rows in selected_rows.values()
        for row in rows
        if math.isfinite(f(row.get(metric)))
    ]
    if not finite or any(not selected_rows[plane] for plane in ("H", "V")):
        poster.no_data_png(path, title, "NO SELECTED H/V TURN CONTRAST")
        return
    turns = [f(row.get("center_turn")) for rows in selected_rows.values() for row in rows]
    xmin, xmax = min(turns), max(turns)
    raw_min, raw_max = min(0.0, min(finite)), max(0.0, max(finite))
    span = raw_max - raw_min
    if span <= 0.0:
        span = max(1e-6, abs(raw_min), abs(raw_max), 1.0)
    pad = 0.08 * span
    ymin, ymax = raw_min - pad, raw_max + pad

    if portrait:
        width, height = 800, 1250
        title_position = (20, 18)
        subtitle_position = (65, 50)
        panels = {"H": (100, 115, 770, 555), "V": (100, 680, 770, 1120)}
        turn_position = (375, 1205)
    else:
        width, height = 1000, 625
        title_position = (24, 12)
        subtitle_position = (105, 42)
        panels = {"H": (105, 98, 970, 280), "V": (105, 355, 970, 537)}
        turn_position = (475, 590)
    pixels = poster.new_canvas(width, height)
    poster.draw_text(
        pixels,
        width,
        height,
        title_position[0],
        title_position[1],
        title,
        poster.INK,
        3,
    )
    poster.draw_text(
        pixels,
        width,
        height,
        subtitle_position[0],
        subtitle_position[1],
        "EXACT-PAIRED; SHARED Y SCALE; CURVES SMOOTHED 5 WINDOWS; ZERO = NO METHOD DIFFERENCE",
        poster.MUTED,
        2,
    )
    colors = {"H": poster.BLUE, "V": poster.GREEN}
    for plane in ("H", "V"):
        area = panels[plane]
        draw_panel_axes(pixels, width, height, area)
        points = sorted(
            (f(row.get("center_turn")), f(row.get(metric)))
            for row in selected_rows[plane]
        )
        draw_scaled_polyline(
            pixels,
            width,
            height,
            smooth_pairs(points),
            (xmin, xmax),
            (ymin, ymax),
            area,
            colors[plane],
            3,
        )
        x0, y0, x1, y1 = area
        zero_y = poster.scale_value(0.0, ymin, ymax, y1, y0)
        poster.line(pixels, width, height, x0, zero_y, x1, zero_y, poster.MUTED)
        x_span = xmax - xmin
        y_span = ymax - ymin
        for fraction, value in ((0.0, xmin), (1.0, xmax)):
            label = poster.format_axis_value(value, x_span)
            label_width = len(label) * 12
            x = x0 + int(round((x1 - x0) * fraction)) - label_width // 2
            x = max(2, min(width - label_width - 2, x))
            poster.draw_text(pixels, width, height, x, y1 + 7, label, poster.MUTED, 3)
        for value, y in ((ymax, y0 - 10), (ymin, y1 - 10)):
            label = poster.format_axis_value(value, y_span)
            label_width = len(label) * 12
            poster.draw_text(
                pixels,
                width,
                height,
                max(2, x0 - label_width - 8),
                y,
                label,
                poster.MUTED,
                3,
            )
        poster.draw_text(
            pixels,
            width,
            height,
            x0,
            y0 - 27,
            f"{plane} BEST{selected_sizes[plane]} | {y_label}",
            colors[plane],
            3,
        )
    poster.draw_text(
        pixels,
        width,
        height,
        turn_position[0],
        turn_position[1],
        "TURN",
        poster.MUTED,
        3,
    )
    poster.write_png(path, width, height, pixels)


def robust_change_point(points: Sequence[dict[str, object]]) -> dict[str, float]:
    rows = sorted(points, key=lambda row: f(row.get("center_turn")))
    if len(rows) < 12:
        return {}
    peak = smooth_pairs([(f(row.get("center_turn")), f(row.get("peak_bin_fraction"))) for row in rows])
    iqr = smooth_pairs([(f(row.get("center_turn")), f(row.get("iqr_width"))) for row in rows])
    sample = smooth_pairs([(f(row.get("center_turn")), f(row.get("sample_fraction"))) for row in rows])
    if not peak or len(peak) != len(iqr) or len(peak) != len(sample):
        return {}
    min_segment = max(5, min(15, len(rows) // 5))
    best: dict[str, float] = {}
    series_values = [[value for _turn, value in series] for series in (peak, iqr, sample)]
    baseline_sse = []
    for values in series_values:
        center = sum(values) / len(values)
        baseline_sse.append(sum((value - center) ** 2 for value in values))
    for split in range(min_segment, len(rows) - min_segment + 1):
        pre_peak = percentile([value for _turn, value in peak[:split]], 0.50)
        post_peak = percentile([value for _turn, value in peak[split:]], 0.50)
        pre_iqr = percentile([value for _turn, value in iqr[:split]], 0.50)
        post_iqr = percentile([value for _turn, value in iqr[split:]], 0.50)
        pre_sample = percentile([value for _turn, value in sample[:split]], 0.50)
        post_sample = percentile([value for _turn, value in sample[split:]], 0.50)
        peak_drop = (pre_peak - post_peak) / max(pre_peak, 1e-9)
        iqr_increase = (post_iqr - pre_iqr) / max(pre_iqr, 1e-9)
        sample_drop = (pre_sample - post_sample) / max(pre_sample, 1e-9)
        directional_effect = max(0.0, peak_drop) + 0.50 * min(2.0, max(0.0, iqr_increase)) + 0.25 * max(0.0, sample_drop)
        fit_gains: list[float] = []
        for values, baseline in zip(series_values, baseline_sse):
            pre_mean = sum(values[:split]) / split
            post_mean = sum(values[split:]) / (len(values) - split)
            segmented_sse = sum((value - pre_mean) ** 2 for value in values[:split])
            segmented_sse += sum((value - post_mean) ** 2 for value in values[split:])
            fit_gains.append((baseline - segmented_sse) / baseline if baseline > 1e-18 else 0.0)
        fit_gain = sum(fit_gains) / len(fit_gains)
        score = fit_gain + 0.25 * directional_effect
        if score > best.get("score", -math.inf):
            best = {
                "turn": 0.5 * (f(rows[split - 1].get("center_turn")) + f(rows[split].get("center_turn"))),
                "pre_peak": pre_peak,
                "post_peak": post_peak,
                "peak_drop": peak_drop,
                "pre_iqr": pre_iqr,
                "post_iqr": post_iqr,
                "iqr_increase": iqr_increase,
                "pre_sample": pre_sample,
                "post_sample": post_sample,
                "sample_drop": sample_drop,
                "directional_effect": directional_effect,
                "score": score,
            }
    return best if best.get("directional_effect", 0.0) >= 0.15 and best.get("score", 0.0) >= 0.15 else {}


def estimate_loss_rows(
    plane: str,
    center_rows: Sequence[dict[str, object]],
    extraction_start: int,
    extraction_end: int,
    stride_turns: int,
    subset_sizes: Sequence[str],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for size in subset_sizes:
        rows = [
            row
            for row in center_rows
            if row.get("plane") == plane and str(row.get("subset_size")) == size
        ]
        rows.sort(key=lambda row: f(row.get("center_turn")))
        points = [(f(row.get("center_turn")), f(row.get("peak_bin_fraction"))) for row in rows]
        smoothed = smooth_pairs(points, radius=2)
        change = robust_change_point(rows)
        finite_points = [(turn, value) for turn, value in smoothed if math.isfinite(turn) and math.isfinite(value)]
        if not finite_points:
            out.append(
                {
                    "plane": plane,
                    "subset_size": size,
                    "peak_concentration_turn": "",
                    "peak_concentration": "",
                    "half_peak_threshold": "",
                    "first_sustained_half_peak_loss_turn": "",
                    "most_likely_change_turn": "",
                    "pre_change_peak_fraction": "",
                    "post_change_peak_fraction": "",
                    "relative_peak_fraction_drop": "",
                    "pre_change_iqr_width": "",
                    "post_change_iqr_width": "",
                    "relative_iqr_width_increase": "",
                    "pre_change_sample_fraction": "",
                    "post_change_sample_fraction": "",
                    "relative_sample_fraction_drop": "",
                    "change_score": "",
                    "fraction_at_extraction_start": "",
                    "fraction_at_extraction_end": "",
                    "median_fraction_inside_extraction_marker": "",
                }
            )
            continue
        peak_idx, (peak_turn, peak_value) = max(enumerate(finite_points), key=lambda item: item[1][1])
        threshold = peak_value * 0.50
        first_turn = math.nan
        run = 0
        for turn, value in finite_points[peak_idx + 1 :]:
            if value < threshold:
                run += 1
                if run >= 5:
                    first_turn = turn - (run - 1) * stride_turns
                    break
            else:
                run = 0
        start_value = min(finite_points, key=lambda item: abs(item[0] - extraction_start))[1]
        end_value = min(finite_points, key=lambda item: abs(item[0] - extraction_end))[1]
        marker_values = [value for turn, value in finite_points if extraction_start <= turn <= extraction_end]
        out.append(
            {
                "plane": plane,
                "subset_size": size,
                "peak_concentration_turn": str(int(round(peak_turn))),
                "peak_concentration": fmt(peak_value),
                "half_peak_threshold": fmt(threshold),
                "first_sustained_half_peak_loss_turn": str(int(round(first_turn))) if math.isfinite(first_turn) else "",
                "most_likely_change_turn": str(int(round(change["turn"]))) if change else "",
                "pre_change_peak_fraction": fmt(change.get("pre_peak", math.nan)),
                "post_change_peak_fraction": fmt(change.get("post_peak", math.nan)),
                "relative_peak_fraction_drop": fmt(change.get("peak_drop", math.nan)),
                "pre_change_iqr_width": fmt(change.get("pre_iqr", math.nan)),
                "post_change_iqr_width": fmt(change.get("post_iqr", math.nan)),
                "relative_iqr_width_increase": fmt(change.get("iqr_increase", math.nan)),
                "pre_change_sample_fraction": fmt(change.get("pre_sample", math.nan)),
                "post_change_sample_fraction": fmt(change.get("post_sample", math.nan)),
                "relative_sample_fraction_drop": fmt(change.get("sample_drop", math.nan)),
                "change_score": fmt(change.get("score", math.nan)),
                "fraction_at_extraction_start": fmt(start_value),
                "fraction_at_extraction_end": fmt(end_value),
                "median_fraction_inside_extraction_marker": fmt(percentile(marker_values, 0.50)),
            }
        )
    return out


def caption_for_density(
    plane: str,
    subset_size: str,
    metrics: dict[str, object],
    turn_start: int,
    turn_end: int,
    window_turns: int,
    stride_turns: int,
    extraction_start: int,
    extraction_end: int,
    normalized: bool,
) -> str:
    color_quantity = "the fraction of accepted ridge picks in each turn column" if normalized else "spill count per turn/tune bin"
    return f"""# Ridge Density Best-{subset_size} {plane}

Image: `ridge_density_best{subset_size}_{plane.lower()}.png`

## What It Shows

This image bins one tracked Best-{subset_size} ensemble tune candidate per accepted spill and sliding turn window. The x-axis is window center turn over the `{turn_start}-{turn_end}` turn range, the y-axis is fractional tune, color is {color_quantity}, and white curves show per-window median and percentile envelopes. Nonzero cells above the 98th percentile are clipped only for color rendering; the exported counts and tracks are unchanged.

## How It Was Made

The completed Best-BPM membership table selected the BPMs for each spill and plane. This sidecar then reread the raw captured-spill payloads, recomputed `{window_turns}`-turn Hann spectra with `{stride_turns}`-turn stride, averaged the selected BPM spectra, and applied the same local tracking style used by the older elite ridge-density plots.

## Why It Matters

This is the closest visual comparison to the older `18d321db` favorite plots. If the adaptive method concentrates ridge picks more strongly, the density should become narrower, more coherent, or higher contrast than Best-1 or all-BPM-style baselines.

## Extraction Context Marker

The primary density image is deliberately unmarked. If requested, a separately named concentration-context figure marks turns `{extraction_start}-{extraction_end}` as a broad review hypothesis only. It is not a measured extraction boundary and is not used by the loss heuristic.

## Current Metrics

- accepted spills contributing: `{metrics.get('spill_count', '')}`
- ridge points: `{metrics.get('ridge_points', '')}`
- median tune: `{metrics.get('median_tune', '')}`
- median IQR width: `{metrics.get('median_iqr_width', '')}`
- median peak-bin fraction: `{metrics.get('median_peak_bin_fraction', '')}`

## What It Does Not Prove

This is BPM-only internal evidence. It does not validate absolute tune against Schottky or SDR references, and it reuses early-window Best-BPM memberships rather than performing a full 50k dynamic subset search.
"""


def caption_for_comparison(plane: str, subset_sizes: Sequence[str]) -> str:
    return f"""# Ridge Density Comparison {plane}

Image: `ridge_density_comparison_{plane.lower()}.png`

## What It Shows

This summary compares Best-{", Best-".join(subset_sizes)} ridge-density concentration metrics for the {plane} plane. Lower IQR width means the selected tunes are more tightly concentrated across spills at a typical window. Higher peak-bin fraction means more spills land in the dominant tune bin. The two panels use independent, explicitly labeled vertical ranges spanning their observed values; compare the trend within each panel, not the apparent slopes between panels.

## Why It Matters

It turns the visual impression of the ridge-density heatmaps into a compact numeric check. A useful adaptive-ensemble result is a reproducible reduction in width and/or increase in peak-bin concentration that reaches a plateau as N grows.

## What It Does Not Prove

The metrics summarize internal ridge concentration only. They are not external tune accuracy metrics and should be interpreted with the corresponding heatmaps.
"""


def caption_for_difference(
    plane: str,
    baseline_size: str,
    ensemble_size: str,
    common_spills: int,
    common_points: int,
) -> str:
    return f"""# Ridge Density Best-{ensemble_size} Minus Best-{baseline_size} {plane}

Image: `ridge_density_best{ensemble_size}_minus_best{baseline_size}_{plane.lower()}.png`

## What It Shows

This subtractive image compares column-normalized ridge-density distributions on exactly paired observations: `{common_points}` common spill/window ridge points from `{common_spills}` spills. Red means Best-{ensemble_size} places more ridge probability in that turn/tune bin than Best-{baseline_size}; blue means it places less. Absolute differences above the 99th percentile are clipped only for color rendering. The white line is the Best-{ensemble_size} median ridge and the dark line is the Best-{baseline_size} median ridge.

## How To Read It

A concentration pattern is blue probability away from a persistent ridge plus red probability near it. If red and blue simply trade places between unrelated tune bands, the methods are selecting different structures rather than concentrating the same one. The map contains ridge-pick probabilities, not measured noise power.

## Scope

This is a diagnostic contrast plot, not an absolute truth metric. It uses BPM-only ridge picks and should be read together with the raw density heatmaps and turn-concentration plot.
"""


def caption_for_concentration(
    plane: str,
    subset_sizes: Sequence[str],
    context_variant: bool = False,
    image_name: str | None = None,
) -> str:
    image_name = image_name or f"ridge_concentration_vs_turn_{plane.lower()}.png"
    return f"""# Ridge Concentration Vs Turn {plane}

Image: `{image_name}`

## What It Shows

This plot tracks the smoothed per-window peak-bin fraction for Best-{", Best-".join(subset_sizes)}. Higher values mean more spills concentrate in the same tune bin at that turn window; lower values mean the ridge-density image is broader or less coherent.

## Why It Matters

For the H plane, this is the safer diagnostic for finding where tracking degrades. It does not assume a fixed extraction start time.{" The orange range is a broad review hypothesis, not a measured boundary." if context_variant else " The primary figure intentionally contains no extraction marker."}
"""


def caption_for_turn_metric(
    plane: str,
    subset_sizes: Sequence[str],
    image_name: str,
    metric_name: str,
    interpretation: str,
) -> str:
    return f"""# {metric_name} Vs Turn {plane}

Image: `{image_name}`

This descriptive diagnostic compares Best-{", Best-".join(subset_sizes)} over the full spill. Curves are smoothed over five adjacent windows for legibility; the unsmoothed values remain in `ridge_density_turn_concentration.csv`.

{interpretation}

It is a BPM-only tracking diagnostic. A change can reflect beam dynamics, reduced signal, tune-band mismatch, or ridge-tracker behavior and is not assigned to extraction without independent timing evidence.
"""


def caption_for_legacy_turn_contrast(
    plane: str,
    subset_sizes: Sequence[str],
    image_name: str,
    metric_name: str,
    interpretation: str,
) -> str:
    return f"""# Exact-Paired Legacy Contrast Vs Turn {plane}: {metric_name}

Image: `{image_name}`

This plot compares the audited legacy normalized-single ridge picks with adaptive Best-{", Best-".join(subset_sizes)} on exactly common spill/window points. The exported values in `ridge_density_legacy_comparison_by_turn.csv` are unsmoothed; curves use a five-window visual smoothing, and the horizontal zero line means no method difference.

{interpretation}

These are changes in the cross-spill distribution of tracked ridge picks. They can characterize concentration or reduced diffuse-pick probability under fixed binning, but they do not measure physical noise removal, absolute tune accuracy, or an extraction mechanism.
"""


def caption_for_selected_turn_contrast_hv(
    selected_sizes: Mapping[str, str],
    image_name: str,
    metric_name: str,
    interpretation: str,
) -> str:
    return f"""# Selected H/V Exact-Paired Legacy Contrast: {metric_name}

Image: `{image_name}`

The two panels show H Best-{selected_sizes['H']} and V Best-{selected_sizes['V']} against the audited legacy normalized-single picks on exactly common spill/window points. Both panels use the same y scale and zero reference. Exported values in `ridge_density_legacy_comparison_by_turn.csv` are unsmoothed; curves use five-window visual smoothing.

{interpretation}

This is a cross-spill ridge-pick distribution contrast. It does not measure physical noise removal, absolute tune accuracy, extraction timing, or a causal loss mechanism.
"""


def caption_for_legacy_pair(plane: str, subset_size: str, metrics: dict[str, object]) -> str:
    return f"""# Legacy Normalized-Single Versus Adaptive Best-{subset_size} {plane}

Image: `ridge_density_legacy_single_vs_best{subset_size}_{plane.lower()}.png`

## What It Shows

Both panels use the exact legacy tune band, 4096-turn Hann windows, 256-turn stride, `{metrics.get('common_ridge_point_count', '')}` exactly paired spill/window ridge points from `{metrics.get('common_spill_count', '')}` spills, column-normalized density, and one shared color scale. The left panel reproduces the old `18d321db` normalized-single ridge picks. Its channel selector operated after RMS normalization and was effectively determined by floating-point residuals, so it is not labeled a highest-RMS or best-BPM result. The right panel uses the adaptive Best-{subset_size} power ensemble selected from early fit windows.

## Quantitative Readout

- legacy median IQR width: `{metrics.get('legacy_median_iqr_width', '')}`
- ensemble median IQR width: `{metrics.get('ensemble_median_iqr_width', '')}`
- exactly paired spill/window ridge points: `{metrics.get('common_ridge_point_count', '')}`
- fraction of turn centers with narrower ensemble IQR: `{metrics.get('fraction_centers_with_narrower_iqr', '')}`
- median peak-bin fraction gain: `{metrics.get('median_peak_bin_fraction_gain', '')}`
- median peak-bin fraction gain 95% turn-block interval: `[{metrics.get('median_peak_bin_fraction_gain_ci_low', '')}, {metrics.get('median_peak_bin_fraction_gain_ci_high', '')}]`
- median normalized density-entropy change: `{metrics.get('median_density_entropy_delta', '')}`
- median probability-mass gain within a shared +/-0.0025 tune ridge: `{metrics.get('median_shared_ridge_mass_gain', '')}`
- probability-mass gain 95% turn-block interval: `[{metrics.get('median_shared_ridge_mass_gain_ci_low', '')}, {metrics.get('median_shared_ridge_mass_gain_ci_high', '')}]`

## Claim Guardrail

Narrower width, lower density entropy, or greater mass near the shared ridge means stronger cross-spill concentration under fixed binning. The intervals resample ordered turn centers in blocks of `{metrics.get('turn_block_windows', '')}` windows to account approximately for window overlap; they measure persistence over the analyzed buffer, not uncertainty over the spill population. The result may be described as reduced diffuse ridge-pick probability, not as physical noise removal or absolute tune improvement without an external reference.
"""


def caption_for_legacy_pair_hv(
    subset_size: str,
    metrics_by_plane: dict[str, dict[str, object]],
) -> str:
    return caption_for_legacy_pair_hv_selected(
        {"H": str(subset_size), "V": str(subset_size)},
        metrics_by_plane,
        f"ridge_density_legacy_single_vs_best{subset_size}_hv.png",
    )


def caption_for_legacy_pair_hv_selected(
    subset_sizes: dict[str, str],
    metrics_by_plane: dict[str, dict[str, object]],
    image_name: str,
) -> str:
    h_size = str(subset_sizes["H"])
    v_size = str(subset_sizes["V"])
    selected_label = f"Best-{h_size}" if h_size == v_size else f"H Best-{h_size} / V Best-{v_size}"
    lines = [
        f"# Legacy Normalized-Single Versus Adaptive {selected_label}",
        "",
        f"Image: `{image_name}`",
        "",
        "Rows are horizontal and vertical; columns are the audited legacy normalized-single selector and the plane-selected adaptive fit-prefix power ensemble. Every panel uses the exact 4096/256-turn legacy protocol. Color is the column-normalized cross-spill probability of a tracked tune pick, all four panels share one scale, and values above the shared 98th percentile are clipped only for color rendering. White curves are the cross-spill P10, median, and P90 ridge tracks. Compare legacy with adaptive within each row; H and V use different tune-band widths, so their apparent vertical thicknesses are not directly comparable.",
        "",
        "| Plane | Exact paired spills | Exact paired ridge points | Legacy median IQR | Adaptive median IQR | Peak-bin gain | Shared-ridge mass gain |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for plane in ("H", "V"):
        metrics = metrics_by_plane[plane]
        lines.append(
            f"| {plane} | {metrics.get('common_spill_count', '')} | {metrics.get('common_ridge_point_count', '')} | "
            f"{metrics.get('legacy_median_iqr_width', '')} | {metrics.get('ensemble_median_iqr_width', '')} | "
            f"{metrics.get('median_peak_bin_fraction_gain', '')} | {metrics.get('median_shared_ridge_mass_gain', '')} |"
        )
    lines.extend(
        [
            "",
            "This is the primary visual persistence comparison. Greater concentration means ridge picks agree more strongly across spills under fixed binning. Column normalization deliberately removes sample-count differences; consult the paired counts and sample-fraction diagnostics before interpreting a narrowing as improved persistence. The figure does not prove physical noise removal, absolute tune accuracy, or an extraction mechanism.",
            "",
        ]
    )
    return "\n".join(lines)


def paired_grid_counts(
    paired: Mapping[str, tuple[Mapping[str, dict[str, object]], tuple[float, float]]],
) -> list[str]:
    lines = [
        "| Plane | Exact paired spills | Exact paired ridge points |",
        "| --- | ---: | ---: |",
    ]
    for plane in ("H", "V"):
        results, _band = paired[plane]
        point_sets = [set(result.get("point_keys", set())) for result in results.values()]
        spill_sets = [set(result.get("spill_keys", set())) for result in results.values()]
        common_points = len(set.intersection(*point_sets)) if point_sets else 0
        common_spills = len(set.intersection(*spill_sets)) if spill_sets else 0
        lines.append(f"| {plane} | {common_spills} | {common_points} |")
    return lines


def caption_for_best1_vs_selected_hv(
    subset_sizes: Mapping[str, str],
    paired: Mapping[str, tuple[Mapping[str, dict[str, object]], tuple[float, float]]],
    image_name: str,
) -> str:
    lines = [
        "# Corrected Adaptive Best-1 Versus Plane-Selected Best-N",
        "",
        f"Image: `{image_name}`",
        "",
        f"Rows are horizontal and vertical. The left column is corrected adaptive Best-1; the right is H Best-{subset_sizes['H']} / V Best-{subset_sizes['V']}. Both methods use exact source-key memberships selected from the same early fit-window prefix and the same 4096/256-turn full-buffer tracking protocol. Every row is restricted to exact common spill/window points, both columns share one probability scale, and white curves are cross-spill P10, median, and P90 tracks.",
        "",
        *paired_grid_counts(paired),
        "",
        "This is the clean ensemble-size comparison: it does not use the flawed historical normalized-single selector. Greater concentration in the selected Best-N column may be attributed to the adaptive ensemble-size choice under this protocol, subject to the leakage-controlled later-window validation. It is still BPM-only internal consistency, not absolute tune truth or measured physical noise removal.",
        "",
    ]
    return "\n".join(lines)


def caption_for_legacy_best1_selected_hv(
    subset_sizes: Mapping[str, str],
    paired: Mapping[str, tuple[Mapping[str, dict[str, object]], tuple[float, float]]],
    image_name: str,
) -> str:
    lines = [
        "# Legacy Selector, Corrected Best-1, And Plane-Selected Best-N",
        "",
        f"Image: `{image_name}`",
        "",
        f"Rows are horizontal and vertical. Columns separate the audited legacy normalized-single selector, corrected adaptive Best-1, and H Best-{subset_sizes['H']} / V Best-{subset_sizes['V']}. All three columns use exact common spill/window points, column-normalized pick probability, one shared color scale, and the same tune bands and 4096/256-turn visual grammar.",
        "",
        *paired_grid_counts(paired),
        "",
        "Interpret the two transitions separately. Legacy to corrected Best-1 includes recovery from the legacy post-normalization selector defect. Corrected Best-1 to the plane-selected Best-N isolates the ensemble-size effect. The total legacy-to-Best-N contrast must not be attributed solely to adding BPMs. None of the columns establishes absolute tune accuracy, physical noise removal, or extraction timing.",
        "",
    ]
    return "\n".join(lines)


def caption_for_legacy_difference(
    plane: str,
    subset_size: str,
    common_spills: int,
    common_points: int,
) -> str:
    return f"""# Adaptive Best-{subset_size} Minus Legacy Normalized-Single {plane}

Image: `ridge_density_best{subset_size}_minus_legacy_single_{plane.lower()}.png`

This subtractive map uses `{common_points}` exactly paired spill/window ridge points from `{common_spills}` spills and the same column-normalized tune-density distributions as the paired side-by-side figure. Red bins gain probability under adaptive Best-{subset_size}; blue bins lose probability relative to the legacy normalized-single method. Absolute differences above the 99th percentile are clipped only for color rendering. The white line is the adaptive median and the dark line is the legacy median.

A favorable concentration pattern is lower probability away from a persistent ridge and higher probability close to it. This is descriptive BPM-only evidence: it does not identify physical noise, establish absolute tune truth, or prove that every lower-probability structure was undesirable.
"""


def loss_summary_text(rows: Sequence[dict[str, object]], extraction_start: int, extraction_end: int) -> str:
    lines = [
        "# Ridge Density Loss-Candidate Summary",
        "",
        "This is a heuristic review aid for locating where ridge concentration degrades, especially in H.",
        "",
        f"- extraction context marker: turns `{extraction_start}-{extraction_end}`",
        "- loss heuristic: first run of five smoothed windows after the concentration peak below half of that peak",
        "- change-point heuristic: unconstrained split that jointly favors lower peak concentration, wider IQR, and lower valid-spill retention after the split",
        "- marker fractions are reported separately so the extraction hypothesis is not forced into the loss estimate",
        "- this is not a measured extraction boundary and not an external tune-validation metric",
        "",
        "| Plane | Best | Peak Turn | Half-Peak Loss | Change Candidate | Peak Drop | IQR Increase | Sample Drop |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {plane} | Best-{subset_size} | `{peak_turn}` | `{loss_turn}` | `{change_turn}` | `{peak_drop}` | `{iqr_increase}` | `{sample_drop}` |".format(
                plane=row.get("plane", ""),
                subset_size=row.get("subset_size", ""),
                peak_turn=row.get("peak_concentration_turn", ""),
                loss_turn=row.get("first_sustained_half_peak_loss_turn", "") or "not found",
                change_turn=row.get("most_likely_change_turn", "") or "not found",
                peak_drop=row.get("relative_peak_fraction_drop", ""),
                iqr_increase=row.get("relative_iqr_width_increase", ""),
                sample_drop=row.get("relative_sample_fraction_drop", ""),
            )
        )
    lines.extend(
        [
            "",
            "Interpret this table only after visually inspecting the corresponding density and concentration plots. A low-concentration turn may indicate real decoherence/extraction dynamics, weak H-plane signal, a wrong tune band, or a ridge-tracking artifact.",
        ]
    )
    return "\n".join(lines) + "\n"


def index_text(
    figures: Sequence[dict[str, object]],
    old_source: str,
    turn_start: int,
    turn_end: int,
    window_turns: int,
    stride_turns: int,
    subset_sizes: Sequence[str],
    normalized: bool,
) -> str:
    lines = [
        "# Best-Ensemble Ridge Density Index",
        "",
        f"These artifacts reproduce the visual grammar of the older favorite `18d321db` legacy normalized-single ridge-density plots with adaptive Best-{', Best-'.join(subset_sizes)} memberships.",
        "",
        f"- old reference source: `{old_source}`",
        f"- turn range: `{turn_start}-{turn_end}`",
        f"- window/stride: `{window_turns} / {stride_turns}`",
        (
            "- color: ridge-pick fraction within each turn column"
            if normalized
            else "- color: spill count per turn/tune bin"
        ),
        "- white curves: per-window median and percentile envelopes",
        "",
        "| Figure | Caption | Plane | Subset | Role |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in figures:
        lines.append(f"| `{row['figure']}` | `{row['caption_file']}` | `{row['plane']}` | `{row['subset_size']}` | {row['role']} |")
    lines.extend(
        [
            "",
            "## Important Scope Note",
            "",
            "This sidecar reuses the completed early-window Best-BPM memberships, then recomputes full-buffer ridge densities. It is the right first comparison with the old gallery, but it is not yet a full 50k dynamic subset search.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_analysis_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        bpm_combination="mean",
        bpm_normalization=args.bpm_normalization,
        detrend=args.detrend,
        dc_handling=args.dc_handling,
        spectrogram_method="hann",
        ridge_source_method="auto",
        injection_start_turn=0,
        injection_window_turns=args.injection_window_turns,
        sliding_window_turns=args.window_turns,
        sliding_stride_turns=args.stride_turns,
        flashes=None,
        window_chunk=args.window_chunk,
        min_peak_confidence=args.min_peak_confidence,
        track_half_width=args.track_half_width,
        max_tune_step_per_window=args.max_tune_step_per_window,
        enable_tracking=True,
        multitaper_nw=2.5,
        multitaper_k=4,
        ridge_method="greedy",
        ridge_normalize="row",
        ridge_anchor_enabled=True,
        ridge_anchor_h=args.ridge_anchor_h,
        ridge_anchor_v=args.ridge_anchor_v,
        ridge_anchor_half_width=0.02,
        ridge_anchor_penalty=1000.0,
        ridge_jump_penalty=500.0,
        ridge_jump2_penalty=20000.0,
        ridge_max_step=0.010,
        ridge_density_tune_bins=args.ridge_density_tune_bins,
        ridge_density_normalize=args.ridge_density_normalize,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--best-root", required=True, help="completed Best-BPM run root")
    parser.add_argument("--membership-csv", default=None, help="optional Best-N curve/result CSV with exact membership rows")
    parser.add_argument("--legacy-sliding-csv", default=None, help="optional legacy gpu_sliding_tune.csv for paired old-vs-new comparisons")
    parser.add_argument("--input", nargs="+", required=True, help="captured run dirs, spill dirs, or manifest.json files")
    parser.add_argument("--out", required=True, help="output artifact directory")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--progress", type=int, default=25)
    parser.add_argument("--turn-start", type=int, default=0)
    parser.add_argument("--turn-end", type=int, default=50000)
    parser.add_argument("--window-turns", type=int, default=4096)
    parser.add_argument("--stride-turns", type=int, default=256)
    parser.add_argument("--subset-sizes", nargs="+", default=["1", "3", "5"], help="membership sizes to render")
    parser.add_argument("--selected-h-n", type=int, default=None, help="verified H ensemble size for the plane-selected H/V comparison")
    parser.add_argument("--selected-v-n", type=int, default=None, help="verified V ensemble size for the plane-selected H/V comparison")
    parser.add_argument("--planes", nargs="+", default=["H", "V"], choices=["H", "V"])
    parser.add_argument("--bpm-normalization", default="rms_per_bpm", choices=["none", "rms_per_bpm", "mad_per_bpm", "injection_rms_per_bpm"])
    parser.add_argument("--detrend", default="mean_subtract", choices=["none", "mean_subtract", "linear", "polynomial_order_2"])
    parser.add_argument("--dc-handling", default="zero_dc_bin", choices=["keep", "zero_dc_bin", "ignore_low_bins"])
    parser.add_argument("--injection-window-turns", type=int, default=4096)
    parser.add_argument("--window-chunk", type=int, default=32)
    parser.add_argument("--min-peak-confidence", type=float, default=2.0)
    parser.add_argument("--track-half-width", type=float, default=0.005)
    parser.add_argument("--max-tune-step-per-window", type=float, default=0.005)
    parser.add_argument("--qx-min", type=float, default=0.620)
    parser.add_argument("--qx-max", type=float, default=0.680)
    parser.add_argument("--qy-min", type=float, default=0.690)
    parser.add_argument("--qy-max", type=float, default=0.740)
    parser.add_argument("--ridge-anchor-h", type=float, default=0.65)
    parser.add_argument("--ridge-anchor-v", type=float, default=0.72)
    parser.add_argument("--ridge-density-tune-bins", type=int, default=160)
    parser.add_argument("--ridge-density-normalize", action="store_true")
    parser.add_argument("--comparison-bootstrap-samples", type=int, default=500, help="moving-block draws for paired legacy contrast metrics")
    parser.add_argument("--extraction-range-start-turn", type=int, default=10000, help="optional broad review-only context marker start; never used by the loss heuristic")
    parser.add_argument("--extraction-range-end-turn", type=int, default=20000, help="optional broad review-only context marker end; never treated as a measured boundary")
    parser.add_argument("--extraction-context-variants", action="store_true", help="also render separately named plots with the broad extraction-review range marked")
    args = parser.parse_args(argv)
    args.mark_extraction_context = False
    if (args.selected_h_n is None) != (args.selected_v_n is None):
        parser.error("--selected-h-n and --selected-v-n must be provided together")
    selected_sizes = None
    if args.selected_h_n is not None:
        selected_sizes = {"H": str(args.selected_h_n), "V": str(args.selected_v_n)}
        missing = sorted(set(selected_sizes.values()) - set(args.subset_sizes), key=int)
        if missing:
            parser.error(f"plane-selected N must be included in --subset-sizes: {', '.join(missing)}")

    best_root = Path(args.best_root)
    out = Path(args.out)
    poster.ensure_dir(out)
    memberships = load_memberships(best_root, args.subset_sizes, Path(args.membership_csv) if args.membership_csv else None)
    legacy_points = load_legacy_points(Path(args.legacy_sliding_csv)) if args.legacy_sliding_csv else {}
    manifests = discover_manifests([Path(item) for item in args.input])
    if args.limit:
        manifests = manifests[: args.limit]
    if not manifests:
        raise SystemExit("no manifest.json files found")

    backend = FftBackend(args.device)
    if args.membership_csv:
        membership_path = Path(args.membership_csv)
        membership_source = str(membership_path.resolve())
        membership_sha256 = file_sha256(membership_path)
    else:
        membership_files = [
            best_root / "subset_search" / f"best{size}" / f"best{size}_results.csv"
            for size in args.subset_sizes
        ]
        membership_source = ",".join(str(path.resolve()) for path in membership_files)
        membership_sha256 = object_sha256(
            [{"path": str(path.resolve()), "sha256": file_sha256(path)} for path in membership_files]
        )
    legacy_path = Path(args.legacy_sliding_csv) if args.legacy_sliding_csv else None
    ensure_run_contract(
        out / "run_contract.json",
        {
            "analysis": "full_buffer_ridge_density",
            "best_root": str(best_root.resolve()),
            "best_bpm_index_sha256": file_sha256(best_root / "manifest" / "bpm_index.csv"),
            "membership_source": membership_source,
            "membership_sha256": membership_sha256,
            "legacy_sliding_source": str(legacy_path.resolve()) if legacy_path else "",
            "legacy_sliding_sha256": file_sha256(legacy_path) if legacy_path else "",
            "legacy_reference_spill_counts": {
                plane: len({(run_name, target_ms) for run_name, target_ms, _center, _tune in legacy_points.get(plane, [])})
                for plane in args.planes
            },
            "legacy_reference_point_counts": {
                plane: len(legacy_points.get(plane, [])) for plane in args.planes
            },
            "input_roots": [str(Path(item).resolve()) for item in args.input],
            "manifest_count": len(manifests),
            "manifest_inventory_sha256": manifest_inventory_sha256(manifests),
            "device_request": args.device,
            "fft_backend": backend.name,
            "turn_start": int(args.turn_start),
            "turn_end": int(args.turn_end),
            "window_turns": int(args.window_turns),
            "stride_turns": int(args.stride_turns),
            "subset_sizes": sorted(int(size) for size in args.subset_sizes),
            "selected_plane_sizes": {plane: int(size) for plane, size in selected_sizes.items()} if selected_sizes else {},
            "planes": list(args.planes),
            "bpm_normalization": args.bpm_normalization,
            "detrend": args.detrend,
            "dc_handling": args.dc_handling,
            "injection_window_turns": int(args.injection_window_turns),
            "min_peak_confidence": float(args.min_peak_confidence),
            "track_half_width": float(args.track_half_width),
            "max_tune_step_per_window": float(args.max_tune_step_per_window),
            "qx_band": [float(args.qx_min), float(args.qx_max)],
            "qy_band": [float(args.qy_min), float(args.qy_max)],
            "ridge_density_tune_bins": int(args.ridge_density_tune_bins),
            "ridge_density_normalize": bool(args.ridge_density_normalize),
            "comparison_bootstrap_samples": int(args.comparison_bootstrap_samples),
            "extraction_context_range": [
                int(args.extraction_range_start_turn),
                int(args.extraction_range_end_turn),
            ],
            "extraction_context_variants": bool(args.extraction_context_variants),
            "limit": int(args.limit),
        },
        (
            out / "ridge_density_best_ensemble_metrics.csv",
            out / "ridge_density_turn_concentration.csv",
        ),
    )
    analysis_args = build_analysis_args(args)
    sliding_by_subset: dict[str, list[dict[str, object]]] = {size: [] for size in args.subset_sizes}
    accepted_by_subset_plane: dict[tuple[str, str], set[int]] = {
        (size, plane): set() for size in args.subset_sizes for plane in args.planes
    }
    spill_counts: dict[tuple[str, str], int] = defaultdict(int)
    warnings: list[str] = []
    started = time.time()

    for spill_index, manifest in enumerate(manifests):
        bundle = load_bundle(manifest)
        spill_id = f"spill_{bundle.target_ms}"
        for plane in args.planes:
            traces, _turns, plane_warnings, labels, _ranking_scores = load_plane_traces(
                bundle,
                plane,
                None,
                False,
                args.turn_start,
                args.turn_end,
                args.bpm_normalization,
                args.injection_window_turns,
                "stream_key",
            )
            warnings.extend(plane_warnings[:3])
            if traces is None or not labels:
                continue
            band = (args.qx_min, args.qx_max) if plane == "H" else (args.qy_min, args.qy_max)
            for size in args.subset_sizes:
                wanted = memberships.get((bundle.run_name, spill_id, plane, size))
                if not wanted:
                    continue
                expected_count = int(size)
                if len(wanted) != expected_count:
                    warnings.append(
                        f"{bundle.run_name}/{spill_id}/{plane}/best{size}: membership has {len(wanted)} exact source keys"
                    )
                    continue
                indices = selected_trace_indices(labels, wanted)
                if len(indices) != expected_count:
                    warnings.append(
                        f"{bundle.run_name}/{spill_id}/{plane}/best{size}: matched {len(indices)}/{expected_count} selected channel payloads"
                    )
                    continue
                selected = traces[indices, :]
                timers = {"fft_seconds": 0.0, "windows": 0.0}
                try:
                    analysis = analyze_plane(
                        bundle,
                        plane,
                        selected,
                        [labels[idx] for idx in indices],
                        selected.shape[1],
                        analysis_args,
                        backend,
                        band,
                        timers,
                    )
                except Exception as exc:
                    warnings.append(f"{bundle.run_name}/{spill_id}/{plane}/best{size}: {exc!r}")
                    continue
                if analysis.sliding_points:
                    accepted_by_subset_plane[(size, plane)].add(spill_index)
                    spill_counts[(plane, size)] += 1
                for point in analysis.sliding_points:
                    row = dict(point)
                    row.update(
                        {
                            "spill_index": spill_index,
                            "run_name": bundle.run_name,
                            "target_ms": bundle.target_ms,
                            "spill_id": spill_id,
                            "plane": plane,
                            "subset_size": size,
                            "selected_bpm_count": len(indices),
                        }
                    )
                    sliding_by_subset[size].append(row)
        if args.progress and (spill_index + 1) % args.progress == 0:
            elapsed = time.time() - started
            print(f"processed {spill_index + 1}/{len(manifests)} manifests in {elapsed:.1f}s", flush=True)

    figure_rows: list[dict[str, object]] = []
    metric_rows: list[dict[str, object]] = []
    center_rows: list[dict[str, object]] = []
    density_results: dict[tuple[str, str], dict[str, object]] = {}
    for size in args.subset_sizes:
        rows = sliding_by_subset[size]
        write_csv(out / f"ridge_density_best{size}_sliding_tune.csv", rows, ["spill_index", "run_name", "target_ms", "spill_id", "plane", "subset_size", "selected_bpm_count", "window_index", "center_turn", "selected_tune", "raw_global_tune", "tracked_local_tune", "selected_confidence", "used_global_fallback", "suspicious_step", "step_delta"])
        for plane in args.planes:
            band = (args.qx_min, args.qx_max) if plane == "H" else (args.qy_min, args.qy_max)
            img = f"ridge_density_best{size}_{plane.lower()}.png"
            result = ridge_density_plot(
                out / img,
                f"RIDGE DENSITY BEST{size} {plane}",
                rows,
                plane,
                band,
                accepted_by_subset_plane[(size, plane)],
                args,
            )
            density_results[(size, plane)] = result
            metrics = ridge_metrics(rows, plane, size, band, args.ridge_density_tune_bins, spill_counts[(plane, size)])
            metric_rows.append(metrics)
            center_rows.extend(
                center_metric_rows(
                    rows,
                    plane,
                    size,
                    band,
                    args.ridge_density_tune_bins,
                    spill_counts[(plane, size)],
                )
            )
            cap = f"ridge_density_best{size}_{plane.lower()}_caption.md"
            write_text(
                out / cap,
                caption_for_density(
                    plane,
                    size,
                    metrics,
                    args.turn_start,
                    args.turn_end,
                    args.window_turns,
                    args.stride_turns,
                    args.extraction_range_start_turn,
                    args.extraction_range_end_turn,
                    args.ridge_density_normalize,
                ),
            )
            figure_rows.append({"figure": img, "caption_file": cap, "plane": plane, "subset_size": size, "role": "ridge density", "source": f"best{size}_results.csv"})

    write_csv(out / "ridge_density_best_ensemble_metrics.csv", metric_rows, RIDGE_METRIC_FIELDS)
    write_csv(out / "ridge_density_turn_concentration.csv", center_rows, CENTER_METRIC_FIELDS)
    legacy_metric_rows: list[dict[str, object]] = []
    legacy_turn_rows: list[dict[str, object]] = []
    paired_legacy_results: dict[
        tuple[str, str],
        tuple[dict[str, object], dict[str, object], tuple[float, float]],
    ] = {}
    for plane in args.planes:
        img = f"ridge_density_comparison_{plane.lower()}.png"
        draw_comparison(out / img, plane, metric_rows, args.subset_sizes)
        cap = f"ridge_density_comparison_{plane.lower()}_caption.md"
        write_text(out / cap, caption_for_comparison(plane, args.subset_sizes))
        figure_rows.append({"figure": img, "caption_file": cap, "plane": plane, "subset_size": "/".join(args.subset_sizes), "role": "metric comparison", "source": "ridge_density_best_ensemble_metrics.csv"})
        concentration_img = f"ridge_concentration_vs_turn_{plane.lower()}.png"
        draw_concentration_plot(out / concentration_img, plane, center_rows, args.subset_sizes, args)
        concentration_cap = f"ridge_concentration_vs_turn_{plane.lower()}_caption.md"
        write_text(out / concentration_cap, caption_for_concentration(plane, args.subset_sizes))
        figure_rows.append({"figure": concentration_img, "caption_file": concentration_cap, "plane": plane, "subset_size": "/".join(args.subset_sizes), "role": "turn concentration", "source": "ridge_density_turn_concentration.csv"})
        metric_specs = (
            ("ridge_iqr_width_vs_turn", "iqr_width", "RIDGE IQR WIDTH", "TUNE IQR", False, "A sustained increase means ridge picks are spreading across spills; lower is more concentrated."),
            ("ridge_p10_p90_width_vs_turn", "p10_p90_width", "RIDGE P10-P90 WIDTH", "TUNE P10-P90", False, "This outer-width view is more sensitive than IQR to a growing diffuse population."),
            ("ridge_valid_spill_fraction_vs_turn", "sample_fraction", "VALID RIDGE-SPILL FRACTION", "VALID SPILL FRACTION", True, "A decline means fewer accepted spills contribute a finite in-band ridge pick at that turn."),
            ("ridge_density_entropy_vs_turn", "density_entropy", "RIDGE DENSITY ENTROPY", "NORMALIZED ENTROPY", True, "Higher entropy means the cross-spill ridge-pick distribution is more diffuse over the tune band."),
            ("ridge_confidence_vs_turn", "median_selected_confidence", "MEDIAN RIDGE CONFIDENCE", "ROBUST PEAK CONFIDENCE", False, "Declining confidence indicates that the selected spectral peak is losing contrast against its local background."),
            ("ridge_global_fallback_fraction_vs_turn", "global_fallback_fraction", "GLOBAL FALLBACK FRACTION", "FALLBACK FRACTION", True, "A rising fallback fraction indicates that continuity tracking could not retain a trusted local peak and reverted to the global in-band peak."),
            ("ridge_suspicious_step_fraction_vs_turn", "suspicious_step_fraction", "SUSPICIOUS STEP FRACTION", "FLAGGED STEP FRACTION", True, "A rising fraction separates tracker discontinuities from a merely broad but continuous ridge."),
        )
        for stem, metric, title, y_label, fraction_scale, interpretation in metric_specs:
            metric_img = f"{stem}_{plane.lower()}.png"
            draw_turn_metric_plot(
                out / metric_img,
                plane,
                center_rows,
                args.subset_sizes,
                metric,
                title,
                y_label,
                fraction_scale,
            )
            metric_cap = f"{stem}_{plane.lower()}_caption.md"
            write_text(
                out / metric_cap,
                caption_for_turn_metric(plane, args.subset_sizes, metric_img, title.title(), interpretation),
            )
            figure_rows.append(
                {
                    "figure": metric_img,
                    "caption_file": metric_cap,
                    "plane": plane,
                    "subset_size": "/".join(args.subset_sizes),
                    "role": f"turn diagnostic: {metric}",
                    "source": "ridge_density_turn_concentration.csv",
                }
            )
        for baseline_index, baseline_size in enumerate(args.subset_sizes):
            for ensemble_size in args.subset_sizes[baseline_index + 1 :]:
                band = (args.qx_min, args.qx_max) if plane == "H" else (args.qy_min, args.qy_max)
                baseline_points = keyed_ensemble_points(sliding_by_subset[baseline_size], plane, band)
                ensemble_points = keyed_ensemble_points(sliding_by_subset[ensemble_size], plane, band)
                baseline, ensemble = exact_paired_density_results(
                    baseline_points,
                    ensemble_points,
                    band,
                    args.ridge_density_tune_bins,
                )
                common_points = len(set(baseline.get("point_keys", set())) & set(ensemble.get("point_keys", set())))
                common_spills = len(set(baseline.get("spill_keys", set())) & set(ensemble.get("spill_keys", set())))
                diff_img = f"ridge_density_best{ensemble_size}_minus_best{baseline_size}_{plane.lower()}.png"
                draw_density_difference(out / diff_img, plane, baseline_size, ensemble_size, baseline, ensemble, band, args)
                diff_cap = f"ridge_density_best{ensemble_size}_minus_best{baseline_size}_{plane.lower()}_caption.md"
                write_text(
                    out / diff_cap,
                    caption_for_difference(plane, baseline_size, ensemble_size, common_spills, common_points),
                )
                figure_rows.append({"figure": diff_img, "caption_file": diff_cap, "plane": plane, "subset_size": f"{ensemble_size}-{baseline_size}", "role": "density difference", "source": "ridge_density_best*_sliding_tune.csv"})
        if plane in legacy_points:
            band = (args.qx_min, args.qx_max) if plane == "H" else (args.qy_min, args.qy_max)
            legacy_keyed = keyed_legacy_points(legacy_points[plane], band)
            for size in args.subset_sizes:
                ensemble_keyed = keyed_ensemble_points(sliding_by_subset[size], plane, band)
                paired_legacy, paired_ensemble = exact_paired_density_results(
                    legacy_keyed,
                    ensemble_keyed,
                    band,
                    args.ridge_density_tune_bins,
                )
                comparison_metrics = legacy_comparison_metrics(
                    plane,
                    size,
                    paired_legacy,
                    paired_ensemble,
                    band,
                    args.ridge_density_tune_bins,
                    turn_block_windows=max(1, int(math.ceil(args.window_turns / max(1, args.stride_turns)))),
                    bootstrap_samples=args.comparison_bootstrap_samples,
                )
                legacy_metric_rows.append(comparison_metrics)
                legacy_turn_rows.extend(
                    legacy_comparison_by_turn_rows(
                        plane,
                        size,
                        paired_legacy,
                        paired_ensemble,
                        band,
                        args.ridge_density_tune_bins,
                    )
                )
                paired_legacy_results[(size, plane)] = (paired_legacy, paired_ensemble, band)
                pair_img = f"ridge_density_legacy_single_vs_best{size}_{plane.lower()}.png"
                draw_legacy_pair(out / pair_img, plane, size, paired_legacy, paired_ensemble, band)
                pair_cap = f"ridge_density_legacy_single_vs_best{size}_{plane.lower()}_caption.md"
                write_text(out / pair_cap, caption_for_legacy_pair(plane, size, comparison_metrics))
                figure_rows.append(
                    {
                        "figure": pair_img,
                        "caption_file": pair_cap,
                        "plane": plane,
                        "subset_size": size,
                        "role": "paired legacy comparison",
                        "source": str(args.legacy_sliding_csv),
                    }
                )
                diff_img = f"ridge_density_best{size}_minus_legacy_single_{plane.lower()}.png"
                draw_density_difference(out / diff_img, plane, "LEGACY", size, paired_legacy, paired_ensemble, band, args)
                diff_cap = f"ridge_density_best{size}_minus_legacy_single_{plane.lower()}_caption.md"
                write_text(
                    out / diff_cap,
                    caption_for_legacy_difference(
                        plane,
                        size,
                        int(comparison_metrics.get("common_spill_count") or 0),
                        int(comparison_metrics.get("common_ridge_point_count") or 0),
                    ),
                )
                figure_rows.append(
                    {
                        "figure": diff_img,
                        "caption_file": diff_cap,
                        "plane": plane,
                        "subset_size": size,
                        "role": "paired legacy density difference",
                        "source": str(args.legacy_sliding_csv),
                    }
                )
        if args.extraction_context_variants:
            context_args = argparse.Namespace(**vars(args))
            context_args.mark_extraction_context = True
            context_img = f"ridge_concentration_vs_turn_{plane.lower()}_extraction_context.png"
            draw_concentration_plot(out / context_img, plane, center_rows, args.subset_sizes, context_args)
            context_cap = f"ridge_concentration_vs_turn_{plane.lower()}_extraction_context_caption.md"
            write_text(out / context_cap, caption_for_concentration(plane, args.subset_sizes, True))
            figure_rows.append(
                {
                    "figure": context_img,
                    "caption_file": context_cap,
                    "plane": plane,
                    "subset_size": "/".join(args.subset_sizes),
                    "role": "exploratory extraction-context concentration",
                    "source": "ridge_density_turn_concentration.csv",
                }
            )
    write_csv(
        out / "ridge_density_legacy_comparison_by_turn.csv",
        legacy_turn_rows,
        LEGACY_TURN_COMPARISON_FIELDS,
    )
    contrast_specs = (
        (
            "ridge_iqr_delta_vs_turn",
            "iqr_delta_ensemble_minus_legacy",
            "ADAPTIVE MINUS LEGACY IQR",
            "DELTA TUNE IQR",
            "Negative values mean adaptive ridge picks are narrower across spills at that turn.",
        ),
        (
            "ridge_p10_p90_delta_vs_turn",
            "p10_p90_delta_ensemble_minus_legacy",
            "ADAPTIVE MINUS LEGACY P10-P90",
            "DELTA TUNE P10-P90",
            "Negative values mean the adaptive cross-spill P10-P90 ridge-pick width is narrower.",
        ),
        (
            "ridge_peak_bin_gain_vs_turn",
            "peak_bin_fraction_gain",
            "ADAPTIVE PEAK-BIN GAIN",
            "DELTA PEAK FRACTION",
            "Positive values mean more adaptive picks occupy the most populated tune bin.",
        ),
        (
            "ridge_entropy_delta_vs_turn",
            "density_entropy_delta",
            "ADAPTIVE MINUS LEGACY ENTROPY",
            "DELTA NORMALIZED ENTROPY",
            "Negative values mean the adaptive cross-spill pick distribution is less diffuse.",
        ),
        (
            "ridge_shared_mass_gain_vs_turn",
            "shared_ridge_mass_gain",
            "ADAPTIVE SHARED-RIDGE MASS GAIN",
            "DELTA RIDGE MASS",
            "Positive values mean more adaptive picks lie within +/-0.0025 tune of the shared legacy/adaptive center.",
        ),
    )
    for plane in ("H", "V"):
        for stem, metric, title, y_label, interpretation in contrast_specs:
            image_name = f"{stem}_{plane.lower()}.png"
            draw_turn_metric_plot(
                out / image_name,
                plane,
                legacy_turn_rows,
                args.subset_sizes,
                metric,
                title,
                y_label,
                zero_reference=True,
            )
            caption_name = image_name.replace(".png", "_caption.md")
            write_text(
                out / caption_name,
                caption_for_legacy_turn_contrast(
                    plane,
                    args.subset_sizes,
                    image_name,
                    title.title(),
                    interpretation,
                ),
            )
            figure_rows.append(
                {
                    "figure": image_name,
                    "caption_file": caption_name,
                    "plane": plane,
                    "subset_size": "/".join(args.subset_sizes),
                    "role": f"paired legacy turn contrast: {metric}",
                    "source": "ridge_density_legacy_comparison_by_turn.csv",
                }
            )
            if selected_sizes:
                selected_size = selected_sizes[plane]
                selected_image = f"{stem}_selected_best{selected_size}_{plane.lower()}.png"
                draw_turn_metric_plot(
                    out / selected_image,
                    plane,
                    legacy_turn_rows,
                    [selected_size],
                    metric,
                    title,
                    y_label,
                    zero_reference=True,
                )
                selected_caption = selected_image.replace(".png", "_caption.md")
                write_text(
                    out / selected_caption,
                    caption_for_legacy_turn_contrast(
                        plane,
                        [selected_size],
                        selected_image,
                        title.title(),
                        interpretation,
                    ),
                )
                figure_rows.append(
                    {
                        "figure": selected_image,
                        "caption_file": selected_caption,
                        "plane": plane,
                        "subset_size": selected_size,
                        "role": f"plane-selected paired legacy turn contrast: {metric}",
                        "source": "ridge_density_legacy_comparison_by_turn.csv",
                    }
                )
    if selected_sizes:
        for stem, metric, title, y_label, interpretation in contrast_specs:
            image_name = (
                f"{stem}_selected_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png"
            )
            draw_selected_turn_contrast_hv(
                out / image_name,
                legacy_turn_rows,
                selected_sizes,
                metric,
                title,
                y_label,
            )
            caption_name = image_name.replace(".png", "_caption.md")
            write_text(
                out / caption_name,
                caption_for_selected_turn_contrast_hv(
                    selected_sizes,
                    image_name,
                    title.title(),
                    interpretation,
                ),
            )
            figure_rows.append(
                {
                    "figure": image_name,
                    "caption_file": caption_name,
                    "plane": "H/V",
                    "subset_size": f"H{selected_sizes['H']}/V{selected_sizes['V']}",
                    "role": f"plane-selected H/V paired legacy turn contrast: {metric}",
                    "source": "ridge_density_legacy_comparison_by_turn.csv",
                }
            )
            poster_image_name = image_name.replace(".png", "_poster.png")
            draw_selected_turn_contrast_hv(
                out / poster_image_name,
                legacy_turn_rows,
                selected_sizes,
                metric,
                title,
                y_label,
                portrait=True,
            )
            poster_caption_name = poster_image_name.replace(".png", "_caption.md")
            write_text(
                out / poster_caption_name,
                caption_for_selected_turn_contrast_hv(
                    selected_sizes,
                    poster_image_name,
                    title.title(),
                    interpretation,
                ),
            )
            figure_rows.append(
                {
                    "figure": poster_image_name,
                    "caption_file": poster_caption_name,
                    "plane": "H/V",
                    "subset_size": f"H{selected_sizes['H']}/V{selected_sizes['V']}",
                    "role": f"plane-selected H/V paired legacy turn contrast poster: {metric}",
                    "source": "ridge_density_legacy_comparison_by_turn.csv",
                }
            )
    legacy_metrics_by_key = {
        (str(row.get("subset_size", "")), str(row.get("plane", ""))): row
        for row in legacy_metric_rows
    }
    if selected_sizes:
        for plane in ("H", "V"):
            selected_size = selected_sizes[plane]
            concentration_img = f"ridge_concentration_selected_best{selected_size}_{plane.lower()}.png"
            draw_concentration_plot(
                out / concentration_img,
                plane,
                center_rows,
                [selected_size],
                args,
            )
            concentration_cap = concentration_img.replace(".png", "_caption.md")
            write_text(
                out / concentration_cap,
                caption_for_concentration(plane, [selected_size], image_name=concentration_img),
            )
            figure_rows.append(
                {
                    "figure": concentration_img,
                    "caption_file": concentration_cap,
                    "plane": plane,
                    "subset_size": selected_size,
                    "role": "plane-selected turn concentration",
                    "source": "ridge_density_turn_concentration.csv",
                }
            )
        selected_keys = {(selected_sizes[plane], plane) for plane in ("H", "V")}
        if selected_keys <= set(paired_legacy_results):
            selected_pairs = {
                plane: paired_legacy_results[(selected_sizes[plane], plane)]
                for plane in ("H", "V")
            }
            selected_metrics = {
                plane: legacy_metrics_by_key[(selected_sizes[plane], plane)]
                for plane in ("H", "V")
            }
            pair_img = (
                f"ridge_density_legacy_single_vs_best_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png"
            )
            pair_cap = pair_img.replace(".png", "_caption.md")
            draw_legacy_pair_hv_selected(out / pair_img, selected_sizes, selected_pairs)
            write_text(
                out / pair_cap,
                caption_for_legacy_pair_hv_selected(selected_sizes, selected_metrics, pair_img),
            )
            figure_rows.append(
                {
                    "figure": pair_img,
                    "caption_file": pair_cap,
                    "plane": "H/V",
                    "subset_size": f"H={selected_sizes['H']};V={selected_sizes['V']}",
                    "role": "plane-selected paired legacy H/V comparison",
                    "source": str(args.legacy_sliding_csv),
                }
            )
        if "1" in args.subset_sizes and all(plane in legacy_points for plane in ("H", "V")):
            best1_vs_selected: dict[
                str,
                tuple[dict[str, dict[str, object]], tuple[float, float]],
            ] = {}
            legacy_best1_selected: dict[
                str,
                tuple[dict[str, dict[str, object]], tuple[float, float]],
            ] = {}
            for plane in ("H", "V"):
                band = (args.qx_min, args.qx_max) if plane == "H" else (args.qy_min, args.qy_max)
                best1_points = keyed_ensemble_points(sliding_by_subset["1"], plane, band)
                selected_points = keyed_ensemble_points(
                    sliding_by_subset[selected_sizes[plane]],
                    plane,
                    band,
                )
                direct_results = exact_paired_density_results_many(
                    {"best1": best1_points, "selected": selected_points},
                    band,
                    args.ridge_density_tune_bins,
                )
                triple_results = exact_paired_density_results_many(
                    {
                        "legacy": keyed_legacy_points(legacy_points[plane], band),
                        "best1": best1_points,
                        "selected": selected_points,
                    },
                    band,
                    args.ridge_density_tune_bins,
                )
                best1_vs_selected[plane] = (direct_results, band)
                legacy_best1_selected[plane] = (triple_results, band)

            selected_label = f"SELECTED H BEST{selected_sizes['H']} / V BEST{selected_sizes['V']}"
            direct_img = (
                f"ridge_density_best1_vs_selected_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png"
            )
            direct_cap = direct_img.replace(".png", "_caption.md")
            draw_paired_density_grid_hv(
                out / direct_img,
                "FULL-SPILL RIDGE DENSITY: CORRECTED BEST1 VS PLANE-SELECTED BEST-N",
                (
                    ("best1", "CORRECTED ADAPTIVE BEST1"),
                    ("selected", selected_label),
                ),
                best1_vs_selected,
            )
            write_text(
                out / direct_cap,
                caption_for_best1_vs_selected_hv(selected_sizes, best1_vs_selected, direct_img),
            )
            figure_rows.append(
                {
                    "figure": direct_img,
                    "caption_file": direct_cap,
                    "plane": "H/V",
                    "subset_size": f"H={selected_sizes['H']};V={selected_sizes['V']}",
                    "role": "plane-selected corrected Best-1 H/V comparison",
                    "source": "ridge_density_best1_sliding_tune.csv and plane-selected sliding tune CSVs",
                }
            )

            triple_img = (
                f"ridge_density_legacy_vs_best1_vs_selected_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png"
            )
            triple_cap = triple_img.replace(".png", "_caption.md")
            draw_paired_density_grid_hv(
                out / triple_img,
                "FULL-SPILL RIDGE DENSITY: LEGACY, CORRECTED BEST1, SELECTED BEST-N",
                (
                    ("legacy", "LEGACY NORMALIZED-SINGLE"),
                    ("best1", "CORRECTED ADAPTIVE BEST1"),
                    ("selected", selected_label),
                ),
                legacy_best1_selected,
            )
            write_text(
                out / triple_cap,
                caption_for_legacy_best1_selected_hv(
                    selected_sizes,
                    legacy_best1_selected,
                    triple_img,
                ),
            )
            figure_rows.append(
                {
                    "figure": triple_img,
                    "caption_file": triple_cap,
                    "plane": "H/V",
                    "subset_size": f"H={selected_sizes['H']};V={selected_sizes['V']}",
                    "role": "plane-selected legacy/corrected Best-1/Best-N H/V comparison",
                    "source": str(args.legacy_sliding_csv),
                }
            )
    for size in args.subset_sizes:
        pair_keys = {(size, "H"), (size, "V")}
        if pair_keys <= set(paired_legacy_results):
            combined = {
                plane: paired_legacy_results[(size, plane)]
                for plane in ("H", "V")
            }
            combined_metrics = {
                plane: legacy_metrics_by_key[(size, plane)]
                for plane in ("H", "V")
            }
            pair_img = f"ridge_density_legacy_single_vs_best{size}_hv.png"
            pair_cap = f"ridge_density_legacy_single_vs_best{size}_hv_caption.md"
            draw_legacy_pair_hv(out / pair_img, size, combined)
            write_text(out / pair_cap, caption_for_legacy_pair_hv(size, combined_metrics))
            figure_rows.append(
                {
                    "figure": pair_img,
                    "caption_file": pair_cap,
                    "plane": "H/V",
                    "subset_size": size,
                    "role": "paired legacy H/V comparison",
                    "source": str(args.legacy_sliding_csv),
                }
            )
    if legacy_metric_rows:
        write_csv(out / "ridge_density_legacy_comparison_metrics.csv", legacy_metric_rows, LEGACY_COMPARISON_FIELDS)
    loss_rows: list[dict[str, object]] = []
    for plane in args.planes:
        loss_rows.extend(
            estimate_loss_rows(
                plane,
                center_rows,
                args.extraction_range_start_turn,
                args.extraction_range_end_turn,
                args.stride_turns,
                args.subset_sizes,
            )
        )
    write_csv(out / "ridge_density_loss_candidates.csv", loss_rows, LOSS_SUMMARY_FIELDS)
    write_text(out / "ridge_density_h_plane_loss_summary.md", loss_summary_text(loss_rows, args.extraction_range_start_turn, args.extraction_range_end_turn))
    write_csv(out / "ridge_density_warnings.csv", [{"warning": warning} for warning in warnings], WARNING_FIELDS)
    write_csv(out / "ridge_density_best_ensemble_manifest.csv", figure_rows, FIGURE_FIELDS)
    write_text(
        out / "ridge_density_best_ensemble_index.md",
        index_text(
            figure_rows,
            str(args.legacy_sliding_csv or "/home/derekste/tbt-spills-2000-autosweep/elite-full/jobs/18d321dbd4fe/combined"),
            args.turn_start,
            args.turn_end,
            args.window_turns,
            args.stride_turns,
            args.subset_sizes,
            args.ridge_density_normalize,
        ),
    )
    write_text(
        out / "ridge_density_best_ensemble_summary.md",
        "# Best-Ensemble Ridge Density Summary\n\n"
        f"- manifests inspected: `{len(manifests)}`\n"
        f"- backend: `{backend.name}`\n"
        f"- turn range: `{args.turn_start}-{args.turn_end}`\n"
        f"- window/stride: `{args.window_turns}/{args.stride_turns}`\n"
        f"- paired legacy sliding source: `{args.legacy_sliding_csv or 'not requested'}`\n"
        f"- extraction-context variants: `{str(args.extraction_context_variants).lower()}`; the primary density and difference figures are unmarked\n"
        f"- warnings sampled: `{len(warnings)}`\n",
    )
    print(f"OUT={out}")
    print(f"FIGURES={sum(1 for _ in out.glob('*.png'))}")
    print(f"CAPTIONS={sum(1 for _ in out.glob('*_caption.md'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
