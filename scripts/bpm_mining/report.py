"""Markdown reports for best-BPM mining."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .io import atomic_write_text, read_csv


def _completed_subset_sizes(inputs: Path) -> list[int]:
    sizes = []
    for size in (1, 3, 5, 10):
        if (inputs / "subset_search" / f"best{size}" / f"best{size}_results.csv").exists():
            sizes.append(size)
    return sizes


def _subset_label(sizes: list[int]) -> str:
    return ", ".join(f"best-{size}" for size in sizes) if sizes else "none"


def _claim_discipline(sizes: list[int]) -> str:
    exhaustive = [size for size in sizes if size in {1, 3}]
    screened = [size for size in sizes if size not in {1, 3}]
    clauses = []
    if exhaustive:
        clauses.append(f"{_subset_label(exhaustive)} are globally exhaustive over valid BPMs")
    if screened:
        clauses.append(f"{_subset_label(screened)} are screened-pool exact searches with beam/random full-space audits")
    return "; ".join(clauses) + "." if clauses else "No subset-search outputs were found."


def _screened_caveat(sizes: list[int]) -> str:
    screened = [size for size in sizes if size not in {1, 3}]
    if not screened:
        return ""
    return f"- {_subset_label(screened)} are not globally exhaustive over all BPMs; they are exact within screened pools with audit metadata."


def _top(rows, plane, n=10):
    picked = [row for row in rows if row.get("plane") == plane]
    picked.sort(key=lambda row: float(row.get("top1_frequency") or 0.0), reverse=True)
    return picked[:n]


def make_report(cfg: dict[str, object], inputs: Path, out: Path) -> None:
    completed_sizes = _completed_subset_sizes(inputs)
    manifest = read_csv(inputs / "manifest" / "spills.csv") if (inputs / "manifest" / "spills.csv").exists() else []
    consensus = read_csv(inputs / "consensus" / "spill_consensus_summary.csv") if (inputs / "consensus" / "spill_consensus_summary.csv").exists() else []
    bpm_stats = read_csv(inputs / "statistics" / "bpm_global_statistics.csv") if (inputs / "statistics" / "bpm_global_statistics.csv").exists() else []
    artifacts = read_csv(inputs / "artifact_selection" / "artifact_manifest.csv") if (inputs / "artifact_selection" / "artifact_manifest.csv").exists() else []
    finalists = read_csv(inputs / "evolution" / "finalist_reevaluation.csv") if (inputs / "evolution" / "finalist_reevaluation.csv").exists() else []
    fixed_direct = read_csv(inputs / "statistics" / "fixed_vs_dynamic_direct_summary.csv") if (inputs / "statistics" / "fixed_vs_dynamic_direct_summary.csv").exists() else []
    heldout = read_csv(inputs / "evolution" / "heldout_spectral_support_summary.csv") if (inputs / "evolution" / "heldout_spectral_support_summary.csv").exists() else []
    handoff = read_csv(inputs / "handoff" / "bpm_handoff_events.csv") if (inputs / "handoff" / "bpm_handoff_events.csv").exists() else []
    class_counts = Counter(row.get("consensus_label", "") for row in consensus)
    lines = [
        "# Strong BPM Analysis Summary",
        "",
        "## Dataset Coverage",
        "",
        f"- spills inventoried: `{len(manifest)}`",
        f"- usable spills: `{sum(1 for row in manifest if row.get('spill_usable') == 'true')}`",
        "",
        "## Integrity/Rejection Summary",
        "",
        "Channel-level rejection details are in `manifest/rejections.csv`; no channel is silently dropped.",
        "",
        "## Within-Spill Consensus Class Counts",
        "",
    ]
    for label, count in sorted(class_counts.items()):
        lines.append(f"- `{label}`: `{count}`")
    for plane in ("H", "V"):
        inclusion_sizes = [size for size in completed_sizes if size > 1]
        header = ["BPM", "top1 frequency"] + [f"top{size} inclusion" for size in inclusion_sizes]
        lines.extend(["", f"## Globally Strongest {plane} BPMs", "", "| " + " | ".join(header) + " |", "| " + " | ".join(["---"] + ["---:"] * (len(header) - 1)) + " |"])
        for row in _top(bpm_stats, plane):
            values = [f"`{row.get('bpm_name','')}`", row.get("top1_frequency", "")]
            for size in inclusion_sizes:
                values.append(row.get(f"top{size}_inclusion_frequency", ""))
            lines.append("| " + " | ".join(values) + " |")
    fixed_lines = [
        "",
        "## Best Fixed Subsets",
        "",
    ]
    if fixed_direct:
        fixed_lines.extend(
            [
                f"Direct fixed-set spectral evaluation rows are available: `{len(fixed_direct)}` summary rows.",
                "Use `statistics/fixed_vs_dynamic_direct_summary.csv` for operational fixed-set candidates.",
                "Legacy `statistics/fixed_sets_*` tables are overlap-filtered dynamic-winner proxy summaries, not held-out fixed-set validation.",
            ]
        )
    else:
        fixed_lines.extend(
            [
                "Direct fixed-set spectral evaluation is not present in this output tree.",
                "Legacy `statistics/fixed_sets_*` tables are overlap-filtered dynamic-winner proxy summaries and should not be treated as held-out fixed-set validation.",
            ]
        )
    caveats = [
        "- The machine tune may vary freely between spills.",
        "- No chronological tune trend is assumed.",
        "- The per-spill consensus is an internal unsupervised reference, not ground truth.",
        "- Dynamic best-BPM selection has look-elsewhere bias.",
        "- Held-out BPM support is used to reduce that bias.",
    ]
    screened_caveat = _screened_caveat(completed_sizes)
    if screened_caveat:
        caveats.append(screened_caveat)
    caveats.extend(
        [
            "- Absolute p-values are not sufficient; use effect sizes and confidence intervals.",
            "- A smooth tune ridge without spectral visibility is not accepted as a measurement.",
            "- No tune value is reported in `NO_RELIABLE_TUNE` windows.",
            "- Expected H near 0.65 and V near 0.72 are soft priors only.",
        ]
    )
    lines.extend(
        fixed_lines
        + [
            "",
            "## Dynamic Per-Spill Subset Performance",
            "",
            "Dynamic subset outputs are in `subset_search/best*/best*_results.csv`; scores are within-spill and use held-out BPM support.",
            f"`evolution/finalist_reevaluation.csv` contains `{len(finalists)}` robust finalist rows across mean, median, trimmed-mean, and static-quality-weighted aggregators.",
            "",
            "## Fixed-Vs-Dynamic Performance",
            "",
            "Direct fixed-set summaries compare frozen trained member sets against dynamic per-spill winners when follow-up outputs are present.",
            "The older cross-fit summaries only describe dynamic winners that overlap trained member lists.",
            "",
            "## Subset-Size Effect Sizes",
            "",
            "`statistics/paired_method_tests.csv` reports paired differences and effect sizes. Tiny p-values alone are not treated as sufficient evidence.",
            "",
            "## Collection-To-Collection Ranking Stability",
            "",
            "`statistics/bpm_rank_stability.csv` includes top-N overlap and scipy-free rank-stability fallbacks.",
            "",
            "## Cluster-Specific BPM Behavior",
            "",
            "`clustering/cluster_bpm_rankings.csv` ranks BPMs within neutral morphology clusters.",
            "",
            "## Visibility Duration Conclusions",
            "",
            "`evolution/subset_evolution_summary.csv` records visible fractions and duration proxies without forcing tunes in unreliable windows.",
            f"`evolution/heldout_spectral_support_summary.csv` rows: `{len(heldout)}`.",
            f"`handoff/bpm_handoff_events.csv` rows: `{len(handoff)}`.",
            "",
            "## Digitizer/Ring-Location Findings",
            "",
            "Digitizer and ring-order fields are preserved in `manifest/bpm_index.csv` and included in global BPM statistics.",
            "",
            "## Statistical Caveats",
            "",
            *caveats,
            "",
            "## Best Poster Artifacts",
            "",
            f"- selected artifact spill-plane rows: `{len(artifacts)}`",
            "- global plots: `artifacts/global/`",
            "- per-spill plots: `artifacts/spills/`",
            "- cache-backed deconstruction and subset-overlay plots supersede older placeholder-style plots when present.",
            "",
            "## Recommended Operational Subset",
            "",
            "Prefer directly re-evaluated fixed subsets when their held-out spectral scores are competitive; otherwise treat dynamic per-spill winners as review examples.",
            "",
            "## Recommended Next Beam Study",
            "",
            "Repeat with a short labeled study where machine settings and independent tune references are logged, then compare these BPM-only rankings against that external reference.",
            "",
        ]
    )
    atomic_write_text(out / "strong_bpm_analysis_summary.md", "\n".join(lines))
    exec_lines = [
        "# Strong BPM Executive Summary",
        "",
        f"This BPM-only mining run inventoried `{len(manifest)}` spills and selected `{len(artifacts)}` poster-review spill-plane artifacts.",
        "",
        f"Completed subset sizes: `{','.join(str(size) for size in completed_sizes)}`.",
        "",
        f"Primary claim discipline: {_claim_discipline(completed_sizes)}",
        "",
        "Use direct fixed-set spectral evaluation for operational fixed-set claims when present; use per-spill dynamic winners as examples, not as external truth labels.",
        "",
    ]
    atomic_write_text(out / "strong_bpm_executive_summary.md", "\n".join(exec_lines))
