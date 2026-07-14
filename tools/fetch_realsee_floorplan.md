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
   (see below), writing them to `rooms_extra.json`.
4. Writes a `floorplan.json` manifest (source URLs + local paths + checksums + a
   room-name/count summary + the SVG/rooms_extra records).

## SVG room completion

`room_layout.json` can miss rooms (this scene: 卫生间A / 衣帽间A / 阳台C — 18
named rooms vs the 21 the rendered PNG shows). The 户型图 tab, however, draws
the floor plan as an **inline SVG** in which *every* room is one `<path>`
polygon (units mm, y down), with the room names as HTML overlays on top. The
completion stage:

1. **Capture** — headless Chromium (Playwright) opens the page, clicks the
   户型图 tab, and dumps the `<svg>` holding the room polygons (chosen as the
   SVG with the most `M/L/Z`-only paths). The overlay room names are mapped
   into SVG user coordinates (`getScreenCTM().inverse()`) and baked into the
   saved file as `<text>` elements → `floorplan.svg` is self-contained.
2. **Registration** — fits the SVG→world transform (per-axis scale + offset,
   y flipped) by greedily matching SVG polygons to the known
   `room_layout.json` rooms on centroid distance and least-squares refining
   (algorithm from `svg_rooms_to_gt.py`). The mean fit IoU is reported
   (~0.85 here); below 0.6 the stage aborts rather than emit garbage.
3. **Recovery** — each unmatched SVG polygon is named by the room-name
   `<text>` that falls inside it, inset by the calibrated half wall thickness
   (the SVG draws rooms to wall *centerlines*, `room_layout.json` to *inner
   surfaces*; solved per matched room by area bisection, median taken), and
   written to `rooms_extra.json` in **metres, in `room_layout.json`'s world
   x/z frame** — directly comparable/mergeable with the layout rooms.

Result on the reference scene: recovers 卫生间A (4.77㎡ vs 4.9 on the PNG),
衣帽间A (4.37 vs 4.4), 阳台C (1.4 vs 1.8 — curved bay balcony, arc
approximation). A polygon with no label inside is emitted as `room_extra_NN`
with a warning.

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
floorplan.svg                         captured room SVG (+ injected <text> room names)
images/hierarchy_floor_plan_0.png     detailed rendered floor plan (matches the 户型图 tab)
images/outline_floor_plan_0.png       radar-minimap outline (matches the 漫游 tab)
images/standard_floor_plan.png        only when the page provides one (often absent)
```

Downloaded image files are integrity-checked against the `checksum` (MD5) each
image entry carries in the page.

## Dependencies

- Base scrape: Python stdlib only (`urllib`) — no external packages.
- SVG room completion only: `numpy`, `shapely`, `playwright` (+ its chromium),
  e.g. `conda install -n artifixer shapely && conda run -n artifixer pip
  install playwright && conda run -n artifixer playwright install chromium`.
  All are lazy-imported; when missing the stage is skipped with a warning and
  the base scrape still succeeds.

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
