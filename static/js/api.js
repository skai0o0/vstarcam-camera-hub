/**
 * Shared client-side store and fetch helpers for C22S Camera Hub.
 */

export const store = {
  cameras: [],
  storage: {},
  settings: null,
  currentTab: 'live',
  focusUid: null,
  settingsUid: null,
  prevBlobUrls: Object.create(null),
  fpsCounters: Object.create(null),
  onStatusUpdate: [],
  onTabChange: [],
};

export async function apiJson(url, options = {}) {
  const opts = { ...options };
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
  return { res, data };
}

export function getCameraByUid(uid) {
  return store.cameras.find((c) => c.uid === uid) || null;
}

export function getDisplayFps(cam) {
  const fc = store.fpsCounters[cam.uid];
  return fc && fc.fps ? fc.fps : (cam.fps ?? 0);
}

export function downscaleImageFile(file, maxDim = 1600, quality = 0.85) {
  return new Promise((resolve) => {
    if (!file || !file.type || !file.type.startsWith('image/')) {
      return resolve(file);
    }
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      let w = img.width;
      let h = img.height;
      if (w <= maxDim && h <= maxDim) return resolve(file);
      if (w > h) {
        h = Math.round((h * maxDim) / w);
        w = maxDim;
      } else {
        w = Math.round((w * maxDim) / h);
        h = maxDim;
      }
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      canvas.toBlob((blob) => resolve(blob || file), 'image/jpeg', quality);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(file);
    };
    img.src = url;
  });
}
