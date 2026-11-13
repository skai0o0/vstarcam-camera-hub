/**
 * Live Grid camera cards renderer: uniform card height, dedicated header strip,
 * rich offline/auth-failed state placeholders, tabular telemetry strip, and clean action dock.
 */
import { getDisplayFps, store } from './api.js';
import { reconnectCamera, toggleManualRecord, triggerMotionManual } from './cam-actions.js';
import { escHtml, svgIcon } from './icons.js';

let cardCallbacks = {
  openFocus: null,
  openSettings: null,
  openQr: null,
  refreshStatus: null,
};

export function initCameraGrid(callbacks) {
  cardCallbacks = { ...cardCallbacks, ...callbacks };
  const grid = document.getElementById('cam-grid');
  if (!grid) return;

  grid.addEventListener('click', (ev) => {
    const actionBtn = ev.target.closest('[data-card-action]');
    if (actionBtn) {
      ev.stopPropagation();
      const uid = actionBtn.dataset.uid;
      const action = actionBtn.dataset.cardAction;
      const cam = store.cameras.find((c) => c.uid === uid);
      if (action === 'trigger') {
        triggerMotionManual(uid, 'trigger', cardCallbacks.refreshStatus);
      } else if (action === 'manual') {
        toggleManualRecord(uid, cardCallbacks.refreshStatus);
      } else if (action === 'focus' && cardCallbacks.openFocus) {
        cardCallbacks.openFocus(uid);
      } else if (action === 'settings' && cardCallbacks.openSettings) {
        cardCallbacks.openSettings(uid);
      } else if (action === 'fix-auth' && cardCallbacks.openQr && cam) {
        cardCallbacks.openQr(cam.formatted_did || cam.did || '', cam.room_name || '', 'link', true);
      } else if (action === 'opt-p2p') {
        reconnectCamera(uid, cardCallbacks.refreshStatus);
      } else if (action === 'open-qr' && cardCallbacks.openQr && cam) {
        cardCallbacks.openQr(cam.formatted_did || cam.did || '', cam.room_name || '', 'unbox');
      }
      return;
    }

    const stage = ev.target.closest('.cam-video-wrap');
    if (stage && stage.dataset.uid && cardCallbacks.openFocus) {
      cardCallbacks.openFocus(stage.dataset.uid);
    }
  });
}

function resolveModeMeta(cam) {
  const connMode = (cam.connection_mode || 'LAN').toUpperCase();
  const rttVal = cam.wan_relay_rtt_ms ?? cam.cmd_srtt_ms;
  const rttTag = rttVal !== null && rttVal !== undefined ? ` ${Math.round(rttVal)}ms` : '';

  if (connMode.includes('RELAY')) {
    const highRtt = rttVal !== null && rttVal !== undefined && rttVal > 80;
    return {
      cls: highRtt ? 'badge warn' : 'badge relay',
      icon: 'cloud',
      label: `RELAY${rttTag}`,
    };
  }
  if (connMode.includes('WAN')) {
    return {
      cls: 'badge info',
      icon: 'globe',
      label: `WAN P2P${rttTag}`,
    };
  }
  return {
    cls: 'badge ok',
    icon: 'wifi',
    label: `LAN${rttTag}`,
  };
}

function buildStateOverlay(cam, isWaiting, didStr, ipPort) {
  if (cam.auth_failed) {
    return {
      show: true,
      icon: 'lock',
      iconColor: 'var(--warning-text)',
      title: 'Camera yêu cầu mật khẩu riêng',
      sub: `Đã kết nối tới ${didStr} (${ipPort}) nhưng mật khẩu hiện tại không khớp.`,
      btnHtml: `<button type="button" class="btn primary sm" data-card-action="fix-auth" data-uid="${escHtml(cam.uid)}">${svgIcon('key', 'icon-sm')} Nhập mật khẩu</button>`,
    };
  }
  if (cam.onboarding_active) {
    return {
      show: true,
      icon: 'radar',
      iconColor: 'var(--accent-text)',
      title: `Đang liên kết ${didStr}`,
      sub: `Trạng thái Cloud: ${cam.wan_cloud_status || 'WAITING_FOR_WIFI'} · Đang tìm & đục lỗ UDP...`,
      btnHtml: `<button type="button" class="btn sm" data-card-action="open-qr" data-uid="${escHtml(cam.uid)}">${svgIcon('qr', 'icon-sm')} Xem tiến trình</button>`,
    };
  }
  if (isWaiting || (cam.state !== 'CONNECTED' && !(cam.ram_buffer_kb > 0))) {
    const cloudSt = cam.wan_cloud_status || 'IDLE';
    return {
      show: true,
      icon: 'refresh',
      iconColor: 'var(--accent-text)',
      title: `Đang kết nối (${cam.state || 'CONNECTING'})`,
      sub: `${didStr} · Cloud: ${cloudSt} · ${ipPort}`,
      btnHtml: `<button type="button" class="btn sm" data-card-action="opt-p2p" data-uid="${escHtml(cam.uid)}">${svgIcon('zap', 'icon-sm')} Tối ưu P2P</button>`,
    };
  }
  return { show: false };
}

export function renderCameraCards() {
  const grid = document.getElementById('cam-grid');
  if (!grid) return;

  if (!store.cameras.length) {
    grid.innerHTML = `
      <div class="card-panel" style="grid-column:1 / -1; text-align:center; padding:42px 18px;">
        <div style="color:var(--text-muted); margin-bottom:10px;">${svgIcon('camera', 'icon-lg')}</div>
        <div style="font-weight:700; font-size:15px; margin-bottom:4px;">Chưa có camera nào được nạp vào Hub</div>
        <div style="color:var(--text-muted); font-size:13px;">Bấm <b>Thêm Camera</b> ở thanh trên hoặc <b>Quét LAN</b> để bắt đầu giám sát.</div>
      </div>
    `;
    return;
  }

  const existingCards = grid.querySelectorAll('.cam-card');
  const existingUids = new Set(Array.from(existingCards).map((el) => el.dataset.uid));
  const currentUids = new Set(store.cameras.map((c) => c.uid));

  if (existingCards.length !== store.cameras.length || [...existingUids].some((u) => !currentUids.has(u))) {
    grid.innerHTML = '';
    for (const c of store.cameras) {
      const card = document.createElement('article');
      card.className = 'cam-card';
      card.dataset.uid = c.uid;
      card.id = `card-${c.uid}`;
      card.innerHTML = `
        <header class="cam-card-head">
          <div class="cam-title-wrap">
            <span class="cam-room-name" id="title-${escHtml(c.uid)}">${escHtml(c.room_name)}</span>
          </div>
          <div class="cam-head-badges">
            <span class="badge" id="mode-${escHtml(c.uid)}"></span>
            <span class="badge" id="badge-${escHtml(c.uid)}"></span>
          </div>
        </header>
        <div class="cam-video-wrap" data-uid="${escHtml(c.uid)}">
          <img id="img-${escHtml(c.uid)}" src="/api/camera/${encodeURIComponent(c.uid)}/snapshot.jpg?t=${Date.now()}" alt="${escHtml(c.room_name)}">
          <span class="cam-fps-pip" id="fps-pip-${escHtml(c.uid)}" style="display:none;">0 FPS</span>
          <span class="badge rec cam-rec-pip" id="rec-pip-${escHtml(c.uid)}" style="display:none;">REC</span>
          <div class="cam-state-overlay" id="overlay-${escHtml(c.uid)}" style="display:none;"></div>
        </div>
        <div class="cam-telemetry-strip">
          <span class="cam-telem-left mono" id="telem-left-${escHtml(c.uid)}"></span>
          <span class="cam-telem-right" id="telem-right-${escHtml(c.uid)}"></span>
        </div>
        <div class="cam-actions">
          <button type="button" class="btn sm" data-card-action="trigger" data-uid="${escHtml(c.uid)}" title="Ghi nhanh đoạn Pre+Post theo cấu hình Hub">
            ${svgIcon('zap', 'icon-sm')}<span>Ghi nhanh</span>
          </button>
          <button type="button" class="btn sm" id="mbtn-${escHtml(c.uid)}" data-card-action="manual" data-uid="${escHtml(c.uid)}">
            ${svgIcon('circleDot', 'icon-sm')}<span>Ghi tay</span>
          </button>
          <button type="button" class="btn sm primary" data-card-action="focus" data-uid="${escHtml(c.uid)}">
            ${svgIcon('maximize', 'icon-sm')}<span>PTZ / Zoom</span>
          </button>
          <button type="button" class="btn sm" data-card-action="settings" data-uid="${escHtml(c.uid)}" title="Mở toàn bộ setting đã reverse của camera">
            ${svgIcon('settings', 'icon-sm')}<span>Setting</span>
          </button>
        </div>
      `;
      const imgEl = card.querySelector('img');
      imgEl.addEventListener('error', () => {
        if (!imgEl.dataset.hasLiveFrame) imgEl.style.display = 'none';
      });
      imgEl.addEventListener('load', () => {
        imgEl.style.display = 'block';
      });
      grid.appendChild(card);
    }

    const evSel = document.getElementById('ev-room-filter');
    if (evSel) {
      const curVal = evSel.value;
      evSel.innerHTML = '<option value="ALL">Tất cả phòng</option>' +
        store.cameras.map((c) => `<option value="${escHtml(c.room_name)}">${escHtml(c.room_name)} (${escHtml(c.did || c.formatted_did || c.ip)})</option>`).join('');
      evSel.value = curVal || 'ALL';
    }
  }

  for (const c of store.cameras) {
    const card = document.getElementById(`card-${c.uid}`);
    if (!card) continue;
    card.className = `cam-card${c.is_recording ? ' recording' : ''}`;

    const isWaiting = c.state === 'WAITING_LAN' || c.state === 'CONNECTING' || c.state === 'DISCONNECTED';
    const liveFps = getDisplayFps(c);
    const didStr = c.did || c.formatted_did || c.hw_deviceid || c.short_uid || (c.uid || '').substring(0, 8);
    const ipPort = (c.ip && c.ip !== '0.0.0.0' && c.ip !== 'Chờ Wi-Fi LAN')
      ? (c.port ? `${c.ip}:${c.port}` : c.ip)
      : '0.0.0.0:32108';

    const titleEl = document.getElementById(`title-${c.uid}`);
    if (titleEl) {
      titleEl.textContent = c.room_name || didStr;
      titleEl.title = `${c.room_name} (${ipPort}) — ${didStr}`;
    }

    const modeMeta = resolveModeMeta(c);
    const modeBadge = document.getElementById(`mode-${c.uid}`);
    if (modeBadge) {
      modeBadge.className = modeMeta.cls;
      modeBadge.title = didStr;
      modeBadge.innerHTML = `${svgIcon(modeMeta.icon, 'icon-sm')}<span>${escHtml(modeMeta.label)}</span>`;
    }

    const stBadge = document.getElementById(`badge-${c.uid}`);
    const recPip = document.getElementById(`rec-pip-${c.uid}`);
    if (stBadge) {
      if (c.is_recording) {
        stBadge.className = 'badge rec';
        stBadge.textContent = `REC +${c.post_roll_remaining}s`;
        if (recPip) recPip.style.display = 'inline-flex';
      } else if (c.auth_failed) {
        stBadge.className = 'badge danger';
        stBadge.textContent = 'Sai MK';
        if (recPip) recPip.style.display = 'none';
      } else if (isWaiting || c.state !== 'CONNECTED') {
        stBadge.className = 'badge warn';
        stBadge.textContent = c.state || 'WAIT';
        if (recPip) recPip.style.display = 'none';
      } else {
        stBadge.className = 'badge ok';
        stBadge.textContent = `${c.ram_buffer_kb ?? 0} KB`;
        if (recPip) recPip.style.display = 'none';
      }
    }

    const overlaySpec = buildStateOverlay(c, isWaiting, didStr, ipPort);
    const overlayEl = document.getElementById(`overlay-${c.uid}`);
    const fpsPipEl = document.getElementById(`fps-pip-${c.uid}`);
    if (overlayEl) {
      if (overlaySpec.show) {
        overlayEl.style.display = 'flex';
        overlayEl.innerHTML = `
          <div class="cam-state-icon" style="color:${overlaySpec.iconColor};">${svgIcon(overlaySpec.icon, 'icon-lg')}</div>
          <div class="cam-state-title">${escHtml(overlaySpec.title)}</div>
          <div class="cam-state-sub">${escHtml(overlaySpec.sub)}</div>
          ${overlaySpec.btnHtml}
        `;
        if (fpsPipEl) fpsPipEl.style.display = 'none';
      } else {
        overlayEl.style.display = 'none';
        if (fpsPipEl) {
          fpsPipEl.style.display = 'inline-block';
          fpsPipEl.textContent = `${liveFps} FPS`;
        }
      }
    }

    const leftEl = document.getElementById(`telem-left-${c.uid}`);
    if (leftEl) {
      leftEl.textContent = `${didStr} · ${ipPort}`;
      leftEl.title = `${didStr} (${ipPort})`;
    }
    const rightEl = document.getElementById(`telem-right-${c.uid}`);
    if (rightEl) {
      const codecStr = String(c.codec || (c.auth_failed ? 'AUTH' : 'WAIT')).toUpperCase();
      rightEl.textContent = `${codecStr} · ${c.fps ?? 0}fps · ${c.kbps ?? 0} KB/s · :${c.rtsp_port} · ${c.clips_saved ?? 0}c`;
    }

    const mbtn = document.getElementById(`mbtn-${c.uid}`);
    if (mbtn) {
      mbtn.className = `btn sm${c.manual_record ? ' danger' : ''}`;
      mbtn.innerHTML = c.manual_record
        ? `${svgIcon('stop', 'icon-sm')}<span>Dừng</span>`
        : `${svgIcon('circleDot', 'icon-sm')}<span>Ghi tay</span>`;
    }
  }
}
