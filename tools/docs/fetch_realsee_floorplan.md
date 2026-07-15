# `fetch_realsee_floorplan.py`

Download the **floor plan (户型图)** — its structured room data **and** its rendered
images — referenced by a Realsee work page. The assets behind the "户型图" tab and
the 漫游 tab's radar minimap.

## What it does

1. Loads the work-page HTML and extracts every floor plan reference:
   - `objectsInUrl.ruler` → a **`room_layout.json`** URL: the structured floor
     plan, one entry per room with `panoIndex`, `roomName` (客厅 / 卧室A / 厨房 …)
     and the 3D wall `lines` (each a `start`/`end` segment; door/window openings
     are the split sub-segments under `children`, `state:false` = opening).
   - `hierarchy_floor_plan[]` → the **detailed rendered** floor plan PNG (room
     names + areas + wall dimensions — the big "户型图" image).
   - `outline_floor_plan[]` → the **outline** PNG used in the 漫游 radar minimap.
   - `standard_floor_plan_url` → an optional "standard" floor plan image (usually
     empty for auto-generated works).
   - `house_layout` → room counts (`bedroom_amount` / `parlor_amount` /
     `cookroom_amount` / `toilet_amount`), plus the listing 户型 (`3室2厅`) and
     面积 (`274.49㎡`).
2. Downloads `room_layout.json` and every floor plan image.
3. **SVG room completion** — recovers the rooms `room_layout.json` misses
   (see below), writing them to `rooms_extra.json` + `rooms_centerline.json`.
4. **Door/window recovery** — reads the base-image line-drawing SVG, classifies
   each door/window/门洞 symbol and snaps it onto the centerlines
   (`doors_windows.json`; see below).
5. Writes a `floorplan.json` manifest (source URLs + local paths + checksums + a
   room-name/count summary + the SVG/rooms_extra/centerline/doors_windows records).

## SVG room completion

`room_layout.json` can miss rooms (this scene: 卫生间A / 衣帽间A / 阳台C — 18
named rooms vs the 21 the rendered PNG shows). The 户型图 tab, however, draws
the floor plan as an **inline SVG** in which *every* room is one `<path>`
polygon (units mm, y down), with the room names as HTML overlays on top. The
completion stage:

1. **Capture** — headless Chromium (Playwright) opens the page, clicks the
   户型图 tab, and dumps the `<svg>` under `.floorplan-plugin__room-highlight`
   — the floor plan layer with exactly one `<path>` per room (an error is
   reported when the element is absent; there is no fallback). The overlay
   room names are mapped into SVG user coordinates
   (`getScreenCTM().inverse()`) and baked into the saved file as `<text>`
   elements → `floorplan.svg` is self-contained.
2. **Registration** — fits the SVG→world transform (per-axis scale + offset,
   y flipped) by greedily matching SVG polygons to the known
   `room_layout.json` rooms on centroid distance and least-squares refining
   (algorithm from `svg_rooms_to_gt.py`). The seed pairs a largest SVG
   polygon with a largest layout room; since the area ranking can differ
   between the two sides, all top-3 × top-3 pairings are tried and the fit
   matching the most rooms wins. The mean fit IoU is reported (~0.85 here);
   below 0.6 the stage aborts rather than emit garbage.
3. **Recovery** — each unmatched SVG polygon is named by the room-name
   `<text>` that falls inside it, inset by the calibrated half wall thickness
   (the SVG draws rooms to wall *centerlines*, `room_layout.json` to *inner
   surfaces*; solved per matched room by area bisection, median taken), and
   written to `rooms_extra.json` in **metres, in `room_layout.json`'s world
   x/z frame** — directly comparable/mergeable with the layout rooms.
4. **Centerlines** — the wall-centerline polygon of **every** room (the 18
   layout rooms + the recovered ones = one entry per SVG room path) is written
   to `rooms_centerline.json`: the registered SVG polygons in the same world
   x/z metre frame, with **no inset applied**. Each entry carries `source`
   (`room_layout` = matched a layout room, `rooms_extra` = recovered room,
   `room_layout_buffered` = layout room the SVG missed, approximated by
   `buffer(+inset)` of its inner polygon).

Result on the reference scene: recovers 卫生间A (4.77㎡ vs 4.9 on the PNG),
衣帽间A (4.37 vs 4.4), 阳台C (1.4 vs 1.8 — curved bay balcony, arc
approximation). A polygon with no label inside is emitted as `room_extra_NN`
with a warning. Self-intersecting paths whose cleanup yields a MultiPolygon
keep their largest part.

## Door/window recovery

The 户型图 tab has a **second** inline SVG — the `.floorplan-plugin__base-image`
layer — that is the full line drawing (walls + door/window symbols). It is saved
verbatim as `floorplan_base.svg` during the same capture. In it, every
door/window is a `<use href="#lineItem-defs-N">` of a **reusable symbol** placed
by an outer `translate(cx,cy) rotate(θ)` transform; the SVG's `<defs>` hold a
fixed **24-symbol library** (Realsee ships all 24; a scene instantiates only the
ones it needs). The recovery stage:

1. **Parse** — walks the `lineItemGroup`, composes each symbol's full transform
   chain down to the `<use>`, and computes its footprint (the def's bbox
   corners, in base-image coordinates). `LINEITEM_DEFS` maps every def index to
   `door` / `window` / `opening` (门洞/垭口) / `ignore`. **defs-22** (a
   radiator/low-cabinet-like glyph, not an opening) is excluded. A rendered
   gallery of all 24 symbols with the classification is at
   [`defs_gallery.html`](defs_gallery.html).
2. **Register** — the base-image frame (~25 mm/unit) is a *different* frame from
   the `room-highlight` SVG (~1 mm/unit), so it is fitted to the world metric
   frame independently: its `type="area"` room polygons are matched to the
   (already world-frame) `rooms_centerline.json` rooms with the same multi-seed
   per-axis affine fit (both y-signs tried). Mean IoU ~0.97 here; below 0.6 the
   stage aborts.
3. **Snap** — each symbol's footprint is projected onto the nearest centerline
   edge; the along-edge extent becomes the opening's sub-segment. Output
   `doors_windows.json`: per opening `type` (door/window/opening), `subtype`
   (符号细分, e.g. 单开门/飘窗), `defs` (library index), `room` + `edge` it snaps
   to, the occupied `segment` and normalized `t=[start,end]` along that edge,
   `width_m`, and `edge_dist_m` (snap residual, typically < 0.1 m).

Result on the new scene (8 rooms): 5 doors, 6 windows, 3 门洞; door widths
0.71–0.82 m, windows 0.87–3.16 m. This stage needs `floorplan_base.svg` to be
present — scenes whose 户型图 renders as a **raster background image** (no
base-image SVG) have no vector symbols to decode and are skipped with a warning
(see *Notes*).

## Usage

```bash
python tools/fetch_realsee_floorplan.py                       # -> data/floorplan
python tools/fetch_realsee_floorplan.py "<work-url>" --out data/floorplan
python tools/fetch_realsee_floorplan.py --svg data/floorplan/floorplan.svg   # reuse saved SVG (offline)
python tools/fetch_realsee_floorplan.py --no-svg                             # skip the completion stage
```

CLI flags: positional `url` (default reference scene), `--out` (default
`data/floorplan`), `--svg PATH` (reuse a saved SVG, no browser), `--no-svg`
(disable the completion stage).

## Outputs (under `--out`)

```
floorplan.json                        manifest (summary + URLs + local paths + checksums)
room_layout.json                      structured per-room wall geometry (35 rooms)
rooms_extra.json                      rooms recovered from the SVG (metres, world x/z)
rooms_centerline.json                 wall-centerline polygons of ALL rooms (metres, world x/z, no inset)
doors_windows.json                    door/window/门洞 openings snapped onto the centerlines (metres)
floorplan.svg                         captured room-highlight SVG (+ injected <text> room names)
floorplan_base.svg                    captured base-image line drawing (walls + door/window symbols)
images/hierarchy_floor_plan_0.png     detailed rendered floor plan (matches the 户型图 tab)
images/outline_floor_plan_0.png       radar-minimap outline (matches the 漫游 tab)
images/standard_floor_plan.png        only when the page provides one (often absent)
```

Downloaded image files are integrity-checked against the `checksum` (MD5) each
image entry carries in the page.

## Dependencies

- Base scrape: Python stdlib only (`urllib`) — no external packages.
- SVG room completion **and** door/window recovery: `numpy`, `shapely`,
  `playwright` (+ its chromium), e.g. `conda install -n artifixer shapely &&
  conda run -n artifixer pip install playwright && conda run -n artifixer
  playwright install chromium`. All are lazy-imported; when missing the stage is
  skipped with a warning and the base scrape still succeeds.

## Notes

- `room_layout.json` coordinates are in the same metric Realsee world frame as the
  panoramas/mesh (`fetch_realsee_panoramas.py`, `fetch_realsee_model.py`), keyed by
  `panoIndex` (the panorama point a room's lines belong to). `y` is roughly the
  wall height; the plan shape lives in `x`/`z`.
- The rendered `hierarchy_floor_plan` PNG has the room labels, areas and wall
  dimensions **baked into the pixels** — that text is not present as JSON in the
  page. Use `room_layout.json` (+ `rooms_extra.json`) for the machine-readable
  geometry.
- `rooms_extra.json` polygons are in the same world x/z frame (metres) as the
  `room_layout.json` rooms, already inset to inner surfaces — merge the two for
  the complete room set. Its `transform` records the fitted SVG(mm, y-down) →
  world affine plus `inset_m` and `fit_mean_iou`.
- `rooms_centerline.json` is the *centerline* counterpart: one polygon per room
  for the **complete** room set (layout + recovered), un-inset — centerline
  areas run larger than the inner-surface ones by ~half a wall thickness per
  side (e.g. 卫生间A 5.66㎡ centerline vs 4.77㎡ inner). Same `transform` block
  as `rooms_extra.json`.
- `doors_windows.json` lists each opening snapped onto a `rooms_centerline.json`
  edge. An opening is on a *shared* wall, so it is recorded once on whichever
  room's edge is nearest — its `segment` is the same physical wall either way.
  The `defs` index links back to the symbol library
  ([`defs_gallery.html`](defs_gallery.html)); `subtype` is a best-effort name
  from that gallery (doors/windows are cross-checked against on-plan placement;
  the finer window variants are labelled by symbol shape).
- **No-SVG scenes**: some works render the 户型图 as a flat **raster background
  image** with no `.floorplan-plugin__base-image` SVG (e.g. the reference scene
  `vwYQ…`). There are then *no vector door/window symbols to decode* — the stage
  is skipped and `doors_windows.json` is not written. Detecting the openings by
  template-matching the raster against the def glyphs was considered but is
  unreliable (the raster carries baked-in text/dimensions/furniture at unknown
  scale, and the door/window marks are small and embedded in the wall strokes),
  so it is deliberately **not** attempted. `room_layout.json` still marks where
  the openings are, without the door-vs-window type: a wall `lines[i]` is split
  into `children` sub-segments and the ones with `state:false` are the openings
  (door/window gaps) — use those when the SVG is absent.
