"""Held-out spectral support for Best-BPM finalist rows."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

import numpy as np

from .identity import identity_fields, manifest_by_index, subset_indices
from .io import atomic_write_text, read_csv, write_csv
from .progress import chunked, write_parent_status, write_shard_status


HELDOUT_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "subset_mask",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "aggregator",
    "source_rank",
    "q_hat",
    "heldout_candidate_fraction",
    "heldout_power_support",
    "heldout_prominence_at_qhat",
    "selected_power_support",
    "selected_prominence_at_qhat",
    "selected_vs_heldout_delta",
    "heldout_bpm_count",
    "selected_bpm_count",
    "quality_flags",
]

HELDOUT_SUMMARY_FIELDS = [
    "plane",
    "subset_size",
    "aggregator",
    "row_count",
    "median_heldout_candidate_fraction",
    "median_heldout_power_support",
    "median_selected_vs_heldout_delta",
]

_HELDOUT_WORKER_STATE: dict[str, object] = {}


def _f(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _fmt(value: float) -> str:
    return f"{value:.9g}" if math.isfinite(value) else ""


def median(values: Sequence[float]) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return math.nan
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def cache_lookup(cache_dir: Path, spectral_config: str) -> dict[tuple[str, str, str], dict[str, str]]:
    return {
        (row["collection"], row["spill_id"], row["plane"]): row
        for row in read_csv(cache_dir / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    }


def finalist_rows(root: Path, limit: int = 0) -> list[dict[str, str]]:
    path = root / "evolution" / "finalist_reevaluation.csv"
    rows = read_csv(path) if path.exists() else []
    rows = [dict(row, _row_index=str(idx)) for idx, row in enumerate(rows)]
    return rows[:limit] if limit else rows


def init_heldout_worker(cache_dir: str, manifest_dir: str, spectral_config: str, half_width: float) -> None:
    global _HELDOUT_WORKER_STATE
    _HELDOUT_WORKER_STATE = {
        "caches": cache_lookup(Path(cache_dir), spectral_config),
        "meta_by_index": manifest_by_index(read_csv(Path(manifest_dir) / "bpm_index.csv")),
        "half_width": float(half_width),
    }


def _support_for_q(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    q_hat: float,
    half_width: float,
) -> tuple[np.ndarray, np.ndarray]:
    if not math.isfinite(q_hat):
        n = spectra.shape[0]
        return np.full(n, math.nan, dtype=np.float32), np.full(n, math.nan, dtype=np.float32)
    qmask = np.abs(tune_axis - q_hat) <= half_width
    if not np.any(qmask):
        qmask[np.argmin(np.abs(tune_axis - q_hat))] = True
    bmask = np.abs(tune_axis - q_hat) <= max(half_width * 5.0, 0.01)
    bmask = bmask & ~qmask
    if not np.any(bmask):
        bmask = ~qmask
    selected = np.asarray(spectra[:, :, qmask], dtype=np.float32)
    background = np.asarray(spectra[:, :, bmask], dtype=np.float32)
    signal = np.max(selected, axis=2)
    bg_med = np.median(background, axis=2)
    ratio = signal / np.maximum(bg_med, 1e-24)
    log_bg = np.log10(background + 1e-24)
    log_signal = np.log10(signal + 1e-24)
    log_med = np.median(log_bg, axis=2)
    log_mad = np.median(np.abs(log_bg - log_med[:, :, None]), axis=2) * 1.4826
    prominence = (log_signal - log_med) / np.maximum(log_mad, 1e-9)
    return np.asarray(np.median(ratio, axis=1), dtype=np.float32), np.asarray(np.median(prominence, axis=1), dtype=np.float32)


def evaluate_group(
    cache: dict[str, str],
    rows: list[dict[str, str]],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
    half_width: float,
) -> list[dict[str, object]]:
    bpm_indices = np.load(cache["bpm_indices_path"])
    pos_by_index = {int(idx): pos for pos, idx in enumerate(bpm_indices)}
    spectra = np.load(cache["spectra_path"], mmap_mode="r")
    tune_axis = np.load(cache["tune_axis_path"])
    support_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    out: list[dict[str, object]] = []
    all_positions = set(range(len(bpm_indices)))
    for row in rows:
        q_hat = _f(row.get("q_hat"))
        q_idx = int(np.argmin(np.abs(tune_axis - q_hat))) if math.isfinite(q_hat) else -1
        if q_idx not in support_cache:
            support_cache[q_idx] = _support_for_q(spectra, tune_axis, q_hat, half_width)
        support, prominence = support_cache[q_idx]
        selected_indices = subset_indices(row, row["plane"], meta_by_index)
        selected_positions = {pos_by_index[idx] for idx in selected_indices if idx in pos_by_index}
        heldout_positions = sorted(all_positions - selected_positions)
        selected_positions_sorted = sorted(selected_positions)
        flags: list[str] = []
        if not selected_positions_sorted:
            flags.append("NO_SELECTED_BPM")
        if not heldout_positions:
            flags.append("NO_HELDOUT_BPM")
        expected_size = int(row.get("subset_size") or 0)
        if expected_size and len(selected_positions_sorted) != expected_size:
            flags.append("SELECTED_CHANNEL_COUNT_MISMATCH")
        held_support = support[heldout_positions] if heldout_positions else np.asarray([], dtype=np.float32)
        held_prom = prominence[heldout_positions] if heldout_positions else np.asarray([], dtype=np.float32)
        sel_support = support[selected_positions_sorted] if selected_positions_sorted else np.asarray([], dtype=np.float32)
        sel_prom = prominence[selected_positions_sorted] if selected_positions_sorted else np.asarray([], dtype=np.float32)
        held_power = median([float(v) for v in held_support])
        sel_power = median([float(v) for v in sel_support])
        candidate_fraction = float(np.mean(held_support >= 3.0)) if held_support.size else math.nan
        identities = identity_fields(row["plane"], [int(bpm_indices[pos]) for pos in selected_positions_sorted], meta_by_index)
        out.append(
            {
                "collection": row["collection"],
                "spill_id": row["spill_id"],
                "plane": row["plane"],
                "subset_size": row["subset_size"],
                "subset_mask": row["subset_mask"],
                **identities,
                "aggregator": row.get("aggregator", ""),
                "source_rank": row.get("source_rank", ""),
                "q_hat": row.get("q_hat", ""),
                "heldout_candidate_fraction": _fmt(candidate_fraction),
                "heldout_power_support": _fmt(held_power),
                "heldout_prominence_at_qhat": _fmt(median([float(v) for v in held_prom])),
                "selected_power_support": _fmt(sel_power),
                "selected_prominence_at_qhat": _fmt(median([float(v) for v in sel_prom])),
                "selected_vs_heldout_delta": _fmt(sel_power - held_power) if math.isfinite(sel_power) and math.isfinite(held_power) else "",
                "heldout_bpm_count": len(heldout_positions),
                "selected_bpm_count": len(selected_positions_sorted),
                "quality_flags": "|".join(flags),
                "_row_index": row.get("_row_index", ""),
            }
        )
    return out


def process_heldout_chunk(args: tuple[int, int, list[dict[str, str]], str | None]) -> tuple[int, int, list[dict[str, object]]]:
    shard_id, total_shards, rows, progress_dir = args
    progress_path = Path(progress_dir) if progress_dir else None
    started = time.time()
    write_shard_status(progress_path, shard_id, total_shards, "running", 0, len(rows), 0, started)
    out: list[dict[str, object]] = []
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["collection"], row["spill_id"], row["plane"])].append(row)
    try:
        done = 0
        for key in sorted(grouped):
            cache = _HELDOUT_WORKER_STATE["caches"].get(key)  # type: ignore[union-attr]
            if cache:
                out.extend(
                    evaluate_group(
                        cache,
                        grouped[key],
                        _HELDOUT_WORKER_STATE["meta_by_index"],  # type: ignore[arg-type]
                        float(_HELDOUT_WORKER_STATE["half_width"]),
                    )
                )
            done += len(grouped[key])
            write_shard_status(progress_path, shard_id, total_shards, "running", done, len(rows), len(out), started)
    except BaseException as exc:
        write_shard_status(progress_path, shard_id, total_shards, "failed", done, len(rows), len(out), started, repr(exc))
        raise
    write_shard_status(progress_path, shard_id, total_shards, "complete", len(rows), len(rows), len(out), started)
    return shard_id, len(rows), out


def summarize(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("plane", "")), str(row.get("subset_size", "")), str(row.get("aggregator", "")))].append(row)
    out: list[dict[str, object]] = []
    for (plane, subset_size, aggregator) in sorted(grouped):
        group = grouped[(plane, subset_size, aggregator)]
        out.append(
            {
                "plane": plane,
                "subset_size": subset_size,
                "aggregator": aggregator,
                "row_count": len(group),
                "median_heldout_candidate_fraction": _fmt(median([_f(row.get("heldout_candidate_fraction")) for row in group])),
                "median_heldout_power_support": _fmt(median([_f(row.get("heldout_power_support")) for row in group])),
                "median_selected_vs_heldout_delta": _fmt(median([_f(row.get("selected_vs_heldout_delta")) for row in group])),
            }
        )
    return out


def evaluate_heldout_support(
    cfg: dict[str, object],
    root: Path,
    out: Path,
    workers: int | None = None,
    limit: int = 0,
    spectral_config: str | None = None,
    tune_half_width: float = 0.0025,
) -> None:
    spectral_config = spectral_config or str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256"))
    rows = finalist_rows(root, limit)
    runtime = cfg.get("runtime", {})
    worker_count = max(1, int(workers if workers is not None else runtime.get("workers", 1) if isinstance(runtime, dict) else 1))
    worker_count = min(worker_count, len(rows) if rows else 1)
    chunk_size = max(1, int(cfg.get("heldout", {}).get("chunk_rows", 2048) if isinstance(cfg.get("heldout"), dict) else 2048))
    chunks = chunked(rows, chunk_size)
    progress_dir = out / "evolution" / "heldout_progress"
    started = time.time()
    write_parent_status(progress_dir, "running", 0, len(chunks), 0, len(rows), 0, started)
    output: list[dict[str, object]] = []
    if worker_count <= 1 or len(chunks) <= 1:
        init_heldout_worker(str(root / "cache"), str(root / "manifest"), spectral_config, tune_half_width)
        rows_done = 0
        for idx, chunk in enumerate(chunks):
            _, row_count, chunk_rows = process_heldout_chunk((idx, len(chunks), chunk, str(progress_dir)))
            rows_done += row_count
            output.extend(chunk_rows)
            write_parent_status(progress_dir, "running", idx + 1, len(chunks), rows_done, len(rows), len(output), started)
    else:
        results: dict[int, tuple[int, list[dict[str, object]]]] = {}
        with ProcessPoolExecutor(max_workers=worker_count, initializer=init_heldout_worker, initargs=(str(root / "cache"), str(root / "manifest"), spectral_config, tune_half_width)) as pool:
            tasks = [(idx, len(chunks), chunk, str(progress_dir)) for idx, chunk in enumerate(chunks)]
            rows_done = 0
            output_rows = 0
            completed = 0
            for future in as_completed(pool.submit(process_heldout_chunk, task) for task in tasks):
                shard_id, row_count, chunk_rows = future.result()
                results[shard_id] = (row_count, chunk_rows)
                rows_done += row_count
                output_rows += len(chunk_rows)
                completed += 1
                write_parent_status(progress_dir, "running", completed, len(chunks), rows_done, len(rows), output_rows, started)
        for idx in sorted(results):
            output.extend(results[idx][1])
    output.sort(key=lambda row: int(row.get("_row_index") or 0))
    summary_rows = summarize(output)
    write_csv(out / "evolution" / "finalist_heldout_spectral_support.csv", output, HELDOUT_FIELDS)
    write_csv(out / "evolution" / "heldout_spectral_support_summary.csv", summary_rows, HELDOUT_SUMMARY_FIELDS)
    atomic_write_text(
        out / "evolution" / "heldout_spectral_support_summary.md",
        "# Held-Out Spectral Support\n\n"
        f"- spectral config: `{spectral_config}`\n"
        f"- tune half-width: `{tune_half_width}`\n"
        f"- finalist rows read: `{len(rows)}`\n"
        f"- output rows: `{len(output)}`\n"
        f"- workers: `{worker_count}`\n"
        "- support is computed from non-selected BPM spectra at each finalist q_hat.\n",
    )
    write_parent_status(progress_dir, "complete", len(chunks), len(chunks), len(rows), len(rows), len(output), started)
