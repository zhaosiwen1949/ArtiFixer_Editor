#!/usr/bin/env python3
"""Batch-run ``fetch_realsee_floorplan.py`` over a CSV of Realsee work pages.

The CSV has two columns per row — ``scene_name`` and the work-page ``url`` — e.g.
``data/csv/batch_fetch.csv``::

    xinghewan, https://open.realsee.com/ke/vwYQ.../...#lianjia
    huizhongbeili-207, https://open.realsee.com/ke/6gyq.../...#lianjia

A leading ``scene_name,url`` header row is optional (auto-detected); blank rows
and ``#`` comment rows are skipped. Each scene is fetched into its own
subdirectory ``<out-root>/<scene_name>/`` (default out-root ``data/floorplan``)
by invoking ``fetch_realsee_floorplan.py`` as a subprocess — so every scene gets
the full pipeline (room_layout + images + SVG room completion + door/window
recovery) and one scene's failure never aborts the rest.

Usage::

    python tools/batch_fetch_realsee_floorplan.py                       # -> data/csv/batch_fetch.csv
    python tools/batch_fetch_realsee_floorplan.py my_scenes.csv
    python tools/batch_fetch_realsee_floorplan.py --out-root data/floorplan --no-svg
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

FETCH_SCRIPT = Path(__file__).with_name("fetch_realsee_floorplan.py")
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "data" / "csv" / "batch_fetch.csv"


def read_scenes(csv_path: Path) -> list[tuple[str, str]]:
    """Parse the CSV -> ``[(scene_name, url), ...]``. Tolerates whitespace, a
    header row, blank lines and ``#`` comments; raises on malformed rows."""
    scenes: list[tuple[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for lineno, row in enumerate(csv.reader(f), 1):
            cells = [c.strip() for c in row]
            if not cells or not any(cells) or cells[0].startswith("#"):
                continue
            if len(cells) < 2 or not cells[0] or not cells[1]:
                raise ValueError(
                    f"{csv_path}:{lineno}: expected 'scene_name, url', got {row!r}")
            name, url = cells[0], cells[1]
            if url.lower() == "url" and name.lower() == "scene_name":
                continue  # header row
            scenes.append((name, url))
    return scenes


def safe_name(name: str) -> str:
    """Reject path separators / traversal; keep the folder name a single
    directory component."""
    cleaned = re.sub(r"[^\w.\-]+", "_", name).strip("._")
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"unusable scene_name: {name!r}")
    return cleaned


def fetch_one(url: str, out_dir: Path, extra: list[str]) -> bool:
    """Run the single-scene fetcher; return True on success."""
    cmd = [sys.executable, str(FETCH_SCRIPT), url, "--out", str(out_dir), *extra]
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        return False
    manifest = out_dir / "floorplan.json"
    if not manifest.exists():
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        rl = data.get("room_layout") or {}
        return bool(rl.get("local_path"))
    except (json.JSONDecodeError, OSError):
        return False


def scene_summary(out_dir: Path) -> str:
    """Compact one-line result read back from the written manifest."""
    try:
        data = json.loads((out_dir / "floorplan.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    rl = data.get("room_layout") or {}
    parts = [f"{rl.get('room_count', '?')} rooms"]
    if data.get("rooms_extra"):
        parts.append(f"+{len(data['rooms_extra'].get('recovered', []))} recovered")
    dw = data.get("doors_windows")
    if dw:
        c = dw.get("counts", {})
        parts.append(f"{c.get('door', 0)}门/{c.get('window', 0)}窗/"
                     f"{c.get('opening', 0)}洞")
    return ", ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=str(DEFAULT_CSV),
                    help=f"CSV of scene_name,url rows (default {DEFAULT_CSV})")
    ap.add_argument("--out-root", default="data/floorplan",
                    help="parent dir for per-scene subfolders (default data/floorplan)")
    ap.add_argument("--no-svg", action="store_true",
                    help="forward --no-svg (skip SVG completion + door/window)")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"! CSV not found: {csv_path}")
    scenes = read_scenes(csv_path)
    if not scenes:
        sys.exit(f"! no scenes in {csv_path}")

    out_root = Path(args.out_root)
    extra = ["--no-svg"] if args.no_svg else []
    print(f"Batch: {len(scenes)} scene(s) from {csv_path} -> {out_root}/\n")

    results: list[tuple[str, bool, str]] = []
    for i, (name, url) in enumerate(scenes, 1):
        folder = safe_name(name)
        out_dir = out_root / folder
        print(f"{'='*70}\n[{i}/{len(scenes)}] {name}  ->  {out_dir}\n{'='*70}")
        try:
            ok = fetch_one(url, out_dir, extra)
        except Exception as exc:  # never let one scene abort the batch
            print(f"! {name}: {exc}", file=sys.stderr)
            ok = False
        results.append((name, ok, scene_summary(out_dir) if ok else ""))
        print()

    n_ok = sum(1 for _, ok, _ in results if ok)
    print(f"{'='*70}\nBatch done: {n_ok}/{len(scenes)} succeeded")
    for name, ok, summ in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}" + (f"  ({summ})" if summ else ""))
    sys.exit(0 if n_ok == len(scenes) else 1)


if __name__ == "__main__":
    main()
