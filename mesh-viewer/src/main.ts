import {
    AmbientLight,
    Box3,
    DirectionalLight,
    PerspectiveCamera,
    SRGBColorSpace,
    Scene,
    Vector3,
    WebGLRenderer
} from 'three';
import { FlyControls } from 'three/examples/jsm/controls/FlyControls.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';

import { registerTrajectoryRecorder, type Viewer } from './trajectory-recorder';

// ---------------------------------------------------------------------------
// three.js viewer for the decoded Realsee mesh (data/model/exported/model.glb,
// served at /model/model.glb by vite.config.ts). Mirrors the splat dev server in
// frontend/: free-navigate the model, record a camera trajectory, export per-frame
// RGB + opacity PNGs + transforms.json to the same FastAPI backend, and play a
// saved trajectory back. The mesh and the panorama cameras share one metric, Y-up,
// OpenGL/NeRF frame (camera looks -Z), so no axis remap is applied anywhere.
// ---------------------------------------------------------------------------

const params = new URLSearchParams(window.location.search.slice(1));
const modelUrl = params.get('model') ?? '/model/model.glb';

const canvas = document.getElementById('view') as HTMLCanvasElement;

const renderer = new WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
    preserveDrawingBuffer: true
});
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setClearColor(0x000000, 0); // transparent: page background shows through
renderer.outputColorSpace = SRGBColorSpace;

const scene = new Scene();
scene.background = null;

const camera = new PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.01, 1000);
camera.position.set(0, 1.5, 3);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 0, 0);

// lights mirror tools/model-extractor/render-preview.mjs so the look matches
scene.add(new AmbientLight(0xffffff, 1.4));
const dir = new DirectionalLight(0xffffff, 0.8);
dir.position.set(1, 2, 1);
scene.add(dir);

let modelRadius = 1;

const frameCamera = (root: import('three').Object3D) => {
    const box = new Box3().setFromObject(root);
    const center = box.getCenter(new Vector3());
    const size = box.getSize(new Vector3());
    modelRadius = Math.max(size.x, size.y, size.z) * 0.5 || 1;

    controls.target.copy(center);
    camera.position.set(
        center.x + modelRadius * 0.6,
        center.y + modelRadius * 0.6,
        center.z + modelRadius * 1.4
    );
    camera.near = Math.max(modelRadius / 1000, 0.001);
    camera.far = modelRadius * 100;
    camera.updateProjectionMatrix();
    controls.update();
};

// ---------------------------------------------------------------------------
// Navigation: two selectable controllers (only one live at a time).
//   Orbit — the default three.js OrbitControls (orbit / pan / zoom).
//   Fly   — three.js FlyControls with its DEFAULT key map: W/S forward-back,
//           A/D strafe, R/F up-down, Q/E roll, arrow keys pitch/yaw, and
//           dragToLook (hold a mouse button to look). Default mode is Fly.
// Fly moves the camera directly; Orbit orbits around controls.target. The
// recorder samples camera.matrixWorld, so recording works in either mode.
// ---------------------------------------------------------------------------
const SPEED_K = 0.5; // movementSpeed = SPEED_K * sceneRadius (metric scale)
// FlyControls' raw default rollSpeed (0.005) makes drag-look / roll almost
// imperceptible; Math.PI/6 rad/s gives a responsive turn (matches the three.js
// fly example's order of magnitude).
const ROLL_SPEED = Math.PI / 6;

let mode: 'fly' | 'orbit' = 'fly';
let fly: FlyControls | null = null;
const state = { playing: false };

// Only the orbit controller listens for input, and only when it's the active
// mode and we're not playing back a trajectory.
const applyControlState = () => {
    controls.enabled = mode === 'orbit' && !state.playing;
};

let updateModeUI = () => {};

const setMode = (m: 'fly' | 'orbit') => {
    mode = m;
    if (m === 'fly') {
        if (!fly) {
            fly = new FlyControls(camera, renderer.domElement);
            fly.dragToLook = true;
            fly.rollSpeed = ROLL_SPEED;
        }
        fly.movementSpeed = modelRadius * SPEED_K;
    } else if (fly) {
        fly.dispose();
        fly = null;
        // re-seat the orbit pivot in front of the camera so orbiting feels right
        // after having flown somewhere
        const fwd = camera.getWorldDirection(new Vector3());
        controls.target.copy(camera.position).addScaledVector(fwd, modelRadius);
    }
    applyControlState();
    if (m === 'orbit') {
        controls.update();
    }
    updateModeUI();
};

new GLTFLoader().load(
    modelUrl,
    (gltf) => {
        scene.add(gltf.scene);
        frameCamera(gltf.scene);
        setMode('fly'); // (re)applies movementSpeed now that sceneRadius is known
    },
    undefined,
    (err) => {
        // eslint-disable-next-line no-console
        console.error('failed to load model', modelUrl, err);
    }
);

// per-frame callbacks (the recorder registers one for playback)
const frameCbs: ((dt: number) => void)[] = [];

const viewer: Viewer = {
    renderer,
    scene,
    camera,
    controls,
    sceneRadius: () => modelRadius,
    onFrame: (cb) => frameCbs.push(cb),
    state,
    applyControlState
};

registerTrajectoryRecorder(viewer);

// --- Fly/Orbit toggle panel (top-left) ---------------------------------------
const buildModeUI = () => {
    const panel = document.createElement('div');
    panel.style.cssText = [
        'position:fixed', 'left:12px', 'top:12px', 'z-index:10000',
        'background:rgba(20,20,20,0.85)', 'color:#fff', 'padding:8px 10px',
        'border-radius:8px', 'font:12px/1.4 sans-serif',
        'box-shadow:0 2px 10px rgba(0,0,0,0.4)'
    ].join(';');

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:6px';
    const mkBtn = (label: string, m: 'fly' | 'orbit') => {
        const b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'border:none;color:#fff;padding:5px 12px;border-radius:4px;cursor:pointer';
        b.addEventListener('click', () => setMode(m));
        return b;
    };
    const flyBtn = mkBtn('Fly', 'fly');
    const orbitBtn = mkBtn('Orbit', 'orbit');
    btnRow.appendChild(flyBtn);
    btnRow.appendChild(orbitBtn);
    panel.appendChild(btnRow);

    const hint = document.createElement('div');
    hint.style.cssText = 'margin-top:6px;opacity:0.75;max-width:230px';
    panel.appendChild(hint);

    updateModeUI = () => {
        flyBtn.style.background = mode === 'fly' ? '#2d8cf0' : '#555';
        orbitBtn.style.background = mode === 'orbit' ? '#2d8cf0' : '#555';
        hint.textContent = mode === 'fly'
            ? 'W/S/A/D 移动 · R/F 升降 · Q/E 滚转 · 方向键转视角 · 按住鼠标拖拽看向'
            : '左键拖拽环绕 · 右键平移 · 滚轮缩放';
    };

    document.body.appendChild(panel);
    updateModeUI();
};
buildModeUI();

// expose for debugging / automated tests
(window as Window & {
    __viewer?: Viewer;
    __app?: { getMode: () => string; getFly: () => FlyControls | null; setMode: (m: 'fly' | 'orbit') => void };
}).__viewer = viewer;
(window as Window & { __app?: unknown }).__app = {
    getMode: () => mode,
    getFly: () => fly,
    setMode
};

window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
});

let last = performance.now();
const animate = () => {
    requestAnimationFrame(animate);
    const now = performance.now();
    const dt = (now - last) / 1000;
    last = now;
    for (const cb of frameCbs) {
        cb(dt);
    }
    if (state.playing) {
        controls.update(); // recorder cb set the pose; OrbitControls applies lookAt
    } else if (mode === 'fly' && fly) {
        fly.update(dt); // FlyControls integrates movement/rotation (no-op when idle)
    } else {
        controls.update();
    }
    renderer.render(scene, camera);
};
animate();
