#!/usr/bin/env python3
"""Merge intensity-study shards and compute paired statistical tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.intensity import merge_intensity_shards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tune-tolerance", type=float, default=0.0025)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--permutation-samples", type=int, default=10_000)
    parser.add_argument("--bootstrap-block-spills", type=int, default=20)
    args = parser.parse_args()
    merge_intensity_shards(
        Path(args.shards),
        Path(args.out),
        args.tune_tolerance,
        args.bootstrap_samples,
        args.permutation_samples,
        args.bootstrap_block_spills,
    )


if __name__ == "__main__":
    main()
