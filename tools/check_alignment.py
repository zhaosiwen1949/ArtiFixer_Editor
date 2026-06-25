#!/usr/bin/env python3
"""Verify the extracted mesh and the panorama camera files share one frame.

Checks two things:
  1. Cross-file consistency of the three camera files (observers_raw.json,
     pano_camera.json, transforms.json) — they should all carry the SAME camera
     centres.
  2. Alignment of those camera centres with the exported mesh (model.obj): same
     up-axis, same scale, cameras inside the footprint at a plausible height.

The visual counterpart is tools/model-extractor/align-overlay.mjs (overlays the
camera positions on the mesh and renders top-down + bird's-eye).

Usage::
    python tools/check_alignment.py
    python tools/check_alignment.py --model data/model/exported/model.obj --pano data/panoramas
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def obj_bbox(path: Path):
    pts = []
    for line in path.read_text().splitlines():
        if line.startswith("v "):
            _, x, y, z = line.split()[:4]
            pts.append((float(x), float(y), float(z)))
    return np.asarray(pts)


def centers(frames):
    return np.asarray([np.asarray(f["transform_matrix"])[:3, 3] for f in frames])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="data/model/exported/model.obj")
    ap.add_argument("--pano", default="data/panoramas")
    args = ap.parse_args()

    pano = Path(args.pano)
    M = obj_bbox(Path(args.model))
    obs = json.loads((pano / "observers_raw.json").read_text())
    P = np.asarray([o["position"] for o in obs])
    PC = centers(json.loads((pano / "pano_camera.json").read_text())["frames"])
    TJ = centers(json.loads((pano / "transforms.json").read_text())["frames"])

    msize, mmin, mmax = M.max(0) - M.min(0), M.min(0), M.max(0)
    csize = P.max(0) - P.min(0)
    up = int(np.argmin(P.std(0)))           # up axis = smallest spread
    mesh_up = int(np.argmin(msize))
    axes = "XYZ"

    print("=== cross-file consistency ===")
    d_pc = np.abs(PC - P).max()
    uTJ = np.unique(TJ.round(3), axis=0)
    print(f"  pano_camera vs observers  max|Δ| = {d_pc:.6f}  ->  {'OK' if d_pc < 1e-4 else 'MISMATCH'}")
    print(f"  transforms unique centres = {len(uTJ)} vs observers {len(P)}  ->  "
          f"{'OK' if len(uTJ) == len(P) else 'MISMATCH'}")

    print("\n=== mesh vs cameras ===")
    print(f"  mesh   size {msize.round(2)}  up-axis = {axes[mesh_up]}")
    print(f"  camera size {csize.round(2)}  up-axis = {axes[up]}  (std {P.std(0).round(3)})")
    inside = bool((P.min(0) >= mmin - 0.5).all() and (P.max(0) <= mmax + 0.5).all())
    floor, ceil = mmin[up], mmax[up]
    cam_h = float(P[:, up].mean() - floor)
    print(f"  cameras inside mesh footprint (±0.5 m): {inside}")
    print(f"  floor={floor:.2f} ceil={ceil:.2f}  mean camera height above floor = {cam_h:.2f} m")

    aligned = (d_pc < 1e-4 and len(uTJ) == len(P) and up == mesh_up
               and inside and 0.8 < cam_h < 2.2)
    print("\n=== VERDICT ===")
    print("  ALIGNED — same frame, same scale, identity transform." if aligned
          else "  NOT aligned — a transform is needed (see numbers above).")


if __name__ == "__main__":
    main()
