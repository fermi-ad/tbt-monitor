#!/usr/bin/env python3
"""Build elite full-data autosweep inputs from a ranked pilot run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

import run_autosweep


POSTER_UNSAFE_LABELS = {"TOO_SLOW", "UNSTABLE_H", "UNSTABLE_V", "OVERFITS_BAND"}
REJECTED_LABELS = POSTER_UNSAFE_LABELS | {"REJECTED"}

CONFIG_EXTRA_FIELDS = ["effective_config_hash", "selection_roles"]
CONFIG_OUT_FIELDS = run_autosweep.CONFIG_FIELDS + CONFIG_EXTRA_FIELDS

SOURCE_FIELDS = [
    "plane",
    "selection_role",
    "config_hash",
    "effective_config_hash",
    "config_label",
    "collection_view",
    "physics_score",
    "poster_score",
    "bpm_robustness_score",
    "bpm_combination",
    "config_name",
    "status",
]

REJECTED_DIAGNOSTIC_FIELDS = [
    "config_hash",
    "effective_config_hash",
    "collection_view",
    "plane",
    "config_label",
    "rejection_reason",
    "overall_score",
    "poster_score",
    "physics_score",
    "bpm_robustness_score",
    "bpm_combination",
    "config_name",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        out = float(text) if text else default
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def effective_config_payload(row: dict[str, str]) -> dict[str, object]:
    payload = {field: row.get(field, "") for field in run_autosweep.CONFIG_FIELDS if field != "config_hash"}
    payload.pop("stage", None)
    payload.pop("config_name", None)
    if payload.get("spectrogram_method") == "hann":
        payload["multitaper_nw"] = ""
        payload["multitaper_k"] = ""
    return payload


def effective_config_hash(row: dict[str, str]) -> str:
    payload = json.dumps(effective_config_payload(row), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def annotate_effective(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for row in rows:
        annotated = dict(row)
        annotated["effective_config_hash"] = effective_config_hash(row)
        out.append(annotated)
    return out


def poster_safe(row: dict[str, str]) -> bool:
    return row.get("config_label", "") not in POSTER_UNSAFE_LABELS and row.get("config_label", "") != "REJECTED"


def combined_candidates(rows: list[dict[str, str]], plane: str | None = None, safe: bool = True) -> list[dict[str, str]]:
    candidates = [row for row in rows if row.get("collection_view") == "combined"]
    if not candidates:
        candidates = list(rows)
    if plane is not None:
        candidates = [row for row in candidates if row.get("plane") == plane]
    if safe:
        safe_rows = [row for row in candidates if poster_safe(row)]
        if safe_rows:
            candidates = safe_rows
    return candidates


def sorted_by(rows: Iterable[dict[str, str]], fields: Sequence[str]) -> list[dict[str, str]]:
    def key(row: dict[str, str]) -> tuple[float, ...]:
        return tuple(parse_float(row.get(field)) for field in fields)

    return sorted(rows, key=key, reverse=True)


def first_or_none(rows: Sequence[dict[str, str]]) -> dict[str, str] | None:
    return rows[0] if rows else None


def find_baseline(rows: list[dict[str, str]], plane: str) -> dict[str, str] | None:
    candidates = combined_candidates(rows, plane, safe=False)
    exact = [
        row
        for row in candidates
        if row.get("config_name") == "hann_2048_256_mean_medium"
        and row.get("bpm_combination") == "mean"
        and row.get("window") == "2048"
        and row.get("stride") == "256"
        and row.get("spectrogram_method") == "hann"
    ]
    return first_or_none(sorted_by(exact, ["physics_score"]))


def select_for_plane(rows: list[dict[str, str]], plane: str) -> list[tuple[str, dict[str, str] | None]]:
    candidates = combined_candidates(rows, plane, safe=False)
    selections: list[tuple[str, dict[str, str] | None]] = []
    selections.append(("top_physics", first_or_none(sorted_by(candidates, ["physics_score"]))))
    robust = [row for row in candidates if row.get("bpm_combination") == "top10_by_confidence"]
    selections.append(("top10_robust", first_or_none(sorted_by(robust, ["bpm_robustness_score", "physics_score"]))))
    median_rows = [row for row in candidates if row.get("bpm_combination") in {"median", "trimmed_mean_10pct"}]
    selections.append(("median_or_trimmed", first_or_none(sorted_by(median_rows, ["physics_score"]))))
    selections.append(("baseline_mean", find_baseline(rows, plane)))
    return selections


def select_poster(rows: list[dict[str, str]]) -> tuple[str, dict[str, str] | None]:
    candidates = combined_candidates(rows, None, safe=True)
    return "poster_best", first_or_none(sorted_by(candidates, ["poster_score", "physics_score"]))


def merge_selected(selections: list[tuple[str, str, dict[str, str] | None]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_effective: dict[str, dict[str, object]] = {}
    source_rows: list[dict[str, object]] = []
    for plane, role, row in selections:
        if row is None:
            source_rows.append({"plane": plane, "selection_role": role, "status": "missing"})
            continue
        effective = row["effective_config_hash"]
        source_rows.append(
            {
                "plane": plane,
                "selection_role": role,
                "config_hash": row.get("config_hash", ""),
                "effective_config_hash": effective,
                "config_label": row.get("config_label", ""),
                "collection_view": row.get("collection_view", ""),
                "physics_score": row.get("physics_score", ""),
                "poster_score": row.get("poster_score", ""),
                "bpm_robustness_score": row.get("bpm_robustness_score", ""),
                "bpm_combination": row.get("bpm_combination", ""),
                "config_name": row.get("config_name", ""),
                "status": "selected",
            }
        )
        if effective not in by_effective:
            out = {field: row.get(field, "") for field in run_autosweep.CONFIG_FIELDS}
            out["effective_config_hash"] = effective
            out["selection_roles"] = f"{plane}:{role}"
            by_effective[effective] = out
        else:
            current = str(by_effective[effective].get("selection_roles", ""))
            by_effective[effective]["selection_roles"] = "|".join(part for part in [current, f"{plane}:{role}"] if part)
    return list(by_effective.values()), source_rows


def usable_dataset_rows(dataset_rows: list[dict[str, str]], health_rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    health_by_manifest = {row.get("manifest_path", ""): row for row in health_rows if row.get("manifest_path")}
    usable = []
    rejected = []
    for row in dataset_rows:
        if row.get("tier") != "TierA":
            rejected.append(dict(row, reason=row.get("reason") or "not_tier_a"))
            continue
        health = health_by_manifest.get(row.get("manifest_path", ""))
        if health and health.get("usable_data_flag") != "true":
            rejected.append(dict(row, reason=health.get("reject_reason", "health_reject")))
            continue
        usable.append(row)
    return usable, rejected


def rejected_config_rows(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    out = []
    for row in rows:
        if row.get("config_label") not in REJECTED_LABELS and not row.get("rejection_reason"):
            continue
        out.append(
            {
                "config_hash": row.get("config_hash", ""),
                "effective_config_hash": row.get("effective_config_hash", ""),
                "collection_view": row.get("collection_view", ""),
                "plane": row.get("plane", ""),
                "config_label": row.get("config_label", ""),
                "rejection_reason": row.get("rejection_reason", ""),
                "overall_score": row.get("overall_score", ""),
                "poster_score": row.get("poster_score", ""),
                "physics_score": row.get("physics_score", ""),
                "bpm_robustness_score": row.get("bpm_robustness_score", ""),
                "bpm_combination": row.get("bpm_combination", ""),
                "config_name": row.get("config_name", ""),
            }
        )
    return out


def write_summary(
    path: Path,
    usable_rows: list[dict[str, str]],
    rejected_spills: list[dict[str, str]],
    selected_configs: list[dict[str, object]],
    source_rows: list[dict[str, object]],
    rejected_configs: list[dict[str, object]],
    handoff_rows: list[dict[str, str]],
    expected_usable: int,
) -> None:
    collections = Counter(row.get("collection", "") for row in usable_rows)
    roles = [row for row in source_rows if row.get("status") == "selected"]
    missing_roles = [row for row in source_rows if row.get("status") != "selected"]
    lines = [
        "# Elite Full-Data Selection Summary",
        "",
        f"- usable Tier A spills: `{len(usable_rows)}`",
        f"- rejected Tier A/non-usable spills: `{len(rejected_spills)}`",
        f"- selected effective configs: `{len(selected_configs)}`",
        f"- selected role rows: `{len(roles)}`",
        f"- pilot handoff rows inspected: `{len(handoff_rows)}`",
        f"- rejected/flagged config rows retained: `{len(rejected_configs)}`",
    ]
    if expected_usable and expected_usable != len(usable_rows):
        lines.append(f"- warning: expected usable spills `{expected_usable}`, found `{len(usable_rows)}`")
    lines.extend(["", "## Usable Collections", "", "| collection | usable spills |", "|---|---:|"])
    for name, count in sorted(collections.items()):
        lines.append(f"| `{name}` | {count} |")
    lines.extend(["", "## Elite Roles", "", "| plane | role | config | label | combination | physics | poster |", "|---|---|---|---|---|---:|---:|"])
    for row in source_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("plane", "")),
                    str(row.get("selection_role", "")),
                    str(row.get("config_hash", ""))[:12],
                    str(row.get("config_label", "")),
                    str(row.get("bpm_combination", "")),
                    str(row.get("physics_score", "")),
                    str(row.get("poster_score", "")),
                ]
            )
            + " |"
        )
    if missing_roles:
        lines.extend(["", "## Missing Roles", ""])
        for row in missing_roles:
            lines.append(f"- `{row.get('plane')}` `{row.get('selection_role')}`")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Selection uses `collection_view=combined` first and falls back only if no combined rows exist.",
            "- Poster-safe selection excludes `TOO_SLOW`, `UNSTABLE_H`, `UNSTABLE_V`, and `OVERFITS_BAND`.",
            "- Effective config hashes ignore labels, scores, views, config names, stages, and irrelevant Hann multitaper parameters.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-dir", required=True)
    parser.add_argument("--top-configs", default="", help="pilot top_configs_for_full.csv; defaults under --pilot-dir")
    parser.add_argument("--dataset", required=True, help="Stage 0 dataset_manifest.csv")
    parser.add_argument("--health", required=True, help="Stage 0 spill_health.csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--expected-usable-spills", type=int, default=0)
    args = parser.parse_args(argv)

    pilot = Path(args.pilot_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ranked = annotate_effective(read_csv(pilot / "autosweep_ranked_configs.csv"))
    top_configs = read_csv(Path(args.top_configs)) if args.top_configs else read_csv(pilot / "top_configs_for_full.csv")
    dataset_rows = read_csv(Path(args.dataset))
    health_rows = read_csv(Path(args.health))
    usable_rows, rejected_spills = usable_dataset_rows(dataset_rows, health_rows)

    selections: list[tuple[str, str, dict[str, str] | None]] = []
    for plane in ("H", "V"):
        selections.extend((plane, role, row) for role, row in select_for_plane(ranked, plane))
    poster_role, poster_row = select_poster(ranked)
    selections.append(("combined", poster_role, poster_row))

    selected_configs, source_rows = merge_selected(selections)
    selected_by_plane = {
        plane: [config for config in selected_configs if f"{plane}:" in str(config.get("selection_roles", ""))]
        for plane in ("H", "V")
    }
    rejected_configs = rejected_config_rows(ranked)

    write_csv(out / "elite_dataset_manifest.csv", usable_rows, dataset_rows[0].keys() if dataset_rows else [])
    write_csv(out / "elite_configs_h.csv", selected_by_plane["H"], CONFIG_OUT_FIELDS)
    write_csv(out / "elite_configs_v.csv", selected_by_plane["V"], CONFIG_OUT_FIELDS)
    write_csv(out / "elite_configs_for_full.csv", selected_configs, CONFIG_OUT_FIELDS)
    write_csv(out / "elite_config_sources.csv", source_rows, SOURCE_FIELDS)
    write_csv(out / "elite_rejected_config_diagnostics.csv", rejected_configs, REJECTED_DIAGNOSTIC_FIELDS)
    if rejected_spills:
        write_csv(out / "elite_rejected_spill_diagnostics.csv", rejected_spills, rejected_spills[0].keys())
    write_summary(
        out / "elite_selection_summary.md",
        usable_rows,
        rejected_spills,
        selected_configs,
        source_rows,
        rejected_configs,
        top_configs,
        args.expected_usable_spills,
    )
    print(f"selected {len(selected_configs)} effective configs over {len(usable_rows)} usable spills")


if __name__ == "__main__":
    main()
