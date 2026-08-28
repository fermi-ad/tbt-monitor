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
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from bpm_mining.ridge_verification import png_dimensions
from prepare_ibic2026_publication import (
    EXPECTED_SOURCE_ROLE_COUNTS,
    PAYLOAD_AUDIT_MANIFEST_SHA256,
    PAYLOAD_MISSING_INVENTORY_SHA256,
    PAYLOAD_AUDIT_TOPOLOGY_EXPECTED,
    RESULTS_PAYLOAD_FIELDS,
    RESULTS_PAYLOAD_SCHEMA,
)


ABSTRACT_SHA256 = "e125b5889dbd28e35e17154297a0abb7abd2ce2ec26538a6c7d5301c67b8eea4"
POSTER_TEMPLATE_SHA256 = "ca9647b1db39860ebdc83854c432842f0dd09b0a7601c8f4af1bd2bf405468a9"
POSTER_STARTER_SHA256 = "b21f8c2e1d121f0d39ec1428576ae19d7ffdf1dd50a55b0a29df8e195ac8be60"
JACOW_SHA256 = "e902c3c4ff34a98604d17ba3dd44989b9ed6c042bfdd179eb4f1b700515f291c"
MANIFEST_FIELDS = ("path", "size_bytes", "sha256")
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
POSTER_BASE = "ibic2026-abstract54-poster"
POSTER_ASSET_FILES = {
    "beamlineMap": "muon-campus-beamlines.png",
    "bestNH": "best_n_validation_h.png",
    "bestNV": "best_n_validation_v.png",
    "ridgeHV": "ridge_density_comparison.png",
}
POSTER_EVIDENCE_GATE_SCHEMA = "tbt-monitor.ibic2026-poster-evidence-gate/v3"
POSTER_INPUT_MANIFEST_SCHEMA = "tbt-monitor.ibic2026-poster-inputs/v2"
POSTER_GATE_INPUT_ROLES = frozenset({"resultsPayload", *POSTER_ASSET_FILES})
POSTER_MAP_CREDIT = (
    "Beamline layout courtesy of George Deinlein, Fermilab staff; used with permission."
)
POSTER_MAP_ATTRIBUTION = {
    "creator": "George Deinlein",
    "affiliation": "Fermilab",
    "role": "staff",
    "creditLine": POSTER_MAP_CREDIT,
    "permissionStatus": "full",
    "permissionScope": "this poster's publication reuse",
    "confirmedOn": "2026-08-19",
}
POSTER_REPORT_NUMBER = "FERMILAB-POSTER-26-0268-AD"
POSTER_ACKNOWLEDGMENT = (
    "This manuscript has been authored by FermiForward Discovery Group, LLC under "
    "Contract No. 89243024CSC000002 with the U.S. Department of Energy, Office of "
    "Science, Office of High Energy Physics."
)
POSTER_PUBLICATION_REQUIREMENTS = {
    "reportNumber": POSTER_REPORT_NUMBER,
    "acknowledgment": POSTER_ACKNOWLEDGMENT,
    "template": {
        "name": "FNAL Scientific Poster A0 Vertical May25",
        "url": "https://www.fnal.gov/faw/designstandards/templates/index.html",
        "placement": {
            "reportNumber": "upper-right blue area",
            "acknowledgment": "bottom-left corner",
        },
    },
    "confirmedOn": "2026-08-19",
}
PAPER_FIGURE_FILES = (
    "best_n_validation_hv.pdf",
    "ridge_density_comparison.pdf",
    "ridge_width_contrast_hv.pdf",
)
PAPER_FIGURE_GEOMETRY = {
    "best_n_validation_hv.pdf": (516.0, 228.0),
    "ridge_density_comparison.pdf": (516.0, 326.0),
    "ridge_width_contrast_hv.pdf": (440.0, 214.0),
}
MATERIALIZATION_MANIFEST_FIELDS = (
    "role",
    "source_path",
    "source_sha256",
    "output_path",
    "output_sha256",
)
PRE_ACCEPTANCE_MATERIALIZED_OUTPUTS = frozenset(
    {
        "PREPARATION_REPORT.md",
        "results_payload.json",
        "poster/content.json",
        "paper/results_table.tex",
        "paper/results_macros.tex",
        *(f"poster/assets/{filename}" for filename in POSTER_ASSET_FILES.values()),
        *(f"paper/figures/{filename}" for filename in PAPER_FIGURE_FILES),
    }
)
FROZEN_EVIDENCE_OUTPUTS = frozenset(
    output
    for output in PRE_ACCEPTANCE_MATERIALIZED_OUTPUTS
    if output != "poster/content.json" and not output.startswith("poster/assets/")
)
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


def finite_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def require_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"required publication file is missing or empty: {path}")
    return path


def require_identical_files(label: str, left: Path, right: Path) -> None:
    require_file(left)
    require_file(right)
    if left.stat().st_size != right.stat().st_size or sha256(left) != sha256(right):
        raise ValueError(f"{label} files differ: {left} != {right}")


def empty_structural_placeholders(pptx: Path) -> list[str]:
    """Return empty placeholder shapes found in slide XML without mutating the PPTX."""
    require_file(pptx)
    namespaces = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    }
    try:
        with zipfile.ZipFile(pptx) as archive:
            slide_members = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            if not slide_members:
                raise ValueError(f"poster PPTX contains no slide XML: {pptx}")
            findings: list[str] = []
            for member in slide_members:
                root = ET.fromstring(archive.read(member))
                for shape in root.findall(".//p:sp", namespaces):
                    placeholder = shape.find("./p:nvSpPr/p:nvPr/p:ph", namespaces)
                    if placeholder is None:
                        continue
                    text = "".join(
                        node.text or "" for node in shape.findall(".//a:t", namespaces)
                    ).strip()
                    if text:
                        continue
                    properties = shape.find("./p:nvSpPr/p:cNvPr", namespaces)
                    shape_id = properties.get("id", "?") if properties is not None else "?"
                    shape_name = (
                        properties.get("name", "unnamed")
                        if properties is not None
                        else "unnamed"
                    )
                    findings.append(f"{member}: shape {shape_id} ({shape_name})")
            return findings
    except (ET.ParseError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid poster PPTX OOXML: {pptx}: {exc}") from exc


def read_json(path: Path) -> dict[str, object]:
    require_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid publication JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"publication JSON root is not an object: {path}")
    return value


def forbidden_payload_key_paths(
    value: object,
    forbidden_terms: Sequence[str] = ("intensity", "legacy"),
    prefix: str = "$",
) -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}"
            if any(term in key.lower() for term in forbidden_terms):
                findings.append(path)
            findings.extend(forbidden_payload_key_paths(child, forbidden_terms, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(forbidden_payload_key_paths(child, forbidden_terms, f"{prefix}[{index}]"))
    return findings


def validate_results_payload_contract(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Enforce the exact v2 publication envelope before semantic checks."""
    if payload.get("schema") != RESULTS_PAYLOAD_SCHEMA:
        raise ValueError(
            f"results payload schema must be {RESULTS_PAYLOAD_SCHEMA}, got {payload.get('schema')!r}"
        )
    if set(payload) != RESULTS_PAYLOAD_FIELDS:
        raise ValueError(
            "results payload field inventory mismatch: "
            f"missing={sorted(RESULTS_PAYLOAD_FIELDS - set(payload))}, "
            f"extra={sorted(set(payload) - RESULTS_PAYLOAD_FIELDS)}"
        )
    forbidden_keys = forbidden_payload_key_paths(payload)
    if forbidden_keys:
        raise ValueError(f"results payload contains stale sidecar fields: {forbidden_keys[:10]}")
    return payload


def reject_stale_results_macros(macros_text: str) -> None:
    """Reject generated commands retained from the standalone intensity sidecar."""
    if re.search(r"\\(?:re)?newcommand\s*\{\\Intensity", macros_text, re.IGNORECASE):
        raise ValueError("paper results macros contain a stale intensity command")


def verify_sha256_manifest(path: Path, expected: Mapping[str, Path]) -> None:
    require_file(path)
    rows: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = SHA256_LINE.fullmatch(raw)
        if not match:
            raise ValueError(f"invalid SHA-256 manifest row: {path}:{line_number}")
        digest, label = match.groups()
        logical_path = Path(label)
        if logical_path.is_absolute() or ".." in logical_path.parts:
            raise ValueError(f"nonportable SHA-256 manifest path: {path}:{line_number}: {label}")
        if label in rows:
            raise ValueError(f"duplicate SHA-256 manifest path: {path}: {label}")
        rows[label] = digest
    if set(rows) != set(expected):
        missing = sorted(set(expected) - set(rows))
        extra = sorted(set(rows) - set(expected))
        raise ValueError(f"SHA-256 manifest inventory mismatch: {path}: missing={missing}, extra={extra}")
    for label, source in expected.items():
        actual = sha256(require_file(source))
        if rows[label] != actual:
            raise ValueError(
                f"SHA-256 manifest mismatch: {path}: {label}: {rows[label]} != {actual}"
            )


def safe_materialized_path(value: str) -> PurePosixPath | None:
    if not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def verify_publication_source_manifest(root: Path) -> None:
    path = require_file(root / "source_manifest.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != MATERIALIZATION_MANIFEST_FIELDS:
            raise ValueError("publication source manifest has the wrong schema")
        rows = list(reader)
    if not rows:
        raise ValueError("publication source manifest is empty")

    outputs: set[str] = set()
    row_keys: set[tuple[str, str, str]] = set()
    role_counts: Counter[str] = Counter()
    root_resolved = root.resolve()
    for line_number, row in enumerate(rows, start=2):
        role = row.get("role", "")
        source_path = row.get("source_path", "")
        source_digest = row.get("source_sha256", "")
        output_path = row.get("output_path", "")
        output_digest = row.get("output_sha256", "")
        key = (role, source_path, output_path)
        if not role or safe_materialized_path(source_path) is None or key in row_keys:
            raise ValueError(f"invalid publication source manifest identity at line {line_number}")
        row_keys.add(key)
        if re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
            raise ValueError(f"invalid publication source hash at line {line_number}")
        # Poster revisions are accepted through the independent, paper-gated
        # poster manifests.  The pre-acceptance publication manifest remains a
        # frozen record of how the evidence packet and paper were produced, so
        # its historical poster rows are intentionally not revalidated here.
        if role.startswith("poster:"):
            continue
        role_counts[role] += 1
        if not output_path:
            if output_digest:
                raise ValueError(f"source-only manifest row has an output hash at line {line_number}")
            continue
        relative = safe_materialized_path(output_path)
        if (
            relative is None
            or output_path in outputs
            or re.fullmatch(r"[0-9a-f]{64}", output_digest) is None
        ):
            raise ValueError(f"invalid publication output manifest row at line {line_number}")
        outputs.add(output_path)
        target = root.joinpath(*relative.parts)
        resolved_target = target.resolve()
        has_symlink = any(
            root.joinpath(*relative.parts[:part_count]).is_symlink()
            for part_count in range(1, len(relative.parts) + 1)
        )
        if has_symlink or root_resolved not in resolved_target.parents:
            raise ValueError(f"unsafe publication output path at line {line_number}: {output_path}")
        require_file(target)
        actual_digest = sha256(target)
        if actual_digest != output_digest or source_digest != output_digest:
            raise ValueError(f"publication source manifest hash mismatch: {output_path}")

    if outputs != FROZEN_EVIDENCE_OUTPUTS:
        missing = sorted(FROZEN_EVIDENCE_OUTPUTS - outputs)
        extra = sorted(outputs - FROZEN_EVIDENCE_OUTPUTS)
        raise ValueError(
            f"publication source manifest output inventory mismatch: missing={missing}, extra={extra}"
        )
    expected_frozen_role_counts = Counter(
        {
            role: count
            for role, count in EXPECTED_SOURCE_ROLE_COUNTS.items()
            if not role.startswith("poster:")
        }
    )
    if role_counts != expected_frozen_role_counts:
        missing = expected_frozen_role_counts - role_counts
        extra = role_counts - expected_frozen_role_counts
        raise ValueError(
            "publication source-role inventory mismatch: "
            f"missing={dict(sorted(missing.items()))}, extra={dict(sorted(extra.items()))}"
        )


def require_recorded_sha256(label: str, value: object, source: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"poster source manifest is missing {label}")
    recorded = str(value.get("sha256") or "")
    actual = sha256(require_file(source))
    if recorded != actual:
        raise ValueError(f"poster source manifest mismatch: {label}: {recorded} != {actual}")
    return value


def _gate_record(
    repo_root: Path,
    value: object,
    label: str,
    expected_path: str | None = None,
) -> tuple[dict[str, object], Path]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"poster evidence gate has an invalid {label} record")
    raw_path = str(value.get("path") or "")
    relative = safe_materialized_path(raw_path)
    if relative is None or (expected_path is not None and raw_path != expected_path):
        raise ValueError(f"poster evidence gate has an invalid {label} path: {raw_path}")
    source = repo_root.joinpath(*relative.parts)
    recorded = str(value.get("sha256") or "")
    actual = sha256(require_file(source))
    if recorded != actual:
        raise ValueError(f"poster evidence gate mismatch: {label}: {recorded} != {actual}")
    return value, source


def verify_poster_input_manifest(root: Path) -> None:
    repo_root = root.resolve().parents[1]
    poster = root / "poster"
    gate_path = poster / "evidence_gate.json"
    gate = read_json(gate_path)
    if gate.get("schema") != POSTER_EVIDENCE_GATE_SCHEMA:
        raise ValueError("poster evidence gate has the wrong schema")

    attribution = gate.get("mapAttribution")
    if attribution != POSTER_MAP_ATTRIBUTION:
        raise ValueError(
            "poster evidence gate does not record the required map attribution and permission"
        )
    publication_requirements = gate.get("publicationRequirements")
    if publication_requirements != POSTER_PUBLICATION_REQUIREMENTS:
        raise ValueError(
            "poster evidence gate does not record the required publication metadata"
        )
    context = gate.get("context")
    if not isinstance(context, dict):
        raise ValueError("poster evidence gate has invalid map source context")

    paper = gate.get("paper")
    inputs = gate.get("inputs")
    if not isinstance(paper, dict) or set(paper) != {"source", "pdf"}:
        raise ValueError("poster evidence gate has the wrong paper inventory")
    if not isinstance(inputs, dict) or set(inputs) != POSTER_GATE_INPUT_ROLES:
        raise ValueError("poster evidence gate has the wrong input inventory")

    verified_paper: dict[str, dict[str, object]] = {}
    for role, expected in {
        "source": "publication/ibic2026/paper/ABSTRACT54.tex",
        "pdf": "publication/ibic2026/paper/build/ABSTRACT54.pdf",
    }.items():
        record, _ = _gate_record(repo_root, paper.get(role), f"paper {role}", expected)
        verified_paper[role] = record
    verified_inputs: dict[str, dict[str, object]] = {}
    for role in POSTER_GATE_INPUT_ROLES:
        expected = "publication/ibic2026/results_payload.json" if role == "resultsPayload" else None
        record, _ = _gate_record(repo_root, inputs.get(role), f"input {role}", expected)
        verified_inputs[role] = record

    manifest = read_json(poster / "input_manifest.json")
    if manifest.get("schema") != POSTER_INPUT_MANIFEST_SCHEMA:
        raise ValueError("poster input manifest has the wrong schema")
    if manifest.get("mapAttribution") != attribution:
        raise ValueError("poster input manifest does not preserve map attribution and permission")
    if manifest.get("publicationRequirements") != publication_requirements:
        raise ValueError(
            "poster input manifest does not preserve publication and print requirements"
        )
    if manifest.get("context") != context:
        raise ValueError("poster input manifest does not preserve map source context")
    gate_record = manifest.get("evidenceGate")
    if (
        not isinstance(gate_record, dict)
        or gate_record.get("path") != "publication/ibic2026/poster/evidence_gate.json"
        or gate_record.get("sha256") != sha256(gate_path)
    ):
        raise ValueError("poster input manifest does not bind the evidence gate")

    immutability = manifest.get("paperImmutability")
    if not isinstance(immutability, dict) or set(immutability) != set(verified_paper):
        raise ValueError("poster input manifest has the wrong paper-immutability inventory")
    for role, gate_value in verified_paper.items():
        record = immutability.get(role)
        expected_hash = gate_value["sha256"]
        if (
            not isinstance(record, dict)
            or record.get("path") != gate_value["path"]
            or record.get("sha256Before") != expected_hash
            or record.get("sha256After") != expected_hash
            or record.get("unchanged") is not True
        ):
            raise ValueError(f"poster input manifest does not preserve paper {role}")

    manifest_inputs = manifest.get("inputs")
    if not isinstance(manifest_inputs, dict) or set(manifest_inputs) != POSTER_GATE_INPUT_ROLES:
        raise ValueError("poster input manifest has the wrong verified-input inventory")
    for role, gate_value in verified_inputs.items():
        record = manifest_inputs.get(role)
        if (
            not isinstance(record, dict)
            or record.get("path") != gate_value["path"]
            or record.get("sha256") != gate_value["sha256"]
        ):
            raise ValueError(f"poster input manifest mismatch: input {role}")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"content", "assets"}:
        raise ValueError("poster input manifest has the wrong output inventory")
    content = outputs.get("content")
    if (
        not isinstance(content, dict)
        or content.get("path") != "publication/ibic2026/poster/content.json"
        or content.get("sha256") != sha256(require_file(poster / "content.json"))
    ):
        raise ValueError("poster input manifest mismatch: content")
    poster_content = read_json(poster / "content.json")
    if poster_content.get("mapCredit") != POSTER_MAP_CREDIT:
        raise ValueError("poster content does not contain the required map credit")
    if poster_content.get("reportNumber") != POSTER_REPORT_NUMBER:
        raise ValueError("poster content does not contain the assigned report number")
    if poster_content.get("acknowledgment") != POSTER_ACKNOWLEDGMENT:
        raise ValueError("poster content does not contain the required acknowledgment")
    output_assets = outputs.get("assets")
    if not isinstance(output_assets, dict) or set(output_assets) != set(POSTER_ASSET_FILES):
        raise ValueError("poster input manifest has the wrong asset-output inventory")
    for role, filename in POSTER_ASSET_FILES.items():
        source = poster / "assets" / filename
        dimensions = png_dimensions(source)
        record = output_assets.get(role)
        if (
            dimensions is None
            or not isinstance(record, dict)
            or record.get("path") != f"publication/ibic2026/poster/assets/{filename}"
            or record.get("sha256") != sha256(require_file(source))
            or record.get("dimensions")
            != {"width": dimensions[0], "height": dimensions[1]}
        ):
            raise ValueError(f"poster input manifest mismatch: asset {role}")


def verify_poster_source_manifest(root: Path) -> None:
    poster = root / "poster"
    build = poster / "build"
    verify_poster_input_manifest(root)
    manifest = read_json(build / "source_manifest.json")
    if manifest.get("schema") != "tbt-monitor.ibic2026-poster-source/v2":
        raise ValueError("poster source manifest has the wrong schema")
    starter = manifest.get("starter")
    if not isinstance(starter, dict) or starter.get("sha256") != POSTER_STARTER_SHA256:
        raise ValueError("poster source manifest has the wrong prepared-starter hash")
    require_recorded_sha256("content", manifest.get("content"), poster / "content.json")
    require_recorded_sha256(
        "evidence gate", manifest.get("evidenceGate"), poster / "evidence_gate.json"
    )
    require_recorded_sha256(
        "input manifest", manifest.get("inputManifest"), poster / "input_manifest.json"
    )

    assets = manifest.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(POSTER_ASSET_FILES):
        raise ValueError("poster source manifest has the wrong asset inventory")
    for key, filename in POSTER_ASSET_FILES.items():
        source = poster / "assets" / filename
        record = require_recorded_sha256(f"asset {key}", assets.get(key), source)
        dimensions = png_dimensions(source)
        raw_dimensions = record.get("dimensions")
        if (
            dimensions is None
            or not isinstance(raw_dimensions, dict)
            or int(raw_dimensions.get("width") or 0) != dimensions[0]
            or int(raw_dimensions.get("height") or 0) != dimensions[1]
        ):
            raise ValueError(f"poster source manifest dimension mismatch: asset {key}")

    outputs = manifest.get("outputs")
    expected_outputs = {
        "pptx": build / f"{POSTER_BASE}.pptx",
        "artifactPreview": build / f"{POSTER_BASE}-artifact-preview.png",
        "layout": build / "layout" / "final-slide-01.layout.json",
    }
    if not isinstance(outputs, dict) or set(outputs) != set(expected_outputs):
        raise ValueError("poster source manifest has the wrong output inventory")
    for key, source in expected_outputs.items():
        require_recorded_sha256(f"output {key}", outputs.get(key), source)


def verify_template_fidelity(path: Path) -> None:
    report = read_json(path)
    if (
        report.get("status") != "pass"
        or report.get("issueCount") != 0
        or report.get("issues") != []
    ):
        raise ValueError("poster template-fidelity report is not a zero-issue pass")


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
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        writer.writeheader()
        for file in files:
            writer.writerow(
                {
                    "path": file.relative_to(root).as_posix(),
                    "size_bytes": file.stat().st_size,
                    "sha256": sha256(file),
                }
            )


def validate_sensitivity_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or int(value.get("run_count") or 0) != 7:
        raise ValueError("publication payload does not contain seven sensitivity runs")
    minimum_available = int(value.get("minimum_available_per_plane") or 0)
    if minimum_available != 4:
        raise ValueError("publication sensitivity majority threshold is not four of seven")
    recommendations = value.get("recommendations")
    unavailable = value.get("unavailable")
    ranges = value.get("ranges")
    runs = value.get("runs")
    if (
        not isinstance(recommendations, dict)
        or not isinstance(unavailable, dict)
        or not isinstance(ranges, dict)
        or not isinstance(runs, list)
        or len(runs) != 7
    ):
        raise ValueError("publication sensitivity payload is incomplete")
    run_names = {
        str(row.get("run") or "")
        for row in runs
        if isinstance(row, dict)
    }
    if len(run_names) != 7 or "" in run_names:
        raise ValueError("publication sensitivity payload does not identify seven unique runs")
    for plane in ("H", "V"):
        plane_values = recommendations.get(plane)
        raw_range = ranges.get(plane)
        if not isinstance(plane_values, list) or len(plane_values) < minimum_available:
            raise ValueError(f"publication sensitivity lacks majority {plane} coverage")
        if not all(isinstance(item, int) and item > 0 for item in plane_values):
            raise ValueError(f"publication sensitivity contains an invalid {plane} recommendation")
        expected_unavailable = 7 - len(plane_values)
        if int(unavailable.get(plane) or 0) != expected_unavailable:
            raise ValueError(f"publication sensitivity {plane} unavailable count is inconsistent")
        if not isinstance(raw_range, dict) or any(
            int(raw_range.get(field) or 0) != expected
            for field, expected in (
                ("available", len(plane_values)),
                ("unavailable", expected_unavailable),
                ("minimum", min(plane_values)),
                ("maximum", max(plane_values)),
            )
        ):
            raise ValueError(f"publication sensitivity {plane} range is inconsistent")
        run_values = []
        for row in runs:
            if not isinstance(row, dict):
                raise ValueError("publication sensitivity run detail is incomplete")
            run_recommendations = row.get("recommendations")
            run_reasons = row.get("reasons")
            if not isinstance(run_recommendations, dict) or not isinstance(run_reasons, dict):
                raise ValueError("publication sensitivity run detail is incomplete")
            run_value = run_recommendations.get(plane)
            if run_value is not None:
                if not isinstance(run_value, int) or run_value <= 0:
                    raise ValueError(f"publication sensitivity run has invalid {plane} N")
                run_values.append(run_value)
            if not str(run_reasons.get(plane) or "").strip():
                raise ValueError(f"publication sensitivity run lacks its {plane} disposition")
        if sorted(run_values) != sorted(plane_values):
            raise ValueError(f"publication sensitivity {plane} run details do not match its summary")
    return value


def validate_publication_coverage_payload(
    payload: Mapping[str, object],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    selected_sizes = payload.get("selected_sizes")
    if not isinstance(selected_sizes, Mapping) or set(selected_sizes) != {"H", "V"}:
        raise ValueError("publication coverage lacks exact H/V selected sizes")
    primary_raw = payload.get("primary_capture")
    if not isinstance(primary_raw, Mapping):
        raise ValueError("publication payload is missing primary capture completeness")
    primary = {
        field: int(primary_raw.get(field) or 0)
        for field in (
            "spill_count",
            "nominal_h_channels",
            "nominal_v_channels",
            "partial_capture_count",
            "source_absence_count",
        )
    }
    expected_primary = {
        "spill_count": 2_000,
        "nominal_h_channels": 60,
        "nominal_v_channels": 60,
        "partial_capture_count": 12,
        "source_absence_count": 16,
    }
    if primary != expected_primary:
        raise ValueError(f"publication primary capture completeness mismatch: {primary}")

    coverage_raw = payload.get("ridge_coverage")
    if not isinstance(coverage_raw, Mapping) or set(coverage_raw) != {"H", "V"}:
        raise ValueError("publication payload is missing selected H/V ridge coverage")
    coverage: dict[str, dict[str, int]] = {}
    for plane in ("H", "V"):
        raw = coverage_raw[plane]
        if not isinstance(raw, Mapping):
            raise ValueError(f"publication {plane} ridge coverage is invalid")
        values = {
            field: int(raw.get(field) or 0)
            for field in (
                "subset_size",
                "spill_count",
                "center_count",
                "sliding_rows",
                "ridge_points",
                "missing_tune_rows",
                "edge_excluded_rows",
            )
        }
        if (
            values["subset_size"] != int(selected_sizes[plane])
            or values["spill_count"] != 2_000
            or values["center_count"] != 180
            or values["sliding_rows"] != 360_000
            or values["ridge_points"] <= 0
            or values["sliding_rows"]
            != values["ridge_points"]
            + values["missing_tune_rows"]
            + values["edge_excluded_rows"]
        ):
            raise ValueError(f"publication {plane} ridge coverage does not close: {values}")
        coverage[plane] = values
    return primary, coverage


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
        root / "poster" / "build" / f"{POSTER_BASE}.pptx",
        root / "poster" / "build" / f"{POSTER_BASE}.pdf",
        root / "poster" / "build" / f"{POSTER_BASE}.png",
        root / "poster" / "build" / f"{POSTER_BASE}-artifact-preview.png",
        root / "poster" / "build" / f"{POSTER_BASE}.pptx.inspect.ndjson",
        root / "poster" / "build" / "source_manifest.json",
        root / "poster" / "build" / "deliverable-sha256.txt",
        root / "poster" / "build" / "layout" / "final-slide-01.layout.json",
        root / "poster" / "build" / "qa" / "template-fidelity-check.json",
        root / "poster" / "build" / "qa" / "template-fidelity-check.txt",
        root / "poster" / "build" / "pdffonts.txt",
        root / "poster" / "build" / "rendered" / "poster-1.png",
        root / "paper" / "ABSTRACT54.tex",
        root / "paper" / "results_table.tex",
        root / "paper" / "results_macros.tex",
        root / "paper" / "build" / "ABSTRACT54.pdf",
        root / "paper" / "build" / "source_manifest.sha256",
        root / "paper" / "build" / "pdffonts.txt",
        *(root / "paper" / "build" / "rendered" / f"page-{page}.png" for page in range(1, 5)),
    )
    for path in required:
        require_file(path)
    verify_publication_source_manifest(root)
    verify_poster_source_manifest(root)
    verify_template_fidelity(
        root / "poster" / "build" / "qa" / "template-fidelity-check.json"
    )
    poster_build = root / "poster" / "build"
    verify_sha256_manifest(
        poster_build / "deliverable-sha256.txt",
        {
            f"{POSTER_BASE}.pptx": poster_build / f"{POSTER_BASE}.pptx",
            f"{POSTER_BASE}.pdf": poster_build / f"{POSTER_BASE}.pdf",
            f"{POSTER_BASE}.png": poster_build / f"{POSTER_BASE}.png",
            f"{POSTER_BASE}-artifact-preview.png": poster_build
            / f"{POSTER_BASE}-artifact-preview.png",
            "source_manifest.json": poster_build / "source_manifest.json",
            "layout/final-slide-01.layout.json": poster_build
            / "layout"
            / "final-slide-01.layout.json",
            f"{POSTER_BASE}.pptx.inspect.ndjson": poster_build
            / f"{POSTER_BASE}.pptx.inspect.ndjson",
            "qa/template-fidelity-check.json": poster_build
            / "qa"
            / "template-fidelity-check.json",
            "qa/template-fidelity-check.txt": poster_build
            / "qa"
            / "template-fidelity-check.txt",
            "pdffonts.txt": poster_build / "pdffonts.txt",
        },
    )
    paper = root / "paper"
    verify_sha256_manifest(
        paper / "build" / "source_manifest.sha256",
        {
            "ABSTRACT54.tex": paper / "ABSTRACT54.tex",
            "jacow.cls": paper / "jacow.cls",
            "results_table.tex": paper / "results_table.tex",
            "results_macros.tex": paper / "results_macros.tex",
            **{
                f"figures/{filename}": paper / "figures" / filename
                for filename in PAPER_FIGURE_FILES
            },
            "build/ABSTRACT54.pdf": paper / "build" / "ABSTRACT54.pdf",
        },
    )
    empty_placeholders = empty_structural_placeholders(
        root / "poster" / "build" / f"{POSTER_BASE}.pptx"
    )
    if empty_placeholders:
        raise ValueError(
            "poster PPTX contains empty structural placeholders: "
            + "; ".join(empty_placeholders)
        )
    for figure in POSTER_ASSET_FILES.values():
        require_png(root / "poster" / "assets" / figure, (500, 300))
    for figure, (width, height) in PAPER_FIGURE_GEOMETRY.items():
        require_geometry(
            f"paper figure {figure}",
            pdf_info(pdfinfo, root / "paper" / "figures" / figure),
            1,
            width,
            height,
            0.5,
        )

    poster_pdf = root / "poster" / "build" / f"{POSTER_BASE}.pdf"
    paper_pdf = root / "paper" / "build" / "ABSTRACT54.pdf"
    poster_info = pdf_info(pdfinfo, poster_pdf)
    paper_info = pdf_info(pdfinfo, paper_pdf)
    require_geometry("poster PDF", poster_info, 1, 2383.94, 3370.39, 4.0)
    require_geometry("paper PDF", paper_info, 4, 595.0, 792.0, 2.0)
    poster_preview = require_png(
        root / "poster" / "build" / f"{POSTER_BASE}.png",
        (3000, 4000),
    )
    poster_render = require_png(
        root / "poster" / "build" / "rendered" / "poster-1.png",
        (4500, 6500),
    )
    require_identical_files(
        "poster preview and PDF raster",
        root / "poster" / "build" / f"{POSTER_BASE}.png",
        root / "poster" / "build" / "rendered" / "poster-1.png",
    )
    paper_renders = [
        require_png(root / "paper" / "build" / "rendered" / f"page-{page}.png", (1000, 1400))
        for page in range(1, 5)
    ]

    payload = read_json(root / "results_payload.json")
    validate_results_payload_contract(payload)
    selected_sizes = payload.get("selected_sizes")
    if not isinstance(selected_sizes, dict) or set(selected_sizes) != {"H", "V"}:
        raise ValueError("results payload is missing exact H/V selected sizes")
    if any(int(selected_sizes[plane]) <= 0 for plane in ("H", "V")):
        raise ValueError("results payload contains a nonpositive selected size")
    primary_capture, ridge_coverage = validate_publication_coverage_payload(payload)
    all_training = payload.get("all_training_control")
    if (
        not isinstance(all_training, dict)
        or all_training.get("schema") != "tbt-monitor.ibic2026-all-training-control/v1"
        or all_training.get("selected_sizes") != selected_sizes
        or int(all_training.get("comparison_count") or 0) != 16
    ):
        raise ValueError("results payload is missing the accepted all-training control")
    all_training_by_plane = all_training.get("by_plane")
    if not isinstance(all_training_by_plane, dict) or set(all_training_by_plane) != {"H", "V"}:
        raise ValueError("results payload has incomplete all-training plane summaries")
    for plane in ("H", "V"):
        row = all_training_by_plane[plane]
        if not isinstance(row, dict):
            raise ValueError(f"results payload has an invalid {plane} all-training summary")
        counts = [
            int(row.get(field) or 0)
            for field in ("selected_favored", "baseline_favored", "unresolved")
        ]
        if any(value < 0 for value in counts) or sum(counts) != 8 or int(row.get("total") or 0) != 8:
            raise ValueError(f"results payload has an invalid {plane} all-training result count")
    adaptive_ridge_rows = payload.get("adaptive_ridge_rows")
    if not isinstance(adaptive_ridge_rows, dict) or set(adaptive_ridge_rows) != {"H", "V"}:
        raise ValueError("results payload is missing exact H/V corrected-Best-1 ridge contrasts")
    for plane in ("H", "V"):
        row = adaptive_ridge_rows[plane]
        contrast_values = [
            finite_number(row.get(field)) if isinstance(row, dict) else None
            for field in (
                "median_iqr_delta_ensemble_minus_baseline",
                "median_iqr_delta_ci_low",
                "median_iqr_delta_ci_high",
            )
        ]
        if (
            not isinstance(row, dict)
            or int(row.get("baseline_subset_size") or 0) != 1
            or int(row.get("ensemble_subset_size") or 0) != int(selected_sizes[plane])
            or any(value is None for value in contrast_values)
            or contrast_values[1] > contrast_values[2]
        ):
            raise ValueError(f"results payload has an invalid {plane} corrected-Best-1 ridge contrast")
    cross_spill_null = payload.get("cross_spill_null")
    if (
        not isinstance(cross_spill_null, dict)
        or cross_spill_null.get("schema") != "tbt-monitor.ibic2026-cross-spill-null/v1"
        or int(cross_spill_null.get("permutation_draws") or 0) != 1_000
        or int(cross_spill_null.get("block_spills") or 0) != 20
        or cross_spill_null.get("permutation_mode")
        != "seeded_block_derangement_shared_across_folds"
        or cross_spill_null.get("seed_namespace") != "best-n-cross-spill-null"
        or not math.isclose(
            finite_number(cross_spill_null.get("tune_half_width")) or math.nan,
            0.0025,
            abs_tol=1e-12,
        )
    ):
        raise ValueError("results payload is missing the accepted cross-spill null control")
    null_rows = cross_spill_null.get("rows")
    null_selected = cross_spill_null.get("selected")
    if (
        not isinstance(null_rows, list)
        or len(null_rows) != 80
        or not isinstance(null_selected, dict)
        or set(null_selected) != {"H", "V"}
    ):
        raise ValueError("results payload has an incomplete cross-spill null inventory")
    null_keys: set[tuple[str, int]] = set()
    for row in null_rows:
        if not isinstance(row, dict):
            raise ValueError("results payload contains a malformed cross-spill null row")
        plane = str(row.get("plane") or "")
        subset_size = int(row.get("subset_size") or 0)
        null_keys.add((plane, subset_size))
        values = [
            finite_number(row.get(field))
            for field in (
                "observed_agreement_rate",
                "null_mean_agreement_rate",
                "null_ci_low",
                "null_ci_high",
            )
        ]
        if (
            any(value is None for value in values)
            or not 0 <= values[0] <= 1
            or not 0 <= values[1] <= 1
            or not 0 <= values[2] <= values[3] <= 1
            or int(row.get("validation_spill_count") or 0) != 500
            or int(row.get("permutation_draws") or 0) != 1_000
            or int(row.get("valid_permutation_draws") or 0) != 1_000
            or int(row.get("block_spills") or 0) != 20
            or row.get("status") != "ok"
        ):
            raise ValueError(f"results payload has an invalid cross-spill null row: {plane} N={subset_size}")
    if null_keys != {(plane, size) for plane in ("H", "V") for size in range(1, 41)}:
        raise ValueError("results payload cross-spill null does not cover H/V N=1..40 exactly")
    best_n_rows = payload.get("best_n_rows")
    if not isinstance(best_n_rows, dict) or set(best_n_rows) != {"H", "V"}:
        raise ValueError("results payload is missing exact H/V Best-N result rows")
    for plane in ("H", "V"):
        row = null_selected[plane]
        accepted_best_n = best_n_rows[plane]
        matching_rows = [
            item
            for item in null_rows
            if isinstance(item, dict)
            and item.get("plane") == plane
            and int(item.get("subset_size") or 0) == int(selected_sizes[plane])
        ]
        if (
            not isinstance(row, dict)
            or not isinstance(accepted_best_n, dict)
            or row.get("plane") != plane
            or int(row.get("subset_size") or 0) != int(selected_sizes[plane])
            or len(matching_rows) != 1
            or row != matching_rows[0]
            or any(
                finite_number(row.get(field)) is None
                for field in (
                    "observed_agreement_rate",
                    "null_mean_agreement_rate",
                    "null_ci_low",
                    "null_ci_high",
                )
            )
            or not math.isclose(
                finite_number(row.get("observed_agreement_rate")) or math.nan,
                finite_number(accepted_best_n.get("blind_q_agreement_rate")) or math.nan,
                abs_tol=1e-9,
            )
        ):
            raise ValueError(f"results payload has the wrong selected {plane} null row")

    best1_membership = payload.get("best1_membership")
    membership_by_plane = (
        best1_membership.get("by_plane") if isinstance(best1_membership, dict) else None
    )
    if (
        not isinstance(best1_membership, dict)
        or best1_membership.get("schema") != "tbt-monitor.ibic2026-best1-membership/v1"
        or not isinstance(membership_by_plane, dict)
        or set(membership_by_plane) != {"H", "V"}
    ):
        raise ValueError("results payload is missing exact H/V Best-1 membership summaries")
    for plane, expected_maximum_percent in (("H", 3.7), ("V", 5.7)):
        row = membership_by_plane[plane]
        maximum = finite_number(row.get("maximum_winner_fraction")) if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or int(row.get("plane_spill_count") or 0) != 2_000
            or int(row.get("available_source_count") or 0) != 60
            or int(row.get("winning_source_count") or 0) != 60
            or maximum is None
            or round(100.0 * maximum, 1) != expected_maximum_percent
            or not str(row.get("maximum_source_keys") or "")
        ):
            raise ValueError(f"results payload has an invalid {plane} Best-1 membership summary")
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
        ("manifest_count", 2_000),
        ("stream_rows", 239_984),
        ("paired_stream_rows", 0),
        ("incomplete_manifests", 12),
        ("missing_position_stream_rows", 16),
        ("warning_count", 12),
        ("flagged_rows", 0),
        ("position_plateau_rows", 0),
        ("paired_plateau_rows", 0),
        ("raw_device_fallback_pair_rows", 0),
    ):
        if int(payload_integrity.get(field) or 0) != expected:
            raise ValueError(f"publication raw-payload audit mismatch: {field}")
    topology = payload_integrity.get("topology")
    if not isinstance(topology, dict) or set(topology) != set(PAYLOAD_AUDIT_TOPOLOGY_EXPECTED):
        raise ValueError("publication raw-payload audit is missing the two primary topologies")
    for collection, raw in topology.items():
        if not isinstance(raw, dict):
            raise ValueError(f"publication raw-payload topology is invalid: {collection}")
        expected = {
            "unique_position_streams": 120,
            "unique_h_streams": 60,
            "unique_v_streams": 60,
            "unique_digitizers": 30,
            **PAYLOAD_AUDIT_TOPOLOGY_EXPECTED[collection],
        }
        if any(int(raw.get(field) or 0) != value for field, value in expected.items()) or raw.get(
            "bad_digitizers"
        ):
            raise ValueError(f"publication raw-payload topology mismatch: {collection}")
    if payload_integrity.get("manifest_inventory_sha256") != PAYLOAD_AUDIT_MANIFEST_SHA256:
        raise ValueError("publication raw-payload audit has the wrong manifest hash")
    if (
        payload_integrity.get("missing_position_stream_inventory_sha256")
        != PAYLOAD_MISSING_INVENTORY_SHA256
    ):
        raise ValueError("publication raw-payload audit has the wrong absent-stream inventory hash")
    sensitivity = validate_sensitivity_payload(payload.get("sensitivity"))
    sensitivity_recommendations = sensitivity.get("recommendations")
    assert isinstance(sensitivity_recommendations, dict)
    transfers = payload.get("cross_collection_transfer")
    if not isinstance(transfers, list) or len(transfers) != 4 or any(
        not isinstance(row, dict) or row.get("status") != "OK" for row in transfers
    ):
        raise ValueError("publication payload does not contain four OK transfer rows")

    poster_content = read_json(root / "poster" / "content.json")
    poster_evidence = poster_content.get("evidence")
    if (
        not isinstance(poster_evidence, dict)
        or poster_evidence.get("primaryCapture") != primary_capture
        or poster_evidence.get("ridgeCoverage") != ridge_coverage
        or poster_evidence.get("crossSpillNull") != null_selected
        or poster_evidence.get("best1Membership") != membership_by_plane
    ):
        raise ValueError("poster evidence does not match publication capture and validation controls")
    macros_text = (root / "paper" / "results_macros.tex").read_text(encoding="utf-8")
    expected_macros = {
        "PrimarySpillCount": primary_capture["spill_count"],
        "PrimaryNominalHChannels": primary_capture["nominal_h_channels"],
        "PrimaryNominalVChannels": primary_capture["nominal_v_channels"],
        "PrimaryPartialCaptures": primary_capture["partial_capture_count"],
        "PrimarySourceAbsences": primary_capture["source_absence_count"],
        "RidgeHStructuralRows": ridge_coverage["H"]["sliding_rows"],
        "RidgeHFinitePicks": ridge_coverage["H"]["ridge_points"],
        "RidgeHBlankPicks": ridge_coverage["H"]["missing_tune_rows"],
        "RidgeHEdgeExcludedPicks": ridge_coverage["H"]["edge_excluded_rows"],
        "RidgeVStructuralRows": ridge_coverage["V"]["sliding_rows"],
        "RidgeVFinitePicks": ridge_coverage["V"]["ridge_points"],
        "RidgeVBlankPicks": ridge_coverage["V"]["missing_tune_rows"],
        "RidgeVEdgeExcludedPicks": ridge_coverage["V"]["edge_excluded_rows"],
        "BestOneUniqueH": 60,
        "BestOneUniqueV": 60,
    }
    for command, value in expected_macros.items():
        definition = rf"\newcommand{{\{command}}}{{{value}}}"
        if definition not in macros_text:
            raise ValueError(f"paper results macros do not bind {command}")
    for command, value in (
        ("BestOneMaxFrequencyH", 3.7),
        ("BestOneMaxFrequencyV", 5.7),
    ):
        definition = rf"\newcommand{{\{command}}}{{{value:.1f}}}"
        if definition not in macros_text:
            raise ValueError(f"paper results macros do not bind {command}")
    for plane in ("H", "V"):
        row = null_selected[plane]
        for suffix, field in (
            ("Mean", "null_mean_agreement_rate"),
            ("Low", "null_ci_low"),
            ("High", "null_ci_high"),
        ):
            value = finite_number(row.get(field))
            definition = rf"\newcommand{{\BestN{plane}Null{suffix}}}{{{value:.4f}}}"
            if definition not in macros_text:
                raise ValueError(f"paper results macros do not bind BestN{plane}Null{suffix}")
    for plane in ("H", "V"):
        row = adaptive_ridge_rows[plane]
        for suffix, field in (
            ("Delta", "median_iqr_delta_ensemble_minus_baseline"),
            ("Low", "median_iqr_delta_ci_low"),
            ("High", "median_iqr_delta_ci_high"),
        ):
            value = finite_number(row.get(field))
            definition = rf"\newcommand{{\Ridge{plane}Iqr{suffix}Milli}}{{{1000.0 * value:.2f}}}"
            if definition not in macros_text:
                raise ValueError(f"paper results macros do not bind Ridge{plane}Iqr{suffix}Milli")
    reject_stale_results_macros(macros_text)

    for path in (
        root / "PREPARATION_REPORT.md",
        root / "results_payload.json",
        root / "source_manifest.csv",
        root / "poster" / "content.json",
        root / "paper" / "ABSTRACT54.tex",
        root / "paper" / "results_table.tex",
        root / "paper" / "results_macros.tex",
    ):
        text = path.read_text(encoding="utf-8")
        if UNRESOLVED.search(text):
            raise ValueError(f"publication source contains unresolved copy: {path}")
        if re.search(r"intensity", text, re.IGNORECASE):
            raise ValueError(f"publication-facing source retains an intensity reference: {path}")

    report = root / "compliance_report.md"
    lines = [
        "# IBIC 2026 Publication Compliance",
        "",
        "All checks below passed. Visual QA was explicitly completed on the final rendered artifacts.",
        "",
        f"- selected ensembles: H Best-{selected_sizes['H']}, V Best-{selected_sizes['V']}",
        (
            "- same-protocol all-training control, selected/all-training/unresolved: "
            f"H {all_training_by_plane['H']['selected_favored']}/"
            f"{all_training_by_plane['H']['baseline_favored']}/"
            f"{all_training_by_plane['H']['unresolved']}; "
            f"V {all_training_by_plane['V']['selected_favored']}/"
            f"{all_training_by_plane['V']['baseline_favored']}/"
            f"{all_training_by_plane['V']['unresolved']}"
        ),
        (
            "- selected cross-spill null mean (95% interval): "
            f"H {float(null_selected['H']['null_mean_agreement_rate']):.4f} "
            f"[{float(null_selected['H']['null_ci_low']):.4f}, {float(null_selected['H']['null_ci_high']):.4f}]; "
            f"V {float(null_selected['V']['null_mean_agreement_rate']):.4f} "
            f"[{float(null_selected['V']['null_ci_low']):.4f}, {float(null_selected['V']['null_ci_high']):.4f}]"
        ),
        (
            "- Best-1 membership: all 60 H and 60 V sources win at least once; "
            f"maximum winner shares H {100 * float(membership_by_plane['H']['maximum_winner_fraction']):.1f}%, "
            f"V {100 * float(membership_by_plane['V']['maximum_winner_fraction']):.1f}%"
        ),
        "- Best-N design: 4000 full-curve spill-plane cases; 1000 stratified validation cases across 5 digitizer folds",
        "- raw payload audit: 239984 captured position streams through turn 50000; 16 manifest-level absences across 12 recorded partial captures; no blocking payload findings",
        (
            "- primary capture: "
            f"{primary_capture['spill_count']} spills, nominal "
            f"{primary_capture['nominal_h_channels']} H + "
            f"{primary_capture['nominal_v_channels']} V, "
            f"{primary_capture['partial_capture_count']} partial captures, "
            f"{primary_capture['source_absence_count']} source absences"
        ),
        (
            "- selected full-buffer ridge coverage: "
            f"H {ridge_coverage['H']['ridge_points']}/{ridge_coverage['H']['sliding_rows']} finite "
            f"(blank {ridge_coverage['H']['missing_tune_rows']}, edge {ridge_coverage['H']['edge_excluded_rows']}); "
            f"V {ridge_coverage['V']['ridge_points']}/{ridge_coverage['V']['sliding_rows']} finite "
            f"(blank {ridge_coverage['V']['missing_tune_rows']}, edge {ridge_coverage['V']['edge_excluded_rows']})"
        ),
        (
            "- Best-N sensitivity: 7 verified runs; "
            f"H {len(sensitivity_recommendations['H'])}/7 available "
            f"(N={min(sensitivity_recommendations['H'])}-{max(sensitivity_recommendations['H'])}); "
            f"V {len(sensitivity_recommendations['V'])}/7 available "
            f"(N={min(sensitivity_recommendations['V'])}-{max(sensitivity_recommendations['V'])})"
        ),
        "- cross-collection transfer rows: 4 OK",
        f"- poster: {poster_info['pages']} A0 page, {poster_info['width_points']} x {poster_info['height_points']} pt",
        f"- paper: {paper_info['pages']} pages, {paper_info['width_points']} x {paper_info['height_points']} pt",
        f"- poster preview pixels: {poster_preview[0]} x {poster_preview[1]}",
        f"- poster PDF render pixels: {poster_render[0]} x {poster_render[1]}",
        "- poster preview source: byte-identical 150 dpi PDF raster with inherited master artwork",
        "- poster source/deliverable manifests: verified",
        "- beamline-map attribution: George Deinlein, Fermilab staff; full publication reuse permission confirmed",
        f"- poster report number: {POSTER_REPORT_NUMBER}",
        "- current FermiForward/DOE acknowledgment: present in the lower-left footer",
        "- publication materialization manifest: verified",
        f"- prepared poster starter: `{POSTER_STARTER_SHA256}`",
        "- poster template fidelity: pass, 0 issues",
        "- empty structural poster placeholders: 0",
        "- paper source manifest: verified",
        "- paper render pixels: " + ", ".join(f"{width} x {height}" for width, height in paper_renders),
        f"- poster visual QA: {poster_visual_qa}",
        f"- paper visual QA: {paper_visual_qa}",
        "",
        "## Authoritative References",
        "",
        f"- accepted abstract: `external/{abstract.name}` (`{reference_hashes['accepted abstract'][0]}`)",
        f"- Fermilab poster template: `external/{poster_template.name}` (`{reference_hashes['poster template'][0]}`)",
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
