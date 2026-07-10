#!/usr/bin/env python3
"""Quantify the post-normalization ranking defect in legacy best-single runs."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import numpy as np

from bpm_mining.io import atomic_write_text, write_csv
from gpu_analyze_captured_spills import discover_manifests, load_bundle, load_plane_traces


FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "channel_count",
    "legacy_selected_source_key",
    "raw_rms_leader_source_key",
    "legacy_selected_raw_rms_rank",
    "legacy_selected_is_raw_top1",
    "legacy_selected_is_raw_top5",
    "legacy_selected_raw_rms",
    "raw_rms_leader",
    "legacy_to_leader_rms_ratio",
    "normalized_rms_min",
    "normalized_rms_max",
    "normalized_rms_range",
]


def fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def evenly_spaced(items: Sequence[Path], limit: int) -> list[Path]:
    if limit <= 0 or limit >= len(items):
        return list(items)
    indices = np.linspace(0, len(items) - 1, limit, dtype=int)
    return [items[int(index)] for index in indices]


def selection_row(
    collection: str,
    spill_id: str,
    plane: str,
    labels: Sequence[str],
    normalized_traces: np.ndarray,
    raw_rms: np.ndarray,
) -> dict[str, object]:
    normalized_rms = np.sqrt(np.mean(np.asarray(normalized_traces, dtype=np.float64) ** 2, axis=1))
    legacy_index = int(np.argsort(normalized_rms)[::-1][0])
    raw_order = np.argsort(raw_rms)[::-1]
    leader_index = int(raw_order[0])
    raw_rank = int(np.flatnonzero(raw_order == legacy_index)[0]) + 1
    leader = float(raw_rms[leader_index])
    selected = float(raw_rms[legacy_index])
    return {
        "collection": collection,
        "spill_id": spill_id,
        "plane": plane,
        "channel_count": len(labels),
        "legacy_selected_source_key": labels[legacy_index],
        "raw_rms_leader_source_key": labels[leader_index],
        "legacy_selected_raw_rms_rank": raw_rank,
        "legacy_selected_is_raw_top1": str(raw_rank == 1).lower(),
        "legacy_selected_is_raw_top5": str(raw_rank <= 5).lower(),
        "legacy_selected_raw_rms": fmt(selected),
        "raw_rms_leader": fmt(leader),
        "legacy_to_leader_rms_ratio": fmt(selected / leader if leader > 0 else math.nan),
        "normalized_rms_min": fmt(float(np.min(normalized_rms))),
        "normalized_rms_max": fmt(float(np.max(normalized_rms))),
        "normalized_rms_range": fmt(float(np.max(normalized_rms) - np.min(normalized_rms))),
    }


def median(values: Sequence[float]) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return math.nan
    mid = len(clean) // 2
    return clean[mid] if len(clean) % 2 else 0.5 * (clean[mid - 1] + clean[mid])


def audit(inputs: Sequence[Path], out: Path, limit: int, turn_end: int) -> None:
    manifests = evenly_spaced(discover_manifests(list(inputs)), limit)
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for manifest in manifests:
        bundle = load_bundle(manifest)
        for plane in ("H", "V"):
            traces, _turns, warnings, labels, raw_rms = load_plane_traces(
                bundle,
                plane,
                None,
                False,
                0,
                turn_end,
                "rms_per_bpm",
                2048,
                "stream_key",
            )
            if traces is None or raw_rms is None or not labels:
                errors.append(f"{bundle.run_name}/{bundle.target_ms}/{plane}: {'; '.join(warnings)}")
                continue
            rows.append(selection_row(bundle.run_name, f"spill_{bundle.target_ms}", plane, labels, traces, raw_rms))
    write_csv(out / "legacy_single_selector_audit.csv", rows, FIELDS)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["plane"])].append(row)
    lines = [
        "# Legacy Best-Single Selector Audit",
        "",
        f"- manifests sampled evenly across inputs: `{len(manifests)}`",
        f"- plane rows audited: `{len(rows)}`",
        f"- load errors: `{len(errors)}`",
        f"- turn range used for raw and normalized RMS: `0-{turn_end}`",
        "",
        "The legacy `best_single_bpm` path ranked RMS after `rms_per_bpm` normalization. The normalized RMS values are nominally one, so floating-point residuals can determine the selected channel. The corrected analyzer ranks raw pre-normalization RMS explicitly and only then applies waveform normalization.",
        "",
        "| Plane | Rows | Legacy equals raw top-1 | Legacy within raw top-5 | Median raw rank | Median selected/leader RMS | Median normalized RMS range |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for plane in ("H", "V"):
        group = grouped.get(plane, [])
        top1 = sum(str(row["legacy_selected_is_raw_top1"]) == "true" for row in group) / max(1, len(group))
        top5 = sum(str(row["legacy_selected_is_raw_top5"]) == "true" for row in group) / max(1, len(group))
        lines.append(
            f"| {plane} | {len(group)} | {top1:.4f} | {top5:.4f} | "
            f"{median([float(row['legacy_selected_raw_rms_rank']) for row in group]):.3f} | "
            f"{median([float(row['legacy_to_leader_rms_ratio']) for row in group]):.6f} | "
            f"{median([float(row['normalized_rms_range']) for row in group]):.3g} |"
        )
    if errors:
        lines.extend(["", "## Sample Errors", "", *[f"- {error}" for error in errors[:20]]])
    lines.extend(
        [
            "",
            "This audit characterizes implementation provenance. Raw RMS is itself only an amplitude proxy, not an external tune-quality label; publication claims should use the leakage-controlled adaptive selection and disjoint validation instead.",
        ]
    )
    atomic_write_text(out / "legacy_single_selector_audit.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=400, help="evenly spaced manifests; 0 audits all")
    parser.add_argument("--turn-end", type=int, default=50_000)
    args = parser.parse_args()
    audit([Path(value) for value in args.input], Path(args.out), args.limit, args.turn_end)


if __name__ == "__main__":
    main()
