#!/usr/bin/env python3
"""Analyze whether paired raw BPM intensity improves ensemble tune extraction."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.config import load_config
from bpm_mining.intensity import analyze_intensity_capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--subset-sizes", default="1,3,5,10")
    parser.add_argument("--analysis-turns", type=int, default=50_000)
    parser.add_argument("--window-turns", type=int, default=4096)
    parser.add_argument("--stride-turns", type=int, default=512)
    parser.add_argument("--fit-windows", type=int, default=8, help="fit prefix; overlapping later windows are purged from inference")
    parser.add_argument("--beam-width", type=int, default=32)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--max-abs-intensity", type=float, default=1e12)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()
    analyze_intensity_capture(
        load_config(args.config),
        Path(args.capture_root),
        Path(args.out),
        device=args.device,
        subset_sizes=[int(value) for value in args.subset_sizes.split(",") if value.strip()],
        analysis_turns=args.analysis_turns,
        window_turns=args.window_turns,
        stride_turns=args.stride_turns,
        fit_windows=args.fit_windows,
        beam_width=args.beam_width,
        tune_half_width=args.tune_half_width,
        max_abs_intensity=args.max_abs_intensity,
        limit=args.limit,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    )


if __name__ == "__main__":
    main()
