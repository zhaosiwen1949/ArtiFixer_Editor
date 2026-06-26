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

See tools/mesh_to_colmap_3dgs.md for the full convention notes.

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
import sys
from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814           # spherical-harmonics band-0 constant
FLIP = np.diag([1.0, -1.0, -1.0])     # OpenGL camera frame -> OpenCV camera frame
COLMAP_MODEL = {"pinhole": (1, 4), "opencv": (4, 8)}  # name -> (model_id, n_params)


# --------------------------------------------------------------------------- #
# mesh loading + surface sampling (texture colours baked to the cloud)
# --------------------------------------------------------------------------- #
def load_colored_mesh(path: Path):
    import trimesh
    scene = trimesh.load(path, process=False)
    if isinstance(scene, trimesh.Scene):
        parts = []
        for g in scene.geometry.values():
            g.visual = g.visual.to_color()      # bake texture -> per-vertex RGBA
            parts.append(g)
        mesh = trimesh.util.concatenate(parts)
    else:
        if not hasattr(scene.visual, "vertex_colors"):
            scene.visual = scene.visual.to_color()
        mesh = scene
    return mesh


def sample_cloud(mesh, n: int):
    """Area-weighted surface samples + per-sample RGB (barycentric texture)."""
    import trimesh
    pts, fidx = trimesh.sample.sample_surface(mesh, n)
    tris = mesh.triangles[fidx]                                   # (n,3,3)
    bary = trimesh.triangles.points_to_barycentric(tris, pts)    # (n,3)
    vcol = mesh.visual.vertex_colors[mesh.faces[fidx]][..., :3]   # (n,3,3) uint8
    rgb = np.clip((bary[..., None] * vcol).sum(axis=1), 0, 255).astype(np.uint8)
    return pts.astype(np.float64), rgb


# --------------------------------------------------------------------------- #
# camera intrinsics / extrinsics
# --------------------------------------------------------------------------- #
def rotmat_to_qvec(R):
    """3x3 rotation -> quaternion (w,x,y,z) (COLMAP's rotmat2qvec)."""
    Rxx, Ryx, Rzx = R[0, 0], R[1, 0], R[2, 0]
    Rxy, Ryy, Rzy = R[0, 1], R[1, 1], R[2, 1]
    Rxz, Ryz, Rzz = R[0, 2], R[1, 2], R[2, 2]
    K = np.array([
        [Rxx - Ryy - Rzz, 0, 0, 0],
        [Ryx + Rxy, Ryy - Rxx - Rzz, 0, 0],
        [Rzx + Rxz, Rzy + Ryz, Rzz - Rxx - Ryy, 0],
        [Ryz - Rzy, Rzx - Rxz, Rxy - Ryx, Rxx + Ryy + Rzz],
    ]) / 3.0
    vals, vecs = np.linalg.eigh(K)
    qvec = vecs[[3, 0, 1, 2], np.argmax(vals)]
    if qvec[0] < 0:
        qvec = -qvec
    return qvec


def c2w_to_w2c_opencv(c2w):
    """OpenGL/NeRF C2W (4x4) -> OpenCV (R_w2c, t_w2c, camera_centre)."""
    R = c2w[:3, :3]
    C = c2w[:3, 3]                       # camera centre in world
    R_cv = R @ FLIP                      # OpenGL cam axes -> OpenCV cam axes
    R_w2c = R_cv.T
    t_w2c = -R_w2c @ C
    return R_w2c, t_w2c, C


def point_index_of(file_path: str):
    m = re.search(r"point_(\d+)", file_path)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# COLMAP binary writers (little-endian)
# --------------------------------------------------------------------------- #
def write_cameras_bin(path, cam_id, model_id, w, h, params):
    with path.open("wb") as f:
        f.write(struct.pack("<Q", 1))
        f.write(struct.pack("<ii", cam_id, model_id))
        f.write(struct.pack("<QQ", w, h))
        f.write(struct.pack("<%dd" % len(params), *params))


def write_images_bin(path, images):
    """images: list of dict(id, qvec(w,x,y,z), tvec, cam_id, name, xys(Nx2), p3d(N,))."""
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(images)))
        for im in images:
            f.write(struct.pack("<i", im["id"]))
            f.write(struct.pack("<4d", *im["qvec"]))
            f.write(struct.pack("<3d", *im["tvec"]))
            f.write(struct.pack("<i", im["cam_id"]))
            f.write(im["name"].encode() + b"\x00")
            xys, p3d = im["xys"], im["p3d"]
            f.write(struct.pack("<Q", len(xys)))
            for (x, y), pid in zip(xys, p3d):
                f.write(struct.pack("<ddq", float(x), float(y), int(pid)))


def write_points3d_bin(path, ids, xyz, rgb, tracks):
    """tracks[i]: list of (image_id, point2D_idx)."""
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(ids)))
        for pid, p, c, tr in zip(ids, xyz, rgb, tracks):
            f.write(struct.pack("<q", int(pid)))
            f.write(struct.pack("<3d", *map(float, p)))
            f.write(struct.pack("<3B", int(c[0]), int(c[1]), int(c[2])))
            f.write(struct.pack("<d", 1.0))                    # reprojection error
            f.write(struct.pack("<Q", len(tr)))
            for img_id, p2d in tr:
                f.write(struct.pack("<ii", int(img_id), int(p2d)))


# --------------------------------------------------------------------------- #
# 3DGS PLY writer (SuperSplat / INRIA layout)
# --------------------------------------------------------------------------- #
def write_3dgs_ply(path, xyz, rgb, alpha):
    from scipy.spatial import cKDTree
    n = len(xyz)
    # per-gaussian isotropic scale = mean distance to 3 nearest neighbours
    tree = cKDTree(xyz)
    d, _ = tree.query(xyz, k=min(4, n))
    nn = d[:, 1:].mean(axis=1) if d.shape[1] > 1 else np.full(n, 0.01)
    nn = np.clip(nn, 1e-4, None)
    scale = np.log(nn)[:, None].repeat(3, axis=1).astype(np.float32)

    f_dc = ((rgb.astype(np.float32) / 255.0) - 0.5) / SH_C0          # (n,3)
    opacity = np.full((n, 1), np.log(alpha / (1 - alpha)), np.float32)
    normals = np.zeros((n, 3), np.float32)
    rot = np.tile(np.array([1, 0, 0, 0], np.float32), (n, 1))        # identity (w,x,y,z)

    data = np.concatenate(
        [xyz.astype(np.float32), normals, f_dc.astype(np.float32),
         opacity, scale, rot], axis=1)                               # (n,17)
    props = (["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2",
              "opacity", "scale_0", "scale_1", "scale_2",
              "rot_0", "rot_1", "rot_2", "rot_3"])
    header = ("ply\nformat binary_little_endian 1.0\n"
              f"element vertex {n}\n"
              + "".join(f"property float {p}\n" for p in props)
              + "end_header\n")
    with path.open("wb") as f:
        f.write(header.encode())
        f.write(data.astype("<f4").tobytes())


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
    sparse = out / "sparse" / "0"
    sparse.mkdir(parents=True, exist_ok=True)

    # --- inputs -----------------------------------------------------------
    tj = json.loads(Path(args.transforms).read_text())
    frames = tj["frames"]
    observers = {int(o["index"]): o for o in json.loads(Path(args.observers).read_text())}
    fx, fy, cx, cy = tj["fl_x"], tj["fl_y"], tj["cx"], tj["cy"]
    W, H = int(tj["w"]), int(tj["h"])
    print(f"transforms.json: {len(frames)} frames, {fx:.1f}px @ {W}x{H}")

    print(f"Loading mesh {args.mesh} + baking texture colours…")
    mesh = load_colored_mesh(Path(args.mesh))
    print(f"  mesh: {len(mesh.vertices)} verts / {len(mesh.faces)} faces")
    print(f"Sampling {args.max_points} surface points…")
    P, RGB = sample_cloud(mesh, args.max_points)
    print(f"  sampled {len(P)} points")

    # --- per-frame extrinsics + C2W verification --------------------------
    cams, max_centre_err = [], 0.0
    for i, fr in enumerate(frames):
        c2w = np.array(fr["transform_matrix"], dtype=np.float64)
        R_w2c, t_w2c, C = c2w_to_w2c_opencv(c2w)
        pidx = point_index_of(fr["file_path"])
        obs = observers.get(pidx)
        if obs is not None:                                  # C2W translation == observer position?
            max_centre_err = max(max_centre_err, float(np.abs(C - np.array(obs["position"])).max()))
        # round-trip: recovered centre from W2C must equal C
        C_back = -R_w2c.T @ t_w2c
        assert np.allclose(C_back, C, atol=1e-9)
        cams.append({
            "id": i + 1, "cam_id": 1, "name": fr["file_path"],
            "qvec": rotmat_to_qvec(R_w2c), "tvec": t_w2c,
            "R_w2c": R_w2c, "t_w2c": t_w2c, "C": C,
        })
    print(f"C2W check: max |C2W.t - observer.position| = {max_centre_err:.6g}  "
          f"({'OK — transform_matrix is C2W, inverting to W2C' if max_centre_err < 1e-3 else 'MISMATCH'})")

    # --- visibility (frustum cull -> occlusion raycast) -------------------
    intersector = None
    if not args.no_occlusion:
        try:
            from trimesh.ray.ray_pyembree import RayMeshIntersector
            intersector = RayMeshIntersector(mesh)
        except Exception as e:                               # noqa: BLE001
            print(f"  ! embree unavailable ({e}); falling back to frustum-only", file=sys.stderr)

    obs_per_point = [[] for _ in range(len(P))]              # point -> [(img_id, x, y, dist)]
    total_cand = total_vis = 0
    for cam in cams:
        Xc = P @ cam["R_w2c"].T + cam["t_w2c"]               # world -> camera
        z = Xc[:, 2]
        u = fx * Xc[:, 0] / z + cx
        v = fy * Xc[:, 1] / z + cy
        infront = (z > 1e-6) & (u >= 0) & (u < W) & (v >= 0) & (v < H)
        cand = np.nonzero(infront)[0]
        if len(cand) == 0:
            continue
        total_cand += len(cand)
        if intersector is not None:
            origin = cam["C"]
            dirs = P[cand] - origin
            dist_pt = np.linalg.norm(dirs, axis=1)
            dirs = dirs / dist_pt[:, None]
            locs, idx_ray, _ = intersector.intersects_location(
                np.broadcast_to(origin, dirs.shape), dirs, multiple_hits=False)
            dhit = np.full(len(cand), np.inf)
            if len(idx_ray):
                dd = np.linalg.norm(locs - origin, axis=1)
                np.minimum.at(dhit, idx_ray, dd)             # closest hit per ray
            eps = np.maximum(1e-3, 5e-3 * dist_pt)
            visible = np.abs(dhit - dist_pt) <= eps
        else:
            visible = np.ones(len(cand), bool)
        vis = cand[visible]
        total_vis += len(vis)
        for p, uu, vv, zz in zip(vis, u[vis], v[vis], z[vis]):
            obs_per_point[p].append((cam["id"], float(uu), float(vv), float(zz)))
    print(f"visibility: {total_cand} frustum candidates -> {total_vis} visible "
          f"({'occlusion-aware' if intersector else 'frustum-only'})")

    # --- cap track length, drop unseen, reindex ---------------------------
    keep = [i for i in range(len(P)) if obs_per_point[i]]
    print(f"points seen by >=1 image: {len(keep)} / {len(P)}")
    p3d_id = {p: k + 1 for k, p in enumerate(keep)}          # 1-based point3D ids
    img_points = {c["id"]: [] for c in cams}                 # img_id -> [(x,y,point3d_id)]
    tracks = []
    kept_xyz, kept_rgb = [], []
    for p in keep:
        ol = sorted(obs_per_point[p], key=lambda t: t[3])[:args.max_track]  # closest first
        tr = []
        pid = p3d_id[p]
        for img_id, x, y, _ in ol:
            lst = img_points[img_id]
            tr.append((img_id, len(lst)))
            lst.append((x, y, pid))
        tracks.append(tr)
        kept_xyz.append(P[p])
        kept_rgb.append(RGB[p])
    kept_xyz = np.asarray(kept_xyz)
    kept_rgb = np.asarray(kept_rgb)
    n_obs = sum(len(v) for v in img_points.values())
    print(f"observations after cap (<= {args.max_track}/pt): {n_obs} "
          f"(mean {n_obs / max(1, len(cams)):.0f}/image)")

    # --- assemble + write COLMAP ------------------------------------------
    model_id, n_par = COLMAP_MODEL[args.camera_model]
    params = [fx, fy, cx, cy] + ([0, 0, 0, 0] if args.camera_model == "opencv" else [])
    write_cameras_bin(sparse / "cameras.bin", 1, model_id, W, H, params[:n_par])

    images = []
    for c in cams:
        pts = img_points[c["id"]]
        xys = np.array([[x, y] for x, y, _ in pts]) if pts else np.zeros((0, 2))
        p3 = [pid for _, _, pid in pts]
        images.append({"id": c["id"], "qvec": c["qvec"], "tvec": c["tvec"],
                       "cam_id": 1, "name": c["name"], "xys": xys, "p3d": p3})
    write_images_bin(sparse / "images.bin", images)
    write_points3d_bin(sparse / "points3D.bin",
                       [p3d_id[p] for p in keep], kept_xyz, kept_rgb, tracks)
    print(f"Wrote {sparse}/cameras.bin, images.bin, points3D.bin")

    # --- write 3DGS asset (same point set) --------------------------------
    ply = out / "init_3dgs.ply"
    write_3dgs_ply(ply, kept_xyz, kept_rgb, args.alpha)
    print(f"Wrote {ply}  ({len(kept_xyz)} gaussians)")

    if args.verify:
        verify(sparse, ply, fx, fy, cx, cy, W, H)


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
