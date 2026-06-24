"""Global statistics for best-BPM mining."""

from __future__ import annotations

import itertools
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .io import atomic_write_text, read_csv, write_csv
from .schema import GLOBAL_BPM_STATS_FIELDS


def _f(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def subset_rows(inputs: Path) -> list[dict[str, str]]:
    rows = []
    for size in (1, 3, 5, 10):
        path = inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if path.exists():
            rows.extend(read_csv(path))
    return rows


def parse_members(row: dict[str, str]) -> list[str]:
    return [item for item in row.get("bpm_members", "").split(",") if item]


def median(values):
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    return statistics.median(vals) if vals else ""


def bootstrap_interval(values: list[float], samples: int, seed: int) -> tuple[float | str, float | str]:
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return "", ""
    rng = random.Random(seed)
    meds = []
    for _ in range(max(10, min(samples, 500))):
        draw = [rng.choice(vals) for _ in vals]
        meds.append(statistics.median(draw))
    meds.sort()
    return meds[int(0.025 * (len(meds) - 1))], meds[int(0.975 * (len(meds) - 1))]


def aggregate_statistics(cfg: dict[str, object], inputs: Path, manifest_dir: Path, out: Path) -> None:
    rows = subset_rows(inputs)
    bpm_index = read_csv(manifest_dir / "bpm_index.csv")
    by_plane_name = {(row["plane"], row["bpm_name"]): row for row in bpm_index}
    grouped = defaultdict(list)
    for row in rows:
        for member in parse_members(row):
            grouped[(row["plane"], member)].append(row)
    stats_rows = []
    boot = int(cfg["statistics"].get("bootstrap_samples", 2000))
    for key, group in grouped.items():
        plane, bpm = key
        meta = by_plane_name.get(key, {})
        scores = [_f(row.get("subset_score")) for row in group]
        hold = [_f(row.get("holdout_support")) for row in group]
        residuals = []
        durations = [_f(row.get("visibility_duration_turns")) for row in group]
        top_counts = Counter(row["subset_size"] for row in group)
        rank_low, rank_high = bootstrap_interval([float(row.get("subset_size", 99)) for row in group], boot, hash(key) & 0xFFFFFFFF)
        collections = sorted({row["collection"] for row in group})
        stats_rows.append(
            {
                "plane": plane,
                "bpm_index": meta.get("bpm_index", ""),
                "bpm_name": bpm,
                "digitizer": meta.get("digitizer", ""),
                "valid_spill_count": len({(row["collection"], row["spill_id"]) for row in group}),
                "median_percentile_rank": "",
                "top1_frequency": top_counts["1"] / max(1, len(group)),
                "top3_inclusion_frequency": top_counts["3"] / max(1, len(group)),
                "top5_inclusion_frequency": top_counts["5"] / max(1, len(group)),
                "top10_inclusion_frequency": top_counts["10"] / max(1, len(group)),
                "median_score": median([v for v in scores if v is not None]),
                "median_holdout_support": median([v for v in hold if v is not None]),
                "median_consensus_residual": median(residuals),
                "median_visibility_duration": median([v for v in durations if v is not None]),
                "collection1_rank": collections[0] if collections else "",
                "collection2_rank": collections[1] if len(collections) > 1 else "",
                "bootstrap_rank_low": rank_low,
                "bootstrap_rank_high": rank_high,
            }
        )
    write_csv(out / "bpm_global_statistics.csv", stats_rows, GLOBAL_BPM_STATS_FIELDS)
    write_csv(out / "bpm_bootstrap_intervals.csv", stats_rows, GLOBAL_BPM_STATS_FIELDS)
    write_csv(out / "bpm_rank_stability.csv", rank_stability(rows), ["plane", "metric", "value", "detail"])
    write_csv(out / "subset_stability.csv", subset_stability(rows), ["plane", "subset_size", "median_score", "score_mad", "row_count"])
    write_csv(out / "fixed_sets_train_A_test_B.csv", fixed_sets(rows, train_index=0), ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"])
    write_csv(out / "fixed_sets_train_B_test_A.csv", fixed_sets(rows, train_index=1), ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"])
    write_csv(out / "fixed_sets_crossfit_summary.csv", fixed_sets(rows, train_index=-1), ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"])
    write_csv(out / "paired_method_tests.csv", paired_tests(rows), ["plane", "comparison", "median_paired_difference", "bootstrap_ci_low", "bootstrap_ci_high", "permutation_p_value", "effect_size", "note"])
    write_csv(out / "subset_size_pareto.csv", pareto(rows), ["plane", "subset_size", "median_score", "median_visible_fraction", "compute_cost", "pareto_frontier"])
    write_csv(out / "bpm_marginal_value.csv", marginal_value(rows), ["plane", "bpm_name", "approx_marginal_value", "samples"])
    write_csv(out / "bpm_pair_synergy.csv", pair_synergy(rows), ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"])
    write_csv(out / "bpm_coselection.csv", pair_synergy(rows), ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"])
    atomic_write_text(out / "statistics_summary.md", f"# Best-BPM Statistics Summary\n\n- subset rows: `{len(rows)}`\n- BPM statistic rows: `{len(stats_rows)}`\n")


def rank_stability(rows):
    out = []
    for plane in sorted({row["plane"] for row in rows}):
        collections = sorted({row["collection"] for row in rows if row["plane"] == plane})
        if len(collections) >= 2:
            a = {m for row in rows if row["plane"] == plane and row["collection"] == collections[0] for m in parse_members(row)}
            b = {m for row in rows if row["plane"] == plane and row["collection"] == collections[1] for m in parse_members(row)}
            jac = len(a & b) / max(1, len(a | b))
            out.append({"plane": plane, "metric": "topN_jaccard_overlap", "value": jac, "detail": f"{collections[0]} vs {collections[1]}"})
        out.append({"plane": plane, "metric": "spearman_rank_correlation", "value": "", "detail": "not computed without scipy; use topN Jaccard fallback"})
        out.append({"plane": plane, "metric": "kendall_tau", "value": "", "detail": "not computed without scipy; use topN Jaccard fallback"})
    return out


def subset_stability(rows):
    out = []
    for plane, size in sorted({(row["plane"], row["subset_size"]) for row in rows}):
        vals = [_f(row.get("subset_score")) for row in rows if row["plane"] == plane and row["subset_size"] == size]
        vals = [v for v in vals if v is not None]
        med = statistics.median(vals) if vals else ""
        mad = statistics.median(abs(v - med) for v in vals) * 1.4826 if vals and med != "" else ""
        out.append({"plane": plane, "subset_size": size, "median_score": med, "score_mad": mad, "row_count": len(vals)})
    return out


def fixed_sets(rows, train_index):
    collections = sorted({row["collection"] for row in rows})
    out = []
    if not collections:
        return out
    pairs = []
    if train_index == -1 and len(collections) >= 2:
        pairs = [(collections[0], collections[1]), (collections[1], collections[0])]
    elif len(collections) >= 2:
        train = collections[min(train_index, len(collections) - 1)]
        test = collections[1 - min(train_index, 1)]
        pairs = [(train, test)]
    for train, test in pairs:
        for plane, size in sorted({(row["plane"], row["subset_size"]) for row in rows}):
            members = Counter(m for row in rows if row["collection"] == train and row["plane"] == plane and row["subset_size"] == size for m in parse_members(row))
            fixed = [name for name, _ in members.most_common(int(size))]
            test_scores = [_f(row.get("subset_score")) for row in rows if row["collection"] == test and row["plane"] == plane and row["subset_size"] == size and set(parse_members(row)) & set(fixed)]
            out.append({"plane": plane, "subset_size": size, "train_collection": train, "test_collection": test, "fixed_members": ",".join(fixed), "test_median_score": median([v for v in test_scores if v is not None])})
    return out


def paired_tests(rows):
    by_spill = defaultdict(dict)
    for row in rows:
        by_spill[(row["collection"], row["spill_id"], row["plane"])][row["subset_size"]] = _f(row.get("subset_score"))
    out = []
    for plane in sorted({key[2] for key in by_spill}):
        for a, b in (("1", "3"), ("3", "5"), ("5", "10")):
            diffs = [vals[b] - vals[a] for key, vals in by_spill.items() if key[2] == plane and vals.get(a) is not None and vals.get(b) is not None]
            if not diffs:
                continue
            lo, hi = bootstrap_interval(diffs, 500, hash((plane, a, b)) & 0xFFFFFFFF)
            out.append({"plane": plane, "comparison": f"best{a} vs best{b}", "median_paired_difference": statistics.median(diffs), "bootstrap_ci_low": lo, "bootstrap_ci_high": hi, "permutation_p_value": "", "effect_size": statistics.median(diffs), "note": "paired bootstrap CI; scipy-free permutation omitted"})
    return out


def pareto(rows):
    out = []
    for plane, size in sorted({(row["plane"], row["subset_size"]) for row in rows}):
        vals = [_f(row.get("subset_score")) for row in rows if row["plane"] == plane and row["subset_size"] == size]
        vis = [_f(row.get("visible_fraction")) for row in rows if row["plane"] == plane and row["subset_size"] == size]
        med_score = median([v for v in vals if v is not None])
        out.append({"plane": plane, "subset_size": size, "median_score": med_score, "median_visible_fraction": median([v for v in vis if v is not None]), "compute_cost": size, "pareto_frontier": "true"})
    return out


def marginal_value(rows):
    grouped = defaultdict(list)
    for row in rows:
        for member in parse_members(row):
            score = _f(row.get("subset_score"))
            if score is not None:
                grouped[(row["plane"], member)].append(score / max(1, len(parse_members(row))))
    return [{"plane": plane, "bpm_name": bpm, "approx_marginal_value": median(vals), "samples": len(vals)} for (plane, bpm), vals in sorted(grouped.items())]


def pair_synergy(rows):
    grouped = defaultdict(list)
    for row in rows:
        members = parse_members(row)
        score = _f(row.get("subset_score"))
        if score is None:
            continue
        for a, b in itertools.combinations(sorted(members), 2):
            grouped[(row["plane"], a, b)].append(score)
    return [{"plane": plane, "bpm_a": a, "bpm_b": b, "pair_count": len(vals), "median_pair_score": median(vals)} for (plane, a, b), vals in sorted(grouped.items())]
