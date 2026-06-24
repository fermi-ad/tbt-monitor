#!/usr/bin/env python3
"""Build a CSV manifest for captured BPM spill collections."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Sequence


FIELDS = [
    "collection",
    "collection_view",
    "tier",
    "spill_id",
    "target_ms",
    "bundle_dir",
    "manifest_path",
    "source_root",
    "stream_count",
    "position_stream_count",
    "intensity_stream_count",
    "h_bpm_count",
    "v_bpm_count",
    "waveform_length",
    "available_planes",
    "capture_status",
    "requested_streams",
    "captured_streams",
    "missing_streams",
    "reason",
]


def discover_manifests(roots: Sequence[Path]) -> list[Path]:
    found: dict[str, Path] = {}
    for root in roots:
        if root.is_file() and root.name == "manifest.json":
            found[str(root.resolve())] = root
        elif root.is_dir() and (root / "manifest.json").exists():
            manifest = root / "manifest.json"
            found[str(manifest.resolve())] = manifest
        elif root.is_dir():
            for manifest in root.rglob("manifest.json"):
                found[str(manifest.resolve())] = manifest
    return sorted(found.values())


def stream_type(key: str) -> str:
    return key.split(":")[-1] if key else ""


def plane_for_stream(item: dict[str, object]) -> str:
    plane = str(item.get("plane", "")).strip().upper()
    if plane in {"H", "V"}:
        return plane
    key = str(item.get("stream_key", ""))
    if ":HP" in key:
        return "H"
    if ":VP" in key:
        return "V"
    return ""


def collection_tier(collection: str, intensity_streams: int) -> str:
    if "positiononly" in collection:
        return "TierA"
    if intensity_streams > 0:
        return "TierB"
    return "Legacy"


def status_from_manifest(data: dict[str, object]) -> tuple[str, int]:
    diag = data.get("capture_diagnostics")
    if isinstance(diag, dict):
        status = str(diag.get("status") or "")
        missing = int(diag.get("missing_streams") or 0)
        return status, missing
    requested = int(data.get("requested_streams") or 0)
    captured = int(data.get("captured_streams") or len(data.get("streams") or []))
    if requested and captured >= requested:
        return "Complete", 0
    missing = max(0, requested - captured) if requested else 0
    return "Partial" if requested else "Unknown", missing


def row_for_manifest(manifest: Path, source_root: Path) -> dict[str, object]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    streams = [item for item in data.get("streams", []) if isinstance(item, dict)]
    collection = manifest.parent.parent.name
    target_ms = data.get("target_ms") or data.get("redis_timestamp_ms") or manifest.parent.name.replace("spill_", "")
    counts = Counter()
    lengths = Counter()
    h_bpms: set[str] = set()
    v_bpms: set[str] = set()
    for item in streams:
        key = str(item.get("stream_key", ""))
        typ = stream_type(key)
        counts[typ] += 1
        sample_count = item.get("sample_count")
        if sample_count is None and item.get("payload_bytes") is not None:
            sample_count = int(item.get("payload_bytes") or 0) // 4
        if sample_count:
            lengths[int(sample_count)] += 1
        if "TBT_POSITION" in typ:
            plane = plane_for_stream(item)
            bpm = str(item.get("bpm_ip") or item.get("device_label") or key)
            if plane == "H":
                h_bpms.add(bpm)
            elif plane == "V":
                v_bpms.add(bpm)
    waveform_length = ""
    if lengths:
        waveform_length = max(lengths.items(), key=lambda kv: (kv[1], kv[0]))[0]
    status, missing = status_from_manifest(data)
    intensity_streams = sum(v for k, v in counts.items() if "TBT_INTENSITY" in k)
    available = "".join(plane for plane, present in (("H", h_bpms), ("V", v_bpms)) if present)
    reason = ""
    if status.lower() != "complete":
        reason = "capture_not_complete"
    elif not h_bpms or not v_bpms:
        reason = "missing_plane"
    return {
        "collection": collection,
        "collection_view": collection,
        "tier": collection_tier(collection, intensity_streams),
        "spill_id": manifest.parent.name,
        "target_ms": target_ms,
        "bundle_dir": str(manifest.parent),
        "manifest_path": str(manifest),
        "source_root": str(source_root),
        "stream_count": len(streams),
        "position_stream_count": sum(v for k, v in counts.items() if "TBT_POSITION" in k),
        "intensity_stream_count": intensity_streams,
        "h_bpm_count": len(h_bpms),
        "v_bpm_count": len(v_bpms),
        "waveform_length": waveform_length,
        "available_planes": available,
        "capture_status": status,
        "requested_streams": data.get("requested_streams", ""),
        "captured_streams": data.get("captured_streams", len(streams)),
        "missing_streams": missing,
        "reason": reason,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    collections = Counter(str(row["collection"]) for row in rows)
    tiers = Counter(str(row["tier"]) for row in rows)
    complete = sum(1 for row in rows if str(row["capture_status"]).lower() == "complete")
    lines = [
        "# Dataset Summary",
        "",
        f"- spills: `{len(rows)}`",
        f"- complete captures: `{complete}`",
        f"- collections: `{len(collections)}`",
        f"- tiers: `{', '.join(f'{k}:{v}' for k, v in sorted(tiers.items()))}`",
        "",
        "| collection | spills |",
        "|---|---:|",
    ]
    for name, count in sorted(collections.items()):
        lines.append(f"| `{name}` | {count} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", required=True, help="captured run roots or spill dirs")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)
    roots = [Path(item) for item in args.roots]
    manifests = discover_manifests(roots)
    if args.limit:
        manifests = manifests[: args.limit]
    rows = []
    for manifest in manifests:
        source = next((root for root in roots if root in manifest.parents or root == manifest), roots[0])
        rows.append(row_for_manifest(manifest, source))
    out = Path(args.out)
    write_csv(out / "dataset_manifest.csv", rows)
    write_summary(out / "dataset_summary.md", rows)
    print(f"wrote {len(rows)} rows to {out / 'dataset_manifest.csv'}")


if __name__ == "__main__":
    main()
