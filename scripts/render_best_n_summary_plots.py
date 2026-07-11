#!/usr/bin/env python3
"""Regenerate Best-N plots from an accepted summary without waveform analysis."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.best_n import write_plots
from bpm_mining.io import read_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    args = parser.parse_args()
    write_plots(read_csv(args.summary), args.out, args.tune_half_width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
