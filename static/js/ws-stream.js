/**
 * Multiplexed Real-Time 25 FPS Binary WebSocket video receiver.
 */
import { store } from './api.js';

let ws = null;
let reconnectTimer = null;

function setWsBadge(connected) {
  const badge = document.getElementById('hdr-ws-status');
  if (!badge) return;
  if (connected) {
    badge.className = 'badge ok';
    badge.innerHTML = '<span class="status-dot"></span><span>Live WS</span>';
  } else {
    badge.className = 'badge warn';
    badge.innerHTML = '<span class="status-dot"></span><span>Đang nối lại WS</span>';
  }
}

export function syncWsConfig() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const gridFpsEl = document.getElementById('grid-fps');
  const gridFps = parseFloat(gridFpsEl ? gridFpsEl.value : '25') || 0;
  const paused = document.hidden || (store.currentTab !== 'live' && !store.focusUid);
  ws.send(JSON.stringify({
    grid_fps: gridFps,
    focus_uid: store.focusUid,
    paused,
  }));
}

export function connectStreamWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/streams`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    setWsBadge(true);
    syncWsConfig();
  };

  ws.onmessage = (ev) => {
    if (!(ev.data instanceof ArrayBuffer)) return;
    const u8 = new Uint8Array(ev.data);
    if (u8.length < 4) return;
    const ulen = u8[0];
    if (u8.length <= 1 + ulen) return;

    let uid = '';
    for (let i = 1; i <= ulen; i++) uid += String.fromCharCode(u8[i]);

    const jpegSlice = u8.subarray(1 + ulen);
    const blob = new Blob([jpegSlice], { type: 'image/jpeg' });
    const url = URL.createObjectURL(blob);

    const gridImg = document.getElementById(`img-${uid}`);
    if (gridImg) {
      gridImg.dataset.hasLiveFrame = '1';
      gridImg.style.display = 'block';
      gridImg.src = url;
    }

    if (store.focusUid === uid) {
      const focusImg = document.getElementById('focus-img');
      if (focusImg) {
        focusImg.style.display = 'block';
        focusImg.src = url;
      }
    }

    if (store.prevBlobUrls[uid]) URL.revokeObjectURL(store.prevBlobUrls[uid]);
    store.prevBlobUrls[uid] = url;

    const now = performance.now();
    if (!store.fpsCounters[uid]) store.fpsCounters[uid] = { count: 0, lastTs: now, fps: 0 };
    const fc = store.fpsCounters[uid];
    fc.count++;
    if (now - fc.lastTs >= 1000) {
      fc.fps = Math.round((fc.count * 1000) / (now - fc.lastTs));
      fc.count = 0;
      fc.lastTs = now;

      const cardPip = document.getElementById(`fps-pip-${uid}`);
      if (cardPip) cardPip.textContent = `${fc.fps} FPS`;

      if (store.focusUid === uid) {
        const focusBadge = document.getElementById('focus-fps-badge');
        if (focusBadge) focusBadge.textContent = `${fc.fps} FPS Live`;
      }
    }
  };

  ws.onclose = () => {
    setWsBadge(false);
    reconnectTimer = setTimeout(connectStreamWs, 1000);
  };
}

document.addEventListener('visibilitychange', syncWsConfig);
