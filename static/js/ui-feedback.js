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
