/**
 * Mobile /sonic Onboarding & Sonic Wave Provisioner Page.
 */
import { apiJson, downscaleImageFile } from './api.js';
import { showToast } from './ui-feedback.js';

let currentTab = 'link';
let pollTimer = null;
let targetUid = null;

function readInitConfig() {
  const el = document.getElementById('sonic-init');
  if (!el) return {};
  try {
    return JSON.parse(el.textContent || '{}');
  } catch (_) {
    return {};
  }
}

function setStatus(msg, level = 'info') {
  const el = document.getElementById('m-status');
  if (!el) return;
  if (!msg) {
    el.style.display = 'none';
    return;
  }
  el.style.display = 'block';
  el.textContent = msg;
  el.style.color = level === 'ok'
    ? 'var(--success-text)'
    : level === 'error'
      ? 'var(--danger-text)'
      : level === 'warn'
        ? 'var(--warning-text)'
        : 'var(--accent-text)';
}

function setStep(idx, cls, text) {
  const el = document.getElementById(`m-st-${idx}`);
  if (!el) return;
  el.className = `step-item ${cls || ''}`;
  if (text) el.textContent = text;
}

function switchMobileTab(tab) {
  currentTab = tab === 'unbox' ? 'unbox' : 'link';
  document.getElementById('tab-btn-link')?.classList.toggle('active', currentTab === 'link');
  document.getElementById('tab-btn-unbox')?.classList.toggle('active', currentTab === 'unbox');
  const wifiSec = document.getElementById('m-unbox-wifi-section');
  const btnLink = document.getElementById('m-btn-link');
  const btnOnboard = document.getElementById('m-btn-onboard');
  if (wifiSec) wifiSec.style.display = currentTab === 'unbox' ? 'flex' : 'none';
  if (btnLink) btnLink.style.display = currentTab === 'link' ? 'inline-flex' : 'none';
  if (btnOnboard) btnOnboard.style.display = currentTab === 'unbox' ? 'inline-flex' : 'none';
}

function syncModeFromBssid() {
  const bssid = (document.getElementById('m-bssid')?.value || '').trim();
  const modeEl = document.getElementById('m-mode');
  if (modeEl && bssid && modeEl.value === 'ssid') {
    modeEl.value = 'mac';
  }
}

function updateWavUrl() {
  const ssid = (document.getElementById('m-ssid')?.value || '').trim();
  const password = document.getElementById('m-pwd')?.value || '';
  const bssid = (document.getElementById('m-bssid')?.value || '').trim();
  const mode = document.getElementById('m-mode')?.value || 'mac';
  const audio = document.getElementById('m-audio');
  if (!audio || !ssid) return;
  const qs = new URLSearchParams({ ssid, password, bssid, mode }).toString();
  audio.src = `/api/wifi/sonic.wav?${qs}`;
}

function stopPlay() {
  const audio = document.getElementById('m-audio');
  if (audio) {
    audio.pause();
    audio.currentTime = 0;
  }
}

async function decodeQrPhoto(file) {
  if (!file) return;
  setStatus('Đang giải mã ảnh tem QR...', 'info');
  try {
    const processed = await downscaleImageFile(file, 1600, 0.85);
    const fd = new FormData();
    fd.append('file', processed, file.name || 'qr.jpg');
    const { data } = await apiJson('/api/camera/decode-qr', { method: 'POST', body: fd });
    if (!data.ok) {
      setStatus(data.error || 'Không đọc được QR từ ảnh — vui lòng chụp gần hoặc nhập tay UID.', 'error');
      return;
    }
    const uidEl = document.getElementById('m-uid');
    const roomEl = document.getElementById('m-room');
    if (uidEl) uidEl.value = data.formatted_did || data.uid_hex || '';
    if (roomEl && !roomEl.value.trim() && data.suggested_name) {
      roomEl.value = data.suggested_name;
    }
    setStatus(`Đã nhận diện UID: ${data.formatted_did}`, 'ok');
    showToast(`Đã đọc ${data.formatted_did}`, 'ok');
  } catch (err) {
    setStatus(`Lỗi đọc ảnh: ${err.message}`, 'error');
  }
}

function pollMobileOnboardStatus(uid, didStr) {
  targetUid = uid;
  const stepsBox = document.getElementById('m-steps');
  if (stepsBox) stepsBox.style.display = 'flex';
  if (pollTimer) clearInterval(pollTimer);

  const check = async () => {
    try {
      const { data } = await apiJson('/api/status');
      const cam = (data.cameras || []).find((c) => c.uid === targetUid);
      if (!cam) return;
      if (cam.state === 'CONNECTED') {
        setStep(1, 'done', `1. Đã lưu ${didStr} lên Hub`);
        setStep(2, 'done', `2. Đã kết nối qua ${cam.connection_mode || 'P2P'}`);
        setStep(3, 'done', `3. Thành công! Camera Online (${cam.ip}:${cam.port})`);
        setStatus(`Camera ${cam.room_name || didStr} đã Online! Đang mở Dashboard...`, 'ok');
        clearInterval(pollTimer);
        pollTimer = null;
        setTimeout(() => { window.location.href = '/'; }, 1800);
      } else if (cam.auth_failed) {
        setStep(2, 'warn', '2. Sai mật khẩu camera — vui lòng nhập lại mật khẩu đúng');
        setStatus('Mật khẩu camera không khớp!', 'warn');
      } else {
        setStep(2, 'active', `2. Đang dò P2P (${cam.state} · Cloud: ${cam.wan_cloud_status || 'WAIT'})`);
      }
    } catch (_) {
      // Transient poll hiccup
    }
  };

  check();
  pollTimer = setInterval(check, 1500);
}

async function startMobileLinkOnly() {
  const uid = (document.getElementById('m-uid')?.value || '').trim();
  const room = (document.getElementById('m-room')?.value || '').trim();
  const password = (document.getElementById('m-campwd')?.value || '').trim() || '888888';
  if (!uid) {
    setStatus('Vui lòng nhập hoặc quét mã UID camera trước.', 'error');
    document.getElementById('m-uid')?.focus();
    return;
  }
  setStatus('Đang liên kết camera lên Hub...', 'info');
  try {
    const { data } = await apiJson('/api/camera/add_qr', {
      method: 'POST',
      body: { device_id: uid, room_name: room, password },
    });
    if (!data.ok) {
      setStatus(data.error || 'Liên kết thất bại', 'error');
      return;
    }
    setStep(1, 'done', `1. Đã lưu ${data.formatted_did || uid}`);
    setStep(2, 'active', '2. Đang bắt tay LAN / WAN P2P...');
    setStep(3, '', '3. Chờ luồng video Live');
    setStatus(data.message || 'Đã gửi yêu cầu kết nối!', 'ok');
    pollMobileOnboardStatus(data.uid, data.formatted_did || uid);
  } catch (err) {
    setStatus(`Lỗi mạng: ${err.message}`, 'error');
  }
}

async function startOneClickMobile() {
  const uid = (document.getElementById('m-uid')?.value || '').trim();
  const room = (document.getElementById('m-room')?.value || '').trim();
  const camPassword = (document.getElementById('m-campwd')?.value || '').trim() || '888888';
  const ssid = (document.getElementById('m-ssid')?.value || '').trim();
  const wifiPassword = document.getElementById('m-pwd')?.value || '';
  const wifiBssid = (document.getElementById('m-bssid')?.value || '').trim();
  const mode = document.getElementById('m-mode')?.value || 'mac';

  if (!uid) {
    setStatus('Vui lòng nhập hoặc chụp ảnh QR mã UID camera.', 'error');
    document.getElementById('m-uid')?.focus();
    return;
  }
  if (!ssid) {
    setStatus('Chế độ Khui hộp cần tên Wi-Fi 2.4GHz (SSID).', 'warn');
    document.getElementById('m-ssid')?.focus();
    return;
  }

  setStatus('Đang đăng ký & tổng hợp sóng âm Sonic Wave...', 'info');
  try {
    const { data } = await apiJson('/api/camera/onboard', {
      method: 'POST',
      body: {
        uid,
        room_name: room,
        cam_password: camPassword,
        wifi_ssid: ssid,
        wifi_password: wifiPassword,
        wifi_bssid: wifiBssid,
        sonic_mode: mode,
      },
    });
    if (!data.ok) {
      setStatus(data.error || 'Khởi tạo thất bại', 'error');
      return;
    }
    const audio = document.getElementById('m-audio');
    if (audio && data.wav_data_url) {
      audio.src = data.wav_data_url;
      audio.play().catch(() => {});
    }
    if (data.qr_data_url) {
      const img = document.getElementById('m-opt-qr-img');
      const wrap = document.getElementById('m-opt-qr-wrap');
      if (img) img.src = data.qr_data_url;
      if (wrap) wrap.style.display = 'flex';
    }
    setStep(1, 'done', `1. Đã lưu ${data.formatted_did || uid}`);
    setStep(2, 'active', '2. Bật max loa ĐT & đưa sát mic camera để nhận Wi-Fi');
    setStep(3, '', '3. Chờ camera kết nối P2P Cloud...');
    setStatus('Đang phát sóng âm & chờ camera kết nối Wi-Fi (tối đa 180s)...', 'ok');
    pollMobileOnboardStatus(data.uid, data.formatted_did || uid);
  } catch (err) {
    setStatus(`Lỗi mạng: ${err.message}`, 'error');
  }
}

async function toggleOpticalQr() {
  const wrap = document.getElementById('m-opt-qr-wrap');
  if (!wrap) return;
  if (wrap.style.display === 'flex' && document.getElementById('m-opt-qr-img')?.src) {
    wrap.style.display = 'none';
    return;
  }
  const ssid = (document.getElementById('m-ssid')?.value || '').trim();
  if (!ssid) {
    setStatus('Nhập tên Wi-Fi (SSID) để tạo mã QR quang học.', 'warn');
    return;
  }
  try {
    const { data } = await apiJson('/api/wifi/qr', {
      method: 'POST',
      body: {
        ssid,
        password: document.getElementById('m-pwd')?.value || '',
        bssid: (document.getElementById('m-bssid')?.value || '').trim(),
        format: 'eye4',
      },
    });
    if (data.ok && data.data_url) {
      document.getElementById('m-opt-qr-img').src = data.data_url;
      wrap.style.display = 'flex';
    } else {
      setStatus(data.error || 'Không tạo được QR Wi-Fi', 'error');
    }
  } catch (err) {
    setStatus(`Lỗi tạo QR: ${err.message}`, 'error');
  }
}

function init() {
  const cfg = readInitConfig();
  if (cfg.ssid) document.getElementById('m-ssid').value = cfg.ssid;
  if (cfg.password) document.getElementById('m-pwd').value = cfg.password;
  if (cfg.bssid) document.getElementById('m-bssid').value = cfg.bssid;
  if (cfg.uid) document.getElementById('m-uid').value = cfg.uid;
  if (cfg.room) document.getElementById('m-room').value = cfg.room;
  if (cfg.mode) document.getElementById('m-mode').value = cfg.mode;
  if (cfg.wav_url) document.getElementById('m-audio').src = cfg.wav_url;

  if (cfg.ssid && !cfg.uid) {
    switchMobileTab('unbox');
  } else {
    switchMobileTab('link');
  }

  document.getElementById('tab-btn-link')?.addEventListener('click', () => switchMobileTab('link'));
  document.getElementById('tab-btn-unbox')?.addEventListener('click', () => switchMobileTab('unbox'));

  document.getElementById('m-campwd-eye')?.addEventListener('click', () => {
    const el = document.getElementById('m-campwd');
    if (el) el.type = el.type === 'password' ? 'text' : 'password';
  });
  document.getElementById('m-pwd-eye')?.addEventListener('click', () => {
    const el = document.getElementById('m-pwd');
    if (el) el.type = el.type === 'password' ? 'text' : 'password';
  });

  document.getElementById('m-qr-file')?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) decodeQrPhoto(file);
    e.target.value = '';
  });

  ['m-ssid', 'm-pwd', 'm-bssid', 'm-mode'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', () => {
      syncModeFromBssid();
      updateWavUrl();
    });
  });

  document.getElementById('m-btn-play-audio')?.addEventListener('click', () => {
    updateWavUrl();
    const a = document.getElementById('m-audio');
    if (a) a.play().catch(() => showToast('Bấm nút Play trực tiếp trên thanh âm thanh', 'warn'));
  });
  document.getElementById('m-btn-stop-audio')?.addEventListener('click', stopPlay);
  document.getElementById('m-btn-opt-qr')?.addEventListener('click', toggleOpticalQr);
  document.getElementById('m-btn-link')?.addEventListener('click', startMobileLinkOnly);
  document.getElementById('m-btn-onboard')?.addEventListener('click', startOneClickMobile);
}

init();
