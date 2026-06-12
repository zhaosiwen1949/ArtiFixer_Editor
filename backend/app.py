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
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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

# Default scene served to the frontend.
DEFAULT_SCENE = DATA_DIR / "export_last.ply"

# session ids and scene names must be simple, filesystem-safe tokens.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

app = FastAPI(title="ArtiFixer 3DGS Trajectory Recorder")

# Allow the Rollup dev server (localhost:3000) to call the API during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
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
async def upload_frame(session: str, index: int = Form(...), image: UploadFile = File(...)):
    """Receive one rendered screenshot for a recording session.

    Saved as data/output/<session>/images/frame_NNNNN.png (1-based, 5 digits).
    """
    _safe(session, "session")
    images_dir = OUTPUT_DIR / session / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    filename = f"frame_{index:05d}.png"
    dest = images_dir / filename
    with dest.open("wb") as f:
        f.write(await image.read())

    return {"saved": f"images/{filename}"}


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
# Static frontend (production build). Mounted last so /api/* takes precedence.
# In dev, run the frontend with `npm run develop` instead.
# ---------------------------------------------------------------------------
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
