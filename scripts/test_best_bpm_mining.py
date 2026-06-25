#!/usr/bin/env python3
"""Stdlib tests for the best-BPM mining pipeline."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from bpm_mining.config import default_config
from bpm_mining.consensus import cluster_candidates, weighted_median
from bpm_mining.io import build_manifest_outputs, read_csv
from bpm_mining.peaks import extract_candidates
from bpm_mining.preprocessing import preprocess_window_np
from bpm_mining.spectra import build_spectral_cache, compute_spectra, tune_axis_for
from bpm_mining.subset_score import combination_array, subset_mask
from bpm_mining.subset_search import supplement_pool
from bpm_mining.peaks import extract_per_bpm_features
from bpm_mining.consensus import build_consensus
from bpm_mining.subset_search import search_best_bpm_subsets
from bpm_mining.evolution import evaluate_evolution
from bpm_mining.statistics import aggregate_statistics
from bpm_mining.statistics import spearman, kendall_tau, paired_tests
from bpm_mining.clustering import cluster_spills
from bpm_mining.artifact_selection import select_artifacts
from bpm_mining.plots import make_artifacts
from bpm_mining.report import make_report
from bpm_mining.verification import verify_best_bpm_outputs


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
        progress = out / "subset_parallel" / "progress"
        self.assertTrue((progress / "parent_status.json").exists())
        self.assertTrue(list(progress.glob("shard_*.json")))

    def test_best_bpm_verifier_reports_missing_outputs(self) -> None:
        out = self.root / "incomplete_best_bpm"
        out.mkdir()
        report = verify_best_bpm_outputs(out, subset_sizes=[1, 3], write_outputs=False)
        self.assertEqual(report["status"], "fail")
        self.assertGreater(report["fail_count"], 0)

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
        self.assertTrue(audits[0]["screened_winner_score"])
        self.assertEqual(best3[0]["search_scope"], "FULL_60")
        self.assertEqual(best5[0]["search_scope"], "SCREENED_POOL")
        self.assertTrue((out / "statistics" / "bpm_global_statistics.csv").exists())
        self.assertTrue(read_csv(out / "evolution" / "finalist_reevaluation.csv"))
        report = (out / "reports" / "strong_bpm_analysis_summary.md").read_text(encoding="utf-8")
        self.assertIn("The machine tune may vary freely between spills", report)
        self.assertIn("Best-5 and best-10 are not globally exhaustive", report)
        verification = verify_best_bpm_outputs(out, subset_sizes=[1, 3, 5], write_outputs=False)
        self.assertEqual(verification["status"], "ok")


if __name__ == "__main__":
    unittest.main()
