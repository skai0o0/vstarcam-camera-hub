/**
 * Non-blocking toast stack, custom accessible dialog (confirm / prompt / wifi),
 * and centralized Escape key handling across modals.
 */
import { escHtml, svgIcon } from './icons.js';

let toastContainer = null;
let dialogBackdrop = null;
const escCloseCallbacks = [];

function ensureToastContainer() {
  if (toastContainer && document.body.contains(toastContainer)) return toastContainer;
  toastContainer = document.createElement('div');
  toastContainer.className = 'toast-stack';
  toastContainer.setAttribute('aria-live', 'polite');
  document.body.appendChild(toastContainer);
  return toastContainer;
}

export function showToast(message, level = 'info', durationMs = 3600) {
  const root = ensureToastContainer();
  const item = document.createElement('div');
  item.className = `toast-item ${level}`;
  const iconName = level === 'ok' ? 'check' : level === 'error' ? 'alert' : level === 'warn' ? 'alert' : 'circleDot';
  const colorVar = level === 'ok'
    ? 'var(--success-text)'
    : level === 'error'
      ? 'var(--danger-text)'
      : level === 'warn'
        ? 'var(--warning-text)'
        : 'var(--accent-text)';
  item.innerHTML = `
    <span style="color:${colorVar}; margin-top:1px;">${svgIcon(iconName, 'icon-sm')}</span>
    <span style="flex:1;">${escHtml(message)}</span>
    <button type="button" class="btn ghost icon-btn" aria-label="Đóng thông báo" style="min-width:24px; min-height:24px; padding:2px;">
      ${svgIcon('close', 'icon-sm')}
    </button>
  `;
  const remove = () => {
    if (item.parentNode) item.parentNode.removeChild(item);
  };
  item.querySelector('button').addEventListener('click', remove);
  root.appendChild(item);
  while (root.children.length > 5) root.removeChild(root.firstChild);
  if (durationMs > 0) setTimeout(remove, durationMs);
}

function ensureDialogRoot() {
  if (dialogBackdrop && document.body.contains(dialogBackdrop)) return dialogBackdrop;
  dialogBackdrop = document.createElement('div');
  dialogBackdrop.className = 'dialog-backdrop';
  dialogBackdrop.setAttribute('role', 'dialog');
  dialogBackdrop.setAttribute('aria-modal', 'true');
  document.body.appendChild(dialogBackdrop);
  return dialogBackdrop;
}

export function confirmDialog({ title = 'Xác nhận thao tác', message = '', confirmText = 'Xác nhận', cancelText = 'Hủy', danger = false }) {
  return new Promise((resolve) => {
    const root = ensureDialogRoot();
    const prevFocus = document.activeElement;
    root.innerHTML = `
      <div class="dialog-box">
        <div class="dialog-title">${escHtml(title)}</div>
        <div class="dialog-message">${escHtml(message)}</div>
        <div class="dialog-actions">
          <button type="button" class="btn" id="dlg-cancel">${escHtml(cancelText)}</button>
          <button type="button" class="btn ${danger ? 'danger' : 'primary'}" id="dlg-ok">${escHtml(confirmText)}</button>
        </div>
      </div>
    `;
    const close = (result) => {
      root.classList.remove('open');
      root.innerHTML = '';
      if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
      resolve(result);
    };
    root.querySelector('#dlg-cancel').addEventListener('click', () => close(false));
    root.querySelector('#dlg-ok').addEventListener('click', () => close(true));
    root.onclick = (e) => { if (e.target === root) close(false); };
    root.classList.add('open');
    root.querySelector(danger ? '#dlg-cancel' : '#dlg-ok').focus();
  });
}

export function promptDialog({
  title = 'Nhập thông tin',
  message = '',
  label = 'Giá trị',
  defaultValue = '',
  placeholder = '',
  password = false,
  confirmText = 'Lưu',
}) {
  return new Promise((resolve) => {
    const root = ensureDialogRoot();
    const prevFocus = document.activeElement;
    root.innerHTML = `
      <form class="dialog-box" id="dlg-form">
        <div class="dialog-title">${escHtml(title)}</div>
        ${message ? `<div class="dialog-message">${escHtml(message)}</div>` : ''}
        <div>
          <label class="field-label" for="dlg-input">${escHtml(label)}</label>
          <div style="display:flex; gap:6px; align-items:center;">
            <input id="dlg-input" type="${password ? 'password' : 'text'}" value="${escHtml(defaultValue)}" placeholder="${escHtml(placeholder)}" style="flex:1;" autocomplete="off">
            ${password ? `<button type="button" class="btn icon-btn" id="dlg-eye" aria-label="Hiện/ẩn mật khẩu">${svgIcon('eye', 'icon-sm')}</button>` : ''}
          </div>
        </div>
        <div class="dialog-actions">
          <button type="button" class="btn" id="dlg-cancel">Hủy</button>
          <button type="submit" class="btn primary">${escHtml(confirmText)}</button>
        </div>
      </form>
    `;
    const inp = root.querySelector('#dlg-input');
    const eye = root.querySelector('#dlg-eye');
    if (eye) {
      eye.addEventListener('click', () => {
        inp.type = inp.type === 'password' ? 'text' : 'password';
      });
    }
    const close = (val) => {
      root.classList.remove('open');
      root.innerHTML = '';
      if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
      resolve(val);
    };
    root.querySelector('#dlg-cancel').addEventListener('click', () => close(null));
    root.querySelector('#dlg-form').addEventListener('submit', (e) => {
      e.preventDefault();
      close(inp.value);
    });
    root.onclick = (e) => { if (e.target === root) close(null); };
    root.classList.add('open');
    inp.focus();
  });
}

export function wifiCredentialsDialog({ cameraName = '', defaultSsid = '', defaultPwd = '' }) {
  return new Promise((resolve) => {
    const root = ensureDialogRoot();
    const prevFocus = document.activeElement;
    root.innerHTML = `
      <form class="dialog-box" id="dlg-wifi-form">
        <div class="dialog-title">Đổi Wi-Fi từ xa — ${escHtml(cameraName)}</div>
        <div class="dialog-message">Gửi trực tiếp cấu hình Wi-Fi mới xuống camera qua kênh lệnh hiện tại (hoặc xếp hàng chờ camera online).</div>
        <div>
          <label class="field-label" for="dlg-wifi-ssid">Tên Wi-Fi 2.4GHz (SSID)</label>
          <input id="dlg-wifi-ssid" type="text" value="${escHtml(defaultSsid)}" placeholder="Nhập chính xác tên Wi-Fi..." style="width:100%;" required>
        </div>
        <div>
          <label class="field-label" for="dlg-wifi-pwd">Mật khẩu Wi-Fi</label>
          <div style="display:flex; gap:6px; align-items:center;">
            <input id="dlg-wifi-pwd" type="password" value="${escHtml(defaultPwd)}" placeholder="Để trống nếu mạng mở" style="flex:1;">
            <button type="button" class="btn icon-btn" id="dlg-wifi-eye" aria-label="Hiện/ẩn mật khẩu">${svgIcon('eye', 'icon-sm')}</button>
          </div>
        </div>
        <div class="dialog-actions">
          <button type="button" class="btn" id="dlg-cancel">Hủy</button>
          <button type="submit" class="btn primary">Gửi cấu hình Wi-Fi</button>
        </div>
      </form>
    `;
    const ssidEl = root.querySelector('#dlg-wifi-ssid');
    const pwdEl = root.querySelector('#dlg-wifi-pwd');
    root.querySelector('#dlg-wifi-eye').addEventListener('click', () => {
      pwdEl.type = pwdEl.type === 'password' ? 'text' : 'password';
    });
    const close = (val) => {
      root.classList.remove('open');
      root.innerHTML = '';
      if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
      resolve(val);
    };
    root.querySelector('#dlg-cancel').addEventListener('click', () => close(null));
    root.querySelector('#dlg-wifi-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const ssid = ssidEl.value.trim();
      if (!ssid) return;
      close({ ssid, password: pwdEl.value });
    });
    root.onclick = (e) => { if (e.target === root) close(null); };
    root.classList.add('open');
    ssidEl.focus();
  });
}

const WIFI_STAGE_LABELS = {
  precheck: 'Kiểm tra mạng hiện tại',
  scan: 'Camera quét Wi-Fi',
  apply: 'Gửi cấu hình',
  switch: 'Rời mạng cũ',
  reconnect: 'Kết nối lại Hub',
  confirm: 'Xác nhận mạng mới',
  rollback: 'Khôi phục mạng cũ',
};

const WIFI_RESULT_STYLE = {
  success: { color: 'var(--success-text)', icon: 'check', title: 'Thành công' },
  fallback: { color: 'var(--warning-text)', icon: 'alert', title: 'Không vào được mạng mới — đã quay về mạng cũ' },
  failed: { color: 'var(--danger-text)', icon: 'alert', title: 'Thất bại' },
  timeout: { color: 'var(--danger-text)', icon: 'alert', title: 'Camera chưa quay lại' },
};

/**
 * Live progress dialog for a verified Wi-Fi switch job.
 * Returns { update(job), isOpen() }; onForce / onRetry are called from the result buttons.
 */
export function wifiJobDialog({ cameraName = '', job = null, onForce = null, onRetry = null, onClose = null }) {
  const root = ensureDialogRoot();
  const prevFocus = document.activeElement;
  let open = true;
  let last = job;

  const close = () => {
    if (!open) return;
    open = false;
    root.classList.remove('open');
    root.innerHTML = '';
    if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
    if (onClose) onClose(last);
  };

  const stepIcon = (status) => {
    if (status === 'ok') return `<span style="color:var(--success-text)">${svgIcon('check', 'icon-sm')}</span>`;
    if (status === 'error') return `<span style="color:var(--danger-text)">${svgIcon('alert', 'icon-sm')}</span>`;
    if (status === 'warn') return `<span style="color:var(--warning-text)">${svgIcon('alert', 'icon-sm')}</span>`;
    return `<span style="color:var(--accent-text)">${svgIcon('circleDot', 'icon-sm icon-spin')}</span>`;
  };

  const render = (j) => {
    if (!open) return;
    last = j;
    const steps = (j && j.steps) || [];
    const done = j && j.state !== 'running';
    const res = j && j.result ? WIFI_RESULT_STYLE[j.result] || WIFI_RESULT_STYLE.failed : null;
    const stepsHtml = steps.map((s) => `
      <li style="display:flex; gap:8px; align-items:flex-start; padding:4px 0;">
        ${stepIcon(s.status)}
        <span style="flex:1;">
          <strong>${escHtml(WIFI_STAGE_LABELS[s.stage] || s.stage)}</strong>
          <span style="opacity:.85;"> — ${escHtml(s.message)}</span>
        </span>
        <span style="opacity:.6; font-variant-numeric:tabular-nums;">${Number(s.t || 0).toFixed(0)}s</span>
      </li>`).join('');
    const scanFailed = j && j.result === 'failed' && j.stage === 'scan';
    const visible = scanFailed && j.visible && j.visible.length
      ? `<div class="dialog-message" style="margin-top:6px;">Mạng camera nhìn thấy: ${escHtml(j.visible.join(', '))}</div>` : '';
    const resultHtml = res ? `
      <div style="border-left:3px solid ${res.color}; padding:6px 10px; margin-top:8px;">
        <div style="display:flex; gap:6px; align-items:center; color:${res.color}; font-weight:600;">
          ${svgIcon(res.icon, 'icon-sm')} ${escHtml(res.title)}
        </div>
        <div style="margin-top:4px;">${escHtml(j.message || '')}</div>
        ${j.advice ? `<div class="dialog-message" style="margin-top:4px;">${escHtml(j.advice)}</div>` : ''}
        ${visible}
        ${j.state === 'watching' ? '<div class="dialog-message" style="margin-top:4px;">Hub vẫn đang theo dõi — nếu camera quay lại, kết quả sẽ tự cập nhật.</div>' : ''}
      </div>` : '';
    const elapsed = j ? `${Math.round(j.elapsed || 0)}s` : '';
    root.innerHTML = `
      <div class="dialog-box" style="max-width:560px;">
        <div class="dialog-title">Đổi Wi-Fi — ${escHtml(cameraName)} → ${escHtml((j && j.ssid) || '')}</div>
        <ul style="list-style:none; margin:0; padding:0;">${stepsHtml || '<li>Đang khởi tạo...</li>'}</ul>
        ${resultHtml}
        <div class="dialog-actions">
          <span style="flex:1; opacity:.6; align-self:center;">${done ? '' : `Đang chạy · ${elapsed}`}</span>
          ${scanFailed && onForce ? '<button type="button" class="btn" id="dlg-wifi-force">Vẫn gửi</button>' : ''}
          ${done && j.result !== 'success' && onRetry ? '<button type="button" class="btn" id="dlg-wifi-retry">Thử lại</button>' : ''}
          <button type="button" class="btn ${done ? 'primary' : ''}" id="dlg-cancel">${done ? 'Đóng' : 'Ẩn (Hub vẫn chạy)'}</button>
        </div>
      </div>`;
    root.querySelector('#dlg-cancel').addEventListener('click', close);
    const f = root.querySelector('#dlg-wifi-force');
    if (f) f.addEventListener('click', () => { close(); onForce(); });
    const r = root.querySelector('#dlg-wifi-retry');
    if (r) r.addEventListener('click', () => { close(); onRetry(); });
  };

  root.onclick = (e) => { if (e.target === root) close(); };
  root.classList.add('open');
  render(job);
  return { update: render, isOpen: () => open };
}

export function registerModalEsc(isOpenFn, closeFn) {
  escCloseCallbacks.push({ isOpenFn, closeFn });
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (dialogBackdrop && dialogBackdrop.classList.contains('open')) {
    const cancelBtn = dialogBackdrop.querySelector('#dlg-cancel');
    if (cancelBtn) cancelBtn.click();
    return;
  }
  for (let i = escCloseCallbacks.length - 1; i >= 0; i--) {
    const item = escCloseCallbacks[i];
    if (item.isOpenFn()) {
      item.closeFn();
      return;
    }
  }
});
