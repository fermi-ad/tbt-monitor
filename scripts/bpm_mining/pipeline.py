"""CLI orchestration for best-BPM mining passes."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

from .artifact_selection import select_artifacts
from .clustering import cluster_spills
from .config import config_hash, load_config
from .consensus import build_consensus
from .evolution import evaluate_evolution
from .io import atomic_write_text, build_manifest_outputs, ensure_dir, write_csv
from .peaks import extract_per_bpm_features
from .plots import make_artifacts
from .report import make_report
from .spectra import build_spectral_cache
from .statistics import aggregate_statistics
from .subset_search import search_best_bpm_subsets


def write_run_manifest(logs: Path, cfg: dict[str, object], pass_name: str, args: argparse.Namespace) -> None:
    ensure_dir(logs)
    payload = {
        "pass": pass_name,
        "config_hash": config_hash(cfg),
        "argv": sys.argv,
        "args": vars(args),
        "started_unix": time.time(),
    }
    atomic_write_text(logs / "run_manifest.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_effective_config(root: Path, cfg: dict[str, object]) -> None:
    payload = {
        "config_hash": config_hash(cfg),
        "config": cfg,
    }
    atomic_write_text(root / "config" / "effective_config.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_failure(logs: Path, pass_name: str, exc: BaseException) -> None:
    ensure_dir(logs)
    with (logs / "failures.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pass": pass_name, "error": repr(exc), "traceback": traceback.format_exc()}) + "\n")


def write_progress(logs: Path, pass_name: str, status: str, elapsed: float) -> None:
    path = logs / "progress.csv"
    exists = path.exists()
    ensure_dir(path.parent)
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pass", "status", "elapsed_seconds"])
        if not exists:
            writer.writeheader()
        writer.writerow({"pass": pass_name, "status": status, "elapsed_seconds": f"{elapsed:.3f}"})


def run_guarded(pass_name: str, cfg: dict[str, object], logs: Path, args: argparse.Namespace, fn: Callable[[], None]) -> None:
    write_run_manifest(logs, cfg, pass_name, args)
    started = time.perf_counter()
    try:
        fn()
    except BaseException as exc:
        append_failure(logs, pass_name, exc)
        write_progress(logs, pass_name, "failed", time.perf_counter() - started)
        raise
    write_progress(logs, pass_name, "ok", time.perf_counter() - started)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--limit", type=int, default=0)


def cmd_manifest(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build best-BPM manifest and integrity tables")
    add_common(parser)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.workers is not None:
        cfg.setdefault("runtime", {})["workers"] = args.workers
    out = Path(args.out)
    run_guarded("manifest", cfg, out.parent / "logs", args, lambda: build_manifest_outputs(cfg, out, args.limit))


def cmd_cache(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build best-BPM spectral cache")
    add_common(parser)
    parser.add_argument("--manifest", required=True, help="manifest directory or spills.csv path")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    manifest = Path(args.manifest)
    manifest_dir = manifest.parent if manifest.is_file() else manifest
    device = args.device or str(cfg["runtime"].get("device", "auto"))
    workers = args.workers if args.workers is not None else int(cfg["runtime"].get("workers", 1))
    out = Path(args.out)
    run_guarded("spectral_cache", cfg, out.parent / "logs", args, lambda: build_spectral_cache(cfg, manifest_dir, out, device, workers, args.resume, args.limit))


def cmd_features(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Extract per-BPM features from spectral cache")
    add_common(parser)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("per_bpm_features", cfg, Path(args.out).parent / "logs", args, lambda: extract_per_bpm_features(cfg, Path(args.cache), Path(args.manifest), Path(args.out)))


def cmd_consensus(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build within-spill tune consensus")
    add_common(parser)
    parser.add_argument("--features", required=True)
    parser.add_argument("--cache", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cache_dir = Path(args.cache) if args.cache else None
    run_guarded("consensus", cfg, Path(args.out).parent / "logs", args, lambda: build_consensus(cfg, Path(args.features), Path(args.out), cache_dir))


def cmd_search(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Search best BPM subsets")
    add_common(parser)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--consensus", required=True)
    parser.add_argument("--subset-sizes", nargs="+", type=int, default=[1, 3, 5, 10])
    parser.add_argument("--device", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded(
        "subset_search",
        cfg,
        Path(args.out).parent / "logs",
        args,
        lambda: search_best_bpm_subsets(
            cfg,
            Path(args.cache),
            Path(args.manifest),
            Path(args.features),
            Path(args.consensus),
            Path(args.out),
            args.subset_sizes,
            args.device or str(cfg["runtime"].get("device", "auto")),
            args.limit,
        ),
    )


def cmd_evolution(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate finalist subset evolution")
    add_common(parser)
    parser.add_argument("--subsets", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default=None)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("evolution", cfg, Path(args.out).parent / "logs", args, lambda: evaluate_evolution(cfg, Path(args.subsets), Path(args.out)))


def cmd_statistics(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Aggregate best-BPM statistics")
    add_common(parser)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    root = Path(args.inputs)
    manifest = Path(args.manifest) if args.manifest else root / "manifest"
    run_guarded("statistics", cfg, Path(args.out).parent / "logs", args, lambda: aggregate_statistics(cfg, root, manifest, Path(args.out)))


def cmd_clustering(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Cluster spill morphologies")
    add_common(parser)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("clustering", cfg, Path(args.out).parent / "logs", args, lambda: cluster_spills(cfg, Path(args.inputs), Path(args.out)))


def cmd_select_artifacts(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Select best-BPM poster artifacts")
    add_common(parser)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("artifact_selection", cfg, Path(args.out).parent / "logs", args, lambda: select_artifacts(cfg, Path(args.inputs), Path(args.out)))


def cmd_make_artifacts(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate best-BPM plots/artifacts")
    add_common(parser)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("artifacts", cfg, Path(args.out).parent / "logs", args, lambda: make_artifacts(cfg, Path(args.inputs), Path(args.manifest), Path(args.out)))


def cmd_report(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write best-BPM mining reports")
    add_common(parser)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_guarded("report", cfg, Path(args.out).parent / "logs", args, lambda: make_report(cfg, Path(args.inputs), Path(args.out)))


def cmd_pipeline(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the full best-BPM mining pipeline")
    add_common(parser)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--subset-sizes", nargs="+", type=int, default=[1, 3, 5, 10])
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    root = Path(args.out)
    device = args.device or str(cfg["runtime"].get("device", "auto"))
    workers = args.workers if args.workers is not None else int(cfg["runtime"].get("workers", 1))
    cfg.setdefault("runtime", {})["workers"] = workers

    def run_all() -> None:
        logs = root / "logs"
        write_effective_config(root, cfg)
        steps: list[tuple[str, Callable[[], None]]] = [
            ("manifest", lambda: build_manifest_outputs(cfg, root / "manifest", args.limit)),
            ("spectral_cache", lambda: build_spectral_cache(cfg, root / "manifest", root / "cache", device, workers, args.resume, args.limit)),
            ("per_bpm_features", lambda: extract_per_bpm_features(cfg, root / "cache", root / "manifest", root / "per_bpm")),
            ("consensus", lambda: build_consensus(cfg, root / "per_bpm", root / "consensus", root / "cache")),
            ("subset_search", lambda: search_best_bpm_subsets(cfg, root / "cache", root / "manifest", root / "per_bpm", root / "consensus", root / "subset_search", args.subset_sizes, device, args.limit)),
            ("evolution", lambda: evaluate_evolution(cfg, root / "subset_search", root / "evolution")),
            ("statistics", lambda: aggregate_statistics(cfg, root, root / "manifest", root / "statistics")),
            ("clustering", lambda: cluster_spills(cfg, root, root / "clustering")),
            ("artifact_selection", lambda: select_artifacts(cfg, root, root / "artifact_selection")),
            ("artifacts", lambda: make_artifacts(cfg, root, root / "artifact_selection" / "artifact_manifest.csv", root / "artifacts")),
            ("report", lambda: make_report(cfg, root, root / "reports")),
        ]
        for step_name, step_fn in steps:
            write_progress(logs, step_name, "started", 0.0)
            step_started = time.perf_counter()
            try:
                step_fn()
            except BaseException as exc:
                append_failure(logs, step_name, exc)
                write_progress(logs, step_name, "failed", time.perf_counter() - step_started)
                raise
            write_progress(logs, step_name, "ok", time.perf_counter() - step_started)

    run_guarded("full_pipeline", cfg, root / "logs", args, run_all)
