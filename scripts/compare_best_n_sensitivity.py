#!/usr/bin/env python3
"""Compare Best-N summary curves across fit-window, fold-seed, or other sensitivity runs."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from bpm_mining.best_n import recommended_n
from bpm_mining.io import atomic_write_text, read_csv, write_csv


METRICS = {
    "blind_q_agreement_rate": ("BLIND FULL-BAND AGREEMENT", "FRACTION WITHIN TOLERANCE", (0.0, 1.0)),
    "blind_q_agreement_ci_low": ("BLIND AGREEMENT CI LOW", "FRACTION WITHIN TOLERANCE", (0.0, 1.0)),
    "blind_q_agreement_ci_high": ("BLIND AGREEMENT CI HIGH", "FRACTION WITHIN TOLERANCE", (0.0, 1.0)),
    "median_blind_selected_heldout_abs_q_delta": ("BLIND CHANNEL-DISJOINT DELTA", "ABSOLUTE TUNE DELTA", None),
    "blind_selected_heldout_abs_q_delta_ci_low": ("BLIND DELTA CI LOW", "ABSOLUTE TUNE DELTA", None),
    "blind_selected_heldout_abs_q_delta_ci_high": ("BLIND DELTA CI HIGH", "ABSOLUTE TUNE DELTA", None),
    "median_test_power_support": ("SELECTED LATER-WINDOW POWER", "POWER / BACKGROUND", None),
    "test_power_support_ci_low": ("SELECTED POWER CI LOW", "POWER / BACKGROUND", None),
    "test_power_support_ci_high": ("SELECTED POWER CI HIGH", "POWER / BACKGROUND", None),
    "median_heldout_power_support": ("HELD-OUT LATER-WINDOW POWER", "POWER / BACKGROUND", None),
    "heldout_power_support_ci_low": ("HELD-OUT POWER CI LOW", "POWER / BACKGROUND", None),
    "heldout_power_support_ci_high": ("HELD-OUT POWER CI HIGH", "POWER / BACKGROUND", None),
    "median_test_peak_prominence": ("SELECTED LATER-WINDOW PROMINENCE", "ROBUST Z", None),
    "test_peak_prominence_ci_low": ("SELECTED PROMINENCE CI LOW", "ROBUST Z", None),
    "test_peak_prominence_ci_high": ("SELECTED PROMINENCE CI HIGH", "ROBUST Z", None),
    "median_heldout_prominence": ("HELD-OUT LATER-WINDOW PROMINENCE", "ROBUST Z", None),
    "heldout_prominence_ci_low": ("HELD-OUT PROMINENCE CI LOW", "ROBUST Z", None),
    "heldout_prominence_ci_high": ("HELD-OUT PROMINENCE CI HIGH", "ROBUST Z", None),
}

COMPARISON_FIELDS = [
    "dimension",
    "plane",
    "subset_size",
    "metric",
    "reference_label",
    "comparison_label",
    "reference_value",
    "comparison_value",
    "delta",
]

RECOMMENDATION_FIELDS = ["dimension", "label", "plane", "recommended_n", "status"]


def finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def keyed(rows: list[dict[str, str]], label: str) -> dict[tuple[str, int], dict[str, str]]:
    output: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        key = (row.get("plane", ""), int(row.get("subset_size") or 0))
        if key in output:
            raise ValueError(f"duplicate Best-N summary key in {label}: {key}")
        output[key] = row
    return output


def comparison_rows(
    dimension: str,
    runs: dict[str, list[dict[str, str]]],
    reference_label: str,
) -> list[dict[str, object]]:
    keyed_runs = {label: keyed(rows, label) for label, rows in runs.items()}
    reference = keyed_runs[reference_label]
    output: list[dict[str, object]] = []
    for label in sorted(runs):
        if label == reference_label:
            continue
        other = keyed_runs[label]
        if set(other) != set(reference):
            missing = len(set(reference) - set(other))
            extra = len(set(other) - set(reference))
            raise ValueError(
                f"Best-N summary key coverage differs for {label} versus {reference_label}: "
                f"missing={missing} extra={extra}"
            )
        for plane, subset_size in sorted(reference):
            for metric in METRICS:
                left = finite(reference[(plane, subset_size)].get(metric))
                right = finite(other[(plane, subset_size)].get(metric))
                output.append(
                    {
                        "dimension": dimension,
                        "plane": plane,
                        "subset_size": subset_size,
                        "metric": metric,
                        "reference_label": reference_label,
                        "comparison_label": label,
                        "reference_value": fmt(left),
                        "comparison_value": fmt(right),
                        "delta": fmt(right - left),
                    }
                )
    return output


def recommendation_rows(
    dimension: str,
    runs: dict[str, list[dict[str, str]]],
    tune_half_width: float,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for label in sorted(runs):
        for plane in ("H", "V"):
            chosen, reason = recommended_n(runs[label], plane, tune_half_width)
            output.append(
                {
                    "dimension": dimension,
                    "label": label,
                    "plane": plane,
                    "recommended_n": int(chosen["subset_size"]) if chosen is not None else "",
                    "status": "OK" if chosen is not None else reason,
                }
            )
    return output


def write_plots(
    dimension: str,
    runs: dict[str, list[dict[str, str]]],
    out: Path,
) -> None:
    try:
        import bpm_dgx_poster as poster
    except Exception:
        return
    colors = [poster.BLUE, poster.ORANGE, poster.GREEN, poster.RED]
    for plane in ("H", "V"):
        for metric, (title, ylabel, ylim) in METRICS.items():
            series = []
            for index, label in enumerate(sorted(runs)):
                points = sorted(
                    (
                        float(row["subset_size"]),
                        finite(row.get(metric)),
                    )
                    for row in runs[label]
                    if row.get("plane") == plane
                )
                series.append((label, points, colors[index % len(colors)]))
            poster.line_plot(
                out / f"best_n_sensitivity_{dimension}_{metric}_{plane.lower()}.png",
                f"{title} {plane}: {dimension}",
                series,
                "SUBSET SIZE N",
                ylabel,
                ylim,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dimension", required=True, help="fit_windows, fold_seed, or another sensitivity label")
    parser.add_argument("--run", action="append", required=True, help="LABEL=/path/to/merged-run; repeat for each run")
    parser.add_argument("--reference-label", required=True)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    run_paths: dict[str, Path] = {}
    for value in args.run:
        label, path = value.split("=", 1)
        if label in run_paths:
            raise SystemExit(f"duplicate run label: {label}")
        run_paths[label] = Path(path)
    if args.reference_label not in run_paths:
        raise SystemExit("reference label is not present in --run values")
    runs = {label: read_csv(path / "best_n_summary.csv") for label, path in run_paths.items()}
    out = Path(args.out)
    comparisons = comparison_rows(args.dimension, runs, args.reference_label)
    recommendations = recommendation_rows(args.dimension, runs, args.tune_half_width)
    write_csv(out / f"best_n_{args.dimension}_sensitivity.csv", comparisons, COMPARISON_FIELDS)
    write_csv(out / f"best_n_{args.dimension}_recommendations.csv", recommendations, RECOMMENDATION_FIELDS)
    write_plots(args.dimension, runs, out)

    lines = [
        f"# Best-N {args.dimension} Sensitivity",
        "",
        f"Reference label: `{args.reference_label}`",
        "",
        "The N recommendation is recomputed independently for every run. Agreement is blind over the full tune band; conditioned selected and held-out contrast metrics remain separate.",
        "",
        "| label | plane | recommended N | status |",
        "| --- | --- | ---: | --- |",
    ]
    for row in recommendations:
        lines.append(f"| {row['label']} | {row['plane']} | {row['recommended_n']} | {row['status']} |")
    atomic_write_text(out / f"best_n_{args.dimension}_sensitivity.md", "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
