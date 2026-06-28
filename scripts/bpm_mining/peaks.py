"""Per-BPM peak extraction from cached spectra."""

from __future__ import annotations

import csv
import math
import shutil
import statistics
import tempfile
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from .config import expected_tune, plane_band
from .io import atomic_write_text, ensure_dir, read_csv, write_csv
from .schema import PER_BPM_INJECTION_FIELDS, PER_BPM_SUMMARY_FIELDS, PER_BPM_WINDOW_FIELDS


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def percentile(values: list[float], pct: float) -> float:
    vals = sorted(v for v in values if math.isfinite(v))
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def mad(values: list[float]) -> float:
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return math.nan
    med = statistics.median(vals)
    return statistics.median(abs(v - med) for v in vals) * 1.4826


def local_maxima(log_power: np.ndarray, min_distance: int, max_peaks: int) -> list[int]:
    if log_power.size < 3:
        return []
    candidates = [idx for idx in range(1, log_power.size - 1) if log_power[idx] >= log_power[idx - 1] and log_power[idx] >= log_power[idx + 1]]
    candidates.sort(key=lambda idx: float(log_power[idx]), reverse=True)
    selected: list[int] = []
    for idx in candidates:
        if all(abs(idx - prior) >= min_distance for prior in selected):
            selected.append(idx)
        if len(selected) >= max_peaks:
            break
    return selected


def peak_width(tune_axis: np.ndarray, log_power: np.ndarray, peak_idx: int) -> float:
    peak = float(log_power[peak_idx])
    floor = float(np.median(log_power))
    half = floor + 0.5 * (peak - floor)
    lo = peak_idx
    hi = peak_idx
    while lo > 0 and log_power[lo] >= half:
        lo -= 1
    while hi < log_power.size - 1 and log_power[hi] >= half:
        hi += 1
    return abs(float(tune_axis[hi]) - float(tune_axis[lo])) if hi > lo else 0.0


def entropy(power: np.ndarray) -> float:
    clean = np.asarray(power, dtype=np.float64)
    clean = np.where(np.isfinite(clean) & (clean > 0), clean, 0.0)
    total = float(np.sum(clean))
    if total <= 0:
        return math.nan
    p = clean / total
    safe_p = np.where(p > 0, p, 1.0)
    ent = -float(np.sum(np.where(p > 0, p * np.log(safe_p), 0.0)))
    return ent / math.log(max(2, p.size))


def extract_candidates(
    power: np.ndarray,
    tune_axis: np.ndarray,
    plane: str,
    cfg: dict[str, object],
) -> list[dict[str, object]]:
    peak_cfg = cfg.get("peak_finding", {})
    max_peaks = int(peak_cfg.get("max_peaks", 3))
    min_distance = int(peak_cfg.get("min_peak_distance_bins", 3))
    half_width = int(peak_cfg.get("local_background_half_width_bins", 15))
    exclude = int(peak_cfg.get("exclude_peak_half_width_bins", 3))
    eps = 1e-24
    log_power = np.log10(np.asarray(power, dtype=np.float64) + eps)
    band_med = float(np.median(log_power))
    band_sigma = float(np.median(np.abs(log_power - band_med)) * 1.4826)
    peaks = local_maxima(log_power, min_distance, max_peaks)
    all_peak_values = [float(power[idx]) for idx in peaks]
    rows = []
    band = plane_band(cfg, plane)
    anchor = expected_tune(cfg, plane)
    for rank, peak_idx in enumerate(peaks, start=1):
        lo = max(0, peak_idx - half_width)
        hi = min(log_power.size, peak_idx + half_width + 1)
        mask = np.ones(hi - lo, dtype=bool)
        ex_lo = max(0, peak_idx - exclude - lo)
        ex_hi = min(mask.size, peak_idx + exclude + 1 - lo)
        mask[ex_lo:ex_hi] = False
        background = log_power[lo:hi][mask]
        local_bg = float(np.median(background)) if background.size else band_med
        local_sigma = max(float(np.median(np.abs(background - local_bg)) * 1.4826) if background.size else band_sigma, 1e-9)
        peak_log = float(log_power[peak_idx])
        second = max((v for idx, v in enumerate(all_peak_values) if idx != rank - 1), default=0.0)
        peak_val = float(power[peak_idx])
        rows.append(
            {
                "candidate_rank": rank,
                "peak_tune": float(tune_axis[peak_idx]),
                "peak_power": peak_val,
                "peak_prominence_z": (peak_log - local_bg) / local_sigma,
                "peak_to_local_background": 10 ** (peak_log - local_bg),
                "peak_width_tune": peak_width(tune_axis, log_power, peak_idx),
                "second_peak_ratio": second / peak_val if peak_val > 0 else 1.0,
                "spectral_entropy": entropy(power),
                "distance_to_band_edge": min(abs(float(tune_axis[peak_idx]) - band[0]), abs(float(tune_axis[peak_idx]) - band[1])),
                "distance_to_expected_anchor": abs(float(tune_axis[peak_idx]) - anchor),
                "valid_candidate": True,
                "quality_flags": "",
            }
        )
    if not rows:
        rows.append(
            {
                "candidate_rank": 1,
                "peak_tune": "",
                "peak_power": "",
                "peak_prominence_z": "",
                "peak_to_local_background": "",
                "peak_width_tune": "",
                "second_peak_ratio": "",
                "spectral_entropy": entropy(power),
                "distance_to_band_edge": "",
                "distance_to_expected_anchor": "",
                "valid_candidate": False,
                "quality_flags": "NO_LOCAL_PEAK",
            }
        )
    return rows


def _write_cache_feature_rows(
    cache: dict[str, str],
    bpm_rows: dict[tuple[str, str], dict[str, str]],
    cfg: dict[str, object],
    window_writer: csv.DictWriter,
    injection_writer: csv.DictWriter,
    summary_state: dict[tuple[object, ...], dict[str, object]],
    injection_pairs: dict[tuple[object, ...], dict[str, float]],
) -> tuple[int, int]:
    window_count = 0
    injection_count = 0
    spectra = np.load(cache["spectra_path"], mmap_mode="r")
    tune_axis = np.load(cache["tune_axis_path"])
    centers = np.load(cache["window_centers_path"])
    bpm_indices = np.load(cache["bpm_indices_path"])
    for bpos, bpm_index in enumerate(bpm_indices):
        bpm = bpm_rows.get((cache["plane"], str(int(bpm_index))), {})
        for widx, center in enumerate(centers):
            for candidate in extract_candidates(np.asarray(spectra[bpos, widx], dtype=np.float32), tune_axis, cache["plane"], cfg):
                row = {
                    "collection": cache["collection"],
                    "spill_id": cache["spill_id"],
                    "plane": cache["plane"],
                    "bpm_index": int(bpm_index),
                    "bpm_name": bpm.get("bpm_name", ""),
                    "digitizer": bpm.get("digitizer", ""),
                    "spectral_config": cache["spectral_config"],
                    "window_index": widx,
                    "center_turn": float(center),
                    **candidate,
                }
                window_writer.writerow({field: row.get(field, "") for field in PER_BPM_WINDOW_FIELDS})
                window_count += 1
                add_summary_observation(summary_state, row)
                if cache["spectral_config"] in {"injection_2048", "injection_4096"}:
                    injection_row = {**row, "delta_q_2048_4096": "", "consistent_across_windows": ""}
                    injection_writer.writerow({field: injection_row.get(field, "") for field in PER_BPM_INJECTION_FIELDS})
                    injection_count += 1
                    if str(row.get("candidate_rank")) == "1" and str(row.get("valid_candidate")).lower() == "true":
                        q = _finite_float(row.get("peak_tune"))
                        if q is not None:
                            injection_pairs[(row["collection"], row["spill_id"], row["plane"], row["bpm_index"])][str(row["spectral_config"])] = q
    return window_count, injection_count


def _process_cache_feature_shard(
    args: tuple[int, dict[str, str], dict[tuple[str, str], dict[str, str]], dict[str, object], Path],
) -> dict[str, object]:
    index, cache, bpm_rows, cfg, shard_dir = args
    window_path = shard_dir / f"window_{index:06d}.csv"
    injection_path = shard_dir / f"injection_{index:06d}.csv"
    summary_state: dict[tuple[object, ...], dict[str, object]] = {}
    injection_pairs: dict[tuple[object, ...], dict[str, float]] = defaultdict(dict)
    try:
        with window_path.open("w", newline="") as window_handle, injection_path.open("w", newline="") as injection_handle:
            window_writer = csv.DictWriter(window_handle, fieldnames=PER_BPM_WINDOW_FIELDS, extrasaction="ignore")
            injection_writer = csv.DictWriter(injection_handle, fieldnames=PER_BPM_INJECTION_FIELDS, extrasaction="ignore")
            window_writer.writeheader()
            injection_writer.writeheader()
            window_count, injection_count = _write_cache_feature_rows(
                cache,
                bpm_rows,
                cfg,
                window_writer,
                injection_writer,
                summary_state,
                injection_pairs,
            )
    except Exception:
        return {"index": index, "status": "error", "message": traceback.format_exc(limit=5)}
    return {
        "index": index,
        "status": "ok",
        "window_path": str(window_path),
        "injection_path": str(injection_path),
        "window_count": window_count,
        "injection_count": injection_count,
        "summary_state": summary_state,
        "injection_pairs": dict(injection_pairs),
    }


def _merge_summary_state(
    target: dict[tuple[object, ...], dict[str, object]],
    source: dict[tuple[object, ...], dict[str, object]],
) -> None:
    for key, state in source.items():
        merged = target.setdefault(
            key,
            {
                "total": 0,
                "valid": 0,
                "tunes": [],
                "prominences": [],
                "widths": [],
                "centers": [],
            },
        )
        merged["total"] = int(merged["total"]) + int(state.get("total", 0))
        merged["valid"] = int(merged["valid"]) + int(state.get("valid", 0))
        for field in ("tunes", "prominences", "widths", "centers"):
            merged[field].extend(state.get(field, []))


def _merge_injection_pairs(
    target: dict[tuple[object, ...], dict[str, float]],
    source: dict[tuple[object, ...], dict[str, float]],
) -> None:
    for key, values in source.items():
        target[key].update(values)


def _concat_csv_shards(paths: list[Path], final_path: Path, fields: list[str]) -> None:
    ensure_dir(final_path.parent)
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(final_path.parent)) as handle:
        tmp = Path(handle.name)
        handle.write((",".join(fields) + "\n").encode("utf-8"))
        for path in paths:
            with path.open("rb") as source:
                source.readline()
                shutil.copyfileobj(source, handle, length=1024 * 1024)
    tmp.replace(final_path)


def extract_per_bpm_features(cfg: dict[str, object], cache_dir: Path, manifest_dir: Path, out: Path, workers: int | None = None) -> None:
    cache_rows = [row for row in read_csv(cache_dir / "index" / "spectral_cache.csv") if row.get("status") == "ok"]
    bpm_rows = {(row["plane"], row["bpm_index"]): row for row in read_csv(manifest_dir / "bpm_index.csv")}
    ensure_dir(out)
    runtime = cfg.get("runtime", {})
    worker_count = max(1, int(workers if workers is not None else runtime.get("workers", 1) if isinstance(runtime, dict) else 1))
    summary_state: dict[tuple[object, ...], dict[str, object]] = {}
    injection_pairs: dict[tuple[object, ...], dict[str, float]] = defaultdict(dict)
    if worker_count > 1 and len(cache_rows) > 1:
        shard_tmp = Path(tempfile.mkdtemp(prefix="feature-shards-", dir=str(out)))
        shard_results: list[dict[str, object]] = []
        window_count = 0
        injection_count = 0
        try:
            tasks = [(idx, cache, bpm_rows, cfg, shard_tmp) for idx, cache in enumerate(cache_rows)]
            with ProcessPoolExecutor(max_workers=worker_count) as pool:
                for future in as_completed(pool.submit(_process_cache_feature_shard, task) for task in tasks):
                    result = future.result()
                    if result.get("status") != "ok":
                        raise RuntimeError(f"per-BPM feature shard failed: {result.get('message', '')}")
                    shard_results.append(result)
                    window_count += int(result.get("window_count", 0))
                    injection_count += int(result.get("injection_count", 0))
            shard_results.sort(key=lambda row: int(row["index"]))
            for result in shard_results:
                _merge_summary_state(summary_state, result.get("summary_state", {}))
                _merge_injection_pairs(injection_pairs, result.get("injection_pairs", {}))
            _concat_csv_shards([Path(str(row["window_path"])) for row in shard_results], out / "per_bpm_window_features.csv", PER_BPM_WINDOW_FIELDS)
            raw_injection = out / "per_bpm_injection_features.raw.csv"
            _concat_csv_shards([Path(str(row["injection_path"])) for row in shard_results], raw_injection, PER_BPM_INJECTION_FIELDS)
            rewrite_injection_with_consistency(raw_injection, out / "per_bpm_injection_features.csv", injection_pairs)
            try:
                raw_injection.unlink()
            except Exception:
                pass
        finally:
            try:
                shutil.rmtree(shard_tmp)
            except Exception:
                pass
        summary_rows = summarize_per_bpm_state(summary_state)
        write_csv(out / "per_bpm_spill_summary.csv", summary_rows, PER_BPM_SUMMARY_FIELDS)
        atomic_write_text(out / "per_bpm_summary.md", f"# Per-BPM Feature Summary\n\n- window candidate rows: `{window_count}`\n- injection rows: `{injection_count}`\n- BPM spill summaries: `{len(summary_rows)}`\n- workers: `{worker_count}`\n")
        return
    tmp_window = tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(out))
    tmp_injection_raw = tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(out))
    window_tmp_path = Path(tmp_window.name)
    injection_raw_path = Path(tmp_injection_raw.name)
    window_count = 0
    injection_count = 0
    try:
        window_writer = csv.DictWriter(tmp_window, fieldnames=PER_BPM_WINDOW_FIELDS, extrasaction="ignore")
        injection_writer = csv.DictWriter(tmp_injection_raw, fieldnames=PER_BPM_INJECTION_FIELDS, extrasaction="ignore")
        window_writer.writeheader()
        injection_writer.writeheader()
        for cache in cache_rows:
            counts = _write_cache_feature_rows(cache, bpm_rows, cfg, window_writer, injection_writer, summary_state, injection_pairs)
            window_count += counts[0]
            injection_count += counts[1]
        tmp_window.close()
        tmp_injection_raw.close()
        window_tmp_path.replace(out / "per_bpm_window_features.csv")
        rewrite_injection_with_consistency(injection_raw_path, out / "per_bpm_injection_features.csv", injection_pairs)
    finally:
        for handle in (tmp_window, tmp_injection_raw):
            try:
                handle.close()
            except Exception:
                pass
        for path in (window_tmp_path, injection_raw_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
    summary_rows = summarize_per_bpm_state(summary_state)
    write_csv(out / "per_bpm_spill_summary.csv", summary_rows, PER_BPM_SUMMARY_FIELDS)
    atomic_write_text(out / "per_bpm_summary.md", f"# Per-BPM Feature Summary\n\n- window candidate rows: `{window_count}`\n- injection rows: `{injection_count}`\n- BPM spill summaries: `{len(summary_rows)}`\n- workers: `{worker_count}`\n")


def add_summary_observation(state_by_key: dict[tuple[object, ...], dict[str, object]], row: dict[str, object]) -> None:
    if str(row.get("candidate_rank")) != "1":
        return
    key = (row["collection"], row["spill_id"], row["plane"], row["bpm_index"], row["bpm_name"], row["digitizer"])
    state = state_by_key.setdefault(
        key,
        {
            "total": 0,
            "valid": 0,
            "tunes": [],
            "prominences": [],
            "widths": [],
            "centers": [],
        },
    )
    state["total"] = int(state["total"]) + 1
    if str(row.get("valid_candidate")).lower() != "true":
        return
    state["valid"] = int(state["valid"]) + 1
    for source, target in (
        ("peak_tune", "tunes"),
        ("peak_prominence_z", "prominences"),
        ("peak_width_tune", "widths"),
        ("center_turn", "centers"),
    ):
        value = _finite_float(row.get(source))
        if value is not None:
            state[target].append(value)


def rewrite_injection_with_consistency(raw_path: Path, final_path: Path, pairs: dict[tuple[object, ...], dict[str, float]]) -> None:
    ensure_dir(final_path.parent)
    with raw_path.open(newline="") as source, tempfile.NamedTemporaryFile("w", newline="", delete=False, dir=str(final_path.parent)) as handle:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(handle, fieldnames=PER_BPM_INJECTION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            pair = pairs.get((row["collection"], row["spill_id"], row["plane"], row["bpm_index"]), {})
            q2048 = pair.get("injection_2048")
            q4096 = pair.get("injection_4096")
            if q2048 is not None and q4096 is not None:
                delta = abs(q2048 - q4096)
                row["delta_q_2048_4096"] = f"{delta:.9g}"
                row["consistent_across_windows"] = str(delta <= 0.003).lower()
            writer.writerow({field: row.get(field, "") for field in PER_BPM_INJECTION_FIELDS})
        tmp = Path(handle.name)
    tmp.replace(final_path)


def summarize_per_bpm_state(state_by_key: dict[tuple[object, ...], dict[str, object]]) -> list[dict[str, object]]:
    output = []
    for key, state in state_by_key.items():
        total = int(state["total"])
        valid_count = int(state["valid"])
        tunes = list(state["tunes"])
        prominences = list(state["prominences"])
        widths = list(state["widths"])
        centers = list(state["centers"])
        steps = [abs(b - a) for a, b in zip(tunes, tunes[1:])]
        visible_fraction = valid_count / max(1, total)
        median_prom = statistics.median(prominences) if prominences else 0.0
        tune_mad = mad(tunes) if tunes else math.nan
        score = max(0.0, min(1.0, (median_prom / 12.0))) * visible_fraction * max(0.0, min(1.0, 1.0 - (0.0 if not math.isfinite(tune_mad) else tune_mad / 0.01)))
        output.append(
            {
                "collection": key[0],
                "spill_id": key[1],
                "plane": key[2],
                "bpm_index": key[3],
                "bpm_name": key[4],
                "digitizer": key[5],
                "valid_candidate_count": valid_count,
                "visible_window_fraction": f"{visible_fraction:.6f}",
                "first_visible_turn": min(centers) if centers else "",
                "last_visible_turn": max(centers) if centers else "",
                "visibility_duration_turns": (max(centers) - min(centers)) if len(centers) > 1 else 0,
                "median_peak_prominence_z": statistics.median(prominences) if prominences else "",
                "p10_peak_prominence_z": percentile(prominences, 0.10) if prominences else "",
                "median_peak_width": statistics.median(widths) if widths else "",
                "median_tune": statistics.median(tunes) if tunes else "",
                "tune_mad": tune_mad if math.isfinite(tune_mad) else "",
                "median_step": statistics.median(steps) if steps else "",
                "p95_step": percentile(steps, 0.95) if steps else "",
                "single_bpm_quality_score": f"{score:.6f}",
            }
        )
    output.sort(key=lambda r: (str(r["collection"]), str(r["spill_id"]), str(r["plane"]), int(r["bpm_index"])))
    return output
