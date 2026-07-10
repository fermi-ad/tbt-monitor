#!/usr/bin/env python3
"""Copy publication review components into one indexed, checksummed package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MANIFEST_FIELDS = ("component", "packaged_path", "source_path", "size_bytes", "sha256")


def parse_component(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"component must be LABEL=PATH: {value!r}")
    label, raw_path = value.split("=", 1)
    if not LABEL_RE.fullmatch(label):
        raise ValueError(f"invalid component label: {label!r}")
    source = Path(raw_path).expanduser().resolve()
    if not source.exists():
        raise ValueError(f"component source does not exist: {source}")
    return label, source


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def copied_files(destination: Path) -> list[Path]:
    if destination.is_file():
        return [destination]
    return sorted(path for path in destination.rglob("*") if path.is_file())


def copy_component(label: str, source: Path, package_root: Path) -> list[dict[str, object]]:
    component_root = package_root / label
    if source.is_dir():
        shutil.copytree(source, component_root, copy_function=shutil.copy2)
        destination = component_root
    else:
        component_root.mkdir(parents=True)
        destination = component_root / source.name
        shutil.copy2(source, destination)
    rows: list[dict[str, object]] = []
    for path in copied_files(destination):
        relative = path.relative_to(package_root).as_posix()
        source_path = source / path.relative_to(destination) if source.is_dir() else source
        rows.append(
            {
                "component": label,
                "packaged_path": relative,
                "source_path": str(source_path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return rows


def write_manifest(path: Path, rows: Sequence[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def package_review(components: Sequence[tuple[str, Path]], out: Path) -> list[dict[str, object]]:
    out = out.expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory is not empty: {out}")
    labels = [label for label, _source in components]
    if len(labels) != len(set(labels)):
        raise ValueError("component labels must be unique")
    for _label, source in components:
        if out == source or out in source.parents or source in out.parents:
            raise ValueError(f"package output and component source must not contain one another: {source}")
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    summaries: list[tuple[str, Path, int, int]] = []
    for label, source in components:
        component_rows = copy_component(label, source, out)
        rows.extend(component_rows)
        summaries.append(
            (
                label,
                source,
                len(component_rows),
                sum(int(row["size_bytes"]) for row in component_rows),
            )
        )
    write_manifest(out / "MANIFEST.csv", rows)
    lines = [
        "# Publication Review Package",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "Every copied file is listed in `MANIFEST.csv` with its original path, byte size, and SHA-256 checksum.",
        "",
        "## Components",
        "",
        "| Component | Files | Bytes | Source |",
        "| --- | ---: | ---: | --- |",
    ]
    for label, source, file_count, size_bytes in summaries:
        lines.append(f"| `{label}` | {file_count} | {size_bytes} | `{source}` |")
    lines.extend(["", f"Total copied files: `{len(rows)}`", ""])
    (out / "PACKAGE_INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", action="append", required=True, help="repeatable LABEL=PATH component")
    parser.add_argument("--out", required=True, help="new or empty package directory")
    args = parser.parse_args(argv)
    try:
        components = [parse_component(value) for value in args.component]
        rows = package_review(components, Path(args.out))
    except ValueError as exc:
        parser.error(str(exc))
    print(f"packaged {len(rows)} files under {Path(args.out).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
