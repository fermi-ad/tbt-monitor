"""Native-PNG review plots for the leakage-controlled all-training baseline."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Mapping, Sequence

from .io import write_csv


PLOT_MANIFEST_FIELDS = [
    "filename",
    "plane",
    "plot_type",
    "metric",
    "width",
    "height",
    "caption",
]
METRICS = (
    ("blind_agreement", "BLIND AGREEMENT", "HIGHER IS BETTER"),
    ("blind_abs_q_delta", "BLIND Q ERROR", "LOWER IS BETTER"),
    ("later_prominence", "LATER PROMINENCE", "HIGHER IS BETTER"),
    ("later_power", "LATER POWER SUPPORT", "HIGHER IS BETTER"),
)
METHODS = (
    ("all_training_mean", "ALL-TRAINING MEAN"),
    ("all_training_median", "ALL-TRAINING MEDIAN"),
)


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _axis_range(values: Sequence[float], include_zero: bool = False) -> tuple[float, float]:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return 0.0, 1.0
    low, high = min(finite), max(finite)
    if include_zero:
        low, high = min(0.0, low), max(0.0, high)
    span = high - low
    pad = span * 0.08 if span > 0 else max(1e-4, abs(high) * 0.08, 0.01)
    if include_zero and low == 0.0:
        low = 0.0
    else:
        low -= pad
    high += pad
    return low, high


def _method_color(poster, method: str) -> tuple[int, int, int]:
    return poster.BLUE if method == "all_training_mean" else poster.RED


def _paired_scatter(
    poster,
    path: Path,
    plane: str,
    metric: str,
    label: str,
    direction_note: str,
    rows: Sequence[Mapping[str, object]],
) -> tuple[int, int]:
    plane_rows = [row for row in rows if row.get("plane") == plane and row.get("metric") == metric]
    if not plane_rows:
        poster.no_data_png(path, f"{plane} {label}")
        return 1400, 860
    selected_n = int(plane_rows[0].get("selected_n") or 0)
    values = [
        value
        for row in plane_rows
        for value in (_f(row.get("baseline_value")), _f(row.get("selected_value")))
        if math.isfinite(value)
    ]
    low, high = _axis_range(values, include_zero=True)
    width, height = 1400, 860
    pixels = poster.new_canvas(width, height)
    title = f"{plane} BEST-{selected_n} / ALL TRAIN: {label}"
    x0, y0, x1, y1 = poster.draw_axes(
        pixels,
        width,
        height,
        title,
        "ALL TRAIN",
        "BEST-N",
    )
    poster.draw_text(
        pixels,
        width,
        height,
        x0,
        y0 - 30,
        f"ONE POINT PER EXACT PAIRED SPILL; FOLDS COLLAPSED; {direction_note}",
        poster.MUTED,
        2,
    )
    diagonal_low = poster.scale_value(low, low, high, x0, x1)
    diagonal_high = poster.scale_value(high, low, high, x0, x1)
    poster.line(pixels, width, height, diagonal_low, y1, diagonal_high, y0, poster.GRID)
    poster.line(pixels, width, height, diagonal_low, y1 + 1, diagonal_high, y0 + 1, poster.GRID)
    for row in plane_rows:
        baseline = _f(row.get("baseline_value"))
        selected = _f(row.get("selected_value"))
        if not math.isfinite(baseline) or not math.isfinite(selected):
            continue
        px = poster.scale_value(baseline, low, high, x0, x1)
        py = poster.scale_value(selected, low, high, y1, y0)
        color = _method_color(poster, str(row.get("baseline_method", "")))
        poster.rect(pixels, width, height, px - 2, py - 2, px + 2, py + 2, color)
    legend_x, legend_y = x1 - 310, y0 + 15
    for index, (method, method_label) in enumerate(METHODS):
        color = _method_color(poster, method)
        y = legend_y + index * 27
        poster.rect(pixels, width, height, legend_x, y, legend_x + 16, y + 12, color)
        poster.draw_text(pixels, width, height, legend_x + 24, y, method_label, poster.MUTED, 2)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (low, high),
        (low, high),
    )
    poster.write_png(path, width, height, pixels)
    return width, height


def _favorable_cdf(
    poster,
    path: Path,
    plane: str,
    metric: str,
    label: str,
    rows: Sequence[Mapping[str, object]],
) -> tuple[int, int]:
    plane_rows = [row for row in rows if row.get("plane") == plane and row.get("metric") == metric]
    if not plane_rows:
        poster.no_data_png(path, f"{plane} {label} FAVORABLE DELTA")
        return 1400, 800
    selected_n = int(plane_rows[0].get("selected_n") or 0)
    values = [_f(row.get("favorable_delta")) for row in plane_rows]
    low, high = _axis_range(values, include_zero=True)
    width, height = 1400, 800
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(
        pixels,
        width,
        height,
        f"{plane} BEST-{selected_n}: {label} DELTA CDF",
        "FAVORABLE DELTA",
        "CDF",
    )
    poster.draw_text(
        pixels,
        width,
        height,
        x0,
        y0 - 30,
        "POSITIVE FAVORS BEST-N; ONE VALUE PER EXACT PAIRED SPILL; FOLDS COLLAPSED",
        poster.MUTED,
        2,
    )
    if low <= 0.0 <= high:
        zero_x = poster.scale_value(0.0, low, high, x0, x1)
        poster.line(pixels, width, height, zero_x, y0, zero_x, y1, poster.INK)
    for method, method_label in METHODS:
        method_values = sorted(
            _f(row.get("favorable_delta"))
            for row in plane_rows
            if row.get("baseline_method") == method
            and math.isfinite(_f(row.get("favorable_delta")))
        )
        color = _method_color(poster, method)
        points: list[tuple[int, int]] = []
        for index, value in enumerate(method_values, start=1):
            px = poster.scale_value(value, low, high, x0, x1)
            py = poster.scale_value(index / len(method_values), 0.0, 1.0, y1, y0)
            points.append((px, py))
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            poster.line(pixels, width, height, ax, ay, bx, by, color)
            poster.line(pixels, width, height, ax, ay + 1, bx, by + 1, color)
        if points:
            for px, py in points[:: max(1, len(points) // 100)]:
                poster.rect(pixels, width, height, px - 1, py - 1, px + 1, py + 1, color)
    legend_x, legend_y = x1 - 310, y0 + 15
    for index, (method, method_label) in enumerate(METHODS):
        color = _method_color(poster, method)
        y = legend_y + index * 27
        poster.rect(pixels, width, height, legend_x, y, legend_x + 16, y + 12, color)
        poster.draw_text(pixels, width, height, legend_x + 24, y, method_label, poster.MUTED, 2)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (low, high),
        (0.0, 1.0),
    )
    poster.write_png(path, width, height, pixels)
    return width, height


def _scoreboard(
    poster,
    path: Path,
    plane: str,
    rows: Sequence[Mapping[str, object]],
) -> tuple[int, int]:
    plane_rows = [row for row in rows if row.get("plane") == plane]
    selected_n = int(plane_rows[0].get("selected_n") or 0) if plane_rows else 0
    width, height = 1700, 760
    pixels = poster.new_canvas(width, height)
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        28,
        f"{plane} BEST-{selected_n} VS ALL-TRAINING CONTROL",
        poster.INK,
        3,
    )
    poster.draw_text(
        pixels,
        width,
        height,
        34,
        68,
        "GREEN SELECTED FAVORED; RED ALL-TRAINING FAVORED; GRAY UNRESOLVED; 95 PCT PAIRED BLOCK CI",
        poster.MUTED,
        2,
    )
    x0, y0, x1, y1 = 300, 150, width - 45, height - 70
    columns = len(METRICS)
    rows_count = len(METHODS)
    keyed = {
        (str(row.get("baseline_method", "")), str(row.get("metric", ""))): row
        for row in plane_rows
    }
    for column, (_metric, label, direction_note) in enumerate(METRICS):
        cx0 = x0 + round(column * (x1 - x0 + 1) / columns)
        cx1 = x0 + round((column + 1) * (x1 - x0 + 1) / columns) - 1
        center = (cx0 + cx1) // 2
        poster.draw_text(pixels, width, height, center - len(label) * 4, 108, label, poster.MUTED, 2)
        poster.draw_text(
            pixels,
            width,
            height,
            center - len(direction_note) * 3,
            130,
            direction_note,
            poster.MUTED,
            1,
        )
    result_colors = {
        "SELECTED_FAVORED": (214, 237, 221),
        "BASELINE_FAVORED": (248, 220, 217),
        "UNRESOLVED": (232, 235, 238),
    }
    for row_index, (method, method_label) in enumerate(METHODS):
        cy0 = y0 + round(row_index * (y1 - y0 + 1) / rows_count)
        cy1 = y0 + round((row_index + 1) * (y1 - y0 + 1) / rows_count) - 1
        poster.draw_text(pixels, width, height, 28, (cy0 + cy1) // 2 - 8, method_label, poster.MUTED, 2)
        for column, (metric, _label, _direction_note) in enumerate(METRICS):
            cx0 = x0 + round(column * (x1 - x0 + 1) / columns)
            cx1 = x0 + round((column + 1) * (x1 - x0 + 1) / columns) - 1
            row = keyed.get((method, metric), {})
            result = str(row.get("result", "UNRESOLVED"))
            poster.rect(pixels, width, height, cx0 + 2, cy0 + 2, cx1 - 2, cy1 - 2, result_colors[result])
            result_label = result.replace("_", " ")
            poster.draw_text(pixels, width, height, cx0 + 14, cy0 + 18, result_label, poster.INK, 2)
            poster.draw_text(
                pixels,
                width,
                height,
                cx0 + 14,
                cy0 + 62,
                f"BEST {row.get('selected_estimate', '')}  ALL {row.get('baseline_estimate', '')}"[:46],
                poster.MUTED,
                2,
            )
            poster.draw_text(
                pixels,
                width,
                height,
                cx0 + 14,
                cy0 + 98,
                f"DELTA {row.get('paired_delta_selected_minus_baseline', '')}"[:46],
                poster.MUTED,
                2,
            )
            poster.draw_text(
                pixels,
                width,
                height,
                cx0 + 14,
                cy0 + 134,
                f"CI {row.get('paired_delta_ci_low', '')} TO {row.get('paired_delta_ci_high', '')}"[:46],
                poster.MUTED,
                2,
            )
            poster.line(pixels, width, height, cx0, cy0, cx1, cy0, poster.GRID)
            poster.line(pixels, width, height, cx0, cy1, cx1, cy1, poster.GRID)
            poster.line(pixels, width, height, cx0, cy0, cx0, cy1, poster.GRID)
            poster.line(pixels, width, height, cx1, cy0, cx1, cy1, poster.GRID)
    poster.write_png(path, width, height, pixels)
    return width, height


def write_plots(
    paired_rows: Sequence[Mapping[str, object]],
    comparison_rows: Sequence[Mapping[str, object]],
    out: Path,
) -> list[dict[str, object]]:
    import bpm_dgx_poster as poster

    out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for plane in ("H", "V"):
        scoreboard_name = f"best_n_vs_all_training_{plane.lower()}_scoreboard.png"
        width, height = _scoreboard(poster, out / scoreboard_name, plane, comparison_rows)
        manifest.append(
            {
                "filename": scoreboard_name,
                "plane": plane,
                "plot_type": "scoreboard",
                "metric": "all",
                "width": width,
                "height": height,
                "caption": "Leakage-controlled Best-N versus all available training channels under the same purged-window and held-out-digitizer protocol.",
            }
        )
        for metric, label, direction_note in METRICS:
            scatter_name = f"best_n_vs_all_training_{plane.lower()}_{metric}_paired_scatter.png"
            width, height = _paired_scatter(
                poster,
                out / scatter_name,
                plane,
                metric,
                label,
                direction_note,
                paired_rows,
            )
            manifest.append(
                {
                    "filename": scatter_name,
                    "plane": plane,
                    "plot_type": "paired_scatter",
                    "metric": metric,
                    "width": width,
                    "height": height,
                    "caption": "Exact spill-paired selected Best-N and all-training baseline values after collapsing held-out folds within spill.",
                }
            )
            cdf_name = f"best_n_vs_all_training_{plane.lower()}_{metric}_favorable_delta_cdf.png"
            width, height = _favorable_cdf(
                poster,
                out / cdf_name,
                plane,
                metric,
                label,
                paired_rows,
            )
            manifest.append(
                {
                    "filename": cdf_name,
                    "plane": plane,
                    "plot_type": "favorable_delta_cdf",
                    "metric": metric,
                    "width": width,
                    "height": height,
                    "caption": "Empirical distribution of exact spill-paired deltas, sign-oriented so positive values favor selected Best-N.",
                }
            )
    write_csv(out / "all_training_plot_manifest.csv", manifest, PLOT_MANIFEST_FIELDS)
    return manifest
