# CLAUDE.md

This file gives guidance to Claude Code (and humans) when working in this repository.

## Purpose

A tool to **view 3D Gaussian Splatting (3DGS) `.ply` scenes in the browser and record camera movement trajectories**. The recorded trajectory (camera poses + per-frame screenshots) is exported in a format suitable for downstream 3D reconstruction / training pipelines (NeRF / 3DGS style `transforms.json`).

## Architecture

- **Frontend (`frontend/`)** — a fork of [playcanvas/supersplat](https://github.com/playcanvas/supersplat) (MIT, TypeScript, PlayCanvas engine). We reuse its splat loading, camera, offscreen render-to-PNG, and UI, and add a **trajectory recorder** (`frontend/src/trajectory-recorder.ts` + a UI panel). The scene is auto-loaded via SuperSplat's `?load=<url>` query param, pointed at the backend's `.ply` URL.
- **Mesh viewer (`mesh-viewer/`)** — a sibling **three.js + Vite + TypeScript** app that views the *decoded mesh* instead of splats: it loads `data/model/exported/model.glb` (`GLTFLoader`) and ports the same recorder/playback (`mesh-viewer/src/trajectory-recorder.ts`). Navigation has two selectable modes (top-left toggle, default **Fly**): **Fly** (`FlyControls`, default keys — W/S/A/D, R/F up-down, Q/E roll, arrows look, drag-to-look) and **Orbit** (`OrbitControls`); recording/playback work in either. It reuses the **same backend** and the **same `transforms.json` format**, so its recordings are interchangeable with the splat ones. Differences from `frontend/`: the mesh loads in its **native world frame**, so the exported C2W is simply `camera.matrixWorld` (no splat-load-transform cancellation); the opacity PNG is a **coverage/silhouette mask** (mesh = foreground, background = black) rather than a grayscale alpha, with a UI **opacity scale** (default `1.0`, range `0–1`) that sets the foreground value to `round(255 × scale)`; intrinsics come from three's **vertical** FOV; PNGs are encoded via a 2D canvas (`src/png.ts`), flipped **once** from the bottom-up WebGL read. Vite serves the model at `/model/*` from `../data/model/exported/`. A **COLMAP export** panel button converts the selected saved session into `data/output/<session>/sparse/0/{cameras,images,points3D}.bin` (`camera_model` **SIMPLE_PINHOLE**) + `init_3dgs.ply`, via the backend endpoint `POST /api/recordings/<session>/colmap` (`{num_points}`, default 100k), which reuses `tools/mesh_to_colmap_core.py` — occlusion-aware, image names `frames/frame_NNNNN.png` from frame order, mesh sampled from `model.obj`. See `mesh-viewer/README.md`.
- **Backend (`backend/`)** — Python / FastAPI. Serves the frontend + streams the `.ply` (range-request friendly for large files), and receives recorded trajectories (poses + screenshots) from **either** viewer, writing them to disk under `data/output/<session>/`. CORS allows both dev servers (`:3000` for `frontend/`, `:5173` for `mesh-viewer/`).
- **`data/`** — gitignored. Holds input scenes (`export_last.ply`) and recorded outputs.

## Recording workflow

Real-time continuous recording. The user free-navigates the scene, clicks **Start** (or presses **Shift+R**), navigates, then **Stop** (**Shift+R** again toggles it off):
- While recording, camera poses are sampled at a fixed rate (**default 30 fps**) — only poses are stored.
- On **Stop**, each sampled pose is re-rendered offscreen at the configured resolution (**default 960×540**) and uploaded to the backend, then a `transforms.json` is written. Per pose, **two PNGs** are saved: the RGB screenshot under `frames/frame_NNNNN.png` and a **grayscale opacity map** (the render's alpha channel = splat coverage) under `opacity/frame_NNNNN.png`. The PNGs are matched to frames by their `frames/frame_NNNNN.png` ordering — `transforms.json` `frames[i]` carries only `transform_matrix` (no `file_path`/`mask_path`). The session folder is named with the local creation timestamp, `YYYYMMDD_HHMMSS`. The resolution also drives the intrinsics (`w`, `h`, `fl_x`, `fl_y`, `cx`, `cy`).

## Playback workflow

The recorder panel also plays back a saved trajectory in the editor:
- The **Playback** section lists saved sessions (fetched from `GET /api/recordings`); pick one and click **▶ Play**.
- The selected session's `transforms.json` is fetched (`GET /api/recordings/<session>/transforms`); each stored C2W matrix is converted back to a PlayCanvas camera pose (the inverse of the record-time conversion: re-apply `splatEntity.worldTransform`, take the translation as the camera position and the local −Z as the look direction), and the camera is driven through the frames over time at the panel FPS, interpolating between consecutive poses (`camera.setPose(position, target, 0)` each frame).

## Trajectory format spec (`transforms.json`)

The output must match the reference `data/transforms.json`. Top-level keys:

| Field | Meaning |
|---|---|
| `camera_model` | `"OPENCV"` |
| `fl_x`, `fl_y` | focal length in pixels (square pixels → equal) |
| `cx`, `cy` | principal point (image center) |
| `w`, `h` | image width / height in pixels |
| `k1`, `k2`, `p1`, `p2` | distortion (all `0` — pinhole) |
| `frames` | array of per-frame entries |

Each `frames[i]`:
- `file_path` — relative path to the screenshot, e.g. `images/frame_00001.png`.
- `transform_matrix` — a **4×4 OpenCV camera-to-world (C2W) matrix**, stored **row-major** (the array's elements are the matrix rows), last row `[0, 0, 0, 1]`.

Intrinsics are derived from render resolution + camera vertical FOV:
- `cx = w/2`, `cy = h/2`
- `fl_y = (h/2) / tan(fov_v/2)`, `fl_x = fl_y`
- `k1 = k2 = p1 = p2 = 0`

## Coordinate conversion (PlayCanvas → OpenGL/NeRF C2W) — IMPORTANT

The `transform_matrix` uses the **OpenGL / NeRF camera-to-world convention**: right-handed, **+X right, +Y up, camera looks −Z**. This is *the same convention PlayCanvas cameras use*, so **no axis flip is applied**. (The top-level `camera_model: "OPENCV"` refers only to the intrinsics + distortion model — `fl_*`, `cx`, `cy`, `k1..p2` — **not** to the extrinsic axis convention. Do **not** right-multiply by `diag(1,−1,−1,1)`; doing so flips +Y/up and renders every view upside-down.)

SuperSplat applies a transform to the splat entity when loading a `.ply`. To produce a C2W matrix in the `.ply`'s native world frame:

1. Express the camera pose in the **splat/PLY frame** to cancel SuperSplat's load transform:
   `C2W = inverse(splatEntity.worldTransform) · camera.worldTransform`
2. Emit as a row-major 4×4 array with last row `[0, 0, 0, 1]`.

**Validation:** `data/transforms.json` pairs with `data/export_last.ply` (its frames are the cameras that produced the splat). Placing a camera at one of those poses should render an upright, well-framed view — use this to confirm the conversion is correct (an upside-down result usually means an erroneous +Y flip was applied).

## Tools (`tools/`) — Realsee scene extraction

Standalone scripts that pull assets out of a Realsee (如视 / 贝壳VR) web tour
(panoramas, camera poses, and the textured 3D model). They are independent of the
viewer/backend; each has a sibling `.md` with full details. Default work URL and
output dirs point at the project's reference scene (`data/` is gitignored).

| Tool | Purpose | Key output | Details |
|---|---|---|---|
| `fetch_realsee_panoramas.py` | Download cube-map panoramas (6 faces/point) + emit per-face pinhole `transforms.json` from embedded `observers` poses | `data/panoramas/{point_XX/, images/, transforms.json, observers_raw.json}` | [doc](tools/docs/fetch_realsee_panoramas.md) |
| `build_panoramas.py` | Stitch the cube faces into equirectangular panoramas + per-point spherical-camera extrinsics | `data/panoramas/{pano/, pano_camera.json}` | [doc](tools/docs/build_panoramas.md) |
| `fetch_realsee_model.py` | Download the textured 3D model: proprietary `.at3d` mesh + texture atlases + manifest | `data/model/{model/*.at3d, materials/, model.json}` | [doc](tools/docs/fetch_realsee_model.md) |
| `fetch_realsee_floorplan.py` | Download the floor plan (户型图): structured `room_layout.json` (per-room names + 3D wall lines) + rendered floor plan / outline PNGs + `house_layout` summary. Also **completes missing rooms** from the page's inline floor-plan SVG (Playwright capture of room `<path>`s + overlay labels → affine fit to the layout frame → `rooms_extra.json`, metres, world x/z; needs numpy/shapely/playwright, else skipped), emits the wall-**centerline** polygons of all rooms (layout + recovered, no inset) as `rooms_centerline.json`, and **recovers door/window positions** from the base-image line drawing (`.floorplan-plugin__base-image` SVG → `floorplan_base.svg`; each opening is a `<use>` of a `lineItem-defs-N` symbol) — classified door/window/门洞, registered to the world frame, snapped onto the centerlines as `doors_windows.json` | `data/floorplan/{room_layout.json, rooms_extra.json, rooms_centerline.json, doors_windows.json, floorplan.svg, floorplan_base.svg, images/, floorplan.json}` | [doc](tools/docs/fetch_realsee_floorplan.md) |
| `model-extractor/` (Node) | Load the work in headless Chromium, let `@realsee/five` decode the `.at3d`, export standard **OBJ+MTL** or **GLB/glTF** (`--format obj\|glb\|gltf\|all`) | `data/model/exported/{model.obj+mtl, model.glb, model.gltf, materials/, preview*.png}` | [README](tools/model-extractor/README.md) |
| `check_alignment.py` (+ `model-extractor/align-overlay.mjs`) | Verify the exported mesh and the panorama camera files share one frame (numeric verdict + camera-on-mesh overlay renders) | `data/model/exported/{align_top.png, align_birdseye.png}` | [doc](tools/docs/check_alignment.md) |
| `mesh_to_colmap_3dgs.py` | Surface-sample the mesh → 3DGS init asset + COLMAP sparse model (poses from `transforms.json`, C2W→W2C, occlusion-aware 2D↔3D, texture colours). Thin CLI over `mesh_to_colmap_core.py` | `data/colmap/{sparse/0/{cameras,images,points3D}.bin, init_3dgs.ply}` | [doc](tools/docs/mesh_to_colmap_3dgs.md) |
| `mesh_to_colmap_core.py` | Shared conversion core (mesh sampling, C2W→W2C, occlusion visibility, COLMAP/PLY binary writers, `convert_trajectory_to_colmap()`) reused by the CLI **and** the backend's mesh-viewer COLMAP export. Supports `simple_pinhole`/`pinhole`/`opencv` | — (library) | — |

Pipelines (run in order):
- **Panoramas + poses:** `fetch_realsee_panoramas.py` → `build_panoramas.py`.
- **3D mesh:** `fetch_realsee_model.py` (raw `.at3d` + textures) → `model-extractor/` (decode `.at3d` to OBJ; binds `materialIndex i → texture_i.jpg`).
- **3DGS init:** the extracted mesh + `transforms.json` poses → `mesh_to_colmap_3dgs.py` (sampled cloud → 3DGS `.ply` + COLMAP `cameras/images/points3D.bin`). Verify frames first with `check_alignment.py`.

Convention notes that bite: Realsee's viewer shares the OpenGL/NeRF camera frame
(no axis flip → `transform_matrix`); panorama `up` is **negated** for
`transforms.json` (180° roll) but the equirect stitcher uses a separate upright
`up = +Y` basis — see the per-tool docs before changing any face/quaternion math.

Per-tool docs (one `.md` per script) live in **`tools/docs/`**.

### Floor-plan door/window symbols (`lineItem-defs`)

The 户型图 `base-image` SVG draws each door/window as `<use href="#lineItem-defs-N">`
of a **fixed 24-symbol library** Realsee ships in the SVG's `<defs>`; a scene
instantiates only the symbols it uses (the outer `<g transform="translate(cx,cy)
rotate(θ)…">` positions each one). `fetch_realsee_floorplan.py`'s
`LINEITEM_DEFS` classifies every index; a rendered gallery of all 24 (with the
classification) is at **`tools/docs/defs_gallery.html`**. The mapping:

| Category | defs indices | Notes |
|---|---|---|
| **door** 门 | 0 单开门, 1 推拉门, 2/3 双开门, 19 折叠门, 21 组合门, 23 弹簧门 | 0/16/... verified by on-plan placement; 0 = single swing (90° arc) |
| **window** 窗 | 5 平窗, 7 飘窗(矩形凸窗), 6/14/15 弧形窗/飘窗, 9/10/11 矩形飘窗, 4/8/12/13/17/18 平窗/固定窗变体 | 5/7 verified |
| **opening** 门洞/垭口 | 16, 20 | no leaf — just wall jambs; 16 verified |
| **excluded** | 22 | 密集横线 (暖气片/矮柜-like) — **confirmed not a door/window**, ignored |

Positions are recovered by registering the base-image frame (~25 mm/unit, a
different frame from the `room-highlight` SVG's ~1 mm/unit) to the world metric
frame via room-polygon matching, then projecting each symbol's footprint onto
the nearest `rooms_centerline.json` edge → `doors_windows.json` (per opening:
`type`, `subtype`, `defs`, `room`, the centerline `edge`, the occupied
`segment` + normalized `t` range, and `width_m`).

## Dev setup

Python uses **conda** (see `README.md` for exact commands). Frontend uses Node 20+ with `npm run develop` (Rollup dev server on `localhost:3000`). Backend runs via `uvicorn`. The `tools/model-extractor/` Node tool has its own `npm install` + `npx playwright install chromium` (see its README).
