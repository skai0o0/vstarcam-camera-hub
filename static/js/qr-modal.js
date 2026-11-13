/**
 * Onboarding & Wi-Fi Provisioning Wizard Modal:
 * - Tab 1 ("link"): Link an existing camera by UID / QR sticker + password (WAN P2P & LAN)
 * - Tab 2 ("unbox"): Provision Wi-Fi to a brand-new camera via Sonic Wave, Optical Eye4 QR, or Mobile /sonic QR
 * - Drag & drop / Ctrl+V / camera file QR sticker decoder + live 180s P2P Cloud progress tracker
 */
import { apiJson, downscaleImageFile, store } from './api.js';
import { escHtml, svgIcon } from './icons.js';
import { registerModalEsc, showToast } from './ui-feedback.js';

let currentWizTab = 'link';
let onboardPollTimer = null;
let activeOnboardUid = null;
let activeOnboardDid = '';
let activeOnboardNeedWifi = false;
let hostWifiLoaded = false;
let hostNetworks = [];
let onRefreshCb = null;

function setWizardTab(tab) {
  currentWizTab = tab === 'unbox' ? 'unbox' : 'link';
  document.querySelectorAll('.wizard-tab-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.wizTab === currentWizTab);
  });
  const unboxSec = document.getElementById('wiz-unbox-wifi-sec');
  const linkActions = document.getElementById('wiz-link-actions');
  const unboxActions = document.getElementById('wiz-unbox-actions');
  const titleSub = document.getElementById('qr-wiz-subtitle');
  if (unboxSec) unboxSec.style.display = currentWizTab === 'unbox' ? 'flex' : 'none';
  if (linkActions) linkActions.style.display = currentWizTab === 'link' ? 'flex' : 'none';
  if (unboxActions) unboxActions.style.display = currentWizTab === 'unbox' ? 'flex' : 'none';
  if (titleSub) {
    titleSub.textContent = currentWizTab === 'link'
      ? 'Camera đã kết nối sẵn Wi-Fi / LAN? Nhập hoặc quét tem QR để gắn vào Hub ngay.'
      : 'Camera mới reset / chưa có Wi-Fi? Nhập Wi-Fi 2.4GHz để phát Sonic Wave & tạo QR quang học.';
  }
  if (currentWizTab === 'unbox' && !hostWifiLoaded) {
    loadHostWifiInfo();
  }
}

function setQrStatus(msg, level = 'info') {
  const el = document.getElementById('qr-status-line');
  if (!el) return;
  if (!msg) {
    el.style.display = 'none';
    el.textContent = '';
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

function syncSonicModeFromBssid() {
  const bssid = (document.getElementById('wifi-bssid-input')?.value || '').trim();
  const sel = document.getElementById('sonic-mode-select');
  if (sel && bssid && sel.value === 'ssid') {
    sel.value = 'mac';
  }
}

function renderWifiChips() {
  const wrap = document.getElementById('wifi-ssid-chips');
  if (!wrap) return;
  if (!hostNetworks.length) {
    wrap.innerHTML = '';
    return;
  }
  wrap.innerHTML = hostNetworks.slice(0, 8).map((n) => {
    const label = `${n.ssid}${n.band ? ` (${n.band})` : ''}`;
    return `<button type="button" class="btn sm" data-pick-ssid="${escHtml(n.ssid)}" title="Tín hiệu ${escHtml(n.signal ?? '')}%">${svgIcon('wifi', 'icon-sm')}<span>${escHtml(label)}</span></button>`;
  }).join('');
}

async function loadHostWifiInfo() {
  try {
    const { data } = await apiJson('/api/wifi/info');
    if (!data.ok) return;
    hostWifiLoaded = true;
    const ssidEl = document.getElementById('wifi-ssid-input');
    const pwdEl = document.getElementById('wifi-pwd-input');
    const bssidEl = document.getElementById('wifi-bssid-input');
    if (ssidEl && !ssidEl.value && data.current_ssid) ssidEl.value = data.current_ssid;
    if (pwdEl && !pwdEl.value && data.saved_password) pwdEl.value = data.saved_password;
    if (bssidEl && !bssidEl.value && data.current_bssid) bssidEl.value = data.current_bssid;
    hostNetworks = Array.isArray(data.networks) ? data.networks : [];
    renderWifiChips();
    syncSonicModeFromBssid();
    if (ssidEl && ssidEl.value.trim()) {
      await refreshMobileSonicLinkPreview();
    }
  } catch (err) {
    console.warn('WiFi info load error:', err);
  }
}

async function refreshMobileSonicLinkPreview() {
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  if (!ssid) return null;
  const password = document.getElementById('wifi-pwd-input')?.value || '';
  const bssid = (document.getElementById('wifi-bssid-input')?.value || '').trim();
  const mode = document.getElementById('sonic-mode-select')?.value || 'mac';
  try {
    const { data } = await apiJson('/api/wifi/sonic', {
      method: 'POST',
      body: { ssid, password, bssid, mode },
    });
    if (!data.ok) return null;
    const mobImg = document.getElementById('mobile-sonic-qr-img');
    const mobUrl = document.getElementById('mobile-sonic-url');
    const mobWrap = document.getElementById('mobile-sonic-handoff');
    if (mobImg && data.mobile_qr_data_url) mobImg.src = data.mobile_qr_data_url;
    if (mobUrl && data.mobile_url) {
      mobUrl.href = data.mobile_url;
      mobUrl.textContent = data.mobile_url;
    }
    if (mobWrap) mobWrap.style.display = 'grid';
    return data;
  } catch (_) {
    return null;
  }
}

async function refreshWifiQrPreview() {
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  if (!ssid) {
    showToast('Vui lòng nhập tên Wi-Fi (SSID) 2.4GHz trước', 'warn');
    return;
  }
  const password = document.getElementById('wifi-pwd-input')?.value || '';
  const bssid = (document.getElementById('wifi-bssid-input')?.value || '').trim();
  try {
    const { data } = await apiJson('/api/wifi/qr', {
      method: 'POST',
      body: { ssid, password, bssid, format: 'eye4' },
    });
    if (!data.ok || !data.data_url) {
      showToast(data.error || 'Không tạo được mã QR Wi-Fi', 'error');
      return;
    }
    const wrap = document.getElementById('wifi-qr-box');
    const img = document.getElementById('wifi-qr-preview');
    if (img) img.src = data.data_url;
    if (wrap) wrap.style.display = 'flex';
    const sonicBox = document.getElementById('sonic-output-panel');
    if (sonicBox) sonicBox.style.display = 'flex';
    showToast('Đã tạo mã QR quang học — giơ trước ống kính camera cách 15–25cm', 'ok');
  } catch (err) {
    showToast(`Lỗi tạo QR Wi-Fi: ${err.message}`, 'error');
  }
}

async function playSonicWaveBrowser() {
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  if (!ssid) {
    showToast('Vui lòng nhập tên Wi-Fi (SSID) 2.4GHz trước', 'warn');
    return;
  }
  setQrStatus('Đang tổng hợp tín hiệu Sonic Wave (2–5kHz)...', 'info');
  const data = await refreshMobileSonicLinkPreview();
  if (!data || !data.wav_data_url) {
    setQrStatus('Không tạo được sóng âm Sonic Wave.', 'error');
    return;
  }
  const panel = document.getElementById('sonic-output-panel');
  const audio = document.getElementById('sonic-audio');
  if (panel) panel.style.display = 'flex';
  if (audio) {
    audio.src = data.wav_data_url;
    audio.currentTime = 0;
    try {
      await audio.play();
      setQrStatus(`Đang phát Sonic Wave (${data.duration_sec}s · mode ${data.mode}). Đặt sát mic camera!`, 'ok');
    } catch (_) {
      setQrStatus('Sóng âm đã sẵn sàng bên dưới — bấm nút Play để phát qua loa.', 'warn');
    }
  }
}

async function playSonicWavePcSpeaker() {
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  if (!ssid) {
    showToast('Vui lòng nhập tên Wi-Fi (SSID) 2.4GHz trước', 'warn');
    return;
  }
  const password = document.getElementById('wifi-pwd-input')?.value || '';
  const bssid = (document.getElementById('wifi-bssid-input')?.value || '').trim();
  const mode = document.getElementById('sonic-mode-select')?.value || 'mac';
  setQrStatus('Đang phát sóng âm trực tiếp ra loa máy chủ...', 'info');
  try {
    const { data } = await apiJson('/api/wifi/play-pc-speaker', {
      method: 'POST',
      body: { ssid, password, bssid, mode },
    });
    if (data.ok) {
      setQrStatus(data.message || 'Đã phát ra loa máy chủ!', 'ok');
      showToast('Đã phát Sonic Wave qua loa Server', 'ok');
    } else {
      setQrStatus(data.error || 'Máy chủ không phát được âm thanh', 'warn');
      showToast(data.error || 'Server không có thiết bị âm thanh — hãy dùng loa trình duyệt hoặc điện thoại', 'warn');
    }
  } catch (err) {
    setQrStatus(`Lỗi gọi loa Server: ${err.message}`, 'error');
  }
}

async function pushWifiDirectCgi() {
  const did = (document.getElementById('qr-did-input')?.value || '').trim();
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  const password = document.getElementById('wifi-pwd-input')?.value || '';
  if (!ssid) {
    showToast('Nhập tên Wi-Fi (SSID) trước khi gửi xuống camera', 'warn');
    return;
  }
  setQrStatus('Đang gửi cấu hình Wi-Fi qua kênh lệnh CGI PPPP...', 'info');
  try {
    const { data } = await apiJson('/api/camera/provision-wifi', {
      method: 'POST',
      body: { ssid, password, uid: did },
    });
    setQrStatus(data.ok ? (data.message || 'Đã đẩy Wi-Fi xuống camera') : (data.error || 'Thất bại'), data.ok ? 'ok' : 'error');
    showToast(data.ok ? 'Đã đẩy Wi-Fi xuống camera' : (data.error || 'Không gửi được'), data.ok ? 'ok' : 'error');
  } catch (err) {
    setQrStatus(`Lỗi kết nối: ${err.message}`, 'error');
  }
}

async function decodeQrFile(file) {
  if (!file) return;
  setQrStatus('Đang giải mã tem QR camera...', 'info');
  try {
    const processed = await downscaleImageFile(file, 1600, 0.85);
    const fd = new FormData();
    fd.append('file', processed, file.name || 'qr.jpg');
    const { data } = await apiJson('/api/camera/decode-qr', { method: 'POST', body: fd });
    if (!data.ok) {
      setQrStatus(data.error || 'Không đọc được mã QR từ ảnh này — hãy chụp rõ tem QR hoặc nhập tay UID.', 'error');
      showToast(data.error || 'Không giải mã được QR', 'warn');
      return;
    }
    const didEl = document.getElementById('qr-did-input');
    const roomEl = document.getElementById('qr-room-input');
    if (didEl) didEl.value = data.formatted_did || data.uid_hex || '';
    if (roomEl && !roomEl.value.trim() && data.suggested_name) {
      roomEl.value = data.suggested_name;
    }
    setQrStatus(`Đã nhận diện camera: ${data.formatted_did} (${data.model || 'VStarcam'}).`, 'ok');
    showToast(`Đã đọc UID ${data.formatted_did}`, 'ok');
  } catch (err) {
    setQrStatus(`Lỗi giải mã ảnh QR: ${err.message}`, 'error');
  }
}

function updateOnboardSteps(stepStates) {
  [1, 2, 3].forEach((n) => {
    const el = document.getElementById(`ob-step-${n}`);
    if (!el) return;
    el.className = `step-item ${stepStates[n]?.cls || ''}`;
    const txt = el.querySelector('.ob-step-text');
    if (txt && stepStates[n]?.text) txt.textContent = stepStates[n].text;
  });
}

function stopOnboardPolling() {
  if (onboardPollTimer) {
    clearInterval(onboardPollTimer);
    onboardPollTimer = null;
  }
}

function startOnboardProgressPolling(uid, didStr, needWifi) {
  stopOnboardPolling();
  activeOnboardUid = uid;
  activeOnboardDid = didStr;
  activeOnboardNeedWifi = needWifi;

  const card = document.getElementById('onboard-progress-card');
  if (card) card.style.display = 'flex';

  updateOnboardSteps({
    1: { cls: 'done', text: `Đã lưu UID ${didStr} lên Hub & mở kênh P2P Cloud` },
    2: {
      cls: 'active',
      text: needWifi
        ? 'Đưa camera lại gần loa để nhận Sonic Wave hoặc quét QR quang học'
        : 'Đang dò tìm địa chỉ LAN & WAN P2P Cloud của camera...',
    },
    3: { cls: '', text: 'Chờ camera phản hồi & bắt đầu stream 25 FPS' },
  });

  const tick = async () => {
    if (onRefreshCb) await onRefreshCb();
    const cam = store.cameras.find((c) => c.uid === activeOnboardUid);
    if (!cam) return;
    const msgEl = document.getElementById('ob-live-msg');
    if (cam.state === 'CONNECTED') {
      updateOnboardSteps({
        1: { cls: 'done', text: `Đã lưu ${activeOnboardDid} vào danh sách Hub` },
        2: { cls: 'done', text: `Đã bắt tay thành công qua ${cam.connection_mode || 'P2P'}` },
        3: { cls: 'done', text: `Online (${cam.ip}:${cam.port}) — Luồng video & điều khiển đã sẵn sàng!` },
      });
      if (msgEl) msgEl.textContent = 'Hoàn tất! Đang đóng trình hướng dẫn sau 2 giây...';
      stopOnboardPolling();
      showToast(`Camera ${cam.room_name || activeOnboardDid} đã Online!`, 'ok');
      setTimeout(() => {
        if (document.getElementById('qr-modal')?.classList.contains('open')) closeQrModal();
      }, 2000);
    } else if (cam.auth_failed) {
      updateOnboardSteps({
        1: { cls: 'done', text: `Đã tìm thấy camera ${activeOnboardDid} tại ${cam.ip}` },
        2: { cls: 'warn', text: 'Mật khẩu camera không khớp! Vui lòng kiểm tra lại mật khẩu ở trên.' },
        3: { cls: '', text: 'Đang chờ mật khẩu đúng để đăng nhập...' },
      });
      if (msgEl) msgEl.textContent = 'Nhập lại mật khẩu và bấm "Gán & Kết nối P2P ngay" để thử lại.';
    } else {
      if (msgEl) {
        msgEl.textContent = `Trạng thái: ${cam.state} · Cloud: ${cam.wan_cloud_status || 'PROBING'} · Mode: ${cam.connection_mode || 'WAN_P2P'}`;
      }
    }
  };

  onboardPollTimer = setInterval(tick, 1400);
}

async function submitLinkCamera() {
  const did = (document.getElementById('qr-did-input')?.value || '').trim();
  const room = (document.getElementById('qr-room-input')?.value || '').trim();
  const password = (document.getElementById('qr-pwd-input')?.value || '').trim();
  const ip = (document.getElementById('qr-ip-input')?.value || '').trim();
  const username = (document.getElementById('qr-user-input')?.value || '').trim();
  if (!did) {
    setQrStatus('Vui lòng nhập mã UID camera (VD: VSTJ082299RLSXK) hoặc tải ảnh tem QR.', 'error');
    document.getElementById('qr-did-input')?.focus();
    return;
  }
  setQrStatus(`Đang liên kết ${did} vào Hub & khởi động dò P2P...`, 'info');
  try {
    const { data } = await apiJson('/api/camera/add_qr', {
      method: 'POST',
      body: { device_id: did, room_name: room, password, username, ip },
    });
    if (!data.ok) {
      setQrStatus(data.error || 'Không thêm được camera', 'error');
      showToast(data.error || 'Không thêm được camera', 'error');
      return;
    }
    setQrStatus(data.message || `Đã liên kết ${data.formatted_did || did}`, 'ok');
    showToast(`Đã gửi yêu cầu kết nối ${data.room_name || did}`, 'ok');
    if (data.connected) {
      if (onRefreshCb) await onRefreshCb();
      closeQrModal();
    } else {
      startOnboardProgressPolling(data.uid, data.formatted_did || did, false);
    }
  } catch (err) {
    setQrStatus(`Lỗi kết nối Server: ${err.message}`, 'error');
  }
}

async function submitUnboxOnboard() {
  const did = (document.getElementById('qr-did-input')?.value || '').trim();
  const room = (document.getElementById('qr-room-input')?.value || '').trim();
  const camPassword = (document.getElementById('qr-pwd-input')?.value || '').trim() || '888888';
  const ssid = (document.getElementById('wifi-ssid-input')?.value || '').trim();
  const wifiPassword = document.getElementById('wifi-pwd-input')?.value || '';
  const wifiBssid = (document.getElementById('wifi-bssid-input')?.value || '').trim();
  const sonicMode = document.getElementById('sonic-mode-select')?.value || 'mac';

  if (!did) {
    setQrStatus('Vui lòng nhập mã UID camera trên thân/đáy camera.', 'error');
    document.getElementById('qr-did-input')?.focus();
    return;
  }
  if (!ssid) {
    setQrStatus('Chế độ Khui hộp cần tên Wi-Fi 2.4GHz (SSID) để tạo sóng âm & mã QR.', 'warn');
    document.getElementById('wifi-ssid-input')?.focus();
    return;
  }

  setQrStatus('Đang đăng ký 1-Click & tạo gói Sonic Wave + QR quang học...', 'info');
  try {
    const { data } = await apiJson('/api/camera/onboard', {
      method: 'POST',
      body: {
        uid: did,
        room_name: room,
        cam_password: camPassword,
        wifi_ssid: ssid,
        wifi_password: wifiPassword,
        wifi_bssid: wifiBssid,
        sonic_mode: sonicMode,
      },
    });
    if (!data.ok) {
      setQrStatus(data.error || 'Onboarding thất bại', 'error');
      showToast(data.error || 'Onboarding thất bại', 'error');
      return;
    }
    const panel = document.getElementById('sonic-output-panel');
    const audio = document.getElementById('sonic-audio');
    const qrWrap = document.getElementById('wifi-qr-box');
    const qrImg = document.getElementById('wifi-qr-preview');
    if (panel) panel.style.display = 'flex';
    if (audio && data.wav_data_url) {
      audio.src = data.wav_data_url;
      audio.play().catch(() => {});
    }
    if (qrImg && data.qr_data_url) qrImg.src = data.qr_data_url;
    if (qrWrap && data.qr_data_url) qrWrap.style.display = 'flex';
    await refreshMobileSonicLinkPreview();

    setQrStatus(data.message || 'Đã khởi tạo phiên Onboarding 180 giây!', 'ok');
    startOnboardProgressPolling(data.uid, data.formatted_did || did, true);
  } catch (err) {
    setQrStatus(`Lỗi Onboarding: ${err.message}`, 'error');
  }
}

export function openQrModal(prefillDid = '', prefillRoom = '', defaultTab = 'link', focusPwd = false) {
  const modal = document.getElementById('qr-modal');
  if (!modal) return;
  const didEl = document.getElementById('qr-did-input');
  const roomEl = document.getElementById('qr-room-input');
  const pwdEl = document.getElementById('qr-pwd-input');
  const obCard = document.getElementById('onboard-progress-card');
  if (didEl) didEl.value = prefillDid || '';
  if (roomEl) roomEl.value = prefillRoom || '';
  if (obCard) obCard.style.display = 'none';
  setQrStatus('', 'info');
  setWizardTab(defaultTab || 'link');

  modal.classList.add('open');
  if (focusPwd && pwdEl) {
    pwdEl.focus();
    pwdEl.select();
  } else if (didEl && !didEl.value) {
    didEl.focus();
  }
}

export function closeQrModal() {
  stopOnboardPolling();
  document.getElementById('qr-modal')?.classList.remove('open');
  const audio = document.getElementById('sonic-audio');
  if (audio) {
    audio.pause();
    audio.currentTime = 0;
  }
}

export function initQrModal({ onRefreshStatus } = {}) {
  onRefreshCb = onRefreshStatus || null;
  const modal = document.getElementById('qr-modal');
  if (!modal) return;

  modal.addEventListener('click', (ev) => {
    if (ev.target === modal) closeQrModal();
  });
  document.getElementById('qr-close-btn')?.addEventListener('click', closeQrModal);

  document.querySelectorAll('.wizard-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => setWizardTab(btn.dataset.wizTab));
  });

  // Password eye toggles inside wizard
  document.getElementById('qr-pwd-eye')?.addEventListener('click', () => {
    const inp = document.getElementById('qr-pwd-input');
    if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';
  });
  document.getElementById('wifi-pwd-eye')?.addEventListener('click', () => {
    const inp = document.getElementById('wifi-pwd-input');
    if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';
  });

  // Wi-Fi helper chips & BSSID auto mode
  document.getElementById('wifi-ssid-chips')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-pick-ssid]');
    if (!btn) return;
    const ssidEl = document.getElementById('wifi-ssid-input');
    if (ssidEl) ssidEl.value = btn.dataset.pickSsid;
    refreshMobileSonicLinkPreview();
  });
  document.getElementById('wifi-bssid-input')?.addEventListener('input', syncSonicModeFromBssid);
  document.getElementById('btn-refresh-wifi-info')?.addEventListener('click', () => {
    hostWifiLoaded = false;
    loadHostWifiInfo();
  });

  // Dropzone, File Select & Paste QR image
  const dz = document.getElementById('qr-dropzone');
  const fileInput = document.getElementById('qr-file-input');
  dz?.addEventListener('click', () => fileInput?.click());
  dz?.addEventListener('dragover', (e) => {
    e.preventDefault();
    dz.classList.add('dragover');
  });
  dz?.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz?.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('dragover');
    const file = e.dataTransfer?.files?.[0];
    if (file) decodeQrFile(file);
  });
  fileInput?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) decodeQrFile(file);
    fileInput.value = '';
  });
  window.addEventListener('paste', (e) => {
    if (!modal.classList.contains('open')) return;
    const items = e.clipboardData?.items || [];
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) decodeQrFile(file);
        break;
      }
    }
  });

  // Action buttons
  document.getElementById('btn-link-camera-submit')?.addEventListener('click', submitLinkCamera);
  document.getElementById('btn-onboard-unbox')?.addEventListener('click', submitUnboxOnboard);
  document.getElementById('btn-sonic-browser')?.addEventListener('click', playSonicWaveBrowser);
  document.getElementById('btn-sonic-pc')?.addEventListener('click', playSonicWavePcSpeaker);
  document.getElementById('btn-wifi-qr')?.addEventListener('click', refreshWifiQrPreview);
  document.getElementById('btn-wifi-cgi')?.addEventListener('click', pushWifiDirectCgi);
  document.getElementById('btn-copy-mobile-url')?.addEventListener('click', async () => {
    const href = document.getElementById('mobile-sonic-url')?.href;
    if (!href || href === '#') return;
    try {
      await navigator.clipboard.writeText(href);
      showToast('Đã copy đường dẫn /sonic cho điện thoại', 'ok');
    } catch (_) {
      showToast('Không thể copy tự động — hãy bấm giữ link để sao chép', 'warn');
    }
  });

  registerModalEsc(() => modal.classList.contains('open'), closeQrModal);
}
