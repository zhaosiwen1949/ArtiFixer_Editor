# Alignment check — mesh ↔ panorama cameras

Verifies that the extracted 3D mesh (`data/model/exported/model.obj`, from
`model-extractor/`) and the three panorama camera files share **one coordinate
frame**, so the mesh can be used directly as a geometry prior / alignment
reference for 3DGS reconstruction.

Two scripts:
- `check_alignment.py` — numeric verdict.
- `model-extractor/align-overlay.mjs` — visual overlay (renders the camera
  positions on the mesh, top-down + bird's-eye).

## What is checked

1. **Cross-file consistency** of the camera files — they must carry the *same*
   camera centres:
   - `pano_camera.json` centres vs `observers_raw.json` positions.
   - `transforms.json` unique centres vs the observer set (it has 6 faces/point).
2. **Mesh ↔ cameras** alignment:
   - same up-axis (smallest-spread axis of the camera cloud vs the mesh's
     shortest dimension),
   - cameras inside the mesh XZ footprint,
   - camera height above the mesh floor is physically plausible (~tripod height).

## How to run

```bash
python tools/check_alignment.py                 # numeric, prints a VERDICT
node tools/model-extractor/align-overlay.mjs    # writes align_top.png + align_birdseye.png
```

(Run `fetch_realsee_panoramas.py` + `build_panoramas.py` and the model extractor
first, so the inputs exist.)

## Result (reference scene)

### Cross-file consistency — all three files share one camera frame
| Check | Result |
|---|---|
| `pano_camera.json` centres vs `observers_raw.json` positions | **max\|Δ\| = 0.000000** (identical) |
| `transforms.json` unique centres vs observers | **39 vs 39** (234 frames = 39 points × 6 faces) |

### Mesh ↔ cameras
| | Mesh (`model.obj`, viewer world) | Cameras (`observers_raw.json`) |
|---|---|---|
| up-axis | **Y** (size 3.21 m = floor-to-ceiling) | **Y** (per-axis std `[5.12, 0.14, 3.74]` → smallest on Y) |
| XZ size | 19.28 × 14.47 m | 17.15 × 12.9 m (smaller — cameras sit indoors, not at outer walls) |
| inside footprint (±0.5 m) | — | **yes** |

- floor Y = −1.23, ceiling Y = +1.98, **mean camera height above floor = 1.42 m**
  (realistic tripod height).

### Verdict

> **ALIGNED — same frame, same scale (metres), same orientation (Y-up),
> identity transform.** No registration is needed between the mesh and the
> camera poses.

## Visual evidence

Written to `data/model/exported/`:
- **`align_top.png`** — top-down floor plan: all 39 camera dots fall **inside the
  rooms** (bedrooms / living room / kitchen / baths / balconies), well distributed.
- **`align_birdseye.png`** — bird's-eye: the dots hover at a consistent ~1.4 m
  height inside each room. (A yellow tick shows each camera's −Z view direction,
  from its quaternion.)

## Implication for 3DGS reconstruction

The mesh and `transforms.json`'s camera extrinsics live in the **same metric,
Y-up world**, so the mesh can be used directly as:
- an **initial point cloud / geometry constraint**, or depth / occlusion / opacity
  supervision.

If a downstream trainer expects a different axis convention (COLMAP/OpenCV Y-down,
or Z-up), apply the **same** axis transform to *both* the mesh and the cameras —
their relative alignment is preserved.
