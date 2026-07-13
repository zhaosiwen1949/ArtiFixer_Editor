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
3. Writes a `floorplan.json` manifest (source URLs + local paths + checksums + a
   room-name/count summary).

## Usage

```bash
python tools/fetch_realsee_floorplan.py                       # -> data/floorplan
python tools/fetch_realsee_floorplan.py "<work-url>" --out data/floorplan
```

CLI flags: positional `url` (default reference scene), `--out` (default
`data/floorplan`).

## Outputs (under `--out`)

```
floorplan.json                        manifest (summary + URLs + local paths + checksums)
room_layout.json                      structured per-room wall geometry (35 rooms)
images/hierarchy_floor_plan_0.png     detailed rendered floor plan (matches the 户型图 tab)
images/outline_floor_plan_0.png       radar-minimap outline (matches the 漫游 tab)
images/standard_floor_plan.png        only when the page provides one (often absent)
```

Downloaded image files are integrity-checked against the `checksum` (MD5) each
image entry carries in the page.

## Dependencies

- Python stdlib only (`urllib`) — no external packages.

## Notes

- `room_layout.json` coordinates are in the same metric Realsee world frame as the
  panoramas/mesh (`fetch_realsee_panoramas.py`, `fetch_realsee_model.py`), keyed by
  `panoIndex` (the panorama point a room's lines belong to). `y` is roughly the
  wall height; the plan shape lives in `x`/`z`.
- The rendered `hierarchy_floor_plan` PNG has the room labels, areas and wall
  dimensions **baked into the pixels** — that text is not present as JSON in the
  page. Use `room_layout.json` for the machine-readable geometry.
