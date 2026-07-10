#!/usr/bin/env python3
"""Merge resumable Best-N Spark shard outputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.best_n import merge_best_n_shards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True, help="directory containing shard_*/ outputs")
    parser.add_argument("--out", required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--bootstrap-block-spills", type=int, default=20)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    args = parser.parse_args()
    merge_best_n_shards(
        Path(args.shards),
        Path(args.out),
        args.bootstrap_samples,
        args.tune_half_width,
        args.bootstrap_block_spills,
    )


if __name__ == "__main__":
    main()
