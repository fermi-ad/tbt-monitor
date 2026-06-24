"""Select a bounded set of informative spills for expensive artifacts."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .io import atomic_write_text, read_csv, write_csv


FIELDS = ["collection", "spill_id", "plane", "category", "score", "reason"]


def _score(row: dict[str, str], field: str) -> float:
    try:
        return float(row.get(field, "") or 0.0)
    except ValueError:
        return 0.0


def select_artifacts(cfg: dict[str, object], inputs: Path, out: Path) -> None:
    max_per_plane = int(cfg["artifacts"].get("max_spills_per_plane", 40))
    subset_rows = []
    for size in (1, 3, 5, 10):
        path = inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if path.exists():
            subset_rows.extend(read_csv(path))
    consensus_path = inputs / "consensus" / "spill_consensus_summary.csv"
    consensus_rows = read_csv(consensus_path) if consensus_path.exists() else []
    selected: dict[tuple[str, str, str], dict[str, object]] = {}

    def add(row, category, score, reason):
        key = (row["collection"], row["spill_id"], row["plane"])
        if key not in selected:
            selected[key] = {"collection": key[0], "spill_id": key[1], "plane": key[2], "category": category, "score": score, "reason": reason}

    for plane in ("H", "V"):
        clean = [row for row in consensus_rows if row["plane"] == plane and row.get("consensus_label") == "CLEAN_CONSENSUS"]
        for row in clean[:5]:
            add(row, "clean_consensus", row.get("clean_window_fraction", ""), "top clean consensus")
        for size, category in (("1", "best1"), ("3", "best3_improvement"), ("5", "best5_improvement"), ("10", "best10_improvement")):
            rows = sorted([row for row in subset_rows if row["plane"] == plane and row["subset_size"] == size], key=lambda r: _score(r, "subset_score"), reverse=True)
            for row in rows[:5]:
                add(row, category, row.get("subset_score", ""), f"top subset size {size}")
        multimodal = [row for row in consensus_rows if row["plane"] == plane and row.get("consensus_label") == "MULTIMODAL"]
        for row in multimodal[:5]:
            add(row, "multimodal", row.get("multimodal_window_fraction", ""), "multimodal consensus")
        low = sorted([row for row in subset_rows if row["plane"] == plane], key=lambda r: _score(r, "subset_score"))
        for row in low[:5]:
            add(row, "low_signal_failure", row.get("subset_score", ""), "low subset score")
    by_plane = defaultdict(list)
    for row in selected.values():
        by_plane[row["plane"]].append(row)
    final = []
    for plane, rows in by_plane.items():
        final.extend(sorted(rows, key=lambda r: str(r["category"]))[:max_per_plane])
    write_csv(out / "artifact_manifest.csv", final, FIELDS)
    atomic_write_text(out / "artifact_selection_summary.md", f"# Artifact Selection Summary\n\n- selected spill-plane rows: `{len(final)}`\n")
