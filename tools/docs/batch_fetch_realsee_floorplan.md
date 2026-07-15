# `batch_fetch_realsee_floorplan.py`

Batch driver for [`fetch_realsee_floorplan.py`](fetch_realsee_floorplan.md):
fetch many Realsee floor plans in one run, each into its own subfolder.

## Input CSV

Two columns per row — `scene_name`, `url` — e.g. `data/csv/batch_fetch.csv`:

```csv
xinghewan, https://open.realsee.com/ke/vwYQ.../...#lianjia
huizhongbeili-207, https://open.realsee.com/ke/6gyq.../...#lianjia
huizhongbeili-106, https://open.realsee.com/ke/BEy8.../...#lianjia
```

- A leading `scene_name,url` **header row is optional** (auto-detected).
- Blank rows and `#` comment rows are skipped.
- Whitespace around cells is trimmed; a malformed row (missing a column) aborts
  with a line number.
- `scene_name` becomes the output folder name — any character outside
  `[A-Za-z0-9._-]` is replaced with `_`, and path separators / `..` are rejected
  (each scene stays a single directory component).

## What it does

For each row it invokes `fetch_realsee_floorplan.py` **as a subprocess** with
`--out <out-root>/<scene_name>`, so every scene gets the full pipeline
(`room_layout.json` + images + SVG room completion + door/window recovery) and
one scene's failure never aborts the rest. Output layout:

```
data/floorplan/
  xinghewan/            room_layout.json, rooms_*.json, floorplan.svg, images/, floorplan.json …
  huizhongbeili-207/    … + floorplan_base.svg, doors_windows.json (scene had a base-image SVG)
  huizhongbeili-106/    …
```

A scene counts as **succeeded** only if the subprocess exits 0 **and** its
`floorplan.json` records a downloaded `room_layout.json`. The run ends with a
per-scene summary (rooms / recovered / 门窗洞 counts) and exits non-zero if any
scene failed.

## Usage

```bash
python tools/batch_fetch_realsee_floorplan.py                     # -> data/csv/batch_fetch.csv
python tools/batch_fetch_realsee_floorplan.py my_scenes.csv
python tools/batch_fetch_realsee_floorplan.py --out-root data/floorplan --no-svg
```

CLI flags: positional `csv` (default `data/csv/batch_fetch.csv`), `--out-root`
(default `data/floorplan`), `--no-svg` (forwarded to each fetch — skips SVG
completion + door/window recovery).

## Dependencies

Same as `fetch_realsee_floorplan.py`: the base scrape is stdlib-only; the SVG
completion + door/window recovery each scene runs need `numpy`, `shapely`,
`playwright` (+ chromium), skipped per scene with a warning when missing.
