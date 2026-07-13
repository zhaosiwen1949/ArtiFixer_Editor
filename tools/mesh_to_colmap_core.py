#!/usr/bin/env python3
"""Reusable core for turning a camera trajectory + textured mesh into a COLMAP
sparse model (+ a 3DGS init point cloud).

This module holds the conversion math and the COLMAP/PLY binary writers so both
the CLI tool (``mesh_to_colmap_3dgs.py``) and the FastAPI backend
(``backend/app.py``, driven from the mesh-viewer web UI) share one source of
truth. See ``mesh_to_colmap_3dgs.md`` for the full convention notes.

Convention (the part that bites): ``transform_matrix`` is OpenGL/NeRF
camera-to-world (C2W) — Y-up, looks -Z. COLMAP ``images.bin`` stores OpenCV
world-to-camera (W2C) — Y-down, looks +Z — so each pose is converted (flip the
camera axes by ``diag(1,-1,-1)`` then invert).

Deps: trimesh, embreex (occlusion rays), scipy (kNN scale), numpy, Pillow.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814           # spherical-harmonics band-0 constant
FLIP = np.diag([1.0, -1.0, -1.0])     # OpenGL camera frame -> OpenCV camera frame
# name -> (model_id, n_params). SIMPLE_PINHOLE=0 [f,cx,cy]; PINHOLE=1 [fx,fy,cx,cy];
# OPENCV=4 [fx,fy,cx,cy,k1,k2,p1,p2].
COLMAP_MODEL = {"simple_pinhole": (0, 3), "pinhole": (1, 4), "opencv": (4, 8)}


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
    """3x3 rotation -> quaternion (w,x,y,z) (COLMAP's rotmat2qvec).

    Elements are unpacked in row-major order (``R.flat``) exactly as COLMAP does —
    Rxx,Ryx,Rzx = R[0,0],R[0,1],R[0,2], etc. Indexing by column instead yields the
    quaternion of R.T (the conjugate), which corrupts the extrinsics.
    """
    Rxx, Ryx, Rzx, Rxy, Ryy, Rzy, Rxz, Ryz, Rzz = np.asarray(R).flat
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


def camera_params(camera_model: str, fx, fy, cx, cy):
    """COLMAP intrinsic params for the chosen model (matches COLMAP_MODEL sizes)."""
    if camera_model == "simple_pinhole":
        return [fx, cx, cy]                       # single focal (fx == fy assumed)
    if camera_model == "opencv":
        return [fx, fy, cx, cy, 0, 0, 0, 0]
    return [fx, fy, cx, cy]                       # pinhole


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
# high-level pipeline
# --------------------------------------------------------------------------- #
def _print_progress(msg: str) -> None:
    print(msg)


def convert_trajectory_to_colmap(
    *,
    frames,
    intrinsics,
    image_names,
    mesh_path,
    out_dir,
    max_points: int = 100000,
    max_track: int = 8,
    occlusion: bool = True,
    camera_model: str = "simple_pinhole",
    alpha: float = 0.1,
    verify_positions=None,
    progress=None,
) -> dict:
    """Sample the mesh and write a COLMAP sparse model + a 3DGS init .ply.

    Args:
        frames: list of dicts, each with ``transform_matrix`` (4x4 OpenGL C2W).
        intrinsics: ``(fx, fy, cx, cy, w, h)``.
        image_names: per-frame COLMAP image ``name`` (same length/order as frames).
        mesh_path: textured mesh to surface-sample (e.g. model.obj).
        out_dir: writes ``out_dir/sparse/0/{cameras,images,points3D}.bin`` and
            ``out_dir/init_3dgs.ply``.
        max_points: surface samples to draw.
        max_track: cap observations per 3D point (closest images first).
        occlusion: occlusion-aware visibility (embree); falls back to frustum-only
            if embree can't load.
        camera_model: ``simple_pinhole`` | ``pinhole`` | ``opencv``.
        alpha: initial gaussian opacity for the .ply.
        verify_positions: optional ``{frame_index: [x,y,z]}`` to check the C2W
            translation equals the known camera centre (self-verification).
        progress: optional ``callback(str)`` for status lines.

    Returns:
        dict with counts and output paths.
    """
    if len(image_names) != len(frames):
        raise ValueError(
            f"image_names ({len(image_names)}) must match frames ({len(frames)})")

    log = progress or _print_progress
    fx, fy, cx, cy, W, H = intrinsics
    W, H = int(W), int(H)
    out_dir = Path(out_dir)
    sparse = out_dir / "sparse" / "0"
    sparse.mkdir(parents=True, exist_ok=True)

    log(f"loading mesh {mesh_path} + baking texture colours…")
    mesh = load_colored_mesh(Path(mesh_path))
    log(f"  mesh: {len(mesh.vertices)} verts / {len(mesh.faces)} faces")
    log(f"sampling {max_points} surface points…")
    P, RGB = sample_cloud(mesh, max_points)
    log(f"  sampled {len(P)} points")

    # --- per-frame extrinsics (+ optional C2W verification) ---------------
    cams, max_centre_err = [], 0.0
    for i, fr in enumerate(frames):
        c2w = np.array(fr["transform_matrix"], dtype=np.float64)
        R_w2c, t_w2c, C = c2w_to_w2c_opencv(c2w)
        if verify_positions is not None and i in verify_positions:
            max_centre_err = max(
                max_centre_err,
                float(np.abs(C - np.array(verify_positions[i])).max()))
        C_back = -R_w2c.T @ t_w2c                     # round-trip: recovered centre
        assert np.allclose(C_back, C, atol=1e-9)
        cams.append({
            "id": i + 1, "cam_id": 1, "name": image_names[i],
            "qvec": rotmat_to_qvec(R_w2c), "tvec": t_w2c,
            "R_w2c": R_w2c, "t_w2c": t_w2c, "C": C,
        })
    if verify_positions is not None:
        ok = "OK — transform_matrix is C2W" if max_centre_err < 1e-3 else "MISMATCH"
        log(f"C2W check: max |C2W.t - known centre| = {max_centre_err:.6g}  ({ok})")

    # --- visibility (frustum cull -> occlusion raycast) -------------------
    intersector = None
    if occlusion:
        try:
            from trimesh.ray.ray_pyembree import RayMeshIntersector
            intersector = RayMeshIntersector(mesh)
        except Exception as e:                        # noqa: BLE001
            log(f"  ! embree unavailable ({e}); falling back to frustum-only")

    obs_per_point = [[] for _ in range(len(P))]       # point -> [(img_id, x, y, dist)]
    total_cand = total_vis = 0
    for cam in cams:
        Xc = P @ cam["R_w2c"].T + cam["t_w2c"]        # world -> camera
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
                np.minimum.at(dhit, idx_ray, dd)      # closest hit per ray
            eps = np.maximum(1e-3, 5e-3 * dist_pt)
            visible = np.abs(dhit - dist_pt) <= eps
        else:
            visible = np.ones(len(cand), bool)
        vis = cand[visible]
        total_vis += len(vis)
        for p, uu, vv, zz in zip(vis, u[vis], v[vis], z[vis]):
            obs_per_point[p].append((cam["id"], float(uu), float(vv), float(zz)))
    log(f"visibility: {total_cand} frustum candidates -> {total_vis} visible "
        f"({'occlusion-aware' if intersector else 'frustum-only'})")

    # --- cap track length, drop unseen, reindex ---------------------------
    keep = [i for i in range(len(P)) if obs_per_point[i]]
    log(f"points seen by >=1 image: {len(keep)} / {len(P)}")
    p3d_id = {p: k + 1 for k, p in enumerate(keep)}   # 1-based point3D ids
    img_points = {c["id"]: [] for c in cams}          # img_id -> [(x,y,point3d_id)]
    tracks = []
    kept_xyz, kept_rgb = [], []
    for p in keep:
        ol = sorted(obs_per_point[p], key=lambda t: t[3])[:max_track]  # closest first
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
    log(f"observations after cap (<= {max_track}/pt): {n_obs} "
        f"(mean {n_obs / max(1, len(cams)):.0f}/image)")

    # --- assemble + write COLMAP ------------------------------------------
    model_id, _ = COLMAP_MODEL[camera_model]
    params = camera_params(camera_model, fx, fy, cx, cy)
    write_cameras_bin(sparse / "cameras.bin", 1, model_id, W, H, params)

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
    log(f"wrote {sparse}/cameras.bin, images.bin, points3D.bin")

    # --- write 3DGS asset (same point set) --------------------------------
    ply = out_dir / "init_3dgs.ply"
    write_3dgs_ply(ply, kept_xyz, kept_rgb, alpha)
    log(f"wrote {ply}  ({len(kept_xyz)} gaussians)")

    return {
        "n_points": int(len(kept_xyz)),
        "n_images": int(len(cams)),
        "n_observations": int(n_obs),
        "sampled_points": int(len(P)),
        "sparse_dir": str(sparse),
        "ply": str(ply),
        "camera_model": camera_model,
        "occlusion": intersector is not None,
    }
