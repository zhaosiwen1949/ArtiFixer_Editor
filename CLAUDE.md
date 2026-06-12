# CLAUDE.md

This file gives guidance to Claude Code (and humans) when working in this repository.

## Purpose

A tool to **view 3D Gaussian Splatting (3DGS) `.ply` scenes in the browser and record camera movement trajectories**. The recorded trajectory (camera poses + per-frame screenshots) is exported in a format suitable for downstream 3D reconstruction / training pipelines (NeRF / 3DGS style `transforms.json`).

## Architecture

- **Frontend (`frontend/`)** — a fork of [playcanvas/supersplat](https://github.com/playcanvas/supersplat) (MIT, TypeScript, PlayCanvas engine). We reuse its splat loading, camera, offscreen render-to-PNG, and UI, and add a **trajectory recorder** (`frontend/src/trajectory-recorder.ts` + a UI panel). The scene is auto-loaded via SuperSplat's `?load=<url>` query param, pointed at the backend's `.ply` URL.
- **Backend (`backend/`)** — Python / FastAPI. Serves the frontend + streams the `.ply` (range-request friendly for large files), and receives recorded trajectories (poses + screenshots), writing them to disk under `data/output/<session>/`.
- **`data/`** — gitignored. Holds input scenes (`export_last.ply`) and recorded outputs.

## Recording workflow

Real-time continuous recording. The user free-navigates the scene, clicks **Start**, navigates, then **Stop**:
- While recording, camera poses are sampled at a fixed rate (**default 30 fps**) — only poses are stored during navigation (cheap) so motion stays smooth.
- On **Stop**, each sampled pose is rendered offscreen to a PNG at the configured resolution (**default 960×540**), and the trajectory + frames are uploaded to the backend.

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

## Coordinate conversion (PlayCanvas → OpenCV C2W) — IMPORTANT

PlayCanvas / OpenGL camera convention: right-handed, **+Y up, camera looks −Z**. OpenCV convention: right-handed, **+Y down, camera looks +Z**. SuperSplat also applies a transform to the splat entity when loading a `.ply`. To produce a C2W matrix in the `.ply`'s native world frame:

1. Express the camera pose in the **splat/PLY frame** to cancel SuperSplat's load transform:
   `M_camera_in_ply = inverse(splatEntity.worldTransform) · camera.worldTransform`
2. Convert OpenGL camera axes to OpenCV by right-multiplying with `diag(1, -1, -1, 1)`:
   `C2W_opencv = M_camera_in_ply · diag(1, -1, -1, 1)`
3. Emit as a row-major 4×4 array with last row `[0, 0, 0, 1]`.

**Validation:** `data/transforms.json` pairs with `data/export_last.ply` (its frames are the cameras that produced the splat). Placing a camera at one of those poses should render an upright, well-framed view — use this to confirm the conversion is correct (a mirrored/upside-down result means the conversion is wrong).

## Dev setup

Python uses **conda** (see `README.md` for exact commands). Frontend uses Node 20+ with `npm run develop` (Rollup dev server on `localhost:3000`). Backend runs via `uvicorn`.
