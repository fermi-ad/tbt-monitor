#!/usr/bin/env python3
"""Recompute block-aware intensity statistics from an existing merged study."""

from __future__ import annotations

import argparse
import math
import shutil
from pathlib import Path

from bpm_mining.intensity import (
    CORRELATION_FIELDS,
    CORRELATION_SUMMARY_FIELDS,
    EFFECT_FIELDS,
    correlation_summary,
    method_effects,
)
from bpm_mining.io import atomic_write_text, ensure_dir, read_csv, write_csv


def finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, help="existing merged intensity-study directory")
    parser.add_argument("--out", required=True)
    parser.add_argument("--tune-tolerance", type=float, default=0.0025)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--permutation-samples", type=int, default=10_000)
    parser.add_argument("--bootstrap-block-spills", type=int, default=20)
    args = parser.parse_args()

    inputs = Path(args.inputs)
    out = Path(args.out)
    ensure_dir(out)
    spills = read_csv(inputs / "intensity_spill_metrics.csv")
    correlations = read_csv(inputs / "intensity_visibility_correlations.csv")
    integrity = read_csv(inputs / "intensity_payload_integrity.csv")
    errors = read_csv(inputs / "errors.csv")
    effects = method_effects(
        spills,
        args.tune_tolerance,
        args.bootstrap_samples,
        args.permutation_samples,
        args.bootstrap_block_spills,
    )
    correlation_summaries = correlation_summary(
        correlations,
        args.bootstrap_samples,
        args.bootstrap_block_spills,
    )

    for source in inputs.iterdir():
        if source.is_file() and source.name not in {
            "intensity_method_effects.csv",
            "intensity_visibility_correlation_summary.csv",
            "intensity_study_summary.md",
        }:
            shutil.copy2(source, out / source.name)
    write_csv(out / "intensity_method_effects.csv", effects, EFFECT_FIELDS)
    write_csv(out / "intensity_visibility_correlations.csv", correlations, CORRELATION_FIELDS)
    write_csv(
        out / "intensity_visibility_correlation_summary.csv",
        correlation_summaries,
        CORRELATION_SUMMARY_FIELDS,
    )

    statistically_beneficial = [
        row
        for row in effects
        if row.get("statistical_benefit_pass") == "true"
        and finite(row.get("fdr_q_value")) <= 0.05
    ]
    retained = [row for row in effects if row.get("retain_method_for_tune_analysis") == "true"]
    bad_within_range = sum(
        1
        for row in integrity
        if "INVALID_WITHIN_ANALYSIS_RANGE" in str(row.get("quality_flags", ""))
    )
    atomic_write_text(
        out / "intensity_study_summary.md",
        "# Intensity-Assisted Tune Study\n\n"
        f"- source merged directory: `{inputs}`\n"
        f"- paired intensity payload rows: `{len(integrity)}`\n"
        f"- payloads with invalid data inside the analysis range: `{bad_within_range}`\n"
        f"- spill-method summaries: `{len(spills)}`\n"
        f"- paired method-effect tests: `{len(effects)}`\n"
        f"- FDR-significant directional effects within tune tolerance: `{len(statistically_beneficial)}`\n"
        f"- effects also exceeding minimum practical thresholds: `{len(retained)}`\n"
        f"- moving-bootstrap/sign-flip block length: `{args.bootstrap_block_spills}` spills within collection\n"
        f"- analysis errors: `{len(errors)}`\n\n"
        "No waveform analysis was repeated. The existing paired spill summaries were re-evaluated with a moving-block bootstrap and block sign-flip permutation test within each collection. Retention also requires the median paired tune shift and at least 95% of spillwise shifts to remain within tolerance.\n",
    )
    print(f"OUT={out}")
    print(f"RETAINED_EFFECTS={len(retained)}")


if __name__ == "__main__":
    main()
