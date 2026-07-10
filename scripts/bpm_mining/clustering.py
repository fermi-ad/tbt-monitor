"""Unsupervised spill morphology clustering with a stdlib/NumPy fallback."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .identity import manifest_by_index, normalize_subset_row
from .io import atomic_write_text, read_csv, write_csv


CLUSTER_FIELDS = ["collection", "spill_id", "cluster_id", "cluster_label", "feature_vector"]
SUMMARY_FIELDS = ["cluster_id", "spill_count", "tags", "median_score_h", "median_score_v"]
RANK_FIELDS = ["cluster_id", "plane", "bpm_name", "inclusion_count"]


def _f(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _median(values: list[float]) -> float | str:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return ""
    middle = len(finite) // 2
    return finite[middle] if len(finite) % 2 else 0.5 * (finite[middle - 1] + finite[middle])


def _preferred_score(features: dict[str, float], plane: str) -> float:
    for size in ("5", "3", "1", "10"):
        value = features.get(f"{plane}_score_{size}", math.nan)
        if math.isfinite(value):
            return value
    return math.nan


def kmeans(x: np.ndarray, k: int, seed: int) -> np.ndarray:
    rng = random.Random(seed)
    centers = x[rng.sample(range(x.shape[0]), min(k, x.shape[0]))].copy()
    if centers.shape[0] < k:
        return np.zeros((x.shape[0],), dtype=int)
    labels = np.zeros((x.shape[0],), dtype=int)
    for _ in range(30):
        dist = np.sum((x[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels = np.argmin(dist, axis=1)
        for idx in range(k):
            members = x[labels == idx]
            if members.size:
                centers[idx] = np.mean(members, axis=0)
    return labels


def cluster_spills(cfg: dict[str, object], inputs: Path, out: Path) -> None:
    consensus_path = inputs / "consensus" / "spill_consensus_summary.csv"
    meta_by_index = manifest_by_index(read_csv(inputs / "manifest" / "bpm_index.csv"))
    subsets = []
    for size in (1, 3, 5, 10):
        path = inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if path.exists():
            subsets.extend(normalize_subset_row(row, meta_by_index) for row in read_csv(path))
    by_spill = defaultdict(dict)
    for row in read_csv(consensus_path) if consensus_path.exists() else []:
        by_spill[(row["collection"], row["spill_id"])][f"{row['plane']}_consensus"] = _f(row.get("dominant_consensus_tune"))
    for row in subsets:
        by_spill[(row["collection"], row["spill_id"])][f"{row['plane']}_score_{row['subset_size']}"] = _f(row.get("subset_score"))
        by_spill[(row["collection"], row["spill_id"])][f"{row['plane']}_visible_{row['subset_size']}"] = _f(row.get("visible_fraction"))
    keys = sorted(by_spill)
    feature_names = sorted({name for vals in by_spill.values() for name in vals})
    matrix = []
    for key in keys:
        matrix.append([by_spill[key].get(name, math.nan) for name in feature_names])
    if not matrix:
        write_csv(out / "spill_clusters.csv", [], CLUSTER_FIELDS)
        write_csv(out / "cluster_summary.csv", [], SUMMARY_FIELDS)
        write_csv(out / "cluster_bpm_rankings.csv", [], RANK_FIELDS)
        return
    x = np.asarray(matrix, dtype=np.float64)
    usable_columns = np.any(np.isfinite(x), axis=0)
    if not np.any(usable_columns):
        raise ValueError("clustering feature matrix has no finite columns")
    x = x[:, usable_columns]
    col_med = np.nanmedian(x, axis=0)
    x = np.where(np.isfinite(x), x, col_med)
    col_scale = np.nanstd(x, axis=0)
    x = (x - col_med) / np.where(col_scale > 0, col_scale, 1.0)
    k = min(6, max(2, int(math.sqrt(len(keys)))))
    labels = kmeans(x, k, int(cfg["runtime"].get("random_seed", 20260614)))
    cluster_rows = []
    for key, label, vector in zip(keys, labels, matrix):
        cluster_rows.append({"collection": key[0], "spill_id": key[1], "cluster_id": f"CLUSTER_{label}", "cluster_label": f"CLUSTER_{label}", "feature_vector": ";".join("" if not math.isfinite(v) else f"{v:.6g}" for v in vector)})
    summary_rows = []
    for label in sorted(set(labels)):
        members = [row for row in cluster_rows if row["cluster_id"] == f"CLUSTER_{label}"]
        tags = []
        if len(members) < max(5, 0.02 * len(cluster_rows)):
            tags.append("SMALL")
        member_features = [
            by_spill[(str(row["collection"]), str(row["spill_id"]))]
            for row in members
        ]
        summary_rows.append(
            {
                "cluster_id": f"CLUSTER_{label}",
                "spill_count": len(members),
                "tags": ",".join(tags),
                "median_score_h": _median([_preferred_score(values, "H") for values in member_features]),
                "median_score_v": _median([_preferred_score(values, "V") for values in member_features]),
            }
        )
    bpm_counts = Counter()
    cluster_lookup = {(row["collection"], row["spill_id"]): row["cluster_id"] for row in cluster_rows}
    for row in subsets:
        cluster = cluster_lookup.get((row["collection"], row["spill_id"]))
        if not cluster:
            continue
        for bpm in row.get("bpm_members", "").split(","):
            if bpm:
                bpm_counts[(cluster, row["plane"], bpm)] += 1
    rank_rows = [{"cluster_id": c, "plane": p, "bpm_name": b, "inclusion_count": n} for (c, p, b), n in sorted(bpm_counts.items())]
    write_csv(out / "spill_clusters.csv", cluster_rows, CLUSTER_FIELDS)
    write_csv(out / "cluster_summary.csv", summary_rows, SUMMARY_FIELDS)
    write_csv(out / "cluster_bpm_rankings.csv", rank_rows, RANK_FIELDS)
    atomic_write_text(out / "clustering_summary.md", f"# Spill Morphology Clustering\n\n- spills clustered: `{len(cluster_rows)}`\n- clusters: `{len(summary_rows)}`\n")
