#!/usr/bin/env python3
"""Standalone BPM-only poster analysis helpers.

This script consumes tbt-monitor ranking/offline-analysis artifacts and writes
poster-phase summaries, diagnostic tables, and lightweight PNG plots. It is
designed to run on the DGX Spark when CuPy is available, but every command has
a CPU/stdlib fallback for local reproduction.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
import statistics
import struct
import sys
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union


MANIFEST_FIELDS = [
    "source_kind",
    "candidate_csv",
    "artifact_dir",
    "run_name",
    "spill_id",
    "target_ms",
    "rank",
    "bundle_dir",
    "artifact_complete",
    "plane_available_h",
    "plane_available_v",
    "bpm_count_h",
    "bpm_count_v",
    "turn_count_h",
    "turn_count_v",
    "label_available",
    "baseline_qx",
    "baseline_qy",
    "baseline_confidence_h",
    "baseline_confidence_v",
    "coverage_h",
    "coverage_v",
    "score",
    "score_h",
    "score_v",
    "std_qx",
    "std_qy",
    "fallback_h",
    "fallback_v",
    "suspicious_h",
    "suspicious_v",
    "quality_flags",
    "warnings",
    "usable_for_analysis",
    "suitable_for_poster",
    "sliding_tune_csv",
    "spectrogram_h",
    "spectrogram_v",
    "tune_vs_time",
    "tune_validation",
]


FEATURE_FIELDS = [
    "spill_id",
    "target_ms",
    "split",
    "label_physics_usable",
    "score",
    "coverage_h",
    "coverage_v",
    "baseline_confidence_h",
    "baseline_confidence_v",
    "std_qx",
    "std_qy",
    "fallback_h",
    "fallback_v",
    "suspicious_h",
    "suspicious_v",
    "turn_count_h",
    "turn_count_v",
    "baseline_qx",
    "baseline_qy",
]


def parse_float(value: object, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        out = float(text)
    except ValueError:
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def parse_int(value: object, default: int = 0) -> int:
    number = parse_float(value)
    if number is None:
        return default
    return int(round(number))


def truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def fmt_float(value: Optional[float], digits: int = 6) -> str:
    if value is None or math.isnan(value) or math.isinf(value):
        return ""
    return f"{value:.{digits}f}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def finite(values: Iterable[Optional[float]]) -> list[float]:
    return [v for v in values if v is not None and math.isfinite(v)]


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = finite(values)
    if not vals:
        return None
    return statistics.fmean(vals)


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = finite(values)
    if not vals:
        return None
    return statistics.median(vals)


def stdev(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = finite(values)
    if len(vals) < 2:
        return None
    return statistics.stdev(vals)


def percentile(values: Iterable[Optional[float]], pct: float) -> Optional[float]:
    vals = sorted(finite(values))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def relpath_or_empty(path: Optional[Path]) -> str:
    if path is None:
        return ""
    return str(path)


SOURCE_FILENAMES = {"candidate_spills.csv", "spills_summary.csv", "capture_index.csv"}


def discover_source_csvs(inputs: Sequence[Path]) -> list[Path]:
    paths: list[Path] = []
    for input_path in inputs:
        if input_path.is_file() and input_path.name in SOURCE_FILENAMES:
            paths.append(input_path)
        elif input_path.is_dir():
            for name in SOURCE_FILENAMES:
                direct = input_path / name
                if direct.exists():
                    paths.append(direct)
                paths.extend(input_path.rglob(name))
    unique: dict[str, Path] = {}
    for path in paths:
        unique[str(path.resolve())] = path
    return sorted(unique.values())


def find_existing(path: Path) -> str:
    return str(path) if path.exists() else ""


def find_sliding_csv(artifact_dir: Path, rank: int, target_ms: str) -> str:
    top = artifact_dir / "top10_visuals"
    candidates = [
        top / f"rank_{rank:02d}_{target_ms}" / "sliding_tune.csv",
        top / f"rank_{rank:02d}" / "sliding_tune.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    if top.exists() and target_ms:
        matches = sorted(top.glob(f"rank_*_{target_ms}/sliding_tune.csv"))
        if matches:
            return str(matches[0])
    return ""


def companion_plot(sliding_csv: str, name: str) -> str:
    if not sliding_csv:
        return ""
    path = Path(sliding_csv).parent / name
    return find_existing(path)


def find_batch_sliding_csv(artifact_dir: Path, spill_index: int, target_ms: str) -> str:
    direct = artifact_dir / f"spill_{spill_index}_{target_ms}_sliding_tune.csv"
    if direct.exists():
        return str(direct)
    matches = sorted(artifact_dir.glob(f"spill_*_{target_ms}_sliding_tune.csv"))
    if matches:
        return str(matches[0])
    return ""


def companion_batch_plot(artifact_dir: Path, spill_index: int, target_ms: str, suffix: str) -> str:
    direct = artifact_dir / f"spill_{spill_index}_{target_ms}_{suffix}"
    if direct.exists():
        return str(direct)
    matches = sorted(artifact_dir.glob(f"spill_*_{target_ms}_{suffix}"))
    if matches:
        return str(matches[0])
    return ""


def quality_flags(row: dict[str, str]) -> str:
    parts: list[str] = []
    for field in ("reason_flags", "quality_flags"):
        value = row.get(field, "").strip()
        if value:
            parts.append(value)
    return "|".join(parts)


def manifest_from_candidate(row: dict[str, str], csv_path: Path) -> dict[str, object]:
    artifact_dir = csv_path.parent
    target_ms = row.get("target_ms", "").strip()
    rank = parse_int(row.get("rank"), 0)
    sliding_csv = find_sliding_csv(artifact_dir, rank, target_ms)

    qx = parse_float(row.get("median_qx"))
    qy = parse_float(row.get("median_qy"))
    conf_h = parse_float(row.get("confidence_p10_h"))
    conf_v = parse_float(row.get("confidence_p10_v"))
    coverage_h = parse_float(row.get("coverage_h"))
    coverage_v = parse_float(row.get("coverage_v"))
    score = parse_float(row.get("score"), 0.0) or 0.0
    turn_h = parse_int(row.get("trackable_turns_h"), 0)
    turn_v = parse_int(row.get("trackable_turns_v"), 0)
    same_streams = parse_int(row.get("same_spill_streams"), parse_int(row.get("captured_streams"), 0))
    plane_streams = max(0, same_streams // 2)
    flags = quality_flags(row)
    warnings = row.get("warnings", "").strip()

    usable = (
        truthy(row.get("artifact_complete", "true"))
        and qx is not None
        and qy is not None
        and score > 0.0
        and (coverage_h or 0.0) >= 0.70
        and (coverage_v or 0.0) >= 0.70
        and (conf_h or 0.0) >= 1.5
        and (conf_v or 0.0) >= 1.5
        and "LOW_H_COVERAGE" not in flags
        and "LOW_V_COVERAGE" not in flags
    )
    poster = usable and rank > 0 and rank <= 10 and not warnings.lower().startswith("error")

    return {
        "source_kind": "ranked_candidate",
        "candidate_csv": str(csv_path),
        "artifact_dir": str(artifact_dir),
        "run_name": row.get("run_name", ""),
        "spill_id": target_ms,
        "target_ms": target_ms,
        "rank": rank,
        "bundle_dir": row.get("bundle_dir", ""),
        "artifact_complete": str(truthy(row.get("artifact_complete", "true"))).lower(),
        "plane_available_h": str(qx is not None).lower(),
        "plane_available_v": str(qy is not None).lower(),
        "bpm_count_h": plane_streams,
        "bpm_count_v": plane_streams,
        "turn_count_h": turn_h,
        "turn_count_v": turn_v,
        "label_available": str(qx is not None and qy is not None).lower(),
        "baseline_qx": fmt_float(qx),
        "baseline_qy": fmt_float(qy),
        "baseline_confidence_h": fmt_float(conf_h),
        "baseline_confidence_v": fmt_float(conf_v),
        "coverage_h": fmt_float(coverage_h),
        "coverage_v": fmt_float(coverage_v),
        "score": fmt_float(score, 3),
        "score_h": fmt_float(parse_float(row.get("score_h")), 6),
        "score_v": fmt_float(parse_float(row.get("score_v")), 6),
        "std_qx": fmt_float(parse_float(row.get("std_qx")), 6),
        "std_qy": fmt_float(parse_float(row.get("std_qy")), 6),
        "fallback_h": parse_int(row.get("fallback_h"), 0),
        "fallback_v": parse_int(row.get("fallback_v"), 0),
        "suspicious_h": parse_int(row.get("suspicious_h"), 0),
        "suspicious_v": parse_int(row.get("suspicious_v"), 0),
        "quality_flags": flags,
        "warnings": warnings,
        "usable_for_analysis": str(usable).lower(),
        "suitable_for_poster": str(poster).lower(),
        "sliding_tune_csv": sliding_csv,
        "spectrogram_h": companion_plot(sliding_csv, "spectrogram_h.png"),
        "spectrogram_v": companion_plot(sliding_csv, "spectrogram_v.png"),
        "tune_vs_time": companion_plot(sliding_csv, "tune_vs_time.png"),
        "tune_validation": companion_plot(sliding_csv, "tune_validation.png"),
    }


def manifest_from_batch_summary(row: dict[str, str], csv_path: Path) -> dict[str, object]:
    artifact_dir = csv_path.parent
    target_ms = row.get("target_ms", "").strip() or row.get("spill_uid", "").strip()
    spill_index = parse_int(row.get("spill_index"), 0)
    sliding_csv = find_batch_sliding_csv(artifact_dir, spill_index, target_ms)
    qx = parse_float(row.get("qx_injection")) or parse_float(row.get("median_qx"))
    qy = parse_float(row.get("qy_injection")) or parse_float(row.get("median_qy"))
    conf_h = parse_float(row.get("confidence_h"))
    conf_v = parse_float(row.get("confidence_v"))
    aligned_fraction = parse_float(row.get("aligned_fraction"))
    flags = quality_flags(row)
    quality_label = row.get("quality_label", "").strip().lower()
    status = row.get("status", "").strip().lower()
    usable = (
        qx is not None
        and qy is not None
        and status not in {"bad", "error", "failed"}
        and quality_label != "bad"
        and (conf_h or 0.0) >= 1.5
        and (conf_v or 0.0) >= 1.5
    )
    poster = usable and quality_label in {"good", "marginal"} and spill_index < 25
    return {
        "source_kind": "batch_summary",
        "candidate_csv": str(csv_path),
        "artifact_dir": str(artifact_dir),
        "run_name": artifact_dir.name,
        "spill_id": target_ms,
        "target_ms": target_ms,
        "rank": spill_index + 1,
        "bundle_dir": "",
        "artifact_complete": "true",
        "plane_available_h": str(qx is not None).lower(),
        "plane_available_v": str(qy is not None).lower(),
        "bpm_count_h": row.get("used_streams_h", row.get("participating_bpms_h", "")),
        "bpm_count_v": row.get("used_streams_v", row.get("participating_bpms_v", "")),
        "turn_count_h": row.get("consensus_turns_h", ""),
        "turn_count_v": row.get("consensus_turns_v", ""),
        "label_available": str(qx is not None and qy is not None).lower(),
        "baseline_qx": fmt_float(qx),
        "baseline_qy": fmt_float(qy),
        "baseline_confidence_h": fmt_float(conf_h),
        "baseline_confidence_v": fmt_float(conf_v),
        "coverage_h": fmt_float(aligned_fraction),
        "coverage_v": fmt_float(aligned_fraction),
        "score": fmt_float((conf_h or 0.0) + (conf_v or 0.0), 3),
        "score_h": fmt_float(conf_h),
        "score_v": fmt_float(conf_v),
        "std_qx": fmt_float(parse_float(row.get("std_qx"))),
        "std_qy": fmt_float(parse_float(row.get("std_qy"))),
        "fallback_h": parse_int(row.get("sliding_fallback_count_h"), 0),
        "fallback_v": parse_int(row.get("sliding_fallback_count_v"), 0),
        "suspicious_h": parse_int(row.get("sliding_suspicious_count_h"), 0),
        "suspicious_v": parse_int(row.get("sliding_suspicious_count_v"), 0),
        "quality_flags": flags,
        "warnings": row.get("warnings", "").strip(),
        "usable_for_analysis": str(usable).lower(),
        "suitable_for_poster": str(poster).lower(),
        "sliding_tune_csv": sliding_csv,
        "spectrogram_h": companion_batch_plot(artifact_dir, spill_index, target_ms, "spectrogram_h.png"),
        "spectrogram_v": companion_batch_plot(artifact_dir, spill_index, target_ms, "spectrogram_v.png"),
        "tune_vs_time": companion_batch_plot(artifact_dir, spill_index, target_ms, "tune_vs_time.png"),
        "tune_validation": companion_batch_plot(artifact_dir, spill_index, target_ms, "tune_validation.png"),
    }


def manifest_from_capture_index(row: dict[str, str], csv_path: Path) -> dict[str, object]:
    artifact_dir = csv_path.parent
    target_ms = row.get("target_ms", "").strip() or row.get("redis_timestamp_ms", "").strip()
    complete_streams = parse_int(row.get("complete_streams"), parse_int(row.get("same_spill_streams"), 0))
    plane_streams = max(0, complete_streams // 2)
    status = row.get("status", "").strip().lower()
    complete = status in {"complete", "ok"} or parse_int(row.get("missing_streams"), 1) == 0
    return {
        "source_kind": "capture_index",
        "candidate_csv": str(csv_path),
        "artifact_dir": str(artifact_dir),
        "run_name": artifact_dir.name,
        "spill_id": target_ms,
        "target_ms": target_ms,
        "rank": parse_int(row.get("capture_index"), 0) + 1,
        "bundle_dir": row.get("bundle_dir", ""),
        "artifact_complete": str(complete).lower(),
        "plane_available_h": "false",
        "plane_available_v": "false",
        "bpm_count_h": plane_streams,
        "bpm_count_v": plane_streams,
        "turn_count_h": "",
        "turn_count_v": "",
        "label_available": "false",
        "baseline_qx": "",
        "baseline_qy": "",
        "baseline_confidence_h": "",
        "baseline_confidence_v": "",
        "coverage_h": "",
        "coverage_v": "",
        "score": "",
        "score_h": "",
        "score_v": "",
        "std_qx": "",
        "std_qy": "",
        "fallback_h": "",
        "fallback_v": "",
        "suspicious_h": "",
        "suspicious_v": "",
        "quality_flags": status.upper() if status else "",
        "warnings": "",
        "usable_for_analysis": str(complete).lower(),
        "suitable_for_poster": "false",
        "sliding_tune_csv": "",
        "spectrogram_h": "",
        "spectrogram_v": "",
        "tune_vs_time": "",
        "tune_validation": "",
    }


def load_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"manifest not found: {path}")
    return read_csv(path)


def numeric(row: dict[str, str], field: str) -> Optional[float]:
    return parse_float(row.get(field))


def bool_field(row: dict[str, str], field: str) -> bool:
    return truthy(row.get(field, "false"))


# ---------------------------------------------------------------------------
# Minimal PNG drawing

Color = tuple[int, int, int]

WHITE: Color = (250, 250, 248)
INK: Color = (32, 36, 40)
MUTED: Color = (105, 112, 120)
GRID: Color = (222, 226, 230)
BLUE: Color = (44, 123, 182)
GREEN: Color = (38, 153, 112)
RED: Color = (190, 72, 72)
ORANGE: Color = (222, 137, 56)
PURPLE: Color = (118, 91, 176)


FONT = {
    " ": ["000", "000", "000", "000", "000", "000", "000"],
    "-": ["000", "000", "000", "111", "000", "000", "000"],
    "_": ["000", "000", "000", "000", "000", "000", "111"],
    ".": ["000", "000", "000", "000", "000", "011", "011"],
    ",": ["000", "000", "000", "000", "000", "011", "010"],
    ":": ["000", "010", "010", "000", "010", "010", "000"],
    "/": ["001", "001", "010", "010", "100", "100", "000"],
    "%": ["101", "001", "010", "010", "100", "101", "000"],
    "(": ["001", "010", "100", "100", "100", "010", "001"],
    ")": ["100", "010", "001", "001", "001", "010", "100"],
    "0": ["111", "101", "101", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "010", "010", "111"],
    "2": ["111", "001", "001", "111", "100", "100", "111"],
    "3": ["111", "001", "001", "111", "001", "001", "111"],
    "4": ["101", "101", "101", "111", "001", "001", "001"],
    "5": ["111", "100", "100", "111", "001", "001", "111"],
    "6": ["111", "100", "100", "111", "101", "101", "111"],
    "7": ["111", "001", "001", "010", "010", "100", "100"],
    "8": ["111", "101", "101", "111", "101", "101", "111"],
    "9": ["111", "101", "101", "111", "001", "001", "111"],
    "A": ["010", "101", "101", "111", "101", "101", "101"],
    "B": ["110", "101", "101", "110", "101", "101", "110"],
    "C": ["111", "100", "100", "100", "100", "100", "111"],
    "D": ["110", "101", "101", "101", "101", "101", "110"],
    "E": ["111", "100", "100", "110", "100", "100", "111"],
    "F": ["111", "100", "100", "110", "100", "100", "100"],
    "G": ["111", "100", "100", "101", "101", "101", "111"],
    "H": ["101", "101", "101", "111", "101", "101", "101"],
    "I": ["111", "010", "010", "010", "010", "010", "111"],
    "J": ["001", "001", "001", "001", "101", "101", "111"],
    "K": ["101", "101", "110", "100", "110", "101", "101"],
    "L": ["100", "100", "100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101", "101", "101"],
    "N": ["101", "111", "111", "111", "111", "111", "101"],
    "O": ["111", "101", "101", "101", "101", "101", "111"],
    "P": ["111", "101", "101", "111", "100", "100", "100"],
    "Q": ["111", "101", "101", "101", "111", "001", "001"],
    "R": ["110", "101", "101", "110", "110", "101", "101"],
    "S": ["111", "100", "100", "111", "001", "001", "111"],
    "T": ["111", "010", "010", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "101", "101", "010"],
    "W": ["101", "101", "101", "101", "111", "111", "101"],
    "X": ["101", "101", "101", "010", "101", "101", "101"],
    "Y": ["101", "101", "101", "010", "010", "010", "010"],
    "Z": ["111", "001", "001", "010", "100", "100", "111"],
}


def new_canvas(width: int, height: int, color: Color = WHITE) -> bytearray:
    return bytearray(color * (width * height))


def set_pixel(pixels: bytearray, width: int, height: int, x: int, y: int, color: Color) -> None:
    if x < 0 or y < 0 or x >= width or y >= height:
        return
    idx = (y * width + x) * 3
    pixels[idx : idx + 3] = bytes(color)


def rect(pixels: bytearray, width: int, height: int, x0: int, y0: int, x1: int, y1: int, color: Color) -> None:
    x0, x1 = sorted((max(0, x0), min(width - 1, x1)))
    y0, y1 = sorted((max(0, y0), min(height - 1, y1)))
    for y in range(y0, y1 + 1):
        start = (y * width + x0) * 3
        end = (y * width + x1 + 1) * 3
        pixels[start:end] = bytes(color) * (x1 - x0 + 1)


def line(pixels: bytearray, width: int, height: int, x0: int, y0: int, x1: int, y1: int, color: Color) -> None:
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        set_pixel(pixels, width, height, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    text: str,
    color: Color = INK,
    scale: int = 2,
) -> None:
    cursor = x
    for char in text.upper():
        glyph = FONT.get(char, FONT[" "])
        for row_idx, row in enumerate(glyph):
            for col_idx, bit in enumerate(row):
                if bit == "1":
                    rect(
                        pixels,
                        width,
                        height,
                        cursor + col_idx * scale,
                        y + row_idx * scale,
                        cursor + (col_idx + 1) * scale - 1,
                        y + (row_idx + 1) * scale - 1,
                        color,
                    )
        cursor += 4 * scale


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    ensure_dir(path.parent)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * row_bytes : (y + 1) * row_bytes])
    data = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)),
            chunk(b"IEND", b""),
        ]
    )
    path.write_bytes(data)


def no_data_png(path: Path, title: str, detail: str = "NO DATA") -> None:
    width, height = 1000, 600
    pixels = new_canvas(width, height)
    draw_text(pixels, width, height, 40, 35, title[:34], INK, 3)
    draw_text(pixels, width, height, 80, 270, detail[:40], RED, 4)
    write_png(path, width, height, pixels)


def plot_area(width: int, height: int) -> tuple[int, int, int, int]:
    return 95, 95, width - 45, height - 85


def scale_value(value: float, src_min: float, src_max: float, dst_min: int, dst_max: int) -> int:
    if src_max <= src_min:
        return (dst_min + dst_max) // 2
    frac = (value - src_min) / (src_max - src_min)
    frac = max(0.0, min(1.0, frac))
    return int(round(dst_min + frac * (dst_max - dst_min)))


def format_axis_value(value: float, span: float) -> str:
    """Format a compact numeric label for the built-in bitmap font."""
    if not math.isfinite(value):
        return ""
    if span >= 100.0 or (abs(value - round(value)) < 1e-9 and (abs(value) >= 1.0 or span >= 2.0)):
        return str(int(round(value)))
    magnitude = max(abs(value), abs(span))
    if magnitude and (magnitude < 1e-3 or magnitude >= 1e6):
        return f"{value:.2e}".replace("e+", "e")
    if span < 0.01:
        return f"{value:.5f}"
    if span < 0.1:
        return f"{value:.4f}"
    if span < 1.0:
        return f"{value:.3f}"
    return f"{value:.1f}"


def draw_numeric_axis_labels(
    pixels: bytearray,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    x_ticks: int = 6,
) -> None:
    """Draw x ticks plus y endpoints without crowding the horizontal y label."""
    x0, y0, x1, y1 = area
    xmin, xmax = x_range
    ymin, ymax = y_range
    x_span = xmax - xmin
    y_span = ymax - ymin
    for tick in range(max(2, x_ticks)):
        fraction = tick / (max(2, x_ticks) - 1)
        value = xmin + fraction * x_span
        label = format_axis_value(value, x_span)
        label_width = len(label) * 8
        x = x0 + int(round((x1 - x0) * fraction)) - label_width // 2
        x = max(2, min(width - label_width - 2, x))
        draw_text(pixels, width, height, x, y1 + 8, label, MUTED, 2)
    for value, y in ((ymax, y0 - 7), (ymin, y1 - 7)):
        label = format_axis_value(value, y_span)
        label_width = len(label) * 8
        draw_text(pixels, width, height, max(2, x0 - label_width - 6), y, label, MUTED, 2)


def draw_axes(pixels: bytearray, width: int, height: int, title: str, x_label: str, y_label: str) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = plot_area(width, height)
    draw_text(pixels, width, height, 34, 30, title[:40], INK, 3)
    rect(pixels, width, height, x0, y0, x1, y1, (245, 247, 248))
    for tick in range(6):
        x = x0 + int((x1 - x0) * tick / 5)
        y = y0 + int((y1 - y0) * tick / 5)
        line(pixels, width, height, x, y0, x, y1, GRID)
        line(pixels, width, height, x0, y, x1, y, GRID)
    line(pixels, width, height, x0, y1, x1, y1, INK)
    line(pixels, width, height, x0, y0, x0, y1, INK)
    draw_text(pixels, width, height, (x0 + x1) // 2 - 70, height - 45, x_label[:20], MUTED, 2)
    draw_text(pixels, width, height, 18, (y0 + y1) // 2 - 20, y_label[:12], MUTED, 2)
    return x0, y0, x1, y1


def line_plot(
    path: Path,
    title: str,
    series: Sequence[tuple[str, Sequence[tuple[float, float]], Color]],
    x_label: str = "SPILL",
    y_label: str = "VALUE",
    y_range: Optional[tuple[float, float]] = None,
) -> None:
    points = [(x, y) for _, rows, _ in series for x, y in rows if math.isfinite(x) and math.isfinite(y)]
    if not points:
        no_data_png(path, title)
        return
    width, height = 1280, 720
    pixels = new_canvas(width, height)
    x0, y0, x1, y1 = draw_axes(pixels, width, height, title, x_label, y_label)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    if y_range is None:
        ymin, ymax = min(ys), max(ys)
        pad = (ymax - ymin) * 0.08 or 0.01
        ymin -= pad
        ymax += pad
    else:
        ymin, ymax = y_range
    unique_x = sorted(set(xs))
    integer_x = unique_x and all(abs(value - round(value)) < 1e-9 for value in unique_x)
    draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (xmin, xmax),
        (ymin, ymax),
        x_ticks=2 if integer_x else 6,
    )
    if integer_x and len(unique_x) > 2:
        tick_indices = sorted(
            index
            for index in {
                *{round(position * (len(unique_x) - 1) / 5) for position in range(1, 5)},
                *(set([unique_x.index(0.0)]) if 0.0 in unique_x else set()),
            }
            if 0 < index < len(unique_x) - 1
        )
        for index in tick_indices:
            value = unique_x[index]
            label = str(int(round(value)))
            x = scale_value(value, xmin, xmax, x0, x1) - len(label) * 4
            draw_text(pixels, width, height, x, y1 + 8, label, MUTED, 2)
    legend_x = x1 - 210
    legend_y = y0 - 34
    for idx, (name, rows, color) in enumerate(series):
        clean = [(x, y) for x, y in rows if math.isfinite(x) and math.isfinite(y)]
        if not clean:
            continue
        for (xa, ya), (xb, yb) in zip(clean, clean[1:]):
            px0 = scale_value(xa, xmin, xmax, x0, x1)
            py0 = scale_value(ya, ymin, ymax, y1, y0)
            px1 = scale_value(xb, xmin, xmax, x0, x1)
            py1 = scale_value(yb, ymin, ymax, y1, y0)
            line(pixels, width, height, px0, py0, px1, py1, color)
        for x, y in clean:
            px = scale_value(x, xmin, xmax, x0, x1)
            py = scale_value(y, ymin, ymax, y1, y0)
            rect(pixels, width, height, px - 2, py - 2, px + 2, py + 2, color)
        rect(pixels, width, height, legend_x, legend_y + idx * 22, legend_x + 14, legend_y + 12 + idx * 22, color)
        draw_text(pixels, width, height, legend_x + 22, legend_y + idx * 22, name[:18], MUTED, 2)
    write_png(path, width, height, pixels)


def hist_plot(
    path: Path,
    title: str,
    values: Sequence[float],
    x_label: str = "VALUE",
    bins: int = 32,
    x_range: Optional[tuple[float, float]] = None,
    color: Color = BLUE,
    note: str = "",
) -> None:
    vals = finite(values)
    if not vals:
        no_data_png(path, title)
        return
    width, height = 1280, 720
    pixels = new_canvas(width, height)
    x0, y0, x1, y1 = draw_axes(pixels, width, height, title, x_label, "COUNT")
    xmin, xmax = x_range if x_range else (min(vals), max(vals))
    if xmax <= xmin:
        half_width = 1.0 if abs(xmin) >= 100.0 else 0.5
        xmin -= half_width
        xmax += half_width
    counts = [0 for _ in range(bins)]
    for value in vals:
        idx = int((value - xmin) / (xmax - xmin) * bins)
        idx = max(0, min(bins - 1, idx))
        counts[idx] += 1
    max_count = max(counts) or 1
    bar_w = max(1, (x1 - x0) // bins)
    for idx, count in enumerate(counts):
        bx0 = x0 + idx * bar_w
        bx1 = min(x1, bx0 + bar_w - 2)
        by0 = scale_value(count, 0, max_count, y1, y0)
        rect(pixels, width, height, bx0, by0, bx1, y1, color)
    xmin_label = format_axis_value(xmin, xmax - xmin)
    xmax_label = format_axis_value(xmax, xmax - xmin)
    draw_text(pixels, width, height, x0, y1 + 8, xmin_label, MUTED, 2)
    draw_text(pixels, width, height, x1 - len(xmax_label) * 8, y1 + 8, xmax_label, MUTED, 2)
    max_label = str(max_count)
    draw_text(pixels, width, height, max(2, x0 - len(max_label) * 8 - 6), y0 - 7, max_label, MUTED, 2)
    draw_text(pixels, width, height, x0 - 14, y1 - 7, "0", MUTED, 2)
    if note:
        draw_text(pixels, width, height, x0, y0 - 28, note[:72], MUTED, 2)
    write_png(path, width, height, pixels)


def bar_plot(
    path: Path,
    title: str,
    labels: Sequence[str],
    values: Sequence[float],
    y_label: str = "VALUE",
    y_range: Optional[tuple[float, float]] = None,
    colors: Optional[Sequence[Color]] = None,
    show_values: bool = False,
    note: str = "",
    x_label: str = "CATEGORY",
) -> None:
    pairs = [(label, value) for label, value in zip(labels, values) if math.isfinite(value)]
    if not pairs:
        no_data_png(path, title)
        return
    width, height = 1000, 660
    pixels = new_canvas(width, height)
    x0, y0, x1, y1 = draw_axes(pixels, width, height, title, x_label, y_label)
    if y_range is None:
        ymin, ymax = 0.0, max([value for _, value in pairs] + [1.0])
    else:
        ymin, ymax = y_range
        if not math.isfinite(ymin) or not math.isfinite(ymax) or ymax <= ymin:
            raise ValueError("bar plot y_range must contain finite increasing values")
    y_span = ymax - ymin
    top_label = format_axis_value(ymax, y_span)
    bottom_label = format_axis_value(ymin, y_span)
    draw_text(pixels, width, height, max(2, x0 - len(top_label) * 8 - 6), y0 - 7, top_label, MUTED, 2)
    draw_text(pixels, width, height, max(2, x0 - len(bottom_label) * 8 - 6), y1 - 7, bottom_label, MUTED, 2)
    if note:
        draw_text(pixels, width, height, x0, y0 - 28, note[:72], MUTED, 2)
    bar_w = max(10, (x1 - x0) // max(1, len(pairs)) - 10)
    for idx, (label, value) in enumerate(pairs):
        bx0 = x0 + idx * ((x1 - x0) // len(pairs)) + 5
        bx1 = min(x1, bx0 + bar_w)
        by0 = scale_value(value, ymin, ymax, y1, y0)
        palette = colors or (BLUE, GREEN, ORANGE, PURPLE, RED)
        color = palette[idx % len(palette)]
        rect(pixels, width, height, bx0, by0, bx1, y1, color)
        short_label = label[:8]
        label_x = (bx0 + bx1) // 2 - len(short_label) * 4
        draw_text(pixels, width, height, label_x, y1 + 8, short_label, MUTED, 2)
        if show_values:
            value_label = format_axis_value(value, y_span)
            value_x = (bx0 + bx1) // 2 - len(value_label) * 4
            value_y = max(y0 + 4, by0 - 22)
            draw_text(pixels, width, height, value_x, value_y, value_label, INK, 2)
    write_png(path, width, height, pixels)


def tune_color(value: Optional[float], tune_min: float, tune_max: float) -> Color:
    if value is None or not math.isfinite(value):
        return (235, 237, 240)
    frac = (value - tune_min) / (tune_max - tune_min)
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        t = frac * 2.0
        return (int(44 + 18 * t), int(123 + 96 * t), int(182 - 62 * t))
    t = (frac - 0.5) * 2.0
    return (int(62 + 190 * t), int(219 - 86 * t), int(120 - 64 * t))


def heatmap_plot(
    path: Path,
    title: str,
    matrix: Sequence[Sequence[Optional[float]]],
    tune_min: float = 0.58,
    tune_max: float = 0.74,
    x_label: str = "WINDOW",
    y_label: str = "SPILL",
) -> None:
    rows = [list(row) for row in matrix if row]
    if not rows:
        no_data_png(path, title)
        return
    cols = max(len(row) for row in rows)
    width, height = 1280, 900
    pixels = new_canvas(width, height)
    x0, y0, x1, y1 = draw_axes(pixels, width, height, title, x_label, y_label)
    cell_w = max(1, (x1 - x0 + 1) // cols)
    cell_h = max(1, (y1 - y0 + 1) // len(rows))
    for row_idx, row in enumerate(rows):
        for col_idx in range(cols):
            value = row[col_idx] if col_idx < len(row) else None
            color = tune_color(value, tune_min, tune_max)
            cx0 = x0 + col_idx * cell_w
            cy0 = y0 + row_idx * cell_h
            rect(pixels, width, height, cx0, cy0, min(x1, cx0 + cell_w - 1), min(y1, cy0 + cell_h - 1), color)
    draw_text(pixels, width, height, x1 - 190, y0 - 26, f"{tune_min:.3f}-{tune_max:.3f}", MUTED, 2)
    write_png(path, width, height, pixels)


# ---------------------------------------------------------------------------
# Phase commands


def build_manifest(args: argparse.Namespace) -> Path:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    input_paths = [Path(item) for item in args.input]
    source_csvs = discover_source_csvs(input_paths)
    if not source_csvs:
        raise SystemExit("no candidate_spills.csv, spills_summary.csv, or capture_index.csv files found")

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for csv_path in source_csvs:
        for record in read_csv(csv_path):
            if csv_path.name == "candidate_spills.csv":
                row = manifest_from_candidate(record, csv_path)
            elif csv_path.name == "spills_summary.csv":
                row = manifest_from_batch_summary(record, csv_path)
            elif csv_path.name == "capture_index.csv":
                row = manifest_from_capture_index(record, csv_path)
            else:
                continue
            key = (str(row.get("candidate_csv", "")), str(row.get("run_name", "")), str(row.get("target_ms", "")))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda row: (str(row.get("run_name", "")), parse_int(row.get("rank"), 0), parse_int(row.get("target_ms"), 0)))

    manifest_path = out_dir / "dataset_manifest.csv"
    write_csv(manifest_path, rows, MANIFEST_FIELDS)
    write_dataset_summary(out_dir / "dataset_summary.md", rows, source_csvs)
    write_dataset_overview(out_dir / "dataset_overview.png", rows)
    return manifest_path


def write_dataset_summary(path: Path, rows: Sequence[dict[str, object]], source_csvs: Sequence[Path]) -> None:
    run_counts = Counter(str(row.get("run_name", "")) for row in rows)
    source_counts = Counter(str(row.get("source_kind", "")) for row in rows)
    usable = sum(1 for row in rows if truthy(row.get("usable_for_analysis")))
    poster = sum(1 for row in rows if truthy(row.get("suitable_for_poster")))
    qx = [parse_float(row.get("baseline_qx")) for row in rows]
    qy = [parse_float(row.get("baseline_qy")) for row in rows]
    scores = [parse_float(row.get("score")) for row in rows]
    lines = [
        "# Dataset Summary",
        "",
        "BPM-only poster manifest built from existing tbt-monitor analysis artifacts.",
        "No Schottky comparison or Schottky-derived labels are used.",
        "",
        f"- source CSV files: `{len(source_csvs)}`",
        f"- manifest rows: `{len(rows)}`",
        f"- usable for analysis: `{usable}`",
        f"- suitable for poster review: `{poster}`",
        f"- Qx median/std: `{fmt_float(median(qx))}` / `{fmt_float(stdev(qx))}`",
        f"- Qy median/std: `{fmt_float(median(qy))}` / `{fmt_float(stdev(qy))}`",
        f"- score median/p10: `{fmt_float(median(scores), 3)}` / `{fmt_float(percentile(scores, 0.10), 3)}`",
    ]
    lines.extend(["## Sources", ""])
    for source, count in sorted(source_counts.items()):
        lines.append(f"- `{source}`: `{count}` rows")
    lines.extend(["", "## Runs", ""])
    for run, count in sorted(run_counts.items()):
        lines.append(f"- `{run}`: `{count}` rows")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `label_available` means the existing BPM baseline fields are present; it is not an independent external reference label.",
            "- `bpm_count_h/v` is inferred from same-spill stream counts when only ranked summary artifacts are available.",
            "- Poster suitability is a conservative first-pass flag: complete artifact, both planes present, adequate confidence/coverage, and rank <= 10 in its source ranking.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def write_dataset_overview(path: Path, rows: Sequence[dict[str, object]]) -> None:
    scores = finite(parse_float(row.get("score")) for row in rows)
    qx = finite(parse_float(row.get("baseline_qx")) for row in rows)
    qy = finite(parse_float(row.get("baseline_qy")) for row in rows)
    usable = sum(1 for row in rows if truthy(row.get("usable_for_analysis")))
    total = max(1, len(rows))

    width, height = 1400, 900
    pixels = new_canvas(width, height)
    draw_text(pixels, width, height, 35, 25, "DATASET OVERVIEW", INK, 4)
    draw_text(pixels, width, height, 45, 95, f"ROWS {len(rows)}  USABLE {usable} ({usable * 100 / total:.1f}%)", MUTED, 3)
    tmp = Path(tempfile.mkdtemp(prefix="poster_overview_"))
    try:
        hist_plot(tmp / "score.png", "SCORE DISTRIBUTION", scores, "SCORE", bins=20, color=BLUE)
        hist_plot(tmp / "qx.png", "QX DISTRIBUTION", qx, "QX", bins=20, x_range=(0.58, 0.74), color=GREEN)
        hist_plot(tmp / "qy.png", "QY DISTRIBUTION", qy, "QY", bins=20, x_range=(0.58, 0.74), color=ORANGE)
        # Keep overview compact by drawing native bars instead of compositing PNGs.
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    x0, y0, x1, y1 = 70, 180, 1320, 820
    rect(pixels, width, height, x0, y0, x1, y1, (245, 247, 248))
    labels = ["TOTAL", "USABLE", "POSTER"]
    values = [len(rows), usable, sum(1 for row in rows if truthy(row.get("suitable_for_poster")))]
    ymax = max(values) or 1
    for idx, (label, value) in enumerate(zip(labels, values)):
        bx0 = x0 + 120 + idx * 340
        bx1 = bx0 + 170
        by0 = scale_value(value, 0, ymax, y1 - 40, y0 + 80)
        rect(pixels, width, height, bx0, by0, bx1, y1 - 40, [BLUE, GREEN, ORANGE][idx])
        draw_text(pixels, width, height, bx0 - 8, y1 - 20, label, INK, 3)
        draw_text(pixels, width, height, bx0 + 20, by0 - 36, str(value), INK, 3)
    draw_text(pixels, width, height, 90, y0 + 25, f"QX MED {fmt_float(median(qx), 4)} STD {fmt_float(stdev(qx), 4)}", MUTED, 3)
    draw_text(pixels, width, height, 90, y0 + 60, f"QY MED {fmt_float(median(qy), 4)} STD {fmt_float(stdev(qy), 4)}", MUTED, 3)
    draw_text(pixels, width, height, 90, y0 + 95, f"SCORE MED {fmt_float(median(scores), 2)}", MUTED, 3)
    write_png(path, width, height, pixels)


def run_baseline_batch(args: argparse.Namespace) -> Path:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    rows = load_manifest(Path(args.manifest))
    summaries: list[dict[str, object]] = []
    for idx, row in enumerate(rows):
        qx = numeric(row, "baseline_qx")
        qy = numeric(row, "baseline_qy")
        summaries.append(
            {
                "spill_index": idx,
                "spill_id": row.get("spill_id", ""),
                "target_ms": row.get("target_ms", ""),
                "run_name": row.get("run_name", ""),
                "baseline_qx": fmt_float(qx),
                "baseline_qy": fmt_float(qy),
                "confidence_h": row.get("baseline_confidence_h", ""),
                "confidence_v": row.get("baseline_confidence_v", ""),
                "coverage_h": row.get("coverage_h", ""),
                "coverage_v": row.get("coverage_v", ""),
                "score": row.get("score", ""),
                "std_qx": row.get("std_qx", ""),
                "std_qy": row.get("std_qy", ""),
                "usable_for_analysis": row.get("usable_for_analysis", "false"),
                "suitable_for_poster": row.get("suitable_for_poster", "false"),
                "quality_flags": row.get("quality_flags", ""),
                "warnings": row.get("warnings", ""),
            }
        )
    summary_path = out_dir / "baseline_summary.csv"
    fields = list(summaries[0].keys()) if summaries else [
        "spill_index",
        "spill_id",
        "target_ms",
        "baseline_qx",
        "baseline_qy",
    ]
    write_csv(summary_path, summaries, fields)
    write_features(out_dir / "features.csv", rows)
    write_baseline_quality_summary(out_dir / "baseline_quality_summary.md", rows)
    make_baseline_plots(out_dir, rows)
    return summary_path


def write_features(path: Path, rows: Sequence[dict[str, str]], split_rows: Optional[Sequence[dict[str, object]]] = None) -> None:
    split_by_spill = {}
    if split_rows:
        split_by_spill = {str(row.get("spill_id", "")): str(row.get("split", "")) for row in split_rows}
    feature_rows: list[dict[str, object]] = []
    for row in rows:
        spill_id = row.get("spill_id", "")
        feature_rows.append(
            {
                "spill_id": spill_id,
                "target_ms": row.get("target_ms", ""),
                "split": split_by_spill.get(spill_id, ""),
                "label_physics_usable": "1" if bool_field(row, "usable_for_analysis") else "0",
                "score": row.get("score", ""),
                "coverage_h": row.get("coverage_h", ""),
                "coverage_v": row.get("coverage_v", ""),
                "baseline_confidence_h": row.get("baseline_confidence_h", ""),
                "baseline_confidence_v": row.get("baseline_confidence_v", ""),
                "std_qx": row.get("std_qx", ""),
                "std_qy": row.get("std_qy", ""),
                "fallback_h": row.get("fallback_h", ""),
                "fallback_v": row.get("fallback_v", ""),
                "suspicious_h": row.get("suspicious_h", ""),
                "suspicious_v": row.get("suspicious_v", ""),
                "turn_count_h": row.get("turn_count_h", ""),
                "turn_count_v": row.get("turn_count_v", ""),
                "baseline_qx": row.get("baseline_qx", ""),
                "baseline_qy": row.get("baseline_qy", ""),
            }
        )
    write_csv(path, feature_rows, FEATURE_FIELDS)


def write_baseline_quality_summary(path: Path, rows: Sequence[dict[str, str]]) -> None:
    qx = [numeric(row, "baseline_qx") for row in rows if bool_field(row, "usable_for_analysis")]
    qy = [numeric(row, "baseline_qy") for row in rows if bool_field(row, "usable_for_analysis")]
    conf_h = [numeric(row, "baseline_confidence_h") for row in rows]
    conf_v = [numeric(row, "baseline_confidence_v") for row in rows]
    flags = Counter()
    for row in rows:
        for flag in row.get("quality_flags", "").split("|"):
            if flag:
                flags[flag] += 1
    lines = [
        "# Baseline Quality Summary",
        "",
        "BPM-only baseline summary derived from existing FFT/flash ranking fields.",
        "",
        f"- total rows: `{len(rows)}`",
        f"- usable rows: `{sum(1 for row in rows if bool_field(row, 'usable_for_analysis'))}`",
        f"- poster candidates: `{sum(1 for row in rows if bool_field(row, 'suitable_for_poster'))}`",
        f"- Qx median/std usable: `{fmt_float(median(qx))}` / `{fmt_float(stdev(qx))}`",
        f"- Qy median/std usable: `{fmt_float(median(qy))}` / `{fmt_float(stdev(qy))}`",
        f"- H confidence median: `{fmt_float(median(conf_h))}`",
        f"- V confidence median: `{fmt_float(median(conf_v))}`",
        "",
        "## Quality Flags",
        "",
    ]
    if flags:
        for flag, count in flags.most_common():
            lines.append(f"- `{flag}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This freezes the current non-ML BPM tune baseline for poster work. The labels here come from the existing BPM artifact fields; no external reference is used.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def make_baseline_plots(out_dir: Path, rows: Sequence[dict[str, str]]) -> None:
    indexed_qx = [(idx, numeric(row, "baseline_qx")) for idx, row in enumerate(rows)]
    indexed_qy = [(idx, numeric(row, "baseline_qy")) for idx, row in enumerate(rows)]
    qx = [value for _, value in indexed_qx if value is not None]
    qy = [value for _, value in indexed_qy if value is not None]
    conf_h = [(idx, numeric(row, "baseline_confidence_h")) for idx, row in enumerate(rows)]
    conf_v = [(idx, numeric(row, "baseline_confidence_v")) for idx, row in enumerate(rows)]
    hist_plot(out_dir / "baseline_qx_hist.png", "BASELINE QX HIST", qx, "QX", x_range=(0.58, 0.74), color=BLUE)
    hist_plot(out_dir / "baseline_qy_hist.png", "BASELINE QY HIST", qy, "QY", x_range=(0.58, 0.74), color=GREEN)
    line_plot(
        out_dir / "baseline_qx_vs_spill.png",
        "BASELINE QX VS SPILL",
        [("QX", [(x, y) for x, y in indexed_qx if y is not None], BLUE)],
        y_range=(0.58, 0.74),
        y_label="QX",
    )
    line_plot(
        out_dir / "baseline_qy_vs_spill.png",
        "BASELINE QY VS SPILL",
        [("QY", [(x, y) for x, y in indexed_qy if y is not None], GREEN)],
        y_range=(0.58, 0.74),
        y_label="QY",
    )
    line_plot(
        out_dir / "baseline_confidence_vs_spill.png",
        "BASELINE CONFIDENCE",
        [
            ("H CONF", [(x, y) for x, y in conf_h if y is not None], BLUE),
            ("V CONF", [(x, y) for x, y in conf_v if y is not None], ORANGE),
        ],
        y_label="CONF",
    )
    usable = sum(1 for row in rows if bool_field(row, "usable_for_analysis"))
    unusable = len(rows) - usable
    bar_plot(out_dir / "baseline_usable_fraction.png", "BASELINE USABLE FRACTION", ["USABLE", "OTHER"], [usable, unusable], "ROWS")
    line_plot(
        out_dir / "injection_tune_reproducibility.png",
        "INJECTION TUNE REPRODUCIBILITY",
        [
            ("QX", [(x, y) for x, y in indexed_qx if y is not None], BLUE),
            ("QY", [(x, y) for x, y in indexed_qy if y is not None], GREEN),
        ],
        y_range=(0.58, 0.74),
        y_label="TUNE",
    )


def read_sliding_tune(path: str) -> dict[str, list[dict[str, str]]]:
    if not path:
        return {"H": [], "V": []}
    csv_path = Path(path)
    if not csv_path.exists():
        return {"H": [], "V": []}
    grouped = {"H": [], "V": []}
    for row in read_csv(csv_path):
        plane = row.get("plane", "").strip().upper()
        if plane in grouped:
            grouped[plane].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: parse_int(item.get("window_index"), 0))
    return grouped


def trace_values(rows: Sequence[dict[str, str]]) -> list[float]:
    return finite(parse_float(row.get("selected_tune")) for row in rows)


def smoothness(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    diffs = [b - a for a, b in zip(values, values[1:])]
    return math.sqrt(statistics.fmean([diff * diff for diff in diffs]))


def run_flash_batch(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    manifest = load_manifest(Path(args.manifest))
    requested = [int(item) for item in args.flashes]
    all_summaries: dict[int, list[dict[str, object]]] = {}
    for count in requested:
        rows: list[dict[str, object]] = []
        for row in manifest:
            traces = read_sliding_tune(row.get("sliding_tune_csv", ""))
            for plane in ("H", "V"):
                values = trace_values(traces[plane])
                conf = finite(parse_float(item.get("selected_confidence")) for item in traces[plane])
                effective = min(count, len(values))
                low_conf = sum(1 for value in conf if value < 1.5)
                rows.append(
                    {
                        "target_ms": row.get("target_ms", ""),
                        "run_name": row.get("run_name", ""),
                        "plane": plane,
                        "requested_flashes": count,
                        "available_windows": len(values),
                        "effective_flashes": effective,
                        "missing_fraction": fmt_float((count - effective) / count if count else 0.0),
                        "tune_mean": fmt_float(mean(values)),
                        "tune_std": fmt_float(stdev(values)),
                        "smoothness_rms": fmt_float(smoothness(values)),
                        "low_confidence_fraction": fmt_float(low_conf / len(conf) if conf else 1.0),
                    }
                )
        all_summaries[count] = rows
        write_csv(
            out_dir / f"flash_summary_{count}.csv",
            rows,
            [
                "target_ms",
                "run_name",
                "plane",
                "requested_flashes",
                "available_windows",
                "effective_flashes",
                "missing_fraction",
                "tune_mean",
                "tune_std",
                "smoothness_rms",
                "low_confidence_fraction",
            ],
        )
    write_flash_comparison(out_dir / "flash_comparison.md", all_summaries)
    largest = max(requested) if requested else 0
    make_flash_plots(out_dir, manifest, all_summaries, largest)


def write_flash_comparison(path: Path, all_summaries: dict[int, list[dict[str, object]]]) -> None:
    lines = [
        "# Flash Comparison",
        "",
        "Flash-mode summary from available `sliding_tune.csv` traces.",
        "Requested flash counts larger than available sliding windows are reported with missing fractions instead of being silently filled.",
        "",
        "| requested flashes | plane | rows | median effective | median missing fraction | median smoothness |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for count, rows in sorted(all_summaries.items()):
        for plane in ("H", "V"):
            plane_rows = [row for row in rows if row.get("plane") == plane]
            lines.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    count,
                    plane,
                    len(plane_rows),
                    fmt_float(median(parse_float(row.get("effective_flashes")) for row in plane_rows), 2),
                    fmt_float(median(parse_float(row.get("missing_fraction")) for row in plane_rows), 3),
                    fmt_float(median(parse_float(row.get("smoothness_rms")) for row in plane_rows), 6),
                )
            )
    write_text(path, "\n".join(lines) + "\n")


def trace_matrix(manifest: Sequence[dict[str, str]], plane: str, max_rows: int = 80) -> list[list[Optional[float]]]:
    matrix: list[list[Optional[float]]] = []
    for row in manifest:
        traces = read_sliding_tune(row.get("sliding_tune_csv", ""))
        values = trace_values(traces[plane])
        if values:
            matrix.append(values)
        if len(matrix) >= max_rows:
            break
    return matrix


def make_flash_plots(
    out_dir: Path,
    manifest: Sequence[dict[str, str]],
    all_summaries: dict[int, list[dict[str, object]]],
    largest: int,
) -> None:
    _ = largest
    heatmap_plot(out_dir / "flash_waterfall_h.png", "FLASH WATERFALL H", trace_matrix(manifest, "H"), y_label="SPILL")
    heatmap_plot(out_dir / "flash_waterfall_v.png", "FLASH WATERFALL V", trace_matrix(manifest, "V"), y_label="SPILL")
    for plane in ("H", "V"):
        labels = []
        values = []
        for count, rows in sorted(all_summaries.items()):
            plane_rows = [row for row in rows if row.get("plane") == plane]
            labels.append(str(count))
            values.append(median(parse_float(row.get("effective_flashes")) for row in plane_rows) or 0.0)
        suffix = "h" if plane == "H" else "v"
        bar_plot(out_dir / f"flash_count_comparison_{suffix}.png", f"FLASH COUNT COMPARISON {plane}", labels, values, "EFFECTIVE")
    labels = []
    values = []
    for count, rows in sorted(all_summaries.items()):
        labels.append(str(count))
        values.append(median(parse_float(row.get("smoothness_rms")) for row in rows) or 0.0)
    bar_plot(out_dir / "flash_reproducibility.png", "FLASH REPRODUCIBILITY", labels, values, "SMOOTH")


def build_spectrograms(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    manifest = load_manifest(Path(args.manifest))
    poster_rows = [row for row in manifest if bool_field(row, "suitable_for_poster")]
    source_rows = poster_rows or [row for row in manifest if row.get("sliding_tune_csv")]
    representative = source_rows[0] if source_rows else None
    copied: list[str] = []
    if representative:
        for plane in ("h", "v"):
            src = representative.get(f"spectrogram_{plane}", "")
            dest = out_dir / f"representative_spectrogram_{plane}.png"
            if src and Path(src).exists():
                shutil.copyfile(src, dest)
                copied.append(dest.name)
            else:
                no_data_png(dest, f"REPRESENTATIVE SPECTROGRAM {plane.upper()}", "SOURCE MISSING")
    else:
        no_data_png(out_dir / "representative_spectrogram_h.png", "REPRESENTATIVE SPECTROGRAM H")
        no_data_png(out_dir / "representative_spectrogram_v.png", "REPRESENTATIVE SPECTROGRAM V")

    heatmap_plot(out_dir / "composite_waterfall_h.png", "COMPOSITE WATERFALL H", trace_matrix(manifest, "H"), y_label="SPILL")
    heatmap_plot(out_dir / "composite_waterfall_v.png", "COMPOSITE WATERFALL V", trace_matrix(manifest, "V"), y_label="SPILL")
    heatmap_plot(out_dir / "median_spectrogram_h.png", "MEDIAN TUNE DENSITY H", median_trace_density(manifest, "H"), y_label="TRACE")
    heatmap_plot(out_dir / "median_spectrogram_v.png", "MEDIAN TUNE DENSITY V", median_trace_density(manifest, "V"), y_label="TRACE")
    lines = [
        "# Spectrogram Products",
        "",
        f"- requested device: `{args.device}`",
        f"- representative source copied: `{', '.join(copied) if copied else 'none'}`",
        "- composite waterfalls use selected tune traces from `sliding_tune.csv`.",
        "- median spectrogram PNGs are tune-density products when raw spectral-power cubes are not available.",
    ]
    write_text(out_dir / "spectrogram_products.md", "\n".join(lines) + "\n")


def median_trace_density(manifest: Sequence[dict[str, str]], plane: str) -> list[list[Optional[float]]]:
    matrix = trace_matrix(manifest, plane, max_rows=200)
    if not matrix:
        return []
    cols = max(len(row) for row in matrix)
    med = []
    for col in range(cols):
        med.append(median(row[col] for row in matrix if col < len(row)))
    return [med for _ in range(max(8, min(32, len(matrix))))]


def run_bpm_subset_checks(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    rows = load_manifest(Path(args.manifest))
    modes = build_subset_proxy_modes(rows)
    summary_rows: list[dict[str, object]] = []
    for name, mode_rows in modes.items():
        for plane, tune_field in (("H", "baseline_qx"), ("V", "baseline_qy")):
            values = [numeric(row, tune_field) for row in mode_rows if bool_field(row, "usable_for_analysis")]
            summary_rows.append(
                {
                    "mode": name,
                    "plane": plane,
                    "spill_count": len(finite(values)),
                    "median_tune": fmt_float(median(values)),
                    "std_tune": fmt_float(stdev(values)),
                    "note": "spill-subset proxy; per-BPM raw subset spectra not present" if name != "all_bpm_baseline" else "all available accepted rows",
                }
            )
    write_csv(out_dir / "bpm_subset_summary.csv", summary_rows, ["mode", "plane", "spill_count", "median_tune", "std_tune", "note"])
    write_subset_markdown(out_dir / "bpm_subset_consistency.md", summary_rows)
    make_subset_plots(out_dir, summary_rows)


def build_subset_proxy_modes(rows: Sequence[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    usable_sorted = [row for row in rows if bool_field(row, "usable_for_analysis")]
    usable_sorted.sort(key=lambda row: parse_float(row.get("score"), 0.0) or 0.0, reverse=True)
    half = max(1, len(usable_sorted) // 2)
    return {
        "all_bpm_baseline": usable_sorted,
        "top_confidence_half": usable_sorted[:half],
        "lower_confidence_half": usable_sorted[half:],
        "odd_spill_split": usable_sorted[::2],
        "even_spill_split": usable_sorted[1::2],
    }


def write_subset_markdown(path: Path, rows: Sequence[dict[str, object]]) -> None:
    lines = [
        "# BPM Subset Consistency",
        "",
        "The ranked summary artifacts do not include per-BPM spectra or `bpm_quality_table.csv`, so this command writes a conservative spill-subset proxy instead of claiming a true per-BPM subset result.",
        "",
        "For a real all-BPM/best-BPM/top-N BPM comparison, run `analyze-phase` or provide per-BPM study artifacts next to the manifest source.",
        "",
        "| mode | plane | spill count | median tune | std tune |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('mode')} | {row.get('plane')} | {row.get('spill_count')} | {row.get('median_tune')} | {row.get('std_tune')} |"
        )
    write_text(path, "\n".join(lines) + "\n")


def make_subset_plots(out_dir: Path, rows: Sequence[dict[str, object]]) -> None:
    for plane in ("H", "V"):
        plane_rows = [row for row in rows if row.get("plane") == plane]
        labels = [str(row.get("mode", "")).replace("_", "-")[:8] for row in plane_rows]
        medians = [parse_float(row.get("median_tune"), 0.0) or 0.0 for row in plane_rows]
        suffix = "h" if plane == "H" else "v"
        bar_plot(out_dir / f"subset_consistency_{suffix}.png", f"SUBSET CONSISTENCY {plane}", labels, medians, "TUNE")
    no_data_png(out_dir / "best_bpm_vs_all_bpm.png", "BEST BPM VS ALL BPM", "PER-BPM DATA NEEDED")


def build_split_rows(rows: Sequence[dict[str, str]], mode: str = "random", seed: int = 7) -> list[dict[str, object]]:
    unique = []
    seen = set()
    for row in rows:
        spill_id = row.get("spill_id", "")
        if spill_id and spill_id not in seen:
            seen.add(spill_id)
            unique.append(row)
    if mode == "chronological":
        unique.sort(key=lambda row: parse_int(row.get("target_ms"), 0))
    else:
        unique = list(unique)
        random.Random(seed).shuffle(unique)
    n = len(unique)
    train_end = int(n * 0.70)
    val_end = train_end + int(n * 0.15)
    split_by_id = {}
    for idx, row in enumerate(unique):
        split = "train" if idx < train_end else "validation" if idx < val_end else "test"
        split_by_id[row.get("spill_id", "")] = split
    return [
        {
            "spill_id": row.get("spill_id", ""),
            "target_ms": row.get("target_ms", ""),
            "run_name": row.get("run_name", ""),
            "split": split_by_id.get(row.get("spill_id", ""), "test"),
            "usable_for_analysis": row.get("usable_for_analysis", "false"),
        }
        for row in rows
    ]


def train_quality_model(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    rows = read_csv(Path(args.features)) if args.features else load_manifest(Path(args.manifest))
    if rows and "label_physics_usable" not in rows[0]:
        split_rows = build_split_rows(rows, args.split_mode, args.seed)
        write_csv(out_dir / "split_manifest.csv", split_rows, ["spill_id", "target_ms", "run_name", "split", "usable_for_analysis"])
        write_features(out_dir / "features.csv", rows, split_rows)
        rows = read_csv(out_dir / "features.csv")
    else:
        split_rows = build_split_rows(rows, args.split_mode, args.seed)
        write_csv(out_dir / "split_manifest.csv", split_rows, ["spill_id", "target_ms", "run_name", "split", "usable_for_analysis"])

    feature_names = [
        "score",
        "coverage_h",
        "coverage_v",
        "baseline_confidence_h",
        "baseline_confidence_v",
        "std_qx",
        "std_qy",
        "fallback_h",
        "fallback_v",
        "suspicious_h",
        "suspicious_v",
        "turn_count_h",
        "turn_count_v",
    ]
    labels = [parse_int(row.get("label_physics_usable"), 0) for row in rows]
    if len(set(labels)) < 2:
        write_text(out_dir / "quality_model_metrics.md", "# Quality Model Metrics\n\nOnly one class is present; classifier training skipped.\n")
        no_data_png(out_dir / "quality_confusion_matrix.png", "QUALITY CONFUSION MATRIX", "ONE CLASS")
        no_data_png(out_dir / "quality_feature_importance.png", "QUALITY FEATURE IMPORTANCE", "ONE CLASS")
        no_data_png(out_dir / "quality_score_vs_error.png", "QUALITY SCORE VS ERROR", "ONE CLASS")
        return

    weights, midpoint = fit_mean_difference_classifier(rows, feature_names, labels)
    predictions = []
    scores = []
    for row in rows:
        score = classifier_score(row, feature_names, weights, midpoint)
        scores.append(score)
        predictions.append(1 if score >= 0.5 else 0)
    metrics = confusion(labels, predictions)
    write_quality_metrics(out_dir / "quality_model_metrics.md", metrics, feature_names, weights)
    plot_confusion_matrix(out_dir / "quality_confusion_matrix.png", metrics)
    plot_feature_importance(out_dir / "quality_feature_importance.png", feature_names, weights)
    plot_quality_score_vs_error(out_dir / "quality_score_vs_error.png", rows, scores)
    write_text(
        out_dir / "ml_training_summary.md",
        "# ML Training Summary\n\n"
        f"- split mode: `{args.split_mode}`\n"
        "- labels: weak BPM-only `label_physics_usable` derived from artifact quality fields\n"
        "- model: dependency-free mean-difference linear classifier\n",
    )


def fit_mean_difference_classifier(
    rows: Sequence[dict[str, str]], feature_names: Sequence[str], labels: Sequence[int]
) -> tuple[dict[str, float], dict[str, float]]:
    means_good: dict[str, float] = {}
    means_bad: dict[str, float] = {}
    midpoint: dict[str, float] = {}
    weights: dict[str, float] = {}
    for name in feature_names:
        good = [parse_float(row.get(name)) for row, label in zip(rows, labels) if label == 1]
        bad = [parse_float(row.get(name)) for row, label in zip(rows, labels) if label == 0]
        mg = mean(good) or 0.0
        mb = mean(bad) or 0.0
        spread = (stdev(finite(good) + finite(bad)) or 1.0) or 1.0
        means_good[name] = mg
        means_bad[name] = mb
        midpoint[name] = (mg + mb) / 2.0
        weights[name] = (mg - mb) / spread
    return weights, midpoint


def classifier_score(row: dict[str, str], feature_names: Sequence[str], weights: dict[str, float], midpoint: dict[str, float]) -> float:
    z = 0.0
    for name in feature_names:
        value = parse_float(row.get(name), midpoint[name]) or midpoint[name]
        z += weights[name] * (value - midpoint[name])
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def confusion(labels: Sequence[int], predictions: Sequence[int]) -> dict[str, Union[int, float]]:
    tp = sum(1 for y, p in zip(labels, predictions) if y == 1 and p == 1)
    tn = sum(1 for y, p in zip(labels, predictions) if y == 0 and p == 0)
    fp = sum(1 for y, p in zip(labels, predictions) if y == 0 and p == 1)
    fn = sum(1 for y, p in zip(labels, predictions) if y == 1 and p == 0)
    total = max(1, tp + tn + fp + fn)
    return {
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "accuracy": (tp + tn) / total,
        "precision": tp / max(1, tp + fp),
        "recall": tp / max(1, tp + fn),
    }


def write_quality_metrics(path: Path, metrics: dict[str, Union[int, float]], feature_names: Sequence[str], weights: dict[str, float]) -> None:
    ranked = sorted(feature_names, key=lambda name: abs(weights[name]), reverse=True)
    lines = [
        "# Quality Model Metrics",
        "",
        "Dependency-free quality classifier using weak BPM-only labels derived from existing artifact quality fields.",
        "",
        f"- accuracy: `{metrics['accuracy']:.3f}`",
        f"- precision: `{metrics['precision']:.3f}`",
        f"- recall: `{metrics['recall']:.3f}`",
        f"- TP/TN/FP/FN: `{metrics['tp']}` / `{metrics['tn']}` / `{metrics['fp']}` / `{metrics['fn']}`",
        "",
        "## Feature Importance",
        "",
    ]
    for name in ranked:
        lines.append(f"- `{name}`: `{weights[name]:.4f}`")
    write_text(path, "\n".join(lines) + "\n")


def plot_confusion_matrix(path: Path, metrics: dict[str, Union[int, float]]) -> None:
    labels = ["TP", "TN", "FP", "FN"]
    values = [float(metrics["tp"]), float(metrics["tn"]), float(metrics["fp"]), float(metrics["fn"])]
    bar_plot(path, "QUALITY CONFUSION MATRIX", labels, values, "COUNT")


def plot_feature_importance(path: Path, feature_names: Sequence[str], weights: dict[str, float]) -> None:
    ranked = sorted(feature_names, key=lambda name: abs(weights[name]), reverse=True)[:10]
    bar_plot(path, "QUALITY FEATURE IMPORTANCE", [name[:8] for name in ranked], [abs(weights[name]) for name in ranked], "ABS W")


def plot_quality_score_vs_error(path: Path, rows: Sequence[dict[str, str]], scores: Sequence[float]) -> None:
    points = []
    for idx, (row, score) in enumerate(zip(rows, scores)):
        err = (parse_float(row.get("std_qx"), 0.0) or 0.0) + (parse_float(row.get("std_qy"), 0.0) or 0.0)
        points.append((score, err + idx * 0.0))
    line_plot(path, "QUALITY SCORE VS ERROR", [("ERR", points, PURPLE)], x_label="SCORE", y_label="STD")


def train_tune_model(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    rows = read_csv(Path(args.features))
    has_labels = rows and "label_qx" in rows[0] and "label_qy" in rows[0]
    if not has_labels:
        write_text(
            out_dir / "tune_model_metrics.md",
            "# Tune Model Metrics\n\n"
            "Tune/ridge estimator training skipped because no independent `label_qx`/`label_qy` columns were provided. "
            "The current BPM baseline tune fields are not treated as independent supervised labels.\n",
        )
        no_data_png(out_dir / "predicted_vs_label_qx.png", "PREDICTED VS LABEL QX", "LABELS NEEDED")
        no_data_png(out_dir / "predicted_vs_label_qy.png", "PREDICTED VS LABEL QY", "LABELS NEEDED")
        no_data_png(out_dir / "residual_hist_qx.png", "RESIDUAL HIST QX", "LABELS NEEDED")
        no_data_png(out_dir / "residual_hist_qy.png", "RESIDUAL HIST QY", "LABELS NEEDED")
        no_data_png(out_dir / "ml_vs_baseline_error.png", "ML VS BASELINE ERROR", "LABELS NEEDED")
        return
    write_text(out_dir / "tune_model_metrics.md", "# Tune Model Metrics\n\nIndependent labels detected, but model training is intentionally deferred in this lightweight tool.\n")


def benchmark_dgx_processing(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    rows = load_manifest(Path(args.manifest))
    results: list[dict[str, object]] = []
    cpu_rate = benchmark_fft_backend("cpu", rows, args.windows, args.fft_points)
    results.append(cpu_rate)
    if args.device in {"auto", "cuda"}:
        results.append(benchmark_fft_backend("cuda", rows, args.windows, args.fft_points))
    write_csv(out_dir / "dgx_benchmark.csv", results, ["backend", "available", "spills", "windows", "fft_points", "seconds", "windows_per_second", "note"])
    write_benchmark_md(out_dir / "dgx_benchmark.md", results)
    labels = [str(row["backend"]).upper() for row in results]
    values = [parse_float(row.get("windows_per_second"), 0.0) or 0.0 for row in results]
    bar_plot(out_dir / "dgx_processing_rate.png", "DGX PROCESSING RATE", labels, values, "WIN/S")


def benchmark_fft_backend(backend: str, rows: Sequence[dict[str, str]], windows: int, fft_points: int) -> dict[str, object]:
    spills = max(1, min(len(rows), 256))
    total_windows = max(1, spills * windows)
    try:
        if backend == "cuda":
            import cupy as xp  # type: ignore
        else:
            import numpy as xp  # type: ignore
    except Exception as exc:
        return {
            "backend": backend,
            "available": "false",
            "spills": spills,
            "windows": total_windows,
            "fft_points": fft_points,
            "seconds": "",
            "windows_per_second": "0",
            "note": f"{backend} backend unavailable: {exc}",
        }
    rng = xp.random.default_rng(123) if backend == "cpu" else None
    start = time.perf_counter()
    if backend == "cuda":
        data = xp.random.standard_normal((total_windows, fft_points), dtype=xp.float32)
        window = xp.hanning(fft_points).astype(xp.float32)
        data = data - xp.mean(data, axis=1, keepdims=True)
        power = xp.abs(xp.fft.rfft(data * window, axis=1)) ** 2
        xp.cuda.Stream.null.synchronize()
        _ = float(xp.mean(power).get())
    else:
        data = rng.standard_normal((total_windows, fft_points), dtype=xp.float32)
        window = xp.hanning(fft_points).astype(xp.float32)
        data = data - xp.mean(data, axis=1, keepdims=True)
        power = xp.abs(xp.fft.rfft(data * window, axis=1)) ** 2
        _ = float(xp.mean(power))
    elapsed = max(1e-9, time.perf_counter() - start)
    return {
        "backend": backend,
        "available": "true",
        "spills": spills,
        "windows": total_windows,
        "fft_points": fft_points,
        "seconds": fmt_float(elapsed, 6),
        "windows_per_second": fmt_float(total_windows / elapsed, 2),
        "note": "synthetic batched FFT benchmark",
    }


def write_benchmark_md(path: Path, rows: Sequence[dict[str, object]]) -> None:
    lines = [
        "# DGX Benchmark",
        "",
        "Synthetic batched FFT benchmark matching the poster-plan processing pattern: mean subtraction, Hann window, real FFT, power calculation.",
        "",
        "| backend | available | windows | seconds | windows/sec | note |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('backend')} | {row.get('available')} | {row.get('windows')} | {row.get('seconds')} | {row.get('windows_per_second')} | {row.get('note')} |"
        )
    write_text(path, "\n".join(lines) + "\n")


def make_poster_plots(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    ensure_dir(out_dir)
    inputs = [Path(item) for item in args.input]
    wanted = [
        "dataset_overview.png",
        "injection_tune_reproducibility.png",
        "composite_waterfall_h.png",
        "composite_waterfall_v.png",
        "median_spectrogram_h.png",
        "median_spectrogram_v.png",
        "flash_count_comparison_h.png",
        "flash_count_comparison_v.png",
        "subset_consistency_h.png",
        "subset_consistency_v.png",
        "quality_confusion_matrix.png",
        "quality_feature_importance.png",
        "dgx_processing_rate.png",
    ]
    copied: list[str] = []
    for name in wanted:
        found = None
        for root in inputs:
            matches = sorted(root.rglob(name)) if root.is_dir() else []
            if matches:
                found = matches[0]
                break
        if found:
            dest = out_dir / name
            shutil.copyfile(found, dest)
            copied.append(name)
    lines = ["# Poster Plot Index", "", "Copied high-priority BPM-only poster plots:", ""]
    for name in copied:
        lines.append(f"- `{name}`")
    missing = [name for name in wanted if name not in copied]
    if missing:
        lines.extend(["", "Missing or skipped:", ""])
        for name in missing:
            lines.append(f"- `{name}`")
    write_text(out_dir / "poster_plot_index.md", "\n".join(lines) + "\n")


def run_all(args: argparse.Namespace) -> None:
    root = Path(args.out)
    processed = root / "processed"
    plots = root / "plots"
    models = root / "models"
    benchmarks = root / "benchmarks"
    poster = root / "poster_plots"
    manifest = build_manifest(argparse.Namespace(input=args.input, out=str(processed)))
    run_baseline_batch(argparse.Namespace(manifest=str(manifest), out=str(processed)))
    run_flash_batch(argparse.Namespace(manifest=str(manifest), flashes=args.flashes, out=str(processed)))
    build_spectrograms(argparse.Namespace(manifest=str(manifest), device=args.device, out=str(plots)))
    run_bpm_subset_checks(argparse.Namespace(manifest=str(manifest), out=str(processed)))
    train_quality_model(
        argparse.Namespace(
            manifest=str(manifest),
            features=str(processed / "features.csv"),
            out=str(models),
            split_mode="random",
            seed=7,
        )
    )
    train_tune_model(argparse.Namespace(features=str(processed / "features.csv"), out=str(models)))
    benchmark_dgx_processing(
        argparse.Namespace(manifest=str(manifest), out=str(benchmarks), device=args.device, windows=16, fft_points=4096)
    )
    make_poster_plots(argparse.Namespace(input=[str(processed), str(plots), str(models), str(benchmarks)], out=str(poster)))
    write_text(
        root / "poster_analysis_summary.md",
        "# Poster Analysis Summary\n\n"
        f"- manifest: `{manifest}`\n"
        f"- processed outputs: `{processed}`\n"
        f"- plot outputs: `{plots}`\n"
        f"- model outputs: `{models}`\n"
        f"- benchmark outputs: `{benchmarks}`\n"
        f"- poster plot set: `{poster}`\n",
    )


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="bpm_dgx_poster_test_") as tmp_text:
        tmp = Path(tmp_text)
        source = tmp / "ranktest"
        top = source / "top10_visuals" / "rank_01_1000"
        ensure_dir(top)
        write_csv(
            source / "candidate_spills.csv",
            [
                {
                    "rank": 1,
                    "score": 90,
                    "trackable_turns": 4096,
                    "run_name": "synthetic",
                    "bundle_dir": "/out/spill_1000",
                    "target_ms": 1000,
                    "artifact_complete": "true",
                    "requested_streams": 240,
                    "captured_streams": 240,
                    "same_spill_streams": 240,
                    "method_h": "grid",
                    "score_h": 90,
                    "trackable_turns_h": 4096,
                    "coverage_h": 0.9,
                    "best_window_h": 2048,
                    "best_stride_h": 256,
                    "median_qx": 0.67,
                    "std_qx": 0.001,
                    "confidence_p10_h": 5,
                    "fallback_h": 0,
                    "suspicious_h": 0,
                    "method_v": "grid",
                    "score_v": 91,
                    "trackable_turns_v": 4096,
                    "coverage_v": 0.9,
                    "best_window_v": 2048,
                    "best_stride_v": 256,
                    "median_qy": 0.71,
                    "std_qy": 0.001,
                    "confidence_p10_v": 5,
                    "fallback_v": 0,
                    "suspicious_v": 0,
                    "reason_flags": "",
                    "warnings": "",
                }
            ],
            [
                "rank",
                "score",
                "trackable_turns",
                "run_name",
                "bundle_dir",
                "target_ms",
                "artifact_complete",
                "requested_streams",
                "captured_streams",
                "same_spill_streams",
                "method_h",
                "score_h",
                "trackable_turns_h",
                "coverage_h",
                "best_window_h",
                "best_stride_h",
                "median_qx",
                "std_qx",
                "confidence_p10_h",
                "fallback_h",
                "suspicious_h",
                "method_v",
                "score_v",
                "trackable_turns_v",
                "coverage_v",
                "best_window_v",
                "best_stride_v",
                "median_qy",
                "std_qy",
                "confidence_p10_v",
                "fallback_v",
                "suspicious_v",
                "reason_flags",
                "warnings",
            ],
        )
        write_csv(
            top / "sliding_tune.csv",
            [
                {"plane": "H", "window_index": 0, "center_turn": 1024, "selected_tune": 0.671, "selected_confidence": 4},
                {"plane": "H", "window_index": 1, "center_turn": 2048, "selected_tune": 0.672, "selected_confidence": 4},
                {"plane": "V", "window_index": 0, "center_turn": 1024, "selected_tune": 0.711, "selected_confidence": 4},
                {"plane": "V", "window_index": 1, "center_turn": 2048, "selected_tune": 0.712, "selected_confidence": 4},
            ],
            ["plane", "window_index", "center_turn", "selected_tune", "selected_confidence"],
        )
        out = tmp / "out"
        run_all(argparse.Namespace(input=[str(source)], out=str(out), flashes=[2, 4], device="cpu"))
        required = [
            out / "processed" / "dataset_manifest.csv",
            out / "processed" / "baseline_summary.csv",
            out / "processed" / "flash_summary_2.csv",
            out / "plots" / "composite_waterfall_h.png",
            out / "benchmarks" / "dgx_benchmark.md",
            out / "poster_plots" / "poster_plot_index.md",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise AssertionError(f"self-test missing outputs: {missing}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BPM-only DGX poster analysis helper")
    parser.add_argument("--self-test", action="store_true", help="run a synthetic end-to-end smoke test")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("build-manifest", help="build dataset manifest from ranking artifacts")
    p.add_argument("--input", nargs="+", required=True, help="candidate_spills.csv file or artifact directory")
    p.add_argument("--out", required=True)
    p.set_defaults(func=build_manifest)

    p = sub.add_parser("run-baseline-batch", help="freeze baseline tune summary from manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=run_baseline_batch)

    p = sub.add_parser("run-flash-batch", help="summarize flash-mode traces")
    p.add_argument("--manifest", required=True)
    p.add_argument("--flashes", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=run_flash_batch)

    p = sub.add_parser("build-spectrograms", help="build poster spectrogram/waterfall products")
    p.add_argument("--manifest", required=True)
    p.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto")
    p.add_argument("--out", required=True)
    p.set_defaults(func=build_spectrograms)

    p = sub.add_parser("run-bpm-subset-checks", help="write BPM subset consistency products")
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=run_bpm_subset_checks)

    p = sub.add_parser("train-quality-model", help="train lightweight quality classifier from feature table")
    p.add_argument("--manifest", help="dataset manifest, used when --features is omitted")
    p.add_argument("--features", help="features CSV from run-baseline-batch")
    p.add_argument("--out", required=True)
    p.add_argument("--split-mode", choices=["random", "chronological"], default="random")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=train_quality_model)

    p = sub.add_parser("train-tune-model", help="train optional tune/ridge model when independent labels exist")
    p.add_argument("--features", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=train_tune_model)

    p = sub.add_parser("benchmark-dgx-processing", help="benchmark synthetic batched FFT processing")
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto")
    p.add_argument("--windows", type=int, default=16)
    p.add_argument("--fft-points", type=int, default=4096)
    p.set_defaults(func=benchmark_dgx_processing)

    p = sub.add_parser("make-poster-plots", help="collect high-priority poster plots")
    p.add_argument("--input", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=make_poster_plots)

    p = sub.add_parser("run-all", help="run the poster analysis pipeline on existing artifacts")
    p.add_argument("--input", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--flashes", nargs="+", default=["128", "256", "512"])
    p.add_argument("--device", choices=["cpu", "cuda", "auto"], default="auto")
    p.set_defaults(func=run_all)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return
    if not hasattr(args, "func"):
        parser.print_help()
        raise SystemExit(2)
    args.func(args)


def main_with_default(command: str, argv: Optional[Sequence[str]] = None) -> None:
    main([command, *(argv or sys.argv[1:])])


if __name__ == "__main__":
    main()
