import { Mat4, Vec3 } from 'playcanvas';

import { ElementType } from './element';
import { Events } from './events';
import { PngCompressor } from './png-compressor';
import { Scene } from './scene';
import { Splat } from './splat';

// ---------------------------------------------------------------------------
// Camera trajectory recorder + playback
//
// Recording: camera poses are sampled at a fixed rate during free navigation
// (only poses are stored, so motion stays smooth). On stop, every sampled pose
// is re-rendered offscreen at the configured resolution and uploaded to the
// FastAPI backend as two PNGs per frame — the RGB screenshot (frames/) and a
// grayscale opacity map built from the render's alpha channel (opacity/) —
// together with a transforms.json that matches the reference OpenGL/NeRF C2W
// format (see CLAUDE.md).
//
// Playback: a previously saved trajectory can be selected and replayed in the
// editor. Each stored C2W matrix is converted back to a PlayCanvas camera pose
// and the camera is driven through the frames over time (at the panel FPS),
// interpolating between consecutive poses for smooth motion.
// ---------------------------------------------------------------------------

type Sample = {
    // PlayCanvas world transform of the camera at sample time (column-major data)
    world: number[];
    // vertical FOV at sample time, used to derive intrinsics
    fov: number;
};

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
    let dedupe = false; // when true, drop consecutive duplicate (stationary) poses on save
    let compressor: PngCompressor | null = null;

    // --- playback state ----------------------------------------------------
    let playing = false;
    let playPoses: { position: Vec3; target: Vec3 }[] = [];
    let playHead = 0; // continuous frame index advanced by dt * fps

    // --- UI handles + helpers (declared early; assigned in buildUI) --------
    let statusEl: HTMLElement;
    let toggleBtn: HTMLButtonElement;
    let fpsInput: HTMLInputElement;
    let resInput: HTMLInputElement;
    let dedupeInput: HTMLInputElement;
    let sessionSelect: HTMLSelectElement;
    let refreshBtn: HTMLButtonElement;
    let playBtn: HTMLButtonElement;

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
        if (dedupeInput) {
            dedupeInput.disabled = recording || playing;
        }

        if (playBtn) {
            playBtn.textContent = playing ? '■ Stop' : '▶ Play';
            playBtn.style.background = playing ? '#c0392b' : '#27ae60';
            playBtn.disabled = recording;
            sessionSelect.disabled = recording || playing;
            refreshBtn.disabled = recording || playing;
        }
    };

    // --- pose sampling -----------------------------------------------------
    const takeSample = () => {
        samples.push({
            world: Array.from(camera.worldTransform.data),
            fov: camera.fov
        });
    };

    // --- coordinate conversion: PlayCanvas world -> OpenGL/NeRF C2W rows ---
    // The target transforms.json uses the OpenGL / NeRF camera-to-world
    // convention (+X right, +Y up, camera looks -Z). PlayCanvas cameras already
    // use exactly this convention, so no axis flip is applied here — we only
    // re-express the pose in the splat's native (ply) frame. (camera_model
    // "OPENCV" in the output refers to the intrinsics/distortion model only.)
    const toOpenGlC2W = (worldData: number[]): number[][] => {
        const splats = scene.getElementsByType(ElementType.splat) as Splat[];
        const camWorld = new Mat4();
        camWorld.data.set(worldData);

        let c2w = camWorld;
        if (splats.length > 0) {
            // express the pose in the splat's native (ply) frame, cancelling
            // whatever transform SuperSplat applied to the splat on load
            const invSplat = splats[0].worldTransform.clone().invert();
            c2w = new Mat4().mul2(invSplat, camWorld);
        }

        // playcanvas Mat4.data is column-major; emit row-major rows
        const d = c2w.data;
        const rows: number[][] = [];
        for (let r = 0; r < 4; r++) {
            rows.push([d[r], d[4 + r], d[8 + r], d[12 + r]]);
        }
        return rows;
    };

    // --- inverse conversion: OpenGL/NeRF C2W rows -> camera position+target -
    // Reverses toOpenGlC2W: re-apply the splat's load transform to bring the
    // pose back into PlayCanvas world space, then extract the camera position
    // (translation) and a look-at target along the camera's local -Z (forward).
    const c2wRowsToPose = (rows: number[][]): { position: Vec3; target: Vec3 } => {
        const splats = scene.getElementsByType(ElementType.splat) as Splat[];

        // rebuild a column-major Mat4 from the row-major rows
        const c2w = new Mat4();
        const cd = c2w.data;
        for (let r = 0; r < 4; r++) {
            for (let c = 0; c < 4; c++) {
                cd[c * 4 + r] = rows[r][c];
            }
        }

        let camWorld = c2w;
        if (splats.length > 0) {
            camWorld = new Mat4().mul2(splats[0].worldTransform, c2w);
        }

        const m = camWorld.data;
        const position = new Vec3(m[12], m[13], m[14]);
        // camera looks down its local -Z; column 2 is the camera's +Z (backward)
        const fwd = new Vec3(-m[8], -m[9], -m[10]).normalize();
        // pick a target distance that normalizes to 1 (avoids the zoom clamp)
        const dist = camera.sceneRadius / camera.fovFactor;
        const target = new Vec3(
            position.x + fwd.x * dist,
            position.y + fwd.y * dist,
            position.z + fwd.z * dist
        );
        return { position, target };
    };

    // --- PlayCanvas world transform -> camera position + look-at target ---
    // Used at export time to drive the camera back to each sampled pose before
    // re-rendering. world is column-major; column 3 is the translation and the
    // camera looks down its local -Z (the negated column 2).
    const worldToPose = (world: number[]): { position: Vec3; target: Vec3 } => {
        const position = new Vec3(world[12], world[13], world[14]);
        const fwd = new Vec3(-world[8], -world[9], -world[10]).normalize();
        const dist = camera.sceneRadius / camera.fovFactor;
        const target = new Vec3(
            position.x + fwd.x * dist,
            position.y + fwd.y * dist,
            position.z + fwd.z * dist
        );
        return { position, target };
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

    // --- undo render.offscreen's vertical flip before compressing ----------
    // render.offscreen returns rows top-to-bottom (origin top-left, for screen-
    // space pixel addressing), but PngCompressor itself flips Y — it expects the
    // GPU's native bottom-up framebuffer data and emits a top-down PNG. Feeding
    // the already-top-down buffer straight in double-flips it, saving every frame
    // upside-down. Flip the rows back to bottom-up so the compressor's flip lands
    // the PNG upright.
    const flipYInPlace = (rgba: Uint8Array, w: number, h: number) => {
        const row = w * 4;
        const tmp = new Uint8Array(row);
        for (let y = 0; y < Math.floor(h / 2); y++) {
            const top = y * row;
            const bot = (h - 1 - y) * row;
            tmp.set(rgba.subarray(top, top + row));
            rgba.copyWithin(top, bot, bot + row);
            rgba.set(tmp, bot);
        }
    };

    // --- build a grayscale opacity PNG from a render's alpha channel -------
    // The offscreen render returns RGBA over a transparent background, so the
    // alpha channel is exactly the splat coverage / opacity. Replicate alpha
    // into RGB (A = 255) and run it through the same compressor as the RGB
    // screenshot, so both PNGs share identical orientation handling.
    const opacityPng = (rgba: Uint8Array, w: number, h: number) => {
        const gray = new Uint8Array(w * h * 4);
        for (let i = 0; i < w * h; i++) {
            const a = rgba[i * 4 + 3];
            gray[i * 4] = a;
            gray[i * 4 + 1] = a;
            gray[i * 4 + 2] = a;
            gray[i * 4 + 3] = 255;
        }
        return compressor.compress(new Uint32Array(gray.buffer), w, h);
    };

    // --- re-render every sample (screenshot + opacity map) and upload ------
    const exportSamples = async () => {
        if (samples.length === 0) {
            setStatus('no poses recorded');
            return;
        }
        if (!compressor) {
            compressor = new PngCompressor();
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
            const sameWorld = (a: number[], b: number[]) => {
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
                if (prev && sameWorld(prev, s.world)) {
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

            // drive the camera back to the sampled pose (damping 0 = snap)
            camera.fov = s.fov;
            const { position, target } = worldToPose(s.world);
            camera.setPose(position, target, 0);

            const rgba = await events.invoke('render.offscreen', width, height) as Uint8Array;
            flipYInPlace(rgba, width, height);
            const imagePng = await compressor.compress(new Uint32Array(rgba.buffer), width, height);
            const maskPng = await opacityPng(rgba, width, height);

            const index = i + 1;
            const name = `frame_${String(index).padStart(5, '0')}.png`;
            const form = new FormData();
            form.append('index', String(index));
            form.append('image', new Blob([imagePng], { type: 'image/png' }), name);
            form.append('opacity', new Blob([maskPng], { type: 'image/png' }), name);
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
        // default to the most recent (last, since names sort chronologically)
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
        updateUI();
        setStatus(`playing ${session} (${playPoses.length} frames)`);
    };

    // advance playback each frame; interpolate between consecutive poses
    events.on('update', (dt: number) => {
        if (!playing || playPoses.length === 0) {
            return;
        }
        const last = playPoses.length - 1;
        playHead += dt * fps;

        if (playHead >= last) {
            const end = playPoses[last];
            camera.setPose(end.position, end.target, 0);
            scene.forceRender = true;
            setStatus('playback finished');
            stopPlayback();
            return;
        }

        const i = Math.floor(playHead);
        const frac = playHead - i;
        const a = playPoses[i];
        const b = playPoses[i + 1];
        const lerp = (u: Vec3, v: Vec3) => new Vec3(
            u.x + (v.x - u.x) * frac,
            u.y + (v.y - u.y) * frac,
            u.z + (v.z - u.z) * frac
        );
        camera.setPose(lerp(a.position, b.position), lerp(a.target, b.target), 0);
        scene.forceRender = true;
    });

    // --- public events -----------------------------------------------------
    events.function('trajectory.recording', () => recording);

    // Shift+R toggles recording (start on first press, stop on next). Ignored
    // while a saved trajectory is playing back.
    events.on('trajectory.toggle', () => {
        if (playing) {
            return;
        }
        if (recording) {
            events.invoke('trajectory.stop');
        } else {
            events.invoke('trajectory.start');
        }
    });

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
        await refreshSessions();
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
                events.invoke('trajectory.stop');
            } else {
                events.invoke('trajectory.start');
            }
        });
        panel.appendChild(toggleBtn);

        // --- playback section ---
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

export { registerTrajectoryRecorderEvents };
