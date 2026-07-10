#!/usr/bin/env python3
"""Build a self-contained static index for a directory of review images."""

from __future__ import annotations

import argparse
import csv
import html
import os
from pathlib import Path
from typing import Mapping, Sequence


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MANIFEST_NAMES = {"figure_manifest.csv", "ridge_density_best_ensemble_manifest.csv"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def normalize_key(root: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return path.name
    return path.as_posix().lstrip("./")


def manifest_metadata(root: Path) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for manifest in sorted(path for path in root.rglob("*.csv") if path.name in MANIFEST_NAMES):
        for row in read_csv(manifest):
            image_value = row.get("path") or row.get("figure") or ""
            if not image_value:
                continue
            keys = {normalize_key(root, image_value), Path(image_value).name}
            clean = {key: value for key, value in row.items() if key and value}
            caption_value = row.get("caption_file") or ""
            if caption_value:
                caption_candidates = [manifest.parent / caption_value, root / caption_value]
                caption = next((path for path in caption_candidates if path.exists()), None)
                if caption:
                    clean["caption"] = caption.read_text(encoding="utf-8").strip()
            for key in keys:
                metadata.setdefault(key, {}).update(clean)
    return metadata


def caption_for_image(image: Path) -> str:
    candidates = (
        image.with_name(f"{image.stem}_caption.md"),
        image.with_suffix(".md"),
    )
    caption = next((path for path in candidates if path.exists()), None)
    return caption.read_text(encoding="utf-8").strip() if caption else ""


def image_rows(root: Path) -> list[dict[str, str]]:
    metadata = manifest_metadata(root)
    rows: list[dict[str, str]] = []
    for image in sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES):
        relative = image.relative_to(root).as_posix()
        item = dict(metadata.get(relative) or metadata.get(image.name) or {})
        parent_category = image.parent.relative_to(root).as_posix()
        if parent_category == ".":
            parent_category = "root"
        category = item.get("category") or item.get("role") or parent_category
        caption = item.get("caption") or caption_for_image(image)
        rows.append(
            {
                "path": relative,
                "name": image.name,
                "category": category,
                "plane": item.get("plane", ""),
                "subset_size": item.get("subset_size", ""),
                "method": item.get("method", ""),
                "description": item.get("description", ""),
                "guardrail": item.get("claim_guardrail", ""),
                "caption": caption,
                "source": item.get("source", ""),
            }
        )
    return rows


def card_markup(row: Mapping[str, str], index: int, link_prefix: str) -> str:
    tags = [row.get(field, "") for field in ("category", "plane", "subset_size", "method")]
    tags = [value for value in tags if value]
    searchable = " ".join([row.get("path", ""), *tags, row.get("description", ""), row.get("caption", "")]).lower()
    tag_markup = "".join(f"<span>{html.escape(value)}</span>" for value in tags)
    description = row.get("description") or first_caption_line(row.get("caption", ""))
    guardrail = row.get("guardrail", "")
    image_path = f"{link_prefix}/{row['path']}" if link_prefix else row["path"]
    return f"""
      <article class="figure" data-search="{html.escape(searchable, quote=True)}" data-category="{html.escape(row.get('category', ''), quote=True)}">
        <a href="{html.escape(image_path, quote=True)}" target="_blank" rel="noopener">
          <img src="{html.escape(image_path, quote=True)}" alt="{html.escape(row['name'], quote=True)}" loading="lazy">
        </a>
        <div class="meta">
          <div class="ordinal">{index:03d}</div>
          <h2>{html.escape(row['name'])}</h2>
          <div class="tags">{tag_markup}</div>
          {f'<p>{html.escape(description)}</p>' if description else ''}
          {f'<p class="guardrail">{html.escape(guardrail)}</p>' if guardrail else ''}
        </div>
      </article>"""


def first_caption_line(caption: str) -> str:
    for line in caption.splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean and not clean.lower().startswith("image:"):
            return clean
    return ""


def build_html(root: Path, title: str, link_prefix: str = "") -> str:
    rows = image_rows(root)
    categories = sorted({row["category"] for row in rows if row["category"]})
    cards = "\n".join(card_markup(row, index, link_prefix) for index, row in enumerate(rows, start=1))
    options = "".join(f'<option value="{html.escape(value)}">{html.escape(value)}</option>' for value in categories)
    title_html = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <style>
    :root {{ color-scheme: light; --ink:#202428; --muted:#687078; --line:#d9dee2; --paper:#fafaf8; --panel:#fff; --accent:#177b67; --warn:#9a4d38; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--paper); color:var(--ink); font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    header {{ position:sticky; top:0; z-index:2; padding:14px 20px; border-bottom:1px solid var(--line); background:rgba(250,250,248,.96); backdrop-filter:blur(8px); }}
    .topline {{ display:flex; align-items:baseline; justify-content:space-between; gap:20px; margin-bottom:10px; }}
    h1 {{ margin:0; font-size:20px; letter-spacing:0; }}
    #count {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
    .controls {{ display:grid; grid-template-columns:minmax(220px,1fr) minmax(180px,280px); gap:10px; }}
    input,select {{ width:100%; height:36px; border:1px solid #b8c0c6; border-radius:4px; background:#fff; color:var(--ink); padding:0 10px; font:inherit; }}
    main {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(520px,100%),1fr)); gap:12px; padding:14px; align-items:start; }}
    .figure {{ min-width:0; overflow:hidden; border:1px solid var(--line); border-radius:4px; background:var(--panel); }}
    .figure[hidden] {{ display:none; }}
    .figure a {{ display:block; background:#eef1f2; }}
    .figure img {{ display:block; width:100%; height:auto; max-height:72vh; object-fit:contain; }}
    .meta {{ position:relative; padding:10px 12px 12px; border-top:1px solid var(--line); }}
    .ordinal {{ position:absolute; right:12px; top:10px; color:#99a1a8; font:12px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    h2 {{ margin:0 48px 7px 0; overflow-wrap:anywhere; font-size:14px; letter-spacing:0; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:5px; }}
    .tags span {{ border-left:3px solid var(--accent); background:#edf5f2; padding:2px 6px; font-size:12px; }}
    p {{ margin:8px 0 0; color:#4d555c; }}
    .guardrail {{ border-left:3px solid var(--warn); padding-left:8px; color:#713b2e; }}
    @media (max-width:650px) {{ header {{ position:static; }} .controls {{ grid-template-columns:1fr; }} main {{ padding:8px; gap:8px; }} }}
  </style>
</head>
<body>
  <header>
    <div class="topline"><h1>{title_html}</h1><div id="count">{len(rows)} figures</div></div>
    <div class="controls">
      <input id="search" type="search" placeholder="Search plane, N, method, filename, or caption" aria-label="Search figures">
      <select id="category" aria-label="Filter by category"><option value="">All categories</option>{options}</select>
    </div>
  </header>
  <main>{cards}</main>
  <script>
    const figures = [...document.querySelectorAll('.figure')];
    const search = document.querySelector('#search');
    const category = document.querySelector('#category');
    const count = document.querySelector('#count');
    function filter() {{
      const query = search.value.trim().toLowerCase();
      const selected = category.value;
      let visible = 0;
      for (const figure of figures) {{
        const show = (!query || figure.dataset.search.includes(query)) && (!selected || figure.dataset.category === selected);
        figure.hidden = !show;
        if (show) visible += 1;
      }}
      count.textContent = `${{visible}} / ${{figures.length}} figures`;
    }}
    search.addEventListener('input', filter);
    category.addEventListener('change', filter);
  </script>
</body>
</html>
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="directory containing images and optional figure manifests")
    parser.add_argument("--out", default=None, help="HTML output path; defaults to ROOT/index.html")
    parser.add_argument("--title", default="Analysis Figure Review Gallery")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"gallery root is not a directory: {root}")
    out = Path(args.out).resolve() if args.out else root / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    relative_root = Path(os.path.relpath(root, out.parent)).as_posix()
    link_prefix = "" if relative_root == "." else relative_root
    rows = image_rows(root)
    out.write_text(build_html(root, args.title, link_prefix), encoding="utf-8")
    print(f"wrote {out} with {len(rows)} figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
