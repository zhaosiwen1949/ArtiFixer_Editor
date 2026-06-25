#!/usr/bin/env node
/**
 * Alignment check: overlay the panorama camera positions (observers_raw.json)
 * on the extracted mesh (model.obj) and render top-down + bird's-eye views.
 * If the cameras land inside the rooms, the mesh and the camera files share the
 * same coordinate frame (no transform needed).
 *
 *   node align-overlay.mjs
 */
import { chromium } from 'playwright';
import http from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const dir = resolve(__dirname, '../../data/model/exported');
const obsPath = resolve(__dirname, '../../data/panoramas/observers_raw.json');
const observers = JSON.parse(readFileSync(obsPath, 'utf8'))
    .map(o => ({ p: o.position, q: o.quaternion }));

const MIME = { '.obj': 'text/plain', '.mtl': 'text/plain', '.jpg': 'image/jpeg', '.png': 'image/png' };
const server = http.createServer((req, res) => {
    const p = resolve(dir, '.' + decodeURIComponent(req.url.split('?')[0]));
    if (!existsSync(p)) { res.writeHead(404); return res.end('nf'); }
    res.writeHead(200, { 'Content-Type': MIME[extname(p)] ?? 'application/octet-stream', 'Access-Control-Allow-Origin': '*' });
    res.end(readFileSync(p));
});
await new Promise(r => server.listen(0, r));
const port = server.address().port;

const html = `<!doctype html><html><head><meta charset=utf8>
<style>html,body{margin:0;background:#15161a}</style>
<script type="importmap">{"imports":{
 "three":"https://unpkg.com/three@0.160.0/build/three.module.js",
 "three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}</script></head>
<body><script type="module">
import * as THREE from 'three';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { MTLLoader } from 'three/addons/loaders/MTLLoader.js';
const OBS = ${JSON.stringify(observers)};
const W=1400,H=900;
const renderer=new THREE.WebGLRenderer({antialias:true,preserveDrawingBuffer:true});
renderer.setSize(W,H); renderer.outputColorSpace=THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x15161a);
scene.add(new THREE.AmbientLight(0xffffff,1.5));
const dl=new THREE.DirectionalLight(0xffffff,0.8); dl.position.set(1,2,1); scene.add(dl);
window.__done=false; window.__err=null; window.__shots={};

function addCameras(){
  // red sphere at each camera centre + a yellow tick along its view dir (-Z local)
  const g=new THREE.SphereGeometry(0.16,16,16);
  const m=new THREE.MeshBasicMaterial({color:0xff3030});
  const grp=new THREE.Group();
  for(const o of OBS){
    const s=new THREE.Mesh(g,m); s.position.set(o.p[0],o.p[1],o.p[2]); grp.add(s);
    const q=new THREE.Quaternion(o.q.x,o.q.y,o.q.z,o.q.w);
    const fwd=new THREE.Vector3(0,0,-1).applyQuaternion(q).multiplyScalar(0.6);
    const pts=[new THREE.Vector3(o.p[0],o.p[1],o.p[2]),
               new THREE.Vector3(o.p[0]+fwd.x,o.p[1]+fwd.y,o.p[2]+fwd.z)];
    grp.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
            new THREE.LineBasicMaterial({color:0xffd000})));
  }
  scene.add(grp);
}

new MTLLoader().setPath('/').load('model.mtl',(mats)=>{
  mats.preload();
  new OBJLoader().setMaterials(mats).setPath('/').load('model.obj',(obj)=>{
    scene.add(obj); addCameras();
    const box=new THREE.Box3().setFromObject(obj);
    const c=box.getCenter(new THREE.Vector3()), s=box.getSize(new THREE.Vector3());
    const r=Math.max(s.x,s.y,s.z);

    // view 1: top-down orthographic floor plan
    const ortho=new THREE.OrthographicCamera(-s.x/2*1.1, s.x/2*1.1, s.z/2*1.1, -s.z/2*1.1, 0.01, r*4);
    ortho.position.set(c.x, c.y+r, c.z); ortho.up.set(0,0,-1); ortho.lookAt(c);
    renderer.render(scene,ortho);
    window.__shots.top = renderer.domElement.toDataURL('image/png');

    // view 2: bird's-eye perspective
    const persp=new THREE.PerspectiveCamera(45,W/H,0.01,1000);
    persp.position.set(c.x+r*0.05, c.y+r*1.5, c.z+r*1.1); persp.lookAt(c);
    renderer.render(scene,persp);
    window.__shots.birdseye = renderer.domElement.toDataURL('image/png');

    window.__info={center:[c.x,c.y,c.z],size:[s.x,s.y,s.z],n:OBS.length};
    window.__done=true;
  },undefined,e=>{window.__err=String(e);window.__done=true;});
},undefined,e=>{window.__err=String(e);window.__done=true;});
</script></body></html>`;

const browser = await chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto(`http://localhost:${port}/__o.html`).catch(() => {});
await page.setContent(html, { waitUntil: 'load' });
await page.waitForFunction('window.__done===true', { timeout: 60000 });
const err = await page.evaluate('window.__err');
if (err) { console.error('Render error:', err); }
const info = await page.evaluate('window.__info');
console.log('Mesh center', info.center.map(n => n.toFixed(2)), 'size', info.size.map(n => n.toFixed(2)), '| cameras', info.n);
for (const [name, file] of [['top', 'align_top.png'], ['birdseye', 'align_birdseye.png']]) {
    const dataUrl = await page.evaluate(`window.__shots.${name}`);
    const b64 = dataUrl.split(',')[1];
    const { writeFileSync } = await import('node:fs');
    writeFileSync(resolve(dir, file), Buffer.from(b64, 'base64'));
    console.log('Wrote', resolve(dir, file));
}
await browser.close();
server.close();
