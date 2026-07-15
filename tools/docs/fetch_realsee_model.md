# `fetch_realsee_model.py`

Download the **textured 3D model** (mesh + textures) referenced by a Realsee work
page — the asset behind the "三维模型" tab.

## What it does

1. Loads the work-page HTML and brace-matches the embedded `model` object:
   - `file_url` — the mesh, a Realsee-proprietary **`.at3d`** binary container.
   - `material_base_url` + `material_textures[]` — texture-atlas JPGs (`texture_N.jpg`).
   - metadata (`modify_time`, `score`, `type`, `tiles`).
2. Downloads the `.at3d` mesh and every texture JPG verbatim.
3. Writes a `model.json` manifest (original URLs + local paths + metadata).

## Usage

```bash
python tools/fetch_realsee_model.py                 # -> data/model
python tools/fetch_realsee_model.py "<work-url>" --out data/model
```

CLI flags: positional `url` (default reference scene), `--out` (default `data/model`).

## Outputs (under `--out`)

- `model/<name>.at3d` — the mesh container.
- `materials/texture_N.jpg` — texture atlases.
- `model.json` — manifest.

## Important: `.at3d` is proprietary

The `.at3d` is a Realsee binary mesh format (`application/octet-stream`; **not**
glTF / Draco / OBJ) with no public decoder. This script is a faithful **raw asset
grab** — the `.at3d` cannot be opened directly in standard 3D tools.

To get a usable mesh (OBJ/MTL), run `tools/model-extractor/` — it loads the work
in a headless browser, lets Realsee's own `@realsee/five` runtime decode the
`.at3d`, and exports standard geometry. The textures this script downloads are
what the extractor binds by material index (`materialIndex i → texture_i.jpg`),
so **run this first**.

## Dependencies

- Python stdlib only (`urllib`) — no external packages.
