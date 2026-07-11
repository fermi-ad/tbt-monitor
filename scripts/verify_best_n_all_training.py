#!/usr/bin/env python3
"""Verify a leakage-controlled Best-N all-training baseline output root."""

from __future__ import annotations

import argparse
from pathlib import Path

from bpm_mining.all_training import verify_outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = verify_outputs(args.root)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
