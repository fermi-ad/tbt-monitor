"""Deterministic run matrix for Best-N hyperparameter sensitivity checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, order=True)
class SensitivityRun:
    beam_width: int
    fit_windows: int
    fold_seed: int

    @property
    def slug(self) -> str:
        return f"beam{self.beam_width}_fit{self.fit_windows}_seed{self.fold_seed}"


def _positive_unique(values: Sequence[int], label: str) -> list[int]:
    result = sorted({int(value) for value in values})
    if not result or any(value <= 0 for value in result):
        raise ValueError(f"{label} values must be positive")
    return result


def build_sensitivity_matrix(
    beam_widths: Sequence[int],
    fit_windows: Sequence[int],
    fold_seeds: Sequence[int],
    baseline_beam_width: int,
    baseline_fit_windows: int,
    baseline_fold_seed: int,
) -> tuple[list[SensitivityRun], dict[str, list[tuple[str, SensitivityRun]]]]:
    """Return unique runs and the three publication sensitivity dimensions."""
    beams = _positive_unique(beam_widths, "beam-width")
    fits = _positive_unique(fit_windows, "fit-window")
    seeds = _positive_unique(fold_seeds, "fold-seed")
    if baseline_beam_width not in beams:
        raise ValueError("baseline beam width must be present in beam_widths")
    if baseline_fit_windows not in fits:
        raise ValueError("baseline fit-window count must be present in fit_windows")
    if baseline_fold_seed not in seeds:
        raise ValueError("baseline fold seed must be present in fold_seeds")

    dimensions = {
        "beam_width": [
            (
                f"beam{beam}",
                SensitivityRun(beam, baseline_fit_windows, baseline_fold_seed),
            )
            for beam in beams
        ],
        "fit_windows": [
            (
                f"fit{fit}",
                SensitivityRun(baseline_beam_width, fit, baseline_fold_seed),
            )
            for fit in fits
        ],
        "fold_seed": [
            (
                f"seed{seed}",
                SensitivityRun(baseline_beam_width, baseline_fit_windows, seed),
            )
            for seed in seeds
        ],
    }
    unique = sorted({run for entries in dimensions.values() for _label, run in entries})
    return unique, dimensions
