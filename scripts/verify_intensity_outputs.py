#!/usr/bin/env python3
"""Verify intensity-study coverage, inference gates, N=1, and gallery assets."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.intensity_verification import verify_intensity_outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--gallery", required=True)
    parser.add_argument("--subset-sizes", nargs="+", type=int, required=True)
    parser.add_argument("--expected-paired-payload-rows", type=int, default=23999)
    parser.add_argument("--expected-spill-rows", type=int, default=12800)
    parser.add_argument("--expected-centers", type=int, default=90)
    parser.add_argument("--minimum-spills-per-group", type=int, default=199)
    parser.add_argument("--analysis-turns", type=int, default=50000)
    parser.add_argument("--tune-tolerance", type=float, default=0.0025)
    parser.add_argument("--expected-block-spills", type=int, default=20)
    args = parser.parse_args()
    report = verify_intensity_outputs(
        Path(args.root),
        Path(args.gallery),
        args.subset_sizes,
        args.expected_paired_payload_rows,
        args.expected_spill_rows,
        args.expected_centers,
        args.minimum_spills_per_group,
        analysis_turns=args.analysis_turns,
        tune_tolerance=args.tune_tolerance,
        expected_block_spills=args.expected_block_spills,
    )
    print(
        f"status={report['status']} errors={report['error_count']} "
        f"retained={report['retained_effects']} figures={report['figure_rows']}"
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
