"""Spectral-cache construction for best-BPM mining."""

from __future__ import annotations

import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

import numpy as np

from .config import plane_band
from .gpu import Backend
from .io import atomic_write_text, ensure_dir, load_spill, load_waveform, read_csv, write_csv
from .preprocessing import hann
from .schema import SPECTRAL_CACHE_FIELDS


def tune_axis_for(window_turns: int, padding_factor: int, band: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    nfft = int(window_turns) * max(1, int(padding_factor))
    freqs = np.fft.rfftfreq(nfft, d=1.0).astype(np.float32)
    q = (1.0 - freqs).astype(np.float32)
    lo, hi = band
    mask = (q >= lo) & (q <= hi)
    return q[mask], np.flatnonzero(mask)


def window_starts(turn_count: int, spec: dict[str, object]) -> list[int]:
    start = int(spec.get("turn_start", 0))
    n = int(spec["window_turns"])
    end = int(spec.get("turn_end") or turn_count)
    end = min(end, turn_count)
    stride = spec.get("stride_turns")
    if not stride:
        return [start] if start + n <= turn_count else []
    starts = []
    current = start
    while current + n <= end:
        starts.append(current)
        current += int(stride)
    return starts


def compute_spectra(traces: np.ndarray, spec: dict[str, object], band: tuple[float, float], cfg: dict[str, object], device: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    backend = Backend(device)
    xp = backend.xp
    window_turns = int(spec["window_turns"])
    starts = window_starts(traces.shape[1], spec)
    if not starts:
        raise ValueError(f"no windows fit {spec['name']} into {traces.shape[1]} turns")
    padding = int(cfg["spectra"].get("nfft_padding_factor", 2))
    tune_axis, bin_indices = tune_axis_for(window_turns, padding, band)
    tap = hann(window_turns)
    norm = float(np.sum(tap * tap)) or 1.0
    out = np.empty((traces.shape[0], len(starts), len(tune_axis)), dtype=np.float32)
    x_traces = xp.asarray(traces, dtype=xp.float32)
    x_tap = xp.asarray(tap, dtype=xp.float32)
    x_bins = xp.asarray(bin_indices)
    detrend = str(spec.get("detrend") or cfg["spectra"].get("detrend", "none"))
    for widx, start in enumerate(starts):
        block = x_traces[:, start : start + window_turns]
        block = block - xp.mean(block, axis=1, keepdims=True)
        if detrend == "linear":
            x = xp.arange(window_turns, dtype=xp.float32)
            x = x - xp.mean(x)
            denom = xp.sum(x * x)
            slope = xp.sum(block * x[None, :], axis=1, keepdims=True) / xp.maximum(denom, 1e-12)
            block = block - slope * x[None, :]
        power = xp.abs(xp.fft.rfft(block * x_tap[None, :], n=window_turns * padding, axis=1)) ** 2 / norm
        out[:, widx, :] = backend.to_numpy(power[:, x_bins]).astype(np.float32)
    backend.synchronize()
    centers = np.asarray([start + window_turns / 2.0 for start in starts], dtype=np.float32)
    return out, tune_axis, centers


def _process_spill(args: tuple[dict[str, object], dict[str, str], list[dict[str, str]], Path, str, bool]) -> list[dict[str, object]]:
    cfg, spill_row, bpm_rows, out, device, resume = args
    rows: list[dict[str, object]] = []
    try:
        spill = load_spill(Path(spill_row["path"]) / "manifest.json")
        bpm_lookup = {(row["plane"], row["bpm_name"], row["source_key"]): int(row["bpm_index"]) for row in bpm_rows}
        for plane in ("H", "V"):
            channels = [ch for ch in spill.channels if ch.plane == plane]
            if not channels:
                continue
            target_len = max(set(ch.sample_count for ch in channels), key=[ch.sample_count for ch in channels].count)
            selected = [ch for ch in channels if ch.sample_count == target_len]
            labels = []
            arrays = []
            indices = []
            for ch in selected:
                try:
                    arrays.append(load_waveform(ch))
                    labels.append(ch.bpm_name)
                    indices.append(bpm_lookup[(ch.plane, ch.bpm_name, ch.source_key)])
                except Exception:
                    continue
            if len(arrays) < 1:
                continue
            traces = np.stack(arrays, axis=0).astype(np.float32)
            band = plane_band(cfg, plane)
            for spec in cfg["spectra"]["configs"]:
                name = str(spec["name"])
                base = out / "spectra" / spill.collection / spill.spill_id / plane
                spectra_path = base / f"{name}.npy"
                tune_path = base / f"{name}.tune_axis.npy"
                centers_path = base / f"{name}.window_centers.npy"
                bpms_path = base / f"{name}.bpm_indices.npy"
                if resume and spectra_path.exists() and tune_path.exists() and centers_path.exists() and bpms_path.exists():
                    arr = np.load(spectra_path, mmap_mode="r")
                    rows.append(_cache_row(spill, plane, spec, spectra_path, tune_path, centers_path, bpms_path, arr.shape, "ok", "resumed"))
                    continue
                try:
                    spectra, tune_axis, centers = compute_spectra(traces, spec, band, cfg, device)
                    dtype_name = str(cfg["spectra"].get("cache_dtype", "float16"))
                    stored = spectra.astype(np.float16 if dtype_name == "float16" else np.float32)
                    ensure_dir(base)
                    np.save(spectra_path, stored)
                    np.save(tune_path, tune_axis.astype(np.float32))
                    np.save(centers_path, centers.astype(np.float32))
                    np.save(bpms_path, np.asarray(indices, dtype=np.int32))
                    rows.append(_cache_row(spill, plane, spec, spectra_path, tune_path, centers_path, bpms_path, stored.shape, "ok", ""))
                except Exception as exc:
                    rows.append(_cache_row(spill, plane, spec, spectra_path, tune_path, centers_path, bpms_path, (0, 0, 0), "error", repr(exc)))
    except Exception:
        rows.append(
            {
                "collection": spill_row.get("collection", ""),
                "spill_id": spill_row.get("spill_id", ""),
                "status": "error",
                "message": traceback.format_exc(limit=3),
            }
        )
    return rows


def _cache_row(spill, plane, spec, spectra_path, tune_path, centers_path, bpms_path, shape, status, message):
    return {
        "collection": spill.collection,
        "spill_id": spill.spill_id,
        "plane": plane,
        "spectral_config": spec["name"],
        "spectra_path": str(spectra_path),
        "tune_axis_path": str(tune_path),
        "window_centers_path": str(centers_path),
        "bpm_indices_path": str(bpms_path),
        "n_valid_bpm": shape[0],
        "n_windows": shape[1],
        "n_tune_bins": shape[2],
        "window_turns": spec["window_turns"],
        "stride_turns": spec.get("stride_turns", ""),
        "turn_start": spec.get("turn_start", 0),
        "turn_end": spec.get("turn_end", ""),
        "dtype": "",
        "status": status,
        "message": message,
    }


def build_spectral_cache(cfg: dict[str, object], manifest_dir: Path, out: Path, device: str, workers: int, resume: bool, limit: int = 0) -> None:
    spills = [row for row in read_csv(manifest_dir / "spills.csv") if row.get("spill_usable") == "true"]
    if limit:
        spills = spills[:limit]
    bpm_rows = read_csv(manifest_dir / "bpm_index.csv")
    ensure_dir(out / "index")
    # A single-GPU Spark node should not spawn multiple independent CuPy
    # contexts for FFT cache construction. CPU cache construction can fan out.
    effective_workers = 1 if device == "cuda" else max(1, int(workers))
    tasks = [(cfg, row, bpm_rows, out, device, resume) for row in spills]
    rows: list[dict[str, object]] = []
    if effective_workers > 1:
        with ProcessPoolExecutor(max_workers=effective_workers) as pool:
            for future in as_completed(pool.submit(_process_spill, task) for task in tasks):
                rows.extend(future.result())
    else:
        for task in tasks:
            rows.extend(_process_spill(task))
    rows.sort(key=lambda r: (str(r.get("collection", "")), str(r.get("spill_id", "")), str(r.get("plane", "")), str(r.get("spectral_config", ""))))
    write_csv(out / "index" / "spectral_cache.csv", rows, SPECTRAL_CACHE_FIELDS)
    ok = sum(1 for row in rows if row.get("status") == "ok")
    atomic_write_text(out / "index" / "spectral_cache_summary.md", f"# Spectral Cache Summary\n\n- rows: `{len(rows)}`\n- ok: `{ok}`\n")
