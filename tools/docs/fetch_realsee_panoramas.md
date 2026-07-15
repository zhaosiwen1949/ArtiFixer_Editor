# `fetch_realsee_panoramas.py`

Download the **cube-map panoramas** from a Realsee (如视 / 贝壳VR) work page and
generate a NeRF/3DGS-style `transforms.json` from the embedded camera poses.

## What it does

1. Loads the work-page HTML and discovers every panorama point. Each point is a
   **cube map** of 6 JPG faces (`f`/`b`/`l`/`r`/`u`/`d` = front/back/left/right/up/down)
   served from a CDN path like
   `…/auto3dhd/<work>/images/cube_<res>/<index>/<hash>/<index>_<face>.jpg`
   (matched by `CUBE_RE`).
2. Downloads all faces in parallel.
3. Parses the embedded `observers` array (per-point `index`, `position` [x,y,z],
   `quaternion` {w,x,y,z}) and emits a per-face pinhole `transforms.json`.

## Usage

```bash
python tools/fetch_realsee_panoramas.py "<work-url>"
python tools/fetch_realsee_panoramas.py "<work-url>" --out data/panoramas --equirect
```

CLI flags:
- positional `url` — work URL (defaults to the project's reference scene).
- `--out` — output dir (default `data/panoramas`).
- `--workers` — parallel downloads (default 8).
- `--equirect` — also stitch each cube into one equirectangular panorama (needs Pillow+numpy).
- `--no-transforms` — skip `transforms.json`.
- `--faces` — comma list of faces to emit as frames (default all 6; e.g. `f,b,l,r`).

## Outputs (under `--out`)

- `point_XX/<face>.jpg` — per-point cube faces.
- `images/point_XX_<face>.jpg` — **flat summary folder** mirroring every face in one place.
- `transforms.json` — per-face pinhole frames (`file_path` = `images/point_XX_<face>.jpg`, `transform_matrix` = 4×4 OpenGL/NeRF C2W).
- `observers_raw.json` — raw poses dump (debug / re-deriving the convention).
- `equirect/point_XX.jpg` — only with `--equirect`.

## Conventions (important)

- Realsee's viewer (`@realsee/five`, Three.js) uses the **same camera frame as
  OpenGL/NeRF** (+X right, +Y up, looks −Z), so **no axis flip** is applied
  going Realsee-world → `transform_matrix`.
- Each cube face is a **90° pinhole** sharing the observer's optical centre.
  Intrinsics: square, `fl = size/2`, `cx = cy = size/2`, no distortion.
- `FACE_LOCAL` gives each face's `(forward, up)` in the panorama-local frame.
  The `up` vectors are **negated** (a 180° roll) — without it the rendered views
  came out rotated 180° (upside-down + left-right reversed). `det` stays +1 (no
  mirror).
- `INVERT_QUATERNION = False` — flip if the stored quaternion is world→local.

## This file is the convention source of truth

`build_panoramas.py` imports `FACES` and `quat_to_matrix` from here (but **not**
`FACE_LOCAL` — see that tool's doc for why). Keep the quaternion handling here.

## Dependencies

- `requests` (imported lazily, only for network calls).
- `--equirect` additionally needs `Pillow` + `numpy`.
