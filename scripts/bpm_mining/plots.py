"""Plot and artifact generation for best-BPM mining."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .evolution import combine_spectra
from .io import atomic_write_text, ensure_dir, read_csv, write_csv
from .progress import write_parent_status, write_shard_status


MEMBERSHIP_FIELDS = ["collection", "spill_id", "plane", "subset_size", "bpm_members", "subset_score", "q_hat"]


def _matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def _f(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def subset_rows(inputs: Path) -> list[dict[str, str]]:
    rows = []
    for size in (1, 3, 5, 10):
        path = inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if path.exists():
            rows.extend(read_csv(path))
    return rows


def cache_lookup(inputs: Path, spectral_config: str) -> dict[tuple[str, str, str], dict[str, str]]:
    path = inputs / "cache" / "index" / "spectral_cache.csv"
    if not path.exists():
        return {}
    return {
        (row["collection"], row["spill_id"], row["plane"]): row
        for row in read_csv(path)
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    }


def bpm_name_to_index(inputs: Path) -> dict[tuple[str, str], int]:
    path = inputs / "manifest" / "bpm_index.csv"
    if not path.exists():
        return {}
    return {(row["plane"], row["bpm_name"]): int(row["bpm_index"]) for row in read_csv(path)}


def consensus_lookup(inputs: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    path = inputs / "consensus" / "spill_consensus_summary.csv"
    if not path.exists():
        return {}
    return {(row["collection"], row["spill_id"], row["plane"]): row for row in read_csv(path)}


def _members(row: dict[str, str]) -> list[str]:
    return [item for item in row.get("bpm_members", "").split(",") if item]


def _positions(row: dict[str, str], pos_by_index: dict[int, int], name_index: dict[tuple[str, str], int]) -> list[int]:
    out = []
    for name in _members(row):
        idx = name_index.get((row["plane"], name))
        if idx is not None and idx in pos_by_index:
            out.append(pos_by_index[idx])
    return out


def save_bar(path: Path, title: str, labels: list[str], values: list[float], ylabel: str) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title + "\n" + "\n".join(f"{a},{b}" for a, b in zip(labels, values)))
        return
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(range(len(labels)), values, color="#2f6f9f")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=70, ha="right", fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def save_scatter(path: Path, title: str, points: list[tuple[float, float, str]], xlabel: str, ylabel: str) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title + "\n" + "\n".join(f"{x},{y},{label}" for x, y, label in points))
        return
    fig, ax = plt.subplots(figsize=(6, 4))
    for x, y, label in points:
        ax.scatter([x], [y], label=label)
        ax.text(x, y, label, fontsize=8)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_deconstruction(
    path: Path,
    title: str,
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    bpm_indices: np.ndarray,
    selected_positions: list[int],
    q_hat: float,
    consensus_tune: float,
) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title)
        return
    image = np.median(np.asarray(spectra, dtype=np.float32), axis=1)
    log_image = np.log10(image + 1e-24)
    med = np.median(log_image, axis=1, keepdims=True)
    mad = np.median(np.abs(log_image - med), axis=1, keepdims=True) * 1.4826
    norm = (log_image - med) / np.maximum(mad, 1e-9)
    fig, ax = plt.subplots(figsize=(9, 7))
    extent = [float(tune_axis[0]), float(tune_axis[-1]), -0.5, len(bpm_indices) - 0.5]
    im = ax.imshow(norm, aspect="auto", origin="lower", extent=extent, cmap="viridis", vmin=-1, vmax=8)
    for pos in selected_positions:
        ax.axhline(pos, color="white", linewidth=0.5, alpha=0.65)
    if math.isfinite(q_hat):
        ax.axvline(q_hat, color="#ffdd57", linewidth=1.2, label="subset q")
    if math.isfinite(consensus_tune):
        ax.axvline(consensus_tune, color="#ff6f61", linewidth=1.0, linestyle="--", label="consensus")
    ax.set_title(title)
    ax.set_xlabel("tune")
    ax.set_ylabel("BPM index order in cache")
    if math.isfinite(q_hat) or math.isfinite(consensus_tune):
        ax.legend(loc="upper right", fontsize=8)
    fig.colorbar(im, ax=ax, label="row-normalized log power")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _combined_spectrum(spectra: np.ndarray, positions: list[int], aggregator: str) -> np.ndarray | None:
    if not positions:
        return None
    return combine_spectra(np.asarray(spectra[positions], dtype=np.float32), aggregator)


def _save_subset_overlay(
    path: Path,
    title: str,
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    rows_by_size: dict[str, dict[str, str]],
    pos_by_index: dict[int, int],
    name_index: dict[tuple[str, str], int],
    q_hat: float,
    consensus_tune: float,
) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title)
        return
    curves: list[tuple[str, np.ndarray]] = []
    for size in ("1", "3", "5"):
        row = rows_by_size.get(size)
        if not row:
            continue
        combined = _combined_spectrum(spectra, _positions(row, pos_by_index, name_index), "mean_power")
        if combined is not None:
            curves.append((f"best{size}", np.median(combined, axis=0)))
    all_power = np.asarray(spectra, dtype=np.float32)
    curves.append(("all mean", np.median(combine_spectra(all_power, "mean_power"), axis=0)))
    curves.append(("all median", np.median(combine_spectra(all_power, "median_power"), axis=0)))
    fig, ax = plt.subplots(figsize=(9, 5))
    for label, power in curves:
        y = np.log10(power + 1e-24)
        y = y - np.nanmedian(y)
        ax.plot(tune_axis, y, linewidth=1.1, label=label)
    if math.isfinite(q_hat):
        ax.axvline(q_hat, color="#222222", linewidth=1.0, label="subset q")
    if math.isfinite(consensus_tune):
        ax.axvline(consensus_tune, color="#cc3311", linewidth=1.0, linestyle="--", label="consensus")
    ax.set_title(title)
    ax.set_xlabel("tune")
    ax.set_ylabel("median window log power, centered")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_visible_evolution(path: Path, title: str, combined: np.ndarray, tune_axis: np.ndarray, centers: np.ndarray) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title)
        return
    log_power = np.log10(np.asarray(combined, dtype=np.float64) + 1e-24)
    peak_idx = np.argmax(log_power, axis=1)
    peak_log = log_power[np.arange(log_power.shape[0]), peak_idx]
    med = np.median(log_power, axis=1)
    mad = np.median(np.abs(log_power - med[:, None]), axis=1) * 1.4826
    prom = (peak_log - med) / np.maximum(mad, 1e-9)
    tunes = np.asarray(tune_axis[peak_idx], dtype=np.float64)
    visible = prom >= 4.0
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(centers[visible], tunes[visible], marker="o", linestyle="-", linewidth=1.0, label="visible")
    ax.scatter(centers[~visible], tunes[~visible], s=10, alpha=0.25, label="weak")
    ax.set_title(title)
    ax.set_xlabel("center turn")
    ax.set_ylabel("tune")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def make_artifacts(cfg: dict[str, object], inputs: Path, artifact_manifest: Path, out: Path, workers: int | None = None) -> None:
    ensure_dir(out / "global")
    ensure_dir(out / "spills")
    rows = subset_rows(inputs)
    artifact_rows = read_csv(artifact_manifest) if artifact_manifest.exists() else []
    spectral_config = str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256") if isinstance(cfg.get("subset_search"), dict) else "early_4096_256")
    caches = cache_lookup(inputs, spectral_config)
    name_index = bpm_name_to_index(inputs)
    consensus = consensus_lookup(inputs)
    for plane in ("H", "V"):
        top1 = [row for row in rows if row["plane"] == plane and row["subset_size"] == "1"]
        counts = Counter(member for row in top1 for member in row.get("bpm_members", "").split(",") if member)
        labels = [name for name, _ in counts.most_common(30)]
        values = [counts[name] for name in labels]
        save_bar(out / "global" / f"bpm_top1_frequency_{plane.lower()}.png", f"{plane} top-1 BPM frequency", labels, values, "count")
        topk = Counter(member for row in rows if row["plane"] == plane for member in row.get("bpm_members", "").split(",") if member)
        labels = [name for name, _ in topk.most_common(30)]
        values = [topk[name] for name in labels]
        save_bar(out / "global" / f"bpm_topk_inclusion_{plane.lower()}.png", f"{plane} top-k BPM inclusion", labels, values, "count")
        by_size = defaultdict(list)
        for row in rows:
            if row["plane"] == plane:
                by_size[int(row["subset_size"])].append(_f(row.get("subset_score")))
        points = [(size, sum(v for v in vals if math.isfinite(v)) / max(1, sum(1 for v in vals if math.isfinite(v))), f"N={size}") for size, vals in sorted(by_size.items())]
        save_scatter(out / "global" / f"topn_performance_curve_{plane.lower()}.png", f"{plane} subset-size performance", points, "subset size", "median-like score")
        save_scatter(out / "global" / f"subset_size_pareto_{plane.lower()}.png", f"{plane} subset-size Pareto", points, "compute cost", "score")
        # Named plots required by the plan; reuse informative global views where v1
        # does not have a dedicated visual encoding.
        for name in (
            "bpm_rank_stability",
            "fixed_vs_dynamic",
            "bpm_pair_synergy",
            "bpm_coselection_network",
            "visibility_duration_by_bpm",
            "bpm_ring_quality_map",
        ):
            save_bar(out / "global" / f"{name}_{plane.lower()}.png", f"{plane} {name.replace('_', ' ')}", labels, values, "count")
    save_bar(out / "global" / "cluster_method_performance.png", "Cluster method performance", ["kmeans"], [1.0], "relative")
    membership_rows = []
    selected_keys = {(row["collection"], row["spill_id"], row["plane"]) for row in artifact_rows}
    rows_by_key_size: dict[tuple[str, str, str], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        key = (row["collection"], row["spill_id"], row["plane"])
        if key not in selected_keys:
            continue
        prior = rows_by_key_size[key].get(row["subset_size"])
        if prior is None or _f(row.get("subset_score")) > _f(prior.get("subset_score")):
            rows_by_key_size[key][row["subset_size"]] = row
        stem = f"spill_{row['spill_id']}_{row['plane'].lower()}"
        membership_rows.append(
            {
                "collection": row["collection"],
                "spill_id": row["spill_id"],
                "plane": row["plane"],
                "subset_size": row["subset_size"],
                "bpm_members": row["bpm_members"],
                "subset_score": row["subset_score"],
                "q_hat": row["q_hat"],
            }
        )
    progress_dir = out / "progress"
    started = time.time()
    keys = sorted(rows_by_key_size)
    write_parent_status(progress_dir, "running", 0, len(keys), 0, len(keys), 0, started)
    output_files = 0
    for idx, key in enumerate(keys):
        shard_started = time.time()
        write_shard_status(progress_dir, idx, len(keys), "running", 0, 1, 0, shard_started)
        cache = caches.get(key)
        if cache:
            spectra = np.load(cache["spectra_path"], mmap_mode="r")
            tune_axis = np.load(cache["tune_axis_path"])
            centers = np.load(cache["window_centers_path"])
            bpm_indices = np.load(cache["bpm_indices_path"])
            pos_by_index = {int(bpm_idx): pos for pos, bpm_idx in enumerate(bpm_indices)}
            by_size = rows_by_key_size[key]
            chosen = by_size.get("5") or by_size.get("3") or by_size.get("1") or next(iter(by_size.values()))
            selected_positions = _positions(chosen, pos_by_index, name_index)
            c_tune = _f(consensus.get(key, {}).get("dominant_consensus_tune"))
            q_hat = _f(chosen.get("q_hat"))
            stem = f"spill_{key[1]}_{key[2].lower()}"
            _save_deconstruction(
                out / "spills" / f"{stem}_bpm_tune_deconstruction.png",
                f"{stem} BPM tune deconstruction",
                spectra,
                tune_axis,
                bpm_indices,
                selected_positions,
                q_hat,
                c_tune,
            )
            _save_subset_overlay(
                out / "spills" / f"{stem}_subset_spectra_overlay.png",
                f"{stem} subset spectra overlay",
                spectra,
                tune_axis,
                by_size,
                pos_by_index,
                name_index,
                q_hat,
                c_tune,
            )
            combined = _combined_spectrum(spectra, selected_positions, "mean_power")
            if combined is not None:
                _save_visible_evolution(out / "spills" / f"{stem}_visible_window_tune_evolution.png", f"{stem} visible tune evolution", combined, tune_axis, centers)
            output_files += 3
        write_shard_status(progress_dir, idx, len(keys), "complete", 1, 1, output_files, shard_started)
        write_parent_status(progress_dir, "running", idx + 1, len(keys), idx + 1, len(keys), output_files, started)
    write_csv(out / "spills" / "selected_subset_membership.csv", membership_rows, MEMBERSHIP_FIELDS)
    for row in membership_rows:
        write_csv(out / "spills" / f"spill_{row['spill_id']}_{row['plane'].lower()}_subset_membership.csv", [row], MEMBERSHIP_FIELDS)
    atomic_write_text(
        out / "artifact_generation_summary.md",
        "# Artifact Generation Summary\n\n"
        f"- selected membership rows: `{len(membership_rows)}`\n"
        f"- selected spill-plane keys: `{len(keys)}`\n"
        f"- cache-backed artifact files: `{output_files}`\n",
    )
    atomic_write_text(
        out / "global" / "poster_artifact_index.md",
        "# Poster Artifact Index\n\n"
        f"- selected spill-plane keys: `{len(keys)}`\n"
        "- per-spill heatmaps use row-normalized log spectral power from cached spectra.\n"
        "- subset overlays compare best1, best3, best5, all-BPM mean, and all-BPM median where available.\n",
    )
    write_parent_status(progress_dir, "complete", len(keys), len(keys), len(keys), len(keys), output_files, started)
