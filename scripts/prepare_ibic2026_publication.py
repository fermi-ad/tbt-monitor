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
    return {
        "run_count": len(manifest),
        "recommendations": recommendations,
        "unavailable": unavailable,
    }


def select_poster_spill(followup_root: Path) -> tuple[dict[str, str], Path]:
    poster_root = followup_root / "artifacts" / "poster"
    rows = read_csv(poster_root / "selected_poster_artifacts.csv")
    candidates = [row for row in rows if row.get("plane") == "V"]
    if not candidates:
        raise ValueError("corrected poster shortlist contains no V example")
    candidates.sort(
        key=lambda row: (
            0 if "improvement" in row.get("category", "") else 1,
            -finite(row.get("score")),
            row.get("spill_id", ""),
        )
    )
    selected = candidates[0]
    files = [name for name in selected.get("recommended_files", "").split(";") if name]
    preferred = [name for name in files if name.endswith("_bpm_tune_deconstruction_poster.png")]
    if not preferred:
        raise ValueError("selected V poster row has no deconstruction PNG")
    return selected, poster_root / preferred[0]


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
        r"    Plane & Best-$N$ & Blind agreement & Blind $|\Delta q|$ & $\Delta$IQR & Shared mass gain \\",
        r"    \midrule",
    ]
    for plane in ("H", "V"):
        best = best_n_rows[plane]
        ridge = ridge_rows[plane]
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
        mass_gain = interval(
            ridge,
            "median_shared_ridge_mass_gain",
            "median_shared_ridge_mass_gain_ci_low",
            "median_shared_ridge_mass_gain_ci_high",
            3,
        )
        lines.append(
            f"    {plane} & {selected_sizes[plane]} & {agreement} & {q_delta} & {iqr_delta} & {mass_gain} \\\\"
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


def publication_content(
    selected_sizes: Mapping[str, int],
    best_n_rows: Mapping[str, Mapping[str, object]],
    ridge_rows: Mapping[str, Mapping[str, object]],
    loss_row: Mapping[str, object],
    selected_spill: Mapping[str, str],
    intensity_effect_count: int,
    retained_intensity_effects: int,
) -> dict[str, object]:
    h_best = best_n_rows["H"]
    v_best = best_n_rows["V"]
    h_ridge = ridge_rows["H"]
    v_ridge = ridge_rows["V"]
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
        "author": "D. Steinkamp | Fermi National Accelerator Laboratory",
        "methodHeading": "ADAPTIVE, LEAKAGE-CONTROLLED BPM ENSEMBLES",
        "methodBody": (
            "Synchronized 2,000-spill snapshots provide 60 H and 60 V channels.\n"
            "Members are selected from eight fit windows; overlapping windows are purged.\n"
            "Later spectra are checked against held-out digitizers with collection-block intervals."
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
            "The vertical plane is the clearest persistence result; horizontal tracking remains weaker and more time dependent.\n"
            "This is BPM-only internal consistency, not an absolute tune calibration."
        ),
        "selectedSpillCaption": (
            f"Corrected {selected_spill.get('plane', '')} {selected_spill.get('category', '').replace('_', ' ')} "
            f"example, {selected_spill.get('spill_id', '').replace('spill_', 'spill ')}; "
            f"adaptive-search score {fmt(selected_spill.get('score'))}."
        ),
        "quantitativeBody": (
            f"2,000 primary spills; 5 digitizer folds. "
            f"H Best-{selected_sizes['H']} blind agreement {pct(h_best.get('blind_q_agreement_rate'))}; "
            f"V Best-{selected_sizes['V']} {pct(v_best.get('blind_q_agreement_rate'))}. "
            f"Intensity weighting retained {retained_intensity_effects}/{intensity_effect_count} tested effects."
        ),
        "hLossCaption": " ".join(loss_parts),
        "assets": {
            "bestNH": "assets/best_n_validation_h.png",
            "bestNV": "assets/best_n_validation_v.png",
            "ridgeHV": "assets/ridge_density_comparison.png",
            "selectedSpill": "assets/selected_spill.png",
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
    ]
    for path in verification_paths:
        require_report(path)

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
    loss_rows = keyed_rows(
        read_csv(ridge_root / "ridge_density_loss_candidates.csv"),
        ("plane", "subset_size"),
        "ridge loss",
    )
    h_loss = loss_rows[("H", str(selected_sizes["H"]))]

    intensity_effects = read_csv(intensity_block20 / "intensity_method_effects.csv")
    retained_effects = [
        row for row in intensity_effects if row.get("retain_method_for_tune_analysis", "").lower() == "true"
    ]
    block_intensity = read_csv(intensity_root / "block_sensitivity" / "intensity_block_sensitivity.csv")
    if retained_effects or any(int(row.get("retained_effects") or 0) != 0 for row in block_intensity):
        raise ValueError("intensity weighting was retained; publication copy must be reconsidered")

    selected_spill, selected_spill_path = select_poster_spill(followup_root)
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
        "selected_spill": selected_spill_path,
    }
    poster_destinations = {
        "best_n_h": poster_assets / "best_n_validation_h.png",
        "best_n_v": poster_assets / "best_n_validation_v.png",
        "ridge_hv": poster_assets / "ridge_density_comparison.png",
        "h_loss": poster_assets / "horizontal_loss_diagnostic.png",
        "selected_spill": poster_assets / "selected_spill.png",
    }
    paper_destinations = {
        "best_n_h": paper_figures / "best_n_validation_h.png",
        "best_n_v": paper_figures / "best_n_validation_v.png",
        "ridge_hv": paper_figures / "ridge_density_comparison.png",
        "h_loss": paper_figures / "horizontal_loss_diagnostic.png",
    }
    for role, destination in poster_destinations.items():
        copy_png(f"poster:{role}", sources[role], destination, publication_root, manifest)
    for role, destination in paper_destinations.items():
        copy_png(f"paper:{role}", sources[role], destination, publication_root, manifest)

    content = publication_content(
        selected_sizes,
        best_n_rows,
        selected_ridge_rows,
        h_loss,
        selected_spill,
        len(intensity_effects),
        len(retained_effects),
    )
    poster_root.mkdir(parents=True, exist_ok=True)
    content_path = poster_root / "content.json"
    content_path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    results_table_path = paper_root / "results_table.tex"
    results_table_path.write_text(
        render_results_table(best_n_rows, selected_ridge_rows, selected_sizes),
        encoding="utf-8",
    )

    payload = {
        "schema": "tbt-monitor.ibic2026-results/v1",
        "selected_sizes": selected_sizes,
        "best_n_rows": best_n_rows,
        "best_n_rationales": rationales,
        "cross_collection_transfer": transfers,
        "sensitivity": sensitivity,
        "block_recommendations": block_recommendations,
        "ridge_rows": selected_ridge_rows,
        "h_loss": h_loss,
        "intensity_effect_count": len(intensity_effects),
        "retained_intensity_effects": len(retained_effects),
        "selected_spill": selected_spill,
        "verification_reports": [str(path.resolve()) for path in verification_paths],
    }
    payload_path = publication_root / "results_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    for role, source in (
        *((f"verification:{path.stem}", path) for path in verification_paths),
        ("poster:content", content_path),
        ("paper:results_table", results_table_path),
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
        f"- selected spill panel: `{selected_spill_path}`",
        "",
        "The ridge figure uses exact common spill/window picks and plane-specific selected N. Its subtraction and concentration metrics do not measure physical noise or absolute tune accuracy.",
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
            args.publication_root.resolve(),
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"selected_sizes": payload["selected_sizes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
