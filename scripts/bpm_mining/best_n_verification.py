"""Fail-closed verification for merged Best-N publication outputs."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .best_n import (
    BEST1_MEMBERSHIP_FREQUENCY_FIELDS,
    BEST1_MEMBERSHIP_SUMMARY_FIELDS,
    CROSS_SPILL_NULL_BLOCK_SPILLS,
    CROSS_SPILL_NULL_DRAWS,
    CROSS_SPILL_NULL_FIELDS,
    best1_membership_rows,
    cross_spill_null_rows,
    recommended_n,
)
from .contracts import CONTRACT_SCHEMA_VERSION, file_sha256
from .identity import indices_from_mask, parse_indices
from .io import atomic_write_text, read_csv


CRITICAL_CURVE_NUMERIC = (
    "q_hat",
    "subset_score",
    "test_blind_q_hat",
    "test_blind_abs_q_delta",
    "test_peak_prominence_at_qhat",
    "test_power_support_at_qhat",
)

CRITICAL_VALIDATION_NUMERIC = (
    "train_q_hat",
    "train_score",
    "selected_test_blind_q_hat",
    "heldout_blind_q_hat",
    "blind_selected_heldout_abs_q_delta",
    "test_peak_prominence_at_qhat",
    "test_power_support_at_qhat",
    "heldout_power_support_at_qhat",
    "heldout_prominence_at_qhat",
)

CRITICAL_SUMMARY_NUMERIC = (
    "median_subset_score",
    "median_test_peak_prominence",
    "median_test_power_support",
    "median_heldout_power_support",
    "median_heldout_prominence",
    "blind_q_agreement_rate",
    "median_blind_selected_heldout_abs_q_delta",
)

REQUIRED_FILES = (
    "run_contract.json",
    "best_n_curve_rows.csv",
    "best_n_disjoint_validation.csv",
    "best_n_summary.csv",
    "best_n_summary.md",
    "best_n_summary_by_collection.csv",
    "best_n_cross_collection_transfer.csv",
    "best_n_cross_collection_transfer.md",
    "best_n_cross_spill_null.csv",
    "best_n_best1_membership_frequency.csv",
    "best_n_best1_membership_summary.csv",
)

REQUIRED_PLOTS = tuple(
    f"best_n_{stem}_{plane}.png"
    for plane in ("h", "v")
    for stem in ("validation", "heldout_power", "test_prominence", "q_delta", "score")
)


def _finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _parts(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _cache_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    return str(row.get("collection", "")), str(row.get("spill_id", "")), str(row.get("plane", ""))


def _issue(
    issues: list[dict[str, object]],
    severity: str,
    code: str,
    message: str,
    count: int = 1,
    examples: Sequence[object] = (),
) -> None:
    issues.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            "count": int(count),
            "examples": [str(value) for value in examples[:5]],
        }
    )


def _check_memberships(
    rows: Sequence[dict[str, str]],
    table: str,
    issues: list[dict[str, object]],
) -> None:
    failures: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        n = int(row.get("subset_size") or 0)
        key = "/".join(_cache_key(row)) + f"/N{n}"
        indices = parse_indices(row.get("bpm_indices"))
        fields = {
            "indices": indices,
            "members": _parts(row.get("bpm_members")),
            "source_keys": _parts(row.get("bpm_source_keys")),
            "digitizers": _parts(row.get("bpm_digitizers")),
        }
        for label, values in fields.items():
            if len(values) != n:
                failures[f"{label}_cardinality"] += 1
                examples[f"{label}_cardinality"].append(key)
        if len(set(indices)) != len(indices):
            failures["duplicate_indices"] += 1
            examples["duplicate_indices"].append(key)
        if table == "curve" and sorted(indices_from_mask(row.get("subset_mask"))) != sorted(indices):
            failures["mask_identity_mismatch"] += 1
            examples["mask_identity_mismatch"].append(key)
    for code, count in sorted(failures.items()):
        _issue(
            issues,
            "error",
            f"{table}_{code}",
            f"{table} rows violate exact membership identity: {code.replace('_', ' ')}",
            count,
            examples[code],
        )


def _check_numeric_fields(
    rows: Sequence[dict[str, str]],
    fields: Sequence[str],
    table: str,
    issues: list[dict[str, object]],
) -> None:
    for field in fields:
        bad = [index for index, row in enumerate(rows) if not math.isfinite(_finite(row.get(field)))]
        if bad:
            _issue(
                issues,
                "error",
                f"{table}_nonfinite_{field}",
                f"{table}.{field} contains non-finite or missing values",
                len(bad),
                bad,
            )


def _normalized_rows(
    rows: Sequence[Mapping[str, object]],
    fields: Sequence[str],
) -> list[dict[str, str]]:
    return [
        {field: "" if row.get(field) is None else str(row.get(field, "")) for field in fields}
        for row in rows
    ]


def _check_derived_control(
    actual: Sequence[Mapping[str, object]],
    expected: Sequence[Mapping[str, object]],
    fields: Sequence[str],
    label: str,
    issues: list[dict[str, object]],
) -> None:
    actual_normalized = _normalized_rows(actual, fields)
    expected_normalized = _normalized_rows(expected, fields)
    if actual_normalized == expected_normalized:
        return
    mismatches = []
    for index in range(max(len(actual_normalized), len(expected_normalized))):
        actual_row = actual_normalized[index] if index < len(actual_normalized) else None
        expected_row = expected_normalized[index] if index < len(expected_normalized) else None
        if actual_row != expected_row:
            mismatches.append(index)
    _issue(
        issues,
        "error",
        f"{label}_mismatch",
        f"{label.replace('_', ' ')} does not match a deterministic recomputation from accepted Best-N rows",
        len(mismatches),
        mismatches,
    )


def _check_contiguous_n(
    rows: Sequence[dict[str, str]],
    expected_max_n: int,
    table: str,
    include_fold: bool,
    issues: list[dict[str, object]],
) -> None:
    grouped: dict[tuple[object, ...], list[int]] = defaultdict(list)
    for row in rows:
        key: tuple[object, ...] = _cache_key(row)
        if include_fold:
            key = (*key, int(row.get("fold") or 0))
        grouped[key].append(int(row.get("subset_size") or 0))
    expected = list(range(1, expected_max_n + 1))
    bad = [key for key, values in grouped.items() if sorted(values) != expected]
    if bad:
        _issue(
            issues,
            "error",
            f"{table}_noncontiguous_n",
            f"{table} groups do not each contain exactly one row for every N=1..{expected_max_n}",
            len(bad),
            bad,
        )


def verification_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Best-N Verification",
        "",
        f"Status: **{str(report.get('status', '')).upper()}**",
        "",
        f"- curve cache rows: `{report.get('curve_cache_key_count', 0)}`",
        f"- curve rows: `{report.get('curve_row_count', 0)}`",
        f"- validation cache rows: `{report.get('validation_cache_key_count', 0)}`",
        f"- validation rows: `{report.get('validation_row_count', 0)}`",
        f"- maximum N: `{report.get('expected_max_n', 0)}`",
        f"- folds: `{report.get('expected_folds', 0)}`",
        f"- cross-spill null rows: `{report.get('cross_spill_null', {}).get('row_count', 0) if isinstance(report.get('cross_spill_null'), Mapping) else 0}`",
        f"- Best-1 membership rows: `{report.get('best1_membership', {}).get('frequency_row_count', 0) if isinstance(report.get('best1_membership'), Mapping) else 0}`",
        "",
        "## Recommendations",
        "",
    ]
    recommendations = report.get("recommendations", {})
    if isinstance(recommendations, Mapping):
        for plane in ("H", "V"):
            value = recommendations.get(plane, {})
            if isinstance(value, Mapping):
                lines.append(
                    f"- {plane}: `{value.get('recommended_n', '')}`; {value.get('status', '')}"
                )
    lines.extend(["", "## Findings", ""])
    issues = report.get("issues", [])
    if not issues:
        lines.append("No verification findings.")
    else:
        lines.extend(
            [
                "| severity | code | count | finding |",
                "| --- | --- | ---: | --- |",
            ]
        )
        for raw in issues:
            issue = raw if isinstance(raw, Mapping) else {}
            lines.append(
                f"| {issue.get('severity', '')} | `{issue.get('code', '')}` | "
                f"{issue.get('count', '')} | {issue.get('message', '')} |"
            )
    return "\n".join(lines) + "\n"


def verify_best_n_outputs(
    root: Path,
    expected_max_n: int,
    expected_curve_cache_keys: int,
    expected_validation_cache_keys: int,
    expected_folds: int,
    tune_half_width: float = 0.0025,
    require_cross_collection: bool = True,
    require_plots: bool = True,
    write_outputs: bool = True,
) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    for filename in REQUIRED_FILES:
        if not (root / filename).is_file():
            _issue(issues, "error", "missing_file", f"required output is missing: {filename}")
    if require_plots:
        for filename in REQUIRED_PLOTS:
            path = root / filename
            if not path.is_file() or path.stat().st_size == 0:
                _issue(issues, "error", "missing_plot", f"required plot is missing or empty: {filename}")

    curve_path = root / "best_n_curve_rows.csv"
    validation_path = root / "best_n_disjoint_validation.csv"
    summary_path = root / "best_n_summary.csv"
    curve = read_csv(curve_path) if curve_path.is_file() else []
    validation = read_csv(validation_path) if validation_path.is_file() else []
    summary = read_csv(summary_path) if summary_path.is_file() else []

    contract: dict[str, object] = {}
    contract_path = root / "run_contract.json"
    if contract_path.is_file():
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _issue(issues, "error", "invalid_run_contract", f"run contract is not valid JSON: {exc}")
    if contract:
        if int(contract.get("contract_schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
            _issue(issues, "error", "run_contract_schema", "run contract schema is unsupported")
        if contract.get("analysis") not in {"best_n", "best_n_merged"}:
            _issue(issues, "error", "run_contract_analysis", "run contract does not identify a Best-N analysis")
        contract_expectations = {
            "max_n": expected_max_n,
            "folds": expected_folds,
        }
        for field, expected in contract_expectations.items():
            if int(contract.get(field) or 0) != expected:
                _issue(
                    issues,
                    "error",
                    f"run_contract_{field}",
                    f"run contract {field} does not match the verifier expectation {expected}",
                )
        if not math.isclose(_finite(contract.get("tune_half_width")), tune_half_width, rel_tol=0.0, abs_tol=1e-12):
            _issue(issues, "error", "run_contract_tune_tolerance", "run contract tune tolerance does not match verification")
        for field in ("config_sha256", "bpm_index_sha256", "spectral_cache_index_sha256"):
            value = str(contract.get(field, ""))
            if len(value) != 64:
                _issue(issues, "error", f"run_contract_{field}", f"run contract is missing a valid {field}")

    curve_keys = {_cache_key(row) for row in curve}
    validation_keys = {_cache_key(row) for row in validation}
    if len(curve_keys) != expected_curve_cache_keys:
        _issue(
            issues,
            "error",
            "curve_cache_key_count",
            f"expected {expected_curve_cache_keys} unique curve cache rows, found {len(curve_keys)}",
        )
    if len(validation_keys) != expected_validation_cache_keys:
        _issue(
            issues,
            "error",
            "validation_cache_key_count",
            f"expected {expected_validation_cache_keys} unique validation cache rows, found {len(validation_keys)}",
        )

    curve_identity = [(*_cache_key(row), int(row.get("subset_size") or 0)) for row in curve]
    curve_duplicates = len(curve_identity) - len(set(curve_identity))
    if curve_duplicates:
        _issue(issues, "error", "duplicate_curve_rows", "curve output contains duplicate keys", curve_duplicates)
    validation_identity = [
        (*_cache_key(row), int(row.get("fold") or 0), int(row.get("subset_size") or 0))
        for row in validation
    ]
    validation_duplicates = len(validation_identity) - len(set(validation_identity))
    if validation_duplicates:
        _issue(
            issues,
            "error",
            "duplicate_validation_rows",
            "validation output contains duplicate keys",
            validation_duplicates,
        )

    _check_contiguous_n(curve, expected_max_n, "curve", False, issues)
    _check_contiguous_n(validation, expected_max_n, "validation", True, issues)
    _check_memberships(curve, "curve", issues)
    _check_memberships(validation, "validation", issues)
    _check_numeric_fields(curve, CRITICAL_CURVE_NUMERIC, "curve", issues)
    _check_numeric_fields(validation, CRITICAL_VALIDATION_NUMERIC, "validation", issues)

    folds_by_key: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for row in validation:
        folds_by_key[_cache_key(row)].add(int(row.get("fold") or 0))
    expected_fold_set = set(range(expected_folds))
    bad_folds = [key for key, folds in folds_by_key.items() if folds != expected_fold_set]
    if bad_folds:
        _issue(
            issues,
            "error",
            "validation_fold_coverage",
            f"validation cache rows do not each contain folds 0..{expected_folds - 1}",
            len(bad_folds),
            bad_folds,
        )

    timing_bad = []
    channel_bad = []
    tune_bad = []
    for index, row in enumerate(validation):
        fit_end = _finite(row.get("fit_end_turn"))
        test_start = _finite(row.get("test_start_turn"))
        if not math.isfinite(fit_end) or not math.isfinite(test_start) or test_start < fit_end:
            timing_bad.append(index)
        n = int(row.get("subset_size") or 0)
        if int(row.get("train_channel_count") or 0) < n or int(row.get("heldout_channel_count") or 0) <= 0:
            channel_bad.append(index)
        for field in ("train_q_hat", "selected_test_blind_q_hat", "heldout_blind_q_hat"):
            value = _finite(row.get(field))
            if not (0.0 < value < 1.0):
                tune_bad.append(index)
                break
    if timing_bad:
        _issue(issues, "error", "validation_timing_overlap", "validation test windows overlap or precede fit windows", len(timing_bad), timing_bad)
    if channel_bad:
        _issue(issues, "error", "validation_channel_counts", "validation has too few training or held-out channels", len(channel_bad), channel_bad)
    if tune_bad:
        _issue(issues, "error", "validation_tune_range", "validation tune values fall outside the fractional range (0,1)", len(tune_bad), tune_bad)

    summary_keys = [(str(row.get("plane", "")), int(row.get("subset_size") or 0)) for row in summary]
    expected_summary_keys = {(plane, n) for plane in ("H", "V") for n in range(1, expected_max_n + 1)}
    if set(summary_keys) != expected_summary_keys or len(summary_keys) != len(expected_summary_keys):
        _issue(issues, "error", "summary_coverage", "summary does not contain exactly one H and V row for every N")
    _check_numeric_fields(summary, CRITICAL_SUMMARY_NUMERIC, "summary", issues)

    curve_counts = Counter((row.get("plane", ""), int(row.get("subset_size") or 0)) for row in curve)
    validation_counts = Counter((row.get("plane", ""), int(row.get("subset_size") or 0)) for row in validation)
    count_bad = []
    for row in summary:
        key = (row.get("plane", ""), int(row.get("subset_size") or 0))
        if int(row.get("curve_row_count") or 0) != curve_counts[key]:
            count_bad.append(f"curve:{key}")
        if int(row.get("validation_row_count") or 0) != validation_counts[key]:
            count_bad.append(f"validation:{key}")
    if count_bad:
        _issue(issues, "error", "summary_row_counts", "summary row counts do not match detailed outputs", len(count_bad), count_bad)

    collections = sorted({key[0] for key in curve_keys | validation_keys if key[0]})
    collection_rows = read_csv(root / "best_n_summary_by_collection.csv") if (root / "best_n_summary_by_collection.csv").is_file() else []
    expected_collection_summary = len(collections) * 2 * expected_max_n
    if len(collection_rows) != expected_collection_summary:
        _issue(
            issues,
            "error",
            "collection_summary_count",
            f"expected {expected_collection_summary} collection-summary rows, found {len(collection_rows)}",
        )
    transfer_rows = read_csv(root / "best_n_cross_collection_transfer.csv") if (root / "best_n_cross_collection_transfer.csv").is_file() else []
    expected_transfer = len(collections) * max(0, len(collections) - 1) * 2
    if require_cross_collection and len(collections) < 2:
        _issue(issues, "error", "cross_collection_unavailable", "publication verification requires at least two capture collections")
    if len(transfer_rows) != expected_transfer:
        _issue(
            issues,
            "error",
            "cross_collection_row_count",
            f"expected {expected_transfer} cross-collection rows, found {len(transfer_rows)}",
        )

    null_path = root / "best_n_cross_spill_null.csv"
    membership_frequency_path = root / "best_n_best1_membership_frequency.csv"
    membership_summary_path = root / "best_n_best1_membership_summary.csv"
    null_rows = read_csv(null_path) if null_path.is_file() else []
    membership_frequency_rows = (
        read_csv(membership_frequency_path) if membership_frequency_path.is_file() else []
    )
    membership_summary_rows = (
        read_csv(membership_summary_path) if membership_summary_path.is_file() else []
    )
    expected_null_rows = cross_spill_null_rows(
        validation,
        tune_half_width,
        CROSS_SPILL_NULL_DRAWS,
        CROSS_SPILL_NULL_BLOCK_SPILLS,
    )
    try:
        expected_membership_frequency, expected_membership_summary = best1_membership_rows(
            curve,
            validation,
        )
    except ValueError as exc:
        expected_membership_frequency, expected_membership_summary = [], []
        _issue(
            issues,
            "error",
            "best1_membership_source_identity",
            f"Best-1 membership cannot be recomputed from malformed source rows: {exc}",
        )
    _check_derived_control(
        null_rows,
        expected_null_rows,
        CROSS_SPILL_NULL_FIELDS,
        "cross_spill_null",
        issues,
    )
    _check_derived_control(
        membership_frequency_rows,
        expected_membership_frequency,
        BEST1_MEMBERSHIP_FREQUENCY_FIELDS,
        "best1_membership_frequency",
        issues,
    )
    _check_derived_control(
        membership_summary_rows,
        expected_membership_summary,
        BEST1_MEMBERSHIP_SUMMARY_FIELDS,
        "best1_membership_summary",
        issues,
    )
    expected_null_keys = {
        (plane, subset_size)
        for plane in ("H", "V")
        for subset_size in range(1, expected_max_n + 1)
    }
    null_keys = {
        (str(row.get("plane", "")), int(row.get("subset_size") or 0))
        for row in null_rows
    }
    if null_keys != expected_null_keys or len(null_rows) != len(expected_null_keys):
        _issue(
            issues,
            "error",
            "cross_spill_null_coverage",
            "cross-spill null does not contain exactly one H and V row for every N",
        )
    membership_planes = [str(row.get("plane", "")) for row in membership_summary_rows]
    if sorted(membership_planes) != ["H", "V"]:
        _issue(
            issues,
            "error",
            "best1_membership_summary_coverage",
            "Best-1 membership summary must contain exactly one H and V row",
        )

    recommendations: dict[str, dict[str, object]] = {}
    for plane in ("H", "V"):
        chosen, reason = recommended_n(summary, plane, tune_half_width)
        if chosen is None:
            recommendations[plane] = {"recommended_n": "", "status": reason}
            _issue(
                issues,
                "warning",
                f"no_{plane.lower()}_recommendation",
                f"{plane} has no automatic Best-N recommendation: {reason}",
            )
            continue
        selected_n = int(chosen["subset_size"])
        recommendations[plane] = {"recommended_n": selected_n, "status": reason}
        larger = [row for row in summary if row.get("plane") == plane and int(row.get("subset_size") or 0) > selected_n]
        if len(larger) < 3:
            _issue(
                issues,
                "error",
                f"{plane.lower()}_recommendation_boundary",
                f"{plane} recommendation Best-{selected_n} has fewer than three evaluated larger N values",
            )

    error_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "error")
    warning_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "warning")
    control_paths = {
        "best_n_cross_spill_null.csv": null_path,
        "best_n_best1_membership_frequency.csv": membership_frequency_path,
        "best_n_best1_membership_summary.csv": membership_summary_path,
    }
    control_output_sha256 = {
        filename: file_sha256(path)
        for filename, path in control_paths.items()
        if path.is_file()
    }
    membership_by_plane = {
        str(row.get("plane", "")): {
            "plane_spill_count": int(row.get("plane_spill_count") or 0),
            "available_source_count": int(row.get("available_source_count") or 0),
            "winning_source_count": int(row.get("winning_source_count") or 0),
            "all_sources_win": str(row.get("all_sources_win", "")).lower() == "true",
            "maximum_winner_count": int(row.get("maximum_winner_count") or 0),
            "maximum_winner_fraction": _finite(row.get("maximum_winner_fraction")),
            "maximum_source_keys": _parts(row.get("maximum_source_keys")),
        }
        for row in membership_summary_rows
    }
    report: dict[str, object] = {
        "schema": "tbt-monitor.best-n-verification/v2",
        "status": "pass" if error_count == 0 else "fail",
        "root": str(root),
        "expected_max_n": expected_max_n,
        "expected_folds": expected_folds,
        "curve_cache_key_count": len(curve_keys),
        "curve_row_count": len(curve),
        "validation_cache_key_count": len(validation_keys),
        "validation_row_count": len(validation),
        "collections": collections,
        "run_contract_analysis": contract.get("analysis", ""),
        "recommendations": recommendations,
        "control_output_sha256": control_output_sha256,
        "cross_spill_null": {
            "row_count": len(null_rows),
            "permutation_draws": CROSS_SPILL_NULL_DRAWS,
            "block_spills": CROSS_SPILL_NULL_BLOCK_SPILLS,
            "tune_half_width": tune_half_width,
            "permutation_mode": "seeded_block_derangement_shared_across_folds",
            "seed_namespace": "best-n-cross-spill-null",
            "status_counts": dict(Counter(str(row.get("status", "")) for row in null_rows)),
        },
        "best1_membership": {
            "frequency_row_count": len(membership_frequency_rows),
            "summary_row_count": len(membership_summary_rows),
            "by_plane": membership_by_plane,
        },
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
    if write_outputs:
        atomic_write_text(root / "best_n_verification.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
        atomic_write_text(root / "best_n_verification.md", verification_markdown(report))
    return report
