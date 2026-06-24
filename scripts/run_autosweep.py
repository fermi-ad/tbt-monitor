#!/usr/bin/env python3
"""Run staged BPM autosweep jobs over captured spill manifests."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
ANALYZER = ROOT / "gpu_analyze_captured_spills.py"

CONFIG_FIELDS = [
    "config_hash",
    "stage",
    "config_name",
    "turn_range",
    "turn_start",
    "turn_end",
    "window",
    "stride",
    "spectrogram_method",
    "multitaper_nw",
    "multitaper_k",
    "bpm_combination",
    "bpm_normalization",
    "detrend",
    "dc_handling",
    "ridge_method",
    "ridge_jump_penalty",
    "ridge_jump2_penalty",
    "ridge_max_step",
    "ridge_anchor_enabled",
    "tune_band",
    "qx_min",
    "qx_max",
    "qy_min",
    "qy_max",
    "enable_tracking",
]

RUN_FIELDS = [
    "job_id",
    "config_hash",
    "collection_view",
    "mode",
    "status",
    "skip_reason",
    "started_utc",
    "elapsed_seconds",
    "spill_count",
    "out_dir",
    "manifest_list",
    "command",
]


TUNE_BANDS = {
    "broad": {"qx": (0.58, 0.74), "qy": (0.58, 0.74)},
    "medium": {"qx": (0.62, 0.68), "qy": (0.69, 0.74)},
    "narrow": {"qx": (0.63, 0.67), "qy": (0.70, 0.74)},
}

TURN_RANGES = {
    "full_15000": (0, 15000),
    "full_50000": (0, 50000),
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def config_hash(config: dict[str, object]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def with_hash(config: dict[str, object]) -> dict[str, object]:
    out = dict(config)
    out["config_hash"] = config_hash({k: v for k, v in out.items() if k != "config_hash"})
    return out


def base_config(name: str) -> dict[str, object]:
    band = TUNE_BANDS["medium"]
    return {
        "stage": "baseline",
        "config_name": name,
        "turn_range": "full_50000",
        "turn_start": 0,
        "turn_end": 50000,
        "window": 2048,
        "stride": 256,
        "spectrogram_method": "hann",
        "multitaper_nw": 2.5,
        "multitaper_k": 4,
        "bpm_combination": "mean",
        "bpm_normalization": "rms_per_bpm",
        "detrend": "mean_subtract",
        "dc_handling": "zero_dc_bin",
        "ridge_method": "greedy",
        "ridge_jump_penalty": 500,
        "ridge_jump2_penalty": 20000,
        "ridge_max_step": 0.010,
        "ridge_anchor_enabled": "true",
        "tune_band": "medium",
        "qx_min": band["qx"][0],
        "qx_max": band["qx"][1],
        "qy_min": band["qy"][0],
        "qy_max": band["qy"][1],
        "enable_tracking": "true",
    }


def baseline_configs() -> list[dict[str, object]]:
    configs = []
    cfg = base_config("hann_2048_256_mean_medium")
    configs.append(cfg)
    cfg = base_config("hann_4096_256_mean_medium")
    cfg.update({"window": 4096})
    configs.append(cfg)
    cfg = base_config("multitaper_4096_256_mean_medium")
    cfg.update({"window": 4096, "spectrogram_method": "multitaper", "ridge_method": "dp"})
    configs.append(cfg)
    return [with_hash(c) for c in configs]


def factor_screen_configs() -> list[dict[str, object]]:
    base = base_config("factor_base")
    configs: list[dict[str, object]] = []
    for window, stride in [(1024, 128), (1024, 256), (2048, 128), (2048, 256), (2048, 512), (4096, 128), (4096, 256), (4096, 512), (8192, 512), (8192, 1024)]:
        cfg = dict(base, stage="factor", config_name=f"window_{window}_stride_{stride}", window=window, stride=stride)
        configs.append(cfg)
    for method, nw, k in [("hann", 2.5, 4), ("multitaper", 2.0, 3), ("multitaper", 2.5, 4), ("multitaper", 3.0, 5)]:
        cfg = dict(base, stage="factor", config_name=f"spectral_{method}_nw{nw}_k{k}", spectrogram_method=method, multitaper_nw=nw, multitaper_k=k)
        configs.append(cfg)
    for combo in ["mean", "median", "trimmed_mean_10pct", "best_single_bpm", "top10_by_confidence", "top20_by_confidence"]:
        configs.append(dict(base, stage="factor", config_name=f"combo_{combo}", bpm_combination=combo))
    for norm in ["none", "rms_per_bpm", "mad_per_bpm"]:
        configs.append(dict(base, stage="factor", config_name=f"norm_{norm}", bpm_normalization=norm))
    for ridge in ["global_peak", "local_tracked_peak", "dp_ridge_injection_anchored"]:
        cfg = dict(base, stage="factor", config_name=f"ridge_{ridge}")
        if ridge == "global_peak":
            cfg.update({"ridge_method": "greedy", "enable_tracking": "false"})
        elif ridge == "dp_ridge_injection_anchored":
            cfg.update({"ridge_method": "dp", "enable_tracking": "true"})
        configs.append(cfg)
    for name, band in TUNE_BANDS.items():
        configs.append(
            dict(
                base,
                stage="factor",
                config_name=f"band_{name}",
                tune_band=name,
                qx_min=band["qx"][0],
                qx_max=band["qx"][1],
                qy_min=band["qy"][0],
                qy_max=band["qy"][1],
            )
        )
    return unique_configs(configs)


def interaction_configs(max_configs: int, seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    windows = [(1024, 128), (1024, 256), (2048, 128), (2048, 256), (4096, 256), (4096, 512), (8192, 512)]
    spectral = [("hann", 2.5, 4), ("multitaper", 2.0, 3), ("multitaper", 2.5, 4), ("multitaper", 3.0, 5)]
    combos = ["mean", "median", "trimmed_mean_10pct", "best_single_bpm", "top10_by_confidence", "top20_by_confidence"]
    norms = ["none", "rms_per_bpm", "mad_per_bpm"]
    ridges = ["global_peak", "local_tracked_peak", "dp_ridge_injection_anchored"]
    bands = list(TUNE_BANDS)
    configs: list[dict[str, object]] = []
    cap = max(0, max_configs)
    while len(configs) < cap:
        base = base_config("interaction")
        window, stride = rng.choice(windows)
        method, nw, k = rng.choice(spectral)
        ridge = rng.choice(ridges)
        band_name = rng.choice(bands)
        band = TUNE_BANDS[band_name]
        base.update(
            {
                "stage": "interaction",
                "config_name": f"interaction_{len(configs) + 1:03d}",
                "window": window,
                "stride": stride,
                "spectrogram_method": method,
                "multitaper_nw": nw,
                "multitaper_k": k,
                "bpm_combination": rng.choice(combos),
                "bpm_normalization": rng.choice(norms),
                "ridge_method": "dp" if ridge == "dp_ridge_injection_anchored" else "greedy",
                "enable_tracking": "false" if ridge == "global_peak" else "true",
                "tune_band": band_name,
                "qx_min": band["qx"][0],
                "qx_max": band["qx"][1],
                "qy_min": band["qy"][0],
                "qy_max": band["qy"][1],
            }
        )
        configs.append(base)
    return unique_configs(configs)


def unique_configs(configs: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    unique: dict[str, dict[str, object]] = {}
    for config in configs:
        hashed = with_hash(config)
        unique[str(hashed["config_hash"])] = hashed
    return list(unique.values())


def pilot_configs(max_configs: int, seed: int) -> list[dict[str, object]]:
    configs = baseline_configs() + factor_screen_configs()
    remaining = max(0, max_configs - len(configs))
    configs += interaction_configs(remaining, seed)
    return unique_configs(configs)[:max_configs]


def load_config_list(path: Path) -> list[dict[str, object]]:
    rows = read_csv(path)
    configs: dict[str, dict[str, object]] = {}
    for row in rows:
        config = {}
        for field in CONFIG_FIELDS:
            if field in row:
                config[field] = row[field]
        if not config:
            continue
        if config.get("config_hash"):
            configs[str(config["config_hash"])] = config
        else:
            hashed = with_hash(config)
            configs[str(hashed["config_hash"])] = hashed
    return list(configs.values())


def spill_rows_for_view(rows: list[dict[str, str]], view: str, count: int) -> list[dict[str, str]]:
    if view != "combined":
        rows = [row for row in rows if row.get("collection") == view]
    rows = [row for row in rows if row.get("manifest_path")]
    rows.sort(key=lambda row: (row.get("collection", ""), int(row.get("target_ms") or 0)))
    if count <= 0 or len(rows) <= count:
        return rows
    if count == 1:
        return [rows[0]]
    picks = []
    for idx in range(count):
        pos = round(idx * (len(rows) - 1) / (count - 1))
        picks.append(rows[pos])
    return picks


def write_manifest_list(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(row["manifest_path"] for row in rows) + "\n", encoding="utf-8")


def skip_reason(config: dict[str, object], rows: list[dict[str, str]]) -> str:
    if not rows:
        return "no_spills"
    turn_start = int(config["turn_start"])
    turn_end = int(config["turn_end"])
    window = int(config["window"])
    if turn_end - turn_start < window:
        return "window_larger_than_turn_range"
    usable_lengths = [int(row.get("waveform_length") or 0) for row in rows]
    if usable_lengths and max(usable_lengths) < turn_end:
        return "turn_range_exceeds_waveform_length"
    return ""


def analyzer_command(config: dict[str, object], manifest_list: Path, out_dir: Path, device: str, heavy_plots: bool) -> list[str]:
    cmd = [
        sys.executable,
        str(ANALYZER),
        "--manifest-list",
        str(manifest_list),
        "--out",
        str(out_dir),
        "--device",
        device,
        "--progress",
        "0",
        "--stride-mode",
        "--turn-start",
        str(config["turn_start"]),
        "--turn-end",
        str(config["turn_end"]),
        "--sliding-window-turns",
        str(config["window"]),
        "--sliding-stride-turns",
        str(config["stride"]),
        "--injection-window-turns",
        str(config["window"]),
        "--spectrogram-method",
        str(config["spectrogram_method"]),
        "--multitaper-nw",
        str(config["multitaper_nw"]),
        "--multitaper-k",
        str(config["multitaper_k"]),
        "--bpm-combination",
        str(config["bpm_combination"]),
        "--bpm-normalization",
        str(config["bpm_normalization"]),
        "--detrend",
        str(config["detrend"]),
        "--dc-handling",
        str(config["dc_handling"]),
        "--ridge-method",
        str(config["ridge_method"]),
        "--ridge-jump-penalty",
        str(config["ridge_jump_penalty"]),
        "--ridge-jump2-penalty",
        str(config["ridge_jump2_penalty"]),
        "--ridge-max-step",
        str(config["ridge_max_step"]),
        "--ridge-anchor-enabled",
        str(config["ridge_anchor_enabled"]),
        "--qx-min",
        str(config["qx_min"]),
        "--qx-max",
        str(config["qx_max"]),
        "--qy-min",
        str(config["qy_min"]),
        "--qy-max",
        str(config["qy_max"]),
    ]
    if str(config["enable_tracking"]) == "false":
        cmd.append("--disable-tracking")
    if not heavy_plots:
        cmd.append("--no-spectrogram")
    return cmd


def run_jobs(args: argparse.Namespace, configs: list[dict[str, object]], dataset: list[dict[str, str]]) -> list[dict[str, object]]:
    out = Path(args.out)
    views = sorted({row.get("collection", "") for row in dataset if row.get("collection")})
    views.append("combined")
    run_rows: list[dict[str, object]] = []
    config_rows = configs
    write_csv(out / "autosweep_config_grid.csv", config_rows, CONFIG_FIELDS)
    job_idx = 0
    too_slow_configs: set[str] = set()
    for config in configs:
        for view in views:
            job_idx += 1
            config_id = str(config["config_hash"])
            selected = spill_rows_for_view(dataset, view, args.spills if args.mode == "pilot" else 0)
            manifest_list = out / "manifest_lists" / f"{view}_{config['config_hash']}.txt"
            job_out = out / "jobs" / str(config["config_hash"]) / view
            reason = "prior_view_too_slow" if config_id in too_slow_configs else skip_reason(config, selected)
            status = "skipped" if reason else "pending"
            if not reason:
                write_manifest_list(manifest_list, selected)
            cmd = analyzer_command(config, manifest_list, job_out, args.device, args.heavy_plots)
            started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            elapsed = 0.0
            if reason:
                pass
            elif args.dry_run:
                status = "dry_run"
            elif (job_out / "gpu_spills_summary.csv").exists() and not args.force:
                status = "cached"
            else:
                t0 = time.perf_counter()
                job_out.mkdir(parents=True, exist_ok=True)
                timeout = args.job_timeout_seconds if args.job_timeout_seconds > 0 else None
                try:
                    proc = subprocess.run(cmd, text=True, timeout=timeout)
                except subprocess.TimeoutExpired:
                    elapsed = time.perf_counter() - t0
                    status = "failed:timeout"
                    reason = f"job_timeout_seconds={args.job_timeout_seconds}"
                    too_slow_configs.add(config_id)
                else:
                    elapsed = time.perf_counter() - t0
                    status = "ok" if proc.returncode == 0 else f"failed:{proc.returncode}"
            run_rows.append(
                {
                    "job_id": job_idx,
                    "config_hash": config["config_hash"],
                    "collection_view": view,
                    "mode": args.mode,
                    "status": status,
                    "skip_reason": reason,
                    "started_utc": started,
                    "elapsed_seconds": f"{elapsed:.3f}",
                    "spill_count": len(selected),
                    "out_dir": str(job_out),
                    "manifest_list": str(manifest_list),
                    "command": " ".join(cmd),
                }
            )
            write_csv(out / "autosweep_run_log.csv", run_rows, RUN_FIELDS)
    return run_rows


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="dataset_manifest.csv")
    parser.add_argument("--mode", choices=["pilot", "full"], required=True)
    parser.add_argument("--spills", type=int, default=200)
    parser.add_argument("--max-configs", type=int, default=300)
    parser.add_argument("--config-list", default="")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20260613)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--heavy-plots", action="store_true")
    parser.add_argument(
        "--job-timeout-seconds",
        type=int,
        default=0,
        help="per-analyzer-job timeout; 0 disables timeout",
    )
    args = parser.parse_args(argv)
    dataset = read_csv(Path(args.dataset))
    if args.mode == "full":
        if not args.config_list:
            raise SystemExit("--config-list is required for --mode full")
        configs = load_config_list(Path(args.config_list))
    else:
        configs = pilot_configs(args.max_configs, args.seed)
    run_jobs(args, configs, dataset)


if __name__ == "__main__":
    main()
