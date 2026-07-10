#!/usr/bin/env python3
"""Summarize Best-BPM NEXT_STEPS sidecar analysis outputs."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from bpm_mining.best_n import recommended_n


MISSING_INPUTS: set[str] = set()
REPORT_WARNINGS: list[str] = []


def require_verification(path: Path, accepted: set[str]) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"required verification report is missing: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid verification report: {path}: {exc}") from exc
    status = str(report.get("status", "")).lower()
    if status not in accepted:
        raise ValueError(f"verification report is not accepted: {path}: status={status!r}")
    return report


def fnum(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        value_f = float(value)
    except ValueError:
        return None
    return value_f if math.isfinite(value_f) else None


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        MISSING_INPUTS.add(str(path))
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def iter_rows(path: Path):
    if not path.exists():
        MISSING_INPUTS.add(str(path))
        return
    with path.open(newline="") as handle:
        yield from csv.DictReader(handle)


def q(values: list[float], p: float) -> float | None:
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * p
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    frac = pos - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def fmt(value: object, digits: int = 4) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if abs(value) >= 1000:
            return f"{value:.0f}"
        return f"{value:.{digits}g}"
    return str(value)


def md_table(headers: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(fmt(cell) for cell in row) + " |")
    return out


def summarize_numeric(rows: list[dict[str, str]], field: str) -> dict[str, float | int | None]:
    vals = [fnum(row.get(field)) for row in rows]
    vals = [v for v in vals if v is not None]
    return {
        "n": len(vals),
        "p05": q(vals, 0.05),
        "p25": q(vals, 0.25),
        "median": median(vals) if vals else None,
        "p75": q(vals, 0.75),
        "p95": q(vals, 0.95),
    }


def summarize_by(rows: list[dict[str, str]], keys: list[str], fields: list[str]) -> list[dict[str, object]]:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key, "") for key in keys)].append(row)
    out: list[dict[str, object]] = []
    for key, vals in groups.items():
        item: dict[str, object] = {name: value for name, value in zip(keys, key)}
        item["rows"] = len(vals)
        for field in fields:
            stats = summarize_numeric(vals, field)
            item[f"{field}_median"] = stats["median"]
            item[f"{field}_p25"] = stats["p25"]
            item[f"{field}_p75"] = stats["p75"]
        out.append(item)
    return out


def fixed_score_contract_mismatches(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for row in rows:
        score = fnum(row.get("score"))
        visible = fnum(row.get("visible_fraction"))
        prominence = fnum(row.get("median_prominence"))
        if score is None or visible is None:
            mismatches.append(row)
            continue
        if prominence is None:
            if visible == 0.0 and score == 0.0 and "NO_VISIBLE_TUNE" in row.get("quality_flags", ""):
                continue
            mismatches.append(row)
            continue
        expected = visible * max(0.0, min(1.0, prominence / 12.0))
        if abs(score - expected) > 1e-6:
            mismatches.append(row)
    return mismatches


def top_bpm_table(root: Path, plane: str) -> list[list[object]]:
    rows = [row for row in read_rows(root / "statistics" / "bpm_global_statistics.csv") if row["plane"] == plane]
    rows.sort(key=lambda row: (-(fnum(row.get("top1_frequency")) or 0.0), -(fnum(row.get("top5_inclusion_frequency")) or 0.0)))
    return [
        [
            row["bpm_name"].replace("acsys_DeliveryRingBPM ", ""),
            row["digitizer"],
            fnum(row.get("top1_frequency")),
            fnum(row.get("top3_inclusion_frequency")),
            fnum(row.get("top5_inclusion_frequency")),
            fnum(row.get("median_score")),
        ]
        for row in rows[:10]
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Canonical Best-BPM run root")
    parser.add_argument("--followup", type=Path, required=True, help="NEXT_STEPS sidecar root")
    parser.add_argument("--best-n", type=Path, default=None, help="optional merged leakage-controlled Best-N root")
    parser.add_argument("--ridge", type=Path, default=None, help="optional full-buffer ridge-density output root")
    parser.add_argument("--intensity", type=Path, default=None, help="optional merged block-aware intensity-study root")
    parser.add_argument("--sensitivity", type=Path, action="append", default=[], help="optional Best-N sensitivity output root; repeat as needed")
    parser.add_argument("--out", type=Path, required=True, help="Markdown report path")
    args = parser.parse_args()

    root = args.root
    followup = args.followup

    verification_reports = [
        require_verification(root / "logs" / "best_bpm_verification.json", {"ok", "pass"}),
        require_verification(followup / "logs" / "best_bpm_followup_verification.json", {"ok", "pass"}),
    ]
    if args.best_n:
        verification_reports.append(require_verification(args.best_n / "best_n_verification.json", {"pass"}))
    if args.ridge:
        verification_reports.append(require_verification(args.ridge / "ridge_density_verification.json", {"pass"}))
    if args.intensity:
        verification_reports.append(require_verification(args.intensity / "intensity_verification.json", {"pass"}))

    fixed_summary = read_rows(followup / "statistics" / "fixed_vs_dynamic_direct_summary.csv")
    heldout_summary = read_rows(followup / "evolution" / "heldout_spectral_support_summary.csv")
    subset_size = read_rows(root / "evolution" / "subset_size_comparison.csv")
    paired = read_rows(root / "statistics" / "paired_method_tests.csv")
    rank_stability = read_rows(root / "statistics" / "bpm_rank_stability.csv")
    pareto = read_rows(root / "statistics" / "subset_size_pareto.csv")
    artifacts = read_rows(root / "artifact_selection" / "artifact_manifest.csv")
    best_results = {
        "1": read_rows(root / "subset_search" / "best1" / "best1_results.csv"),
        "3": read_rows(root / "subset_search" / "best3" / "best3_results.csv"),
        "5": read_rows(root / "subset_search" / "best5" / "best5_results.csv"),
    }
    cluster_summary = read_rows(root / "clustering" / "cluster_summary.csv")
    cluster_bpm_rankings = read_rows(root / "clustering" / "cluster_bpm_rankings.csv")

    fixed_eval = list(iter_rows(followup / "statistics" / "fixed_set_direct_evaluation.csv"))
    fixed_mismatches = fixed_score_contract_mismatches(fixed_eval)
    if fixed_mismatches:
        REPORT_WARNINGS.append(
            f"ignored `fixed_vs_dynamic_direct_summary.csv` because {len(fixed_mismatches)} "
            "direct-control rows mix incompatible score definitions; rerun the corrected fixed-set sidecar"
        )
        fixed_eval = []
        fixed_summary = []
    heldout = list(iter_rows(followup / "evolution" / "finalist_heldout_spectral_support.csv"))
    visibility = list(iter_rows(followup / "handoff" / "bpm_window_visibility.csv"))
    handoff_events = list(iter_rows(followup / "handoff" / "bpm_handoff_events.csv"))
    visibility_summary = list(iter_rows(followup / "handoff" / "bpm_visibility_summary.csv"))

    best_n_summary = read_rows(args.best_n / "best_n_summary.csv") if args.best_n else []
    best_n_transfer = read_rows(args.best_n / "best_n_cross_collection_transfer.csv") if args.best_n else []
    best_n_tune_half_width = 0.0025
    if args.best_n:
        best_n_contract = json.loads((args.best_n / "run_contract.json").read_text(encoding="utf-8"))
        best_n_tune_half_width = float(best_n_contract.get("tune_half_width", best_n_tune_half_width))
    sensitivity_recommendations: list[dict[str, str]] = []
    for sensitivity_root in args.sensitivity:
        run_manifest = read_rows(sensitivity_root / "sensitivity_run_manifest.csv")
        run_identities = {
            (row.get("beam_width", ""), row.get("fit_windows", ""), row.get("fold_seed", ""))
            for row in run_manifest
        }
        if len(run_manifest) != 7 or len(run_identities) != 7 or any(row.get("status") != "verified" for row in run_manifest):
            raise ValueError(
                f"Best-N sensitivity matrix must contain seven unique verified runs: {sensitivity_root}"
            )
        recommendation_files = sorted(sensitivity_root.rglob("best_n_*_recommendations.csv"))
        if not recommendation_files:
            MISSING_INPUTS.add(str(sensitivity_root / "best_n_*_recommendations.csv"))
        for path in recommendation_files:
            sensitivity_recommendations.extend(read_rows(path))
    ridge_legacy = read_rows(args.ridge / "ridge_density_legacy_comparison_metrics.csv") if args.ridge else []
    ridge_loss = read_rows(args.ridge / "ridge_density_loss_candidates.csv") if args.ridge else []
    ridge_manifest = read_rows(args.ridge / "ridge_density_best_ensemble_manifest.csv") if args.ridge else []
    intensity_effects = read_rows(args.intensity / "intensity_method_effects.csv") if args.intensity else []
    intensity_correlations = read_rows(args.intensity / "intensity_visibility_correlation_summary.csv") if args.intensity else []

    lines: list[str] = []
    lines.append("# NEXT_STEPS Output Analysis")
    lines.append("")
    lines.append("This report is computed from the verifier-clean canonical Best-BPM run plus the NEXT_STEPS sidecar outputs.")
    lines.append("")
    lines.append("## Evidence Set")
    evidence_rows = [
        ["accepted verification reports", len(verification_reports)],
        ["subset-size comparison", len(subset_size)],
        ["best1/3/5 winner rows", sum(len(rows) for rows in best_results.values())],
        ["paired method tests", len(paired)],
        ["fixed direct evaluation", len(fixed_eval)],
        ["fixed direct summary", len(fixed_summary)],
        ["held-out support", len(heldout)],
        ["held-out support summary", len(heldout_summary)],
        ["handoff window visibility", len(visibility)],
        ["handoff events", len(handoff_events)],
        ["handoff BPM summaries", len(visibility_summary)],
        ["selected artifact rows", len(artifacts)],
    ]
    if args.best_n:
        evidence_rows.extend([["Best-N summary rows", len(best_n_summary)], ["Best-N transfer rows", len(best_n_transfer)]])
    if args.sensitivity:
        evidence_rows.append(["Best-N sensitivity recommendation rows", len(sensitivity_recommendations)])
    if args.ridge:
        evidence_rows.extend([["ridge legacy comparison rows", len(ridge_legacy)], ["ridge loss rows", len(ridge_loss)], ["ridge figures", len(ridge_manifest)]])
    if args.intensity:
        evidence_rows.extend([["intensity effect tests", len(intensity_effects)], ["intensity correlation summary rows", len(intensity_correlations)]])
    lines.extend(md_table(["Surface", "Rows / files"], evidence_rows))
    if MISSING_INPUTS:
        lines.append("")
        lines.append("Missing optional/report inputs:")
        lines.extend(f"- `{path}`" for path in sorted(MISSING_INPUTS))
    if REPORT_WARNINGS:
        lines.append("")
        lines.append("Report guardrails:")
        lines.extend(f"- {warning}." for warning in REPORT_WARNINGS)
    lines.append("")

    lines.append("## Main Conclusions")
    dynamic_grouped = summarize_by(subset_size, ["plane", "subset_size"], ["subset_score"])
    dynamic_score = {
        (str(row["plane"]), str(row["subset_size"])): row["subset_score_median"]
        for row in dynamic_grouped
    }
    for plane in ("H", "V"):
        q_parts = []
        score_parts = []
        for size, rows in best_results.items():
            q_values = [fnum(row.get("q_hat")) for row in rows if row.get("plane") == plane]
            q_values = [value for value in q_values if value is not None]
            q_parts.append(f"Best-{size} {fmt(median(q_values) if q_values else None, 6)}")
            score_parts.append(f"Best-{size} {fmt(dynamic_score.get((plane, size)), 5)}")
        lines.append(
            f"- **{plane} adaptive-search medians:** tune candidates {', '.join(q_parts)}; "
            f"selection scores {', '.join(score_parts)}. Selection scores are not compared with the direct-control metric below."
        )

        control_plane = [
            row
            for row in fixed_summary
            if row.get("plane") == plane
        ]
        dynamic_controls = {
            (str(row.get("test_collection", "")), str(row.get("subset_size", ""))): row
            for row in control_plane
            if str(row.get("method", "")).startswith("dynamic_")
        }
        fixed_plane = [row for row in control_plane if str(row.get("method", "")).startswith("fixed_")]
        fixed_ranked = sorted(
            fixed_plane,
            key=lambda row: fnum(row.get("median_score")) if fnum(row.get("median_score")) is not None else -math.inf,
            reverse=True,
        )
        if fixed_ranked:
            best_fixed = fixed_ranked[0]
            matched_dynamic = dynamic_controls.get(
                (str(best_fixed.get("test_collection", "")), str(best_fixed.get("subset_size", "")))
            )
            lines.append(
                f"- **{plane} strongest same-metric frozen control:** "
                f"`{best_fixed.get('method', '')}` median score {fmt(fnum(best_fixed.get('median_score')), 5)} "
                f"on `{best_fixed.get('test_collection', '')}` versus matched adaptive "
                f"{fmt(fnum(matched_dynamic.get('median_score')), 5) if matched_dynamic else 'NA'}. "
                "This direct control is descriptive because adaptive membership selection reused these windows."
            )

        heldout_plane = [row for row in heldout_summary if row.get("plane") == plane]
        heldout_ranked = sorted(
            heldout_plane,
            key=lambda row: fnum(row.get("median_heldout_candidate_fraction"))
            if fnum(row.get("median_heldout_candidate_fraction")) is not None
            else -math.inf,
            reverse=True,
        )
        if heldout_ranked:
            best_heldout = heldout_ranked[0]
            lines.append(
                f"- **{plane} strongest held-out summary:** Best-{best_heldout.get('subset_size', '')} "
                f"`{best_heldout.get('aggregator', '')}` candidate fraction "
                f"{fmt(fnum(best_heldout.get('median_heldout_candidate_fraction')), 5)}, power support "
                f"{fmt(fnum(best_heldout.get('median_heldout_power_support')), 5)} over "
                f"{best_heldout.get('evaluable_row_count', '')}/{best_heldout.get('row_count', '')} evaluable rows."
            )

        top_rows = top_bpm_table(root, plane)
        if top_rows:
            lines.append(
                f"- **{plane} most frequent Best-1 contributor:** `{top_rows[0][0]}` with top-1 frequency "
                f"{fmt(top_rows[0][2], 5)}; recurrence is not a standalone operational validation."
            )

    event_counts_now = Counter(row.get("event_label", "") for row in handoff_events)
    lines.append(
        "- **Handoff labels are descriptive:** "
        + ", ".join(f"{label or 'UNLABELED'}={count}" for label, count in event_counts_now.most_common())
        + ". Thresholded event counts do not establish a physical handoff mechanism."
    )
    lines.append(
        "- **Scope:** these are BPM-only internal-consistency observations. A verifier-clean artifact tree does not provide an external tune reference."
    )
    lines.append("")
    consensus_counts = Counter()
    consensus_by_plane = Counter()
    for row in best_results["1"]:
        label = row.get("consensus_label", "")
        consensus_counts[label] += 1
        consensus_by_plane[(row.get("plane", ""), label)] += 1
    lines.append("Consensus class counts from best1 winner rows:")
    lines.extend(md_table(["Consensus label", "Rows"], [[key, value] for key, value in consensus_counts.most_common()]))
    lines.append("")
    lines.extend(md_table(["Plane", "Consensus label", "Rows"], [[plane, label, count] for (plane, label), count in sorted(consensus_by_plane.items())]))
    lines.append("")

    lines.append("## Dynamic Subset Size Effects")
    dyn = summarize_by(subset_size, ["plane", "subset_size"], ["subset_score", "visible_fraction", "visibility_duration_turns"])
    dyn.sort(key=lambda row: (row["plane"], int(row["subset_size"])))
    lines.extend(
        md_table(
            ["Plane", "N", "Rows", "Median score", "Median visible fraction", "Median visible turns"],
            [
                [
                    row["plane"],
                    row["subset_size"],
                    row["rows"],
                    row["subset_score_median"],
                    row["visible_fraction_median"],
                    row["visibility_duration_turns_median"],
                ]
                for row in dyn
            ],
        )
    )
    lines.append("")
    lines.append("Dynamic winner q_hat distributions:")
    tune_rows: list[list[object]] = []
    for size, rows in best_results.items():
        for plane in ("H", "V"):
            vals = [fnum(row.get("q_hat")) for row in rows if row.get("plane") == plane]
            vals = [v for v in vals if v is not None]
            tune_rows.append([plane, size, len(vals), q(vals, 0.05), q(vals, 0.25), median(vals) if vals else None, q(vals, 0.75), q(vals, 0.95)])
    lines.extend(md_table(["Plane", "N", "Valid q rows", "q05", "q25", "median", "q75", "q95"], tune_rows))
    lines.append("")
    lines.append("Paired subset-size tests:")
    lines.extend(
        md_table(
            ["Plane", "Comparison", "Median diff", "CI low", "CI high", "effect size"],
            [
                [
                    row["plane"],
                    row["comparison"],
                    fnum(row["median_paired_difference"]),
                    fnum(row["bootstrap_ci_low"]),
                    fnum(row["bootstrap_ci_high"]),
                    fnum(row["effect_size"]),
                ]
                for row in paired
            ],
        )
    )
    lines.append("")

    lines.append("## Direct Fixed-Set Vs Dynamic Evaluation")
    fixed_summary.sort(key=lambda row: (row["plane"], row["test_collection"], row["method"]))
    lines.extend(
        md_table(
            ["Plane", "Test collection", "Method", "N", "Median score", "Median visible fraction", "Median q_hat", "Median abs dynamic delta"],
            [
                [
                    row["plane"],
                    row["test_collection"].replace("tbt-capture-positiononly-1000-", ""),
                    row["method"],
                    row["subset_size"],
                    fnum(row["median_score"]),
                    fnum(row["median_visible_fraction"]),
                    fnum(row["median_q_hat"]),
                    fnum(row["median_abs_dynamic_delta"]),
                ]
                for row in fixed_summary
            ],
        )
    )
    lines.append("")
    fixed_q = summarize_by(fixed_eval, ["plane", "method"], ["score", "visible_fraction"])
    fixed_q.sort(key=lambda row: (row["plane"], -(row["score_median"] or -1.0), row["method"]))
    lines.append("Direct row-level median scores by method across both collections:")
    lines.extend(
        md_table(
            ["Plane", "Method", "Rows", "Median score", "Score p25", "Score p75", "Median visible frac"],
            [
                [row["plane"], row["method"], row["rows"], row["score_median"], row["score_p25"], row["score_p75"], row["visible_fraction_median"]]
                for row in fixed_q
            ],
        )
    )
    lines.append("")

    lines.append("## Held-Out Spectral Support")
    lines.append("`selected_vs_heldout_delta` is selected-subset power at q_hat minus non-selected BPM power at the same q_hat. Positive values mean the selected subset is stronger; negative values mean the non-selected BPMs are stronger.")
    lines.extend(
        md_table(
            ["Plane", "N", "Aggregator", "Rows", "Evaluable", "Median held-out candidate fraction", "Median held-out power support", "Median selected-heldout delta"],
            [
                [
                    row["plane"],
                    row["subset_size"],
                    row["aggregator"],
                    row["row_count"],
                    row.get("evaluable_row_count", ""),
                    fnum(row["median_heldout_candidate_fraction"]),
                    fnum(row["median_heldout_power_support"]),
                    fnum(row["median_selected_vs_heldout_delta"]),
                ]
                for row in heldout_summary
            ],
        )
    )
    heldout_groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in heldout:
        heldout_groups[(row["plane"], row["subset_size"], row["aggregator"])].append(row)
    heldout_rows: list[list[object]] = []
    for key, rows in sorted(heldout_groups.items()):
        q_valid = sum(1 for row in rows if fnum(row.get("q_hat")) is not None)
        frac_vals = [fnum(row.get("heldout_candidate_fraction")) for row in rows]
        delta_vals = [fnum(row.get("selected_vs_heldout_delta")) for row in rows]
        delta_vals_nn = [v for v in delta_vals if v is not None]
        heldout_rows.append(
            [
                key[0],
                key[1],
                key[2],
                len(rows),
                q_valid / len(rows) if rows else None,
                q([v for v in frac_vals if v is not None], 0.25),
                median([v for v in frac_vals if v is not None]) if any(v is not None for v in frac_vals) else None,
                q([v for v in frac_vals if v is not None], 0.75),
                sum(1 for v in delta_vals_nn if v > 0) / len(delta_vals_nn) if delta_vals_nn else None,
            ]
        )
    lines.append("")
    lines.append("Held-out row-level support distribution:")
    lines.extend(
        md_table(
            ["Plane", "N", "Aggregator", "Rows", "q_hat valid frac", "Candidate frac p25", "Candidate frac median", "Candidate frac p75", "selected > heldout frac"],
            heldout_rows,
        )
    )
    lines.append("")

    lines.append("## Global BPM Stability")
    lines.append("Top H BPMs:")
    lines.extend(md_table(["BPM", "Digitizer", "top1", "top3", "top5", "median score"], top_bpm_table(root, "H")))
    lines.append("")
    lines.append("Top V BPMs:")
    lines.extend(md_table(["BPM", "Digitizer", "top1", "top3", "top5", "median score"], top_bpm_table(root, "V")))
    lines.append("")
    lines.append("Collection-to-collection rank stability:")
    lines.extend(md_table(["Plane", "Metric", "Value"], [[row["plane"], row["metric"], fnum(row["value"])] for row in rank_stability]))
    lines.append("")

    lines.append("## Cluster Structure")
    lines.extend(md_table(["Cluster", "Spills", "Tags"], [[row["cluster_id"], row["spill_count"], row["tags"]] for row in cluster_summary]))
    lines.append("")
    cluster_top: dict[tuple[str, str], dict[str, str]] = {}
    for row in cluster_bpm_rankings:
        key = (row["cluster_id"], row["plane"])
        prev = cluster_top.get(key)
        if prev is None or int(row["inclusion_count"]) > int(prev["inclusion_count"]):
            cluster_top[key] = row
    lines.append("Top included BPM per cluster and plane:")
    lines.extend(
        md_table(
            ["Cluster", "Plane", "BPM", "Inclusion count"],
            [
                [cluster, plane, row["bpm_name"].replace("acsys_DeliveryRingBPM ", ""), row["inclusion_count"]]
                for (cluster, plane), row in sorted(cluster_top.items())
            ],
        )
    )
    lines.append("")

    lines.append("## Handoff And Visibility")
    class_counts = Counter(row["visibility_class"] for row in visibility)
    class_by_plane = Counter((row["plane"], row["visibility_class"]) for row in visibility)
    event_counts = Counter(row["event_label"] for row in handoff_events)
    event_by_plane_size = Counter((row["plane"], row["subset_size"], row["event_label"]) for row in handoff_events)
    top_counts = Counter()
    for row in visibility:
        for field in ("is_top1_visible", "is_top3_visible", "is_top5_visible", "is_top10_visible"):
            if row[field] == "true":
                top_counts[(row["plane"], field)] += 1
    lines.extend(
        md_table(
            ["Visibility class", "Rows", "Fraction"],
            [[key, value, value / len(visibility)] for key, value in class_counts.most_common()],
        )
    )
    lines.append("")
    lines.extend(
        md_table(
            ["Plane", "Visibility class", "Rows"],
            [[plane, label, count] for (plane, label), count in sorted(class_by_plane.items())],
        )
    )
    lines.append("")
    lines.extend(
        md_table(
            ["Event label", "Rows", "Fraction"],
            [[key, value, value / len(handoff_events)] for key, value in event_counts.most_common()],
        )
    )
    lines.append("")
    lines.extend(
        md_table(
            ["Plane", "Top set", "Event label", "Rows"],
            [[plane, f"Top-{size}", label, count] for (plane, size, label), count in sorted(event_by_plane_size.items())],
        )
    )
    lines.append("")
    total_by_plane = Counter(row["plane"] for row in visibility)
    lines.append("Fraction of per-BPM windows belonging to each strict visible Top-N set:")
    lines.extend(
        md_table(
            ["Plane", "Top set", "Visible row fraction"],
            [
                [plane, field.replace("is_", "").replace("_visible", ""), top_counts[(plane, field)] / total_by_plane[plane]]
                for plane in sorted(total_by_plane)
                for field in ("is_top1_visible", "is_top3_visible", "is_top5_visible", "is_top10_visible")
            ],
        )
    )
    lines.append("")
    bpm_vis: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in visibility_summary:
        key = (row["plane"], row["bpm_name"].replace("acsys_DeliveryRingBPM ", ""), row["digitizer"])
        for field in (
            "visible_window_fraction",
            "median_visibility_score",
            "median_support_at_consensus",
            "top1_window_fraction",
            "top3_window_fraction",
            "top5_window_fraction",
            "top10_window_fraction",
        ):
            val = fnum(row.get(field))
            if val is not None:
                bpm_vis[key][field].append(val)
    for plane in ("H", "V"):
        ranked = []
        for (row_plane, bpm, digitizer), values_by_field in bpm_vis.items():
            if row_plane == plane:
                score_vals = values_by_field.get("median_visibility_score", [])
                support_vals = values_by_field.get("median_support_at_consensus", [])
                top5_vals = values_by_field.get("top5_window_fraction", [])
                strict_vals = values_by_field.get("visible_window_fraction", [])
                ranked.append(
                    (
                        median(score_vals) if score_vals else 0.0,
                        median(support_vals) if support_vals else 0.0,
                        median(top5_vals) if top5_vals else 0.0,
                        median(strict_vals) if strict_vals else 0.0,
                        len(score_vals),
                        bpm,
                        digitizer,
                    )
                )
        ranked.sort(reverse=True)
        lines.append(f"Top {plane} BPMs in selected artifacts by weak visibility score:")
        lines.extend(
            md_table(
                ["BPM", "Digitizer", "Rows", "Median visibility score", "Median support", "Median top5 frac", "Strict visible frac"],
                [[bpm, digitizer, n, score, support, top5, strict] for score, support, top5, strict, n, bpm, digitizer in ranked[:10]],
            )
        )
        lines.append("")

    lines.append("## Artifact Set")
    artifact_counts = Counter(row["category"] for row in artifacts)
    lines.extend(md_table(["Category", "Rows"], [[key, value] for key, value in artifact_counts.most_common()]))
    lines.append("")

    if args.best_n:
        lines.append("## Leakage-Controlled Best-N")
        for plane in ("H", "V"):
            chosen, rationale = recommended_n(best_n_summary, plane, best_n_tune_half_width)
            if chosen is None:
                lines.append(f"- **{plane}: no automatic Best-N recommendation.** {rationale}.")
            else:
                lines.append(f"- **{plane}: Best-{chosen.get('subset_size', '')}.** {rationale}.")
        lines.append("")
        lines.extend(
            md_table(
                ["Plane", "N", "Spills", "Blind agree [95% CI]", "Blind abs dq [95% CI]", "Selected power", "Held-out power", "Selected prom.", "Held-out prom."],
                [
                    [
                        row.get("plane", ""),
                        row.get("subset_size", ""),
                        row.get("validation_spill_count", ""),
                        f"{row.get('blind_q_agreement_rate', '')} [{row.get('blind_q_agreement_ci_low', '')}, {row.get('blind_q_agreement_ci_high', '')}]",
                        f"{row.get('median_blind_selected_heldout_abs_q_delta', '')} [{row.get('blind_selected_heldout_abs_q_delta_ci_low', '')}, {row.get('blind_selected_heldout_abs_q_delta_ci_high', '')}]",
                        row.get("median_test_power_support", ""),
                        row.get("median_heldout_power_support", ""),
                        row.get("median_test_peak_prominence", ""),
                        row.get("median_heldout_prominence", ""),
                    ]
                    for row in sorted(best_n_summary, key=lambda row: (row.get("plane", ""), int(row.get("subset_size") or 0)))
                ],
            )
        )
        lines.append("")
        lines.append("Cross-collection global-N transfer:")
        lines.extend(
            md_table(
                ["Train", "Test", "Plane", "Status", "N", "Test knee", "Blind agree", "Blind abs dq", "Gain vs N1"],
                [
                    [
                        row.get("train_collection", ""),
                        row.get("test_collection", ""),
                        row.get("plane", ""),
                        row.get("status", ""),
                        row.get("selected_n", ""),
                        row.get("test_collection_knee_n", ""),
                        row.get("test_blind_q_agreement_rate", ""),
                        row.get("test_median_blind_selected_heldout_abs_q_delta", ""),
                        row.get("blind_agreement_gain_vs_n1", ""),
                    ]
                    for row in best_n_transfer
                ],
            )
        )
        lines.append("")

    if sensitivity_recommendations:
        lines.append("## Best-N Sensitivity Recommendations")
        lines.extend(
            md_table(
                ["Dimension", "Run", "Plane", "Recommended N", "Status"],
                [
                    [
                        row.get("dimension", ""),
                        row.get("label", ""),
                        row.get("plane", ""),
                        row.get("recommended_n", ""),
                        row.get("status", ""),
                    ]
                    for row in sensitivity_recommendations
                ],
            )
        )
        lines.append("")

    if args.ridge:
        lines.append("## Exact-Paired Full-Buffer Ridge Comparison")
        lines.extend(
            md_table(
                ["Plane", "N", "Paired points", "Legacy IQR", "Ensemble IQR", "IQR delta [turn-block CI]", "Peak gain [CI]", "Entropy delta [CI]", "Shared-ridge mass gain [CI]"],
                [
                    [
                        row.get("plane", ""),
                        row.get("subset_size", ""),
                        row.get("common_ridge_point_count", ""),
                        row.get("legacy_median_iqr_width", ""),
                        row.get("ensemble_median_iqr_width", ""),
                        f"{row.get('median_iqr_delta_ensemble_minus_legacy', '')} [{row.get('median_iqr_delta_ci_low', '')}, {row.get('median_iqr_delta_ci_high', '')}]",
                        f"{row.get('median_peak_bin_fraction_gain', '')} [{row.get('median_peak_bin_fraction_gain_ci_low', '')}, {row.get('median_peak_bin_fraction_gain_ci_high', '')}]",
                        f"{row.get('median_density_entropy_delta', '')} [{row.get('median_density_entropy_delta_ci_low', '')}, {row.get('median_density_entropy_delta_ci_high', '')}]",
                        f"{row.get('median_shared_ridge_mass_gain', '')} [{row.get('median_shared_ridge_mass_gain_ci_low', '')}, {row.get('median_shared_ridge_mass_gain_ci_high', '')}]",
                    ]
                    for row in sorted(ridge_legacy, key=lambda row: (row.get("plane", ""), int(row.get("subset_size") or 0)))
                ],
            )
        )
        lines.append("")
        lines.append("Data-derived concentration-loss candidates:")
        lines.extend(
            md_table(
                ["Plane", "N", "Peak turn", "Half-peak loss", "Change turn", "Peak drop", "IQR increase", "Sample drop"],
                [
                    [
                        row.get("plane", ""),
                        row.get("subset_size", ""),
                        row.get("peak_concentration_turn", ""),
                        row.get("first_sustained_half_peak_loss_turn", ""),
                        row.get("most_likely_change_turn", ""),
                        row.get("relative_peak_fraction_drop", ""),
                        row.get("relative_iqr_width_increase", ""),
                        row.get("relative_sample_fraction_drop", ""),
                    ]
                    for row in sorted(ridge_loss, key=lambda row: (row.get("plane", ""), int(row.get("subset_size") or 0)))
                ],
            )
        )
        lines.append("")
        lines.append("Turn-block intervals describe persistence across overlapping windows. They are not spill-population intervals, physical noise measurements, or external tune validation.")
        lines.append("")

    if args.intensity:
        lines.append("## Block-Aware Intensity Study")
        significant = [
            row
            for row in intensity_effects
            if row.get("statistical_benefit_pass") == "true"
            and fnum(row.get("fdr_q_value")) is not None
            and float(row.get("fdr_q_value", "nan")) <= 0.05
        ]
        practical = [row for row in intensity_effects if row.get("practical_effect_pass") == "true"]
        retained = [row for row in intensity_effects if row.get("retain_method_for_tune_analysis") == "true"]
        lines.append(f"- paired method-effect tests: `{len(intensity_effects)}`")
        lines.append(f"- FDR-significant directional effects within tune tolerance: `{len(significant)}`")
        lines.append(f"- effects clearing the minimum practical threshold: `{len(practical)}`")
        lines.append(f"- retained weighting effects: `{len(retained)}`")
        lines.append("")

        best_effects: dict[tuple[str, str], tuple[float, dict[str, str], float, float]] = {}
        for row in intensity_effects:
            margin = fnum(row.get("minimum_practical_effect"))
            delta = fnum(row.get("median_paired_delta"))
            low = fnum(row.get("bootstrap_ci_low"))
            high = fnum(row.get("bootstrap_ci_high"))
            if not margin or delta is None or low is None or high is None:
                continue
            sign = -1.0 if row.get("beneficial_direction") == "decrease" else 1.0
            ratio = sign * delta / margin
            interval = sorted((sign * low / margin, sign * high / margin))
            key = (row.get("plane", ""), row.get("method", ""))
            if key not in best_effects or ratio > best_effects[key][0]:
                best_effects[key] = (ratio, row, interval[0], interval[1])
        lines.append("Most favorable practical-effect fraction per plane/method (1.0 is the declared minimum):")
        lines.extend(
            md_table(
                ["Plane", "Method", "Metric", "N", "Effect / minimum [95% CI]", "FDR q", "Retain"],
                [
                    [
                        plane,
                        method,
                        row.get("metric", ""),
                        row.get("subset_size", ""),
                        f"{fmt(ratio, 5)} [{fmt(low, 5)}, {fmt(high, 5)}]",
                        row.get("fdr_q_value", ""),
                        row.get("retain_method_for_tune_analysis", ""),
                    ]
                    for (plane, method), (ratio, row, low, high) in sorted(best_effects.items())
                ],
            )
        )
        lines.append("")

        correlation_grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
        for row in intensity_correlations:
            correlation_grouped[(row.get("plane", ""), row.get("subset_size", ""), row.get("metric", ""))].append(row)
        strongest_correlations = []
        for key, rows in sorted(correlation_grouped.items()):
            finite_rows = [row for row in rows if fnum(row.get("median_spearman_rho")) is not None]
            if finite_rows:
                strongest = max(finite_rows, key=lambda row: abs(fnum(row.get("median_spearman_rho")) or 0.0))
                strongest_correlations.append([*key, strongest.get("lag_windows", ""), strongest.get("median_spearman_rho", ""), f"[{strongest.get('bootstrap_ci_low', '')}, {strongest.get('bootstrap_ci_high', '')}]"])
        lines.append("Largest absolute exploratory lag correlation per plane/N/metric:")
        lines.extend(md_table(["Plane", "N", "Metric", "Lag windows", "Median rho", "95% block CI"], strongest_correlations))
        lines.append("")
        lines.append("Lag and crossing-turn associations are exploratory. They do not identify extraction onset or establish causation.")
        lines.append("")

    lines.append("## Interpretation Guardrails")
    for item in [
        "Treat expected H/V tune values as soft discovery priors, not labels. Quote the data-derived medians above for the supplied run.",
        "Use paired intervals and effect sizes to compare Best-1/3/5. Do not infer the optimal ensemble size from training score; use the separate leakage-controlled Best-N result.",
        "Do not promote a frozen set, all-BPM fallback, or recurrent individual BPM unless its directly recomputed held-out result supports that claim.",
        "Handoff and visibility classes are thresholded review aids. Report their measured counts from this run and retain a no-reliable-tune state.",
        "No table in this report establishes absolute tune accuracy. External reference matching or a controlled settings study remains required for that claim.",
    ]:
        lines.append(f"- {item}")
    lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
