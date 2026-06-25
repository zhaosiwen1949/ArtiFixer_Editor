#!/usr/bin/env node
/**
 * Sanity-render the exported OBJ+MTL with three.js in headless Chromium and
 * save a screenshot, to visually confirm geometry + textures + UV orientation.
 *
 *   node render-preview.mjs            # renders ../../data/model/exported/model.obj
 */
import { chromium } from 'playwright';
import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const dir = resolve(__dirname, process.argv[2] ?? '../../data/model/exported');
const shot = resolve(dir, 'preview.png');

const MIME = { '.obj': 'text/plain', '.mtl': 'text/plain', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.html': 'text/html' };
const server = http.createServer((req, res) => {
    const p = resolve(dir, '.' + decodeURIComponent(req.url.split('?')[0]));
    if (!existsSync(p)) { res.writeHead(404); return res.end('nf'); }
    res.writeHead(200, { 'Content-Type': MIME[extname(p)] ?? 'application/octet-stream', 'Access-Control-Allow-Origin': '*' });
    res.end(readFileSync(p));
});
await new Promise(r => server.listen(0, r));
const port = server.address().port;

const html = `<!doctype html><html><head><meta charset=utf8>
<style>html,body{margin:0;background:#202024}</style>
<script type="importmap">{"imports":{
 "three":"https://unpkg.com/three@0.160.0/build/three.module.js",
 "three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}</script></head>
<body><script type="module">
import * as THREE from 'three';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { MTLLoader } from 'three/addons/loaders/MTLLoader.js';
const W=1400,H=900;
const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
renderer.setSize(W,H); document.body.appendChild(renderer.domElement);
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x202024);
const camera=new THREE.PerspectiveCamera(45,W/H,0.01,1000);
scene.add(new THREE.AmbientLight(0xffffff,1.4));
const d=new THREE.DirectionalLight(0xffffff,1.0); d.position.set(1,2,1); scene.add(d);
window.__done=false; window.__err=null;
new MTLLoader().setPath('/').load('model.mtl',(mats)=>{
  mats.preload();
  const ol=new OBJLoader().setMaterials(mats).setPath('/');
  ol.load('model.obj',(obj)=>{
    scene.add(obj);
    const box=new THREE.Box3().setFromObject(obj);
    const c=box.getCenter(new THREE.Vector3()), s=box.getSize(new THREE.Vector3());
    const r=Math.max(s.x,s.y,s.z);
    // bird's-eye tilt similar to the reference screenshot
    camera.position.set(c.x+r*0.05, c.y+r*1.5, c.z+r*1.1);
    camera.lookAt(c);
    renderer.render(scene,camera);
    window.__info={center:[c.x,c.y,c.z],size:[s.x,s.y,s.z]};
    window.__done=true;
  },undefined,(e)=>{window.__err=String(e);window.__done=true;});
},undefined,(e)=>{window.__err=String(e);window.__done=true;});
</script></body></html>`;

const browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.on('console', m => console.log('[page]', m.text()));
await page.goto(`http://localhost:${port}/preview.html`).catch(() => {});
await page.setContent(html, { waitUntil: 'load' });
await page.waitForFunction('window.__done===true', { timeout: 60000 });
const info = await page.evaluate('window.__info');
const err = await page.evaluate('window.__err');
if (err) console.log('Loader error:', err);
if (info) console.log('Model center', info.center.map(n => n.toFixed(2)), 'size', info.size.map(n => n.toFixed(2)));
await page.locator('canvas').screenshot({ path: shot });
console.log('Wrote', shot);
await browser.close();
server.close();
