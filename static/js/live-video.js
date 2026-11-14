/**
 * Live video via WebCodecs (`/ws/video/{uid}?codec=hevc|h264`).
 *
 * The hub forwards the camera's compressed stream (HEVC as-is, or H.264 transcoded on its
 * iGPU) and the viewer's device decodes it in hardware, instead of the hub decoding HEVC
 * and re-encoding MJPEG for every viewer. Frames are drawn on a <canvas> laid over the
 * existing <img>; browsers without WebCodecs support for either codec keep the MJPEG path.
 *
 * Wire format after a JSON config message: [1B flags(bit0=key)][4B ts ms BE][Annex-B AU].
 */
import { store } from './api.js';

const CODEC_STRINGS = {
  hevc: 'hev1.1.6.L120.90',
  h264: 'avc1.4D4028',
};
const MAX_DECODE_QUEUE = 25;     // decoder falling behind -> drop to next keyframe
const RECONNECT_MS = 1500;

let chosenCodec;                  // undefined = not probed yet, null = unsupported
let probing = null;
const players = new Map();        // uid -> LivePlayer

function liveModePref() {
  try { return localStorage.getItem('liveVideoMode') || 'auto'; } catch (_) { return 'auto'; }
}

/** Probe once: 'hevc' | 'h264' | null (MJPEG fallback). */
export function probeLiveCodec() {
  if (chosenCodec !== undefined) return Promise.resolve(chosenCodec);
  if (probing) return probing;
  probing = (async () => {
    const pref = liveModePref();
    if (pref === 'mjpeg' || typeof window.VideoDecoder === 'undefined') return null;
    const order = pref === 'h264' ? ['h264'] : ['hevc', 'h264'];
    for (const codec of order) {
      try {
        const res = await VideoDecoder.isConfigSupported({
          codec: CODEC_STRINGS[codec], optimizeForLatency: true,
        });
        if (res && res.supported) return codec;
      } catch (_) { /* try next */ }
    }
    return null;
  })().then((c) => {
    chosenCodec = c;
    console.info('[live-video] codec:', c || 'MJPEG fallback');
    return c;
  });
  return probing;
}

export function liveVideoActive() {
  return Boolean(chosenCodec);
}

export function liveVideoCodec() {
  return chosenCodec || null;
}

function noteFrame(uid) {
  const now = performance.now();
  if (!store.fpsCounters[uid]) store.fpsCounters[uid] = { count: 0, lastTs: now, fps: 0 };
  const fc = store.fpsCounters[uid];
  fc.count++;
  if (now - fc.lastTs >= 1000) {
    fc.fps = Math.round((fc.count * 1000) / (now - fc.lastTs));
    fc.count = 0;
    fc.lastTs = now;
    const pip = document.getElementById(`fps-pip-${uid}`);
    if (pip) pip.textContent = `${fc.fps} FPS`;
    if (store.focusUid === uid) {
      const badge = document.getElementById('focus-fps-badge');
      if (badge) badge.textContent = `${fc.fps} FPS Live`;
    }
  }
}

class LivePlayer {
  constructor(uid, codec) {
    this.uid = uid;
    this.codec = codec;
    this.canvas = null;
    this.ws = null;
    this.decoder = null;
    this.codecString = CODEC_STRINGS[codec];
    this.waitKey = true;
    this.active = true;
    this.errors = [];
    this.lastFrame = null;   // ImageBitmap-free: we redraw by keeping a tiny copy canvas
    this.connect();
  }

  attach(canvas) {
    if (this.canvas === canvas) return;
    if (this.canvas) this.canvas.classList.remove('on');
    this.canvas = canvas;
    if (canvas && this.lastFrame) {
      canvas.width = this.lastFrame.width;
      canvas.height = this.lastFrame.height;
      canvas.getContext('2d').drawImage(this.lastFrame, 0, 0);
      canvas.classList.add('on');
    }
  }

  connect() {
    if (!this.active) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/video/${encodeURIComponent(this.uid)}?codec=${this.codec}`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    let gotMessage = false;
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      if (!gotMessage) { gotMessage = true; this.failStreak = 0; }
      if (typeof ev.data === 'string') {
        try {
          const cfg = JSON.parse(ev.data);
          if (cfg.codecString) this.codecString = cfg.codecString;
        } catch (_) { /* ignore */ }
        this.resetDecoder();
        return;
      }
      this.onChunk(ev.data);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (!gotMessage) {
        // Rejected before any data (e.g. 503: no iGPU for H.264) three times in a row
        this.failStreak = (this.failStreak || 0) + 1;
        if (this.failStreak >= 3) {
          onPlayerFatal(this);
          return;
        }
      }
      if (this.active) setTimeout(() => this.connect(), RECONNECT_MS);
    };
    ws.onerror = () => {};
  }

  resetDecoder() {
    if (this.decoder && this.decoder.state !== 'closed') {
      try { this.decoder.close(); } catch (_) { /* ignore */ }
    }
    this.waitKey = true;
    this.decoder = new VideoDecoder({
      output: (frame) => this.draw(frame),
      error: (e) => this.onDecodeError(e),
    });
    this.decoder.configure({ codec: this.codecString, optimizeForLatency: true });
  }

  onDecodeError(e) {
    const now = performance.now();
    this.errors = this.errors.filter((t) => now - t < 10000);
    this.errors.push(now);
    console.warn('[live-video]', this.uid, e && e.message);
    if (this.errors.length >= 4) {
      // Keeps failing (e.g. HEVC advertised but not really decodable): give up on WebCodecs
      onPlayerFatal(this);
      return;
    }
    if (this.active) this.resetDecoder();
  }

  onChunk(buf) {
    if (!this.decoder || this.decoder.state !== 'configured') return;
    const u8 = new Uint8Array(buf);
    if (u8.length < 6) return;
    const key = (u8[0] & 1) === 1;
    const ts = ((u8[1] << 24) | (u8[2] << 16) | (u8[3] << 8) | u8[4]) >>> 0;
    if (this.waitKey && !key) return;
    if (this.decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
      this.waitKey = true;          // can't keep up: skip to the next keyframe
      if (!key) return;
    }
    this.waitKey = false;
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: key ? 'key' : 'delta',
        timestamp: ts * 1000,
        data: u8.subarray(5),
      }));
    } catch (e) {
      this.onDecodeError(e);
    }
  }

  draw(frame) {
    try {
      const w = frame.displayWidth;
      const h = frame.displayHeight;
      const c = this.canvas;
      if (c) {
        if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
        c.getContext('2d').drawImage(frame, 0, 0);
        c.classList.add('on');
      }
      // Keep a small copy so a canvas swap (grid <-> focus) shows a picture immediately
      if (!this.lastFrame) this.lastFrame = document.createElement('canvas');
      if (performance.now() - (this._lastCopy || 0) > 1000) {
        this._lastCopy = performance.now();
        this.lastFrame.width = w;
        this.lastFrame.height = h;
        this.lastFrame.getContext('2d').drawImage(frame, 0, 0);
      }
      noteFrame(this.uid);
    } finally {
      frame.close();
    }
  }

  stop() {
    this.active = false;
    if (this.canvas) this.canvas.classList.remove('on');
    const ws = this.ws;
    this.ws = null;
    if (ws) { try { ws.close(); } catch (_) { /* ignore */ } }
    if (this.decoder && this.decoder.state !== 'closed') {
      try { this.decoder.close(); } catch (_) { /* ignore */ }
    }
  }
}

let fatalHandler = null;
function onPlayerFatal(player) {
  // Fall back to the next codec (hevc -> h264), then to MJPEG for everyone.
  console.warn('[live-video] decoder keeps failing for', player.codec, '- falling back');
  const next = player.codec === 'hevc' ? 'h264' : null;
  chosenCodec = next;
  for (const p of players.values()) p.stop();
  players.clear();
  if (fatalHandler) fatalHandler();
}

/** Called by the MJPEG module after a codec fallback so it can resume / resync. */
export function onLiveVideoFallback(fn) {
  fatalHandler = fn;
}

function ensureCanvas(container, id) {
  if (!container) return null;
  let cv = document.getElementById(id);
  if (!cv || !container.contains(cv)) {
    cv = document.createElement('canvas');
    cv.id = id;
    cv.className = 'live-canvas';
    // Right after the snapshot <img> so badges/overlays that follow it stay on top
    const img = container.querySelector('img');
    if (img) img.after(cv); else container.prepend(cv);
  }
  return cv;
}

/** Start/stop players so exactly the visible cameras stream (all, or just the focused one). */
export function syncLiveVideo() {
  if (!chosenCodec) return;
  const want = new Map();     // uid -> canvas
  if (!document.hidden) {
    if (store.focusUid) {
      const stage = document.getElementById('focus-stage');
      want.set(store.focusUid, ensureCanvas(stage, 'focus-live-canvas'));
    } else if (store.currentTab === 'live') {
      const gridFps = parseFloat(document.getElementById('grid-fps')?.value || '25') || 0;
      if (gridFps > 0) {
        for (const c of store.cameras) {
          if (c.state !== 'CONNECTED' || c.auth_failed) continue;
          const wrap = document.querySelector(`.cam-video-wrap[data-uid="${CSS.escape(c.uid)}"]`);
          const cv = ensureCanvas(wrap, `live-canvas-${c.uid}`);
          if (cv) want.set(c.uid, cv);
        }
      }
    }
  }
  for (const [uid, p] of players) {
    if (!want.has(uid) || p.codec !== chosenCodec) {
      p.stop();
      players.delete(uid);
    }
  }
  for (const [uid, canvas] of want) {
    let p = players.get(uid);
    if (!p) {
      p = new LivePlayer(uid, chosenCodec);
      players.set(uid, p);
    }
    p.attach(canvas);
  }
}

/** Canvas currently showing the focused camera (for snapshots), if any. */
export function focusLiveCanvas() {
  const cv = document.getElementById('focus-live-canvas');
  return cv && cv.classList.contains('on') && cv.width ? cv : null;
}
