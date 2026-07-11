"""Leakage-controlled all-training-channel baselines for accepted Best-N runs."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from .best_n import (
    SUMMARY_FIELDS,
    VALIDATION_FIELDS,
    _f,
    _fmt,
    aggregate_metrics,
    block_bootstrap_ci,
    cache_key,
    cache_rows,
    fold_by_digitizer,
    mean,
    median,
    purged_window_split,
    recommended_n,
    summarize,
)
from .contracts import ensure_run_contract, file_sha256, load_run_contract, object_sha256
from .identity import identity_fields, manifest_by_index
from .io import atomic_write_text, read_csv, write_csv
from .statistics import stable_seed
from .subset_search import metadata_for_bpms


BASELINE_METHODS = ("all_training_mean", "all_training_median")
DETAIL_FIELDS = ["method", *VALIDATION_FIELDS]
BASELINE_SUMMARY_FIELDS = [
    "method",
    *SUMMARY_FIELDS,
    "train_channel_count_min",
    "train_channel_count_median",
    "train_channel_count_max",
    "heldout_channel_count_min",
    "heldout_channel_count_median",
    "heldout_channel_count_max",
]
COMPARISON_FIELDS = [
    "plane",
    "selected_n",
    "baseline_method",
    "metric",
    "favorable_direction",
    "exact_paired_row_count",
    "paired_spill_count",
    "selected_estimate",
    "baseline_estimate",
    "paired_delta_selected_minus_baseline",
    "paired_delta_ci_low",
    "paired_delta_ci_high",
    "result",
]
PAIRED_SPILL_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "selected_n",
    "baseline_method",
    "metric",
    "favorable_direction",
    "fold_pair_count",
    "selected_value",
    "baseline_value",
    "paired_delta_selected_minus_baseline",
    "favorable_delta",
]
COMPARISON_METRICS = (
    ("blind_agreement", "blind_q_agreement_within_tolerance", "higher", mean),
    ("blind_abs_q_delta", "blind_selected_heldout_abs_q_delta", "lower", median),
    ("later_prominence", "test_peak_prominence_at_qhat", "higher", median),
    ("later_power", "test_power_support_at_qhat", "higher", median),
)


def _spill_sort_key(spill_id: str) -> tuple[int, object]:
    try:
        return 0, int(spill_id)
    except ValueError:
        return 1, spill_id


def aggregate_blind_q(
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    aggregator: str,
) -> float:
    if spectra.size == 0 or spectra.shape[1] == 0:
        return math.nan
    clean = np.asarray(spectra, dtype=np.float32)
    if aggregator == "median":
        combined = np.median(clean, axis=0)
    elif aggregator == "mean":
        combined = np.mean(clean, axis=0, dtype=np.float32)
    else:
        raise ValueError(f"unsupported all-training aggregator: {aggregator}")
    peak_indices = np.argmax(combined, axis=1)
    return median([float(value) for value in tune_axis[peak_indices]])


def all_training_rows_for_cache(
    cache: dict[str, str],
    cfg: dict[str, object],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
    folds: int,
    fold_seed: int,
    requested_fit_windows: int,
    tune_half_width: float,
    methods: Sequence[str] = BASELINE_METHODS,
) -> list[dict[str, object]]:
    spectra = np.asarray(np.load(cache["spectra_path"], mmap_mode="r"), dtype=np.float32)
    tune_axis = np.asarray(np.load(cache["tune_axis_path"]), dtype=np.float32)
    centers = np.asarray(np.load(cache["window_centers_path"]), dtype=np.float32)
    bpm_indices = np.asarray(np.load(cache["bpm_indices_path"]), dtype=np.int32)
    plane = cache["plane"]
    bpm_meta = metadata_for_bpms(Path(str(cfg["_manifest_dir"])), plane)
    window_turns = int(cache.get("window_turns") or 4096)
    fit_count, test_start = purged_window_split(centers, requested_fit_windows, window_turns)
    assigned = fold_by_digitizer(bpm_indices, bpm_meta, folds, fold_seed)
    rows: list[dict[str, object]] = []
    for fold in range(folds):
        train_positions = [pos for pos, idx in enumerate(bpm_indices) if assigned[int(idx)] != fold]
        heldout_positions = [pos for pos, idx in enumerate(bpm_indices) if assigned[int(idx)] == fold]
        if not train_positions or not heldout_positions:
            continue
        train_fit_spectra = spectra[train_positions, :fit_count, :]
        train_test_spectra = spectra[train_positions, test_start:, :]
        heldout_test_spectra = spectra[heldout_positions, test_start:, :]
        train_indices = [int(bpm_indices[pos]) for pos in train_positions]
        identities = identity_fields(plane, train_indices, meta_by_index)
        for method in methods:
            aggregator = method.removeprefix("all_training_")
            train_q = aggregate_blind_q(train_fit_spectra, tune_axis, aggregator)
            baseline_test = aggregate_metrics(
                train_test_spectra,
                tune_axis,
                train_q,
                tune_half_width,
                aggregator,
            )
            heldout_test = aggregate_metrics(
                heldout_test_spectra,
                tune_axis,
                train_q,
                tune_half_width,
                "median",
            )
            train_test_delta = abs(baseline_test["q_hat"] - train_q)
            selected_heldout_delta = abs(baseline_test["q_hat"] - heldout_test["q_hat"])
            blind_selected_heldout_delta = abs(
                baseline_test["blind_q_hat"] - heldout_test["blind_q_hat"]
            )
            rows.append(
                {
                    "method": method,
                    "collection": cache["collection"],
                    "spill_id": cache["spill_id"],
                    "plane": plane,
                    "fold": fold,
                    "subset_size": len(train_indices),
                    **identities,
                    "train_q_hat": _fmt(train_q),
                    "train_score": "",
                    "train_visible_fraction": "",
                    "test_q_hat_near_train": _fmt(baseline_test["q_hat"]),
                    "heldout_q_hat_near_train": _fmt(heldout_test["q_hat"]),
                    "selected_test_blind_q_hat": _fmt(baseline_test["blind_q_hat"]),
                    "heldout_blind_q_hat": _fmt(heldout_test["blind_q_hat"]),
                    "train_test_abs_q_delta": _fmt(train_test_delta),
                    "selected_heldout_abs_q_delta": _fmt(selected_heldout_delta),
                    "blind_selected_heldout_abs_q_delta": _fmt(blind_selected_heldout_delta),
                    "q_agreement_within_tolerance": (
                        _fmt(float(selected_heldout_delta <= tune_half_width))
                        if math.isfinite(selected_heldout_delta)
                        else ""
                    ),
                    "blind_q_agreement_within_tolerance": (
                        _fmt(float(blind_selected_heldout_delta <= tune_half_width))
                        if math.isfinite(blind_selected_heldout_delta)
                        else ""
                    ),
                    "test_peak_prominence_at_qhat": _fmt(baseline_test["peak_prominence"]),
                    "test_p10_peak_prominence_at_qhat": _fmt(
                        baseline_test["p10_peak_prominence"]
                    ),
                    "test_power_support_at_qhat": _fmt(baseline_test["power_support"]),
                    "test_visible_fraction_at_qhat": _fmt(baseline_test["visible_fraction"]),
                    "test_spectral_entropy": _fmt(baseline_test["spectral_entropy"]),
                    "heldout_power_support_at_qhat": _fmt(heldout_test["power_support"]),
                    "heldout_prominence_at_qhat": _fmt(heldout_test["peak_prominence"]),
                    "heldout_visible_fraction_at_qhat": _fmt(heldout_test["visible_fraction"]),
                    "train_channel_count": len(train_positions),
                    "heldout_channel_count": len(heldout_positions),
                    "fit_window_count": fit_count,
                    "test_window_count": train_test_spectra.shape[1],
                    "fit_end_turn": _fmt(float(centers[fit_count - 1]) + window_turns / 2.0),
                    "test_start_turn": (
                        _fmt(float(centers[test_start]) - window_turns / 2.0)
                        if test_start < len(centers)
                        else ""
                    ),
                    "beam_width": 0,
                    "candidates_scored": 1,
                }
            )
    return rows


def detail_key(row: Mapping[str, object]) -> tuple[str, str, str, int, str]:
    return (
        str(row.get("collection", "")),
        str(row.get("spill_id", "")),
        str(row.get("plane", "")),
        int(row.get("fold") or 0),
        str(row.get("method", "")),
    )


def selected_key(row: Mapping[str, object]) -> tuple[str, str, str, int]:
    return (
        str(row.get("collection", "")),
        str(row.get("spill_id", "")),
        str(row.get("plane", "")),
        int(row.get("fold") or 0),
    )


def completed_cache_keys(
    rows: Sequence[Mapping[str, object]],
    methods: Sequence[str],
    folds: int,
) -> set[tuple[str, str, str]]:
    observed: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        observed[cache_key(row)].append((int(row.get("fold") or 0), str(row.get("method", ""))))
    expected = sorted((fold, method) for fold in range(folds) for method in methods)
    return {key for key, values in observed.items() if sorted(values) == expected}


def sorted_detail_rows(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rows, key=detail_key)


def baseline_summary_rows(
    rows: Sequence[dict[str, object]],
    bootstrap_samples: int,
    bootstrap_block_spills: int,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for method in BASELINE_METHODS:
        method_rows = [row for row in rows if row.get("method") == method]
        # All available training channels define one method even when a fold or
        # spill has fewer channels. Collapse the grouping sentinel while keeping
        # the exact per-row cardinality and its range in the exported summary.
        normalized = [{**row, "subset_size": 0} for row in method_rows]
        for row in summarize([], normalized, bootstrap_samples, bootstrap_block_spills):
            plane_rows = [item for item in method_rows if item.get("plane") == row["plane"]]
            train_counts = [float(item["train_channel_count"]) for item in plane_rows]
            heldout_counts = [float(item["heldout_channel_count"]) for item in plane_rows]
            out.append(
                {
                    "method": method,
                    **row,
                    "train_channel_count_min": int(min(train_counts)),
                    "train_channel_count_median": _fmt(median(train_counts)),
                    "train_channel_count_max": int(max(train_counts)),
                    "heldout_channel_count_min": int(min(heldout_counts)),
                    "heldout_channel_count_median": _fmt(median(heldout_counts)),
                    "heldout_channel_count_max": int(max(heldout_counts)),
                }
            )
    return sorted(out, key=lambda row: (str(row["plane"]), str(row["method"])))


def paired_spill_rows(
    selected_rows: Sequence[dict[str, object]],
    baseline_rows: Sequence[dict[str, object]],
    selected_sizes: Mapping[str, int],
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for plane in ("H", "V"):
        selected_n = int(selected_sizes[plane])
        selected = {
            selected_key(row): row
            for row in selected_rows
            if row.get("plane") == plane and int(row.get("subset_size") or 0) == selected_n
        }
        for method in BASELINE_METHODS:
            baseline = {
                selected_key(row): row
                for row in baseline_rows
                if row.get("plane") == plane and row.get("method") == method
            }
            if set(selected) != set(baseline):
                raise ValueError(
                    f"selected Best-N and {method} key sets differ for {plane}: "
                    f"selected={len(selected)} baseline={len(baseline)}"
                )
            pairs = [(selected[key], baseline[key]) for key in sorted(selected)]
            for metric, field, direction, statistic in COMPARISON_METRICS:
                grouped: dict[tuple[str, str], list[tuple[float, float, float]]] = defaultdict(list)
                for selected_row, baseline_row in pairs:
                    selected_value = _f(selected_row.get(field))
                    baseline_value = _f(baseline_row.get(field))
                    if not math.isfinite(selected_value) or not math.isfinite(baseline_value):
                        continue
                    grouped[
                        (str(selected_row["collection"]), str(selected_row["spill_id"]))
                    ].append(
                        (
                            selected_value,
                            baseline_value,
                            selected_value - baseline_value,
                        )
                    )
                if not grouped:
                    raise ValueError(f"no finite paired {metric} values for {plane} {method}")
                for (collection, spill_id), values in sorted(
                    grouped.items(),
                    key=lambda item: (item[0][0], _spill_sort_key(item[0][1])),
                ):
                    selected_value = statistic([value[0] for value in values])
                    baseline_value = statistic([value[1] for value in values])
                    delta = statistic([value[2] for value in values])
                    out.append(
                        {
                            "collection": collection,
                            "spill_id": spill_id,
                            "plane": plane,
                            "selected_n": selected_n,
                            "baseline_method": method,
                            "metric": metric,
                            "favorable_direction": direction,
                            "fold_pair_count": len(values),
                            "selected_value": _fmt(selected_value),
                            "baseline_value": _fmt(baseline_value),
                            "paired_delta_selected_minus_baseline": _fmt(delta),
                            "favorable_delta": _fmt(delta if direction == "higher" else -delta),
                        }
                    )
    return out


def comparison_rows(
    paired_rows: Sequence[dict[str, object]],
    selected_sizes: Mapping[str, int],
    bootstrap_samples: int,
    bootstrap_block_spills: int,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    statistics = {metric: statistic for metric, _field, _direction, statistic in COMPARISON_METRICS}
    directions = {metric: direction for metric, _field, direction, _statistic in COMPARISON_METRICS}
    for plane in ("H", "V"):
        selected_n = int(selected_sizes[plane])
        for method in BASELINE_METHODS:
            for metric, _field, _direction, _statistic in COMPARISON_METRICS:
                rows = [
                    row
                    for row in paired_rows
                    if row.get("plane") == plane
                    and row.get("baseline_method") == method
                    and row.get("metric") == metric
                ]
                statistic = statistics[metric]
                direction = directions[metric]
                delta_series: dict[str, list[float]] = defaultdict(list)
                selected_values: list[float] = []
                baseline_values: list[float] = []
                delta_values: list[float] = []
                exact_pair_count = 0
                for row in rows:
                    selected_value = _f(row.get("selected_value"))
                    baseline_value = _f(row.get("baseline_value"))
                    delta = _f(row.get("paired_delta_selected_minus_baseline"))
                    if not all(math.isfinite(value) for value in (selected_value, baseline_value, delta)):
                        continue
                    selected_values.append(selected_value)
                    baseline_values.append(baseline_value)
                    delta_values.append(delta)
                    delta_series[str(row["collection"])].append(delta)
                    exact_pair_count += int(row.get("fold_pair_count") or 0)
                if not delta_values:
                    raise ValueError(f"no finite paired {metric} values for {plane} {method}")
                low, high = block_bootstrap_ci(
                    delta_series,
                    bootstrap_samples,
                    stable_seed("best-n-all-training", plane, method, metric),
                    bootstrap_block_spills,
                    statistic,
                )
                delta = statistic(delta_values)
                if direction == "higher":
                    result = "SELECTED_FAVORED" if low > 0 else "BASELINE_FAVORED" if high < 0 else "UNRESOLVED"
                else:
                    result = "SELECTED_FAVORED" if high < 0 else "BASELINE_FAVORED" if low > 0 else "UNRESOLVED"
                out.append(
                    {
                        "plane": plane,
                        "selected_n": selected_n,
                        "baseline_method": method,
                        "metric": metric,
                        "favorable_direction": direction,
                        "exact_paired_row_count": exact_pair_count,
                        "paired_spill_count": len(delta_values),
                        "selected_estimate": _fmt(statistic(selected_values)),
                        "baseline_estimate": _fmt(statistic(baseline_values)),
                        "paired_delta_selected_minus_baseline": _fmt(delta),
                        "paired_delta_ci_low": _fmt(low),
                        "paired_delta_ci_high": _fmt(high),
                        "result": result,
                    }
                )
    return out


def comparison_report(rows: Sequence[dict[str, object]]) -> str:
    lines = [
        "# Best-N Versus All Training Channels",
        "",
        "This control uses the same purged later windows and held-out digitizer folds as the accepted Best-N validation. `all_training_mean` and `all_training_median` aggregate every channel on the training side of each fold; they are not the literal 60-channel all-BPM result because held-out channels remain independent.",
        "",
    ]
    for plane in ("H", "V"):
        selected_n = next((row.get("selected_n") for row in rows if row.get("plane") == plane), "")
        lines.extend([f"## {plane} Best-{selected_n}", ""])
        for row in rows:
            if row.get("plane") != plane:
                continue
            lines.append(
                f"- {row.get('baseline_method')} {row.get('metric')}: selected {row.get('selected_estimate')}, "
                f"baseline {row.get('baseline_estimate')}, selected-minus-baseline "
                f"{row.get('paired_delta_selected_minus_baseline')} "
                f"[{row.get('paired_delta_ci_low')}, {row.get('paired_delta_ci_high')}], "
                f"{row.get('result')}."
            )
        lines.append("")
    lines.append(
        "This is an internal held-out reproducibility control, not an external tune calibration."
    )
    return "\n".join(lines) + "\n"


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def verify_outputs(root: Path, write_outputs: bool = True) -> dict[str, object]:
    issues: list[dict[str, str]] = []

    def issue(code: str, message: str) -> None:
        issues.append({"code": code, "message": message})

    contract_path = root / "run_contract.json"
    detail_path = root / "best_n_all_training_validation.csv"
    summary_path = root / "best_n_all_training_summary.csv"
    paired_path = root / "best_n_vs_all_training_paired_spills.csv"
    comparison_path = root / "best_n_vs_all_training_comparison.csv"
    report_path = root / "best_n_vs_all_training_report.md"
    plots_root = root / "plots"
    plot_manifest_path = plots_root / "all_training_plot_manifest.csv"
    try:
        contract = load_run_contract(contract_path)
    except Exception as exc:
        contract = {}
        issue("contract", str(exc))
    methods = tuple(contract.get("methods") or ())
    folds = int(contract.get("folds") or 0)
    expected_cache_keys = int(contract.get("expected_validation_cache_keys") or 0)
    expected_cache_keys_by_plane = {
        str(key): int(value)
        for key, value in (contract.get("expected_cache_keys_by_plane") or {}).items()
    }
    expected_selected_sizes = contract.get("selected_sizes") or {}
    expected_folds = set(range(folds))
    source_hashes = contract.get("source_hashes") if isinstance(contract, dict) else None
    source_paths = {
        str(label): Path(str(value.get("path") or ""))
        for label, value in (source_hashes.items() if isinstance(source_hashes, dict) else ())
        if isinstance(value, dict)
    }
    for path, code in (
        (detail_path, "detail_missing"),
        (summary_path, "summary_missing"),
        (paired_path, "paired_missing"),
        (comparison_path, "comparison_missing"),
        (report_path, "report_missing"),
        (plot_manifest_path, "plot_manifest_missing"),
    ):
        if not path.is_file() or path.stat().st_size == 0:
            issue(code, f"missing or empty output: {path}")
    detail = read_csv(detail_path) if detail_path.is_file() else []
    if methods != BASELINE_METHODS:
        issue("methods", f"expected methods {BASELINE_METHODS}, found {methods}")
    expected_detail_rows = expected_cache_keys * folds * len(BASELINE_METHODS)
    if len(detail) != expected_detail_rows:
        issue("detail_count", f"expected {expected_detail_rows} detail rows, found {len(detail)}")
    if len({detail_key(row) for row in detail}) != len(detail):
        issue("detail_duplicates", "all-training detail rows contain duplicate science keys")
    complete = completed_cache_keys(detail, BASELINE_METHODS, folds) if folds else set()
    if len(complete) != expected_cache_keys:
        issue("detail_coverage", f"expected {expected_cache_keys} complete cache keys, found {len(complete)}")
    source_validation_path = source_paths.get("best_n_validation", Path())
    source_validation = read_csv(source_validation_path) if source_validation_path.is_file() else []
    source_cache_keys = {cache_key(row) for row in source_validation}
    if source_cache_keys and complete != source_cache_keys:
        issue("detail_source_coverage", "all-training cache keys differ from accepted Best-N validation")
    for row in detail:
        train_count = int(row.get("train_channel_count") or 0)
        if int(row.get("subset_size") or 0) != train_count:
            issue("cardinality", f"subset/train count mismatch for {detail_key(row)}")
            break
        if len([value for value in row.get("bpm_indices", "").split(",") if value]) != train_count:
            issue("identity_cardinality", f"identity count mismatch for {detail_key(row)}")
            break
        if len([value for value in row.get("bpm_source_keys", "").split(",") if value]) != train_count:
            issue("source_key_cardinality", f"source-key count mismatch for {detail_key(row)}")
            break
        if len([value for value in row.get("bpm_digitizers", "").split(",") if value]) != train_count:
            issue("digitizer_cardinality", f"digitizer count mismatch for {detail_key(row)}")
            break
        fit_end = _f(row.get("fit_end_turn"))
        test_start = _f(row.get("test_start_turn"))
        if not math.isfinite(fit_end) or not math.isfinite(test_start):
            issue("timing_nonfinite", f"fit/test timing is nonfinite for {detail_key(row)}")
            break
        if fit_end > test_start:
            issue("timing_overlap", f"fit/test overlap for {detail_key(row)}")
            break
        if int(row.get("fit_window_count") or 0) < 1 or int(row.get("test_window_count") or 0) < 1:
            issue("window_count", f"fit/test window count is empty for {detail_key(row)}")
            break
        for field in (
            "train_q_hat",
            "selected_test_blind_q_hat",
            "heldout_blind_q_hat",
            "blind_selected_heldout_abs_q_delta",
            "test_peak_prominence_at_qhat",
            "test_power_support_at_qhat",
        ):
            if not math.isfinite(_f(row.get(field))):
                issue("nonfinite_metric", f"{field} is nonfinite for {detail_key(row)}")
                break
        if issues and issues[-1]["code"] == "nonfinite_metric":
            break
    by_cache_method: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    for row in detail:
        by_cache_method[(*cache_key(row), str(row.get("method", "")))].add(
            int(row.get("fold") or 0)
        )
    if any(observed != expected_folds for observed in by_cache_method.values()):
        issue("fold_coverage", "one or more all-training cache/method groups lack the exact fold set")
    summary = read_csv(summary_path) if summary_path.is_file() else []
    if len(summary) != 2 * len(BASELINE_METHODS):
        issue("summary_count", f"expected {2 * len(BASELINE_METHODS)} summary rows, found {len(summary)}")
    expected_summary_keys = {
        (plane, method) for plane in ("H", "V") for method in BASELINE_METHODS
    }
    summary_keys = {(row.get("plane", ""), row.get("method", "")) for row in summary}
    if summary_keys != expected_summary_keys:
        issue("summary_coverage", f"summary method/plane coverage differs: {sorted(summary_keys)}")
    for row in summary:
        if int(row.get("subset_size") or -1) != 0:
            issue("summary_sentinel", f"all-training summary subset sentinel is not zero: {row}")
            break
        for prefix in ("train", "heldout"):
            low = _f(row.get(f"{prefix}_channel_count_min"))
            middle = _f(row.get(f"{prefix}_channel_count_median"))
            high = _f(row.get(f"{prefix}_channel_count_max"))
            if not all(math.isfinite(value) for value in (low, middle, high)) or not low <= middle <= high:
                issue("summary_cardinality", f"invalid {prefix} channel-count range: {row}")
                break
    paired = read_csv(paired_path) if paired_path.is_file() else []
    expected_paired_rows = expected_cache_keys * len(BASELINE_METHODS) * len(COMPARISON_METRICS)
    if len(paired) != expected_paired_rows:
        issue("paired_count", f"expected {expected_paired_rows} paired-spill rows, found {len(paired)}")
    paired_keys = {
        (
            row.get("collection", ""),
            row.get("spill_id", ""),
            row.get("plane", ""),
            row.get("baseline_method", ""),
            row.get("metric", ""),
        )
        for row in paired
    }
    if len(paired_keys) != len(paired):
        issue("paired_duplicates", "paired-spill rows contain duplicate science keys")
    for row in paired:
        if int(row.get("fold_pair_count") or 0) != folds:
            issue("paired_fold_count", f"paired spill does not contain every fold: {row}")
            break
        if int(row.get("selected_n") or 0) != int(expected_selected_sizes.get(row.get("plane"), 0)):
            issue("paired_selected_n", f"paired spill selected N disagrees with contract: {row}")
            break
        delta = _f(row.get("paired_delta_selected_minus_baseline"))
        favorable = _f(row.get("favorable_delta"))
        expected_favorable = delta if row.get("favorable_direction") == "higher" else -delta
        if not math.isfinite(delta) or not math.isfinite(favorable) or not math.isclose(
            favorable, expected_favorable, rel_tol=0.0, abs_tol=1e-8
        ):
            issue("paired_delta", f"paired favorable delta is inconsistent: {row}")
            break
    expected_pair_keys_by_plane = {
        plane: {
            (key[0], key[1]) for key in source_cache_keys if key[2] == plane
        }
        for plane in ("H", "V")
    }
    for plane in ("H", "V"):
        if len(expected_pair_keys_by_plane[plane]) != expected_cache_keys_by_plane.get(plane, 0):
            issue("paired_plane_coverage", f"paired spill coverage differs for {plane}")
        for method in BASELINE_METHODS:
            for metric, _field, _direction, _statistic in COMPARISON_METRICS:
                observed = {
                    (row.get("collection", ""), row.get("spill_id", ""))
                    for row in paired
                    if row.get("plane") == plane
                    and row.get("baseline_method") == method
                    and row.get("metric") == metric
                }
                if observed != expected_pair_keys_by_plane[plane]:
                    issue(
                        "paired_group_coverage",
                        f"paired spill keys differ for {plane}/{method}/{metric}",
                    )
                    break
    comparison = read_csv(comparison_path) if comparison_path.is_file() else []
    expected_comparisons = 2 * len(BASELINE_METHODS) * len(COMPARISON_METRICS)
    if len(comparison) != expected_comparisons:
        issue("comparison_count", f"expected {expected_comparisons} comparison rows, found {len(comparison)}")
    comparison_keys = {
        (row.get("plane", ""), row.get("baseline_method", ""), row.get("metric", ""))
        for row in comparison
    }
    expected_comparison_keys = {
        (plane, method, metric)
        for plane in ("H", "V")
        for method in BASELINE_METHODS
        for metric, _field, _direction, _statistic in COMPARISON_METRICS
    }
    if comparison_keys != expected_comparison_keys:
        issue("comparison_coverage", "comparison method/plane/metric coverage differs from contract")
    for row in comparison:
        if row.get("result") not in {"SELECTED_FAVORED", "BASELINE_FAVORED", "UNRESOLVED"}:
            issue("comparison_result", f"invalid comparison result: {row}")
            break
        if int(row.get("selected_n") or 0) != int(expected_selected_sizes.get(row.get("plane"), 0)):
            issue("selected_n", f"comparison selected N disagrees with contract: {row}")
            break
        plane_cache_keys = expected_cache_keys_by_plane.get(str(row.get("plane", "")), 0)
        if int(row.get("exact_paired_row_count") or 0) != plane_cache_keys * folds:
            issue("paired_row_count", f"comparison does not cover every plane-specific fold row: {row}")
            break
        if int(row.get("paired_spill_count") or 0) != plane_cache_keys:
            issue("paired_spill_count", f"comparison does not cover every plane-specific spill: {row}")
            break
        for field in (
            "selected_estimate",
            "baseline_estimate",
            "paired_delta_selected_minus_baseline",
            "paired_delta_ci_low",
            "paired_delta_ci_high",
        ):
            if not math.isfinite(_f(row.get(field))):
                issue("comparison_nonfinite", f"{field} is nonfinite: {row}")
                break
        if issues and issues[-1]["code"] == "comparison_nonfinite":
            break
        if _f(row.get("paired_delta_ci_low")) > _f(row.get("paired_delta_ci_high")):
            issue("comparison_interval", f"comparison interval is reversed: {row}")
            break
    plot_manifest = read_csv(plot_manifest_path) if plot_manifest_path.is_file() else []
    expected_plot_keys = {
        (plane, "scoreboard", "all") for plane in ("H", "V")
    } | {
        (plane, plot_type, metric)
        for plane in ("H", "V")
        for plot_type in ("paired_scatter", "favorable_delta_cdf")
        for metric, _field, _direction, _statistic in COMPARISON_METRICS
    }
    observed_plot_keys = {
        (row.get("plane", ""), row.get("plot_type", ""), row.get("metric", ""))
        for row in plot_manifest
    }
    if observed_plot_keys != expected_plot_keys or len(plot_manifest) != len(expected_plot_keys):
        issue("plot_coverage", "all-training plot manifest does not contain the exact review set")
    plot_names = [row.get("filename", "") for row in plot_manifest]
    if len(set(plot_names)) != len(plot_names):
        issue("plot_duplicates", "all-training plot manifest contains duplicate filenames")
    for row in plot_manifest:
        filename = str(row.get("filename", ""))
        if not filename or Path(filename).name != filename or not filename.endswith(".png"):
            issue("plot_filename", f"unsafe plot filename: {filename}")
            break
        dimensions = _png_dimensions(plots_root / filename)
        expected_dimensions = (int(row.get("width") or 0), int(row.get("height") or 0))
        if dimensions != expected_dimensions:
            issue(
                "plot_dimensions",
                f"plot dimensions differ for {filename}: expected {expected_dimensions}, found {dimensions}",
            )
            break
    if isinstance(source_hashes, dict):
        for label, value in source_hashes.items():
            source = Path(str(value.get("path") or "")) if isinstance(value, dict) else Path()
            expected_hash = str(value.get("sha256") or "") if isinstance(value, dict) else ""
            if not source.is_file() or file_sha256(source) != expected_hash:
                issue("source_hash", f"source changed after evaluation: {label}: {source}")
                break
    output_paths = [
        detail_path,
        summary_path,
        paired_path,
        comparison_path,
        report_path,
        plot_manifest_path,
        *(plots_root / str(row.get("filename", "")) for row in plot_manifest),
    ]
    output_hashes = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in output_paths
        if path.is_file()
    }
    result = {
        "schema": "tbt-monitor.best-n-all-training-verification/v1",
        "status": "pass" if not issues else "fail",
        "issue_count": len(issues),
        "issues": issues,
        "detail_rows": len(detail),
        "complete_cache_keys": len(complete),
        "summary_rows": len(summary),
        "paired_spill_rows": len(paired),
        "comparison_rows": len(comparison),
        "plot_rows": len(plot_manifest),
        "output_sha256": output_hashes,
    }
    if write_outputs:
        atomic_write_text(
            root / "best_n_all_training_verification.json",
            json.dumps(result, indent=2, sort_keys=True) + "\n",
        )
        atomic_write_text(
            root / "best_n_all_training_verification.md",
            "# Best-N All-Training Verification\n\n"
            f"- status: `{result['status']}`\n"
            f"- issues: `{len(issues)}`\n"
            f"- detail rows: `{len(detail)}`\n"
            f"- complete cache keys: `{len(complete)}`\n"
            f"- summary rows: `{len(summary)}`\n"
            f"- paired spill rows: `{len(paired)}`\n"
            f"- comparison rows: `{len(comparison)}`\n"
            f"- plots: `{len(plot_manifest)}`\n"
            f"- hashed outputs: `{len(output_hashes)}`\n"
            + "".join(f"- {row['code']}: {row['message']}\n" for row in issues),
        )
    return result


def evaluate(
    cfg: dict[str, object],
    inputs_root: Path,
    best_n_root: Path,
    out: Path,
    progress_every: int = 25,
    resume: bool = False,
) -> dict[str, object]:
    accepted_contract = load_run_contract(best_n_root / "run_contract.json")
    if accepted_contract.get("analysis") != "best_n_merged":
        raise ValueError("all-training control requires an accepted merged Best-N root")
    accepted_verification_path = best_n_root / "best_n_verification.json"
    try:
        accepted_verification = json.loads(
            accepted_verification_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"all-training control requires a valid Best-N verification report: {exc}"
        ) from exc
    if accepted_verification.get("status") != "pass":
        raise ValueError("all-training control requires a passing Best-N verification report")
    folds = int(accepted_contract["folds"])
    fold_seed = int(accepted_contract["fold_seed"])
    fit_windows = int(accepted_contract["fit_windows"])
    tune_half_width = float(accepted_contract["tune_half_width"])
    bootstrap_samples = int(accepted_contract["bootstrap_samples"])
    bootstrap_block_spills = int(accepted_contract["bootstrap_block_spills"])
    spectral_config = str(accepted_contract["spectral_config"])
    selected_validation_path = best_n_root / "best_n_disjoint_validation.csv"
    selected_summary_path = best_n_root / "best_n_summary.csv"
    selected_rows = read_csv(selected_validation_path)
    selected_summary = read_csv(selected_summary_path)
    selected_sizes: dict[str, int] = {}
    accepted_recommendations = accepted_verification.get("recommendations") or {}
    for plane in ("H", "V"):
        chosen, reason = recommended_n(selected_summary, plane, tune_half_width)
        if chosen is None:
            raise ValueError(f"accepted Best-N root has no {plane} recommendation: {reason}")
        selected_sizes[plane] = int(chosen["subset_size"])
        reported = accepted_recommendations.get(plane) or {}
        if int(reported.get("recommended_n") or 0) != selected_sizes[plane]:
            raise ValueError(
                f"Best-N verifier and summary disagree for {plane}: "
                f"verifier={reported.get('recommended_n')} summary={selected_sizes[plane]}"
            )
    expected_keys = {cache_key(row) for row in selected_rows}
    all_cache = cache_rows(inputs_root, spectral_config)
    cache_by_key = {cache_key(row): row for row in all_cache}
    if not expected_keys <= set(cache_by_key):
        missing = sorted(expected_keys - set(cache_by_key))
        raise ValueError(f"accepted validation cache keys are absent from inputs: {missing[:5]}")
    validation_cache = [cache_by_key[key] for key in sorted(expected_keys)]
    expected_cache_keys_by_plane = {
        plane: sum(1 for key in expected_keys if key[2] == plane) for plane in ("H", "V")
    }
    if sum(expected_cache_keys_by_plane.values()) != len(validation_cache):
        raise ValueError("accepted validation cache contains a plane outside H/V")
    manifest_dir = inputs_root / "manifest"
    runtime_cfg = dict(cfg)
    runtime_cfg["_manifest_dir"] = str(manifest_dir)
    meta_by_index = manifest_by_index(read_csv(manifest_dir / "bpm_index.csv"))
    out.mkdir(parents=True, exist_ok=True)
    detail_path = out / "best_n_all_training_validation.csv"
    summary_path = out / "best_n_all_training_summary.csv"
    paired_path = out / "best_n_vs_all_training_paired_spills.csv"
    comparison_path = out / "best_n_vs_all_training_comparison.csv"
    report_path = out / "best_n_vs_all_training_report.md"
    contract_cfg = {key: value for key, value in runtime_cfg.items() if key != "_manifest_dir"}
    source_hashes = {
        "all_training_algorithm": {
            "path": str(Path(__file__).resolve()),
            "sha256": file_sha256(Path(__file__)),
        },
        "all_training_plots": {
            "path": str(Path(__file__).with_name("all_training_plots.py").resolve()),
            "sha256": file_sha256(Path(__file__).with_name("all_training_plots.py")),
        },
        "best_n_primitives": {
            "path": str(Path(__file__).with_name("best_n.py").resolve()),
            "sha256": file_sha256(Path(__file__).with_name("best_n.py")),
        },
        "best_n_contract": {
            "path": str((best_n_root / "run_contract.json").resolve()),
            "sha256": file_sha256(best_n_root / "run_contract.json"),
        },
        "best_n_validation": {
            "path": str(selected_validation_path.resolve()),
            "sha256": file_sha256(selected_validation_path),
        },
        "best_n_summary": {
            "path": str(selected_summary_path.resolve()),
            "sha256": file_sha256(selected_summary_path),
        },
        "best_n_verification": {
            "path": str(accepted_verification_path.resolve()),
            "sha256": file_sha256(accepted_verification_path),
        },
        "bpm_index": {
            "path": str((manifest_dir / "bpm_index.csv").resolve()),
            "sha256": file_sha256(manifest_dir / "bpm_index.csv"),
        },
        "spectral_cache_index": {
            "path": str((inputs_root / "cache" / "index" / "spectral_cache.csv").resolve()),
            "sha256": file_sha256(inputs_root / "cache" / "index" / "spectral_cache.csv"),
        },
    }
    ensure_run_contract(
        out / "run_contract.json",
        {
            "analysis": "best_n_all_training",
            "config_sha256": object_sha256(contract_cfg),
            "inputs_root": str(inputs_root.resolve()),
            "best_n_root": str(best_n_root.resolve()),
            "bpm_index_sha256": file_sha256(manifest_dir / "bpm_index.csv"),
            "spectral_cache_index_sha256": file_sha256(
                inputs_root / "cache" / "index" / "spectral_cache.csv"
            ),
            "spectral_config": spectral_config,
            "methods": list(BASELINE_METHODS),
            "expected_validation_cache_keys": len(validation_cache),
            "expected_cache_keys_by_plane": expected_cache_keys_by_plane,
            "folds": folds,
            "fold_seed": fold_seed,
            "fit_windows": fit_windows,
            "tune_half_width": tune_half_width,
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_block_spills": bootstrap_block_spills,
            "selected_sizes": selected_sizes,
            "source_hashes": source_hashes,
        },
        (detail_path, summary_path, paired_path, comparison_path, report_path),
    )
    rows: list[dict[str, object]] = list(read_csv(detail_path)) if resume and detail_path.exists() else []
    complete = completed_cache_keys(rows, BASELINE_METHODS, folds)
    started = time.time()
    for index, cache in enumerate(validation_cache, start=1):
        key = cache_key(cache)
        if key not in complete:
            rows = [row for row in rows if cache_key(row) != key]
            rows.extend(
                all_training_rows_for_cache(
                    cache,
                    runtime_cfg,
                    meta_by_index,
                    folds,
                    fold_seed,
                    fit_windows,
                    tune_half_width,
                )
            )
        if progress_every and (index % progress_every == 0 or index == len(validation_cache)):
            write_csv(detail_path, sorted_detail_rows(rows), DETAIL_FIELDS)
            atomic_write_text(
                out / "progress.txt",
                f"validation {index}/{len(validation_cache)} rows={len(rows)} elapsed_seconds={time.time() - started:.1f}\n",
            )
    rows = sorted_detail_rows(rows)
    write_csv(detail_path, rows, DETAIL_FIELDS)
    summary = baseline_summary_rows(rows, bootstrap_samples, bootstrap_block_spills)
    write_csv(summary_path, summary, BASELINE_SUMMARY_FIELDS)
    paired = paired_spill_rows(selected_rows, rows, selected_sizes)
    write_csv(paired_path, paired, PAIRED_SPILL_FIELDS)
    comparisons = comparison_rows(
        paired,
        selected_sizes,
        bootstrap_samples,
        bootstrap_block_spills,
    )
    write_csv(comparison_path, comparisons, COMPARISON_FIELDS)
    atomic_write_text(report_path, comparison_report(comparisons))
    from .all_training_plots import write_plots

    write_plots(paired, comparisons, out / "plots")
    atomic_write_text(
        out / "run_summary.md",
        "# Best-N All-Training Control Run\n\n"
        f"- accepted Best-N root: `{best_n_root}`\n"
        f"- exact validation cache keys: `{len(validation_cache)}`\n"
        f"- folds: `{folds}`\n"
        f"- methods: `{', '.join(BASELINE_METHODS)}`\n"
        f"- selected sizes: `H Best-{selected_sizes['H']}`, `V Best-{selected_sizes['V']}`\n"
        f"- elapsed seconds: `{time.time() - started:.1f}`\n",
    )
    verification = verify_outputs(out)
    if verification["status"] != "pass":
        raise ValueError(
            f"Best-N all-training control failed verification with {verification['issue_count']} issues"
        )
    return verification
