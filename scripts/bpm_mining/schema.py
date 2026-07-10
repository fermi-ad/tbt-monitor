"""CSV schemas and small data objects for best-BPM mining."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PLANES = ("H", "V")

CHANNEL_REJECTION_FLAGS = [
    "MISSING",
    "DECODE_FAILED",
    "TOO_SHORT",
    "CONSTANT",
    "CLIPPED",
    "NAN_INF",
    "EXTREME_RMS",
    "UNKNOWN_PLANE",
]

SPILLS_FIELDS = [
    "collection",
    "spill_id",
    "timestamp",
    "path",
    "h_bpm_count",
    "v_bpm_count",
    "turn_count_h",
    "turn_count_v",
    "usable_h",
    "usable_v",
    "spill_usable",
    "rejection_flags",
]

BPM_INDEX_FIELDS = ["bpm_index", "bpm_name", "plane", "digitizer", "ring_order", "source_key"]

CHANNEL_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "source_key",
    "payload_path",
    "turn_count",
    "finite",
    "constant",
    "clipped",
    "rms",
    "mad",
    "usable",
    "rejection_flags",
]

REJECTION_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "bpm_name",
    "source_key",
    "payload_path",
    "rejection_flags",
    "detail",
]

SPECTRAL_CACHE_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "spectral_config",
    "spectra_path",
    "tune_axis_path",
    "window_centers_path",
    "bpm_indices_path",
    "n_valid_bpm",
    "n_windows",
    "n_tune_bins",
    "window_turns",
    "stride_turns",
    "turn_start",
    "turn_end",
    "dtype",
    "status",
    "message",
]

PER_BPM_WINDOW_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "spectral_config",
    "window_index",
    "center_turn",
    "candidate_rank",
    "peak_tune",
    "peak_power",
    "peak_prominence_z",
    "peak_to_local_background",
    "peak_width_tune",
    "second_peak_ratio",
    "spectral_entropy",
    "distance_to_band_edge",
    "distance_to_expected_anchor",
    "valid_candidate",
    "quality_flags",
]

PER_BPM_INJECTION_FIELDS = PER_BPM_WINDOW_FIELDS + ["delta_q_2048_4096", "consistent_across_windows"]

PER_BPM_SUMMARY_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "valid_candidate_count",
    "visible_window_fraction",
    "first_visible_turn",
    "last_visible_turn",
    "visibility_duration_turns",
    "median_peak_prominence_z",
    "p10_peak_prominence_z",
    "median_peak_width",
    "median_tune",
    "tune_mad",
    "median_step",
    "p95_step",
    "single_bpm_quality_score",
]

CONSENSUS_WINDOW_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "spectral_config",
    "window_index",
    "center_turn",
    "consensus_tune",
    "consensus_ci_low",
    "consensus_ci_high",
    "consensus_uncertainty",
    "unique_bpm_count",
    "unique_bpm_fraction",
    "total_weight",
    "weighted_mad_tune",
    "cluster_width",
    "cluster_prominence",
    "second_cluster_ratio",
    "consensus_label",
]

CONSENSUS_SUMMARY_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "dominant_consensus_tune",
    "median_consensus_uncertainty",
    "clean_window_fraction",
    "weak_window_fraction",
    "multimodal_window_fraction",
    "no_consensus_window_fraction",
    "consensus_label",
]

BEST_SUBSET_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "subset_mask",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "candidate_pool_size",
    "search_scope",
    "search_exact",
    "audit_performed",
    "aggregator",
    "q_hat",
    "subset_score",
    "holdout_support",
    "peak_quality",
    "consensus_agreement",
    "window_stability",
    "diversity_score",
    "ambiguity_penalty",
    "visible_fraction",
    "visibility_duration_turns",
    "consensus_tune",
    "consensus_label",
    "quality_flags",
]

GLOBAL_BPM_STATS_FIELDS = [
    "plane",
    "bpm_index",
    "bpm_name",
    "digitizer",
    "source_key",
    "ring_order",
    "valid_spill_count",
    "median_percentile_rank",
    "top1_frequency",
    "top3_inclusion_frequency",
    "top5_inclusion_frequency",
    "top10_inclusion_frequency",
    "median_score",
    "median_holdout_support",
    "median_consensus_residual",
    "median_visibility_duration",
    "collection1_rank",
    "collection2_rank",
    "bootstrap_rank_low",
    "bootstrap_rank_high",
]


@dataclass(frozen=True)
class Channel:
    collection: str
    spill_id: str
    timestamp: str
    manifest_path: Path
    payload_path: Path
    plane: str
    bpm_name: str
    digitizer: str
    source_key: str
    sample_count: int
    payload_bytes: Optional[int]


@dataclass(frozen=True)
class Spill:
    collection: str
    spill_id: str
    timestamp: str
    path: Path
    manifest_path: Path
    channels: tuple[Channel, ...]
