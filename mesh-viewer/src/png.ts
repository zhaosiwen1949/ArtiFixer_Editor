// Encode an RGBA pixel buffer (as returned by WebGLRenderer.readRenderTargetPixels)
// into a PNG Blob via a 2D canvas — the three.js equivalent of the frontend's
// lodepng worker.
//
// Orientation: WebGL framebuffer reads are BOTTOM-UP (row 0 = bottom of the
// image). We flip exactly once here into a top-down ImageData so the saved PNG is
// upright. (This is the single, explicit flip that the splat path got wrong by
// flipping twice — render.offscreen + the lodepng worker.)

type Mode = 'rgb' | 'opacity';

const toBlob = (canvas: HTMLCanvasElement): Promise<Blob> =>
    new Promise((resolve, reject) => {
        canvas.toBlob((b) => {
            if (b) {
                resolve(b);
            } else {
                reject(new Error('canvas.toBlob returned null'));
            }
        }, 'image/png');
    });

// src is bottom-up RGBA (width*height*4). mode 'rgb' keeps colour (+ the render's
// alpha = transparent background); mode 'opacity' emits a grayscale coverage mask
// (mesh-covered pixel -> the foreground value, transparent background -> 0). The
// foreground value is `round(255 * opacityScale)` clamped to [0,255], so the global
// opacity scale (default 1.0 -> 255) lets callers dim the whole mask uniformly.
const pixelsToPngBlob = async (
    src: Uint8Array,
    width: number,
    height: number,
    mode: Mode,
    opacityScale = 1.0
): Promise<Blob> => {
    const fg = Math.max(0, Math.min(255, Math.round(255 * opacityScale)));
    const out = new Uint8ClampedArray(width * height * 4);
    const row = width * 4;
    for (let y = 0; y < height; y++) {
        const srcOff = (height - 1 - y) * row; // bottom-up source
        const dstOff = y * row; // top-down destination
        if (mode === 'rgb') {
            out.set(src.subarray(srcOff, srcOff + row), dstOff);
        } else {
            for (let x = 0; x < width; x++) {
                const a = src[srcOff + x * 4 + 3] > 0 ? fg : 0;
                const d = dstOff + x * 4;
                out[d] = a;
                out[d + 1] = a;
                out[d + 2] = a;
                out[d + 3] = 255;
            }
        }
    }

    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
        throw new Error('failed to get 2d context');
    }
    ctx.putImageData(new ImageData(out, width, height), 0, 0);
    return toBlob(canvas);
};

export { pixelsToPngBlob };
