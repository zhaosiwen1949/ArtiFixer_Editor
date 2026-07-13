#!/usr/bin/env python3
"""Fetch the floor plan (户型图) data + images from a Realsee work page.

Companion to ``fetch_realsee_model.py`` / ``fetch_realsee_panoramas.py``. The
Realsee "户型图" tab shows a room-labelled floor plan; the 漫游 tab shows a small
outline radar of it. Everything needed to reproduce both is embedded in the
work-page HTML (plus one referenced JSON asset):

  * ``objectsInUrl.ruler`` — a ``room_layout.json`` URL: the **structured** floor
    plan, one entry per room with ``roomName`` (客厅 / 卧室A / 厨房 …) and the 3D
    wall ``lines`` (start/end segments, with door/window gaps as ``children``).
  * ``hierarchy_floor_plan[]`` — the **detailed rendered** floor plan PNG (room
    names + areas + wall dimensions; this is the big "户型图" image).
  * ``outline_floor_plan[]`` — the **outline** PNG used in the 漫游 radar minimap.
  * ``standard_floor_plan_url`` — an optional "standard" floor plan image
    (frequently empty for auto-generated works).
  * ``house_layout`` — a JSON string of room counts
    (``bedroom_amount`` / ``parlor_amount`` / ``cookroom_amount`` / ``toilet_amount``).
  * plus the listing summary 户型 (e.g. ``3室2厅``) and 面积 (e.g. ``274.49㎡``).

This script downloads the ``room_layout.json`` and every floor plan image, and
writes a ``floorplan.json`` manifest tying the pieces together (source URLs +
local paths + checksums + a room-name/count summary).

Only the Python stdlib is used (urllib), so no extra packages are required.

Usage::

    python tools/fetch_realsee_floorplan.py                       # -> data/floorplan
    python tools/fetch_realsee_floorplan.py "<work-url>" --out data/floorplan
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = (
    "https://open.realsee.com/ke/vwYQ3drRBl69nj28/"
    "KpokNd82rwjh1hkhQTMNpa3cg8zGbPXe/#lianjia"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://open.realsee.com/",
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# JSON value extraction from the embedded page state
# --------------------------------------------------------------------------- #
def _scan_json_value(s: str, start: int) -> tuple[str, int]:
    """Return (raw_json_text, end_index) for the JSON value beginning at/after
    ``start``. Handles objects, arrays, quoted strings (with escapes) and
    scalars, so nested braces/brackets and quoted punctuation are respected."""
    while start < len(s) and s[start] in " \t\r\n":
        start += 1
    c = s[start]
    if c in "{[":
        close = "}" if c == "{" else "]"
        depth = 0
        in_str = esc = False
        for j in range(start, len(s)):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == c:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    return s[start:j + 1], j + 1
        raise ValueError("unbalanced brackets")
    if c == '"':
        esc = False
        for j in range(start + 1, len(s)):
            ch = s[j]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                return s[start:j + 1], j + 1
        raise ValueError("unterminated string")
    j = start
    while j < len(s) and s[j] not in ",}]":
        j += 1
    return s[start:j], j


def extract_value(html: str, key: str):
    """Parse the JSON value assigned to the first ``"key":`` in the page, or
    ``None`` if the key is absent."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:', html)
    if not m:
        return None
    raw, _ = _scan_json_value(html, m.end())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def extract_floorplan(html: str) -> dict:
    """Pull all floor plan pieces out of the work-page HTML."""
    fp: dict = {}

    # Structured room layout (JSON asset URL lives under objectsInUrl.ruler).
    objects = extract_value(html, "objectsInUrl") or {}
    layout_url = objects.get("ruler") if isinstance(objects, dict) else None
    if not layout_url:  # fall back to a direct scan for the asset
        m = re.search(r'https?://[^"\\]*room_layout\.json[^"\\]*', html)
        layout_url = m.group(0) if m else None
    fp["room_layout_url"] = layout_url

    # Rendered floor plan images.
    fp["hierarchy_floor_plan"] = extract_value(html, "hierarchy_floor_plan") or []
    fp["outline_floor_plan"] = extract_value(html, "outline_floor_plan") or []
    fp["standard_floor_plan_url"] = extract_value(html, "standard_floor_plan_url") or ""
    fp["standard_floor_plan_checksum"] = (
        extract_value(html, "standard_floor_plan_checksum") or ""
    )

    # Room-count summary (a JSON-encoded string) + listing 户型/面积.
    house_layout = extract_value(html, "house_layout")
    if isinstance(house_layout, str):
        try:
            house_layout = json.loads(house_layout)
        except json.JSONDecodeError:
            pass
    fp["house_layout"] = house_layout

    m = re.search(r'"content":"([^"]*)","icon":"house_model"', html)
    fp["house_type"] = m.group(1) if m else None
    m = re.search(r'"content":"([^"]*)","icon":"area","title":"面积"', html)
    fp["area"] = m.group(1) if m else None

    return fp


# --------------------------------------------------------------------------- #
# Download helpers
# --------------------------------------------------------------------------- #
def download(url: str, dest: Path) -> Path | None:
    """Download url -> dest. Returns dest on success, None on failure."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  · cached {dest.name}")
        return dest
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        dest.write_bytes(data)
        print(f"  ✓ {dest.name} ({len(data) / 1024:.0f} KB)")
        return dest
    except Exception as exc:  # network / HTTP error
        print(f"  ! failed {url}: {exc}", file=sys.stderr)
        return None


def _image_records(entries, kind: str, img_dir: Path, out_dir: Path) -> list[dict]:
    """Download a ``[{checksum,index,url}, ...]`` image list, naming files
    ``<kind>_<index>.png``. Returns manifest records."""
    records = []
    for e in entries:
        url = e.get("url")
        if not url:
            continue
        index = e.get("index", 0)
        ext = url.rsplit(".", 1)[-1].split("?")[0] or "png"
        dest = img_dir / f"{kind}_{index}.{ext}"
        got = download(url, dest)
        records.append({
            "url": url,
            "index": index,
            "checksum": e.get("checksum"),
            "local_path": str(dest.relative_to(out_dir)) if got else None,
        })
    return records


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="Realsee work URL")
    ap.add_argument("--out", default="data/floorplan", help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.out)
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading work page: {args.url}")
    html = fetch_html(args.url)
    fp = extract_floorplan(html)

    summary = fp.get("house_type") or "?"
    print(f"Floor plan: 户型 {summary}, 面积 {fp.get('area') or '?'}, "
          f"counts {fp.get('house_layout')}")

    # 1. Structured room layout JSON.
    layout_record = None
    room_names: list[str] = []
    room_count = 0
    if fp["room_layout_url"]:
        print("Downloading room_layout.json...")
        dest = download(fp["room_layout_url"], out_dir / "room_layout.json")
        if dest:
            try:
                rooms = json.loads(dest.read_text(encoding="utf-8"))
                room_count = len(rooms)
                # unique room names in first-seen order
                seen = dict.fromkeys(r.get("roomName") for r in rooms if r.get("roomName"))
                room_names = list(seen)
            except (json.JSONDecodeError, OSError):
                pass
        layout_record = {
            "url": fp["room_layout_url"],
            "local_path": "room_layout.json" if dest else None,
            "room_count": room_count,
            "room_names": room_names,
        }
        print(f"  {room_count} rooms: {', '.join(room_names)}")
    else:
        print("! No room_layout.json URL found in page.", file=sys.stderr)

    # 2. Floor plan images.
    print("Downloading floor plan images...")
    hierarchy = _image_records(fp["hierarchy_floor_plan"], "hierarchy_floor_plan",
                               img_dir, out_dir)
    outline = _image_records(fp["outline_floor_plan"], "outline_floor_plan",
                             img_dir, out_dir)
    standard = None
    if fp["standard_floor_plan_url"]:
        url = fp["standard_floor_plan_url"]
        ext = url.rsplit(".", 1)[-1].split("?")[0] or "png"
        dest = download(url, img_dir / f"standard_floor_plan.{ext}")
        standard = {
            "url": url,
            "checksum": fp["standard_floor_plan_checksum"] or None,
            "local_path": str(dest.relative_to(out_dir)) if dest else None,
        }

    # 3. Manifest.
    manifest = {
        "source_page": args.url,
        "house_summary": {
            "house_type": fp.get("house_type"),
            "area": fp.get("area"),
            "house_layout": fp.get("house_layout"),
        },
        "room_layout": layout_record,
        "images": {
            "hierarchy_floor_plan": hierarchy,   # detailed rendered floor plan
            "outline_floor_plan": outline,       # radar minimap outline
            "standard_floor_plan": standard,     # optional, often absent
        },
    }
    (out_dir / "floorplan.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))

    n_img = (sum(1 for r in hierarchy if r["local_path"])
             + sum(1 for r in outline if r["local_path"])
             + (1 if standard and standard["local_path"] else 0))
    print(f"\nDone. room_layout {'ok' if layout_record and layout_record['local_path'] else 'FAILED'}, "
          f"{n_img} image(s) -> {out_dir}")
    print(f"Manifest: {out_dir / 'floorplan.json'}")


if __name__ == "__main__":
    main()
