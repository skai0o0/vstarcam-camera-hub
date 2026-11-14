/**
 * Live camera audio over `/ws/audio/{uid}` (8kHz mono G.711 / PCM), played with Web Audio.
 * Each binary message is `[1B codec][payload]`: 0 = u-law, 1 = a-law, 2 = PCM s16le.
 */

const RATE = 8000;
const LEAD_SEC = 0.3;      // jitter cushion before the first chunk plays
const MAX_LAG_SEC = 1.2;   // if playback falls this far behind, drop the backlog

const ULAW = new Float32Array(256);
const ALAW = new Float32Array(256);
(function buildTables() {
  for (let i = 0; i < 256; i++) {
    let u = ~i & 0xff;
    const sign = u & 0x80;
    const exp = (u >> 4) & 0x07;
    const mant = u & 0x0f;
    let s = (((mant << 3) + 0x84) << exp) - 0x84;
    ULAW[i] = (sign ? -s : s) / 32768;

    let a = i ^ 0x55;
    const aexp = (a & 0x70) >> 4;
    let t = (a & 0x0f) << 4;
    t = aexp === 0 ? t + 8 : (t + 0x108) << (aexp - 1);
    ALAW[i] = ((a & 0x80) ? t : -t) / 32768;
  }
}());

let ctx = null;
let gain = null;
let ws = null;
let currentUid = null;
let nextTime = 0;
let onStateChange = null;

function decode(buf) {
  const view = new Uint8Array(buf);
  const codec = view[0];
  const body = view.subarray(1);
  if (codec === 2) {
    const n = body.length >> 1;
    const out = new Float32Array(n);
    const dv = new DataView(body.buffer, body.byteOffset, n * 2);
    for (let i = 0; i < n; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
    return out;
  }
  const table = codec === 1 ? ALAW : ULAW;
  const out = new Float32Array(body.length);
  for (let i = 0; i < body.length; i++) out[i] = table[body[i]];
  return out;
}

function play(samples) {
  if (!ctx || !samples.length) return;
  const now = ctx.currentTime;
  if (nextTime < now || nextTime - now > MAX_LAG_SEC) nextTime = now + LEAD_SEC;
  const buffer = ctx.createBuffer(1, samples.length, RATE);   // resampled by the context
  buffer.copyToChannel(samples, 0);
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(gain);
  src.start(nextTime);
  nextTime += buffer.duration;
}

function notify(state) {
  if (onStateChange) onStateChange(currentUid, state);
}

/** Start listening to `uid` (must be called from a user gesture so audio may start). */
export function startLiveAudio(uid, stateCb = null) {
  stopLiveAudio();
  onStateChange = stateCb;
  currentUid = uid;
  if (!ctx) {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    // Camera mics are quiet (~-37 dB mean): boost, with a compressor so peaks don't clip.
    gain = ctx.createGain();
    gain.gain.value = 4.0;
    const comp = ctx.createDynamicsCompressor();
    gain.connect(comp);
    comp.connect(ctx.destination);
  }
  ctx.resume().catch(() => {});
  nextTime = 0;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const sock = new WebSocket(`${proto}//${location.host}/ws/audio/${encodeURIComponent(uid)}`);
  sock.binaryType = 'arraybuffer';
  ws = sock;
  notify('connecting');
  let gotData = false;
  sock.onmessage = (ev) => {
    if (ws !== sock || typeof ev.data === 'string') return;
    if (!gotData) { gotData = true; notify('playing'); }
    play(decode(ev.data));
  };
  sock.onclose = () => {
    if (ws === sock) { ws = null; notify('stopped'); }
  };
  sock.onerror = () => {
    if (ws === sock) notify('error');
  };
}

export function stopLiveAudio() {
  const sock = ws;
  ws = null;
  if (sock) {
    try { sock.close(); } catch (_) { /* already closed */ }
  }
  if (currentUid) notify('stopped');
  currentUid = null;
}

export function liveAudioUid() {
  return ws ? currentUid : null;
}
