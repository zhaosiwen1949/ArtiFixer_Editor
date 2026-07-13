import {
    Matrix4,
    PerspectiveCamera,
    Quaternion,
    SRGBColorSpace,
    Scene,
    Vector3,
    WebGLRenderer,
    WebGLRenderTarget
} from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

import { pixelsToPngBlob } from './png';

// ---------------------------------------------------------------------------
// Camera trajectory recorder + playback for the three.js mesh viewer.
//
// Port of frontend/src/trajectory-recorder.ts (the splat dev server) to three.js.
// The data contracts are identical — the same FastAPI endpoints and the same
// transforms.json OpenGL/NeRF camera-to-world (C2W) format — so recordings made
// here are interchangeable with the splat ones. Only the engine calls differ.
//
// Unlike the splat path there is NO splat load transform to cancel: the mesh is
// loaded in its native world frame, so C2W is exactly camera.matrixWorld.
// ---------------------------------------------------------------------------

// The viewer surface this module needs from main.ts.
export interface Viewer {
    renderer: WebGLRenderer;
    scene: Scene;
    camera: PerspectiveCamera;
    controls: OrbitControls;
    sceneRadius: () => number;
    onFrame: (cb: (dt: number) => void) => void;
    // shared control state owned by main.ts: `playing` freezes navigation during
    // playback; applyControlState() reasserts which controller is live.
    state: { playing: boolean };
    applyControlState: () => void;
}

type Sample = {
    // three.js camera world matrix at sample time (column-major elements)
    world: number[];
    // vertical FOV (deg) at sample time, used to derive intrinsics
    fov: number;
};

// resolve the backend base url (override with ?backend=...)
const resolveBackend = () => {
    const param = new URLSearchParams(window.location.search.slice(1)).get('backend');
    return (param ?? 'http://localhost:8000').replace(/\/$/, '');
};

const registerTrajectoryRecorder = (viewer: Viewer) => {
    const backend = resolveBackend();
    const { renderer, scene, camera, controls } = viewer;

    let recording = false;
    let intervalId: number | null = null;
    let samples: Sample[] = [];
    let fps = 30;
    let width = 960;
    let height = 540;
    let dedupe = false;
    // global scale applied to the opacity/coverage mask's foreground value
    // (1.0 -> white 255). Settable in the UI; clamped to [0, 1].
    let opacityScale = 1.0;
    // surface-sample count for the COLMAP point cloud (settable in the UI).
    let colmapPoints = 100000;

    // --- playback state ----------------------------------------------------
    let playing = false;
    let playPoses: { position: Vector3; target: Vector3 }[] = [];
    let playHead = 0;
    let prevDamping = controls.enableDamping;

    // --- offscreen render reusables ----------------------------------------
    let rt: WebGLRenderTarget | null = null;
    const offCam = new PerspectiveCamera();

    // --- UI handles --------------------------------------------------------
    let statusEl: HTMLElement;
    let toggleBtn: HTMLButtonElement;
    let fpsInput: HTMLInputElement;
    let resInput: HTMLInputElement;
    let opacityInput: HTMLInputElement;
    let dedupeInput: HTMLInputElement;
    let sessionSelect: HTMLSelectElement;
    let refreshBtn: HTMLButtonElement;
    let playBtn: HTMLButtonElement;
    let colmapPointsInput: HTMLInputElement;
    let colmapBtn: HTMLButtonElement;
    let colmapBusy = false;

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
        toggleBtn.disabled = playing;
        fpsInput.disabled = recording;
        resInput.disabled = recording || playing;
        opacityInput.disabled = recording || playing;
        dedupeInput.disabled = recording || playing;
        playBtn.textContent = playing ? '■ Stop' : '▶ Play';
        playBtn.style.background = playing ? '#c0392b' : '#27ae60';
        playBtn.disabled = recording;
        sessionSelect.disabled = recording || playing;
        refreshBtn.disabled = recording || playing;
        colmapPointsInput.disabled = recording || playing || colmapBusy;
        colmapBtn.disabled = recording || playing || colmapBusy;
    };

    // --- pose sampling -----------------------------------------------------
    const takeSample = () => {
        camera.updateMatrixWorld();
        samples.push({
            world: camera.matrixWorld.elements.slice(),
            fov: camera.fov
        });
    };

    // --- C2W: three.js camera world matrix -> OpenGL/NeRF C2W rows ---------
    // three.js camera.matrixWorld IS the camera-to-world matrix in the OpenGL/NeRF
    // frame (+X right, +Y up, looks -Z) — exactly the transforms.json convention.
    // No axis flip and no splat-load-transform cancellation. Matrix4.elements is
    // column-major, so we transpose into row-major rows.
    const toOpenGlC2W = (e: number[]): number[][] => {
        const rows: number[][] = [];
        for (let r = 0; r < 4; r++) {
            rows.push([e[r], e[4 + r], e[8 + r], e[12 + r]]);
        }
        return rows;
    };

    // --- inverse: C2W rows -> camera position + look-at target -------------
    const c2wRowsToPose = (rows: number[][]): { position: Vector3; target: Vector3 } => {
        // rebuild a column-major Matrix4 from row-major rows
        const e: number[] = new Array(16);
        for (let r = 0; r < 4; r++) {
            for (let c = 0; c < 4; c++) {
                e[c * 4 + r] = rows[r][c];
            }
        }
        const position = new Vector3(e[12], e[13], e[14]);
        const fwd = new Vector3(-e[8], -e[9], -e[10]).normalize();
        const dist = viewer.sceneRadius();
        const target = position.clone().add(fwd.multiplyScalar(dist));
        return { position, target };
    };

    // --- intrinsics from vertical fov + resolution -------------------------
    const intrinsics = (fovDeg: number) => {
        const fl = (height / 2) / Math.tan((fovDeg * Math.PI) / 180 / 2);
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

    // --- render one sampled pose offscreen -> RGBA (bottom-up) -------------
    const renderPose = (s: Sample): Uint8Array => {
        if (!rt || rt.width !== width || rt.height !== height) {
            rt?.dispose();
            rt = new WebGLRenderTarget(width, height);
            rt.texture.colorSpace = SRGBColorSpace; // match the on-screen sRGB look
        }

        // place the offscreen camera at the sampled world pose
        const m = new Matrix4().fromArray(s.world);
        const pos = new Vector3();
        const quat = new Quaternion();
        const scl = new Vector3();
        m.decompose(pos, quat, scl);
        offCam.position.copy(pos);
        offCam.quaternion.copy(quat);
        offCam.scale.copy(scl);
        offCam.fov = s.fov;
        offCam.aspect = width / height;
        offCam.near = camera.near;
        offCam.far = camera.far;
        offCam.updateProjectionMatrix();
        offCam.updateMatrixWorld(true);

        const buf = new Uint8Array(width * height * 4);
        renderer.setRenderTarget(rt);
        renderer.clear();
        renderer.render(scene, offCam);
        renderer.readRenderTargetPixels(rt, 0, 0, width, height, buf);
        renderer.setRenderTarget(null);
        return buf;
    };

    // --- re-render every sample (RGB + opacity) and upload -----------------
    const exportSamples = async () => {
        if (samples.length === 0) {
            setStatus('no poses recorded');
            return;
        }

        // session id = folder creation timestamp (local time), YYYYMMDD_HHMMSS
        const now = new Date();
        const pad = (n: number) => String(n).padStart(2, '0');
        const session = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_` +
            `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;

        // optionally drop consecutive duplicate (stationary) poses
        let kept = samples;
        if (dedupe) {
            const eps = 1e-6;
            const same = (a: number[], b: number[]) => {
                for (let i = 0; i < 16; i++) {
                    if (Math.abs(a[i] - b[i]) > eps) {
                        return false;
                    }
                }
                return true;
            };
            kept = [];
            let prev: number[] | null = null;
            for (const s of samples) {
                if (prev && same(prev, s.world)) {
                    continue;
                }
                kept.push(s);
                prev = s.world;
            }
        }
        const removed = samples.length - kept.length;

        const frames: { transform_matrix: number[][] }[] = [];
        for (let i = 0; i < kept.length; i++) {
            const s = kept[i];
            setStatus(`rendering ${i + 1}/${kept.length}`);

            const rgba = renderPose(s);
            const imagePng = await pixelsToPngBlob(rgba, width, height, 'rgb');
            const maskPng = await pixelsToPngBlob(rgba, width, height, 'opacity', opacityScale);

            const index = i + 1;
            const name = `frame_${String(index).padStart(5, '0')}.png`;
            const form = new FormData();
            form.append('index', String(index));
            form.append('image', imagePng, name);
            form.append('opacity', maskPng, name);
            await fetch(`${backend}/api/recordings/${session}/frame`, { method: 'POST', body: form });

            frames.push({ transform_matrix: toOpenGlC2W(s.world) });
        }

        const transforms = { ...intrinsics(kept[0].fov), frames };
        setStatus('saving transforms.json');
        await fetch(`${backend}/api/recordings/${session}/finalize`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(transforms)
        });

        const dupNote = dedupe ? ` (removed ${removed} duplicate)` : '';
        setStatus(`saved ${frames.length} frames${dupNote} → output/${session}`);
    };

    // --- recording control -------------------------------------------------
    const startRecording = () => {
        if (recording || playing) {
            return;
        }
        recording = true;
        samples = [];
        takeSample();
        intervalId = window.setInterval(takeSample, 1000 / fps);
        updateUI();
        setStatus('recording…');
    };

    const stopRecording = async () => {
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
        await refreshSessions();
        updateUI();
    };

    // --- playback ----------------------------------------------------------
    const populateSessions = (sessions: { session: string; frames: number }[]) => {
        sessionSelect.innerHTML = '';
        if (sessions.length === 0) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = '(no recordings)';
            sessionSelect.appendChild(opt);
            return;
        }
        for (const s of sessions) {
            const opt = document.createElement('option');
            opt.value = s.session;
            opt.textContent = `${s.session} (${s.frames})`;
            sessionSelect.appendChild(opt);
        }
        sessionSelect.value = sessions[sessions.length - 1].session;
    };

    const refreshSessions = async () => {
        try {
            const res = await fetch(`${backend}/api/recordings`);
            const data = await res.json();
            populateSessions(data.sessions ?? []);
        } catch (e) {
            setStatus('failed to list recordings');
        }
    };

    const stopPlayback = () => {
        if (!playing) {
            return;
        }
        playing = false;
        viewer.state.playing = false;
        viewer.applyControlState();
        controls.enableDamping = prevDamping;
        updateUI();
    };

    const startPlayback = async () => {
        if (playing || recording) {
            return;
        }
        const session = sessionSelect.value;
        if (!session) {
            setStatus('no trajectory selected');
            return;
        }
        setStatus(`loading ${session}…`);
        try {
            const res = await fetch(`${backend}/api/recordings/${session}/transforms`);
            if (!res.ok) {
                setStatus('failed to load trajectory');
                return;
            }
            const data = await res.json();
            const frames = (data.frames ?? []) as { transform_matrix: number[][] }[];
            if (frames.length === 0) {
                setStatus('trajectory has no frames');
                return;
            }
            playPoses = frames.map(f => c2wRowsToPose(f.transform_matrix));
        } catch (e) {
            setStatus('failed to load trajectory');
            return;
        }
        playHead = 0;
        playing = true;
        viewer.state.playing = true;
        viewer.applyControlState(); // freeze fly/orbit input while playing
        prevDamping = controls.enableDamping;
        controls.enableDamping = false; // exact, no residual damping during playback
        updateUI();
        setStatus(`playing ${session} (${playPoses.length} frames)`);
    };

    const drivePose = (p: { position: Vector3; target: Vector3 }) => {
        camera.position.copy(p.position);
        controls.target.copy(p.target);
    };

    // advance playback each render frame; interpolate between consecutive poses
    viewer.onFrame((dt: number) => {
        if (!playing || playPoses.length === 0) {
            return;
        }
        const last = playPoses.length - 1;
        playHead += dt * fps;

        if (playHead >= last) {
            drivePose(playPoses[last]);
            setStatus('playback finished');
            stopPlayback();
            return;
        }

        const i = Math.floor(playHead);
        const frac = playHead - i;
        const a = playPoses[i];
        const b = playPoses[i + 1];
        drivePose({
            position: a.position.clone().lerp(b.position, frac),
            target: a.target.clone().lerp(b.target, frac)
        });
    });

    // --- keyboard shortcut: Shift+R toggles recording ----------------------
    window.addEventListener('keydown', (e) => {
        if (e.shiftKey && (e.key === 'R' || e.key === 'r')) {
            if (playing) {
                return;
            }
            if (recording) {
                stopRecording();
            } else {
                startRecording();
            }
        }
    });

    // --- COLMAP export -----------------------------------------------------
    // Ask the backend to turn the selected saved session into a COLMAP sparse
    // model (sparse/0/{cameras,images,points3D}.bin) + init_3dgs.ply. The heavy
    // work (mesh sampling, occlusion-aware visibility) runs in Python on the
    // backend; this just triggers it and reports the result.
    const exportColmap = async () => {
        if (recording || playing || colmapBusy) {
            return;
        }
        const session = sessionSelect.value;
        if (!session) {
            setStatus('no trajectory selected');
            return;
        }
        colmapBusy = true;
        updateUI();
        setStatus(`converting ${session} → COLMAP (${colmapPoints} pts)…`);
        try {
            const res = await fetch(`${backend}/api/recordings/${session}/colmap`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ num_points: colmapPoints })
            });
            if (!res.ok) {
                let detail = `${res.status}`;
                try {
                    detail = (await res.json()).detail ?? detail;
                } catch { /* non-JSON error body */ }
                setStatus(`COLMAP failed: ${detail}`);
                return;
            }
            const s = await res.json();
            setStatus(`COLMAP: ${s.n_points} pts, ${s.n_images} imgs, ` +
                `${s.n_observations} obs → ${session}/sparse/0`);
        } catch (e) {
            setStatus('COLMAP request failed (backend running?)');
        } finally {
            colmapBusy = false;
            updateUI();
        }
    };

    // --- floating UI panel -------------------------------------------------
    const buildUI = () => {
        const panel = document.createElement('div');
        panel.style.cssText = [
            'position:fixed', 'right:12px', 'bottom:12px', 'z-index:10000',
            'background:rgba(20,20,20,0.85)', 'color:#fff', 'padding:10px 12px',
            'border-radius:8px', 'font:12px/1.4 sans-serif', 'min-width:200px',
            'box-shadow:0 2px 10px rgba(0,0,0,0.4)'
        ].join(';');

        const title = document.createElement('div');
        title.textContent = 'Mesh Trajectory Recorder';
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
            if (v > 0) {
                fps = v;
            } else {
                fpsInput.value = String(fps);
            }
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

        opacityInput = document.createElement('input');
        opacityInput.type = 'number';
        opacityInput.min = '0';
        opacityInput.max = '1';
        opacityInput.step = '0.05';
        opacityInput.value = String(opacityScale);
        opacityInput.title = 'Global scale for the opacity mask foreground (1.0 = white 255)';
        opacityInput.addEventListener('change', () => {
            const v = parseFloat(opacityInput.value);
            if (Number.isFinite(v) && v >= 0 && v <= 1) {
                opacityScale = v;
            }
            opacityInput.value = String(opacityScale);
        });
        panel.appendChild(row('Opacity scale', opacityInput));

        dedupeInput = document.createElement('input');
        dedupeInput.type = 'checkbox';
        dedupeInput.checked = dedupe;
        dedupeInput.style.cssText = 'margin:0;cursor:pointer';
        dedupeInput.addEventListener('change', () => {
            dedupe = dedupeInput.checked;
        });
        const dedupeRow = document.createElement('label');
        dedupeRow.title = 'Drop consecutive identical (stationary) camera poses when saving';
        dedupeRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:pointer';
        dedupeRow.appendChild(dedupeInput);
        const dedupeSpan = document.createElement('span');
        dedupeSpan.textContent = 'Remove duplicate poses';
        dedupeRow.appendChild(dedupeSpan);
        panel.appendChild(dedupeRow);

        toggleBtn = document.createElement('button');
        toggleBtn.title = 'Shortcut: Shift+R';
        toggleBtn.style.cssText = 'width:100%;border:none;color:#fff;padding:6px;border-radius:4px;cursor:pointer;margin-top:2px';
        toggleBtn.addEventListener('click', () => {
            if (recording) {
                stopRecording();
            } else {
                startRecording();
            }
        });
        panel.appendChild(toggleBtn);

        const divider = document.createElement('div');
        divider.style.cssText = 'border-top:1px solid #444;margin:10px 0 8px';
        panel.appendChild(divider);

        const pbTitle = document.createElement('div');
        pbTitle.textContent = 'Playback';
        pbTitle.style.cssText = 'font-weight:600;margin-bottom:6px';
        panel.appendChild(pbTitle);

        sessionSelect = document.createElement('select');
        sessionSelect.style.cssText = 'width:100%;background:#333;color:#fff;border:1px solid #555;border-radius:4px;padding:3px 4px;margin-bottom:6px';
        panel.appendChild(sessionSelect);

        const pbRow = document.createElement('div');
        pbRow.style.cssText = 'display:flex;gap:6px';

        refreshBtn = document.createElement('button');
        refreshBtn.textContent = '⟳';
        refreshBtn.title = 'Refresh list';
        refreshBtn.style.cssText = 'flex:0 0 auto;border:none;color:#fff;background:#555;padding:6px 10px;border-radius:4px;cursor:pointer';
        refreshBtn.addEventListener('click', () => {
            refreshSessions();
        });
        pbRow.appendChild(refreshBtn);

        playBtn = document.createElement('button');
        playBtn.style.cssText = 'flex:1 1 auto;border:none;color:#fff;padding:6px;border-radius:4px;cursor:pointer';
        playBtn.addEventListener('click', () => {
            if (playing) {
                stopPlayback();
                setStatus('playback stopped');
            } else {
                startPlayback();
            }
        });
        pbRow.appendChild(playBtn);
        panel.appendChild(pbRow);

        // --- COLMAP export section ---
        const cmDivider = document.createElement('div');
        cmDivider.style.cssText = 'border-top:1px solid #444;margin:10px 0 8px';
        panel.appendChild(cmDivider);

        const cmTitle = document.createElement('div');
        cmTitle.textContent = 'COLMAP export';
        cmTitle.style.cssText = 'font-weight:600;margin-bottom:6px';
        panel.appendChild(cmTitle);

        colmapPointsInput = document.createElement('input');
        colmapPointsInput.type = 'number';
        colmapPointsInput.min = '1';
        colmapPointsInput.step = '10000';
        colmapPointsInput.value = String(colmapPoints);
        colmapPointsInput.title = 'Surface-sample count for the point cloud (points3D.bin)';
        colmapPointsInput.addEventListener('change', () => {
            const v = parseInt(colmapPointsInput.value, 10);
            if (Number.isFinite(v) && v >= 1) {
                colmapPoints = v;
            }
            colmapPointsInput.value = String(colmapPoints);
        });
        panel.appendChild(row('Points', colmapPointsInput));

        colmapBtn = document.createElement('button');
        colmapBtn.textContent = 'Export COLMAP';
        colmapBtn.title = 'Convert the selected session → sparse/0/*.bin + init_3dgs.ply';
        colmapBtn.style.cssText = 'width:100%;border:none;color:#fff;background:#8e44ad;padding:6px;border-radius:4px;cursor:pointer';
        colmapBtn.addEventListener('click', () => {
            exportColmap();
        });
        panel.appendChild(colmapBtn);

        statusEl = document.createElement('div');
        statusEl.style.cssText = 'margin-top:8px;opacity:0.8;min-height:16px';
        panel.appendChild(statusEl);

        document.body.appendChild(panel);
        updateUI();
        setStatus('idle');
        refreshSessions();
    };

    buildUI();
};

export { registerTrajectoryRecorder };
