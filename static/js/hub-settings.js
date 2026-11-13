/**
 * Tab 3: Smart Motion NVR Settings, Room Lifecycle Manager & Network Diagnostics Table.
 */
import { apiJson, getDisplayFps, store } from './api.js';
import {
  changeCameraPassword,
  deleteCamera,
  pushWifiToCamera,
  rebootCamera,
  reconnectCamera,
} from './cam-actions.js';
import { escHtml, svgIcon } from './icons.js';
import { showToast } from './ui-feedback.js';

let onRefreshCb = null;

export function syncSettingsForm(settings = store.settings) {
  if (!settings) return;
  const setVal = (id, v) => {
    const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = v;
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = Boolean(v);
  };
  setVal('set-preroll', settings.pre_roll_sec);
  setVal('set-postroll', settings.post_roll_sec);
  setVal('set-sens', settings.pframe_sensitivity);
  setVal('set-pframe-cd', settings.pframe_cooldown_sec ?? 20);
  setChk('set-pframe-en', settings.enable_pframe_trigger);
  setChk('set-auto-25fps', settings.auto_25fps);
  setVal('set-maxgb', settings.max_storage_gb);
  setVal('set-freegb', settings.min_free_gb);
}

async function saveHubSettings() {
  const payload = {
    pre_roll_sec: Number(document.getElementById('set-preroll')?.value || 6),
    post_roll_sec: Number(document.getElementById('set-postroll')?.value || 6),
    pframe_sensitivity: Number(document.getElementById('set-sens')?.value || 2.8),
    pframe_cooldown_sec: Number(document.getElementById('set-pframe-cd')?.value ?? 20),
    enable_pframe_trigger: Boolean(document.getElementById('set-pframe-en')?.checked),
    auto_25fps: Boolean(document.getElementById('set-auto-25fps')?.checked),
    max_storage_gb: Number(document.getElementById('set-maxgb')?.value || 50),
    min_free_gb: Number(document.getElementById('set-freegb')?.value || 5),
  };
  try {
    const { res, data } = await apiJson('/api/settings', {
      method: 'POST',
      body: payload,
    });
    if (!res.ok || data.ok === false) {
      showToast(data.error || 'Không lưu được cấu hình Hub', 'error');
      return;
    }
    showToast('Đã lưu & áp dụng cấu hình NVR ngay lập tức', 'ok');
    if (onRefreshCb) onRefreshCb();
  } catch (err) {
    showToast(`Lỗi lưu cấu hình: ${err.message}`, 'error');
  }
}

export function renderRoomRenameList() {
  const container = document.getElementById('room-Rename-list');
  if (!container) return;

  const sig = store.cameras.map((c) => `${c.uid}:${c.room_name}`).join('|');
  if (container.dataset.sig === sig && container.children.length) return;
  container.dataset.sig = sig;

  if (!store.cameras.length) {
    container.innerHTML = '<div style="color:var(--text-muted); font-size:12.5px;">Chưa có camera nào được nạp.</div>';
    return;
  }

  container.innerHTML = store.cameras.map((c) => {
    const didStr = c.did || c.formatted_did || c.short_uid || (c.uid || '').substring(0, 8);
    const ipStr = c.ip && c.ip !== '0.0.0.0' ? `${c.ip}:${c.port}` : 'Chờ IP';
    const modeStr = c.connection_mode || 'P2P';
    return `
      <div class="room-item-card" data-room-uid="${escHtml(c.uid)}">
        <div class="room-item-top">
          <div>
            <div style="font-weight:700; font-size:13px; color:var(--text-primary);">${escHtml(didStr)}</div>
            <div class="mono" style="font-size:11px; color:var(--text-muted);">${escHtml(modeStr)} · ${escHtml(ipStr)} · RTSP :${escHtml(c.rtsp_port)}</div>
          </div>
          <span class="badge ${c.state === 'CONNECTED' ? 'ok' : c.auth_failed ? 'danger' : 'warn'}">${escHtml(c.auth_failed ? 'Sai MK' : (c.state || 'WAIT'))}</span>
        </div>
        <div style="display:flex; gap:6px; align-items:center;">
          <input type="text" class="room-name-inp" value="${escHtml(c.room_name)}" placeholder="Tên phòng..." style="flex:1; min-width:140px;">
          <button type="button" class="btn sm primary" data-room-act="rename" data-uid="${escHtml(c.uid)}">${svgIcon('save', 'icon-sm')}<span>Lưu tên</span></button>
        </div>
        <div class="room-item-actions">
          <button type="button" class="btn sm" data-room-act="wifi" data-uid="${escHtml(c.uid)}">${svgIcon('wifi', 'icon-sm')}<span>Wi-Fi</span></button>
          <button type="button" class="btn sm" data-room-act="p2p" data-uid="${escHtml(c.uid)}">${svgIcon('zap', 'icon-sm')}<span>Tối ưu P2P</span></button>
          <button type="button" class="btn sm" data-room-act="pwd" data-uid="${escHtml(c.uid)}">${svgIcon('key', 'icon-sm')}<span>Đổi MK</span></button>
          <button type="button" class="btn sm" data-room-act="reboot" data-uid="${escHtml(c.uid)}">${svgIcon('power', 'icon-sm')}<span>Reboot</span></button>
          <button type="button" class="btn sm danger" data-room-act="delete" data-uid="${escHtml(c.uid)}">${svgIcon('trash', 'icon-sm')}<span>Xóa</span></button>
        </div>
      </div>
    `;
  }).join('');
}

export function renderNetworkDiagnostics() {
  const tbody = document.getElementById('net-diag-body');
  if (!tbody) return;

  if (!store.cameras.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-muted); text-align:center; padding:18px;">Chưa có phiên kết nối camera nào.</td></tr>';
    return;
  }

  tbody.innerHTML = store.cameras.map((c) => {
    const mode = String(c.connection_mode || 'LAN');
    const modeUpper = mode.toUpperCase();
    const badgeCls = modeUpper.includes('RELAY') ? 'badge relay' : modeUpper.includes('WAN') ? 'badge info' : 'badge ok';
    const rttVal = c.wan_relay_rtt_ms ?? c.cmd_srtt_ms;
    const rttStr = rttVal !== null && rttVal !== undefined ? `${Math.round(rttVal)} ms` : '—';
    const liveFps = getDisplayFps(c);
    const didStr = c.did || c.formatted_did || c.short_uid || c.uid.substring(0, 8);
    const ipStr = c.ip && c.ip !== '0.0.0.0' ? `${c.ip}:${c.port}` : (c.wan_cloud_status || 'WAIT');
    const retxInfo = `${c.cmd_unacked ?? 0} unack / ${c.cmd_retx ?? 0} retx`;

    return `
      <tr>
        <td data-label="Camera">
          <div style="font-weight:700; color:var(--text-primary);">${escHtml(c.room_name || didStr)}</div>
          <div class="mono" style="font-size:11px; color:var(--text-muted);">${escHtml(didStr)}</div>
        </td>
        <td data-label="Đường truyền">
          <span class="${badgeCls}">${escHtml(mode)}</span>
          <div class="mono" style="font-size:11px; color:var(--text-muted); margin-top:3px;">${escHtml(ipStr)}</div>
        </td>
        <td data-label="RTT" class="mono">${escHtml(rttStr)}</td>
        <td data-label="Luồng Video" class="mono">${liveFps} / ${c.fps ?? 0} fps · ${c.kbps ?? 0} KB/s<br><span style="color:var(--text-muted);">${escHtml(c.codec || 'H264')} · Loss ${c.packet_loss_pct ?? 0}%</span></td>
        <td data-label="Kênh lệnh" class="mono" style="font-size:11.5px;">
          <span class="badge ${c.state === 'CONNECTED' ? 'ok' : c.auth_failed ? 'danger' : 'warn'}">${escHtml(c.auth_failed ? 'Sai MK' : (c.state || 'WAIT'))}</span>
          <div style="color:var(--text-muted); margin-top:3px;">${escHtml(retxInfo)}</div>
        </td>
        <td data-label="Tác vụ" style="text-align:right;">
          <button type="button" class="btn sm" data-diag-opt="${escHtml(c.uid)}">${svgIcon('zap', 'icon-sm')}<span>Tối ưu</span></button>
        </td>
      </tr>
    `;
  }).join('');
}

export function initHubSettings({ onRefreshStatus } = {}) {
  onRefreshCb = onRefreshStatus || null;
  document.getElementById('btn-save-hub-settings')?.addEventListener('click', saveHubSettings);

  document.getElementById('room-Rename-list')?.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('[data-room-act]');
    if (!btn) return;
    const uid = btn.dataset.uid;
    const act = btn.dataset.roomAct;
    const cam = store.cameras.find((c) => c.uid === uid);
    const name = cam?.room_name || uid;

    if (act === 'rename') {
      const card = btn.closest('.room-item-card');
      const newName = (card?.querySelector('.room-name-inp')?.value || '').trim();
      if (!newName) {
        showToast('Tên phòng không được để trống', 'warn');
        return;
      }
      const { res, data } = await apiJson(`/api/camera/${uid}/rename`, {
        method: 'POST',
        body: { room_name: newName },
      });
      if (!res.ok || data.ok === false) {
        showToast(data.error || 'Đổi tên thất bại', 'error');
        return;
      }
      showToast(`Đã đổi tên phòng thành "${newName}"`, 'ok');
      const list = document.getElementById('room-Rename-list');
      if (list) list.dataset.sig = '';
      if (onRefreshCb) onRefreshCb();
    } else if (act === 'wifi') {
      pushWifiToCamera(uid, name);
    } else if (act === 'p2p') {
      reconnectCamera(uid, onRefreshCb);
    } else if (act === 'pwd') {
      changeCameraPassword(uid, name, onRefreshCb);
    } else if (act === 'reboot') {
      rebootCamera(uid, name, onRefreshCb);
    } else if (act === 'delete') {
      deleteCamera(uid, name, onRefreshCb);
    }
  });

  document.getElementById('net-diag-body')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-diag-opt]');
    if (!btn) return;
    reconnectCamera(btn.dataset.diagOpt, onRefreshCb);
  });
}
