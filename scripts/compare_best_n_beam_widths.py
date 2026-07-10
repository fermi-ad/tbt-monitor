#!/usr/bin/env python3
"""Compare exact membership and metrics across bounded Best-N beam-width runs."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from pathlib import Path

from bpm_mining.io import atomic_write_text, read_csv, write_csv


FIELDS = [
    "table",
    "plane",
    "subset_size",
    "reference_width",
    "comparison_width",
    "row_count",
    "exact_membership_fraction",
    "median_abs_score_delta",
    "max_abs_score_delta",
    "median_abs_q_delta",
    "max_abs_q_delta",
]


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _median(values: list[float]) -> float:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return math.nan
    middle = len(finite) // 2
    return finite[middle] if len(finite) % 2 else 0.5 * (finite[middle - 1] + finite[middle])


def _fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def keyed_unique(
    rows: list[dict[str, str]],
    key_fields: tuple[str, ...],
    label: str,
) -> dict[tuple[str, ...], dict[str, str]]:
    keyed: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in key_fields)
        if key in keyed:
            raise ValueError(f"duplicate {label} key: {key}")
        keyed[key] = row
    return keyed


def compare_table(
    table: str,
    filename: str,
    runs: dict[int, Path],
    reference_width: int,
) -> list[dict[str, object]]:
    key_fields = ("collection", "spill_id", "plane", "subset_size") if table == "curve" else ("collection", "spill_id", "plane", "fold", "subset_size")
    rows_by_width: dict[int, dict[tuple[str, ...], dict[str, str]]] = {}
    for width, path in runs.items():
        rows_by_width[width] = keyed_unique(
            read_csv(path / filename),
            key_fields,
            f"{table} width {width}",
        )
    reference = rows_by_width[reference_width]
    output: list[dict[str, object]] = []
    for width in sorted(rows_by_width):
        if width == reference_width:
            continue
        other_keys = set(rows_by_width[width])
        reference_keys = set(reference)
        if other_keys != reference_keys:
            missing = len(reference_keys - other_keys)
            extra = len(other_keys - reference_keys)
            raise ValueError(
                f"{table} width {width} key coverage differs from width {reference_width}: "
                f"missing={missing} extra={extra}"
            )
        grouped: dict[tuple[str, int], list[tuple[dict[str, str], dict[str, str]]]] = defaultdict(list)
        for key in sorted(reference):
            grouped[(key[2], int(key[-1]))].append((reference[key], rows_by_width[width][key]))
        for (plane, subset_size), pairs in sorted(grouped.items()):
            score_field = "subset_score" if table == "curve" else "train_score"
            q_field = "q_hat" if table == "curve" else "train_q_hat"
            score_deltas = [abs(_f(left.get(score_field)) - _f(right.get(score_field))) for left, right in pairs]
            q_deltas = [abs(_f(left.get(q_field)) - _f(right.get(q_field))) for left, right in pairs]
            output.append(
                {
                    "table": table,
                    "plane": plane,
                    "subset_size": subset_size,
                    "reference_width": reference_width,
                    "comparison_width": width,
                    "row_count": len(pairs),
                    "exact_membership_fraction": _fmt(sum(left.get("bpm_indices") == right.get("bpm_indices") for left, right in pairs) / max(1, len(pairs))),
                    "median_abs_score_delta": _fmt(_median(score_deltas)),
                    "max_abs_score_delta": _fmt(max(score_deltas) if score_deltas else math.nan),
                    "median_abs_q_delta": _fmt(_median(q_deltas)),
                    "max_abs_q_delta": _fmt(max(q_deltas) if q_deltas else math.nan),
                }
            )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="WIDTH=/path/to/run; repeat for each width")
    parser.add_argument("--reference-width", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    runs = {}
    for value in args.run:
        width, path = value.split("=", 1)
        runs[int(width)] = Path(path)
    if args.reference_width not in runs:
        raise SystemExit("reference width is not present in --run values")
    rows = compare_table("curve", "best_n_curve_rows.csv", runs, args.reference_width)
    rows.extend(compare_table("validation", "best_n_disjoint_validation.csv", runs, args.reference_width))
    out = Path(args.out)
    write_csv(out / "best_n_beam_width_comparison.csv", rows, FIELDS)
    lines = [
        "# Best-N Beam-Width Convergence",
        "",
        f"Reference width: `{args.reference_width}`",
        "",
        "Exact-membership differences are expected when near-tied beams diverge. Score and tune deltas determine whether that changes the model-selection conclusion.",
        "",
        "| table | plane | N | width | exact membership | median abs score delta | median abs tune delta |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['table']} | {row['plane']} | {row['subset_size']} | {row['comparison_width']} | "
            f"{row['exact_membership_fraction']} | {row['median_abs_score_delta']} | {row['median_abs_q_delta']} |"
        )
    atomic_write_text(out / "best_n_beam_width_comparison.md", "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
