#!/usr/bin/env python3
"""Stdlib tests for the best-BPM mining pipeline."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from bpm_mining.config import default_config
from bpm_mining.consensus import cluster_candidates, weighted_median
from bpm_mining.io import build_manifest_outputs, read_csv, write_csv
from bpm_mining.identity import manifest_by_index, normalize_subset_row, subset_indices
from bpm_mining.peaks import extract_candidates
from bpm_mining.preprocessing import preprocess_window_np
from bpm_mining.spectra import build_spectral_cache, compute_spectra, tune_axis_for
from bpm_mining.subset_score import (
    combination_array,
    subset_mask,
    visibility_fraction_and_duration,
)
from bpm_mining.subset_search import supplement_pool
from bpm_mining.peaks import extract_per_bpm_features
from bpm_mining.consensus import build_consensus
from bpm_mining.subset_search import search_best_bpm_subsets
from bpm_mining.evolution import evaluate_evolution
from bpm_mining.fixed_sets import evaluate_fixed_sets
from bpm_mining.heldout import evaluate_group as evaluate_heldout_group, evaluate_heldout_support
from bpm_mining.handoff import _jaccard, run_handoff_analysis
from bpm_mining.statistics import aggregate_statistics
from bpm_mining.statistics import (
    kendall_tau,
    moving_block_resample,
    paired_tests,
    permutation_draw_count,
    rank_biserial_effect,
    spearman,
)
from bpm_mining.clustering import cluster_spills
from bpm_mining.artifact_selection import select_artifacts
from bpm_mining.best_n import (
    aggregate_metrics,
    block_bootstrap_ci,
    collapsed_validation_values,
    completed_curve_keys,
    completed_validation_keys,
    cross_collection_transfer,
    evaluate_best_n,
    fold_by_digitizer,
    merge_best_n_shards,
    purged_window_split,
    recommended_n,
    stratified_limit,
    training_candidates,
)
from bpm_mining.best_n_sensitivity import (
    SensitivityRun,
    build_sensitivity_matrix,
    read_mem_available_gib,
    validate_parallel_run_controls,
)
from bpm_mining.best_n_verification import verify_best_n_outputs
from bpm_mining.ridge_verification import (
    audit_sliding_file,
    contracted_center_grid,
    png_dimensions,
    verify_ridge_density_outputs,
)
from bpm_mining.intensity import (
    combine_weighted_spectra,
    first_sustained_bad_block,
    has_weight_fallback,
    intensity_integrity_row,
    method_effects,
    method_weight_fallbacks,
    method_weights,
    paired_channels,
)
from bpm_mining.intensity_verification import verify_intensity_outputs
from bpm_mining.intensity_plots import (
    DENSITY_DELTA_DESCRIPTION,
    DENSITY_DELTA_GUARDRAIL,
    DENSITY_DELTA_NOTE,
    DENSITY_DELTA_ZERO_NOTE,
    exact_paired_density_rows as exact_paired_intensity_density_rows,
    raster_cell_bounds as intensity_raster_cell_bounds,
)
from bpm_mining.payload_integrity import (
    device_fallback_values,
    longest_finite_exact_run,
    longest_true_run,
)
from bpm_mining.plots import make_artifacts
from bpm_mining.report import make_report
from bpm_mining.verification import verify_best_bpm_followups, verify_best_bpm_outputs
from audit_intensity_capture import audit as audit_intensity_capture
from audit_delivery_ring_payloads import audit_manifest as audit_delivery_ring_manifest
from run_best_n_sensitivity_matrix import _RunJob, _execute_jobs
from make_best_bpm_ridge_density import (
    caption_for_density,
    caption_for_difference,
    caption_for_legacy_difference,
    draw_legacy_pair_hv,
    draw_legacy_pair_hv_selected,
    draw_paired_density_grid_hv,
    draw_selected_turn_contrast_hv,
    draw_turn_metric_plot,
    exact_paired_density_results,
    exact_paired_density_results_many,
    keyed_ensemble_points,
    keyed_legacy_points,
    legacy_comparison_by_turn_rows,
    legacy_comparison_metrics,
    load_memberships,
    raster_cell_bounds,
    robust_change_point,
)
from gpu_analyze_captured_spills import (
    preprocess_traces,
    raster_cell_bounds as legacy_raster_cell_bounds,
    select_trace_subset,
)
from audit_legacy_single_bpm_selection import selection_row as legacy_selection_row
from package_publication_review import package_review, verify_review_package
from prepare_ibic2026_publication import (
    best_n_design_summary,
    prepare_publication,
    publication_content,
    publication_numeric_summary,
    render_results_macros,
    render_results_table,
    sensitivity_summary,
)
from repair_best_bpm_visibility_duration import repair_visibility_durations
from analyze_next_steps_outputs import fixed_score_contract_mismatches
from compare_intensity_block_sensitivity import (
    summarize_run as summarize_intensity_block_run,
    validate_sensitivity_rows as validate_intensity_sensitivity_rows,
)
from compare_best_n_beam_widths import compare_table as compare_best_n_beam_table
from compare_best_n_sensitivity import comparison_rows as compare_best_n_sensitivity_rows
from finalize_ibic2026_publication import (
    POSTER_STARTER_SHA256,
    empty_structural_placeholders,
    parse_pdfinfo,
    require_identical_files,
    sha256 as publication_sha256,
    verify_poster_source_manifest,
    verify_sha256_manifest,
    verify_template_fidelity,
)


def synthetic_collection(root: Path, spills: int = 3, bpms: int = 8, turns: int = 1024) -> None:
    root.mkdir(parents=True)
    rng = np.random.default_rng(20260614)
    n = np.arange(turns, dtype=np.float32)
    for spill in range(spills):
        target = 1000 + spill
        bundle = root / f"spill_{target}"
        payload_dir = bundle / "payloads"
        payload_dir.mkdir(parents=True)
        streams = []
        idx = 0
        for plane, tune in (("H", 0.65 + spill * 0.001), ("V", 0.72 - spill * 0.001)):
            freq = 1.0 - tune
            for bpm in range(bpms):
                amp = 1.0 + (bpms - bpm) / bpms
                phase = bpm * 0.37
                noise = 0.03 + 0.01 * bpm
                signal = amp * np.cos(2.0 * math.pi * freq * n + phase)
                signal *= np.exp(-n / 4000.0)
                signal += rng.normal(0.0, noise, size=turns)
                payload_name = f"payloads/stream_{idx:03d}_{plane}_{bpm}.bin"
                signal.astype("<f4").tofile(bundle / payload_name)
                name = f"{plane}P{600 + bpm:03d}"
                streams.append(
                    {
                        "bpm_ip": f"digitizer-{bpm // 2}",
                        "stream_key": f"{{TEST}}:{name}:TBT_POSITION_RAW",
                        "plane": plane,
                        "stream_id": f"{target}-0",
                        "stream_ms": target,
                        "aligned": True,
                        "payload_file": payload_name,
                        "payload_bytes": turns * 4,
                        "sample_count": turns,
                    }
                )
                idx += 1
        manifest = {
            "schema_version": 1,
            "artifact_type": "tbt-monitor.captured-spill",
            "target_ms": target,
            "requested_streams": len(streams),
            "streams": streams,
            "warnings": [],
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def small_config(collection: Path) -> dict[str, object]:
    cfg = default_config()
    cfg["data"]["collections"] = [str(collection)]
    cfg["integrity"]["minimum_turns"] = 256
    cfg["spectra"]["cache_dtype"] = "float32"
    cfg["spectra"]["configs"] = [
        {"name": "injection_2048", "turn_start": 0, "window_turns": 256},
        {"name": "injection_4096", "turn_start": 0, "window_turns": 512},
        {"name": "early_4096_256", "turn_start": 0, "turn_end": 1024, "window_turns": 512, "stride_turns": 128},
    ]
    cfg["subset_search"]["search_spectral_config"] = "early_4096_256"
    cfg["subset_search"]["subset_chunk_size"] = 32
    cfg["subset_search"]["best5_pool_size"] = 8
    cfg["subset_search"]["random_audit_samples"] = 50
    cfg["subset_search"]["beam_width"] = 32
    cfg["runtime"]["workers"] = 1
    cfg["runtime"]["device"] = "cpu"
    return cfg


class BestBpmMiningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="best-bpm-mining-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tune_axis_alias_mapping(self) -> None:
        axis, bins = tune_axis_for(1024, 2, (0.60, 0.70))
        self.assertTrue(np.any(np.abs(axis - 0.65) < 0.001))
        self.assertTrue(np.all(axis > 0.5))
        self.assertTrue(len(axis) == len(bins))

    def test_rank_biserial_effect_uses_absolute_difference_ranks(self) -> None:
        self.assertEqual(rank_biserial_effect([1.0, 2.0, -3.0]), 0.0)
        self.assertEqual(rank_biserial_effect([1.0, 1.0, -1.0]), 1.0 / 3.0)
        self.assertEqual(permutation_draw_count(10_000), 10_000)
        self.assertEqual(permutation_draw_count(1), 100)

    def test_best_n_validation_windows_do_not_overlap_fit_windows(self) -> None:
        centers = np.asarray([2048 + 256 * index for index in range(43)], dtype=np.float32)
        fit_count, test_start = purged_window_split(centers, 8, 4096)
        self.assertEqual(fit_count, 8)
        self.assertGreater(test_start, fit_count)
        fit_end = centers[fit_count - 1] + 2048
        test_begin = centers[test_start] - 2048
        self.assertGreaterEqual(test_begin, fit_end)

    def test_best_n_training_candidates_ignore_later_windows(self) -> None:
        tune_axis = np.asarray([0.64, 0.65, 0.66], dtype=np.float32)
        fit = np.asarray([[[1.0, 10.0, 1.0], [1.0, 8.0, 2.0]]], dtype=np.float32)
        later = np.asarray([[[1.0, 1.0, 1000.0], [1.0, 1.0, 1000.0]]], dtype=np.float32)
        baseline = training_candidates(fit, tune_axis)
        with_later_signal = training_candidates(np.concatenate([fit, later], axis=1)[:, : fit.shape[1]], tune_axis)
        np.testing.assert_array_equal(baseline, with_later_signal)
        self.assertAlmostEqual(float(baseline[0]), 0.65, places=6)

    def test_best_n_limited_sample_is_evenly_spaced_within_strata(self) -> None:
        rows = [
            {"collection": collection, "plane": plane, "spill_id": f"{spill:02d}"}
            for collection in ("a", "b")
            for plane in ("H", "V")
            for spill in range(10)
        ]
        selected = stratified_limit(rows, 8)
        self.assertEqual(len(selected), 8)
        by_stratum = {
            (collection, plane): [row["spill_id"] for row in selected if row["collection"] == collection and row["plane"] == plane]
            for collection in ("a", "b")
            for plane in ("H", "V")
        }
        self.assertTrue(all(spills == ["00", "09"] for spills in by_stratum.values()))

    def test_best_n_reports_blind_full_band_tune_separately(self) -> None:
        tune_axis = np.asarray([0.64, 0.65, 0.66, 0.67], dtype=np.float32)
        spectra = np.asarray(
            [
                [[1.0, 3.0, 2.0, 20.0], [1.0, 4.0, 2.0, 18.0]],
                [[1.0, 2.0, 2.0, 22.0], [1.0, 3.0, 2.0, 19.0]],
            ],
            dtype=np.float32,
        )
        metrics = aggregate_metrics(spectra, tune_axis, reference_q=0.65, tune_half_width=0.0025)
        self.assertAlmostEqual(metrics["q_hat"], 0.65, places=6)
        self.assertAlmostEqual(metrics["blind_q_hat"], 0.67, places=6)

    def test_best_n_block_bootstrap_is_deterministic_and_collection_stratified(self) -> None:
        series = {"a": [1.0, 2.0, 3.0, 4.0], "b": [10.0, 11.0, 12.0, 13.0]}
        first = block_bootstrap_ci(series, samples=120, seed=17, block_spills=2)
        second = block_bootstrap_ci(series, samples=120, seed=17, block_spills=2)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], 7.0)
        self.assertGreaterEqual(first[1], 7.0)

    def test_moving_block_resample_uses_full_nonwrapping_blocks(self) -> None:
        class RecordingRandom:
            def __init__(self) -> None:
                self.stops: list[int] = []

            def randrange(self, stop: int) -> int:
                self.stops.append(stop)
                return 0

        rng = RecordingRandom()
        sample = moving_block_resample([0.0, 1.0, 2.0, 3.0, 4.0], rng, 2)  # type: ignore[arg-type]
        self.assertEqual(sample, [0.0, 1.0, 0.0, 1.0, 0.0])
        self.assertEqual(rng.stops, [4, 4, 4])

    def test_best_n_folds_keep_digitizer_siblings_together(self) -> None:
        indices = [0, 1, 2, 3, 4, 5]
        metadata = {
            0: {"digitizer": "d0"},
            1: {"digitizer": "d0"},
            2: {"digitizer": "d1"},
            3: {"digitizer": "d1"},
            4: {"digitizer": "d2"},
            5: {"digitizer": "d2"},
        }
        assigned = fold_by_digitizer(indices, metadata, folds=3, seed=20260709)
        self.assertEqual(assigned[0], assigned[1])
        self.assertEqual(assigned[2], assigned[3])
        self.assertEqual(assigned[4], assigned[5])
        self.assertEqual(set(assigned.values()), {0, 1, 2})

    def test_best_n_disjoint_folds_reject_missing_digitizer_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing digitizer identity"):
            fold_by_digitizer([0, 1], {0: {"digitizer": "d0"}, 1: {"digitizer": ""}}, folds=2, seed=17)

    def test_best_n_sensitivity_matrix_reuses_shared_baseline(self) -> None:
        runs, dimensions = build_sensitivity_matrix(
            [16, 32, 64],
            [4, 8, 16],
            [20260709, 20260710, 20260711],
            32,
            8,
            20260709,
        )
        self.assertEqual(len(runs), 7)
        self.assertEqual([label for label, _run in dimensions["beam_width"]], ["beam16", "beam32", "beam64"])
        baseline = [run for run in runs if run.beam_width == 32 and run.fit_windows == 8 and run.fold_seed == 20260709]
        self.assertEqual(len(baseline), 1)

    def test_best_n_sensitivity_parallel_controls_and_memavailable(self) -> None:
        validate_parallel_run_controls(1, 32.0, 5.0, 3)
        validate_parallel_run_controls(2, 32.0, 5.0, 3)
        for bad_parallelism in (0, 3):
            with self.assertRaisesRegex(ValueError, "parallel_runs"):
                validate_parallel_run_controls(bad_parallelism, 32.0, 5.0, 3)
        with self.assertRaisesRegex(ValueError, "minimum_available_memory_gib"):
            validate_parallel_run_controls(2, 0.0, 5.0, 3)
        with self.assertRaisesRegex(ValueError, "memory_check_seconds"):
            validate_parallel_run_controls(2, 32.0, 0.0, 3)
        with self.assertRaisesRegex(ValueError, "low_memory_samples"):
            validate_parallel_run_controls(2, 32.0, 5.0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp) / "meminfo"
            meminfo.write_text(
                "MemTotal:       131072000 kB\nMemAvailable:    50331648 kB\n",
                encoding="utf-8",
            )
            self.assertEqual(read_mem_available_gib(meminfo), 48.0)
            meminfo.write_text("MemAvailable: not-a-number kB\n", encoding="utf-8")
            self.assertIsNone(read_mem_available_gib(meminfo))
            self.assertIsNone(read_mem_available_gib(Path(tmp) / "missing"))

    def test_best_n_sensitivity_dry_run_records_parallel_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / "inputs"
            (inputs / "manifest").mkdir(parents=True)
            (inputs / "cache" / "index").mkdir(parents=True)
            (inputs / "manifest" / "bpm_index.csv").write_text("source_key\n", encoding="utf-8")
            (inputs / "cache" / "index" / "spectral_cache.csv").write_text(
                "collection,spill_id,plane\n",
                encoding="utf-8",
            )
            out = root / "out"
            subprocess.run(
                [
                    sys.executable,
                    "scripts/run_best_n_sensitivity_matrix.py",
                    "--inputs",
                    str(inputs),
                    "--out",
                    str(out),
                    "--device",
                    "cpu",
                    "--max-n",
                    "1",
                    "--curve-limit",
                    "1",
                    "--validation-limit",
                    "1",
                    "--folds",
                    "1",
                    "--parallel-runs",
                    "2",
                    "--dry-run",
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
            )
            controls = json.loads((out / "execution_controls.json").read_text(encoding="utf-8"))
            self.assertEqual(controls["parallel_runs"], 2)
            self.assertEqual(controls["minimum_available_memory_gib"], 32.0)
            manifest = read_csv(out / "sensitivity_run_manifest.csv")
            self.assertEqual(len(manifest), 7)
            self.assertEqual({row["status"] for row in manifest}, {"planned"})

    def test_best_n_sensitivity_scheduler_is_bounded_and_fails_on_memory_floor(self) -> None:
        args = argparse.Namespace(
            dry_run=False,
            parallel_runs=2,
            minimum_available_memory_gib=32.0,
            memory_check_seconds=0.01,
            low_memory_samples=1,
            max_n=1,
            curve_limit=1,
            validation_limit=1,
            folds=1,
            bootstrap_block_spills=1,
        )
        runs = [SensitivityRun(1, 1, 1), SensitivityRun(2, 1, 1)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "parallel"
            out.mkdir()
            jobs = []
            for run in runs:
                start_marker = root / f"{run.slug}.start"
                end_marker = root / f"{run.slug}.end"
                jobs.append(
                    _RunJob(
                        run=run,
                        output=root / run.slug,
                        command=[
                            sys.executable,
                            "-c",
                            (
                                "import time; from pathlib import Path; "
                                f"Path({str(start_marker)!r}).touch(); time.sleep(0.3); "
                                f"Path({str(end_marker)!r}).touch()"
                            ),
                        ],
                    )
                )
            manifest = _execute_jobs(
                args,
                jobs,
                root,
                out,
                verify_run=lambda _args, _job: None,
                read_available_memory_gib=lambda: 128.0,
            )
            self.assertEqual([row["status"] for row in manifest], ["verified", "verified"])
            self.assertTrue(all(job.process is not None and job.process.poll() == 0 for job in jobs))
            start_times = [(root / f"{run.slug}.start").stat().st_mtime_ns for run in runs]
            end_times = [(root / f"{run.slug}.end").stat().st_mtime_ns for run in runs]
            self.assertLess(max(start_times), min(end_times))

            low_out = root / "low"
            low_out.mkdir()
            low_jobs = [
                _RunJob(
                    run=run,
                    output=root / f"low-{run.slug}",
                    command=[sys.executable, "-c", "import time; time.sleep(5)"],
                )
                for run in runs
            ]
            with self.assertRaisesRegex(RuntimeError, "available-memory floor"):
                _execute_jobs(
                    args,
                    low_jobs,
                    root,
                    low_out,
                    verify_run=lambda _args, _job: None,
                    read_available_memory_gib=lambda: 0.0,
                )
            self.assertTrue(all(job.process is not None and job.process.poll() is not None for job in low_jobs))
            abort = json.loads((low_out / "memory_guard_abort.json").read_text(encoding="utf-8"))
            self.assertEqual(abort["status"], "aborted")
            self.assertEqual(abort["active_runs"], [run.slug for run in runs])

    def test_best_n_resume_requires_exact_contiguous_rows(self) -> None:
        key = {"collection": "a", "spill_id": "1", "plane": "H"}
        curve = [{**key, "subset_size": value} for value in (1, 3)]
        self.assertEqual(completed_curve_keys(curve, 3), set())
        curve.append({**key, "subset_size": 2})
        self.assertEqual(completed_curve_keys(curve, 3), {("a", "1", "H")})
        curve.append({**key, "subset_size": 2})
        self.assertEqual(completed_curve_keys(curve, 3), set())

        validation = [
            {**key, "fold": fold, "subset_size": subset_size}
            for fold in (0, 1)
            for subset_size in (1, 2, 3)
        ]
        self.assertEqual(completed_validation_keys(validation, 3, 2), {("a", "1", "H")})
        validation = [row for row in validation if not (row["fold"] == 1 and row["subset_size"] == 2)]
        self.assertEqual(completed_validation_keys(validation, 3, 2), set())

    def test_best_n_comparators_reject_incomplete_key_coverage(self) -> None:
        reference = self.root / "beam_reference"
        comparison = self.root / "beam_comparison"
        reference.mkdir()
        comparison.mkdir()
        fields = ["collection", "spill_id", "plane", "subset_size", "subset_score", "q_hat", "bpm_indices"]
        write_csv(
            reference / "best_n_curve_rows.csv",
            [
                {"collection": "a", "spill_id": "1", "plane": "H", "subset_size": 1, "subset_score": 1, "q_hat": 0.65, "bpm_indices": "0"},
                {"collection": "a", "spill_id": "1", "plane": "H", "subset_size": 2, "subset_score": 2, "q_hat": 0.65, "bpm_indices": "0,1"},
            ],
            fields,
        )
        write_csv(
            comparison / "best_n_curve_rows.csv",
            [{"collection": "a", "spill_id": "1", "plane": "H", "subset_size": 1, "subset_score": 1, "q_hat": 0.65, "bpm_indices": "0"}],
            fields,
        )
        with self.assertRaisesRegex(ValueError, "key coverage differs"):
            compare_best_n_beam_table("curve", "best_n_curve_rows.csv", {32: reference, 64: comparison}, 32)

        summary_reference = [
            {"plane": "H", "subset_size": "1"},
            {"plane": "H", "subset_size": "2"},
        ]
        with self.assertRaisesRegex(ValueError, "key coverage differs"):
            compare_best_n_sensitivity_rows(
                "fit_windows",
                {"fit8": summary_reference, "fit16": summary_reference[:1]},
                "fit8",
            )

    def test_ridge_sliding_audit_requires_exact_grid_per_spill(self) -> None:
        grid = contracted_center_grid(turn_span=8, window_turns=4, stride_turns=2)
        self.assertEqual(grid, (2, 4, 6))
        path = self.root / "ridge_sliding.csv"
        rows = []
        for spill_id, centers in (("1", grid), ("2", grid[:-1]), ("1", grid)):
            rows.extend(
                {
                    "run_name": "run",
                    "target_ms": spill_id,
                    "spill_id": spill_id,
                    "plane": "H",
                    "center_turn": center,
                    "selected_bpm_count": 3,
                    "selected_tune": 0.65,
                }
                for center in centers
            )
        write_csv(path, rows, list(rows[0]))
        audit = audit_sliding_file(path, subset_size=3, expected_center_grid=grid)
        self.assertEqual(audit["group_counts"]["H"], 3)
        self.assertEqual(audit["duplicate_groups"]["H"], 1)
        self.assertEqual(audit["bad_center_grids"]["H"], 1)

    def test_intensity_fallback_label_presence(self) -> None:
        self.assertFalse(has_weight_fallback(""))
        self.assertFalse(has_weight_fallback("   "))
        self.assertTrue(has_weight_fallback("NO_USABLE_INTENSITY_UNWEIGHTED"))

    def test_best_n_validation_statistics_collapse_folds_within_spill(self) -> None:
        rows = [
            {"collection": "a", "spill_id": "1", "metric": value}
            for value in (1.0, 2.0, 9.0)
        ] + [
            {"collection": "a", "spill_id": "2", "metric": value}
            for value in (4.0, 5.0, 6.0)
        ]
        self.assertEqual(collapsed_validation_values(rows, "metric"), [2.0, 5.0])

    def test_best_n_recommendation_transfers_global_n_between_collections(self) -> None:
        def row(collection: str, subset_size: int, agreement: float, delta: float, support: float) -> dict[str, object]:
            return {
                "collection": collection,
                "plane": "H",
                "subset_size": subset_size,
                "validation_row_count": 100,
                "blind_q_agreement_rate": agreement,
                "median_blind_selected_heldout_abs_q_delta": delta,
                "median_test_peak_prominence": 8.0 + subset_size,
                "median_test_power_support": support,
                "median_heldout_prominence": 7.0 + subset_size,
                "median_heldout_power_support": support - 0.2,
            }

        rows = [
            row("a", 1, 0.70, 0.0040, 2.0),
            row("a", 2, 0.92, 0.0010, 3.0),
            row("b", 1, 0.72, 0.0035, 2.1),
            row("b", 2, 0.88, 0.0012, 2.9),
        ]
        chosen, _reason = recommended_n([item for item in rows if item["collection"] == "a"], "H", 0.0025)
        self.assertIsNotNone(chosen)
        self.assertEqual(int(chosen["subset_size"]), 2)
        transfer = cross_collection_transfer(rows, 0.0025)
        a_to_b = next(item for item in transfer if item["train_collection"] == "a" and item["test_collection"] == "b" and item["plane"] == "H")
        self.assertEqual(a_to_b["status"], "OK")
        self.assertEqual(a_to_b["selected_n"], 2)
        self.assertGreater(float(a_to_b["blind_agreement_gain_vs_n1"]), 0.0)
        self.assertGreater(float(a_to_b["blind_q_delta_reduction_vs_n1"]), 0.0)

    def test_best_n_recommendation_rejects_boundary_limited_knee(self) -> None:
        rows = [
            {
                "plane": "V",
                "subset_size": subset_size,
                "validation_row_count": 100,
                "blind_q_agreement_rate": 0.80 + 0.01 * subset_size,
                "median_blind_selected_heldout_abs_q_delta": 0.02 / subset_size,
                "median_test_peak_prominence": float(subset_size),
                "median_test_power_support": float(subset_size),
                "median_heldout_prominence": float(subset_size),
                "median_heldout_power_support": float(subset_size),
            }
            for subset_size in range(1, 11)
        ]
        chosen, reason = recommended_n(rows, "V", 0.0025)
        self.assertIsNone(chosen)
        self.assertIn("boundary-limited", reason)

    def test_peak_candidate_and_consensus(self) -> None:
        tune_axis = np.linspace(0.60, 0.70, 200, dtype=np.float32)
        power = np.ones_like(tune_axis)
        power[np.argmin(np.abs(tune_axis - 0.65))] = 100.0
        cfg = default_config()
        candidates = extract_candidates(power, tune_axis, "H", cfg)
        self.assertAlmostEqual(float(candidates[0]["peak_tune"]), 0.65, places=3)
        rows = []
        for idx, delta in enumerate((-0.0005, 0.0, 0.0005, 0.001)):
            rows.append(
                {
                    "bpm_index": str(idx),
                    "peak_tune": str(0.65 + delta),
                    "peak_prominence_z": "8",
                    "second_peak_ratio": "0.1",
                    "distance_to_band_edge": "0.02",
                    "valid_candidate": "true",
                }
            )
        clusters = cluster_candidates(rows, 0.002, 0.65)
        self.assertEqual(clusters[0]["unique_bpm_count"], 4)
        self.assertAlmostEqual(weighted_median([(0.64, 1), (0.66, 1)]), 0.64)

    def test_subset_mask_and_combinations(self) -> None:
        self.assertEqual(subset_mask([0, 2, 5]), 0b100101)
        combos = combination_array(list(range(8)), 3)
        self.assertEqual(combos.shape[0], 56)

    def test_visibility_duration_spans_only_visible_windows(self) -> None:
        fraction, duration = visibility_fraction_and_duration(
            [5.0, 1.0, 5.5, 1.0],
            [100.0, 200.0, 300.0, 400.0],
        )
        self.assertEqual(fraction, 0.5)
        self.assertEqual(duration, 200.0)
        self.assertEqual(visibility_fraction_and_duration([5.0], [100.0]), (1.0, 0.0))
        self.assertEqual(_jaccard(set(), set()), 1.0)

    def test_subset_identity_uses_mask_when_digitizer_labels_are_ambiguous(self) -> None:
        rows = [
            {"plane": "H", "bpm_index": "0", "bpm_name": "digitizer-0", "digitizer": "digitizer-0", "source_key": "{TEST}:HP600:TBT_POSITION_RAW"},
            {"plane": "H", "bpm_index": "1", "bpm_name": "digitizer-0", "digitizer": "digitizer-0", "source_key": "{TEST}:HP601:TBT_POSITION_RAW"},
            {"plane": "H", "bpm_index": "2", "bpm_name": "digitizer-1", "digitizer": "digitizer-1", "source_key": "{TEST}:HP602:TBT_POSITION_RAW"},
        ]
        meta = manifest_by_index(rows)
        legacy = {"plane": "H", "subset_mask": str(subset_mask([0, 2])), "bpm_members": "digitizer-0,digitizer-1"}
        self.assertEqual(subset_indices(legacy, "H", meta), [0, 2])
        normalized = normalize_subset_row(legacy, meta)
        self.assertEqual(normalized["bpm_indices"], "0,2")
        self.assertEqual(normalized["bpm_members"], "HP600,HP602")
        self.assertEqual(normalized["bpm_digitizers"], "digitizer-0,digitizer-1")

    def test_manifest_ring_order_comes_from_channel_token(self) -> None:
        collection = self.root / "ring-order-positiononly"
        synthetic_collection(collection, spills=1, bpms=4)
        cfg = small_config(collection)
        out = self.root / "ring_order_manifest"
        build_manifest_outputs(cfg, out)
        rows = read_csv(out / "bpm_index.csv")
        self.assertEqual({row["ring_order"] for row in rows if row["plane"] == "H"}, {"600", "601", "602", "603"})

    def test_intensity_pairing_and_single_bpm_weighting_invariance(self) -> None:
        bundle = self.root / "intensity-capture" / "spill_123"
        payloads = bundle / "payloads"
        payloads.mkdir(parents=True)
        position_path = payloads / "position.bin"
        intensity_path = payloads / "intensity.bin"
        np.arange(16, dtype="<f4").tofile(position_path)
        np.linspace(10, 1, 16, dtype="<f4").tofile(intensity_path)
        position_key = "{TEST}:HP609:TBT_POSITION_RAW"
        intensity_key = "{TEST}:HP609:TBT_INTENSITY_RAW"
        manifest = {
            "streams": [
                {
                    "stream_key": position_key,
                    "plane": "H",
                    "bpm_ip": "digitizer-1",
                    "stream_id": "123-1",
                    "stream_ms": 123,
                    "payload_file": "payloads/position.bin",
                    "sample_count": 16,
                },
                {
                    "stream_key": intensity_key,
                    "plane": "H",
                    "bpm_ip": "digitizer-1",
                    "stream_id": "123-1",
                    "stream_ms": 123,
                    "payload_file": "payloads/intensity.bin",
                    "sample_count": 16,
                },
            ]
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        pairs = paired_channels(bundle / "manifest.json")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].channel, "HP609")
        self.assertEqual(pairs[0].position_sample_count, 16)
        self.assertEqual(pairs[0].intensity_sample_count, 16)
        self.assertEqual(pairs[0].position_payload_sample_count, 16)
        self.assertEqual(pairs[0].intensity_payload_sample_count, 16)
        manifest["streams"][1]["sample_count"] = 15
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        mismatched = paired_channels(bundle / "manifest.json")[0]
        integrity = intensity_integrity_row(mismatched, analysis_turns=15, max_abs_intensity=1e12)
        self.assertIn("SAMPLE_COUNT_MISMATCH", integrity["quality_flags"])
        self.assertIn("PAYLOAD_SIZE_MISMATCH", integrity["quality_flags"])

        spectra = np.arange(24, dtype=np.float32).reshape(1, 3, 8) + 1.0
        relative = np.asarray([[0.2, 1.0, 3.0]], dtype=np.float32)
        baseline, _ = combine_weighted_spectra(spectra, method_weights(relative, "unweighted"))
        for method in ("sqrt_intensity", "linear_intensity", "intensity_gate_50pct"):
            weighted, _ = combine_weighted_spectra(spectra, method_weights(relative, method))
            np.testing.assert_allclose(weighted, baseline)
        relative_with_missing = np.asarray([[math.nan, 1.0, math.nan]], dtype=np.float32)
        for method in ("sqrt_intensity", "linear_intensity", "intensity_gate_50pct"):
            weighted, _ = combine_weighted_spectra(spectra, method_weights(relative_with_missing, method))
            np.testing.assert_allclose(weighted, baseline)
        self.assertEqual(
            method_weight_fallbacks(relative_with_missing, "sqrt_intensity"),
            ["NO_USABLE_INTENSITY_UNWEIGHTED", "", "NO_USABLE_INTENSITY_UNWEIGHTED"],
        )
        self.assertEqual(
            method_weight_fallbacks(relative, "intensity_gate_50pct"),
            ["EMPTY_FINITE_GATE_STRONGEST", "", ""],
        )
        valid = np.ones(32, dtype=bool)
        valid[16:24] = False
        self.assertEqual(first_sustained_bad_block(valid, 8, 0.99), 16)

    def test_intensity_capture_audit_names_missing_exact_pair(self) -> None:
        collection = self.root / "intensity-audit"
        synthetic_collection(collection, spills=2, bpms=2, turns=256)
        for manifest_path in collection.glob("spill_*/manifest.json"):
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            intensity_streams = []
            for position in data["streams"]:
                position_key = position["stream_key"]
                intensity_key = position_key.replace("TBT_POSITION_RAW", "TBT_INTENSITY_RAW")
                intensity_path = manifest_path.parent / position["payload_file"].replace("stream_", "intensity_")
                np.ones(256, dtype="<f4").tofile(intensity_path)
                intensity_streams.append(
                    {
                        **position,
                        "stream_key": intensity_key,
                        "payload_file": str(intensity_path.relative_to(manifest_path.parent)),
                    }
                )
            data["streams"].extend(intensity_streams)
            data["requested_streams"] = len(data["streams"])
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
        partial = sorted(collection.glob("spill_*/manifest.json"))[1]
        data = json.loads(partial.read_text(encoding="utf-8"))
        data["streams"] = [stream for stream in data["streams"] if ":VP601:" not in stream["stream_key"]]
        partial.write_text(json.dumps(data), encoding="utf-8")

        out = self.root / "intensity-audit-out"
        audit_intensity_capture(collection, out)
        inventory = read_csv(out / "intensity_capture_inventory.csv")
        missing = read_csv(out / "intensity_capture_missing_pairs.csv")
        self.assertEqual(len(inventory), 2)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["channel"], "VP601")
        self.assertEqual(missing[0]["status"], "PAIR_ABSENT")

    def test_payload_integrity_detects_exact_plateaus_and_device_fallback_pairs(self) -> None:
        position = np.asarray([0.0, 0.2, 0.4, *([1.01] * 12), 0.7], dtype=np.float32)
        intensity = np.asarray([10.0, 20.0, 30.0, *([1101.0] * 12), 40.0], dtype=np.float32)
        start, turns, values = longest_finite_exact_run([position, intensity])
        self.assertEqual((start, turns), (3, 12))
        np.testing.assert_allclose(values, (1.01, 1101.0))
        fallback = device_fallback_values("HP101")
        self.assertEqual(fallback, (1.01, 1101.0))
        mask = (position == np.float32(fallback[0])) & (intensity == np.float32(fallback[1]))
        self.assertEqual(longest_true_run(mask), (3, 12))
        self.assertIsNone(device_fallback_values("invalid"))

    def test_delivery_ring_manifest_audit_flags_raw_fallback_plateau(self) -> None:
        bundle = self.root / "payload-audit" / "spill_1"
        payloads = bundle / "payloads"
        payloads.mkdir(parents=True)
        position = np.asarray([0.0, 0.2, 0.4, *([1.01] * 12), 0.7], dtype="<f4")
        intensity = np.asarray([10.0, 20.0, 30.0, *([1101.0] * 12), 40.0], dtype="<f4")
        position.tofile(payloads / "position.bin")
        intensity.tofile(payloads / "intensity.bin")
        key = "{TEST}:HP101"
        manifest = {
            "streams": [
                {
                    "stream_key": f"{key}:TBT_POSITION_RAW",
                    "plane": "H",
                    "bpm_ip": "digitizer-1",
                    "stream_id": "1-0",
                    "stream_ms": 1,
                    "payload_file": "payloads/position.bin",
                    "sample_count": len(position),
                },
                {
                    "stream_key": f"{key}:TBT_INTENSITY_RAW",
                    "plane": "H",
                    "bpm_ip": "digitizer-1",
                    "stream_id": "1-0",
                    "stream_ms": 1,
                    "payload_file": "payloads/intensity.bin",
                    "sample_count": len(intensity),
                },
            ]
        }
        manifest_path = bundle / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        rows, topology = audit_delivery_ring_manifest(manifest_path, analysis_turns=16, plateau_turns=8)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(topology), 1)
        self.assertIn("LONG_EXACT_POSITION_PLATEAU", rows[0]["quality_flags"])
        self.assertIn("LONG_EXACT_PAIRED_PLATEAU", rows[0]["quality_flags"])
        self.assertIn("RAW_DEVICE_FALLBACK_PAIR", rows[0]["quality_flags"])

    def test_tiny_intensity_entropy_shift_is_not_practically_retained(self) -> None:
        rows = []
        for spill in range(32):
            baseline = 0.800000 + spill * 1e-7
            for method, entropy in (
                ("unweighted", baseline),
                ("sqrt_intensity", baseline - 1e-5),
                ("linear_intensity", baseline - 1e-5),
                ("intensity_gate_50pct", baseline),
            ):
                rows.append(
                    {
                        "collection": "a",
                        "spill_id": str(spill),
                        "plane": "V",
                        "subset_size": 5,
                        "method": method,
                        "median_test_q_near_train": 0.72,
                        "median_peak_prominence_at_train_q": 6.0,
                        "median_power_support_at_train_q": 2.0,
                        "visible_test_window_fraction": 0.5,
                        "median_spectral_entropy": entropy,
                        "median_abs_q_delta_from_train": 0.001,
                    }
                )
        effects = method_effects(rows, 0.0025, bootstrap_samples=100, permutation_samples=256)
        entropy = [
            row
            for row in effects
            if row["method"] == "linear_intensity" and row["metric"] == "median_spectral_entropy"
        ][0]
        self.assertEqual(entropy["practical_effect_pass"], "false")
        self.assertEqual(entropy["retain_method_for_tune_analysis"], "false")
        self.assertEqual(int(entropy["bootstrap_block_spills"]), 20)

    def test_intensity_method_requires_spillwise_tune_stability(self) -> None:
        rows = []
        for spill in range(40):
            baseline_q = 0.720
            candidate_q = baseline_q + (0.004 if spill < 4 else 0.0)
            for method, prominence, q_value in (
                ("unweighted", 5.0, baseline_q),
                ("sqrt_intensity", 7.0, candidate_q),
                ("linear_intensity", 5.0, baseline_q),
                ("intensity_gate_50pct", 5.0, baseline_q),
            ):
                rows.append(
                    {
                        "collection": "a",
                        "spill_id": str(spill),
                        "plane": "V",
                        "subset_size": 5,
                        "method": method,
                        "median_test_q_near_train": q_value,
                        "median_peak_prominence_at_train_q": prominence,
                        "median_power_support_at_train_q": 2.0,
                        "visible_test_window_fraction": 0.5,
                        "median_spectral_entropy": 0.8,
                        "median_abs_q_delta_from_train": 0.001,
                    }
                )
        effects = method_effects(rows, 0.0025, bootstrap_samples=100, permutation_samples=256)
        prominence = [
            row
            for row in effects
            if row["method"] == "sqrt_intensity" and row["metric"] == "median_peak_prominence_at_train_q"
        ][0]
        self.assertEqual(float(prominence["q_shift_within_tolerance_fraction"]), 0.9)
        self.assertEqual(prominence["statistical_benefit_pass"], "false")
        self.assertEqual(prominence["retain_method_for_tune_analysis"], "false")

    def test_intensity_density_subtraction_is_exact_paired_probability(self) -> None:
        baseline = [
            {
                "collection": "a",
                "spill_id": str(spill),
                "plane": "V",
                "subset_size": 5,
                "window_index": 0,
                "center_turn": 2048,
                "q_global": 0.720 + spill * 0.001,
            }
            for spill in range(2)
        ]
        weighted = [{**row, "q_global": float(row["q_global"]) + 0.0005} for row in baseline]
        paired_baseline, paired_weighted = exact_paired_intensity_density_rows(
            baseline,
            weighted,
            (0.69, 0.74),
        )
        self.assertEqual(len(paired_baseline), 2)
        self.assertEqual(len(paired_weighted), 2)
        with self.assertRaisesRegex(ValueError, "identical exact spill/window keys"):
            exact_paired_intensity_density_rows(baseline, weighted[:-1], (0.69, 0.74))
        with self.assertRaisesRegex(ValueError, "duplicate unweighted intensity ridge point"):
            exact_paired_intensity_density_rows([*baseline, baseline[0]], weighted, (0.69, 0.74))

        copy = " ".join((DENSITY_DELTA_NOTE, DENSITY_DELTA_DESCRIPTION, DENSITY_DELTA_GUARDRAIL))
        self.assertIn("probability", copy.lower())
        self.assertIn("exact common", copy.lower())
        self.assertIn("P99", DENSITY_DELTA_DESCRIPTION)
        self.assertNotIn("suppresses", copy.lower())
        self.assertNotIn("weighted adds", copy.lower())
        self.assertEqual(DENSITY_DELTA_ZERO_NOTE, "NO RIDGE-PICK PROBABILITY REDISTRIBUTION")

    def test_intensity_raster_cells_fill_uneven_axes_without_gaps(self) -> None:
        for reverse in (False, True):
            cells = [
                intensity_raster_cell_bounds(index, 7, 13, 113, reverse=reverse)
                for index in range(7)
            ]
            ordered = sorted(cells)
            self.assertEqual(ordered[0][0], 13)
            self.assertEqual(ordered[-1][1], 113)
            for left, right in zip(ordered, ordered[1:]):
                self.assertEqual(left[1] + 1, right[0])

    def test_intensity_best1_zero_effect_has_numeric_null_inference(self) -> None:
        rows = []
        for spill in range(24):
            for method in ("unweighted", "sqrt_intensity", "linear_intensity", "intensity_gate_50pct"):
                rows.append(
                    {
                        "collection": "a",
                        "spill_id": str(spill),
                        "plane": "H",
                        "subset_size": 1,
                        "method": method,
                        "median_test_q_near_train": 0.65,
                        "median_peak_prominence_at_train_q": 5.0,
                        "median_power_support_at_train_q": 2.0,
                        "visible_test_window_fraction": 0.5,
                        "median_spectral_entropy": 0.8,
                        "median_abs_q_delta_from_train": 0.001,
                    }
                )
        effects = method_effects(rows, 0.0025, bootstrap_samples=100, permutation_samples=256)
        self.assertEqual(len(effects), 15)
        for row in effects:
            self.assertEqual(float(row["median_paired_delta"]), 0.0)
            self.assertEqual(float(row["permutation_p_value"]), 1.0)
            self.assertEqual(float(row["fdr_q_value"]), 1.0)
            self.assertEqual(row["retain_method_for_tune_analysis"], "false")

    def test_publication_review_package_copies_and_hashes_components(self) -> None:
        source_file = self.root / "paper.pdf"
        source_file.write_bytes(b"paper")
        source_dir = self.root / "gallery-source"
        source_dir.mkdir()
        (source_dir / "figure.png").write_bytes(b"figure")
        out = self.root / "review-package"
        rows = package_review((("paper", source_file), ("gallery", source_dir)), out)
        self.assertEqual(len(rows), 2)
        self.assertEqual((out / "paper" / "paper.pdf").read_bytes(), b"paper")
        self.assertEqual((out / "gallery" / "figure.png").read_bytes(), b"figure")
        manifest = read_csv(out / "MANIFEST.csv")
        self.assertEqual(len(manifest), 2)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in manifest))
        self.assertTrue((out / "PACKAGE_INDEX.md").exists())
        verification_path = out / "PACKAGE_VERIFICATION.json"
        self.assertTrue(verification_path.exists())
        verification = verify_review_package(out)
        self.assertEqual(verification["status"], "pass")
        self.assertEqual(verification["manifest_rows"], 2)
        self.assertEqual(verification["gallery_images"], 1)
        gallery = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("Publication Review Gallery", gallery)
        self.assertIn("gallery/figure.png", gallery)
        (out / "gallery" / "figure.png").write_bytes(b"change")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify_review_package(out)

    def test_publication_review_package_rejects_unsafe_destinations(self) -> None:
        source = self.root / "source.txt"
        source.write_text("source", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unique"):
            package_review((("same", source), ("same", source)), self.root / "duplicates")
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not empty"):
            package_review((("source", source),), occupied)
        occupied_file = self.root / "occupied-file"
        occupied_file.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            package_review((("source", source),), occupied_file)

    def test_publication_pdfinfo_parser_requires_geometry(self) -> None:
        info = parse_pdfinfo(
            "Title: test\nPages:          4\nPage size:      595 x 792 pts\n"
        )
        self.assertEqual(info["pages"], 4)
        self.assertEqual(info["width_points"], 595.0)
        self.assertEqual(info["height_points"], 792.0)
        with self.assertRaisesRegex(ValueError, "missing page count"):
            parse_pdfinfo("Title: no geometry\n")

    def test_publication_requires_pdf_derived_poster_preview(self) -> None:
        preview = self.root / "poster.png"
        render = self.root / "poster-render.png"
        preview.write_bytes(b"same-png")
        render.write_bytes(b"same-png")
        require_identical_files("poster preview", preview, render)
        render.write_bytes(b"different-png")
        with self.assertRaisesRegex(ValueError, "files differ"):
            require_identical_files("poster preview", preview, render)

    def test_publication_rejects_empty_structural_poster_placeholders(self) -> None:
        pptx = self.root / "poster.pptx"
        slide_xml = """\
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="Filled title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
      <p:txBody><a:p><a:r><a:t>Poster title</a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="3" name="Empty body"/><p:cNvSpPr/><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>
      <p:txBody><a:p><a:r><a:t>   </a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="4" name="Empty ordinary shape"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:txBody><a:p/></p:txBody>
    </p:sp>
  </p:spTree></p:cSld>
</p:sld>
"""
        with zipfile.ZipFile(pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/slides/slide1.xml", slide_xml)
        self.assertEqual(
            empty_structural_placeholders(pptx),
            ["ppt/slides/slide1.xml: shape 3 (Empty body)"],
        )

        clean_pptx = self.root / "clean-poster.pptx"
        with zipfile.ZipFile(clean_pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                slide_xml.replace(">   </a:t>", ">Results</a:t>"),
            )
        self.assertEqual(empty_structural_placeholders(clean_pptx), [])

    def test_publication_verifies_portable_checksum_manifests(self) -> None:
        alpha = self.root / "alpha.txt"
        beta = self.root / "nested" / "beta.txt"
        alpha.write_text("alpha", encoding="utf-8")
        beta.parent.mkdir()
        beta.write_text("beta", encoding="utf-8")
        manifest = self.root / "checksums.txt"
        manifest.write_text(
            f"{publication_sha256(alpha)}  alpha.txt\n"
            f"{publication_sha256(beta)}  nested/beta.txt\n",
            encoding="utf-8",
        )
        expected = {"alpha.txt": alpha, "nested/beta.txt": beta}
        verify_sha256_manifest(manifest, expected)

        beta.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256 manifest mismatch"):
            verify_sha256_manifest(manifest, expected)
        manifest.write_text(f"{'a' * 64}  /absolute/path\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nonportable"):
            verify_sha256_manifest(manifest, {"/absolute/path": alpha})

    def test_publication_verifies_poster_source_and_fidelity_manifests(self) -> None:
        publication = self.root / "publication"
        poster = publication / "poster"
        assets = poster / "assets"
        build = poster / "build"
        layout = build / "layout" / "final-slide-01.layout.json"
        assets.mkdir(parents=True)
        layout.parent.mkdir(parents=True)
        content = poster / "content.json"
        content.write_text("{}", encoding="utf-8")

        png_header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (480).to_bytes(4, "big")
        )
        asset_names = {
            "bestNH": "best_n_validation_h.png",
            "bestNV": "best_n_validation_v.png",
            "ridgeHV": "ridge_density_comparison.png",
            "ridgeContrast": "ridge_width_contrast_hv.png",
            "hLoss": "horizontal_loss_diagnostic.png",
        }
        asset_records = {}
        for key, filename in asset_names.items():
            path = assets / filename
            path.write_bytes(png_header)
            asset_records[key] = {
                "sha256": publication_sha256(path),
                "dimensions": {"width": 640, "height": 480},
            }

        pptx = build / "ibic2026-abstract54-poster.pptx"
        preview = build / "ibic2026-abstract54-poster-artifact-preview.png"
        pptx.write_bytes(b"pptx")
        preview.write_bytes(png_header)
        layout.write_text("{}", encoding="utf-8")
        source_manifest = {
            "schema": "tbt-monitor.ibic2026-poster-source/v1",
            "starter": {"sha256": POSTER_STARTER_SHA256},
            "content": {"sha256": publication_sha256(content)},
            "assets": asset_records,
            "outputs": {
                "pptx": {"sha256": publication_sha256(pptx)},
                "artifactPreview": {"sha256": publication_sha256(preview)},
                "layout": {"sha256": publication_sha256(layout)},
            },
        }
        (build / "source_manifest.json").write_text(
            json.dumps(source_manifest), encoding="utf-8"
        )
        verify_poster_source_manifest(publication)
        content.write_text('{"changed": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "poster source manifest mismatch"):
            verify_poster_source_manifest(publication)

        fidelity = build / "template-fidelity-check.json"
        fidelity.write_text(
            json.dumps({"status": "pass", "issueCount": 0, "issues": []}),
            encoding="utf-8",
        )
        verify_template_fidelity(fidelity)
        fidelity.write_text(
            json.dumps({"status": "pass", "issueCount": 1, "issues": ["drift"]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "zero-issue pass"):
            verify_template_fidelity(fidelity)

    def test_publication_copy_and_table_are_plane_specific(self) -> None:
        best = {
            plane: {
                "blind_q_agreement_rate": "0.5",
                "blind_q_agreement_ci_low": "0.4",
                "blind_q_agreement_ci_high": "0.6",
                "median_blind_selected_heldout_abs_q_delta": "0.001",
                "blind_selected_heldout_abs_q_delta_ci_low": "0.0008",
                "blind_selected_heldout_abs_q_delta_ci_high": "0.0012",
            }
            for plane in ("H", "V")
        }
        ridge = {
            plane: {
                "median_iqr_delta_ensemble_minus_legacy": "-0.002",
                "median_iqr_delta_ci_low": "-0.0025",
                "median_iqr_delta_ci_high": "-0.0015",
                "median_shared_ridge_mass_gain": "0.1",
                "median_shared_ridge_mass_gain_ci_low": "0.08",
                "median_shared_ridge_mass_gain_ci_high": "0.12",
            }
            for plane in ("H", "V")
        }
        sizes = {"H": 7, "V": 11}
        design = {
            "curve_spill_plane_count": 4000,
            "validation_spill_plane_count": 1000,
            "digitizer_fold_count": 5,
        }
        table = render_results_table(best, ridge, sizes)
        self.assertIn("H & 7", table)
        self.assertIn("V & 11", table)
        self.assertIn("concentration, not absolute tune accuracy", table)
        content = publication_content(
            sizes,
            best,
            ridge,
            {"first_sustained_half_peak_loss_turn": "12000", "most_likely_change_turn": "14000"},
            design,
            240,
            0,
        )
        self.assertEqual(content["author"], "Derek Steinkamp | Fermi National Accelerator Laboratory")
        self.assertIn("H Best-7", content["ridgeCaption"])
        self.assertIn("V Best-11", content["ridgeCaption"])
        self.assertIn("P10-P90 width minus legacy", content["ridgeContrastCaption"])
        self.assertIn("0/240", content["quantitativeBody"])
        self.assertIn("4,000 H/V curve cases", content["quantitativeBody"])
        self.assertIn("1,000 stratified validation cases", content["quantitativeBody"])
        self.assertIn("5 held-out-digitizer folds", content["quantitativeBody"])
        self.assertNotIn("cases x5", content["quantitativeBody"])
        self.assertIn("1,000 stratified spill-plane", content["methodBody"])

    def test_publication_numeric_macros_are_generated_from_accepted_rows(self) -> None:
        primary = [
            {"plane": plane, "subset_size": str(size), "subset_score": str(value + offset)}
            for plane, offset in (("H", 0.0), ("V", 0.1))
            for size, value in ((1, 0.3), (3, 0.4), (5, 0.5))
        ]
        paired = [
            {"plane": plane, "comparison": comparison, "median_paired_difference": str(value)}
            for plane, offset in (("H", 0.0), ("V", 0.01))
            for comparison, value in (("best1 vs best3", 0.05 + offset), ("best3 vs best5", 0.02 + offset))
        ]
        intensity = [
            {
                "statistical_benefit_pass": "true" if index == 0 else "false",
                "fdr_q_value": "0.01" if index == 0 else "1",
                "practical_effect_pass": "false",
                "retain_method_for_tune_analysis": "false",
            }
            for index in range(3)
        ]
        design = {
            "curve_spill_plane_count": 4000,
            "validation_spill_plane_count": 1000,
            "digitizer_fold_count": 5,
            "maximum_n": 40,
            "curve_evaluation_row_count": 160000,
            "validation_evaluation_row_count": 200000,
        }
        summary = publication_numeric_summary(primary, paired, intensity, design)
        macros = render_results_macros(summary)
        self.assertIn(r"\newcommand{\PrimaryHBestOneScore}{0.300}", macros)
        self.assertIn(r"\newcommand{\PrimaryVThreeToFiveGain}{0.0300}", macros)
        self.assertIn(r"\newcommand{\IntensityEffectCount}{3}", macros)
        self.assertIn(r"\newcommand{\IntensityFdrCount}{1}", macros)
        self.assertIn(r"\newcommand{\IntensityRetainedCount}{0}", macros)
        self.assertIn(r"\newcommand{\BestNCurveSpillPlaneCount}{4000}", macros)
        self.assertIn(r"\newcommand{\BestNValidationSpillPlaneCount}{1000}", macros)
        self.assertIn(r"\newcommand{\BestNDigitizerFoldCount}{5}", macros)
        with self.assertRaisesRegex(ValueError, "definitive study design"):
            best_n_design_summary({"curve_cache_key_count": 4000})

    def test_publication_rejects_unresolved_best_n_sensitivity(self) -> None:
        root = self.root / "best-n-sensitivity"
        manifest = []
        for index in range(7):
            run_root = root / "runs" / f"run-{index}"
            run_root.mkdir(parents=True)
            rows = [
                {
                    "plane": plane,
                    "subset_size": 1,
                    "validation_row_count": 20 if plane == "H" else 0,
                    "blind_q_agreement_rate": 0.9,
                    "median_blind_selected_heldout_abs_q_delta": 0.001,
                    "median_test_peak_prominence": 5,
                    "median_test_power_support": 5,
                    "median_heldout_prominence": 5,
                    "median_heldout_power_support": 5,
                }
                for plane in ("H", "V")
            ]
            write_csv(run_root / "best_n_summary.csv", rows, list(rows[0]))
            (run_root / "run_contract.json").write_text(
                json.dumps({"tune_half_width": 0.0025}), encoding="utf-8"
            )
            manifest.append(
                {
                    "run": f"run-{index}",
                    "output": str(run_root),
                    "beam_width": 16 + index,
                    "fit_windows": 4 + index,
                    "fold_seed": index,
                    "status": "verified",
                }
            )
        write_csv(root / "sensitivity_run_manifest.csv", manifest, list(manifest[0]))
        with self.assertRaisesRegex(ValueError, "unresolved recommendations: V=7/7"):
            sensitivity_summary(root, 0.0025)

    def test_publication_materialization_binds_reports_numbers_and_figures(self) -> None:
        primary = self.root / "primary"
        followup = self.root / "followup"
        best_n = self.root / "best-n"
        ridge = self.root / "ridge"
        intensity = self.root / "intensity"
        payload_audit = self.root / "payload-audit"
        publication = self.root / "publication"

        def json_file(path: Path, value: dict[str, object]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")

        def csv_file(path: Path, rows: list[dict[str, object]]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_csv(path, rows, list(rows[0]))

        for report in (
            primary / "logs" / "best_bpm_verification.json",
            followup / "logs" / "best_bpm_followup_verification.json",
            *(best_n / f"merged_block{block}" / "best_n_verification.json" for block in (10, 20, 40)),
            ridge / "ridge_density_verification.json",
            intensity / "merged_block20" / "intensity_verification.json",
        ):
            json_file(report, {"status": "pass", "error_count": 0})
        json_file(
            best_n / "merged_block20" / "best_n_verification.json",
            {
                "status": "pass",
                "error_count": 0,
                "expected_max_n": 40,
                "expected_folds": 5,
                "curve_cache_key_count": 4000,
                "curve_row_count": 160000,
                "validation_cache_key_count": 1000,
                "validation_row_count": 200000,
            },
        )
        json_file(
            payload_audit / "delivery_ring_payload_audit.json",
            {
                "schema": "tbt-monitor.delivery-ring-payload-audit/v1",
                "status": "pass",
                "analysis_turns": 50000,
                "plateau_turns": 128,
                "manifest_count": 2200,
                "stream_rows": 263999,
                "paired_stream_rows": 23999,
                "incomplete_manifests": 1,
                "flagged_rows": 0,
                "position_plateau_rows": 0,
                "paired_plateau_rows": 0,
                "raw_device_fallback_pair_rows": 0,
                "error_count": 0,
                "manifest_inventory_sha256": "a" * 64,
                "topology": {
                    name: {
                        "unique_position_streams": 120,
                        "unique_h_streams": 60,
                        "unique_v_streams": 60,
                        "unique_digitizers": 30,
                        "bad_digitizers": [],
                    }
                    for name in ("a", "b", "intensity")
                },
            },
        )

        best_rows = []
        for plane in ("H", "V"):
            best_rows.append(
                {
                    "plane": plane,
                    "subset_size": 1,
                    "validation_row_count": 20,
                    "validation_spill_count": 20,
                    "blind_q_agreement_rate": 0.9,
                    "blind_q_agreement_ci_low": 0.85,
                    "blind_q_agreement_ci_high": 0.95,
                    "median_blind_selected_heldout_abs_q_delta": 0.001,
                    "blind_selected_heldout_abs_q_delta_ci_low": 0.0008,
                    "blind_selected_heldout_abs_q_delta_ci_high": 0.0012,
                    "median_test_peak_prominence": 5,
                    "median_test_power_support": 5,
                    "median_heldout_prominence": 5,
                    "median_heldout_power_support": 5,
                }
            )
        block20 = best_n / "merged_block20"
        json_file(block20 / "run_contract.json", {"tune_half_width": 0.0025})
        csv_file(block20 / "best_n_summary.csv", best_rows)
        csv_file(
            block20 / "best_n_cross_collection_transfer.csv",
            [
                {"train_collection": train, "test_collection": test, "plane": plane, "status": "OK"}
                for train, test in (("A", "B"), ("B", "A"))
                for plane in ("H", "V")
            ],
        )
        csv_file(
            best_n / "block_sensitivity" / "best_n_bootstrap_block_spills_recommendations.csv",
            [{"plane": plane, "status": "OK"} for plane in ("H", "V")],
        )
        sensitivity_manifest = []
        for index in range(7):
            run_root = best_n / "sensitivity" / "runs" / f"run-{index}"
            csv_file(run_root / "best_n_summary.csv", best_rows)
            json_file(run_root / "run_contract.json", {"tune_half_width": 0.0025})
            sensitivity_manifest.append(
                {
                    "run": f"run-{index}",
                    "output": str(run_root),
                    "beam_width": 16 + index,
                    "fit_windows": 4 + index,
                    "fold_seed": index,
                    "status": "verified",
                }
            )
        csv_file(best_n / "sensitivity" / "sensitivity_run_manifest.csv", sensitivity_manifest)

        json_file(ridge / "run_contract.json", {"selected_plane_sizes": {"H": 1, "V": 1}})
        ridge_rows = [
            {
                "plane": plane,
                "subset_size": 1,
                "median_iqr_delta_ensemble_minus_legacy": -0.002,
                "median_iqr_delta_ci_low": -0.0025,
                "median_iqr_delta_ci_high": -0.0015,
                "median_shared_ridge_mass_gain": 0.1,
                "median_shared_ridge_mass_gain_ci_low": 0.08,
                "median_shared_ridge_mass_gain_ci_high": 0.12,
            }
            for plane in ("H", "V")
        ]
        csv_file(ridge / "ridge_density_legacy_comparison_metrics.csv", ridge_rows)
        csv_file(
            ridge / "ridge_density_loss_candidates.csv",
            [
                {
                    "plane": plane,
                    "subset_size": 1,
                    "first_sustained_half_peak_loss_turn": 12000 if plane == "H" else "",
                    "most_likely_change_turn": 14000 if plane == "H" else "",
                }
                for plane in ("H", "V")
            ],
        )
        figure_paths = (
            block20 / "best_n_validation_h.png",
            block20 / "best_n_validation_v.png",
            ridge / "ridge_density_legacy_single_vs_best_h1_v1_hv.png",
            ridge / "ridge_concentration_selected_best1_h.png",
            ridge / "ridge_p10_p90_delta_vs_turn_selected_h1_v1_hv.png",
            ridge / "ridge_p10_p90_delta_vs_turn_selected_h1_v1_hv_poster.png",
        )
        for path in figure_paths:
            draw_turn_metric_plot(
                path,
                "H",
                [
                    {"plane": "H", "subset_size": "1", "center_turn": 100, "value": -0.1},
                    {"plane": "H", "subset_size": "1", "center_turn": 200, "value": 0.1},
                ],
                ["1"],
                "value",
                "TEST FIGURE",
                "VALUE",
                zero_reference=True,
            )

        csv_file(
            primary / "evolution" / "subset_size_comparison.csv",
            [
                {"plane": plane, "subset_size": size, "subset_score": value + offset}
                for plane, offset in (("H", 0.0), ("V", 0.1))
                for size, value in ((1, 0.3), (3, 0.4), (5, 0.5))
            ],
        )
        csv_file(
            primary / "statistics" / "paired_method_tests.csv",
            [
                {"plane": plane, "comparison": comparison, "median_paired_difference": value + offset}
                for plane, offset in (("H", 0.0), ("V", 0.01))
                for comparison, value in (("best1 vs best3", 0.05), ("best3 vs best5", 0.02))
            ],
        )
        csv_file(
            intensity / "merged_block20" / "intensity_method_effects.csv",
            [
                {
                    "statistical_benefit_pass": "false",
                    "fdr_q_value": 1,
                    "practical_effect_pass": "false",
                    "retain_method_for_tune_analysis": "false",
                }
            ],
        )
        csv_file(
            intensity / "block_sensitivity" / "intensity_block_sensitivity.csv",
            [{"label": "block20", "retained_effects": 0}],
        )

        payload = prepare_publication(primary, followup, best_n, ridge, intensity, payload_audit, publication)
        self.assertEqual(payload["selected_sizes"], {"H": 1, "V": 1})
        self.assertEqual(payload["numeric_summary"]["intensity_effect_count"], 1)
        self.assertEqual(payload["payload_integrity"]["stream_rows"], 263999)
        self.assertEqual(payload["best_n_design"]["validation_spill_plane_count"], 1000)
        content = json.loads((publication / "poster" / "content.json").read_text(encoding="utf-8"))
        self.assertEqual(content["assets"]["ridgeContrast"], "assets/ridge_width_contrast_hv.png")
        self.assertNotIn("selectedSpill", content["assets"])
        self.assertTrue((publication / "poster" / "assets" / "ridge_width_contrast_hv.png").is_file())
        self.assertTrue((publication / "paper" / "figures" / "ridge_width_contrast_hv.png").is_file())
        self.assertIn("IntensityEffectCount}{1}", (publication / "paper" / "results_macros.tex").read_text())
        manifest = read_csv(publication / "source_manifest.csv")
        self.assertTrue(any(row["role"] == "poster:ridge_width_hv_poster" for row in manifest))
        self.assertTrue(any(row["role"] == "paper:ridge_width_hv" for row in manifest))
        audit_path = payload_audit / "delivery_ring_payload_audit.json"
        incomplete_audit = json.loads(audit_path.read_text(encoding="utf-8"))
        incomplete_audit["stream_rows"] = 263998
        audit_path.write_text(json.dumps(incomplete_audit), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match the publication corpus"):
            prepare_publication(primary, followup, best_n, ridge, intensity, payload_audit, publication)

    def test_intensity_block_sensitivity_separates_statistical_and_practical_passes(self) -> None:
        root = self.root / "intensity-block"
        root.mkdir()
        (root / "intensity_method_effects.csv").write_text(
            "subset_size,statistical_benefit_pass,practical_effect_pass,retain_method_for_tune_analysis,"
            "fdr_q_value,bootstrap_block_spills,minimum_practical_effect,bootstrap_ci_low,"
            "bootstrap_ci_high,beneficial_direction,median_paired_delta\n"
            "1,true,false,false,0.01,10,1.0,0.5,0.7,increase,0\n",
            encoding="utf-8",
        )
        summary = summarize_intensity_block_run("block10", root)
        self.assertEqual(summary["fdr_significant_directional_effects"], 1)
        self.assertEqual(summary["practical_ci_passes"], 0)
        self.assertEqual(summary["retained_effects"], 0)
        self.assertEqual(summary["max_directional_ci_fraction_of_mpe"], 0.5)
        validate_intensity_sensitivity_rows([summary])
        reversed_summary = dict(summary)
        reversed_summary["label"] = "block20"
        reversed_summary["retained_effect_keys"] = "H|3|sqrt_intensity|median_power_support_at_train_q"
        with self.assertRaisesRegex(ValueError, "reverse"):
            validate_intensity_sensitivity_rows([summary, reversed_summary])

    def test_legacy_ridge_comparison_reports_descriptive_concentration_gain(self) -> None:
        spill_keys = {("run", str(index)) for index in range(5)}
        legacy = {
            "centers": [100, 200],
            "density": np.zeros((20, 2), dtype=np.float32),
            "grouped": {
                100: [0.641, 0.646, 0.650, 0.654, 0.659],
                200: [0.642, 0.647, 0.650, 0.653, 0.658],
            },
            "spill_keys": spill_keys,
        }
        ensemble = {
            "centers": [100, 200],
            "density": np.zeros((20, 2), dtype=np.float32),
            "grouped": {
                100: [0.6490, 0.6495, 0.6500, 0.6505, 0.6510],
                200: [0.6491, 0.6496, 0.6500, 0.6504, 0.6509],
            },
            "spill_keys": spill_keys,
        }
        metrics = legacy_comparison_metrics("H", "5", legacy, ensemble, (0.62, 0.68), 60)
        self.assertEqual(int(metrics["common_spill_count"]), 5)
        self.assertEqual(float(metrics["fraction_centers_with_narrower_iqr"]), 1.0)
        self.assertLess(float(metrics["median_iqr_delta_ensemble_minus_legacy"]), 0.0)
        self.assertLess(float(metrics["median_density_entropy_delta"]), 0.0)
        self.assertGreater(float(metrics["median_shared_ridge_mass_gain"]), 0.0)
        self.assertLess(float(metrics["median_iqr_delta_ci_high"]), 0.0)
        self.assertGreater(float(metrics["median_shared_ridge_mass_gain_ci_low"]), 0.0)
        self.assertEqual(int(metrics["turn_block_windows"]), 16)

    def test_ridge_subtractive_copy_stays_on_pick_probability(self) -> None:
        density = caption_for_density(
            "H",
            "5",
            {"spill_count": 2000},
            0,
            50000,
            4096,
            256,
            10000,
            20000,
            False,
        )
        difference = caption_for_difference("H", "1", "5", 1988, 357840)
        legacy = caption_for_legacy_difference("H", "5", 1988, 357840)
        self.assertIn("98th percentile", density)
        self.assertIn("99th percentile", difference)
        self.assertIn("99th percentile", legacy)
        for text in (difference, legacy):
            self.assertIn("probability", text.lower())
            self.assertNotIn("suppresses diffuse noise", text.lower())
            self.assertNotIn("cleanly removing noise", text.lower())

    def test_ridge_contrasts_use_exact_common_spill_window_points(self) -> None:
        baseline = keyed_legacy_points(
            [
                ("run", "1", 100, 0.650),
                ("run", "1", 200, 0.651),
                ("run", "2", 100, 0.652),
            ],
            (0.62, 0.68),
        )
        ensemble = keyed_ensemble_points(
            [
                {"run_name": "run", "target_ms": "1", "center_turn": 100, "plane": "H", "selected_tune": 0.6505},
                {"run_name": "run", "target_ms": "2", "center_turn": 100, "plane": "H", "selected_tune": 0.6515},
                {"run_name": "run", "target_ms": "3", "center_turn": 100, "plane": "H", "selected_tune": 0.6495},
            ],
            "H",
            (0.62, 0.68),
        )
        paired_baseline, paired_ensemble = exact_paired_density_results(baseline, ensemble, (0.62, 0.68), 60)
        self.assertEqual(paired_baseline["centers"], [100])
        self.assertEqual(paired_ensemble["centers"], [100])
        self.assertEqual(len(paired_baseline["point_keys"]), 2)
        self.assertEqual(paired_baseline["point_keys"], paired_ensemble["point_keys"])
        self.assertEqual(paired_baseline["spill_keys"], {("run", "1"), ("run", "2")})
        turn_rows = legacy_comparison_by_turn_rows(
            "H",
            "5",
            paired_baseline,
            paired_ensemble,
            (0.62, 0.68),
            60,
        )
        self.assertEqual(len(turn_rows), 1)
        self.assertEqual(int(turn_rows[0]["center_turn"]), 100)
        self.assertEqual(int(turn_rows[0]["paired_ridge_count"]), 2)
        self.assertLess(float(turn_rows[0]["iqr_delta_ensemble_minus_legacy"]), 0.0)
        self.assertLess(float(turn_rows[0]["p10_p90_delta_ensemble_minus_legacy"]), 0.0)
        for field in (
            "peak_bin_fraction_gain",
            "density_entropy_delta",
            "shared_ridge_mass_gain",
        ):
            self.assertTrue(math.isfinite(float(turn_rows[0][field])))
        contrast_path = self.root / "ridge_turn_contrast.png"
        draw_turn_metric_plot(
            contrast_path,
            "H",
            [
                {"plane": "H", "subset_size": "5", "center_turn": 100, "delta": -0.001},
                {"plane": "H", "subset_size": "5", "center_turn": 200, "delta": 0.001},
            ],
            ["5"],
            "delta",
            "PAIRED TURN CONTRAST",
            "DELTA TUNE WIDTH",
            zero_reference=True,
        )
        self.assertEqual(png_dimensions(contrast_path), (1400, 780))
        combined_contrast_path = self.root / "ridge_turn_contrast_hv.png"
        draw_selected_turn_contrast_hv(
            combined_contrast_path,
            [
                {"plane": plane, "subset_size": size, "center_turn": turn, "delta": value}
                for plane, size, offset in (("H", "5", 0.0), ("V", "3", 0.0002))
                for turn, value in ((100, -0.001 + offset), (200, 0.001 + offset))
            ],
            {"H": "5", "V": "3"},
            "delta",
            "SELECTED PAIRED TURN CONTRAST",
            "DELTA TUNE WIDTH",
        )
        self.assertEqual(png_dimensions(combined_contrast_path), (1000, 625))
        portrait_contrast_path = self.root / "ridge_turn_contrast_hv_poster.png"
        draw_selected_turn_contrast_hv(
            portrait_contrast_path,
            [
                {"plane": plane, "subset_size": size, "center_turn": turn, "delta": value}
                for plane, size, offset in (("H", "5", 0.0), ("V", "3", 0.0002))
                for turn, value in ((100, -0.001 + offset), (200, 0.001 + offset))
            ],
            {"H": "5", "V": "3"},
            "delta",
            "SELECTED PAIRED TURN CONTRAST",
            "DELTA TUNE WIDTH",
            portrait=True,
        )
        self.assertEqual(png_dimensions(portrait_contrast_path), (800, 1250))
        v_baseline = keyed_legacy_points(
            [("run", "1", 100, 0.720), ("run", "2", 100, 0.722)],
            (0.69, 0.74),
        )
        v_ensemble = keyed_ensemble_points(
            [
                {"run_name": "run", "target_ms": "1", "center_turn": 100, "plane": "V", "selected_tune": 0.7205},
                {"run_name": "run", "target_ms": "2", "center_turn": 100, "plane": "V", "selected_tune": 0.7215},
            ],
            "V",
            (0.69, 0.74),
        )
        paired_v_baseline, paired_v_ensemble = exact_paired_density_results(
            v_baseline,
            v_ensemble,
            (0.69, 0.74),
            60,
        )
        combined_path = self.root / "ridge_hv.png"
        draw_legacy_pair_hv(
            combined_path,
            "5",
            {
                "H": (paired_baseline, paired_ensemble, (0.62, 0.68)),
                "V": (paired_v_baseline, paired_v_ensemble, (0.69, 0.74)),
            },
        )
        self.assertEqual(png_dimensions(combined_path), (2400, 900))
        selected_path = self.root / "ridge_h5_v3.png"
        draw_legacy_pair_hv_selected(
            selected_path,
            {"H": "5", "V": "3"},
            {
                "H": (paired_baseline, paired_ensemble, (0.62, 0.68)),
                "V": (paired_v_baseline, paired_v_ensemble, (0.69, 0.74)),
            },
        )
        self.assertEqual(png_dimensions(selected_path), (2400, 900))
        h_three_way = exact_paired_density_results_many(
            {"legacy": baseline, "best1": ensemble, "selected": ensemble},
            (0.62, 0.68),
            60,
        )
        v_three_way = exact_paired_density_results_many(
            {"legacy": v_baseline, "best1": v_ensemble, "selected": v_ensemble},
            (0.69, 0.74),
            60,
        )
        self.assertEqual(
            h_three_way["legacy"]["point_keys"],
            h_three_way["best1"]["point_keys"],
        )
        self.assertEqual(len(h_three_way["selected"]["point_keys"]), 2)
        triple_path = self.root / "ridge_legacy_best1_selected_hv.png"
        draw_paired_density_grid_hv(
            triple_path,
            "LEGACY, CORRECTED BEST1, SELECTED BEST-N",
            (
                ("legacy", "LEGACY NORMALIZED-SINGLE"),
                ("best1", "CORRECTED ADAPTIVE BEST1"),
                ("selected", "SELECTED H BEST5 / V BEST3"),
            ),
            {
                "H": (h_three_way, (0.62, 0.68)),
                "V": (v_three_way, (0.69, 0.74)),
            },
        )
        self.assertEqual(png_dimensions(triple_path), (3000, 900))
        direct_path = self.root / "ridge_best1_selected_hv.png"
        draw_paired_density_grid_hv(
            direct_path,
            "CORRECTED BEST1 VS SELECTED BEST-N",
            (
                ("best1", "CORRECTED ADAPTIVE BEST1"),
                ("selected", "SELECTED H BEST5 / V BEST3"),
            ),
            {
                "H": ({key: h_three_way[key] for key in ("best1", "selected")}, (0.62, 0.68)),
                "V": ({key: v_three_way[key] for key in ("best1", "selected")}, (0.69, 0.74)),
            },
        )
        self.assertEqual(png_dimensions(direct_path), (2400, 900))
        with self.assertRaisesRegex(ValueError, "duplicate legacy ridge point"):
            keyed_legacy_points(
                [("run", "1", 100, 0.650), ("run", "1", 100, 0.651)],
                (0.62, 0.68),
            )
        with self.assertRaisesRegex(ValueError, "duplicate ensemble ridge point"):
            keyed_ensemble_points(
                [
                    {"run_name": "run", "target_ms": "1", "center_turn": 100, "plane": "H", "selected_tune": 0.650},
                    {"run_name": "run", "target_ms": "1", "center_turn": 100, "plane": "H", "selected_tune": 0.651},
                ],
                "H",
                (0.62, 0.68),
            )

    def test_ridge_raster_cells_fill_uneven_axes_without_gaps(self) -> None:
        for bounds in (raster_cell_bounds, legacy_raster_cell_bounds):
            forward = [bounds(index, 160, 95, 815) for index in range(160)]
            reverse = [bounds(index, 160, 95, 815, reverse=True) for index in range(160)]
            self.assertEqual(forward[0][0], 95)
            self.assertEqual(forward[-1][1], 815)
            self.assertTrue(all(left[1] + 1 == right[0] for left, right in zip(forward, forward[1:])))
            self.assertEqual(reverse[0][1], 815)
            self.assertEqual(reverse[-1][0], 95)
            self.assertTrue(all(left[0] - 1 == right[1] for left, right in zip(reverse, reverse[1:])))

    def test_ridge_memberships_reject_duplicate_spill_plane_n(self) -> None:
        best_root = self.root / "ridge_membership_best_root"
        (best_root / "manifest").mkdir(parents=True)
        bpm_row = {
            "bpm_index": 0,
            "bpm_name": "digitizer-1",
            "plane": "H",
            "digitizer": "digitizer-1",
            "ring_order": 609,
            "source_key": "{TEST}:HP609:TBT_POSITION_RAW",
        }
        write_csv(best_root / "manifest" / "bpm_index.csv", [bpm_row], list(bpm_row))
        membership_path = self.root / "duplicate_membership.csv"
        membership_row = {
            "collection": "run",
            "spill_id": "spill_1",
            "plane": "H",
            "subset_size": 1,
            "bpm_indices": "0",
            "subset_mask": "1",
            "subset_score": "1",
        }
        write_csv(membership_path, [membership_row, membership_row], list(membership_row))
        with self.assertRaisesRegex(ValueError, "duplicate membership row"):
            load_memberships(best_root, ["1"], membership_path)

    def test_ridge_loss_change_point_is_data_derived(self) -> None:
        rows = []
        for index in range(40):
            early = index < 20
            rows.append(
                {
                    "center_turn": 2048 + index * 256,
                    "peak_bin_fraction": 0.50 if early else 0.20,
                    "iqr_width": 0.002 if early else 0.006,
                    "sample_fraction": 1.0 if early else 0.75,
                }
            )
        change = robust_change_point(rows)
        self.assertTrue(change)
        self.assertGreater(change["peak_drop"], 0.0)
        self.assertGreater(change["iqr_increase"], 0.0)
        self.assertGreaterEqual(change["turn"], 2048 + 17 * 256)
        self.assertLessEqual(change["turn"], 2048 + 22 * 256)

    def test_best_single_uses_pre_normalization_rms_ranking(self) -> None:
        raw = np.asarray(
            [
                [10.0, -10.0, 10.0, -10.0],
                [1.0, -1.0, 1.0, -1.0],
            ],
            dtype=np.float32,
        )
        raw_rms = np.sqrt(np.mean(raw.astype(np.float64) ** 2, axis=1))
        normalized = preprocess_traces(raw, "rms_per_bpm", 4)
        np.testing.assert_allclose(np.sqrt(np.mean(normalized**2, axis=1)), [1.0, 1.0])
        selected, _fraction = select_trace_subset(normalized, "best_single_bpm", raw_rms)
        np.testing.assert_array_equal(selected, normalized[:1])
        audit = legacy_selection_row("run", "spill", "H", ["HP100", "HP101"], normalized, raw_rms)
        self.assertEqual(int(audit["legacy_selected_raw_rms_rank"]), 2)
        self.assertEqual(audit["legacy_selected_is_raw_top1"], "false")

    def test_preprocessing_and_pool_supplements(self) -> None:
        ramp = np.tile(np.linspace(1.0, 5.0, 16, dtype=np.float32), (2, 1))
        detrended = preprocess_window_np(ramp, "linear")
        self.assertLess(float(np.max(np.abs(detrended))), 1e-5)
        bpm_indices = np.asarray([0, 1, 2, 3], dtype=np.int32)
        bpm_meta = {
            0: {"digitizer": "d0", "ring_order": "100"},
            1: {"digitizer": "d0", "ring_order": "200"},
            2: {"digitizer": "d1", "ring_order": "300"},
            3: {"digitizer": "d2", "ring_order": "400"},
        }
        class Score:
            def __init__(self, idx):
                self.subset = (idx,)

        pool = supplement_pool([0], [Score(0), Score(1), Score(2), Score(3)], bpm_indices, bpm_meta, 4)
        self.assertEqual(set(pool), {0, 1, 2, 3})
        self.assertAlmostEqual(float(spearman({"a": 3, "b": 2, "c": 1}, {"a": 30, "b": 20, "c": 10})), 1.0)
        self.assertAlmostEqual(float(kendall_tau({"a": 3, "b": 2, "c": 1}, {"a": 30, "b": 20, "c": 10})), 1.0)

    def test_paired_statistics_emit_permutation_and_fdr(self) -> None:
        rows = []
        for spill in range(8):
            for size, score in (("1", 0.40 + spill * 0.001), ("3", 0.55 + spill * 0.001), ("5", 0.62 + spill * 0.001), ("10", 0.64 + spill * 0.001)):
                rows.append(
                    {
                        "collection": "a",
                        "spill_id": str(spill),
                        "plane": "H",
                        "subset_size": size,
                        "subset_score": str(score),
                    }
                )
        cfg = default_config()
        cfg["statistics"]["permutation_samples"] = 128
        stats = paired_tests(rows, cfg)
        self.assertEqual(len(stats), 3)
        self.assertTrue(all(row["permutation_p_value"] for row in stats))
        self.assertTrue(all(row["fdr_q_value"] for row in stats))
        self.assertTrue(all(row["effect_size"] for row in stats))

    def test_malformed_position_stream_is_rejected_not_dropped(self) -> None:
        collection = self.root / "malformed-positiononly"
        bundle = collection / "spill_1234"
        bundle.mkdir(parents=True)
        manifest = {
            "target_ms": 1234,
            "streams": [
                {
                    "stream_key": "{TEST}:BAD:TBT_POSITION_RAW",
                    "payload_file": "",
                    "sample_count": "",
                }
            ],
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cfg = small_config(collection)
        out = self.root / "malformed_out"
        build_manifest_outputs(cfg, out / "manifest")
        channels = read_csv(out / "manifest" / "channels.csv")
        rejections = read_csv(out / "manifest" / "rejections.csv")
        self.assertEqual(len(channels), 1)
        self.assertTrue(rejections)
        self.assertIn("UNKNOWN_PLANE", rejections[0]["rejection_flags"])
        self.assertIn("MISSING", rejections[0]["rejection_flags"])

    def test_parallel_per_bpm_features_match_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-positiononly"
        synthetic_collection(collection, spills=2, bpms=4)
        cfg = small_config(collection)
        out = self.root / "parallel_extract"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm_serial", workers=1)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm_parallel", workers=2)
        for name in ("per_bpm_window_features.csv", "per_bpm_injection_features.csv", "per_bpm_spill_summary.csv"):
            serial = (out / "per_bpm_serial" / name).read_text(encoding="utf-8")
            parallel = (out / "per_bpm_parallel" / name).read_text(encoding="utf-8")
            self.assertEqual(serial, parallel)

    def test_parallel_consensus_matches_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-consensus-positiononly"
        synthetic_collection(collection, spills=2, bpms=4)
        cfg = small_config(collection)
        out = self.root / "parallel_consensus"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus_serial", out / "cache", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus_parallel", out / "cache", workers=2)
        for name in ("spill_consensus_windows.csv", "spill_consensus_summary.csv", "consensus_class_counts.csv"):
            serial = (out / "consensus_serial" / name).read_text(encoding="utf-8")
            parallel = (out / "consensus_parallel" / name).read_text(encoding="utf-8")
            self.assertEqual(serial, parallel)

    def test_parallel_subset_search_matches_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-search-positiononly"
        synthetic_collection(collection, spills=2, bpms=4)
        cfg = small_config(collection)
        cfg["subset_search"]["random_audit_samples"] = 16
        out = self.root / "parallel_search"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus", out / "cache", workers=1)
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_serial", [1, 3], "cpu", 0, 1)
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_parallel", [1, 3], "cpu", 0, 2)
        for name in ("best1/best1_results.csv", "best1/best1_rankings.csv", "best3/best3_results.csv", "best3/best3_top_candidates.csv", "audit_results.csv"):
            serial = (out / "subset_serial" / name).read_text(encoding="utf-8")
            parallel = (out / "subset_parallel" / name).read_text(encoding="utf-8")
            self.assertEqual(serial, parallel)
        result = read_csv(out / "subset_serial" / "best3" / "best3_results.csv")[0]
        self.assertEqual(len(result["bpm_indices"].split(",")), 3)
        self.assertEqual(len(result["bpm_source_keys"].split(",")), 3)
        self.assertEqual(len(result["bpm_members"].split(",")), 3)
        self.assertTrue(all(member.startswith(("HP", "VP")) for member in result["bpm_members"].split(",")))
        progress = out / "subset_parallel" / "progress"
        self.assertTrue((progress / "parent_status.json").exists())
        self.assertTrue(list(progress.glob("shard_*.json")))

    def test_parallel_evolution_matches_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-evolution-positiononly"
        synthetic_collection(collection, spills=2, bpms=4)
        cfg = small_config(collection)
        cfg["subset_search"]["random_audit_samples"] = 16
        cfg["evolution"] = {"finalist_chunk_rows": 4}
        out = self.root / "parallel_evolution"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus", out / "cache", workers=1)
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_search", [1, 3], "cpu", 0, 1)
        evaluate_evolution(cfg, out / "subset_search", out / "evolution_serial", out / "cache", out / "per_bpm", out / "manifest", workers=1)
        evaluate_evolution(cfg, out / "subset_search", out / "evolution_parallel", out / "cache", out / "per_bpm", out / "manifest", workers=2)
        for name in ("subset_evolution_windows.csv", "subset_evolution_summary.csv", "subset_size_comparison.csv", "finalist_reevaluation.csv"):
            serial = (out / "evolution_serial" / name).read_text(encoding="utf-8")
            parallel = (out / "evolution_parallel" / name).read_text(encoding="utf-8")
            self.assertEqual(serial, parallel)
        progress = out / "evolution_parallel" / "progress"
        self.assertTrue((progress / "parent_status.json").exists())
        self.assertTrue(list(progress.glob("shard_*.json")))

    def test_parallel_fixed_sets_match_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-fixed-positiononly"
        synthetic_collection(collection, spills=2, bpms=5)
        cfg = small_config(collection)
        cfg["subset_search"]["random_audit_samples"] = 16
        out = self.root / "parallel_fixed"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus", out / "cache", workers=1)
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_search", [1, 3], "cpu", 0, 1)
        evaluate_fixed_sets(cfg, out, out / "fixed_serial", workers=1, subset_sizes=[1, 3])
        evaluate_fixed_sets(cfg, out, out / "fixed_parallel", workers=2, subset_sizes=[1, 3])
        for name in ("statistics/fixed_set_direct_evaluation.csv", "statistics/fixed_vs_dynamic_direct_summary.csv"):
            self.assertEqual((out / "fixed_serial" / name).read_text(encoding="utf-8"), (out / "fixed_parallel" / name).read_text(encoding="utf-8"))
        self.assertTrue((out / "fixed_parallel" / "statistics" / "fixed_set_progress" / "parent_status.json").exists())

    def test_parallel_heldout_matches_serial(self) -> None:
        try:
            with ProcessPoolExecutor(max_workers=1) as pool:
                list(pool.map(int, ["1"]))
        except PermissionError as exc:
            self.skipTest(f"process pools unavailable in this sandbox: {exc}")
        collection = self.root / "parallel-heldout-positiononly"
        synthetic_collection(collection, spills=2, bpms=5)
        cfg = small_config(collection)
        cfg["subset_search"]["random_audit_samples"] = 16
        cfg["evolution"] = {"finalist_chunk_rows": 4}
        cfg["heldout"] = {"chunk_rows": 4}
        out = self.root / "parallel_heldout"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm", workers=1)
        build_consensus(cfg, out / "per_bpm", out / "consensus", out / "cache", workers=1)
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_search", [1, 3], "cpu", 0, 1)
        evaluate_evolution(cfg, out / "subset_search", out / "evolution", out / "cache", out / "per_bpm", out / "manifest", workers=1)
        evaluate_heldout_support(cfg, out, out / "heldout_serial", workers=1)
        evaluate_heldout_support(cfg, out, out / "heldout_parallel", workers=2)
        for name in ("evolution/finalist_heldout_spectral_support.csv", "evolution/heldout_spectral_support_summary.csv"):
            self.assertEqual((out / "heldout_serial" / name).read_text(encoding="utf-8"), (out / "heldout_parallel" / name).read_text(encoding="utf-8"))
        heldout_rows = read_csv(out / "heldout_serial" / "evolution" / "finalist_heldout_spectral_support.csv")
        self.assertTrue(heldout_rows)
        self.assertTrue(all(int(row["selected_bpm_count"]) == int(row["subset_size"]) for row in heldout_rows))
        self.assertTrue(all("SELECTED_CHANNEL_COUNT_MISMATCH" not in row["quality_flags"] for row in heldout_rows))
        self.assertTrue((out / "heldout_parallel" / "evolution" / "heldout_progress" / "parent_status.json").exists())

    def test_heldout_no_q_is_explicitly_unevaluable(self) -> None:
        spectra_path = self.root / "heldout_no_q_spectra.npy"
        tune_path = self.root / "heldout_no_q_tune.npy"
        indices_path = self.root / "heldout_no_q_indices.npy"
        np.save(spectra_path, np.ones((3, 2, 16), dtype=np.float32))
        np.save(tune_path, np.linspace(0.60, 0.70, 16, dtype=np.float32))
        np.save(indices_path, np.asarray([0, 1, 2], dtype=np.int64))
        cache = {
            "spectra_path": str(spectra_path),
            "tune_axis_path": str(tune_path),
            "bpm_indices_path": str(indices_path),
        }
        meta = {
            ("H", index): {
                "bpm_index": str(index),
                "bpm_name": f"HP{index:03d}",
                "digitizer": f"digitizer-{index}",
                "source_key": f"{{TEST}}:HP{index:03d}:TBT_POSITION_RAW",
                "plane": "H",
            }
            for index in range(3)
        }
        rows = [{
            "collection": "collection",
            "spill_id": "spill_1",
            "plane": "H",
            "subset_size": "1",
            "subset_mask": "1",
            "bpm_indices": "0",
            "aggregator": "mean_power",
            "source_rank": "1",
            "q_hat": "",
        }]
        result = evaluate_heldout_group(cache, rows, meta, 0.0025)[0]
        self.assertIn("NO_VALID_Q", result["quality_flags"])
        for field in (
            "heldout_candidate_fraction",
            "heldout_power_support",
            "heldout_prominence_at_qhat",
            "selected_power_support",
            "selected_prominence_at_qhat",
            "selected_vs_heldout_delta",
        ):
            self.assertEqual(result[field], "")

    def test_fixed_score_contract_accepts_flagged_no_visible_state(self) -> None:
        row = {
            "visible_fraction": "0",
            "median_prominence": "",
            "score": "0",
            "quality_flags": "NO_VALID_Q|NO_VISIBLE_TUNE",
        }
        self.assertFalse(fixed_score_contract_mismatches([row]))
        self.assertTrue(fixed_score_contract_mismatches([{**row, "quality_flags": ""}]))

    def test_best_bpm_verifier_reports_missing_outputs(self) -> None:
        out = self.root / "incomplete_best_bpm"
        out.mkdir()
        report = verify_best_bpm_outputs(out, subset_sizes=[1, 3], write_outputs=False)
        self.assertEqual(report["status"], "fail")
        self.assertGreater(report["fail_count"], 0)

    def test_ridge_verifier_reports_missing_outputs(self) -> None:
        out = self.root / "incomplete_ridge"
        out.mkdir()
        report = verify_ridge_density_outputs(
            out,
            subset_sizes=[1, 3, 5],
            minimum_spills=1,
            expected_centers=1,
            write_outputs=False,
        )
        self.assertEqual(report["status"], "fail")
        self.assertIsNone(png_dimensions(out / "missing.png"))

    def test_intensity_verifier_reports_missing_outputs(self) -> None:
        out = self.root / "incomplete_intensity"
        gallery = self.root / "incomplete_intensity_gallery"
        out.mkdir()
        gallery.mkdir()
        report = verify_intensity_outputs(
            out,
            gallery,
            subset_sizes=[1, 3],
            expected_paired_payload_rows=1,
            expected_spill_rows=1,
            expected_centers=1,
            minimum_spills_per_group=1,
            write_outputs=False,
        )
        self.assertEqual(report["status"], "fail")

    def test_best_n_disjoint_validation_emits_complete_curve(self) -> None:
        collection = self.root / "best-n-positiononly"
        synthetic_collection(collection, spills=1, bpms=6)
        cfg = small_config(collection)
        root = self.root / "best_n_inputs"
        build_manifest_outputs(cfg, root / "manifest")
        build_spectral_cache(cfg, root / "manifest", root / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, root / "cache", root / "manifest", root / "per_bpm", workers=1)
        build_consensus(cfg, root / "per_bpm", root / "consensus", root / "cache", workers=1)
        shards = self.root / "best_n_shards"
        for shard_index in range(2):
            evaluate_best_n(
                cfg,
                root,
                shards / f"shard_{shard_index}",
                device="cpu",
                max_n=3,
                beam_width=4,
                curve_limit=2,
                validation_limit=2,
                validation_beam_width=3,
                folds=3,
                requested_fit_windows=2,
                bootstrap_samples=100,
                progress_every=0,
                shard_index=shard_index,
                shard_count=2,
            )
        out = self.root / "best_n_outputs"
        merge_best_n_shards(shards, out, bootstrap_samples=100)
        curve = read_csv(out / "best_n_curve_rows.csv")
        validation = read_csv(out / "best_n_disjoint_validation.csv")
        summary = read_csv(out / "best_n_summary.csv")
        self.assertEqual({int(row["subset_size"]) for row in curve}, {1, 2, 3})
        self.assertEqual({int(row["subset_size"]) for row in validation}, {1, 2, 3})
        self.assertTrue(all(len(row["bpm_indices"].split(",")) == int(row["subset_size"]) for row in validation))
        self.assertTrue(all(int(row["test_window_count"]) > 0 for row in validation))
        self.assertTrue(all(row["q_agreement_within_tolerance"] in {"0", "1"} for row in validation))
        self.assertTrue(all(row["blind_q_agreement_within_tolerance"] in {"0", "1"} for row in validation))
        self.assertTrue(all(row["selected_test_blind_q_hat"] for row in validation))
        self.assertTrue(all(row["heldout_blind_q_hat"] for row in validation))
        self.assertEqual({row["plane"] for row in summary}, {"H", "V"})
        self.assertTrue(all(row["blind_q_agreement_rate"] for row in summary))
        self.assertTrue(all(row["median_heldout_power_support"] for row in summary))
        self.assertTrue(all(row["median_heldout_prominence"] for row in summary))
        self.assertTrue(all(int(row["validation_spill_count"]) == 1 for row in summary))
        self.assertTrue((out / "best_n_summary_by_collection.csv").exists())
        self.assertTrue((out / "best_n_cross_collection_transfer.csv").exists())
        self.assertTrue((out / "best_n_cross_collection_transfer.md").exists())
        verification = verify_best_n_outputs(
            out,
            expected_max_n=3,
            expected_curve_cache_keys=2,
            expected_validation_cache_keys=2,
            expected_folds=3,
            require_cross_collection=False,
        )
        self.assertEqual(verification["status"], "pass")
        self.assertTrue((out / "best_n_verification.md").exists())
        contract = json.loads((out / "run_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["analysis"], "best_n_merged")
        self.assertEqual(contract["source_shard_indices"], [0, 1])
        with self.assertRaisesRegex(ValueError, "run contract mismatch"):
            evaluate_best_n(
                cfg,
                root,
                shards / "shard_0",
                device="cpu",
                max_n=3,
                beam_width=5,
                curve_limit=2,
                validation_limit=2,
                validation_beam_width=3,
                folds=3,
                requested_fit_windows=2,
                bootstrap_samples=100,
                progress_every=0,
                shard_index=0,
                shard_count=2,
                resume=True,
            )
        write_csv(out / "best_n_curve_rows.csv", curve[:-1], list(curve[0]))
        incomplete = verify_best_n_outputs(
            out,
            expected_max_n=3,
            expected_curve_cache_keys=2,
            expected_validation_cache_keys=2,
            expected_folds=3,
            require_cross_collection=False,
            write_outputs=False,
        )
        self.assertEqual(incomplete["status"], "fail")
        self.assertTrue(any(issue["code"] == "curve_noncontiguous_n" for issue in incomplete["issues"]))
        shard_zero_curve = read_csv(shards / "shard_0" / "best_n_curve_rows.csv")
        shard_one_curve = read_csv(shards / "shard_1" / "best_n_curve_rows.csv")
        write_csv(
            shards / "shard_1" / "best_n_curve_rows.csv",
            [*shard_one_curve, shard_zero_curve[0]],
            list(shard_zero_curve[0]),
        )
        with self.assertRaisesRegex(ValueError, "duplicate Best-N curve key"):
            merge_best_n_shards(shards, self.root / "best_n_duplicate_merge", bootstrap_samples=100)

    def test_end_to_end_small_pipeline(self) -> None:
        collection = self.root / "synthetic-positiononly"
        synthetic_collection(collection)
        cfg = small_config(collection)
        out = self.root / "best_bpm_mining"
        build_manifest_outputs(cfg, out / "manifest")
        build_spectral_cache(cfg, out / "manifest", out / "cache", "cpu", 1, False)
        extract_per_bpm_features(cfg, out / "cache", out / "manifest", out / "per_bpm")
        build_consensus(cfg, out / "per_bpm", out / "consensus")
        search_best_bpm_subsets(cfg, out / "cache", out / "manifest", out / "per_bpm", out / "consensus", out / "subset_search", [1, 3, 5])
        evaluate_evolution(cfg, out / "subset_search", out / "evolution", out / "cache", out / "per_bpm", out / "manifest")
        aggregate_statistics(cfg, out, out / "manifest", out / "statistics")
        cluster_spills(cfg, out, out / "clustering")
        select_artifacts(cfg, out, out / "artifact_selection")
        make_artifacts(cfg, out, out / "artifact_selection" / "artifact_manifest.csv", out / "artifacts")
        make_report(cfg, out, out / "reports")
        follow = out / "followups"
        evaluate_fixed_sets(cfg, out, follow, workers=1, subset_sizes=[1, 3, 5])
        evaluate_heldout_support(cfg, out, follow, workers=1, limit=16)
        make_artifacts(cfg, out, out / "artifact_selection" / "artifact_manifest.csv", follow / "artifacts", workers=1)
        run_handoff_analysis(cfg, out, follow, workers=1, limit=2)

        self.assertEqual(len(read_csv(out / "manifest" / "spills.csv")), 3)
        best1 = read_csv(out / "subset_search" / "best1" / "best1_results.csv")
        best1_rankings = read_csv(out / "subset_search" / "best1" / "best1_rankings.csv")
        best3 = read_csv(out / "subset_search" / "best3" / "best3_results.csv")
        best5 = read_csv(out / "subset_search" / "best5" / "best5_results.csv")
        audits = read_csv(out / "subset_search" / "audit_results.csv")
        self.assertTrue(best1)
        self.assertGreater(len(best1_rankings), len(best1))
        self.assertTrue(best3)
        self.assertTrue(best5)
        self.assertTrue(all(len(row["bpm_indices"].split(",")) == int(row["subset_size"]) for row in best1 + best3 + best5))
        self.assertTrue(all(len(row["bpm_source_keys"].split(",")) == int(row["subset_size"]) for row in best1 + best3 + best5))
        self.assertTrue(audits[0]["screened_winner_score"])
        self.assertEqual(best3[0]["search_scope"], "FULL_60")
        self.assertEqual(best5[0]["search_scope"], "SCREENED_POOL")
        global_statistics = read_csv(out / "statistics" / "bpm_global_statistics.csv")
        self.assertTrue(global_statistics)
        self.assertTrue(all(row["ring_order"] for row in global_statistics))
        cluster_summary = read_csv(out / "clustering" / "cluster_summary.csv")
        self.assertTrue(cluster_summary)
        self.assertTrue(all(row["median_score_h"] and row["median_score_v"] for row in cluster_summary))
        self.assertTrue(read_csv(out / "evolution" / "finalist_reevaluation.csv"))
        heldout_rows = read_csv(follow / "evolution" / "finalist_heldout_spectral_support.csv")
        self.assertTrue(heldout_rows)
        self.assertTrue(all(int(row["selected_bpm_count"]) == int(row["subset_size"]) for row in heldout_rows))
        self.assertTrue(all("SELECTED_CHANNEL_COUNT_MISMATCH" not in row["quality_flags"] for row in heldout_rows))
        fixed_rows = read_csv(follow / "statistics" / "fixed_set_direct_evaluation.csv")
        self.assertFalse(fixed_score_contract_mismatches(fixed_rows))
        dynamic_fixed_rows = [row for row in fixed_rows if row["method"].startswith("dynamic_best")]
        self.assertTrue(dynamic_fixed_rows)
        self.assertTrue(all(row["spectral_config"] and row["median_prominence"] for row in dynamic_fixed_rows))
        for row in fixed_rows:
            expected_score = float(row["visible_fraction"]) * max(
                0.0,
                min(1.0, float(row["median_prominence"]) / 12.0),
            )
            self.assertAlmostEqual(float(row["score"]), expected_score, places=7)
        for plane in ("h", "v"):
            self.assertTrue((follow / "artifacts" / "global" / f"fixed_vs_dynamic_direct_{plane}.png").exists())
            self.assertTrue((follow / "artifacts" / "global" / f"fixed_vs_dynamic_direct_{plane}_caption.md").exists())
        report = (out / "reports" / "strong_bpm_analysis_summary.md").read_text(encoding="utf-8")
        self.assertIn("The machine tune may vary freely between spills", report)
        self.assertIn("best-5 are not globally exhaustive", report)
        self.assertNotIn("best-10 are not globally exhaustive", report)
        exec_report = (out / "reports" / "strong_bpm_executive_summary.md").read_text(encoding="utf-8")
        self.assertIn("Completed subset sizes: `1,3,5`", exec_report)
        self.assertNotIn("best-10", exec_report)
        self.assertTrue((out / "artifacts" / "poster" / "selected_poster_artifacts.csv").exists())
        self.assertTrue((out / "artifacts" / "poster" / "poster_artifact_index.md").exists())
        for artifact_name in (
            "global_topn_performance_hv.png",
            "global_bpm_inclusion_h.png",
            "global_bpm_inclusion_v.png",
            "poster_contact_sheet.png",
        ):
            artifact_path = out / "artifacts" / "poster" / artifact_name
            self.assertTrue(artifact_path.exists() or artifact_path.with_suffix(".txt").exists())
        poster_rows = read_csv(out / "artifacts" / "poster" / "selected_poster_artifacts.csv")
        self.assertLessEqual(len(poster_rows), 8)
        self.assertEqual({row["plane"] for row in poster_rows}, {"H", "V"})
        self.assertTrue(all("spill_spill_" not in row["recommended_files"] for row in poster_rows))
        self.assertTrue((follow / "artifacts" / "poster" / "selected_poster_artifacts.csv").exists())
        handoff_dir = follow / "handoff"
        visibility_rows = read_csv(handoff_dir / "bpm_window_visibility.csv")
        self.assertTrue(visibility_rows)
        self.assertTrue(all(row["is_top10_visible"] in {"true", "false"} for row in visibility_rows))
        for plane in ("h", "v"):
            for stem in (
                "handoff_rate_vs_turn",
                "visible_bpm_fraction_vs_turn",
                "visible_set_support_vs_turn",
                "bpm_visibility_cluster_map",
                "top_bpm_membership_vs_turn",
            ):
                self.assertTrue((handoff_dir / f"{stem}_{plane}.png").exists())
                self.assertTrue((handoff_dir / f"{stem}_{plane}_caption.md").exists())
        handoff_keys = {
            (row["spill_id"], row["plane"].lower())
            for row in visibility_rows
        }
        for spill_id, plane in handoff_keys:
            stem = f"spill_{spill_id}_{plane}"
            self.assertTrue((handoff_dir / f"{stem}_bpm_visibility_handoff.png").exists())
            self.assertTrue((handoff_dir / f"{stem}_top_sets_vs_turn.png").exists())
        best3[0]["visibility_duration_turns"] = "999999"
        write_csv(
            out / "subset_search" / "best3" / "best3_results.csv",
            best3,
            list(best3[0]),
        )
        repair_visibility_durations(
            cfg,
            out,
            out / "visibility_duration_repair_test",
            [1, 3, 5],
        )
        best3 = read_csv(out / "subset_search" / "best3" / "best3_results.csv")
        self.assertNotEqual(best3[0]["visibility_duration_turns"], "999999")
        self.assertTrue((out / "visibility_duration_repair_test" / "visibility_duration_repair.json").exists())
        verification = verify_best_bpm_outputs(out, subset_sizes=[1, 3, 5], write_outputs=False)
        self.assertEqual(verification["status"], "ok")
        followup_verification = verify_best_bpm_followups(follow, write_outputs=False)
        self.assertEqual(followup_verification["status"], "ok", followup_verification)
        best3[0]["bpm_source_keys"] = "wrong-source-key"
        write_csv(
            out / "subset_search" / "best3" / "best3_results.csv",
            best3,
            list(best3[0]),
        )
        corrupted = verify_best_bpm_outputs(out, subset_sizes=[1, 3, 5], write_outputs=False)
        self.assertEqual(corrupted["status"], "fail")
        identity_check = next(
            check for check in corrupted["checks"] if check["kind"] == "cross-table identity"
        )
        self.assertTrue(any("bpm_source_keys" in message for message in identity_check["messages"]))


if __name__ == "__main__":
    unittest.main()
