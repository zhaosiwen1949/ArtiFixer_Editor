#!/usr/bin/env node
/**
 * Direction 2: export the Realsee 3D model via its own runtime.
 *
 * Instead of decoding the proprietary `.at3d` ourselves, we load the work in a
 * headless Chromium, let Realsee's bundled `@realsee/five` SDK decode the mesh
 * into a THREE.js scene, then reach into that scene and dump geometry +
 * per-mesh texture references. The result is written as a standard OBJ + MTL
 * (the texture JPGs were already downloaded by tools/fetch_realsee_model.py).
 *
 * The Five instance is reached through the React fiber tree (it is exposed as
 * `unsafe__fiveInstance` on a context provider). From it we BFS the object
 * graph for THREE meshes (`obj.isMesh` with a position attribute).
 *
 * Usage:
 *   node extract.mjs                       # default work URL -> ../../data/model/exported
 *   node extract.mjs "<work-url>" --out <dir> --headed --timeout 120000
 */

import { chromium } from 'playwright';
import { mkdirSync, writeFileSync, existsSync, copyFileSync, readdirSync } from 'node:fs';
import { dirname, resolve, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

const DEFAULT_URL =
    'https://open.realsee.com/ke/vwYQ3drRBl69nj28/KpokNd82rwjh1hkhQTMNpa3cg8zGbPXe/#lianjia';

// --- args ------------------------------------------------------------------
const argv = process.argv.slice(2);
const url = argv.find(a => !a.startsWith('--')) ?? DEFAULT_URL;
const getOpt = (name, def) => {
    const i = argv.indexOf(`--${name}`);
    return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : def;
};
const outDir = resolve(__dirname, getOpt('out', '../../data/model/exported'));
const texDir = resolve(__dirname, '../../data/model/materials');
const headed = argv.includes('--headed');
const timeout = parseInt(getOpt('timeout', '120000'), 10);

// --- the in-page extraction (runs in the browser) --------------------------
// Returns { meshes: [{ name, matrixWorld:[16], position:[...], uv:[...]|null,
//   index:[...]|null, groups:[{start,count,materialIndex}], textures:[url|null] }] }
function pageExtract() {
    // 1. locate the Five instance via React fiber / context
    const isFive = o => o && typeof o === 'object' &&
        (typeof o.loadModels === 'function' || ('works' in o && typeof o.on === 'function'));
    function findFive() {
        const els = document.querySelectorAll('*');
        for (const el of els) {
            const key = Object.keys(el).find(k =>
                k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
            if (!key) continue;
            let fiber = el[key];
            let depth = 0;
            while (fiber && depth < 200) {
                const sn = fiber.stateNode;
                if (sn && isFive(sn.five)) return sn.five;
                if (isFive(sn)) return sn;
                const p = fiber.memoizedProps;
                if (p && isFive(p.unsafe__fiveInstance)) return p.unsafe__fiveInstance;
                fiber = fiber.return;
                depth++;
            }
        }
        return null;
    }

    const five = findFive();
    if (!five) return { error: 'five-not-found' };

    // 2. BFS the object graph from `five` for THREE meshes
    const meshes = [];
    const seen = new Set();
    const queue = [five];
    let steps = 0;
    const skip = v =>
        v == null || typeof v !== 'object' ||
        ArrayBuffer.isView(v) || v instanceof ArrayBuffer ||
        (typeof Node !== 'undefined' && v instanceof Node) ||
        (typeof WebGLRenderingContext !== 'undefined' && v instanceof WebGLRenderingContext) ||
        (typeof WebGL2RenderingContext !== 'undefined' && v instanceof WebGL2RenderingContext);

    while (queue.length && steps < 400000) {
        const o = queue.shift();
        steps++;
        if (skip(o) || seen.has(o)) continue;
        seen.add(o);

        if (o.isMesh && o.geometry && o.geometry.attributes && o.geometry.attributes.position) {
            meshes.push(o);
            continue; // don't descend into mesh internals
        }
        // descend (own enumerable keys only)
        for (const k in o) {
            let v;
            try { v = o[k]; } catch { continue; }
            if (!skip(v) && !seen.has(v)) queue.push(v);
        }
    }
    if (meshes.length === 0) return { error: 'no-meshes', steps };

    // 3. dump each mesh
    const texFromTexture = t => {
        if (!t) return null;
        const img = t.image || (t.source && t.source.data);
        if (!img) return null;
        return img.currentSrc || img.src || (typeof img === 'string' ? img : null) || null;
    };
    const texUrl = mat => {
        if (!mat) return null;
        let u = texFromTexture(mat.map);
        if (u) return u;
        if (mat.uniforms) {                 // ShaderMaterial: scan uniforms for a texture
            for (const k in mat.uniforms) {
                const val = mat.uniforms[k] && mat.uniforms[k].value;
                if (val && (val.isTexture || val.image || val.source)) {
                    u = texFromTexture(val);
                    if (u) return u;
                }
            }
        }
        return null;
    };
    const matDiag = mat => mat ? ({
        type: mat.type, name: mat.name,
        hasMap: !!mat.map,
        mapImg: mat.map && mat.map.image ? (mat.map.image.currentSrc || mat.map.image.src || mat.map.image.tagName || 'img?') : null,
        uniformKeys: mat.uniforms ? Object.keys(mat.uniforms) : null,
        uniformTex: mat.uniforms ? Object.keys(mat.uniforms).filter(k => {
            const v = mat.uniforms[k] && mat.uniforms[k].value; return v && (v.isTexture || v.image || v.source);
        }) : null,
    }) : null;

    const out = meshes.map((m, i) => {
        m.updateWorldMatrix?.(true, false);
        const g = m.geometry;
        const mats = Array.isArray(m.material) ? m.material : [m.material];
        return {
            name: m.name || `mesh_${i}`,
            nv: g.attributes.position.count,
            matrixWorld: Array.from(m.matrixWorld.elements),
            position: Array.from(g.attributes.position.array),
            uv: g.attributes.uv ? Array.from(g.attributes.uv.array) : null,
            normal: g.attributes.normal ? Array.from(g.attributes.normal.array) : null,
            index: g.index ? Array.from(g.index.array) : null,
            groups: (g.groups && g.groups.length) ? g.groups.map(gr => ({
                start: gr.start, count: gr.count, materialIndex: gr.materialIndex || 0
            })) : null,
            textures: mats.map(texUrl),
            diag: mats.map(matDiag),
        };
    });
    return { meshes: out, steps };
}

// --- OBJ/MTL writers (Node side) -------------------------------------------
function mat4mulVec3(e, x, y, z) {
    // column-major THREE Matrix4 elements
    const w = e[3] * x + e[7] * y + e[11] * z + e[15] || 1;
    return [
        (e[0] * x + e[4] * y + e[8] * z + e[12]) / w,
        (e[1] * x + e[5] * y + e[9] * z + e[13]) / w,
        (e[2] * x + e[6] * y + e[10] * z + e[14]) / w,
    ];
}

function buildObjMtl(meshes, texFiles) {
    const obj = ['# Realsee model exported via @realsee/five runtime', 'mtllib model.mtl'];
    const mtl = ['# materials'];
    const madeMat = new Set();    // material name already written to mtl
    let vBase = 0, vtBase = 0, vnBase = 0;

    // The .at3d textures are decoded to GPU ImageBitmaps (no URL), but the mesh's
    // material array is ordered, so materialIndex i -> texture_i.jpg (the files
    // downloaded separately). Fall back to a flat untextured material otherwise.
    const ensureMat = (materialIndex) => {
        const file = texFiles[materialIndex] ?? null;
        const name = file ? `mat_${materialIndex}` : 'untextured';
        if (!madeMat.has(name)) {
            madeMat.add(name);
            mtl.push(`newmtl ${name}`, 'Kd 1 1 1');
            if (file) mtl.push(`map_Kd materials/${file}`);
            mtl.push('');
        }
        return name;
    };

    for (const m of meshes) {
        const e = m.matrixWorld;
        const pos = m.position, uv = m.uv, nrm = m.normal;
        const nv = pos.length / 3;
        for (let i = 0; i < nv; i++) {
            const [x, y, z] = mat4mulVec3(e, pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]);
            obj.push(`v ${x} ${y} ${z}`);
        }
        if (uv) for (let i = 0; i < uv.length / 2; i++) obj.push(`vt ${uv[2 * i]} ${uv[2 * i + 1]}`);
        if (nrm) for (let i = 0; i < nrm.length / 3; i++) obj.push(`vn ${nrm[3 * i]} ${nrm[3 * i + 1]} ${nrm[3 * i + 2]}`);

        obj.push(`o ${m.name}`);
        const idx = m.index ?? Array.from({ length: nv }, (_, i) => i);
        const groups = m.groups ?? [{ start: 0, count: idx.length, materialIndex: 0 }];
        const face = (a) => {
            const v = vBase + a + 1;
            const t = uv ? vtBase + a + 1 : '';
            const n = nrm ? vnBase + a + 1 : '';
            if (uv && nrm) return `${v}/${t}/${n}`;
            if (uv) return `${v}/${t}`;
            if (nrm) return `${v}//${n}`;
            return `${v}`;
        };
        for (const gr of groups) {
            obj.push(`usemtl ${ensureMat(gr.materialIndex)}`);
            for (let i = gr.start; i < gr.start + gr.count; i += 3) {
                obj.push(`f ${face(idx[i])} ${face(idx[i + 1])} ${face(idx[i + 2])}`);
            }
        }
        vBase += nv;
        if (uv) vtBase += uv.length / 2;
        if (nrm) vnBase += nrm.length / 3;
    }
    return { obj: obj.join('\n') + '\n', mtl: mtl.join('\n') + '\n' };
}

// --- main ------------------------------------------------------------------
(async () => {
    mkdirSync(outDir, { recursive: true });
    console.log(`Launching ${headed ? 'headed' : 'headless'} Chromium…`);
    const browser = await chromium.launch({
        headless: !headed,
        args: ['--use-gl=angle', '--use-angle=swiftshader', '--ignore-gpu-blocklist',
            '--enable-webgl', '--enable-unsafe-swiftshader'],
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.on('console', msg => { if (msg.type() === 'error') console.log('  [page error]', msg.text()); });

    console.log(`Loading ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
    await page.waitForTimeout(4000);

    // try to switch to the 三维模型 (3D model) tab so the mesh loads
    try {
        const tab = page.getByText('三维模型', { exact: true });
        await tab.click({ timeout: 8000 });
        console.log('Clicked 三维模型 tab.');
    } catch {
        console.log('Could not find 三维模型 tab (may already be showing / different layout).');
    }

    // poll until meshes are extractable
    console.log('Waiting for the model mesh to decode…');
    const deadline = Date.now() + timeout;
    let result = null;
    const ready = r => r && r.meshes && r.meshes.some(m =>
        m.diag && m.diag.some(d => d && d.hasMap) && m.position.length / 3 > 1000);
    while (Date.now() < deadline) {
        result = await page.evaluate(pageExtract);
        if (ready(result)) break;
        const n = result && result.meshes ? result.meshes.length : 0;
        console.log(`  …${n} mesh(es) so far, no textured model yet; waiting`);
        await page.waitForTimeout(2500);
    }

    if (!ready(result)) {
        console.error('Extraction failed:', result);
        if (headed) { console.log('Leaving browser open for inspection (headed mode).'); await page.waitForTimeout(600000); }
        await browser.close();
        process.exit(1);
    }

    const totalV = result.meshes.reduce((s, m) => s + m.position.length / 3, 0);
    console.log(`Extracted ${result.meshes.length} mesh(es), ${totalV} vertices (BFS steps: ${result.steps}).`);
    for (const m of result.meshes) {
        console.log(`  · ${m.name}: ${m.position.length / 3} v, textures=${JSON.stringify(m.textures)}`);
        console.log(`      diag=${JSON.stringify(m.diag)}`);
    }

    // copy textures next to the OBJ; build the materialIndex -> file mapping
    const outMat = resolve(outDir, 'materials');
    mkdirSync(outMat, { recursive: true });
    let texFiles = [];
    if (existsSync(texDir)) {
        texFiles = readdirSync(texDir)
            .filter(f => /^texture_\d+\.(jpg|jpeg|png)$/i.test(f))
            .sort((a, b) => parseInt(a.match(/\d+/)) - parseInt(b.match(/\d+/)));
        for (const f of readdirSync(texDir)) copyFileSync(resolve(texDir, f), resolve(outMat, f));
    }
    console.log(`Texture files (materialIndex order): ${JSON.stringify(texFiles)}`);

    // export only real textured meshes (drop bounding-box / gizmo helpers)
    let exportMeshes = result.meshes.filter(m => m.diag && m.diag.some(d => d && d.hasMap));
    if (exportMeshes.length === 0) {  // fallback: keep the largest mesh
        exportMeshes = [result.meshes.reduce((a, b) => (b.position.length > a.position.length ? b : a))];
    }
    console.log(`Exporting ${exportMeshes.length} of ${result.meshes.length} meshes (textured).`);

    const { obj, mtl } = buildObjMtl(exportMeshes, texFiles);
    writeFileSync(resolve(outDir, 'model.obj'), obj);
    writeFileSync(resolve(outDir, 'model.mtl'), mtl);
    writeFileSync(resolve(outDir, 'extract_raw.json'),
        JSON.stringify({
            url, meshCount: result.meshes.length, totalVertices: totalV,
            meshes: result.meshes.map(m => ({ name: m.name, nv: m.position.length / 3, textures: m.textures, diag: m.diag })),
        }, null, 2));
    console.log(`Wrote ${resolve(outDir, 'model.obj')} (+ model.mtl, materials/)`);

    await browser.close();
})().catch(err => { console.error(err); process.exit(1); });
