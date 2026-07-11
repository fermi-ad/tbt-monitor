"""Direct fixed-set evaluation from cached Best-BPM spectra."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

import numpy as np
import bpm_dgx_poster as poster

from .evolution import combine_spectra, score_evolution_windows
from .identity import identity_fields, manifest_by_index, normalize_subset_row, parse_indices, subset_indices
from .io import atomic_write_text, read_csv, write_csv
from .progress import chunked, write_parent_status, write_shard_status


FIXED_EVAL_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "spectral_config",
    "method",
    "subset_size",
    "train_collection",
    "test_collection",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "q_hat",
    "score",
    "visible_fraction",
    "visibility_duration_turns",
    "last_visible_turn",
    "median_prominence",
    "median_abs_step_visible",
    "p95_step_visible",
    "ridge_jump_fraction",
    "quality_flags",
]

FIXED_SUMMARY_FIELDS = [
    "plane",
    "method",
    "subset_size",
    "train_collection",
    "test_collection",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "row_count",
    "median_score",
    "median_visible_fraction",
    "median_visibility_duration_turns",
    "median_q_hat",
    "median_abs_dynamic_delta",
]

_FIXED_WORKER_STATE: dict[str, object] = {}


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


def _split_members(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def cache_rows(cache_dir: Path, spectral_config: str, limit: int = 0) -> list[dict[str, str]]:
    rows = [
        row
        for row in read_csv(cache_dir / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    ]
    rows.sort(key=lambda row: (row["collection"], row["spill_id"], row["plane"]))
    return rows[:limit] if limit else rows


def bpm_maps(manifest_dir: Path) -> dict[tuple[str, int], dict[str, str]]:
    return manifest_by_index(read_csv(manifest_dir / "bpm_index.csv"))


def best1_rows(root: Path) -> list[dict[str, str]]:
    path = root / "subset_search" / "best1" / "best1_rankings.csv"
    return read_csv(path) if path.exists() else []


def dynamic_membership_specs(
    root: Path,
    subset_sizes: Sequence[int],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for size in subset_sizes:
        path = root / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if not path.exists():
            continue
        for row in read_csv(path):
            row = normalize_subset_row(row, meta_by_index)
            out.append(
                {
                    "collection": row["collection"],
                    "spill_id": row["spill_id"],
                    "plane": row["plane"],
                    "method": f"dynamic_best{size}",
                    "subset_size": str(size),
                    "bpm_indices": row.get("bpm_indices", ""),
                    "bpm_members": row.get("bpm_members", ""),
                    "bpm_source_keys": row.get("bpm_source_keys", ""),
                    "bpm_digitizers": row.get("bpm_digitizers", ""),
                }
            )
    return out


def choose_fixed_members(root: Path, manifest_dir: Path, subset_sizes: Sequence[int]) -> list[dict[str, object]]:
    rows = best1_rows(root)
    meta_by_index = bpm_maps(manifest_dir)
    collections = sorted({row["collection"] for row in rows})
    if not collections:
        collections = sorted({row["collection"] for row in read_csv(root / "manifest" / "spills.csv")})
    specs: list[dict[str, object]] = []
    for train in collections:
        test_collections = [col for col in collections if col != train] or [train]
        for plane in ("H", "V"):
            scores: dict[int, list[float]] = defaultdict(list)
            counts: dict[int, int] = defaultdict(int)
            for row in rows:
                if row.get("collection") != train or row.get("plane") != plane:
                    continue
                members = subset_indices(row, plane, meta_by_index)
                if len(members) != 1:
                    continue
                score = _f(row.get("subset_score"))
                if math.isfinite(score):
                    scores[members[0]].append(score)
                    counts[members[0]] += 1
            ranked = sorted(
                [idx for meta_plane, idx in meta_by_index if meta_plane == plane and scores.get(idx)],
                key=lambda idx: (median(scores.get(idx, [])), counts.get(idx, 0), idx),
                reverse=True,
            )
            required = max([int(size) for size in subset_sizes if int(size) > 0] or [0])
            if len(ranked) < required:
                raise ValueError(
                    f"{train}/{plane}: only {len(ranked)} BPMs have finite training scores; need {required}"
                )
            for size in subset_sizes:
                size = int(size)
                if size <= 0:
                    continue
                member_indices = ranked[:size]
                if len(member_indices) != size:
                    continue
                identities = identity_fields(plane, member_indices, meta_by_index)
                for test in test_collections:
                    specs.append(
                        {
                            "method": f"fixed_top{size}",
                            "subset_size": str(size),
                            "train_collection": train,
                            "test_collection": test,
                            "plane": plane,
                            **identities,
                        }
                    )
    specs.sort(key=lambda row: (str(row["train_collection"]), str(row["test_collection"]), str(row["plane"]), str(row["subset_size"])))
    return specs


def init_fixed_worker(specs_json: str, dynamic_specs_json: str) -> None:
    global _FIXED_WORKER_STATE
    dynamic_by_cache: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    seen_dynamic: set[tuple[str, str, str, str]] = set()
    for spec in json.loads(dynamic_specs_json):
        cache_key = (str(spec["collection"]), str(spec["spill_id"]), str(spec["plane"]))
        unique_key = (*cache_key, str(spec["method"]))
        if unique_key in seen_dynamic:
            raise ValueError(f"duplicate dynamic membership: {unique_key}")
        seen_dynamic.add(unique_key)
        dynamic_by_cache[cache_key].append(spec)
    _FIXED_WORKER_STATE = {
        "specs": json.loads(specs_json),
        "dynamic_by_cache": dynamic_by_cache,
    }


def _score_row(
    cache: dict[str, str],
    method: str,
    subset_size: str,
    train_collection: str,
    test_collection: str,
    bpm_indices: str,
    bpm_members: str,
    bpm_source_keys: str,
    bpm_digitizers: str,
    combined: np.ndarray,
    tune_axis: np.ndarray,
    centers: np.ndarray,
) -> dict[str, object]:
    metrics = score_evolution_windows(combined, tune_axis, centers)
    visible_fraction = metrics["visible_fraction"]
    median_prominence = metrics["median_prominence"]
    score = visible_fraction * max(
        0.0,
        min(1.0, median_prominence / 12.0 if math.isfinite(median_prominence) else 0.0),
    )
    flags: list[str] = []
    if not math.isfinite(metrics["q_hat"]):
        flags.append("NO_VALID_Q")
    if not math.isfinite(median_prominence):
        flags.append("NO_VISIBLE_TUNE" if visible_fraction == 0.0 else "INVALID_PROMINENCE")
    return {
        "collection": cache["collection"],
        "spill_id": cache["spill_id"],
        "plane": cache["plane"],
        "spectral_config": cache["spectral_config"],
        "method": method,
        "subset_size": subset_size,
        "train_collection": train_collection,
        "test_collection": test_collection,
        "bpm_indices": bpm_indices,
        "bpm_members": bpm_members,
        "bpm_source_keys": bpm_source_keys,
        "bpm_digitizers": bpm_digitizers,
        "q_hat": _fmt(metrics["q_hat"]),
        "score": _fmt(score),
        "visible_fraction": _fmt(visible_fraction),
        "visibility_duration_turns": _fmt(metrics["visibility_duration_turns"]),
        "last_visible_turn": _fmt(metrics["last_visible_turn"]),
        "median_prominence": _fmt(median_prominence),
        "median_abs_step_visible": _fmt(metrics["median_abs_step_visible"]),
        "p95_step_visible": _fmt(metrics["p95_step_visible"]),
        "ridge_jump_fraction": _fmt(metrics["ridge_jump_fraction"]),
        "quality_flags": "|".join(flags),
    }


def evaluate_cache_row(
    cache: dict[str, str],
    specs: list[dict[str, object]],
    dynamic_by_cache: dict[tuple[str, str, str], list[dict[str, object]]],
) -> list[dict[str, object]]:
    spectra = np.load(cache["spectra_path"], mmap_mode="r")
    tune_axis = np.load(cache["tune_axis_path"])
    centers = np.load(cache["window_centers_path"])
    bpm_indices = np.load(cache["bpm_indices_path"])
    pos_by_index = {int(idx): pos for pos, idx in enumerate(bpm_indices)}
    out: list[dict[str, object]] = []
    if spectra.shape[0] == 0:
        return out
    all_spectra = np.asarray(spectra, dtype=np.float32)
    for method, aggregator in (("all_bpm_mean", "mean_power"), ("all_bpm_median", "median_power")):
        out.append(_score_row(cache, method, "all", "", cache["collection"], "", "", "", "", combine_spectra(all_spectra, aggregator), tune_axis, centers))
    cache_key = (cache["collection"], cache["spill_id"], cache["plane"])
    for spec in dynamic_by_cache.get(cache_key, []):
        wanted = parse_indices(spec.get("bpm_indices"))
        expected = int(spec["subset_size"])
        positions = [pos_by_index[idx] for idx in wanted if idx in pos_by_index]
        if len(wanted) != expected or len(positions) != expected:
            raise ValueError(
                f"{cache_key} {spec['method']} resolves {len(positions)}/{expected} exact cached channels"
            )
        selected = np.asarray(spectra[positions], dtype=np.float32)
        out.append(
            _score_row(
                cache,
                str(spec["method"]),
                str(spec["subset_size"]),
                "",
                cache["collection"],
                str(spec.get("bpm_indices", "")),
                str(spec.get("bpm_members", "")),
                str(spec.get("bpm_source_keys", "")),
                str(spec.get("bpm_digitizers", "")),
                combine_spectra(selected, "mean_power"),
                tune_axis,
                centers,
            )
        )
    for spec in specs:
        if spec["test_collection"] != cache["collection"] or spec["plane"] != cache["plane"]:
            continue
        wanted = parse_indices(spec.get("bpm_indices"))
        positions = [pos_by_index[idx] for idx in wanted if idx in pos_by_index]
        expected = int(spec["subset_size"])
        if len(wanted) != expected or len(positions) != expected:
            raise ValueError(
                f"{cache_key} {spec['method']} resolves {len(positions)}/{expected} exact cached channels"
            )
        selected = np.asarray(spectra[positions], dtype=np.float32)
        out.append(
            _score_row(
                cache,
                str(spec["method"]),
                str(spec["subset_size"]),
                str(spec["train_collection"]),
                str(spec["test_collection"]),
                str(spec.get("bpm_indices", "")),
                str(spec["bpm_members"]),
                str(spec.get("bpm_source_keys", "")),
                str(spec.get("bpm_digitizers", "")),
                combine_spectra(selected, "mean_power"),
                tune_axis,
                centers,
            )
        )
    return out


def process_fixed_chunk(args: tuple[int, int, list[dict[str, str]], str | None]) -> tuple[int, int, list[dict[str, object]]]:
    shard_id, total_shards, rows, progress_dir = args
    progress_path = Path(progress_dir) if progress_dir else None
    started = time.time()
    write_shard_status(progress_path, shard_id, total_shards, "running", 0, len(rows), 0, started)
    out: list[dict[str, object]] = []
    try:
        for idx, row in enumerate(rows, start=1):
            out.extend(
                evaluate_cache_row(
                    row,
                    _FIXED_WORKER_STATE["specs"],  # type: ignore[arg-type]
                    _FIXED_WORKER_STATE["dynamic_by_cache"],  # type: ignore[arg-type]
                )
            )
            if idx % 10 == 0 or idx == len(rows):
                write_shard_status(progress_path, shard_id, total_shards, "running", idx, len(rows), len(out), started)
    except BaseException as exc:
        write_shard_status(progress_path, shard_id, total_shards, "failed", 0, len(rows), len(out), started, repr(exc))
        raise
    write_shard_status(progress_path, shard_id, total_shards, "complete", len(rows), len(rows), len(out), started)
    return shard_id, len(rows), out


def summarize(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, str, str], list[dict[str, object]]] = defaultdict(list)
    dynamic_medians: dict[tuple[str, str, str], float] = {}
    for row in rows:
        key = (
            str(row.get("plane", "")),
            str(row.get("method", "")),
            str(row.get("subset_size", "")),
            str(row.get("train_collection", "")),
            str(row.get("test_collection", "")),
        )
        grouped[key].append(row)
    for (plane, method, subset_size, _train, test), group in grouped.items():
        if method.startswith("dynamic_best"):
            dynamic_medians[(plane, test, subset_size)] = median([_f(row.get("score")) for row in group])
    out: list[dict[str, object]] = []
    for key in sorted(grouped):
        plane, method, subset_size, train, test = key
        group = grouped[key]
        scores = [_f(row.get("score")) for row in group]
        visible = [_f(row.get("visible_fraction")) for row in group]
        durations = [_f(row.get("visibility_duration_turns")) for row in group]
        qhats = [_f(row.get("q_hat")) for row in group]
        med = median(scores)
        dyn = dynamic_medians.get((plane, test, subset_size))
        identity = next((row for row in group if row.get("bpm_indices")), {})
        out.append(
            {
                "plane": plane,
                "method": method,
                "subset_size": subset_size,
                "train_collection": train,
                "test_collection": test,
                "bpm_indices": str(identity.get("bpm_indices", "")),
                "bpm_members": str(identity.get("bpm_members", "")),
                "bpm_source_keys": str(identity.get("bpm_source_keys", "")),
                "bpm_digitizers": str(identity.get("bpm_digitizers", "")),
                "row_count": len(group),
                "median_score": _fmt(med),
                "median_visible_fraction": _fmt(median(visible)),
                "median_visibility_duration_turns": _fmt(median(durations)),
                "median_q_hat": _fmt(median(qhats)),
                "median_abs_dynamic_delta": _fmt(abs(med - dyn)) if dyn is not None and math.isfinite(med) and math.isfinite(dyn) else "",
            }
        )
    return out


def write_plots(summary_rows: Sequence[dict[str, object]], out: Path) -> None:
    collections = sorted({str(row.get("test_collection", "")) for row in summary_rows if row.get("test_collection")})
    labels = {collection: chr(ord("A") + index) for index, collection in enumerate(collections)}
    dynamic_colors = (poster.BLUE, poster.GREEN, poster.RED)
    fixed_colors = (poster.ORANGE, poster.PURPLE, (80, 105, 115))
    for plane in ("H", "V"):
        rows = [row for row in summary_rows if row.get("plane") == plane]
        series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]] = []
        for index, collection in enumerate(collections):
            points = sorted(
                (
                    float(row["subset_size"]),
                    _f(row.get("median_score")),
                )
                for row in rows
                if row.get("test_collection") == collection
                and str(row.get("method", "")).startswith("dynamic_best")
            )
            if points:
                series.append((f"ADAPTIVE {labels[collection]}", points, dynamic_colors[index % len(dynamic_colors)]))
        fixed_directions = sorted(
            {
                (str(row.get("train_collection", "")), str(row.get("test_collection", "")))
                for row in rows
                if str(row.get("method", "")).startswith("fixed_")
            }
        )
        for index, (train, test) in enumerate(fixed_directions):
            points = sorted(
                (
                    float(row["subset_size"]),
                    _f(row.get("median_score")),
                )
                for row in rows
                if row.get("train_collection") == train
                and row.get("test_collection") == test
                and str(row.get("method", "")).startswith("fixed_")
            )
            if points:
                series.append(
                    (
                        f"FROZEN {labels.get(train, '?')}->{labels.get(test, '?')}",
                        points,
                        fixed_colors[index % len(fixed_colors)],
                    )
                )
        path = out / "artifacts" / "global" / f"fixed_vs_dynamic_direct_{plane.lower()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        poster.line_plot(
            path,
            f"{plane} SAME-METRIC ADAPTIVE VS FROZEN",
            series,
            x_label="ENSEMBLE SIZE N",
            y_label="EVOLUTION SCORE",
        )
        mapping = ", ".join(f"{labels[collection]} = {collection}" for collection in collections)
        atomic_write_text(
            path.with_name(f"{path.stem}_caption.md"),
            f"# {plane} Same-Metric Adaptive Versus Frozen Control\n\n"
            f"Collection labels: {mapping}. Every curve is recomputed from the same spectral cache with the same evolution score. "
            "Frozen A->B is trained on collection A and applied to B. The comparison is descriptive because adaptive memberships reuse their selection windows; leakage-controlled Best-N validation carries the inferential claim.\n",
        )

        method_labels = (
            ("all_bpm_mean", "ALL MEAN"),
            ("all_bpm_median", "ALL MED"),
            ("dynamic_best1", "ADAPT 1"),
            ("dynamic_best3", "ADAPT 3"),
            ("dynamic_best5", "ADAPT 5"),
            ("fixed_top1", "FROZEN 1"),
            ("fixed_top3", "FROZEN 3"),
            ("fixed_top5", "FROZEN 5"),
        )
        bar_labels: list[str] = []
        bar_values: list[float] = []
        for method, label in method_labels:
            values = [
                _f(row.get("median_score"))
                for row in rows
                if row.get("method") == method
            ]
            values = [value for value in values if math.isfinite(value)]
            if values:
                bar_labels.append(label)
                bar_values.append(median(values))
        control_path = out / "artifacts" / "global" / f"fixed_dynamic_all_bpm_summary_{plane.lower()}.png"
        maximum = max(bar_values, default=0.0)
        upper = maximum * 1.2 if maximum > 0 else 1.0
        poster.bar_plot(
            control_path,
            f"{plane} SAME-METRIC CONTROL SUMMARY",
            bar_labels,
            bar_values,
            y_label="SCORE",
            y_range=(0.0, upper),
            colors=(
                poster.BLUE,
                poster.GREEN,
                poster.PURPLE,
                poster.PURPLE,
                poster.PURPLE,
                poster.ORANGE,
                poster.ORANGE,
                poster.ORANGE,
            ),
            show_values=True,
            note="DESCRIPTIVE SAME-WINDOW MEDIAN; TWO COLLECTIONS OR DIRECTIONS",
            x_label="METHOD",
        )
        atomic_write_text(
            control_path.with_name(f"{control_path.stem}_caption.md"),
            f"# {plane} Same-Metric Adaptive, Frozen, And All-BPM Controls\n\n"
            "Each bar is the median of the two collection or train/test-direction summary rows. "
            "All-BPM mean and median use all 60 plane channels; adaptive and frozen bars use N=1,3,5. "
            "Every method is recomputed from the same cache with the same evolution score. "
            "This reused-window comparison is descriptive, not leakage-controlled inference. "
            "A higher all-BPM bar forbids claiming that a small adaptive set outperforms all-BPM aggregation under this metric.\n",
        )


def evaluate_fixed_sets(
    cfg: dict[str, object],
    root: Path,
    out: Path,
    workers: int | None = None,
    limit: int = 0,
    spectral_config: str | None = None,
    subset_sizes: Sequence[int] = (1, 3, 5),
) -> None:
    spectral_config = spectral_config or str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256"))
    cache = cache_rows(root / "cache", spectral_config, limit)
    specs = choose_fixed_members(root, root / "manifest", subset_sizes)
    meta_by_index = bpm_maps(root / "manifest")
    dynamic_specs = dynamic_membership_specs(root, subset_sizes, meta_by_index)
    progress_dir = out / "statistics" / "fixed_set_progress"
    runtime = cfg.get("runtime", {})
    worker_count = max(1, int(workers if workers is not None else runtime.get("workers", 1) if isinstance(runtime, dict) else 1))
    worker_count = min(worker_count, len(cache) if cache else 1)
    chunk_size = max(1, int(math.ceil(len(cache) / max(1, worker_count))))
    chunks = chunked(cache, chunk_size)
    started = time.time()
    write_parent_status(progress_dir, "running", 0, len(chunks), 0, len(cache), 0, started)
    specs_json = json.dumps(specs, sort_keys=True)
    dynamic_specs_json = json.dumps(dynamic_specs, sort_keys=True)
    fixed_rows: list[dict[str, object]] = []
    if worker_count <= 1 or len(chunks) <= 1:
        init_fixed_worker(specs_json, dynamic_specs_json)
        rows_done = 0
        for idx, chunk in enumerate(chunks):
            _, row_count, chunk_rows = process_fixed_chunk((idx, len(chunks), chunk, str(progress_dir)))
            rows_done += row_count
            fixed_rows.extend(chunk_rows)
            write_parent_status(progress_dir, "running", idx + 1, len(chunks), rows_done, len(cache), len(fixed_rows), started)
    else:
        results: dict[int, tuple[int, list[dict[str, object]]]] = {}
        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=init_fixed_worker,
            initargs=(specs_json, dynamic_specs_json),
        ) as pool:
            tasks = [(idx, len(chunks), chunk, str(progress_dir)) for idx, chunk in enumerate(chunks)]
            rows_done = 0
            output_rows = 0
            completed = 0
            for future in as_completed(pool.submit(process_fixed_chunk, task) for task in tasks):
                shard_id, row_count, chunk_rows = future.result()
                results[shard_id] = (row_count, chunk_rows)
                rows_done += row_count
                output_rows += len(chunk_rows)
                completed += 1
                write_parent_status(progress_dir, "running", completed, len(chunks), rows_done, len(cache), output_rows, started)
        for idx in sorted(results):
            fixed_rows.extend(results[idx][1])
    all_rows = sorted(fixed_rows, key=lambda row: (str(row.get("collection", "")), str(row.get("spill_id", "")), str(row.get("plane", "")), str(row.get("method", "")), str(row.get("subset_size", "")), str(row.get("train_collection", ""))))
    summary_rows = summarize(all_rows)
    write_csv(out / "statistics" / "fixed_set_direct_evaluation.csv", all_rows, FIXED_EVAL_FIELDS)
    write_csv(out / "statistics" / "fixed_vs_dynamic_direct_summary.csv", summary_rows, FIXED_SUMMARY_FIELDS)
    write_plots(summary_rows, out)
    atomic_write_text(
        out / "statistics" / "fixed_set_direct_summary.md",
        "# Direct Fixed-Set Evaluation\n\n"
        f"- spectral config: `{spectral_config}`\n"
        f"- cache rows evaluated: `{len(cache)}`\n"
        f"- output rows: `{len(all_rows)}`\n"
        f"- workers: `{worker_count}`\n"
        "- dynamic memberships, fixed memberships, and all-BPM controls are all rescored from the same cached spectra with the same evolution metric.\n"
        "- this descriptive comparison reuses the membership-selection windows; leakage-controlled later-window Best-N validation remains the publication inference.\n",
    )
    write_parent_status(progress_dir, "complete", len(chunks), len(chunks), len(cache), len(cache), len(all_rows), started)
