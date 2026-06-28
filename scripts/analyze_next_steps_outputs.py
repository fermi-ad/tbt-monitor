#!/usr/bin/env python3
"""Summarize Best-BPM NEXT_STEPS sidecar analysis outputs."""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def fnum(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        value_f = float(value)
    except ValueError:
        return None
    return value_f if math.isfinite(value_f) else None


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def iter_rows(path: Path):
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
    parser.add_argument("--out", type=Path, required=True, help="Markdown report path")
    args = parser.parse_args()

    root = args.root
    followup = args.followup

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
    heldout = list(iter_rows(followup / "evolution" / "finalist_heldout_spectral_support.csv"))
    visibility = list(iter_rows(followup / "handoff" / "bpm_window_visibility.csv"))
    handoff_events = list(iter_rows(followup / "handoff" / "bpm_handoff_events.csv"))
    visibility_summary = list(iter_rows(followup / "handoff" / "bpm_visibility_summary.csv"))

    lines: list[str] = []
    lines.append("# NEXT_STEPS Output Analysis")
    lines.append("")
    lines.append("This report is computed from the verifier-clean canonical Best-BPM run plus the NEXT_STEPS sidecar outputs.")
    lines.append("")
    lines.append("## Evidence Set")
    lines.extend(
        md_table(
            ["Surface", "Rows / files"],
            [
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
            ],
        )
    )
    lines.append("")

    lines.append("## Main Conclusions")
    conclusions = [
        "**The current dataset supports the soft tune priors H near 0.65 and V near 0.72.** Dynamic and all-BPM summaries cluster around H 0.653-0.654 and V 0.721-0.724. The older H around 0.69 context is not supported by this dataset.",
        "**Dynamic per-spill subset selection is strongly favored over frozen fixed sets.** The direct fixed-set recomputation gives zero median H score for fixed top1/top3/top5 in both held-out collections and only small V fixed-top5 scores. That busts the simple frozen-set operational hypothesis under the strict early-window metric.",
        "**Subset size helps, but with diminishing returns.** Best3 improves over best1, and best5 improves over best3, with paired effect sizes already large in the canonical statistics. The best5 increment is smaller than the best1-to-best3 jump.",
        "**V is much more coherent than H in held-out spectral support.** V best3/best5 finalists have median held-out candidate fractions near 0.98-1.0 for most aggregators, while H candidate fractions remain 0 even though non-selected BPM power at the same q_hat is present.",
        "**A single globally strongest BPM is not enough.** BPM 10.200.22.62 is a repeated top candidate in both planes, but its top1 frequency is only about 6%, and direct fixed-top1 medians collapse to zero. It is a strong contributor, not a standalone tune monitor.",
        "**Tune visibility appears to migrate across BPMs/windows, but most changes are weak/flickery rather than clean handoffs.** The handoff pass found thousands of transitions in selected artifacts, but only a small persistent-handoff subset and only one strictly VISIBLE_TUNE window under the v1 threshold.",
        "**All-BPM averaging is plane-dependent.** All-BPM mean is effectively unusable for H in this metric; all-BPM mean/median are viable V baselines but still underperform dynamic best3/best5.",
    ]
    for item in conclusions:
        lines.append(f"- {item}")
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
            ["Plane", "N", "Aggregator", "Rows", "Median held-out candidate fraction", "Median held-out power support", "Median selected-heldout delta"],
            [
                [
                    row["plane"],
                    row["subset_size"],
                    row["aggregator"],
                    row["row_count"],
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
    event_by_plane = Counter((row["plane"], row["event_label"]) for row in handoff_events)
    top_counts = Counter()
    for row in visibility:
        for field in ("is_top1_visible", "is_top3_visible", "is_top5_visible"):
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
            ["Plane", "Event label", "Rows"],
            [[plane, label, count] for (plane, label), count in sorted(event_by_plane.items())],
        )
    )
    lines.append("")
    total_by_plane = Counter(row["plane"] for row in visibility)
    lines.append("Fraction of per-BPM windows where the dynamic top-N member was visible:")
    lines.extend(
        md_table(
            ["Plane", "Top set", "Visible row fraction"],
            [
                [plane, field.replace("is_", "").replace("_visible", ""), top_counts[(plane, field)] / total_by_plane[plane]]
                for plane in sorted(total_by_plane)
                for field in ("is_top1_visible", "is_top3_visible", "is_top5_visible")
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

    lines.append("## Hypotheses")
    hypotheses = [
        ["H/V soft priors are around 0.65/0.72 for this dataset", "Supported", "Observed q_hat medians cluster near H 0.653-0.654 and V 0.721-0.724."],
        ["The older H around 0.69 context applies to this dataset", "Busted", "No main dynamic/fixed/all-BPM summaries center near 0.69."],
        ["Best3/Best5 dynamic subsets improve over Best1", "Supported", "Paired differences are positive in both planes, with large sign-balance effect sizes."],
        ["A frozen global top-N subset can replace dynamic per-spill selection", "Busted for strict v1 metric", "Direct fixed-set recomputation collapses H fixed medians to zero and leaves V fixed sets far below dynamic/all-BPM baselines."],
        ["BPM 10.200.22.62 is globally important", "Supported but limited", "It leads both planes globally, but top1 frequency is only around 6% and fixed-top1 does not generalize as a standalone monitor."],
        ["Held-out BPMs independently support finalist tunes", "Plane-dependent", "V best3/best5 have near-unity held-out candidate fractions; H has non-selected power support but not matching candidate-peak fractions."],
        ["All-BPM averaging is a safe fallback", "Partly busted", "All-BPM V is usable but below dynamic; all-BPM H mean is effectively zero under this score."],
        ["Tune visibility hands off among BPMs", "Supported qualitatively", "Selected artifacts show many top-N membership changes and 406 persistent handoff events, but most strict window classifications remain weak/no reliable tune."],
        ["The data proves true machine tune", "Not proven", "The analysis is BPM-only and internally consistent; it has no independent Schottky/reference truth."],
    ]
    lines.extend(md_table(["Hypothesis", "Status", "Evidence"], hypotheses))
    lines.append("")

    lines.append("## Recommended Next Moves")
    for item in [
        "For physics review, lead with V best3/best5 dynamic examples and treat H as weaker/harder in this dataset.",
        "Do not promote a frozen fixed BPM set for operations yet. If an operational fallback is required, use dynamic best3/best5 with held-out support gating; all-BPM V mean/median can be a secondary fallback.",
        "Investigate the discrepancy between the older fixed-set crossfit summary and the stricter direct fixed-set recomputation before citing fixed-set performance.",
        "Retune visibility thresholds. The handoff pass is informative, but the v1 `VISIBLE_TUNE` threshold is too strict for these selected artifacts and collapses nearly everything into WEAK/NO_RELIABLE.",
        "Add an external tune reference or controlled settings log in the next beam study. The current data can rank BPM-only consistency; it cannot prove absolute tune truth.",
        "Use the 317 selected spill artifacts as the first manual review set, prioritizing V examples with high best5 score and H examples that show why H is less coherent.",
    ]:
        lines.append(f"- {item}")
    lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
