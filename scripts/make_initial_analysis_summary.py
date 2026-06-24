#!/usr/bin/env python3
"""Write the first-pass autosweep analysis summary and lightweight plots."""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path
from typing import Sequence

from bpm_dgx_poster import BLUE, GREEN, ORANGE, bar_plot, hist_plot, no_data_png


ARTIFACT_FIELDS = [
    "rank",
    "kind",
    "plane",
    "config_hash",
    "collection_view",
    "score",
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
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def top_configs(rows: list[dict[str, str]], plane: str, metric: str, limit: int) -> list[dict[str, str]]:
    candidates = [row for row in rows if row.get("plane") == plane and row.get("collection_view") == "combined"]
    if not candidates:
        candidates = [row for row in rows if row.get("plane") == plane]
    candidates.sort(key=lambda row: parse_float(row.get(metric)), reverse=True)
    return candidates[:limit]


def top_poster_configs(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    candidates = [row for row in rows if row.get("collection_view") == "combined"]
    if not candidates:
        candidates = list(rows)
    candidates.sort(key=lambda row: parse_float(row.get("poster_score")), reverse=True)
    return candidates[:limit]


def top_spills(rows: list[dict[str, str]], plane: str, limit: int) -> list[dict[str, str]]:
    candidates = [row for row in rows if row.get("plane") == plane]
    candidates.sort(key=lambda row: parse_float(row.get("physics_score")), reverse=True)
    return candidates[:limit]


def labels_and_values(rows: Sequence[dict[str, str]], score_field: str) -> tuple[list[str], list[float]]:
    labels = [str(row.get("config_hash", ""))[:8] for row in rows]
    values = [parse_float(row.get(score_field)) for row in rows]
    return labels, values


def make_plots(out: Path, config_rows: list[dict[str, str]], spill_rows: list[dict[str, str]], top_n: int) -> None:
    plots = out / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    top_overall = sorted(config_rows, key=lambda row: parse_float(row.get("overall_score")), reverse=True)[:top_n]
    labels, values = labels_and_values(top_overall, "overall_score")
    bar_plot(plots / "ranked_config_scoreboard.png", "AUTOSWEEP CONFIG SCOREBOARD", labels, values, "SCORE")

    hist_plot(
        plots / "config_score_distribution.png",
        "CONFIG SCORE DISTRIBUTION",
        [parse_float(row.get("overall_score")) for row in config_rows],
        "SCORE",
        bins=24,
        x_range=(0.0, 1.0),
        color=BLUE,
    )
    hist_plot(
        plots / "spill_physics_score_distribution.png",
        "SPILL PHYSICS SCORE DISTRIBUTION",
        [parse_float(row.get("physics_score")) for row in spill_rows],
        "SCORE",
        bins=24,
        x_range=(0.0, 1.0),
        color=GREEN,
    )
    for plane in ("H", "V"):
        configs = top_configs(config_rows, plane, "physics_score", top_n)
        labels, values = labels_and_values(configs, "physics_score")
        suffix = plane.lower()
        bar_plot(plots / f"top_{suffix}_physics_configs.png", f"TOP {plane} PHYSICS CONFIGS", labels, values, "SCORE")
        hist_plot(
            plots / f"anchor_distance_{suffix}.png",
            f"{plane} ANCHOR DISTANCE",
            [parse_float(row.get("injection_anchor_distance")) for row in spill_rows if row.get("plane") == plane],
            "TUNE",
            bins=24,
            x_range=(0.0, 0.08),
            color=ORANGE,
        )
    poster = top_poster_configs(config_rows, top_n)
    labels, values = labels_and_values(poster, "poster_score")
    bar_plot(plots / "top_poster_configs.png", "TOP POSTER CONFIGS", labels, values, "SCORE")

    if not config_rows:
        no_data_png(plots / "ranked_config_scoreboard.png", "AUTOSWEEP CONFIG SCOREBOARD")
    if not spill_rows:
        no_data_png(plots / "spill_physics_score_distribution.png", "SPILL PHYSICS SCORE DISTRIBUTION")


def collect_artifacts(out: Path, spill_rows: list[dict[str, str]], top_n: int) -> list[dict[str, object]]:
    artifact_dir = out / "top_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    selected = top_spills(spill_rows, "H", top_n) + top_spills(spill_rows, "V", top_n)
    rows: list[dict[str, object]] = []
    names = [
        "spectrogram_h.png",
        "spectrogram_v.png",
        "tune_vs_time.png",
        "tune_validation.png",
        "method_comparison_h.png",
        "method_comparison_v.png",
    ]
    seen: set[tuple[str, str]] = set()
    rank = 0
    for spill in selected:
        key = (spill.get("config_hash", ""), spill.get("target_ms", ""))
        if key in seen:
            continue
        seen.add(key)
        rank += 1
        job_dir = Path(spill.get("job_out_dir", ""))
        target = str(spill.get("target_ms", ""))
        spill_index = str(spill.get("spill_id", "")).replace("spill_", "")
        candidates = list(job_dir.rglob(f"*{target}*.png")) if job_dir.exists() and target else []
        if not candidates and job_dir.exists() and spill_index:
            candidates = list(job_dir.rglob(f"*{spill_index}*.png"))
        if not candidates and job_dir.exists():
            candidates = [path for path in (job_dir / "plots").glob("*.png") if path.name in names]
        for source in candidates[:6]:
            dest = artifact_dir / f"{rank:02d}_{spill.get('plane','')}_{spill.get('config_hash','')[:8]}_{source.name}"
            try:
                shutil.copy2(source, dest)
            except OSError:
                continue
            rows.append(
                {
                    "rank": rank,
                    "kind": source.stem,
                    "plane": spill.get("plane", ""),
                    "config_hash": spill.get("config_hash", ""),
                    "collection_view": spill.get("collection_view", ""),
                    "score": spill.get("physics_score", ""),
                    "source": str(source),
                    "copied_to": str(dest),
                }
            )
    return rows


def table(rows: Sequence[dict[str, str]], columns: Sequence[str], limit: int) -> list[str]:
    if not rows:
        return ["No rows."]
    out = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows[:limit]:
        out.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return out


def write_summary(
    path: Path,
    config_rows: list[dict[str, str]],
    spill_rows: list[dict[str, str]],
    collection_rows: list[dict[str, str]],
    artifact_rows: list[dict[str, object]],
    top_n: int,
) -> None:
    h = top_configs(config_rows, "H", "physics_score", top_n)
    v = top_configs(config_rows, "V", "physics_score", top_n)
    poster = top_poster_configs(config_rows, top_n)
    lines = [
        "# Initial Autosweep Analysis Summary",
        "",
        "This is a BPM-only Spark autosweep summary. It ranks candidate tune-tracking configurations against captured raw position bundles; it does not use Schottky or external reference labels.",
        "",
        "## Coverage",
        "",
        f"- ranked config-plane rows: `{len(config_rows)}`",
        f"- ranked spill-plane rows: `{len(spill_rows)}`",
        f"- collection-plane rows: `{len(collection_rows)}`",
        f"- copied top artifacts: `{len(artifact_rows)}`",
        "",
        "## Best H Configs",
        "",
        *table(h, ["config_hash", "config_label", "physics_score", "poster_score", "window", "stride", "spectrogram_method", "bpm_combination"], top_n),
        "",
        "## Best V Configs",
        "",
        *table(v, ["config_hash", "config_label", "physics_score", "poster_score", "window", "stride", "spectrogram_method", "bpm_combination"], top_n),
        "",
        "## Best Poster/Combined Configs",
        "",
        *table(poster, ["config_hash", "config_label", "poster_score", "physics_score", "plane", "window", "stride", "tune_band"], top_n),
        "",
        "## Outputs",
        "",
        "- `autosweep_ranked_configs.csv` ranks configuration/collection/plane rows.",
        "- `autosweep_ranked_spills.csv` ranks spill/config/plane rows.",
        "- `top_configs_for_full.csv` is the handoff list for the elite full-data stage.",
        "- `plots/` contains lightweight score distributions and top-config comparisons.",
        "- `top_artifacts/` contains copied GPU plots when they were generated by the underlying analyzer jobs.",
        "",
        "## Notes",
        "",
        "- The overall score uses the required weights: 0.25 injection, 0.25 ridge, 0.20 BPM robustness, 0.15 spectrogram quality, 0.10 usable fraction, 0.05 compute efficiency.",
        "- Broad bands are discovery only; poster candidates should be checked against medium/narrow bands around H 0.65 and V 0.72.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking-dir", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    ranking = Path(args.ranking_dir)
    out = Path(args.out) if args.out else ranking
    out.mkdir(parents=True, exist_ok=True)

    config_rows = read_csv(ranking / "autosweep_ranked_configs.csv")
    spill_rows = read_csv(ranking / "autosweep_ranked_spills.csv")
    collection_rows = read_csv(ranking / "autosweep_collection_scores.csv")
    make_plots(out, config_rows, spill_rows, args.top)
    artifact_rows = collect_artifacts(out, spill_rows, args.top)
    write_csv(out / "top_artifacts_manifest.csv", artifact_rows, ARTIFACT_FIELDS)
    write_summary(out / "initial_analysis_summary.md", config_rows, spill_rows, collection_rows, artifact_rows, args.top)
    print(f"wrote {out / 'initial_analysis_summary.md'}")


if __name__ == "__main__":
    main()
