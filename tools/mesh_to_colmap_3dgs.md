# `mesh_to_colmap_3dgs.py`

Turn the extracted textured mesh + the known panorama camera poses into a 3DGS
**initialization**: one surface-sampled point cloud that is written as both a
**3DGS asset** and a **COLMAP sparse model** (with 2D↔3D correspondences). Gives a
downstream trainer (gsplat / INRIA 3DGS / nerfstudio) a geometry-accurate init in
the real, metric, Y-up world.

## What it does

1. Load `data/model/exported/model.obj` (4 textured submeshes), **bake texture →
   per-vertex colours**, merge into one mesh.
2. **Area-weighted surface sample** `--max-points` points; per-point RGB by
   barycentric texture interpolation.
3. Build a **single shared PINHOLE camera** from `transforms.json` intrinsics.
4. Convert each frame's pose **C2W(OpenGL) → W2C(OpenCV)** (see below) and
   self-verify against `observers_raw.json`.
5. **Occlusion-aware visibility**: frustum-cull the cloud per image, then embree
   first-hit raycast from the camera centre — keep a point only if the mesh does
   not occlude it. Record its pixel projection.
6. **Cap track length** (`--max-track`, closest images first); drop points seen by
   no image (keeps the COLMAP model valid and the two assets identical).
7. Write `sparse/0/{cameras,images,points3D}.bin` + `init_3dgs.ply`.

## Usage

```bash
python tools/mesh_to_colmap_3dgs.py                                   # 300k pts, occlusion-aware
python tools/mesh_to_colmap_3dgs.py --max-points 100000 --max-track 8 --verify
python tools/mesh_to_colmap_3dgs.py --no-occlusion --camera-model opencv
```

CLI: `--mesh`, `--transforms`, `--observers`, `--out` (default `data/colmap`),
`--max-points` (300000), `--max-track` (8), `--alpha` (0.1, initial opacity),
`--camera-model` (`pinhole`|`opencv`), `--no-occlusion`, `--verify`.

## Output (under `--out`, default `data/colmap/`)

```
sparse/0/cameras.bin     1 shared PINHOLE camera (fx=fy=1024, cx=cy=1024, 2048²)
sparse/0/images.bin      234 images: W2C extrinsics + 2D↔3D correspondences
sparse/0/points3D.bin    the cloud: xyz + RGB + per-point tracks
init_3dgs.ply            3DGS asset (same point set), binary_little_endian
```

The COLMAP **image root** is `data/panoramas/` (image `name`s are
`images/point_XX_<face>.jpg`).

## Key point: extrinsic convention (the part that bites)

`transforms.json` `transform_matrix` is **OpenGL/NeRF camera-to-world (C2W)**:
right-handed, +X right, **+Y up, camera looks −Z** (same as PlayCanvas; see
CLAUDE.md). COLMAP `images.bin` stores **world-to-camera (W2C)** in the **OpenCV**
camera frame: +X right, **+Y down, looks +Z**, as quaternion `(qw,qx,qy,qz)` +
translation `t`. So per frame:

```
R   = C2W[:3,:3];  C = C2W[:3,3]          # C = camera centre in world
R_cv  = R @ diag(1,-1,-1)                 # OpenGL cam axes -> OpenCV cam axes
R_w2c = R_cvᵀ
t_w2c = -R_w2c · C
qvec  = rotmat2qvec(R_w2c)  (w,x,y,z);  tvec = t_w2c
```

Note this `diag(1,−1,−1)` flip is **required here** even though CLAUDE.md forbids
it when *emitting* `transforms.json` — the difference is that COLMAP's extrinsics
genuinely use the OpenCV (Y-down/+Z) camera frame, while `transforms.json` is
OpenGL (Y-up/−Z).

### Verification (built in, printed each run)

- **Is `transform_matrix` really C2W?** For each frame the script checks
  `C2W[:3,3] == observers_raw.json[point].position`. They match exactly
  (`max|Δ| = 0`), proving the stored translation is the **camera centre in world**
  — i.e. the matrix is C2W, so inverting to W2C is correct. (If it were already
  W2C, the translation would be `−R·C`, not `C`.)
- **Round-trip:** recovers `−R_w2cᵀ·t_w2c` and asserts it equals `C`.
- **External (manual):** overlaying one image's `POINT2D` on its real photo
  (`data/panoramas/images/point_00_b.jpg`) shows the 2D points land on the actual
  scene (chandelier / walls / floor / sofa) — confirming intrinsics + W2C +
  projection + occlusion end-to-end.

## COLMAP binary layouts (little-endian)

- **cameras.bin**: `uint64 N`; per camera `int32 id, int32 model_id, uint64 w,
  uint64 h, float64×nparams`. PINHOLE = model_id 1, params `[fx,fy,cx,cy]`
  (OPENCV = 4, `[fx,fy,cx,cy,k1,k2,p1,p2]`, distortion 0).
- **images.bin**: `uint64 N`; per image `int32 id, double qw,qx,qy,qz, double
  tx,ty,tz, int32 cam_id, char* name\0, uint64 nPoints2D, {double x, double y,
  int64 point3D_id}×`.
- **points3D.bin**: `uint64 N`; per point `int64 id, double x,y,z, uint8 r,g,b,
  double error, uint64 trackLen, {int32 image_id, int32 point2D_idx}×`.

## 3DGS PLY encodings (SuperSplat / INRIA)

`binary_little_endian`, one `element vertex`, properties in order:
`x,y,z, nx,ny,nz, f_dc_0..2, opacity, scale_0..2, rot_0..3` (all float32).

- colour: `f_dc_i = (rgb_i − 0.5) / 0.28209479177387814` (SH band-0; rgb∈[0,1]).
- `opacity` = **logit** `log(α/(1−α))` (viewer applies sigmoid); default α=0.1.
- `scale_i` = `log(σ)`; σ = mean distance to the 3 nearest neighbours (kNN).
- `rot` = quaternion **`(w,x,y,z)`**, identity `(1,0,0,0)`.
- normals unused (`0`).

Loads directly in this repo's viewer via `?load=<url>`.

## Feeding a 3DGS trainer

- Point to `data/colmap/sparse/0/` as the sparse model and `data/panoramas/` as
  the image root.
- Everything is in the same **metric, Y-up** world as the mesh
  (`tools/check_alignment.md`). If a trainer needs a different axis convention,
  apply the same axis transform to the cameras and the cloud together.
- `init_3dgs.ply` can seed Gaussians directly; `points3D.bin` carries the same
  xyz+RGB for COLMAP-style initialization.

## Dependencies

`trimesh`, `embreex` (occlusion rays), `scipy` (kNN), `numpy`, `Pillow`
(conda env `artifixer`). Without `embreex`, pass `--no-occlusion` (frustum-only).
