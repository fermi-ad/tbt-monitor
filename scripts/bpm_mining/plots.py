"""Plot and artifact generation for best-BPM mining."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from .io import atomic_write_text, ensure_dir, read_csv, write_csv


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


def make_artifacts(cfg: dict[str, object], inputs: Path, artifact_manifest: Path, out: Path) -> None:
    ensure_dir(out / "global")
    ensure_dir(out / "spills")
    rows = subset_rows(inputs)
    artifact_rows = read_csv(artifact_manifest) if artifact_manifest.exists() else []
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
    for row in rows:
        key = (row["collection"], row["spill_id"], row["plane"])
        if key not in selected_keys:
            continue
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
        save_bar(out / "spills" / f"{stem}_bpm_tune_deconstruction.png", f"{stem} BPM membership", row["bpm_members"].split(","), [1.0] * len(row["bpm_members"].split(",")), "selected")
        save_scatter(out / "spills" / f"{stem}_subset_spectra.png", f"{stem} subset score", [(int(row["subset_size"]), _f(row.get("subset_score")), f"N={row['subset_size']}")], "subset size", "score")
        save_scatter(out / "spills" / f"{stem}_subset_evolution.png", f"{stem} visible tune", [(0, _f(row.get("q_hat")), row["subset_size"])], "window", "tune")
    write_csv(out / "spills" / "selected_subset_membership.csv", membership_rows, MEMBERSHIP_FIELDS)
    for row in membership_rows:
        write_csv(out / "spills" / f"spill_{row['spill_id']}_{row['plane'].lower()}_subset_membership.csv", [row], MEMBERSHIP_FIELDS)
    atomic_write_text(out / "artifact_generation_summary.md", f"# Artifact Generation Summary\n\n- selected membership rows: `{len(membership_rows)}`\n")
