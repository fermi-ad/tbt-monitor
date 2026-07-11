#!/usr/bin/env python3
"""Join manifest-level position absences to accepted per-spill Best-N memberships."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from bpm_mining.contracts import file_sha256
from bpm_mining.io import atomic_write_text, read_csv, write_csv


FIELDS = (
    "collection",
    "spill_id",
    "plane",
    "missing_position_source_key",
    "evaluation_status",
    "selected_n",
    "selected_cardinality",
    "missing_stream_selected",
    "selected_source_keys",
)


def compare_absences(
    missing_path: Path,
    curve_path: Path,
    out: Path,
    selected_sizes: Mapping[str, int],
) -> dict[str, object]:
    missing_rows = read_csv(missing_path)
    if not missing_rows:
        raise ValueError("missing-position inventory is empty")
    if set(selected_sizes) != {"H", "V"} or any(value <= 0 for value in selected_sizes.values()):
        raise ValueError("selected sizes must contain positive H and V values")

    affected_keys = {
        (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
        for row in missing_rows
    }
    accepted: dict[tuple[str, str, str], dict[str, str]] = {}
    curve_collections: set[str] = set()
    for row in read_csv(curve_path):
        collection = row.get("collection", "")
        curve_collections.add(collection)
        key = (collection, row.get("spill_id", ""), row.get("plane", ""))
        if key not in affected_keys:
            continue
        if int(row.get("subset_size") or 0) != selected_sizes.get(key[2], -1):
            continue
        if key in accepted:
            raise ValueError(f"duplicate accepted Best-N row for {key}")
        accepted[key] = row

    output: list[dict[str, object]] = []
    evaluated_keys: set[tuple[str, str, str]] = set()
    for missing in missing_rows:
        collection = missing.get("collection", "")
        spill_id = missing.get("spill_id", "")
        plane = missing.get("plane", "")
        source_key = missing.get("missing_position_source_key", "")
        key = (collection, spill_id, plane)
        if collection not in curve_collections:
            output.append(
                {
                    "collection": collection,
                    "spill_id": spill_id,
                    "plane": plane,
                    "missing_position_source_key": source_key,
                    "evaluation_status": "OUTSIDE_BEST_N_POSITION_CORPUS",
                    "selected_n": "",
                    "selected_cardinality": "",
                    "missing_stream_selected": "false",
                    "selected_source_keys": "",
                }
            )
            continue
        row = accepted.get(key)
        if row is None:
            raise ValueError(f"missing accepted Best-N row for affected spill-plane {key}")
        selected = [value for value in row.get("bpm_source_keys", "").split(",") if value]
        expected_n = selected_sizes[plane]
        if len(selected) != expected_n or len(set(selected)) != expected_n:
            raise ValueError(f"accepted Best-N cardinality mismatch for {key}")
        evaluated_keys.add(key)
        output.append(
            {
                "collection": collection,
                "spill_id": spill_id,
                "plane": plane,
                "missing_position_source_key": source_key,
                "evaluation_status": "OK",
                "selected_n": expected_n,
                "selected_cardinality": len(selected),
                "missing_stream_selected": str(source_key in selected).lower(),
                "selected_source_keys": ",".join(selected),
            }
        )

    output.sort(
        key=lambda row: (
            str(row["collection"]),
            str(row["spill_id"]),
            str(row["plane"]),
            str(row["missing_position_source_key"]),
        )
    )
    out.mkdir(parents=True, exist_ok=True)
    comparison_path = out / "missing_position_best_n_intersections.csv"
    write_csv(comparison_path, output, FIELDS)
    evaluated = [row for row in output if row["evaluation_status"] == "OK"]
    outside = [row for row in output if row["evaluation_status"] != "OK"]
    overlap = [row for row in evaluated if row["missing_stream_selected"] == "true"]
    report = {
        "schema": "tbt-monitor.payload-absence-best-n-intersection/v1",
        "status": "pass" if not overlap else "fail",
        "missing_position_rows": len(output),
        "evaluated_position_rows": len(evaluated),
        "evaluated_spill_plane_rows": len(evaluated_keys),
        "outside_best_n_corpus_rows": len(outside),
        "selected_overlap_rows": len(overlap),
        "selected_sizes": dict(selected_sizes),
        "missing_position_inventory_sha256": file_sha256(missing_path),
        "best_n_curve_sha256": file_sha256(curve_path),
        "comparison_sha256": file_sha256(comparison_path),
    }
    report_path = out / "missing_position_best_n_intersection.json"
    atomic_write_text(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Payload Absence / Best-N Intersection",
        "",
        f"Status: **{str(report['status']).upper()}**",
        "",
        f"- absent position rows: `{report['missing_position_rows']}`",
        f"- rows in the position-only Best-N corpus: `{report['evaluated_position_rows']}`",
        f"- affected position-only spill-plane rows: `{report['evaluated_spill_plane_rows']}`",
        f"- rows outside the Best-N position corpus: `{report['outside_best_n_corpus_rows']}`",
        f"- absent streams present in accepted membership: `{report['selected_overlap_rows']}`",
        "",
        "This identity join does not restore absent channels or estimate their counterfactual signal. It only proves that accepted per-spill memberships retain exact cardinality without naming an absent source stream.",
        "",
    ]
    atomic_write_text(out / "missing_position_best_n_intersection.md", "\n".join(lines))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missing-position-csv", type=Path, required=True)
    parser.add_argument("--best-n-curve-csv", type=Path, required=True)
    parser.add_argument("--selected-h-n", type=int, required=True)
    parser.add_argument("--selected-v-n", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare_absences(
            args.missing_position_csv.resolve(),
            args.best_n_curve_csv.resolve(),
            args.out.resolve(),
            {"H": args.selected_h_n, "V": args.selected_v_n},
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
