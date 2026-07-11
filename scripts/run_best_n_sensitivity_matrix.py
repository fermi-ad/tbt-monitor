#!/usr/bin/env python3
"""Run and compare the declared Best-N publication sensitivity matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bpm_mining.best_n_sensitivity import (
    SensitivityRun,
    build_sensitivity_matrix,
    read_mem_available_gib,
    validate_parallel_run_controls,
)
from bpm_mining.best_n_verification import verify_best_n_outputs
from bpm_mining.contracts import file_sha256
from bpm_mining.io import atomic_write_text, write_csv


MANIFEST_FIELDS = [
    "run",
    "beam_width",
    "fit_windows",
    "fold_seed",
    "max_n",
    "curve_limit",
    "validation_limit",
    "folds",
    "bootstrap_block_spills",
    "status",
    "elapsed_seconds",
    "output",
]


@dataclass
class _RunJob:
    run: SensitivityRun
    output: Path
    command: list[str]
    started: float = 0.0
    process: subprocess.Popen[bytes] | None = None


def _parameters(args: argparse.Namespace, run: SensitivityRun) -> dict[str, object]:
    config = Path(args.config).resolve()
    inputs = Path(args.inputs).resolve()
    return {
        "config": str(config),
        "config_sha256": file_sha256(config),
        "inputs": str(inputs),
        "bpm_index_sha256": file_sha256(inputs / "manifest" / "bpm_index.csv"),
        "spectral_cache_index_sha256": file_sha256(inputs / "cache" / "index" / "spectral_cache.csv"),
        "device": args.device,
        "beam_width": run.beam_width,
        "fit_windows": run.fit_windows,
        "fold_seed": run.fold_seed,
        "max_n": args.max_n,
        "curve_limit": args.curve_limit,
        "validation_limit": args.validation_limit,
        "folds": args.folds,
        "bootstrap_samples": args.bootstrap_samples,
        "bootstrap_block_spills": args.bootstrap_block_spills,
        "tune_half_width": args.tune_half_width,
        "spectral_config": args.spectral_config or "",
    }


def _evaluation_command(
    args: argparse.Namespace,
    run: SensitivityRun,
    output: Path,
) -> list[str]:
    command = [
        sys.executable,
        "scripts/evaluate_best_n_curve.py",
        "--config",
        args.config,
        "--inputs",
        args.inputs,
        "--out",
        str(output),
        "--device",
        args.device,
        "--max-n",
        str(args.max_n),
        "--beam-width",
        str(run.beam_width),
        "--curve-limit",
        str(args.curve_limit),
        "--validation-limit",
        str(args.validation_limit),
        "--validation-beam-width",
        str(run.beam_width),
        "--folds",
        str(args.folds),
        "--fold-seed",
        str(run.fold_seed),
        "--fit-windows",
        str(run.fit_windows),
        "--tune-half-width",
        str(args.tune_half_width),
        "--bootstrap-samples",
        str(args.bootstrap_samples),
        "--bootstrap-block-spills",
        str(args.bootstrap_block_spills),
        "--progress-every",
        str(args.progress_every),
    ]
    if args.spectral_config:
        command.extend(["--spectral-config", args.spectral_config])
    if args.resume:
        command.append("--resume")
    return command


def _comparison_command(
    script: str,
    dimension: str,
    entries: list[tuple[str, SensitivityRun]],
    run_root: Path,
    reference_label: str,
    output: Path,
) -> list[str]:
    command = [sys.executable, script]
    is_beam_width_comparison = script.endswith("compare_best_n_beam_widths.py")
    if script.endswith("compare_best_n_sensitivity.py"):
        command.extend(["--dimension", dimension])
    for label, run in entries:
        comparison_label = label.removeprefix("beam") if is_beam_width_comparison else label
        command.extend(["--run", f"{comparison_label}={run_root / run.slug}"])
    if is_beam_width_comparison:
        command.extend(["--reference-width", reference_label.removeprefix("beam")])
    else:
        command.extend(["--reference-label", reference_label])
    command.extend(["--out", str(output)])
    return command


def _write_index(
    out: Path,
    dimensions: dict[str, list[tuple[str, SensitivityRun]]],
    reference_labels: dict[str, str],
    dry_run: bool,
    parallel_runs: int,
    minimum_available_memory_gib: float,
) -> None:
    concurrency_text = (
        f"Evaluator concurrency: `{parallel_runs}`; available-memory floor: "
        f"`{minimum_available_memory_gib:g} GiB`."
        if parallel_runs > 1
        else "Evaluator concurrency: `1` (serial); available-memory floor is not enforced."
    )
    lines = [
        "# Best-N Sensitivity Matrix",
        "",
        f"Execution mode: `{'plan only' if dry_run else 'completed'}`",
        concurrency_text,
        "",
        "The matrix changes one hyperparameter at a time. The beam-width comparison also checks exact membership, score, and tune convergence; fit-window and fold-seed runs test whether the inferred knee is stable to the selection prefix and digitizer partition.",
        "",
    ]
    for dimension, entries in dimensions.items():
        lines.extend(
            [
                f"## {dimension}",
                "",
                f"Reference: `{reference_labels[dimension]}`",
                "",
                "| label | beam width | fit windows | fold seed | output |",
                "| --- | ---: | ---: | ---: | --- |",
            ]
        )
        for label, run in entries:
            lines.append(
                f"| {label} | {run.beam_width} | {run.fit_windows} | {run.fold_seed} | `runs/{run.slug}` |"
            )
        lines.append("")
    atomic_write_text(out / "SENSITIVITY_INDEX.md", "\n".join(lines) + "\n")


def _manifest_row(args: argparse.Namespace, job: _RunJob, status: str) -> dict[str, object]:
    return {
        "run": job.run.slug,
        "beam_width": job.run.beam_width,
        "fit_windows": job.run.fit_windows,
        "fold_seed": job.run.fold_seed,
        "max_n": args.max_n,
        "curve_limit": args.curve_limit,
        "validation_limit": args.validation_limit,
        "folds": args.folds,
        "bootstrap_block_spills": args.bootstrap_block_spills,
        "status": status,
        "elapsed_seconds": f"{max(0.0, time.time() - job.started):.3f}" if job.started else "0.000",
        "output": str(job.output),
    }


def _verify_run(args: argparse.Namespace, job: _RunJob) -> None:
    report = verify_best_n_outputs(
        job.output,
        expected_max_n=args.max_n,
        expected_curve_cache_keys=args.curve_limit,
        expected_validation_cache_keys=args.validation_limit,
        expected_folds=args.folds,
        tune_half_width=args.tune_half_width,
        require_cross_collection=True,
        require_plots=True,
    )
    if report["status"] != "pass":
        raise RuntimeError(f"Best-N sensitivity run failed verification: {job.run.slug}")


def _terminate_jobs(jobs: list[_RunJob]) -> None:
    processes = [job.process for job in jobs if job.process is not None and job.process.poll() is None]
    for process in processes:
        process.terminate()
    deadline = time.monotonic() + 10.0
    for process in processes:
        timeout = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes:
        process.wait()


def _execute_jobs(
    args: argparse.Namespace,
    jobs: list[_RunJob],
    repo_root: Path,
    out: Path,
    verify_run: Callable[[argparse.Namespace, _RunJob], None] = _verify_run,
    read_available_memory_gib: Callable[[], float | None] = read_mem_available_gib,
) -> list[dict[str, object]]:
    if args.dry_run:
        manifest = [_manifest_row(args, job, "planned") for job in jobs]
        write_csv(out / "sensitivity_run_manifest.csv", manifest, MANIFEST_FIELDS)
        return manifest

    pending = list(jobs)
    active: list[_RunJob] = []
    completed: dict[str, dict[str, object]] = {}
    order = {job.run.slug: index for index, job in enumerate(jobs)}
    low_memory_samples = 0
    try:
        while pending or active:
            while pending and len(active) < args.parallel_runs:
                job = pending.pop(0)
                job.started = time.time()
                job.process = subprocess.Popen(job.command, cwd=repo_root)
                active.append(job)
                print(
                    f"sensitivity_run={job.run.slug} pid={job.process.pid} status=launched "
                    f"active={len(active)}/{args.parallel_runs}",
                    flush=True,
                )

            for job in list(active):
                assert job.process is not None
                status = job.process.poll()
                if status is None:
                    continue
                active.remove(job)
                if status != 0:
                    raise subprocess.CalledProcessError(status, job.command)
                verify_run(args, job)
                completed[job.run.slug] = _manifest_row(args, job, "verified")
                manifest = sorted(completed.values(), key=lambda row: order[str(row["run"])])
                write_csv(out / "sensitivity_run_manifest.csv", manifest, MANIFEST_FIELDS)
                print(f"sensitivity_run={job.run.slug} status=verified", flush=True)

            if not active and not pending:
                break

            if args.parallel_runs > 1:
                available_gib = read_available_memory_gib()
                if available_gib is None:
                    atomic_write_text(
                        out / "memory_guard_abort.json",
                        json.dumps(
                            {
                                "status": "aborted",
                                "reason": "MemAvailable unreadable",
                                "active_runs": [job.run.slug for job in active],
                                "minimum_available_memory_gib": args.minimum_available_memory_gib,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    )
                    raise RuntimeError("parallel sensitivity runs require readable Linux MemAvailable")
                if available_gib < args.minimum_available_memory_gib:
                    low_memory_samples += 1
                    print(
                        f"memory_guard available_gib={available_gib:.3f} "
                        f"floor_gib={args.minimum_available_memory_gib:.3f} "
                        f"low_samples={low_memory_samples}/{args.low_memory_samples}",
                        flush=True,
                    )
                else:
                    low_memory_samples = 0
                if low_memory_samples >= args.low_memory_samples:
                    atomic_write_text(
                        out / "memory_guard_abort.json",
                        json.dumps(
                            {
                                "status": "aborted",
                                "reason": "sustained available-memory floor breach",
                                "active_runs": [job.run.slug for job in active],
                                "available_memory_gib": available_gib,
                                "minimum_available_memory_gib": args.minimum_available_memory_gib,
                                "low_memory_samples": low_memory_samples,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                    )
                    raise RuntimeError(
                        "available-memory floor persisted; sensitivity evaluators were terminated "
                        "with resumable checkpoints"
                    )
            time.sleep(args.memory_check_seconds)
    except BaseException:
        _terminate_jobs(active)
        raise
    return sorted(completed.values(), key=lambda row: order[str(row["run"])])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/best_bpm_mining.yaml")
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cuda")
    parser.add_argument("--spectral-config", default=None)
    parser.add_argument("--max-n", type=int, default=30)
    parser.add_argument("--curve-limit", type=int, default=400)
    parser.add_argument("--validation-limit", type=int, default=200)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--beam-widths", nargs="+", type=int, default=[16, 32, 64])
    parser.add_argument("--fit-windows", nargs="+", type=int, default=[4, 8, 16])
    parser.add_argument("--fold-seeds", nargs="+", type=int, default=[20260709, 20260710, 20260711])
    parser.add_argument("--baseline-beam-width", type=int, default=32)
    parser.add_argument("--baseline-fit-windows", type=int, default=8)
    parser.add_argument("--baseline-fold-seed", type=int, default=20260709)
    parser.add_argument("--beam-reference-width", type=int, default=64)
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    parser.add_argument("--bootstrap-block-spills", type=int, default=20)
    parser.add_argument("--tune-half-width", type=float, default=0.0025)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--parallel-runs",
        type=int,
        default=1,
        help="concurrent evaluators; Spark qualification permits at most 2",
    )
    parser.add_argument(
        "--minimum-available-memory-gib",
        type=float,
        default=32.0,
        help="terminate concurrent evaluators after a sustained MemAvailable breach",
    )
    parser.add_argument("--memory-check-seconds", type=float, default=5.0)
    parser.add_argument("--low-memory-samples", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        validate_parallel_run_controls(
            args.parallel_runs,
            args.minimum_available_memory_gib,
            args.memory_check_seconds,
            args.low_memory_samples,
        )
    except ValueError as exc:
        parser.error(str(exc))

    runs, dimensions = build_sensitivity_matrix(
        args.beam_widths,
        args.fit_windows,
        args.fold_seeds,
        args.baseline_beam_width,
        args.baseline_fit_windows,
        args.baseline_fold_seed,
    )
    beam_reference_label = f"beam{args.beam_reference_width}"
    reference_labels = {
        "beam_width": beam_reference_label,
        "fit_windows": f"fit{args.baseline_fit_windows}",
        "fold_seed": f"seed{args.baseline_fold_seed}",
    }
    if beam_reference_label not in {label for label, _run in dimensions["beam_width"]}:
        raise SystemExit("beam reference width must be present in --beam-widths")

    out = Path(args.out).resolve()
    run_root = out / "runs"
    if out.exists() and any(out.iterdir()) and not args.resume:
        raise SystemExit(f"output directory is not empty; pass --resume to reuse it: {out}")
    run_root.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []
    jobs: list[_RunJob] = []

    repo_root = Path(__file__).resolve().parents[1]
    for run in runs:
        run_out = run_root / run.slug
        run_out.mkdir(parents=True, exist_ok=True)
        parameters = _parameters(args, run)
        parameter_path = run_out / "run_parameters.json"
        if parameter_path.exists():
            existing = json.loads(parameter_path.read_text(encoding="utf-8"))
            if existing != parameters:
                raise SystemExit(f"resume parameter mismatch: {parameter_path}")
        else:
            atomic_write_text(parameter_path, json.dumps(parameters, indent=2, sort_keys=True) + "\n")
        command = _evaluation_command(args, run, run_out)
        commands.append(command)
        jobs.append(_RunJob(run=run, output=run_out, command=command))

    atomic_write_text(
        out / "execution_controls.json",
        json.dumps(
            {
                "parallel_runs": args.parallel_runs,
                "minimum_available_memory_gib": args.minimum_available_memory_gib,
                "memory_check_seconds": args.memory_check_seconds,
                "low_memory_samples": args.low_memory_samples,
                "memory_source": "/proc/meminfo:MemAvailable" if args.parallel_runs > 1 else "not required",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _execute_jobs(args, jobs, repo_root, out)

    comparison_commands: list[list[str]] = []
    for dimension, entries in dimensions.items():
        comparison_commands.append(
            _comparison_command(
                "scripts/compare_best_n_sensitivity.py",
                dimension,
                entries,
                run_root,
                reference_labels[dimension],
                out / dimension,
            )
        )
    comparison_commands.append(
        _comparison_command(
            "scripts/compare_best_n_beam_widths.py",
            "beam_width",
            dimensions["beam_width"],
            run_root,
            beam_reference_label,
            out / "beam_width",
        )
    )
    commands.extend(comparison_commands)
    if not args.dry_run:
        for command in comparison_commands:
            subprocess.run(command, cwd=repo_root, check=True)
        subprocess.run(
            [
                sys.executable,
                "scripts/build_image_gallery.py",
                "--root",
                str(out),
                "--out",
                str(out / "index.html"),
                "--title",
                "Best-N Sensitivity Matrix",
            ],
            cwd=repo_root,
            check=True,
        )
    atomic_write_text(
        out / "commands.json",
        json.dumps(commands, indent=2) + "\n",
    )
    _write_index(
        out,
        dimensions,
        reference_labels,
        args.dry_run,
        args.parallel_runs,
        args.minimum_available_memory_gib,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
