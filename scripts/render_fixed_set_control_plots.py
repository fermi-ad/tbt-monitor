#!/usr/bin/env python3
"""Render fixed/adaptive/all-BPM control plots from an accepted summary."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.fixed_sets import write_plots
from bpm_mining.io import read_csv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    write_plots(read_csv(args.summary), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
