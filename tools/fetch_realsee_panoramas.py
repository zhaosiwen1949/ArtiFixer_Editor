#!/usr/bin/env python3
"""Fetch panorama images from a Realsee (如视 / 贝壳VR) work page.

A Realsee work stores each panorama point as a **cube map**: 6 JPG faces
(front/back/left/right/up/down) under a CDN path like::

    https://vr-public.realsee-cdn.cn/release/auto3dhd/<work>/images/cube_2048/<i>/<hash>/<i>_<face>.jpg

This script loads the work page HTML, discovers every cube-map panorama point
embedded in it, and downloads all faces. With ``--equirect`` it additionally
stitches each cube into a single equirectangular panorama (needs Pillow+numpy).

Usage::

    python tools/fetch_realsee_panoramas.py "<work-url>"
    python tools/fetch_realsee_panoramas.py "<work-url>" --out data/panoramas --equirect

The default URL is the project's reference scene.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# `requests` is imported lazily inside the network functions so that other
# tools can import the geometry helpers (FACE_LOCAL, quat_to_matrix, ...)
# without having requests installed.

DEFAULT_URL = (
    "https://open.realsee.com/ke/vwYQ3drRBl69nj28/"
    "KpokNd82rwjh1hkhQTMNpa3cg8zGbPXe/#lianjia"
)

# Cube faces as named in the Realsee CDN: front/back/left/right/up/down.
FACES = ("f", "b", "l", "r", "u", "d")

# --- Coordinate convention knobs (flip these if a view comes out wrong) -------
#
# Realsee's web viewer is @realsee/five, built on Three.js, whose camera frame
# is the SAME as OpenGL / NeRF: right-handed, +X right, +Y up, looks down -Z.
# The project's transforms.json also uses that exact convention (see CLAUDE.md),
# so NO axis flip is applied when going Realsee-world -> transform_matrix.
#
# Each cube face is a 90deg pinhole camera sharing the observer's optical centre.
# The panorama's LOCAL frame is assumed front = -Z, up = +Y, right = +X. Each
# entry is (forward, up) in that local frame; the observer quaternion then
# rotates the face into world space.
#
# NOTE: the `up` vectors are NEGATED relative to the naive choice — i.e. every
# face is rolled 180deg about its view axis. Without this, rendered views came
# out rotated 180deg (upside-down AND left-right reversed). Negating `up` flips
# both the right and up basis columns (a proper 180deg roll, det stays +1, no
# mirror). The up/down faces keep the geometrically continuous orientation.
FACE_LOCAL = {
    "f": ((0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
    "b": ((0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "l": ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    "r": ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
    "u": ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
    "d": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
}

# Set True if the stored quaternion is world->local instead of local->world.
INVERT_QUATERNION = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://open.realsee.com/",
}

# Matches .../images/cube_<res>/<index>/<hash>/<index>_<face>.jpg and captures
# everything up to (and including) ".../images/" plus the index and hash.
CUBE_RE = re.compile(
    r"(https?:)?(//[\w.-]+/[\w./-]*?/images/)cube_(\d+)/(\d+)/([a-f0-9]{32})"
)


def fetch_html(url: str) -> str:
    import requests
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def discover_panoramas(html: str) -> tuple[str, int, list[tuple[int, str]]]:
    """Return (images_base_url, cube_resolution, [(index, hash), ...]).

    Panorama points are de-duplicated and returned in scene order.
    """
    base_url = None
    resolution = None
    seen: dict[int, str] = {}
    for m in CUBE_RE.finditer(html):
        scheme, base, res, index, h = m.groups()
        base_url = base_url or ("https:" + base if base.startswith("//") else base)
        resolution = resolution or int(res)
        seen.setdefault(int(index), h)
    if not base_url:
        raise SystemExit(
            "No cube-map panoramas found in page. The work may be private, "
            "expired, or use a different viewer format."
        )
    points = sorted(seen.items())
    return base_url, resolution, points


def face_url(base: str, res: int, index: int, h: str, face: str) -> str:
    return f"{base}cube_{res}/{index}/{h}/{index}_{face}.jpg"


# --- Camera-pose extraction & transforms.json ---------------------------------

def parse_observers(html: str) -> dict[int, dict]:
    """Extract the `observers` array (one entry per panorama) from the page.

    Each observer carries `index`, `position` [x,y,z] and `quaternion`
    {w,x,y,z} — the camera's world position and orientation. Returns a dict
    keyed by index. Empty if the page doesn't embed poses.
    """
    i = html.find('"observers"')
    if i < 0:
        return {}
    start = html.index("[", i)
    depth = 0
    for j in range(start, len(html)):
        if html[j] == "[":
            depth += 1
        elif html[j] == "]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    else:
        return {}
    try:
        observers = json.loads(html[start:end])
    except json.JSONDecodeError:
        return {}
    return {int(o["index"]): o for o in observers if "index" in o}


def quat_to_matrix(q: dict) -> list[list[float]]:
    """3x3 rotation matrix (local->world) for quaternion {w,x,y,z}."""
    w, x, y, z = q["w"], q["x"], q["y"], q["z"]
    if INVERT_QUATERNION:  # conjugate = inverse for a unit quaternion
        x, y, z = -x, -y, -z
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ]


def _matvec(R, v):
    return [sum(R[r][c] * v[c] for c in range(3)) for r in range(3)]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _norm(v):
    m = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / m for c in v]


def face_c2w(position, R, face: str) -> list[list[float]]:
    """4x4 OpenGL camera-to-world matrix (row-major) for one cube face."""
    fwd_local, up_local = FACE_LOCAL[face]
    fwd = _matvec(R, fwd_local)
    up = _matvec(R, up_local)
    right = _norm(_cross(fwd, up))
    true_up = _cross(right, fwd)
    back = [-fwd[0], -fwd[1], -fwd[2]]  # camera +Z points opposite the view dir
    # Columns are the camera axes in world space: [right, up, back | position].
    return [
        [right[0], true_up[0], back[0], position[0]],
        [right[1], true_up[1], back[1], position[1]],
        [right[2], true_up[2], back[2], position[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]


def build_transforms(observers, points, faces, size: int, img_prefix: str) -> dict:
    """Assemble a NeRF/3DGS-style transforms.json from observer poses.

    Each selected cube face becomes one pinhole frame (90deg FOV, square).
    """
    half = size / 2.0
    fl = half / math.tan(math.radians(90.0) / 2.0)  # == size/2 for 90deg
    transforms = {
        "camera_model": "OPENCV",
        "fl_x": fl, "fl_y": fl,
        "cx": half, "cy": half,
        "w": size, "h": size,
        "k1": 0.0, "k2": 0.0, "p1": 0.0, "p2": 0.0,
        "frames": [],
    }
    point_indices = {idx for idx, _ in points}
    for index, _h in points:
        obs = observers.get(index)
        if obs is None:
            continue
        R = quat_to_matrix(obs["quaternion"])
        for face in faces:
            transforms["frames"].append({
                "file_path": f"{img_prefix}point_{index:02d}_{face}.jpg",
                "transform_matrix": face_c2w(obs["position"], R, face),
            })
    transforms["frames"].sort(key=lambda f: f["file_path"])
    return transforms


def download(url: str, dest: Path):
    """Download url -> dest. Returns dest on success, None if it failed."""
    import requests
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest
    except requests.RequestException as exc:  # network / 4xx / 5xx
        print(f"  ! failed {url}: {exc}", file=sys.stderr)
        return None


def download_all_faces(base, res, points, out_dir: Path, workers: int) -> list[Path]:
    """Download every face of every panorama. Returns list of point dirs.

    Faces are saved per-point under ``point_<i>/<face>.jpg`` AND mirrored into a
    flat ``images/`` summary folder as ``point_<i>_<face>.jpg`` so every image
    lives in one place.
    """
    all_dir = out_dir / "images"
    all_dir.mkdir(parents=True, exist_ok=True)

    jobs = []  # (url, dest, summary_dest)
    point_dirs = []
    for index, h in points:
        pdir = out_dir / f"point_{index:02d}"
        pdir.mkdir(parents=True, exist_ok=True)
        point_dirs.append(pdir)
        for face in FACES:
            url = face_url(base, res, index, h, face)
            jobs.append((url, pdir / f"{face}.jpg",
                         all_dir / f"point_{index:02d}_{face}.jpg"))

    print(f"Downloading {len(jobs)} faces ({len(points)} panoramas × {len(FACES)})...")
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download, url, dest): summary
                   for url, dest, summary in jobs}
        for fut in as_completed(futures):
            summary = futures[fut]
            dest = fut.result()
            if dest:
                ok += 1
                # Mirror into the flat summary folder.
                if not (summary.exists() and summary.stat().st_size > 0):
                    summary.write_bytes(dest.read_bytes())
    print(f"Done: {ok}/{len(jobs)} faces saved under {out_dir}")
    print(f"Summary folder with all {ok} images: {all_dir}")
    return point_dirs


def stitch_equirect(point_dir: Path, out_path: Path, height: int = 2048) -> None:
    """Stitch 6 cube faces into one equirectangular panorama.

    Requires Pillow + numpy. Standard cube->equirect remapping.
    """
    import numpy as np
    from PIL import Image

    faces = {f: np.asarray(Image.open(point_dir / f"{f}.jpg").convert("RGB"))
             for f in FACES}
    size = faces["f"].shape[0]  # square faces
    width = height * 2

    # Spherical coords for every output pixel.
    lon = (np.linspace(0, 1, width, endpoint=False) * 2 - 1) * math.pi      # -pi..pi
    lat = (0.5 - np.linspace(0, 1, height, endpoint=False)) * math.pi       # pi/2..-pi/2
    lon, lat = np.meshgrid(lon, lat)
    x = np.cos(lat) * np.sin(lon)
    y = np.sin(lat)
    z = np.cos(lat) * np.cos(lon)

    out = np.zeros((height, width, 3), dtype=np.uint8)
    absx, absy, absz = np.abs(x), np.abs(y), np.abs(z)

    def sample(face_key, u, v, mask):
        img = faces[face_key]
        ui = np.clip(((u + 1) * 0.5 * size).astype(int), 0, size - 1)
        vi = np.clip(((v + 1) * 0.5 * size).astype(int), 0, size - 1)
        out[mask] = img[vi[mask], ui[mask]]

    # +Z front, -Z back, +X right, -X left, +Y up, -Y down.
    m = (absz >= absx) & (absz >= absy) & (z > 0)
    sample("f", x / absz, -y / absz, m)
    m = (absz >= absx) & (absz >= absy) & (z < 0)
    sample("b", -x / absz, -y / absz, m)
    m = (absx >= absy) & (absx >= absz) & (x > 0)
    sample("r", -z / absx, -y / absx, m)
    m = (absx >= absy) & (absx >= absz) & (x < 0)
    sample("l", z / absx, -y / absx, m)
    m = (absy >= absx) & (absy >= absz) & (y > 0)
    sample("u", x / absy, z / absy, m)
    m = (absy >= absx) & (absy >= absz) & (y < 0)
    sample("d", x / absy, -z / absy, m)

    Image.fromarray(out).save(out_path, quality=92)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="Realsee work URL")
    ap.add_argument("--out", default="data/panoramas", help="output directory")
    ap.add_argument("--workers", type=int, default=8, help="parallel downloads")
    ap.add_argument("--equirect", action="store_true",
                    help="also stitch each cube into an equirectangular panorama")
    ap.add_argument("--no-transforms", action="store_true",
                    help="skip generating transforms.json")
    ap.add_argument("--faces", default=",".join(FACES),
                    help="comma list of cube faces to emit as frames "
                         "(default all 6; e.g. 'f,b,l,r' for horizontal only)")
    args = ap.parse_args()
    faces = tuple(f.strip() for f in args.faces.split(",") if f.strip() in FACES)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading work page: {args.url}")
    html = fetch_html(args.url)
    base, res, points = discover_panoramas(html)
    print(f"Found {len(points)} panorama points (cube_{res}) at {base}")

    point_dirs = download_all_faces(base, res, points, out_dir, args.workers)

    if not args.no_transforms:
        observers = parse_observers(html)
        if not observers:
            print("\n! No camera poses ('observers') found in page — "
                  "skipping transforms.json.", file=sys.stderr)
        else:
            # Dump the raw poses for debugging / re-deriving the convention.
            (out_dir / "observers_raw.json").write_text(
                json.dumps([observers[i] for i in sorted(observers)],
                           indent=2, ensure_ascii=False))
            transforms = build_transforms(observers, points, faces, res, "images/")
            (out_dir / "transforms.json").write_text(
                json.dumps(transforms, indent=2))
            matched = len({i for i, _ in points} & set(observers))
            print(f"\nWrote transforms.json: {len(transforms['frames'])} frames "
                  f"({matched} panoramas × {len(faces)} faces), "
                  f"intrinsics {res}×{res} @ 90° FOV.")
            print(f"Raw poses: {out_dir / 'observers_raw.json'}")

    if args.equirect:
        try:
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            print("\n--equirect needs Pillow+numpy. Install with:\n"
                  "  pip install pillow numpy", file=sys.stderr)
            return
        eq_dir = out_dir / "equirect"
        eq_dir.mkdir(exist_ok=True)
        print(f"\nStitching {len(point_dirs)} equirectangular panoramas...")
        for pdir in point_dirs:
            dest = eq_dir / f"{pdir.name}.jpg"
            try:
                stitch_equirect(pdir, dest)
                print(f"  ✓ {dest}")
            except Exception as exc:  # missing face, decode error, etc.
                print(f"  ! {pdir.name}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
