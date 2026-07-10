"""Exact channel identity helpers for Best-BPM subset artifacts."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


_CHANNEL_RE = re.compile(r":([HV]P\d+):")


def parse_indices(value: object) -> list[int]:
    out: list[int] = []
    for part in str(value or "").replace(";", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            idx = int(text)
        except ValueError:
            continue
        if idx >= 0 and idx not in out:
            out.append(idx)
    return out


def indices_from_mask(value: object, max_bits: int = 64) -> list[int]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        mask = int(text, 0)
    except ValueError:
        try:
            mask = int(Decimal(text))
        except (InvalidOperation, ValueError, OverflowError):
            return []
    if mask < 0:
        return []
    return [idx for idx in range(max_bits) if mask & (1 << idx)]


def channel_token(source_key: object) -> str:
    match = _CHANNEL_RE.search(str(source_key or ""))
    return match.group(1) if match else ""


def channel_label(meta: Mapping[str, object]) -> str:
    token = channel_token(meta.get("source_key"))
    if token:
        return token
    source_key = str(meta.get("source_key") or "").strip()
    if source_key:
        return source_key
    name = str(meta.get("bpm_name") or "").strip()
    if name:
        return name
    return str(meta.get("bpm_index") or "").strip()


def manifest_by_index(rows: Sequence[Mapping[str, object]]) -> dict[tuple[str, int], dict[str, str]]:
    out: dict[tuple[str, int], dict[str, str]] = {}
    for raw in rows:
        try:
            idx = int(str(raw.get("bpm_index", "")))
        except ValueError:
            continue
        plane = str(raw.get("plane", ""))
        normalized = {str(key): str(value) for key, value in raw.items()}
        token = channel_token(normalized.get("source_key"))
        if token:
            normalized["ring_order"] = token[2:]
        out[(plane, idx)] = normalized
    return out


def subset_indices(
    row: Mapping[str, object],
    plane: str,
    meta_by_index: Mapping[tuple[str, int], Mapping[str, object]],
) -> list[int]:
    explicit = parse_indices(row.get("bpm_indices"))
    if explicit:
        return explicit

    masked = indices_from_mask(row.get("subset_mask"))
    if masked:
        return masked

    source_keys = [part.strip() for part in str(row.get("bpm_source_keys") or "").split(",") if part.strip()]
    if source_keys:
        by_source = {
            str(meta.get("source_key") or ""): idx
            for (meta_plane, idx), meta in meta_by_index.items()
            if meta_plane == plane
        }
        resolved = [by_source[key] for key in source_keys if key in by_source]
        if resolved:
            return resolved

    members = [part.strip() for part in str(row.get("bpm_members") or "").split(",") if part.strip()]
    if not members:
        return []
    by_label: dict[str, list[int]] = {}
    for (meta_plane, idx), meta in meta_by_index.items():
        if meta_plane != plane:
            continue
        for candidate in (channel_label(meta), str(meta.get("source_key") or ""), str(meta.get("bpm_name") or "")):
            if candidate:
                by_label.setdefault(candidate, []).append(idx)
    resolved: list[int] = []
    for member in members:
        matches = by_label.get(member, [])
        if len(matches) == 1 and matches[0] not in resolved:
            resolved.append(matches[0])
    return resolved


def identity_fields(
    plane: str,
    indices: Sequence[int],
    meta_by_index: Mapping[tuple[str, int], Mapping[str, object]],
) -> dict[str, str]:
    ordered = [int(idx) for idx in indices]
    metas = [meta_by_index.get((plane, idx), {}) for idx in ordered]
    return {
        "bpm_indices": ",".join(str(idx) for idx in ordered),
        "bpm_members": ",".join(channel_label(meta) or str(idx) for idx, meta in zip(ordered, metas)),
        "bpm_source_keys": ",".join(str(meta.get("source_key") or "") for meta in metas),
        "bpm_digitizers": ",".join(str(meta.get("digitizer") or "") for meta in metas),
    }


def normalize_subset_row(
    row: Mapping[str, object],
    meta_by_index: Mapping[tuple[str, int], Mapping[str, object]],
) -> dict[str, str]:
    normalized = {str(key): str(value) for key, value in row.items()}
    plane = normalized.get("plane", "")
    indices = subset_indices(normalized, plane, meta_by_index)
    if indices:
        normalized.update(identity_fields(plane, indices, meta_by_index))
    return normalized
