"""Preprocessing helpers for BPM spectral analysis."""

from __future__ import annotations

import math

import numpy as np


def hann(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones((n,), dtype=np.float32)
    idx = np.arange(n, dtype=np.float32)
    return (0.5 - 0.5 * np.cos((2.0 * math.pi * idx) / (n - 1))).astype(np.float32)


def detrend_linear_np(block: np.ndarray) -> np.ndarray:
    if block.shape[-1] < 2:
        return block
    x = np.arange(block.shape[-1], dtype=np.float32)
    x = x - float(np.mean(x))
    denom = float(np.sum(x * x)) or 1.0
    centered = block - np.mean(block, axis=-1, keepdims=True)
    slope = np.sum(centered * x[None, :], axis=-1, keepdims=True) / denom
    return centered - slope * x[None, :]


def preprocess_window_np(block: np.ndarray, detrend: str = "none") -> np.ndarray:
    clean = np.asarray(block, dtype=np.float32)
    if detrend == "linear":
        return detrend_linear_np(clean)
    return clean - np.mean(clean, axis=-1, keepdims=True)
