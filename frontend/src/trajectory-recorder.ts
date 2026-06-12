import { Mat4, Vec3 } from 'playcanvas';

import { ElementType } from './element';
import { Events } from './events';
import { PngCompressor } from './png-compressor';
import { Scene } from './scene';
import { Splat } from './splat';

// ---------------------------------------------------------------------------
// Camera trajectory recorder
//
// While recording, camera poses are sampled at a fixed rate during free
// navigation (only poses are stored, so motion stays smooth). On stop, every
// sampled pose is re-rendered offscreen to a PNG at the configured resolution
// and uploaded to the FastAPI backend together with a transforms.json that
// matches the reference OpenCV C2W format (see CLAUDE.md).
// ---------------------------------------------------------------------------

type Sample = {
    // PlayCanvas world transform of the camera at sample time (column-major data)
    world: number[];
    // orbit state used to faithfully reproduce the pose when re-rendering
    azim: number;
    elev: number;
    distance: number;
    focal: { x: number; y: number; z: number };
    fov: number;
};

// 4x4 diagonal(1,-1,-1,1): converts OpenGL/PlayCanvas camera axes (look -Z,
// +Y up) to OpenCV camera axes (look +Z, +Y down). Right-multiplied onto C2W.
const glToCvFlip = (() => {
    const m = new Mat4();
    m.data[5] = -1; // flip Y
    m.data[10] = -1; // flip Z
    return m;
})();

// resolve the backend base url (override with ?backend=...)
const resolveBackend = () => {
    const param = new URLSearchParams(window.location.search.slice(1)).get('backend');
    return (param ?? 'http://localhost:8000').replace(/\/$/, '');
};

const registerTrajectoryRecorderEvents = (scene: Scene, events: Events) => {
    const backend = resolveBackend();
    const camera = scene.camera;

    let recording = false;
    let intervalId: number | null = null;
    let samples: Sample[] = [];
    let fps = 30;
    let width = 960;
    let height = 540;

    let compressor: PngCompressor | null = null;

    // --- UI handles + helpers (declared early; assigned in buildUI) --------
    let statusEl: HTMLElement;
    let toggleBtn: HTMLButtonElement;
    let fpsInput: HTMLInputElement;
    let resInput: HTMLInputElement;

    const setStatus = (text: string) => {
        if (statusEl) {
            statusEl.textContent = text;
        }
    };

    const updateUI = () => {
        if (!toggleBtn) {
            return;
        }
        toggleBtn.textContent = recording ? '■ Stop' : '● Record';
        toggleBtn.style.background = recording ? '#c0392b' : '#2d8cf0';
        fpsInput.disabled = recording;
        resInput.disabled = recording;
    };

    // --- pose sampling -----------------------------------------------------
    const takeSample = () => {
        samples.push({
            world: Array.from(camera.worldTransform.data),
            azim: camera.azim,
            elev: camera.elevation,
            distance: camera.distance,
            focal: (() => {
                const f = camera.focalPoint;
                return { x: f.x, y: f.y, z: f.z };
            })(),
            fov: camera.fov
        });
    };

    // --- coordinate conversion: PlayCanvas world -> OpenCV C2W rows --------
    const toOpenCvC2W = (worldData: number[]): number[][] => {
        const splats = scene.getElementsByType(ElementType.splat) as Splat[];
        const camWorld = new Mat4();
        camWorld.data.set(worldData);

        let camInScene = camWorld;
        if (splats.length > 0) {
            // express the pose in the splat's native (ply) frame, cancelling
            // whatever transform SuperSplat applied to the splat on load
            const invSplat = splats[0].worldTransform.clone().invert();
            camInScene = new Mat4().mul2(invSplat, camWorld);
        }

        const c2w = new Mat4().mul2(camInScene, glToCvFlip);

        // playcanvas Mat4.data is column-major; emit row-major rows
        const d = c2w.data;
        const rows: number[][] = [];
        for (let r = 0; r < 4; r++) {
            rows.push([d[r], d[4 + r], d[8 + r], d[12 + r]]);
        }
        return rows;
    };

    // --- intrinsics from fov + resolution (square pixels, centered) -------
    const intrinsics = (fovDeg: number) => {
        // offscreen render sets horizontalFov = width > height, so fov applies
        // to the larger axis
        const axis = width >= height ? width : height;
        const fl = (axis / 2) / Math.tan((fovDeg * Math.PI) / 180 / 2);
        return {
            camera_model: 'OPENCV',
            fl_x: fl,
            fl_y: fl,
            cx: width / 2,
            cy: height / 2,
            w: width,
            h: height,
            k1: 0,
            k2: 0,
            p1: 0,
            p2: 0
        };
    };

    // --- render every sample, upload --------------------------------------
    const exportSamples = async () => {
        if (samples.length === 0) {
            setStatus('no frames recorded');
            return;
        }
        if (!compressor) {
            compressor = new PngCompressor();
        }

        // session id derived from sample count + a clock-free counter is not
        // available; use the page performance origin time, which is monotonic
        const session = `rec_${Math.round(performance.now())}`;
        const frames: { file_path: string; transform_matrix: number[][] }[] = [];

        for (let i = 0; i < samples.length; i++) {
            const s = samples[i];
            setStatus(`rendering ${i + 1}/${samples.length}`);

            // reproduce the pose exactly (damping 0 = snap)
            camera.setFocalPoint(new Vec3(s.focal.x, s.focal.y, s.focal.z), 0);
            camera.setAzimElev(s.azim, s.elev, 0);
            camera.setDistance(s.distance, 0);

            const rgba = await events.invoke('render.offscreen', width, height) as Uint8Array;
            const png = await compressor.compress(new Uint32Array(rgba.buffer), width, height);

            const index = i + 1;
            const filePath = `images/frame_${String(index).padStart(5, '0')}.png`;

            const form = new FormData();
            form.append('index', String(index));
            form.append('image', new Blob([png], { type: 'image/png' }), `frame_${String(index).padStart(5, '0')}.png`);
            await fetch(`${backend}/api/recordings/${session}/frame`, { method: 'POST', body: form });

            frames.push({ file_path: filePath, transform_matrix: toOpenCvC2W(s.world) });
        }

        const transforms = { ...intrinsics(samples[0].fov), frames };
        setStatus('saving transforms.json');
        await fetch(`${backend}/api/recordings/${session}/finalize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(transforms)
        });

        setStatus(`saved ${frames.length} frames → output/${session}`);
    };

    // --- public events -----------------------------------------------------
    events.function('trajectory.recording', () => recording);

    events.function('trajectory.start', () => {
        if (recording) {
            return;
        }
        recording = true;
        samples = [];
        takeSample();
        intervalId = window.setInterval(takeSample, 1000 / fps);
        updateUI();
        setStatus('recording…');
    });

    events.function('trajectory.stop', async () => {
        if (!recording) {
            return;
        }
        recording = false;
        if (intervalId !== null) {
            window.clearInterval(intervalId);
            intervalId = null;
        }
        updateUI();
        await exportSamples();
        updateUI();
    });

    // --- minimal floating UI ----------------------------------------------
    const buildUI = () => {
        const panel = document.createElement('div');
        panel.style.cssText = [
            'position:fixed', 'right:12px', 'bottom:12px', 'z-index:10000',
            'background:rgba(20,20,20,0.85)', 'color:#fff', 'padding:10px 12px',
            'border-radius:8px', 'font:12px/1.4 sans-serif', 'min-width:200px',
            'box-shadow:0 2px 10px rgba(0,0,0,0.4)'
        ].join(';');

        const title = document.createElement('div');
        title.textContent = 'Trajectory Recorder';
        title.style.cssText = 'font-weight:600;margin-bottom:8px';
        panel.appendChild(title);

        const row = (labelText: string, input: HTMLInputElement) => {
            const r = document.createElement('label');
            r.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px';
            const span = document.createElement('span');
            span.textContent = labelText;
            input.style.cssText = 'width:90px;background:#333;color:#fff;border:1px solid #555;border-radius:4px;padding:2px 4px';
            r.appendChild(span);
            r.appendChild(input);
            return r;
        };

        fpsInput = document.createElement('input');
        fpsInput.value = String(fps);
        fpsInput.addEventListener('change', () => {
            const v = parseFloat(fpsInput.value);
            if (v > 0) fps = v;
            else fpsInput.value = String(fps);
        });
        panel.appendChild(row('FPS', fpsInput));

        resInput = document.createElement('input');
        resInput.value = `${width}x${height}`;
        resInput.addEventListener('change', () => {
            const m = resInput.value.match(/^(\d+)\s*x\s*(\d+)$/i);
            if (m) {
                width = parseInt(m[1], 10);
                height = parseInt(m[2], 10);
            } else {
                resInput.value = `${width}x${height}`;
            }
        });
        panel.appendChild(row('Resolution', resInput));

        toggleBtn = document.createElement('button');
        toggleBtn.style.cssText = 'width:100%;border:none;color:#fff;padding:6px;border-radius:4px;cursor:pointer;margin-top:2px';
        toggleBtn.addEventListener('click', () => {
            if (recording) {
                events.invoke('trajectory.stop');
            } else {
                events.invoke('trajectory.start');
            }
        });
        panel.appendChild(toggleBtn);

        statusEl = document.createElement('div');
        statusEl.style.cssText = 'margin-top:8px;opacity:0.8;min-height:16px';
        panel.appendChild(statusEl);

        document.body.appendChild(panel);
        updateUI();
        setStatus('idle');
    };

    buildUI();
};

export { registerTrajectoryRecorderEvents };
