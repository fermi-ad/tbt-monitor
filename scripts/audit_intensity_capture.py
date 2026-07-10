#!/usr/bin/env python3
"""Inventory exact position/intensity pairs and capture completeness by spill."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from bpm_mining.identity import channel_token
from bpm_mining.io import atomic_write_text, discover_manifests, safe_payload_path, write_csv


INVENTORY_FIELDS = [
    "collection",
    "spill_id",
    "requested_streams",
    "captured_streams",
    "source_warning_count",
    "position_streams",
    "intensity_streams",
    "expected_channel_pairs",
    "exact_channel_pairs",
    "complete_payload_pairs",
    "stream_identity_matches",
    "stream_identity_mismatches",
    "missing_channel_pairs",
    "unmatched_position_streams",
    "unmatched_intensity_streams",
    "missing_payload_streams",
    "missing_channels",
    "quality_flags",
]

MISSING_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "channel",
    "digitizer",
    "position_source_key",
    "intensity_source_key",
    "position_present",
    "intensity_present",
    "position_payload_present",
    "intensity_payload_present",
    "stream_identity_match",
    "status",
]


@dataclass
class SpillCapture:
    collection: str
    spill_id: str
    requested_streams: int
    warnings: int
    bundle: Path
    streams: dict[str, dict[str, object]]


def pair_base(source_key: str) -> str:
    return source_key.replace(":TBT_POSITION_RAW", "").replace(":TBT_INTENSITY_RAW", "")


def paired_keys(base: str) -> tuple[str, str]:
    return f"{base}:TBT_POSITION_RAW", f"{base}:TBT_INTENSITY_RAW"


def payload_present(bundle: Path, stream: dict[str, object] | None) -> bool:
    if not stream:
        return False
    path = safe_payload_path(bundle, stream.get("payload_file"))
    return bool(path and path.is_file() and path.stat().st_size > 0)


def stream_identity_match(position: dict[str, object] | None, intensity: dict[str, object] | None) -> bool:
    if not position or not intensity:
        return False
    return (
        str(position.get("stream_id") or "") == str(intensity.get("stream_id") or "")
        and str(position.get("stream_ms") or "") == str(intensity.get("stream_ms") or "")
    )


def load_capture(manifest_path: Path) -> SpillCapture:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    streams = {
        str(item.get("stream_key")): item
        for item in data.get("streams", [])
        if isinstance(item, dict) and item.get("stream_key")
    }
    return SpillCapture(
        collection=manifest_path.parent.parent.name,
        spill_id=manifest_path.parent.name,
        requested_streams=int(data.get("requested_streams") or 0),
        warnings=len(data.get("warnings", [])) if isinstance(data.get("warnings", []), list) else 0,
        bundle=manifest_path.parent,
        streams=streams,
    )


def audit(capture_root: Path, out: Path) -> None:
    captures = [load_capture(path) for path in discover_manifests([capture_root])]
    expected_bases = sorted(
        {
            pair_base(key)
            for capture in captures
            for key in capture.streams
            if key.endswith((":TBT_POSITION_RAW", ":TBT_INTENSITY_RAW"))
        }
    )
    metadata: dict[str, tuple[str, str]] = {}
    for capture in captures:
        for key, stream in capture.streams.items():
            if not key.endswith((":TBT_POSITION_RAW", ":TBT_INTENSITY_RAW")):
                continue
            base = pair_base(key)
            token = channel_token(key) or base
            plane = str(stream.get("plane") or token[:1]).upper()
            digitizer = str(stream.get("bpm_ip") or stream.get("digitizer") or "")
            metadata.setdefault(base, (plane, digitizer))

    inventory: list[dict[str, object]] = []
    missing_rows: list[dict[str, object]] = []
    for capture in captures:
        position_keys = {key for key in capture.streams if key.endswith(":TBT_POSITION_RAW")}
        intensity_keys = {key for key in capture.streams if key.endswith(":TBT_INTENSITY_RAW")}
        exact_pairs = 0
        complete_pairs = 0
        identity_matches = 0
        missing_payloads = 0
        missing_channels: list[str] = []
        unmatched_positions = 0
        unmatched_intensities = 0
        for stream in capture.streams.values():
            if not payload_present(capture.bundle, stream):
                missing_payloads += 1
        for base in expected_bases:
            position_key, intensity_key = paired_keys(base)
            position = capture.streams.get(position_key)
            intensity = capture.streams.get(intensity_key)
            position_present = position is not None
            intensity_present = intensity is not None
            position_payload = payload_present(capture.bundle, position)
            intensity_payload = payload_present(capture.bundle, intensity)
            identity_match = stream_identity_match(position, intensity)
            if position_present and intensity_present:
                exact_pairs += 1
                if position_payload and intensity_payload:
                    complete_pairs += 1
                if identity_match:
                    identity_matches += 1
            if position_present and not intensity_present:
                unmatched_positions += 1
            if intensity_present and not position_present:
                unmatched_intensities += 1
            if position_present and intensity_present and position_payload and intensity_payload and identity_match:
                continue
            plane, digitizer = metadata.get(base, ("", ""))
            channel = channel_token(position_key) or channel_token(intensity_key) or base
            missing_channels.append(channel)
            if not position_present and not intensity_present:
                status = "PAIR_ABSENT"
            elif not position_present:
                status = "POSITION_STREAM_ABSENT"
            elif not intensity_present:
                status = "INTENSITY_STREAM_ABSENT"
            elif not position_payload or not intensity_payload:
                status = "PAYLOAD_ABSENT"
            else:
                status = "STREAM_IDENTITY_MISMATCH"
            missing_rows.append(
                {
                    "collection": capture.collection,
                    "spill_id": capture.spill_id,
                    "plane": plane,
                    "channel": channel,
                    "digitizer": digitizer,
                    "position_source_key": position_key,
                    "intensity_source_key": intensity_key,
                    "position_present": str(position_present).lower(),
                    "intensity_present": str(intensity_present).lower(),
                    "position_payload_present": str(position_payload).lower(),
                    "intensity_payload_present": str(intensity_payload).lower(),
                    "stream_identity_match": str(identity_match).lower(),
                    "status": status,
                }
            )
        flags: list[str] = []
        if capture.requested_streams and len(capture.streams) != capture.requested_streams:
            flags.append("SOURCE_CAPTURE_INCOMPLETE")
        if exact_pairs != len(expected_bases):
            flags.append("MISSING_EXACT_PAIR")
        if complete_pairs != exact_pairs:
            flags.append("MISSING_PAYLOAD")
        if identity_matches != exact_pairs:
            flags.append("STREAM_IDENTITY_MISMATCH")
        inventory.append(
            {
                "collection": capture.collection,
                "spill_id": capture.spill_id,
                "requested_streams": capture.requested_streams,
                "captured_streams": len(capture.streams),
                "source_warning_count": capture.warnings,
                "position_streams": len(position_keys),
                "intensity_streams": len(intensity_keys),
                "expected_channel_pairs": len(expected_bases),
                "exact_channel_pairs": exact_pairs,
                "complete_payload_pairs": complete_pairs,
                "stream_identity_matches": identity_matches,
                "stream_identity_mismatches": exact_pairs - identity_matches,
                "missing_channel_pairs": len(expected_bases) - exact_pairs,
                "unmatched_position_streams": unmatched_positions,
                "unmatched_intensity_streams": unmatched_intensities,
                "missing_payload_streams": missing_payloads,
                "missing_channels": ",".join(sorted(set(missing_channels))),
                "quality_flags": "|".join(flags),
            }
        )

    inventory.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"])))
    missing_rows.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"]), str(row["plane"]), str(row["channel"])))
    write_csv(out / "intensity_capture_inventory.csv", inventory, INVENTORY_FIELDS)
    write_csv(out / "intensity_capture_missing_pairs.csv", missing_rows, MISSING_FIELDS)
    complete = [row for row in inventory if not row["quality_flags"]]
    total_pairs = sum(int(row["complete_payload_pairs"]) for row in inventory)
    lines = [
        "# Intensity Capture Inventory",
        "",
        f"- spill manifests: `{len(inventory)}`",
        f"- expected exact channel pairs per complete spill: `{len(expected_bases)}`",
        f"- complete spill manifests: `{len(complete)}`",
        f"- incomplete spill manifests: `{len(inventory) - len(complete)}`",
        f"- complete position/intensity payload pairs: `{total_pairs}`",
        f"- missing or mismatched pair rows: `{len(missing_rows)}`",
        "",
    ]
    if missing_rows:
        lines.extend(
            [
                "| spill | channel | digitizer | status |",
                "| --- | --- | --- | --- |",
                *[
                    f"| {row['spill_id']} | {row['channel']} | {row['digitizer']} | {row['status']} |"
                    for row in missing_rows
                ],
                "",
            ]
        )
    lines.append("Only exact source-key pairs with present payload files and matching stream identity are eligible for the intensity-assisted tune analysis.")
    atomic_write_text(out / "intensity_capture_inventory.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    audit(Path(args.capture_root), Path(args.out))


if __name__ == "__main__":
    main()
