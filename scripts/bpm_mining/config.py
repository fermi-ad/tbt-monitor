"""Configuration helpers for the best-BPM mining pipeline.

The repository default config is JSON-compatible YAML. That keeps the Spark
runtime dependency surface to the Python standard library plus NumPy/CuPy.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


def default_config() -> dict[str, Any]:
    return {
        "data": {
            "collections": [
                "/path/to/captured-spills/collection-1",
                "/path/to/captured-spills/collection-2",
            ]
        },
        "physics": {
            "expected_tune_h": 0.65,
            "expected_tune_v": 0.72,
            "discovery_band_h": [0.60, 0.70],
            "discovery_band_v": [0.67, 0.75],
        },
        "integrity": {
            "minimum_turns": 2048,
            "constant_epsilon": 1e-9,
            "clip_fraction": 0.02,
            "extreme_rms": 1_000_000.0,
        },
        "spectra": {
            "nfft_padding_factor": 2,
            "primary_method": "hann",
            "cache_dtype": "float16",
            "configs": [
                {"name": "injection_2048", "turn_start": 0, "window_turns": 2048},
                {"name": "injection_4096", "turn_start": 0, "window_turns": 4096},
                {
                    "name": "early_2048_256",
                    "turn_start": 0,
                    "turn_end": 10000,
                    "window_turns": 2048,
                    "stride_turns": 256,
                },
                {
                    "name": "early_4096_256",
                    "turn_start": 0,
                    "turn_end": 15000,
                    "window_turns": 4096,
                    "stride_turns": 256,
                },
            ],
        },
        "peak_finding": {
            "max_peaks": 3,
            "min_peak_distance_bins": 3,
            "local_background_half_width_bins": 15,
            "exclude_peak_half_width_bins": 3,
        },
        "consensus": {"min_unique_bpms": 3, "bootstrap_samples": 200, "cluster_eps_min": 0.0015},
        "subset_search": {
            "search_spectral_config": "early_4096_256",
            "max_search_turn": 10000,
            "max_search_windows": 16,
            "best3_keep": 256,
            "best5_pool_size": 20,
            "best5_keep": 128,
            "best10_pool_size": 18,
            "best10_keep": 64,
            "subset_chunk_size": 512,
            "cuda_workers": 4,
            "beam_width": 512,
            "random_audit_samples": 10000,
            "audit_improvement_threshold": 0.01,
        },
        "evolution": {
            "window_turns": 4096,
            "stride_turns": 256,
            "secondary_window_turns": 2048,
            "secondary_stride_turns": 128,
        },
        "statistics": {
            "bootstrap_samples": 2000,
            "bootstrap_block_spills": 20,
            "permutation_samples": 10000,
            "fdr_alpha": 0.05,
            "shapley_like_permutations": 5000,
        },
        "artifacts": {"max_spills_per_plane": 40},
        "runtime": {"device": "cuda", "spill_batch_size": 8, "workers": 4, "random_seed": 20260614, "resume": True},
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | Path | None) -> dict[str, Any]:
    cfg = default_config()
    if not path:
        return cfg
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        return cfg
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} must be JSON-compatible YAML so the Spark runtime remains stdlib-only: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path} did not contain a top-level mapping")
    return deep_merge(cfg, loaded)


def config_hash(cfg: dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def plane_band(cfg: dict[str, Any], plane: str) -> tuple[float, float]:
    key = "discovery_band_h" if plane.upper() == "H" else "discovery_band_v"
    lo, hi = cfg["physics"][key]
    return float(lo), float(hi)


def expected_tune(cfg: dict[str, Any], plane: str) -> float:
    key = "expected_tune_h" if plane.upper() == "H" else "expected_tune_v"
    return float(cfg["physics"][key])
