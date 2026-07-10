#!/usr/bin/env python3
"""Verify full-buffer ridge-density coverage and publication artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.ridge_verification import verify_ridge_density_outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--subset-sizes", nargs="+", type=int, required=True)
    parser.add_argument("--minimum-spills", type=int, default=1900)
    parser.add_argument("--expected-centers", type=int, default=180)
    parser.add_argument("--allow-missing-context-variants", action="store_true")
    args = parser.parse_args()
    report = verify_ridge_density_outputs(
        Path(args.root),
        args.subset_sizes,
        args.minimum_spills,
        args.expected_centers,
        require_context_variants=not args.allow_missing_context_variants,
    )
    print(
        f"status={report['status']} errors={report['error_count']} "
        f"warnings={report['finding_warning_count']} figures={report['figure_count']}"
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
