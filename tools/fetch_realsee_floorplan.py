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

SVG room completion
-------------------
``room_layout.json`` can miss rooms (this scene: 卫生间A / 衣帽间A / 阳台C). The
户型图 tab however draws the floor plan as an inline SVG in which **every** room
is one ``<path>`` polygon (units mm, y down), with the room names as HTML
overlays on top of it. An optional completion stage therefore:

  1. captures that SVG with headless Chromium (Playwright): open the page,
     click the 户型图 tab, dump the ``<svg>`` holding the room polygons, and
     bake the overlay room names into it as ``<text>`` elements (screen → SVG
     coords via ``getScreenCTM().inverse()``) → ``floorplan.svg``;
  2. fits the SVG→layout transform (per-axis scale + offset, y flipped) by
     greedily matching SVG polygons to the known ``room_layout.json`` rooms and
     least-squares refining on the matched centroids;
  3. names each leftover SVG polygon by the room-name ``<text>`` falling inside
     it, insets it by the calibrated half wall thickness (the SVG draws rooms
     to wall centerlines, room_layout to inner surfaces), and writes the
     recovered rooms to ``rooms_extra.json`` (metres, room_layout world x/z).
     The wall-centerline polygons of **all** rooms (layout + recovered; the
     registered SVG polygons, no inset) go to ``rooms_centerline.json``.

The base scrape is stdlib-only; the completion stage additionally needs
``numpy``, ``shapely`` and ``playwright`` (+ its chromium) and is skipped with
a warning when they are missing. ``--svg`` reuses a saved SVG (no browser),
``--no-svg`` disables the stage entirely.

Usage::

    python tools/fetch_realsee_floorplan.py                       # -> data/floorplan
    python tools/fetch_realsee_floorplan.py "<work-url>" --out data/floorplan
    python tools/fetch_realsee_floorplan.py --svg data/floorplan/floorplan.svg
    python tools/fetch_realsee_floorplan.py --no-svg
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
    """Download url -> dest, overwriting any existing file. Returns dest on
    success, None on failure."""
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


# --------------------------------------------------------------------------- #
# SVG room completion (optional stage; needs playwright + numpy + shapely)
# --------------------------------------------------------------------------- #
# JS run in the page: find the floor plan <svg> (the one with the most
# M/L/Z-only room paths), map every visible room-name overlay into its user
# coordinate system, and return the SVG source + labels.
_JS_GRAB_SVG = """
() => {
    const isRoomPath = d => d && /^[ML0-9,.eZ\\s-]+$/.test(d);
    let best = null, bestN = 0;
    for (const svg of document.querySelectorAll('svg')) {
        const n = [...svg.querySelectorAll('path')]
            .filter(p => isRoomPath(p.getAttribute('d'))).length;
        if (n > bestN) { best = svg; bestN = n; }
    }
    if (!best || bestN < 5) return null;
    const inv = best.getScreenCTM().inverse();
    const box = best.getBoundingClientRect();
    const labels = [];
    for (const el of document.querySelectorAll('div,span')) {
        if (el.children.length) continue;
        const t = el.textContent.trim();
        if (!t || t.length > 8 || !/[\\u4e00-\\u9fff]/.test(t)) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
        if (cx < box.left || cx > box.right || cy < box.top || cy > box.bottom)
            continue;
        const pt = new DOMPoint(cx, cy).matrixTransform(inv);
        labels.push({ x: pt.x, y: pt.y, text: t });
    }
    return { svg: best.outerHTML, labels, n_paths: bestN };
}
"""


def capture_floorplan_svg(url: str, dest: Path) -> Path | None:
    """Open ``url`` in headless Chromium, click the 户型图 tab and save the room
    SVG (with the overlay room names injected as ``<text>``) to ``dest``.
    Returns ``dest`` on success, ``None`` (with a warning) on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("! playwright not installed - skipping SVG capture "
              "(pip install playwright && playwright install chromium)",
              file=sys.stderr)
        return None

    import html as html_mod

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            grabbed = None
            tab = page.get_by_text("户型图", exact=True)
            for i in range(max(tab.count(), 1)):
                try:
                    tab.nth(i).click(timeout=5000)
                except Exception:
                    continue
                # success criterion: a room SVG shows up
                for _ in range(20):
                    page.wait_for_timeout(500)
                    grabbed = page.evaluate(_JS_GRAB_SVG)
                    if grabbed:
                        break
                if grabbed:
                    break
            browser.close()
    except Exception as exc:  # browser / navigation error
        print(f"! SVG capture failed: {exc}", file=sys.stderr)
        return None

    if not grabbed:
        print("! floor plan SVG not found on page - skipping completion",
              file=sys.stderr)
        return None

    svg = grabbed["svg"]
    texts = "".join(
        f'<text x="{l["x"]:.1f}" y="{l["y"]:.1f}">{html_mod.escape(l["text"])}</text>'
        for l in grabbed["labels"])
    svg = svg.replace("</svg>", texts + "</svg>")
    dest.write_text(svg, encoding="utf-8")
    print(f"  ✓ {dest.name} ({grabbed['n_paths']} room paths, "
          f"{len(grabbed['labels'])} labels)")
    return dest


def parse_svg_polygons(svg_text: str) -> list:
    """floorplan.svg -> list of Nx2 arrays (SVG units, mm, y down)."""
    import numpy as np
    polys = []
    for d in re.findall(r'<path\b[^>]*?\sd="([^"]+)"', svg_text):
        if not set(re.findall(r"[A-Za-z]", d)) <= {"M", "L", "Z"}:
            continue  # markers/icons etc.
        pts = [tuple(map(float, m.split(",")))
               for m in re.findall(r"[-\d.e]+,[-\d.e]+", d)]
        if len(pts) >= 3:
            polys.append(np.asarray(pts))
    return polys


def parse_svg_labels(svg_text: str) -> list[tuple[float, float, str]]:
    """``<text x y>`` room-name labels -> [(x, y, name)] (SVG coords). Labels
    that look like measurements (㎡ / pure numbers) are dropped."""
    labels = []
    for x, y, t in re.findall(
            r'<text[^>]*\bx="([-\d.e]+)"[^>]*\by="([-\d.e]+)"[^>]*>([^<]+)</text>',
            svg_text):
        t = t.strip()
        if not t or "㎡" in t or re.fullmatch(r"[\d.]+", t):
            continue
        labels.append((float(x), float(y), t))
    return labels


def load_layout_rooms(layout_path: Path) -> dict:
    """room_layout.json -> {roomName: shapely Polygon} (world x/z, metres).

    A room's ``lines`` mix plan edges (constant y) and vertical wall edges
    (y varies); keep the former and chain them into a loop (segments are
    unordered and endpoints can be a few mm apart). Duplicate names (one room
    seen from several panoIndex) keep the largest polygon."""
    import math

    from shapely.geometry import Polygon

    def room_polygon(lines, tol=0.05):
        segs = [((l["start"][0], l["start"][2]), (l["end"][0], l["end"][2]))
                for l in lines if abs(l["start"][1] - l["end"][1]) < 1e-6]
        if len(segs) < 3:
            return None
        pts = [segs[0][0], segs[0][1]]
        used = {0}
        while len(used) < len(segs):
            cur = pts[-1]
            best = bp = None
            bd = tol
            for i, (a, b) in enumerate(segs):
                if i in used:
                    continue
                da, db = math.dist(cur, a), math.dist(cur, b)
                if da < bd:
                    best, bp, bd = i, b, da
                if db < bd:
                    best, bp, bd = i, a, db
            if best is None:
                return None
            pts.append(bp)
            used.add(best)
        poly = Polygon(pts).buffer(0)
        return poly if poly.area > 0 else None

    rooms: dict = {}
    for r in json.loads(layout_path.read_text(encoding="utf-8")):
        name = r.get("roomName")
        poly = room_polygon(r.get("lines", []))
        if not name or poly is None:
            continue
        if name not in rooms or poly.area > rooms[name].area:
            rooms[name] = poly
    return rooms


def fit_transform(svg_polys, gt_rooms, n_iter=3):
    """Fit x_gt = ax*x + bx, z_gt = ay*y + by (y flip -> ay < 0).

    Seeded from the largest room on both sides (unique enough in practice),
    then alternate greedy centroid matching / least-squares refit.
    Returns (params (ax, bx, ay, by), {gt_name: svg_index})."""
    import numpy as np
    from shapely.geometry import Polygon

    svg_cent = np.asarray([Polygon(p).centroid.coords[0] for p in svg_polys])
    svg_area = np.asarray([Polygon(p).area for p in svg_polys])
    names = list(gt_rooms)
    gt_cent = np.asarray([gt_rooms[n].centroid.coords[0] for n in names])
    gt_area = np.asarray([gt_rooms[n].area for n in names])

    i_svg = int(np.argmax(svg_area))
    i_gt = int(np.argmax(gt_area))
    s = float(np.sqrt(gt_area[i_gt] / svg_area[i_svg]))     # ~0.001 (mm->m)
    ax, ay = s, -s                                          # SVG y is down
    bx = gt_cent[i_gt, 0] - ax * svg_cent[i_svg, 0]
    by = gt_cent[i_gt, 1] - ay * svg_cent[i_svg, 1]

    match = {}
    for _ in range(n_iter):
        # greedy 1:1 matching on transformed centroid distance
        t_cent = np.stack([ax * svg_cent[:, 0] + bx,
                           ay * svg_cent[:, 1] + by], axis=1)
        dmat = np.linalg.norm(t_cent[:, None] - gt_cent[None], axis=2)
        order = np.dstack(np.unravel_index(
            np.argsort(dmat, axis=None), dmat.shape))[0]
        used_s, used_g, match = set(), set(), {}
        for i, j in order:
            if dmat[i, j] > 1.5 or i in used_s or j in used_g:
                continue
            used_s.add(i)
            used_g.add(j)
            match[names[j]] = int(i)
        # least-squares refit per axis on matched centroids
        si = np.asarray([match[n] for n in match])
        gi = np.asarray([names.index(n) for n in match])
        Ax = np.stack([svg_cent[si, 0], np.ones(len(si))], axis=1)
        Ay = np.stack([svg_cent[si, 1], np.ones(len(si))], axis=1)
        (ax, bx), _, _, _ = np.linalg.lstsq(Ax, gt_cent[gi, 0], rcond=None)
        (ay, by), _, _, _ = np.linalg.lstsq(Ay, gt_cent[gi, 1], rcond=None)

    return (float(ax), float(bx), float(ay), float(by)), match


def calibrate_inset(to_gt, svg_polys, gt_rooms, match, t_max=0.3):
    """Estimate the SVG->inner-surface inset (metres).

    The SVG draws rooms to the wall centerlines, so they run ~half a wall
    thickness larger on every side than room_layout.json's inner surfaces.
    For each matched room, solve buffer(-t) area == GT area by bisection and
    take the median t across rooms."""
    import numpy as np

    ts = []
    for name, i in match.items():
        poly, target = to_gt(svg_polys[i]), gt_rooms[name].area
        if poly.area <= target:
            ts.append(0.)
            continue
        lo, hi = 0., t_max
        for _ in range(40):
            mid = (lo + hi) / 2
            if poly.buffer(-mid).area > target:
                lo = mid
            else:
                hi = mid
        ts.append((lo + hi) / 2)
    return float(np.median(ts))


def recover_missing_rooms(svg_path: Path, layout_path: Path,
                          out_dir: Path) -> tuple[dict, dict, dict] | None:
    """Complete room_layout.json from the captured floor plan SVG.

    Writes ``out_dir/rooms_extra.json`` (metres, room_layout world x/z) and
    ``out_dir/rooms_centerline.json`` (the wall-**centerline** polygon of
    every room — layout + recovered — same frame, no inset applied), and
    returns (svg_record, rooms_extra_record, rooms_centerline_record), or
    ``None`` when the completion could not run."""
    try:
        import numpy as np
        from shapely.geometry import Point, Polygon
    except ImportError as exc:
        print(f"! {exc.name} not installed - skipping SVG room completion",
              file=sys.stderr)
        return None

    # GEOS emits spurious "invalid value" RuntimeWarnings on polygons with
    # collinear vertices (common in these SVG paths); the results are finite.
    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="shapely")

    svg_text = svg_path.read_text(encoding="utf-8")
    svg_polys = parse_svg_polygons(svg_text)
    labels = parse_svg_labels(svg_text)
    gt_rooms = load_layout_rooms(layout_path)
    print(f"  SVG polygons: {len(svg_polys)}, labels: {len(labels)}, "
          f"layout rooms: {len(gt_rooms)}")
    if len(svg_polys) < 3 or not gt_rooms:
        print("! too few polygons/rooms to fit - skipping", file=sys.stderr)
        return None

    (ax, bx, ay, by), match = fit_transform(svg_polys, gt_rooms)

    def to_gt(p):
        return Polygon(np.stack([ax * p[:, 0] + bx, ay * p[:, 1] + by],
                                axis=1)).buffer(0)

    # fit sanity: IoU between transformed SVG rooms and their layout twin
    ious = []
    for name, i in match.items():
        tp = to_gt(svg_polys[i])
        ious.append(tp.intersection(gt_rooms[name]).area /
                    tp.union(gt_rooms[name]).area)
    mean_iou = float(np.mean(ious)) if ious else 0.0
    print(f"  transform: x*{ax:.6f}{bx:+.3f}, y*{ay:.6f}{by:+.3f}  "
          f"(matched {len(match)} rooms, mean fit IoU {mean_iou:.3f})")
    if mean_iou < 0.6:
        print("! fit IoU too low - not writing rooms_extra.json", file=sys.stderr)
        return None

    inset = calibrate_inset(to_gt, svg_polys, gt_rooms, match)
    print(f"  calibrated inset {inset:.3f} m (half wall thickness)")

    def poly_coords(p):
        return [[round(x, 4), round(y, 4)] for x, y in p.exterior.coords[:-1]]

    # name leftover polygons by the room-name label falling inside them
    leftover_idx = [i for i in range(len(svg_polys))
                    if i not in set(match.values())]
    rooms = []
    leftover_names = []
    for n, i in enumerate(leftover_idx):
        raw = Polygon(svg_polys[i]).buffer(0)
        inside = [t for x, y, t in labels if raw.contains(Point(x, y))]
        fresh = [t for t in inside if t not in gt_rooms]
        name = (fresh or inside or [f"room_extra_{n + 1:02d}"])[0]
        if name.startswith("room_extra_"):
            print(f"  ! unnamed leftover polygon (area "
                  f"{to_gt(svg_polys[i]).area:.1f} m2) -> {name}", file=sys.stderr)
        leftover_names.append(name)
        poly = to_gt(svg_polys[i]).buffer(-inset).simplify(0.005)
        rooms.append({
            "name": name,
            "area_m2": round(poly.area, 2),
            "polygon": poly_coords(poly),
        })
    print(f"  recovered {len(rooms)} room(s): "
          f"{', '.join(r['name'] for r in rooms) or '-'}")

    # wall-centerline polygons of ALL rooms (layout + recovered): the SVG
    # polygon transformed to world metres, with NO inset applied
    def centerline_entry(name, source, svg_idx):
        cp = to_gt(svg_polys[svg_idx]).simplify(0.005)
        return {"name": name, "source": source,
                "area_m2": round(cp.area, 2), "polygon": poly_coords(cp)}

    centerline = [centerline_entry(n, "room_layout", i)
                  for n, i in match.items()]
    centerline += [centerline_entry(n, "rooms_extra", i)
                   for n, i in zip(leftover_names, leftover_idx)]
    # layout rooms the SVG match missed: approximate by outward inset buffer
    for name, poly in gt_rooms.items():
        if name in match:
            continue
        cp = poly.buffer(inset).simplify(0.005)
        centerline.append({"name": name, "source": "room_layout_buffered",
                           "area_m2": round(cp.area, 2),
                           "polygon": poly_coords(cp)})
        print(f"  ! {name} has no SVG match - centerline approximated by "
              f"buffer(+{inset:.3f})", file=sys.stderr)

    extra = {
        "_comment": [
            "由页面户型图 SVG 恢复的、room_layout.json 缺失的房间（米制，room_layout 世界系 x/z）。",
            "生成：python tools/fetch_realsee_floorplan.py；transform 为 SVG(mm,y向下)->world 的逐轴仿射。",
        ],
        "source_svg": svg_path.name,
        "transform": {"ax": ax, "bx": bx, "ay": ay, "by": by,
                      "inset_m": round(inset, 4),
                      "fit_mean_iou": round(mean_iou, 3)},
        "rooms": rooms,
    }
    (out_dir / "rooms_extra.json").write_text(
        json.dumps(extra, indent=1, ensure_ascii=False))

    center = {
        "_comment": [
            "全部房间（room_layout.json + rooms_extra.json）的墙体中线多边形（米制，room_layout 世界系 x/z）。",
            "SVG 户型图按墙体中线绘制；本文件为配准后的 SVG 多边形原样输出（未做 inset 内缩）。",
            "source: room_layout=与 room_layout.json 房间匹配的 SVG 多边形; rooms_extra=SVG 补全房间;",
            "        room_layout_buffered=SVG 未匹配到、由内表面向外 buffer(+inset) 近似。",
        ],
        "source_svg": svg_path.name,
        "transform": {"ax": ax, "bx": bx, "ay": ay, "by": by,
                      "inset_m": round(inset, 4),
                      "fit_mean_iou": round(mean_iou, 3)},
        "rooms": centerline,
    }
    (out_dir / "rooms_centerline.json").write_text(
        json.dumps(center, indent=1, ensure_ascii=False))
    print(f"  centerlines: {len(centerline)} room(s) -> rooms_centerline.json")

    svg_record = {
        "local_path": str(svg_path.relative_to(out_dir))
        if svg_path.is_relative_to(out_dir) else str(svg_path),
        "polygon_count": len(svg_polys),
        "label_count": len(labels),
    }
    extra_record = {
        "local_path": "rooms_extra.json",
        "recovered": [r["name"] for r in rooms],
        "fit_mean_iou": round(mean_iou, 3),
    }
    centerline_record = {
        "local_path": "rooms_centerline.json",
        "room_count": len(centerline),
    }
    return svg_record, extra_record, centerline_record


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="Realsee work URL")
    ap.add_argument("--out", default="data/floorplan", help="output directory")
    ap.add_argument("--svg", default=None, metavar="PATH",
                    help="reuse a saved floor plan SVG (skip the browser capture)")
    ap.add_argument("--no-svg", action="store_true",
                    help="skip the SVG room completion stage entirely")
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

    # 3. SVG room completion (recover rooms missing from room_layout.json).
    svg_record = extra_record = centerline_record = None
    if not args.no_svg:
        print("SVG room completion...")
        layout_path = out_dir / "room_layout.json"
        svg_path = Path(args.svg) if args.svg else None
        if svg_path is None:
            svg_path = capture_floorplan_svg(args.url, out_dir / "floorplan.svg")
        if svg_path is None or not svg_path.exists():
            print("! no floor plan SVG - skipping completion", file=sys.stderr)
        elif not layout_path.exists():
            print("! no room_layout.json - skipping completion", file=sys.stderr)
        else:
            result = recover_missing_rooms(svg_path, layout_path, out_dir)
            if result:
                svg_record, extra_record, centerline_record = result

    # 4. Manifest.
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
        "svg": svg_record,                       # captured floor plan SVG
        "rooms_extra": extra_record,             # rooms recovered from the SVG
        "rooms_centerline": centerline_record,   # centerline polygons, all rooms
    }
    (out_dir / "floorplan.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))

    n_img = (sum(1 for r in hierarchy if r["local_path"])
             + sum(1 for r in outline if r["local_path"])
             + (1 if standard and standard["local_path"] else 0))
    extra_note = (f", {len(extra_record['recovered'])} room(s) recovered from SVG"
                  if extra_record else "")
    print(f"\nDone. room_layout {'ok' if layout_record and layout_record['local_path'] else 'FAILED'}, "
          f"{n_img} image(s){extra_note} -> {out_dir}")
    print(f"Manifest: {out_dir / 'floorplan.json'}")


if __name__ == "__main__":
    main()
