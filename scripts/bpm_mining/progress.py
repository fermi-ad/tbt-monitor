"""Shared progress helpers for long Best-BPM follow-up passes."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .io import atomic_write_text


def _payload(
    status: str,
    chunks_completed: int,
    chunks_total: int,
    rows_completed: int,
    rows_total: int,
    output_rows: int,
    started_unix: float,
    message: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    now = time.time()
    payload: dict[str, object] = {
        "status": status,
        "chunks_completed": chunks_completed,
        "chunks_total": chunks_total,
        "rows_completed": rows_completed,
        "rows_total": rows_total,
        "fraction_complete": rows_completed / max(1, rows_total),
        "output_rows": output_rows,
        "started_unix": started_unix,
        "updated_unix": now,
        "elapsed_seconds": now - started_unix,
        "message": message,
    }
    if extra:
        payload.update(extra)
    return payload


def write_parent_status(
    progress_dir: Path | None,
    status: str,
    chunks_completed: int,
    chunks_total: int,
    rows_completed: int,
    rows_total: int,
    output_rows: int,
    started_unix: float,
    message: str = "",
    extra: dict[str, object] | None = None,
) -> None:
    if progress_dir is None:
        return
    progress_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        progress_dir / "parent_status.json",
        json.dumps(
            _payload(
                status,
                chunks_completed,
                chunks_total,
                rows_completed,
                rows_total,
                output_rows,
                started_unix,
                message,
                extra,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def write_shard_status(
    progress_dir: Path | None,
    shard_id: int,
    total_shards: int,
    status: str,
    rows_completed: int,
    rows_total: int,
    output_rows: int,
    started_unix: float,
    message: str = "",
    extra: dict[str, object] | None = None,
) -> None:
    if progress_dir is None:
        return
    progress_dir.mkdir(parents=True, exist_ok=True)
    payload = _payload(
        status,
        1 if status == "complete" else 0,
        1,
        rows_completed,
        rows_total,
        output_rows,
        started_unix,
        message,
        extra,
    )
    payload["shard_id"] = shard_id
    payload["total_shards"] = total_shards
    atomic_write_text(progress_dir / f"shard_{shard_id:03d}.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def chunked(rows: list[dict[str, str]], chunk_size: int) -> list[list[dict[str, str]]]:
    size = max(1, int(chunk_size))
    return [rows[idx : idx + size] for idx in range(0, len(rows), size)]
