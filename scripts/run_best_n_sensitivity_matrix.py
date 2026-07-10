#!/usr/bin/env python3
"""Run and compare the declared Best-N publication sensitivity matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from bpm_mining.best_n_sensitivity import SensitivityRun, build_sensitivity_matrix
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
    if script.endswith("compare_best_n_sensitivity.py"):
        command.extend(["--dimension", dimension])
    for label, run in entries:
        command.extend(["--run", f"{label}={run_root / run.slug}"])
    if script.endswith("compare_best_n_beam_widths.py"):
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
) -> None:
    lines = [
        "# Best-N Sensitivity Matrix",
        "",
        f"Execution mode: `{'plan only' if dry_run else 'completed'}`",
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
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
    manifest: list[dict[str, object]] = []
    commands: list[list[str]] = []

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
        started = time.time()
        status = "planned"
        if not args.dry_run:
            subprocess.run(command, cwd=repo_root, check=True)
            report = verify_best_n_outputs(
                run_out,
                expected_max_n=args.max_n,
                expected_curve_cache_keys=args.curve_limit,
                expected_validation_cache_keys=args.validation_limit,
                expected_folds=args.folds,
                tune_half_width=args.tune_half_width,
                require_cross_collection=True,
                require_plots=True,
            )
            if report["status"] != "pass":
                raise RuntimeError(f"Best-N sensitivity run failed verification: {run.slug}")
            status = "verified"
        manifest.append(
            {
                "run": run.slug,
                "beam_width": run.beam_width,
                "fit_windows": run.fit_windows,
                "fold_seed": run.fold_seed,
                "max_n": args.max_n,
                "curve_limit": args.curve_limit,
                "validation_limit": args.validation_limit,
                "folds": args.folds,
                "bootstrap_block_spills": args.bootstrap_block_spills,
                "status": status,
                "elapsed_seconds": f"{time.time() - started:.3f}",
                "output": str(run_out),
            }
        )
        write_csv(out / "sensitivity_run_manifest.csv", manifest, MANIFEST_FIELDS)

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
    _write_index(out, dimensions, reference_labels, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
