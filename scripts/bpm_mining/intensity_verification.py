"""Strict verification for the intensity-assisted tune study and gallery."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Mapping, Sequence

from .contracts import CONTRACT_SCHEMA_VERSION
from .identity import parse_indices
from .io import atomic_write_text, read_csv
from .ridge_verification import png_dimensions


METHODS = ("unweighted", "sqrt_intensity", "linear_intensity", "intensity_gate_50pct")
WEIGHTED_METHODS = METHODS[1:]
EFFECT_METRICS = (
    "median_peak_prominence_at_train_q",
    "median_power_support_at_train_q",
    "visible_test_window_fraction",
    "median_spectral_entropy",
    "median_abs_q_delta_from_train",
)
REQUIRED_FILES = (
    "intensity_payload_integrity.csv",
    "intensity_window_metrics.csv",
    "intensity_spill_metrics.csv",
    "intensity_loss_turns.csv",
    "intensity_method_effects.csv",
    "intensity_visibility_correlations.csv",
    "intensity_visibility_correlation_summary.csv",
    "errors.csv",
    "intensity_study_summary.md",
)
WINDOW_INVARIANT_FIELDS = (
    "train_q_hat",
    "q_global",
    "q_near_train",
    "peak_prominence_global",
    "peak_prominence_at_train_q",
    "power_support_at_train_q",
    "spectral_entropy",
    "visible_at_train_q",
    "global_intensity_normalized",
    "selected_intensity_normalized",
    "effective_member_count",
)
FALLBACK_LABELS = {
    "",
    "NO_USABLE_INTENSITY_UNWEIGHTED",
    "EMPTY_FINITE_GATE_STRONGEST",
}
EFFECT_NUMERIC_FIELDS = (
    "spill_count",
    "minimum_practical_effect",
    "median_paired_delta",
    "bootstrap_ci_low",
    "bootstrap_ci_high",
    "permutation_p_value",
    "fdr_q_value",
    "median_abs_q_shift_vs_unweighted",
    "q_shift_within_tolerance_fraction",
    "bootstrap_block_spills",
)


def _finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _parts(value: object) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _same_numeric_value(left: float, right: float, tolerance: float = 1e-8) -> bool:
    if math.isfinite(left) and math.isfinite(right):
        return abs(left - right) <= tolerance
    return not math.isfinite(left) and not math.isfinite(right)


def _iter_csv(path: Path):
    if not path.is_file():
        return
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


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


def verification_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Intensity Study Verification",
        "",
        f"Status: **{str(report.get('status', '')).upper()}**",
        "",
        f"- paired payload rows: `{report.get('paired_payload_rows', 0)}`",
        f"- window rows: `{report.get('window_rows', 0)}`",
        f"- spill-method rows: `{report.get('spill_rows', 0)}`",
        f"- method-effect rows: `{report.get('effect_rows', 0)}`",
        f"- gallery figures: `{report.get('figure_rows', 0)}`",
        f"- retained effects: `{report.get('retained_effects', 0)}`",
        f"- explicitly labeled fallback windows: `{report.get('fallback_windows', 0)}`",
        "",
        "## Findings",
        "",
    ]
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


def verify_intensity_outputs(
    root: Path,
    gallery: Path,
    subset_sizes: Sequence[int],
    expected_paired_payload_rows: int,
    expected_spill_rows: int,
    expected_centers: int,
    minimum_spills_per_group: int,
    analysis_turns: int = 50000,
    tune_tolerance: float = 0.0025,
    expected_block_spills: int = 20,
    minimum_width: int = 1000,
    minimum_height: int = 600,
    write_outputs: bool = True,
) -> dict[str, object]:
    sizes = sorted({int(value) for value in subset_sizes})
    issues: list[dict[str, object]] = []
    window_turns = 0
    stride_turns = 0
    contract_path = root / "run_contract.json"
    contract: dict[str, object] = {}
    if not contract_path.is_file():
        _issue(issues, "error", "missing_run_contract", "intensity run contract is missing")
    else:
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _issue(issues, "error", "invalid_run_contract", f"intensity run contract is invalid: {exc}")
    if contract:
        if int(contract.get("contract_schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
            _issue(issues, "error", "run_contract_schema", "intensity run contract schema is unsupported")
        if contract.get("analysis") != "intensity_tune_weighting_merged":
            _issue(issues, "error", "run_contract_analysis", "intensity verifier requires a merged run contract")
        if sorted(int(value) for value in contract.get("subset_sizes", [])) != sizes:
            _issue(issues, "error", "run_contract_subset_sizes", "intensity run contract subset sizes do not match verification")
        for field, expected in (
            ("analysis_turns", analysis_turns),
            ("bootstrap_block_spills", expected_block_spills),
        ):
            if int(contract.get(field) or 0) != int(expected):
                _issue(
                    issues,
                    "error",
                    f"run_contract_{field}",
                    f"intensity run contract {field} does not match verification",
                )
        if not math.isclose(_finite(contract.get("tune_tolerance")), tune_tolerance, rel_tol=0.0, abs_tol=1e-12):
            _issue(
                issues,
                "error",
                "run_contract_tune_tolerance",
                "intensity run contract tune tolerance does not match verification",
            )
        if not math.isclose(_finite(contract.get("tune_half_width")), tune_tolerance, rel_tol=0.0, abs_tol=1e-12):
            _issue(
                issues,
                "error",
                "run_contract_tune_half_width",
                "intensity selection half-width does not match the declared tune tolerance",
            )
        window_turns = int(contract.get("window_turns") or 0)
        stride_turns = int(contract.get("stride_turns") or 0)
        contract_centers = (
            (analysis_turns - window_turns) // stride_turns + 1
            if window_turns > 0 and stride_turns > 0 and analysis_turns >= window_turns
            else 0
        )
        if contract_centers != expected_centers:
            _issue(issues, "error", "run_contract_centers", "intensity run contract window geometry does not match verification")
        if window_turns != 4096 or stride_turns != 512 or int(contract.get("fit_windows") or 0) != 8:
            _issue(
                issues,
                "error",
                "run_contract_window_protocol",
                "intensity publication contract must use 4096/512 windows and eight fit windows",
            )
        for field in ("config_sha256", "manifest_inventory_sha256"):
            if len(str(contract.get(field, ""))) != 64:
                _issue(
                    issues,
                    "error",
                    f"run_contract_{field}",
                    f"intensity run contract is missing a valid {field}",
                )
    for filename in REQUIRED_FILES:
        path = root / filename
        if not path.is_file() or path.stat().st_size == 0:
            _issue(issues, "error", "missing_file", f"required intensity output is missing or empty: {filename}")

    integrity = read_csv(root / "intensity_payload_integrity.csv") if (root / "intensity_payload_integrity.csv").is_file() else []
    window_path = root / "intensity_window_metrics.csv"
    spills = read_csv(root / "intensity_spill_metrics.csv") if (root / "intensity_spill_metrics.csv").is_file() else []
    effects = read_csv(root / "intensity_method_effects.csv") if (root / "intensity_method_effects.csv").is_file() else []
    errors = read_csv(root / "errors.csv") if (root / "errors.csv").is_file() else []

    if len(integrity) != expected_paired_payload_rows:
        _issue(issues, "error", "paired_payload_count", f"expected {expected_paired_payload_rows} paired payload rows, found {len(integrity)}")
    integrity_keys = [
        (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""), row.get("position_source_key", ""))
        for row in integrity
    ]
    if len(integrity_keys) != len(set(integrity_keys)):
        _issue(issues, "error", "duplicate_payload_pairs", "paired payload table contains duplicate exact source keys", len(integrity_keys) - len(set(integrity_keys)))
    bad_identity = [key for key, row in zip(integrity_keys, integrity) if str(row.get("stream_identity_match", "")).lower() != "true"]
    if bad_identity:
        _issue(issues, "error", "stream_identity_mismatch", "position/intensity pairs do not share exact stream identity", len(bad_identity), bad_identity)
    count_mismatches = [
        key
        for key, row in zip(integrity_keys, integrity)
        if len(
            {
                int(row.get("position_sample_count") or 0),
                int(row.get("intensity_sample_count") or 0),
                int(row.get("position_payload_sample_count") or 0),
                int(row.get("intensity_payload_sample_count") or 0),
            }
        )
        != 1
        or int(row.get("sample_count") or 0)
        != min(
            int(row.get("position_sample_count") or 0),
            int(row.get("intensity_sample_count") or 0),
            int(row.get("position_payload_sample_count") or 0),
            int(row.get("intensity_payload_sample_count") or 0),
        )
    ]
    if count_mismatches:
        _issue(
            issues,
            "error",
            "sample_count_mismatch",
            "paired position/intensity payloads do not have equal advertised and on-disk sample counts",
            len(count_mismatches),
            count_mismatches,
        )
    invalid_range = [
        key
        for key, row in zip(integrity_keys, integrity)
        if "INVALID_WITHIN_ANALYSIS_RANGE" in str(row.get("quality_flags", ""))
        or (math.isfinite(_finite(row.get("first_bad_block_turn"))) and _finite(row.get("first_bad_block_turn")) < analysis_turns)
    ]
    if invalid_range:
        _issue(issues, "error", "invalid_analysis_range", "raw intensity becomes invalid inside the declared analysis range", len(invalid_range), invalid_range)
    if errors:
        _issue(issues, "error", "analysis_errors", "intensity shards reported analysis errors", len(errors), [row.get("detail", "") for row in errors])

    if len(spills) != expected_spill_rows:
        _issue(issues, "error", "spill_row_count", f"expected {expected_spill_rows} spill-method rows, found {len(spills)}")
    spill_keys = [
        (
            row.get("collection", ""),
            row.get("spill_id", ""),
            row.get("plane", ""),
            int(row.get("subset_size") or 0),
            row.get("method", ""),
        )
        for row in spills
    ]
    if len(spill_keys) != len(set(spill_keys)):
        _issue(issues, "error", "duplicate_spill_rows", "spill-method table contains duplicate keys", len(spill_keys) - len(set(spill_keys)))
    spill_groups = Counter((key[2], key[3], key[4]) for key in spill_keys)
    expected_groups = {(plane, size, method) for plane in ("H", "V") for size in sizes for method in METHODS}
    if set(spill_groups) != expected_groups:
        _issue(issues, "error", "spill_group_coverage", "spill-method table does not cover every H/V, N, and weighting method")
    for key in sorted(expected_groups):
        if spill_groups[key] < minimum_spills_per_group:
            _issue(issues, "error", "spill_group_count", f"{key} has {spill_groups[key]} spills; minimum is {minimum_spills_per_group}")

    spill_bases_by_method: dict[str, set[tuple[str, str, str, int]]] = defaultdict(set)
    memberships_by_base: dict[tuple[str, str, str, int], set[tuple[str, str, str]]] = defaultdict(set)
    for key, row in zip(spill_keys, spills):
        base_key = key[:4]
        spill_bases_by_method[str(key[4])].add(base_key)
        memberships_by_base[base_key].add(
            (
                str(row.get("bpm_indices", "")),
                str(row.get("bpm_members", "")),
                str(row.get("bpm_source_keys", "")),
            )
        )
    unweighted_spill_bases = spill_bases_by_method.get("unweighted", set())
    mismatched_method_spills = [
        method for method in METHODS if spill_bases_by_method.get(method, set()) != unweighted_spill_bases
    ]
    if mismatched_method_spills:
        _issue(
            issues,
            "error",
            "spill_method_pairing",
            "intensity methods do not cover identical exact collection/spill/plane/N keys",
            len(mismatched_method_spills),
            mismatched_method_spills,
        )
    membership_mismatches = [key for key, signatures in memberships_by_base.items() if len(signatures) != 1]
    if membership_mismatches:
        _issue(
            issues,
            "error",
            "spill_method_membership",
            "intensity methods do not preserve the same exact selected membership",
            len(membership_mismatches),
            membership_mismatches,
        )

    membership_bad = []
    for index, row in enumerate(spills):
        size = int(row.get("subset_size") or 0)
        if (
            len(parse_indices(row.get("bpm_indices"))) != size
            or len(_parts(row.get("bpm_members"))) != size
            or len(_parts(row.get("bpm_source_keys"))) != size
        ):
            membership_bad.append(index)
    if membership_bad:
        _issue(issues, "error", "spill_membership_cardinality", "spill-method rows have incomplete exact selected membership", len(membership_bad), membership_bad)

    n1_baseline: dict[tuple[str, str, str, int], tuple[float, ...]] = {}
    for row in _iter_csv(window_path):
        if int(row.get("subset_size") or 0) == 1 and row.get("method") == "unweighted":
            key = (
                row.get("collection", ""),
                row.get("spill_id", ""),
                row.get("plane", ""),
                int(row.get("window_index") or 0),
            )
            n1_baseline[key] = tuple(_finite(row.get(field)) for field in WINDOW_INVARIANT_FIELDS)

    window_count = 0
    duplicate_window_count = 0
    previous_window_sort_key: tuple[str, ...] | None = None
    out_of_order_window_count = 0
    out_of_order_window_examples: list[tuple[str, ...]] = []
    window_group_counts: Counter[tuple[str, str, str, int, str]] = Counter()
    fallback_values: Counter[str] = Counter()
    fallback_by_spill: dict[tuple[str, str, str, int, str], list[int]] = defaultdict(lambda: [0, 0])
    unexpected_fallbacks: Counter[str] = Counter()
    unweighted_fallback_count = 0
    unweighted_fallback_examples: list[tuple[object, ...]] = []
    invariant_failure_count = 0
    invariant_failure_examples: list[str] = []
    n1_weighted_rows = 0
    window_geometry_failure_count = 0
    window_geometry_failure_examples: list[tuple[object, ...]] = []
    invalid_global_q_count = 0
    invalid_global_q_examples: list[tuple[object, ...]] = []
    for row in _iter_csv(window_path):
        window_count += 1
        key = (
            row.get("collection", ""),
            row.get("spill_id", ""),
            row.get("plane", ""),
            int(row.get("subset_size") or 0),
            row.get("method", ""),
            int(row.get("window_index") or 0),
        )
        sort_key = tuple(str(row.get(field, "")) for field in (
            "collection",
            "spill_id",
            "plane",
            "subset_size",
            "method",
            "window_index",
        ))
        if sort_key == previous_window_sort_key:
            duplicate_window_count += 1
        elif previous_window_sort_key is not None and sort_key < previous_window_sort_key:
            out_of_order_window_count += 1
            if len(out_of_order_window_examples) < 5:
                out_of_order_window_examples.append(sort_key)
        previous_window_sort_key = sort_key
        group_key = key[:-1]
        window_group_counts[group_key] += 1
        center_turn = _finite(row.get("center_turn"))
        expected_center_turn = window_turns // 2 + key[-1] * stride_turns
        if (
            key[-1] < 0
            or key[-1] >= expected_centers
            or window_turns <= 0
            or stride_turns <= 0
            or not math.isfinite(center_turn)
            or abs(center_turn - expected_center_turn) > 1e-8
        ):
            window_geometry_failure_count += 1
            if len(window_geometry_failure_examples) < 5:
                window_geometry_failure_examples.append((*key, row.get("center_turn", "")))
        if not math.isfinite(_finite(row.get("q_global"))):
            invalid_global_q_count += 1
            if len(invalid_global_q_examples) < 5:
                invalid_global_q_examples.append(key)
        fallback = row.get("weight_fallback", "")
        fallback_values[fallback] += 1
        if fallback not in FALLBACK_LABELS:
            unexpected_fallbacks[fallback] += 1
        if row.get("method") == "unweighted" and fallback:
            unweighted_fallback_count += 1
            if len(unweighted_fallback_examples) < 5:
                unweighted_fallback_examples.append(key)
        if row.get("window_role") == "test":
            fallback_by_spill[group_key][0] += int(bool(fallback))
            fallback_by_spill[group_key][1] += 1
        if int(row.get("subset_size") or 0) == 1 and row.get("method") in WEIGHTED_METHODS:
            n1_weighted_rows += 1
            base_key = (key[0], key[1], key[2], key[5])
            baseline = n1_baseline.get(base_key)
            candidate = tuple(_finite(row.get(field)) for field in WINDOW_INVARIANT_FIELDS)
            if baseline is None or any(
                not _same_numeric_value(left, right)
                for left, right in zip(baseline or (), candidate)
            ):
                invariant_failure_count += 1
                if len(invariant_failure_examples) < 5:
                    invariant_failure_examples.append(f"{base_key}:{row.get('method', '')}")

    expected_window_rows = expected_spill_rows * expected_centers
    if window_count != expected_window_rows:
        _issue(issues, "error", "window_row_count", f"expected {expected_window_rows} window rows, found {window_count}")
    if duplicate_window_count:
        _issue(issues, "error", "duplicate_window_rows", "window table contains duplicate adjacent keys", duplicate_window_count)
    if out_of_order_window_count:
        _issue(
            issues,
            "error",
            "window_sort_order",
            "window table is not in canonical key order, so streaming duplicate verification is invalid",
            out_of_order_window_count,
            out_of_order_window_examples,
        )
    expected_window_groups = set(spill_keys)
    bad_window_groups = [key for key, count in window_group_counts.items() if count != expected_centers]
    missing_window_groups = expected_window_groups - set(window_group_counts)
    unexpected_window_groups = set(window_group_counts) - expected_window_groups
    if bad_window_groups or missing_window_groups or unexpected_window_groups:
        _issue(
            issues,
            "error",
            "window_grid_coverage",
            "spill-method groups do not each contain the expected complete exact turn grid",
            len(bad_window_groups) + len(missing_window_groups) + len(unexpected_window_groups),
            [*bad_window_groups[:5], *sorted(missing_window_groups)[:5], *sorted(unexpected_window_groups)[:5]],
        )
    if window_geometry_failure_count:
        _issue(
            issues,
            "error",
            "window_exact_geometry",
            "window indices and center turns do not match the contracted 4096/512 grid",
            window_geometry_failure_count,
            window_geometry_failure_examples,
        )
    if invalid_global_q_count:
        _issue(
            issues,
            "error",
            "window_global_q",
            "global ridge picks must be finite for every exact method/window point",
            invalid_global_q_count,
            invalid_global_q_examples,
        )

    if unexpected_fallbacks:
        _issue(issues, "error", "fallback_label", "window rows contain unknown weight-fallback labels", sum(unexpected_fallbacks.values()), list(unexpected_fallbacks))
    if unweighted_fallback_count:
        _issue(issues, "error", "unweighted_fallback_label", "unweighted windows must not carry an intensity fallback label", unweighted_fallback_count, unweighted_fallback_examples)
    spill_fallback_bad = []
    for key, row in zip(spill_keys, spills):
        fallback_count, test_count = fallback_by_spill.get(key, [0, 0])
        exported = _finite(row.get("weight_fallback_window_fraction"))
        expected = fallback_count / test_count if test_count else math.nan
        if not math.isfinite(exported) or not math.isfinite(expected) or abs(exported - expected) > 1e-8:
            spill_fallback_bad.append(key)
    if spill_fallback_bad:
        _issue(issues, "error", "fallback_fraction", "spill fallback fractions do not match labeled test windows", len(spill_fallback_bad), spill_fallback_bad)

    expected_n1_baseline = sum(spill_groups[(plane, 1, "unweighted")] for plane in ("H", "V")) * expected_centers
    if len(n1_baseline) != expected_n1_baseline or n1_weighted_rows != expected_n1_baseline * len(WEIGHTED_METHODS):
        invariant_failure_count += abs(len(n1_baseline) - expected_n1_baseline)
        invariant_failure_count += abs(n1_weighted_rows - expected_n1_baseline * len(WEIGHTED_METHODS))
        invariant_failure_examples.append("incomplete Best-1 method/window coverage")
    if invariant_failure_count:
        _issue(issues, "error", "n1_weighting_invariance", "N=1 weighting methods are not exact zero-effect controls", invariant_failure_count, invariant_failure_examples)

    effect_keys = [
        (row.get("plane", ""), int(row.get("subset_size") or 0), row.get("method", ""), row.get("metric", ""))
        for row in effects
    ]
    expected_effect_keys = {
        (plane, size, method, metric)
        for plane in ("H", "V")
        for size in sizes
        for method in WEIGHTED_METHODS
        for metric in EFFECT_METRICS
    }
    if set(effect_keys) != expected_effect_keys or len(effect_keys) != len(expected_effect_keys):
        _issue(issues, "error", "effect_coverage", "effect table does not contain exactly one row for every H/V, N, method, and metric")
    effect_bad_numeric = []
    decision_bad = []
    n1_effect_bad = []
    for row in effects:
        key = (row.get("plane", ""), row.get("subset_size", ""), row.get("method", ""), row.get("metric", ""))
        if any(not math.isfinite(_finite(row.get(field))) for field in EFFECT_NUMERIC_FIELDS):
            effect_bad_numeric.append(key)
            continue
        if int(float(row.get("spill_count") or 0)) < minimum_spills_per_group:
            effect_bad_numeric.append(key)
        if int(float(row.get("bootstrap_block_spills") or 0)) != expected_block_spills:
            decision_bad.append(f"{key}:block")
        q_value = _finite(row.get("fdr_q_value"))
        stable = (
            _finite(row.get("median_abs_q_shift_vs_unweighted")) <= tune_tolerance
            and _finite(row.get("q_shift_within_tolerance_fraction")) >= 0.95
        )
        expected_retain = (
            row.get("statistical_benefit_pass") == "true"
            and row.get("practical_effect_pass") == "true"
            and q_value <= 0.05
            and stable
        )
        if (row.get("retain_method_for_tune_analysis") == "true") != expected_retain:
            decision_bad.append(f"{key}:retain")
        if int(row.get("subset_size") or 0) == 1:
            zero_fields = ("median_paired_delta", "bootstrap_ci_low", "bootstrap_ci_high", "median_abs_q_shift_vs_unweighted")
            if any(abs(_finite(row.get(field))) > 1e-10 for field in zero_fields) or row.get("retain_method_for_tune_analysis") != "false":
                n1_effect_bad.append(key)
    if effect_bad_numeric:
        _issue(issues, "error", "effect_numeric", "effect rows have incomplete inference values or spill counts", len(effect_bad_numeric), effect_bad_numeric)
    if decision_bad:
        _issue(issues, "error", "effect_decision_contract", "effect retain/reject decisions or block lengths do not match the declared gates", len(decision_bad), decision_bad)
    if n1_effect_bad:
        _issue(issues, "error", "n1_effect_contract", "N=1 effect summaries are not exact zero-effect controls", len(n1_effect_bad), n1_effect_bad)

    manifest_path = gallery / "figure_manifest.csv"
    figures = read_csv(manifest_path) if manifest_path.is_file() else []
    if not figures:
        _issue(issues, "error", "missing_gallery_manifest", "intensity gallery figure manifest is missing or empty")
    figure_paths = [Path(row.get("path", "")) for row in figures]
    if len(figure_paths) != len(set(figure_paths)):
        _issue(issues, "error", "duplicate_gallery_paths", "intensity gallery manifest contains duplicate paths", len(figure_paths) - len(set(figure_paths)))
    subtractive_caption_failures: list[str] = []
    for row, path in zip(figures, figure_paths):
        if not path.is_absolute():
            path = gallery / path
        dimensions = png_dimensions(path)
        if dimensions is None or dimensions[0] < minimum_width or dimensions[1] < minimum_height:
            _issue(issues, "error", "invalid_gallery_figure", f"gallery figure is missing, invalid, or undersized: {path}")
        if not row.get("description") or not row.get("claim_guardrail"):
            _issue(issues, "error", "gallery_caption_contract", f"gallery figure lacks a description or claim guardrail: {path.name}")
        if row.get("category") == "ridge_density_difference":
            copy = f"{row.get('description', '')} {row.get('claim_guardrail', '')}".lower().replace("-", " ")
            if (
                "probability" not in copy
                or "exact common" not in copy
                or "p99" not in copy
                or "suppresses" in copy
                or "weighted adds" in copy
            ):
                subtractive_caption_failures.append(path.name)
    if subtractive_caption_failures:
        _issue(
            issues,
            "error",
            "gallery_subtractive_semantics",
            "intensity subtraction captions must state exact-common probability redistribution and P99 display clipping without suppression language",
            len(subtractive_caption_failures),
            subtractive_caption_failures,
        )
    categories = Counter(row.get("category", "") for row in figures)
    expected_category_minimums = {
        "integrity": 1,
        "ridge_density": 2 * len(sizes) * len(METHODS),
        "ridge_density_difference": 2 * len(sizes) * len(WEIGHTED_METHODS),
        "ridge_concentration": 2 * len(sizes),
        "intensity_relationship": 2 * len(sizes) * 2,
        "method_effect": 8,
        "method_effect_practical_fraction": 8,
        "lag_correlation": 2 * len(sizes),
        "loss_turn": 2 * len(sizes) * 3,
        "representative_overlay": 1,
    }
    for category, minimum in expected_category_minimums.items():
        if categories[category] < minimum:
            _issue(issues, "error", "gallery_category_coverage", f"gallery category '{category}' has {categories[category]} rows; expected at least {minimum}")

    retained = [row for row in effects if row.get("retain_method_for_tune_analysis") == "true"]
    error_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "error")
    warning_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "warning")
    report: dict[str, object] = {
        "status": "pass" if error_count == 0 else "fail",
        "root": str(root),
        "gallery": str(gallery),
        "subset_sizes": sizes,
        "paired_payload_rows": len(integrity),
        "window_rows": window_count,
        "spill_rows": len(spills),
        "effect_rows": len(effects),
        "figure_rows": len(figures),
        "retained_effects": len(retained),
        "fallback_windows": sum(count for label, count in fallback_values.items() if label),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
    if write_outputs:
        atomic_write_text(root / "intensity_verification.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
        atomic_write_text(root / "intensity_verification.md", verification_markdown(report))
    return report
