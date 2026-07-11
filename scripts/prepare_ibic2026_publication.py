#!/usr/bin/env python3
"""Bind verifier-clean analysis roots to the IBIC 2026 poster and paper sources."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from bpm_mining.best_n import recommended_n
from bpm_mining.ridge_verification import png_dimensions


MANIFEST_FIELDS = (
    "role",
    "source_path",
    "source_sha256",
    "output_path",
    "output_sha256",
)

PAYLOAD_AUDIT_EXPECTED = {
    "analysis_turns": 50_000,
    "plateau_turns": 128,
    "manifest_count": 2_200,
    "stream_rows": 263_999,
    "paired_stream_rows": 23_999,
    "incomplete_manifests": 1,
    "flagged_rows": 0,
    "position_plateau_rows": 0,
    "paired_plateau_rows": 0,
    "raw_device_fallback_pair_rows": 0,
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"required CSV is missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"required JSON is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_report(path: Path) -> dict[str, object]:
    report = read_json(path)
    status = str(report.get("status", "")).lower()
    if status not in {"ok", "pass"}:
        raise ValueError(f"verification report is not accepted: {path}: status={status!r}")
    for field in ("error_count", "fail_count"):
        if field in report and int(report[field] or 0) != 0:
            raise ValueError(f"verification report contains failures: {path}: {field}={report[field]}")
    return report


def require_payload_audit(path: Path) -> dict[str, object]:
    report = require_report(path)
    if report.get("schema") != "tbt-monitor.delivery-ring-payload-audit/v1":
        raise ValueError(f"unsupported Delivery Ring payload-audit schema: {path}")
    mismatches = {
        field: (int(report.get(field) or 0), expected)
        for field, expected in PAYLOAD_AUDIT_EXPECTED.items()
        if int(report.get(field) or 0) != expected
    }
    if mismatches:
        raise ValueError(f"Delivery Ring payload audit does not match the publication corpus: {mismatches}")
    topology = report.get("topology")
    if not isinstance(topology, Mapping) or len(topology) != 3:
        raise ValueError("Delivery Ring payload audit must cover all three publication collections")
    for collection, raw in topology.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"Delivery Ring payload topology is invalid: {collection}")
        expected = {
            "unique_position_streams": 120,
            "unique_h_streams": 60,
            "unique_v_streams": 60,
            "unique_digitizers": 30,
        }
        mismatches = {
            field: (int(raw.get(field) or 0), value)
            for field, value in expected.items()
            if int(raw.get(field) or 0) != value
        }
        if mismatches or raw.get("bad_digitizers"):
            raise ValueError(f"Delivery Ring payload topology mismatch for {collection}: {mismatches}")
    if len(str(report.get("manifest_inventory_sha256") or "")) != 64:
        raise ValueError("Delivery Ring payload audit is missing its manifest-inventory hash")
    return report


def best_n_design_summary(report: Mapping[str, object]) -> dict[str, int]:
    fields = {
        "curve_spill_plane_count": int(report.get("curve_cache_key_count") or 0),
        "validation_spill_plane_count": int(report.get("validation_cache_key_count") or 0),
        "digitizer_fold_count": int(report.get("expected_folds") or 0),
        "maximum_n": int(report.get("expected_max_n") or 0),
        "curve_evaluation_row_count": int(report.get("curve_row_count") or 0),
        "validation_evaluation_row_count": int(report.get("validation_row_count") or 0),
    }
    expected = {
        "curve_spill_plane_count": 4_000,
        "validation_spill_plane_count": 1_000,
        "digitizer_fold_count": 5,
        "maximum_n": 40,
        "curve_evaluation_row_count": 160_000,
        "validation_evaluation_row_count": 200_000,
    }
    mismatches = {
        field: (fields[field], value)
        for field, value in expected.items()
        if fields[field] != value
    }
    if mismatches:
        raise ValueError(f"Best-N verification report does not match the definitive study design: {mismatches}")
    return fields


def finite(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def fmt(value: object, digits: int = 3) -> str:
    number = finite(value)
    if not math.isfinite(number):
        return "NA"
    if number == 0:
        return "0"
    if abs(number) < 0.001:
        return f"{number:.2e}"
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def pct(value: object, digits: int = 1) -> str:
    number = finite(value)
    return "NA" if not math.isfinite(number) else f"{100.0 * number:.{digits}f}%"


def selected_best_n_rows(
    summary_rows: Sequence[dict[str, str]],
    tune_half_width: float,
) -> tuple[dict[str, int], dict[str, dict[str, str]], dict[str, str]]:
    sizes: dict[str, int] = {}
    rows: dict[str, dict[str, str]] = {}
    rationales: dict[str, str] = {}
    for plane in ("H", "V"):
        selected, rationale = recommended_n(summary_rows, plane, tune_half_width)
        if selected is None:
            raise ValueError(f"Best-N has no verifier-eligible {plane} recommendation: {rationale}")
        size = int(selected["subset_size"])
        sizes[plane] = size
        rows[plane] = {str(key): str(value) for key, value in selected.items()}
        rationales[plane] = rationale
    return sizes, rows, rationales


def keyed_rows(
    rows: Sequence[dict[str, str]],
    key_fields: Sequence[str],
    label: str,
) -> dict[tuple[str, ...], dict[str, str]]:
    output: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in key_fields)
        if key in output:
            raise ValueError(f"duplicate {label} key: {key}")
        output[key] = row
    return output


def sensitivity_summary(root: Path, tune_half_width: float) -> dict[str, object]:
    manifest = read_csv(root / "sensitivity_run_manifest.csv")
    identities = {
        (row.get("beam_width", ""), row.get("fit_windows", ""), row.get("fold_seed", ""))
        for row in manifest
    }
    if len(manifest) != 7 or len(identities) != 7 or any(row.get("status") != "verified" for row in manifest):
        raise ValueError("Best-N sensitivity matrix must contain seven unique verified runs")
    recommendations: dict[str, list[int]] = {"H": [], "V": []}
    unavailable: dict[str, int] = {"H": 0, "V": 0}
    for row in manifest:
        run_root = Path(row["output"])
        if not run_root.is_dir():
            run_root = root / "runs" / row["run"]
        summary = read_csv(run_root / "best_n_summary.csv")
        contract = read_json(run_root / "run_contract.json")
        run_tolerance = float(contract.get("tune_half_width") or tune_half_width)
        for plane in ("H", "V"):
            selected, _reason = recommended_n(summary, plane, run_tolerance)
            if selected is None:
                unavailable[plane] += 1
            else:
                recommendations[plane].append(int(selected["subset_size"]))
    if any(unavailable.values()):
        detail = ", ".join(
            f"{plane}={unavailable[plane]}/{len(manifest)}"
            for plane in ("H", "V")
            if unavailable[plane]
        )
        raise ValueError(
            "Best-N sensitivity matrix contains unresolved recommendations: " + detail
        )
    return {
        "run_count": len(manifest),
        "recommendations": recommendations,
        "unavailable": unavailable,
    }


def copy_png(
    role: str,
    source: Path,
    destination: Path,
    publication_root: Path,
    manifest: list[dict[str, str]],
) -> None:
    dimensions = png_dimensions(source)
    if dimensions is None or dimensions[0] < 500 or dimensions[1] < 300:
        raise ValueError(f"publication source PNG is missing, invalid, or undersized: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    manifest.append(
        {
            "role": role,
            "source_path": str(source.resolve()),
            "source_sha256": sha256(source),
            "output_path": destination.relative_to(publication_root).as_posix(),
            "output_sha256": sha256(destination),
        }
    )


def interval(row: Mapping[str, object], value: str, low: str, high: str, digits: int = 3) -> str:
    return f"{fmt(row.get(value), digits)} [{fmt(row.get(low), digits)}, {fmt(row.get(high), digits)}]"


def render_results_table(
    best_n_rows: Mapping[str, Mapping[str, object]],
    ridge_rows: Mapping[str, Mapping[str, object]],
    adaptive_rows: Mapping[str, Mapping[str, object]],
    selected_sizes: Mapping[str, int],
) -> str:
    lines = [
        r"\begin{table*}[!htb]",
        r"  \centering",
        r"  \caption{Leakage-controlled Best-$N$ intervals use collection-preserving spill blocks. Ridge intervals use overlapping-turn blocks on exact-paired picks and describe concentration, not absolute tune accuracy or measured physical noise.}",
        r"  \label{tab:results}",
        r"  \small",
        r"  \begin{tabular}{@{}lccccc@{}}",
        r"    \toprule",
        r"    Plane & Best-$N$ & Blind agreement & Blind $|\Delta q|$ & $\Delta$IQR vs B1 & $\Delta$IQR vs legacy \\",
        r"    \midrule",
    ]
    for plane in ("H", "V"):
        best = best_n_rows[plane]
        ridge = ridge_rows[plane]
        adaptive = adaptive_rows[plane]
        agreement = interval(
            best,
            "blind_q_agreement_rate",
            "blind_q_agreement_ci_low",
            "blind_q_agreement_ci_high",
        )
        q_delta = interval(
            best,
            "median_blind_selected_heldout_abs_q_delta",
            "blind_selected_heldout_abs_q_delta_ci_low",
            "blind_selected_heldout_abs_q_delta_ci_high",
            4,
        )
        iqr_delta = interval(
            ridge,
            "median_iqr_delta_ensemble_minus_legacy",
            "median_iqr_delta_ci_low",
            "median_iqr_delta_ci_high",
            4,
        )
        adaptive_iqr_delta = interval(
            adaptive,
            "median_iqr_delta_ensemble_minus_baseline",
            "median_iqr_delta_ci_low",
            "median_iqr_delta_ci_high",
            4,
        )
        lines.append(
            f"    {plane} & {selected_sizes[plane]} & {agreement} & {q_delta} & {adaptive_iqr_delta} & {iqr_delta} \\\\"
        )
    lines.extend(
        [
            r"    \bottomrule",
            r"  \end{tabular}",
            r"\end{table*}",
            "",
        ]
    )
    return "\n".join(lines)


def publication_numeric_summary(
    primary_rows: Sequence[dict[str, str]],
    paired_rows: Sequence[dict[str, str]],
    intensity_effects: Sequence[dict[str, str]],
    best_n_design: Mapping[str, int],
) -> dict[str, object]:
    output: dict[str, object] = {}
    for plane in ("H", "V"):
        for size in (1, 3, 5):
            values = [
                finite(row.get("subset_score"))
                for row in primary_rows
                if row.get("plane") == plane and int(row.get("subset_size") or 0) == size
            ]
            values = [value for value in values if math.isfinite(value)]
            if not values:
                raise ValueError(f"primary score rows are missing for {plane} Best-{size}")
            output[f"primary_{plane.lower()}_best{size}_median_score"] = median(values)
    paired = keyed_rows(paired_rows, ("plane", "comparison"), "primary paired result")
    for plane in ("H", "V"):
        for left, right in ((1, 3), (3, 5)):
            key = (plane, f"best{left} vs best{right}")
            row = paired.get(key)
            value = finite(row.get("median_paired_difference")) if row else math.nan
            if not math.isfinite(value):
                raise ValueError(f"primary paired result is missing for {plane} Best-{left} vs Best-{right}")
            output[f"primary_{plane.lower()}_best{left}_to_best{right}_gain"] = value
    output["intensity_effect_count"] = len(intensity_effects)
    output["intensity_fdr_significant_count"] = sum(
        row.get("statistical_benefit_pass", "").lower() == "true"
        and finite(row.get("fdr_q_value")) <= 0.05
        for row in intensity_effects
    )
    output["intensity_practical_count"] = sum(
        row.get("practical_effect_pass", "").lower() == "true" for row in intensity_effects
    )
    output["intensity_retained_count"] = sum(
        row.get("retain_method_for_tune_analysis", "").lower() == "true"
        for row in intensity_effects
    )
    output.update(best_n_design)
    return output


def render_results_macros(values: Mapping[str, object]) -> str:
    commands = (
        ("PrimaryHBestOneScore", "primary_h_best1_median_score", 3),
        ("PrimaryHBestThreeScore", "primary_h_best3_median_score", 3),
        ("PrimaryHBestFiveScore", "primary_h_best5_median_score", 3),
        ("PrimaryVBestOneScore", "primary_v_best1_median_score", 3),
        ("PrimaryVBestThreeScore", "primary_v_best3_median_score", 3),
        ("PrimaryVBestFiveScore", "primary_v_best5_median_score", 3),
        ("PrimaryHOneToThreeGain", "primary_h_best1_to_best3_gain", 4),
        ("PrimaryHThreeToFiveGain", "primary_h_best3_to_best5_gain", 4),
        ("PrimaryVOneToThreeGain", "primary_v_best1_to_best3_gain", 4),
        ("PrimaryVThreeToFiveGain", "primary_v_best3_to_best5_gain", 4),
        ("IntensityEffectCount", "intensity_effect_count", 0),
        ("IntensityFdrCount", "intensity_fdr_significant_count", 0),
        ("IntensityPracticalCount", "intensity_practical_count", 0),
        ("IntensityRetainedCount", "intensity_retained_count", 0),
        ("BestNCurveSpillPlaneCount", "curve_spill_plane_count", 0),
        ("BestNValidationSpillPlaneCount", "validation_spill_plane_count", 0),
        ("BestNDigitizerFoldCount", "digitizer_fold_count", 0),
    )
    lines = ["% Generated by scripts/prepare_ibic2026_publication.py; do not edit."]
    for command, key, digits in commands:
        if key not in values:
            raise ValueError(f"publication numerical summary is missing {key}")
        if digits == 0:
            rendered = str(int(values[key]))
        else:
            rendered = f"{float(values[key]):.{digits}f}"
        lines.append(f"\\newcommand{{\\{command}}}{{{rendered}}}")
    return "\n".join(lines) + "\n"


def publication_content(
    selected_sizes: Mapping[str, int],
    best_n_rows: Mapping[str, Mapping[str, object]],
    ridge_rows: Mapping[str, Mapping[str, object]],
    adaptive_rows: Mapping[str, Mapping[str, object]],
    loss_row: Mapping[str, object],
    best_n_design: Mapping[str, int],
    intensity_effect_count: int,
    retained_intensity_effects: int,
) -> dict[str, object]:
    h_best = best_n_rows["H"]
    v_best = best_n_rows["V"]
    h_ridge = ridge_rows["H"]
    v_ridge = ridge_rows["V"]
    h_adaptive = adaptive_rows["H"]
    v_adaptive = adaptive_rows["V"]
    adaptive_status = {}
    for plane, row in (("H", h_adaptive), ("V", v_adaptive)):
        low = finite(row.get("median_iqr_delta_ci_low"))
        high = finite(row.get("median_iqr_delta_ci_high"))
        if math.isfinite(high) and high < 0:
            adaptive_status[plane] = "narrower than corrected Best-1"
        elif math.isfinite(low) and low > 0:
            adaptive_status[plane] = "broader than corrected Best-1"
        else:
            adaptive_status[plane] = "not resolved from corrected Best-1"
    loss_turn = loss_row.get("first_sustained_half_peak_loss_turn", "")
    change_turn = loss_row.get("most_likely_change_turn", "")
    loss_parts = [f"Best-{selected_sizes['H']} H concentration is tracked over 50,000 turns."]
    if loss_turn:
        loss_parts.append(f"Sustained half-peak loss begins near turn {loss_turn}.")
    if change_turn:
        loss_parts.append(f"The multimetric change candidate is turn {change_turn}.")
    if not loss_turn and not change_turn:
        loss_parts.append("No stable loss turn passes the data-derived diagnostic rule.")
    loss_parts.append("No extraction timestamp or cause is inferred.")
    return {
        "title": "Turn-by-turn tune analysis using adaptive BPM ensembles in the Fermilab Mu2e Delivery Ring",
        "author": "Derek Steinkamp | Fermi National Accelerator Laboratory",
        "methodHeading": "ADAPTIVE, LEAKAGE-CONTROLLED BPM ENSEMBLES",
        "methodBody": (
            "Synchronized 2,000-spill snapshots provide 60 H and 60 V channels.\n"
            "Members are selected from eight fit windows; overlapping windows are purged.\n"
            f"Later validation uses {best_n_design['validation_spill_plane_count']:,} stratified spill-plane "
            f"cases across {best_n_design['digitizer_fold_count']} held-out-digitizer folds."
        ),
        "bestNHCaption": (
            f"H Best-{selected_sizes['H']}: blind full-band selected/held-out agreement "
            f"{pct(h_best.get('blind_q_agreement_rate'))} "
            f"[{pct(h_best.get('blind_q_agreement_ci_low'))}, {pct(h_best.get('blind_q_agreement_ci_high'))}]; "
            f"median |Delta q| {fmt(h_best.get('median_blind_selected_heldout_abs_q_delta'), 4)}."
        ),
        "bestNVCaption": (
            f"V Best-{selected_sizes['V']}: blind full-band selected/held-out agreement "
            f"{pct(v_best.get('blind_q_agreement_rate'))} "
            f"[{pct(v_best.get('blind_q_agreement_ci_low'))}, {pct(v_best.get('blind_q_agreement_ci_high'))}]; "
            f"median |Delta q| {fmt(v_best.get('median_blind_selected_heldout_abs_q_delta'), 4)}."
        ),
        "ridgeCaption": (
            f"Exact-paired 50,000-turn ridge-pick density: H Best-{selected_sizes['H']} and "
            f"V Best-{selected_sizes['V']} versus the audited legacy normalized-single selector. "
            f"Shared-ridge mass gains are H {fmt(h_ridge.get('median_shared_ridge_mass_gain'))} and "
            f"V {fmt(v_ridge.get('median_shared_ridge_mass_gain'))}; color is pick probability, not power."
        ),
        "conclusionHeading": "RESULT AND LIMIT",
        "conclusionBody": (
            "Plane-specific adaptive ensembles recover the most reproducible later-window BPM tune candidates.\n"
            f"Full-buffer ensemble-size contrast: H {adaptive_status['H']}; V {adaptive_status['V']}.\n"
            "This is BPM-only internal consistency, not an absolute tune calibration."
        ),
        "ridgeContrastCaption": (
            f"Selected H Best-{selected_sizes['H']} and V Best-{selected_sizes['V']} P10-P90 width "
            "minus corrected adaptive Best-1 on exact paired spill/windows. Negative is narrower; "
            "zero is no ensemble-size difference."
        ),
        "quantitativeBody": (
            f"2,000 spills; {best_n_design['curve_spill_plane_count']:,} H/V curve cases; "
            f"{best_n_design['validation_spill_plane_count']:,} stratified validation cases; "
            f"{best_n_design['digitizer_fold_count']} held-out-digitizer folds. "
            f"H Best-{selected_sizes['H']} blind agreement {pct(h_best.get('blind_q_agreement_rate'))}; "
            f"V Best-{selected_sizes['V']} {pct(v_best.get('blind_q_agreement_rate'))}. "
            f"Median IQR change vs corrected Best-1: H {fmt(h_adaptive.get('median_iqr_delta_ensemble_minus_baseline'), 4)}, "
            f"V {fmt(v_adaptive.get('median_iqr_delta_ensemble_minus_baseline'), 4)}. "
            f"Intensity weighting retained {retained_intensity_effects}/{intensity_effect_count} tested effects."
        ),
        "hLossCaption": " ".join(loss_parts),
        "assets": {
            "bestNH": "assets/best_n_validation_h.png",
            "bestNV": "assets/best_n_validation_v.png",
            "ridgeHV": "assets/ridge_density_comparison.png",
            "ridgeContrast": "assets/ridge_width_contrast_hv.png",
            "hLoss": "assets/horizontal_loss_diagnostic.png",
        },
    }


def write_manifest(path: Path, rows: Sequence[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def prepare_publication(
    primary_root: Path,
    followup_root: Path,
    best_n_root: Path,
    ridge_root: Path,
    intensity_root: Path,
    payload_audit_root: Path,
    publication_root: Path,
) -> dict[str, object]:
    best_n_block20 = best_n_root / "merged_block20"
    intensity_block20 = intensity_root / "merged_block20"
    verification_paths = [
        primary_root / "logs" / "best_bpm_verification.json",
        followup_root / "logs" / "best_bpm_followup_verification.json",
        *(best_n_root / f"merged_block{block}" / "best_n_verification.json" for block in (10, 20, 40)),
        ridge_root / "ridge_density_verification.json",
        intensity_block20 / "intensity_verification.json",
        payload_audit_root / "delivery_ring_payload_audit.json",
    ]
    accepted_reports = {path: require_report(path) for path in verification_paths[:-1]}
    payload_audit = require_payload_audit(verification_paths[-1])
    best_n_design = best_n_design_summary(
        accepted_reports[best_n_block20 / "best_n_verification.json"]
    )

    best_n_contract = read_json(best_n_block20 / "run_contract.json")
    tune_half_width = float(best_n_contract.get("tune_half_width") or 0.0025)
    best_n_summary = read_csv(best_n_block20 / "best_n_summary.csv")
    selected_sizes, best_n_rows, rationales = selected_best_n_rows(best_n_summary, tune_half_width)

    transfers = read_csv(best_n_block20 / "best_n_cross_collection_transfer.csv")
    if len(transfers) != 4 or any(row.get("status") != "OK" for row in transfers):
        raise ValueError("Best-N cross-collection transfer must contain four OK rows")
    sensitivity = sensitivity_summary(best_n_root / "sensitivity", tune_half_width)

    block_recommendations = read_csv(
        best_n_root / "block_sensitivity" / "best_n_bootstrap_block_spills_recommendations.csv"
    )
    if any(row.get("status") != "OK" for row in block_recommendations):
        raise ValueError("Best-N block-length sensitivity contains an unresolved recommendation")

    ridge_contract = read_json(ridge_root / "run_contract.json")
    raw_contract_sizes = ridge_contract.get("selected_plane_sizes", {})
    if not isinstance(raw_contract_sizes, Mapping):
        raise ValueError("ridge selected_plane_sizes must be an H/V object")
    contract_sizes = {
        plane: int(value)
        for plane, value in raw_contract_sizes.items()
    }
    if contract_sizes != selected_sizes:
        raise ValueError(
            f"ridge plane-selected sizes do not match Best-N recommendations: {contract_sizes} != {selected_sizes}"
        )
    ridge_metrics = keyed_rows(
        read_csv(ridge_root / "ridge_density_legacy_comparison_metrics.csv"),
        ("plane", "subset_size"),
        "ridge metric",
    )
    selected_ridge_rows = {
        plane: ridge_metrics[(plane, str(selected_sizes[plane]))] for plane in ("H", "V")
    }
    adaptive_metrics = keyed_rows(
        read_csv(ridge_root / "ridge_density_adaptive_pair_comparison_metrics.csv"),
        ("plane", "baseline_subset_size", "ensemble_subset_size"),
        "adaptive ridge-pair metric",
    )
    selected_adaptive_rows = {
        plane: adaptive_metrics[(plane, "1", str(selected_sizes[plane]))]
        for plane in ("H", "V")
    }
    loss_rows = keyed_rows(
        read_csv(ridge_root / "ridge_density_loss_candidates.csv"),
        ("plane", "subset_size"),
        "ridge loss",
    )
    h_loss = loss_rows[("H", str(selected_sizes["H"]))]

    intensity_effects = read_csv(intensity_block20 / "intensity_method_effects.csv")
    numeric_summary = publication_numeric_summary(
        read_csv(primary_root / "evolution" / "subset_size_comparison.csv"),
        read_csv(primary_root / "statistics" / "paired_method_tests.csv"),
        intensity_effects,
        best_n_design,
    )
    retained_effects = [
        row for row in intensity_effects if row.get("retain_method_for_tune_analysis", "").lower() == "true"
    ]
    block_intensity = read_csv(intensity_root / "block_sensitivity" / "intensity_block_sensitivity.csv")
    if retained_effects or any(int(row.get("retained_effects") or 0) != 0 for row in block_intensity):
        raise ValueError("intensity weighting was retained; publication copy must be reconsidered")

    poster_root = publication_root / "poster"
    paper_root = publication_root / "paper"
    poster_assets = poster_root / "assets"
    paper_figures = paper_root / "figures"
    manifest: list[dict[str, str]] = []

    sources = {
        "best_n_h": best_n_block20 / "best_n_validation_h.png",
        "best_n_v": best_n_block20 / "best_n_validation_v.png",
        "ridge_hv": ridge_root
        / f"ridge_density_legacy_single_vs_best_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png",
        "h_loss": ridge_root
        / f"ridge_concentration_selected_best{selected_sizes['H']}_h.png",
        "ridge_width_hv": ridge_root
        / f"ridge_p10_p90_delta_vs_turn_best1_to_selected_h{selected_sizes['H']}_v{selected_sizes['V']}_hv.png",
        "ridge_width_hv_poster": ridge_root
        / f"ridge_p10_p90_delta_vs_turn_best1_to_selected_h{selected_sizes['H']}_v{selected_sizes['V']}_hv_poster.png",
    }
    poster_destinations = {
        "best_n_h": poster_assets / "best_n_validation_h.png",
        "best_n_v": poster_assets / "best_n_validation_v.png",
        "ridge_hv": poster_assets / "ridge_density_comparison.png",
        "h_loss": poster_assets / "horizontal_loss_diagnostic.png",
        "ridge_width_hv_poster": poster_assets / "ridge_width_contrast_hv.png",
    }
    paper_destinations = {
        "best_n_h": paper_figures / "best_n_validation_h.png",
        "best_n_v": paper_figures / "best_n_validation_v.png",
        "ridge_hv": paper_figures / "ridge_density_comparison.png",
        "h_loss": paper_figures / "horizontal_loss_diagnostic.png",
        "ridge_width_hv": paper_figures / "ridge_width_contrast_hv.png",
    }
    for role, destination in poster_destinations.items():
        copy_png(f"poster:{role}", sources[role], destination, publication_root, manifest)
    for role, destination in paper_destinations.items():
        copy_png(f"paper:{role}", sources[role], destination, publication_root, manifest)

    content = publication_content(
        selected_sizes,
        best_n_rows,
        selected_ridge_rows,
        selected_adaptive_rows,
        h_loss,
        best_n_design,
        len(intensity_effects),
        len(retained_effects),
    )
    poster_root.mkdir(parents=True, exist_ok=True)
    content_path = poster_root / "content.json"
    content_path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    results_table_path = paper_root / "results_table.tex"
    results_table_path.write_text(
        render_results_table(
            best_n_rows,
            selected_ridge_rows,
            selected_adaptive_rows,
            selected_sizes,
        ),
        encoding="utf-8",
    )
    results_macros_path = paper_root / "results_macros.tex"
    results_macros_path.write_text(render_results_macros(numeric_summary), encoding="utf-8")

    payload = {
        "schema": "tbt-monitor.ibic2026-results/v1",
        "selected_sizes": selected_sizes,
        "best_n_rows": best_n_rows,
        "best_n_rationales": rationales,
        "cross_collection_transfer": transfers,
        "sensitivity": sensitivity,
        "block_recommendations": block_recommendations,
        "ridge_rows": selected_ridge_rows,
        "adaptive_ridge_rows": selected_adaptive_rows,
        "h_loss": h_loss,
        "numeric_summary": numeric_summary,
        "intensity_effect_count": len(intensity_effects),
        "retained_intensity_effects": len(retained_effects),
        "best_n_design": best_n_design,
        "payload_integrity": payload_audit,
        "verification_reports": [str(path.resolve()) for path in verification_paths],
    }
    payload_path = publication_root / "results_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for role, source in (
        *((f"verification:{path.stem}", path) for path in verification_paths),
        ("poster:content", content_path),
        ("paper:results_table", results_table_path),
        ("paper:results_macros", results_macros_path),
        ("publication:results_payload", payload_path),
    ):
        manifest.append(
            {
                "role": role,
                "source_path": str(source.resolve()),
                "source_sha256": sha256(source),
                "output_path": source.relative_to(publication_root).as_posix()
                if publication_root in source.parents
                else "",
                "output_sha256": sha256(source) if publication_root in source.parents else "",
            }
        )
    sensitivity_text = []
    for plane in ("H", "V"):
        values = sensitivity["recommendations"][plane]
        rendered = f"{min(values)}-{max(values)}" if values else "none"
        sensitivity_text.append(
            f"- {plane} sensitivity recommendations: `{rendered}` over `{len(values)}` available runs; "
            f"unavailable: `{sensitivity['unavailable'][plane]}`"
        )
    report_lines = [
        "# IBIC 2026 Publication Preparation",
        "",
        "All copied figures and numerical text were generated only after the listed strict reports passed.",
        "",
        f"- selected H ensemble: `Best-{selected_sizes['H']}`",
        f"- selected V ensemble: `Best-{selected_sizes['V']}`",
        *sensitivity_text,
        f"- retained intensity effects: `{len(retained_effects)}/{len(intensity_effects)}`",
        f"- Best-N full curve spill-plane cases: `{best_n_design['curve_spill_plane_count']}`",
        f"- Best-N validation spill-plane cases: `{best_n_design['validation_spill_plane_count']}` across `{best_n_design['digitizer_fold_count']}` digitizer folds",
        f"- raw payload rows scanned through 50000 turns: `{payload_audit['stream_rows']}`",
        f"- raw device-coded fallback pairs: `{payload_audit['raw_device_fallback_pair_rows']}`",
        f"- H selected-minus-corrected-Best-1 median IQR: `{selected_adaptive_rows['H']['median_iqr_delta_ensemble_minus_baseline']}`",
        f"- V selected-minus-corrected-Best-1 median IQR: `{selected_adaptive_rows['V']['median_iqr_delta_ensemble_minus_baseline']}`",
        "",
        "The wide ridge figure preserves the exact-paired legacy visual reference; the width-contrast panel and clean metrics compare selected Best-N directly with corrected adaptive Best-1. Neither measures physical noise or absolute tune accuracy.",
        "",
    ]
    preparation_report = publication_root / "PREPARATION_REPORT.md"
    preparation_report.write_text("\n".join(report_lines), encoding="utf-8")
    manifest.append(
        {
            "role": "publication:preparation_report",
            "source_path": str(preparation_report.resolve()),
            "source_sha256": sha256(preparation_report),
            "output_path": preparation_report.relative_to(publication_root).as_posix(),
            "output_sha256": sha256(preparation_report),
        }
    )
    manifest.sort(key=lambda row: (row["role"], row["output_path"], row["source_path"]))
    write_manifest(publication_root / "source_manifest.csv", manifest)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-root", type=Path, required=True)
    parser.add_argument("--followup-root", type=Path, required=True)
    parser.add_argument("--best-n-root", type=Path, required=True)
    parser.add_argument("--ridge-root", type=Path, required=True)
    parser.add_argument("--intensity-root", type=Path, required=True)
    parser.add_argument("--payload-audit-root", type=Path, required=True)
    parser.add_argument(
        "--publication-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "publication" / "ibic2026",
    )
    args = parser.parse_args(argv)
    try:
        payload = prepare_publication(
            args.primary_root.resolve(),
            args.followup_root.resolve(),
            args.best_n_root.resolve(),
            args.ridge_root.resolve(),
            args.intensity_root.resolve(),
            args.payload_audit_root.resolve(),
            args.publication_root.resolve(),
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"selected_sizes": payload["selected_sizes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
