/**
 * Main entry point for C22S Camera Hub Dashboard:
 * Coordinates polling (/api/status), WebSocket streams, tab switching,
 * layout controls, and initializes all modals cleanly.
 */
import { apiJson, store } from './api.js';
import { initCameraGrid, renderCameraCards } from './cards.js';
import { initEventsTab, loadEvents } from './events.js';
import {
  initHubSettings,
  renderNetworkDiagnostics,
  renderRoomRenameList,
  syncSettingsForm,
} from './hub-settings.js';
import { svgIcon } from './icons.js';
import { initFocusModal, openFocusModal, updateFocusStageTelemetry } from './ptz-modal.js';
import { initQrModal, openQrModal } from './qr-modal.js';
import { initSettingsModal, openSettingsModal } from './settings-modal.js';
import { showToast } from './ui-feedback.js';
import { connectStreamWs, syncWsConfig } from './ws-stream.js';

let statusTimer = null;
let statusInFlight = null;
let lastStatusAt = 0;

function shouldPollContinuously() {
  return !document.hidden && (store.currentTab === 'live' || Boolean(store.focusUid));
}

function stopStatusPoll() {
  if (statusTimer) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
}

function scheduleStatusPoll() {
  stopStatusPoll();
  if (!shouldPollContinuously()) return;
  statusTimer = setTimeout(() => {
    refreshStatus();
  }, 1500);
}

function switchTab(tab) {
  store.currentTab = tab;
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.view-section').forEach((sec) => {
    sec.classList.toggle('active', sec.id === `tab-${tab}`);
  });
  syncWsConfig();
  stopStatusPoll();

  if (tab === 'live') {
    renderCameraCards();
    refreshStatus();
  } else if (tab === 'events') {
    loadEvents();
  } else if (tab === 'settings') {
    syncSettingsForm();
    renderRoomRenameList();
    renderNetworkDiagnostics();
    if (Date.now() - lastStatusAt > 3000) {
      refreshStatus();
    }
  }
}

function updateHeaderStats(data) {
  const cams = store.cameras;
  const connectedCount = cams.filter((c) => c.state === 'CONNECTED').length;
  const recCount = cams.filter((c) => c.is_recording).length;

  const camBadge = document.getElementById('hdr-cam-count');
  if (camBadge) {
    if (!cams.length) {
      camBadge.className = 'badge';
      camBadge.textContent = '0 Camera';
    } else if (recCount > 0) {
      camBadge.className = 'badge rec';
      camBadge.textContent = `${connectedCount}/${cams.length} Online · ${recCount} REC`;
    } else {
      camBadge.className = connectedCount > 0 ? 'badge ok' : 'badge warn';
      camBadge.textContent = `${connectedCount}/${cams.length} Online`;
    }
  }

  const stBadge = document.getElementById('hdr-storage');
  if (stBadge) {
    const preMb = (cams.reduce((acc, c) => acc + (c.ram_buffer_kb || 0), 0) / 1024).toFixed(1);
    const st = data.storage || {};
    stBadge.textContent = `RAM ${preMb} MB · ${st.clip_count ?? 0} clip (${st.used_mb ?? 0} MB) · Trống ${st.disk_free_gb ?? 0} GB`;
  }
}

export async function refreshStatus() {
  if (statusInFlight) return statusInFlight;
  stopStatusPoll();

  statusInFlight = (async () => {
    try {
      const { data } = await apiJson('/api/status');
      store.cameras = Array.isArray(data.cameras) ? data.cameras : [];
      store.storage = data.storage || {};
      store.settings = data.settings || null;
      lastStatusAt = Date.now();

      updateHeaderStats(data);
      const grid = document.getElementById('cam-grid');
      if (store.currentTab === 'live' || (grid && !grid.children.length)) {
        renderCameraCards();
      }

      if (store.focusUid) {
        updateFocusStageTelemetry();
      }
      if (store.currentTab === 'settings') {
        syncSettingsForm(store.settings);
        renderRoomRenameList();
        renderNetworkDiagnostics();
      }
    } catch (err) {
      console.warn('Status poll error:', err);
    } finally {
      statusInFlight = null;
      scheduleStatusPoll();
    }
  })();

  return statusInFlight;
}

async function scanLanNow() {
  const btn = document.getElementById('btn-scan-lan');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `${svgIcon('radar', 'icon-sm icon-spin')}<span>Đang quét LAN...</span>`;
  }
  try {
    const { data } = await apiJson('/api/camera/scan', { method: 'POST' });
    showToast(
      `Đã quét mạng LAN: phát hiện ${data.found_new ?? 0} camera mới (tổng ${data.active_count ?? 0} active)`,
      data.found_new > 0 ? 'ok' : 'info',
    );
    await refreshStatus();
  } catch (err) {
    showToast(`Lỗi quét LAN: ${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `${svgIcon('radar', 'icon-sm')}<span>Quét LAN</span>`;
    }
  }
}

function applyGridColumns(val) {
  const grid = document.getElementById('cam-grid');
  if (!grid) return;
  grid.classList.remove('cols-2', 'cols-3', 'cols-4');
  if (val === '2' || val === '3' || val === '4') {
    grid.classList.add(`cols-${val}`);
  }
}

function bootstrap() {
  initCameraGrid({
    openFocus: openFocusModal,
    openSettings: openSettingsModal,
    openQr: openQrModal,
    refreshStatus,
  });
  initFocusModal({
    onRefreshStatus: refreshStatus,
    onOpenSettings: openSettingsModal,
    onOpenQr: openQrModal,
  });
  initSettingsModal();
  initQrModal({ onRefreshStatus: refreshStatus });
  initEventsTab({ onRefreshStatus: refreshStatus });
  initHubSettings({ onRefreshStatus: refreshStatus });

  // Navigation Tabs
  document.querySelectorAll('.tab-btn[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Toolbar controls
  document.getElementById('btn-open-qr')?.addEventListener('click', () => openQrModal('', '', 'link'));
  document.getElementById('btn-scan-lan')?.addEventListener('click', scanLanNow);
  document.getElementById('btn-refresh-diag')?.addEventListener('click', () => refreshStatus());
  document.getElementById('grid-cols')?.addEventListener('change', (e) => applyGridColumns(e.target.value));
  document.getElementById('grid-fps')?.addEventListener('change', syncWsConfig);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopStatusPoll();
    } else if (shouldPollContinuously()) {
      refreshStatus();
    }
  });

  refreshStatus();
  connectStreamWs();
}

bootstrap();
