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
  and uploaded to the backend as **two PNGs** — the RGB screenshot (`frames/`) and a
  **binary opacity/coverage mask** (`opacity/`, mesh = white, background = black) — plus
  a `transforms.json`.
- **Playback**: pick a saved session and replay the camera path over the mesh.

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
opacity/frame_NNNNN.png   binary coverage mask (white mesh on black, upright)
transforms.json           intrinsics + per-frame transform_matrix (C2W, OpenGL/NeRF)
```

## Notes

- `npm run build` does a type-check (`tsc --noEmit`) + `vite build`.
- three.js is pinned via npm (`three@0.160.x`), matching the `tools/model-extractor/`
  renderers (which use the same version from a CDN import map).
- PNG orientation: WebGL framebuffer reads are bottom-up, so `src/png.ts` flips **once**
  into a top-down image before encoding — keeping the saved frames upright.
