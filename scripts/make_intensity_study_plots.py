#!/usr/bin/env python3
"""Generate the pure-PNG intensity-study review gallery."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.config import load_config
from bpm_mining.intensity_plots import make_intensity_gallery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    make_intensity_gallery(load_config(args.config), Path(args.inputs), Path(args.out))


if __name__ == "__main__":
    main()
