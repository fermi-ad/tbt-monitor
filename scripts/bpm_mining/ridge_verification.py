"""Strict structural and coverage verification for ridge-density galleries."""

from __future__ import annotations

import csv
import json
import math
import struct
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Mapping, Sequence

from .contracts import CONTRACT_SCHEMA_VERSION
from .io import atomic_write_text, read_csv


CORE_FILES = (
    "run_contract.json",
    "ridge_density_best_ensemble_metrics.csv",
    "ridge_density_turn_concentration.csv",
    "ridge_density_legacy_comparison_metrics.csv",
    "ridge_density_legacy_comparison_by_turn.csv",
    "ridge_density_adaptive_pair_comparison_metrics.csv",
    "ridge_density_adaptive_pair_comparison_by_turn.csv",
    "ridge_density_loss_candidates.csv",
    "ridge_density_h_plane_loss_summary.md",
    "ridge_density_warnings.csv",
    "ridge_density_best_ensemble_manifest.csv",
    "ridge_density_best_ensemble_index.md",
    "ridge_density_best_ensemble_summary.md",
)

LEGACY_NUMERIC_FIELDS = (
    "legacy_median_iqr_width",
    "ensemble_median_iqr_width",
    "median_iqr_delta_ensemble_minus_legacy",
    "median_iqr_delta_ci_low",
    "median_iqr_delta_ci_high",
    "fraction_centers_with_narrower_iqr",
    "legacy_median_peak_bin_fraction",
    "ensemble_median_peak_bin_fraction",
    "median_peak_bin_fraction_gain",
    "median_peak_bin_fraction_gain_ci_low",
    "median_peak_bin_fraction_gain_ci_high",
    "legacy_median_density_entropy",
    "ensemble_median_density_entropy",
    "median_density_entropy_delta",
    "median_density_entropy_delta_ci_low",
    "median_density_entropy_delta_ci_high",
    "median_shared_ridge_mass_gain",
    "median_shared_ridge_mass_gain_ci_low",
    "median_shared_ridge_mass_gain_ci_high",
)

LEGACY_TURN_NUMERIC_FIELDS = (
    "shared_ridge_center",
    "legacy_iqr_width",
    "ensemble_iqr_width",
    "iqr_delta_ensemble_minus_legacy",
    "legacy_p10_p90_width",
    "ensemble_p10_p90_width",
    "p10_p90_delta_ensemble_minus_legacy",
    "legacy_peak_bin_fraction",
    "ensemble_peak_bin_fraction",
    "peak_bin_fraction_gain",
    "legacy_density_entropy",
    "ensemble_density_entropy",
    "density_entropy_delta",
    "legacy_shared_ridge_mass",
    "ensemble_shared_ridge_mass",
    "shared_ridge_mass_gain",
)

ADAPTIVE_NUMERIC_FIELDS = (
    "baseline_median_iqr_width",
    "ensemble_median_iqr_width",
    "median_iqr_delta_ensemble_minus_baseline",
    "median_iqr_delta_ci_low",
    "median_iqr_delta_ci_high",
    "fraction_centers_with_narrower_ensemble_iqr",
    "baseline_median_p10_p90_width",
    "ensemble_median_p10_p90_width",
    "baseline_median_peak_bin_fraction",
    "ensemble_median_peak_bin_fraction",
    "median_peak_bin_fraction_gain",
    "median_peak_bin_fraction_gain_ci_low",
    "median_peak_bin_fraction_gain_ci_high",
    "baseline_median_density_entropy",
    "ensemble_median_density_entropy",
    "median_density_entropy_delta",
    "median_density_entropy_delta_ci_low",
    "median_density_entropy_delta_ci_high",
    "median_shared_ridge_mass_gain",
    "median_shared_ridge_mass_gain_ci_low",
    "median_shared_ridge_mass_gain_ci_high",
)

ADAPTIVE_TURN_NUMERIC_FIELDS = (
    "shared_ridge_center",
    "baseline_iqr_width",
    "ensemble_iqr_width",
    "iqr_delta_ensemble_minus_baseline",
    "baseline_p10_p90_width",
    "ensemble_p10_p90_width",
    "p10_p90_delta_ensemble_minus_baseline",
    "baseline_peak_bin_fraction",
    "ensemble_peak_bin_fraction",
    "peak_bin_fraction_gain",
    "baseline_density_entropy",
    "ensemble_density_entropy",
    "density_entropy_delta",
    "baseline_shared_ridge_mass",
    "ensemble_shared_ridge_mass",
    "shared_ridge_mass_gain",
)

CRITICAL_WARNING_PARTS = (
    "membership has",
    "selected channel payloads",
    "matched 0/",
    "matched 1/",
    "matched 2/3",
    "matched 2/5",
    "matched 3/5",
    "matched 4/5",
)


def _finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


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


def contracted_center_grid(turn_span: int, window_turns: int, stride_turns: int) -> tuple[int, ...]:
    if window_turns <= 0 or stride_turns <= 0 or turn_span < window_turns:
        return ()
    return tuple(
        start + window_turns // 2
        for start in range(0, turn_span - window_turns + 1, stride_turns)
    )


def audit_sliding_file(
    path: Path,
    subset_size: int,
    expected_center_grid: Sequence[int],
) -> dict[str, object]:
    """Audit an exact sliding table while retaining only spill identities."""
    expected_grid = tuple(int(value) for value in expected_center_grid)
    row_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()
    duplicate_groups: Counter[str] = Counter()
    bad_center_grids: Counter[str] = Counter()
    bad_center_examples: dict[str, list[str]] = defaultdict(list)
    seen_groups: set[tuple[str, str, str, str]] = set()
    cardinality_bad: Counter[str] = Counter()
    tune_bad: Counter[str] = Counter()
    tune_examples: dict[str, list[str]] = defaultdict(list)
    unknown_planes: Counter[str] = Counter()
    current_group: tuple[str, str, str, str] | None = None
    current_centers: list[int] = []

    def finish_group() -> None:
        nonlocal current_group, current_centers
        if current_group is None:
            return
        group_plane = current_group[3]
        group_counts[group_plane] += 1
        if current_group in seen_groups:
            duplicate_groups[group_plane] += 1
        else:
            seen_groups.add(current_group)
        if tuple(current_centers) != expected_grid:
            bad_center_grids[group_plane] += 1
            if len(bad_center_examples[group_plane]) < 5:
                bad_center_examples[group_plane].append(
                    f"{current_group[:3]} centers={len(current_centers)}"
                )
        current_group = None
        current_centers = []

    for row in _iter_csv(path):
        plane = row.get("plane", "")
        if plane not in {"H", "V"}:
            unknown_planes[plane] += 1
            continue
        group = (
            row.get("run_name", ""),
            row.get("target_ms", ""),
            row.get("spill_id", ""),
            plane,
        )
        if current_group is not None and group != current_group:
            finish_group()
        if current_group is None:
            current_group = group
        row_counts[plane] += 1
        current_centers.append(int(float(row.get("center_turn") or 0)))
        if int(row.get("selected_bpm_count") or 0) != subset_size:
            cardinality_bad[plane] += 1
        low, high = (0.620, 0.680) if plane == "H" else (0.690, 0.740)
        selected_tune = row.get("selected_tune", "")
        if not (low <= _finite(selected_tune) <= high):
            tune_bad[plane] += 1
            if len(tune_examples[plane]) < 5:
                tune_examples[plane].append(selected_tune)
    finish_group()
    return {
        "row_counts": row_counts,
        "group_counts": group_counts,
        "duplicate_groups": duplicate_groups,
        "bad_center_grids": bad_center_grids,
        "bad_center_examples": bad_center_examples,
        "cardinality_bad": cardinality_bad,
        "tune_bad": tune_bad,
        "tune_examples": tune_examples,
        "unknown_planes": unknown_planes,
    }


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", header[16:24])


def verification_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Ridge-Density Verification",
        "",
        f"Status: **{str(report.get('status', '')).upper()}**",
        "",
        f"- subset sizes: `{','.join(str(value) for value in report.get('subset_sizes', []))}`",
        f"- figure rows: `{report.get('figure_count', 0)}`",
        f"- warnings: `{report.get('warning_count', 0)}`",
        "",
        "## Coverage",
        "",
        "| plane | N | spills | centers | ridge points | paired legacy spills | paired points |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    coverage = report.get("coverage", [])
    if isinstance(coverage, Sequence):
        for raw in coverage:
            row = raw if isinstance(raw, Mapping) else {}
            lines.append(
                f"| {row.get('plane', '')} | {row.get('subset_size', '')} | "
                f"{row.get('spill_count', '')} | {row.get('center_count', '')} | "
                f"{row.get('ridge_points', '')} | {row.get('legacy_spill_count', '')} | "
                f"{row.get('legacy_point_count', '')} |"
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


def verify_ridge_density_outputs(
    root: Path,
    subset_sizes: Sequence[int],
    minimum_spills: int,
    expected_centers: int,
    minimum_width: int = 1000,
    minimum_height: int = 600,
    require_context_variants: bool = True,
    write_outputs: bool = True,
) -> dict[str, object]:
    sizes = sorted({int(value) for value in subset_sizes})
    issues: list[dict[str, object]] = []
    for filename in CORE_FILES:
        path = root / filename
        if not path.is_file() or path.stat().st_size == 0:
            _issue(issues, "error", "missing_file", f"required ridge output is missing or empty: {filename}")

    contract: dict[str, object] = {}
    expected_center_grid: tuple[int, ...] = ()
    expected_adaptive_spills = 0
    expected_legacy_spills: dict[str, int] = {}
    expected_legacy_points: dict[str, int] = {}
    selected_plane_sizes: dict[str, int] = {}
    contract_path = root / "run_contract.json"
    if contract_path.is_file():
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _issue(issues, "error", "invalid_run_contract", f"ridge run contract is invalid: {exc}")
    if contract:
        if int(contract.get("contract_schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
            _issue(issues, "error", "run_contract_schema", "ridge run contract schema is unsupported")
        if contract.get("analysis") != "full_buffer_ridge_density":
            _issue(issues, "error", "run_contract_analysis", "ridge run contract identifies the wrong analysis")
        if sorted(int(value) for value in contract.get("subset_sizes", [])) != sizes:
            _issue(issues, "error", "run_contract_subset_sizes", "ridge run contract subset sizes do not match verification")
        raw_selected_sizes = contract.get("selected_plane_sizes", {})
        if raw_selected_sizes:
            if not isinstance(raw_selected_sizes, Mapping) or set(raw_selected_sizes) != {"H", "V"}:
                _issue(issues, "error", "run_contract_selected_sizes", "selected ridge ensemble sizes must identify H and V")
            else:
                selected_plane_sizes = {
                    plane: int(raw_selected_sizes[plane]) for plane in ("H", "V")
                }
                if any(value not in sizes for value in selected_plane_sizes.values()):
                    _issue(issues, "error", "run_contract_selected_sizes", "selected ridge ensemble sizes are not in the requested N set")
        expected_contract_centers = 0
        expected_adaptive_spills = int(contract.get("manifest_count") or 0)
        raw_legacy_spills = contract.get("legacy_reference_spill_counts", {})
        raw_legacy_points = contract.get("legacy_reference_point_counts", {})
        if isinstance(raw_legacy_spills, Mapping):
            expected_legacy_spills = {
                plane: int(raw_legacy_spills.get(plane) or 0) for plane in ("H", "V")
            }
        if isinstance(raw_legacy_points, Mapping):
            expected_legacy_points = {
                plane: int(raw_legacy_points.get(plane) or 0) for plane in ("H", "V")
            }
        if expected_adaptive_spills <= 0 or any(expected_legacy_spills.get(plane, 0) <= 0 for plane in ("H", "V")):
            _issue(
                issues,
                "error",
                "run_contract_source_counts",
                "ridge contract is missing adaptive or legacy source coverage counts",
            )
        window_turns = int(contract.get("window_turns") or 0)
        stride_turns = int(contract.get("stride_turns") or 0)
        turn_span = int(contract.get("turn_end") or 0) - int(contract.get("turn_start") or 0)
        if window_turns > 0 and stride_turns > 0 and turn_span >= window_turns:
            expected_contract_centers = (turn_span - window_turns) // stride_turns + 1
            expected_center_grid = contracted_center_grid(turn_span, window_turns, stride_turns)
        if expected_contract_centers != expected_centers:
            _issue(issues, "error", "run_contract_centers", "ridge run contract window geometry does not match verification")
        if contract.get("planes") != ["H", "V"]:
            _issue(issues, "error", "run_contract_planes", "ridge publication contract must cover H and V")
        if contract.get("qx_band") != [0.62, 0.68] or contract.get("qy_band") != [0.69, 0.74]:
            _issue(issues, "error", "run_contract_tune_bands", "ridge run contract does not use the paired legacy tune bands")
        exact_protocol = {
            "turn_start": 0,
            "turn_end": 50000,
            "window_turns": 4096,
            "stride_turns": 256,
            "injection_window_turns": 4096,
            "bpm_normalization": "rms_per_bpm",
            "detrend": "mean_subtract",
            "dc_handling": "zero_dc_bin",
        }
        protocol_mismatches = [
            field for field, expected in exact_protocol.items() if contract.get(field) != expected
        ]
        for field, expected in (
            ("min_peak_confidence", 2.0),
            ("track_half_width", 0.005),
            ("max_tune_step_per_window", 0.005),
        ):
            if not math.isclose(_finite(contract.get(field)), expected, rel_tol=0.0, abs_tol=1e-12):
                protocol_mismatches.append(field)
        if protocol_mismatches:
            _issue(
                issues,
                "error",
                "run_contract_legacy_protocol_match",
                "ridge publication run does not match the legacy 18d321db tracking protocol: "
                + ",".join(sorted(protocol_mismatches)),
            )
        for field in (
            "best_bpm_index_sha256",
            "membership_sha256",
            "legacy_sliding_sha256",
            "manifest_inventory_sha256",
        ):
            if len(str(contract.get(field, ""))) != 64:
                _issue(issues, "error", f"run_contract_{field}", f"ridge run contract is missing a valid {field}")

    metrics = read_csv(root / "ridge_density_best_ensemble_metrics.csv") if (root / "ridge_density_best_ensemble_metrics.csv").is_file() else []
    centers = read_csv(root / "ridge_density_turn_concentration.csv") if (root / "ridge_density_turn_concentration.csv").is_file() else []
    legacy = read_csv(root / "ridge_density_legacy_comparison_metrics.csv") if (root / "ridge_density_legacy_comparison_metrics.csv").is_file() else []
    legacy_turns = read_csv(root / "ridge_density_legacy_comparison_by_turn.csv") if (root / "ridge_density_legacy_comparison_by_turn.csv").is_file() else []
    adaptive_pairs = read_csv(root / "ridge_density_adaptive_pair_comparison_metrics.csv") if (root / "ridge_density_adaptive_pair_comparison_metrics.csv").is_file() else []
    adaptive_turns = read_csv(root / "ridge_density_adaptive_pair_comparison_by_turn.csv") if (root / "ridge_density_adaptive_pair_comparison_by_turn.csv").is_file() else []
    losses = read_csv(root / "ridge_density_loss_candidates.csv") if (root / "ridge_density_loss_candidates.csv").is_file() else []
    figures = read_csv(root / "ridge_density_best_ensemble_manifest.csv") if (root / "ridge_density_best_ensemble_manifest.csv").is_file() else []
    warnings = read_csv(root / "ridge_density_warnings.csv") if (root / "ridge_density_warnings.csv").is_file() else []

    expected_keys = {(plane, size) for plane in ("H", "V") for size in sizes}
    metric_by_key = {
        (row.get("plane", ""), int(row.get("subset_size") or 0)): row
        for row in metrics
    }
    legacy_by_key = {
        (row.get("plane", ""), int(row.get("subset_size") or 0)): row
        for row in legacy
    }
    loss_keys = {(row.get("plane", ""), int(row.get("subset_size") or 0)) for row in losses}
    for name, keys, count in (
        ("metrics", set(metric_by_key), len(metrics)),
        ("legacy", set(legacy_by_key), len(legacy)),
        ("loss", loss_keys, len(losses)),
    ):
        if keys != expected_keys or count != len(expected_keys):
            _issue(issues, "error", f"{name}_coverage", f"{name} table does not contain exactly one H/V row for every requested N")

    center_groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in centers:
        center_groups[(row.get("plane", ""), int(row.get("subset_size") or 0))].append(row)
    legacy_turn_groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in legacy_turns:
        legacy_turn_groups[(row.get("plane", ""), int(row.get("subset_size") or 0))].append(row)
    if set(legacy_turn_groups) != expected_keys:
        _issue(
            issues,
            "error",
            "legacy_turn_coverage",
            "legacy turn-contrast table does not cover every requested H/V N",
        )
    adaptive_size_pairs = list(combinations(sizes, 2))
    if 1 in sizes:
        adaptive_size_pairs.insert(0, (1, 1))
    expected_adaptive_pair_keys = {
        (plane, baseline, ensemble)
        for plane in ("H", "V")
        for baseline, ensemble in adaptive_size_pairs
    }
    adaptive_pair_by_key = {
        (
            row.get("plane", ""),
            int(row.get("baseline_subset_size") or 0),
            int(row.get("ensemble_subset_size") or 0),
        ): row
        for row in adaptive_pairs
    }
    if (
        set(adaptive_pair_by_key) != expected_adaptive_pair_keys
        or len(adaptive_pairs) != len(expected_adaptive_pair_keys)
    ):
        _issue(
            issues,
            "error",
            "adaptive_pair_coverage",
            "adaptive pair metrics do not contain exactly one H/V row for every N pair plus Best-1 control",
        )
    adaptive_turn_groups: dict[
        tuple[str, int, int], list[dict[str, str]]
    ] = defaultdict(list)
    for row in adaptive_turns:
        adaptive_turn_groups[
            (
                row.get("plane", ""),
                int(row.get("baseline_subset_size") or 0),
                int(row.get("ensemble_subset_size") or 0),
            )
        ].append(row)
    if set(adaptive_turn_groups) != expected_adaptive_pair_keys:
        _issue(
            issues,
            "error",
            "adaptive_pair_turn_coverage",
            "adaptive pair turn contrasts do not cover every required H/V N pair",
        )
    for key in sorted(expected_adaptive_pair_keys):
        row = adaptive_pair_by_key.get(key, {})
        plane, baseline, ensemble = key
        if (
            int(row.get("common_spill_count") or 0) != expected_adaptive_spills
            or int(row.get("common_ridge_point_count") or 0)
            != expected_adaptive_spills * expected_centers
            or int(row.get("common_center_count") or 0) != expected_centers
            or int(row.get("subset_size") or 0) != ensemble
        ):
            _issue(
                issues,
                "error",
                "adaptive_pair_exact_coverage",
                f"{plane} Best-{baseline} versus Best-{ensemble} lacks exact adaptive pairing coverage",
            )
        missing_numeric = [
            field
            for field in ADAPTIVE_NUMERIC_FIELDS
            if not math.isfinite(_finite(row.get(field)))
        ]
        if missing_numeric:
            _issue(
                issues,
                "error",
                "adaptive_pair_metric_nonfinite",
                f"{plane} Best-{baseline} versus Best-{ensemble} has incomplete metrics",
                len(missing_numeric),
                missing_numeric,
            )
        turn_rows = adaptive_turn_groups.get(key, [])
        turn_centers = [
            int(float(turn_row.get("center_turn") or 0)) for turn_row in turn_rows
        ]
        bad_turn_rows = [
            turn_row.get("center_turn", "")
            for turn_row in turn_rows
            if int(turn_row.get("paired_ridge_count") or 0) != expected_adaptive_spills
            or int(turn_row.get("subset_size") or 0) != ensemble
            or any(
                not math.isfinite(_finite(turn_row.get(field)))
                for field in ADAPTIVE_TURN_NUMERIC_FIELDS
            )
        ]
        if (
            len(turn_rows) != expected_centers
            or len(set(turn_centers)) != expected_centers
            or tuple(sorted(turn_centers)) != expected_center_grid
            or bad_turn_rows
        ):
            _issue(
                issues,
                "error",
                "adaptive_pair_turn_metrics",
                f"{plane} Best-{baseline} versus Best-{ensemble} lacks the exact finite turn grid",
                max(1, len(bad_turn_rows)),
                bad_turn_rows,
            )
    sliding_audits: dict[int, dict[str, object]] = {}
    for size in sizes:
        path = root / f"ridge_density_best{size}_sliding_tune.csv"
        if not path.is_file() or path.stat().st_size == 0:
            _issue(issues, "error", "missing_sliding_file", f"missing or empty sliding ridge table: {path.name}")
        audit = audit_sliding_file(path, size, expected_center_grid)
        unknown_planes = audit["unknown_planes"]
        if unknown_planes:
            _issue(issues, "error", "sliding_unknown_plane", f"Best-{size} sliding table contains unknown plane labels", sum(unknown_planes.values()), list(unknown_planes))
        sliding_audits[size] = audit
    coverage: list[dict[str, object]] = []
    for plane, size in sorted(expected_keys):
        metric = metric_by_key.get((plane, size), {})
        legacy_row = legacy_by_key.get((plane, size), {})
        center_rows = center_groups.get((plane, size), [])
        center_values = [int(float(row.get("center_turn") or 0)) for row in center_rows]
        spill_count = int(metric.get("spill_count") or 0)
        ridge_points = int(metric.get("ridge_points") or 0)
        valid_centers = int(metric.get("valid_center_count") or 0)
        legacy_spills = int(legacy_row.get("common_spill_count") or 0)
        legacy_points = int(legacy_row.get("common_ridge_point_count") or 0)
        coverage.append(
            {
                "plane": plane,
                "subset_size": size,
                "spill_count": spill_count,
                "center_count": len(set(center_values)),
                "ridge_points": ridge_points,
                "legacy_spill_count": legacy_spills,
                "legacy_point_count": legacy_points,
            }
        )
        if spill_count < minimum_spills:
            _issue(issues, "error", "insufficient_spill_coverage", f"{plane} Best-{size} has {spill_count} spills; minimum is {minimum_spills}")
        if spill_count != expected_adaptive_spills:
            _issue(
                issues,
                "error",
                "adaptive_spill_coverage",
                f"{plane} Best-{size} has {spill_count} adaptive spills; contract requires {expected_adaptive_spills}",
            )
        if (
            valid_centers != expected_centers
            or len(set(center_values)) != expected_centers
            or len(center_values) != expected_centers
            or tuple(sorted(center_values)) != expected_center_grid
        ):
            _issue(issues, "error", "center_coverage", f"{plane} Best-{size} lacks the exact contracted turn-center grid")
        if ridge_points != spill_count * expected_centers:
            _issue(issues, "error", "ridge_point_coverage", f"{plane} Best-{size} does not contain one ridge point per accepted spill and turn center")
        low, high = (0.620, 0.680) if plane == "H" else (0.690, 0.740)
        bad_tunes = [row.get("median_tune", "") for row in center_rows if not (low <= _finite(row.get("median_tune")) <= high)]
        if bad_tunes:
            _issue(issues, "error", "center_tune_band", f"{plane} Best-{size} center medians leave the legacy comparison band", len(bad_tunes), bad_tunes)
        if (
            legacy_spills != expected_legacy_spills.get(plane, 0)
            or legacy_points != expected_legacy_points.get(plane, 0)
            or legacy_points != legacy_spills * expected_centers
            or int(legacy_row.get("common_center_count") or 0) != expected_centers
        ):
            _issue(issues, "error", "legacy_pair_coverage", f"{plane} Best-{size} does not meet exact legacy pairing coverage")
        missing_numeric = [field for field in LEGACY_NUMERIC_FIELDS if not math.isfinite(_finite(legacy_row.get(field)))]
        if missing_numeric:
            _issue(issues, "error", "legacy_metric_nonfinite", f"{plane} Best-{size} has incomplete paired legacy metrics", len(missing_numeric), missing_numeric)
        turn_rows = legacy_turn_groups.get((plane, size), [])
        turn_centers = [int(float(row.get("center_turn") or 0)) for row in turn_rows]
        if (
            len(turn_rows) != expected_centers
            or len(set(turn_centers)) != expected_centers
            or tuple(sorted(turn_centers)) != expected_center_grid
        ):
            _issue(
                issues,
                "error",
                "legacy_turn_center_coverage",
                f"{plane} Best-{size} lacks the exact paired turn-contrast grid",
            )
        bad_turn_rows = [
            row.get("center_turn", "")
            for row in turn_rows
            if int(row.get("paired_ridge_count") or 0) != expected_legacy_spills.get(plane, 0)
            or any(not math.isfinite(_finite(row.get(field))) for field in LEGACY_TURN_NUMERIC_FIELDS)
        ]
        if bad_turn_rows:
            _issue(
                issues,
                "error",
                "legacy_turn_metrics",
                f"{plane} Best-{size} has incomplete or nonfinite paired turn contrasts",
                len(bad_turn_rows),
                bad_turn_rows,
            )

        audit = sliding_audits[size]
        row_count = audit["row_counts"][plane]
        if row_count != ridge_points:
            _issue(issues, "error", "sliding_metric_count", f"{plane} Best-{size} sliding rows do not match the exported ridge-point count")
        group_count = audit["group_counts"][plane]
        if group_count != spill_count:
            _issue(issues, "error", "sliding_spill_count", f"{plane} Best-{size} sliding groups do not match the exported accepted-spill count")
        duplicate_group_count = audit["duplicate_groups"][plane]
        if duplicate_group_count:
            _issue(issues, "error", "duplicate_sliding_groups", f"{plane} Best-{size} sliding table repeats a spill-plane group", duplicate_group_count)
        bad_grid_count = audit["bad_center_grids"][plane]
        if bad_grid_count:
            _issue(
                issues,
                "error",
                "sliding_center_grid",
                f"{plane} Best-{size} sliding spill groups do not each contain the exact contracted center grid",
                bad_grid_count,
                audit["bad_center_examples"][plane],
            )
        bad_cardinality = audit["cardinality_bad"][plane]
        if bad_cardinality:
            _issue(issues, "error", "sliding_member_cardinality", f"{plane} Best-{size} sliding rows have incorrect selected BPM counts", bad_cardinality)
        bad_selected_tunes = audit["tune_bad"][plane]
        if bad_selected_tunes:
            _issue(issues, "error", "sliding_tune_band", f"{plane} Best-{size} selected tunes leave the configured band", bad_selected_tunes, audit["tune_examples"][plane])

    figure_names = [row.get("figure", "") for row in figures]
    duplicate_figures = len(figure_names) - len(set(figure_names))
    if duplicate_figures:
        _issue(issues, "error", "duplicate_figure_manifest", "figure manifest contains duplicate filenames", duplicate_figures)
    for row in figures:
        figure = root / row.get("figure", "")
        caption = root / row.get("caption_file", "")
        dimensions = png_dimensions(figure)
        if dimensions is None or dimensions[0] < minimum_width or dimensions[1] < minimum_height:
            _issue(issues, "error", "invalid_figure", f"figure is missing, invalid, or undersized: {figure.name}")
        if not caption.is_file() or caption.stat().st_size == 0:
            _issue(issues, "error", "missing_caption", f"caption is missing or empty: {caption.name}")
    roles = Counter(row.get("role", "") for row in figures)
    pair_count = len(sizes) * (len(sizes) - 1) // 2
    expected_role_minimums = {
        "ridge density": 2 * len(sizes),
        "metric comparison": 2,
        "turn concentration": 2,
        "density difference": 2 * pair_count,
        "paired legacy comparison": 2 * len(sizes),
        "paired legacy H/V comparison": len(sizes),
        "paired legacy density difference": 2 * len(sizes),
        "turn diagnostic: iqr_width": 2,
        "turn diagnostic: p10_p90_width": 2,
        "turn diagnostic: sample_fraction": 2,
        "turn diagnostic: density_entropy": 2,
        "turn diagnostic: median_selected_confidence": 2,
        "turn diagnostic: global_fallback_fraction": 2,
        "turn diagnostic: suspicious_step_fraction": 2,
        "paired legacy turn contrast: iqr_delta_ensemble_minus_legacy": 2,
        "paired legacy turn contrast: p10_p90_delta_ensemble_minus_legacy": 2,
        "paired legacy turn contrast: peak_bin_fraction_gain": 2,
        "paired legacy turn contrast: density_entropy_delta": 2,
        "paired legacy turn contrast: shared_ridge_mass_gain": 2,
    }
    if require_context_variants:
        expected_role_minimums["exploratory extraction-context concentration"] = 2
    if selected_plane_sizes:
        expected_role_minimums["plane-selected paired legacy H/V comparison"] = 1
        expected_role_minimums["plane-selected corrected Best-1 H/V comparison"] = 1
        expected_role_minimums[
            "plane-selected legacy/corrected Best-1/Best-N H/V comparison"
        ] = 1
        expected_role_minimums["plane-selected turn concentration"] = 2
        for metric in (
            "iqr_delta_ensemble_minus_legacy",
            "p10_p90_delta_ensemble_minus_legacy",
            "peak_bin_fraction_gain",
            "density_entropy_delta",
            "shared_ridge_mass_gain",
        ):
            expected_role_minimums[f"plane-selected paired legacy turn contrast: {metric}"] = 2
            expected_role_minimums[
                f"plane-selected H/V paired legacy turn contrast: {metric}"
            ] = 1
            expected_role_minimums[
                f"plane-selected H/V paired legacy turn contrast poster: {metric}"
            ] = 1
        for metric in (
            "iqr_delta_ensemble_minus_baseline",
            "p10_p90_delta_ensemble_minus_baseline",
            "peak_bin_fraction_gain",
            "density_entropy_delta",
            "shared_ridge_mass_gain",
        ):
            expected_role_minimums[
                f"plane-selected H/V corrected Best-1 turn contrast: {metric}"
            ] = 1
            expected_role_minimums[
                f"plane-selected H/V corrected Best-1 turn contrast poster: {metric}"
            ] = 1
    for role, minimum in expected_role_minimums.items():
        if roles[role] < minimum:
            _issue(issues, "error", "figure_role_coverage", f"figure role '{role}' has {roles[role]} rows; expected at least {minimum}")

    warning_text = [row.get("warning", "") for row in warnings if row.get("warning")]
    critical = [warning for warning in warning_text if any(part in warning for part in CRITICAL_WARNING_PARTS)]
    if critical:
        _issue(issues, "error", "critical_generation_warnings", "ridge generation reported membership or payload-resolution failures", len(critical), critical)
    noncritical = [warning for warning in warning_text if warning not in critical]
    if noncritical:
        _issue(issues, "warning", "generation_warnings", "ridge generation emitted noncritical data-quality warnings requiring review", len(noncritical), noncritical)

    error_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "error")
    warning_count = sum(int(issue["count"]) for issue in issues if issue["severity"] == "warning")
    report: dict[str, object] = {
        "status": "pass" if error_count == 0 else "fail",
        "root": str(root),
        "subset_sizes": sizes,
        "minimum_spills": minimum_spills,
        "expected_centers": expected_centers,
        "figure_count": len(figures),
        "warning_count": len(warning_text),
        "error_count": error_count,
        "finding_warning_count": warning_count,
        "coverage": coverage,
        "issues": issues,
    }
    if write_outputs:
        atomic_write_text(root / "ridge_density_verification.json", json.dumps(report, indent=2, sort_keys=True) + "\n")
        atomic_write_text(root / "ridge_density_verification.md", verification_markdown(report))
    return report
