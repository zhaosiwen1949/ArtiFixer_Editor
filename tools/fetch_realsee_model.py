#!/usr/bin/env python3
"""Fetch the textured 3D model (mesh + textures) from a Realsee work page.

Companion to ``fetch_realsee_panoramas.py``. The Realsee "三维模型" tab renders a
single textured mesh of the whole home. The work-page HTML embeds a ``model``
object describing it::

    "model": {
        "file_url": ".../model/<hash>/auto3d-XXXX.at3d",   # the mesh
        "material_base_url": ".../materials/<hash>/",
        "material_textures": [".../texture_0.jpg", ...],    # texture atlases
        "modify_time": ..., "score": ..., "type": ...
    }

The mesh ``file_url`` is a Realsee-proprietary **.at3d** binary container
(``application/octet-stream``; not glTF / Draco / OBJ). This script downloads the
.at3d mesh and every texture JPG verbatim, and writes a ``model.json`` recording
the original URLs + local paths + metadata. (Decoding .at3d into a standard mesh
format is out of scope — this is a faithful asset grab.)

Only the Python stdlib is used (urllib), so no extra packages are required.

Usage::

    python tools/fetch_realsee_model.py                 # -> data/model
    python tools/fetch_realsee_model.py "<work-url>" --out data/model
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = (
    "https://open.realsee.com/ke/vwYQ3drRBl69nj28/"
    "KpokNd82rwjh1hkhQTMNpa3cg8zGbPXe/#lianjia"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://open.realsee.com/",
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def extract_model(html: str) -> dict:
    """Pull the embedded ``model`` object out of the page HTML.

    Finds ``"model":{`` and brace-matches the JSON object that follows.
    """
    key = '"model":{'
    i = html.find(key)
    while i >= 0:
        start = html.index("{", i)
        depth = 0
        for j in range(start, len(html)):
            if html[j] == "{":
                depth += 1
            elif html[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        else:
            break
        try:
            obj = json.loads(html[start:end])
        except json.JSONDecodeError:
            obj = {}
        # the real model block is the one carrying the mesh file_url
        if isinstance(obj, dict) and obj.get("file_url"):
            return obj
        i = html.find(key, end)
    raise SystemExit(
        "No 3D model ('model.file_url') found in page. The work may be private, "
        "expired, or have no 3D model."
    )


def download(url: str, dest: Path) -> Path | None:
    """Download url -> dest. Returns dest on success, None on failure."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  · cached {dest.name}")
        return dest
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        dest.write_bytes(data)
        print(f"  ✓ {dest.name} ({len(data) / 1024:.0f} KB)")
        return dest
    except Exception as exc:  # network / HTTP error
        print(f"  ! failed {url}: {exc}", file=sys.stderr)
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="Realsee work URL")
    ap.add_argument("--out", default="data/model", help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.out)
    mesh_dir = out_dir / "model"
    mat_dir = out_dir / "materials"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    mat_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading work page: {args.url}")
    html = fetch_html(args.url)
    model = extract_model(html)

    mesh_url = model["file_url"]
    textures = model.get("material_textures", [])
    print(f"Found model: {mesh_url.rsplit('/', 1)[-1]}  + {len(textures)} textures")

    # mesh (.at3d)
    print("Downloading mesh...")
    mesh_name = mesh_url.rsplit("/", 1)[-1]
    mesh_dest = download(mesh_url, mesh_dir / mesh_name)

    # textures
    print("Downloading textures...")
    tex_records = []
    for url in textures:
        name = url.rsplit("/", 1)[-1]
        dest = download(url, mat_dir / name)
        tex_records.append({
            "url": url,
            "local_path": str((mat_dir / name).relative_to(out_dir)) if dest else None,
        })

    # manifest
    manifest = {
        "source_page": args.url,
        "mesh": {
            "url": mesh_url,
            "format": "at3d (Realsee proprietary binary mesh container)",
            "local_path": str(mesh_dest.relative_to(out_dir)) if mesh_dest else None,
        },
        "material_base_url": model.get("material_base_url"),
        "textures": tex_records,
        "metadata": {k: model.get(k) for k in ("modify_time", "score", "type", "tiles")},
    }
    (out_dir / "model.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False))

    ok_tex = sum(1 for t in tex_records if t["local_path"])
    print(f"\nDone. Mesh {'ok' if mesh_dest else 'FAILED'}, "
          f"{ok_tex}/{len(textures)} textures -> {out_dir}")
    print(f"Manifest: {out_dir / 'model.json'}")


if __name__ == "__main__":
    main()
