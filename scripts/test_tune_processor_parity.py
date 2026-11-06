#!/usr/bin/env python3
"""Qualify the Rust TUNE CLI against the publication's Python implementations.

The reference imports the actual spectral-cache, Best-N, later-support, and
ridge routines. It never reads the Rust implementation or derives expectations
from Rust output. Synthetic raw waveforms are generated in a temporary directory;
the repository contains only compact numerical golden summaries, never captures.

    cargo build --locked --bin tbt-processor
    python3 scripts/test_tune_processor_parity.py
    python3 scripts/test_tune_processor_parity.py \
        --publication-bundle /authorized/capture/spill_1780957889653 \
        --require-publication-fixture

Use --write-synthetic-goldens only after reviewing an intentional change to the
Python reference or synthetic cases. This operation never invokes Rust. Frozen
publication evidence is reported separately from synthetic numerical parity.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

import numpy as np

import gpu_analyze_captured_spills as ridge
from bpm_mining import best_n
from bpm_mining.spectra import compute_spectra
from bpm_mining.subset_search import score_combos


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "tune"
ORACLE_FILES = (
    "scripts/bpm_mining/spectra.py",
    "scripts/bpm_mining/preprocessing.py",
    "scripts/bpm_mining/best_n.py",
    "scripts/bpm_mining/subset_search.py",
    "scripts/bpm_mining/subset_score.py",
    "scripts/gpu_analyze_captured_spills.py",
)
SCORE_FIELDS = (
    "subset_score", "holdout_support", "peak_quality", "consensus_agreement",
    "window_stability", "diversity_score", "ambiguity_penalty", "visible_fraction",
    "visibility_duration_turns",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paper_spec() -> dict:
    """An independently frozen paper profile; compare to CLI defaults as well."""
    selection = dict(window_turns=4096, step_turns=256, padding_factor=2,
                     mean_subtract=True, window="hann", normalization="none",
                     normalization_floor=1e-12, cache_precision="float16")
    trajectory = dict(selection, padding_factor=1, normalization="rms", cache_precision="float32")
    return dict(
        schema_version=1, profile="paper-best-n-v1", analysis_start_turn=0,
        analysis_end_turn=50000, selection_end_turn=15000, turn_period_us=1.69,
        planes={
            "H": dict(enabled=True, mode="best_n", n=5, members=[],
                      selection_band=[0.60, 0.70], trajectory_band=[0.620, 0.680]),
            "V": dict(enabled=True, mode="best_n", n=12, members=[],
                      selection_band=[0.67, 0.75], trajectory_band=[0.690, 0.740]),
        }, selection=selection, trajectory=trajectory,
        search=dict(fit_windows=8, beam_width=64, tune_tolerance_bins=4.0,
                    tune_tolerance_min=0.002, later_support_half_width=0.0025,
                    score=dict(prominence_scale=12.0, visibility_threshold=4.0,
                               stability_scale=0.006, consensus_neutral=0.5,
                               digitizer_weight=0.65, ring_span_weight=0.35,
                               ring_order_scale=1000.0, ambiguity_ratio_start=0.6,
                               ambiguity_weight=0.5, ambiguity_cap=0.25,
                               local_peak_weights=[0.75, 0.25], peak_quality_weights=[0.7, 0.3],
                               window_weights=[0.35, 0.20, 0.15, 0.10, 0.10, 0.10],
                               aggregate_weights=[0.60, 0.25, 0.15], log_power_floor=1e-24,
                               mad_scale=1.4826, mad_floor=1e-9)),
        tracking=dict(enabled=True, injection_window_turns=4096, half_width=0.005, max_step=0.005),
        quality=dict(min_peak_confidence=2.0, require_intensity=True, min_beam_fraction=0.95,
                     beam_threshold_override=None, suppress_synthetic=True, stale_after_ms=30000),
    )


@dataclass(frozen=True)
class Case:
    name: str
    turns: int
    h_channels: int
    v_channels: int
    window: int = 4096
    step: int = 256
    h_n: int = 5
    v_n: int = 12
    seed: int = 20261106
    precision: str = "float16"
    start: int = 0
    end: int | None = 50000
    nonmodal: bool = False
    static: bool = False

    def spec(self) -> dict:
        spec = paper_spec()
        spec["quality"]["suppress_synthetic"] = False
        spec["analysis_start_turn"] = self.start
        spec["analysis_end_turn"] = self.end
        for stage in ("selection", "trajectory"):
            spec[stage]["window_turns"] = self.window
            spec[stage]["step_turns"] = self.step
        spec["selection"]["cache_precision"] = self.precision
        spec["tracking"]["injection_window_turns"] = self.window
        for plane, n in (("H", self.h_n), ("V", self.v_n)):
            spec["planes"][plane]["n"] = n
            if self.static:
                spec["planes"][plane]["mode"] = "static"
                spec["planes"][plane]["members"] = [bpm_id(plane, i) for i in range(n)]
        return spec


CASES = (
    Case("paper_profile_h5_v12", 11037, 8, 15),
    Case("full_inventory_120_channels", 2309, 60, 60, window=256, step=64, seed=781),
    Case("float32_cache", 2309, 8, 9, window=512, step=128, h_n=3, v_n=4,
         precision="float32", seed=912),
    Case("short_fit_and_modal_lengths", 1603, 8, 9, window=512, step=128,
         h_n=3, v_n=4, seed=671, nonmodal=True),
    Case("crop_before_rms_static", 50317, 4, 5, h_n=3, v_n=4, seed=512,
         start=113, end=50000, static=True),
)


def bpm_id(plane: str, index: int) -> str:
    # Six sectors, ten monitors per plane. These labels are synthetic identities.
    return f"{'HP' if plane == 'H' else 'VP'}{100 + (index // 10) * 100 + 2 * (index % 10) + (1 if plane == 'H' else 2):03d}"


def synthetic_input(case: Case) -> tuple[dict, dict[str, list[dict]]]:
    rng = np.random.default_rng(case.seed)
    eligible: dict[str, list[dict]] = {"H": [], "V": []}
    traces = []
    turns = np.arange(case.turns, dtype=np.float64)
    for plane, count, q in (("H", case.h_channels, 0.6481), ("V", case.v_channels, 0.7173)):
        # The chirp, independent noise, varying offsets, and sideband mixture
        # exercise selection and full-trace RMS weighting without exact ties.
        phase = 2 * np.pi * ((1 - q) * turns + 0.0004 * turns * turns / case.turns)
        for index in range(count):
            amplitude = 0.37 + 0.061 * ((index * 7 + 3) % 13)
            noise = 0.12 + 0.013 * ((index * 11 + 1) % 17)
            sideband = 0.045 + 0.017 * (index % 6)
            samples = (amplitude * np.sin(phase + index * 0.37)
                       + sideband * np.cos(2 * np.pi * (1 - q + 0.009 + index * 0.00013) * turns)
                       + rng.normal(0, noise, case.turns)
                       + 0.06 * ((index * 5) % 11)).astype(np.float32)
            if case.static:
                # Large samples outside the declared crop expose an incorrect
                # pre-crop RMS normalization, without entering any FFT window.
                samples[:case.start] += np.float32(12 + index)
                samples[case.end:] += np.float32(30 + 3 * index)
                # These final cropped samples are beyond the last complete
                # FFT window but still contribute to the paper's RMS scale.
                samples[case.end - 17:case.end] += np.float32(20 * (index + 1))
            ident = bpm_id(plane, index)
            trace = dict(id=ident, plane=plane, digitizer=f"synthetic-{index // 2:02d}",
                         ring_order=float(ident[2:]), source_key=f"synthetic:{ident}:position",
                         source_stream_id="1793970000000-0", intensity_stream_id="1793970000000-0",
                         beam_threshold=0.5, samples=samples.tolist(),
                         intensity=np.full(case.turns, 2.0, dtype=np.float32).tolist())
            traces.append(trace)
            eligible[plane].append(trace)
        if case.nonmodal:
            # Explicitly known nonmodal channels must not shorten all 17 others.
            wrong = copy.deepcopy(eligible[plane][-1])
            wrong["id"] = "HP901" if plane == "H" else "VP902"
            wrong["source_key"] = f"synthetic:{wrong['id']}:position"
            wrong["samples"] = wrong["samples"][:-197]
            wrong["intensity"] = wrong["intensity"][:-197]
            traces.append(wrong)
    return dict(schema_version=1, acquisition_id=f"synthetic-parity:{case.name}",
                source_time_ms=1793970000000, captured_at_ms=1793970000000,
                synthetic=True, traces=traces, warnings=[]), eligible


def oracle_plane(case: Case, plane: str, traces: list[dict], *, require_unique: bool = True) -> dict:
    spec = case.spec()
    pspec = spec["planes"][plane]
    raw = np.asarray([trace["samples"] for trace in traces], dtype=np.float32)
    raw = raw[:, case.start:case.end]
    selection_end = min(raw.shape[1], spec["selection_end_turn"] - case.start)
    spectra, axis, centers = compute_spectra(
        raw[:, :selection_end], dict(name="parity", window_turns=case.window, stride_turns=case.step),
        tuple(pspec["selection_band"]), {"spectra": {"nfft_padding_factor": 2}}, "cpu")
    # Quantization is the publication-cache boundary. Float32 is tested separately.
    spectra = spectra.astype(np.float16 if case.precision == "float16" else np.float32).astype(np.float32)
    fit_count, test_start = best_n.purged_window_split(centers, 8, case.window)
    fit = spectra[:, :fit_count]
    candidates = best_n.training_candidates(fit, axis)
    indices = np.arange(len(traces), dtype=np.int32)
    metadata = {i: dict(digitizer=trace["digitizer"], ring_order=str(trace["ring_order"]))
                for i, trace in enumerate(traces)}
    margins = []

    def record_score_margin(*args, **kwargs):
        scores = score_combos(*args, **kwargs)
        if len(scores) > 1:
            margins.append(scores[0].subset_score - scores[1].subset_score)
        return scores

    if case.static:
        positions = tuple(range(pspec["n"]))
        score = score_combos(fit, axis, centers[:fit_count], indices, candidates,
                            np.asarray([positions], dtype=np.int16), metadata, None,
                            case.window, 128, "cpu")[0]
    else:
        with patch("bpm_mining.best_n.score_combos", side_effect=record_score_margin):
            curve = best_n.beam_search_curve(fit, axis, centers[:fit_count], indices,
                                            candidates, metadata, None, case.window,
                                            pspec["n"], 64, 128, "cpu")
        score = curve[-1][0]
        positions = score.subset
        if require_unique and (not margins or margins[-1] <= 1e-7):
            raise AssertionError(f"{case.name}/{plane}: synthetic winner is not distinctly unique")
    later = best_n.aggregate_metrics(spectra[list(positions), test_start:], axis, score.q_hat, 0.0025)
    selected = ridge.preprocess_traces(raw[list(positions)], "rms_per_bpm", case.window)
    args = ridge.build_parser().parse_args([])
    args.flashes = None
    args.injection_window_turns = case.window
    args.sliding_window_turns = case.window
    args.sliding_stride_turns = case.step
    args.ridge_anchor_enabled = False
    backend = ridge.FftBackend("cpu")
    result = ridge.analyze_plane(
        ridge.SpillBundle(Path("."), "synthetic-parity", 1793970000000, len(positions), [], []),
        plane, selected, [traces[i]["id"] for i in positions], selected.shape[1], args,
        backend, tuple(pspec["trajectory_band"]), {"fft_seconds": 0.0, "windows": 0.0})
    full_spectra = result.spectra_by_method["hann"]
    # The live result retains pre-confidence diagnostic candidates. The paper
    # returns only accepted picks, so recover the diagnostics through its own
    # picker while advancing trust using the already-computed paper decisions.
    trusted = result.injection_peak.tune if result.injection_peak else None
    for power, point in zip(full_spectra, result.sliding_points):
        raw_peak = ridge.pick_peak_in_band(power, tuple(pspec["trajectory_band"]), 0.0)
        local_band = ridge.local_tracking_band(tuple(pspec["trajectory_band"]), trusted, 0.005)
        local_peak = ridge.pick_peak_in_band(power, local_band, 0.0) if local_band else None
        candidate = local_peak if point["tracked_local_tune"] is not None else raw_peak
        point["raw_candidate_q"] = raw_peak.tune if raw_peak else None
        point["tracked_candidate_q"] = local_peak.tune if local_peak else None
        point["candidate_q"] = candidate.tune if candidate else None
        point["candidate_confidence"] = candidate.confidence if candidate else None
        if point["tracked_local_tune"] is not None and not point["suspicious_step"]:
            trusted = point["tracked_local_tune"]
    start_bin, end_bin = ridge.band_indices(case.window, tuple(pspec["trajectory_band"]))
    return dict(
        selected_members=[traces[i]["id"] for i in positions],
        available_n=len(traces), used_turns=raw.shape[1], fit_windows=fit_count,
        fit_end_turn=int(centers[fit_count - 1] + case.window / 2) + case.start,
        test_start_turn=(int(centers[test_start] - case.window / 2) + case.start
                         if test_start < len(centers) else None),
        selection_q=score.q_hat, score={field: getattr(score, field) for field in SCORE_FIELDS},
        later_support=dict(window_count=len(centers) - test_start, **later),
        winner_margin=margins[-1] if margins else None,
        spectrum_tune=(np.arange(start_bin, end_bin) / case.window).tolist(),
        spectrum_power=full_spectra[:, start_bin:end_bin],
        windows=result.sliding_points,
    )


def near(label: str, got, expected, *, rtol: float = 3e-4, atol: float = 2e-5) -> None:
    if expected is None or not math.isfinite(float(expected)):
        if got is not None:
            raise AssertionError(f"{label}: expected null, got {got}")
        return
    if got is None or not math.isclose(float(got), float(expected), rel_tol=rtol, abs_tol=atol):
        raise AssertionError(f"{label}: got {got!r}, expected {expected!r} (rtol={rtol}, atol={atol})")


def compare_plane(label: str, got: dict, expected: dict, case: Case, *, beam_fraction: float | None = 1.0) -> None:
    for field in ("selected_members", "available_n", "used_turns", "fit_windows", "fit_end_turn", "test_start_turn"):
        if got[field] != expected[field]:
            raise AssertionError(f"{label}.{field}: {got[field]!r} != {expected[field]!r}")
    near(f"{label}.candidate_q", got["candidate_q"], expected["selection_q"], rtol=0, atol=1e-10)
    if got.get("score") is None:
        raise AssertionError(f"{label}: missing ensemble score")
    for field in SCORE_FIELDS:
        near(f"{label}.score.{field}", got["score"][field], expected["score"][field])
    for field, value in expected["later_support"].items():
        near(f"{label}.later_support.{field}", got["later_support"][field], value)
    np.testing.assert_allclose(got["spectrum_tune"], expected["spectrum_tune"], rtol=0, atol=1e-12,
                               err_msg=f"{label}.spectrum_tune")
    if len(got["windows"]) != len(expected["windows"]):
        raise AssertionError(f"{label}: trajectory window count mismatch")
    for index, (actual, reference) in enumerate(zip(got["windows"], expected["windows"])):
        prefix = f"{label}.windows[{index}]"
        center = reference["center_turn"] + case.start
        assert actual["center_turn"] == center, prefix
        assert actual["start_turn"] == center - case.window // 2, prefix
        assert actual["end_turn_exclusive"] == center + case.window // 2, prefix
        for target, source in (("raw_q", "raw_candidate_q"), ("tracked_q", "tracked_candidate_q"),
                               ("candidate_q", "candidate_q")):
            near(f"{prefix}.{target}", actual[target], reference[source], rtol=0, atol=2e-8)
        near(f"{prefix}.confidence", actual["confidence"], reference["candidate_confidence"], rtol=5e-4)
        # Live diagnostics retain candidates even below confidence. On those
        # suppressed rows the flags describe that retained candidate; the paper
        # has no accepted peak on which to evaluate a step or fallback.
        if reference["selected_tune"] is not None:
            assert actual["suspicious_step"] == reference["suspicious_step"], prefix
            assert actual["used_raw_fallback"] == reference["used_global_fallback"], prefix
        near(f"{prefix}.q", actual["q"], None if reference["suspicious_step"] else reference["selected_tune"],
             rtol=0, atol=2e-8)
        near(f"{prefix}.beam_fraction", actual["beam_fraction"], beam_fraction, rtol=0, atol=0)
        np.testing.assert_allclose(actual["spectrum_power"], expected["spectrum_power"][index],
                                   rtol=5e-4, atol=2e-4, err_msg=f"{prefix}.spectrum_power")


def compact_reference(expected: dict) -> dict:
    summary = {key: value for key, value in expected.items()
               if key not in ("windows", "spectrum_power", "spectrum_tune")}
    windows = expected["windows"]
    summary["window_count"] = len(windows)
    summary["sample_windows"] = {str(index): windows[index] for index in sorted({0, len(windows) // 2, len(windows) - 1})}
    summary["spectrum_bin_count"] = len(expected["spectrum_tune"])
    return summary


def clean_json(value):
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def compare_golden(label: str, got, expected) -> None:
    if isinstance(expected, dict):
        assert set(got) == set(expected), f"{label}: golden fields changed"
        for key, value in expected.items():
            compare_golden(f"{label}.{key}", got[key], value)
    elif isinstance(expected, list):
        assert len(got) == len(expected), label
        for i, (actual, value) in enumerate(zip(got, expected)):
            compare_golden(f"{label}[{i}]", actual, value)
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        near(label, got, expected, rtol=2e-6, atol=2e-8)
    else:
        assert got == expected, f"{label}: {got!r} != {expected!r}"


def run_processor(binary: Path, case: Case, payload: dict, spec: dict, folder: Path) -> dict:
    input_path, spec_path, output_path = (folder / f"{case.name}-{kind}.json" for kind in ("input", "spec", "output"))
    input_path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    spec_path.write_text(json.dumps(spec, allow_nan=False), encoding="utf-8")
    completed = subprocess.run([str(binary), "analyze", "--input", str(input_path), "--spec", str(spec_path),
                                "--output", str(output_path)], cwd=ROOT, text=True, capture_output=True, timeout=180)
    if completed.returncode:
        raise AssertionError(f"{case.name}: processor failed:\n{completed.stderr}\n{completed.stdout}")
    return json.loads(output_path.read_text(encoding="utf-8"))


def static_missing_check(binary: Path, folder: Path) -> None:
    case = Case("static_missing_member", 1800, 3, 4, window=512, step=128, h_n=2, v_n=2, static=True)
    payload, _ = synthetic_input(case)
    spec = case.spec()
    spec["planes"]["H"]["members"] = [bpm_id("H", 0), "HP999"]
    result = run_processor(binary, case, payload, spec, folder)
    plane = result["planes"]["H"]
    assert plane["state"] != "valid", "missing static member produced a valid ensemble"
    assert not plane["selected_members"], "missing static member was silently substituted"
    assert plane["q"] is None and not plane["windows"], "incomplete static set produced tune windows"


def publication_fixture_check(fixture_path: Path, bundle_path: Path | None) -> dict:
    """Bind the nonraw excerpt to published hashes and, when present, raw bytes."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1 and fixture["fixture_type"] == "accepted-publication"
    with (ROOT / "publication/ibic2026/source_manifest.csv").open(newline="") as handle:
        manifest = {row["role"]: row for row in csv.DictReader(handle)}
    for role, key in (("analysis:best_n_contract", "best_n_contract"),
                      ("analysis:ridge_contract", "ridge_contract"),
                      ("analysis:ridge_selected_h_sliding_tune", "ridge_best5_csv"),
                      ("analysis:ridge_selected_v_sliding_tune", "ridge_best12_csv")):
        assert fixture["hashes"][key] == manifest[role]["source_sha256"], f"unbound publication source: {role}"
    assert fixture["contract_parameters"]["bpm_index_sha256"] == fixture["hashes"]["bpm_index"]
    assert fixture["contract_parameters"]["spectral_cache_index_sha256"] == fixture["hashes"]["spectral_cache_index"]
    assert fixture["ridge_parameters"]["membership_sha256"] == fixture["hashes"]["curve_csv"]
    if bundle_path is not None:
        root = bundle_path.resolve()
        for relative, expected in fixture["bundle_files"].items():
            path = (root / relative).resolve()
            assert path.is_relative_to(root), "frozen payload path escapes bundle"
            assert sha256(path) == expected, f"publication payload hash mismatch: {relative}"
    return fixture


def numeric_cell(row: dict, field: str):
    value = row[field]
    return None if value is None or value == "" else float(value)


def compare_archived_publication(plane: str, actual: dict, reference: dict, fixture: dict) -> None:
    """Check independent accepted CUDA output, in addition to today's CPU oracle."""
    row = fixture["best_n_rows"][plane]
    members = row["bpm_members"].split(",")
    assert sorted(actual["selected_members"]) == sorted(members), f"publication/{plane}: archived members differ"
    assert sorted(reference["selected_members"]) == sorted(members), f"publication/{plane}: CPU oracle members differ"
    near(f"publication/{plane}.archived.selection_q", actual["candidate_q"], numeric_cell(row, "q_hat"), rtol=0, atol=1e-9)
    for field in SCORE_FIELDS:
        near(f"publication/{plane}.archived.{field}", actual["score"][field], numeric_cell(row, field))
    for target, source in (("fit_windows", "fit_window_count"), ("fit_end_turn", "fit_end_turn"),
                           ("test_start_turn", "test_start_turn")):
        assert actual[target] == int(row[source]), f"publication/{plane}.{target}"
    later_fields = {
        "window_count": "test_window_count", "q_hat": "test_q_hat_near_train",
        "blind_q_hat": "test_blind_q_hat", "peak_prominence": "test_peak_prominence_at_qhat",
        "p10_peak_prominence": "test_p10_peak_prominence_at_qhat",
        "power_support": "test_power_support_at_qhat", "visible_fraction": "test_visible_fraction_at_qhat",
        "spectral_entropy": "test_spectral_entropy",
    }
    for target, source in later_fields.items():
        near(f"publication/{plane}.archived.later.{target}", actual["later_support"][target], numeric_cell(row, source))
    archived = fixture["ridge_rows"][plane]
    assert len(archived) == len(actual["windows"]), f"publication/{plane}: archived trajectory count differs"
    # These columns are the paper's accepted candidates, before operational
    # intensity/synthetic/suspicious-step display gates. Blank cells remain null.
    for index, (row, point) in enumerate(zip(archived, reference["windows"])):
        assert int(row["window_index"]) == index
        assert int(row["center_turn"]) == point["center_turn"]
        for field in ("selected_tune", "raw_global_tune", "tracked_local_tune"):
            near(f"publication/{plane}.archived.window[{index}].{field}", point[field], numeric_cell(row, field),
                 rtol=0, atol=2e-8)
        near(f"publication/{plane}.archived.window[{index}].confidence", point["selected_confidence"],
             numeric_cell(row, "selected_confidence"), rtol=5e-4)
        for field in ("used_global_fallback", "suspicious_step"):
            assert point[field] == (str(row[field]).lower() == "true"), f"publication/{plane}/{index}.{field}"


def publication_parity(binary: Path, bundle_path: Path, fixture: dict, fixture_path: Path, folder: Path) -> dict:
    manifest_path = bundle_path / "manifest.json"
    bundle = ridge.load_bundle(manifest_path)
    assert fixture["spill_id"] == f"spill_{bundle.target_ms}"
    metadata = {row["source_key"]: row for row in fixture["bpm_index"]}
    traces = {}
    for plane in ("H", "V"):
        raw, _, warnings, labels, _ = ridge.load_plane_traces(
            bundle, plane, None, False, 0, 50000, "none", 4096, "stream_key")
        assert raw is not None and not warnings, f"publication/{plane}: incomplete raw capture: {warnings}"
        traces[plane] = sorted(
            [dict(id=key.split(":")[-2], plane=plane, source_key=key,
                  digitizer=metadata[key]["digitizer"], ring_order=float(key.split(":")[-2][2:]), samples=samples)
             for key, samples in zip(labels, raw)], key=lambda trace: trace["id"])
    case = Case("publication", 50000, len(traces["H"]), len(traces["V"]))
    references = {}
    for plane in ("H", "V"):
        print(f"Checking frozen publication {plane} Python reference ...", flush=True)
        references[plane] = oracle_plane(case, plane, traces[plane], require_unique=False)
    sources = [dict(id=trace["id"], plane=plane, digitizer=trace["digitizer"], ring_order=trace["ring_order"],
                    position_key=trace["source_key"], intensity_key=trace["source_key"].replace("POSITION", "INTENSITY"),
                    host="127.0.0.1", port=6379, db=0, beam_threshold=0.0)
               for plane in ("H", "V") for trace in traces[plane]]
    source_path = folder / "publication-sources.json"
    # Analyze only parses this inventory to recover frozen metadata; it makes no
    # Redis connection. Loopback addresses are intentional offline placeholders.
    source_path.write_text(json.dumps(dict(version=1, sources=sources)), encoding="utf-8")
    spec = paper_spec()
    spec["quality"]["require_intensity"] = False
    spec_path = folder / "publication-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    output_path = folder / "publication-result.json"
    completed = subprocess.run([str(binary), "analyze", "--input", str(bundle_path), "--sources", str(source_path),
                                "--spec", str(spec_path), "--output", str(output_path)],
                               cwd=ROOT, text=True, capture_output=True, timeout=180)
    if completed.returncode:
        raise AssertionError(f"publication replay failed:\n{completed.stderr}\n{completed.stdout}")
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["synthetic"] is False, "original capture was unexpectedly marked synthetic"
    assert result["source_time_ms"] == bundle.target_ms, "capture timestamp changed during replay"
    for plane in ("H", "V"):
        compare_plane(f"publication/{plane}", result["planes"][plane], references[plane], case, beam_fraction=None)
        compare_archived_publication(plane, result["planes"][plane], references[plane], fixture)
        print(f"PASS frozen publication {plane}: exact members, scores, later support, and {len(references[plane]['windows'])} windows", flush=True)
    return dict(status="passed", collection=fixture["collection"], spill_id=fixture["spill_id"],
                fixture_sha256=sha256(fixture_path), source_sha256=fixture["hashes"],
                raw_files_verified=len(fixture["bundle_files"]),
                winner_margins={plane: references[plane]["winner_margin"] for plane in ("H", "V")},
                intensity_qualification="not tested: accepted capture is position-only")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processor", type=Path, default=ROOT / "target/debug/tbt-processor")
    parser.add_argument("--case", action="append", choices=[case.name for case in CASES])
    parser.add_argument("--write-synthetic-goldens", action="store_true")
    parser.add_argument("--publication-bundle", type=Path, help="external original accepted spill directory; never committed")
    parser.add_argument("--publication-fixture", type=Path, default=FIXTURES / "publication_reference.json")
    parser.add_argument("--require-publication-fixture", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.write_synthetic_goldens and not args.processor.is_file():
        parser.error("processor binary missing; run cargo build --locked --bin tbt-processor")
    if not args.write_synthetic_goldens:
        defaults = subprocess.run([str(args.processor.resolve()), "defaults"], cwd=ROOT, text=True,
                                  capture_output=True, check=True, timeout=30)
        compare_golden("default_spec", json.loads(defaults.stdout), paper_spec())
    golden_path = FIXTURES / "synthetic_paper_reference.json"
    source_hashes = {path: sha256(ROOT / path) for path in ORACLE_FILES}
    golden = {} if args.write_synthetic_goldens else json.loads(golden_path.read_text(encoding="utf-8"))
    if golden and golden["oracle_source_sha256"] != source_hashes:
        raise AssertionError("paper Python source changed; review and explicitly refresh synthetic goldens")
    summary = dict(schema_version=1, evidence_kind="synthetic_python_reference", numpy_version=np.__version__,
                   oracle_source_sha256=source_hashes, paper_spec=paper_spec(), cases={})
    started = time.perf_counter()
    publication = None
    if not args.write_synthetic_goldens:
        publication = publication_fixture_check(args.publication_fixture, args.publication_bundle)
    with tempfile.TemporaryDirectory(prefix="tune-parity-") as temporary:
        folder = Path(temporary)
        for case in CASES:
            if args.case and case.name not in args.case:
                continue
            print(f"Checking {case.name} ...", flush=True)
            payload, eligible = synthetic_input(case)
            references = {plane: oracle_plane(case, plane, eligible[plane]) for plane in ("H", "V")}
            summaries = clean_json({plane: compact_reference(value) for plane, value in references.items()})
            summary["cases"][case.name] = dict(generator=asdict(case), planes=summaries)
            if not args.write_synthetic_goldens:
                compare_golden(case.name, summaries, golden["cases"][case.name]["planes"])
                result = run_processor(args.processor.resolve(), case, payload, case.spec(), folder)
                for plane, reference in references.items():
                    compare_plane(f"{case.name}/{plane}", result["planes"][plane], reference, case)
            print(f"PASS {case.name}", flush=True)
        if not args.write_synthetic_goldens:
            static_missing_check(args.processor.resolve(), folder)
            print("PASS static_missing_member", flush=True)
            if args.publication_bundle is not None:
                publication = publication_parity(args.processor.resolve(), args.publication_bundle.resolve(),
                                                 publication, args.publication_fixture, folder)
    if args.write_synthetic_goldens:
        if args.case:
            parser.error("refresh all synthetic goldens together; omit --case")
        FIXTURES.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(clean_json(summary), indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"Wrote {golden_path.relative_to(ROOT)} from Python only")
        return 0
    report = dict(synthetic_parity="passed", case_count=len(summary["cases"]),
                  numpy_version=np.__version__, elapsed_seconds=round(time.perf_counter() - started, 3),
                  oracle_source_sha256=source_hashes,
                  publication_fixture=publication if args.publication_bundle else "not_replayed",
                  tolerances=dict(selection_score_relative=3e-4, selection_score_absolute=2e-5,
                                  trajectory_tune_absolute=2e-8,
                                  spectrum_relative=5e-4, spectrum_absolute=2e-4),
                  publication_note="Position-only publication replay does not qualify an intensity-based beam-present gate.")
    if args.publication_bundle is None:
        print("NOT REPLAYED publication fixture: source bindings checked; pass --publication-bundle for numerical qualification", flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.require_publication_fixture and args.publication_bundle is None:
        return 1
    print(f"Numerical parity passed in {report['elapsed_seconds']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
