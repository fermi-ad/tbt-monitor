#!/usr/bin/env python3
"""Copy publication review components into one indexed, checksummed package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from build_image_gallery import IMAGE_SUFFIXES, build_html, image_rows


LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MANIFEST_FIELDS = ("component", "packaged_path", "source_path", "size_bytes", "sha256")
PACKAGE_METADATA = {
    "MANIFEST.csv",
    "PACKAGE_INDEX.md",
    "PACKAGE_VERIFICATION.json",
    "index.html",
}
COPY_IGNORE = shutil.ignore_patterns(
    ".DS_Store",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "*.pyc",
    "*.pyo",
)


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
        shutil.copytree(source, component_root, copy_function=shutil.copy2, ignore=COPY_IGNORE)
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


def verify_review_package(root: Path, require_receipt: bool = True) -> dict[str, object]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"review package is not a directory: {root}")
    manifest_path = root / "MANIFEST.csv"
    if not manifest_path.is_file():
        raise ValueError(f"review package manifest is missing: {manifest_path}")
    with manifest_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
            raise ValueError("review package manifest has the wrong fields")
        rows = list(reader)
    if not rows:
        raise ValueError("review package manifest is empty")
    package_index = root / "PACKAGE_INDEX.md"
    if not package_index.is_file() or package_index.stat().st_size == 0:
        raise ValueError(f"review package index is missing or empty: {package_index}")

    recorded: dict[str, dict[str, str]] = {}
    for row in rows:
        relative = row.get("packaged_path") or ""
        logical = Path(relative)
        component = row.get("component") or ""
        if (
            not LABEL_RE.fullmatch(component)
            or not row.get("source_path")
            or not relative
            or logical.is_absolute()
            or ".." in logical.parts
            or not logical.parts
            or logical.parts[0] != component
        ):
            raise ValueError(f"unsafe review package manifest path: {relative!r}")
        if relative in recorded:
            raise ValueError(f"duplicate review package manifest path: {relative}")
        source = root / logical
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"manifested review file is missing: {relative}")
        try:
            recorded_size = int(row.get("size_bytes") or "")
        except ValueError as exc:
            raise ValueError(f"invalid manifested byte size: {relative}") from exc
        actual_size = source.stat().st_size
        if recorded_size != actual_size:
            raise ValueError(
                f"review package byte-size mismatch: {relative}: {recorded_size} != {actual_size}"
            )
        actual_hash = sha256(source)
        if row.get("sha256") != actual_hash:
            raise ValueError(f"review package SHA-256 mismatch: {relative}")
        recorded[relative] = row

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() not in PACKAGE_METADATA
    }
    if set(recorded) != actual_files:
        missing = sorted(set(recorded) - actual_files)
        extra = sorted(actual_files - set(recorded))
        raise ValueError(
            f"review package file inventory mismatch: missing={missing}, extra={extra}"
        )

    gallery_path = root / "index.html"
    if not gallery_path.is_file():
        raise ValueError(f"review package gallery is missing: {gallery_path}")
    gallery = gallery_path.read_text(encoding="utf-8")
    images = image_rows(root)
    if gallery.count('<article class="figure"') != len(images):
        raise ValueError("review package gallery card count does not match its images")
    for image in images:
        escaped = html.escape(image["path"], quote=True)
        if gallery.count(f'src="{escaped}"') != 1:
            raise ValueError(f"review package image is not indexed exactly once: {image['path']}")

    manifested_images = sum(
        1 for path in recorded if Path(path).suffix.lower() in IMAGE_SUFFIXES
    )
    if manifested_images != len(images):
        raise ValueError("review package image manifest count does not match its gallery")
    report = {
        "schema": "tbt-monitor.publication-review-verification/v1",
        "status": "pass",
        "manifest_rows": len(rows),
        "copied_files": len(actual_files),
        "gallery_images": len(images),
        "manifest_sha256": sha256(manifest_path),
        "package_index_sha256": sha256(package_index),
        "gallery_sha256": sha256(gallery_path),
    }
    if require_receipt:
        receipt_path = root / "PACKAGE_VERIFICATION.json"
        if not receipt_path.is_file():
            raise ValueError(f"review package verification receipt is missing: {receipt_path}")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid review package verification receipt: {receipt_path}") from exc
        if receipt != report:
            raise ValueError("review package verification receipt does not match recomputed state")
    return report


def package_review(
    components: Sequence[tuple[str, Path]],
    out: Path,
    gallery_title: str = "Publication Review Gallery",
) -> list[dict[str, object]]:
    out = out.expanduser().resolve()
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"output path is not a directory: {out}")
        if any(out.iterdir()):
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
        "`PACKAGE_VERIFICATION.json` records the post-copy manifest and gallery verification counts.",
        "",
        "## Components",
        "",
        "| Component | Files | Bytes | Source |",
        "| --- | ---: | ---: | --- |",
    ]
    for label, source, file_count, size_bytes in summaries:
        lines.append(f"| `{label}` | {file_count} | {size_bytes} | `{source}` |")
    lines.extend(["", f"Total copied files: `{len(rows)}`", ""])
    lines.extend(
        [
            "Open `index.html` for a searchable, filterable gallery of every packaged image.",
            "After transfer, rerun `scripts/package_publication_review.py --verify-only PATH` against this directory.",
            "",
        ]
    )
    (out / "PACKAGE_INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "index.html").write_text(build_html(out, gallery_title), encoding="utf-8")
    verification = verify_review_package(out, require_receipt=False)
    (out / "PACKAGE_VERIFICATION.json").write_text(
        json.dumps(verification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", action="append", help="repeatable LABEL=PATH component")
    parser.add_argument("--out", help="new or empty package directory")
    parser.add_argument("--verify-only", help="verify an existing copied review package")
    parser.add_argument("--title", default="Publication Review Gallery", help="title shown in the image gallery")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            if args.component or args.out:
                parser.error("--verify-only cannot be combined with --component or --out")
            report = verify_review_package(Path(args.verify_only))
            print(json.dumps(report, sort_keys=True))
            return 0
        if not args.component or not args.out:
            parser.error("--component and --out are required when creating a package")
        components = [parse_component(value) for value in args.component]
        rows = package_review(components, Path(args.out), args.title)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"packaged {len(rows)} files under {Path(args.out).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
