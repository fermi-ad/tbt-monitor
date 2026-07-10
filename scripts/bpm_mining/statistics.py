"""Global statistics for best-BPM mining."""

from __future__ import annotations

import hashlib
import itertools
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .identity import channel_label, manifest_by_index, normalize_subset_row
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


def best1_ranking_rows(inputs: Path) -> list[dict[str, str]]:
    path = inputs / "subset_search" / "best1" / "best1_rankings.csv"
    return read_csv(path) if path.exists() else []


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


def moving_block_resample(
    values: list[float],
    rng: random.Random,
    block_spills: int,
) -> list[float]:
    """Draw full non-wrapping blocks, then truncate to the observed length."""
    if not values:
        return []
    block = max(1, min(int(block_spills), len(values)))
    sampled: list[float] = []
    while len(sampled) < len(values):
        start = rng.randrange(len(values) - block + 1)
        sampled.extend(values[start : start + block])
    return sampled[: len(values)]


def block_bootstrap_interval(
    series_by_collection: dict[str, list[float]],
    samples: int,
    seed: int,
    block_spills: int,
) -> tuple[float | str, float | str]:
    series = {
        collection: [value for value in values if math.isfinite(value)]
        for collection, values in series_by_collection.items()
    }
    series = {collection: values for collection, values in series.items() if values}
    if not series:
        return "", ""
    rng = random.Random(seed)
    medians: list[float] = []
    for _ in range(max(100, samples)):
        draw: list[float] = []
        for collection in sorted(series):
            values = series[collection]
            draw.extend(moving_block_resample(values, rng, block_spills))
        medians.append(statistics.median(draw))
    medians.sort()
    return medians[int(0.025 * (len(medians) - 1))], medians[int(0.975 * (len(medians) - 1))]


def aggregate_statistics(cfg: dict[str, object], inputs: Path, manifest_dir: Path, out: Path) -> None:
    bpm_index = read_csv(manifest_dir / "bpm_index.csv")
    meta_by_index = manifest_by_index(bpm_index)
    rows = [normalize_subset_row(row, meta_by_index) for row in subset_rows(inputs)]
    ranking_rows = [normalize_subset_row(row, meta_by_index) for row in best1_ranking_rows(inputs)]
    by_plane_name = {(row["plane"], channel_label(row)): row for row in bpm_index}
    grouped = defaultdict(list)
    for row in rows:
        for member in parse_members(row):
            grouped[(row["plane"], member)].append(row)
    totals = Counter((row["plane"], row["subset_size"]) for row in rows)
    rank_percentiles: dict[tuple[str, str], list[float]] = defaultdict(list)
    rank_percentiles_by_collection: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    rankings_by_spill = defaultdict(list)
    for row in ranking_rows:
        rankings_by_spill[(row["collection"], row["spill_id"], row["plane"])].append(row)
    for key, group in rankings_by_spill.items():
        group.sort(key=lambda row: _f(row.get("subset_score")) or -math.inf, reverse=True)
        denom = max(1, len(group) - 1)
        for rank, row in enumerate(group):
            member = parse_members(row)[0] if parse_members(row) else ""
            pct = rank / denom
            rank_percentiles[(row["plane"], member)].append(pct)
            rank_percentiles_by_collection[(row["collection"], row["plane"], member)].append(pct)
    stats_rows = []
    boot = int(cfg["statistics"].get("bootstrap_samples", 2000))
    for key in sorted(by_plane_name):
        plane, bpm = key
        group = grouped.get(key, [])
        meta = by_plane_name.get(key, {})
        scores = [_f(row.get("subset_score")) for row in group]
        hold = [_f(row.get("holdout_support")) for row in group]
        residuals = [
            abs(q - c)
            for q, c in ((_f(row.get("q_hat")), _f(row.get("consensus_tune"))) for row in group)
            if q is not None and c is not None
        ]
        durations = [_f(row.get("visibility_duration_turns")) for row in group]
        top_counts = Counter(row["subset_size"] for row in group)
        rank_values = rank_percentiles.get(key, [])
        rank_low, rank_high = bootstrap_interval(rank_values, boot, stable_seed("bpm-rank", *key))
        collections = sorted({row["collection"] for row in rows})
        c1 = median(rank_percentiles_by_collection.get((collections[0], plane, bpm), [])) if collections else ""
        c2 = median(rank_percentiles_by_collection.get((collections[1], plane, bpm), [])) if len(collections) > 1 else ""
        stats_rows.append(
            {
                "plane": plane,
                "bpm_index": meta.get("bpm_index", ""),
                "bpm_name": bpm,
                "digitizer": meta.get("digitizer", ""),
                "source_key": meta.get("source_key", ""),
                "ring_order": meta.get("ring_order", ""),
                "valid_spill_count": len({(row["collection"], row["spill_id"]) for row in group}),
                "median_percentile_rank": median(rank_values),
                "top1_frequency": top_counts["1"] / max(1, totals[(plane, "1")]),
                "top3_inclusion_frequency": top_counts["3"] / max(1, totals[(plane, "3")]),
                "top5_inclusion_frequency": top_counts["5"] / max(1, totals[(plane, "5")]),
                "top10_inclusion_frequency": top_counts["10"] / max(1, totals[(plane, "10")]),
                "median_score": median([v for v in scores if v is not None]),
                "median_holdout_support": median([v for v in hold if v is not None]),
                "median_consensus_residual": median(residuals),
                "median_visibility_duration": median([v for v in durations if v is not None]),
                "collection1_rank": c1,
                "collection2_rank": c2,
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
    write_csv(out / "paired_method_tests.csv", paired_tests(rows, cfg), ["plane", "comparison", "median_paired_difference", "bootstrap_ci_low", "bootstrap_ci_high", "permutation_p_value", "fdr_q_value", "effect_size", "note"])
    write_csv(out / "subset_size_pareto.csv", pareto(rows), ["plane", "subset_size", "median_score", "median_visible_fraction", "compute_cost", "pareto_frontier"])
    write_csv(out / "bpm_marginal_value.csv", marginal_value(rows), ["plane", "bpm_name", "approx_marginal_value", "samples"])
    write_csv(out / "bpm_pair_synergy.csv", pair_synergy(rows), ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"])
    write_csv(out / "bpm_coselection.csv", pair_synergy(rows), ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"])
    atomic_write_text(out / "statistics_summary.md", f"# Best-BPM Statistics Summary\n\n- subset rows: `{len(rows)}`\n- BPM statistic rows: `{len(stats_rows)}`\n")


def ranks_for_scores(scores: dict[str, float]) -> dict[str, float]:
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ranks: dict[str, float] = {}
    idx = 0
    while idx < len(ordered):
        j = idx
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[idx][1]:
            j += 1
        rank = (idx + j) / 2.0 + 1.0
        for k in range(idx, j + 1):
            ranks[ordered[k][0]] = rank
        idx = j + 1
    return ranks


def spearman(a_scores: dict[str, float], b_scores: dict[str, float]) -> float | str:
    common = sorted(set(a_scores) & set(b_scores))
    if len(common) < 2:
        return ""
    ar = ranks_for_scores({key: a_scores[key] for key in common})
    br = ranks_for_scores({key: b_scores[key] for key in common})
    av = sum(ar[key] for key in common) / len(common)
    bv = sum(br[key] for key in common) / len(common)
    num = sum((ar[key] - av) * (br[key] - bv) for key in common)
    da = math.sqrt(sum((ar[key] - av) ** 2 for key in common))
    db = math.sqrt(sum((br[key] - bv) ** 2 for key in common))
    return num / (da * db) if da > 0 and db > 0 else ""


def kendall_tau(a_scores: dict[str, float], b_scores: dict[str, float]) -> float | str:
    common = sorted(set(a_scores) & set(b_scores))
    if len(common) < 2:
        return ""
    concordant = 0
    discordant = 0
    for left, right in itertools.combinations(common, 2):
        da = a_scores[left] - a_scores[right]
        db = b_scores[left] - b_scores[right]
        prod = da * db
        if prod > 0:
            concordant += 1
        elif prod < 0:
            discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else ""


def rank_stability(rows):
    out = []
    for plane in sorted({row["plane"] for row in rows}):
        collections = sorted({row["collection"] for row in rows if row["plane"] == plane})
        if len(collections) >= 2:
            a_scores = Counter(m for row in rows if row["plane"] == plane and row["collection"] == collections[0] for m in parse_members(row))
            b_scores = Counter(m for row in rows if row["plane"] == plane and row["collection"] == collections[1] for m in parse_members(row))
            top_count = min(10, len(a_scores), len(b_scores))
            a = {name for name, _count in a_scores.most_common(top_count)}
            b = {name for name, _count in b_scores.most_common(top_count)}
            jac = len(a & b) / max(1, len(a | b))
            out.append(
                {
                    "plane": plane,
                    "metric": "top10_jaccard_overlap",
                    "value": jac,
                    "detail": f"top {top_count}: {collections[0]} vs {collections[1]}",
                }
            )
            out.append({"plane": plane, "metric": "spearman_rank_correlation", "value": spearman(dict(a_scores), dict(b_scores)), "detail": f"{collections[0]} vs {collections[1]}"})
            out.append({"plane": plane, "metric": "kendall_tau", "value": kendall_tau(dict(a_scores), dict(b_scores)), "detail": f"{collections[0]} vs {collections[1]}"})
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


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def permutation_draw_count(samples: int) -> int:
    """Honor the declared Monte Carlo count while retaining a useful floor."""
    return max(100, int(samples))


def paired_permutation_p_value(diffs: list[float], samples: int, seed: int) -> float | str:
    vals = [v for v in diffs if math.isfinite(v) and v != 0.0]
    if not vals:
        return ""
    observed = abs(statistics.median(vals))
    total_exact = 2 ** len(vals)
    rng = random.Random(seed)
    exceed = 0
    draws = permutation_draw_count(samples)
    if total_exact <= draws:
        draws = total_exact
        for mask in range(total_exact):
            flipped = [value if (mask >> idx) & 1 else -value for idx, value in enumerate(vals)]
            if abs(statistics.median(flipped)) >= observed - 1e-15:
                exceed += 1
        return exceed / draws
    for _ in range(draws):
        flipped = [value if rng.random() < 0.5 else -value for value in vals]
        if abs(statistics.median(flipped)) >= observed - 1e-15:
            exceed += 1
    return (exceed + 1) / (draws + 1)


def block_sign_permutation_p_value(
    series_by_collection: dict[str, list[float]],
    samples: int,
    seed: int,
    block_spills: int,
) -> float | str:
    blocks: list[list[float]] = []
    block = max(1, int(block_spills))
    for collection in sorted(series_by_collection):
        values = [value for value in series_by_collection[collection] if math.isfinite(value)]
        blocks.extend(values[start : start + block] for start in range(0, len(values), block))
    if not any(blocks):
        return ""
    blocks = [values for values in blocks if any(value != 0.0 for value in values)]
    if not blocks:
        return 1.0
    observed = abs(statistics.median([value for values in blocks for value in values]))
    total_exact = 2 ** len(blocks)
    draws = permutation_draw_count(samples)
    exceed = 0
    if total_exact <= draws:
        draws = total_exact
        signs = (
            [1.0 if (mask >> index) & 1 else -1.0 for index in range(len(blocks))]
            for mask in range(total_exact)
        )
    else:
        rng = random.Random(seed)
        signs = ([1.0 if rng.random() < 0.5 else -1.0 for _ in blocks] for _ in range(draws))
    for block_signs in signs:
        flipped = [sign * value for sign, values in zip(block_signs, blocks) for value in values]
        if abs(statistics.median(flipped)) >= observed - 1e-15:
            exceed += 1
    return exceed / draws if total_exact <= draws else (exceed + 1) / (draws + 1)


def rank_biserial_effect(diffs: list[float]) -> float | str:
    vals = [v for v in diffs if math.isfinite(v) and v != 0.0]
    if not vals:
        return ""
    order = sorted(range(len(vals)), key=lambda index: abs(vals[index]))
    ranks = [0.0 for _value in vals]
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and abs(vals[order[end + 1]]) == abs(vals[order[start]]):
            end += 1
        average_rank = 0.5 * ((start + 1) + (end + 1))
        for position in range(start, end + 1):
            ranks[order[position]] = average_rank
        start = end + 1
    positive = sum(rank for rank, value in zip(ranks, vals) if value > 0)
    negative = sum(rank for rank, value in zip(ranks, vals) if value < 0)
    total = positive + negative
    return (positive - negative) / total if total else ""


def benjamini_hochberg(rows: list[dict[str, object]]) -> None:
    indexed = [
        (idx, float(row["permutation_p_value"]))
        for idx, row in enumerate(rows)
        if row.get("permutation_p_value") not in {"", None}
    ]
    if not indexed:
        return
    indexed.sort(key=lambda item: item[1])
    m = len(indexed)
    q_by_idx: dict[int, float] = {}
    prior = 1.0
    for rank, (idx, p_value) in reversed(list(enumerate(indexed, start=1))):
        q = min(prior, p_value * m / rank)
        prior = q
        q_by_idx[idx] = q
    for idx, q in q_by_idx.items():
        rows[idx]["fdr_q_value"] = f"{q:.9g}"


def paired_tests(rows, cfg):
    by_spill = defaultdict(dict)
    for row in rows:
        by_spill[(row["collection"], row["spill_id"], row["plane"])][row["subset_size"]] = _f(row.get("subset_score"))
    out = []
    samples = int(cfg.get("statistics", {}).get("permutation_samples", 10000)) if isinstance(cfg.get("statistics"), dict) else 10000
    bootstrap_samples = int(cfg.get("statistics", {}).get("bootstrap_samples", 2000)) if isinstance(cfg.get("statistics"), dict) else 2000
    block_spills = int(cfg.get("statistics", {}).get("bootstrap_block_spills", 20)) if isinstance(cfg.get("statistics"), dict) else 20

    def spill_sort_key(value: str) -> tuple[int, object]:
        token = value.replace("spill_", "")
        try:
            return 0, int(token)
        except ValueError:
            return 1, token

    for plane in sorted({key[2] for key in by_spill}):
        for a, b in (("1", "3"), ("3", "5"), ("5", "10")):
            series_by_collection: dict[str, list[float]] = defaultdict(list)
            for key in sorted(by_spill, key=lambda item: (item[0], spill_sort_key(item[1]), item[2])):
                vals = by_spill[key]
                if key[2] == plane and vals.get(a) is not None and vals.get(b) is not None:
                    series_by_collection[key[0]].append(vals[b] - vals[a])
            diffs = [value for collection in sorted(series_by_collection) for value in series_by_collection[collection]]
            if not diffs:
                continue
            seed = stable_seed("paired", plane, a, b)
            lo, hi = block_bootstrap_interval(series_by_collection, bootstrap_samples, seed, block_spills)
            p_value = block_sign_permutation_p_value(series_by_collection, samples, seed, block_spills)
            out.append(
                {
                    "plane": plane,
                    "comparison": f"best{a} vs best{b}",
                    "median_paired_difference": statistics.median(diffs),
                    "bootstrap_ci_low": lo,
                    "bootstrap_ci_high": hi,
                    "permutation_p_value": f"{p_value:.9g}" if isinstance(p_value, float) else "",
                    "fdr_q_value": "",
                    "effect_size": rank_biserial_effect(diffs),
                    "note": f"paired subset-score differences; moving-block bootstrap and block sign-flip permutation within collection (block={block_spills} spills); effect_size is the matched-pairs rank-biserial correlation",
                }
            )
    benjamini_hochberg(out)
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
