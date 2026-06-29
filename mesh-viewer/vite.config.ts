import { createReadStream, existsSync, statSync } from 'node:fs';
import { extname, resolve } from 'node:path';

import type { Plugin } from 'vite';
import { defineConfig } from 'vite';

// The decoded mesh lives outside this folder, under the repo's gitignored data/
// dir (data/model/exported/), produced by tools/model-extractor. Rather than copy
// it into the app, serve that directory under the /model/ URL prefix during dev
// (and preview), so GLTFLoader can fetch /model/model.glb directly.
const MODEL_DIR = resolve(__dirname, '../data/model/exported');

const MIME: Record<string, string> = {
    '.glb': 'model/gltf-binary',
    '.gltf': 'model/gltf+json',
    '.bin': 'application/octet-stream',
    '.obj': 'text/plain',
    '.mtl': 'text/plain',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png'
};

const serveModel = (): Plugin => {
    const middleware = (req: any, res: any, next: () => void) => {
        const url: string = req.url ?? '';
        if (!url.startsWith('/model/')) {
            next();
            return;
        }
        const rel = decodeURIComponent(url.slice('/model/'.length).split('?')[0]);
        const file = resolve(MODEL_DIR, rel);
        // keep within MODEL_DIR
        if (!file.startsWith(MODEL_DIR) || !existsSync(file) || !statSync(file).isFile()) {
            res.statusCode = 404;
            res.end('not found');
            return;
        }
        res.setHeader('Content-Type', MIME[extname(file).toLowerCase()] ?? 'application/octet-stream');
        res.setHeader('Content-Length', String(statSync(file).size));
        res.setHeader('Access-Control-Allow-Origin', '*');
        createReadStream(file).pipe(res);
    };
    return {
        name: 'serve-exported-model',
        configureServer(server) {
            server.middlewares.use(middleware);
        },
        configurePreviewServer(server) {
            server.middlewares.use(middleware);
        }
    };
};

export default defineConfig({
    plugins: [serveModel()],
    server: {
        port: 5173,
        fs: {
            // allow reading the sibling data/ dir
            allow: [resolve(__dirname, '..')]
        }
    }
});
