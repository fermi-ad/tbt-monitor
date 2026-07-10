#!/usr/bin/env python3
"""Verify a merged Best-N run before it can support publication claims."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.best_n_verification import verify_best_n_outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--max-n", type=int, required=True)
    parser.add_argument("--curve-cache-rows", type=int, required=True)
    parser.add_argument("--validation-cache-rows", type=int, required=True)
    parser.add_argument("--folds", type=int, required=True)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--allow-single-collection", action="store_true")
    parser.add_argument("--allow-missing-plots", action="store_true")
    args = parser.parse_args()
    report = verify_best_n_outputs(
        Path(args.root),
        expected_max_n=args.max_n,
        expected_curve_cache_keys=args.curve_cache_rows,
        expected_validation_cache_keys=args.validation_cache_rows,
        expected_folds=args.folds,
        tune_half_width=args.tune_half_width,
        require_cross_collection=not args.allow_single_collection,
        require_plots=not args.allow_missing_plots,
    )
    print(
        f"status={report['status']} errors={report['error_count']} "
        f"warnings={report['warning_count']}"
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
