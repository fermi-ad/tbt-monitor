#!/usr/bin/env python3
"""Evaluate all training channels under an accepted Best-N held-out protocol."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.all_training import evaluate
from bpm_mining.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--best-n-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    evaluate(
        load_config(args.config),
        args.inputs,
        args.best_n_root,
        args.out,
        progress_every=args.progress_every,
        resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
