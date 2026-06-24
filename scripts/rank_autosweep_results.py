#!/usr/bin/env python3
"""Rank staged BPM autosweep results and label configs/spills."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence


SCORE_WEIGHTS = {
    "injection_score": 0.25,
    "ridge_score": 0.25,
    "bpm_robustness_score": 0.20,
    "spectrogram_quality_score": 0.15,
    "usable_fraction_score": 0.10,
    "compute_efficiency_score": 0.05,
}

SPILL_SCORE_FIELDS = [
    "config_hash",
    "collection_view",
    "collection",
    "spill_id",
    "target_ms",
    "plane",
    "spill_label",
    "overall_score",
    "poster_score",
    "physics_score",
    "compute_score",
    "injection_score",
    "ridge_score",
    "bpm_robustness_score",
    "spectrogram_quality_score",
    "usable_fraction_score",
    "compute_efficiency_score",
    "injection_tune",
    "injection_anchor",
    "injection_anchor_distance",
    "injection_confidence",
    "median_tune",
    "tune_std",
    "finite_fraction",
    "p95_abs_step",
    "band_edge_fraction",
    "dominant_bpm_fraction",
    "odd_even_delta",
    "first_second_half_delta",
    "subset_delta",
    "ridge_prominence",
    "sliding_windows",
    "quality_flags",
    "warnings",
    "job_out_dir",
]

CONFIG_SCORE_FIELDS = [
    "config_hash",
    "collection_view",
    "plane",
    "config_label",
    "overall_score",
    "poster_score",
    "physics_score",
    "compute_score",
    "injection_score",
    "ridge_score",
    "bpm_robustness_score",
    "spectrogram_quality_score",
    "usable_fraction_score",
    "compute_efficiency_score",
    "spill_count",
    "good_spills",
    "marginal_spills",
    "bad_spills",
    "missing_data_spills",
    "no_signal_spills",
    "ambiguous_ridge_spills",
    "rejection_reason",
    "config_name",
    "stage",
    "turn_range",
    "turn_start",
    "turn_end",
    "window",
    "stride",
    "spectrogram_method",
    "multitaper_nw",
    "multitaper_k",
    "bpm_combination",
    "bpm_normalization",
    "detrend",
    "dc_handling",
    "ridge_method",
    "ridge_jump_penalty",
    "ridge_jump2_penalty",
    "ridge_max_step",
    "ridge_anchor_enabled",
    "tune_band",
    "qx_min",
    "qx_max",
    "qy_min",
    "qy_max",
    "enable_tracking",
]

COLLECTION_SCORE_FIELDS = [
    "collection_view",
    "plane",
    "best_config_hash",
    "best_config_label",
    "overall_score",
    "poster_score",
    "physics_score",
    "usable_fraction_score",
    "spill_count",
    "good_spills",
    "marginal_spills",
    "bad_spills",
]

REJECTED_FIELDS = [
    "config_hash",
    "collection_view",
    "plane",
    "config_label",
    "rejection_reason",
    "overall_score",
    "usable_fraction_score",
    "band_edge_fraction",
    "p95_abs_step",
    "config_name",
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


def parse_float(value: object, default: float | None = None) -> float | None:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        out = float(text)
    except ValueError:
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def parse_int(value: object, default: int = 0) -> int:
    number = parse_float(value)
    if number is None:
        return default
    return int(round(number))


def fmt(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.6f}"


def finite(values: Iterable[float | None]) -> list[float]:
    return [value for value in values if value is not None and math.isfinite(value)]


def mean(values: Iterable[float | None]) -> float | None:
    vals = finite(values)
    if not vals:
        return None
    return statistics.fmean(vals)


def median(values: Iterable[float | None]) -> float | None:
    vals = finite(values)
    if not vals:
        return None
    return statistics.median(vals)


def clamp01(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def inverse_linear(value: float | None, good: float, bad: float) -> float:
    if value is None:
        return 0.0
    if bad <= good:
        return 0.0
    if value <= good:
        return 1.0
    if value >= bad:
        return 0.0
    return 1.0 - (value - good) / (bad - good)


def direct_linear(value: float | None, bad: float, good: float) -> float:
    if value is None:
        return 0.0
    if good <= bad:
        return 0.0
    if value <= bad:
        return 0.0
    if value >= good:
        return 1.0
    return (value - bad) / (good - bad)


def row_key(row: dict[str, str], plane: str) -> str:
    suffix = "x" if plane == "H" else "y"
    return f"q{suffix}"


def anchor_for_plane(plane: str) -> float:
    return 0.65 if plane == "H" else 0.72


def config_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row.get("config_hash", ""): row for row in rows if row.get("config_hash")}


def run_rows(autosweep_dir: Path) -> list[dict[str, str]]:
    rows = read_csv(autosweep_dir / "autosweep_run_log.csv")
    return [row for row in rows if row.get("status") in {"ok", "cached"}]


def compute_efficiency_by_job(rows: list[dict[str, str]]) -> dict[str, float]:
    rates = []
    by_job: dict[str, float] = {}
    for row in rows:
        elapsed = parse_float(row.get("elapsed_seconds"), 0.0) or 0.0
        spills = parse_int(row.get("spill_count"), 0)
        if elapsed > 0 and spills > 0:
            rate = spills / elapsed
            rates.append(rate)
            by_job[row.get("out_dir", "")] = rate
    max_rate = max(rates) if rates else 0.0
    if max_rate <= 0:
        return {row.get("out_dir", ""): 1.0 for row in rows}
    return {job: clamp01(rate / max_rate) for job, rate in by_job.items()}


def score_spill(row: dict[str, str], run: dict[str, str], config: dict[str, str], plane: str, efficiency: float) -> dict[str, object]:
    suffix = "h" if plane == "H" else "v"
    q_key = row_key(row, plane)
    tune = parse_float(row.get(f"{q_key}_injection"))
    anchor = anchor_for_plane(plane)
    distance = abs(tune - anchor) if tune is not None else None
    confidence = parse_float(row.get(f"confidence_{suffix}"))
    finite_fraction = parse_float(row.get(f"finite_fraction_{suffix}"), 0.0)
    p95_step = parse_float(row.get(f"p95_abs_step_{suffix}"))
    band_edge_fraction = parse_float(row.get(f"band_edge_fraction_{suffix}"), 1.0)
    dominant_bpm_fraction = parse_float(row.get(f"dominant_bpm_fraction_{suffix}"), 1.0)
    odd_even_delta = parse_float(row.get(f"odd_even_delta_{suffix}"))
    first_second_half_delta = parse_float(row.get(f"first_second_half_delta_{suffix}"))
    subset_delta = parse_float(row.get(f"subset_delta_{suffix}"))
    ridge_prominence = parse_float(row.get(f"ridge_prominence_{suffix}"))
    windows = parse_int(row.get(f"sliding_windows_{suffix}"), 0)
    suspicious = parse_int(row.get(f"sliding_suspicious_count_{suffix}"), 0)
    fallback = parse_int(row.get(f"sliding_fallback_count_{suffix}"), 0)
    missing_seed = parse_int(row.get(f"sliding_missing_seed_count_{suffix}"), 0)
    median_tune = parse_float(row.get("median_qx" if plane == "H" else "median_qy"))
    tune_std = parse_float(row.get("std_qx" if plane == "H" else "std_qy"))
    usable = str(row.get("usable_for_analysis", "")).lower() == "true"

    anchor_score = inverse_linear(distance, 0.005, 0.050)
    confidence_score = direct_linear(confidence, 1.5, 8.0)
    injection_score = 0.60 * anchor_score + 0.40 * confidence_score

    smooth_score = inverse_linear(p95_step, 0.0015, 0.020)
    edge_score = inverse_linear(band_edge_fraction, 0.02, 0.25)
    finite_score = clamp01(finite_fraction)
    suspect_fraction = (suspicious + fallback + missing_seed) / max(1, windows)
    suspect_score = inverse_linear(suspect_fraction, 0.05, 0.40)
    ridge_score = 0.35 * smooth_score + 0.25 * edge_score + 0.25 * finite_score + 0.15 * suspect_score

    dominant_score = inverse_linear(dominant_bpm_fraction, 0.30, 0.90)
    robustness_step_score = inverse_linear(tune_std, 0.0015, 0.020)
    subset_score = inverse_linear(subset_delta, 0.0015, 0.020) if subset_delta is not None else 0.5
    bpm_robustness_score = 0.45 * dominant_score + 0.30 * robustness_step_score + 0.25 * subset_score

    prominence_score = direct_linear(ridge_prominence, 0.2, 5.0)
    spectrogram_quality_score = 0.55 * prominence_score + 0.25 * finite_score + 0.20 * edge_score
    usable_fraction_score = 1.0 if usable and windows > 0 else 0.0
    compute_efficiency_score = clamp01(efficiency)

    components = {
        "injection_score": injection_score,
        "ridge_score": ridge_score,
        "bpm_robustness_score": bpm_robustness_score,
        "spectrogram_quality_score": spectrogram_quality_score,
        "usable_fraction_score": usable_fraction_score,
        "compute_efficiency_score": compute_efficiency_score,
    }
    overall = sum(SCORE_WEIGHTS[key] * components[key] for key in SCORE_WEIGHTS)
    poster_score = (
        0.35 * spectrogram_quality_score
        + 0.25 * ridge_score
        + 0.20 * bpm_robustness_score
        + 0.15 * injection_score
        + 0.05 * compute_efficiency_score
    )
    physics_score = (
        0.35 * injection_score
        + 0.30 * ridge_score
        + 0.25 * bpm_robustness_score
        + 0.10 * usable_fraction_score
    )
    compute_score = compute_efficiency_score
    label = spill_label(row, usable, windows, tune, confidence, overall, band_edge_fraction, p95_step)
    return {
        "config_hash": run.get("config_hash", ""),
        "collection_view": run.get("collection_view", ""),
        "collection": row.get("run_name", ""),
        "spill_id": Path(row.get("bundle_dir", "")).name,
        "target_ms": row.get("target_ms", ""),
        "plane": plane,
        "spill_label": label,
        "overall_score": fmt(overall),
        "poster_score": fmt(poster_score),
        "physics_score": fmt(physics_score),
        "compute_score": fmt(compute_score),
        "injection_score": fmt(injection_score),
        "ridge_score": fmt(ridge_score),
        "bpm_robustness_score": fmt(bpm_robustness_score),
        "spectrogram_quality_score": fmt(spectrogram_quality_score),
        "usable_fraction_score": fmt(usable_fraction_score),
        "compute_efficiency_score": fmt(compute_efficiency_score),
        "injection_tune": fmt(tune),
        "injection_anchor": fmt(anchor),
        "injection_anchor_distance": fmt(distance),
        "injection_confidence": fmt(confidence),
        "median_tune": fmt(median_tune),
        "tune_std": fmt(tune_std),
        "finite_fraction": fmt(finite_fraction),
        "p95_abs_step": fmt(p95_step),
        "band_edge_fraction": fmt(band_edge_fraction),
        "dominant_bpm_fraction": fmt(dominant_bpm_fraction),
        "odd_even_delta": fmt(odd_even_delta),
        "first_second_half_delta": fmt(first_second_half_delta),
        "subset_delta": fmt(subset_delta),
        "ridge_prominence": fmt(ridge_prominence),
        "sliding_windows": windows,
        "quality_flags": row.get("quality_flags", ""),
        "warnings": row.get("warnings", ""),
        "job_out_dir": run.get("out_dir", ""),
        **{field: config.get(field, "") for field in CONFIG_SCORE_FIELDS if field in config},
    }


def spill_label(
    row: dict[str, str],
    usable: bool,
    windows: int,
    tune: float | None,
    confidence: float | None,
    score: float,
    band_edge_fraction: float | None,
    p95_step: float | None,
) -> str:
    flags = row.get("quality_flags", "")
    if "missing_h" in flags or "missing_v" in flags:
        return "MISSING_DATA"
    if not usable or windows == 0:
        return "MISSING_DATA"
    if tune is None or confidence is None or confidence < 1.5:
        return "NO_SIGNAL"
    if (band_edge_fraction or 0.0) > 0.30 or (p95_step or 0.0) > 0.025:
        return "AMBIGUOUS_RIDGE"
    if score >= 0.75:
        return "GOOD"
    if score >= 0.50:
        return "MARGINAL"
    return "BAD"


def aggregate_config_rows(
    spill_scores: list[dict[str, object]],
    configs: dict[str, dict[str, str]],
    min_spills: int,
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in spill_scores:
        groups[(str(row["config_hash"]), str(row["collection_view"]), str(row["plane"]))].append(row)
    out = []
    for (config_hash, view, plane), rows in sorted(groups.items()):
        cfg = configs.get(config_hash, {})
        labels = defaultdict(int)
        for row in rows:
            labels[str(row.get("spill_label", ""))] += 1
        component_means = {
            key: mean(parse_float(row.get(key)) for row in rows)
            for key in [
                "injection_score",
                "ridge_score",
                "bpm_robustness_score",
                "spectrogram_quality_score",
                "usable_fraction_score",
                "compute_efficiency_score",
                "poster_score",
                "physics_score",
                "compute_score",
            ]
        }
        overall = sum(SCORE_WEIGHTS[key] * (component_means[key] or 0.0) for key in SCORE_WEIGHTS)
        rejection_reason = config_rejection_reason(rows, labels, min_spills)
        label = config_label(overall, component_means, labels, rejection_reason, plane)
        out.append(
            {
                "config_hash": config_hash,
                "collection_view": view,
                "plane": plane,
                "config_label": label,
                "overall_score": fmt(overall),
                "poster_score": fmt(component_means["poster_score"]),
                "physics_score": fmt(component_means["physics_score"]),
                "compute_score": fmt(component_means["compute_score"]),
                "injection_score": fmt(component_means["injection_score"]),
                "ridge_score": fmt(component_means["ridge_score"]),
                "bpm_robustness_score": fmt(component_means["bpm_robustness_score"]),
                "spectrogram_quality_score": fmt(component_means["spectrogram_quality_score"]),
                "usable_fraction_score": fmt(component_means["usable_fraction_score"]),
                "compute_efficiency_score": fmt(component_means["compute_efficiency_score"]),
                "spill_count": len(rows),
                "good_spills": labels["GOOD"],
                "marginal_spills": labels["MARGINAL"],
                "bad_spills": labels["BAD"],
                "missing_data_spills": labels["MISSING_DATA"],
                "no_signal_spills": labels["NO_SIGNAL"],
                "ambiguous_ridge_spills": labels["AMBIGUOUS_RIDGE"],
                "rejection_reason": rejection_reason,
                **{field: cfg.get(field, "") for field in CONFIG_SCORE_FIELDS if field in cfg},
            }
        )
    out.sort(key=lambda row: (parse_float(row.get("overall_score"), 0.0) or 0.0), reverse=True)
    return out


def config_rejection_reason(rows: list[dict[str, object]], labels: dict[str, int], min_spills: int) -> str:
    if len(rows) < min_spills:
        return "TOO_FEW_SPILLS"
    usable = mean(parse_float(row.get("usable_fraction_score")) for row in rows) or 0.0
    if usable < 0.35:
        return "LOW_USABLE_FRACTION"
    band_edge = mean(parse_float(row.get("band_edge_fraction")) for row in rows) or 0.0
    if band_edge > 0.30:
        return "OVERFITS_BAND"
    p95_step = mean(parse_float(row.get("p95_abs_step")) for row in rows) or 0.0
    if p95_step > 0.025:
        return "UNSTABLE_RIDGE"
    subset_delta = mean(parse_float(row.get("subset_delta")) for row in rows) or 0.0
    if subset_delta > 0.025:
        return "UNSTABLE_RIDGE"
    if labels["NO_SIGNAL"] / max(1, len(rows)) > 0.50:
        return "NO_SIGNAL"
    return ""


def config_label(
    overall: float,
    components: dict[str, float | None],
    labels: dict[str, int],
    rejection_reason: str,
    plane: str,
) -> str:
    if rejection_reason == "OVERFITS_BAND":
        return "OVERFITS_BAND"
    if rejection_reason == "UNSTABLE_RIDGE":
        return "UNSTABLE_H" if plane == "H" else "UNSTABLE_V"
    if rejection_reason:
        return "REJECTED"
    if (components.get("compute_efficiency_score") or 0.0) < 0.15:
        return "TOO_SLOW"
    total = max(1, sum(labels.values()))
    good_fraction = labels["GOOD"] / total
    if overall >= 0.75 and good_fraction >= 0.50:
        return "RECOMMENDED"
    if overall >= 0.60:
        return "PROMISING"
    if overall >= 0.45:
        return "EXPLORATORY"
    return "REJECTED"


def aggregate_collections(config_scores: list[dict[str, object]]) -> list[dict[str, object]]:
    best: dict[tuple[str, str], dict[str, object]] = {}
    for row in config_scores:
        key = (str(row.get("collection_view", "")), str(row.get("plane", "")))
        score = parse_float(row.get("overall_score"), 0.0) or 0.0
        if key not in best or score > (parse_float(best[key].get("overall_score"), 0.0) or 0.0):
            best[key] = row
    rows = []
    for (view, plane), row in sorted(best.items()):
        rows.append(
            {
                "collection_view": view,
                "plane": plane,
                "best_config_hash": row.get("config_hash", ""),
                "best_config_label": row.get("config_label", ""),
                "overall_score": row.get("overall_score", ""),
                "poster_score": row.get("poster_score", ""),
                "physics_score": row.get("physics_score", ""),
                "usable_fraction_score": row.get("usable_fraction_score", ""),
                "spill_count": row.get("spill_count", ""),
                "good_spills": row.get("good_spills", ""),
                "marginal_spills": row.get("marginal_spills", ""),
                "bad_spills": row.get("bad_spills", ""),
            }
        )
    return rows


def load_spill_scores(args: argparse.Namespace, runs: list[dict[str, str]], configs: dict[str, dict[str, str]]) -> list[dict[str, object]]:
    efficiency = compute_efficiency_by_job(runs)
    out: list[dict[str, object]] = []
    for run in runs:
        summary = Path(run.get("out_dir", "")) / "gpu_spills_summary.csv"
        if not summary.exists():
            continue
        config = configs.get(run.get("config_hash", ""), {})
        for row in read_csv(summary):
            for plane in ("H", "V"):
                if args.plane != "both" and args.plane != plane:
                    continue
                mode = row.get("plane_mode", "both")
                if mode not in {"both", plane}:
                    continue
                out.append(score_spill(row, run, config, plane, efficiency.get(run.get("out_dir", ""), 1.0)))
    out.sort(
        key=lambda row: (
            parse_float(row.get("overall_score"), 0.0) or 0.0,
            parse_float(row.get("physics_score"), 0.0) or 0.0,
        ),
        reverse=True,
    )
    return out


def write_top_config_list(path: Path, config_scores: list[dict[str, object]], per_plane: int) -> None:
    selected: dict[str, dict[str, object]] = {}
    for plane in ("H", "V"):
        candidates = [row for row in config_scores if row.get("plane") == plane and row.get("collection_view") == "combined"]
        candidates.sort(key=lambda row: parse_float(row.get("physics_score"), 0.0) or 0.0, reverse=True)
        for row in candidates[:per_plane]:
            selected[str(row["config_hash"])] = row
    poster = [row for row in config_scores if row.get("collection_view") == "combined"]
    poster.sort(key=lambda row: parse_float(row.get("poster_score"), 0.0) or 0.0, reverse=True)
    for row in poster[:per_plane]:
        selected[str(row["config_hash"])] = row
    fields = CONFIG_SCORE_FIELDS
    write_csv(path, list(selected.values()), fields)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autosweep-dir", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--min-spills", type=int, default=2)
    parser.add_argument("--top-per-plane", type=int, default=5)
    parser.add_argument("--plane", choices=["H", "V", "both"], default="both")
    args = parser.parse_args(argv)

    autosweep_dir = Path(args.autosweep_dir)
    out_dir = Path(args.out) if args.out else autosweep_dir
    runs = run_rows(autosweep_dir)
    configs = config_map(read_csv(autosweep_dir / "autosweep_config_grid.csv"))
    spill_scores = load_spill_scores(args, runs, configs)
    config_scores = aggregate_config_rows(spill_scores, configs, args.min_spills)
    collection_scores = aggregate_collections(config_scores)
    rejected = [
        row
        for row in config_scores
        if str(row.get("config_label", "")) in {"REJECTED", "TOO_SLOW", "OVERFITS_BAND", "UNSTABLE_H", "UNSTABLE_V"}
    ]

    write_csv(out_dir / "autosweep_spill_scores.csv", spill_scores, SPILL_SCORE_FIELDS)
    write_csv(out_dir / "autosweep_config_scores.csv", config_scores, CONFIG_SCORE_FIELDS)
    write_csv(out_dir / "autosweep_collection_scores.csv", collection_scores, COLLECTION_SCORE_FIELDS)
    write_csv(out_dir / "autosweep_ranked_configs.csv", config_scores, CONFIG_SCORE_FIELDS)
    write_csv(out_dir / "autosweep_ranked_spills.csv", spill_scores, SPILL_SCORE_FIELDS)
    write_csv(out_dir / "autosweep_rejected_configs.csv", rejected, REJECTED_FIELDS)
    write_top_config_list(out_dir / "top_configs_for_full.csv", config_scores, args.top_per_plane)
    print(f"ranked {len(spill_scores)} spill-plane rows and {len(config_scores)} config-plane rows")


if __name__ == "__main__":
    main()
