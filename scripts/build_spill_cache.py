#!/usr/bin/env python3
"""Build a lightweight spill metadata cache for autosweep jobs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Sequence


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def by_key(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    out = {}
    for row in rows:
        out[(row.get("collection", ""), row.get("spill_id", ""))] = row
    return out


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--health", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu", help="accepted for workflow symmetry; CPU is used")
    args = parser.parse_args(argv)

    manifest = read_csv(Path(args.manifest))
    health_rows = by_key(read_csv(Path(args.health))) if args.health else {}
    collections: dict[str, dict[str, object]] = {}
    spills = []
    for row in manifest:
        collection = row.get("collection", "")
        coll = collections.setdefault(
            collection,
            {
                "collection": collection,
                "tier": row.get("tier", ""),
                "source_root": row.get("source_root", ""),
                "spill_count": 0,
                "usable_spill_count": 0,
            },
        )
        coll["spill_count"] = int(coll["spill_count"]) + 1
        health = health_rows.get((collection, row.get("spill_id", "")), {})
        usable = health.get("usable_data_flag", "true" if row.get("reason", "") == "" else "false") == "true"
        if usable:
            coll["usable_spill_count"] = int(coll["usable_spill_count"]) + 1
        spills.append(
            {
                "collection": collection,
                "spill_id": row.get("spill_id", ""),
                "target_ms": row.get("target_ms", ""),
                "bundle_dir": row.get("bundle_dir", ""),
                "manifest_path": row.get("manifest_path", ""),
                "tier": row.get("tier", ""),
                "waveform_length": row.get("waveform_length", ""),
                "planes": row.get("available_planes", ""),
                "h_bpm_count": row.get("h_bpm_count", ""),
                "v_bpm_count": row.get("v_bpm_count", ""),
                "usable": usable,
                "reject_reason": health.get("reject_reason", row.get("reason", "")),
                "rms_median": health.get("rms_median", ""),
                "mad_median": health.get("mad_median", ""),
            }
        )

    by_collection: dict[str, list[str]] = defaultdict(list)
    for spill in spills:
        by_collection[str(spill["collection"])].append(str(spill["manifest_path"]))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = {
        "schema_version": 1,
        "manifest": str(Path(args.manifest)),
        "health": args.health,
        "collections": sorted(collections.values(), key=lambda item: str(item["collection"])),
        "spills": spills,
        "manifest_paths_by_collection": {key: sorted(value) for key, value in sorted(by_collection.items())},
    }
    (out / "spill_cache_index.json").write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out / 'spill_cache_index.json'}")


if __name__ == "__main__":
    main()
