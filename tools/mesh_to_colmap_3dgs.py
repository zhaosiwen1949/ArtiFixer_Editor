#!/usr/bin/env python3
"""Mesh -> initialization point cloud -> 3DGS asset + COLMAP sparse model.

Samples a point cloud from the textured mesh (data/model/exported/model.obj) and
uses that single cloud to produce, in one metric Y-up frame:

  * a **3DGS asset** ``init_3dgs.ply`` (SuperSplat / INRIA Gaussian-splat format),
  * a **COLMAP sparse model** ``sparse/0/{cameras,images,points3D}.bin`` whose
    ``images.bin`` carries 2D<->3D correspondences (point-cloud projections).

The camera intrinsics/extrinsics come from ``data/panoramas/transforms.json``.
That file stores **OpenGL/NeRF camera-to-world (C2W)** matrices (Y-up, looks -Z),
whereas COLMAP ``images.bin`` stores **OpenCV world-to-camera (W2C)** (Y-down,
looks +Z) — so each pose is converted (flip camera axes by diag(1,-1,-1), then
invert). The conversion is self-verified against ``observers_raw.json`` (the C2W
translation must equal the raw observer position = camera centre in world).

The heavy lifting lives in ``mesh_to_colmap_core.py`` (shared with the FastAPI
backend, which drives the same conversion from the mesh-viewer web UI). See
tools/docs/mesh_to_colmap_3dgs.md for the full convention notes.

Deps: trimesh, embreex (occlusion rays), scipy (kNN scale), numpy, Pillow.

Usage::
    python tools/mesh_to_colmap_3dgs.py                       # 300k pts, occlusion-aware
    python tools/mesh_to_colmap_3dgs.py --max-points 100000 --max-track 8
    python tools/mesh_to_colmap_3dgs.py --no-occlusion --verify
"""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

from mesh_to_colmap_core import COLMAP_MODEL, convert_trajectory_to_colmap


def point_index_of(file_path: str):
    m = re.search(r"point_(\d+)", file_path)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default="data/model/exported/model.obj")
    ap.add_argument("--transforms", default="data/panoramas/transforms.json")
    ap.add_argument("--observers", default="data/panoramas/observers_raw.json")
    ap.add_argument("--out", default="data/colmap")
    ap.add_argument("--max-points", type=int, default=300000)
    ap.add_argument("--max-track", type=int, default=8)
    ap.add_argument("--alpha", type=float, default=0.1, help="initial gaussian opacity")
    ap.add_argument("--camera-model", choices=list(COLMAP_MODEL), default="pinhole")
    ap.add_argument("--no-occlusion", action="store_true",
                    help="frustum-only visibility (skip mesh raycasting)")
    ap.add_argument("--verify", action="store_true", help="reparse outputs and report")
    args = ap.parse_args()

    import json
    out = Path(args.out)

    # --- inputs -----------------------------------------------------------
    tj = json.loads(Path(args.transforms).read_text())
    frames = tj["frames"]
    fx, fy, cx, cy = tj["fl_x"], tj["fl_y"], tj["cx"], tj["cy"]
    W, H = int(tj["w"]), int(tj["h"])
    print(f"transforms.json: {len(frames)} frames, {fx:.1f}px @ {W}x{H}")

    image_names = [fr["file_path"] for fr in frames]

    # observer positions keyed by frame index (for the C2W self-verification)
    verify_positions = None
    obs_path = Path(args.observers)
    if obs_path.exists():
        observers = {int(o["index"]): o for o in json.loads(obs_path.read_text())}
        verify_positions = {}
        for i, fr in enumerate(frames):
            obs = observers.get(point_index_of(fr["file_path"]))
            if obs is not None:
                verify_positions[i] = obs["position"]

    stats = convert_trajectory_to_colmap(
        frames=frames,
        intrinsics=(fx, fy, cx, cy, W, H),
        image_names=image_names,
        mesh_path=args.mesh,
        out_dir=out,
        max_points=args.max_points,
        max_track=args.max_track,
        occlusion=not args.no_occlusion,
        camera_model=args.camera_model,
        alpha=args.alpha,
        verify_positions=verify_positions,
    )
    print(f"Wrote {stats['sparse_dir']}/cameras.bin, images.bin, points3D.bin")
    print(f"Wrote {stats['ply']}  ({stats['n_points']} gaussians)")

    if args.verify:
        sparse = out / "sparse" / "0"
        verify(sparse, out / "init_3dgs.ply", fx, fy, cx, cy, W, H)


def verify(sparse, ply, fx, fy, cx, cy, W, H):
    print("\n=== verify (reparse) ===")
    with (sparse / "cameras.bin").open("rb") as f:
        (nc,) = struct.unpack("<Q", f.read(8))
        cid, model, w, h = struct.unpack("<iiQQ", f.read(24))
        print(f"  cameras.bin: {nc} camera(s), model_id={model}, {w}x{h}")
    with (sparse / "images.bin").open("rb") as f:
        (ni,) = struct.unpack("<Q", f.read(8))
        tot2d = 0
        first = None
        for _ in range(ni):
            iid = struct.unpack("<i", f.read(4))[0]
            f.read(56)                                  # qvec(4d)+tvec(3d)
            f.read(4)                                   # cam_id
            name = b""
            while (ch := f.read(1)) != b"\x00":
                name += ch
            (np2,) = struct.unpack("<Q", f.read(8))
            f.read(np2 * 24)
            tot2d += np2
            if first is None:
                first = (iid, name.decode(), np2)
        print(f"  images.bin: {ni} images, {tot2d} total 2D obs; first={first}")
    with (ply).open("rb") as f:
        head = b""
        while b"end_header" not in head:
            head += f.read(64)
        nverts = int(re.search(rb"element vertex (\d+)", head).group(1))
        print(f"  init_3dgs.ply: {nverts} gaussians, binary_little_endian")


if __name__ == "__main__":
    main()
