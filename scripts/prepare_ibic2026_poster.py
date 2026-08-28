#!/usr/bin/env python3
"""Materialize the IBIC 2026 poster from a frozen paper evidence gate.

This command is intentionally one-way: it reads the accepted paper, result
payload, scientific figures, and contextual beamline map, then writes only
inside ``publication/ibic2026/poster``.  It never regenerates or edits the
paper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


GATE_SCHEMA = "tbt-monitor.ibic2026-poster-evidence-gate/v3"
MANIFEST_SCHEMA = "tbt-monitor.ibic2026-poster-inputs/v2"
RESULTS_SCHEMA = "tbt-monitor.ibic2026-results/v2"

PAPER_PATHS = {
    "source": "publication/ibic2026/paper/ABSTRACT54.tex",
    "pdf": "publication/ibic2026/paper/build/ABSTRACT54.pdf",
}
RESULTS_PATH = "publication/ibic2026/results_payload.json"
POSTER_RELATIVE = PurePosixPath("publication/ibic2026/poster")
DEFAULT_GATE = POSTER_RELATIVE / "evidence_gate.json"

INPUT_ROLES = ("resultsPayload", "bestNH", "bestNV", "ridgeHV", "beamlineMap")
ASSET_OUTPUTS = {
    "bestNH": "assets/best_n_validation_h.png",
    "bestNV": "assets/best_n_validation_v.png",
    "ridgeHV": "assets/ridge_density_comparison.png",
    "beamlineMap": "assets/muon-campus-beamlines.png",
}

MAP_CREDIT = (
    "Beamline layout courtesy of George Deinlein, Fermilab staff; used with permission."
)
MAP_ATTRIBUTION = {
    "creator": "George Deinlein",
    "affiliation": "Fermilab",
    "role": "staff",
    "creditLine": MAP_CREDIT,
    "permissionStatus": "full",
    "permissionScope": "this poster's publication reuse",
    "confirmedOn": "2026-08-19",
}
REPORT_NUMBER = "FERMILAB-POSTER-26-0268-AD"
ACKNOWLEDGMENT = (
    "This manuscript has been authored by FermiForward Discovery Group, LLC under "
    "Contract No. 89243024CSC000002 with the U.S. Department of Energy, Office of "
    "Science, Office of High Energy Physics."
)
PUBLICATION_REQUIREMENTS = {
    "reportNumber": REPORT_NUMBER,
    "acknowledgment": ACKNOWLEDGMENT,
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
SHA256_RE = re.compile(r"[0-9a-f]{64}")
NON_ASCII_HYPHEN_RE = re.compile(r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PosterPreparationError(ValueError):
    """Raised when the frozen evidence gate cannot be materialized safely."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PosterPreparationError(f"{label} must be a JSON object")
    return value


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PosterPreparationError(f"missing {label}: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PosterPreparationError(f"invalid {label}: {path}: {exc}") from exc
    return _require_object(value, label)


def _safe_repo_path(repo_root: Path, raw_path: object, label: str) -> tuple[str, Path]:
    if not isinstance(raw_path, str) or not raw_path:
        raise PosterPreparationError(f"{label} path must be a nonempty string")
    pure = PurePosixPath(raw_path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise PosterPreparationError(f"{label} path must be normalized and repo-relative: {raw_path}")
    normalized = pure.as_posix()
    candidate = (repo_root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as exc:
        raise PosterPreparationError(f"{label} path escapes the repository: {raw_path}") from exc
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise PosterPreparationError(f"missing or empty {label}: {raw_path}")
    return normalized, candidate


def _validate_spec(
    repo_root: Path,
    value: object,
    label: str,
    expected_path: str | None = None,
) -> dict[str, object]:
    spec = _require_object(value, label)
    if set(spec) != {"path", "sha256"}:
        raise PosterPreparationError(
            f"{label} must contain exactly path and sha256; found {sorted(spec)}"
        )
    path_text, path = _safe_repo_path(repo_root, spec["path"], label)
    if expected_path is not None and path_text != expected_path:
        raise PosterPreparationError(
            f"{label} must point to {expected_path}, not {path_text}"
        )
    expected_hash = spec["sha256"]
    if not isinstance(expected_hash, str) or SHA256_RE.fullmatch(expected_hash) is None:
        raise PosterPreparationError(f"{label} sha256 must be 64 lowercase hex characters")
    actual_hash = sha256(path)
    if actual_hash != expected_hash:
        raise PosterPreparationError(
            f"{label} hash mismatch: expected {expected_hash}, found {actual_hash}"
        )
    return {
        "path": path_text,
        "resolved": path,
        "sha256": actual_hash,
        "sizeBytes": path.stat().st_size,
    }


def _png_dimensions(path: Path, label: str) -> dict[str, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise PosterPreparationError(f"{label} is not a PNG: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width < 500 or height < 300:
        raise PosterPreparationError(
            f"{label} is undersized for the poster: {width}x{height}"
        )
    return {"width": width, "height": height}


def _validate_gate(repo_root: Path, gate: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "schema",
        "paper",
        "inputs",
        "mapAttribution",
        "publicationRequirements",
        "context",
    }
    required = set(allowed)
    if not required.issubset(gate) or not set(gate).issubset(allowed):
        raise PosterPreparationError(
            "evidence gate must contain schema, paper, inputs, mapAttribution, "
            f"publicationRequirements, and context; found {sorted(gate)}"
        )
    if gate["schema"] != GATE_SCHEMA:
        raise PosterPreparationError(
            f"unsupported evidence gate schema: {gate['schema']!r}; expected {GATE_SCHEMA}"
        )
    if gate.get("mapAttribution") != MAP_ATTRIBUTION:
        raise PosterPreparationError(
            "evidence gate does not record the required beamline-map attribution and permission"
        )
    if gate.get("publicationRequirements") != PUBLICATION_REQUIREMENTS:
        raise PosterPreparationError(
            "evidence gate does not record the required poster number, acknowledgment, "
            "and template placement"
        )
    context = gate.get("context", {})
    if not isinstance(context, dict):
        raise PosterPreparationError("evidence gate context must be an object")

    paper = _require_object(gate["paper"], "evidence gate paper")
    if set(paper) != set(PAPER_PATHS):
        raise PosterPreparationError(
            f"evidence gate paper roles must be {sorted(PAPER_PATHS)}; found {sorted(paper)}"
        )
    verified_paper = {
        role: _validate_spec(
            repo_root,
            paper[role],
            f"paper {role}",
            expected_path=PAPER_PATHS[role],
        )
        for role in PAPER_PATHS
    }
    with verified_paper["pdf"]["resolved"].open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise PosterPreparationError("frozen paper PDF does not have a PDF header")

    inputs = _require_object(gate["inputs"], "evidence gate inputs")
    if set(inputs) != set(INPUT_ROLES):
        raise PosterPreparationError(
            f"evidence gate input roles must be {sorted(INPUT_ROLES)}; found {sorted(inputs)}"
        )
    verified_inputs = {
        role: _validate_spec(
            repo_root,
            inputs[role],
            f"poster input {role}",
            expected_path=RESULTS_PATH if role == "resultsPayload" else None,
        )
        for role in INPUT_ROLES
    }
    asset_paths = [verified_inputs[role]["path"] for role in ASSET_OUTPUTS]
    if len(set(asset_paths)) != len(asset_paths):
        raise PosterPreparationError("poster figure and beamline input paths must be distinct")
    for role in ASSET_OUTPUTS:
        verified_inputs[role]["dimensions"] = _png_dimensions(
            verified_inputs[role]["resolved"], f"poster input {role}"
        )
    return {
        "paper": verified_paper,
        "inputs": verified_inputs,
        "mapAttribution": gate["mapAttribution"],
        "publicationRequirements": gate["publicationRequirements"],
        "context": context,
    }


def _plane_object(value: object, plane: str, label: str) -> dict[str, object]:
    container = _require_object(value, label)
    if plane not in container:
        raise PosterPreparationError(f"{label} is missing plane {plane}")
    return _require_object(container[plane], f"{label}.{plane}")


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PosterPreparationError(f"{label} must be numeric") from exc
    if not (-float("inf") < number < float("inf")):
        raise PosterPreparationError(f"{label} must be finite")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise PosterPreparationError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PosterPreparationError(f"{label} must be an integer") from exc
    if number <= 0 or str(number) != str(value):
        raise PosterPreparationError(f"{label} must be a positive canonical integer")
    return number


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise PosterPreparationError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PosterPreparationError(f"{label} must be an integer") from exc
    if number < 0 or str(number) != str(value):
        raise PosterPreparationError(f"{label} must be a nonnegative canonical integer")
    return number


def _format_percent(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def _assert_ascii_hyphens(value: object, label: str = "content") -> None:
    if isinstance(value, str):
        if NON_ASCII_HYPHEN_RE.search(value):
            raise PosterPreparationError(f"{label} contains a non-ASCII hyphen")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _assert_ascii_hyphens(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_ascii_hyphens(child, f"{label}[{index}]")


def render_content(payload: Mapping[str, object]) -> dict[str, object]:
    if payload.get("schema") != RESULTS_SCHEMA:
        raise PosterPreparationError(
            f"unsupported results payload schema: {payload.get('schema')!r}; expected {RESULTS_SCHEMA}"
        )

    selected_sizes = _require_object(payload.get("selected_sizes"), "selected_sizes")
    h_size = _positive_int(selected_sizes.get("H"), "selected_sizes.H")
    v_size = _positive_int(selected_sizes.get("V"), "selected_sizes.V")
    if (h_size, v_size) != (5, 12):
        raise PosterPreparationError(
            f"frozen poster operating points must be H Best-5 and V Best-12, not {h_size}/{v_size}"
        )

    cross_spill = _require_object(payload.get("cross_spill_null"), "cross_spill_null")
    selected_null = _require_object(cross_spill.get("selected"), "cross_spill_null.selected")
    h_null = _plane_object(selected_null, "H", "cross_spill_null.selected")
    v_null = _plane_object(selected_null, "V", "cross_spill_null.selected")
    for plane, row, size in (("H", h_null, h_size), ("V", v_null, v_size)):
        if row.get("status") != "ok" or _positive_int(
            row.get("subset_size"), f"cross_spill_null.selected.{plane}.subset_size"
        ) != size:
            raise PosterPreparationError(
                f"selected cross-spill null for {plane} does not match Best-{size}"
            )

    h_observed = _finite_float(h_null.get("observed_agreement_rate"), "H observed agreement")
    h_null_high = _finite_float(h_null.get("null_ci_high"), "H null upper bound")
    v_observed = _finite_float(v_null.get("observed_agreement_rate"), "V observed agreement")
    v_null_high = _finite_float(v_null.get("null_ci_high"), "V null upper bound")
    if not h_observed > h_null_high:
        raise PosterPreparationError("H observed agreement is not above its null upper bound")
    if not v_observed > v_null_high or not (v_observed - v_null_high) > (
        h_observed - h_null_high
    ):
        raise PosterPreparationError("V agreement is not more clearly separated from null than H")
    rendered_rates = tuple(
        _format_percent(value)
        for value in (h_observed, h_null_high, v_observed, v_null_high)
    )
    if rendered_rates != ("9.1%", "8.9%", "26.3%", "18.3%"):
        raise PosterPreparationError(
            "frozen agreement/null values no longer match the accepted poster claims"
        )

    best1 = _require_object(payload.get("best1_membership"), "best1_membership")
    best1_by_plane = _require_object(best1.get("by_plane"), "best1_membership.by_plane")
    h_membership = _plane_object(best1_by_plane, "H", "best1_membership.by_plane")
    v_membership = _plane_object(best1_by_plane, "V", "best1_membership.by_plane")
    h_winners = _positive_int(
        h_membership.get("winning_source_count"),
        "best1_membership.by_plane.H.winning_source_count",
    )
    v_winners = _positive_int(
        v_membership.get("winning_source_count"),
        "best1_membership.by_plane.V.winning_source_count",
    )
    if h_winners != _positive_int(
        h_membership.get("available_source_count"),
        "best1_membership.by_plane.H.available_source_count",
    ) or v_winners != _positive_int(
        v_membership.get("available_source_count"),
        "best1_membership.by_plane.V.available_source_count",
    ):
        raise PosterPreparationError("not every available H/V source wins Best-1 at least once")
    if (h_winners, v_winners) != (60, 60):
        raise PosterPreparationError(
            f"frozen Best-1 membership must cover 60 H and 60 V sources, not {h_winners}/{v_winners}"
        )

    adaptive_ridge = _require_object(payload.get("adaptive_ridge_rows"), "adaptive_ridge_rows")
    h_ridge = _plane_object(adaptive_ridge, "H", "adaptive_ridge_rows")
    h_iqr_delta = _finite_float(
        h_ridge.get("median_iqr_delta_ensemble_minus_baseline"),
        "adaptive_ridge_rows.H.median_iqr_delta_ensemble_minus_baseline",
    )
    if h_iqr_delta >= 0:
        raise PosterPreparationError("H selected ridge is not narrower than corrected Best-1")

    sensitivity = _require_object(payload.get("sensitivity"), "sensitivity")
    sensitivity_ranges = _require_object(sensitivity.get("ranges"), "sensitivity.ranges")
    for plane in ("H", "V"):
        plane_range = _plane_object(sensitivity_ranges, plane, "sensitivity.ranges")
        minimum = _positive_int(plane_range.get("minimum"), f"sensitivity.ranges.{plane}.minimum")
        maximum = _positive_int(plane_range.get("maximum"), f"sensitivity.ranges.{plane}.maximum")
        if minimum >= maximum:
            raise PosterPreparationError(
                f"{plane} sensitivity does not support the non-unique operating-point claim"
            )

    all_training = _require_object(
        payload.get("all_training_control"), "all_training_control"
    )
    all_training_by_plane = _require_object(
        all_training.get("by_plane"), "all_training_control.by_plane"
    )
    for plane in ("H", "V"):
        comparison = _plane_object(
            all_training_by_plane, plane, "all_training_control.by_plane"
        )
        selected_favored = _nonnegative_int(
            comparison.get("selected_favored"),
            f"all_training_control.by_plane.{plane}.selected_favored",
        )
        baseline_favored = _nonnegative_int(
            comparison.get("baseline_favored"),
            f"all_training_control.by_plane.{plane}.baseline_favored",
        )
        if selected_favored < 1 or baseline_favored < 1:
            raise PosterPreparationError(
                f"{plane} all-training results do not support the competitive-baseline claim"
            )

    primary_capture = _require_object(payload.get("primary_capture"), "primary_capture")
    ridge_coverage = _require_object(payload.get("ridge_coverage"), "ridge_coverage")
    _plane_object(ridge_coverage, "H", "ridge_coverage")
    _plane_object(ridge_coverage, "V", "ridge_coverage")

    content: dict[str, object] = {
        "title": "Which BPMs can we trust, spill by spill?",
        "subtitle": "Adaptive turn-by-turn tune analysis in the Mu2e Delivery Ring",
        "author": "Derek Steinkamp | Fermi National Accelerator Laboratory",
        "reportNumber": REPORT_NUMBER,
        "acknowledgment": ACKNOWLEDGMENT,
        "mapCaption": (
            "The loop at right is the Delivery Ring. Its position readouts do not behave equally."
        ),
        "mapCredit": MAP_CREDIT,
        "methodHeading": "THERE IS NO SINGLE BEST BPM",
        "methodBody": (
            f"All {h_winners} H and {v_winners} V sources win at least once. "
            "Choose early; test later with held-out digitizers."
        ),
        "bestNHCaption": (
            f"H: Best-{h_size} reached {_format_percent(h_observed)}. The null band ends at "
            f"{_format_percent(h_null_high)} - promising, not decisive."
        ),
        "bestNVCaption": (
            f"V: Best-{v_size} reached {_format_percent(v_observed)}. The null band ends at "
            f"{_format_percent(v_null_high)} - a clear separation."
        ),
        "ridgeHeading": "DOES THE CANDIDATE PERSIST FOR 50,000 TURNS?",
        "conclusionHeading": "WHAT DID WE LEARN?",
        "conclusionBody": (
            f"H Best-{h_size} narrows the ridge. V Best-{v_size} agrees more strongly. "
            "All-channel aggregation remains competitive.\n\n"
            "Useful operating points - not universal optima.\n\n"
            "Is this the machine tune? Not yet. Next: change the tune on purpose "
            "(a controlled quadrupole scan) and ask whether the candidate follows."
        ),
        "assets": dict(ASSET_OUTPUTS),
        "evidence": {
            "best1Membership": {"H": h_membership, "V": v_membership},
            "crossSpillNull": {"H": h_null, "V": v_null},
            "primaryCapture": primary_capture,
            "ridgeCoverage": ridge_coverage,
        },
    }
    _assert_ascii_hyphens(content)
    return content


def _manifest_entry(record: Mapping[str, object]) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": record["path"],
        "sha256": record["sha256"],
        "sizeBytes": record["sizeBytes"],
    }
    if "dimensions" in record:
        entry["dimensions"] = record["dimensions"]
    return entry


def _ensure_output_path(poster_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise PosterPreparationError(f"unsafe poster output path: {relative}")
    destination = poster_root / Path(*pure.parts)
    resolved = destination.resolve()
    try:
        resolved.relative_to(poster_root.resolve())
    except ValueError as exc:
        raise PosterPreparationError(f"poster output path escapes poster root: {relative}") from exc
    return destination


def _assert_inputs_unchanged(records: Mapping[str, Mapping[str, object]]) -> None:
    for label, record in records.items():
        current = sha256(record["resolved"])
        if current != record["sha256"]:
            raise PosterPreparationError(
                f"frozen input changed during poster materialization: {label}"
            )


def prepare_poster(repo_root: Path, gate_path: Path | None = None) -> dict[str, object]:
    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        raise PosterPreparationError(f"repository root does not exist: {repo_root}")
    poster_root = repo_root / Path(*POSTER_RELATIVE.parts)
    poster_root.mkdir(parents=True, exist_ok=True)
    try:
        poster_root.resolve().relative_to(repo_root)
    except ValueError as exc:
        raise PosterPreparationError("poster output root escapes the repository") from exc

    raw_gate_path = gate_path or Path(*DEFAULT_GATE.parts)
    if raw_gate_path.is_absolute():
        try:
            gate_file = raw_gate_path.resolve()
            gate_relative = gate_file.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise PosterPreparationError("evidence gate must be inside the repository") from exc
    else:
        gate_relative, gate_file = _safe_repo_path(repo_root, raw_gate_path.as_posix(), "evidence gate")
    if not gate_file.is_file():
        raise PosterPreparationError(f"missing evidence gate: {gate_file}")
    gate_hash = sha256(gate_file)
    gate_size = gate_file.stat().st_size
    gate = _read_json(gate_file, "evidence gate")
    verified = _validate_gate(repo_root, gate)

    paper_records = verified["paper"]
    input_records = verified["inputs"]
    payload = _read_json(input_records["resultsPayload"]["resolved"], "results payload")
    content = render_content(payload)

    all_records: dict[str, Mapping[str, object]] = {
        **{f"paper.{key}": value for key, value in paper_records.items()},
        **{f"inputs.{key}": value for key, value in input_records.items()},
    }
    paper_before = {role: record["sha256"] for role, record in paper_records.items()}

    with tempfile.TemporaryDirectory(prefix=".prepare-poster-", dir=poster_root) as scratch:
        stage = Path(scratch)
        staged_outputs: dict[str, Path] = {}
        for role, relative in ASSET_OUTPUTS.items():
            staged = stage / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(input_records[role]["resolved"], staged)
            staged_outputs[relative] = staged

        content_stage = stage / "content.json"
        content_stage.write_text(
            json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staged_outputs["content.json"] = content_stage

        if sha256(gate_file) != gate_hash:
            raise PosterPreparationError("evidence gate changed during poster materialization")
        _assert_inputs_unchanged(all_records)
        paper_after = {role: sha256(record["resolved"]) for role, record in paper_records.items()}
        if paper_after != paper_before:
            raise PosterPreparationError("frozen paper changed during poster materialization")

        outputs: dict[str, object] = {
            "content": {
                "path": (POSTER_RELATIVE / "content.json").as_posix(),
                "sha256": sha256(content_stage),
                "sizeBytes": content_stage.stat().st_size,
            },
            "assets": {},
        }
        for role, relative in ASSET_OUTPUTS.items():
            staged = staged_outputs[relative]
            outputs["assets"][role] = {
                "path": (POSTER_RELATIVE / relative).as_posix(),
                "sha256": sha256(staged),
                "sizeBytes": staged.stat().st_size,
                "dimensions": input_records[role]["dimensions"],
            }

        manifest: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "evidenceGate": {
                "path": gate_relative,
                "sha256": gate_hash,
                "sizeBytes": gate_size,
            },
            "paperImmutability": {
                role: {
                    "path": paper_records[role]["path"],
                    "sha256Before": paper_before[role],
                    "sha256After": paper_after[role],
                    "unchanged": paper_before[role] == paper_after[role],
                }
                for role in PAPER_PATHS
            },
            "inputs": {
                role: _manifest_entry(input_records[role]) for role in INPUT_ROLES
            },
            "mapAttribution": verified["mapAttribution"],
            "publicationRequirements": verified["publicationRequirements"],
            "context": verified["context"],
            "outputs": outputs,
        }
        manifest_stage = stage / "input_manifest.json"
        manifest_stage.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        for relative, staged in staged_outputs.items():
            destination = _ensure_output_path(poster_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
        if sha256(gate_file) != gate_hash:
            raise PosterPreparationError("evidence gate changed while committing poster outputs")
        _assert_inputs_unchanged(all_records)
        paper_after_commit = {
            role: sha256(record["resolved"]) for role, record in paper_records.items()
        }
        if paper_after_commit != paper_before:
            raise PosterPreparationError("frozen paper changed while committing poster outputs")
        manifest["paperImmutability"] = {
            role: {
                "path": paper_records[role]["path"],
                "sha256Before": paper_before[role],
                "sha256After": paper_after_commit[role],
                "unchanged": paper_before[role] == paper_after_commit[role],
            }
            for role in PAPER_PATHS
        }
        manifest_stage.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_destination = _ensure_output_path(poster_root, "input_manifest.json")
        os.replace(manifest_stage, manifest_destination)

    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    parser.add_argument(
        "--gate",
        type=Path,
        help="repo-relative evidence gate path (defaults to publication/ibic2026/poster/evidence_gate.json)",
    )
    args = parser.parse_args(argv)
    try:
        manifest = prepare_poster(args.repo_root, args.gate)
    except PosterPreparationError as exc:
        parser.error(str(exc))
    print(json.dumps({"posterInputs": manifest["inputs"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
