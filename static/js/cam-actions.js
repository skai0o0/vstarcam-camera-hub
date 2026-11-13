/**
 * Shared camera lifecycle & control actions (used by grid cards, PTZ workbench, and room manager).
 */
import { apiJson, getCameraByUid } from './api.js';
import { confirmDialog, promptDialog, showToast, wifiCredentialsDialog } from './ui-feedback.js';

export async function triggerMotionManual(uid, action, onDone) {
  if (!uid) return;
  const { res, data } = await apiJson(`/api/camera/${uid}/record`, {
    method: 'POST',
    body: { action },
  });
  if (!res.ok || data.ok === false) {
    showToast(data.error || 'Không gửi được lệnh ghi hình', 'error');
  } else if (action === 'trigger') {
    showToast('Đã kích hoạt ghi nhanh Pre+Post', 'ok');
  } else if (action === 'manual_on') {
    showToast('Đã bật ghi thủ công', 'ok');
  } else if (action === 'manual_off') {
    showToast('Đã dừng ghi thủ công & lưu clip', 'info');
  }
  if (onDone) onDone();
}

export async function toggleManualRecord(uid, onDone) {
  const cam = getCameraByUid(uid);
  if (!cam) return;
  const action = cam.manual_record ? 'manual_off' : 'manual_on';
  await triggerMotionManual(uid, action, onDone);
}

export async function reconnectCamera(uid, onDone, stageNotify) {
  if (!uid) return;
  const notify = stageNotify || ((msg, ok) => showToast(msg, ok ? 'ok' : 'error'));
  notify('Đang thử đục lỗ WAN P2P / chọn Relay RTT thấp nhất...', true);
  try {
    const { data } = await apiJson(`/api/camera/${uid}/reconnect`, { method: 'POST' });
    notify(data.ok ? data.message : (data.error || 'Tối ưu thất bại'), !!data.ok);
    if (onDone) onDone();
  } catch (err) {
    notify(`Lỗi kết nối: ${err.message}`, false);
  }
}

export async function rebootCamera(uid, name, onDone, stageNotify) {
  const ok = await confirmDialog({
    title: 'Khởi động lại camera',
    message: `Gửi lệnh khởi động lại từ xa tới "${name}" (${(uid || '').substring(0, 8)})? Luồng video sẽ tạm gián đoạn vài chục giây.`,
    confirmText: 'Khởi động lại',
    danger: true,
  });
  if (!ok) return;
  const notify = stageNotify || ((msg, isOk) => showToast(msg, isOk ? 'ok' : 'error'));
  notify('Đang gửi lệnh khởi động lại...', true);
  try {
    const { data } = await apiJson(`/api/camera/${uid}/reboot`, { method: 'POST' });
    notify(data.ok ? data.message : (data.error || 'Không gửi được lệnh reboot'), !!data.ok);
    if (onDone) onDone();
  } catch (err) {
    notify(`Lỗi kết nối: ${err.message}`, false);
  }
}

export async function changeCameraPassword(uid, name, onDone, stageNotify) {
  const newPwd = await promptDialog({
    title: 'Đổi mật khẩu camera',
    message: `Nhập mật khẩu mới áp dụng cho tài khoản quản trị trên "${name}":`,
    label: 'Mật khẩu mới',
    placeholder: 'Nhập mật khẩu mới...',
    password: true,
    confirmText: 'Cập nhật mật khẩu',
  });
  if (!newPwd || !newPwd.trim()) return;
  const notify = stageNotify || ((msg, isOk) => showToast(msg, isOk ? 'ok' : 'error'));
  notify('Đang cập nhật mật khẩu xuống camera...', true);
  try {
    const { data } = await apiJson(`/api/camera/${uid}/password`, {
      method: 'POST',
      body: { password: newPwd.trim() },
    });
    notify(data.ok ? data.message : (data.error || 'Đổi mật khẩu thất bại'), !!data.ok);
    if (onDone) onDone();
  } catch (err) {
    notify(`Lỗi kết nối: ${err.message}`, false);
  }
}

export async function pushWifiToCamera(uid, name) {
  const ssidEl = document.getElementById('wifi-ssid-input');
  const pwdEl = document.getElementById('wifi-pwd-input');
  const creds = await wifiCredentialsDialog({
    cameraName: name,
    defaultSsid: ssidEl ? ssidEl.value : '',
    defaultPwd: pwdEl ? pwdEl.value : '',
  });
  if (!creds) return;
  try {
    const { data } = await apiJson('/api/camera/provision-wifi', {
      method: 'POST',
      body: { ssid: creds.ssid, password: creds.password, uid },
    });
    showToast(data.ok ? data.message : (data.error || 'Không thể nạp Wi-Fi'), data.ok ? 'ok' : 'error', 5000);
  } catch (err) {
    showToast(`Lỗi nạp Wi-Fi: ${err.message}`, 'error');
  }
}

export async function deleteCamera(uid, name, onDone) {
  const ok = await confirmDialog({
    title: 'Xóa camera khỏi Hub',
    message: `Bạn có chắc muốn gỡ "${name}" khỏi danh sách quản lý của Hub?`,
    confirmText: 'Xóa camera',
    danger: true,
  });
  if (!ok) return;
  await apiJson(`/api/camera/${uid}`, { method: 'DELETE' });
  const renameList = document.getElementById('room-Rename-list');
  if (renameList) renameList.dataset.count = '';
  showToast(`Đã xóa camera "${name}"`, 'info');
  if (onDone) onDone();
}
