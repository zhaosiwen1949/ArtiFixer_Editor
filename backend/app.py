"""FastAPI backend for the 3DGS viewer + camera trajectory recorder.

Responsibilities:
  * Stream the input .ply scene (range-request friendly for large files).
  * Optionally serve the built frontend (frontend/dist).
  * Receive recorded trajectories: per-frame screenshots + the final
    transforms.json, written under data/output/<session>/.

Run (from the backend/ directory):
    uvicorn app:app --reload --host 0.0.0.0 --port 8000
"""

import json
import re
import sys
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"

# The textured mesh surface-sampled for the COLMAP point cloud (see /colmap below).
MESH_PATH = DATA_DIR / "model" / "exported" / "model.obj"

# The mesh->COLMAP conversion core lives under tools/ (shared with the CLI tool
# mesh_to_colmap_3dgs.py). It's imported lazily in the /colmap endpoint so the
# server starts even if the heavy deps (trimesh/embreex/scipy) are absent.
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

# Default scene served to the frontend.
DEFAULT_SCENE = DATA_DIR / "export_last.ply"

# session ids and scene names must be simple, filesystem-safe tokens.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

app = FastAPI(title="ArtiFixer 3DGS Trajectory Recorder")

# Allow the dev servers to call the API during dev: the splat viewer's Rollup
# server (frontend/, :3000) and the three.js mesh viewer's Vite server
# (mesh-viewer/, :5173).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe(token: str, what: str) -> str:
    if not SAFE_NAME.match(token):
        raise HTTPException(status_code=400, detail=f"invalid {what}: {token!r}")
    return token


# ---------------------------------------------------------------------------
# Scene serving
# ---------------------------------------------------------------------------
@app.get("/api/scene")
def get_default_scene():
    """Stream the default scene .ply. FileResponse handles HTTP range requests,
    which the frontend loader needs for large (~250 MB) files."""
    if not DEFAULT_SCENE.exists():
        raise HTTPException(status_code=404, detail=f"scene not found: {DEFAULT_SCENE.name}")
    return FileResponse(
        DEFAULT_SCENE,
        media_type="application/octet-stream",
        filename=DEFAULT_SCENE.name,
    )


@app.get("/api/scene/{name}")
def get_scene(name: str):
    """Stream a named .ply from the data/ directory."""
    _safe(name, "scene name")
    path = DATA_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"scene not found: {name}")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


# ---------------------------------------------------------------------------
# Trajectory upload
# ---------------------------------------------------------------------------
@app.post("/api/recordings/{session}/frame")
async def upload_frame(
    session: str,
    index: int = Form(...),
    image: UploadFile = File(...),
    opacity: UploadFile | None = File(None),
):
    """Receive one rendered frame for a recording session.

    Saved as data/output/<session>/frames/frame_NNNNN.png (1-based, 5 digits).
    If an opacity map is included, it is saved alongside as
    data/output/<session>/opacity/frame_NNNNN.png.
    """
    _safe(session, "session")
    filename = f"frame_{index:05d}.png"

    frames_dir = OUTPUT_DIR / session / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    with (frames_dir / filename).open("wb") as f:
        f.write(await image.read())

    saved = {"image": f"frames/{filename}"}
    if opacity is not None:
        opacity_dir = OUTPUT_DIR / session / "opacity"
        opacity_dir.mkdir(parents=True, exist_ok=True)
        with (opacity_dir / filename).open("wb") as f:
            f.write(await opacity.read())
        saved["opacity"] = f"opacity/{filename}"

    return {"saved": saved}


@app.post("/api/recordings/{session}/finalize")
async def finalize(session: str, transforms: dict):
    """Receive the assembled transforms.json (intrinsics + frames) and persist it
    to data/output/<session>/transforms.json."""
    _safe(session, "session")
    session_dir = OUTPUT_DIR / session
    session_dir.mkdir(parents=True, exist_ok=True)

    out_path = session_dir / "transforms.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(transforms, f, indent=2)

    n_frames = len(transforms.get("frames", []))
    return {"saved": str(out_path.relative_to(PROJECT_ROOT)), "frames": n_frames}


# ---------------------------------------------------------------------------
# Trajectory playback (listing + fetch)
# ---------------------------------------------------------------------------
@app.get("/api/recordings")
def list_recordings():
    """List saved recording sessions (those that contain a transforms.json),
    sorted by name (timestamp-named folders sort chronologically)."""
    if not OUTPUT_DIR.exists():
        return {"sessions": []}

    sessions = []
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        tj = d / "transforms.json"
        if not tj.exists():
            continue
        try:
            with tj.open("r", encoding="utf-8") as f:
                data = json.load(f)
            n_frames = len(data.get("frames", []))
        except (json.JSONDecodeError, OSError):
            n_frames = 0
        sessions.append({"session": d.name, "frames": n_frames})

    return {"sessions": sessions}


@app.get("/api/recordings/{session}/transforms")
def get_transforms(session: str):
    """Return a saved session's transforms.json (for playback in the editor)."""
    _safe(session, "session")
    path = OUTPUT_DIR / session / "transforms.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"transforms not found: {session}")
    return FileResponse(path, media_type="application/json")


# ---------------------------------------------------------------------------
# COLMAP export
# ---------------------------------------------------------------------------
@app.post("/api/recordings/{session}/colmap")
def export_colmap(session: str, num_points: int = Body(100000, embed=True)):
    """Convert a saved trajectory into a COLMAP sparse model + a 3DGS init cloud.

    Reads data/output/<session>/transforms.json (OpenGL/NeRF C2W poses +
    intrinsics), surface-samples the textured mesh (data/model/exported/model.obj)
    to `num_points`, and writes, under the session folder:
        sparse/0/{cameras,images,points3D}.bin   (camera_model = SIMPLE_PINHOLE)
        init_3dgs.ply

    Image names are generated from frame order (`frames/frame_NNNNN.png`), matching
    the recorded RGB screenshots. Visibility is occlusion-aware (embree). Runs
    synchronously — a plain `def` endpoint so FastAPI executes it in a threadpool.
    """
    _safe(session, "session")
    if num_points < 1:
        raise HTTPException(status_code=400, detail="num_points must be >= 1")

    session_dir = OUTPUT_DIR / session
    tj_path = session_dir / "transforms.json"
    if not tj_path.exists():
        raise HTTPException(status_code=404, detail=f"transforms not found: {session}")
    if not MESH_PATH.exists():
        raise HTTPException(status_code=404, detail=f"mesh not found: {MESH_PATH.name}")

    tj = json.loads(tj_path.read_text(encoding="utf-8"))
    frames = tj.get("frames", [])
    if not frames:
        raise HTTPException(status_code=400, detail="transforms.json has no frames")

    fx, fy = tj["fl_x"], tj["fl_y"]
    cx, cy, W, H = tj["cx"], tj["cy"], int(tj["w"]), int(tj["h"])
    # Order-based image names, matching frames/frame_NNNNN.png on disk (1-based).
    image_names = [f"frames/frame_{i + 1:05d}.png" for i in range(len(frames))]

    try:
        from mesh_to_colmap_core import convert_trajectory_to_colmap
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"COLMAP conversion deps unavailable ({e}); "
                   f"install trimesh/embreex/scipy in the backend env",
        )

    try:
        stats = convert_trajectory_to_colmap(
            frames=frames,
            intrinsics=(fx, fy, cx, cy, W, H),
            image_names=image_names,
            mesh_path=MESH_PATH,
            out_dir=session_dir,
            max_points=num_points,
            camera_model="simple_pinhole",
        )
    except Exception as e:  # noqa: BLE001 — surface conversion failures to the UI
        raise HTTPException(status_code=500, detail=f"conversion failed: {e}")

    # Report output paths relative to the project root.
    stats["sparse_dir"] = str(Path(stats["sparse_dir"]).relative_to(PROJECT_ROOT))
    stats["ply"] = str(Path(stats["ply"]).relative_to(PROJECT_ROOT))
    return stats


# ---------------------------------------------------------------------------
# Static frontend (production build). Mounted last so /api/* takes precedence.
# In dev, run the frontend with `npm run develop` instead.
# ---------------------------------------------------------------------------
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
