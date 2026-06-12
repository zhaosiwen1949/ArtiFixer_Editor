# ArtiFixer — 3DGS Viewer & Camera Trajectory Recorder

View 3D Gaussian Splatting (3DGS) `.ply` scenes in the browser and **record camera movement trajectories**. Recorded trajectories are exported as a NeRF/3DGS-style `transforms.json` (OpenCV pinhole intrinsics + per-frame camera-to-world poses) together with rendered screenshots — ready for downstream reconstruction / training pipelines.

## Architecture

| Component | Tech | Role |
|---|---|---|
| `frontend/` | Fork of [playcanvas/supersplat](https://github.com/playcanvas/supersplat) (TypeScript / PlayCanvas) | In-browser 3DGS viewer + trajectory recorder UI |
| `backend/`  | Python / FastAPI | Serves the `.ply` scene, receives recorded poses + screenshots, writes them to disk |
| `data/`     | (gitignored) | Input scenes (`export_last.ply`) and recorded outputs |

The trajectory recorder lives in `frontend/src/trajectory-recorder.ts` and is wired in `frontend/src/main.ts`. See [`CLAUDE.md`](./CLAUDE.md) for the full output-format spec and the PlayCanvas→OpenCV coordinate conversion.

## Prerequisites

- **Node.js ≥ 20.19** (for the frontend)
- **conda** (for the Python backend)
- A 3DGS `.ply` scene placed at `data/export_last.ply`

## Setup & run

### 1. Backend (FastAPI, via conda)

```bash
# create and activate a dedicated conda environment
conda create -n artifixer python=3.10 -y
conda activate artifixer

# install dependencies
pip install -r backend/requirements.txt

# run the API (serves the .ply + receives uploads) on port 8000
cd backend
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

The scene is served at `http://localhost:8000/api/scene/export_last.ply`.

### 2. Frontend (SuperSplat dev server)

```bash
cd frontend
npm install
npm run develop        # Rollup dev server on http://localhost:3000
```

Open the viewer with the scene auto-loaded from the backend:

```
http://localhost:3000/?load=http://localhost:8000/api/scene/export_last.ply
```

> SuperSplat detects the file type from the load URL, so the URL **must end in `.ply`** — use the `/api/scene/<name>.ply` route (not the extension-less `/api/scene`).
>
> The recorder's backend base URL defaults to `http://localhost:8000`. Override it with a `&backend=<url>` query param if needed.

## Recording a trajectory

1. Open the viewer URL above; wait for the splat to load.
2. In the **Trajectory Recorder** panel (bottom-right), set **FPS** (default 30) and **Resolution** (default `960x540`).
3. Click **● Record** and navigate the scene freely — camera poses are sampled continuously.
4. Click **■ Stop**. Each sampled pose is re-rendered offscreen to a PNG and uploaded.

Output is written by the backend to:

```
data/output/<session>/
  transforms.json
  images/frame_00001.png
  images/frame_00002.png
  ...
```

## Output format (`transforms.json`)

Top-level OpenCV pinhole intrinsics (`camera_model`, `fl_x`, `fl_y`, `cx`, `cy`, `w`, `h`, `k1`, `k2`, `p1`, `p2`) plus a `frames` array. Each frame:

```json
{
  "file_path": "images/frame_00001.png",
  "transform_matrix": [[...],[...],[...],[0,0,0,1]]
}
```

`transform_matrix` is a 4×4 **OpenCV camera-to-world (C2W)** matrix in **row-major** order. Intrinsics are derived from the render resolution and camera FOV (square pixels, centered principal point). See [`CLAUDE.md`](./CLAUDE.md) for the coordinate-conversion details and how to validate against the reference `data/transforms.json`.

## License

The `frontend/` fork retains SuperSplat's MIT license. See `frontend/LICENSE` and the repository `LICENSE`.
