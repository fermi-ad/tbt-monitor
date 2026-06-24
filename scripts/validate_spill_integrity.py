#!/usr/bin/env python3
"""Validate captured BPM spill payload integrity from a dataset manifest."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np


FIELDS = [
    "collection",
    "spill_id",
    "target_ms",
    "bundle_dir",
    "manifest_path",
    "position_stream_count",
    "h_bpm_count",
    "v_bpm_count",
    "turn_count",
    "missing_bpm_count",
    "constant_waveform_count",
    "clipped_waveform_count",
    "nan_inf_count",
    "rms_min",
    "rms_median",
    "rms_max",
    "mad_median",
    "usable_data_flag",
    "reject_reason",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def fmt(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.6g}"


def payload_path(bundle_dir: Path, payload_file: object) -> Path | None:
    if not isinstance(payload_file, str) or not payload_file:
        return None
    rel = Path(payload_file)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None
    return bundle_dir / rel


def stream_plane(item: dict[str, object]) -> str:
    plane = str(item.get("plane", "")).upper()
    if plane in {"H", "V"}:
        return plane
    key = str(item.get("stream_key", ""))
    if ":HP" in key:
        return "H"
    if ":VP" in key:
        return "V"
    return ""


def position_streams(manifest: dict[str, object]) -> list[dict[str, object]]:
    out = []
    for item in manifest.get("streams", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("stream_key", ""))
        if "TBT_POSITION" in key:
            out.append(item)
    return out


def inspect_spill(row: dict[str, str]) -> dict[str, object]:
    manifest_path = Path(row["manifest_path"])
    bundle_dir = Path(row["bundle_dir"])
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    streams = position_streams(data)
    rms_values: list[float] = []
    mad_values: list[float] = []
    constant_count = 0
    clipped_count = 0
    nonfinite_total = 0
    readable = 0
    turn_counts: dict[int, int] = {}
    h_bpms: set[str] = set()
    v_bpms: set[str] = set()
    missing = 0

    for stream in streams:
        plane = stream_plane(stream)
        bpm = str(stream.get("bpm_ip") or stream.get("device_label") or stream.get("stream_key", ""))
        if plane == "H":
            h_bpms.add(bpm)
        elif plane == "V":
            v_bpms.add(bpm)
        path = payload_path(bundle_dir, stream.get("payload_file"))
        sample_count = stream.get("sample_count")
        if sample_count is None and stream.get("payload_bytes") is not None:
            sample_count = int(stream.get("payload_bytes") or 0) // 4
        if path is None or sample_count is None or not path.exists():
            missing += 1
            continue
        values = np.fromfile(path, dtype="<f4", count=int(sample_count))
        if values.size == 0:
            missing += 1
            continue
        readable += 1
        turn_counts[int(values.size)] = turn_counts.get(int(values.size), 0) + 1
        finite = values[np.isfinite(values)]
        nonfinite_total += int(values.size - finite.size)
        if finite.size == 0:
            constant_count += 1
            continue
        rms = float(np.sqrt(np.mean(finite.astype(np.float64) ** 2)))
        med = float(np.median(finite))
        mad = float(np.median(np.abs(finite - med)))
        rms_values.append(rms)
        mad_values.append(mad)
        if float(np.max(finite) - np.min(finite)) <= max(1e-9, abs(med) * 1e-9):
            constant_count += 1
        lo = float(np.min(finite))
        hi = float(np.max(finite))
        if hi > lo:
            edge_fraction = (np.count_nonzero(finite == lo) + np.count_nonzero(finite == hi)) / finite.size
            if edge_fraction >= 0.05:
                clipped_count += 1

    turn_count = max(turn_counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if turn_counts else 0
    expected_bpms = max(len(h_bpms), len(v_bpms))
    missing_bpm_count = max(0, expected_bpms * 2 - len(h_bpms) - len(v_bpms))
    reasons = []
    if str(row.get("capture_status", "")).lower() != "complete":
        reasons.append("PARTIAL_CAPTURE")
    if not h_bpms:
        reasons.append("MISSING_H")
    if not v_bpms:
        reasons.append("MISSING_V")
    if missing:
        reasons.append("MISSING_PAYLOAD")
    if nonfinite_total:
        reasons.append("NAN_INF")
    if readable and constant_count >= readable:
        reasons.append("CONSTANT_WAVEFORM")
    usable = not reasons
    rms_sorted = sorted(rms_values)
    mad_sorted = sorted(mad_values)
    return {
        "collection": row.get("collection", ""),
        "spill_id": row.get("spill_id", ""),
        "target_ms": row.get("target_ms", ""),
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(manifest_path),
        "position_stream_count": len(streams),
        "h_bpm_count": len(h_bpms),
        "v_bpm_count": len(v_bpms),
        "turn_count": turn_count,
        "missing_bpm_count": missing_bpm_count,
        "constant_waveform_count": constant_count,
        "clipped_waveform_count": clipped_count,
        "nan_inf_count": nonfinite_total,
        "rms_min": fmt(min(rms_sorted) if rms_sorted else None),
        "rms_median": fmt(float(np.median(rms_sorted)) if rms_sorted else None),
        "rms_max": fmt(max(rms_sorted) if rms_sorted else None),
        "mad_median": fmt(float(np.median(mad_sorted)) if mad_sorted else None),
        "usable_data_flag": str(usable).lower(),
        "reject_reason": "|".join(reasons),
    }


def write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    usable = sum(1 for row in rows if row.get("usable_data_flag") == "true")
    reasons: dict[str, int] = {}
    for row in rows:
        for reason in str(row.get("reject_reason", "")).split("|"):
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
    lines = [
        "# Spill Health Summary",
        "",
        f"- spills inspected: `{len(rows)}`",
        f"- usable spills: `{usable}`",
        "",
        "| reject reason | count |",
        "|---|---:|",
    ]
    for reason, count in sorted(reasons.items()):
        lines.append(f"| `{reason}` | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="dataset_manifest.csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help="accepted for workflow symmetry; CPU is used")
    args = parser.parse_args(argv)
    rows_in = read_csv(Path(args.manifest))
    if args.limit:
        rows_in = rows_in[: args.limit]
    rows = [inspect_spill(row) for row in rows_in]
    out = Path(args.out)
    write_csv(out / "spill_health.csv", rows, FIELDS)
    write_summary(out / "spill_health_summary.md", rows)
    print(f"wrote {len(rows)} rows to {out / 'spill_health.csv'}")


if __name__ == "__main__":
    main()
