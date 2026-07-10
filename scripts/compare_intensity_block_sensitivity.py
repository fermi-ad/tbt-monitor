#!/usr/bin/env python3
"""Compare block-aware intensity decisions across completed re-summaries."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Mapping, Sequence

import bpm_dgx_poster as poster

from bpm_mining.io import atomic_write_text, ensure_dir, read_csv, write_csv


SUMMARY_FIELDS = [
    "label",
    "block_spills",
    "effect_tests",
    "fdr_significant_directional_effects",
    "practical_ci_passes",
    "retained_effects",
    "retained_effect_keys",
    "max_directional_ci_fraction_of_mpe",
    "minimum_fdr_q_value",
    "n1_max_abs_median_paired_delta",
    "source",
]


def number(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def parse_run(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"run must be LABEL=PATH: {value!r}")
    label, raw_path = value.split("=", 1)
    root = Path(raw_path).expanduser().resolve()
    source = root / "intensity_method_effects.csv"
    if not source.exists():
        raise ValueError(f"missing intensity effects: {source}")
    return label, root


def summarize_run(label: str, root: Path) -> dict[str, object]:
    rows = read_csv(root / "intensity_method_effects.csv")
    block_values = {int(number(row.get("bootstrap_block_spills"))) for row in rows if math.isfinite(number(row.get("bootstrap_block_spills")))}
    if len(block_values) != 1:
        raise ValueError(f"{root}: expected one bootstrap block length, found {sorted(block_values)}")
    fdr_passes = [
        row
        for row in rows
        if row.get("statistical_benefit_pass") == "true"
        and math.isfinite(number(row.get("fdr_q_value")))
        and number(row.get("fdr_q_value")) <= 0.05
    ]
    practical = [row for row in rows if row.get("practical_effect_pass") == "true"]
    retained = [row for row in rows if row.get("retain_method_for_tune_analysis") == "true"]
    retained_keys = sorted(
        f"{row.get('plane', '')}|{row.get('subset_size', '')}|{row.get('method', '')}|{row.get('metric', '')}"
        for row in retained
    )
    directional_ci_fractions: list[float] = []
    for row in rows:
        margin = number(row.get("minimum_practical_effect"))
        low = number(row.get("bootstrap_ci_low"))
        high = number(row.get("bootstrap_ci_high"))
        if not math.isfinite(margin) or margin <= 0 or not math.isfinite(low) or not math.isfinite(high):
            continue
        directional_bound = low if row.get("beneficial_direction") == "increase" else -high
        directional_ci_fractions.append(directional_bound / margin)
    q_values = [number(row.get("fdr_q_value")) for row in rows]
    q_values = [value for value in q_values if math.isfinite(value)]
    n1_deltas: list[float] = []
    for row in rows:
        subset_size = number(row.get("subset_size"))
        delta = number(row.get("median_paired_delta"))
        if math.isfinite(subset_size) and int(subset_size) == 1 and math.isfinite(delta):
            n1_deltas.append(abs(delta))
    return {
        "label": label,
        "block_spills": next(iter(block_values)),
        "effect_tests": len(rows),
        "fdr_significant_directional_effects": len(fdr_passes),
        "practical_ci_passes": len(practical),
        "retained_effects": len(retained),
        "retained_effect_keys": ";".join(retained_keys),
        "max_directional_ci_fraction_of_mpe": max(directional_ci_fractions, default=math.nan),
        "minimum_fdr_q_value": min(q_values, default=math.nan),
        "n1_max_abs_median_paired_delta": max(n1_deltas, default=math.nan),
        "source": str(root),
    }


def validate_sensitivity_rows(rows: Sequence[Mapping[str, object]], n1_tolerance: float = 1e-10) -> None:
    if not rows:
        raise ValueError("no intensity block-sensitivity runs were supplied")
    effect_counts = {int(row["effect_tests"]) for row in rows}
    if len(effect_counts) != 1:
        raise ValueError(f"block-length runs have different effect-test counts: {sorted(effect_counts)}")
    signatures = {str(row.get("retained_effect_keys", "")) for row in rows}
    if len(signatures) != 1:
        raise ValueError("block-length runs reverse the exact retained-effect decision")
    bad_n1 = [
        row.get("label", "")
        for row in rows
        if not math.isfinite(number(row.get("n1_max_abs_median_paired_delta")))
        or number(row.get("n1_max_abs_median_paired_delta")) > n1_tolerance
    ]
    if bad_n1:
        raise ValueError(f"Best-1 zero-effect control failed for: {', '.join(str(value) for value in bad_n1)}")


def render(rows: Sequence[Mapping[str, object]], out: Path) -> None:
    x_values = [float(row["block_spills"]) for row in rows]
    poster.line_plot(
        out / "intensity_block_sensitivity_counts.png",
        "INTENSITY DECISION VS BLOCK LENGTH",
        [
            ("FDR DIRECTIONAL", list(zip(x_values, [float(row["fdr_significant_directional_effects"]) for row in rows])), poster.BLUE),
            ("PRACTICAL CI", list(zip(x_values, [float(row["practical_ci_passes"]) for row in rows])), poster.ORANGE),
            ("RETAINED", list(zip(x_values, [float(row["retained_effects"]) for row in rows])), poster.GREEN),
        ],
        x_label="BLOCK LENGTH (SPILLS)",
        y_label="EFFECT TEST COUNT",
        y_range=(0.0, max([float(row["fdr_significant_directional_effects"]) for row in rows] + [1.0]) * 1.1),
    )
    poster.line_plot(
        out / "intensity_block_sensitivity_practical_margin.png",
        "INTENSITY CI / PRACTICAL EFFECT",
        [
            ("MAX CI / MPE", list(zip(x_values, [number(row["max_directional_ci_fraction_of_mpe"]) for row in rows])), poster.BLUE),
            ("RETAIN AT 1.0", list(zip(x_values, [1.0 for _row in rows])), poster.RED),
        ],
        x_label="BLOCK LENGTH (SPILLS)",
        y_label="CI / MIN EFFECT",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="repeatable LABEL=PATH merged/re-summary root")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        rows = [summarize_run(*parse_run(value)) for value in args.run]
    except ValueError as exc:
        parser.error(str(exc))
    rows.sort(key=lambda row: (int(row["block_spills"]), str(row["label"])))
    try:
        validate_sensitivity_rows(rows)
    except ValueError as exc:
        parser.error(str(exc))
    out = Path(args.out).expanduser().resolve()
    ensure_dir(out)
    write_csv(out / "intensity_block_sensitivity.csv", rows, SUMMARY_FIELDS)
    render(rows, out)
    retained = sum(int(row["retained_effects"]) for row in rows)
    lines = [
        "# Intensity Block-Length Sensitivity",
        "",
        "The retain/reject decision is evaluated independently at each spill-block length. FDR significance without a confidence interval that clears the minimum practical effect is not sufficient for retention.",
        "",
        "| Block spills | Effect tests | FDR directional | Practical CI | Retained | Max directional CI / MPE |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['block_spills']} | {row['effect_tests']} | {row['fdr_significant_directional_effects']} | "
            f"{row['practical_ci_passes']} | {row['retained_effects']} | {number(row['max_directional_ci_fraction_of_mpe']):.4g} |"
        )
    lines.extend(
        [
            "",
            f"Total retained effects across sensitivity runs: `{retained}`.",
            f"Exact retained-effect decision is identical across block lengths: `true`.",
            "",
        ]
    )
    atomic_write_text(out / "intensity_block_sensitivity.md", "\n".join(lines))
    write_csv(
        out / "figure_manifest.csv",
        [
            {
                "path": "intensity_block_sensitivity_counts.png",
                "category": "block sensitivity",
                "description": "FDR-significant, practically meaningful, and retained intensity-effect counts versus spill-block length.",
                "claim_guardrail": "Statistical detectability alone is not a useful tune-estimator improvement.",
                "source": "intensity_block_sensitivity.csv",
            },
            {
                "path": "intensity_block_sensitivity_practical_margin.png",
                "category": "block sensitivity",
                "description": "Strongest directional confidence bound divided by its predeclared minimum practical effect.",
                "claim_guardrail": "The red line is the retention threshold; values below one do not clear practical significance.",
                "source": "intensity_block_sensitivity.csv",
            },
        ],
        ["path", "category", "description", "claim_guardrail", "source"],
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
