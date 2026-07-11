"""Pure-PNG review gallery for the intensity-assisted tune study."""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from .config import plane_band
from .io import ensure_dir, read_csv, write_csv


FIGURE_FIELDS = [
    "category",
    "plane",
    "subset_size",
    "method",
    "path",
    "description",
    "claim_guardrail",
]

METHODS = ("unweighted", "sqrt_intensity", "linear_intensity", "intensity_gate_50pct")

DENSITY_DELTA_NOTE = "RED: HIGHER PICK PROBABILITY; BLUE: LOWER VS UNWEIGHTED"
DENSITY_DELTA_DESCRIPTION = (
    "Exact-common spill/window difference of per-turn, column-normalized "
    "global-ridge-pick distributions; raster color is symmetrically clipped "
    "at absolute P99 for display only."
)
DENSITY_DELTA_GUARDRAIL = (
    "Red/blue are higher/lower ridge-pick probability at exact common "
    "spill/window points; they do not isolate physical noise."
)

METHOD_LABELS = {
    "sqrt_intensity": "SQRT",
    "linear_intensity": "LINEAR",
    "intensity_gate_50pct": "GATE 50 PCT",
}

METRIC_LABELS = {
    "median_peak_prominence_at_train_q": "PEAK PROMINENCE",
    "median_power_support_at_train_q": "TUNE BAND POWER",
    "median_spectral_entropy": "SPECTRAL ENTROPY",
    "median_abs_q_delta_from_train": "ABS Q MINUS TRAIN Q",
}


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _median(values: Sequence[float]) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return math.nan
    middle = len(finite) // 2
    return finite[middle] if len(finite) % 2 else 0.5 * (finite[middle - 1] + finite[middle])


def _percentile(values: Sequence[float], fraction: float) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return math.nan
    position = max(0.0, min(1.0, fraction)) * (len(finite) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return finite[lower]
    return finite[lower] + (finite[upper] - finite[lower]) * (position - lower)


def _poster():
    import bpm_dgx_poster as poster

    return poster


def _polyline(
    pixels: bytearray,
    width: int,
    height: int,
    points: Sequence[tuple[float, float]],
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    area: tuple[int, int, int, int],
    color,
    thickness: int = 1,
) -> None:
    poster = _poster()
    x0, y0, x1, y1 = area
    clean = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    for (xa, ya), (xb, yb) in zip(clean, clean[1:]):
        ax = poster.scale_value(xa, x_range[0], x_range[1], x0, x1)
        ay = poster.scale_value(ya, y_range[0], y_range[1], y1, y0)
        bx = poster.scale_value(xb, x_range[0], x_range[1], x0, x1)
        by = poster.scale_value(yb, y_range[0], y_range[1], y1, y0)
        for offset in range(-(thickness // 2), thickness // 2 + 1):
            poster.line(pixels, width, height, ax, ay + offset, bx, by + offset, color)


def _sequential_color(fraction: float) -> tuple[int, int, int]:
    value = max(0.0, min(1.0, fraction))
    stops = [
        (245, 247, 248),
        (38, 92, 126),
        (36, 151, 138),
        (131, 205, 78),
        (253, 231, 37),
    ]
    position = value * (len(stops) - 1)
    lower = min(len(stops) - 2, int(position))
    amount = position - lower
    return tuple(int(stops[lower][index] + amount * (stops[lower + 1][index] - stops[lower][index])) for index in range(3))


def _diverging_color(value: float, maximum: float) -> tuple[int, int, int]:
    if maximum <= 0:
        return 245, 247, 248
    fraction = max(-1.0, min(1.0, value / maximum))
    neutral = (245, 247, 248)
    target = (190, 72, 72) if fraction > 0 else (44, 123, 182)
    amount = abs(fraction)
    return tuple(int(neutral[index] + amount * (target[index] - neutral[index])) for index in range(3))


def _density(
    rows: Sequence[Mapping[str, object]],
    band: tuple[float, float],
    bins: int,
) -> tuple[list[int], np.ndarray, dict[int, list[float]]]:
    centers = sorted({int(float(str(row.get("center_turn") or 0))) for row in rows})
    center_index = {center: index for index, center in enumerate(centers)}
    density = np.zeros((bins, len(centers)), dtype=np.float32)
    grouped: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        value = _f(row.get("q_global"))
        if not math.isfinite(value) or value < band[0] or value > band[1]:
            continue
        center = int(float(str(row.get("center_turn") or 0)))
        bin_index = int((value - band[0]) / (band[1] - band[0]) * bins)
        bin_index = max(0, min(bins - 1, bin_index))
        density[bin_index, center_index[center]] += 1.0
        grouped[center].append(value)
    return centers, density, grouped


def _normalized_columns(density: np.ndarray) -> np.ndarray:
    total = np.sum(density, axis=0, keepdims=True)
    return density / np.where(total > 0, total, 1.0)


def exact_paired_density_rows(
    baseline_rows: Sequence[Mapping[str, object]],
    method_rows: Sequence[Mapping[str, object]],
    band: tuple[float, float],
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    def keyed(
        rows: Sequence[Mapping[str, object]],
        label: str,
    ) -> dict[tuple[str, str, str, int, int, int], Mapping[str, object]]:
        result = {}
        for row in rows:
            key = (
                str(row.get("collection") or ""),
                str(row.get("spill_id") or ""),
                str(row.get("plane") or ""),
                int(float(str(row.get("subset_size") or 0))),
                int(float(str(row.get("window_index") or 0))),
                int(float(str(row.get("center_turn") or 0))),
            )
            if key in result:
                raise ValueError(f"duplicate {label} intensity ridge point: {key}")
            result[key] = row
        return result

    baseline = keyed(baseline_rows, "unweighted")
    method = keyed(method_rows, "weighted")
    if baseline.keys() != method.keys():
        baseline_only = len(baseline.keys() - method.keys())
        method_only = len(method.keys() - baseline.keys())
        raise ValueError(
            "intensity ridge subtraction requires identical exact spill/window keys: "
            f"unweighted_only={baseline_only} weighted_only={method_only}"
        )
    paired = []
    for key in sorted(baseline):
        baseline_q = _f(baseline[key].get("q_global"))
        method_q = _f(method[key].get("q_global"))
        if not (
            math.isfinite(baseline_q)
            and math.isfinite(method_q)
            and band[0] <= baseline_q <= band[1]
            and band[0] <= method_q <= band[1]
        ):
            continue
        paired.append((baseline[key], method[key]))
    if not paired:
        raise ValueError("intensity ridge subtraction has no exact common finite in-band points")
    return [row[0] for row in paired], [row[1] for row in paired]


def ridge_plot(
    path: Path,
    title: str,
    rows: Sequence[Mapping[str, object]],
    band: tuple[float, float],
    bins: int = 192,
) -> None:
    poster = _poster()
    centers, density, grouped = _density(rows, band, bins)
    if not centers or not np.any(density):
        poster.no_data_png(path, title)
        return
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    positive = density[density > 0]
    maximum = max(1.0, float(np.percentile(positive, 98))) if positive.size else 1.0
    cell_width = max(1, (x1 - x0 + 1) // len(centers))
    cell_height = max(1, (y1 - y0 + 1) // bins)
    for column in range(len(centers)):
        for row_index in range(bins):
            value = float(density[row_index, column])
            color = _sequential_color(value / maximum) if value > 0 else (245, 247, 248)
            left = x0 + column * cell_width
            top = y1 - (row_index + 1) * cell_height
            poster.rect(pixels, width, height, left, top, min(x1, left + cell_width - 1), min(y1, top + cell_height - 1), color)
    x_range = float(min(centers)), float(max(centers) or min(centers) + 1)
    for fraction, thickness, color in ((0.10, 1, (255, 255, 255)), (0.90, 1, (255, 255, 255)), (0.50, 3, (255, 255, 255))):
        points = [(float(center), _percentile(grouped[center], fraction)) for center in centers if grouped[center]]
        _polyline(pixels, width, height, points, x_range, band, (x0, y0, x1, y1), color, thickness)
    poster.draw_text(pixels, width, height, x0, y1 + 8, str(min(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 110, y1 + 8, str(max(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x0, y0 - 28, "COLOR: SPILL COUNT; WHITE: P10 MED P90", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def density_delta_plot(
    path: Path,
    title: str,
    baseline_rows: Sequence[Mapping[str, object]],
    method_rows: Sequence[Mapping[str, object]],
    band: tuple[float, float],
    bins: int = 192,
) -> None:
    poster = _poster()
    baseline_rows, method_rows = exact_paired_density_rows(baseline_rows, method_rows, band)
    centers_a, density_a, grouped_a = _density(baseline_rows, band, bins)
    centers_b, density_b, grouped_b = _density(method_rows, band, bins)
    if not centers_a or centers_a != centers_b:
        raise ValueError("intensity ridge subtraction produced mismatched turn centers")
    difference = _normalized_columns(density_b) - _normalized_columns(density_a)
    finite = np.abs(difference[np.isfinite(difference)])
    maximum = max(1e-6, float(np.percentile(finite, 99))) if finite.size else 1.0
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    cell_width = max(1, (x1 - x0 + 1) // len(centers_a))
    cell_height = max(1, (y1 - y0 + 1) // bins)
    for column in range(len(centers_a)):
        for row_index in range(bins):
            left = x0 + column * cell_width
            top = y1 - (row_index + 1) * cell_height
            color = _diverging_color(float(difference[row_index, column]), maximum)
            poster.rect(pixels, width, height, left, top, min(x1, left + cell_width - 1), min(y1, top + cell_height - 1), color)
    x_range = float(min(centers_a)), float(max(centers_a) or min(centers_a) + 1)
    for grouped, color, thickness in ((grouped_a, poster.INK, 2), (grouped_b, (255, 255, 255), 3)):
        points = [(float(center), _percentile(grouped[center], 0.50)) for center in centers_a if grouped[center]]
        _polyline(pixels, width, height, points, x_range, band, (x0, y0, x1, y1), color, thickness)
    poster.draw_text(pixels, width, height, x0, y0 - 28, DENSITY_DELTA_NOTE, poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 420, y0 - 28, "WHITE: WEIGHTED MED; DARK: UNWEIGHTED MED", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def concentration_plot(
    path: Path,
    title: str,
    rows_by_method: Mapping[str, Sequence[Mapping[str, object]]],
    band: tuple[float, float],
    bins: int = 192,
) -> None:
    poster = _poster()
    series = []
    colors = {
        "unweighted": poster.BLUE,
        "sqrt_intensity": poster.GREEN,
        "linear_intensity": poster.ORANGE,
        "intensity_gate_50pct": poster.PURPLE,
    }
    for method, rows in rows_by_method.items():
        centers, density, _grouped = _density(rows, band, bins)
        points = []
        for index, center in enumerate(centers):
            total = float(np.sum(density[:, index]))
            points.append((float(center), float(np.max(density[:, index])) / total if total else math.nan))
        series.append((method.replace("_intensity", "").replace("intensity_", "")[:18], points, colors[method]))
    poster.line_plot(path, title, series, "TURN", "PEAK BIN FRACTION", (0.0, 1.0))


def binned_scatter_plot(
    path: Path,
    title: str,
    rows: Sequence[Mapping[str, object]],
    y_field: str,
    y_label: str,
) -> None:
    poster = _poster()
    points = [
        (_f(row.get("global_intensity_normalized")), _f(row.get(y_field)))
        for row in rows
        if str(row.get("window_role")) == "test"
    ]
    points = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if not points:
        poster.no_data_png(path, title)
        return
    xmin = max(0.0, _percentile([point[0] for point in points], 0.01))
    xmax = _percentile([point[0] for point in points], 0.99)
    ymin = _percentile([point[1] for point in points], 0.01)
    ymax = _percentile([point[1] for point in points], 0.99)
    xbins, ybins = 80, 70
    histogram = np.zeros((ybins, xbins), dtype=np.float32)
    grouped: dict[int, list[float]] = defaultdict(list)
    for x, y in points:
        x_index = max(0, min(xbins - 1, int((x - xmin) / max(1e-12, xmax - xmin) * xbins)))
        y_index = max(0, min(ybins - 1, int((y - ymin) / max(1e-12, ymax - ymin) * ybins)))
        histogram[y_index, x_index] += 1
        grouped[x_index].append(y)
    width, height = 1280, 820
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "NORMALIZED INTENSITY", y_label)
    maximum = max(1.0, float(np.percentile(histogram[histogram > 0], 98))) if np.any(histogram > 0) else 1.0
    cell_width = max(1, (x1 - x0 + 1) // xbins)
    cell_height = max(1, (y1 - y0 + 1) // ybins)
    for x_index in range(xbins):
        for y_index in range(ybins):
            value = float(histogram[y_index, x_index])
            color = _sequential_color(value / maximum) if value else (245, 247, 248)
            left = x0 + x_index * cell_width
            top = y1 - (y_index + 1) * cell_height
            poster.rect(pixels, width, height, left, top, min(x1, left + cell_width - 1), min(y1, top + cell_height - 1), color)
    median_points = [
        (xmin + (index + 0.5) / xbins * (xmax - xmin), _median(values))
        for index, values in sorted(grouped.items())
    ]
    _polyline(pixels, width, height, median_points, (xmin, xmax), (ymin, ymax), (x0, y0, x1, y1), (255, 255, 255), 3)
    poster.draw_numeric_axis_labels(pixels, width, height, (x0, y0, x1, y1), (xmin, xmax), (ymin, ymax))
    poster.draw_text(pixels, width, height, x0, y0 - 28, "COLOR: WINDOW COUNT; WHITE: BIN MEDIAN", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def loss_scatter_plot(path: Path, title: str, rows: Sequence[Mapping[str, object]]) -> None:
    poster = _poster()
    points = [(_f(row.get("intensity_crossing_turn")), _f(row.get("power_support_loss_turn"))) for row in rows]
    points = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if not points:
        poster.no_data_png(path, title)
        return
    width, height = 1100, 820
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "INTENSITY CROSSING TURN", "POWER LOSS TURN")
    minimum = min([value for point in points for value in point])
    maximum = max([value for point in points for value in point])
    poster.line(pixels, width, height, x0, y1, x1, y0, poster.MUTED)
    for x, y in points:
        px = poster.scale_value(x, minimum, maximum, x0, x1)
        py = poster.scale_value(y, minimum, maximum, y1, y0)
        poster.rect(pixels, width, height, px - 2, py - 2, px + 2, py + 2, poster.BLUE)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (minimum, maximum),
        (minimum, maximum),
    )
    poster.draw_text(
        pixels,
        width,
        height,
        x0,
        y0 - 28,
        f"N {len(points)}; DIAGONAL: EQUAL TURNS; ASSOCIATION IS NOT CAUSATION",
        poster.MUTED,
        2,
    )
    poster.write_png(path, width, height, pixels)


def method_effect_plot(
    path: Path,
    title: str,
    rows: Sequence[Mapping[str, object]],
    normalized_to_practical_effect: bool = False,
) -> None:
    """Draw block-bootstrap intervals for intensity methods against unweighted."""
    poster = _poster()
    selected = [row for row in rows if row.get("method") in METHOD_LABELS]
    if not selected:
        poster.no_data_png(path, title)
        return
    practical = max((_f(row.get("minimum_practical_effect")) for row in selected), default=math.nan)
    direction = str(selected[0].get("beneficial_direction") or "increase")
    sign = -1.0 if direction == "decrease" else 1.0
    plotted: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    for row in selected:
        x = _f(row.get("subset_size"))
        center = _f(row.get("median_paired_delta"))
        low = _f(row.get("bootstrap_ci_low"))
        high = _f(row.get("bootstrap_ci_high"))
        if not all(math.isfinite(value) for value in (x, center, low, high)):
            continue
        if normalized_to_practical_effect:
            if not math.isfinite(practical) or practical <= 0:
                continue
            center *= sign / practical
            transformed = sorted((low * sign / practical, high * sign / practical))
            low, high = transformed
        plotted[str(row["method"])].append((x, center, low, high))
    points = [point for method_points in plotted.values() for point in method_points]
    if not points:
        poster.no_data_png(path, title)
        return
    xmin = min(point[0] for point in points)
    xmax = max(point[0] for point in points)
    values = [value for point in points for value in point[1:]] + [0.0]
    if normalized_to_practical_effect:
        ymin = min(-0.15, min(values) * 1.10)
        ymax = max(1.05, max(values) * 1.10)
    else:
        bound = max(abs(value) for value in values) * 1.15 or 0.01
        ymin, ymax = -bound, bound
    width, height = 1280, 720
    pixels = poster.new_canvas(width, height)
    y_label = "EFFECT RATIO" if normalized_to_practical_effect else "PAIRED DELTA"
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "SUBSET SIZE N", y_label)
    zero_y = poster.scale_value(0.0, ymin, ymax, y1, y0)
    poster.line(pixels, width, height, x0, zero_y, x1, zero_y, poster.INK)
    if normalized_to_practical_effect:
        threshold_y = poster.scale_value(1.0, ymin, ymax, y1, y0)
        poster.line(pixels, width, height, x0, threshold_y, x1, threshold_y, poster.RED)
    colors = {
        "sqrt_intensity": poster.GREEN,
        "linear_intensity": poster.ORANGE,
        "intensity_gate_50pct": poster.PURPLE,
    }
    legend_x = x1 - 220
    legend_y = y0 + 14
    for index, method in enumerate(METHOD_LABELS):
        method_points = sorted(plotted.get(method, []))
        if not method_points:
            continue
        color = colors[method]
        centers = [(point[0], point[1]) for point in method_points]
        _polyline(pixels, width, height, centers, (xmin, xmax), (ymin, ymax), (x0, y0, x1, y1), color)
        for x, center, low, high in method_points:
            px = poster.scale_value(x, xmin, xmax, x0, x1)
            py = poster.scale_value(center, ymin, ymax, y1, y0)
            low_y = poster.scale_value(low, ymin, ymax, y1, y0)
            high_y = poster.scale_value(high, ymin, ymax, y1, y0)
            poster.line(pixels, width, height, px, low_y, px, high_y, color)
            poster.line(pixels, width, height, px - 4, low_y, px + 4, low_y, color)
            poster.line(pixels, width, height, px - 4, high_y, px + 4, high_y, color)
            poster.rect(pixels, width, height, px - 3, py - 3, px + 3, py + 3, color)
        poster.rect(pixels, width, height, legend_x, legend_y + index * 22, legend_x + 14, legend_y + 12 + index * 22, color)
        poster.draw_text(pixels, width, height, legend_x + 22, legend_y + index * 22, METHOD_LABELS[method], poster.MUTED, 2)
    poster.draw_numeric_axis_labels(pixels, width, height, (x0, y0, x1, y1), (xmin, xmax), (ymin, ymax), x_ticks=2)
    for value in sorted({point[0] for point in points} - {xmin, xmax}):
        label = str(int(value)) if abs(value - round(value)) < 1e-9 else f"{value:g}"
        px = poster.scale_value(value, xmin, xmax, x0, x1)
        poster.draw_text(pixels, width, height, px - len(label) * 4, y1 + 8, label, poster.MUTED, 2)
    if normalized_to_practical_effect:
        note = "BARS: BLOCK BOOTSTRAP 95 PCT CI; RED: MIN PRACTICAL EFFECT"
    else:
        note = f"BARS: BLOCK BOOTSTRAP 95 PCT CI; MIN PRACTICAL {direction.upper()} {practical:.4g}"
    poster.draw_text(pixels, width, height, x0, y0 - 28, note[:68], poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def representative_overlays(
    rows: Sequence[Mapping[str, object]],
    correlation_rows: Sequence[Mapping[str, object]],
    out: Path,
    figures: list[dict[str, object]],
) -> None:
    poster = _poster()
    correlations: dict[tuple[str, int], list[tuple[float, str, str]]] = defaultdict(list)
    for row in correlation_rows:
        if row.get("metric") == "power_support_at_train_q" and str(row.get("lag_windows")) == "0":
            correlations[(str(row["plane"]), int(row["subset_size"]))].append(
                (_f(row.get("spearman_rho")), str(row["collection"]), str(row["spill_id"]))
            )
    for (plane, subset_size), values in sorted(correlations.items()):
        finite = sorted(value for value in values if math.isfinite(value[0]))
        if not finite:
            continue
        choices = (("negative", finite[0]), ("median", finite[len(finite) // 2]), ("positive", finite[-1]))
        for label, (_rho, collection, spill_id) in choices:
            selected = [
                row
                for row in rows
                if row.get("plane") == plane
                and int(row.get("subset_size") or 0) == subset_size
                and row.get("method") == "unweighted"
                and row.get("collection") == collection
                and row.get("spill_id") == spill_id
                and str(row.get("window_role")) == "test"
            ]
            selected.sort(key=lambda row: _f(row.get("center_turn")))
            intensity = [_f(row.get("global_intensity_normalized")) for row in selected]
            power = [_f(row.get("power_support_at_train_q")) for row in selected]
            power_reference = _median(power[:5])
            power_normalized = [value / power_reference if power_reference > 0 else math.nan for value in power]
            centers = [_f(row.get("center_turn")) for row in selected]
            path = out / "representative" / f"{plane.lower()}_best{subset_size}_{label}_{spill_id}.png"
            poster.line_plot(
                path,
                f"{plane} BEST{subset_size} {label.upper()} INTENSITY ASSOCIATION",
                [
                    ("INTENSITY", list(zip(centers, intensity)), poster.BLUE),
                    ("POWER SUPPORT", list(zip(centers, power_normalized)), poster.ORANGE),
                ],
                "TURN",
                "NORMALIZED",
            )
            figures.append(
                {
                    "category": "representative_overlay",
                    "plane": plane,
                    "subset_size": subset_size,
                    "method": "unweighted",
                    "path": str(path),
                    "description": f"Representative {label} within-spill rank association between normalized intensity and tune-band power support.",
                    "claim_guardrail": "Selected by correlation rank for inspection; not an unbiased effect-size estimate.",
                }
            )


def make_intensity_gallery(
    cfg: dict[str, object],
    inputs: Path,
    out: Path,
) -> None:
    poster = _poster()
    window_path = inputs / "intensity_window_metrics.csv"
    windows = read_csv(window_path) if window_path.exists() else []
    effects = read_csv(inputs / "intensity_method_effects.csv")
    correlations = read_csv(inputs / "intensity_visibility_correlations.csv")
    correlation_summary = read_csv(inputs / "intensity_visibility_correlation_summary.csv")
    losses = read_csv(inputs / "intensity_loss_turns.csv")
    integrity = read_csv(inputs / "intensity_payload_integrity.csv")
    ensure_dir(out)
    figures: list[dict[str, object]] = []

    horizon_values = [
        _f(row.get("first_bad_block_turn")) if math.isfinite(_f(row.get("first_bad_block_turn"))) else _f(row.get("sample_count"))
        for row in integrity
    ]
    path = out / "integrity" / "intensity_valid_horizon_histogram.png"
    poster.hist_plot(
        path,
        "RAW INTENSITY VALID HORIZON",
        horizon_values,
        "FIRST BAD BLOCK TURN",
        bins=50,
        note="ANALYSIS STOPS AT 50000; FORMAT DIAGNOSTIC, NOT BEAM LOSS",
    )
    figures.append(
        {
            "category": "integrity",
            "plane": "",
            "subset_size": "",
            "method": "",
            "path": str(path),
            "description": "Distribution of the first 1024-turn block with less than 99% finite/plausible raw intensity.",
            "claim_guardrail": "A payload-format integrity diagnostic, not a beam-loss distribution.",
        }
    )

    subset_sizes = sorted(
        {
            int(row["subset_size"])
            for row in (*windows, *effects, *correlation_summary, *losses)
            if row.get("subset_size") not in {None, ""}
        }
    )
    window_subset_sizes = subset_sizes if windows else []
    for plane in ("H", "V"):
        band = plane_band(cfg, plane)
        for subset_size in window_subset_sizes:
            rows_by_method = {
                method: [
                    row
                    for row in windows
                    if row.get("plane") == plane and int(row.get("subset_size") or 0) == subset_size and row.get("method") == method
                ]
                for method in METHODS
            }
            for method, rows in rows_by_method.items():
                path = out / "ridge_density" / f"{plane.lower()}_best{subset_size}_{method}.png"
                ridge_plot(path, f"{plane} BEST{subset_size} {method.replace('_', ' ')} RIDGE DENSITY", rows, band)
                figures.append(
                    {
                        "category": "ridge_density",
                        "plane": plane,
                        "subset_size": subset_size,
                        "method": method,
                        "path": str(path),
                        "description": "Global spectral maximum per spill/window; color is spill count and white lines are P10/median/P90.",
                        "claim_guardrail": "Shows repeatability of the strongest in-band ridge, not absolute tune accuracy.",
                    }
                )
                if method != "unweighted":
                    delta_path = out / "ridge_delta" / f"{plane.lower()}_best{subset_size}_{method}_minus_unweighted.png"
                    density_delta_plot(
                        delta_path,
                        f"{plane} BEST{subset_size} {method.replace('_', ' ')} MINUS UNWEIGHTED",
                        rows_by_method["unweighted"],
                        rows,
                        band,
                    )
                    figures.append(
                        {
                            "category": "ridge_density_difference",
                            "plane": plane,
                            "subset_size": subset_size,
                            "method": method,
                            "path": str(delta_path),
                            "description": DENSITY_DELTA_DESCRIPTION,
                            "claim_guardrail": DENSITY_DELTA_GUARDRAIL,
                        }
                    )
            path = out / "concentration" / f"{plane.lower()}_best{subset_size}_method_concentration.png"
            concentration_plot(path, f"{plane} BEST{subset_size} RIDGE CONCENTRATION BY METHOD", rows_by_method, band)
            figures.append(
                {
                    "category": "ridge_concentration",
                    "plane": plane,
                    "subset_size": subset_size,
                    "method": "comparison",
                    "path": str(path),
                    "description": "Fraction of spill ridges occupying the most populated tune bin at each turn.",
                    "claim_guardrail": "Higher is narrower only at the declared fixed binning; compare methods at identical bins.",
                }
            )
            baseline = rows_by_method["unweighted"]
            for field, label in (("peak_prominence_at_train_q", "PROMINENCE"), ("power_support_at_train_q", "POWER SUPPORT")):
                scatter_path = out / "intensity_relationship" / f"{plane.lower()}_best{subset_size}_intensity_vs_{field}.png"
                binned_scatter_plot(scatter_path, f"{plane} BEST{subset_size} INTENSITY VS {label}", baseline, field, label)
                figures.append(
                    {
                        "category": "intensity_relationship",
                        "plane": plane,
                        "subset_size": subset_size,
                        "method": "unweighted",
                        "path": str(scatter_path),
                        "description": f"Binned later-window relationship between normalized intensity and {label.lower()}.",
                        "claim_guardrail": "Pooled windows are autocorrelated and turn-confounded; spill-level rank correlations are the inferential result.",
                    }
                )

    for plane in ("H", "V"):
        for metric in ("median_peak_prominence_at_train_q", "median_power_support_at_train_q", "median_spectral_entropy", "median_abs_q_delta_from_train"):
            metric_rows = [row for row in effects if row.get("plane") == plane and row.get("metric") == metric]
            metric_label = METRIC_LABELS[metric]
            path = out / "method_effects" / f"{plane.lower()}_{metric}_paired_delta.png"
            method_effect_plot(path, f"{plane} INTENSITY EFFECT: {metric_label}", metric_rows)
            figures.append(
                {
                    "category": "method_effect",
                    "plane": plane,
                    "subset_size": "all",
                    "method": "comparison",
                    "path": str(path),
                    "description": "Median spill-level paired difference from unweighted with moving-block bootstrap intervals.",
                    "claim_guardrail": "Intervals account for spill ordering within collection; use the FDR q-values and practical threshold together for inference.",
                }
            )
            ratio_path = out / "method_effects" / f"{plane.lower()}_{metric}_practical_fraction.png"
            method_effect_plot(
                ratio_path,
                f"{plane} PRACTICAL EFFECT: {metric_label}",
                metric_rows,
                normalized_to_practical_effect=True,
            )
            figures.append(
                {
                    "category": "method_effect_practical_fraction",
                    "plane": plane,
                    "subset_size": "all",
                    "method": "comparison",
                    "path": str(ratio_path),
                    "description": "Beneficial-direction paired effect divided by the predeclared minimum practical effect; red marks one practical-effect unit.",
                    "claim_guardrail": "Crossing the practical threshold is necessary but not sufficient; statistical significance and tune-shift tolerance must also pass.",
                }
            )
        for subset_size in subset_sizes:
            lag_series = []
            for metric, label, color in (
                ("peak_prominence_at_train_q", "PEAK PROMINENCE", poster.GREEN),
                ("power_support_at_train_q", "POWER SUPPORT", poster.ORANGE),
            ):
                points = [
                    (float(row["lag_windows"]), _f(row.get("median_spearman_rho")))
                    for row in correlation_summary
                    if row.get("plane") == plane and int(row.get("subset_size") or 0) == subset_size and row.get("metric") == metric
                ]
                lag_series.append((label, points, color))
            lag_path = out / "correlation" / f"{plane.lower()}_best{subset_size}_lag_correlation.png"
            poster.line_plot(lag_path, f"{plane} BEST{subset_size} INTENSITY LAG CORRELATION", lag_series, "LAG WINDOWS", "MEDIAN SPEARMAN", (-1.0, 1.0))
            figures.append(
                {
                    "category": "lag_correlation",
                    "plane": plane,
                    "subset_size": subset_size,
                    "method": "unweighted",
                    "path": str(lag_path),
                    "description": "Median within-spill Spearman correlation versus lag; positive lag means intensity precedes the tune metric.",
                    "claim_guardrail": "Exploratory temporal association; overlapping windows preclude treating lag points as independent.",
                }
            )
            for threshold in (0.75, 0.50, 0.25):
                selected_losses = [
                    row
                    for row in losses
                    if row.get("plane") == plane
                    and int(row.get("subset_size") or 0) == subset_size
                    and abs(_f(row.get("threshold_fraction")) - threshold) < 1e-6
                ]
                loss_path = out / "loss_turn" / f"{plane.lower()}_best{subset_size}_threshold_{int(threshold * 100)}.png"
                loss_scatter_plot(loss_path, f"{plane} BEST{subset_size} {int(threshold * 100)} PCT CROSSING TURNS", selected_losses)
                figures.append(
                    {
                        "category": "loss_turn",
                        "plane": plane,
                        "subset_size": subset_size,
                        "method": "unweighted",
                        "path": str(loss_path),
                        "description": "Per-spill intensity-envelope crossing versus tune-band power-support loss turn.",
                        "claim_guardrail": "Threshold sensitivity is shown explicitly; absent crossings are omitted and no fixed extraction start is assumed.",
                    }
                )

    if windows:
        representative_overlays(windows, correlations, out, figures)
    write_csv(out / "figure_manifest.csv", figures, FIGURE_FIELDS)
