# `build_panoramas.py`

Stitch the downloaded cube faces into **equirectangular panoramas** and convert
the per-face poses into `pano_camera.json` (one spherical-camera extrinsic per
point). Companion to `fetch_realsee_panoramas.py` — run that first.

## What it does

For each panorama point:
- `pano/point_XX.jpg` — one equirectangular (2:1) panorama stitched from the 6
  cube faces.
- `pano_camera.json` — one **panorama-camera extrinsic per point** (the
  spherical-camera counterpart of the per-face `transforms.json`).

## Usage

```bash
python tools/build_panoramas.py                          # data/panoramas, height 2048
python tools/build_panoramas.py --in data/panoramas --height 2048
```

CLI flags:
- `--in` — folder holding `images/` + `observers_raw.json` (default `data/panoramas`).
- `--height` — equirect height in px (width = 2×height; default 2048).

## Inputs / outputs

- Inputs: `<in>/images/point_XX_<face>.jpg` + `<in>/observers_raw.json`
  (both produced by `fetch_realsee_panoramas.py`).
- Outputs: `<in>/pano/point_XX.jpg` and `<in>/pano_camera.json`
  (`camera_model: "EQUIRECTANGULAR"`, `w`,`h`, per-frame `position` /
  `quaternion` / `transform_matrix = [R_quat | position]`).

## Conventions (important — why it does NOT reuse `FACE_LOCAL`)

- It imports `FACES` and `quat_to_matrix` from `fetch_realsee_panoramas` so the
  **pano extrinsic matches `transforms.json`'s quaternion handling**.
- It deliberately does **not** import `FACE_LOCAL`. That file negates `up`
  (a 180° roll) to suit the downstream pinhole/3DGS convention of
  `transforms.json`. The cube-face JPGs are stored **upright**, so the equirect
  must use the true geometric basis (front = −Z, **up = +Y**) or every
  horizontal face (f/b/l/r) comes out vertically flipped. Hence a separate
  `PANO_FACE_LOCAL` with `up = +Y`; up/down faces use the geometrically
  continuous roll.
- Equirect mapping: `lon` −π..π (left..right, 0 at centre), `lat` +π/2..−π/2
  (top..bottom); local ray `d = (cos lat sin lon, sin lat, −cos lat cos lon)`.
  Image centre = front, top = world-up.

## Dependencies

- `Pillow` + `numpy`.
