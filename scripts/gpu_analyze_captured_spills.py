#!/usr/bin/env python3
"""GPU-backed offline analysis for captured tbt-monitor spill bundles.

This command reads raw captured-spill bundles directly from ``capture-spills``
output. It mirrors the Rust analyzer's core tune path closely enough for
poster-scale processing: mean-subtracted Hann windows, full FFT power, median
band confidence, parabolic peak refinement, flash window placement, and local
tracking seeded by the injection tune. It also adds poster-oriented tune
evolution products: ridge-density plots, optional multitaper spectra, dynamic
programming ridge extraction, and representative SVD/PCA denoising products.
CuPy is used for window FFT batches when available; NumPy remains the CPU
reproducibility path.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - exercised only on incomplete envs
    raise SystemExit("gpu_analyze_captured_spills.py requires numpy") from exc

try:
    import bpm_dgx_poster as poster
except ImportError as exc:  # pragma: no cover - script is intended to run from repo checkout
    raise SystemExit("run from a checkout containing scripts/bpm_dgx_poster.py") from exc


MIN_PEAK_SEARCH_BIN = 3
DEFAULT_Q_BAND = (0.64, 0.74)


SPILL_FIELDS = [
    "spill_index",
    "run_name",
    "bundle_dir",
    "target_ms",
    "status",
    "usable_for_analysis",
    "suitable_for_poster",
    "requested_streams",
    "captured_streams",
    "used_streams_h",
    "used_streams_v",
    "consensus_turns_h",
    "consensus_turns_v",
    "injection_start_turn",
    "injection_window_turns",
    "sliding_window_turns",
    "sliding_stride_turns",
    "flash_count",
    "plane_mode",
    "turn_start",
    "turn_end",
    "bpm_combination",
    "bpm_normalization",
    "detrend",
    "dc_handling",
    "ridge_anchor_enabled",
    "ridge_anchor_h",
    "ridge_anchor_v",
    "ridge_anchor_half_width",
    "qx_band_min",
    "qx_band_max",
    "qy_band_min",
    "qy_band_max",
    "qx_injection",
    "qy_injection",
    "confidence_h",
    "confidence_v",
    "median_qx",
    "median_qy",
    "std_qx",
    "std_qy",
    "min_qx",
    "max_qx",
    "min_qy",
    "max_qy",
    "sliding_fallback_count_h",
    "sliding_fallback_count_v",
    "sliding_suspicious_count_h",
    "sliding_suspicious_count_v",
    "sliding_missing_seed_count_h",
    "sliding_missing_seed_count_v",
    "sliding_windows_h",
    "sliding_windows_v",
    "finite_fraction_h",
    "finite_fraction_v",
    "median_abs_step_h",
    "median_abs_step_v",
    "p95_abs_step_h",
    "p95_abs_step_v",
    "band_edge_fraction_h",
    "band_edge_fraction_v",
    "dominant_bpm_fraction_h",
    "dominant_bpm_fraction_v",
    "odd_even_delta_h",
    "odd_even_delta_v",
    "first_second_half_delta_h",
    "first_second_half_delta_v",
    "subset_delta_h",
    "subset_delta_v",
    "ridge_prominence_h",
    "ridge_prominence_v",
    "quality_flags",
    "warnings",
]


SLIDING_FIELDS = [
    "spill_index",
    "run_name",
    "target_ms",
    "plane",
    "window_index",
    "center_turn",
    "raw_global_tune",
    "tracked_local_tune",
    "selected_tune",
    "raw_global_confidence",
    "selected_confidence",
    "used_global_fallback",
    "suspicious_step",
    "step_delta",
    "dp_ridge_tune",
    "dp_ridge_power",
    "dp_ridge_score",
    "delta_ridge_vs_selected",
    "band_edge",
    "ridge_anchor_distance",
    "ridge_prominence",
]


RIDGE_FIELDS = [
    "window_index",
    "center_turn",
    "ridge_tune",
    "ridge_power",
    "ridge_score",
    "local_peak_tune",
    "selected_tune_baseline",
    "delta_vs_baseline",
]


FLASH_FIELDS = [
    "target_ms",
    "run_name",
    "plane",
    "requested_flashes",
    "available_windows",
    "effective_flashes",
    "missing_fraction",
    "tune_mean",
    "tune_std",
    "smoothness_rms",
    "low_confidence_fraction",
    "fallback_fraction",
    "suspicious_fraction",
]


BPM_OBSERVATION_FIELDS = [
    "spill_index",
    "target_ms",
    "run_name",
    "plane",
    "bpm_label",
    "injection_tune",
    "injection_confidence",
    "injection_prominence",
    "injection_rms",
]


BPM_LEADERBOARD_FIELDS = [
    "plane",
    "bpm_label",
    "observation_count",
    "finite_tune_count",
    "median_tune",
    "tune_std",
    "median_confidence",
    "median_prominence",
    "median_rms",
    "score",
]


@dataclass
class PeakResult:
    tune: float
    confidence: float
    peak_power: float
    median_power: float
    prominence: float


@dataclass
class StreamMeta:
    bpm_ip: str
    stream_key: str
    plane: str
    stream_id: str
    stream_ms: int
    payload_path: Path
    sample_count: int
    payload_bytes: Optional[int]
    aligned: Optional[bool]


@dataclass
class SpillBundle:
    bundle_dir: Path
    run_name: str
    target_ms: int
    requested_streams: Optional[int]
    streams: list[StreamMeta]
    warnings: list[str]


@dataclass
class PlaneAnalysis:
    plane: str
    used_streams: int
    consensus_turns: int
    injection_peak: Optional[PeakResult]
    sliding_points: list[dict[str, object]]
    band_spectra: Optional[np.ndarray]
    fallback_count: int
    suspicious_count: int
    missing_seed_count: int
    spectra_by_method: dict[str, np.ndarray] = field(default_factory=dict)
    band_spectra_by_method: dict[str, np.ndarray] = field(default_factory=dict)
    ridge_points: list[dict[str, object]] = field(default_factory=list)
    dominant_bpm_fraction: float = 0.0
    subset_deltas: dict[str, Optional[float]] = field(default_factory=dict)
    bpm_observations: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PlaneRepresentative:
    score: float
    spill_index: int
    bundle: SpillBundle
    analysis: PlaneAnalysis


class FftBackend:
    def __init__(self, requested: str):
        self.requested = requested
        self.is_cuda = False
        self.name = "numpy"
        self.xp = np
        self.cp = None
        if requested in {"auto", "cuda"}:
            try:
                import cupy as cp  # type: ignore

                cp.cuda.runtime.getDeviceCount()
                self.is_cuda = True
                self.name = "cupy"
                self.xp = cp
                self.cp = cp
            except Exception as exc:
                if requested == "cuda":
                    raise SystemExit(f"CUDA requested but CuPy is unavailable: {exc}") from exc

    def to_numpy(self, value):
        if self.is_cuda:
            return self.cp.asnumpy(value)
        return np.asarray(value)

    def synchronize(self) -> None:
        if self.is_cuda:
            self.cp.cuda.Stream.null.synchronize()

    def describe(self) -> dict[str, object]:
        if not self.is_cuda:
            return {"backend": "numpy", "device": "CPU"}
        props = self.cp.cuda.runtime.getDeviceProperties(0)
        name = props.get("name", b"CUDA GPU")
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        return {
            "backend": "cupy",
            "device": name,
            "runtime_version": self.cp.cuda.runtime.runtimeGetVersion(),
            "driver_version": self.cp.cuda.runtime.driverGetVersion(),
            "compute_capability": f"{props.get('major')}.{props.get('minor')}",
            "cupy_version": self.cp.__version__,
        }


def fmt_float(value: Optional[float], digits: int = 6) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.{digits}f}"


def finite(values: Iterable[Optional[float]]) -> list[float]:
    return [value for value in values if value is not None and math.isfinite(value)]


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = finite(values)
    return statistics.median(vals) if vals else None


def stdev(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = finite(values)
    return statistics.stdev(vals) if len(vals) > 1 else None


def smoothness(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    diffs = [b - a for a, b in zip(values, values[1:])]
    return math.sqrt(statistics.fmean(diff * diff for diff in diffs))


def percentile(values: Iterable[Optional[float]], pct: float) -> Optional[float]:
    vals = sorted(finite(values))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    poster.ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def safe_payload_path(bundle_dir: Path, payload_file: object) -> Optional[Path]:
    if not isinstance(payload_file, str) or not payload_file:
        return None
    path = Path(payload_file)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    return bundle_dir / path


def classify_plane(stream: dict[str, object]) -> Optional[str]:
    plane = str(stream.get("plane", "")).strip().upper()
    if plane in {"H", "V"}:
        return plane
    key = str(stream.get("stream_key", ""))
    if ":HP" in key and ("TBT_POSITION" in key):
        return "H"
    if ":VP" in key and ("TBT_POSITION" in key):
        return "V"
    return None


def int_or_none(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_bundle(manifest_path: Path) -> SpillBundle:
    bundle_dir = manifest_path.parent
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    streams: list[StreamMeta] = []
    warnings = [str(item) for item in data.get("warnings", []) if str(item)]
    for item in data.get("streams", []):
        if not isinstance(item, dict):
            warnings.append("manifest stream entry is not an object")
            continue
        plane = classify_plane(item)
        if plane is None:
            continue
        payload_path = safe_payload_path(bundle_dir, item.get("payload_file"))
        if payload_path is None:
            warnings.append(f"unsafe or missing payload_file for {item.get('stream_key', '')}")
            continue
        sample_count = int_or_none(item.get("sample_count"))
        payload_bytes = int_or_none(item.get("payload_bytes"))
        if sample_count is None and payload_bytes is not None:
            sample_count = payload_bytes // 4
        if sample_count is None:
            warnings.append(f"missing sample_count for {item.get('stream_key', '')}")
            continue
        streams.append(
            StreamMeta(
                bpm_ip=str(item.get("bpm_ip", "")),
                stream_key=str(item.get("stream_key", "")),
                plane=plane,
                stream_id=str(item.get("stream_id", "")),
                stream_ms=int_or_none(item.get("stream_ms")) or 0,
                payload_path=payload_path,
                sample_count=sample_count,
                payload_bytes=payload_bytes,
                aligned=item.get("aligned") if isinstance(item.get("aligned"), bool) else None,
            )
        )
    target_ms = int_or_none(data.get("target_ms"))
    if target_ms is None:
        raise ValueError(f"{manifest_path} has no target_ms")
    return SpillBundle(
        bundle_dir=bundle_dir,
        run_name=bundle_dir.parent.name,
        target_ms=target_ms,
        requested_streams=int_or_none(data.get("requested_streams")),
        streams=streams,
        warnings=warnings,
    )


def discover_manifests(inputs: Sequence[Path], manifest_lists: Sequence[Path] = ()) -> list[Path]:
    found: dict[str, Path] = {}
    for list_path in manifest_lists:
        with list_path.open() as handle:
            for line in handle:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                manifest = Path(text)
                if manifest.name != "manifest.json":
                    continue
                found[str(manifest.resolve())] = manifest
    for input_path in inputs:
        if input_path.is_file() and input_path.name == "manifest.json":
            found[str(input_path.resolve())] = input_path
        elif input_path.is_dir() and (input_path / "manifest.json").exists():
            manifest = input_path / "manifest.json"
            found[str(manifest.resolve())] = manifest
        elif input_path.is_dir():
            for manifest in input_path.rglob("manifest.json"):
                found[str(manifest.resolve())] = manifest
    return sorted(found.values())


def consensus_length(streams: Sequence[StreamMeta]) -> Optional[int]:
    if not streams:
        return None
    counts = Counter(stream.sample_count for stream in streams)
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def preprocess_traces(
    traces: np.ndarray,
    normalization: str,
    injection_window_turns: int,
) -> np.ndarray:
    if traces.size == 0 or normalization == "none":
        return traces
    working = np.asarray(traces, dtype=np.float32).copy()
    if normalization == "rms_per_bpm":
        scale = np.sqrt(np.mean(working * working, axis=1, keepdims=True))
    elif normalization == "injection_rms_per_bpm":
        window = max(1, min(int(injection_window_turns), working.shape[1]))
        head = working[:, :window]
        scale = np.sqrt(np.mean(head * head, axis=1, keepdims=True))
    elif normalization == "mad_per_bpm":
        center = np.median(working, axis=1, keepdims=True)
        scale = np.median(np.abs(working - center), axis=1, keepdims=True) * 1.4826
    else:
        raise ValueError(f"unsupported bpm normalization {normalization}")
    scale = np.where(np.isfinite(scale) & (scale > 1e-12), scale, 1.0)
    return working / scale.astype(np.float32)


def load_plane_traces(
    bundle: SpillBundle,
    plane: str,
    max_traces: Optional[int],
    aligned_only: bool,
    turn_start: int,
    turn_end: Optional[int],
    normalization: str,
    injection_window_turns: int,
    label_mode: str = "bpm_ip",
) -> tuple[Optional[np.ndarray], int, list[str], list[str], Optional[np.ndarray]]:
    warnings: list[str] = []
    streams = [stream for stream in bundle.streams if stream.plane == plane]
    if aligned_only:
        streams = [stream for stream in streams if stream.aligned is not False]
    length = consensus_length(streams)
    if length is None:
        return None, 0, [f"plane {plane} has no streams"], [], None
    streams = [stream for stream in streams if stream.sample_count == length]
    if max_traces is not None and max_traces > 0 and len(streams) > max_traces:
        streams = streams[:max_traces]
    arrays: list[np.ndarray] = []
    labels: list[str] = []
    for stream in streams:
        if not stream.payload_path.exists():
            warnings.append(f"missing payload {stream.payload_path}")
            continue
        if stream.payload_bytes is not None:
            try:
                size = stream.payload_path.stat().st_size
            except OSError as exc:
                warnings.append(f"cannot stat {stream.payload_path}: {exc}")
                continue
            if size != stream.payload_bytes:
                warnings.append(
                    f"{stream.stream_key} payload byte count {size} differs from manifest {stream.payload_bytes}"
                )
                continue
        data = np.fromfile(stream.payload_path, dtype="<f4", count=length)
        if data.size != length:
            warnings.append(f"{stream.stream_key} decoded {data.size} samples, expected {length}")
            continue
        start = max(0, int(turn_start))
        end = int(turn_end) if turn_end is not None else data.size
        end = max(start, min(end, data.size))
        if start >= end:
            warnings.append(f"{stream.stream_key} turn range {turn_start}:{turn_end} is empty")
            continue
        arrays.append(np.asarray(data[start:end], dtype=np.float32))
        labels.append(stream.stream_key if label_mode == "stream_key" else stream.bpm_ip or stream.stream_key)
    if not arrays:
        return None, length, warnings + [f"plane {plane} has no readable payloads"], [], None
    traces = np.stack(arrays, axis=0)
    ranking_rms = np.sqrt(np.mean(np.asarray(traces, dtype=np.float64) ** 2, axis=1))
    traces = preprocess_traces(traces, normalization, injection_window_turns)
    return traces, traces.shape[1], warnings, labels, ranking_rms


def hann_window(xp, n: int):
    if n <= 1:
        return xp.ones((n,), dtype=xp.float32)
    idx = xp.arange(n, dtype=xp.float32)
    return (0.5 - 0.5 * xp.cos((2.0 * math.pi * idx) / (n - 1))).astype(xp.float32)


_TAPER_CACHE: dict[tuple[int, float, int], tuple[np.ndarray, str]] = {}


def multitaper_tapers_cpu(n: int, nw: float, k: int) -> tuple[np.ndarray, str]:
    key = (n, float(nw), int(k))
    cached = _TAPER_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from scipy.signal.windows import dpss  # type: ignore

        tapers = np.asarray(dpss(n, NW=nw, Kmax=k, sym=False, norm=2), dtype=np.float32)
        source = "scipy_dpss"
    except Exception:
        # Spark/local review environments may not have SciPy. Compute Slepian
        # sequences directly from the discrete spectral concentration matrix so
        # multitaper still means DPSS rather than a generic orthogonal window.
        half_bandwidth = float(nw) / float(n)
        grid = np.arange(n, dtype=np.float64)
        lag = grid[:, None] - grid[None, :]
        kernel = np.empty((n, n), dtype=np.float64)
        zero = lag == 0.0
        kernel[zero] = 2.0 * half_bandwidth
        kernel[~zero] = np.sin(2.0 * math.pi * half_bandwidth * lag[~zero]) / (math.pi * lag[~zero])
        eigvals, eigvecs = np.linalg.eigh(kernel)
        order = np.argsort(eigvals)[::-1][:k]
        tapers = eigvecs[:, order].T
        for idx in range(tapers.shape[0]):
            norm = math.sqrt(float(np.sum(tapers[idx] * tapers[idx]))) or 1.0
            tapers[idx] = tapers[idx] / norm
            max_idx = int(np.argmax(np.abs(tapers[idx])))
            if tapers[idx, max_idx] < 0.0:
                tapers[idx] = -tapers[idx]
        tapers = tapers.astype(np.float32)
        source = "numpy_slepian"
    _TAPER_CACHE[key] = (tapers, source)
    return tapers, source


def requested_spectrogram_methods(args: argparse.Namespace) -> list[str]:
    if args.spectrogram_method == "both":
        return ["hann", "multitaper"]
    return [args.spectrogram_method]


def baseline_method(args: argparse.Namespace) -> str:
    methods = requested_spectrogram_methods(args)
    return "hann" if "hann" in methods else methods[0]


def ridge_source_method(args: argparse.Namespace) -> str:
    methods = requested_spectrogram_methods(args)
    if args.ridge_source_method != "auto":
        return args.ridge_source_method if args.ridge_source_method in methods else baseline_method(args)
    return "multitaper" if "multitaper" in methods else baseline_method(args)


def select_trace_subset(
    traces_np: np.ndarray,
    mode: str,
    ranking_scores: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, float]:
    if traces_np.size == 0:
        return traces_np, 0.0
    count = traces_np.shape[0]
    if mode in {"mean", "median", "trimmed_mean_10pct"}:
        return traces_np, 1.0 / max(1, count)
    if mode == "odd_even":
        subset = traces_np[::2]
        return subset, subset.shape[0] / max(1, count)
    if mode == "first_second_half":
        subset = traces_np[: max(1, count // 2)]
        return subset, subset.shape[0] / max(1, count)

    rms = (
        np.asarray(ranking_scores, dtype=np.float64)
        if ranking_scores is not None and len(ranking_scores) == count
        else np.sqrt(np.mean(np.asarray(traces_np, dtype=np.float64) ** 2, axis=1))
    )
    order = np.argsort(rms)[::-1]
    if mode == "best_single_bpm":
        selected = order[:1]
    elif mode == "top5_by_confidence":
        selected = order[: min(5, count)]
    elif mode == "top10_by_confidence":
        selected = order[: min(10, count)]
    elif mode == "top20_by_confidence":
        selected = order[: min(20, count)]
    else:
        raise ValueError(f"unsupported BPM combination {mode}")
    return traces_np[selected], len(selected) / max(1, count)


def detrend_windows(xp, windows, mode: str):
    if mode == "none":
        return windows
    if mode == "mean_subtract":
        means = xp.mean(windows, axis=2, keepdims=True)
        return windows - means
    if mode == "linear":
        n = windows.shape[2]
        x = xp.linspace(-1.0, 1.0, n, dtype=xp.float32).reshape((1, 1, n))
        means = xp.mean(windows, axis=2, keepdims=True)
        centered = windows - means
        denom = xp.sum(x * x)
        slope = xp.sum(centered * x, axis=2, keepdims=True) / denom
        return centered - slope * x
    if mode == "polynomial_order_2":
        n = int(windows.shape[2])
        x_np = np.linspace(-1.0, 1.0, n, dtype=np.float32)
        design = np.stack([np.ones_like(x_np), x_np, x_np * x_np], axis=1)
        pinv = np.linalg.pinv(design).astype(np.float32)
        design_xp = xp.asarray(design, dtype=xp.float32)
        pinv_xp = xp.asarray(pinv.T, dtype=xp.float32)
        coeff = xp.tensordot(windows, pinv_xp, axes=([2], [0]))
        trend = xp.sum(coeff[:, :, None, :] * design_xp.reshape((1, 1, n, 3)), axis=3)
        return windows - trend
    raise ValueError(f"unsupported detrend mode {mode}")


def combine_power(xp, power_by_trace, mode: str):
    if mode == "median":
        return xp.median(power_by_trace, axis=0)
    if mode == "trimmed_mean_10pct":
        count = int(power_by_trace.shape[0])
        if count < 10:
            return xp.mean(power_by_trace, axis=0)
        sorted_power = xp.sort(power_by_trace, axis=0)
        trim = max(1, count // 10)
        return xp.mean(sorted_power[trim : count - trim], axis=0)
    return xp.mean(power_by_trace, axis=0)


def average_spectra(
    traces_np: np.ndarray,
    starts: Sequence[int],
    window_turns: int,
    backend: FftBackend,
    window_chunk: int,
    method: str = "hann",
    bpm_combination: str = "mean",
    detrend: str = "mean_subtract",
    dc_handling: str = "zero_dc_bin",
    multitaper_nw: float = 2.5,
    multitaper_k: int = 4,
    timers: Optional[dict[str, float]] = None,
    ranking_scores: Optional[np.ndarray] = None,
) -> np.ndarray:
    if traces_np.size == 0 or not starts:
        return np.empty((0, window_turns), dtype=np.float32)
    traces_np, _dominant_fraction = select_trace_subset(traces_np, bpm_combination, ranking_scores)
    xp = backend.xp
    traces = xp.asarray(traces_np, dtype=xp.float32)
    offsets = xp.arange(window_turns, dtype=xp.int64)
    if method == "hann":
        tapers = hann_window(xp, window_turns).reshape((1, 1, 1, window_turns))
        taper_count = 1
    elif method == "multitaper":
        tapers_np, source = multitaper_tapers_cpu(window_turns, multitaper_nw, multitaper_k)
        tapers = xp.asarray(tapers_np, dtype=xp.float32).reshape((tapers_np.shape[0], 1, 1, window_turns))
        taper_count = int(tapers_np.shape[0])
        if timers is not None:
            timers["multitaper_taper_count"] = taper_count
            timers["multitaper_taper_source"] = source
    else:
        raise ValueError(f"unsupported spectrogram method {method}")
    out: list[np.ndarray] = []
    for pos in range(0, len(starts), max(1, window_chunk)):
        chunk = list(starts[pos : pos + max(1, window_chunk)])
        idx = xp.asarray(chunk, dtype=xp.int64).reshape((-1, 1)) + offsets.reshape((1, -1))
        windows = traces[:, idx]
        signals = detrend_windows(xp, windows, detrend)[None, :, :, :] * tapers
        fft_values = xp.fft.fft(signals, axis=3)
        power = xp.real(fft_values * xp.conj(fft_values)).astype(xp.float32)
        power_by_trace = xp.mean(power, axis=0)
        avg = combine_power(xp, power_by_trace, bpm_combination)
        if dc_handling in {"zero_dc_bin", "ignore_low_bins"}:
            avg[:, 0] = 0.0
        if dc_handling == "ignore_low_bins":
            avg[:, :MIN_PEAK_SEARCH_BIN] = 0.0
        out.append(backend.to_numpy(avg).astype(np.float32, copy=False))
    backend.synchronize()
    if timers is not None:
        timers[f"{method}_trace_window_ffts"] = timers.get(f"{method}_trace_window_ffts", 0.0) + (
            len(starts) * traces_np.shape[0] * taper_count
        )
    return np.concatenate(out, axis=0)


def pick_peak_in_band(
    spectrum: np.ndarray,
    band: tuple[float, float],
    min_peak_confidence: float,
) -> Optional[PeakResult]:
    n = int(spectrum.shape[0])
    if n < 8:
        return None
    start_idx = int(math.floor(band[0] * n))
    end_idx = int(math.ceil(band[1] * n))
    start_idx = min(max(start_idx, MIN_PEAK_SEARCH_BIN), max(0, n - 2))
    end_idx = min(max(end_idx, 2), max(0, n - 1))
    if end_idx <= start_idx:
        return None
    band_values = spectrum[start_idx:end_idx]
    if band_values.size == 0:
        return None
    rel_idx = int(np.argmax(band_values))
    idx = start_idx + rel_idx
    best_power = float(spectrum[idx])
    if not math.isfinite(best_power):
        return None
    refined_idx = float(idx)
    if 0 < idx + 1 < n:
        y1 = float(spectrum[idx - 1])
        y2 = float(spectrum[idx])
        y3 = float(spectrum[idx + 1])
        denom = y1 - 2.0 * y2 + y3
        if abs(denom) > 1e-15:
            delta = 0.5 * (y1 - y3) / denom
            if math.isfinite(delta) and abs(delta) <= 1.0:
                refined_idx += delta
    tune = min(max(refined_idx / n, 0.0), 1.0)
    sorted_band = np.sort(band_values)
    median_power = max(float(sorted_band[sorted_band.size // 2]), 1e-12)
    confidence = best_power / median_power
    if confidence < min_peak_confidence:
        return None
    return PeakResult(
        tune=tune,
        confidence=confidence,
        peak_power=best_power,
        median_power=median_power,
        prominence=best_power - median_power,
    )


def subset_trace_pairs(traces: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    count = int(traces.shape[0])
    pairs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if count >= 4:
        pairs["odd_even"] = (traces[::2], traces[1::2])
    if count >= 4:
        mid = count // 2
        pairs["first_second_half"] = (traces[:mid], traces[mid:])
    return {name: pair for name, pair in pairs.items() if pair[0].size and pair[1].size}


def injection_peak_for_subset(
    traces: np.ndarray,
    args: argparse.Namespace,
    backend: FftBackend,
    band: tuple[float, float],
    timers: dict[str, float],
    injection_window_turns: int,
) -> Optional[PeakResult]:
    spectra = average_spectra(
        traces,
        [args.injection_start_turn],
        injection_window_turns,
        backend,
        args.window_chunk,
        method=baseline_method(args),
        bpm_combination="mean",
        detrend=args.detrend,
        dc_handling=args.dc_handling,
        multitaper_nw=args.multitaper_nw,
        multitaper_k=args.multitaper_k,
        timers=timers,
    )
    if spectra.size == 0:
        return None
    return pick_peak_in_band(spectra[0], band, args.min_peak_confidence)


def compute_subset_deltas(
    traces: np.ndarray,
    args: argparse.Namespace,
    backend: FftBackend,
    band: tuple[float, float],
    timers: dict[str, float],
    injection_window_turns: int,
) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {}
    for name, (left, right) in subset_trace_pairs(traces).items():
        left_peak = injection_peak_for_subset(left, args, backend, band, timers, injection_window_turns)
        right_peak = injection_peak_for_subset(right, args, backend, band, timers, injection_window_turns)
        if left_peak is None or right_peak is None:
            out[f"{name}_delta"] = None
        else:
            out[f"{name}_delta"] = abs(left_peak.tune - right_peak.tune)
    deltas = [value for value in out.values() if value is not None]
    out["subset_delta"] = max(deltas) if deltas else None
    return out


def per_bpm_injection_observations(
    labels: Sequence[str],
    traces: np.ndarray,
    args: argparse.Namespace,
    backend: FftBackend,
    band: tuple[float, float],
    timers: dict[str, float],
    injection_window_turns: int,
) -> list[dict[str, object]]:
    if traces.size == 0 or not labels:
        return []
    start = max(0, int(args.injection_start_turn))
    end = start + int(injection_window_turns)
    if start >= traces.shape[1] or end > traces.shape[1]:
        return []
    t0 = time.perf_counter()
    xp = backend.xp
    segment_np = np.asarray(traces[:, start:end], dtype=np.float32)
    rms = np.sqrt(np.mean(segment_np.astype(np.float64) * segment_np.astype(np.float64), axis=1))
    segment = xp.asarray(segment_np, dtype=xp.float32).reshape((segment_np.shape[0], 1, injection_window_turns))
    windows = detrend_windows(xp, segment, args.detrend)[:, 0, :]
    windows = windows * hann_window(xp, injection_window_turns).reshape((1, injection_window_turns))
    fft_values = xp.fft.fft(windows, axis=1)
    power = xp.real(fft_values * xp.conj(fft_values)).astype(xp.float32)
    if args.dc_handling in {"zero_dc_bin", "ignore_low_bins"}:
        power[:, 0] = 0.0
    if args.dc_handling == "ignore_low_bins":
        power[:, :MIN_PEAK_SEARCH_BIN] = 0.0
    power_np = backend.to_numpy(power)
    backend.synchronize()
    timers["fft_seconds"] += time.perf_counter() - t0
    timers["bpm_leaderboard_ffts"] = timers.get("bpm_leaderboard_ffts", 0.0) + segment_np.shape[0]

    rows: list[dict[str, object]] = []
    for idx, spectrum in enumerate(power_np):
        peak = pick_peak_in_band(spectrum, band, 0.0)
        rows.append(
            {
                "bpm_label": labels[idx] if idx < len(labels) else f"bpm_{idx + 1}",
                "injection_tune": peak.tune if peak else None,
                "injection_confidence": peak.confidence if peak else None,
                "injection_prominence": peak.prominence if peak else None,
                "injection_rms": float(rms[idx]) if idx < len(rms) else None,
            }
        )
    return rows


def resolved_flash_count(requested: int, total_turns: int, window_turns: int) -> int:
    if window_turns <= 0:
        return 0
    max_flashes = max(1, total_turns // window_turns)
    return min(max(1, requested), max_flashes)


def flash_window_starts(total_turns: int, window_turns: int, flash_count: int) -> list[int]:
    if window_turns <= 0 or window_turns > total_turns:
        return []
    last_start = total_turns - window_turns
    half_window = window_turns // 2
    min_center = half_window
    max_center = last_start + half_window
    starts: list[int] = []
    for idx in range(flash_count):
        numerator = 2 * idx + 1
        denom = max(1, 2 * flash_count)
        center = numerator * total_turns // denom
        clamped_center = min(max(center, min_center), max_center)
        start = min(max(0, clamped_center - half_window), last_start)
        if not starts or starts[-1] != start:
            starts.append(start)
    if not starts:
        starts.append(0)
    return starts


def sliding_window_starts(
    total_turns: int,
    window_turns: int,
    stride_turns: int,
    flash_count: Optional[int],
) -> list[int]:
    if window_turns <= 0 or window_turns > total_turns:
        return []
    if flash_count is not None:
        return flash_window_starts(
            total_turns,
            window_turns,
            resolved_flash_count(flash_count, total_turns, window_turns),
        )
    return list(range(0, total_turns - window_turns + 1, max(1, stride_turns)))


def local_tracking_band(
    global_band: tuple[float, float],
    trusted: Optional[float],
    half_width: float,
) -> Optional[tuple[float, float]]:
    if trusted is None or not math.isfinite(trusted) or half_width <= 0.0:
        return None
    local_min = max(global_band[0], trusted - half_width)
    local_max = min(global_band[1], trusted + half_width)
    if local_max <= local_min:
        return None
    return (local_min, local_max)


def band_indices(n: int, band: tuple[float, float]) -> tuple[int, int]:
    start_idx = max(MIN_PEAK_SEARCH_BIN, int(math.floor(band[0] * n)))
    end_idx = min(n - 1, int(math.ceil(band[1] * n)))
    return start_idx, end_idx


def normalize_ridge_score(score: np.ndarray, mode: str) -> np.ndarray:
    out = np.array(score, dtype=np.float64, copy=True)
    if mode == "none":
        return out
    if mode == "global":
        mean = float(np.nanmean(out))
        std = float(np.nanstd(out)) or 1.0
        return (out - mean) / std
    if mode == "row":
        mean = np.nanmean(out, axis=1, keepdims=True)
        std = np.nanstd(out, axis=1, keepdims=True)
        std[std == 0.0] = 1.0
        return (out - mean) / std
    raise ValueError(f"unsupported ridge normalization {mode}")


def extract_dp_ridge(
    spectra: np.ndarray,
    starts: Sequence[int],
    window_turns: int,
    band: tuple[float, float],
    args: argparse.Namespace,
    anchor_tune: Optional[float] = None,
) -> list[dict[str, object]]:
    if spectra.size == 0:
        return []
    start_idx, end_idx = band_indices(spectra.shape[1], band)
    if end_idx <= start_idx:
        return []
    band_power = np.maximum(spectra[:, start_idx:end_idx], 1e-30)
    spectral_score = normalize_ridge_score(np.log(band_power), args.ridge_normalize)
    tune_bins = np.arange(start_idx, end_idx, dtype=np.float64) / float(spectra.shape[1])
    n_steps, n_bins = spectral_score.shape
    if args.ridge_anchor_enabled and anchor_tune is not None and n_steps > 0:
        anchor_distance = np.maximum(0.0, np.abs(tune_bins - anchor_tune) - args.ridge_anchor_half_width)
        spectral_score[0, :] -= args.ridge_anchor_penalty * anchor_distance
    dp = np.empty_like(spectral_score, dtype=np.float64)
    back = np.zeros((n_steps, n_bins), dtype=np.int32)
    dp[0, :] = spectral_score[0, :]
    hard_step = args.ridge_max_step if args.ridge_max_step > 0 else None
    for step in range(1, n_steps):
        delta = np.abs(tune_bins.reshape((1, -1)) - tune_bins.reshape((-1, 1)))
        penalty = args.ridge_jump_penalty * delta + args.ridge_jump2_penalty * delta * delta
        transition = dp[step - 1, :].reshape((-1, 1)) - penalty
        if hard_step is not None:
            transition = np.where(delta <= hard_step, transition, -np.inf)
        prev = np.argmax(transition, axis=0)
        dp[step, :] = spectral_score[step, :] + transition[prev, np.arange(n_bins)]
        back[step, :] = prev.astype(np.int32)
    path = np.zeros(n_steps, dtype=np.int32)
    path[-1] = int(np.argmax(dp[-1, :]))
    for step in range(n_steps - 1, 0, -1):
        path[step - 1] = back[step, path[step]]
    rows: list[dict[str, object]] = []
    for idx, local_bin in enumerate(path):
        global_bin = start_idx + int(local_bin)
        ridge_tune = global_bin / float(spectra.shape[1])
        power = float(spectra[idx, global_bin])
        rows.append(
            {
                "window_index": idx,
                "center_turn": starts[idx] + window_turns // 2,
                "ridge_tune": ridge_tune,
                "ridge_power": power,
                "ridge_score": float(spectral_score[idx, local_bin]),
            }
        )
    return rows


def extract_greedy_ridge(
    spectra: np.ndarray,
    starts: Sequence[int],
    window_turns: int,
    band: tuple[float, float],
    min_peak_confidence: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for idx, spectrum in enumerate(spectra):
        peak = pick_peak_in_band(spectrum, band, min_peak_confidence)
        rows.append(
            {
                "window_index": idx,
                "center_turn": starts[idx] + window_turns // 2,
                "ridge_tune": peak.tune if peak else None,
                "ridge_power": peak.peak_power if peak else None,
                "ridge_score": peak.confidence if peak else None,
            }
        )
    return rows


def enrich_points_with_ridge(
    points: list[dict[str, object]],
    spectra: np.ndarray,
    ridge_points: list[dict[str, object]],
    band: tuple[float, float],
    min_peak_confidence: float,
    anchor_tune: Optional[float] = None,
) -> None:
    edge_width = 1.0 / max(1, spectra.shape[1]) if spectra.size else 0.0
    for point, ridge, spectrum in zip(points, ridge_points, spectra):
        local_peak = pick_peak_in_band(spectrum, band, min_peak_confidence)
        selected = float_or_none(point.get("selected_tune"))
        ridge_tune = float_or_none(ridge.get("ridge_tune"))
        point["dp_ridge_tune"] = ridge_tune
        point["dp_ridge_power"] = ridge.get("ridge_power", "")
        point["dp_ridge_score"] = ridge.get("ridge_score", "")
        point["delta_ridge_vs_selected"] = (
            abs(ridge_tune - selected) if ridge_tune is not None and selected is not None else None
        )
        point["band_edge"] = (
            bool(selected is not None and (selected <= band[0] + edge_width or selected >= band[1] - edge_width))
        )
        point["ridge_anchor_distance"] = (
            abs(ridge_tune - anchor_tune) if ridge_tune is not None and anchor_tune is not None else None
        )
        point["ridge_prominence"] = local_peak.prominence if local_peak else None
        ridge["local_peak_tune"] = local_peak.tune if local_peak else None
        ridge["selected_tune_baseline"] = selected
        ridge["delta_vs_baseline"] = point["delta_ridge_vs_selected"]


def ridge_anchor_for_plane(args: argparse.Namespace, plane: str) -> Optional[float]:
    if not args.ridge_anchor_enabled:
        return None
    return args.ridge_anchor_h if plane == "H" else args.ridge_anchor_v


def analyze_plane(
    bundle: SpillBundle,
    plane: str,
    traces: np.ndarray,
    bpm_labels: Sequence[str],
    consensus_turns: int,
    args: argparse.Namespace,
    backend: FftBackend,
    band: tuple[float, float],
    compute_timer: dict[str, float],
    ranking_scores: Optional[np.ndarray] = None,
) -> PlaneAnalysis:
    warnings: list[str] = []
    selected_traces, dominant_fraction = select_trace_subset(traces, args.bpm_combination, ranking_scores)
    injection_window_turns = args.sliding_window_turns if args.flashes is not None else args.injection_window_turns
    bpm_observations = per_bpm_injection_observations(
        bpm_labels,
        traces,
        args,
        backend,
        band,
        compute_timer,
        injection_window_turns,
    )
    required_turns = max(args.injection_start_turn + injection_window_turns, args.sliding_window_turns)
    if consensus_turns < required_turns:
        warnings.append(f"plane {plane} consensus turn count {consensus_turns} is smaller than required {required_turns}")
        return PlaneAnalysis(plane, selected_traces.shape[0], consensus_turns, None, [], None, 0, 0, 0, dominant_bpm_fraction=dominant_fraction, bpm_observations=bpm_observations, warnings=warnings)

    t0 = time.perf_counter()
    injection_spectra = average_spectra(
        traces,
        [args.injection_start_turn],
        injection_window_turns,
        backend,
        args.window_chunk,
        method=baseline_method(args),
        bpm_combination=args.bpm_combination,
        detrend=args.detrend,
        dc_handling=args.dc_handling,
        multitaper_nw=args.multitaper_nw,
        multitaper_k=args.multitaper_k,
        timers=compute_timer,
        ranking_scores=ranking_scores,
    )
    elapsed_fft = time.perf_counter() - t0
    compute_timer["fft_seconds"] += elapsed_fft
    compute_timer[f"{baseline_method(args)}_fft_seconds"] = compute_timer.get(f"{baseline_method(args)}_fft_seconds", 0.0) + elapsed_fft
    injection_peak = pick_peak_in_band(injection_spectra[0], band, args.min_peak_confidence)
    if injection_peak is None:
        warnings.append(f"plane {plane} had no injection peak in configured tune band")
    subset_t0 = time.perf_counter()
    subset_deltas = compute_subset_deltas(
        traces,
        args,
        backend,
        band,
        compute_timer,
        injection_window_turns,
    )
    compute_timer["fft_seconds"] += time.perf_counter() - subset_t0

    starts = sliding_window_starts(consensus_turns, args.sliding_window_turns, args.sliding_stride_turns, args.flashes)
    if not starts:
        warnings.append(f"plane {plane} has no sliding windows")
        return PlaneAnalysis(plane, selected_traces.shape[0], consensus_turns, injection_peak, [], None, 0, 0, 0, dominant_bpm_fraction=dominant_fraction, bpm_observations=bpm_observations, warnings=warnings)

    if args.flashes is not None:
        expected = resolved_flash_count(args.flashes, consensus_turns, args.sliding_window_turns)
        if len(starts) < expected:
            warnings.append(
                f"plane {plane} flash sampling reduced from requested {args.flashes} to {len(starts)} windows"
            )

    spectra_by_method: dict[str, np.ndarray] = {}
    for method in requested_spectrogram_methods(args):
        t0 = time.perf_counter()
        spectra_by_method[method] = average_spectra(
            traces,
            starts,
            args.sliding_window_turns,
            backend,
            args.window_chunk,
            method=method,
            bpm_combination=args.bpm_combination,
            detrend=args.detrend,
            dc_handling=args.dc_handling,
            multitaper_nw=args.multitaper_nw,
            multitaper_k=args.multitaper_k,
            timers=compute_timer,
            ranking_scores=ranking_scores,
        )
        elapsed_fft = time.perf_counter() - t0
        compute_timer["fft_seconds"] += elapsed_fft
        compute_timer[f"{method}_fft_seconds"] = compute_timer.get(f"{method}_fft_seconds", 0.0) + elapsed_fft
    spectra = spectra_by_method[baseline_method(args)]
    compute_timer["windows"] += len(starts) * selected_traces.shape[0]

    previous_trusted = injection_peak.tune if injection_peak is not None else None
    fallback_count = 0
    suspicious_count = 0
    missing_seed_count = 0
    points: list[dict[str, object]] = []
    for idx, (start, spectrum) in enumerate(zip(starts, spectra)):
        raw_peak = pick_peak_in_band(spectrum, band, args.min_peak_confidence)
        tracked_peak: Optional[PeakResult] = None
        selected_peak: Optional[PeakResult]
        used_global_fallback = False
        suspicious_step = False
        step_delta: Optional[float] = None

        if not args.enable_tracking:
            selected_peak = raw_peak
        elif previous_trusted is not None:
            local_band = local_tracking_band(band, previous_trusted, args.track_half_width)
            if local_band is not None:
                tracked_peak = pick_peak_in_band(spectrum, local_band, args.min_peak_confidence)
            if tracked_peak is not None:
                selected_peak = tracked_peak
            else:
                selected_peak = raw_peak
                if selected_peak is not None:
                    used_global_fallback = True
                    fallback_count += 1
            if selected_peak is not None:
                step_delta = abs(selected_peak.tune - previous_trusted)
                if step_delta > args.max_tune_step_per_window:
                    suspicious_step = True
                    suspicious_count += 1
                elif not used_global_fallback:
                    previous_trusted = selected_peak.tune
        else:
            selected_peak = raw_peak
            missing_seed_count += 1

        points.append(
            {
                "plane": plane,
                "window_index": idx,
                "center_turn": start + args.sliding_window_turns // 2,
                "raw_global_tune": raw_peak.tune if raw_peak else None,
                "tracked_local_tune": tracked_peak.tune if tracked_peak else None,
                "selected_tune": selected_peak.tune if selected_peak else None,
                "raw_global_confidence": raw_peak.confidence if raw_peak else None,
                "selected_confidence": selected_peak.confidence if selected_peak else None,
                "used_global_fallback": used_global_fallback,
                "suspicious_step": suspicious_step,
                "step_delta": step_delta,
            }
        )

    ridge_spectra = spectra_by_method[ridge_source_method(args)]
    ridge_t0 = time.perf_counter()
    anchor_tune = ridge_anchor_for_plane(args, plane)
    if args.ridge_method == "dp":
        ridge_points = extract_dp_ridge(ridge_spectra, starts, args.sliding_window_turns, band, args, anchor_tune)
    else:
        ridge_points = extract_greedy_ridge(ridge_spectra, starts, args.sliding_window_turns, band, args.min_peak_confidence)
    compute_timer["ridge_seconds"] = compute_timer.get("ridge_seconds", 0.0) + (time.perf_counter() - ridge_t0)
    enrich_points_with_ridge(points, ridge_spectra, ridge_points, band, args.min_peak_confidence, anchor_tune)

    band_start, band_end = band_indices(args.sliding_window_turns, band)
    band_spectra = None
    band_spectra_by_method: dict[str, np.ndarray] = {}
    if band_end > band_start:
        for method, method_spectra in spectra_by_method.items():
            band_spectra_by_method[method] = np.log10(
                np.maximum(method_spectra[:, band_start:band_end], 1e-30)
            ).astype(np.float32, copy=False)
        band_spectra = band_spectra_by_method[baseline_method(args)]

    return PlaneAnalysis(
        plane=plane,
        used_streams=selected_traces.shape[0],
        consensus_turns=consensus_turns,
        injection_peak=injection_peak,
        sliding_points=points,
        band_spectra=band_spectra,
        fallback_count=fallback_count,
        suspicious_count=suspicious_count,
        missing_seed_count=missing_seed_count,
        spectra_by_method=spectra_by_method,
        band_spectra_by_method=band_spectra_by_method,
        ridge_points=ridge_points,
        dominant_bpm_fraction=dominant_fraction,
        subset_deltas=subset_deltas,
        bpm_observations=bpm_observations,
        warnings=warnings,
    )


def selected_tunes(analysis: Optional[PlaneAnalysis]) -> list[float]:
    if analysis is None:
        return []
    return finite(point.get("selected_tune") for point in analysis.sliding_points)  # type: ignore[arg-type]


def selected_confidences(analysis: Optional[PlaneAnalysis]) -> list[float]:
    if analysis is None:
        return []
    return finite(point.get("selected_confidence") for point in analysis.sliding_points)  # type: ignore[arg-type]


def analysis_metrics(analysis: Optional[PlaneAnalysis]) -> dict[str, object]:
    if analysis is None or not analysis.sliding_points:
        return {
            "finite_fraction": "",
            "median_abs_step": "",
            "p95_abs_step": "",
            "band_edge_fraction": "",
            "dominant_bpm_fraction": "",
            "ridge_prominence": "",
        }
    tunes = [float_or_none(point.get("selected_tune")) for point in analysis.sliding_points]
    finite_tunes = [value for value in tunes if value is not None]
    steps = [abs(b - a) for a, b in zip(finite_tunes, finite_tunes[1:])]
    band_edges = sum(1 for point in analysis.sliding_points if str(point.get("band_edge", "")).lower() == "true")
    prominences = finite(point.get("ridge_prominence") for point in analysis.sliding_points)  # type: ignore[arg-type]
    return {
        "finite_fraction": len(finite_tunes) / max(1, len(analysis.sliding_points)),
        "median_abs_step": median(steps),
        "p95_abs_step": percentile(steps, 0.95),
        "band_edge_fraction": band_edges / max(1, len(analysis.sliding_points)),
        "dominant_bpm_fraction": analysis.dominant_bpm_fraction,
        "ridge_prominence": median(prominences),
    }


def quality_flags(
    bundle: SpillBundle,
    h: Optional[PlaneAnalysis],
    v: Optional[PlaneAnalysis],
    plane_mode: str = "both",
) -> list[str]:
    flags: list[str] = []
    if plane_mode in {"both", "H"} and h is None:
        flags.append("missing_h")
    if plane_mode in {"both", "V"} and v is None:
        flags.append("missing_v")
    for label, analysis in (("h", h), ("v", v)):
        if analysis is None:
            continue
        if analysis.injection_peak is None:
            flags.append(f"no_injection_peak_{label}")
        if analysis.used_streams < 8:
            flags.append(f"low_stream_count_{label}")
        if analysis.sliding_points:
            fallback_fraction = analysis.fallback_count / len(analysis.sliding_points)
            suspicious_fraction = analysis.suspicious_count / len(analysis.sliding_points)
            if fallback_fraction > 0.25:
                flags.append(f"high_fallback_{label}")
            if suspicious_fraction > 0.25:
                flags.append(f"high_suspicious_{label}")
    if bundle.warnings:
        flags.append("manifest_warnings")
    return flags


def summary_row(
    idx: int,
    bundle: SpillBundle,
    h: Optional[PlaneAnalysis],
    v: Optional[PlaneAnalysis],
    args: argparse.Namespace,
) -> dict[str, object]:
    qx = selected_tunes(h)
    qy = selected_tunes(v)
    h_metrics = analysis_metrics(h)
    v_metrics = analysis_metrics(v)
    flags = quality_flags(bundle, h, v, args.plane)
    if args.plane == "H":
        usable = h is not None and h.injection_peak is not None and bool(qx)
    elif args.plane == "V":
        usable = v is not None and v.injection_peak is not None and bool(qy)
    else:
        usable = h is not None and v is not None and h.injection_peak is not None and v.injection_peak is not None
        usable = usable and bool(qx) and bool(qy)
    suitable = usable and not any(flag.startswith("high_") or flag.startswith("missing_") for flag in flags)
    warnings = list(bundle.warnings)
    if h is not None:
        warnings.extend(h.warnings)
    if v is not None:
        warnings.extend(v.warnings)
    return {
        "spill_index": idx,
        "run_name": bundle.run_name,
        "bundle_dir": str(bundle.bundle_dir),
        "target_ms": bundle.target_ms,
        "status": "ok" if usable else "partial",
        "usable_for_analysis": str(usable).lower(),
        "suitable_for_poster": str(suitable).lower(),
        "requested_streams": bundle.requested_streams if bundle.requested_streams is not None else "",
        "captured_streams": len(bundle.streams),
        "used_streams_h": h.used_streams if h else 0,
        "used_streams_v": v.used_streams if v else 0,
        "consensus_turns_h": h.consensus_turns if h else 0,
        "consensus_turns_v": v.consensus_turns if v else 0,
        "injection_start_turn": args.injection_start_turn,
        "injection_window_turns": args.sliding_window_turns if args.flashes is not None else args.injection_window_turns,
        "sliding_window_turns": args.sliding_window_turns,
        "sliding_stride_turns": args.sliding_stride_turns,
        "flash_count": args.flashes if args.flashes is not None else "",
        "plane_mode": args.plane,
        "turn_start": args.turn_start,
        "turn_end": args.turn_end if args.turn_end is not None else "",
        "bpm_combination": args.bpm_combination,
        "bpm_normalization": args.bpm_normalization,
        "detrend": args.detrend,
        "dc_handling": args.dc_handling,
        "ridge_anchor_enabled": str(args.ridge_anchor_enabled).lower(),
        "ridge_anchor_h": fmt_float(args.ridge_anchor_h),
        "ridge_anchor_v": fmt_float(args.ridge_anchor_v),
        "ridge_anchor_half_width": fmt_float(args.ridge_anchor_half_width),
        "qx_band_min": fmt_float(args.qx_min),
        "qx_band_max": fmt_float(args.qx_max),
        "qy_band_min": fmt_float(args.qy_min),
        "qy_band_max": fmt_float(args.qy_max),
        "qx_injection": fmt_float(h.injection_peak.tune if h and h.injection_peak else None),
        "qy_injection": fmt_float(v.injection_peak.tune if v and v.injection_peak else None),
        "confidence_h": fmt_float(h.injection_peak.confidence if h and h.injection_peak else None),
        "confidence_v": fmt_float(v.injection_peak.confidence if v and v.injection_peak else None),
        "median_qx": fmt_float(median(qx)),
        "median_qy": fmt_float(median(qy)),
        "std_qx": fmt_float(stdev(qx)),
        "std_qy": fmt_float(stdev(qy)),
        "min_qx": fmt_float(min(qx) if qx else None),
        "max_qx": fmt_float(max(qx) if qx else None),
        "min_qy": fmt_float(min(qy) if qy else None),
        "max_qy": fmt_float(max(qy) if qy else None),
        "sliding_fallback_count_h": h.fallback_count if h else 0,
        "sliding_fallback_count_v": v.fallback_count if v else 0,
        "sliding_suspicious_count_h": h.suspicious_count if h else 0,
        "sliding_suspicious_count_v": v.suspicious_count if v else 0,
        "sliding_missing_seed_count_h": h.missing_seed_count if h else 0,
        "sliding_missing_seed_count_v": v.missing_seed_count if v else 0,
        "sliding_windows_h": len(h.sliding_points) if h else 0,
        "sliding_windows_v": len(v.sliding_points) if v else 0,
        "finite_fraction_h": fmt_float(h_metrics.get("finite_fraction")),
        "finite_fraction_v": fmt_float(v_metrics.get("finite_fraction")),
        "median_abs_step_h": fmt_float(h_metrics.get("median_abs_step")),
        "median_abs_step_v": fmt_float(v_metrics.get("median_abs_step")),
        "p95_abs_step_h": fmt_float(h_metrics.get("p95_abs_step")),
        "p95_abs_step_v": fmt_float(v_metrics.get("p95_abs_step")),
        "band_edge_fraction_h": fmt_float(h_metrics.get("band_edge_fraction")),
        "band_edge_fraction_v": fmt_float(v_metrics.get("band_edge_fraction")),
        "dominant_bpm_fraction_h": fmt_float(h_metrics.get("dominant_bpm_fraction")),
        "dominant_bpm_fraction_v": fmt_float(v_metrics.get("dominant_bpm_fraction")),
        "odd_even_delta_h": fmt_float(h.subset_deltas.get("odd_even_delta") if h else None),
        "odd_even_delta_v": fmt_float(v.subset_deltas.get("odd_even_delta") if v else None),
        "first_second_half_delta_h": fmt_float(h.subset_deltas.get("first_second_half_delta") if h else None),
        "first_second_half_delta_v": fmt_float(v.subset_deltas.get("first_second_half_delta") if v else None),
        "subset_delta_h": fmt_float(h.subset_deltas.get("subset_delta") if h else None),
        "subset_delta_v": fmt_float(v.subset_deltas.get("subset_delta") if v else None),
        "ridge_prominence_h": fmt_float(h_metrics.get("ridge_prominence")),
        "ridge_prominence_v": fmt_float(v_metrics.get("ridge_prominence")),
        "quality_flags": "|".join(sorted(set(flags))),
        "warnings": "|".join(warnings),
    }


def sliding_rows(
    idx: int,
    bundle: SpillBundle,
    analyses: Sequence[Optional[PlaneAnalysis]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for analysis in analyses:
        if analysis is None:
            continue
        for point in analysis.sliding_points:
            row = {
                "spill_index": idx,
                "run_name": bundle.run_name,
                "target_ms": bundle.target_ms,
                "plane": analysis.plane,
            }
            for key in SLIDING_FIELDS:
                if key not in row:
                    value = point.get(key, "")
                    row[key] = fmt_float(value) if isinstance(value, float) else value
            rows.append(row)
    return rows


def flash_rows(spill_rows: Sequence[dict[str, object]], sliding: Sequence[dict[str, object]], requested: int) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in sliding:
        key = (str(row.get("target_ms", "")), str(row.get("run_name", "")), str(row.get("plane", "")))
        grouped.setdefault(key, []).append(row)
    rows: list[dict[str, object]] = []
    for (target_ms, run_name, plane), group in sorted(grouped.items(), key=lambda item: item[0]):
        group.sort(key=lambda row: int(row.get("window_index", 0)))
        values = finite(float_or_none(row.get("selected_tune")) for row in group)
        confidences = finite(float_or_none(row.get("selected_confidence")) for row in group)
        low_conf = sum(1 for value in confidences if value < 1.5)
        fallback = sum(1 for row in group if str(row.get("used_global_fallback", "")).lower() == "true")
        suspicious = sum(1 for row in group if str(row.get("suspicious_step", "")).lower() == "true")
        denom = max(1, len(group))
        rows.append(
            {
                "target_ms": target_ms,
                "run_name": run_name,
                "plane": plane,
                "requested_flashes": requested,
                "available_windows": len(group),
                "effective_flashes": min(requested, len(group)),
                "missing_fraction": fmt_float((requested - min(requested, len(group))) / requested if requested else 0.0),
                "tune_mean": fmt_float(statistics.fmean(values) if values else None),
                "tune_std": fmt_float(stdev(values)),
                "smoothness_rms": fmt_float(smoothness(values)),
                "low_confidence_fraction": fmt_float(low_conf / len(confidences) if confidences else 1.0),
                "fallback_fraction": fmt_float(fallback / denom),
                "suspicious_fraction": fmt_float(suspicious / denom),
            }
        )
    return rows


def aggregate_bpm_leaderboard(observations: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in observations:
        key = (str(row.get("plane", "")), str(row.get("bpm_label", "")))
        grouped.setdefault(key, []).append(row)
    rows: list[dict[str, object]] = []
    for (plane, label), group in sorted(grouped.items()):
        tunes = finite(float_or_none(row.get("injection_tune")) for row in group)
        confidences = finite(float_or_none(row.get("injection_confidence")) for row in group)
        prominences = finite(float_or_none(row.get("injection_prominence")) for row in group)
        rms_values = finite(float_or_none(row.get("injection_rms")) for row in group)
        rows.append(
            {
                "plane": plane,
                "bpm_label": label,
                "observation_count": len(group),
                "finite_tune_count": len(tunes),
                "median_tune": median(tunes),
                "tune_std": stdev(tunes),
                "median_confidence": median(confidences),
                "median_prominence": median(prominences),
                "median_rms": median(rms_values),
            }
        )

    max_conf = max(finite(row.get("median_confidence") for row in rows) or [1.0])
    max_prom = max(finite(row.get("median_prominence") for row in rows) or [1.0])
    max_rms = max(finite(row.get("median_rms") for row in rows) or [1.0])
    for row in rows:
        tune_std = float_or_none(row.get("tune_std")) or 0.0
        consistency = max(0.0, min(1.0, 1.0 - tune_std / 0.02))
        confidence = (float_or_none(row.get("median_confidence")) or 0.0) / max(max_conf, 1e-12)
        prominence = (float_or_none(row.get("median_prominence")) or 0.0) / max(max_prom, 1e-12)
        rms_score = (float_or_none(row.get("median_rms")) or 0.0) / max(max_rms, 1e-12)
        row["score"] = 0.35 * confidence + 0.30 * prominence + 0.20 * consistency + 0.15 * rms_score
    rows.sort(key=lambda row: (str(row.get("plane", "")), -(float_or_none(row.get("score")) or 0.0)))
    return rows


def write_bpm_leaderboard_products(out_dir: Path, observations: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    leaderboard = aggregate_bpm_leaderboard(observations)
    csv_rows = [
        {
            field: fmt_float(row.get(field)) if isinstance(row.get(field), float) else row.get(field, "")
            for field in BPM_LEADERBOARD_FIELDS
        }
        for row in leaderboard
    ]
    write_csv(out_dir / "bpm_leaderboard.csv", csv_rows, BPM_LEADERBOARD_FIELDS)
    for plane in ("H", "V"):
        rows = [row for row in leaderboard if row.get("plane") == plane][:20]
        poster.bar_plot(
            out_dir / f"bpm_leaderboard_{plane.lower()}.png",
            f"BPM LEADERBOARD {plane}",
            [str(row.get("bpm_label", "")) for row in rows],
            [float_or_none(row.get("score")) or 0.0 for row in rows],
            "SCORE",
        )
    return csv_rows


def subset_consistency_plots(out_dir: Path, spill_rows: Sequence[dict[str, object]]) -> None:
    for plane, suffix in (("H", "h"), ("V", "v")):
        odd_even = [
            (idx, value)
            for idx, row in enumerate(spill_rows)
            for value in [float_or_none(row.get(f"odd_even_delta_{suffix}"))]
            if value is not None
        ]
        first_second = [
            (idx, value)
            for idx, row in enumerate(spill_rows)
            for value in [float_or_none(row.get(f"first_second_half_delta_{suffix}"))]
            if value is not None
        ]
        ymax = max([value for _, value in odd_even + first_second] + [0.01])
        poster.line_plot(
            out_dir / f"subset_consistency_{suffix}.png",
            f"SUBSET CONSISTENCY {plane}",
            [
                ("ODD/EVEN", odd_even, poster.BLUE),
                ("FIRST/SECOND", first_second, poster.ORANGE),
            ],
            x_label="SPILL",
            y_label="DELTA Q",
            y_range=(0.0, min(max(0.01, ymax * 1.15), 0.08)),
        )


def float_or_none(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def tune_matrix(sliding_rows_in: Sequence[dict[str, object]], plane: str, max_rows: int = 800) -> list[list[Optional[float]]]:
    by_spill: dict[tuple[int, int], list[tuple[int, Optional[float]]]] = {}
    for row in sliding_rows_in:
        if row.get("plane") != plane:
            continue
        key = (int(row.get("spill_index", 0)), int(row.get("target_ms", 0)))
        by_spill.setdefault(key, []).append((int(row.get("window_index", 0)), float_or_none(row.get("selected_tune"))))
    matrix: list[list[Optional[float]]] = []
    for _, points in sorted(by_spill.items())[:max_rows]:
        points.sort()
        matrix.append([value for _, value in points])
    return matrix


def color_ramp(frac: float) -> tuple[int, int, int]:
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        t = frac * 2.0
        color = (int(36 + 20 * t), int(78 + 126 * t), int(126 - 31 * t))
        return tuple(max(0, min(255, item)) for item in color)
    t = (frac - 0.5) * 2.0
    color = (int(56 + 203 * t), int(204 + 18 * t), int(95 - 48 * t))
    return tuple(max(0, min(255, item)) for item in color)


def power_heatmap_plot(path: Path, title: str, matrix: Optional[np.ndarray], band: tuple[float, float]) -> None:
    if matrix is None or matrix.size == 0:
        poster.no_data_png(path, title)
        return
    finite_values = matrix[np.isfinite(matrix)]
    if finite_values.size == 0:
        poster.no_data_png(path, title)
        return
    vmin = float(np.percentile(finite_values, 5))
    vmax = float(np.percentile(finite_values, 95))
    if vmax <= vmin:
        vmax = vmin + 1.0
    rows, cols = matrix.shape
    width, height = 1280, 760
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TUNE BIN", "FLASH")
    cell_w = max(1, (x1 - x0 + 1) // cols)
    cell_h = max(1, (y1 - y0 + 1) // rows)
    for row_idx in range(rows):
        for col_idx in range(cols):
            value = float(matrix[row_idx, col_idx])
            color = color_ramp((value - vmin) / (vmax - vmin)) if math.isfinite(value) else (235, 237, 240)
            cx0 = x0 + col_idx * cell_w
            cy0 = y0 + row_idx * cell_h
            poster.rect(pixels, width, height, cx0, cy0, min(x1, cx0 + cell_w - 1), min(y1, cy0 + cell_h - 1), color)
    poster.draw_text(pixels, width, height, x0, y1 + 8, f"{band[0]:.3f}", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 80, y1 + 8, f"{band[1]:.3f}", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 210, y0 - 26, f"log10 power p5-p95", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def draw_scaled_polyline(
    pixels: bytearray,
    width: int,
    height: int,
    points: Sequence[tuple[float, float]],
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    area: tuple[int, int, int, int],
    color: tuple[int, int, int],
    thickness: int = 1,
) -> None:
    clean = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
    if len(clean) < 2:
        return
    x0, y0, x1, y1 = area
    for (xa, ya), (xb, yb) in zip(clean, clean[1:]):
        px0 = poster.scale_value(xa, x_range[0], x_range[1], x0, x1)
        py0 = poster.scale_value(ya, y_range[0], y_range[1], y1, y0)
        px1 = poster.scale_value(xb, x_range[0], x_range[1], x0, x1)
        py1 = poster.scale_value(yb, y_range[0], y_range[1], y1, y0)
        for offset in range(-(thickness // 2), thickness // 2 + 1):
            poster.line(pixels, width, height, px0, py0 + offset, px1, py1 + offset, color)


def ridge_value(row: dict[str, object], source: str) -> Optional[float]:
    if source == "dp":
        return float_or_none(row.get("dp_ridge_tune")) or float_or_none(row.get("selected_tune"))
    return float_or_none(row.get("selected_tune"))


def raster_cell_bounds(
    index: int,
    count: int,
    start: int,
    end: int,
    *,
    reverse: bool = False,
) -> tuple[int, int]:
    """Map one density bin onto an inclusive pixel interval without gaps."""
    if count <= 0 or index < 0 or index >= count or end < start:
        raise ValueError("invalid raster cell geometry")
    span = end - start + 1
    if reverse:
        low = end - ((index + 1) * span // count) + 1
        high = end - (index * span // count)
    else:
        low = start + index * span // count
        high = start + (index + 1) * span // count - 1
    return low, high


def ridge_density_plot(
    path: Path,
    title: str,
    sliding: Sequence[dict[str, object]],
    plane: str,
    band: tuple[float, float],
    accepted_spills: set[int],
    value_source: str,
    args: argparse.Namespace,
) -> None:
    rows = [
        row
        for row in sliding
        if row.get("plane") == plane
        and int(row.get("spill_index", -1)) in accepted_spills
        and ridge_value(row, value_source) is not None
    ]
    if not rows:
        poster.no_data_png(path, title)
        return
    centers = sorted({int(row.get("center_turn", 0)) for row in rows})
    center_index = {center: idx for idx, center in enumerate(centers)}
    tune_bins = np.linspace(band[0], band[1], args.ridge_density_tune_bins + 1)
    density = np.zeros((args.ridge_density_tune_bins, len(centers)), dtype=np.float32)
    grouped: dict[int, list[float]] = {center: [] for center in centers}
    for row in rows:
        value = ridge_value(row, value_source)
        if value is None or value < band[0] or value > band[1]:
            continue
        center = int(row.get("center_turn", 0))
        x_idx = center_index[center]
        y_idx = int((value - band[0]) / (band[1] - band[0]) * args.ridge_density_tune_bins)
        y_idx = max(0, min(args.ridge_density_tune_bins - 1, y_idx))
        density[y_idx, x_idx] += 1.0
        grouped[center].append(value)
    if args.ridge_density_normalize:
        col_sum = density.sum(axis=0, keepdims=True)
        col_sum[col_sum == 0.0] = 1.0
        density = density / col_sum
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    finite_vals = density[density > 0]
    vmax = float(np.percentile(finite_vals, 98)) if finite_vals.size else 1.0
    vmax = max(vmax, 1.0 / max(1, len(accepted_spills)) if args.ridge_density_normalize else 1.0)
    for col in range(len(centers)):
        cx0, cx1 = raster_cell_bounds(col, len(centers), x0, x1)
        for row_idx in range(args.ridge_density_tune_bins):
            value = float(density[row_idx, col])
            frac = value / vmax if vmax else 0.0
            color = color_ramp(frac) if value > 0 else (245, 247, 248)
            cy0, cy1 = raster_cell_bounds(
                row_idx,
                args.ridge_density_tune_bins,
                y0,
                y1,
                reverse=True,
            )
            poster.rect(pixels, width, height, cx0, cy0, cx1, cy1, color)
    median_points = []
    p10_points = []
    p90_points = []
    p25_points = []
    p75_points = []
    for center in centers:
        vals = sorted(grouped[center])
        if not vals:
            continue
        median_points.append((center, float(np.percentile(vals, 50))))
        p10_points.append((center, float(np.percentile(vals, 10))))
        p90_points.append((center, float(np.percentile(vals, 90))))
        p25_points.append((center, float(np.percentile(vals, 25))))
        p75_points.append((center, float(np.percentile(vals, 75))))
    x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
    y_range = band
    area = (x0, y0, x1, y1)
    draw_scaled_polyline(pixels, width, height, p10_points, x_range, y_range, area, (255, 255, 255), 1)
    draw_scaled_polyline(pixels, width, height, p90_points, x_range, y_range, area, (255, 255, 255), 1)
    draw_scaled_polyline(pixels, width, height, p25_points, x_range, y_range, area, (235, 237, 240), 1)
    draw_scaled_polyline(pixels, width, height, p75_points, x_range, y_range, area, (235, 237, 240), 1)
    draw_scaled_polyline(pixels, width, height, median_points, x_range, y_range, area, (255, 255, 255), 3)
    poster.draw_text(pixels, width, height, x0, y1 + 8, str(min(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 110, y1 + 8, str(max(centers)), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x0, y0 - 28, f"{plane} {band[0]:.3f}-{band[1]:.3f} N={len(accepted_spills)}", poster.MUTED, 2)
    color_label = "FRACTION" if args.ridge_density_normalize else "SPILL COUNT"
    poster.draw_text(pixels, width, height, x1 - 360, y0 - 28, f"COLOR: {color_label} (P98 CLIP), WHITE: MED/PCT", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def spectrogram_overlay_plot(
    path: Path,
    title: str,
    log_spectra: Optional[np.ndarray],
    band: tuple[float, float],
    centers: Sequence[float],
    ridge: Sequence[tuple[float, float]],
    baseline: Sequence[tuple[float, float]],
    metadata: str,
) -> None:
    if log_spectra is None or log_spectra.size == 0 or not centers:
        poster.no_data_png(path, title)
        return
    finite_values = log_spectra[np.isfinite(log_spectra)]
    if finite_values.size == 0:
        poster.no_data_png(path, title)
        return
    vmin = float(np.percentile(finite_values, 5))
    vmax = float(np.percentile(finite_values, 98))
    if vmax <= vmin:
        vmax = vmin + 1.0
    rows, cols = log_spectra.shape
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title, "TURN", "TUNE")
    cell_w = max(1, (x1 - x0 + 1) // max(1, rows))
    cell_h = max(1, (y1 - y0 + 1) // max(1, cols))
    for row_idx in range(rows):
        for col_idx in range(cols):
            value = float(log_spectra[row_idx, col_idx])
            color = color_ramp((value - vmin) / (vmax - vmin)) if math.isfinite(value) else (245, 247, 248)
            cx0 = x0 + row_idx * cell_w
            cy0 = y1 - (col_idx + 1) * cell_h
            poster.rect(pixels, width, height, cx0, cy0, min(x1, cx0 + cell_w - 1), min(y1, cy0 + cell_h - 1), color)
    x_range = (float(min(centers)), float(max(centers) or min(centers) + 1))
    area = (x0, y0, x1, y1)
    draw_scaled_polyline(pixels, width, height, baseline, x_range, band, area, (30, 30, 30), 1)
    draw_scaled_polyline(pixels, width, height, ridge, x_range, band, area, (255, 255, 255), 3)
    poster.draw_text(pixels, width, height, x0, y0 - 28, metadata[:80], poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 230, y0 - 28, "COLOR: LOG10 POWER", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x0, y1 + 8, str(int(min(centers))), poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 110, y1 + 8, str(int(max(centers))), poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)


def method_comparison_plot(path: Path, title: str, analysis: PlaneAnalysis, args: argparse.Namespace, band: tuple[float, float]) -> None:
    centers = [float(point.get("center_turn", 0)) for point in analysis.sliding_points]
    series = []
    selected = [
        (center, float_or_none(point.get("selected_tune")))
        for center, point in zip(centers, analysis.sliding_points)
        if float_or_none(point.get("selected_tune")) is not None
    ]
    if selected:
        series.append(("BASELINE", selected, poster.BLUE))
    if "multitaper" in analysis.spectra_by_method:
        mt = []
        for center, spectrum in zip(centers, analysis.spectra_by_method["multitaper"]):
            peak = pick_peak_in_band(spectrum, band, args.min_peak_confidence)
            if peak:
                mt.append((center, peak.tune))
        if mt:
            series.append(("MULTITAPER", mt, poster.ORANGE))
    ridge = [
        (center, float_or_none(point.get("dp_ridge_tune")))
        for center, point in zip(centers, analysis.sliding_points)
        if float_or_none(point.get("dp_ridge_tune")) is not None
    ]
    if ridge:
        series.append(("DP RIDGE", ridge, poster.GREEN))
    poster.line_plot(path, title, series, x_label="TURN", y_label="TUNE", y_range=band)


def singular_values_plot(path: Path, title: str, values: np.ndarray) -> None:
    rows = [(idx + 1, float(value)) for idx, value in enumerate(values[: min(20, len(values))])]
    poster.line_plot(path, title, [("S", rows, poster.PURPLE)], x_label="MODE", y_label="SINGULAR VALUE")


def benchmark_bar_plot(path: Path, args: argparse.Namespace, timers: dict[str, float], elapsed: float) -> None:
    labels = ["TOTAL", "LOAD", "FFT", "RIDGE", "PLOT", "SVD"]
    values = [
        elapsed,
        float(timers.get("load_seconds", 0.0)),
        float(timers.get("fft_seconds", 0.0)),
        float(timers.get("ridge_seconds", 0.0)),
        float(timers.get("plot_seconds", 0.0)),
        float(timers.get("svd_seconds", 0.0)),
    ]
    poster.bar_plot(path, "DGX PROCESSING BENCHMARK", labels, values, "SECONDS")


def starts_from_analysis(analysis: PlaneAnalysis, window_turns: int) -> list[int]:
    starts: list[int] = []
    for point in analysis.sliding_points:
        center = int(float(point.get("center_turn", 0)))
        starts.append(max(0, center - window_turns // 2))
    return starts


def ridge_series_from_points(points: Sequence[dict[str, object]]) -> list[tuple[float, float]]:
    series: list[tuple[float, float]] = []
    for point in points:
        center = float_or_none(point.get("center_turn"))
        tune = float_or_none(point.get("ridge_tune"))
        if center is not None and tune is not None:
            series.append((center, tune))
    return series


def selected_series_from_analysis(analysis: PlaneAnalysis) -> list[tuple[float, float]]:
    series: list[tuple[float, float]] = []
    for point in analysis.sliding_points:
        center = float_or_none(point.get("center_turn"))
        tune = float_or_none(point.get("selected_tune"))
        if center is not None and tune is not None:
            series.append((center, tune))
    return series


def method_metadata(method: str, analysis: PlaneAnalysis, args: argparse.Namespace, band: tuple[float, float]) -> str:
    if method == "multitaper":
        method_label = f"METHOD MULTITAPER NW {args.multitaper_nw:g} K {args.multitaper_k}"
    else:
        method_label = "METHOD HANN"
    return (
        f"{method_label} WINDOW {args.sliding_window_turns} STRIDE {args.sliding_stride_turns} "
        f"BPM {analysis.used_streams} BAND {band[0]:.3f}-{band[1]:.3f}"
    )


def write_ridge_trace(path: Path, analysis: PlaneAnalysis) -> None:
    rows: list[dict[str, object]] = []
    for ridge in analysis.ridge_points:
        rows.append(
            {
                field: fmt_float(ridge.get(field)) if isinstance(ridge.get(field), float) else ridge.get(field, "")
                for field in RIDGE_FIELDS
            }
        )
    write_csv(path, rows, RIDGE_FIELDS)


def svd_reconstructions(
    traces: np.ndarray,
    modes: Sequence[int],
    normalize_bpm: bool,
) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    x = np.asarray(traces, dtype=np.float64)
    mean = np.mean(x, axis=1, keepdims=True)
    centered = x - mean
    scale = np.ones((x.shape[0], 1), dtype=np.float64)
    if normalize_bpm:
        scale = np.sqrt(np.mean(centered * centered, axis=1, keepdims=True))
        scale[scale == 0.0] = 1.0
        working = centered / scale
    else:
        working = centered
    u, s, vt = np.linalg.svd(working, full_matrices=False)
    recon: dict[int, np.ndarray] = {}
    for mode_count in modes:
        keep = max(1, min(int(mode_count), len(s)))
        reduced = (u[:, :keep] * s[:keep]) @ vt[:keep, :]
        if normalize_bpm:
            reduced = reduced * scale
        recon[keep] = (reduced + mean).astype(np.float32)
    return s.astype(np.float64), recon


def write_svd_products(
    out_dir: Path,
    representative: PlaneRepresentative,
    plane: str,
    band: tuple[float, float],
    backend: FftBackend,
    args: argparse.Namespace,
    timers: dict[str, float],
) -> Optional[list[tuple[int, float]]]:
    if not args.svd_denoise:
        return None
    t0 = time.perf_counter()
    traces, consensus_turns, warnings, _labels, _ranking_scores = load_plane_traces(
        representative.bundle,
        plane,
        args.max_traces_per_plane,
        args.aligned_only,
        args.turn_start,
        args.turn_end,
        args.bpm_normalization,
        args.injection_window_turns,
    )
    timers["load_seconds"] = timers.get("load_seconds", 0.0) + (time.perf_counter() - t0)
    if traces is None:
        poster.no_data_png(out_dir / f"svd_singular_values_{plane.lower()}.png", f"SVD SINGULAR VALUES {plane}", "; ".join(warnings))
        return None

    starts = starts_from_analysis(representative.analysis, args.sliding_window_turns)
    if not starts:
        return None

    t0 = time.perf_counter()
    singular_values, reconstructions = svd_reconstructions(traces, args.svd_modes, args.svd_normalize_bpm)
    timers["svd_seconds"] = timers.get("svd_seconds", 0.0) + (time.perf_counter() - t0)

    singular_values_plot(out_dir / f"svd_singular_values_{plane.lower()}.png", f"SVD SINGULAR VALUES {plane}", singular_values)
    energy = singular_values * singular_values
    cumulative: Optional[list[tuple[int, float]]] = None
    if energy.size and float(np.sum(energy)) > 0.0:
        total = float(np.sum(energy))
        running = np.cumsum(energy)
        cumulative = [(idx + 1, float(value / total)) for idx, value in enumerate(running[: min(20, len(running))])]

    compare_series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]] = []
    selected = selected_series_from_analysis(representative.analysis)
    if selected:
        compare_series.append(("BASELINE", selected, poster.BLUE))
    colors = [poster.ORANGE, poster.GREEN, poster.PURPLE, poster.RED]
    for color_idx, (mode_count, reconstructed) in enumerate(sorted(reconstructions.items())):
        t0 = time.perf_counter()
        spectra = average_spectra(
            reconstructed,
            starts,
            args.sliding_window_turns,
            backend,
            args.window_chunk,
            method=baseline_method(args),
            bpm_combination=args.bpm_combination,
            detrend=args.detrend,
            dc_handling=args.dc_handling,
            multitaper_nw=args.multitaper_nw,
            multitaper_k=args.multitaper_k,
            timers=timers,
        )
        timers["fft_seconds"] = timers.get("fft_seconds", 0.0) + (time.perf_counter() - t0)
        band_start, band_end = band_indices(args.sliding_window_turns, band)
        log_spectra = None
        if band_end > band_start:
            log_spectra = np.log10(np.maximum(spectra[:, band_start:band_end], 1e-30)).astype(np.float32)
        ridge_points = extract_dp_ridge(
            spectra,
            starts,
            args.sliding_window_turns,
            band,
            args,
            ridge_anchor_for_plane(args, plane),
        )
        ridge_line = ridge_series_from_points(ridge_points)
        label = f"SVD {mode_count}"
        if ridge_line:
            compare_series.append((label, ridge_line, colors[color_idx % len(colors)]))
        spectrogram_overlay_plot(
            out_dir / f"svd_spectrogram_{plane.lower()}_modes_{mode_count}.png",
            f"SVD SPECTROGRAM {plane} MODES {mode_count}",
            log_spectra,
            band,
            [float(point.get("center_turn", 0)) for point in representative.analysis.sliding_points],
            ridge_line,
            selected,
            f"SVD MODES {mode_count} WINDOW {args.sliding_window_turns} STRIDE {args.sliding_stride_turns} BPM {representative.analysis.used_streams}",
        )
    if compare_series:
        poster.line_plot(
            out_dir / f"svd_ridge_compare_{plane.lower()}.png",
            f"SVD RIDGE COMPARE {plane}",
            compare_series,
            x_label="TURN",
            y_label="TUNE",
            y_range=band,
        )
    return cumulative


def write_representative_products(
    out_dir: Path,
    representatives: dict[str, PlaneRepresentative],
    backend: FftBackend,
    args: argparse.Namespace,
    timers: dict[str, float],
) -> None:
    plot_t0 = time.perf_counter()
    svd_energy_series: list[tuple[str, list[tuple[int, float]], tuple[int, int, int]]] = []
    for plane, band, color in (("H", (args.qx_min, args.qx_max), poster.BLUE), ("V", (args.qy_min, args.qy_max), poster.GREEN)):
        representative = representatives.get(plane)
        if representative is None:
            poster.no_data_png(out_dir / f"single_spill_spectrogram_{plane.lower()}.png", f"SINGLE SPILL SPECTROGRAM {plane}")
            continue
        analysis = representative.analysis
        centers = [float(point.get("center_turn", 0)) for point in analysis.sliding_points]
        selected = selected_series_from_analysis(analysis)
        ridge = ridge_series_from_points(analysis.ridge_points)
        write_ridge_trace(out_dir / f"ridge_trace_{plane.lower()}.csv", analysis)

        for method, log_spectra in analysis.band_spectra_by_method.items():
            spectrogram_overlay_plot(
                out_dir / f"spectrogram_{plane.lower()}_{method}.png",
                f"SPECTROGRAM {plane} {method.upper()}",
                log_spectra,
                band,
                centers,
                ridge,
                selected,
                method_metadata(method, analysis, args, band),
            )

        source = ridge_source_method(args)
        log_spectra = analysis.band_spectra_by_method.get(source)
        if log_spectra is None:
            log_spectra = analysis.band_spectra
        spectrogram_overlay_plot(
            out_dir / f"single_spill_spectrogram_{plane.lower()}.png",
            f"SINGLE SPILL SPECTROGRAM {plane}",
            log_spectra,
            band,
            centers,
            ridge,
            selected,
            f"SPILL {representative.spill_index} {method_metadata(source, analysis, args, band)}",
        )
        spectrogram_overlay_plot(
            out_dir / f"ridge_overlay_{plane.lower()}.png",
            f"RIDGE OVERLAY {plane}",
            log_spectra,
            band,
            centers,
            ridge,
            selected,
            f"RIDGE {args.ridge_method.upper()} SOURCE {source.upper()} WINDOW {args.sliding_window_turns} BPM {analysis.used_streams}",
        )
        if len(analysis.spectra_by_method) > 1:
            method_comparison_plot(
                out_dir / f"spectrogram_method_compare_{plane.lower()}.png",
                f"SPECTROGRAM METHOD COMPARE {plane}",
                analysis,
                args,
                band,
            )
        method_comparison_plot(
            out_dir / f"method_comparison_{plane.lower()}.png",
            f"METHOD COMPARISON {plane}",
            analysis,
            args,
            band,
        )
        energy = write_svd_products(out_dir, representative, plane, band, backend, args, timers)
        if energy:
            svd_energy_series.append((f"{plane} ENERGY", energy, color))
    if args.svd_denoise and svd_energy_series:
        poster.line_plot(
            out_dir / "svd_method_comparison.png",
            "SVD METHOD COMPARISON",
            svd_energy_series,
            x_label="MODE",
            y_label="CUMULATIVE ENERGY",
            y_range=(0.0, 1.0),
        )
    timers["plot_seconds"] = timers.get("plot_seconds", 0.0) + (time.perf_counter() - plot_t0)


def trace_window_fft_count(timers: dict[str, float]) -> int:
    total = 0.0
    for key, value in timers.items():
        if key.endswith("_trace_window_ffts"):
            total += float(value)
    if total <= 0.0:
        total = float(timers.get("windows", 0.0))
    return int(round(total))


def estimate_memory_mib(args: argparse.Namespace) -> float:
    traces = max(1, int(args.max_traces_per_plane or 24))
    taper_count = int(args.multitaper_k if "multitaper" in requested_spectrogram_methods(args) else 1)
    chunk = max(1, int(args.window_chunk))
    window = max(1, int(args.sliding_window_turns))
    working_bytes = chunk * traces * window * max(1, taper_count) * 4
    fft_bytes = chunk * traces * window * max(1, taper_count) * 8
    return (working_bytes + fft_bytes) / (1024.0 * 1024.0)


def make_plots(
    out_dir: Path,
    spill_rows: Sequence[dict[str, object]],
    sliding: Sequence[dict[str, object]],
    spectra_by_plane: dict[str, list[np.ndarray]],
    args: argparse.Namespace,
    timers: dict[str, float],
) -> None:
    plot_t0 = time.perf_counter()
    qx = [(idx, float_or_none(row.get("median_qx"))) for idx, row in enumerate(spill_rows)]
    qy = [(idx, float_or_none(row.get("median_qy"))) for idx, row in enumerate(spill_rows)]
    poster.line_plot(
        out_dir / "gpu_median_tune_vs_spill.png",
        "GPU MEDIAN TUNE VS SPILL",
        [
            ("QX", [(x, y) for x, y in qx if y is not None], poster.BLUE),
            ("QY", [(x, y) for x, y in qy if y is not None], poster.GREEN),
        ],
        y_range=(min(args.qx_min, args.qy_min), max(args.qx_max, args.qy_max)),
        y_label="TUNE",
    )
    poster.hist_plot(
        out_dir / "gpu_qx_hist.png",
        "GPU QX HIST",
        [value for _, value in qx if value is not None],
        "QX",
        x_range=(args.qx_min, args.qx_max),
        color=poster.BLUE,
    )
    poster.hist_plot(
        out_dir / "gpu_qy_hist.png",
        "GPU QY HIST",
        [value for _, value in qy if value is not None],
        "QY",
        x_range=(args.qy_min, args.qy_max),
        color=poster.GREEN,
    )
    poster.heatmap_plot(
        out_dir / "gpu_flash_waterfall_h.png",
        "GPU FLASH WATERFALL H",
        tune_matrix(sliding, "H"),
        tune_min=args.qx_min,
        tune_max=args.qx_max,
        x_label="FLASH",
        y_label="SPILL",
    )
    poster.heatmap_plot(
        out_dir / "gpu_flash_waterfall_v.png",
        "GPU FLASH WATERFALL V",
        tune_matrix(sliding, "V"),
        tune_min=args.qy_min,
        tune_max=args.qy_max,
        x_label="FLASH",
        y_label="SPILL",
    )
    accepted = {int(row.get("spill_index", -1)) for row in spill_rows if str(row.get("usable_for_analysis")) == "true"}
    ridge_density_plot(
        out_dir / "ridge_density_h.png",
        "RIDGE DENSITY H",
        sliding,
        "H",
        (args.qx_min, args.qx_max),
        accepted,
        "dp" if args.ridge_method == "dp" else "selected",
        args,
    )
    ridge_density_plot(
        out_dir / "ridge_density_v.png",
        "RIDGE DENSITY V",
        sliding,
        "V",
        (args.qy_min, args.qy_max),
        accepted,
        "dp" if args.ridge_method == "dp" else "selected",
        args,
    )
    poster.line_plot(
        out_dir / "injection_tune_reproducibility.png",
        "INJECTION TUNE REPRODUCIBILITY",
        [
            ("QX", [(idx, float_or_none(row.get("qx_injection"))) for idx, row in enumerate(spill_rows) if float_or_none(row.get("qx_injection")) is not None], poster.BLUE),
            ("QY", [(idx, float_or_none(row.get("qy_injection"))) for idx, row in enumerate(spill_rows) if float_or_none(row.get("qy_injection")) is not None], poster.GREEN),
        ],
        x_label="SPILL",
        y_label="TUNE",
        y_range=(min(args.qx_min, args.qy_min), max(args.qx_max, args.qy_max)),
    )
    for plane, band in (("H", (args.qx_min, args.qx_max)), ("V", (args.qy_min, args.qy_max))):
        if spectra_by_plane.get(plane):
            rows = np.stack(spectra_by_plane[plane], axis=0)
            median_spectrogram = np.nanmedian(rows, axis=0)
        else:
            median_spectrogram = None
        power_heatmap_plot(out_dir / f"gpu_median_spectrogram_{plane.lower()}.png", f"GPU MEDIAN SPECTROGRAM {plane}", median_spectrogram, band)
    subset_consistency_plots(out_dir, spill_rows)
    timers["plot_seconds"] = timers.get("plot_seconds", 0.0) + (time.perf_counter() - plot_t0)


def write_summary(
    out_dir: Path,
    spill_rows: Sequence[dict[str, object]],
    sliding: Sequence[dict[str, object]],
    backend: FftBackend,
    elapsed: float,
    timers: dict[str, float],
    args: argparse.Namespace,
) -> None:
    usable = sum(1 for row in spill_rows if str(row.get("usable_for_analysis")) == "true")
    suitable = sum(1 for row in spill_rows if str(row.get("suitable_for_poster")) == "true")
    qx = [float_or_none(row.get("median_qx")) for row in spill_rows]
    qy = [float_or_none(row.get("median_qy")) for row in spill_rows]
    device = backend.describe()
    windows = trace_window_fft_count(timers)
    fft_seconds = float(timers.get("fft_seconds", 0.0))
    total_for_rate = fft_seconds if fft_seconds > 0.0 else elapsed
    memory_mib = estimate_memory_mib(args)
    ranking_source = (
        "raw pre-normalization RMS over the loaded turn range"
        if args.bpm_combination in {"best_single_bpm", "top5_by_confidence", "top10_by_confidence", "top20_by_confidence"}
        else "not applicable"
    )
    lines = [
        "# GPU Captured-Spill Analysis Summary",
        "",
        "Offline BPM tune analysis over raw captured-spill bundles.",
        "",
        f"- backend: `{device.get('backend')}`",
        f"- device: `{device.get('device')}`",
        f"- spills processed: `{len(spill_rows)}`",
        f"- usable spills: `{usable}`",
        f"- suitable-for-poster spills: `{suitable}`",
        f"- sliding rows: `{len(sliding)}`",
        f"- requested flashes: `{args.flashes if args.flashes is not None else 'stride'}`",
        f"- window/stride turns: `{args.sliding_window_turns}` / `{args.sliding_stride_turns}`",
        f"- plane mode: `{args.plane}`",
        f"- turn range: `{args.turn_start}` / `{args.turn_end if args.turn_end is not None else 'full'}`",
        f"- BPM combination: `{args.bpm_combination}`",
        f"- BPM subset ranking source: `{ranking_source}`",
        f"- BPM normalization: `{args.bpm_normalization}`",
        f"- detrend/DC handling: `{args.detrend}` / `{args.dc_handling}`",
        f"- spectrogram method: `{args.spectrogram_method}`",
        f"- ridge method/source: `{args.ridge_method}` / `{ridge_source_method(args)}`",
        f"- ridge anchor enabled/H/V/half-width: `{str(args.ridge_anchor_enabled).lower()}` / `{args.ridge_anchor_h:g}` / `{args.ridge_anchor_v:g}` / `{args.ridge_anchor_half_width:g}`",
        f"- multitaper NW/K: `{args.multitaper_nw:g}` / `{args.multitaper_k}`",
        f"- ridge penalties jump/jump2/max-step: `{args.ridge_jump_penalty:g}` / `{args.ridge_jump2_penalty:g}` / `{args.ridge_max_step:g}`",
        f"- SVD denoise: `{str(args.svd_denoise).lower()}`",
        f"- Qx median/std: `{fmt_float(median(qx))}` / `{fmt_float(stdev(qx))}`",
        f"- Qy median/std: `{fmt_float(median(qy))}` / `{fmt_float(stdev(qy))}`",
        f"- elapsed seconds: `{elapsed:.3f}`",
        f"- FFT seconds: `{fft_seconds:.3f}`",
        f"- plot seconds: `{timers.get('plot_seconds', 0.0):.3f}`",
        f"- SVD seconds: `{timers.get('svd_seconds', 0.0):.3f}`",
        f"- trace-window FFTs: `{windows}`",
        f"- trace-window FFTs/sec: `{windows / total_for_rate:.2f}`",
        f"- estimated peak working memory MiB: `{memory_mib:.1f}`",
        "",
        "## Outputs",
        "",
        "- `gpu_spills_summary.csv`",
        "- `gpu_sliding_tune.csv`",
        f"- `gpu_flash_summary_{args.flashes}.csv`" if args.flashes is not None else "- no flash summary was requested",
        "- `gpu_median_tune_vs_spill.png`",
        "- `gpu_flash_waterfall_h.png`, `gpu_flash_waterfall_v.png`",
        "- `gpu_median_spectrogram_h.png`, `gpu_median_spectrogram_v.png`",
        "- `injection_tune_reproducibility.png`",
        "- `ridge_density_h.png`, `ridge_density_v.png`",
        "- `single_spill_spectrogram_h.png`, `single_spill_spectrogram_v.png`",
        "- `spectrogram_h_hann.png`, `spectrogram_v_hann.png`",
        "- `spectrogram_h_multitaper.png`, `spectrogram_v_multitaper.png` when requested",
        "- `method_comparison_h.png`, `method_comparison_v.png`",
        "- `bpm_leaderboard.csv`, `bpm_leaderboard_h.png`, `bpm_leaderboard_v.png`",
        "- `subset_consistency_h.png`, `subset_consistency_v.png`",
        "- `ridge_trace_h.csv`, `ridge_trace_v.csv`",
        "- `ridge_overlay_h.png`, `ridge_overlay_v.png`",
        "- `dgx_benchmark.md`, `dgx_benchmark.png`, `dgx_processing_benchmark.png`",
    ]
    if args.svd_denoise:
        lines.extend(
            [
                "- `svd_singular_values_h.png`, `svd_singular_values_v.png`",
                "- `svd_spectrogram_h_modes_<N>.png`, `svd_spectrogram_v_modes_<N>.png`",
                "- `svd_ridge_compare_h.png`, `svd_ridge_compare_v.png`",
                "- `svd_method_comparison.png`",
            ]
        )
    lines.extend(["", "## Backend Details", ""])
    for key, value in device.items():
        lines.append(f"- `{key}`: `{value}`")
    if "multitaper_taper_source" in timers:
        lines.extend(
            [
                "",
                "## Multitaper Details",
                "",
                f"- taper source: `{timers.get('multitaper_taper_source')}`",
                f"- taper count: `{int(timers.get('multitaper_taper_count', args.multitaper_k))}`",
            ]
        )
    poster.write_text(out_dir / "gpu_analysis_summary.md", "\n".join(lines) + "\n")

    bench_lines = [
        "# DGX Captured-Spill Benchmark",
        "",
        f"- backend: `{device.get('backend')}`",
        f"- device: `{device.get('device')}`",
        f"- spills: `{len(spill_rows)}`",
        f"- elapsed_seconds: `{elapsed:.6f}`",
        f"- spills_per_second: `{len(spill_rows) / elapsed if elapsed > 0 else 0.0:.2f}`",
        f"- fft_seconds: `{fft_seconds:.6f}`",
        f"- load_seconds: `{timers.get('load_seconds', 0.0):.6f}`",
        f"- ridge_seconds: `{timers.get('ridge_seconds', 0.0):.6f}`",
        f"- plot_seconds: `{timers.get('plot_seconds', 0.0):.6f}`",
        f"- svd_seconds: `{timers.get('svd_seconds', 0.0):.6f}`",
        f"- trace_window_ffts: `{windows}`",
        f"- trace_window_ffts_per_sec: `{windows / total_for_rate:.2f}`",
        f"- estimated_peak_working_memory_mib: `{memory_mib:.1f}`",
        f"- spectrogram_method: `{args.spectrogram_method}`",
        f"- ridge_method: `{args.ridge_method}`",
    ]
    bench_text = "\n".join(bench_lines) + "\n"
    poster.write_text(out_dir / "gpu_benchmark.md", bench_text)
    poster.write_text(out_dir / "dgx_benchmark.md", bench_text)
    benchmark_bar_plot(out_dir / "dgx_benchmark.png", args, timers, elapsed)
    shutil.copyfile(out_dir / "dgx_benchmark.png", out_dir / "dgx_processing_benchmark.png")


def analyze(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    poster.ensure_dir(out_dir)
    manifests = discover_manifests([Path(item) for item in args.input], [Path(item) for item in args.manifest_list])
    if args.limit:
        manifests = manifests[: args.limit]
    if not manifests:
        raise SystemExit("no captured-spill manifest.json files found")

    bundles = [load_bundle(path) for path in manifests]
    bundles.sort(key=lambda bundle: (bundle.run_name, bundle.target_ms))

    backend = FftBackend(args.device)
    timers = {"load_seconds": 0.0, "fft_seconds": 0.0, "windows": 0.0}
    spill_rows: list[dict[str, object]] = []
    sliding: list[dict[str, object]] = []
    spectra_by_plane: dict[str, list[np.ndarray]] = {"H": [], "V": []}
    representatives: dict[str, PlaneRepresentative] = {}
    bpm_observations: list[dict[str, object]] = []

    started = time.perf_counter()
    for idx, bundle in enumerate(bundles):
        if args.progress and (idx == 0 or (idx + 1) % args.progress == 0 or idx + 1 == len(bundles)):
            print(f"[{idx + 1}/{len(bundles)}] {bundle.run_name}/{bundle.target_ms}", flush=True)

        analyses: dict[str, Optional[PlaneAnalysis]] = {"H": None, "V": None}
        load_warnings: dict[str, list[str]] = {"H": [], "V": []}
        for plane, band in (("H", (args.qx_min, args.qx_max)), ("V", (args.qy_min, args.qy_max))):
            if args.plane != "both" and args.plane != plane:
                continue
            t0 = time.perf_counter()
            traces, consensus_turns, warnings, bpm_labels, ranking_scores = load_plane_traces(
                bundle,
                plane,
                args.max_traces_per_plane,
                args.aligned_only,
                args.turn_start,
                args.turn_end,
                args.bpm_normalization,
                args.injection_window_turns,
            )
            timers["load_seconds"] += time.perf_counter() - t0
            load_warnings[plane].extend(warnings)
            if traces is None:
                continue
            analysis = analyze_plane(
                bundle,
                plane,
                traces,
                bpm_labels,
                consensus_turns,
                args,
                backend,
                band,
                timers,
                ranking_scores,
            )
            analysis.warnings.extend(load_warnings[plane])
            analyses[plane] = analysis
            if analysis.band_spectra is not None and not args.no_spectrogram:
                spectra_by_plane[plane].append(analysis.band_spectra)
            for observation in analysis.bpm_observations:
                bpm_observations.append(
                    {
                        "spill_index": idx,
                        "target_ms": bundle.target_ms,
                        "run_name": bundle.run_name,
                        "plane": plane,
                        **observation,
                    }
                )
        row = summary_row(idx, bundle, analyses["H"], analyses["V"], args)
        spill_rows.append(row)
        if str(row.get("usable_for_analysis")) == "true":
            for plane in ("H", "V"):
                analysis = analyses[plane]
                if analysis is None:
                    continue
                score_parts = selected_confidences(analysis)
                score = (analysis.injection_peak.confidence if analysis.injection_peak else 0.0) + (
                    statistics.fmean(score_parts) if score_parts else 0.0
                )
                current = representatives.get(plane)
                if current is None or score > current.score:
                    representatives[plane] = PlaneRepresentative(score, idx, bundle, analysis)
        sliding.extend(sliding_rows(idx, bundle, [analyses["H"], analyses["V"]]))

    write_csv(out_dir / "gpu_spills_summary.csv", spill_rows, SPILL_FIELDS)
    write_csv(out_dir / "gpu_sliding_tune.csv", sliding, SLIDING_FIELDS)
    if args.flashes is not None:
        write_csv(out_dir / f"gpu_flash_summary_{args.flashes}.csv", flash_rows(spill_rows, sliding, args.flashes), FLASH_FIELDS)
    write_csv(out_dir / "bpm_observations.csv", bpm_observations, BPM_OBSERVATION_FIELDS)
    write_bpm_leaderboard_products(out_dir, bpm_observations)
    write_representative_products(out_dir, representatives, backend, args, timers)
    make_plots(out_dir, spill_rows, sliding, spectra_by_plane, args, timers)
    elapsed = time.perf_counter() - started
    write_summary(out_dir, spill_rows, sliding, backend, elapsed, timers, args)


def make_synthetic_bundle(root: Path, target_ms: int, qx: float, qy: float, turns: int = 2048) -> None:
    payload_dir = root / f"spill_{target_ms}" / "payloads"
    payload_dir.mkdir(parents=True)
    streams = []
    n = np.arange(turns, dtype=np.float32)
    rng = np.random.default_rng(1234 + target_ms)
    idx = 0
    for plane, tune in (("H", qx), ("V", qy)):
        for bpm in range(4):
            freq = 1.0 - tune
            signal = np.cos(2.0 * math.pi * freq * n + bpm * 0.1).astype(np.float32)
            signal += rng.normal(0.0, 0.02, size=turns).astype(np.float32)
            payload_name = f"payloads/stream_{idx:03d}_{plane}_{target_ms}.bin"
            payload_path = root / f"spill_{target_ms}" / payload_name
            signal.astype("<f4").tofile(payload_path)
            key_mid = "HP101" if plane == "H" else "VP102"
            streams.append(
                {
                    "bpm_ip": f"test-{bpm}",
                    "stream_key": f"{{TEST}}:{key_mid}:TBT_POSITION_RAW",
                    "plane": plane,
                    "stream_id": f"{target_ms}-0",
                    "stream_ms": target_ms,
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
        "target_ms": target_ms,
        "requested_streams": len(streams),
        "streams": streams,
        "warnings": [],
    }
    (root / f"spill_{target_ms}" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="gpu-captured-self-test-") as tmp:
        root = Path(tmp) / "synthetic-run"
        root.mkdir()
        make_synthetic_bundle(root, 1000, 0.681, 0.713, turns=2048)
        make_synthetic_bundle(root, 2000, 0.684, 0.709, turns=2048)
        out = Path(tmp) / "out"
        args = build_parser().parse_args(
            [
                "--input",
                str(root),
                "--out",
                str(out),
                "--device",
                "cpu",
                "--sliding-window-turns",
                "512",
                "--sliding-stride-turns",
                "256",
                "--flashes",
                "4",
                "--min-peak-confidence",
                "1.2",
                "--progress",
                "0",
            ]
        )
        analyze(args)
        rows = list(csv.DictReader((out / "gpu_spills_summary.csv").open()))
        if len(rows) != 2:
            raise SystemExit(f"self-test expected 2 rows, got {len(rows)}")
        for row in rows:
            qx = float(row["median_qx"])
            qy = float(row["median_qy"])
            if not (0.67 <= qx <= 0.70 and 0.70 <= qy <= 0.72):
                raise SystemExit(f"self-test tune out of range: qx={qx} qy={qy}")
        print("gpu captured-spill self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="run a synthetic captured-bundle self-test")
    parser.add_argument("--input", nargs="+", default=[], help="captured run dirs, spill dirs, or manifest.json files")
    parser.add_argument("--manifest-list", nargs="*", default=[], help="text files containing one manifest.json path per line")
    parser.add_argument("--out", default="gpu-captured-analysis", help="output directory")
    parser.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu", help="FFT backend")
    parser.add_argument("--limit", type=int, default=0, help="limit number of manifests for smoke tests")
    parser.add_argument("--progress", type=int, default=25, help="print progress every N spills; 0 disables")
    parser.add_argument("--aligned-only", action="store_true", help="exclude manifest streams marked aligned=false")
    parser.add_argument("--max-traces-per-plane", type=int, default=0, help="cap streams per plane; 0 uses all")
    parser.add_argument("--window-chunk", type=int, default=32, help="number of sliding windows per GPU FFT chunk")
    parser.add_argument("--turn-start", type=int, default=0, help="first turn/sample to include")
    parser.add_argument("--turn-end", type=int, default=None, help="exclusive ending turn/sample; default uses full payload")
    parser.add_argument("--plane", choices=["H", "V", "both"], default="both", help="plane(s) to analyze")
    parser.add_argument(
        "--bpm-combination",
        choices=[
            "mean",
            "median",
            "trimmed_mean_10pct",
            "best_single_bpm",
            "top5_by_confidence",
            "top10_by_confidence",
            "top20_by_confidence",
            "odd_even",
            "first_second_half",
        ],
        default="mean",
        help="how to combine BPM spectra per plane; best/top-N modes rank raw pre-normalization RMS",
    )
    parser.add_argument(
        "--bpm-normalization",
        choices=["none", "rms_per_bpm", "mad_per_bpm", "injection_rms_per_bpm"],
        default="none",
        help="per-BPM waveform normalization before FFT windows",
    )
    parser.add_argument(
        "--detrend",
        choices=["none", "mean_subtract", "linear", "polynomial_order_2"],
        default="mean_subtract",
        help="per-window detrending before FFT",
    )
    parser.add_argument(
        "--dc-handling",
        choices=["keep", "zero_dc_bin", "ignore_low_bins"],
        default="zero_dc_bin",
        help="DC/low-bin handling after FFT",
    )
    parser.add_argument("--spectrogram-method", choices=["hann", "multitaper", "both"], default="hann")
    parser.add_argument("--multitaper-nw", type=float, default=2.5)
    parser.add_argument("--multitaper-k", type=int, default=4)
    parser.add_argument("--injection-start-turn", type=int, default=0)
    parser.add_argument("--injection-window-turns", type=int, default=2048)
    parser.add_argument("--sliding-window-turns", type=int, default=2048)
    parser.add_argument("--sliding-stride-turns", type=int, default=256)
    parser.add_argument("--flashes", type=int, default=128, help="evenly spaced flash windows; omit with --stride-mode")
    parser.add_argument("--stride-mode", action="store_true", help="use stride windows instead of flash windows")
    parser.add_argument("--qx-min", type=float, default=DEFAULT_Q_BAND[0])
    parser.add_argument("--qx-max", type=float, default=DEFAULT_Q_BAND[1])
    parser.add_argument("--qy-min", type=float, default=DEFAULT_Q_BAND[0])
    parser.add_argument("--qy-max", type=float, default=DEFAULT_Q_BAND[1])
    parser.add_argument("--min-peak-confidence", type=float, default=2.0)
    parser.add_argument("--track-half-width", type=float, default=0.005)
    parser.add_argument("--max-tune-step-per-window", type=float, default=0.005)
    parser.add_argument("--disable-tracking", dest="enable_tracking", action="store_false")
    parser.add_argument("--no-spectrogram", action="store_true", help="skip median band-spectrogram accumulation")
    parser.add_argument("--ridge-method", choices=["greedy", "dp"], default="greedy")
    parser.add_argument("--ridge-source-method", choices=["auto", "hann", "multitaper"], default="auto")
    parser.add_argument("--ridge-jump-penalty", type=float, default=500.0)
    parser.add_argument("--ridge-jump2-penalty", type=float, default=20000.0)
    parser.add_argument("--ridge-max-step", type=float, default=0.010)
    parser.add_argument("--ridge-normalize", choices=["row", "global", "none"], default="row")
    parser.add_argument("--ridge-anchor-enabled", choices=["true", "false"], default="true")
    parser.add_argument("--ridge-anchor-h", type=float, default=0.65)
    parser.add_argument("--ridge-anchor-v", type=float, default=0.72)
    parser.add_argument("--ridge-anchor-half-width", type=float, default=0.02)
    parser.add_argument("--ridge-anchor-penalty", type=float, default=1000.0)
    parser.add_argument("--ridge-density-tune-bins", type=int, default=160)
    parser.add_argument("--ridge-density-normalize", action="store_true", help="normalize ridge-density columns by spill count")
    parser.add_argument("--svd-denoise", action="store_true", help="write representative SVD/PCA denoising products")
    parser.add_argument("--svd-modes", default="1,2,4", help="comma-separated leading SVD mode counts to reconstruct")
    parser.add_argument("--svd-normalize-bpm", choices=["true", "false"], default="true")
    parser.set_defaults(enable_tracking=True)
    return parser


def parse_svd_modes(value: object) -> list[int]:
    if isinstance(value, list):
        return value
    modes: list[int] = []
    for part in str(value).split(","):
        text = part.strip()
        if not text:
            continue
        try:
            mode = int(text)
        except ValueError as exc:
            raise SystemExit("--svd-modes must be a comma-separated list of positive integers") from exc
        if mode <= 0:
            raise SystemExit("--svd-modes entries must be positive")
        modes.append(mode)
    if not modes:
        raise SystemExit("--svd-modes must include at least one mode count")
    return sorted(set(modes))


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    if args.self_test:
        return args
    if not args.input and not args.manifest_list:
        raise SystemExit("--input or --manifest-list is required unless --self-test is used")
    if args.max_traces_per_plane <= 0:
        args.max_traces_per_plane = None
    if args.turn_start < 0:
        raise SystemExit("--turn-start must be >= 0")
    if args.turn_end is not None and args.turn_end <= args.turn_start:
        raise SystemExit("--turn-end must be greater than --turn-start")
    if args.stride_mode:
        args.flashes = None
    if args.flashes is not None and args.flashes <= 0:
        raise SystemExit("--flashes must be >= 1")
    if args.sliding_window_turns <= 0 or args.sliding_stride_turns <= 0:
        raise SystemExit("sliding window and stride must be >= 1")
    if args.injection_window_turns <= 0:
        raise SystemExit("injection window must be >= 1")
    if args.window_chunk <= 0:
        raise SystemExit("--window-chunk must be >= 1")
    if args.multitaper_nw <= 0.0:
        raise SystemExit("--multitaper-nw must be > 0")
    if args.multitaper_k <= 0:
        raise SystemExit("--multitaper-k must be >= 1")
    if args.ridge_jump_penalty < 0.0 or args.ridge_jump2_penalty < 0.0:
        raise SystemExit("ridge penalties must be >= 0")
    if args.ridge_max_step < 0.0:
        raise SystemExit("--ridge-max-step must be >= 0")
    if args.ridge_anchor_half_width < 0.0 or args.ridge_anchor_penalty < 0.0:
        raise SystemExit("ridge anchor half-width and penalty must be >= 0")
    if args.ridge_density_tune_bins <= 1:
        raise SystemExit("--ridge-density-tune-bins must be > 1")
    if args.ridge_source_method != "auto" and args.ridge_source_method not in requested_spectrogram_methods(args):
        raise SystemExit("--ridge-source-method must be one of the requested spectrogram methods")
    args.svd_modes = parse_svd_modes(args.svd_modes)
    args.svd_normalize_bpm = args.svd_normalize_bpm == "true"
    args.ridge_anchor_enabled = args.ridge_anchor_enabled == "true"
    for name in ("qx", "qy"):
        lo = getattr(args, f"{name}_min")
        hi = getattr(args, f"{name}_max")
        if not (0.0 <= lo < hi <= 1.0):
            raise SystemExit(f"{name} band must satisfy 0 <= min < max <= 1")
    return args


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return
    analyze(normalize_args(args))


if __name__ == "__main__":
    main()
