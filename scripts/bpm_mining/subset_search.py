"""Best 1/3/5/10 BPM subset search."""

from __future__ import annotations

import math
import random
import hashlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence

import numpy as np

from .io import atomic_write_text, read_csv, write_csv
from .schema import BEST_SUBSET_FIELDS
from .subset_score import SubsetScore, combination_array, score_subset_chunk, subset_mask


POOL_FIELDS = ["collection", "spill_id", "plane", "subset_size", "pool_size", "bpm_indices", "bpm_members", "source"]
AUDIT_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "audit_type",
    "best_audit_score",
    "screened_winner_score",
    "improvement",
    "pool_expanded",
    "bpm_members",
]


def _f(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def metadata_for_bpms(manifest_dir: Path, plane: str) -> dict[int, dict[str, str]]:
    rows = read_csv(manifest_dir / "bpm_index.csv")
    return {int(row["bpm_index"]): row for row in rows if row.get("plane") == plane}


def candidate_tune_index(features_dir: Path) -> dict[tuple[str, str, str], dict[int, float]]:
    index: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    for row in read_csv(features_dir / "per_bpm_spill_summary.csv"):
        tune = _f(row.get("median_tune"))
        if tune is None:
            continue
        index[(row["collection"], row["spill_id"], row["plane"])][int(row["bpm_index"])] = tune
    return index


def empty_results() -> tuple[dict[int, list[dict[str, object]]], dict[int, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]]]:
    return {1: [], 3: [], 5: [], 10: []}, {1: [], 3: [], 5: [], 10: []}, [], []


def merge_results(
    target_results: dict[int, list[dict[str, object]]],
    target_candidates: dict[int, list[dict[str, object]]],
    target_pools: list[dict[str, object]],
    target_audits: list[dict[str, object]],
    source: tuple[dict[int, list[dict[str, object]]], dict[int, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]]],
) -> None:
    results, candidates, pools, audits = source
    for size, rows in results.items():
        target_results.setdefault(size, []).extend(rows)
    for size, rows in candidates.items():
        target_candidates.setdefault(size, []).extend(rows)
    target_pools.extend(pools)
    target_audits.extend(audits)


def candidate_tunes_for(
    tune_index: dict[tuple[str, str, str], dict[int, float]],
    collection: str,
    spill_id: str,
    plane: str,
    bpm_indices: Sequence[int],
) -> np.ndarray:
    tune_by_bpm = tune_index.get((collection, spill_id, plane), {})
    return np.asarray([tune_by_bpm.get(int(idx), math.nan) for idx in bpm_indices], dtype=np.float32)


def consensus_lookup(consensus_dir: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    return {
        (row["collection"], row["spill_id"], row["plane"]): row
        for row in read_csv(consensus_dir / "spill_consensus_summary.csv")
    }


def score_combos(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    centers: np.ndarray,
    bpm_indices: np.ndarray,
    candidate_tunes: np.ndarray,
    combos: np.ndarray,
    bpm_meta: dict[int, dict[str, str]],
    consensus: dict[str, str] | None,
    window_turns: int,
    chunk_size: int,
    device: str,
) -> list[SubsetScore]:
    if combos.size == 0:
        return []
    digitizers = [bpm_meta.get(int(idx), {}).get("digitizer", "") for idx in bpm_indices]
    ring_orders = []
    for idx in bpm_indices:
        try:
            ring_orders.append(float(bpm_meta.get(int(idx), {}).get("ring_order", "")))
        except ValueError:
            ring_orders.append(math.nan)
    c_tune = _f(consensus.get("dominant_consensus_tune")) if consensus else None
    c_unc = _f(consensus.get("median_consensus_uncertainty")) if consensus else None
    tol = max(4.0 / window_turns, 0.002)
    out: list[SubsetScore] = []
    for start in range(0, combos.shape[0], chunk_size):
        out.extend(
            score_subset_chunk(
                spectra,
                tune_axis,
                centers,
                candidate_tunes,
                combos[start : start + chunk_size],
                digitizers,
                ring_orders,
                c_tune,
                c_unc,
                tol,
                device,
            )
        )
    out.sort(key=lambda score: score.subset_score, reverse=True)
    return out


def result_row(
    collection: str,
    spill_id: str,
    plane: str,
    score: SubsetScore,
    bpm_indices: np.ndarray,
    bpm_meta: dict[int, dict[str, str]],
    pool_size: int,
    scope: str,
    exact: bool,
    audit: bool,
    consensus: dict[str, str] | None,
    quality_flags: str = "",
) -> dict[str, object]:
    members_idx = [int(bpm_indices[pos]) for pos in score.subset]
    names = [bpm_meta.get(idx, {}).get("bpm_name", str(idx)) for idx in members_idx]
    return {
        "collection": collection,
        "spill_id": spill_id,
        "plane": plane,
        "subset_size": len(members_idx),
        "subset_mask": subset_mask(members_idx),
        "bpm_members": ",".join(names),
        "candidate_pool_size": pool_size,
        "search_scope": scope,
        "search_exact": str(exact).lower(),
        "audit_performed": str(audit).lower(),
        "aggregator": "mean_power",
        "q_hat": f"{score.q_hat:.9g}" if math.isfinite(score.q_hat) else "",
        "subset_score": f"{score.subset_score:.9g}",
        "holdout_support": f"{score.holdout_support:.9g}",
        "peak_quality": f"{score.peak_quality:.9g}",
        "consensus_agreement": f"{score.consensus_agreement:.9g}",
        "window_stability": f"{score.window_stability:.9g}",
        "diversity_score": f"{score.diversity_score:.9g}",
        "ambiguity_penalty": f"{score.ambiguity_penalty:.9g}",
        "visible_fraction": f"{score.visible_fraction:.9g}",
        "visibility_duration_turns": f"{score.visibility_duration_turns:.9g}",
        "consensus_tune": consensus.get("dominant_consensus_tune", "") if consensus else "",
        "consensus_label": consensus.get("consensus_label", "") if consensus else "",
        "quality_flags": quality_flags,
    }


def pool_row(collection, spill_id, plane, subset_size, pool, bpm_indices, bpm_meta, source):
    members_idx = [int(bpm_indices[pos]) for pos in pool]
    return {
        "collection": collection,
        "spill_id": spill_id,
        "plane": plane,
        "subset_size": subset_size,
        "pool_size": len(pool),
        "bpm_indices": ",".join(str(idx) for idx in members_idx),
        "bpm_members": ",".join(bpm_meta.get(idx, {}).get("bpm_name", str(idx)) for idx in members_idx),
        "source": source,
    }


def supplement_pool(
    pool: list[int],
    single_scores: list[SubsetScore],
    bpm_indices: np.ndarray,
    bpm_meta: dict[int, dict[str, str]],
    cap: int,
) -> list[int]:
    if len(pool) >= cap:
        return pool[:cap]
    ordered = [int(score.subset[0]) for score in single_scores if len(score.subset) == 1]
    by_digitizer: dict[str, int] = {}
    by_sector: dict[int, int] = {}
    for pos in ordered:
        bpm_idx = int(bpm_indices[pos])
        meta = bpm_meta.get(bpm_idx, {})
        digitizer = meta.get("digitizer", "")
        if digitizer and digitizer not in by_digitizer:
            by_digitizer[digitizer] = pos
        try:
            sector = int(float(meta.get("ring_order", "0")) // 100)
        except ValueError:
            sector = -1
        if sector >= 0 and sector not in by_sector:
            by_sector[sector] = pos
    expanded = list(pool)
    for pos in list(by_digitizer.values()) + list(by_sector.values()) + ordered:
        if pos not in expanded:
            expanded.append(pos)
        if len(expanded) >= cap:
            break
    return expanded[:cap]


def beam_audit(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    centers: np.ndarray,
    bpm_indices: np.ndarray,
    candidate_tunes: np.ndarray,
    bpm_meta: dict[int, dict[str, str]],
    consensus: dict[str, str] | None,
    window_turns: int,
    chunk_size: int,
    subset_size: int,
    beam_width: int,
    seeds: list[tuple[int, ...]],
    device: str,
) -> list[SubsetScore]:
    n = spectra.shape[0]
    beam = sorted(set(tuple(sorted(seed)) for seed in seeds if len(seed) == 1))
    if not beam:
        beam = [(idx,) for idx in range(n)]
    current_size = 1
    while current_size < subset_size:
        expanded = set()
        for subset in beam:
            for idx in range(n):
                if idx not in subset:
                    expanded.add(tuple(sorted((*subset, idx))))
        combos = np.asarray(sorted(expanded), dtype=np.int16)
        scored = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combos, bpm_meta, consensus, window_turns, chunk_size, device)
        beam = [score.subset for score in scored[:beam_width]]
        current_size += 1
    final = np.asarray(sorted(set(beam)), dtype=np.int16)
    return score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, final, bpm_meta, consensus, window_turns, chunk_size, device)


def random_audit(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    centers: np.ndarray,
    bpm_indices: np.ndarray,
    candidate_tunes: np.ndarray,
    bpm_meta: dict[int, dict[str, str]],
    consensus: dict[str, str] | None,
    window_turns: int,
    chunk_size: int,
    subset_size: int,
    samples: int,
    seed: int,
    device: str,
) -> list[SubsetScore]:
    rng = random.Random(seed)
    n = spectra.shape[0]
    combos = {tuple(sorted(rng.sample(range(n), subset_size))) for _ in range(samples) if n >= subset_size}
    if not combos:
        return []
    return score_combos(
        spectra,
        tune_axis,
        centers,
        bpm_indices,
        candidate_tunes,
        np.asarray(sorted(combos), dtype=np.int16),
        bpm_meta,
        consensus,
        window_turns,
        chunk_size,
        device,
    )


def process_cache_rows(
    cfg: dict[str, object],
    cache_rows: Sequence[dict[str, str]],
    manifest_dir: Path,
    features_dir: Path,
    consensus_dir: Path,
    subset_sizes: Sequence[int],
    device: str,
) -> tuple[dict[int, list[dict[str, object]]], dict[int, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]]]:
    search_cfg = cfg["subset_search"]
    chunk_size = int(search_cfg.get("subset_chunk_size", 512))
    max_windows = int(search_cfg.get("max_search_windows", 16))
    consensus_map = consensus_lookup(consensus_dir)
    tune_index = candidate_tune_index(features_dir)
    results, top_candidates, pools, audits = empty_results()
    for cache in cache_rows:
        collection, spill_id, plane = cache["collection"], cache["spill_id"], cache["plane"]
        bpm_meta = metadata_for_bpms(manifest_dir, plane)
        spectra = np.asarray(np.load(cache["spectra_path"], mmap_mode="r")[:, :max_windows, :], dtype=np.float32)
        centers = np.asarray(np.load(cache["window_centers_path"])[: spectra.shape[1]], dtype=np.float32)
        tune_axis = np.asarray(np.load(cache["tune_axis_path"]), dtype=np.float32)
        bpm_indices = np.asarray(np.load(cache["bpm_indices_path"]), dtype=np.int32)
        if spectra.shape[0] == 0:
            continue
        candidate_tunes = candidate_tunes_for(tune_index, collection, spill_id, plane, bpm_indices)
        consensus = consensus_map.get((collection, spill_id, plane))
        window_turns = int(cache.get("window_turns") or 4096)
        best_by_size: dict[int, list[SubsetScore]] = {}
        if 1 in subset_sizes:
            combos = np.arange(spectra.shape[0], dtype=np.int16)[:, None]
            best1 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combos, bpm_meta, consensus, window_turns, chunk_size, device)
            best_by_size[1] = best1
            if best1:
                results[1].append(result_row(collection, spill_id, plane, best1[0], bpm_indices, bpm_meta, spectra.shape[0], "FULL_60", True, False, consensus))
                for score in best1:
                    top_candidates[1].append(result_row(collection, spill_id, plane, score, bpm_indices, bpm_meta, spectra.shape[0], "FULL_60", True, False, consensus))
        if 3 in subset_sizes and spectra.shape[0] >= 3:
            combos = combination_array(list(range(spectra.shape[0])), 3)
            best3 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combos, bpm_meta, consensus, window_turns, chunk_size, device)
            best_by_size[3] = best3
            if best3:
                results[3].append(result_row(collection, spill_id, plane, best3[0], bpm_indices, bpm_meta, spectra.shape[0], "FULL_60", True, False, consensus))
                keep = int(search_cfg.get("best3_keep", 256))
                for score in best3[:keep]:
                    top_candidates[3].append(result_row(collection, spill_id, plane, score, bpm_indices, bpm_meta, spectra.shape[0], "FULL_60", True, False, consensus))
        if 5 in subset_sizes and spectra.shape[0] >= 5:
            top_single = [idx for score in best_by_size.get(1, [])[:12] for idx in score.subset]
            top_triple_members = [idx for score in best_by_size.get(3, [])[:256] for idx in score.subset]
            pool = sorted(set(top_single + top_triple_members), key=lambda idx: (top_single + top_triple_members).count(idx), reverse=True)
            pool = supplement_pool(pool, best_by_size.get(1, []), bpm_indices, bpm_meta, min(int(search_cfg.get("best5_pool_size", 20)), spectra.shape[0]))
            pools.append(pool_row(collection, spill_id, plane, 5, pool, bpm_indices, bpm_meta, "top_single_and_top_triples"))
            if len(pool) >= 5:
                best5 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combination_array(pool, 5), bpm_meta, consensus, window_turns, chunk_size, device)
                best_by_size[5] = best5
                audited = False
                expanded_by_audit = False
                best_audit = []
                if best5:
                    best_audit = run_audits(cfg, spectra, tune_axis, centers, bpm_indices, candidate_tunes, bpm_meta, consensus, window_turns, chunk_size, 5, best_by_size, collection, spill_id, plane, audits, device, best5[0].subset_score)
                    audited = True
                    if best_audit and best_audit[0].subset_score > best5[0].subset_score + float(search_cfg.get("audit_improvement_threshold", 0.01)):
                        pool = sorted(set(pool + list(best_audit[0].subset)))
                        pools.append(pool_row(collection, spill_id, plane, 5, pool, bpm_indices, bpm_meta, "POOL_EXPANDED_BY_AUDIT"))
                        best5 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combination_array(pool, 5), bpm_meta, consensus, window_turns, chunk_size, device)
                        expanded_by_audit = True
                if best5:
                    results[5].append(result_row(collection, spill_id, plane, best5[0], bpm_indices, bpm_meta, len(pool), "SCREENED_POOL", True, audited, consensus, "POOL_EXPANDED_BY_AUDIT" if expanded_by_audit else ""))
                    for score in best5[: int(search_cfg.get("best5_keep", 128))]:
                        top_candidates[5].append(result_row(collection, spill_id, plane, score, bpm_indices, bpm_meta, len(pool), "SCREENED_POOL", True, audited, consensus))
        if 10 in subset_sizes and spectra.shape[0] >= 10:
            top_single = [idx for score in best_by_size.get(1, [])[:16] for idx in score.subset]
            top5_members = [idx for score in best_by_size.get(5, [])[:128] for idx in score.subset]
            pool = sorted(set(top_single + top5_members), key=lambda idx: (top_single + top5_members).count(idx), reverse=True)
            pool = supplement_pool(pool, best_by_size.get(1, []), bpm_indices, bpm_meta, min(int(search_cfg.get("best10_pool_size", 18)), spectra.shape[0]))
            pools.append(pool_row(collection, spill_id, plane, 10, pool, bpm_indices, bpm_meta, "top_single_and_top5"))
            if len(pool) >= 10:
                best10 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combination_array(pool, 10), bpm_meta, consensus, window_turns, chunk_size, device)
                audited = False
                expanded_by_audit = False
                if best10:
                    best_audit = run_audits(cfg, spectra, tune_axis, centers, bpm_indices, candidate_tunes, bpm_meta, consensus, window_turns, chunk_size, 10, best_by_size, collection, spill_id, plane, audits, device, best10[0].subset_score)
                    audited = True
                    if best_audit and best_audit[0].subset_score > best10[0].subset_score + float(search_cfg.get("audit_improvement_threshold", 0.01)):
                        pool = sorted(set(pool + list(best_audit[0].subset)))
                        pools.append(pool_row(collection, spill_id, plane, 10, pool, bpm_indices, bpm_meta, "POOL_EXPANDED_BY_AUDIT"))
                        best10 = score_combos(spectra, tune_axis, centers, bpm_indices, candidate_tunes, combination_array(pool, 10), bpm_meta, consensus, window_turns, chunk_size, device)
                        expanded_by_audit = True
                if best10:
                    results[10].append(result_row(collection, spill_id, plane, best10[0], bpm_indices, bpm_meta, len(pool), "SCREENED_POOL", True, audited, consensus, "POOL_EXPANDED_BY_AUDIT" if expanded_by_audit else ""))
                    for score in best10[: int(search_cfg.get("best10_keep", 64))]:
                        top_candidates[10].append(result_row(collection, spill_id, plane, score, bpm_indices, bpm_meta, len(pool), "SCREENED_POOL", True, audited, consensus))
    return results, top_candidates, pools, audits


def _process_cache_chunk(
    args: tuple[int, list[dict[str, str]], dict[str, object], str, str, str, list[int], str],
) -> tuple[int, tuple[dict[int, list[dict[str, object]]], dict[int, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]]]]:
    index, rows, cfg, manifest_dir, features_dir, consensus_dir, subset_sizes, device = args
    return index, process_cache_rows(cfg, rows, Path(manifest_dir), Path(features_dir), Path(consensus_dir), subset_sizes, device)


def split_chunks(rows: Sequence[dict[str, str]], chunks: int) -> list[list[dict[str, str]]]:
    chunks = max(1, min(chunks, len(rows)))
    size = int(math.ceil(len(rows) / chunks))
    return [list(rows[start : start + size]) for start in range(0, len(rows), size)]


def search_best_bpm_subsets(
    cfg: dict[str, object],
    cache_dir: Path,
    manifest_dir: Path,
    features_dir: Path,
    consensus_dir: Path,
    out: Path,
    subset_sizes: Sequence[int],
    device: str = "cpu",
    limit: int = 0,
    workers: int | None = None,
) -> None:
    search_cfg = cfg["subset_search"]
    spectral_config = str(search_cfg["search_spectral_config"])
    cache_rows = [
        row
        for row in read_csv(cache_dir / "index" / "spectral_cache.csv")
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    ]
    if limit:
        cache_rows = cache_rows[:limit]
    requested_workers = max(1, int(workers if workers is not None else cfg.get("runtime", {}).get("workers", 1) if isinstance(cfg.get("runtime"), dict) else 1))
    cuda_cap = int(search_cfg.get("cuda_workers", 4))
    worker_count = min(requested_workers, cuda_cap) if device == "cuda" else requested_workers
    worker_count = max(1, min(worker_count, len(cache_rows) if cache_rows else 1))
    results, top_candidates, pools, audits = empty_results()
    if worker_count > 1:
        chunks = split_chunks(cache_rows, worker_count)
        tasks = [
            (idx, chunk, cfg, str(manifest_dir), str(features_dir), str(consensus_dir), list(subset_sizes), device)
            for idx, chunk in enumerate(chunks)
        ]
        chunk_results = []
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            for future in as_completed(pool.submit(_process_cache_chunk, task) for task in tasks):
                chunk_results.append(future.result())
        for _, partial in sorted(chunk_results, key=lambda item: item[0]):
            merge_results(results, top_candidates, pools, audits, partial)
    else:
        results, top_candidates, pools, audits = process_cache_rows(cfg, cache_rows, manifest_dir, features_dir, consensus_dir, subset_sizes, device)
    for size, rows in results.items():
        if size in subset_sizes:
            write_csv(out / f"best{size}" / f"best{size}_results.csv", rows, BEST_SUBSET_FIELDS)
            candidates = top_candidates.get(size, rows)
            name = "best1_rankings.csv" if size == 1 else f"best{size}_top_candidates.csv"
            write_csv(out / f"best{size}" / name, candidates, BEST_SUBSET_FIELDS)
    if 5 in subset_sizes:
        write_csv(out / "best5" / "best5_pool.csv", [row for row in pools if row["subset_size"] == 5], POOL_FIELDS)
    if 10 in subset_sizes:
        write_csv(out / "best10" / "best10_pool.csv", [row for row in pools if row["subset_size"] == 10], POOL_FIELDS)
    write_csv(out / "audit_results.csv", audits, AUDIT_FIELDS)
    total = sum(len(rows) for rows in results.values())
    atomic_write_text(out / "subset_search_summary.md", f"# Subset Search Summary\n\n- best result rows: `{total}`\n- audit rows: `{len(audits)}`\n")


def run_audits(cfg, spectra, tune_axis, centers, bpm_indices, candidate_tunes, bpm_meta, consensus, window_turns, chunk_size, subset_size, best_by_size, collection, spill_id, plane, audit_rows, device, screened_score):
    search_cfg = cfg["subset_search"]
    seeds = []
    seeds.extend(score.subset for score in best_by_size.get(1, [])[:10])
    seeds.extend(score.subset for score in best_by_size.get(3, [])[:32])
    beam = beam_audit(
        spectra,
        tune_axis,
        centers,
        bpm_indices,
        candidate_tunes,
        bpm_meta,
        consensus,
        window_turns,
        chunk_size,
        subset_size,
        int(search_cfg.get("beam_width", 512)),
        list(seeds),
        device,
    )
    rnd = random_audit(
        spectra,
        tune_axis,
        centers,
        bpm_indices,
        candidate_tunes,
        bpm_meta,
        consensus,
        window_turns,
        chunk_size,
        subset_size,
        int(search_cfg.get("random_audit_samples", 10000)),
        int(cfg["runtime"].get("random_seed", 20260614)) + stable_seed(collection, spill_id, plane, subset_size) % 100000,
        device,
    )
    best = sorted((beam[:1] + rnd[:1]), key=lambda score: score.subset_score, reverse=True)
    for audit_type, scored in (("BEAM_AUDIT", beam), ("RANDOM_AUDIT", rnd)):
        if not scored:
            continue
        improvement = scored[0].subset_score - screened_score
        expanded = improvement > float(search_cfg.get("audit_improvement_threshold", 0.01))
        audit_rows.append(
            {
                "collection": collection,
                "spill_id": spill_id,
                "plane": plane,
                "subset_size": subset_size,
                "audit_type": audit_type,
                "best_audit_score": f"{scored[0].subset_score:.9g}",
                "screened_winner_score": f"{screened_score:.9g}",
                "improvement": f"{improvement:.9g}",
                "pool_expanded": str(expanded).lower(),
                "bpm_members": ",".join(str(int(bpm_indices[pos])) for pos in scored[0].subset),
            }
        )
    return best
