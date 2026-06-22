#!/usr/bin/env python3
"""Stitch downloaded cube faces into equirectangular panoramas + pose file.

Companion to ``fetch_realsee_panoramas.py``. That script downloads each
panorama as 6 cube faces and writes a per-face pinhole ``transforms.json``.
This script instead produces, for each panorama point:

  * ``pano/point_<i>.jpg`` — one equirectangular (360x180) panorama, stitched
    from the 6 cube faces.
  * ``pano_camera.json``   — one **panorama-camera extrinsic per point**
    (position + orientation as a 4x4 C2W matrix), the spherical-camera
    counterpart of the per-face ``transforms.json``.

Consistency: the cube-face orientation (``FACE_LOCAL``) and quaternion handling
are imported from ``fetch_realsee_panoramas`` so the panoramas and poses use the
exact same convention as the already-corrected ``transforms.json`` — no second
copy of the convention to keep in sync.

Equirectangular convention (matches the stored extrinsic):
  * x = width axis → longitude -pi..pi (left..right), 0 at image centre.
  * y = height axis → latitude +pi/2..-pi/2 (top..bottom).
  * local ray  d_local = (cos lat sin lon, sin lat, -cos lat cos lon)
  * world ray  d_world = R_pano @ d_local,  where the pano extrinsic stores
    ``transform_matrix = [R_pano | position]`` (R_pano = observer quaternion).
  → image centre looks along the panorama's front, top maps to world-up.

Needs Pillow + numpy::  pip install pillow numpy

Usage::
    python tools/build_panoramas.py                       # data/panoramas
    python tools/build_panoramas.py --in data/panoramas --height 2048
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Reuse the quaternion convention from the fetch script (the pano extrinsic must
# match transforms.json), but NOT its FACE_LOCAL.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_realsee_panoramas import FACES, quat_to_matrix  # noqa: E402

# Face geometry for STITCHING is intentionally separate from the fetch script's
# FACE_LOCAL. There, `up` is negated (a 180deg roll) to suit the downstream
# pinhole/3DGS convention of transforms.json. The cube-face JPGs themselves are
# stored upright (verified: scene-up at the top rows), so the equirect must use
# the true geometric basis — front = -Z, up = +Y — or every horizontal face comes
# out vertically flipped. The up/down faces use the geometrically continuous roll.
PANO_FACE_LOCAL = {
    "f": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "b": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "l": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "r": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    "u": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "d": ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0)),
}


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(v):
    import math
    m = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / m for c in v]


def face_basis(face: str):
    """(forward, right, up) unit vectors for a cube face in pano-local space."""
    fwd, up = PANO_FACE_LOCAL[face]
    right = _norm(_cross(fwd, up))
    true_up = _cross(right, fwd)
    return list(fwd), right, true_up


def stitch_equirect(images_dir: Path, index: int, height: int):
    """Build one equirectangular image (H x 2H x 3 uint8) from 6 cube faces."""
    import numpy as np
    from PIL import Image

    faces = {}
    size = None
    for f in FACES:
        p = images_dir / f"point_{index:02d}_{f}.jpg"
        arr = np.asarray(Image.open(p).convert("RGB"))
        faces[f] = arr
        size = arr.shape[0]

    W = height * 2
    lon = (np.arange(W) + 0.5) / W * 2 * np.pi - np.pi        # -pi..pi
    lat = np.pi / 2 - (np.arange(height) + 0.5) / height * np.pi  # +pi/2..-pi/2
    lon, lat = np.meshgrid(lon, lat)
    coslat = np.cos(lat)
    d = np.stack([coslat * np.sin(lon), np.sin(lat), -coslat * np.cos(lon)], -1)

    # For every output pixel pick the face whose forward axis is most aligned
    # with the ray, then sample that face with a pinhole projection.
    flist = list(FACES)
    fwd_dots = np.stack(
        [d @ np.asarray(face_basis(f)[0], float) for f in flist], 0)  # (6,H,W)
    pick = np.argmax(fwd_dots, axis=0)

    out = np.zeros((height, W, 3), dtype=np.uint8)
    for i, f in enumerate(flist):
        fwd, right, up = (np.asarray(v, float) for v in face_basis(f))
        m = pick == i
        if not m.any():
            continue
        dm = d[m]
        denom = dm @ fwd
        a = (dm @ right) / denom
        b = (dm @ up) / denom
        u = np.clip(((a + 1) * 0.5 * size).astype(int), 0, size - 1)
        v = np.clip(((1 - b) * 0.5 * size).astype(int), 0, size - 1)
        out[m] = faces[f][v, u]

    return Image.fromarray(out), W


def pano_extrinsic(obs: dict) -> list[list[float]]:
    """4x4 C2W (row-major) for the spherical camera: [R_quat | position]."""
    R = quat_to_matrix(obs["quaternion"])
    p = obs["position"]
    return [
        [R[0][0], R[0][1], R[0][2], p[0]],
        [R[1][0], R[1][1], R[1][2], p[1]],
        [R[2][0], R[2][1], R[2][2], p[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="root", default="data/panoramas",
                    help="folder holding images/ + observers_raw.json")
    ap.add_argument("--height", type=int, default=2048,
                    help="equirectangular height in px (width = 2*height)")
    args = ap.parse_args()

    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        sys.exit("This script needs Pillow+numpy:\n  pip install pillow numpy")

    root = Path(args.root)
    images_dir = root / "images"
    obs_file = root / "observers_raw.json"
    if not images_dir.is_dir() or not obs_file.is_file():
        sys.exit(f"Missing {images_dir} or {obs_file}. "
                 f"Run fetch_realsee_panoramas.py first.")

    observers = {int(o["index"]): o for o in json.loads(obs_file.read_text())}
    pano_dir = root / "pano"
    pano_dir.mkdir(exist_ok=True)

    W = args.height * 2
    cameras = {"camera_model": "EQUIRECTANGULAR", "w": W, "h": args.height,
               "frames": []}

    print(f"Stitching {len(observers)} panoramas at {W}x{args.height}...")
    for index in sorted(observers):
        img, _ = stitch_equirect(images_dir, index, args.height)
        out_path = pano_dir / f"point_{index:02d}.jpg"
        img.save(out_path, quality=92)
        obs = observers[index]
        cameras["frames"].append({
            "file_path": f"pano/point_{index:02d}.jpg",
            "position": obs["position"],
            "quaternion": obs["quaternion"],
            "transform_matrix": pano_extrinsic(obs),
        })
        print(f"  ✓ {out_path.name}")

    (root / "pano_camera.json").write_text(json.dumps(cameras, indent=2))
    print(f"\nWrote {len(cameras['frames'])} panoramas -> {pano_dir}")
    print(f"Wrote pano_camera.json -> {root / 'pano_camera.json'}")


if __name__ == "__main__":
    main()
