"""Raw waveform integrity helpers for Delivery Ring publication inputs."""

from __future__ import annotations

from typing import Sequence

import numpy as np


def longest_finite_exact_run(arrays: Sequence[np.ndarray]) -> tuple[int | None, int, tuple[float, ...]]:
    """Return the earliest longest run where every finite array stays exact."""
    if not arrays:
        return None, 0, ()
    size = min(int(np.asarray(values).size) for values in arrays)
    if size <= 0:
        return None, 0, ()
    values = [np.asarray(array).reshape(-1)[:size] for array in arrays]
    finite = np.logical_and.reduce([np.isfinite(array) for array in values])
    if size == 1:
        return (0, 1, tuple(float(array[0]) for array in values)) if finite[0] else (None, 0, ())

    same = finite[1:] & finite[:-1]
    for array in values:
        same &= array[1:] == array[:-1]
    boundaries = np.concatenate(
        (
            np.asarray([0], dtype=np.int64),
            np.flatnonzero(~same).astype(np.int64) + 1,
            np.asarray([size], dtype=np.int64),
        )
    )
    lengths = np.diff(boundaries)
    valid_segments = finite[boundaries[:-1]]
    if not np.any(valid_segments):
        return None, 0, ()
    valid_lengths = np.where(valid_segments, lengths, 0)
    segment = int(np.argmax(valid_lengths))
    start = int(boundaries[segment])
    length = int(valid_lengths[segment])
    return start, length, tuple(float(array[start]) for array in values)


def longest_true_run(mask: np.ndarray) -> tuple[int | None, int]:
    """Return the earliest longest contiguous true run."""
    values = np.asarray(mask, dtype=bool).reshape(-1)
    if not values.size or not np.any(values):
        return None, 0
    padded = np.concatenate((np.asarray([False]), values, np.asarray([False])))
    transitions = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(transitions == 1)
    ends = np.flatnonzero(transitions == -1)
    lengths = ends - starts
    winner = int(np.argmax(lengths))
    return int(starts[winner]), int(lengths[winner])


def device_fallback_values(channel: str) -> tuple[float, float] | None:
    """Return the checked-out DR device-coded fallback pair for HP/VP channels."""
    if len(channel) < 3 or channel[:2] not in {"HP", "VP"}:
        return None
    try:
        number = int(channel[2:])
    except ValueError:
        return None
    return number / 100.0, float(1000 + number)
