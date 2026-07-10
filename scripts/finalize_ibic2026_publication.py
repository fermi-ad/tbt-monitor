#!/usr/bin/env python3
"""Verify final IBIC artifacts and write the publication inventory/compliance report."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Sequence

from bpm_mining.ridge_verification import png_dimensions


ABSTRACT_SHA256 = "e125b5889dbd28e35e17154297a0abb7abd2ce2ec26538a6c7d5301c67b8eea4"
POSTER_TEMPLATE_SHA256 = "ca9647b1db39860ebdc83854c432842f0dd09b0a7601c8f4af1bd2bf405468a9"
JACOW_SHA256 = "e902c3c4ff34a98604d17ba3dd44989b9ed6c042bfdd179eb4f1b700515f291c"
MANIFEST_FIELDS = ("path", "size_bytes", "sha256")
UNRESOLVED = re.compile(
    r"\b(?:pending|provisional|tbd|todo)\b|\[\s+\]|final manuscript will report",
    re.IGNORECASE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"required publication file is missing or empty: {path}")
    return path


def read_json(path: Path) -> dict[str, object]:
    require_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid publication JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"publication JSON root is not an object: {path}")
    return value


def parse_pdfinfo(text: str) -> dict[str, object]:
    pages_match = re.search(r"^Pages:\s+(\d+)\s*$", text, re.MULTILINE)
    size_match = re.search(
        r"^Page size:\s+([0-9.]+)\s+x\s+([0-9.]+)\s+pts(?:\s+\(([^)]+)\))?",
        text,
        re.MULTILINE,
    )
    if not pages_match or not size_match:
        raise ValueError("pdfinfo output is missing page count or page size")
    return {
        "pages": int(pages_match.group(1)),
        "width_points": float(size_match.group(1)),
        "height_points": float(size_match.group(2)),
        "label": size_match.group(3) or "",
    }


def pdf_info(pdfinfo: str, path: Path) -> dict[str, object]:
    require_file(path)
    completed = subprocess.run(
        [pdfinfo, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_pdfinfo(completed.stdout)


def require_geometry(
    label: str,
    info: dict[str, object],
    pages: int,
    width: float,
    height: float,
    tolerance: float,
) -> None:
    if int(info["pages"]) != pages:
        raise ValueError(f"{label} has {info['pages']} pages; expected {pages}")
    if not math.isclose(float(info["width_points"]), width, abs_tol=tolerance) or not math.isclose(
        float(info["height_points"]), height, abs_tol=tolerance
    ):
        raise ValueError(
            f"{label} page size is {info['width_points']} x {info['height_points']} pt; "
            f"expected {width} x {height}"
        )


def require_png(path: Path, minimum: tuple[int, int]) -> tuple[int, int]:
    dimensions = png_dimensions(path)
    if dimensions is None or dimensions[0] < minimum[0] or dimensions[1] < minimum[1]:
        raise ValueError(f"PNG is missing, invalid, or undersized: {path}: {dimensions}")
    return dimensions


def publication_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "publication_manifest.csv"
        and path.name != ".DS_Store"
    )


def write_manifest(path: Path, root: Path, files: Sequence[Path]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for file in files:
            writer.writerow(
                {
                    "path": file.relative_to(root).as_posix(),
                    "size_bytes": file.stat().st_size,
                    "sha256": sha256(file),
                }
            )


def finalize(
    root: Path,
    abstract: Path,
    poster_template: Path,
    pdfinfo: str,
    poster_visual_qa: str,
    paper_visual_qa: str,
) -> list[Path]:
    root = root.resolve()
    abstract = require_file(abstract.resolve())
    poster_template = require_file(poster_template.resolve())
    reference_hashes = {
        "accepted abstract": (sha256(abstract), ABSTRACT_SHA256),
        "poster template": (sha256(poster_template), POSTER_TEMPLATE_SHA256),
        "JACoW class": (sha256(require_file(root / "paper" / "jacow.cls")), JACOW_SHA256),
    }
    for label, (actual, expected) in reference_hashes.items():
        if actual != expected:
            raise ValueError(f"{label} SHA-256 mismatch: {actual} != {expected}")
    if poster_visual_qa != "pass" or paper_visual_qa != "pass":
        raise ValueError("poster and paper visual QA must both be explicitly marked pass")

    required = (
        root / "PREPARATION_REPORT.md",
        root / "results_payload.json",
        root / "source_manifest.csv",
        root / "poster" / "content.json",
        root / "poster" / "build" / "ibic2026-abstract54-poster.pptx",
        root / "poster" / "build" / "ibic2026-abstract54-poster.pdf",
        root / "poster" / "build" / "ibic2026-abstract54-poster.png",
        root / "poster" / "build" / "source_manifest.json",
        root / "poster" / "build" / "rendered" / "poster-1.png",
        root / "paper" / "ABSTRACT54.tex",
        root / "paper" / "results_table.tex",
        root / "paper" / "results_macros.tex",
        root / "paper" / "build" / "ABSTRACT54.pdf",
        root / "paper" / "build" / "source_manifest.sha256",
        *(root / "paper" / "build" / "rendered" / f"page-{page}.png" for page in range(1, 5)),
    )
    for path in required:
        require_file(path)
    for figure in (
        "best_n_validation_h.png",
        "best_n_validation_v.png",
        "ridge_density_comparison.png",
        "ridge_width_contrast_hv.png",
        "horizontal_loss_diagnostic.png",
    ):
        require_png(root / "paper" / "figures" / figure, (500, 300))
        require_png(root / "poster" / "assets" / figure, (500, 300))

    poster_pdf = root / "poster" / "build" / "ibic2026-abstract54-poster.pdf"
    paper_pdf = root / "paper" / "build" / "ABSTRACT54.pdf"
    poster_info = pdf_info(pdfinfo, poster_pdf)
    paper_info = pdf_info(pdfinfo, paper_pdf)
    require_geometry("poster PDF", poster_info, 1, 2383.94, 3370.39, 4.0)
    require_geometry("paper PDF", paper_info, 4, 595.0, 792.0, 2.0)
    poster_preview = require_png(
        root / "poster" / "build" / "ibic2026-abstract54-poster.png",
        (3000, 4000),
    )
    poster_render = require_png(
        root / "poster" / "build" / "rendered" / "poster-1.png",
        (4500, 6500),
    )
    paper_renders = [
        require_png(root / "paper" / "build" / "rendered" / f"page-{page}.png", (1000, 1400))
        for page in range(1, 5)
    ]

    payload = read_json(root / "results_payload.json")
    selected_sizes = payload.get("selected_sizes")
    if not isinstance(selected_sizes, dict) or set(selected_sizes) != {"H", "V"}:
        raise ValueError("results payload is missing exact H/V selected sizes")
    if any(int(selected_sizes[plane]) <= 0 for plane in ("H", "V")):
        raise ValueError("results payload contains a nonpositive selected size")
    if int(payload.get("retained_intensity_effects") or 0) != 0:
        raise ValueError("publication payload retains an intensity weighting effect")
    best_n_design = payload.get("best_n_design")
    if not isinstance(best_n_design, dict):
        raise ValueError("publication payload is missing the Best-N study design")
    for field, expected in (
        ("curve_spill_plane_count", 4_000),
        ("validation_spill_plane_count", 1_000),
        ("digitizer_fold_count", 5),
        ("maximum_n", 40),
        ("curve_evaluation_row_count", 160_000),
        ("validation_evaluation_row_count", 200_000),
    ):
        if int(best_n_design.get(field) or 0) != expected:
            raise ValueError(f"publication Best-N study-design mismatch: {field}")
    payload_integrity = payload.get("payload_integrity")
    if not isinstance(payload_integrity, dict) or payload_integrity.get("status") != "pass":
        raise ValueError("publication payload does not contain a passing raw-payload audit")
    for field, expected in (
        ("analysis_turns", 50_000),
        ("plateau_turns", 128),
        ("manifest_count", 2_200),
        ("stream_rows", 263_999),
        ("paired_stream_rows", 23_999),
        ("incomplete_manifests", 1),
        ("flagged_rows", 0),
        ("position_plateau_rows", 0),
        ("paired_plateau_rows", 0),
        ("raw_device_fallback_pair_rows", 0),
    ):
        if int(payload_integrity.get(field) or 0) != expected:
            raise ValueError(f"publication raw-payload audit mismatch: {field}")
    topology = payload_integrity.get("topology")
    if not isinstance(topology, dict) or len(topology) != 3:
        raise ValueError("publication raw-payload audit is missing three collection topologies")
    for collection, raw in topology.items():
        if not isinstance(raw, dict):
            raise ValueError(f"publication raw-payload topology is invalid: {collection}")
        if (
            int(raw.get("unique_position_streams") or 0) != 120
            or int(raw.get("unique_h_streams") or 0) != 60
            or int(raw.get("unique_v_streams") or 0) != 60
            or int(raw.get("unique_digitizers") or 0) != 30
            or raw.get("bad_digitizers")
        ):
            raise ValueError(f"publication raw-payload topology mismatch: {collection}")
    if len(str(payload_integrity.get("manifest_inventory_sha256") or "")) != 64:
        raise ValueError("publication raw-payload audit is missing its manifest hash")
    sensitivity = payload.get("sensitivity")
    if not isinstance(sensitivity, dict) or int(sensitivity.get("run_count") or 0) != 7:
        raise ValueError("publication payload does not contain seven sensitivity runs")
    transfers = payload.get("cross_collection_transfer")
    if not isinstance(transfers, list) or len(transfers) != 4 or any(
        not isinstance(row, dict) or row.get("status") != "OK" for row in transfers
    ):
        raise ValueError("publication payload does not contain four OK transfer rows")

    for path in (
        root / "poster" / "content.json",
        root / "paper" / "ABSTRACT54.tex",
        root / "paper" / "results_table.tex",
        root / "paper" / "results_macros.tex",
    ):
        if UNRESOLVED.search(path.read_text(encoding="utf-8")):
            raise ValueError(f"publication source contains unresolved copy: {path}")

    report = root / "compliance_report.md"
    lines = [
        "# IBIC 2026 Publication Compliance",
        "",
        "All checks below passed. Visual QA was explicitly completed on the final rendered artifacts.",
        "",
        f"- selected ensembles: H Best-{selected_sizes['H']}, V Best-{selected_sizes['V']}",
        "- retained intensity weighting effects: 0",
        "- Best-N design: 4000 full-curve spill-plane cases; 1000 validation cases x 5 folds",
        "- raw payload audit: 263999 streams through turn 50000, no blocking findings",
        "- Best-N sensitivity runs: 7",
        "- cross-collection transfer rows: 4 OK",
        f"- poster: {poster_info['pages']} A0 page, {poster_info['width_points']} x {poster_info['height_points']} pt",
        f"- paper: {paper_info['pages']} pages, {paper_info['width_points']} x {paper_info['height_points']} pt",
        f"- poster preview pixels: {poster_preview[0]} x {poster_preview[1]}",
        f"- poster PDF render pixels: {poster_render[0]} x {poster_render[1]}",
        "- paper render pixels: " + ", ".join(f"{width} x {height}" for width, height in paper_renders),
        f"- poster visual QA: {poster_visual_qa}",
        f"- paper visual QA: {paper_visual_qa}",
        "",
        "## Authoritative References",
        "",
        f"- accepted abstract: `{abstract}` (`{reference_hashes['accepted abstract'][0]}`)",
        f"- Fermilab poster template: `{poster_template}` (`{reference_hashes['poster template'][0]}`)",
        f"- tracked JACoW class: `{reference_hashes['JACoW class'][0]}`",
        "",
        "The result remains BPM-only internal reproducibility evidence. It does not establish absolute tune accuracy, physical noise removal, or extraction onset.",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    files = publication_files(root)
    write_manifest(root / "publication_manifest.csv", root, files)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--abstract", type=Path, required=True)
    parser.add_argument("--poster-template", type=Path, required=True)
    parser.add_argument("--pdfinfo", default="pdfinfo")
    parser.add_argument("--poster-visual-qa", choices=("pass", "fail"), required=True)
    parser.add_argument("--paper-visual-qa", choices=("pass", "fail"), required=True)
    args = parser.parse_args(argv)
    try:
        files = finalize(
            args.root,
            args.abstract,
            args.poster_template,
            args.pdfinfo,
            args.poster_visual_qa,
            args.paper_visual_qa,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))
    print(f"verified and inventoried {len(files)} publication files under {args.root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
