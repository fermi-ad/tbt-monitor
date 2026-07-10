"""Output-contract checks for Best-BPM mining runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from .artifact_selection import FIELDS as ARTIFACT_SELECTION_FIELDS
from .clustering import CLUSTER_FIELDS, RANK_FIELDS as CLUSTER_RANK_FIELDS, SUMMARY_FIELDS as CLUSTER_SUMMARY_FIELDS
from .evolution import FINALIST_FIELDS, SIZE_FIELDS, SUMMARY_FIELDS as EVOLUTION_SUMMARY_FIELDS, WINDOW_FIELDS as EVOLUTION_WINDOW_FIELDS
from .fixed_sets import FIXED_EVAL_FIELDS, FIXED_SUMMARY_FIELDS
from .heldout import HELDOUT_FIELDS, HELDOUT_SUMMARY_FIELDS
from .handoff import HANDOFF_EVENT_FIELDS, VISIBILITY_SUMMARY_FIELDS, WINDOW_VISIBILITY_FIELDS
from .identity import channel_token, identity_fields, indices_from_mask, manifest_by_index, parse_indices
from .io import atomic_write_text, read_csv
from .schema import (
    BEST_SUBSET_FIELDS,
    BPM_INDEX_FIELDS,
    CHANNEL_FIELDS,
    CONSENSUS_SUMMARY_FIELDS,
    CONSENSUS_WINDOW_FIELDS,
    GLOBAL_BPM_STATS_FIELDS,
    PER_BPM_INJECTION_FIELDS,
    PER_BPM_SUMMARY_FIELDS,
    PER_BPM_WINDOW_FIELDS,
    REJECTION_FIELDS,
    SPECTRAL_CACHE_FIELDS,
    SPILLS_FIELDS,
)


STATISTICS_SCHEMAS: dict[str, Sequence[str]] = {
    "bpm_global_statistics.csv": GLOBAL_BPM_STATS_FIELDS,
    "bpm_bootstrap_intervals.csv": GLOBAL_BPM_STATS_FIELDS,
    "bpm_rank_stability.csv": ["plane", "metric", "value", "detail"],
    "subset_stability.csv": ["plane", "subset_size", "median_score", "score_mad", "row_count"],
    "fixed_sets_train_A_test_B.csv": ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"],
    "fixed_sets_train_B_test_A.csv": ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"],
    "fixed_sets_crossfit_summary.csv": ["plane", "subset_size", "train_collection", "test_collection", "fixed_members", "test_median_score"],
    "paired_method_tests.csv": [
        "plane",
        "comparison",
        "median_paired_difference",
        "bootstrap_ci_low",
        "bootstrap_ci_high",
        "permutation_p_value",
        "fdr_q_value",
        "effect_size",
        "note",
    ],
    "subset_size_pareto.csv": ["plane", "subset_size", "median_score", "median_visible_fraction", "compute_cost", "pareto_frontier"],
    "bpm_marginal_value.csv": ["plane", "bpm_name", "approx_marginal_value", "samples"],
    "bpm_pair_synergy.csv": ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"],
    "bpm_coselection.csv": ["plane", "bpm_a", "bpm_b", "pair_count", "median_pair_score"],
}

FOLLOWUP_CSV_SCHEMAS: list[tuple[str, Sequence[str], int]] = [
    ("statistics/fixed_set_direct_evaluation.csv", FIXED_EVAL_FIELDS, 1),
    ("statistics/fixed_vs_dynamic_direct_summary.csv", FIXED_SUMMARY_FIELDS, 1),
    ("evolution/finalist_heldout_spectral_support.csv", HELDOUT_FIELDS, 1),
    ("evolution/heldout_spectral_support_summary.csv", HELDOUT_SUMMARY_FIELDS, 1),
    ("handoff/bpm_window_visibility.csv", WINDOW_VISIBILITY_FIELDS, 1),
    ("handoff/bpm_handoff_events.csv", HANDOFF_EVENT_FIELDS, 1),
    ("handoff/bpm_visibility_summary.csv", VISIBILITY_SUMMARY_FIELDS, 1),
]

FOLLOWUP_FILES = [
    "statistics/fixed_set_direct_summary.md",
    "evolution/heldout_spectral_support_summary.md",
    "handoff/handoff_summary.md",
]


def _csv_header(path: Path) -> list[str]:
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration:
            return []


def _csv_row_count(path: Path) -> int:
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def _csv_check(
    root: Path,
    rel: str,
    fields: Sequence[str],
    min_rows: int,
    max_count_bytes: int,
    count_large_csv: bool,
) -> dict[str, object]:
    path = root / rel
    check: dict[str, object] = {"path": rel, "kind": "csv", "required": True, "status": "ok", "messages": []}
    messages: list[str] = check["messages"]  # type: ignore[assignment]
    if not path.exists():
        check["status"] = "fail"
        messages.append("missing")
        return check
    size = path.stat().st_size
    check["bytes"] = size
    if size == 0:
        check["status"] = "fail"
        messages.append("empty file")
        return check
    header = _csv_header(path)
    check["columns"] = header
    missing_columns = [field for field in fields if field not in header]
    if missing_columns:
        check["status"] = "fail"
        messages.append("missing columns: " + ",".join(missing_columns))
    should_count = count_large_csv or size <= max_count_bytes
    if should_count:
        rows = _csv_row_count(path)
        check["rows"] = rows
        if rows < min_rows:
            check["status"] = "fail"
            messages.append(f"row count {rows} < required {min_rows}")
    else:
        check["rows"] = None
        check["row_count_skipped"] = True
        messages.append(f"row count skipped for large CSV ({size} bytes)")
    return check


def _file_check(root: Path, rel: str, min_bytes: int = 1) -> dict[str, object]:
    path = root / rel
    check: dict[str, object] = {"path": rel, "kind": "file", "required": True, "status": "ok", "messages": []}
    messages: list[str] = check["messages"]  # type: ignore[assignment]
    if not path.exists():
        check["status"] = "fail"
        messages.append("missing")
        return check
    size = path.stat().st_size
    check["bytes"] = size
    if size < min_bytes:
        check["status"] = "fail"
        messages.append(f"bytes {size} < required {min_bytes}")
    return check


def _optional_progress_check(root: Path) -> dict[str, object]:
    progress = root / "subset_search" / "progress"
    check: dict[str, object] = {
        "path": "subset_search/progress",
        "kind": "directory",
        "required": False,
        "status": "ok",
        "messages": [],
    }
    messages: list[str] = check["messages"]  # type: ignore[assignment]
    shards = sorted(progress.glob("shard_*.json")) if progress.exists() else []
    check["shard_files"] = len(shards)
    if not shards:
        check["status"] = "warn"
        messages.append("no shard progress files found; older runs may not include them")
    return check


def _directory_has_artifacts(root: Path, rel: str) -> dict[str, object]:
    path = root / rel
    check: dict[str, object] = {"path": rel, "kind": "directory", "required": True, "status": "ok", "messages": []}
    messages: list[str] = check["messages"]  # type: ignore[assignment]
    if not path.exists():
        check["status"] = "fail"
        messages.append("missing")
        return check
    artifacts = [item for item in path.rglob("*") if item.is_file() and item.stat().st_size > 0]
    check["artifact_files"] = len(artifacts)
    if not artifacts:
        check["status"] = "fail"
        messages.append("no non-empty files")
    return check


def _best_subset_checks(subset_sizes: Iterable[int]) -> list[tuple[str, Sequence[str], int]]:
    checks: list[tuple[str, Sequence[str], int]] = []
    for size in subset_sizes:
        checks.append((f"subset_search/best{size}/best{size}_results.csv", BEST_SUBSET_FIELDS, 1))
        name = "best1_rankings.csv" if size == 1 else f"best{size}_top_candidates.csv"
        checks.append((f"subset_search/best{size}/{name}", BEST_SUBSET_FIELDS, 1))
        if size in {5, 10}:
            checks.append((f"subset_search/best{size}/best{size}_pool.csv", ["collection", "spill_id", "plane", "subset_size", "pool_size", "bpm_indices", "bpm_members", "source"], 1))
    return checks


def _subset_identity_check(root: Path, subset_sizes: Sequence[int]) -> dict[str, object]:
    check: dict[str, object] = {
        "path": "subset_search/best*/best*_results.csv",
        "kind": "cross-table identity",
        "required": True,
        "status": "ok",
        "messages": [],
    }
    messages: list[str] = check["messages"]  # type: ignore[assignment]
    bpm_path = root / "manifest" / "bpm_index.csv"
    spills_path = root / "manifest" / "spills.csv"
    if not bpm_path.is_file() or not spills_path.is_file():
        check["status"] = "fail"
        messages.append("manifest tables required for exact identity verification are missing")
        return check

    bpm_rows = read_csv(bpm_path)
    bpm_keys = [(row.get("plane", ""), row.get("bpm_index", "")) for row in bpm_rows]
    duplicate_manifest_keys = len(bpm_keys) - len(set(bpm_keys))
    if duplicate_manifest_keys:
        messages.append(f"duplicate plane/BPM-index manifest keys: {duplicate_manifest_keys}")
    duplicate_source_keys = len(bpm_rows) - len(
        {(row.get("plane", ""), row.get("source_key", "")) for row in bpm_rows}
    )
    if duplicate_source_keys:
        messages.append(f"duplicate plane/source-key manifest identities: {duplicate_source_keys}")
    ring_mismatches = 0
    for row in bpm_rows:
        token = channel_token(row.get("source_key"))
        if token and str(row.get("ring_order", "")) != token[2:]:
            ring_mismatches += 1
    if ring_mismatches:
        messages.append(f"manifest ring-order/token mismatches: {ring_mismatches}")
    meta_by_index = manifest_by_index(bpm_rows)

    expected_keys: set[tuple[str, str, str]] = set()
    for row in read_csv(spills_path):
        for plane, field in (("H", "usable_h"), ("V", "usable_v")):
            if str(row.get(field, "")).lower() == "true":
                expected_keys.add((row.get("collection", ""), row.get("spill_id", ""), plane))

    rows_by_subset: dict[int, int] = {}
    for subset_size in subset_sizes:
        candidates = (
            root / "subset_search" / f"best{subset_size}" / f"best{subset_size}_results.csv",
            root / "subset_search" / f"best{subset_size}_results.csv",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            messages.append(f"missing exact result table for Best-{subset_size}")
            continue
        rows = read_csv(path)
        rows_by_subset[int(subset_size)] = len(rows)
        keys = [(row.get("collection", ""), row.get("spill_id", ""), row.get("plane", "")) for row in rows]
        duplicate_keys = len(keys) - len(set(keys))
        if duplicate_keys:
            messages.append(f"Best-{subset_size} duplicate spill-plane keys: {duplicate_keys}")
        if set(keys) != expected_keys or len(keys) != len(expected_keys):
            messages.append(
                f"Best-{subset_size} spill-plane coverage differs from usable manifest: "
                f"expected={len(expected_keys)} rows={len(keys)} unique={len(set(keys))}"
            )

        failure_counts: Counter[str] = Counter()
        for row in rows:
            plane = row.get("plane", "")
            declared_size = int(row.get("subset_size") or 0)
            raw_indices = [part.strip() for part in row.get("bpm_indices", "").split(",") if part.strip()]
            indices = parse_indices(row.get("bpm_indices"))
            masked = indices_from_mask(row.get("subset_mask"))
            if declared_size != subset_size:
                failure_counts["declared_size"] += 1
            if len(raw_indices) != subset_size or len(indices) != subset_size:
                failure_counts["index_cardinality"] += 1
            if sorted(indices) != sorted(masked):
                failure_counts["mask_identity"] += 1
            expected_identity = identity_fields(plane, indices, meta_by_index)
            for field in ("bpm_indices", "bpm_members", "bpm_source_keys", "bpm_digitizers"):
                if str(row.get(field, "")) != expected_identity[field]:
                    failure_counts[field] += 1
        for failure, count in sorted(failure_counts.items()):
            messages.append(f"Best-{subset_size} exact identity failure {failure}: {count}")

    check["usable_spill_plane_rows"] = len(expected_keys)
    check["rows_by_subset"] = rows_by_subset
    check["manifest_rows"] = len(bpm_rows)
    if messages:
        check["status"] = "fail"
    return check


def verify_best_bpm_outputs(
    root: Path,
    subset_sizes: Sequence[int] = (1, 3, 5, 10),
    max_count_bytes: int = 1_000_000_000,
    count_large_csv: bool = False,
    write_outputs: bool = True,
    include_followups: bool = False,
) -> dict[str, object]:
    root = Path(root)
    if not root.exists():
        return {
            "status": "fail",
            "root": str(root),
            "checks": [
                {
                    "path": ".",
                    "kind": "directory",
                    "required": True,
                    "status": "fail",
                    "messages": ["root directory missing"],
                }
            ],
            "fail_count": 1,
            "warn_count": 0,
            "subset_sizes": list(subset_sizes),
        }
    csv_checks: list[tuple[str, Sequence[str], int]] = [
        ("manifest/spills.csv", SPILLS_FIELDS, 1),
        ("manifest/bpm_index.csv", BPM_INDEX_FIELDS, 1),
        ("manifest/channels.csv", CHANNEL_FIELDS, 1),
        ("manifest/rejections.csv", REJECTION_FIELDS, 0),
        ("cache/index/spectral_cache.csv", SPECTRAL_CACHE_FIELDS, 1),
        ("per_bpm/per_bpm_window_features.csv", PER_BPM_WINDOW_FIELDS, 1),
        ("per_bpm/per_bpm_injection_features.csv", PER_BPM_INJECTION_FIELDS, 1),
        ("per_bpm/per_bpm_spill_summary.csv", PER_BPM_SUMMARY_FIELDS, 1),
        ("consensus/spill_consensus_windows.csv", CONSENSUS_WINDOW_FIELDS, 1),
        ("consensus/spill_consensus_summary.csv", CONSENSUS_SUMMARY_FIELDS, 1),
        ("consensus/consensus_class_counts.csv", ["consensus_label", "count"], 1),
        *_best_subset_checks(subset_sizes),
        ("subset_search/audit_results.csv", ["collection", "spill_id", "plane", "subset_size", "audit_type", "best_audit_score", "screened_winner_score", "improvement", "pool_expanded", "bpm_members"], 0),
        ("evolution/subset_evolution_windows.csv", EVOLUTION_WINDOW_FIELDS, 1),
        ("evolution/subset_evolution_summary.csv", EVOLUTION_SUMMARY_FIELDS, 1),
        ("evolution/subset_size_comparison.csv", SIZE_FIELDS, 1),
        ("evolution/finalist_reevaluation.csv", FINALIST_FIELDS, 1),
        ("clustering/spill_clusters.csv", CLUSTER_FIELDS, 1),
        ("clustering/cluster_summary.csv", CLUSTER_SUMMARY_FIELDS, 1),
        ("clustering/cluster_bpm_rankings.csv", CLUSTER_RANK_FIELDS, 1),
        ("artifact_selection/artifact_manifest.csv", ARTIFACT_SELECTION_FIELDS, 1),
        ("artifacts/spills/selected_subset_membership.csv", ["collection", "spill_id", "plane", "subset_size", "bpm_members", "subset_score", "q_hat"], 1),
    ]
    for name, fields in STATISTICS_SCHEMAS.items():
        min_rows = 1 if name in {"bpm_global_statistics.csv", "subset_stability.csv", "paired_method_tests.csv"} else 0
        csv_checks.append((f"statistics/{name}", fields, min_rows))

    checks: list[dict[str, object]] = [
        _csv_check(root, rel, fields, min_rows, max_count_bytes, count_large_csv)
        for rel, fields, min_rows in csv_checks
    ]
    checks.append(_subset_identity_check(root, subset_sizes))
    for rel in [
        "manifest/dataset_summary.md",
        "cache/index/spectral_cache_summary.md",
        "per_bpm/per_bpm_summary.md",
        "consensus/consensus_summary.md",
        "subset_search/subset_search_summary.md",
        "evolution/evolution_summary.md",
        "statistics/statistics_summary.md",
        "clustering/clustering_summary.md",
        "artifact_selection/artifact_selection_summary.md",
        "artifacts/artifact_generation_summary.md",
        "reports/strong_bpm_analysis_summary.md",
        "reports/strong_bpm_executive_summary.md",
    ]:
        checks.append(_file_check(root, rel))
    checks.append(_directory_has_artifacts(root, "artifacts/global"))
    checks.append(_directory_has_artifacts(root, "artifacts/spills"))
    checks.append(_optional_progress_check(root))
    if include_followups or any((root / rel).exists() for rel, _fields, _min_rows in FOLLOWUP_CSV_SCHEMAS):
        checks.extend(_followup_checks(root, max_count_bytes, count_large_csv))

    fail_count = sum(1 for check in checks if check["status"] == "fail")
    warn_count = sum(1 for check in checks if check["status"] == "warn")
    status = "ok" if fail_count == 0 else "fail"
    payload: dict[str, object] = {
        "status": status,
        "root": str(root),
        "checks": checks,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "subset_sizes": list(subset_sizes),
    }
    if write_outputs:
        atomic_write_text(root / "logs" / "best_bpm_verification.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        atomic_write_text(root / "logs" / "best_bpm_verification_report.md", render_verification_report(payload))
    return payload


def _followup_checks(root: Path, max_count_bytes: int, count_large_csv: bool) -> list[dict[str, object]]:
    checks = [_csv_check(root, rel, fields, min_rows, max_count_bytes, count_large_csv) for rel, fields, min_rows in FOLLOWUP_CSV_SCHEMAS]
    checks.extend(_file_check(root, rel) for rel in FOLLOWUP_FILES)
    checks.append(_followup_semantic_check(root))
    if (root / "artifacts").exists():
        checks.append(_directory_has_artifacts(root, "artifacts/spills"))
        checks.append(_directory_has_artifacts(root, "artifacts/global"))
    if (root / "handoff").exists():
        checks.append(_directory_has_artifacts(root, "handoff"))
    return checks


def _number(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _parts(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _followup_semantic_check(root: Path) -> dict[str, object]:
    check: dict[str, object] = {
        "path": ".",
        "kind": "followup semantics",
        "required": True,
        "status": "ok",
        "messages": [],
    }
    messages: list[str] = check["messages"]  # type: ignore[assignment]

    fixed_path = root / "statistics" / "fixed_set_direct_evaluation.csv"
    if fixed_path.is_file():
        rows = read_csv(fixed_path)
        keys = [
            (
                row.get("collection", ""),
                row.get("spill_id", ""),
                row.get("plane", ""),
                row.get("method", ""),
                row.get("subset_size", ""),
                row.get("train_collection", ""),
                row.get("test_collection", ""),
            )
            for row in rows
        ]
        if len(keys) != len(set(keys)):
            messages.append(f"fixed-set evaluation duplicate keys: {len(keys) - len(set(keys))}")
        bad_scores = 0
        bad_identity = 0
        for row in rows:
            visible = _number(row.get("visible_fraction"))
            prominence = _number(row.get("median_prominence"))
            score = _number(row.get("score"))
            flags = str(row.get("quality_flags", ""))
            finite_score_state = math.isfinite(visible) and math.isfinite(score)
            if math.isfinite(prominence):
                expected = visible * max(0.0, min(1.0, prominence / 12.0))
                valid_score = finite_score_state and abs(score - expected) <= 1e-7
            else:
                valid_score = (
                    finite_score_state
                    and visible == 0.0
                    and score == 0.0
                    and "NO_VISIBLE_TUNE" in flags
                )
            if not valid_score:
                bad_scores += 1
            method = str(row.get("method", ""))
            if method.startswith(("dynamic_best", "fixed_top")):
                size = int(row.get("subset_size") or 0)
                if (
                    len(parse_indices(row.get("bpm_indices"))) != size
                    or len(_parts(row.get("bpm_members"))) != size
                    or len(_parts(row.get("bpm_source_keys"))) != size
                    or len(_parts(row.get("bpm_digitizers"))) != size
                ):
                    bad_identity += 1
        if bad_scores:
            messages.append(f"fixed/dynamic rows violate the shared score contract: {bad_scores}")
        if bad_identity:
            messages.append(f"fixed/dynamic rows have incomplete exact membership: {bad_identity}")

    heldout_path = root / "evolution" / "finalist_heldout_spectral_support.csv"
    if heldout_path.is_file():
        rows = read_csv(heldout_path)
        keys = [
            (
                row.get("collection", ""),
                row.get("spill_id", ""),
                row.get("plane", ""),
                row.get("subset_size", ""),
                row.get("aggregator", ""),
                row.get("source_rank", ""),
            )
            for row in rows
        ]
        if len(keys) != len(set(keys)):
            messages.append(f"held-out evaluation duplicate keys: {len(keys) - len(set(keys))}")
        bad_rows = 0
        support_fields = (
            "heldout_candidate_fraction",
            "heldout_power_support",
            "heldout_prominence_at_qhat",
            "selected_power_support",
            "selected_prominence_at_qhat",
            "selected_vs_heldout_delta",
        )
        for row in rows:
            size = int(row.get("subset_size") or 0)
            flags = str(row.get("quality_flags", ""))
            identity_bad = (
                int(row.get("selected_bpm_count") or 0) != size
                or int(row.get("heldout_bpm_count") or 0) <= 0
                or len(parse_indices(row.get("bpm_indices"))) != size
                or "SELECTED_CHANNEL_COUNT_MISMATCH" in flags
            )
            q_hat = _number(row.get("q_hat"))
            support_finite = [math.isfinite(_number(row.get(field))) for field in support_fields]
            if math.isfinite(q_hat):
                metric_bad = not all(support_finite) or "NO_VALID_Q" in flags
            else:
                metric_bad = "NO_VALID_Q" not in flags or any(support_finite)
            if identity_bad or metric_bad:
                bad_rows += 1
        if bad_rows:
            messages.append(f"held-out rows fail cardinality, finite-metric, or quality checks: {bad_rows}")

    events_path = root / "handoff" / "bpm_handoff_events.csv"
    if events_path.is_file():
        rows = read_csv(events_path)
        keys = [
            (
                row.get("collection", ""),
                row.get("spill_id", ""),
                row.get("plane", ""),
                row.get("subset_size", ""),
                row.get("window_index", ""),
            )
            for row in rows
        ]
        if len(keys) != len(set(keys)):
            messages.append(f"handoff event duplicate keys: {len(keys) - len(set(keys))}")
        groups: dict[tuple[str, str, str], dict[int, int]] = {}
        for row in rows:
            group = (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
            size = int(row.get("subset_size") or 0)
            counts = groups.setdefault(group, {})
            counts[size] = counts.get(size, 0) + 1
        incomplete_groups = [
            group
            for group, counts in groups.items()
            if set(counts) != {1, 3, 5, 10} or len(set(counts.values())) != 1
        ]
        if incomplete_groups:
            messages.append(f"handoff groups lack complete equal-length Top-1/3/5/10 transitions: {len(incomplete_groups)}")
        bad_states = 0
        for row in rows:
            previous = set(_parts(row.get("previous_members")))
            current = set(_parts(row.get("current_members")))
            label = row.get("event_label", "")
            expected = None
            if not previous and not current:
                expected = "NO_VISIBLE_SET"
                if abs(_number(row.get("jaccard_vs_previous")) - 1.0) > 1e-12:
                    bad_states += 1
            elif previous and not current:
                expected = "VISIBILITY_LOSS"
            elif not previous and current:
                expected = "VISIBILITY_RECOVERY"
            if expected is not None and label != expected:
                bad_states += 1
            if label == "PERSISTENT_HANDOFF" and (not previous or not current):
                bad_states += 1
        if bad_states:
            messages.append(f"handoff rows have inconsistent visible-set transition labels: {bad_states}")

    visibility_path = root / "handoff" / "bpm_window_visibility.csv"
    if visibility_path.is_file():
        visibility_rows = read_csv(visibility_path)
        bad_flags = 0
        for row in visibility_rows:
            flags = [row.get(f"is_top{size}_visible", "") for size in (1, 3, 5, 10)]
            if any(flag not in {"true", "false"} for flag in flags):
                bad_flags += 1
                continue
            selected = [flag == "true" for flag in flags]
            if any(selected[index] and not selected[index + 1] for index in range(3)):
                bad_flags += 1
        if bad_flags:
            messages.append(f"handoff visibility rows violate nested Top-1/3/5/10 membership: {bad_flags}")

        handoff_dir = visibility_path.parent
        for plane in ("h", "v"):
            for stem in (
                "handoff_rate_vs_turn",
                "visible_bpm_fraction_vs_turn",
                "visible_set_support_vs_turn",
                "bpm_visibility_cluster_map",
                "top_bpm_membership_vs_turn",
            ):
                for suffix in (".png", "_caption.md"):
                    path = handoff_dir / f"{stem}_{plane}{suffix}"
                    if not path.is_file() or path.stat().st_size == 0:
                        messages.append(f"missing required handoff artifact: {path.name}")

        spill_keys = {
            (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
            for row in visibility_rows
        }
        output_names = [f"spill_{spill_id}_{plane.lower()}" for _collection, spill_id, plane in spill_keys]
        if len(output_names) != len(set(output_names)):
            messages.append("handoff spill identifiers collide across collections")
        for stem in output_names:
            for artifact in ("bpm_visibility_handoff", "top_sets_vs_turn"):
                for suffix in (".png", "_caption.md"):
                    path = handoff_dir / f"{stem}_{artifact}{suffix}"
                    if not path.is_file() or path.stat().st_size == 0:
                        messages.append(f"missing required per-spill handoff artifact: {path.name}")

    poster_manifest = root / "artifacts" / "poster" / "selected_poster_artifacts.csv"
    if poster_manifest.is_file():
        rows = read_csv(poster_manifest)
        if {row.get("plane", "") for row in rows} != {"H", "V"}:
            messages.append("curated poster examples are not plane-balanced")
        missing_figures = []
        for row in rows:
            for filename in str(row.get("recommended_files", "")).split(";"):
                if filename and not (poster_manifest.parent / filename).is_file():
                    missing_figures.append(filename)
        if missing_figures:
            messages.append(f"recommended poster figures are missing: {len(missing_figures)}")

    if messages:
        check["status"] = "fail"
    return check


def verify_best_bpm_followups(
    root: Path,
    max_count_bytes: int = 1_000_000_000,
    count_large_csv: bool = False,
    write_outputs: bool = True,
) -> dict[str, object]:
    root = Path(root)
    checks: list[dict[str, object]]
    if not root.exists():
        checks = [{"path": ".", "kind": "directory", "required": True, "status": "fail", "messages": ["root directory missing"]}]
    else:
        checks = _followup_checks(root, max_count_bytes, count_large_csv)
    fail_count = sum(1 for check in checks if check["status"] == "fail")
    warn_count = sum(1 for check in checks if check["status"] == "warn")
    payload: dict[str, object] = {
        "status": "ok" if fail_count == 0 else "fail",
        "root": str(root),
        "checks": checks,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "subset_sizes": [],
        "followups_only": True,
    }
    if write_outputs:
        atomic_write_text(root / "logs" / "best_bpm_followup_verification.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
        atomic_write_text(root / "logs" / "best_bpm_followup_verification_report.md", render_verification_report(payload))
    return payload


def render_verification_report(payload: dict[str, object]) -> str:
    checks = payload["checks"]  # type: ignore[index]
    lines = [
        "# Best-BPM Output Verification",
        "",
        f"- status: `{payload['status']}`",
        f"- failures: `{payload['fail_count']}`",
        f"- warnings: `{payload['warn_count']}`",
        f"- subset sizes: `{','.join(str(size) for size in payload['subset_sizes'])}`",
        "",
        "| Status | Path | Detail |",
        "| --- | --- | --- |",
    ]
    for check in checks:  # type: ignore[assignment]
        messages = "; ".join(check.get("messages", [])) or "ok"
        if check.get("rows") is not None:
            messages += f"; rows={check['rows']}"
        elif check.get("row_count_skipped"):
            messages += "; rows=skipped"
        if check.get("artifact_files") is not None:
            messages += f"; files={check['artifact_files']}"
        if check.get("shard_files") is not None:
            messages += f"; shard_files={check['shard_files']}"
        lines.append(f"| `{check['status']}` | `{check['path']}` | {messages} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Best-BPM mining output directory against the expected artifact contract")
    parser.add_argument("--root", required=True, help="Best-BPM output directory")
    parser.add_argument("--subset-sizes", nargs="+", type=int, default=[1, 3, 5, 10])
    parser.add_argument("--max-count-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--count-large-csv", action="store_true")
    parser.add_argument("--include-followups", action="store_true", help="also require follow-up output groups when verifying a full run")
    parser.add_argument("--followups-only", action="store_true", help="verify a sidecar follow-up output root instead of a full Best-BPM run")
    args = parser.parse_args(argv)
    if args.followups_only:
        payload = verify_best_bpm_followups(Path(args.root), args.max_count_bytes, args.count_large_csv, True)
    else:
        payload = verify_best_bpm_outputs(Path(args.root), args.subset_sizes, args.max_count_bytes, args.count_large_csv, True, args.include_followups)
    print(render_verification_report(payload))
    return 0 if payload["status"] == "ok" else 1
