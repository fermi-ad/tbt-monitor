#!/usr/bin/env python3
"""Summarize an elite full-data autosweep run and collate heavy artifacts."""

from __future__ import annotations

import argparse
import csv
import math
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence


POSTER_UNSAFE_LABELS = {"REJECTED", "TOO_SLOW", "UNSTABLE_H", "UNSTABLE_V", "OVERFITS_BAND"}

ARTIFACT_FIELDS = [
    "rank",
    "role",
    "plane",
    "config_hash",
    "collection_view",
    "source",
    "copied_to",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        out = float(text) if text else default
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def safe_rows(rows: list[dict[str, str]], plane: str | None = None) -> list[dict[str, str]]:
    out = [row for row in rows if row.get("collection_view") == "combined"]
    if not out:
        out = list(rows)
    if plane is not None:
        out = [row for row in out if row.get("plane") == plane]
    safe = [row for row in out if row.get("config_label") not in POSTER_UNSAFE_LABELS]
    return safe or out


def best_row(rows: list[dict[str, str]], fields: Sequence[str]) -> dict[str, str] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: tuple(parse_float(row.get(field)) for field in fields), reverse=True)[0]


def best_poster_config(rows: list[dict[str, str]]) -> dict[str, object] | None:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in safe_rows(rows):
        grouped[row.get("config_hash", "")].append(row)
    best: dict[str, object] | None = None
    for config_hash, group in grouped.items():
        poster_score = sum(parse_float(row.get("poster_score")) for row in group) / max(1, len(group))
        physics_score = sum(parse_float(row.get("physics_score")) for row in group) / max(1, len(group))
        row = group[0]
        candidate: dict[str, object] = {
            **row,
            "config_hash": config_hash,
            "plane": ",".join(sorted({item.get("plane", "") for item in group if item.get("plane")})),
            "poster_score": f"{poster_score:.6f}",
            "physics_score": f"{physics_score:.6f}",
        }
        if best is None or poster_score > parse_float(best.get("poster_score")):
            best = candidate
    return best


def table(rows: Sequence[dict[str, object] | None], columns: Sequence[str]) -> list[str]:
    clean = [row for row in rows if row]
    if not clean:
        return ["No rows."]
    out = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in clean:
        out.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return out


def run_out_dirs(run_rows: list[dict[str, str]]) -> dict[tuple[str, str], Path]:
    return {
        (row.get("config_hash", ""), row.get("collection_view", "")): Path(row.get("out_dir", ""))
        for row in run_rows
        if row.get("status") in {"ok", "cached"} and row.get("out_dir")
    }


def artifact_names() -> set[str]:
    return {
        "injection_tune_reproducibility.png",
        "ridge_density_h.png",
        "ridge_density_v.png",
        "method_comparison_h.png",
        "method_comparison_v.png",
        "spectrogram_method_compare_h.png",
        "spectrogram_method_compare_v.png",
        "gpu_median_spectrogram_h.png",
        "gpu_median_spectrogram_v.png",
        "single_spill_spectrogram_h.png",
        "single_spill_spectrogram_v.png",
        "ridge_overlay_h.png",
        "ridge_overlay_v.png",
        "bpm_leaderboard_h.png",
        "bpm_leaderboard_v.png",
        "subset_consistency_h.png",
        "subset_consistency_v.png",
        "gpu_median_tune_vs_spill.png",
    }


def collect_artifacts(out: Path, run_rows: list[dict[str, str]], selected_rows: Sequence[dict[str, object] | None]) -> list[dict[str, object]]:
    gallery = out / "poster_candidate_gallery"
    gallery.mkdir(parents=True, exist_ok=True)
    dirs = run_out_dirs(run_rows)
    rows: list[dict[str, object]] = []
    wanted = artifact_names()
    seen: set[tuple[str, str, str]] = set()
    rank = 0
    for selected in selected_rows:
        if not selected:
            continue
        config_hash = str(selected.get("config_hash", ""))
        view = str(selected.get("collection_view", "combined")) or "combined"
        job_dir = dirs.get((config_hash, view)) or dirs.get((config_hash, "combined"))
        if job_dir is None or not job_dir.exists():
            continue
        rank += 1
        role = str(selected.get("summary_role", "candidate"))
        plane = str(selected.get("plane", ""))
        for source in sorted(path for path in job_dir.glob("*.png") if path.name in wanted):
            key = (config_hash, role, source.name)
            if key in seen:
                continue
            seen.add(key)
            dest = gallery / f"{rank:02d}_{role}_{config_hash[:8]}_{source.name}"
            try:
                shutil.copy2(source, dest)
            except OSError:
                continue
            rows.append(
                {
                    "rank": rank,
                    "role": role,
                    "plane": plane,
                    "config_hash": config_hash,
                    "collection_view": view,
                    "source": str(source),
                    "copied_to": str(dest),
                }
            )
    return rows


def write_summary(
    path: Path,
    config_rows: list[dict[str, str]],
    run_rows: list[dict[str, str]],
    dataset_rows: list[dict[str, str]],
    source_rows: list[dict[str, str]],
    rejected_rows: list[dict[str, str]],
    artifact_rows: list[dict[str, object]],
    selections: Sequence[dict[str, object] | None],
) -> None:
    statuses = Counter(row.get("status", "") for row in run_rows)
    h_best, v_best, h_robust, v_robust, poster = selections
    lines = [
        "# Elite Full-Data Autosweep Summary",
        "",
        "BPM-only elite full-data pass over usable Tier A raw position bundles.",
        "",
        "## Coverage",
        "",
        f"- usable input spills: `{len(dataset_rows)}`",
        f"- ranked config-plane rows: `{len(config_rows)}`",
        f"- selected role rows: `{len(source_rows)}`",
        f"- retained rejected/flagged config diagnostics: `{len(rejected_rows)}`",
        f"- copied elite artifacts: `{len(artifact_rows)}`",
        f"- run status counts: `{dict(statuses)}`",
        "",
        "## Best Configs",
        "",
        *table(
            [h_best, v_best, h_robust, v_robust, poster],
            ["summary_role", "config_hash", "plane", "config_label", "physics_score", "poster_score", "bpm_robustness_score", "window", "stride", "bpm_combination"],
        ),
        "",
        "## Caveats",
        "",
        "- These rankings are BPM-only and do not include Schottky/reference validation.",
        "- Poster-safe summaries exclude `REJECTED`, `TOO_SLOW`, `UNSTABLE_H`, `UNSTABLE_V`, and `OVERFITS_BAND` labels.",
        "- Per-collection rows remain diagnostic; final selections prefer `collection_view=combined`.",
        "- BPM leaderboard and subset-consistency artifacts are diagnostics for method stability, not independent physics truth.",
        "",
        "## Outputs",
        "",
        "- `elite_full_summary.md`",
        "- `elite_artifacts_manifest.csv`",
        "- `poster_candidate_gallery/`",
        "- full autosweep ranked CSVs from `rank_autosweep_results.py`",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elite-dir", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    elite = Path(args.elite_dir)
    out = Path(args.out) if args.out else elite
    out.mkdir(parents=True, exist_ok=True)

    configs = read_csv(elite / "autosweep_ranked_configs.csv")
    runs = read_csv(elite / "autosweep_run_log.csv")
    dataset = read_csv(elite / "elite_dataset_manifest.csv")
    sources = read_csv(elite / "elite_config_sources.csv")
    rejected = read_csv(elite / "elite_rejected_config_diagnostics.csv")

    h_best = best_row(safe_rows(configs, "H"), ["physics_score"])
    v_best = best_row(safe_rows(configs, "V"), ["physics_score"])
    h_robust = best_row([row for row in safe_rows(configs, "H") if row.get("bpm_combination") == "top10_by_confidence"], ["bpm_robustness_score", "physics_score"])
    v_robust = best_row([row for row in safe_rows(configs, "V") if row.get("bpm_combination") == "top10_by_confidence"], ["bpm_robustness_score", "physics_score"])
    poster = best_poster_config(configs)
    selections: list[dict[str, object] | None] = [
        dict(h_best, summary_role="best_h") if h_best else None,
        dict(v_best, summary_role="best_v") if v_best else None,
        dict(h_robust, summary_role="best_robust_h") if h_robust else None,
        dict(v_robust, summary_role="best_robust_v") if v_robust else None,
        dict(poster, summary_role="best_poster") if poster else None,
    ]

    artifact_rows = collect_artifacts(out, runs, selections)
    write_csv(out / "elite_artifacts_manifest.csv", artifact_rows, ARTIFACT_FIELDS)
    write_summary(out / "elite_full_summary.md", configs, runs, dataset, sources, rejected, artifact_rows, selections)
    print(f"wrote {out / 'elite_full_summary.md'}")


if __name__ == "__main__":
    main()
