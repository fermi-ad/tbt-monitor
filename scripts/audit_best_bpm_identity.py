#!/usr/bin/env python3
"""Audit exact BPM identity retention and ring-order provenance in a run root."""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from pathlib import Path

from bpm_mining.identity import channel_label, channel_token, indices_from_mask, manifest_by_index, parse_indices
from bpm_mining.io import atomic_write_text, read_csv, write_csv


FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "mask_member_count",
    "explicit_member_count",
    "explicit_mask_match",
    "legacy_resolved_count",
    "legacy_exact_retention",
    "ambiguous_legacy_member_count",
    "identity_flags",
]


def subset_result_paths(root: Path) -> list[tuple[int, Path]]:
    out = []
    for subset_size in (1, 3, 5, 10):
        candidates = (
            root / "subset_search" / f"best{subset_size}" / f"best{subset_size}_results.csv",
            root / "subset_search" / f"best{subset_size}_results.csv",
        )
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if path is not None:
            out.append((subset_size, path))
    return out


def audit(root: Path, out: Path) -> None:
    manifest_rows = read_csv(root / "manifest" / "bpm_index.csv")
    meta = manifest_by_index(manifest_rows)
    by_plane_label: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_plane_token: dict[tuple[str, str], list[int]] = defaultdict(list)
    raw_ring_mismatches = 0
    for row in manifest_rows:
        token = channel_token(row.get("source_key"))
        if token and str(row.get("ring_order")) != token[2:]:
            raw_ring_mismatches += 1
    ring_mismatches = 0
    for (plane, index), row in meta.items():
        by_plane_label[(plane, str(row.get("bpm_name") or ""))].append(index)
        by_plane_token[(plane, channel_label(row))].append(index)
        token = channel_token(row.get("source_key"))
        if token and str(row.get("ring_order")) != token[2:]:
            ring_mismatches += 1

    rows: list[dict[str, object]] = []
    for declared_size, path in subset_result_paths(root):
        for source in read_csv(path):
            plane = source.get("plane", "")
            exact = indices_from_mask(source.get("subset_mask"))
            explicit = parse_indices(source.get("bpm_indices"))
            legacy: list[int] = []
            ambiguous = 0
            for member in [value.strip() for value in source.get("bpm_members", "").split(",") if value.strip()]:
                token_matches = by_plane_token.get((plane, member), [])
                label_matches = by_plane_label.get((plane, member), [])
                matches = token_matches if token_matches else label_matches
                if len(matches) > 1:
                    ambiguous += 1
                if matches:
                    # This reproduces the historical last-write-wins name lookup.
                    legacy.append(matches[-1])
            retention = len(set(exact) & set(legacy)) / max(1, len(set(exact)))
            flags = []
            if len(exact) != declared_size:
                flags.append("MASK_COUNT_MISMATCH")
            if explicit and explicit != exact:
                flags.append("EXPLICIT_MASK_MISMATCH")
            if not explicit:
                flags.append("NO_EXPLICIT_INDICES")
            if ambiguous:
                flags.append("AMBIGUOUS_LEGACY_LABEL")
            rows.append(
                {
                    "collection": source.get("collection", ""),
                    "spill_id": source.get("spill_id", ""),
                    "plane": plane,
                    "subset_size": declared_size,
                    "mask_member_count": len(exact),
                    "explicit_member_count": len(explicit),
                    "explicit_mask_match": str(bool(explicit) and explicit == exact).lower(),
                    "legacy_resolved_count": len(set(legacy)),
                    "legacy_exact_retention": f"{retention:.9g}",
                    "ambiguous_legacy_member_count": ambiguous,
                    "identity_flags": "|".join(flags),
                }
            )
    write_csv(out / "identity_audit_rows.csv", rows, FIELDS)
    lines = [
        "# Best-BPM Identity Audit",
        "",
        f"- manifest rows: `{len(manifest_rows)}`",
        f"- duplicate plane/name labels: `{sum(1 for values in by_plane_label.values() if len(values) > 1)}`",
        f"- raw manifest ring-order mismatches: `{raw_ring_mismatches}`",
        f"- raw manifest distinct ring-order values: `{len({row.get('ring_order', '') for row in manifest_rows})}`",
        f"- ring-order mismatches after exact-token normalization: `{ring_mismatches}`",
        f"- subset rows audited: `{len(rows)}`",
        "",
        "| N | rows | mean legacy retention | zero retention | explicit/mask mismatch |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for subset_size in sorted({int(row["subset_size"]) for row in rows}):
        group = [row for row in rows if int(row["subset_size"]) == subset_size]
        retentions = [float(row["legacy_exact_retention"]) for row in group]
        lines.append(
            f"| {subset_size} | {len(group)} | {sum(retentions) / max(1, len(retentions)):.6f} | "
            f"{sum(1 for value in retentions if math.isclose(value, 0.0))} | "
            f"{sum(1 for row in group if 'EXPLICIT_MASK_MISMATCH' in str(row['identity_flags']))} |"
        )
    lines.extend(
        [
            "",
            "Legacy retention reproduces the historical last-write-wins lookup and is diagnostic only. The subset mask is authoritative for old rows; corrected rows must also carry matching explicit indices and source keys.",
        ]
    )
    atomic_write_text(out / "identity_audit_summary.md", "\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    audit(Path(args.inputs), Path(args.out))


if __name__ == "__main__":
    main()
