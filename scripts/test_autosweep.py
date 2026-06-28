#!/usr/bin/env python3
"""Stdlib tests for BPM autosweep helper scripts."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import build_collection_manifest
import build_elite_full_stage
import build_spill_cache
import gpu_run_telemetry
import gpu_analyze_captured_spills
import make_elite_full_summary
import make_initial_analysis_summary
import rank_autosweep_results
import run_autosweep
import validate_spill_integrity


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


class AutosweepScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="autosweep-test-")
        self.root = Path(self.tmp.name)
        self.collection = self.root / "synthetic-positiononly-run"
        self.collection.mkdir()
        gpu_analyze_captured_spills.make_synthetic_bundle(self.collection, 1000, 0.681, 0.713, turns=2048)
        gpu_analyze_captured_spills.make_synthetic_bundle(self.collection, 2000, 0.684, 0.709, turns=2048)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_manifest_health_and_cache(self) -> None:
        out = self.root / "stage0"
        build_collection_manifest.main(["--roots", str(self.collection), "--out", str(out)])
        manifest = out / "dataset_manifest.csv"
        rows = read_csv(manifest)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tier"], "TierA")
        self.assertEqual(rows[0]["h_bpm_count"], "4")
        self.assertEqual(rows[0]["v_bpm_count"], "4")
        self.assertEqual(rows[0]["waveform_length"], "2048")

        validate_spill_integrity.main(["--manifest", str(manifest), "--out", str(out)])
        health_rows = read_csv(out / "spill_health.csv")
        self.assertEqual(len(health_rows), 2)
        self.assertEqual(health_rows[0]["usable_data_flag"], "true")

        build_spill_cache.main(["--manifest", str(manifest), "--health", str(out / "spill_health.csv"), "--out", str(out)])
        self.assertTrue((out / "spill_cache_index.json").exists())

    def test_config_hash_grid_and_skip_reason(self) -> None:
        cfg = run_autosweep.base_config("stable")
        hashed1 = run_autosweep.with_hash(cfg)
        hashed2 = run_autosweep.with_hash(dict(reversed(list(cfg.items()))))
        self.assertEqual(hashed1["config_hash"], hashed2["config_hash"])
        grid = run_autosweep.pilot_configs(20, 20260613)
        self.assertLessEqual(len(grid), 20)
        reason = run_autosweep.skip_reason(hashed1, [{"waveform_length": "2048", "manifest_path": "x"}])
        self.assertEqual(reason, "turn_range_exceeds_waveform_length")
        handoff = self.root / "handoff.csv"
        run_autosweep.write_csv(handoff, [hashed1], run_autosweep.CONFIG_FIELDS)
        loaded = run_autosweep.load_config_list(handoff)
        self.assertEqual(loaded[0]["config_hash"], hashed1["config_hash"])

    def test_effective_config_hash_ignores_names_and_hann_multitaper(self) -> None:
        cfg1 = run_autosweep.base_config("same_knobs_a")
        cfg2 = dict(cfg1)
        cfg2.update({"stage": "interaction", "config_name": "same_knobs_b", "multitaper_nw": 9.0, "multitaper_k": 99})
        row1 = {key: str(value) for key, value in run_autosweep.with_hash(cfg1).items()}
        row2 = {key: str(value) for key, value in run_autosweep.with_hash(cfg2).items()}
        self.assertNotEqual(row1["config_hash"], row2["config_hash"])
        self.assertEqual(build_elite_full_stage.effective_config_hash(row1), build_elite_full_stage.effective_config_hash(row2))

    def test_dry_run_log(self) -> None:
        out = self.root / "stage0"
        build_collection_manifest.main(["--roots", str(self.collection), "--out", str(out)])
        run_out = self.root / "autosweep"
        run_autosweep.main(
            [
                "--dataset",
                str(out / "dataset_manifest.csv"),
                "--mode",
                "pilot",
                "--spills",
                "1",
                "--max-configs",
                "3",
                "--out",
                str(run_out),
                "--dry-run",
                "--parallel-jobs",
                "2",
            ]
        )
        rows = read_csv(run_out / "autosweep_run_log.csv")
        self.assertTrue(rows)
        self.assertTrue(any(row["status"] in {"dry_run", "skipped"} for row in rows))
        self.assertTrue((run_out / "autosweep_config_grid.csv").exists())

    def test_gpu_telemetry_summary(self) -> None:
        telemetry = self.root / "gpu_telemetry.csv"
        with telemetry.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=gpu_run_telemetry.GPU_TELEMETRY_FIELDS)
            writer.writeheader()
            writer.writerow(
                {
                    "epoch": "100",
                    "iso_time": "1970-01-01T00:01:40+0000",
                    "gpu_timestamp": "1970/01/01 00:01:40",
                    "gpu_index": "0",
                    "gpu_name": "TEST",
                    "utilization_gpu_pct": "50",
                    "utilization_memory_pct": "10",
                    "power_draw_w": "100",
                    "compute_apps": "1, python, 100",
                }
            )
            writer.writerow(
                {
                    "epoch": "130",
                    "iso_time": "1970-01-01T00:02:10+0000",
                    "gpu_timestamp": "1970/01/01 00:02:10",
                    "gpu_index": "0",
                    "gpu_name": "TEST",
                    "utilization_gpu_pct": "100",
                    "utilization_memory_pct": "20",
                    "power_draw_w": "200",
                    "compute_apps": "1, python, 100",
                }
            )
        summary = gpu_run_telemetry.write_summary(telemetry, self.root / "gpu_summary.json", self.root / "gpu_summary.md")
        self.assertEqual(summary.sample_count, 2)
        self.assertAlmostEqual(summary.average_gpu_utilization_pct, 75.0)
        self.assertTrue((self.root / "gpu_summary.json").exists())
        self.assertTrue((self.root / "gpu_summary.md").exists())

    def test_full_mode_uses_exact_config_list(self) -> None:
        out = self.root / "stage0"
        build_collection_manifest.main(["--roots", str(self.collection), "--out", str(out)])
        cfg = run_autosweep.base_config("only_full_config")
        cfg.update({"turn_end": 2048, "window": 512, "stride": 256})
        cfg = run_autosweep.with_hash(cfg)
        config_list = self.root / "elite_configs_for_full.csv"
        run_autosweep.write_csv(config_list, [cfg], run_autosweep.CONFIG_FIELDS)
        run_out = self.root / "full-exact"
        run_autosweep.main(
            [
                "--dataset",
                str(out / "dataset_manifest.csv"),
                "--mode",
                "full",
                "--config-list",
                str(config_list),
                "--out",
                str(run_out),
                "--dry-run",
            ]
        )
        grid = read_csv(run_out / "autosweep_config_grid.csv")
        self.assertEqual(len(grid), 1)
        self.assertEqual(grid[0]["config_hash"], cfg["config_hash"])

    def test_elite_selection_outputs_and_poster_safe_filter(self) -> None:
        out = self.root / "stage0"
        build_collection_manifest.main(["--roots", str(self.collection), "--out", str(out)])
        validate_spill_integrity.main(["--manifest", str(out / "dataset_manifest.csv"), "--out", str(out)])

        def cfg_row(name: str, plane: str, combo: str, label: str, physics: float, poster: float, robust: float) -> dict[str, object]:
            cfg = run_autosweep.base_config(name)
            cfg.update({"turn_end": 2048, "bpm_combination": combo})
            if name != "hann_2048_256_mean_medium":
                cfg.update({"window": 512, "stride": 256})
            row: dict[str, object] = run_autosweep.with_hash(cfg)
            row.update(
                {
                    "collection_view": "combined",
                    "plane": plane,
                    "config_label": label,
                    "overall_score": physics,
                    "poster_score": poster,
                    "physics_score": physics,
                    "bpm_robustness_score": robust,
                    "rejection_reason": "" if label not in {"TOO_SLOW", "UNSTABLE_H", "UNSTABLE_V", "OVERFITS_BAND"} else label,
                }
            )
            return row

        rows = [
            cfg_row("h_top", "H", "best_single_bpm", "RECOMMENDED", 0.91, 0.80, 0.70),
            cfg_row("h_robust", "H", "top10_by_confidence", "RECOMMENDED", 0.86, 0.78, 0.95),
            cfg_row("h_median", "H", "median", "RECOMMENDED", 0.84, 0.76, 0.72),
            cfg_row("hann_2048_256_mean_medium", "H", "mean", "RECOMMENDED", 0.60, 0.55, 0.50),
            cfg_row("v_top", "V", "best_single_bpm", "RECOMMENDED", 0.90, 0.79, 0.68),
            cfg_row("v_robust", "V", "top10_by_confidence", "RECOMMENDED", 0.83, 0.75, 0.93),
            cfg_row("v_trimmed", "V", "trimmed_mean_10pct", "RECOMMENDED", 0.82, 0.74, 0.70),
            cfg_row("hann_2048_256_mean_medium", "V", "mean", "RECOMMENDED", 0.59, 0.54, 0.49),
            cfg_row("unsafe_poster", "H", "mean", "TOO_SLOW", 0.50, 0.99, 0.50),
            cfg_row("safe_poster", "V", "mean", "PROMISING", 0.72, 0.88, 0.65),
        ]
        pilot = self.root / "pilot"
        run_autosweep.write_csv(pilot / "autosweep_ranked_configs.csv", rows, rank_autosweep_results.CONFIG_SCORE_FIELDS)
        run_autosweep.write_csv(pilot / "top_configs_for_full.csv", rows[:2], rank_autosweep_results.CONFIG_SCORE_FIELDS)
        elite = self.root / "elite"
        build_elite_full_stage.main(
            [
                "--pilot-dir",
                str(pilot),
                "--dataset",
                str(out / "dataset_manifest.csv"),
                "--health",
                str(out / "spill_health.csv"),
                "--out",
                str(elite),
                "--expected-usable-spills",
                "2",
            ]
        )
        sources = read_csv(elite / "elite_config_sources.csv")
        roles = {(row["plane"], row["selection_role"]) for row in sources}
        self.assertIn(("H", "top_physics"), roles)
        self.assertIn(("H", "top10_robust"), roles)
        self.assertIn(("H", "median_or_trimmed"), roles)
        self.assertIn(("H", "baseline_mean"), roles)
        self.assertIn(("V", "top_physics"), roles)
        self.assertIn(("V", "top10_robust"), roles)
        self.assertIn(("V", "median_or_trimmed"), roles)
        self.assertIn(("V", "baseline_mean"), roles)
        self.assertIn(("combined", "poster_best"), roles)
        poster = [row for row in sources if row["selection_role"] == "poster_best"][0]
        self.assertEqual(poster["config_name"], "safe_poster")
        rejected = read_csv(elite / "elite_rejected_config_diagnostics.csv")
        self.assertTrue(any(row["config_name"] == "unsafe_poster" for row in rejected))
        full = read_csv(elite / "elite_configs_for_full.csv")
        self.assertTrue(full)
        self.assertEqual(len(read_csv(elite / "elite_dataset_manifest.csv")), 2)
        run_autosweep.write_csv(elite / "autosweep_ranked_configs.csv", rows, rank_autosweep_results.CONFIG_SCORE_FIELDS)
        run_autosweep.write_csv(elite / "autosweep_run_log.csv", [], run_autosweep.RUN_FIELDS)
        make_elite_full_summary.main(["--elite-dir", str(elite)])
        self.assertTrue((elite / "elite_full_summary.md").exists())
        self.assertTrue((elite / "elite_artifacts_manifest.csv").exists())

    def test_job_timeout_skips_remaining_config_views(self) -> None:
        out = self.root / "stage0"
        build_collection_manifest.main(["--roots", str(self.collection), "--out", str(out)])
        sleepy = self.root / "sleepy_analyzer.py"
        sleepy.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
        old_analyzer = run_autosweep.ANALYZER
        old_pilot_configs = run_autosweep.pilot_configs
        cfg = run_autosweep.base_config("timeout_unit")
        cfg.update({"turn_range": "unit_2048", "turn_end": 2048, "window": 512, "stride": 256})
        cfg = run_autosweep.with_hash(cfg)
        run_autosweep.ANALYZER = sleepy
        run_autosweep.pilot_configs = lambda max_configs, seed: [cfg]
        try:
            run_out = self.root / "autosweep-timeout"
            run_autosweep.main(
                [
                    "--dataset",
                    str(out / "dataset_manifest.csv"),
                    "--mode",
                    "pilot",
                    "--spills",
                    "1",
                    "--max-configs",
                    "1",
                    "--out",
                    str(run_out),
                    "--job-timeout-seconds",
                    "1",
                ]
            )
        finally:
            run_autosweep.ANALYZER = old_analyzer
            run_autosweep.pilot_configs = old_pilot_configs

        rows = read_csv(run_out / "autosweep_run_log.csv")
        self.assertEqual(rows[0]["status"], "failed:timeout")
        self.assertEqual(rows[0]["skip_reason"], "job_timeout_seconds=1")
        self.assertTrue(any(row["status"] == "skipped" and row["skip_reason"] == "prior_view_too_slow" for row in rows[1:]))

    def test_analyze_rank_and_summary_smoke(self) -> None:
        manifest_list = self.root / "manifests.txt"
        manifests = sorted(str(path) for path in self.collection.rglob("manifest.json"))
        manifest_list.write_text("\n".join(manifests) + "\n", encoding="utf-8")
        job_out = self.root / "autosweep" / "jobs" / "testconfig" / "combined"
        args = gpu_analyze_captured_spills.build_parser().parse_args(
            [
                "--manifest-list",
                str(manifest_list),
                "--out",
                str(job_out),
                "--device",
                "cpu",
                "--progress",
                "0",
                "--stride-mode",
                "--turn-start",
                "0",
                "--turn-end",
                "2048",
                "--sliding-window-turns",
                "512",
                "--sliding-stride-turns",
                "256",
                "--injection-window-turns",
                "512",
                "--spectrogram-method",
                "hann",
                "--bpm-combination",
                "mean",
                "--bpm-normalization",
                "rms_per_bpm",
                "--detrend",
                "mean_subtract",
                "--dc-handling",
                "zero_dc_bin",
                "--ridge-anchor-enabled",
                "true",
                "--qx-min",
                "0.62",
                "--qx-max",
                "0.70",
                "--qy-min",
                "0.69",
                "--qy-max",
                "0.74",
                "--no-spectrogram",
            ]
        )
        gpu_analyze_captured_spills.analyze(gpu_analyze_captured_spills.normalize_args(args))
        self.assertTrue((job_out / "bpm_leaderboard.csv").exists())
        self.assertTrue((job_out / "bpm_leaderboard_h.png").exists())
        self.assertTrue((job_out / "subset_consistency_h.png").exists())

        cfg = run_autosweep.base_config("unit")
        cfg.update(
            {
                "turn_end": 2048,
                "window": 512,
                "stride": 256,
                "qx_min": 0.62,
                "qx_max": 0.70,
                "qy_min": 0.69,
                "qy_max": 0.74,
            }
        )
        cfg = run_autosweep.with_hash(cfg)
        autosweep = self.root / "autosweep"
        run_autosweep.write_csv(autosweep / "autosweep_config_grid.csv", [cfg], run_autosweep.CONFIG_FIELDS)
        run_autosweep.write_csv(
            autosweep / "autosweep_run_log.csv",
            [
                {
                    "job_id": 1,
                    "config_hash": cfg["config_hash"],
                    "collection_view": "combined",
                    "mode": "pilot",
                    "status": "ok",
                    "skip_reason": "",
                    "started_utc": "2026-06-13T00:00:00Z",
                    "elapsed_seconds": "1.0",
                    "spill_count": "2",
                    "out_dir": str(job_out),
                    "manifest_list": str(manifest_list),
                    "command": "unit",
                }
            ],
            run_autosweep.RUN_FIELDS,
        )
        rank_autosweep_results.main(["--autosweep-dir", str(autosweep), "--out", str(autosweep)])
        ranked = read_csv(autosweep / "autosweep_ranked_configs.csv")
        self.assertTrue(ranked)
        self.assertIn(ranked[0]["config_label"], {"RECOMMENDED", "PROMISING", "EXPLORATORY", "REJECTED", "UNSTABLE_H", "UNSTABLE_V", "OVERFITS_BAND", "TOO_SLOW"})
        top_full = read_csv(autosweep / "top_configs_for_full.csv")
        self.assertTrue(top_full)
        for field in run_autosweep.CONFIG_FIELDS:
            self.assertIn(field, top_full[0])

        make_initial_analysis_summary.main(["--ranking-dir", str(autosweep), "--out", str(autosweep), "--top", "3"])
        self.assertTrue((autosweep / "initial_analysis_summary.md").exists())
        self.assertTrue((autosweep / "plots" / "ranked_config_scoreboard.png").exists())


if __name__ == "__main__":
    unittest.main()
