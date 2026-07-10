#!/usr/bin/env python3
"""Run a Best-N beam sweep and disjoint-digitizer validation."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.best_n import evaluate_best_n
from bpm_mining.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--inputs", required=True, help="canonical Best-BPM run root with cache and manifest")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--spectral-config", default=None)
    parser.add_argument("--max-n", type=int, default=12)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--curve-limit", type=int, default=0, help="stratified cache-row limit; 0 uses all spill-plane rows")
    parser.add_argument("--validation-limit", type=int, default=500, help="stratified spill-plane rows for disjoint validation")
    parser.add_argument("--validation-beam-width", type=int, default=32)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--fold-seed", type=int, default=20260709)
    parser.add_argument("--fit-windows", type=int, default=8, help="prefix windows used to select ensemble members; overlapping later windows are purged")
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    parser.add_argument("--bootstrap-block-spills", type=int, default=20, help="moving-block length within each capture collection")
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    evaluate_best_n(
        load_config(args.config),
        Path(args.inputs),
        Path(args.out),
        device=args.device,
        spectral_config=args.spectral_config,
        max_n=args.max_n,
        beam_width=args.beam_width,
        curve_limit=args.curve_limit,
        validation_limit=args.validation_limit,
        validation_beam_width=args.validation_beam_width,
        folds=args.folds,
        fold_seed=args.fold_seed,
        requested_fit_windows=args.fit_windows,
        tune_half_width=args.tune_half_width,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_block_spills=args.bootstrap_block_spills,
        progress_every=args.progress_every,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
