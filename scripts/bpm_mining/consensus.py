"""Within-spill consensus clustering for BPM tune candidates."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .io import atomic_write_text, read_csv, write_csv
from .peaks import extract_candidates
from .schema import CONSENSUS_SUMMARY_FIELDS, CONSENSUS_WINDOW_FIELDS


def _f(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def weighted_median(items: list[tuple[float, float]]) -> float:
    clean = sorted((x, max(0.0, w)) for x, w in items if math.isfinite(x) and math.isfinite(w))
    if not clean:
        return math.nan
    total = sum(w for _, w in clean) or len(clean)
    accum = 0.0
    for x, w in clean:
        accum += w or 1.0
        if accum >= total * 0.5:
            return x
    return clean[-1][0]


def candidate_weight(row: dict[str, str], anchor: float) -> float:
    prom = _f(row.get("peak_prominence_z")) or 0.0
    second = _f(row.get("second_peak_ratio"))
    edge = _f(row.get("distance_to_band_edge")) or 0.0
    q = _f(row.get("peak_tune"))
    if q is None:
        return 0.0
    anchor_weight = math.exp(-0.5 * ((q - anchor) / 0.04) ** 2)
    return max(0.0, min(1.0, prom / 10.0)) * max(0.0, min(1.0, 1.0 - (second if second is not None else 1.0))) * max(0.0, min(1.0, edge / 0.01)) * (0.75 + 0.25 * anchor_weight)


def cluster_candidates(rows: list[dict[str, str]], eps: float, anchor: float) -> list[dict[str, object]]:
    best_by_bpm: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("valid_candidate") != "True" and row.get("valid_candidate") != "true":
            continue
        q = _f(row.get("peak_tune"))
        if q is None:
            continue
        bpm = row.get("bpm_index", "")
        old = best_by_bpm.get(bpm)
        if old is None or (candidate_weight(row, anchor) > candidate_weight(old, anchor)):
            best_by_bpm[bpm] = row
    candidates = sorted(best_by_bpm.values(), key=lambda r: float(r["peak_tune"]))
    clusters: list[list[dict[str, str]]] = []
    for row in candidates:
        q = float(row["peak_tune"])
        if not clusters:
            clusters.append([row])
            continue
        center = weighted_median([(float(item["peak_tune"]), candidate_weight(item, anchor)) for item in clusters[-1]])
        if abs(q - center) <= eps:
            clusters[-1].append(row)
        else:
            clusters.append([row])
    scored = []
    for cluster in clusters:
        weighted = [(float(row["peak_tune"]), candidate_weight(row, anchor)) for row in cluster]
        center = weighted_median(weighted)
        weights = [w for _, w in weighted]
        tunes = [q for q, _ in weighted]
        width = max(tunes) - min(tunes) if tunes else math.nan
        total_weight = sum(weights)
        mad = statistics.median(abs(q - center) for q in tunes) * 1.4826 if tunes else math.nan
        scored.append(
            {
                "rows": cluster,
                "center": center,
                "unique_bpm_count": len({row["bpm_index"] for row in cluster}),
                "total_weight": total_weight,
                "weighted_mad_tune": mad,
                "cluster_width": width,
                "cluster_prominence": total_weight / max(1, len(cluster)),
            }
        )
    scored.sort(key=lambda c: (c["total_weight"], c["unique_bpm_count"]), reverse=True)
    return scored


def bootstrap_ci(rows: list[dict[str, str]], anchor: float, samples: int, seed: int) -> tuple[float, float, float]:
    if not rows:
        return math.nan, math.nan, math.nan
    rng = random.Random(seed)
    bpms = sorted({row["bpm_index"] for row in rows})
    centers = []
    for _ in range(max(1, samples)):
        selected_bpms = [rng.choice(bpms) for _ in bpms]
        sample = [row for row in rows if row["bpm_index"] in selected_bpms]
        clusters = cluster_candidates(sample, eps=0.003, anchor=anchor)
        if clusters:
            centers.append(float(clusters[0]["center"]))
    centers.sort()
    if not centers:
        return math.nan, math.nan, math.nan
    lo = centers[int(0.025 * (len(centers) - 1))]
    hi = centers[int(0.975 * (len(centers) - 1))]
    return lo, hi, (hi - lo) / 2.0


def consensus_label(unique_fraction: float, second_ratio: float, mad: float, unique_count: int) -> str:
    if unique_count < 3:
        return "NO_CONSENSUS"
    if second_ratio >= 0.60:
        return "MULTIMODAL"
    if unique_fraction >= 0.25 and mad <= 0.003:
        return "CLEAN_CONSENSUS"
    if unique_fraction >= 0.10:
        return "WEAK_CONSENSUS"
    return "NO_CONSENSUS"


def consensus_window_row(
    cfg: dict[str, object],
    key: tuple[str, str, str, str, str, str],
    group: list[dict[str, str]],
    boot: int,
    min_eps: float,
) -> dict[str, object]:
    collection, spill_id, plane, spec_name, window_index, center_turn = key
    window_turns = 4096 if "4096" in spec_name else 2048
    eps = max(4.0 / window_turns, min_eps)
    anchor = float(cfg["physics"]["expected_tune_h" if plane == "H" else "expected_tune_v"])
    clusters = cluster_candidates(group, eps, anchor)
    valid_bpms = len({row["bpm_index"] for row in group})
    if clusters:
        first = clusters[0]
        second_weight = float(clusters[1]["total_weight"]) if len(clusters) > 1 else 0.0
        second_ratio = second_weight / max(float(first["total_weight"]), 1e-12)
        unique_fraction = float(first["unique_bpm_count"]) / max(1, valid_bpms)
        lo, hi, unc = bootstrap_ci(first["rows"], anchor, boot, hash(key) & 0xFFFFFFFF)
        label = consensus_label(unique_fraction, second_ratio, float(first["weighted_mad_tune"]), int(first["unique_bpm_count"]))
        return {
            "collection": collection,
            "spill_id": spill_id,
            "plane": plane,
            "spectral_config": spec_name,
            "window_index": window_index,
            "center_turn": center_turn,
            "consensus_tune": first["center"],
            "consensus_ci_low": lo,
            "consensus_ci_high": hi,
            "consensus_uncertainty": unc,
            "unique_bpm_count": first["unique_bpm_count"],
            "unique_bpm_fraction": unique_fraction,
            "total_weight": first["total_weight"],
            "weighted_mad_tune": first["weighted_mad_tune"],
            "cluster_width": first["cluster_width"],
            "cluster_prominence": first["cluster_prominence"],
            "second_cluster_ratio": second_ratio,
            "consensus_label": label,
        }
    return {
        "collection": collection,
        "spill_id": spill_id,
        "plane": plane,
        "spectral_config": spec_name,
        "window_index": window_index,
        "center_turn": center_turn,
        "consensus_label": "NO_CONSENSUS",
    }


def window_rows_from_cache(cfg: dict[str, object], cache_dir: Path) -> list[dict[str, object]]:
    search_cfg = cfg.get("subset_search", {}) if isinstance(cfg.get("subset_search"), dict) else {}
    spectral_config = str(search_cfg.get("search_spectral_config", "early_4096_256"))
    max_windows = int(search_cfg.get("max_search_windows", 16))
    boot = int(cfg["consensus"].get("bootstrap_samples", 200))
    min_eps = float(cfg["consensus"].get("cluster_eps_min", 0.0015))
    window_rows: list[dict[str, object]] = []
    cache_rows = [
        row
        for row in read_csv(cache_dir / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    ]
    for cache in cache_rows:
        spectra = np.load(cache["spectra_path"], mmap_mode="r")
        tune_axis = np.load(cache["tune_axis_path"])
        centers = np.load(cache["window_centers_path"])
        bpm_indices = np.load(cache["bpm_indices_path"])
        for widx, center in enumerate(centers[:max_windows]):
            group: list[dict[str, str]] = []
            for bpos, bpm_index in enumerate(bpm_indices):
                candidates = extract_candidates(np.asarray(spectra[bpos, widx], dtype=np.float32), tune_axis, cache["plane"], cfg)
                for candidate in candidates:
                    if int(candidate.get("candidate_rank", 0)) != 1:
                        continue
                    group.append(
                        {
                            "bpm_index": str(int(bpm_index)),
                            "peak_tune": str(candidate.get("peak_tune", "")),
                            "peak_prominence_z": str(candidate.get("peak_prominence_z", "")),
                            "second_peak_ratio": str(candidate.get("second_peak_ratio", "")),
                            "distance_to_band_edge": str(candidate.get("distance_to_band_edge", "")),
                            "valid_candidate": str(candidate.get("valid_candidate", "")),
                        }
                    )
            key = (cache["collection"], cache["spill_id"], cache["plane"], cache["spectral_config"], str(widx), str(float(center)))
            window_rows.append(consensus_window_row(cfg, key, group, boot, min_eps))
    return window_rows


def window_rows_from_feature_csv(cfg: dict[str, object], features_dir: Path) -> list[dict[str, object]]:
    rows = [row for row in read_csv(features_dir / "per_bpm_window_features.csv") if row.get("candidate_rank") == "1"]
    grouped: dict[tuple[str, str, str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["collection"], row["spill_id"], row["plane"], row["spectral_config"], row["window_index"], row["center_turn"])].append(row)
    window_rows: list[dict[str, object]] = []
    boot = int(cfg["consensus"].get("bootstrap_samples", 200))
    min_eps = float(cfg["consensus"].get("cluster_eps_min", 0.0015))
    for key, group in grouped.items():
        window_rows.append(consensus_window_row(cfg, key, group, boot, min_eps))
    return window_rows


def build_consensus(cfg: dict[str, object], features_dir: Path, out: Path, cache_dir: Path | None = None) -> None:
    if cache_dir is not None and (cache_dir / "index" / "spectral_cache.csv").exists():
        window_rows = window_rows_from_cache(cfg, cache_dir)
    else:
        window_rows = window_rows_from_feature_csv(cfg, features_dir)
    summary_rows = summarize_consensus(window_rows)
    class_counts = Counter(row.get("consensus_label", "") for row in window_rows)
    write_csv(out / "spill_consensus_windows.csv", window_rows, CONSENSUS_WINDOW_FIELDS)
    write_csv(out / "spill_consensus_summary.csv", summary_rows, CONSENSUS_SUMMARY_FIELDS)
    write_csv(out / "consensus_class_counts.csv", [{"consensus_label": key, "count": value} for key, value in sorted(class_counts.items())], ["consensus_label", "count"])
    atomic_write_text(out / "consensus_summary.md", f"# Consensus Summary\n\n- window rows: `{len(window_rows)}`\n- spill-plane rows: `{len(summary_rows)}`\n")


def summarize_consensus(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))].append(row)
    output = []
    for key, group in grouped.items():
        tunes = [_f(row.get("consensus_tune")) for row in group]
        tunes = [v for v in tunes if v is not None]
        uncs = [_f(row.get("consensus_uncertainty")) for row in group]
        uncs = [v for v in uncs if v is not None]
        counts = Counter(str(row.get("consensus_label", "")) for row in group)
        total = max(1, len(group))
        label = counts.most_common(1)[0][0] if counts else "NO_CONSENSUS"
        output.append(
            {
                "collection": key[0],
                "spill_id": key[1],
                "plane": key[2],
                "dominant_consensus_tune": statistics.median(tunes) if tunes else "",
                "median_consensus_uncertainty": statistics.median(uncs) if uncs else "",
                "clean_window_fraction": counts["CLEAN_CONSENSUS"] / total,
                "weak_window_fraction": counts["WEAK_CONSENSUS"] / total,
                "multimodal_window_fraction": counts["MULTIMODAL"] / total,
                "no_consensus_window_fraction": counts["NO_CONSENSUS"] / total,
                "consensus_label": label,
            }
        )
    output.sort(key=lambda r: (str(r["collection"]), str(r["spill_id"]), str(r["plane"])))
    return output
