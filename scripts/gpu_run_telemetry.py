#!/usr/bin/env python3
"""Lightweight GPU telemetry for long offline analysis runs.

The monitor uses `nvidia-smi` when it is available and writes a small CSV that
can be summarized after the run. It intentionally stays stdlib-only so it works
inside the Spark analysis venvs without extra dependencies.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


GPU_TELEMETRY_FIELDS = [
    "epoch",
    "iso_time",
    "gpu_timestamp",
    "gpu_index",
    "gpu_name",
    "utilization_gpu_pct",
    "utilization_memory_pct",
    "power_draw_w",
    "compute_apps",
]


@dataclass
class TelemetrySummary:
    sample_count: int
    first_iso_time: str
    last_iso_time: str
    wall_hours: float
    average_gpu_utilization_pct: float
    utilized_gpu_hours: float
    average_power_w: float
    energy_wh: float


def _run(command: Sequence[str]) -> str:
    proc = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return proc.stdout.strip()


def _float_or_none(value: object) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def query_gpu_sample() -> dict[str, object] | None:
    gpu = _run(
        [
            "nvidia-smi",
            "--query-gpu=timestamp,index,name,utilization.gpu,utilization.memory,power.draw",
            "--format=csv,noheader,nounits",
        ]
    )
    if not gpu:
        return None
    first = next((line for line in gpu.splitlines() if line.strip()), "")
    parts = [part.strip() for part in first.split(",", maxsplit=5)]
    if len(parts) != 6:
        return None
    apps = _run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    return {
        "epoch": int(time.time()),
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "gpu_timestamp": parts[0],
        "gpu_index": parts[1],
        "gpu_name": parts[2],
        "utilization_gpu_pct": parts[3],
        "utilization_memory_pct": parts[4],
        "power_draw_w": parts[5],
        "compute_apps": ";".join(line.strip() for line in apps.splitlines() if line.strip()),
    }


def append_sample(path: Path, sample: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GPU_TELEMETRY_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({field: sample.get(field, "") for field in GPU_TELEMETRY_FIELDS})


def read_samples(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def summarize_samples(rows: Sequence[dict[str, str]], default_interval_seconds: float = 30.0) -> TelemetrySummary:
    if not rows:
        return TelemetrySummary(0, "", "", 0.0, 0.0, 0.0, 0.0, 0.0)
    epochs = []
    utils = []
    powers = []
    for row in rows:
        try:
            epochs.append(float(row.get("epoch", "") or "nan"))
        except ValueError:
            pass
        util = _float_or_none(row.get("utilization_gpu_pct"))
        if util is not None:
            utils.append(util)
        power = _float_or_none(row.get("power_draw_w"))
        if power is not None:
            powers.append(power)
    wall_seconds = max(0.0, max(epochs) - min(epochs)) if len(epochs) >= 2 else 0.0
    intervals: list[float] = []
    if len(epochs) >= 2:
        ordered = sorted(epochs)
        intervals = [max(0.0, b - a) for a, b in zip(ordered, ordered[1:])]
    if not intervals and rows:
        intervals = [default_interval_seconds] * len(rows)
    if len(intervals) < len(utils):
        fill = intervals[-1] if intervals else default_interval_seconds
        intervals = intervals + [fill] * (len(utils) - len(intervals))
    utilized_gpu_hours = sum((util / 100.0) * interval for util, interval in zip(utils, intervals)) / 3600.0
    if len(intervals) < len(powers):
        fill = intervals[-1] if intervals else default_interval_seconds
        intervals_for_power = intervals + [fill] * (len(powers) - len(intervals))
    else:
        intervals_for_power = intervals
    energy_wh = sum(power * interval for power, interval in zip(powers, intervals_for_power)) / 3600.0
    return TelemetrySummary(
        sample_count=len(rows),
        first_iso_time=rows[0].get("iso_time", ""),
        last_iso_time=rows[-1].get("iso_time", ""),
        wall_hours=wall_seconds / 3600.0,
        average_gpu_utilization_pct=sum(utils) / len(utils) if utils else 0.0,
        utilized_gpu_hours=utilized_gpu_hours,
        average_power_w=sum(powers) / len(powers) if powers else 0.0,
        energy_wh=energy_wh,
    )


def write_summary(csv_path: Path, json_path: Path, markdown_path: Path | None = None) -> TelemetrySummary:
    summary = summarize_samples(read_samples(csv_path))
    payload = summary.__dict__
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if markdown_path is not None:
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    return summary


def render_markdown(summary: TelemetrySummary) -> str:
    return "\n".join(
        [
            "# GPU Telemetry Summary",
            "",
            f"- samples: `{summary.sample_count}`",
            f"- first sample: `{summary.first_iso_time}`",
            f"- last sample: `{summary.last_iso_time}`",
            f"- wall hours: `{summary.wall_hours:.3f}`",
            f"- average GPU utilization: `{summary.average_gpu_utilization_pct:.1f}%`",
            f"- utilized GPU-hours: `{summary.utilized_gpu_hours:.3f}`",
            f"- average power: `{summary.average_power_w:.2f} W`",
            f"- energy: `{summary.energy_wh:.2f} Wh`",
            "",
        ]
    )


class TelemetryThread:
    def __init__(self, csv_path: Path, interval_seconds: float = 30.0, sampler: Callable[[], dict[str, object] | None] = query_gpu_sample):
        self.csv_path = csv_path
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.sampler = sampler
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gpu-telemetry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            sample = self.sampler()
            if sample is not None:
                append_sample(self.csv_path, sample)
            self._stop.wait(self.interval_seconds)


def cmd_monitor(args: argparse.Namespace) -> int:
    csv_path = Path(args.out)
    started = time.monotonic()
    while True:
        sample = query_gpu_sample()
        if sample is not None:
            append_sample(csv_path, sample)
        if args.duration_seconds > 0 and time.monotonic() - started >= args.duration_seconds:
            break
        time.sleep(max(1.0, args.interval_seconds))
    if args.summary_json:
        write_summary(csv_path, Path(args.summary_json), Path(args.summary_md) if args.summary_md else None)
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    write_summary(Path(args.input), Path(args.summary_json), Path(args.summary_md) if args.summary_md else None)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    monitor = sub.add_parser("monitor", help="poll nvidia-smi into a telemetry CSV")
    monitor.add_argument("--out", required=True)
    monitor.add_argument("--interval-seconds", type=float, default=30.0)
    monitor.add_argument("--duration-seconds", type=float, default=0.0)
    monitor.add_argument("--summary-json", default="")
    monitor.add_argument("--summary-md", default="")
    monitor.set_defaults(func=cmd_monitor)
    summarize = sub.add_parser("summarize", help="summarize a telemetry CSV")
    summarize.add_argument("--input", required=True)
    summarize.add_argument("--summary-json", required=True)
    summarize.add_argument("--summary-md", default="")
    summarize.set_defaults(func=cmd_summarize)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
