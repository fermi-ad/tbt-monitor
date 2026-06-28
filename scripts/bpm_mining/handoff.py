"""BPM tune-visibility handoff analysis."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

import numpy as np

from .io import atomic_write_text, read_csv, write_csv
from .progress import chunked, write_parent_status, write_shard_status


WINDOW_VISIBILITY_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "spectral_config",
    "window_index",
    "center_turn",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "consensus_tune",
    "consensus_label",
    "peak_tune",
    "peak_prominence_z",
    "power_at_consensus",
    "local_background_at_consensus",
    "support_at_consensus",
    "second_peak_ratio",
    "spectral_entropy",
    "visibility_score",
    "visibility_class",
    "is_top1_visible",
    "is_top3_visible",
    "is_top5_visible",
    "quality_flags",
]

HANDOFF_EVENT_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "window_index",
    "center_turn",
    "previous_members",
    "current_members",
    "jaccard_vs_previous",
    "handoff_score",
    "handoff_persistence",
    "consensus_tune",
    "consensus_delta",
    "event_label",
    "quality_flags",
]

VISIBILITY_SUMMARY_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "visible_window_fraction",
    "first_visible_turn",
    "last_visible_turn",
    "visibility_duration_turns",
    "median_visibility_score",
    "median_support_at_consensus",
    "top1_window_fraction",
    "top3_window_fraction",
    "top5_window_fraction",
    "handoff_event_count",
]

_HANDOFF_WORKER_STATE: dict[str, object] = {}


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


def selected_keys(root: Path, limit: int = 0) -> set[tuple[str, str, str]]:
    path = root / "artifact_selection" / "artifact_manifest.csv"
    rows = read_csv(path) if path.exists() else []
    keys = sorted({(row["collection"], row["spill_id"], row["plane"]) for row in rows})
    if limit:
        keys = keys[:limit]
    return set(keys)


def selected_cache_rows(root: Path, spectral_config: str, limit: int = 0) -> list[dict[str, str]]:
    keys = selected_keys(root, limit)
    rows = [
        row
        for row in read_csv(root / "cache" / "index" / "spectral_cache.csv")
        if row.get("status") == "ok"
        and row.get("spectral_config") == spectral_config
        and (row["collection"], row["spill_id"], row["plane"]) in keys
    ]
    rows.sort(key=lambda row: (row["collection"], row["spill_id"], row["plane"]))
    return rows


def bpm_meta(manifest_dir: Path) -> dict[tuple[str, int], dict[str, str]]:
    return {(row["plane"], int(row["bpm_index"])): row for row in read_csv(manifest_dir / "bpm_index.csv")}


def consensus_maps(root: Path) -> tuple[dict[tuple[str, str, str, str, str], dict[str, str]], dict[tuple[str, str, str], dict[str, str]]]:
    windows = {}
    path = root / "consensus" / "spill_consensus_windows.csv"
    if path.exists():
        for row in read_csv(path):
            windows[(row["collection"], row["spill_id"], row["plane"], row["spectral_config"], row["window_index"])] = row
    summary = {}
    spath = root / "consensus" / "spill_consensus_summary.csv"
    if spath.exists():
        summary = {(row["collection"], row["spill_id"], row["plane"]): row for row in read_csv(spath)}
    return windows, summary


def init_handoff_worker(root: str, spectral_config: str, thresholds_json: str) -> None:
    global _HANDOFF_WORKER_STATE
    root_path = Path(root)
    windows, summary = consensus_maps(root_path)
    _HANDOFF_WORKER_STATE = {
        "spectral_config": spectral_config,
        "bpm_meta": bpm_meta(root_path / "manifest"),
        "consensus_windows": windows,
        "consensus_summary": summary,
        "thresholds": json.loads(thresholds_json),
    }


def _entropy(power: np.ndarray) -> float:
    vals = np.asarray(power, dtype=np.float64)
    total = float(np.sum(vals))
    if total <= 0.0 or not math.isfinite(total):
        return math.nan
    p = vals / total
    p = p[p > 0]
    return float(-np.sum(p * np.log(p)) / max(1e-12, math.log(vals.size)))


def _local_metrics(power: np.ndarray, tune_axis: np.ndarray, q: float) -> tuple[float, float, float]:
    if not math.isfinite(q):
        return math.nan, math.nan, math.nan
    idx = int(np.argmin(np.abs(tune_axis - q)))
    signal = float(power[idx])
    lo = max(0, idx - 15)
    hi = min(power.size, idx + 16)
    mask = np.ones(hi - lo, dtype=bool)
    core_lo = max(0, idx - lo - 2)
    core_hi = min(mask.size, idx - lo + 3)
    mask[core_lo:core_hi] = False
    local = np.asarray(power[lo:hi][mask], dtype=np.float64)
    if local.size == 0:
        local = np.asarray(power, dtype=np.float64)
    bg = float(np.median(local))
    ratio = signal / max(bg, 1e-24)
    return signal, bg, ratio


def _peak_metrics(power: np.ndarray, tune_axis: np.ndarray) -> tuple[float, float, float, float]:
    log_power = np.log10(np.asarray(power, dtype=np.float64) + 1e-24)
    idx = int(np.argmax(log_power))
    peak = float(tune_axis[idx])
    med = float(np.median(log_power))
    mad = float(np.median(np.abs(log_power - med)) * 1.4826)
    prom = (float(log_power[idx]) - med) / max(mad, 1e-9)
    ordered = np.sort(np.asarray(power, dtype=np.float64))
    second_ratio = float(ordered[-2] / max(ordered[-1], 1e-24)) if ordered.size > 1 else 0.0
    edge_distance = min(abs(peak - float(tune_axis[0])), abs(peak - float(tune_axis[-1])))
    return peak, prom, second_ratio, edge_distance


def _visibility_class(score: float, prominence: float, edge_distance: float, thresholds: dict[str, object]) -> str:
    visible_score = float(thresholds.get("visible_score", 0.65))
    weak_score = float(thresholds.get("weak_score", 0.35))
    visible_prom = float(thresholds.get("visible_prominence", 4.0))
    weak_prom = float(thresholds.get("weak_prominence", 3.0))
    edge_min = float(thresholds.get("edge_min", 0.003))
    if score >= visible_score and prominence >= visible_prom and edge_distance >= edge_min:
        return "VISIBLE_TUNE"
    if score >= weak_score or prominence >= weak_prom:
        return "WEAK_TUNE"
    return "NO_RELIABLE_TUNE"


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / max(1, len(a | b))


def process_cache(cache: dict[str, str]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    meta_by_index = _HANDOFF_WORKER_STATE["bpm_meta"]  # type: ignore[assignment]
    consensus_windows = _HANDOFF_WORKER_STATE["consensus_windows"]  # type: ignore[assignment]
    consensus_summary = _HANDOFF_WORKER_STATE["consensus_summary"]  # type: ignore[assignment]
    thresholds = _HANDOFF_WORKER_STATE["thresholds"]  # type: ignore[assignment]
    spectra = np.load(cache["spectra_path"], mmap_mode="r")
    tune_axis = np.load(cache["tune_axis_path"])
    centers = np.load(cache["window_centers_path"])
    bpm_indices = np.load(cache["bpm_indices_path"])
    key_base = (cache["collection"], cache["spill_id"], cache["plane"])
    summary = consensus_summary.get(key_base, {})  # type: ignore[union-attr]
    visibility_rows: list[dict[str, object]] = []
    window_top: dict[int, dict[int, list[str]]] = {}
    window_consensus: dict[int, float] = {}
    for widx in range(spectra.shape[1]):
        cwin = consensus_windows.get((*key_base, cache["spectral_config"], str(widx)), {})  # type: ignore[union-attr]
        q = _f(cwin.get("consensus_tune") or summary.get("dominant_consensus_tune"))
        label = cwin.get("consensus_label") or summary.get("consensus_label", "")
        window_consensus[widx] = q
        scored: list[tuple[float, dict[str, object]]] = []
        for bpos, bpm_idx_raw in enumerate(bpm_indices):
            bpm_idx = int(bpm_idx_raw)
            meta = meta_by_index.get((cache["plane"], bpm_idx), {})  # type: ignore[union-attr]
            power = np.asarray(spectra[bpos, widx], dtype=np.float32)
            peak_tune, prom, second_ratio, edge_distance = _peak_metrics(power, tune_axis)
            signal, bg, support = _local_metrics(power, tune_axis, q)
            entropy = _entropy(power)
            score = (
                0.40 * max(0.0, min(1.0, prom / 8.0 if math.isfinite(prom) else 0.0))
                + 0.25 * max(0.0, min(1.0, math.log10(max(support, 1.0)) / math.log10(8.0) if math.isfinite(support) else 0.0))
                + 0.15 * max(0.0, 1.0 - min(1.0, second_ratio if math.isfinite(second_ratio) else 1.0))
                + 0.10 * max(0.0, 1.0 - min(1.0, entropy if math.isfinite(entropy) else 1.0))
                + 0.10 * max(0.0, min(1.0, edge_distance / 0.01 if math.isfinite(edge_distance) else 0.0))
            )
            vclass = _visibility_class(score, prom, edge_distance, thresholds)  # type: ignore[arg-type]
            flags = []
            if not math.isfinite(q):
                flags.append("NO_CONSENSUS_TUNE")
            row = {
                "collection": cache["collection"],
                "spill_id": cache["spill_id"],
                "plane": cache["plane"],
                "spectral_config": cache["spectral_config"],
                "window_index": widx,
                "center_turn": _fmt(float(centers[widx])),
                "bpm_index": bpm_idx,
                "bpm_name": meta.get("bpm_name", str(bpm_idx)),
                "digitizer": meta.get("digitizer", ""),
                "consensus_tune": _fmt(q),
                "consensus_label": label,
                "peak_tune": _fmt(peak_tune),
                "peak_prominence_z": _fmt(prom),
                "power_at_consensus": _fmt(signal),
                "local_background_at_consensus": _fmt(bg),
                "support_at_consensus": _fmt(support),
                "second_peak_ratio": _fmt(second_ratio),
                "spectral_entropy": _fmt(entropy),
                "visibility_score": _fmt(score),
                "visibility_class": vclass,
                "is_top1_visible": "false",
                "is_top3_visible": "false",
                "is_top5_visible": "false",
                "quality_flags": "|".join(flags),
            }
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        tops = {
            1: [str(item[1]["bpm_name"]) for item in scored[:1]],
            3: [str(item[1]["bpm_name"]) for item in scored[:3]],
            5: [str(item[1]["bpm_name"]) for item in scored[:5]],
        }
        window_top[widx] = tops
        top_sets = {size: set(names) for size, names in tops.items()}
        for _score, row in scored:
            name = str(row["bpm_name"])
            row["is_top1_visible"] = str(name in top_sets[1]).lower()
            row["is_top3_visible"] = str(name in top_sets[3]).lower()
            row["is_top5_visible"] = str(name in top_sets[5]).lower()
            visibility_rows.append(row)
    event_rows: list[dict[str, object]] = []
    for size in (1, 3, 5):
        previous: set[str] | None = None
        previous_q = math.nan
        for widx in range(spectra.shape[1]):
            current = set(window_top.get(widx, {}).get(size, []))
            if previous is None:
                previous = current
                previous_q = window_consensus.get(widx, math.nan)
                continue
            jac = _jaccard(previous, current)
            handoff_score = 1.0 - jac
            persistence = 1
            for next_idx in range(widx + 1, spectra.shape[1]):
                if _jaccard(current, set(window_top.get(next_idx, {}).get(size, []))) >= 0.8:
                    persistence += 1
                else:
                    break
            q = window_consensus.get(widx, math.nan)
            q_delta = abs(q - previous_q) if math.isfinite(q) and math.isfinite(previous_q) else math.nan
            label = "STABLE"
            if handoff_score >= 0.6 and persistence >= 3 and (not math.isfinite(q_delta) or q_delta <= 0.006):
                label = "PERSISTENT_HANDOFF"
            elif handoff_score >= 0.6:
                label = "FLICKER"
            event_rows.append(
                {
                    "collection": cache["collection"],
                    "spill_id": cache["spill_id"],
                    "plane": cache["plane"],
                    "subset_size": size,
                    "window_index": widx,
                    "center_turn": _fmt(float(centers[widx])),
                    "previous_members": ",".join(sorted(previous)),
                    "current_members": ",".join(sorted(current)),
                    "jaccard_vs_previous": _fmt(jac),
                    "handoff_score": _fmt(handoff_score),
                    "handoff_persistence": persistence,
                    "consensus_tune": _fmt(q),
                    "consensus_delta": _fmt(q_delta),
                    "event_label": label,
                    "quality_flags": "",
                }
            )
            previous = current
            previous_q = q
    summary_rows: list[dict[str, object]] = []
    events_by_bpm = defaultdict(int)
    for event in event_rows:
        if event["event_label"] == "PERSISTENT_HANDOFF":
            for name in str(event["current_members"]).split(","):
                if name:
                    events_by_bpm[(event["collection"], event["spill_id"], event["plane"], name)] += 1
    grouped = defaultdict(list)
    for row in visibility_rows:
        grouped[(row["collection"], row["spill_id"], row["plane"], row["bpm_index"], row["bpm_name"], row["digitizer"])].append(row)
    for (collection, spill_id, plane, bpm_index, bpm_name, digitizer), rows in grouped.items():
        visible = [row for row in rows if row["visibility_class"] == "VISIBLE_TUNE"]
        turns = [_f(row.get("center_turn")) for row in visible]
        summary_rows.append(
            {
                "collection": collection,
                "spill_id": spill_id,
                "plane": plane,
                "bpm_index": bpm_index,
                "bpm_name": bpm_name,
                "digitizer": digitizer,
                "visible_window_fraction": _fmt(len(visible) / max(1, len(rows))),
                "first_visible_turn": _fmt(min(turns)) if turns else "",
                "last_visible_turn": _fmt(max(turns)) if turns else "",
                "visibility_duration_turns": _fmt(max(turns) - min(turns)) if len(turns) > 1 else "",
                "median_visibility_score": _fmt(median([_f(row.get("visibility_score")) for row in rows])),
                "median_support_at_consensus": _fmt(median([_f(row.get("support_at_consensus")) for row in rows])),
                "top1_window_fraction": _fmt(sum(row["is_top1_visible"] == "true" for row in rows) / max(1, len(rows))),
                "top3_window_fraction": _fmt(sum(row["is_top3_visible"] == "true" for row in rows) / max(1, len(rows))),
                "top5_window_fraction": _fmt(sum(row["is_top5_visible"] == "true" for row in rows) / max(1, len(rows))),
                "handoff_event_count": events_by_bpm.get((collection, spill_id, plane, bpm_name), 0),
            }
        )
    return visibility_rows, event_rows, summary_rows


def process_handoff_chunk(args: tuple[int, int, list[dict[str, str]], str | None]) -> tuple[int, int, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    shard_id, total_shards, rows, progress_dir = args
    progress_path = Path(progress_dir) if progress_dir else None
    started = time.time()
    write_shard_status(progress_path, shard_id, total_shards, "running", 0, len(rows), 0, started)
    visibility: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    summary: list[dict[str, object]] = []
    try:
        for idx, cache in enumerate(rows, start=1):
            v, e, s = process_cache(cache)
            visibility.extend(v)
            events.extend(e)
            summary.extend(s)
            write_shard_status(progress_path, shard_id, total_shards, "running", idx, len(rows), len(visibility), started)
    except BaseException as exc:
        write_shard_status(progress_path, shard_id, total_shards, "failed", 0, len(rows), len(visibility), started, repr(exc))
        raise
    write_shard_status(progress_path, shard_id, total_shards, "complete", len(rows), len(rows), len(visibility), started)
    return shard_id, len(rows), visibility, events, summary


def _matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def write_handoff_plots(out: Path, visibility_rows: Sequence[dict[str, object]], event_rows: Sequence[dict[str, object]]) -> None:
    plt = _matplotlib()
    if plt is None:
        atomic_write_text(out / "handoff_plots_unavailable.txt", "matplotlib unavailable\n")
        return
    for plane in ("H", "V"):
        rows = [row for row in visibility_rows if row.get("plane") == plane]
        by_turn: dict[float, list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            by_turn[_f(row.get("center_turn"))].append(row)
        xs = sorted(k for k in by_turn if math.isfinite(k))
        frac = []
        for x in xs:
            vals = by_turn[x]
            frac.append(sum(row.get("visibility_class") == "VISIBLE_TUNE" for row in vals) / max(1, len(vals)))
        for name, y, ylabel in (
            (f"visible_bpm_fraction_vs_turn_{plane.lower()}.png", frac, "visible BPM fraction"),
            (
                f"top_bpm_membership_vs_turn_{plane.lower()}.png",
                [sum(row.get("is_top5_visible") == "true" for row in by_turn[x]) for x in xs],
                "top5 membership count",
            ),
        ):
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.plot(xs, y, marker="o", linewidth=1.0)
            ax.set_title(f"{plane} {ylabel}")
            ax.set_xlabel("center turn")
            ax.set_ylabel(ylabel)
            fig.tight_layout()
            fig.savefig(out / name, dpi=160)
            plt.close(fig)
        ev = [row for row in event_rows if row.get("plane") == plane and row.get("event_label") == "PERSISTENT_HANDOFF"]
        counts = defaultdict(int)
        for row in ev:
            counts[_f(row.get("center_turn"))] += 1
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(list(counts.keys()), list(counts.values()), width=180)
        ax.set_title(f"{plane} persistent handoff rate")
        ax.set_xlabel("center turn")
        ax.set_ylabel("events")
        fig.tight_layout()
        fig.savefig(out / f"handoff_rate_vs_turn_{plane.lower()}.png", dpi=160)
        plt.close(fig)
        summary_by_bpm = defaultdict(list)
        for row in rows:
            summary_by_bpm[str(row.get("bpm_name"))].append(_f(row.get("visibility_score")))
        labels = sorted(summary_by_bpm)
        values = [median(summary_by_bpm[label]) for label in labels]
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.imshow(np.asarray(values, dtype=float)[None, :], aspect="auto", cmap="viridis")
        ax.set_title(f"{plane} BPM visibility cluster map")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=75, ha="right", fontsize=6)
        ax.set_yticks([])
        fig.tight_layout()
        fig.savefig(out / f"bpm_visibility_cluster_map_{plane.lower()}.png", dpi=160)
        plt.close(fig)
    selected = sorted({(str(row["collection"]), str(row["spill_id"]), str(row["plane"])) for row in visibility_rows})[:12]
    for collection, spill_id, plane in selected:
        rows = [row for row in visibility_rows if row["collection"] == collection and row["spill_id"] == spill_id and row["plane"] == plane]
        bpm_names = sorted({str(row["bpm_name"]) for row in rows})
        turns = sorted({_f(row["center_turn"]) for row in rows if math.isfinite(_f(row["center_turn"]))})
        matrix = np.full((len(bpm_names), len(turns)), np.nan, dtype=float)
        bpm_pos = {name: idx for idx, name in enumerate(bpm_names)}
        turn_pos = {turn: idx for idx, turn in enumerate(turns)}
        for row in rows:
            turn = _f(row["center_turn"])
            if str(row["bpm_name"]) in bpm_pos and turn in turn_pos:
                matrix[bpm_pos[str(row["bpm_name"])], turn_pos[turn]] = _f(row["visibility_score"])
        stem = f"spill_{spill_id}_{plane.lower()}"
        fig, ax = plt.subplots(figsize=(10, 6))
        im = ax.imshow(matrix, aspect="auto", origin="lower", cmap="viridis", vmin=0, vmax=1)
        ax.set_title(f"{stem} BPM visibility handoff")
        ax.set_xlabel("window index")
        ax.set_ylabel("BPM")
        ax.set_yticks(range(len(bpm_names)))
        ax.set_yticklabels(bpm_names, fontsize=5)
        fig.colorbar(im, ax=ax, label="visibility score")
        fig.tight_layout()
        fig.savefig(out / f"{stem}_bpm_visibility_handoff.png", dpi=160)
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(8, 4))
        ev = [row for row in event_rows if row["collection"] == collection and row["spill_id"] == spill_id and row["plane"] == plane]
        ax.plot([_f(row["center_turn"]) for row in ev], [_f(row["handoff_score"]) for row in ev], marker="o", linewidth=0.8)
        ax.set_title(f"{stem} top sets vs turn")
        ax.set_xlabel("center turn")
        ax.set_ylabel("handoff score")
        fig.tight_layout()
        fig.savefig(out / f"{stem}_top_sets_vs_turn.png", dpi=160)
        plt.close(fig)


def run_handoff_analysis(
    cfg: dict[str, object],
    root: Path,
    out: Path,
    workers: int | None = None,
    limit: int = 0,
    spectral_config: str | None = None,
) -> None:
    spectral_config = spectral_config or str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256"))
    thresholds = cfg.get("handoff", {}) if isinstance(cfg.get("handoff"), dict) else {}
    caches = selected_cache_rows(root, spectral_config, limit)
    runtime = cfg.get("runtime", {})
    worker_count = max(1, int(workers if workers is not None else runtime.get("workers", 1) if isinstance(runtime, dict) else 1))
    worker_count = min(worker_count, len(caches) if caches else 1)
    chunks = chunked(caches, max(1, math.ceil(len(caches) / max(1, worker_count))))
    progress_dir = out / "handoff" / "progress"
    started = time.time()
    write_parent_status(progress_dir, "running", 0, len(chunks), 0, len(caches), 0, started)
    visibility: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    summary: list[dict[str, object]] = []
    if worker_count <= 1 or len(chunks) <= 1:
        init_handoff_worker(str(root), spectral_config, json.dumps(thresholds, sort_keys=True))
        done = 0
        for idx, chunk in enumerate(chunks):
            _, row_count, v, e, s = process_handoff_chunk((idx, len(chunks), chunk, str(progress_dir)))
            done += row_count
            visibility.extend(v)
            events.extend(e)
            summary.extend(s)
            write_parent_status(progress_dir, "running", idx + 1, len(chunks), done, len(caches), len(visibility), started)
    else:
        results: dict[int, tuple[int, list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]] = {}
        with ProcessPoolExecutor(max_workers=worker_count, initializer=init_handoff_worker, initargs=(str(root), spectral_config, json.dumps(thresholds, sort_keys=True))) as pool:
            tasks = [(idx, len(chunks), chunk, str(progress_dir)) for idx, chunk in enumerate(chunks)]
            done = 0
            completed = 0
            output_rows = 0
            for future in as_completed(pool.submit(process_handoff_chunk, task) for task in tasks):
                shard_id, row_count, v, e, s = future.result()
                results[shard_id] = (row_count, v, e, s)
                done += row_count
                output_rows += len(v)
                completed += 1
                write_parent_status(progress_dir, "running", completed, len(chunks), done, len(caches), output_rows, started)
        for idx in sorted(results):
            visibility.extend(results[idx][1])
            events.extend(results[idx][2])
            summary.extend(results[idx][3])
    visibility.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["window_index"]), int(row["bpm_index"])))
    events.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["subset_size"]), int(row["window_index"])))
    summary.sort(key=lambda row: (str(row["collection"]), str(row["spill_id"]), str(row["plane"]), int(row["bpm_index"])))
    write_csv(out / "handoff" / "bpm_window_visibility.csv", visibility, WINDOW_VISIBILITY_FIELDS)
    write_csv(out / "handoff" / "bpm_handoff_events.csv", events, HANDOFF_EVENT_FIELDS)
    write_csv(out / "handoff" / "bpm_visibility_summary.csv", summary, VISIBILITY_SUMMARY_FIELDS)
    write_handoff_plots(out / "handoff", visibility, events)
    persistent = sum(row.get("event_label") == "PERSISTENT_HANDOFF" for row in events)
    atomic_write_text(
        out / "handoff" / "handoff_summary.md",
        "# BPM Handoff Summary\n\n"
        f"- spectral config: `{spectral_config}`\n"
        f"- selected cache rows: `{len(caches)}`\n"
        f"- visibility rows: `{len(visibility)}`\n"
        f"- handoff event rows: `{len(events)}`\n"
        f"- persistent handoff events: `{persistent}`\n"
        f"- workers: `{worker_count}`\n"
        "- visibility is position-derived and uses within-spill consensus as an internal BPM-only reference.\n",
    )
    write_parent_status(progress_dir, "complete", len(chunks), len(chunks), len(caches), len(caches), len(visibility), started)
