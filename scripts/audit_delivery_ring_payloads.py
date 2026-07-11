#!/usr/bin/env python3
"""Fail closed on corruption or threshold sentinels in raw publication payloads."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

import numpy as np

from bpm_mining.contracts import file_sha256, manifest_inventory_sha256
from bpm_mining.identity import channel_token
from bpm_mining.io import atomic_write_text, discover_manifests, safe_payload_path, write_csv
from bpm_mining.payload_integrity import device_fallback_values, longest_finite_exact_run, longest_true_run


ROW_FIELDS = (
    "collection",
    "spill_id",
    "plane",
    "channel",
    "digitizer",
    "manifest_path",
    "position_source_key",
    "intensity_source_key",
    "stream_identity_match",
    "position_advertised_samples",
    "position_payload_samples",
    "intensity_advertised_samples",
    "intensity_payload_samples",
    "analysis_samples",
    "position_finite_fraction",
    "intensity_finite_fraction",
    "position_longest_exact_run_start",
    "position_longest_exact_run_turns",
    "position_longest_exact_run_value",
    "paired_longest_exact_run_start",
    "paired_longest_exact_run_turns",
    "paired_longest_exact_run_position",
    "paired_longest_exact_run_intensity",
    "fallback_position_value",
    "fallback_intensity_value",
    "fallback_position_samples",
    "fallback_intensity_samples",
    "fallback_pair_samples",
    "fallback_pair_longest_run_start",
    "fallback_pair_longest_run_turns",
    "quality_flags",
)

MISSING_POSITION_FIELDS = (
    "collection",
    "spill_id",
    "manifest_path",
    "capture_status",
    "reported_missing_streams",
    "captured_position_streams",
    "expected_position_streams",
    "missing_position_stream_count",
    "missing_position_source_key",
    "plane",
    "channel",
    "digitizer",
    "warnings",
)


def _sample_count(stream: Mapping[str, object], path: Path | None) -> tuple[int, int]:
    advertised = int(stream.get("sample_count") or 0)
    if advertised <= 0:
        advertised = int(stream.get("payload_bytes") or 0) // 4
    payload = path.stat().st_size // 4 if path and path.is_file() else 0
    return advertised, payload


def _fmt(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.10g}"


def _load(path: Path, count: int) -> np.ndarray:
    return np.memmap(path, dtype="<f4", mode="r", shape=(count,))


def _stream_identity_match(position: Mapping[str, object], intensity: Mapping[str, object]) -> bool:
    return (
        str(position.get("stream_id") or "") == str(intensity.get("stream_id") or "")
        and str(position.get("stream_ms") or "") == str(intensity.get("stream_ms") or "")
    )


def audit_manifest(
    manifest_path: Path,
    analysis_turns: int,
    plateau_turns: int,
) -> tuple[list[dict[str, object]], set[tuple[str, str, str]]]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    streams = {
        str(item.get("stream_key")): item
        for item in data.get("streams", [])
        if isinstance(item, dict) and item.get("stream_key")
    }
    collection = manifest_path.parent.parent.name
    spill_id = manifest_path.parent.name
    rows: list[dict[str, object]] = []
    topology: set[tuple[str, str, str]] = set()
    for position_key in sorted(key for key in streams if key.endswith(":TBT_POSITION_RAW")):
        position = streams[position_key]
        channel = channel_token(position_key)
        plane = str(position.get("plane") or channel[:1]).upper()
        digitizer = str(position.get("bpm_ip") or position.get("digitizer") or "")
        topology.add((position_key, plane, digitizer))
        intensity_key = position_key.replace(":TBT_POSITION_RAW", ":TBT_INTENSITY_RAW")
        intensity = streams.get(intensity_key)
        position_path = safe_payload_path(manifest_path.parent, position.get("payload_file"))
        intensity_path = (
            safe_payload_path(manifest_path.parent, intensity.get("payload_file")) if intensity else None
        )
        position_advertised, position_payload = _sample_count(position, position_path)
        intensity_advertised, intensity_payload = _sample_count(intensity or {}, intensity_path)
        flags: list[str] = []
        if not position_path or not position_path.is_file():
            flags.append("MISSING_POSITION_PAYLOAD")
        if position_path and position_path.is_file() and position_path.stat().st_size % 4:
            flags.append("POSITION_PAYLOAD_BYTE_MISALIGNMENT")
        if position_advertised != position_payload:
            flags.append("POSITION_SAMPLE_COUNT_MISMATCH")
        if min(position_advertised, position_payload) < analysis_turns:
            flags.append("POSITION_SHORTER_THAN_ANALYSIS_RANGE")
        if intensity:
            if not intensity_path or not intensity_path.is_file():
                flags.append("MISSING_INTENSITY_PAYLOAD")
            if intensity_path and intensity_path.is_file() and intensity_path.stat().st_size % 4:
                flags.append("INTENSITY_PAYLOAD_BYTE_MISALIGNMENT")
            if intensity_advertised != intensity_payload:
                flags.append("INTENSITY_SAMPLE_COUNT_MISMATCH")
            if min(intensity_advertised, intensity_payload) < analysis_turns:
                flags.append("INTENSITY_SHORTER_THAN_ANALYSIS_RANGE")
            if not _stream_identity_match(position, intensity):
                flags.append("STREAM_IDENTITY_MISMATCH")

        available = min(position_advertised, position_payload, analysis_turns)
        if intensity:
            available = min(available, intensity_advertised, intensity_payload)
        position_values: np.ndarray | None = None
        intensity_values: np.ndarray | None = None
        position_finite_fraction = math.nan
        intensity_finite_fraction = math.nan
        position_run_start: int | None = None
        position_run_turns = 0
        position_run_values: tuple[float, ...] = ()
        paired_run_start: int | None = None
        paired_run_turns = 0
        paired_run_values: tuple[float, ...] = ()
        fallback_position = math.nan
        fallback_intensity = math.nan
        fallback_position_samples = 0
        fallback_intensity_samples = 0
        fallback_pair_samples = 0
        fallback_run_start: int | None = None
        fallback_run_turns = 0
        try:
            if available > 0 and position_path and position_path.is_file():
                position_values = _load(position_path, position_payload)[:available]
                position_finite_fraction = float(np.mean(np.isfinite(position_values)))
                position_run_start, position_run_turns, position_run_values = longest_finite_exact_run(
                    [position_values]
                )
                if position_finite_fraction < 1.0:
                    flags.append("NONFINITE_POSITION_WITHIN_ANALYSIS_RANGE")
                if position_run_turns >= plateau_turns:
                    flags.append("LONG_EXACT_POSITION_PLATEAU")
            if (
                available > 0
                and intensity
                and intensity_path
                and intensity_path.is_file()
                and position_values is not None
            ):
                intensity_values = _load(intensity_path, intensity_payload)[:available]
                intensity_finite_fraction = float(np.mean(np.isfinite(intensity_values)))
                paired_run_start, paired_run_turns, paired_run_values = longest_finite_exact_run(
                    [position_values, intensity_values]
                )
                if intensity_finite_fraction < 1.0:
                    flags.append("NONFINITE_INTENSITY_WITHIN_ANALYSIS_RANGE")
                if paired_run_turns >= plateau_turns:
                    flags.append("LONG_EXACT_PAIRED_PLATEAU")
                fallback = device_fallback_values(channel)
                if fallback:
                    fallback_position, fallback_intensity = fallback
                    position_match = position_values == np.float32(fallback_position)
                    intensity_match = intensity_values == np.float32(fallback_intensity)
                    pair_match = position_match & intensity_match
                    fallback_position_samples = int(np.count_nonzero(position_match))
                    fallback_intensity_samples = int(np.count_nonzero(intensity_match))
                    fallback_pair_samples = int(np.count_nonzero(pair_match))
                    fallback_run_start, fallback_run_turns = longest_true_run(pair_match)
                    if fallback_run_turns >= 4 or fallback_pair_samples >= 16:
                        flags.append("RAW_DEVICE_FALLBACK_PAIR")
        except (OSError, ValueError) as exc:
            flags.append(f"PAYLOAD_READ_ERROR:{type(exc).__name__}")

        rows.append(
            {
                "collection": collection,
                "spill_id": spill_id,
                "plane": plane,
                "channel": channel,
                "digitizer": digitizer,
                "manifest_path": str(manifest_path.resolve()),
                "position_source_key": position_key,
                "intensity_source_key": intensity_key if intensity else "",
                "stream_identity_match": str(_stream_identity_match(position, intensity)).lower()
                if intensity
                else "",
                "position_advertised_samples": position_advertised,
                "position_payload_samples": position_payload,
                "intensity_advertised_samples": intensity_advertised if intensity else "",
                "intensity_payload_samples": intensity_payload if intensity else "",
                "analysis_samples": available,
                "position_finite_fraction": _fmt(position_finite_fraction),
                "intensity_finite_fraction": _fmt(intensity_finite_fraction),
                "position_longest_exact_run_start": "" if position_run_start is None else position_run_start,
                "position_longest_exact_run_turns": position_run_turns,
                "position_longest_exact_run_value": _fmt(position_run_values[0]) if position_run_values else "",
                "paired_longest_exact_run_start": "" if paired_run_start is None else paired_run_start,
                "paired_longest_exact_run_turns": paired_run_turns,
                "paired_longest_exact_run_position": _fmt(paired_run_values[0]) if paired_run_values else "",
                "paired_longest_exact_run_intensity": _fmt(paired_run_values[1])
                if len(paired_run_values) > 1
                else "",
                "fallback_position_value": _fmt(fallback_position),
                "fallback_intensity_value": _fmt(fallback_intensity),
                "fallback_position_samples": fallback_position_samples,
                "fallback_intensity_samples": fallback_intensity_samples,
                "fallback_pair_samples": fallback_pair_samples,
                "fallback_pair_longest_run_start": "" if fallback_run_start is None else fallback_run_start,
                "fallback_pair_longest_run_turns": fallback_run_turns,
                "quality_flags": "|".join(flags),
            }
        )
    return rows, topology


def audit(
    capture_roots: Sequence[Path],
    out: Path,
    analysis_turns: int = 50_000,
    plateau_turns: int = 128,
    progress_every: int = 10,
) -> dict[str, object]:
    if analysis_turns <= 0:
        raise ValueError("analysis_turns must be positive")
    if plateau_turns < 2:
        raise ValueError("plateau_turns must be at least 2")
    if progress_every < 0:
        raise ValueError("progress_every cannot be negative")
    manifests = discover_manifests(capture_roots)
    if not manifests:
        raise ValueError("no capture manifests found")
    rows: list[dict[str, object]] = []
    topology_by_collection: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    manifest_topologies: list[tuple[Path, str, set[tuple[str, str, str]]]] = []
    manifest_counts: Counter[str] = Counter()
    position_counts: dict[str, list[int]] = defaultdict(list)
    for index, manifest in enumerate(manifests, start=1):
        manifest_rows, topology = audit_manifest(manifest, analysis_turns, plateau_turns)
        collection = manifest.parent.parent.name
        rows.extend(manifest_rows)
        topology_by_collection[collection].update(topology)
        manifest_topologies.append((manifest, collection, topology))
        manifest_counts[collection] += 1
        position_counts[collection].append(len(manifest_rows))
        if progress_every and (index % progress_every == 0 or index == len(manifests)):
            atomic_write_text(
                out / "progress.txt",
                f"manifests={index}/{len(manifests)} stream_rows={len(rows)}\n",
            )

    topology_reports: dict[str, dict[str, object]] = {}
    topology_errors: list[str] = []
    incomplete_manifests = 0
    for collection, topology in sorted(topology_by_collection.items()):
        planes = Counter(plane for _key, plane, _digitizer in topology)
        digitizers: dict[str, Counter[str]] = defaultdict(Counter)
        for _key, plane, digitizer in topology:
            digitizers[digitizer][plane] += 1
        bad_digitizers = sorted(
            digitizer
            for digitizer, counts in digitizers.items()
            if not digitizer or counts != Counter({"H": 2, "V": 2})
        )
        counts = position_counts[collection]
        incomplete = sum(value != 120 for value in counts)
        incomplete_manifests += incomplete
        topology_reports[collection] = {
            "manifests": manifest_counts[collection],
            "unique_position_streams": len(topology),
            "unique_h_streams": planes["H"],
            "unique_v_streams": planes["V"],
            "unique_digitizers": len(digitizers),
            "bad_digitizers": bad_digitizers,
            "position_streams_per_manifest_min": min(counts),
            "position_streams_per_manifest_median": median(counts),
            "position_streams_per_manifest_max": max(counts),
            "incomplete_manifests": incomplete,
        }
        if len(topology) != 120 or planes != Counter({"H": 60, "V": 60}) or len(digitizers) != 30:
            topology_errors.append(collection)
        if bad_digitizers:
            topology_errors.append(collection)

    missing_position_rows: list[dict[str, object]] = []
    for manifest, collection, topology in manifest_topologies:
        expected_topology = topology_by_collection[collection]
        missing = sorted(expected_topology - topology)
        if not missing:
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        diagnostics = data.get("capture_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
        warnings = data.get("warnings")
        warnings = warnings if isinstance(warnings, list) else []
        for source_key, plane, digitizer in missing:
            missing_position_rows.append(
                {
                    "collection": collection,
                    "spill_id": manifest.parent.name,
                    "manifest_path": str(manifest.resolve()),
                    "capture_status": str(diagnostics.get("status") or ""),
                    "reported_missing_streams": int(diagnostics.get("missing_streams") or 0),
                    "captured_position_streams": len(topology),
                    "expected_position_streams": len(expected_topology),
                    "missing_position_stream_count": len(missing),
                    "missing_position_source_key": source_key,
                    "plane": plane,
                    "channel": channel_token(source_key),
                    "digitizer": digitizer,
                    "warnings": " | ".join(map(str, warnings)),
                }
            )

    flagged = [row for row in rows if row["quality_flags"]]
    flag_counts: Counter[str] = Counter()
    for row in flagged:
        flag_counts.update(str(row["quality_flags"]).split("|"))
    report = {
        "schema": "tbt-monitor.delivery-ring-payload-audit/v1",
        "status": "pass" if not flagged and not topology_errors else "fail",
        "capture_roots": [str(path.resolve()) for path in capture_roots],
        "manifest_inventory_sha256": manifest_inventory_sha256(manifests),
        "analysis_turns": analysis_turns,
        "plateau_turns": plateau_turns,
        "manifest_count": len(manifests),
        "stream_rows": len(rows),
        "paired_stream_rows": sum(bool(row["intensity_source_key"]) for row in rows),
        "incomplete_manifests": incomplete_manifests,
        "missing_position_stream_rows": len(missing_position_rows),
        "flagged_rows": len(flagged),
        "position_plateau_rows": flag_counts["LONG_EXACT_POSITION_PLATEAU"],
        "paired_plateau_rows": flag_counts["LONG_EXACT_PAIRED_PLATEAU"],
        "raw_device_fallback_pair_rows": flag_counts["RAW_DEVICE_FALLBACK_PAIR"],
        "error_count": len(flagged) + len(set(topology_errors)),
        "warning_count": incomplete_manifests,
        "flag_counts": dict(sorted(flag_counts.items())),
        "topology": topology_reports,
    }
    rows.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"]), str(row["plane"]), str(row["channel"])))
    missing_position_rows.sort(
        key=lambda row: (
            str(row["collection"]),
            str(row["spill_id"]),
            str(row["plane"]),
            str(row["channel"]),
        )
    )
    write_csv(out / "delivery_ring_payload_rows.csv", rows, ROW_FIELDS)
    missing_path = out / "missing_position_streams.csv"
    write_csv(missing_path, missing_position_rows, MISSING_POSITION_FIELDS)
    report["missing_position_stream_inventory_sha256"] = file_sha256(missing_path)
    atomic_write_text(out / "delivery_ring_payload_audit.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Delivery Ring Raw Payload Audit",
        "",
        f"Status: **{report['status'].upper()}**",
        "",
        f"- manifests: `{report['manifest_count']}`",
        f"- raw position stream rows: `{report['stream_rows']}`",
        f"- exact raw position/intensity pairs: `{report['paired_stream_rows']}`",
        f"- analysis range: first `{analysis_turns}` turns",
        f"- exact-plateau threshold: `{plateau_turns}` turns",
        f"- flagged rows: `{report['flagged_rows']}`",
        f"- raw device-coded fallback pair rows: `{report['raw_device_fallback_pair_rows']}`",
        f"- incomplete 120-channel manifests: `{incomplete_manifests}` (reported, not silently dropped)",
        f"- manifest-level absent position streams: `{len(missing_position_rows)}` (enumerated in `missing_position_streams.csv`)",
        "",
        "The scan treats any nonfinite first-50k sample, count mismatch, long exact raw plateau, or repeated device-coded position/intensity fallback pair as a publication-blocking error. A missing channel manifest is reported separately because the analysis contracts define the accepted spill and pair counts.",
        "",
    ]
    atomic_write_text(out / "delivery_ring_payload_audit.md", "\n".join(lines))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--analysis-turns", type=int, default=50_000)
    parser.add_argument("--plateau-turns", type=int, default=128)
    parser.add_argument("--progress-every", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        report = audit(
            [path.resolve() for path in args.capture_root],
            args.out.resolve(),
            analysis_turns=args.analysis_turns,
            plateau_turns=args.plateau_turns,
            progress_every=args.progress_every,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"status": report["status"], "stream_rows": report["stream_rows"]}, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
