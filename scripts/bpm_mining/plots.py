"""Plot and artifact generation for best-BPM mining."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import bpm_dgx_poster as poster

from .evolution import combine_spectra
from .identity import manifest_by_index, normalize_subset_row, subset_indices
from .io import atomic_write_text, ensure_dir, read_csv, write_csv
from .progress import write_parent_status, write_shard_status


MEMBERSHIP_FIELDS = [
    "collection",
    "spill_id",
    "plane",
    "subset_size",
    "subset_mask",
    "bpm_indices",
    "bpm_members",
    "bpm_source_keys",
    "bpm_digitizers",
    "subset_score",
    "q_hat",
]
POSTER_FIELDS = ["collection", "spill_id", "plane", "category", "score", "reason", "caption", "recommended_files"]


def _matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:
        return None


def _f(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def subset_rows(inputs: Path) -> list[dict[str, str]]:
    rows = []
    for size in (1, 3, 5, 10):
        path = inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv"
        if path.exists():
            rows.extend(read_csv(path))
    return rows


def cache_lookup(inputs: Path, spectral_config: str) -> dict[tuple[str, str, str], dict[str, str]]:
    path = inputs / "cache" / "index" / "spectral_cache.csv"
    if not path.exists():
        return {}
    return {
        (row["collection"], row["spill_id"], row["plane"]): row
        for row in read_csv(path)
        if row.get("status") == "ok" and row.get("spectral_config") == spectral_config
    }


def consensus_lookup(inputs: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    path = inputs / "consensus" / "spill_consensus_summary.csv"
    if not path.exists():
        return {}
    return {(row["collection"], row["spill_id"], row["plane"]): row for row in read_csv(path)}


def _members(row: dict[str, str]) -> list[str]:
    return [item for item in row.get("bpm_members", "").split(",") if item]


def _positions(
    row: dict[str, str],
    pos_by_index: dict[int, int],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
) -> list[int]:
    return [pos_by_index[idx] for idx in subset_indices(row, row["plane"], meta_by_index) if idx in pos_by_index]


def _artifact_score(row: dict[str, str]) -> float:
    return _f(row.get("score"))


def _poster_priority(row: dict[str, str]) -> tuple[int, float, str, str, str]:
    category = row.get("category", "")
    plane = row.get("plane", "")
    priority = {
        ("V", "best5_improvement"): 0,
        ("V", "best3_improvement"): 1,
        ("V", "best1"): 2,
        ("H", "best5_improvement"): 3,
        ("H", "best3_improvement"): 4,
        ("H", "best1"): 5,
    }.get((plane, category), 6)
    return (priority, -(_artifact_score(row) if math.isfinite(_artifact_score(row)) else -math.inf), row.get("collection", ""), row.get("spill_id", ""), plane)


def _poster_caption(row: dict[str, str]) -> str:
    plane = row.get("plane", "")
    category = row.get("category", "selected")
    score = row.get("score", "")
    spill = row.get("spill_id", "")
    return f"{plane} {category} example for {spill}; selection score {score}. Cached spectra show BPM-vs-tune structure, subset overlays, and visible-window tune evolution where reliable."


def _select_poster_rows(rows: list[dict[str, str]], max_examples: int) -> list[dict[str, str]]:
    if max_examples <= 0:
        return []
    ranked: list[dict[str, str]] = []
    ranked_keys: set[tuple[str, str, str]] = set()
    for row in sorted(rows, key=_poster_priority):
        key = (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
        if key not in ranked_keys:
            ranked.append(row)
            ranked_keys.add(key)
    if max_examples == 1:
        return ranked[:1]
    selected: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    minimum_per_plane = min(2, max(1, max_examples // 4))

    def add(row: dict[str, str]) -> None:
        key = (row.get("collection", ""), row.get("spill_id", ""), row.get("plane", ""))
        if key not in seen and len(selected) < max_examples:
            selected.append(row)
            seen.add(key)

    for plane in ("V", "H"):
        for row in [candidate for candidate in ranked if candidate.get("plane") == plane][:minimum_per_plane]:
            add(row)
    represented = {(row.get("plane", ""), row.get("category", "")) for row in selected}
    for row in ranked:
        category_key = (row.get("plane", ""), row.get("category", ""))
        if category_key not in represented:
            add(row)
            represented.add(category_key)
        if len(selected) >= max_examples:
            return selected
    for row in ranked:
        add(row)
        if len(selected) >= max_examples:
            return selected
    return selected


def _poster_file_list(stem: str) -> list[str]:
    return [
        f"{stem}_bpm_tune_deconstruction_poster.png",
        f"{stem}_subset_spectra_overlay_poster.png",
        f"{stem}_visible_window_tune_evolution_poster.png",
    ]


def _spill_stem(spill_id: str, plane: str) -> str:
    token = spill_id if spill_id.startswith("spill_") else f"spill_{spill_id}"
    return f"{token}_{plane.lower()}"


def _finite(values: list[float]) -> list[float]:
    return [value for value in values if math.isfinite(value)]


def _mean(values: list[float]) -> float:
    vals = _finite(values)
    return sum(vals) / len(vals) if vals else math.nan


def save_bar(path: Path, title: str, labels: list[str], values: list[float], ylabel: str) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title + "\n" + "\n".join(f"{a},{b}" for a, b in zip(labels, values)))
        return
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(range(len(labels)), values, color="#2f6f9f")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=70, ha="right", fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_global_topn_poster(path: Path, inputs: Path, rows: list[dict[str, str]]) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    pareto = read_csv(inputs / "statistics" / "subset_size_pareto.csv") if (inputs / "statistics" / "subset_size_pareto.csv").exists() else []
    if pareto:
        by_plane = {
            plane: sorted(
                [row for row in pareto if row.get("plane") == plane],
                key=lambda row: int(float(row.get("subset_size") or 0)),
            )
            for plane in ("H", "V")
        }
    else:
        by_plane = {}
        for plane in ("H", "V"):
            plane_rows = [row for row in rows if row.get("plane") == plane]
            sizes = sorted({int(float(row.get("subset_size") or 0)) for row in plane_rows})
            by_plane[plane] = [
                {
                    "subset_size": str(size),
                    "median_score": str(_mean([_f(row.get("subset_score")) for row in plane_rows if row.get("subset_size") == str(size)])),
                    "median_visible_fraction": str(_mean([_f(row.get("visible_fraction")) for row in plane_rows if row.get("subset_size") == str(size)])),
                }
                for size in sizes
            ]
    if plt is None:
        lines = ["Top-N poster performance"]
        for plane, plane_rows in by_plane.items():
            for row in plane_rows:
                lines.append(f"{plane},{row.get('subset_size','')},{row.get('median_score','')},{row.get('median_visible_fraction','')}")
        atomic_write_text(path.with_suffix(".txt"), "\n".join(lines) + "\n")
        return
    fig, (score_ax, visible_ax) = plt.subplots(2, 1, figsize=(8.5, 7), sharex=True)
    colors = {"H": "#1f77b4", "V": "#d62728"}
    for plane, plane_rows in by_plane.items():
        xs = [int(float(row.get("subset_size") or 0)) for row in plane_rows]
        scores = [_f(row.get("median_score")) for row in plane_rows]
        visible = [_f(row.get("median_visible_fraction")) for row in plane_rows]
        if xs:
            score_ax.plot(xs, scores, marker="o", linewidth=1.8, color=colors[plane], label=f"{plane} score")
            visible_ax.plot(xs, visible, marker="s", linewidth=1.8, color=colors[plane], label=f"{plane} visible fraction")
    score_ax.set_title("Subset-size performance for poster review")
    score_ax.set_ylabel("median subset score")
    score_ax.grid(alpha=0.25)
    score_ax.legend(fontsize=8)
    visible_ax.set_xlabel("subset size")
    visible_ax.set_ylabel("median visible fraction")
    visible_ax.grid(alpha=0.25)
    visible_ax.legend(fontsize=8)
    visible_ax.set_xticks(sorted({int(float(row.get("subset_size") or 0)) for rows_for_plane in by_plane.values() for row in rows_for_plane}))
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def _save_bpm_inclusion_poster(path: Path, inputs: Path, plane: str, rows: list[dict[str, str]]) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    stats = read_csv(inputs / "statistics" / "bpm_global_statistics.csv") if (inputs / "statistics" / "bpm_global_statistics.csv").exists() else []
    plane_stats = [row for row in stats if row.get("plane") == plane]
    if not plane_stats:
        counts: dict[str, dict[str, float]] = defaultdict(lambda: {"top1": 0.0, "top3": 0.0, "top5": 0.0})
        totals = Counter(row.get("subset_size") for row in rows if row.get("plane") == plane)
        for row in rows:
            if row.get("plane") != plane:
                continue
            size = row.get("subset_size", "")
            field = f"top{size}" if size in {"1", "3", "5"} else ""
            if not field:
                continue
            for member in row.get("bpm_members", "").split(","):
                if member:
                    counts[member][field] += 1.0 / max(1, totals[size])
        plane_stats = [
            {
                "bpm_name": name,
                "top1_frequency": values["top1"],
                "top3_inclusion_frequency": values["top3"],
                "top5_inclusion_frequency": values["top5"],
            }
            for name, values in counts.items()
        ]
    ranked = sorted(
        plane_stats,
        key=lambda row: sum(
            value if math.isfinite(value) else 0.0
            for value in (_f(row.get(field)) for field in ("top1_frequency", "top3_inclusion_frequency", "top5_inclusion_frequency"))
        ),
        reverse=True,
    )[:24]
    labels = [row.get("bpm_name", "") for row in ranked]
    series = [
        ("top1", [value if math.isfinite(value := _f(row.get("top1_frequency"))) else 0.0 for row in ranked], "#2f6f9f"),
        ("top3", [value if math.isfinite(value := _f(row.get("top3_inclusion_frequency"))) else 0.0 for row in ranked], "#7aa974"),
        ("top5", [value if math.isfinite(value := _f(row.get("top5_inclusion_frequency"))) else 0.0 for row in ranked], "#f2b134"),
    ]
    if plt is None:
        lines = [f"{plane} BPM inclusion"]
        for idx, label in enumerate(labels):
            lines.append(",".join([label] + [str(values[idx]) for _, values, _ in series]))
        atomic_write_text(path.with_suffix(".txt"), "\n".join(lines) + "\n")
        return
    fig, ax = plt.subplots(figsize=(12, 5.5))
    x = np.arange(len(labels))
    width = 0.26
    for offset, (label, values, color) in zip((-width, 0.0, width), series):
        ax.bar(x + offset, values, width=width, label=label, color=color)
    ax.set_title(f"{plane} BPM inclusion frequency")
    ax.set_ylabel("selection frequency")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=70, ha="right", fontsize=7)
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def save_scatter(path: Path, title: str, points: list[tuple[float, float, str]], xlabel: str, ylabel: str) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None:
        atomic_write_text(path.with_suffix(".txt"), title + "\n" + "\n".join(f"{x},{y},{label}" for x, y, label in points))
        return
    fig, ax = plt.subplots(figsize=(6, 4))
    for x, y, label in points:
        ax.scatter([x], [y], label=label)
        ax.text(x, y, label, fontsize=8)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _save_deconstruction(
    path: Path,
    title: str,
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    bpm_indices: np.ndarray,
    selected_positions: list[int],
    q_hat: float,
    consensus_tune: float,
) -> None:
    ensure_dir(path.parent)
    image = np.median(np.asarray(spectra, dtype=np.float32), axis=1)
    log_image = np.log10(image + 1e-24)
    med = np.median(log_image, axis=1, keepdims=True)
    mad = np.median(np.abs(log_image - med), axis=1, keepdims=True) * 1.4826
    norm = (log_image - med) / np.maximum(mad, 1e-9)
    order = np.argsort(np.asarray(tune_axis, dtype=np.float64))
    ordered_axis = np.asarray(tune_axis, dtype=np.float64)[order]
    norm = norm[:, order]
    target_columns = min(480, norm.shape[1])
    edges = np.linspace(0, norm.shape[1], target_columns + 1, dtype=int)
    reduced = np.stack(
        [np.max(norm[:, edges[index] : max(edges[index] + 1, edges[index + 1])], axis=1) for index in range(target_columns)],
        axis=1,
    )
    reduced_axis = np.asarray(
        [np.mean(ordered_axis[edges[index] : max(edges[index] + 1, edges[index + 1])]) for index in range(target_columns)],
        dtype=np.float64,
    )
    width, height = 1400, 900
    pixels = poster.new_canvas(width, height)
    x0, y0, x1, y1 = poster.draw_axes(pixels, width, height, title.upper(), "TUNE", "BPM ORDER")
    for row_index in range(reduced.shape[0]):
        cy0 = y0 + int(row_index * (y1 - y0 + 1) / reduced.shape[0])
        cy1 = y0 + int((row_index + 1) * (y1 - y0 + 1) / reduced.shape[0]) - 1
        for column in range(reduced.shape[1]):
            cx0 = x0 + int(column * (x1 - x0 + 1) / reduced.shape[1])
            cx1 = x0 + int((column + 1) * (x1 - x0 + 1) / reduced.shape[1]) - 1
            value = float(reduced[row_index, column])
            color = poster.tune_color(value if math.isfinite(value) else None, -1.0, 8.0)
            poster.rect(pixels, width, height, cx0, cy0, max(cx0, cx1), max(cy0, cy1), color)
    for pos in selected_positions:
        y = y0 + int((pos + 0.5) * (y1 - y0 + 1) / max(1, reduced.shape[0]))
        poster.line(pixels, width, height, x0, y, x1, y, (255, 255, 255))
    tune_min = float(reduced_axis[0])
    tune_max = float(reduced_axis[-1])
    if math.isfinite(q_hat) and tune_min <= q_hat <= tune_max:
        x = poster.scale_value(q_hat, tune_min, tune_max, x0, x1)
        poster.line(pixels, width, height, x, y0, x, y1, poster.ORANGE)
        poster.line(pixels, width, height, x + 1, y0, x + 1, y1, poster.ORANGE)
    if math.isfinite(consensus_tune) and tune_min <= consensus_tune <= tune_max:
        x = poster.scale_value(consensus_tune, tune_min, tune_max, x0, x1)
        for y in range(y0, y1 + 1, 8):
            poster.line(pixels, width, height, x, y, x, min(y + 4, y1), poster.RED)
    poster.draw_numeric_axis_labels(
        pixels,
        width,
        height,
        (x0, y0, x1, y1),
        (tune_min, tune_max),
        (0.0, float(max(1, reduced.shape[0] - 1))),
    )
    poster.draw_text(pixels, width, height, x0, y0 - 28, "COLOR: ROW-NORMALIZED LOG POWER", poster.MUTED, 2)
    poster.draw_text(pixels, width, height, x1 - 360, y0 - 28, "WHITE: SELECTED  ORANGE: SUBSET Q  RED: CONSENSUS", poster.MUTED, 2)
    poster.write_png(path, width, height, pixels)
    atomic_write_text(
        path.with_name(f"{path.stem}_caption.md"),
        "# BPM-Tune Deconstruction\n\nEach row is one exact BPM channel; color is median-over-window log power normalized by that row's robust background. White rows are the selected adaptive members. Orange is the subset tune candidate and dashed red is the within-spill consensus. The consensus is internal evidence, not external tune truth.\n",
    )


def _combined_spectrum(spectra: np.ndarray, positions: list[int], aggregator: str) -> np.ndarray | None:
    if not positions:
        return None
    return combine_spectra(np.asarray(spectra[positions], dtype=np.float32), aggregator)


def _save_subset_overlay(
    path: Path,
    title: str,
    spectra: np.ndarray,
    tune_axis: np.ndarray,
    rows_by_size: dict[str, dict[str, str]],
    pos_by_index: dict[int, int],
    meta_by_index: dict[tuple[str, int], dict[str, str]],
    q_hat: float,
    consensus_tune: float,
) -> None:
    ensure_dir(path.parent)
    curves: list[tuple[str, np.ndarray]] = []
    for size in ("1", "3", "5"):
        row = rows_by_size.get(size)
        if not row:
            continue
        combined = _combined_spectrum(spectra, _positions(row, pos_by_index, meta_by_index), "mean_power")
        if combined is not None:
            curves.append((f"best{size}", np.median(combined, axis=0)))
    all_power = np.asarray(spectra, dtype=np.float32)
    curves.append(("all mean", np.median(combine_spectra(all_power, "mean_power"), axis=0)))
    curves.append(("all median", np.median(combine_spectra(all_power, "median_power"), axis=0)))
    order = np.argsort(np.asarray(tune_axis, dtype=np.float64))
    ordered_axis = np.asarray(tune_axis, dtype=np.float64)[order]
    colors = (poster.BLUE, poster.GREEN, poster.ORANGE, poster.PURPLE, poster.RED)
    series: list[tuple[str, list[tuple[float, float]], tuple[int, int, int]]] = []
    all_y: list[float] = []
    for index, (label, power) in enumerate(curves):
        y = np.log10(power + 1e-24)
        y = y - np.nanmedian(y)
        ordered_y = np.asarray(y, dtype=np.float64)[order]
        points = [(float(x), float(value)) for x, value in zip(ordered_axis, ordered_y)]
        series.append((label.upper(), points, colors[index % len(colors)]))
        all_y.extend(value for _x, value in points if math.isfinite(value))
    if all_y:
        ymin, ymax = min(all_y), max(all_y)
        if math.isfinite(q_hat):
            series.append(("SUBSET Q", [(q_hat, ymin), (q_hat, ymax)], poster.INK))
        if math.isfinite(consensus_tune):
            series.append(("CONSENSUS", [(consensus_tune, ymin), (consensus_tune, ymax)], poster.RED))
    poster.line_plot(
        path,
        title.upper(),
        series,
        "TUNE",
        "CENTERED LOG POWER",
    )
    atomic_write_text(
        path.with_name(f"{path.stem}_caption.md"),
        "# Subset Spectral Overlay\n\nMedian-over-window spectra compare adaptive Best-1/3/5 with all-BPM mean and median controls after centering each curve in log power. Vertical references mark the selected subset candidate and internal consensus. Relative contrast, not absolute vertical scale, is the intended comparison.\n",
    )


def _save_visible_evolution(path: Path, title: str, combined: np.ndarray, tune_axis: np.ndarray, centers: np.ndarray) -> None:
    ensure_dir(path.parent)
    log_power = np.log10(np.asarray(combined, dtype=np.float64) + 1e-24)
    peak_idx = np.argmax(log_power, axis=1)
    peak_log = log_power[np.arange(log_power.shape[0]), peak_idx]
    med = np.median(log_power, axis=1)
    mad = np.median(np.abs(log_power - med[:, None]), axis=1) * 1.4826
    prom = (peak_log - med) / np.maximum(mad, 1e-9)
    tunes = np.asarray(tune_axis[peak_idx], dtype=np.float64)
    visible = prom >= 4.0
    poster.line_plot(
        path,
        title.upper(),
        [
            ("VISIBLE PROM>=4", list(zip(centers[visible], tunes[visible])), poster.GREEN),
            ("WEAK", list(zip(centers[~visible], tunes[~visible])), poster.MUTED),
        ],
        "CENTER TURN",
        "TUNE",
        (float(np.min(tune_axis)), float(np.max(tune_axis))),
    )
    atomic_write_text(
        path.with_name(f"{path.stem}_caption.md"),
        "# Visible-Window Tune Evolution\n\nGreen windows have robust peak prominence at least 4.0; gray windows are shown for diagnostic context but are not accepted as reliable tune candidates. Lines connect sampled window centers and do not imply a continuous physical trajectory.\n",
    )


def _write_poster_contact_sheet(path: Path, image_paths: list[Path]) -> None:
    plt = _matplotlib()
    ensure_dir(path.parent)
    if plt is None or not image_paths:
        atomic_write_text(path.with_suffix(".txt"), "\n".join(str(item) for item in image_paths) + "\n")
        return
    images = []
    for item in image_paths[:8]:
        try:
            images.append((item, plt.imread(item)))
        except Exception:
            continue
    if not images:
        atomic_write_text(path.with_suffix(".txt"), "\n".join(str(item) for item in image_paths) + "\n")
        return
    cols = min(2, len(images))
    rows = int(math.ceil(len(images) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 6, rows * 4))
    flat_axes = np.asarray(axes).reshape(-1)
    for ax, (item, image) in zip(flat_axes, images):
        ax.imshow(image)
        ax.set_title(item.stem.replace("_", " "), fontsize=8)
        ax.axis("off")
    for ax in flat_axes[len(images) :]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def make_artifacts(cfg: dict[str, object], inputs: Path, artifact_manifest: Path, out: Path, workers: int | None = None, limit: int = 0) -> None:
    ensure_dir(out / "global")
    ensure_dir(out / "spills")
    ensure_dir(out / "poster")
    meta_by_index = manifest_by_index(read_csv(inputs / "manifest" / "bpm_index.csv"))
    rows = [normalize_subset_row(row, meta_by_index) for row in subset_rows(inputs)]
    artifact_rows = read_csv(artifact_manifest) if artifact_manifest.exists() else []
    if limit:
        artifact_rows = artifact_rows[:limit]
    artifact_cfg = cfg.get("artifacts", {}) if isinstance(cfg.get("artifacts"), dict) else {}
    poster_max_examples = max(0, int(artifact_cfg.get("poster_max_examples", 8)))
    poster_rows = _select_poster_rows(artifact_rows, poster_max_examples)
    poster_keys = {(row["collection"], row["spill_id"], row["plane"]) for row in poster_rows}
    spectral_config = str(cfg.get("subset_search", {}).get("search_spectral_config", "early_4096_256") if isinstance(cfg.get("subset_search"), dict) else "early_4096_256")
    caches = cache_lookup(inputs, spectral_config)
    consensus = consensus_lookup(inputs)
    _save_global_topn_poster(out / "poster" / "global_topn_performance_hv.png", inputs, rows)
    for poster_plane in ("H", "V"):
        _save_bpm_inclusion_poster(out / "poster" / f"global_bpm_inclusion_{poster_plane.lower()}.png", inputs, poster_plane, rows)
    for plane in ("H", "V"):
        top1 = [row for row in rows if row["plane"] == plane and row["subset_size"] == "1"]
        counts = Counter(member for row in top1 for member in row.get("bpm_members", "").split(",") if member)
        labels = [name for name, _ in counts.most_common(30)]
        values = [counts[name] for name in labels]
        save_bar(out / "global" / f"bpm_top1_frequency_{plane.lower()}.png", f"{plane} top-1 BPM frequency", labels, values, "count")
        topk = Counter(member for row in rows if row["plane"] == plane for member in row.get("bpm_members", "").split(",") if member)
        labels = [name for name, _ in topk.most_common(30)]
        values = [topk[name] for name in labels]
        save_bar(out / "global" / f"bpm_topk_inclusion_{plane.lower()}.png", f"{plane} top-k BPM inclusion", labels, values, "count")
        pareto_rows = [
            row
            for row in read_csv(inputs / "statistics" / "subset_size_pareto.csv")
            if row.get("plane") == plane
        ]
        pareto_rows.sort(key=lambda row: int(float(row.get("subset_size") or 0)))
        performance_points = [
            (
                _f(row.get("subset_size")),
                _f(row.get("median_score")),
                f"N={row.get('subset_size', '')}",
            )
            for row in pareto_rows
        ]
        compute_points = [
            (
                _f(row.get("compute_cost")),
                _f(row.get("median_score")),
                f"N={row.get('subset_size', '')}",
            )
            for row in pareto_rows
        ]
        save_scatter(
            out / "global" / f"topn_performance_curve_{plane.lower()}.png",
            f"{plane} subset-size performance",
            performance_points,
            "subset size",
            "median subset score",
        )
        save_scatter(
            out / "global" / f"subset_size_pareto_{plane.lower()}.png",
            f"{plane} subset-size Pareto",
            compute_points,
            "relative compute cost",
            "median subset score",
        )
        rank_rows = [
            row
            for row in read_csv(inputs / "statistics" / "bpm_rank_stability.csv")
            if row.get("plane") == plane
        ]
        save_bar(
            out / "global" / f"bpm_rank_stability_{plane.lower()}.png",
            f"{plane} collection-to-collection rank stability",
            [row.get("metric", "") for row in rank_rows],
            [_f(row.get("value")) for row in rank_rows],
            "association",
        )
        pair_rows = [
            row
            for row in read_csv(inputs / "statistics" / "bpm_pair_synergy.csv")
            if row.get("plane") == plane
        ]
        pair_rows.sort(
            key=lambda row: (int(float(row.get("pair_count") or 0)), _f(row.get("median_pair_score"))),
            reverse=True,
        )
        pair_rows = pair_rows[:24]
        pair_labels = [f"{row.get('bpm_a', '')}+{row.get('bpm_b', '')}" for row in pair_rows]
        save_bar(
            out / "global" / f"bpm_pair_synergy_{plane.lower()}.png",
            f"{plane} descriptive pair co-selection score",
            pair_labels,
            [_f(row.get("median_pair_score")) for row in pair_rows],
            "median subset score",
        )
        save_scatter(
            out / "global" / f"bpm_coselection_network_{plane.lower()}.png",
            f"{plane} pair frequency versus score",
            [
                (
                    _f(row.get("pair_count")),
                    _f(row.get("median_pair_score")),
                    f"{row.get('bpm_a', '')}+{row.get('bpm_b', '')}",
                )
                for row in pair_rows
            ],
            "co-selection count",
            "median subset score",
        )
        global_stats = [
            row
            for row in read_csv(inputs / "statistics" / "bpm_global_statistics.csv")
            if row.get("plane") == plane
        ]
        duration_rows = sorted(
            global_stats,
            key=lambda row: _f(row.get("median_visibility_duration")),
            reverse=True,
        )[:24]
        save_bar(
            out / "global" / f"visibility_duration_by_bpm_{plane.lower()}.png",
            f"{plane} selected-BPM visibility duration",
            [row.get("bpm_name", "") for row in duration_rows],
            [_f(row.get("median_visibility_duration")) for row in duration_rows],
            "turns",
        )
        ring_rows = sorted(global_stats, key=lambda row: int(float(row.get("ring_order") or 0)))
        save_scatter(
            out / "global" / f"bpm_ring_quality_map_{plane.lower()}.png",
            f"{plane} ring-order selection quality",
            [
                (
                    _f(row.get("ring_order")),
                    _f(row.get("median_score")),
                    row.get("bpm_name", ""),
                )
                for row in ring_rows
            ],
            "BPM ring token",
            "median selected score",
        )
    cluster_summary = read_csv(inputs / "clustering" / "cluster_summary.csv")
    save_scatter(
        out / "global" / "cluster_method_performance.png",
        "Spill-cluster H/V score structure",
        [
            (
                _f(row.get("median_score_h")),
                _f(row.get("median_score_v")),
                row.get("cluster_id", ""),
            )
            for row in cluster_summary
        ],
        "median H score",
        "median V score",
    )
    save_bar(
        out / "global" / "cluster_spill_counts.png",
        "Spill-cluster populations",
        [row.get("cluster_id", "") for row in cluster_summary],
        [_f(row.get("spill_count")) for row in cluster_summary],
        "spills",
    )
    membership_rows = []
    selected_keys = {(row["collection"], row["spill_id"], row["plane"]) for row in artifact_rows}
    rows_by_key_size: dict[tuple[str, str, str], dict[str, dict[str, str]]] = defaultdict(dict)
    for row in rows:
        key = (row["collection"], row["spill_id"], row["plane"])
        if key not in selected_keys:
            continue
        prior = rows_by_key_size[key].get(row["subset_size"])
        if prior is None or _f(row.get("subset_score")) > _f(prior.get("subset_score")):
            rows_by_key_size[key][row["subset_size"]] = row
        membership_rows.append(
            {
                "collection": row["collection"],
                "spill_id": row["spill_id"],
                "plane": row["plane"],
                "subset_size": row["subset_size"],
                "subset_mask": row.get("subset_mask", ""),
                "bpm_indices": row.get("bpm_indices", ""),
                "bpm_members": row["bpm_members"],
                "bpm_source_keys": row.get("bpm_source_keys", ""),
                "bpm_digitizers": row.get("bpm_digitizers", ""),
                "subset_score": row["subset_score"],
                "q_hat": row["q_hat"],
            }
        )
    progress_dir = out / "progress"
    started = time.time()
    keys = sorted(rows_by_key_size)
    write_parent_status(progress_dir, "running", 0, len(keys), 0, len(keys), 0, started)
    output_files = 0
    poster_output_files: list[Path] = []
    for idx, key in enumerate(keys):
        shard_started = time.time()
        write_shard_status(progress_dir, idx, len(keys), "running", 0, 1, 0, shard_started)
        cache = caches.get(key)
        if cache:
            spectra = np.load(cache["spectra_path"], mmap_mode="r")
            tune_axis = np.load(cache["tune_axis_path"])
            centers = np.load(cache["window_centers_path"])
            bpm_indices = np.load(cache["bpm_indices_path"])
            pos_by_index = {int(bpm_idx): pos for pos, bpm_idx in enumerate(bpm_indices)}
            by_size = rows_by_key_size[key]
            chosen = by_size.get("5") or by_size.get("3") or by_size.get("1") or next(iter(by_size.values()))
            selected_positions = _positions(chosen, pos_by_index, meta_by_index)
            c_tune = _f(consensus.get(key, {}).get("dominant_consensus_tune"))
            q_hat = _f(chosen.get("q_hat"))
            stem = _spill_stem(key[1], key[2])
            _save_deconstruction(
                out / "spills" / f"{stem}_bpm_tune_deconstruction.png",
                f"{stem} BPM tune deconstruction",
                spectra,
                tune_axis,
                bpm_indices,
                selected_positions,
                q_hat,
                c_tune,
            )
            _save_subset_overlay(
                out / "spills" / f"{stem}_subset_spectra_overlay.png",
                f"{stem} subset spectra overlay",
                spectra,
                tune_axis,
                by_size,
                pos_by_index,
                meta_by_index,
                q_hat,
                c_tune,
            )
            combined = _combined_spectrum(spectra, selected_positions, "mean_power")
            if combined is not None:
                _save_visible_evolution(out / "spills" / f"{stem}_visible_window_tune_evolution.png", f"{stem} visible tune evolution", combined, tune_axis, centers)
            output_files += 3
            if key in poster_keys:
                poster_deconstruction = out / "poster" / f"{stem}_bpm_tune_deconstruction_poster.png"
                poster_overlay = out / "poster" / f"{stem}_subset_spectra_overlay_poster.png"
                poster_evolution = out / "poster" / f"{stem}_visible_window_tune_evolution_poster.png"
                _save_deconstruction(
                    poster_deconstruction,
                    f"{stem} poster BPM tune deconstruction",
                    spectra,
                    tune_axis,
                    bpm_indices,
                    selected_positions,
                    q_hat,
                    c_tune,
                )
                _save_subset_overlay(
                    poster_overlay,
                    f"{stem} poster subset spectra overlay",
                    spectra,
                    tune_axis,
                    by_size,
                    pos_by_index,
                    meta_by_index,
                    q_hat,
                    c_tune,
                )
                if combined is not None:
                    _save_visible_evolution(poster_evolution, f"{stem} poster visible tune evolution", combined, tune_axis, centers)
                poster_output_files.extend([poster_deconstruction, poster_overlay, poster_evolution])
        write_shard_status(progress_dir, idx, len(keys), "complete", 1, 1, output_files, shard_started)
        write_parent_status(progress_dir, "running", idx + 1, len(keys), idx + 1, len(keys), output_files, started)
    write_csv(out / "spills" / "selected_subset_membership.csv", membership_rows, MEMBERSHIP_FIELDS)
    for row in membership_rows:
        write_csv(out / "spills" / f"{_spill_stem(row['spill_id'], row['plane'])}_subset_membership.csv", [row], MEMBERSHIP_FIELDS)
    poster_manifest_rows = []
    for row in poster_rows:
        stem = _spill_stem(row["spill_id"], row["plane"])
        poster_manifest_rows.append(
            {
                "collection": row["collection"],
                "spill_id": row["spill_id"],
                "plane": row["plane"],
                "category": row.get("category", ""),
                "score": row.get("score", ""),
                "reason": row.get("reason", ""),
                "caption": _poster_caption(row),
                "recommended_files": ";".join(_poster_file_list(stem)),
            }
        )
    write_csv(out / "poster" / "selected_poster_artifacts.csv", poster_manifest_rows, POSTER_FIELDS)
    index_lines = [
        "# Poster Artifact Index",
        "",
        f"- selected poster examples: `{len(poster_manifest_rows)}`",
        f"- poster example cap: `{poster_max_examples}`",
        "- poster plots are cache-backed review candidates, not external tune truth.",
        "",
        "| Spill | Plane | Category | Score | Caption | Files |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for row in poster_manifest_rows:
        index_lines.append(
            f"| `{row['spill_id']}` | `{row['plane']}` | `{row['category']}` | {row['score']} | {row['caption']} | `{row['recommended_files']}` |"
        )
    atomic_write_text(out / "poster" / "poster_artifact_index.md", "\n".join(index_lines) + "\n")
    _write_poster_contact_sheet(out / "poster" / "poster_contact_sheet.png", poster_output_files[::3])
    atomic_write_text(
        out / "artifact_generation_summary.md",
        "# Artifact Generation Summary\n\n"
        f"- selected membership rows: `{len(membership_rows)}`\n"
        f"- selected spill-plane keys: `{len(keys)}`\n"
        f"- cache-backed artifact files: `{output_files}`\n"
        f"- curated poster examples: `{len(poster_manifest_rows)}`\n",
    )
    atomic_write_text(
        out / "global" / "poster_artifact_index.md",
        "# Poster Artifact Index\n\n"
        f"- selected spill-plane keys: `{len(keys)}`\n"
        "- per-spill heatmaps use row-normalized log spectral power from cached spectra.\n"
        "- subset overlays compare best1, best3, best5, all-BPM mean, and all-BPM median where available.\n",
    )
    write_parent_status(progress_dir, "complete", len(keys), len(keys), len(keys), len(keys), output_files, started)
