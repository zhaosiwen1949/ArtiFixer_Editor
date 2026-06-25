# Realsee 3D model extractor (direction 2 — via the live runtime)

Exports the Realsee "三维模型" mesh as a standard **OBJ + MTL + textures**, by
loading the work in headless Chromium and letting Realsee's own bundled
`@realsee/five` SDK decode the proprietary `.at3d`, then reaching into the
decoded THREE.js scene and dumping geometry + UVs.

Why this works: the `.at3d` is a proprietary binary with no public decoder, but
the SDK decodes it to a THREE.js mesh at runtime. We grab that mesh (the live
`Five` instance is reachable through the React fiber tree → its scene graph),
read `position` / `uv` / `index` / per-group `materialIndex`, and map each
material to the `texture_N.jpg` atlases (downloaded separately). The texture
pixels live in GPU `ImageBitmap`s with no URL, so the mapping is by
**material index → texture_N.jpg** (order matches).

## Setup (one-time)

    cd tools/model-extractor
    npm install                      # playwright
    npx playwright install chromium  # ~150 MB headless Chromium (SwiftShader WebGL)

Also run the asset downloader first so the texture JPGs exist locally:

    python tools/fetch_realsee_model.py     # -> data/model/{model,materials}/

## Use

    node extract.mjs                         # default work URL, OBJ
    node extract.mjs --format glb            # single self-contained GLB (textures embedded)
    node extract.mjs --format all            # obj + glb + gltf
    node extract.mjs "<work-url>" --out <dir> [--format obj,glb] [--headed] [--timeout 120000]
    node render-preview.mjs                   # sanity render of model.obj  -> exported/preview.png
    node render-preview.mjs model.glb         # sanity render of model.glb  -> exported/preview_glb.png

`--format` is a comma list of `obj` | `glb` | `gltf` | `all` (default `obj`):
- **obj** — `model.obj` + `model.mtl` + `materials/texture_*.jpg` (multi-file).
- **glb** — `model.glb`, one self-contained binary, textures embedded. Best for
  modern pipelines / web viewers. (Larger than the source JPEGs: `GLTFExporter`
  re-encodes the texture atlases as PNG.)
- **gltf** — `model.gltf`, JSON with embedded data URIs (also self-contained).

Output (default `data/model/exported/`): the chosen model file(s), `materials/`,
`preview*.png`, and `extract_raw.json` (mesh/material diagnostics).

## Notes / limits

- Headless Chromium needs WebGL; the SwiftShader flags are set in `extract.mjs`.
- The model loads lazily — the script clicks the **三维模型** tab and polls until a
  textured mesh with >1000 vertices appears (helper bounding-box / gizmo meshes
  are filtered out).
- Coordinates are the viewer's world space (Y up). If a UV/axis looks flipped in
  your downstream tool, flip `vt` v→`1-v` or swap the up axis there.
