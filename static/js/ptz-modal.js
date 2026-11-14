/**
 * Focus & PTZ Workbench Modal:
 * - Full-stage 16:9 video canvas with Fit (Contain/Cover), Snapshot download, Fullscreen & stage HUD
 * - 3 compact right-sidebar sub-tabs (PTZ & Presets | Image, Audio & AI | System, Network & CGI)
 * - Hold-to-pan D-pad + keyboard Arrow navigation + optimistic CGI state sync shared with Settings Modal
 */
import { apiJson, getCameraByUid, getDisplayFps, store } from './api.js';
import {
  changeCameraPassword,
  pushWifiToCamera,
  rebootCamera,
  reconnectCamera,
  triggerMotionManual,
} from './cam-actions.js';
import { escHtml, svgIcon } from './icons.js';
import { syncWsConfig } from './ws-stream.js';
import { liveAudioUid, startLiveAudio, stopLiveAudio } from './live-audio.js';
import { focusLiveCanvas } from './live-video.js';
import { confirmDialog, registerModalEsc, showToast } from './ui-feedback.js';

export const PTZ_STATE_PATCH = {
  ir_off: () => ({ ircut: '0' }),
  ir_auto: () => ({ ircut: '1' }),
  ir_on: () => ({ ircut: '2' }),
  flip_normal: () => ({ flip: '0' }),
  flip_v: () => ({ flip: '1' }),
  flip_h: () => ({ flip: '2' }),
  flip_vh: () => ({ flip: '3' }),
  osd_on: () => ({ osdenable: '1' }),
  osd_off: () => ({ osdenable: '0' }),
  hz_50: () => ({ mode: '0' }),
  hz_60: () => ({ mode: '1' }),
  enc_framerate: (e) => {
    const fps = Number(e?.value || 25);
    return { enc_framerate: String(fps), enc_keyframe: String(Math.min(150, Math.max(fps, fps * 2))) };
  },
  enc_keyframe: (e) => ({ enc_keyframe: String(e.value) }),
  enc_ratemode: (e) => ({ enc_ratemode: String(e.value) }),
  brightness: (e) => ({ vbright: String(e.value) }),
  contrast: (e) => ({ vcontrast: String(e.value) }),
  saturation: (e) => ({ saturation: String(e.value) }),
  hue: (e) => ({ vhue: String(e.value) }),
  mic_volume: (e) => ({ involume: String(e.value) }),
  speaker_volume: (e) => ({ outvolume: String(e.value) }),
  ptz_speed: (e) => ({
    speed: String(e.value),
    ptz_patrol_rate: String(e.value),
    ptz_patrol_up_rate: String(e.value),
    ptz_patrol_down_rate: String(e.value),
    ptz_patrol_left_rate: String(e.value),
    ptz_patrol_right_rate: String(e.value),
  }),
  ptz_center_onstart: (e) => ({ ptz_center_onstart: String(e.value) }),
  led_mode: (e) => ({ led_mode: String(e.value) }),
  track_on: () => ({ track_status: '1', enable: '1' }),
  track_off: () => ({ track_status: '0', enable: '0' }),
  humanframe_on: (e) => (
    e && e.sensitive !== undefined
      ? { bHumanoidFrame: '1', sensitive: String(e.sensitive) }
      : { bHumanoidFrame: '1' }
  ),
  humanframe_off: () => ({ bHumanoidFrame: '0' }),
  human_sensitive: (e) => ({ sensitive: String(e.value) }),
  motion_sensitivity: (e) => ({ motion_sensitivity: String(e.value), motion_armed: '1' }),
};

const camUiStateByUid = Object.create(null);
let camParamsSections = null;
let toastHideTimer = null;
let ptzHeldDir = null;
let keyHeldDir = null;
let wasFocusOffline = false;
let lastBannerKey = '';
let callbacks = {
  onRefreshStatus: null,
  onOpenSettings: null,
  onOpenQr: null,
};

function getFocusOfflineReason(cam) {
  if (!cam) return 'Camera không tồn tại hoặc đã bị xoá.';
  if (cam.auth_failed) {
    return 'Camera đang sai mật khẩu P2P — hãy bấm "Nhập mật khẩu" ở thanh cảnh báo.';
  }
  if (cam.state !== 'CONNECTED') {
    const mode = cam.connection_mode || 'P2P';
    return `Camera đang ${cam.state || 'OFFLINE'} (${mode}) — chờ kết nối hoàn tất rồi điều khiển.`;
  }
  return null;
}

function resetFocusScroll() {
  const modal = document.getElementById('focus-modal');
  if (!modal) return;
  modal.querySelectorAll('.focus-body, .ptz-sidebar, .ptz-tab-pane').forEach((el) => {
    el.scrollTop = 0;
  });
}

function updateFocusConnBanner(cam) {
  const sidebar = document.querySelector('#focus-modal .ptz-sidebar');
  const banner = document.getElementById('ptz-conn-banner');
  const textEl = document.getElementById('ptz-conn-text');
  const actEl = document.getElementById('ptz-conn-actions');
  if (!banner || !textEl || !actEl) return;

  const offlineReason = getFocusOfflineReason(cam);
  if (sidebar) sidebar.classList.toggle('is-offline', Boolean(offlineReason));

  if (!offlineReason) {
    banner.style.display = 'none';
    banner.classList.remove('is-auth');
    lastBannerKey = '';
    if (wasFocusOffline) {
      wasFocusOffline = false;
      if (!camParamsSections && store.focusUid) loadCamParams();
    }
    return;
  }

  wasFocusOffline = true;
  const key = cam ? `${cam.uid}|${cam.state}|${cam.auth_failed ? 1 : 0}|${cam.connection_mode || ''}` : 'missing';
  banner.style.display = 'flex';
  banner.classList.toggle('is-auth', Boolean(cam?.auth_failed));
  if (key === lastBannerKey) return;
  lastBannerKey = key;

  if (cam?.auth_failed) {
    textEl.textContent = 'Sai mật khẩu P2P — điều khiển tạm khoá.';
    actEl.innerHTML = `<button type="button" class="btn sm danger" data-banner-act="pwd">Nhập mật khẩu</button><button type="button" class="btn sm" data-banner-act="reconnect">Thử lại</button>`;
  } else if (cam) {
    textEl.textContent = `Đang ${cam.state || 'OFFLINE'} qua ${cam.connection_mode || 'WAN_P2P'} — PTZ chờ online.`;
    actEl.innerHTML = `<button type="button" class="btn sm primary" data-banner-act="reconnect">Tối ưu P2P</button>`;
  } else {
    textEl.textContent = 'Camera không khả dụng.';
    actEl.innerHTML = '';
  }
}

export function getCamUiState(uid = store.focusUid) {
  if (!uid) return {};
  if (!camUiStateByUid[uid]) camUiStateByUid[uid] = {};
  return camUiStateByUid[uid];
}

export function showPtzToast(msg, ok = true) {
  const el = document.getElementById('ptz-status');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  el.style.color = ok ? 'var(--success-text)' : 'var(--danger-text)';
  el.style.borderColor = ok ? 'var(--success-border)' : 'var(--danger-border)';
  if (toastHideTimer) clearTimeout(toastHideTimer);
  toastHideTimer = setTimeout(() => {
    if (el) el.style.display = 'none';
  }, ok ? 3200 : 5000);
}

function renderPresetGrid(activePreset = null) {
  const grid = document.getElementById('preset-grid');
  if (!grid) return;
  grid.innerHTML = Array.from({ length: 16 }, (_, idx) => {
    const n = idx + 1;
    return `<button type="button" class="btn preset-pill ${activePreset === n ? 'primary' : ''}" data-preset-go="${n}" title="Gọi góc nhớ ${n}">Góc ${n}</button>`;
  }).join('');
}

function setBtnActive(id, active) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('active', Boolean(active));
}

function setSliderPair(slId, valId, rawVal) {
  if (rawVal === undefined || rawVal === '' || rawVal === null) return;
  const num = Number(rawVal);
  if (Number.isNaN(num)) return;
  const sl = document.getElementById(slId);
  const vl = document.getElementById(valId);
  if (sl) sl.value = String(num);
  if (vl) vl.textContent = String(num);
}

export function syncCamControlsFromState() {
  const s = getCamUiState();
  ['0', '1', '2'].forEach((v) => setBtnActive(`btn-ir-${v}`, s.ircut === v));
  ['0', '1', '2', '3'].forEach((v) => setBtnActive(`btn-flip-${v}`, s.flip === v));
  setBtnActive('btn-osd-1', s.osdenable === '1');
  setBtnActive('btn-osd-0', s.osdenable === '0');
  setBtnActive('btn-hz-0', s.mode === '0');
  setBtnActive('btn-hz-1', s.mode === '1');
  ['15', '20', '25', '30'].forEach((v) => setBtnActive(`btn-camfps-${v}`, s.enc_framerate === v));
  setBtnActive('btn-led-1', s.led_mode === '1' || s.led_mode === '2');
  setBtnActive('btn-led-0', s.led_mode === '0');

  const trk = s.track_status ?? s.enable;
  setBtnActive('btn-track-1', trk === '1');
  setBtnActive('btn-track-0', trk === '0');

  const hf = s.bHumanoidFrame;
  setBtnActive('btn-hf-1', hf === '1');
  setBtnActive('btn-hf-0', hf === '0');
  const hsensRow = document.getElementById('row-human-sens');
  if (hsensRow) {
    hsensRow.style.display = (hf === '1' || s.sensitive !== undefined) ? 'flex' : 'none';
  }
  ['0', '1', '2', '3'].forEach((v) => setBtnActive(`btn-hsens-${v}`, s.sensitive === v));

  setSliderPair('sl-ptzspeed', 'val-ptzspeed', s.ptz_patrol_rate ?? s.speed);
  setSliderPair('sl-brightness', 'val-brightness', s.vbright);
  setSliderPair('sl-contrast', 'val-contrast', s.vcontrast);
  setSliderPair('sl-saturation', 'val-saturation', s.saturation);
  setSliderPair('sl-mic', 'val-mic', s.involume);
  setSliderPair('sl-speaker', 'val-speaker', s.outvolume);
  setSliderPair('sl-motion', 'val-motion', s.motion_sensitivity);
}

export async function sendPtz(action, extra) {
  if (!store.focusUid) return null;
  const uid = store.focusUid;
  let actualAction = action;
  if (action === 'toggle-flip-180') {
    actualAction = getCamUiState(uid).flip === '3' ? 'flip_normal' : 'flip_vh';
  }

  const isStopHold = actualAction.endsWith('_stop');
  const cam = getCameraByUid(uid);
  const offlineReason = getFocusOfflineReason(cam);
  if (offlineReason) {
    if (!isStopHold) showPtzToast(offlineReason, false);
    return { ok: false, offline: true, error: offlineReason };
  }

  const state = getCamUiState(uid);
  if (PTZ_STATE_PATCH[actualAction]) {
    Object.assign(state, PTZ_STATE_PATCH[actualAction](extra || {}));
    syncCamControlsFromState();
  }

  const isStop = actualAction === 'stop' || isStopHold;
  if (!isStop) showPtzToast(`Đang gửi lệnh ${actualAction}...`, true);

  try {
    const { res, data } = await apiJson(`/api/camera/${uid}/ptz`, {
      method: 'POST',
      body: { action: actualAction, ...(extra || {}) },
    });
    if (!res.ok || !data.ok) {
      showPtzToast(data.error || `Lệnh ${actualAction} thất bại`, false);
      return data;
    }
    if (data.values) {
      for (const [k, v] of Object.entries(data.values)) {
        if (!['result', 'cmd', 'command', 'uid'].includes(k)) state[k] = String(v);
      }
      syncCamControlsFromState();
    }
    if (isStop) {
      showPtzToast(`Đã dừng (${data.connection_mode || 'P2P'})`, true);
    } else if (data.executed) {
      showPtzToast(`Đã áp dụng ${actualAction} (${data.connection_mode || 'P2P'})`, true);
    } else {
      showPtzToast(`Đã thực hiện ${actualAction} (${data.connection_mode || 'P2P'})`, true);
    }
    return data;
  } catch (err) {
    showPtzToast(`Lỗi mạng: ${err.message}`, false);
    return null;
  }
}

function renderCapsBadges(st) {
  const capsEl = document.getElementById('cam-caps');
  if (!capsEl || !st) return;
  const caps = [
    ['Xoay PTZ', Number(st.p2pstatus) !== 0 || st.haveptz],
    ['Micro', st.havemic],
    ['Loa', st.havespeaker],
    ['Thẻ nhớ SD', st.sdstatus && st.sdstatus !== '0'],
    ['Đèn LED', st.haveled],
    ['Phát hiện người', st.support_humanoid],
    ['H.265', st.support_h265],
    ['RTSP', st.support_rtsp],
  ];
  capsEl.innerHTML = caps.map(([lbl, on]) => {
    const yes = on === '1' || on === 1 || on === true;
    return `<span class="badge ${yes ? 'ok' : ''}">${escHtml(lbl)}</span>`;
  }).join('');
}

function renderCamParamsTable() {
  const container = document.getElementById('cam-params');
  if (!container) return;
  if (!camParamsSections) {
    container.innerHTML = '<div style="padding:10px; color:var(--text-muted); font-size:12px;">Bấm "Đọc từ camera" để tải thông số live.</div>';
    return;
  }
  const filter = (document.getElementById('cam-params-filter')?.value || '').trim().toLowerCase();
  const rows = [];
  for (const sec of camParamsSections) {
    const vals = sec.values;
    if (!vals) {
      if (!filter) {
        rows.push(`<tr class="sec"><td colspan="2">${escHtml(sec.title)} (${escHtml(sec.cgi)}) — Chưa kịp trả lời</td></tr>`);
      }
      continue;
    }
    const keys = Object.keys(vals).sort().filter((k) => {
      if (!filter) return true;
      return `${sec.title} ${sec.cgi} ${k} ${vals[k]}`.toLowerCase().includes(filter);
    });
    if (!keys.length) continue;
    rows.push(`<tr class="sec"><td colspan="2">${escHtml(sec.title)} · <code>${escHtml(sec.cgi)}</code></td></tr>`);
    for (const k of keys) {
      rows.push(`<tr><td>${escHtml(k)}</td><td class="mono">${escHtml(vals[k] === '' ? '""' : vals[k])}</td></tr>`);
    }
  }
  container.innerHTML = rows.length
    ? `<table>${rows.join('')}</table>`
    : '<div style="padding:10px; color:var(--text-muted); font-size:12px;">Không có trường nào khớp từ khóa lọc.</div>';
}

export async function loadCamParams() {
  if (!store.focusUid) return;
  const uid = store.focusUid;
  const offlineReason = getFocusOfflineReason(getCameraByUid(uid));
  if (offlineReason) {
    showPtzToast(offlineReason, false);
    return;
  }
  const btn = document.getElementById('focus-load-params-btn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `${svgIcon('refresh', 'icon-sm icon-spin')}<span>Đang đọc...</span>`;
  }
  try {
    const { data } = await apiJson(`/api/camera/${uid}/params?timeout=15`);
    if (uid !== store.focusUid) return;
    if (!data.ok) {
      showPtzToast(data.error || 'Camera chưa phản hồi params', false);
      return;
    }
    camParamsSections = data.sections || [];
    const state = getCamUiState(uid);
    for (const sec of camParamsSections) {
      if (!sec.values) continue;
      for (const [k, v] of Object.entries(sec.values)) {
        if (!['result', 'cmd', 'command', 'uid'].includes(k)) state[k] = String(v);
      }
      if (sec.cgi === 'get_status.cgi') renderCapsBadges(sec.values);
    }
    syncCamControlsFromState();
    renderCamParamsTable();
    showPtzToast(`Đã đồng bộ thông số từ camera (${data.connection_mode || 'P2P'})`, true);
  } catch (err) {
    showPtzToast(`Lỗi đọc params: ${err.message}`, false);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `${svgIcon('refresh', 'icon-sm')}<span>Đọc từ camera</span>`;
    }
  }
}

export function updateFocusStageTelemetry() {
  if (!store.focusUid) return;
  const c = getCameraByUid(store.focusUid);
  if (!c) {
    updateFocusConnBanner(null);
    return;
  }
  updateFocusConnBanner(c);

  const liveFps = getDisplayFps(c);
  const fpsBadge = document.getElementById('focus-fps-badge');
  if (fpsBadge) {
    if (c.auth_failed) {
      fpsBadge.className = 'badge err mono';
      fpsBadge.textContent = 'Sai mật khẩu';
    } else if (c.state === 'CONNECTED') {
      fpsBadge.className = 'badge ok mono';
      fpsBadge.textContent = `${liveFps} FPS Live`;
    } else {
      fpsBadge.className = 'badge warn mono';
      fpsBadge.textContent = c.state || 'OFFLINE';
    }
  }

  const ipStr = c.ip && c.ip !== '0.0.0.0' ? `${c.ip}${c.port ? ':' + c.port : ''}` : 'Chờ IP LAN';
  const didStr = c.did || c.formatted_did || c.short_uid || c.uid.substring(0, 8);
  const metaEl = document.getElementById('focus-hud-meta');
  if (metaEl) {
    metaEl.textContent = `${didStr} · ${ipStr} · ${c.connection_mode || 'P2P'}${c.wan_relay_rtt_ms ? ` (${Math.round(c.wan_relay_rtt_ms)}ms)` : ''}`;
  }
  const infoEl = document.getElementById('focus-info');
  if (infoEl) {
    infoEl.textContent = `${String(c.codec || 'H264').toUpperCase()} · Cam ${c.fps ?? 0} FPS (Live ${liveFps}) · ${c.kbps ?? 0} KB/s · RAM ${c.ram_buffer_kb ?? 0} KB · RTSP :${c.rtsp_port}`;
  }
  const recBtn = document.getElementById('focus-rec20-btn');
  if (recBtn) {
    recBtn.className = `btn sm ${c.is_recording ? 'danger' : ''}`;
    recBtn.innerHTML = c.is_recording
      ? `${svgIcon('stop', 'icon-sm')}<span>Đang ghi (+${c.post_roll_remaining}s)</span>`
      : `${svgIcon('zap', 'icon-sm')}<span>Ghi nhanh</span>`;
  }
}

function switchPtzSubtab(tabId) {
  document.querySelectorAll('.ptz-subtab-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.ptzTab === tabId);
  });
  ['ptz', 'image', 'sys'].forEach((id) => {
    const pane = document.getElementById(`ptz-pane-${id}`);
    if (pane) pane.classList.toggle('active', id === tabId);
  });
  resetFocusScroll();
}

const AUDIO_BTN_LABEL = {
  connecting: 'Đang mở tiếng...',
  playing: 'Tắt tiếng',
  stopped: 'Nghe',
  error: 'Nghe',
};

function renderAudioBtn(state) {
  const btn = document.getElementById('focus-audio-btn');
  if (!btn) return;
  btn.innerHTML = `${svgIcon('volume', 'icon-sm')}<span>${escHtml(AUDIO_BTN_LABEL[state] || 'Nghe')}</span>`;
  btn.classList.toggle('active', state === 'playing' || state === 'connecting');
}

export function openFocusModal(uid) {
  const c = getCameraByUid(uid);
  if (!c) return;
  if (liveAudioUid() && liveAudioUid() !== uid) stopLiveAudio();
  renderAudioBtn(liveAudioUid() === uid ? 'playing' : 'stopped');
  store.focusUid = uid;
  camParamsSections = null;
  wasFocusOffline = Boolean(getFocusOfflineReason(c));
  lastBannerKey = '';

  const ipStr = c.ip && c.ip !== '0.0.0.0' ? c.ip : (c.did || c.short_uid || 'WAN P2P');
  document.getElementById('focus-title').textContent = `${c.room_name || 'Camera'} (${ipStr})`;
  const fpsBadge = document.getElementById('focus-fps-badge');
  if (fpsBadge) fpsBadge.textContent = `${getDisplayFps(c)} FPS Live`;

  const focusImg = document.getElementById('focus-img');
  const gridImg = document.getElementById(`img-${uid}`);
  if (focusImg) {
    if (!focusImg.dataset.errWired) {
      focusImg.dataset.errWired = '1';
      focusImg.addEventListener('error', () => {
        if (!store.focusUid || !focusImg.src.startsWith('blob:')) return;
        focusImg.src = `/api/camera/${encodeURIComponent(store.focusUid)}/snapshot.jpg?t=${Date.now()}`;
      });
    }
    if (store.prevBlobUrls[uid]) {
      focusImg.src = store.prevBlobUrls[uid];
    } else if (gridImg && gridImg.src) {
      focusImg.src = gridImg.src;
    } else {
      focusImg.src = `/api/camera/${encodeURIComponent(uid)}/snapshot.jpg?t=${Date.now()}`;
    }
    focusImg.style.display = 'block';
  }

  document.getElementById('ptz-status').style.display = 'none';
  renderPresetGrid();
  syncCamControlsFromState();
  renderCamParamsTable();
  updateFocusStageTelemetry();
  switchPtzSubtab('ptz');

  document.getElementById('focus-modal').classList.add('open');
  resetFocusScroll();
  syncWsConfig();

  if (c.state === 'CONNECTED' && !c.auth_failed) {
    loadCamParams();
  }
}

export function closeFocusModal() {
  if (ptzHeldDir) {
    sendPtz(`${ptzHeldDir}_stop`);
    ptzHeldDir = null;
  }
  if (keyHeldDir) {
    sendPtz(`${keyHeldDir}_stop`);
    keyHeldDir = null;
  }
  store.focusUid = null;
  stopLiveAudio();
  const focusImg = document.getElementById('focus-img');
  if (focusImg) focusImg.src = '';
  document.getElementById('focus-modal')?.classList.remove('open');
  syncWsConfig();
}

function captureSnapshot() {
  const img = document.getElementById('focus-img');
  const live = focusLiveCanvas();
  if (!live && (!img || !img.naturalWidth)) {
    showToast('Chưa có khung hình nào để chụp ảnh', 'warn');
    return;
  }
  const canvas = document.createElement('canvas');
  canvas.width = live ? live.width : img.naturalWidth;
  canvas.height = live ? live.height : img.naturalHeight;
  canvas.getContext('2d').drawImage(live || img, 0, 0);
  const c = getCameraByUid(store.focusUid);
  const safeName = (c?.room_name || store.focusUid || 'camera').replace(/[^a-zA-Z0-9_-]/g, '_');
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = document.createElement('a');
  a.href = canvas.toDataURL('image/jpeg', 0.92);
  a.download = `${safeName}_${ts}.jpg`;
  a.click();
  showToast('Đã tải ảnh chụp màn hình camera', 'ok');
}

export function initFocusModal(opts = {}) {
  callbacks = { ...callbacks, ...opts };
  const modal = document.getElementById('focus-modal');
  if (!modal) return;

  modal.addEventListener('click', (ev) => {
    if (ev.target === modal) closeFocusModal();
  });
  document.getElementById('focus-close-btn')?.addEventListener('click', closeFocusModal);
  document.getElementById('focus-rec20-btn')?.addEventListener('click', () => {
    if (store.focusUid) triggerMotionManual(store.focusUid, 'trigger', callbacks.onRefreshStatus);
  });
  document.getElementById('focus-snap-btn')?.addEventListener('click', captureSnapshot);
  document.getElementById('focus-audio-btn')?.addEventListener('click', () => {
    if (!store.focusUid) return;
    if (liveAudioUid() === store.focusUid) {
      stopLiveAudio();
    } else {
      startLiveAudio(store.focusUid, (uid, state) => {
        if (uid !== store.focusUid) return;
        renderAudioBtn(state);
        if (state === 'error') showToast('Không mở được kênh âm thanh', 'error');
      });
    }
  });
  document.getElementById('focus-fit-btn')?.addEventListener('click', () => {
    const stage = document.getElementById('focus-stage');
    const btn = document.getElementById('focus-fit-btn');
    if (!stage) return;
    const isCover = stage.classList.toggle('fit-cover');
    if (btn) btn.classList.toggle('active', isCover);
  });
  document.getElementById('focus-fs-btn')?.addEventListener('click', () => {
    const box = modal.querySelector('.focus-modal-box');
    if (!box) return;
    if (!document.fullscreenElement) {
      box.requestFullscreen?.().catch(() => showToast('Trình duyệt không hỗ trợ Fullscreen', 'warn'));
    } else {
      document.exitFullscreen?.();
    }
  });
  document.getElementById('focus-settings-btn')?.addEventListener('click', () => {
    const uid = store.focusUid;
    if (!uid || !callbacks.onOpenSettings) return;
    closeFocusModal();
    callbacks.onOpenSettings(uid);
  });

  // 3 Sub-tab switches
  document.querySelectorAll('.ptz-subtab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchPtzSubtab(btn.dataset.ptzTab));
  });

  // Inline offline/auth banner quick actions
  document.getElementById('ptz-conn-actions')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-banner-act]');
    if (!btn || !store.focusUid) return;
    const c = getCameraByUid(store.focusUid);
    if (!c) return;
    if (btn.dataset.bannerAct === 'pwd') {
      changeCameraPassword(c.uid, c.room_name || c.uid, callbacks.onRefreshStatus, showPtzToast);
    } else if (btn.dataset.bannerAct === 'reconnect') {
      reconnectCamera(c.uid, callbacks.onRefreshStatus, showPtzToast);
    }
  });

  // D-Pad pointer hold/tap navigation
  document.querySelectorAll('.dpad button[data-ptz]').forEach((btn) => {
    const dir = btn.dataset.ptz;
    let pointerDownAt = 0;
    let holdTimer = null;
    let sentHold = false;

    const release = () => {
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      if (ptzHeldDir === dir) {
        ptzHeldDir = null;
        if (sentHold || performance.now() - pointerDownAt >= 180) {
          sendPtz(`${dir}_stop`);
        }
      }
      sentHold = false;
    };

    btn.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (getFocusOfflineReason(getCameraByUid(store.focusUid))) {
        sendPtz(dir, { hold: false });
        return;
      }
      btn.setPointerCapture?.(e.pointerId);
      pointerDownAt = performance.now();
      ptzHeldDir = dir;
      sentHold = false;
      sendPtz(dir, { hold: false });
      holdTimer = setTimeout(() => {
        if (ptzHeldDir === dir) {
          sentHold = true;
          sendPtz(dir, { hold: true });
        }
      }, 220);
    });
    btn.addEventListener('pointerup', release);
    btn.addEventListener('pointercancel', release);
    btn.addEventListener('lostpointercapture', release);
  });

  document.getElementById('ptz-center-btn')?.addEventListener('click', () => sendPtz('center'));
  document.getElementById('ptz-hscan-btn')?.addEventListener('click', () => sendPtz('hscan'));
  document.getElementById('ptz-vscan-btn')?.addEventListener('click', () => sendPtz('vscan'));
  document.getElementById('ptz-stop-btn')?.addEventListener('click', () => sendPtz('stop'));

  // Presets 1..16
  const runPreset = async (n, save = false) => {
    const offlineReason = getFocusOfflineReason(getCameraByUid(store.focusUid));
    if (offlineReason) {
      showPtzToast(offlineReason, false);
      return;
    }
    if (save) {
      const ok = await confirmDialog({
        title: `Lưu Góc nhớ #${n}`,
        message: `Ghi đè vị trí góc xoay hiện tại vào Góc nhớ ${n} trên bộ nhớ camera?`,
        confirmText: `Lưu vào Góc ${n}`,
      });
      if (!ok) return;
    }
    const sel = document.getElementById('ptz-preset');
    if (sel) sel.value = String(n);
    renderPresetGrid(n);
    sendPtz(`preset${n}_${save ? 'set' : 'go'}`);
  };

  document.getElementById('preset-grid')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-preset-go]');
    if (btn) runPreset(Number(btn.dataset.presetGo), false);
  });
  document.getElementById('ptz-preset-go-btn')?.addEventListener('click', () => {
    runPreset(Number(document.getElementById('ptz-preset')?.value || 1), false);
  });
  document.getElementById('ptz-preset-save-btn')?.addEventListener('click', () => {
    runPreset(Number(document.getElementById('ptz-preset')?.value || 1), true);
  });

  // Delegated quick camera actions
  modal.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-cam-act]');
    if (!btn) return;
    const act = btn.dataset.camAct;
    const val = btn.dataset.camVal;
    sendPtz(act, val !== undefined ? { value: Number(val) } : undefined);
  });

  // Sliders
  const sliderConfigs = [
    ['sl-ptzspeed', 'val-ptzspeed', 'ptz_speed'],
    ['sl-brightness', 'val-brightness', 'brightness'],
    ['sl-contrast', 'val-contrast', 'contrast'],
    ['sl-saturation', 'val-saturation', 'saturation'],
    ['sl-mic', 'val-mic', 'mic_volume'],
    ['sl-speaker', 'val-speaker', 'speaker_volume'],
    ['sl-motion', 'val-motion', 'motion_sensitivity'],
  ];
  for (const [slId, valId, act] of sliderConfigs) {
    const sl = document.getElementById(slId);
    const vl = document.getElementById(valId);
    if (!sl) continue;
    sl.addEventListener('input', () => {
      if (vl) vl.textContent = sl.value;
    });
    sl.addEventListener('change', () => {
      sendPtz(act, { value: Number(sl.value) });
    });
  }

  // System & Remote Setup
  document.getElementById('focus-opt-p2p-btn')?.addEventListener('click', () => {
    if (store.focusUid) reconnectCamera(store.focusUid, callbacks.onRefreshStatus, showPtzToast);
  });
  document.getElementById('focus-reboot-btn')?.addEventListener('click', () => {
    const c = getCameraByUid(store.focusUid);
    if (c) rebootCamera(c.uid, c.room_name || c.uid, callbacks.onRefreshStatus, showPtzToast);
  });
  document.getElementById('focus-pwd-btn')?.addEventListener('click', () => {
    const c = getCameraByUid(store.focusUid);
    if (c) changeCameraPassword(c.uid, c.room_name || c.uid, callbacks.onRefreshStatus, showPtzToast);
  });
  document.getElementById('focus-wifi-btn')?.addEventListener('click', () => {
    const c = getCameraByUid(store.focusUid);
    if (c) pushWifiToCamera(c.uid, c.room_name || c.uid);
  });

  document.getElementById('focus-load-params-btn')?.addEventListener('click', loadCamParams);
  document.getElementById('cam-params-filter')?.addEventListener('input', renderCamParamsTable);

  // Raw CGI Console
  const sendRawCgi = async () => {
    if (!store.focusUid) return;
    const offlineReason = getFocusOfflineReason(getCameraByUid(store.focusUid));
    if (offlineReason) {
      showPtzToast(offlineReason, false);
      return;
    }
    const inp = document.getElementById('raw-cgi');
    const out = document.getElementById('raw-cgi-out');
    const cgi = (inp?.value || '').trim();
    if (!cgi) {
      showToast('Nhập lệnh CGI trước khi gửi', 'warn');
      return;
    }
    if (out) {
      out.style.display = 'block';
      out.textContent = `Đang gửi ${cgi}...`;
    }
    try {
      const { data } = await apiJson(`/api/camera/${store.focusUid}/cgi`, {
        method: 'POST',
        body: { cgi },
      });
      if (out) out.textContent = JSON.stringify(data.ok ? data.values : data, null, 2);
      showPtzToast(data.ok ? `CGI OK (${data.connection_mode || 'P2P'})` : (data.error || 'CGI lỗi'), !!data.ok);
    } catch (err) {
      if (out) out.textContent = `Lỗi: ${err.message}`;
    }
  };
  document.getElementById('raw-cgi-send-btn')?.addEventListener('click', sendRawCgi);
  document.getElementById('raw-cgi')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendRawCgi();
  });

  // Keyboard Arrow pan/tilt while Focus Modal is open
  const ARROW_DIR = {
    ArrowUp: 'up',
    ArrowDown: 'down',
    ArrowLeft: 'left',
    ArrowRight: 'right',
  };
  document.addEventListener('keydown', (e) => {
    if (!modal.classList.contains('open') || !store.focusUid) return;
    const tag = (document.activeElement?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    const dir = ARROW_DIR[e.key];
    if (!dir) return;
    e.preventDefault();
    if (e.repeat || keyHeldDir === dir) return;
    if (getFocusOfflineReason(getCameraByUid(store.focusUid))) {
      sendPtz(dir, { hold: true });
      return;
    }
    keyHeldDir = dir;
    sendPtz(dir, { hold: true });
  });
  document.addEventListener('keyup', (e) => {
    const dir = ARROW_DIR[e.key];
    if (!dir || keyHeldDir !== dir) return;
    keyHeldDir = null;
    sendPtz(`${dir}_stop`);
  });

  registerModalEsc(() => modal.classList.contains('open'), closeFocusModal);
}
