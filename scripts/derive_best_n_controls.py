#!/usr/bin/env python3
"""Derive cross-spill null and Best-1 membership controls from accepted Best-N rows."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.best_n import (
    CROSS_SPILL_NULL_BLOCK_SPILLS,
    CROSS_SPILL_NULL_DRAWS,
    write_best_n_controls,
)
from bpm_mining.io import read_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="merged Best-N output directory")
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--draws", type=int, default=CROSS_SPILL_NULL_DRAWS)
    parser.add_argument("--block-spills", type=int, default=CROSS_SPILL_NULL_BLOCK_SPILLS)
    args = parser.parse_args()

    root = Path(args.root)
    controls = write_best_n_controls(
        read_csv(root / "best_n_curve_rows.csv"),
        read_csv(root / "best_n_disjoint_validation.csv"),
        root,
        args.tune_half_width,
        args.draws,
        args.block_spills,
    )
    print(
        f"null_rows={len(controls['cross_spill_null'])} "
        f"membership_rows={len(controls['best1_membership_frequency'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
