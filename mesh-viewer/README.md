# mesh-viewer

A three.js viewer for the **decoded Realsee mesh** (`data/model/exported/model.glb`),
with the *same* camera-trajectory recorder + playback as the splat dev server in
[`frontend/`](../frontend). It's a sibling of `frontend/`: where that views 3DGS `.ply`
splats, this views the textured triangle mesh. Recordings from both write to the same
FastAPI backend in the same `transforms.json` format, so they're interchangeable.

## What it does

- Loads `model.glb` and lets you free-navigate in one of two camera modes (toggle in the
  top-left panel; see **Navigation** below).
- **Record** a camera trajectory (Shift+R, or the panel button): poses are sampled at a
  fixed rate during navigation.
- On **Stop**, every sampled pose is re-rendered offscreen at the configured resolution
  and uploaded to the backend as **two PNGs** — the RGB screenshot (`frames/`) and an
  **opacity/coverage mask** (`opacity/`, mesh = foreground, background = black) — plus
  a `transforms.json`.
- **Opacity scale** (panel field, default `1.0`, range `0–1`): a global multiplier on
  the mask's foreground value, so `1.0` writes white `255` for covered pixels and e.g.
  `0.5` writes `127`. The background stays `0`.
- **Playback**: pick a saved session and replay the camera path over the mesh.
- **COLMAP export**: turn the selected saved session into a COLMAP sparse model +
  a 3DGS init cloud (see **COLMAP export** below).

The mesh and the panorama cameras share one metric, **Y-up, OpenGL/NeRF** frame (camera
looks −Z), and the mesh is loaded in its native world frame, so the exported
`transform_matrix` is simply the three.js `camera.matrixWorld` (no axis flip, no
splat-load-transform cancellation). See the repo `CLAUDE.md` for the format spec.

## Navigation

A top-left panel switches between two camera controllers (only one is active at a time;
**Fly is the default**). Recording/playback work in either mode.

- **Fly** — three.js `FlyControls` with its default key map:
  - **W / S** forward / back, **A / D** strafe, **R / F** up / down, **Q / E** roll,
    **arrow keys** pitch / yaw.
  - **Hold a mouse button and drag to look** (`dragToLook`); releasing frees the cursor
    to click the panels.
  - Move speed scales with the model size.
- **Orbit** — three.js `OrbitControls`: left-drag orbit, right-drag pan, wheel zoom.

Switching **Orbit → after flying** re-seats the orbit pivot in front of the camera, so
orbiting resumes around what you're looking at.

## COLMAP export

The recorder panel's **COLMAP export** section converts the **selected saved session**
(the one chosen in the Playback dropdown) into a COLMAP sparse model, so a recording
can seed a COLMAP / 3DGS training pipeline.

- **Points** — how many points to surface-sample from the mesh for `points3D.bin`
  (default `100000`).
- **Export COLMAP** — POSTs to the backend, which does the Python-only heavy lifting
  (mesh sampling with texture colours, pose conversion, occlusion-aware visibility via
  embree) and writes, under `data/output/<session>/`:

  ```
  sparse/0/cameras.bin     1 shared SIMPLE_PINHOLE camera ([f, cx, cy])
  sparse/0/images.bin      one image per frame: W2C extrinsics + 2D↔3D correspondences
  sparse/0/points3D.bin    the sampled cloud: xyz + RGB + per-point tracks
  init_3dgs.ply            3DGS asset (same point set), binary_little_endian
  ```

  Image `name`s are `frames/frame_NNNNN.png` (generated from frame order, matching the
  recorded RGB files), so the COLMAP **image root** is the session folder. It runs
  **synchronously** — the panel shows `converting…` then a summary; large sessions can
  take a minute or two.

The conversion reuses `tools/mesh_to_colmap_core.py` (shared with the standalone CLI
`tools/mesh_to_colmap_3dgs.py`). The mesh sampled is `data/model/exported/model.obj`.
Requires the backend's Python env to have `trimesh`, `embreex`, `scipy` (the repo's
`artifixer` conda env does). `transform_matrix` is OpenGL/NeRF **C2W**; COLMAP stores
OpenCV **W2C**, so poses are flipped by `diag(1,−1,−1)` and inverted — see
`tools/mesh_to_colmap_3dgs.md`.

## Prerequisites

1. **The model exists.** Produce `data/model/exported/model.glb`:
   ```bash
   python tools/fetch_realsee_model.py          # downloads the raw .at3d + textures
   cd tools/model-extractor && npm install && npx playwright install chromium
   node extract.mjs --format glb                # decodes .at3d -> model.glb
   ```
2. **The backend is running** (receives recordings):
   ```bash
   cd backend && uvicorn app:app --reload --host 0.0.0.0 --port 8000
   ```

## Run

```bash
cd mesh-viewer
npm install
npm run dev
```

Open <http://localhost:5173/>. The model is served by a small dev-server middleware
(see `vite.config.ts`) that maps `/model/*` → `../data/model/exported/*`.

### Query params

- `?model=<url>` — load a different model (default `/model/model.glb`). Any URL that
  `GLTFLoader` accepts works.
- `?backend=<url>` — backend base URL for recordings (default `http://localhost:8000`).

## Output

Recordings land under `data/output/<session>/` (shared with the splat viewer):

```
frames/frame_NNNNN.png    RGB screenshot (upright)
opacity/frame_NNNNN.png   coverage mask (mesh foreground on black, upright; foreground = 255 × opacity scale)
transforms.json           intrinsics + per-frame transform_matrix (C2W, OpenGL/NeRF)
```

## Notes

- `npm run build` does a type-check (`tsc --noEmit`) + `vite build`.
- three.js is pinned via npm (`three@0.160.x`), matching the `tools/model-extractor/`
  renderers (which use the same version from a CDN import map).
- PNG orientation: WebGL framebuffer reads are bottom-up, so `src/png.ts` flips **once**
  into a top-down image before encoding — keeping the saved frames upright.
