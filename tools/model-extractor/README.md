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

    node extract.mjs                         # default work URL
    node extract.mjs "<work-url>" --out <dir> [--headed] [--timeout 120000]
    node render-preview.mjs                  # sanity render -> exported/preview.png

Output (default `data/model/exported/`):
- `model.obj`, `model.mtl`, `materials/texture_*.jpg`, `preview.png`
- `extract_raw.json` — mesh/material diagnostics

## Notes / limits

- Headless Chromium needs WebGL; the SwiftShader flags are set in `extract.mjs`.
- The model loads lazily — the script clicks the **三维模型** tab and polls until a
  textured mesh with >1000 vertices appears (helper bounding-box / gizmo meshes
  are filtered out).
- Coordinates are the viewer's world space (Y up). If a UV/axis looks flipped in
  your downstream tool, flip `vt` v→`1-v` or swap the up axis there.
