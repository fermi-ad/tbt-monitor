"""Captured-spill bundle IO and manifest/integrity pass."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .schema import (
    BPM_INDEX_FIELDS,
    CHANNEL_FIELDS,
    CHANNEL_REJECTION_FLAGS,
    REJECTION_FIELDS,
    SPILLS_FIELDS,
    Channel,
    Spill,
)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as handle:
        handle.write(text)
        tmp = Path(handle.name)
    tmp.replace(path)


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(path.parent)) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
        tmp = Path(handle.name)
    tmp.replace(path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def discover_manifests(roots: Sequence[Path], limit: int = 0) -> list[Path]:
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
    manifests = sorted(found.values())
    return manifests[:limit] if limit else manifests


def _int_or_none(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_plane(item: dict[str, object]) -> str:
    plane = str(item.get("plane", "")).strip().upper()
    if plane in {"H", "V"}:
        return plane
    key = str(item.get("stream_key", ""))
    if ":HP" in key and "TBT_POSITION" in key:
        return "H"
    if ":VP" in key and "TBT_POSITION" in key:
        return "V"
    return ""


def parse_bpm_name(item: dict[str, object], plane: str) -> str:
    for key in ("bpm_name", "device_label", "device", "bpm_ip"):
        value = str(item.get(key, "")).strip()
        if value:
            return value
    stream_key = str(item.get("stream_key", ""))
    match = re.search(r":([HV]P\d+)", stream_key)
    if match:
        return match.group(1)
    return f"{plane}_{stream_key or 'unknown'}"


def parse_digitizer(item: dict[str, object], bpm_name: str) -> str:
    for key in ("digitizer", "bpm_ip"):
        value = str(item.get(key, "")).strip()
        if value:
            return value
    match = re.match(r"([HV]P\d+)", bpm_name)
    return match.group(1) if match else ""


def safe_payload_path(bundle_dir: Path, payload_file: object) -> Path | None:
    if not isinstance(payload_file, str) or not payload_file:
        return None
    path = Path(payload_file)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    return bundle_dir / path


def load_spill(manifest_path: Path) -> Spill:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_dir = manifest_path.parent
    collection = bundle_dir.parent.name
    timestamp = str(data.get("target_ms") or data.get("redis_timestamp_ms") or bundle_dir.name.replace("spill_", ""))
    channels: list[Channel] = []
    for item in data.get("streams", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("stream_key", ""))
        if "TBT_POSITION" not in key:
            continue
        plane = classify_plane(item)
        payload_path = safe_payload_path(bundle_dir, item.get("payload_file"))
        sample_count = _int_or_none(item.get("sample_count"))
        payload_bytes = _int_or_none(item.get("payload_bytes"))
        if sample_count is None and payload_bytes is not None:
            sample_count = payload_bytes // 4
        if not plane or payload_path is None or sample_count is None:
            continue
        bpm_name = parse_bpm_name(item, plane)
        channels.append(
            Channel(
                collection=collection,
                spill_id=bundle_dir.name,
                timestamp=timestamp,
                manifest_path=manifest_path,
                payload_path=payload_path,
                plane=plane,
                bpm_name=bpm_name,
                digitizer=parse_digitizer(item, bpm_name),
                source_key=key,
                sample_count=sample_count,
                payload_bytes=payload_bytes,
            )
        )
    return Spill(collection, bundle_dir.name, timestamp, bundle_dir, manifest_path, tuple(channels))


def load_waveform(channel: Channel, count: int | None = None) -> np.ndarray:
    if not channel.payload_path.exists():
        raise FileNotFoundError(str(channel.payload_path))
    expected = channel.sample_count if count is None else min(channel.sample_count, int(count))
    data = np.fromfile(channel.payload_path, dtype="<f4", count=expected)
    if data.size != expected:
        raise ValueError(f"decoded {data.size} samples, expected {expected}")
    return np.asarray(data, dtype=np.float32)


def robust_mad(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    med = float(np.median(values))
    return float(np.median(np.abs(values - med)) * 1.4826)


def channel_quality(channel: Channel, cfg: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    integrity = cfg.get("integrity", {}) if isinstance(cfg.get("integrity"), dict) else {}
    minimum_turns = int(integrity.get("minimum_turns", 2048))
    constant_epsilon = float(integrity.get("constant_epsilon", 1e-9))
    clip_fraction = float(integrity.get("clip_fraction", 0.02))
    extreme_rms = float(integrity.get("extreme_rms", 1_000_000.0))
    flags: list[str] = []
    detail = ""
    finite = False
    constant = False
    clipped = False
    rms = math.nan
    mad = math.nan
    turn_count = channel.sample_count
    if not channel.plane:
        flags.append("UNKNOWN_PLANE")
    if not channel.payload_path.exists():
        flags.append("MISSING")
    if channel.sample_count < minimum_turns:
        flags.append("TOO_SHORT")
    try:
        data = load_waveform(channel)
        finite = bool(np.all(np.isfinite(data)))
        if not finite:
            flags.append("NAN_INF")
        valid = data[np.isfinite(data)]
        if valid.size:
            rms = float(np.sqrt(np.mean(valid * valid)))
            mad = robust_mad(valid)
            constant = bool(np.nanmax(valid) - np.nanmin(valid) <= constant_epsilon)
            if constant:
                flags.append("CONSTANT")
            if rms > extreme_rms:
                flags.append("EXTREME_RMS")
            lo = np.percentile(valid, 0.1)
            hi = np.percentile(valid, 99.9)
            clipped = bool((np.mean(valid <= lo) > clip_fraction) or (np.mean(valid >= hi) > clip_fraction))
            if clipped and (hi - lo) <= constant_epsilon:
                flags.append("CLIPPED")
        else:
            flags.append("NAN_INF")
    except Exception as exc:
        flags.append("DECODE_FAILED")
        detail = repr(exc)
    flags = [flag for flag in CHANNEL_REJECTION_FLAGS if flag in set(flags)]
    usable = not flags
    row = {
        "collection": channel.collection,
        "spill_id": channel.spill_id,
        "plane": channel.plane,
        "bpm_name": channel.bpm_name,
        "digitizer": channel.digitizer,
        "source_key": channel.source_key,
        "payload_path": str(channel.payload_path),
        "turn_count": turn_count,
        "finite": str(finite).lower(),
        "constant": str(constant).lower(),
        "clipped": str(clipped).lower(),
        "rms": f"{rms:.9g}" if math.isfinite(rms) else "",
        "mad": f"{mad:.9g}" if math.isfinite(mad) else "",
        "usable": str(usable).lower(),
        "rejection_flags": "|".join(flags),
    }
    rejection_rows = []
    if flags:
        rejection_rows.append(
            {
                "collection": channel.collection,
                "spill_id": channel.spill_id,
                "plane": channel.plane,
                "bpm_name": channel.bpm_name,
                "source_key": channel.source_key,
                "payload_path": str(channel.payload_path),
                "rejection_flags": "|".join(flags),
                "detail": detail,
            }
        )
    return row, rejection_rows


def spill_channel_quality(args: tuple[Spill, dict[str, object], dict[tuple[str, str, str, str], int]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    spill, cfg, bpm_index = args
    channel_rows: list[dict[str, object]] = []
    rejection_rows: list[dict[str, object]] = []
    for ch in spill.channels:
        row, rejects = channel_quality(ch, cfg)
        row["bpm_index"] = bpm_index[(ch.plane, ch.bpm_name, ch.source_key, ch.digitizer)]
        channel_rows.append(row)
        rejection_rows.extend(rejects)
    return channel_rows, rejection_rows


def build_manifest_outputs(cfg: dict[str, object], out: Path, limit: int = 0) -> None:
    roots = [Path(item) for item in cfg["data"]["collections"]]
    spills = [load_spill(path) for path in discover_manifests(roots, limit=limit)]
    bpm_keys = sorted({(ch.plane, ch.bpm_name, ch.source_key, ch.digitizer) for spill in spills for ch in spill.channels})
    bpm_index: dict[tuple[str, str, str, str], int] = {}
    bpm_rows = []
    plane_counts: Counter[str] = Counter()
    for plane, bpm_name, source_key, digitizer in bpm_keys:
        idx = plane_counts[plane]
        plane_counts[plane] += 1
        bpm_index[(plane, bpm_name, source_key, digitizer)] = idx
        ring_match = re.search(r"(\d+)", bpm_name)
        bpm_rows.append(
            {
                "bpm_index": idx,
                "bpm_name": bpm_name,
                "plane": plane,
                "digitizer": digitizer,
                "ring_order": int(ring_match.group(1)) if ring_match else "",
                "source_key": source_key,
            }
        )
    channel_rows: list[dict[str, object]] = []
    rejection_rows: list[dict[str, object]] = []
    workers = max(1, int(cfg.get("runtime", {}).get("workers", 1)) if isinstance(cfg.get("runtime"), dict) else 1)
    tasks = [(spill, cfg, bpm_index) for spill in spills]
    if workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for rows, rejects in pool.map(spill_channel_quality, tasks, chunksize=1):
                channel_rows.extend(rows)
                rejection_rows.extend(rejects)
    else:
        for task in tasks:
            rows, rejects = spill_channel_quality(task)
            channel_rows.extend(rows)
            rejection_rows.extend(rejects)
    usable_by_spill_plane: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in channel_rows:
        if row["usable"] == "true":
            usable_by_spill_plane[(str(row["collection"]), str(row["spill_id"]), str(row["plane"]))].append(row)
    spill_rows = []
    for spill in spills:
        h = usable_by_spill_plane[(spill.collection, spill.spill_id, "H")]
        v = usable_by_spill_plane[(spill.collection, spill.spill_id, "V")]
        h_turns = Counter(int(row["turn_count"]) for row in h if str(row["turn_count"]).isdigit())
        v_turns = Counter(int(row["turn_count"]) for row in v if str(row["turn_count"]).isdigit())
        flags = sorted({flag for row in channel_rows if row["collection"] == spill.collection and row["spill_id"] == spill.spill_id for flag in str(row["rejection_flags"]).split("|") if flag})
        usable_h = len(h) >= 3
        usable_v = len(v) >= 3
        spill_rows.append(
            {
                "collection": spill.collection,
                "spill_id": spill.spill_id,
                "timestamp": spill.timestamp,
                "path": str(spill.path),
                "h_bpm_count": len(h),
                "v_bpm_count": len(v),
                "turn_count_h": h_turns.most_common(1)[0][0] if h_turns else "",
                "turn_count_v": v_turns.most_common(1)[0][0] if v_turns else "",
                "usable_h": str(usable_h).lower(),
                "usable_v": str(usable_v).lower(),
                "spill_usable": str(usable_h and usable_v).lower(),
                "rejection_flags": "|".join(flags),
            }
        )
    write_csv(out / "spills.csv", spill_rows, SPILLS_FIELDS)
    write_csv(out / "bpm_index.csv", bpm_rows, BPM_INDEX_FIELDS)
    write_csv(out / "channels.csv", channel_rows, CHANNEL_FIELDS)
    write_csv(out / "rejections.csv", rejection_rows, REJECTION_FIELDS)
    collections = Counter(row["collection"] for row in spill_rows)
    lines = [
        "# Best-BPM Dataset Summary",
        "",
        f"- spills discovered: `{len(spill_rows)}`",
        f"- usable H/V spills: `{sum(1 for row in spill_rows if row['spill_usable'] == 'true')}`",
        f"- BPM plane-channel index rows: `{len(bpm_rows)}`",
        f"- rejected channel rows: `{len(rejection_rows)}`",
        "",
        "| collection | spills |",
        "| --- | ---: |",
    ]
    for name, count in sorted(collections.items()):
        lines.append(f"| `{name}` | {count} |")
    atomic_write_text(out / "dataset_summary.md", "\n".join(lines) + "\n")


def rows_by_key(rows: Iterable[dict[str, str]], *fields: str) -> dict[tuple[str, ...], dict[str, str]]:
    return {tuple(row.get(field, "") for field in fields): row for row in rows}
