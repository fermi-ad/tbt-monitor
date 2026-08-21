#!/usr/bin/env python3
"""Render the IBIC paper figures with publication-scale typography.

Line art is emitted as vector PDF through SVG.  Ridge-density panels embed
only the heat-map cells as PNG; axes, labels, quantile tracks, and population
traces remain vector in the paper PDF.
"""

from __future__ import annotations

import argparse
import base64
import csv
import math
import shutil
import struct
import subprocess
import zlib
from array import array
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from xml.sax.saxutils import escape


INK = "#25292d"
MUTED = "#667079"
GRID = "#dce1e5"
BLUE = "#2377b4"
GREEN = "#20956f"
NULL = "#9aa4ad"
NULL_FILL = "#d9dee2"
WHITE = "#ffffff"
BACKGROUND = "#ffffff"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def finite(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def percentile(values: Iterable[float], fraction: float) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return math.nan
    position = min(1.0, max(0.0, fraction)) * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)


def _svg_root(width: float, height: float, body: Sequence[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}pt" height="{height}pt" '
        f'viewBox="0 0 {width} {height}">\n'
        "<style>text{font-family:Arial,Helvetica,sans-serif;fill:#25292d}"
        ".small{font-size:7.5px}.label{font-size:8px}.title{font-size:9px;font-weight:700}"
        ".subtitle{font-size:7.5px;fill:#667079}</style>\n"
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>\n'
        + "\n".join(body)
        + "\n</svg>\n"
    )


def _text(x: float, y: float, value: str, cls: str = "label", anchor: str = "start", **attrs: object) -> str:
    extra = " ".join(f'{key.replace("_", "-")}="{escape(str(val))}"' for key, val in attrs.items())
    # CairoSVG/rsvg can emit a non-subset Type-3 fallback font for literal
    # space glyphs. Separate words with positioned tspans instead: the visual
    # gap and extracted word boundary remain, while all glyphs stay in the
    # embedded/subset Arial face required by the paper gate.
    words = value.split()
    rendered = escape(words[0]) if words else ""
    rendered += "".join(f'<tspan dx="3">{escape(word)}</tspan>' for word in words[1:])
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" class="{cls}" text-anchor="{anchor}" {extra}>'
        f"{rendered}</text>"
    )


def _polyline(points: Sequence[tuple[float, float]], color: str, width: float = 1.2, dash: str = "") -> str:
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}"{dashed}/>'


def _polygon(points: Sequence[tuple[float, float]], fill: str, opacity: float = 1.0) -> str:
    coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return f'<polygon points="{coords}" fill="{fill}" fill-opacity="{opacity}" stroke="none"/>'


def _render(svg_path: Path, pdf_path: Path | None, png_path: Path | None, png_size: tuple[int, int]) -> None:
    executable = shutil.which("rsvg-convert")
    if not executable:
        raise RuntimeError("rsvg-convert is required for publication figure rendering")
    if pdf_path is not None:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [executable, "--format", "pdf", "--output", str(pdf_path), str(svg_path)],
            check=True,
        )
    if png_path is not None:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                executable,
                "--format",
                "png",
                "--width",
                str(png_size[0]),
                "--height",
                str(png_size[1]),
                "--output",
                str(png_path),
                str(svg_path),
            ],
            check=True,
        )


def _axis_ticks(low: float, high: float, count: int) -> list[float]:
    if count < 2 or high <= low:
        return [low]
    return [low + index * (high - low) / (count - 1) for index in range(count)]


def _best_n_panel(
    body: list[str],
    plane: str,
    summary_rows: Sequence[Mapping[str, object]],
    null_rows: Sequence[Mapping[str, object]],
    selected_n: int,
    area: tuple[float, float, float, float],
    y_max: float,
    show_y_labels: bool,
) -> None:
    x0, y0, x1, y1 = area
    observed = sorted(
        (row for row in summary_rows if str(row.get("plane")) == plane),
        key=lambda row: int(row["subset_size"]),
    )
    null_by_n = {
        int(row["subset_size"]): row
        for row in null_rows
        if str(row.get("plane")) == plane and str(row.get("status", "ok")).lower() == "ok"
    }
    if not observed or not null_by_n:
        raise ValueError(f"missing Best-N/null rows for plane {plane}")
    sizes = [int(row["subset_size"]) for row in observed]
    x_min, x_max = min(sizes), max(sizes)

    def sx(value: float) -> float:
        return x0 + (value - x_min) * (x1 - x0) / max(1.0, x_max - x_min)

    def sy(value: float) -> float:
        return y1 - value * (y1 - y0) / y_max

    body.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="#fbfcfd"/>')
    for value in _axis_ticks(0.0, y_max, 4):
        y = sy(value)
        body.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{GRID}" stroke-width="0.6"/>')
        if show_y_labels:
            body.append(_text(x0 - 5, y + 2.7, f"{100*value:.0f}", "small", "end"))
    tick_sizes = sorted({sizes[0], sizes[-1], selected_n, *[size for size in sizes if size % 10 == 0]})
    for size in tick_sizes:
        x = sx(size)
        body.append(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y1+3}" stroke="{INK}" stroke-width="0.7"/>')
        body.append(_text(x, y1 + 11, str(size), "small", "middle"))

    common_sizes = [size for size in sizes if size in null_by_n]
    upper = [(sx(size), sy(finite(null_by_n[size]["null_ci_high"]))) for size in common_sizes]
    lower = [(sx(size), sy(finite(null_by_n[size]["null_ci_low"]))) for size in reversed(common_sizes)]
    body.append(_polygon([*upper, *lower], NULL_FILL, 0.9))
    null_mean = [(sx(size), sy(finite(null_by_n[size]["null_mean_agreement_rate"]))) for size in common_sizes]
    body.append(_polyline(null_mean, NULL, 1.0, "3,2"))

    color = BLUE if plane == "H" else GREEN
    observed_points: list[tuple[float, float]] = []
    for row in observed:
        size = int(row["subset_size"])
        center = finite(row.get("blind_q_agreement_rate"))
        low = finite(row.get("blind_q_agreement_ci_low"))
        high = finite(row.get("blind_q_agreement_ci_high"))
        if not math.isfinite(center):
            continue
        x = sx(size)
        observed_points.append((x, sy(center)))
        if math.isfinite(low) and math.isfinite(high):
            body.append(f'<line x1="{x}" y1="{sy(low)}" x2="{x}" y2="{sy(high)}" stroke="{color}" stroke-width="0.7"/>')
        body.append(f'<circle cx="{x}" cy="{sy(center)}" r="1.8" fill="{color}"/>')
    body.append(_polyline(observed_points, color, 1.4))
    selected_row = next(row for row in observed if int(row["subset_size"]) == selected_n)
    selected_rate = finite(selected_row["blind_q_agreement_rate"])
    selected_x = sx(selected_n)
    body.append(f'<line x1="{selected_x}" y1="{y0}" x2="{selected_x}" y2="{y1}" stroke="{INK}" stroke-width="0.7" stroke-dasharray="2,2"/>')
    body.append(_text(selected_x + 3, max(y0 + 10, sy(selected_rate) - 4), f"Best-{selected_n}: {100*selected_rate:.1f}%", "small"))
    body.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="none" stroke="{INK}" stroke-width="0.7"/>')
    body.append(_text((x0 + x1) / 2, y0 - 7, f"{plane} plane", "title", "middle"))
    body.append(_text((x0 + x1) / 2, y1 + 21, "Ensemble size N", "label", "middle"))


def render_best_n(
    summary_path: Path,
    null_path: Path,
    paper_dir: Path,
    poster_dir: Path,
    selected_sizes: Mapping[str, int],
) -> None:
    summary_rows = read_csv(summary_path)
    null_rows = read_csv(null_path)
    highs = [
        finite(row.get(field))
        for row in [*summary_rows, *null_rows]
        for field in ("blind_q_agreement_ci_high", "blind_q_agreement_rate", "null_ci_high")
    ]
    finite_highs = [value for value in highs if math.isfinite(value)]
    y_max = max(0.10, math.ceil(max(finite_highs) / 0.05) * 0.05)

    width, height = 516.0, 228.0
    body = [
        _text(width / 2, 12, "Digitizer-disjoint blind agreement and cross-spill null", "title", "middle"),
        _text(width / 2, 23, "Agreement means |Δq| ≤ 0.0025; band is the 2.5–97.5% block-permutation null", "subtitle", "middle"),
        _text(10, 105, "Agreement (%)", "label", "middle", transform="rotate(-90 10 105)"),
    ]
    _best_n_panel(body, "H", summary_rows, null_rows, selected_sizes["H"], (38, 43, 250, 177), y_max, True)
    _best_n_panel(body, "V", summary_rows, null_rows, selected_sizes["V"], (290, 43, 502, 177), y_max, False)
    body.extend(
        [
            f'<line x1="324" y1="216" x2="342" y2="216" stroke="{INK}" stroke-width="1.5"/>',
            _text(346, 219, "observed (95% block interval)", "small"),
            f'<rect x="139" y="212" width="18" height="8" fill="{NULL_FILL}"/>',
            f'<line x1="139" y1="216" x2="157" y2="216" stroke="{NULL}" stroke-width="1" stroke-dasharray="3,2"/>',
            _text(161, 219, "cross-spill null", "small"),
        ]
    )
    paper_dir.mkdir(parents=True, exist_ok=True)
    svg_path = paper_dir / "best_n_validation_hv.svg"
    svg_path.write_text(_svg_root(width, height, body), encoding="utf-8")
    _render(svg_path, paper_dir / "best_n_validation_hv.pdf", None, (1, 1))

    for plane in ("H", "V"):
        pwidth, pheight = 504.0, 288.0
        pbody = [
            _text(pwidth / 2, 15, f"{plane} digitizer-disjoint blind agreement", "title", "middle"),
            _text(pwidth / 2, 27, "|Δq| ≤ 0.0025; gray band is the cross-spill null", "subtitle", "middle"),
            _text(12, 135, "Agreement (%)", "label", "middle", transform="rotate(-90 12 135)"),
        ]
        _best_n_panel(pbody, plane, summary_rows, null_rows, selected_sizes[plane], (45, 47, 490, 235), y_max, True)
        psvg = poster_dir / f"best_n_validation_{plane.lower()}.svg"
        poster_dir.mkdir(parents=True, exist_ok=True)
        psvg.write_text(_svg_root(pwidth, pheight, pbody), encoding="utf-8")
        _render(psvg, None, poster_dir / f"best_n_validation_{plane.lower()}.png", (1750, 1000))


def _ridge_key(row: Mapping[str, str]) -> tuple[int, str, str, str, int, int]:
    return (
        int(row["spill_index"]),
        row["run_name"],
        row["target_ms"],
        row["plane"],
        int(row["window_index"]),
        int(row["center_turn"]),
    )


def paired_ridge_groups(
    best1_path: Path,
    selected_path: Path,
    plane: str,
    band: tuple[float, float],
) -> tuple[dict[int, tuple[array, array]], int]:
    grouped: dict[int, tuple[array, array]] = defaultdict(lambda: (array("f"), array("f")))
    spill_keys: set[tuple[str, str]] = set()
    with best1_path.open(newline="", encoding="utf-8") as left_handle, selected_path.open(
        newline="", encoding="utf-8"
    ) as right_handle:
        left_rows = csv.DictReader(left_handle)
        right_rows = csv.DictReader(right_handle)
        for left, right in zip_longest(left_rows, right_rows):
            if left is None or right is None or _ridge_key(left) != _ridge_key(right):
                raise ValueError(f"ridge rows are not aligned between {best1_path.name} and {selected_path.name}")
            if left["plane"] != plane:
                continue
            baseline = finite(left.get("selected_tune"))
            selected = finite(right.get("selected_tune"))
            if not (band[0] <= baseline <= band[1] and band[0] <= selected <= band[1]):
                continue
            center = int(left["center_turn"])
            grouped[center][0].append(baseline)
            grouped[center][1].append(selected)
            spill_keys.add((left["run_name"], left["target_ms"]))
    if not grouped:
        raise ValueError(f"no exact-paired ridge rows for plane {plane}")
    return dict(grouped), len(spill_keys)


def _heatmap_png(groups: Mapping[int, tuple[array, array]], method_index: int, band: tuple[float, float], bins: int, path: Path, vmax: float) -> None:
    centers = sorted(groups)
    width, height = len(centers), bins
    pixels = bytearray((248, 250, 252) * (width * height))
    for x, center in enumerate(centers):
        values = groups[center][method_index]
        counts = [0] * bins
        for value in values:
            index = int((value - band[0]) * bins / (band[1] - band[0]))
            counts[max(0, min(bins - 1, index))] += 1
        denominator = max(1, len(values))
        for tune_index, count in enumerate(counts):
            fraction = min(1.0, (count / denominator) / max(vmax, 1e-12))
            if fraction <= 0:
                color = (248, 250, 252)
            elif fraction < 0.55:
                t = fraction / 0.55
                color = tuple(round(a + t * (b - a)) for a, b in zip((238, 247, 249), (54, 145, 190)))
            else:
                t = (fraction - 0.55) / 0.45
                color = tuple(round(a + t * (b - a)) for a, b in zip((54, 145, 190), (84, 35, 112)))
            y = bins - 1 - tune_index
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(color)
    _write_png(path, width, height, pixels)


def _write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(pixels[row * width * 3 : (row + 1) * width * 3])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _density_vmax(groups: Mapping[int, tuple[array, array]], bins: int, band: tuple[float, float]) -> float:
    values: list[float] = []
    for baseline, selected in groups.values():
        for method in (baseline, selected):
            counts = [0] * bins
            for tune in method:
                index = int((tune - band[0]) * bins / (band[1] - band[0]))
                counts[max(0, min(bins - 1, index))] += 1
            denominator = max(1, len(method))
            values.extend(count / denominator for count in counts if count)
    return max(1e-6, percentile(values, 0.99))


def _embedded_png(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _ridge_quantile_points(
    groups: Mapping[int, tuple[array, array]],
    method_index: int,
    fraction: float,
    area: tuple[float, float, float, float],
    band: tuple[float, float],
) -> list[tuple[float, float]]:
    centers = sorted(groups)
    x0, y0, x1, y1 = area
    result: list[tuple[float, float]] = []
    for center in centers:
        value = percentile(groups[center][method_index], fraction)
        x = x0 + (center - centers[0]) * (x1 - x0) / max(1, centers[-1] - centers[0])
        y = y1 - (value - band[0]) * (y1 - y0) / (band[1] - band[0])
        result.append((x, y))
    return result


def render_ridge_density(
    ridge_root: Path,
    paper_dir: Path,
    poster_dir: Path,
    selected_sizes: Mapping[str, int],
) -> None:
    best1 = ridge_root / "ridge_density_best1_sliding_tune.csv"
    groups_by_plane: dict[str, tuple[dict[int, tuple[array, array]], int, tuple[float, float]]] = {}
    for plane, band in (("H", (0.62, 0.68)), ("V", (0.69, 0.74))):
        selected = ridge_root / f"ridge_density_best{selected_sizes[plane]}_sliding_tune.csv"
        groups, spill_count = paired_ridge_groups(best1, selected, plane, band)
        groups_by_plane[plane] = (groups, spill_count, band)

    width, height = 516.0, 326.0
    body = [
        _text(width / 2, 12, "Cross-spill ridge-pick location probability", "title", "middle"),
        _text(width / 2, 23, "Exact-paired corrected Best-1 and declared Best-N; raster heat maps with vector tracks", "subtitle", "middle"),
        _text(156, 36, "Corrected adaptive Best-1", "label", "middle"),
        _text(382, 36, f"H Best-{selected_sizes['H']} / V Best-{selected_sizes['V']}", "label", "middle"),
    ]
    panel_areas = {
        ("H", 0): (52.0, 48.0, 260.0, 126.0),
        ("H", 1): (278.0, 48.0, 486.0, 126.0),
        ("V", 0): (52.0, 190.0, 260.0, 268.0),
        ("V", 1): (278.0, 190.0, 486.0, 268.0),
    }
    temp_images: list[Path] = []
    for plane in ("H", "V"):
        groups, spill_count, band = groups_by_plane[plane]
        centers = sorted(groups)
        vmax = _density_vmax(groups, 160, band)
        for method_index in (0, 1):
            image_path = paper_dir / f".ridge_heatmap_{plane.lower()}_{method_index}.png"
            _heatmap_png(groups, method_index, band, 160, image_path, vmax)
            temp_images.append(image_path)
            x0, y0, x1, y1 = panel_areas[(plane, method_index)]
            body.append(
                f'<image x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" '
                f'preserveAspectRatio="none" href="data:image/png;base64,{_embedded_png(image_path)}"/>'
            )
            for fraction, dash, line_width in ((0.10, "3,2", 0.8), (0.50, "", 1.4), (0.90, "3,2", 0.8)):
                body.append(
                    _polyline(
                        _ridge_quantile_points(groups, method_index, fraction, (x0, y0, x1, y1), band),
                        WHITE,
                        line_width,
                        dash,
                    )
                )
            body.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="none" stroke="{INK}" stroke-width="0.6"/>')
        trace_y0 = 133.0 if plane == "H" else 275.0
        trace_y1 = trace_y0 + 20.0
        for method_index in (0, 1):
            tx0 = panel_areas[(plane, method_index)][0]
            tx1 = panel_areas[(plane, method_index)][2]
            trace_points = []
            for center in centers:
                valid_fraction = len(groups[center][method_index]) / max(1, spill_count)
                x = tx0 + (center - centers[0]) * (tx1 - tx0) / max(1, centers[-1] - centers[0])
                y = trace_y1 - valid_fraction * (trace_y1 - trace_y0)
                trace_points.append((x, y))
            body.append(f'<rect x="{tx0}" y="{trace_y0}" width="{tx1-tx0}" height="{trace_y1-trace_y0}" fill="#fbfcfd" stroke="{GRID}" stroke-width="0.5"/>')
            body.append(_polyline(trace_points, BLUE if plane == "H" else GREEN, 1.0))
            body.append(_text(tx0 + 3, trace_y1 - 3, f"valid / {spill_count} paired spills", "small"))
            body.append(_text(tx0, trace_y1 + 10, f"{centers[0]:,}", "small", "start"))
            body.append(_text(tx1, trace_y1 + 10, f"{centers[-1]:,}", "small", "end"))
        body.append(_text(20, (panel_areas[(plane, 0)][1] + panel_areas[(plane, 0)][3]) / 2, plane, "title", "middle"))
        for tune in (band[0], band[1]):
            y = panel_areas[(plane, 0)][3] if tune == band[0] else panel_areas[(plane, 0)][1]
            body.append(_text(47, y + 2.7, f"{tune:.2f}", "small", "end"))
    body.append(_text(width / 2, 323, "Turn", "label", "middle"))
    paper_dir.mkdir(parents=True, exist_ok=True)
    svg_path = paper_dir / "ridge_density_comparison.svg"
    svg_path.write_text(_svg_root(width, height, body), encoding="utf-8")
    _render(svg_path, paper_dir / "ridge_density_comparison.pdf", poster_dir / "ridge_density_comparison.png", (2400, 1516))
    for image_path in temp_images:
        image_path.unlink(missing_ok=True)


def _smooth(values: Sequence[tuple[float, float]], half_width: int = 2) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for index, (x, _value) in enumerate(values):
        local = [value for _x, value in values[max(0, index - half_width) : index + half_width + 1]]
        result.append((x, sum(local) / len(local)))
    return result


def render_ridge_width(
    ridge_root: Path,
    paper_dir: Path,
    poster_dir: Path,
    selected_sizes: Mapping[str, int],
) -> None:
    rows = read_csv(ridge_root / "ridge_density_adaptive_pair_comparison_by_turn.csv")
    curves: dict[str, list[tuple[float, float]]] = {}
    for plane in ("H", "V"):
        curves[plane] = sorted(
            (
                finite(row["center_turn"]),
                finite(row["p10_p90_delta_ensemble_minus_baseline"]),
            )
            for row in rows
            if row.get("plane") == plane and int(row["subset_size"]) == selected_sizes[plane]
        )
        curves[plane] = [(x, y) for x, y in curves[plane] if math.isfinite(x) and math.isfinite(y)]
        if not curves[plane]:
            raise ValueError(f"missing ridge-width curve for plane {plane}")
    all_y = [y for curve in curves.values() for _x, y in curve]
    y_limit = max(abs(min(all_y)), abs(max(all_y))) * 1.08
    width, height = 440.0, 214.0
    body = [
        _text(width / 2, 12, "Selected Best-N minus corrected Best-1 ridge width", "title", "middle"),
        _text(width / 2, 23, "Descriptive five-window smooth; zero means no method difference", "subtitle", "middle"),
        _text(10, 110, "Δ(P10–P90 tune width)", "label", "middle", transform="rotate(-90 10 110)"),
    ]
    areas = {"H": (48.0, 39.0, 428.0, 96.0), "V": (48.0, 124.0, 428.0, 181.0)}
    x_min = min(x for curve in curves.values() for x, _y in curve)
    x_max = max(x for curve in curves.values() for x, _y in curve)
    for plane in ("H", "V"):
        x0, y0, x1, y1 = areas[plane]

        def sx(value: float) -> float:
            return x0 + (value - x_min) * (x1 - x0) / (x_max - x_min)

        def sy(value: float) -> float:
            return y1 - (value + y_limit) * (y1 - y0) / (2 * y_limit)

        body.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="#fbfcfd"/>')
        body.append(f'<line x1="{x0}" y1="{sy(0)}" x2="{x1}" y2="{sy(0)}" stroke="{MUTED}" stroke-width="0.7"/>')
        points = [(sx(x), sy(y)) for x, y in _smooth(curves[plane])]
        color = BLUE if plane == "H" else GREEN
        body.append(_polyline(points, color, 1.5))
        body.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="none" stroke="{INK}" stroke-width="0.6"/>')
        body.append(_text(x0 + 4, y0 + 10, f"{plane} Best-{selected_sizes[plane]}", "title"))
        body.append(_text(x0 - 5, sy(y_limit) + 3, f"{1000*y_limit:.0f}", "small", "end"))
        body.append(_text(x0 - 5, sy(-y_limit) + 3, f"{-1000*y_limit:.0f}", "small", "end"))
        for turn in (x_min, x_max):
            body.append(_text(sx(turn), y1 + 10, f"{turn:,.0f}", "small", "middle"))
    body.append(_text(25, 33, "×10⁻³", "small"))
    body.append(_text(width / 2, 211, "Turn", "label", "middle"))
    paper_dir.mkdir(parents=True, exist_ok=True)
    svg_path = paper_dir / "ridge_width_contrast_hv.svg"
    svg_path.write_text(_svg_root(width, height, body), encoding="utf-8")
    _render(svg_path, paper_dir / "ridge_width_contrast_hv.pdf", poster_dir / "ridge_width_contrast_hv.png", (2000, 973))


def render_publication_figures(
    best_n_root: Path,
    ridge_root: Path,
    paper_dir: Path,
    poster_dir: Path,
    selected_sizes: Mapping[str, int],
) -> None:
    render_best_n(
        best_n_root / "best_n_summary.csv",
        best_n_root / "best_n_cross_spill_null.csv",
        paper_dir,
        poster_dir,
        selected_sizes,
    )
    render_ridge_density(ridge_root, paper_dir, poster_dir, selected_sizes)
    render_ridge_width(ridge_root, paper_dir, poster_dir, selected_sizes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--best-n-root", type=Path, required=True)
    parser.add_argument("--ridge-root", type=Path, required=True)
    parser.add_argument("--paper-dir", type=Path, required=True)
    parser.add_argument("--poster-dir", type=Path, required=True)
    parser.add_argument("--selected-h", type=int, required=True)
    parser.add_argument("--selected-v", type=int, required=True)
    args = parser.parse_args(argv)
    render_publication_figures(
        args.best_n_root.resolve(),
        args.ridge_root.resolve(),
        args.paper_dir.resolve(),
        args.poster_dir.resolve(),
        {"H": args.selected_h, "V": args.selected_v},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
