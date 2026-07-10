#!/usr/bin/env python3
"""Repair historical whole-span visibility durations in completed subset rows."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from bpm_mining.config import config_hash, load_config
from bpm_mining.contracts import file_sha256
from bpm_mining.identity import parse_indices
from bpm_mining.io import atomic_write_text, read_csv, write_csv
from bpm_mining.schema import BEST_SUBSET_FIELDS
from bpm_mining.subset_score import subset_window_prominence, visibility_fraction_and_duration


AUDIT_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "visible_fraction",
    "old_visibility_duration_turns",
    "corrected_visibility_duration_turns",
]


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def result_path(root: Path, subset_size: int) -> Path:
    candidates = (
        root / "subset_search" / f"best{subset_size}" / f"best{subset_size}_results.csv",
        root / "subset_search" / f"best{subset_size}_results.csv",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise ValueError(f"missing Best-{subset_size} result table")
    return path


def repair_visibility_durations(
    cfg: dict[str, object],
    root: Path,
    audit_out: Path,
    subset_sizes: list[int],
) -> None:
    spectral_config = str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256"))
    max_windows = int(cfg.get("subset_search", {}).get("max_search_windows", 16))
    cache_rows = [
        row
        for row in read_csv(root / "cache" / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    ]
    cache_by_key = {
        (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", "")): row
        for row in cache_rows
    }
    if len(cache_by_key) != len(cache_rows):
        raise ValueError("spectral cache contains duplicate spill-plane keys")

    paths = {size: result_path(root, size) for size in subset_sizes}
    original_hashes = {str(path): file_sha256(path) for path in paths.values()}
    rows_by_size = {size: read_csv(path) for size, path in paths.items()}
    keyed_rows: dict[tuple[str, str, str], dict[int, dict[str, str]]] = {}
    for size, rows in rows_by_size.items():
        for row in rows:
            key = (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
            by_size = keyed_rows.setdefault(key, {})
            if size in by_size:
                raise ValueError(f"duplicate Best-{size} result key: {key}")
            by_size[size] = row

    expected_sizes = set(subset_sizes)
    if any(set(rows) != expected_sizes for rows in keyed_rows.values()):
        raise ValueError("subset result tables do not share exact spill-plane coverage")
    if set(keyed_rows) != set(cache_by_key):
        raise ValueError(
            f"subset/cache spill-plane coverage mismatch: subsets={len(keyed_rows)} cache={len(cache_by_key)}"
        )

    audit_rows: list[dict[str, object]] = []
    for key in sorted(keyed_rows):
        cache = cache_by_key[key]
        spectra = np.asarray(
            np.load(cache["spectra_path"], mmap_mode="r")[:, :max_windows, :],
            dtype=np.float32,
        )
        centers = np.asarray(
            np.load(cache["window_centers_path"])[: spectra.shape[1]],
            dtype=np.float32,
        )
        bpm_indices = np.asarray(np.load(cache["bpm_indices_path"]), dtype=np.int32)
        position_by_index = {int(value): index for index, value in enumerate(bpm_indices)}
        for size in subset_sizes:
            row = keyed_rows[key][size]
            selected_indices = parse_indices(row.get("bpm_indices"))
            if len(selected_indices) != size or any(index not in position_by_index for index in selected_indices):
                raise ValueError(f"Best-{size} row has invalid exact membership: {key}")
            selected_positions = [position_by_index[index] for index in selected_indices]
            prominence = subset_window_prominence(spectra[selected_positions])
            visible_fraction, duration = visibility_fraction_and_duration(prominence, centers)
            exported_fraction = _f(row.get("visible_fraction"))
            if not math.isfinite(exported_fraction) or abs(exported_fraction - visible_fraction) > 1e-6:
                raise ValueError(
                    f"Best-{size} visibility fraction does not reproduce for {key}: "
                    f"exported={exported_fraction} recomputed={visible_fraction}"
                )
            old_duration = row.get("visibility_duration_turns", "")
            row["visibility_duration_turns"] = _fmt(duration)
            audit_rows.append(
                {
                    "collection": key[0],
                    "spill_id": key[1],
                    "plane": key[2],
                    "subset_size": size,
                    "visible_fraction": _fmt(visible_fraction),
                    "old_visibility_duration_turns": old_duration,
                    "corrected_visibility_duration_turns": _fmt(duration),
                }
            )

    for size, path in paths.items():
        write_csv(path, rows_by_size[size], BEST_SUBSET_FIELDS)
    audit_out.mkdir(parents=True, exist_ok=True)
    write_csv(audit_out / "visibility_duration_repair.csv", audit_rows, AUDIT_FIELDS)
    changed = sum(
        row["old_visibility_duration_turns"] != row["corrected_visibility_duration_turns"]
        for row in audit_rows
    )
    payload = {
        "config_hash": config_hash(cfg),
        "spectral_config": spectral_config,
        "max_search_windows": max_windows,
        "subset_sizes": subset_sizes,
        "rows": len(audit_rows),
        "changed_rows": changed,
        "input_sha256": original_hashes,
        "output_sha256": {str(path): file_sha256(path) for path in paths.values()},
    }
    atomic_write_text(
        audit_out / "visibility_duration_repair.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    atomic_write_text(
        audit_out / "visibility_duration_repair.md",
        "# Visibility-Duration Repair\n\n"
        f"- rows verified: `{len(audit_rows)}`\n"
        f"- rows whose whole-span placeholder changed: `{changed}`\n"
        f"- spectral config: `{spectral_config}`\n"
        f"- fit windows: `{max_windows}`\n\n"
        "The subset score and selected membership are unchanged. Only the descriptive duration now spans the first through last window whose peak prominence is at least 4.0.\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--subset-sizes", nargs="+", type=int, default=[1, 3, 5])
    args = parser.parse_args()
    repair_visibility_durations(
        load_config(args.config),
        Path(args.root),
        Path(args.out),
        sorted(set(args.subset_sizes)),
    )


if __name__ == "__main__":
    main()
