/**
 * Reverse-Engineered 27-CGI Camera Setting Inspector Modal:
 * - Left category rail + sticky filter toolbar + JSON export
 * - Interactive setting rows (direct write via /api/camera/{uid}/ptz)
 * - Lazy table render for Raw 27 CGI sections
 */
import { apiJson, getCameraByUid } from './api.js';
import { escHtml, svgIcon } from './icons.js';
import { getCamUiState, PTZ_STATE_PATCH } from './ptz-modal.js';
import { registerModalEsc, showToast } from './ui-feedback.js';

let settingsUid = null;
let settingsPayload = null;
let settingsCat = 'all';

const SETTINGS_CATEGORIES = [
  ['all', 'Tất cả nhóm', 'grid'],
  ['caps', 'Năng lực & FW', 'cpu'],
  ['system', 'Hệ thống & User', 'sliders'],
  ['image', 'Hình ảnh & Âm thanh', 'camera'],
  ['ptz', 'PTZ & Đèn LED', 'compass'],
  ['ai', 'AI & Chuyển động (21xx)', 'radar'],
  ['record', 'Video Stream & Thẻ SD', 'film'],
  ['schedule', 'Báo động & Lịch (2017)', 'alert'],
  ['network', 'Mạng, Wi-Fi & RTSP/ONVIF', 'wifi'],
  ['cloud', 'Cloud Factory & Dịch vụ', 'cloud'],
  ['raw', 'Raw 27 CGI Inspector', 'terminal'],
];

const SET_MAPS = {
  bool01: { '0': 'Tắt (0)', '1': 'Bật (1)' },
  ircut: { '0': 'Tắt IR / Ban ngày (0)', '1': 'Tự động (1)', '2': 'Bật IR / Ban đêm (2)' },
  flip: { '0': 'Chuẩn (0)', '1': 'Lật dọc (1)', '2': 'Gương ngang (2)', '3': 'Lật 180° (3)' },
  hz: { '0': '50Hz (VN/EU)', '1': '60Hz (US/JP)' },
  hsens: { '0': 'Tắt (0)', '1': 'Thấp (1)', '2': 'Trung bình (2)', '3': 'Cao (3)' },
  vfmt: { '0': 'H.264 (AVC)', '1': 'H.265 (HEVC)' },
  led: { '0': 'Tắt đèn LED (0)', '1': 'Bật đèn theo trạng thái (1)', '2': 'Chế độ LED 2' },
  wifiAuth: { '0': 'Open/None', '1': 'WEP', '2': 'WPA-PSK TKIP', '3': 'WPA-PSK AES', '4': 'WPA2-PSK AES', '5': 'WPA2-PSK TKIP' },
  ratemode: { '0': 'VBR (Biến thiên)', '1': 'CBR (Cố định)' },
};

function setSecVals(cgiSubstr) {
  if (!settingsPayload || !settingsPayload.sections) return {};
  const sec = settingsPayload.sections.find((s) => s.cgi === cgiSubstr || s.cgi.includes(cgiSubstr));
  return (sec && sec.values && sec.values.cgi !== 'not support') ? sec.values : {};
}

function fmtEpoch(sec) {
  const n = Number(sec);
  if (!n || isNaN(n)) return '';
  try { return new Date(n * 1000).toLocaleString('vi-VN'); } catch (_) { return ''; }
}

function fmtTz(tzSec) {
  const n = Number(tzSec);
  if (tzSec === undefined || tzSec === '' || isNaN(n)) return '';
  const offsetH = -n / 3600;
  const sign = offsetH >= 0 ? '+' : '';
  return `UTC${sign}${Number.isInteger(offsetH) ? offsetH : offsetH.toFixed(1)} (POSIX tz=${n}s)`;
}

function summarizePlan21(vals, prefix) {
  const slots = [];
  for (let i = 1; i <= 21; i++) {
    const v = vals[prefix + i];
    if (v !== undefined) slots.push([i, String(v)]);
  }
  if (!slots.length) return null;
  const active = slots.filter(([, v]) => v !== '0' && v !== '-1');
  return { total: slots.length, active, rawPreview: slots.map(([i, v]) => `P${i}:${v}`).join(' ') };
}

function summarizeWeeklySched(vals, prefix) {
  const days = [['sun', 'CN'], ['mon', 'T2'], ['tue', 'T3'], ['wed', 'T4'], ['thu', 'T5'], ['fri', 'T6'], ['sat', 'T7']];
  const parts = [];
  let active = 0;
  for (const [d, lbl] of days) {
    const segs = [0, 1, 2].map((k) => vals[`${prefix}_${d}_${k}`]).filter((v) => v !== undefined);
    if (!segs.length) continue;
    active += segs.filter((v) => v !== '-1' && v !== '0').length;
    parts.push(`${lbl}:[${segs.join(',')}]`);
  }
  return parts.length ? { active, text: parts.join(' · ') } : null;
}

function mkBtnGroup(action, cur, choices, hint) {
  return `<div class="set-ctrl">${choices.map(([val, lbl, matchVal]) => {
    const cmpVal = matchVal !== undefined ? matchVal : val;
    const isAct = cur !== undefined && String(cur) === String(cmpVal);
    const act = typeof val === 'string' && val.startsWith('act:') ? val.slice(4) : action;
    const payload = typeof val === 'string' && val.startsWith('act:') ? '' : String(val);
    return `<button type="button" class="btn sm ${isAct ? 'active' : ''}" data-set-act="${escHtml(act)}" data-set-val="${escHtml(payload)}" data-set-hint="${escHtml(hint || '')}">${escHtml(lbl)}</button>`;
  }).join('')}</div>`;
}

function mkNumCtrl(id, action, cur, min, max, hint) {
  const safeVal = (cur !== undefined && cur !== '') ? cur : min;
  return `<div class="set-ctrl">
    <input type="number" id="${escHtml(id)}" value="${escHtml(safeVal)}" min="${min}" max="${max}">
    <button type="button" class="btn sm primary" data-set-num-act="${escHtml(action)}" data-set-input="${escHtml(id)}" data-set-hint="${escHtml(hint || '')}">Áp dụng</button>
  </div>`;
}

function renderSettingRow(name, keyLabel, val, desc, ctrlHtml, filter) {
  const hay = `${name} ${keyLabel} ${val ?? ''} ${desc || ''}`.toLowerCase();
  if (filter && !hay.includes(filter)) return '';
  const shown = (val === undefined || val === null)
    ? '<span style="color:var(--text-muted);">(không có)</span>'
    : escHtml(val === '' ? '""' : val);
  return `<tr class="set-row">
    <td style="width:36%;"><div class="set-name">${escHtml(name)}</div><div class="set-key">${escHtml(keyLabel)}</div></td>
    <td><div class="set-raw">${shown}</div>${desc ? `<div class="set-desc">${escHtml(desc)}</div>` : ''}</td>
    <td style="width:28%; text-align:right;">${ctrlHtml || ''}</td>
  </tr>`;
}

function renderMotionAreaGrid(v2123) {
  const rows = [];
  let activeCells = 0;
  for (let r = 0; r < 18; r++) {
    const mask = Number(v2123[`md_reign${r}`]);
    if (isNaN(mask)) return '';
    rows.push(mask);
  }
  let cells = '';
  for (let r = 0; r < 18; r++) {
    const m = rows[r];
    for (let c = 0; c < 22; c++) {
      const on = ((m >>> c) & 1) === 1;
      if (on) activeCells++;
      cells += `<div class="md-cell ${on ? 'on' : ''}"></div>`;
    }
  }
  return `<div style="padding:12px; display:flex; gap:14px; flex-wrap:wrap; align-items:center; border-top:1px solid var(--border-subtle);">
    <div class="md-grid" title="Lưới 18 hàng × 22 cột vùng phát hiện chuyển động">${cells}</div>
    <div style="font-size:12px; line-height:1.55;">
      <div><b>Lưới vùng phát hiện chuyển động 18×22 (<code>cmd=2123</code>)</b></div>
      <div style="color:var(--text-muted);">Đang kích hoạt: <b style="color:var(--success-text);">${activeCells} / 396 ô</b></div>
      <div style="color:var(--text-muted); font-size:11.5px;">Mỗi <code>md_reign0..17</code> là bitmask 22-bit.</div>
    </div>
  </div>`;
}

async function applyCamSetting(action, extra, sectionHint) {
  if (!settingsUid) return;
  const st = document.getElementById('settings-status');
  if (st) st.innerHTML = `<span style="color:var(--accent-text);">Đang ghi lệnh <code>${escHtml(action)}</code> xuống camera...</span>`;
  try {
    const { data: d } = await apiJson(`/api/camera/${settingsUid}/ptz`, {
      method: 'POST',
      body: { action, ...(extra || {}) },
    });
    if (!d.ok) {
      if (st) st.innerHTML = `<span style="color:var(--danger-text);">${escHtml(d.error || 'Camera từ chối lệnh')}</span>`;
      showToast(d.error || 'Camera từ chối lệnh', 'error');
      return;
    }
    if (settingsPayload && settingsPayload.sections) {
      const patch = {};
      if (d.values) {
        for (const [k, v] of Object.entries(d.values)) {
          if (!['result', 'cmd', 'command', 'uid'].includes(k)) patch[k] = String(v);
        }
      }
      const fn = PTZ_STATE_PATCH[action];
      if (fn) Object.assign(patch, fn(extra || {}));
      if (action === 'cry_detect') patch.enable = String(extra.value);
      if (action === 'ai_2137') patch.enable = String(extra.value);
      if (action === 'depart_detect_time') patch.detectTime = String(extra.value);
      if (action === 'rtsp_tls') patch.tls_enable = String(extra.value);
      if (action === 'onvif_enable') patch.onvifenable = String(extra.value);
      if (action === 'rtsp_enable') patch.rtspenable = String(extra.value);
      if (action === 'hue') patch.vhue = String(extra.value);
      if (action === 'ptz_center_onstart') patch.ptz_center_onstart = String(extra.value);
      if (action === 'ptz_speed') {
        const sp = String(extra.value);
        Object.assign(patch, { speed: sp, ptz_patrol_up_rate: sp, ptz_patrol_down_rate: sp, ptz_patrol_left_rate: sp, ptz_patrol_right_rate: sp });
      }
      for (const sec of settingsPayload.sections) {
        if (!sec.values) continue;
        for (const [k, v] of Object.entries(patch)) {
          if (k === 'enable') {
            if (sectionHint && sec.cgi.includes(sectionHint)) sec.values[k] = v;
          } else if (k in sec.values || (sectionHint && sec.cgi.includes(sectionHint))) {
            sec.values[k] = v;
          }
        }
      }
      if (document.getElementById('focus-modal')?.classList.contains('open')) {
        Object.assign(getCamUiState(), patch);
      }
      renderSettingsCatalog();
    }
    if (st) st.innerHTML = `<span style="color:var(--success-text); font-weight:600;">Đã lưu <code>${escHtml(action)}</code> xuống camera (${escHtml(d.connection_mode || 'P2P')})</span>`;
    showToast(`Đã áp dụng ${action}`, 'ok');
  } catch (err) {
    if (st) st.innerHTML = `<span style="color:var(--danger-text);">Lỗi mạng: ${escHtml(err.message)}</span>`;
  }
}

function renderSettingsRail() {
  const rail = document.getElementById('settings-cat-rail');
  if (!rail) return;
  rail.style.display = settingsPayload ? 'flex' : 'none';
  // With the rail hidden the grid must collapse to one column, or the body lands in the 230px rail column
  rail.parentElement?.classList.toggle('no-rail', !settingsPayload);
  if (!settingsPayload) return;
  rail.innerHTML = SETTINGS_CATEGORIES.map(([id, label, icon]) =>
    `<button type="button" class="set-cat-btn ${settingsCat === id ? 'active' : ''}" data-set-cat="${id}">${svgIcon(icon, 'icon-sm')}<span>${escHtml(label)}</span></button>`
  ).join('');
}

function renderRawSectionInner(sec, filter) {
  const vals = sec.values;
  if (vals === null) {
    return '<div style="padding:10px 12px; color:var(--warning-text); font-size:12px;">Camera chưa kịp phản hồi lệnh này — bấm đọc lại.</div>';
  }
  const keys = Object.keys(vals).sort().filter((k) => {
    if (!filter) return true;
    return `${sec.cgi} ${sec.title} ${k} ${vals[k]}`.toLowerCase().includes(filter);
  });
  if (!keys.length) return '<div style="padding:10px 12px; color:var(--text-muted); font-size:12px;">Không có key dữ liệu.</div>';
  return `<table class="set-table">${keys.map((k) => `<tr class="set-row"><td style="width:40%; font-family:var(--font-mono); color:var(--text-muted);">${escHtml(k)}</td><td class="set-raw">${escHtml(vals[k] === '' ? '""' : vals[k])}</td></tr>`).join('')}</table>`;
}

export function renderSettingsCatalog() {
  const box = document.getElementById('settings-content');
  if (!box || !settingsPayload) return;
  const filter = (document.getElementById('settings-filter')?.value || '').trim().toLowerCase();
  const showCat = (id) => settingsCat === 'all' || settingsCat === id;

  const st = setSecVals('get_status.cgi');
  const cp = setSecVals('get_camera_params.cgi');
  const mc = setSecVals('get_misc.cgi');
  const rc = setSecVals('get_record.cgi');
  const pm = setSecVals('get_params.cgi');
  const rt = setSecVals('get_rtsp.cgi');
  const ov = setSecVals('get_onvif.cgi');
  const fc = setSecVals('get_factory_param.cgi');
  const lg = setSecVals('get_login.cgi');
  const cu = setSecVals('check_user.cgi');
  const ws = setSecVals('get_wifi_scan_result.cgi');
  const rf = setSecVals('get_record_file.cgi');
  const gl = setSecVals('get_log.cgi');
  const c2105 = setSecVals('cmd=2105');
  const c2112 = setSecVals('cmd=2112');
  const c2122 = setSecVals('cmd=2122');
  const c2123 = setSecVals('cmd=2123');
  const c2126 = setSecVals('cmd=2126');
  const c2127 = setSecVals('cmd=2127');
  const c2136 = setSecVals('cmd=2136');
  const c2137 = setSecVals('cmd=2137');
  const c2016 = setSecVals('cmd=2016');

  const blocks = [];

  if (showCat('caps')) {
    const capDefs = [
      ['haveMotor', 'Động cơ xoay PTZ'], ['haveMic', 'Micro thu âm'], ['haveHorn', 'Loa đàm thoại'],
      ['haveWifi', 'Module Wi-Fi'], ['haveTf', 'Khe thẻ nhớ (haveTf)'], ['support_AI', 'Nền tảng AI'],
      ['support_humanDetect', 'Phát hiện người'], ['support_humanoidFrame', 'Khung người (2126)'],
      ['support_motionArea', 'Vùng chuyển động 18×22 (2123)'], ['support_cryDetect', 'Tiếng khóc (2112)'],
      ['CryDetectEnable', 'CryDetectEnable'], ['support_departDetect', 'Rời vị trí (2136)'],
      ['support_smokeDetect', 'Cảnh báo khói (2137)'], ['support_voiceTypedef', 'Âm báo tùy chỉnh'],
      ['support_pixel_shift', 'Đổi độ phân giải'], ['support_h264_h265_shift', 'H.264/H.265 (2105)'],
      ['support_rtspTls', 'RTSP qua TLS (2122)'], ['support_vuid', 'VUID mới'],
      ['wifi_change_realtime', 'Đổi Wi-Fi tức thời'], ['hardwareTestFunc', 'Hardware Test'],
      ['onvif_support_enable', 'Hỗ trợ ONVIF'], ['DualAuthentication', 'Xác thực 2 lớp'],
    ];
    const pills = capDefs.map(([k, lbl]) => {
      const v = st[k] !== undefined ? st[k] : pm[k];
      if (filter && !`${k} ${lbl} ${v ?? ''}`.toLowerCase().includes(filter)) return '';
      const on = v === '1';
      const missing = v === undefined;
      return `<span class="badge ${on ? 'ok' : ''}" style="${missing ? 'opacity:0.35;' : (on ? '' : 'opacity:0.6;')}" title="${escHtml(k)} = ${escHtml(v ?? 'không khai báo')}">${escHtml(lbl)}: <b>${missing ? '—' : escHtml(v)}</b></span>`;
    }).filter(Boolean).join('');
    if (pills) {
      blocks.push(`<details open class="set-card"><summary><span>Năng lực phần cứng &amp; Cờ Firmware (<code>get_status.cgi</code>)</span><span class="badge">${st.sys_ver ? 'FW ' + escHtml(st.sys_ver) : ''}</span></summary><div class="cap-grid">${pills}</div></details>`);
    }
  }

  const wrapCard = (catId, title, badgeTxt, rowsArr, extraHtml = '') => {
    const html = rowsArr.filter(Boolean).join('');
    if (!html && !extraHtml) return;
    blocks.push(`<details open class="set-card" data-cat="${catId}">
      <summary><span>${title}</span><span class="badge">${escHtml(badgeTxt)}</span></summary>
      ${html ? `<table class="set-table">${html}</table>` : ''}${extraHtml}
    </details>`);
  };

  if (showCat('system')) {
    wrapCard('system', 'Hệ thống, Phiên bản & Người dùng', `${st.deviceid || ''}`, [
      renderSettingRow('Mã thiết bị (UID/DID)', 'get_status.cgi → deviceid', st.deviceid, 'Định danh duy nhất trên tem QR / PPPP', '', filter),
      renderSettingRow('Tên hiển thị OSD (Alias)', 'get_status.cgi → alias', st.alias, 'Đổi qua tab Cài đặt & Quản lý Phòng hoặc set_alias.cgi', '', filter),
      renderSettingRow('Firmware hệ thống', 'get_status.cgi → sys_ver / app_version', `${st.sys_ver || '?'} · app: ${st.app_version || '?'}`, 'Hai nhánh firmware phổ biến: .12 và .19', '', filter),
      renderSettingRow('Phần cứng / OEM / Độ phân giải gốc', 'get_status.cgi → oem_id / cameraMode / pixel', `OEM=${st.oem_id || ''}, camera_type=${st.camera_type || ''}, devicetype=${st.devicetype || ''}, mode=${st.cameraMode || ''}, pixel=${st.pixel || ''}`, '', '', filter),
      renderSettingRow('Đồng hồ hiện tại trên camera', 'get_status.cgi → now', st.now ? `${st.now} (${fmtEpoch(st.now)})` : undefined, '', '', filter),
      renderSettingRow('Trạng thái Reset / Encrypt / Under', 'get_status.cgi → ResetResult / encrypt / under', `ResetResult=${st.ResetResult}, encrypt=${st.encrypt}, under=${st.under}, params_md5=${st.params_md5 || ''}`, '', '', filter),
      renderSettingRow('Tài khoản kết nối & Quyền', 'get_login.cgi → loginuser / pri', `${lg.loginuser || ''} (pri=${lg.pri || ''}, loginpass=${lg.loginpass || '***'})`, 'pri=255 là quyền Admin cao nhất', '', filter),
      renderSettingRow('Phiên P2P đồng thời', 'check_user.cgi → current_users / max_support_users', cu.current_users !== undefined ? `${cu.current_users} / ${cu.max_support_users} phiên` : undefined, 'Giới hạn client PPPP cùng lúc (tối đa 4)', '', filter),
      renderSettingRow('Danh sách user trên camera', 'get_params.cgi → user1..user3', `user1=${pm.user1_name || '""'}, user2=${pm.user2_name || '""'}, user3=${pm.user3_name || ''} (${pm.user3_pwd || '***'})`, '', '', filter),
      renderSettingRow('Nhật ký hệ thống (get_log)', 'get_log.cgi → log_text', gl.log_text === '' ? '(trống)' : gl.log_text, '', '', filter),
    ]);
  }

  if (showCat('image')) {
    const osdCur = st.osdenable !== undefined ? st.osdenable : cp.OSDEnable;
    wrapCard('image', 'Hình ảnh, Cảm biến ISP, Hồng ngoại & Âm thanh', 'IR / Flip / OSD / Mic', [
      renderSettingRow('Hồng ngoại / Ngày Đêm (IR-Cut)', 'camera_control.cgi?param=14 → ircut', SET_MAPS.ircut[cp.ircut] || cp.ircut, `ircut_forceon=${st.ircut_forceon ?? '?'} · Ghi trực tiếp param=14`, mkBtnGroup('ir', cp.ircut, [['act:ir_off', 'Tắt (0)', '0'], ['act:ir_auto', 'Tự động (1)', '1'], ['act:ir_on', 'Bật đêm (2)', '2']], 'camera_params'), filter),
      renderSettingRow('Lật / Xoay khung hình (Flip/Mirror)', 'camera_control.cgi?param=5 → flip', SET_MAPS.flip[cp.flip] || cp.flip, 'Ghi trực tiếp param=5', mkBtnGroup('flip', cp.flip, [['act:flip_normal', 'Chuẩn (0)', '0'], ['act:flip_v', 'Dọc (1)', '1'], ['act:flip_h', 'Gương (2)', '2'], ['act:flip_vh', '180° (3)', '3']], 'camera_params'), filter),
      renderSettingRow('Chữ đóng dấu ngày giờ (OSD)', 'set_misc.cgi?osdenable=0/1 → osdenable / OSDEnable', SET_MAPS.bool01[osdCur] || osdCur, 'Firmware .19 yêu cầu set_misc.cgi', mkBtnGroup('osd', osdCur, [['act:osd_off', 'Ẩn (0)', '0'], ['act:osd_on', 'Hiện (1)', '1']], 'camera_params'), filter),
      renderSettingRow('Tần số chống nhấp nháy đèn', 'camera_control.cgi?param=3 → mode', SET_MAPS.hz[cp.mode] || cp.mode, 'Việt Nam dùng 50Hz', mkBtnGroup('hz', cp.mode, [['act:hz_50', '50Hz (0)', '0'], ['act:hz_60', '60Hz (1)', '1']], 'camera_params'), filter),
      renderSettingRow('Độ sáng (Brightness)', 'camera_control.cgi?param=1 → vbright', cp.vbright, 'Dải 0 – 255 (mặc định ~126–128)', mkNumCtrl('set-in-vbright', 'brightness', cp.vbright, 0, 255, 'camera_params'), filter),
      renderSettingRow('Độ tương phản (Contrast)', 'camera_control.cgi?param=2 → vcontrast', cp.vcontrast, 'Dải 0 – 255', mkNumCtrl('set-in-vcontrast', 'contrast', cp.vcontrast, 0, 255, 'camera_params'), filter),
      renderSettingRow('Độ bão hòa màu (Saturation)', 'camera_control.cgi?param=8 → vsaturation', cp.vsaturation, 'Dải 0 – 255', mkNumCtrl('set-in-vsat', 'saturation', cp.vsaturation, 0, 255, 'camera_params'), filter),
      renderSettingRow('Tông màu (Hue)', 'camera_control.cgi?param=9 → vhue', cp.vhue, 'Dải 0 – 255', mkNumCtrl('set-in-vhue', 'hue', cp.vhue, 0, 255, 'camera_params'), filter),
      renderSettingRow('Âm lượng Micro thu (involume)', 'camera_control.cgi?param=24 → involume', cp.involume, 'Dải 0 – 31 (áp dụng RTSP & MP4)', mkNumCtrl('set-in-mic', 'mic_volume', cp.involume, 0, 31, 'camera_params'), filter),
      renderSettingRow('Âm lượng Loa phát (outvolume)', 'camera_control.cgi?param=25 → outvolume', cp.outvolume, 'Dải 0 – 31', mkNumCtrl('set-in-spk', 'speaker_volume', cp.outvolume, 0, 31, 'camera_params'), filter),
      renderSettingRow('Mã cảm biến & Tỉ lệ zoom (2016)', 'get_camera_params.cgi → cameratype / cmd=2016 → ratio', `cameratype=${cp.cameratype ?? '?'}, ratio=${c2016.ratio ?? '?'}`, '', '', filter),
    ]);
  }

  if (showCat('ptz')) {
    wrapCard('ptz', 'Động cơ PTZ, Giới hạn mềm & Đèn LED', `Tốc độ ${mc.ptz_patrol_rate || '?'}`, [
      renderSettingRow('Tốc độ xoay PTZ tổng & 4 hướng', 'get_misc.cgi → ptz_patrol_*_rate & speed', `chung=${mc.ptz_patrol_rate ?? '?'} (up=${mc.ptz_patrol_up_rate}, down=${mc.ptz_patrol_down_rate}, left=${mc.ptz_patrol_left_rate}, right=${mc.ptz_patrol_right_rate}), cp.speed=${cp.speed ?? '?'}`, '1 = chậm nhất · 10 = nhanh nhất', mkNumCtrl('set-in-ptzspd', 'ptz_speed', mc.ptz_patrol_rate, 1, 10, 'get_misc'), filter),
      renderSettingRow('Tự xoay về tâm khi bật nguồn', 'set_misc.cgi → ptz_center_onstart', SET_MAPS.bool01[mc.ptz_center_onstart] || mc.ptz_center_onstart, 'Ghi & đọc lại trên firmware', mkBtnGroup('ptz_center_onstart', mc.ptz_center_onstart, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'get_misc'), filter),
      renderSettingRow('Đèn LED báo trạng thái trên thân', 'set_misc.cgi → led_mode', SET_MAPS.led[mc.led_mode] || mc.led_mode, '0 = Tắt LED · 1 = Bật LED', mkBtnGroup('led_mode', mc.led_mode, [['0', 'Tắt (0)'], ['1', 'Bật (1)'], ['2', 'Mức 2']], 'get_misc'), filter),
      renderSettingRow('Giới hạn mềm PTZ (Soft Limit)', 'get_misc.cgi → ptz_soft_limit_*', `ngang_max=${mc.ptz_soft_limit_max_level ?? '?'} (stop=${mc.ptz_soft_limit_stop_percent_level ?? '?'}%), dọc_max=${mc.ptz_soft_limit_max_vert ?? '?'} (stop=${mc.ptz_soft_limit_stop_percent_vert ?? '?'}%)`, 'Tổng số bước trục pan/tilt (chỉ đọc)', '', filter),
      renderSettingRow('Tham số tuần tra khác', 'get_misc.cgi → preset_onstart / ptruntimes / ptz_disppreset / device_type', `preset_onstart=${mc.preset_onstart}, ptruntimes=${mc.ptruntimes}, ptz_disppreset=${mc.ptz_disppreset}, device_type=${mc.device_type}`, 'Chỉ đọc trên firmware', '', filter),
    ]);
  }

  if (showCat('ai')) {
    const has2112 = c2112.enable !== undefined;
    const has2136 = c2136.detectTime !== undefined;
    const has2137 = c2137.enable !== undefined;
    const hasHf = c2126.bHumanoidFrame !== undefined;
    const mdHtml = c2123.md_reign0 !== undefined && (!filter || 'md_reign 2123 luoi vung chuyen dong'.includes(filter)) ? renderMotionAreaGrid(c2123) : '';
    wrapCard('ai', 'AI & Phát hiện Thông minh (trans_cmd_string 21xx)', 'cmd=2112..2137', [
      renderSettingRow('Tự động xoay bám theo người', 'cmd=2127 → enable / track_status', `${SET_MAPS.bool01[c2127.enable] || c2127.enable} (track_status=${c2127.track_status ?? '?'})`, 'track_status=1 khi đang khóa mục tiêu', mkBtnGroup('track', c2127.enable, [['act:track_off', 'Tắt (0)', '0'], ['act:track_on', 'Bật (1)', '1']], 'cmd=2127'), filter),
      renderSettingRow('Độ nhạy phát hiện hình người', 'cmd=2126 → sensitive', SET_MAPS.hsens[c2126.sensitive] || c2126.sensitive, 'Dải 0=Tắt · 1=Thấp · 2=TB · 3=Cao', mkBtnGroup('human_sensitive', c2126.sensitive, [['0', 'Tắt (0)'], ['1', 'Thấp (1)'], ['2', 'TB (2)'], ['3', 'Cao (3)']], 'cmd=2126'), filter),
      renderSettingRow('Vẽ khung nhận diện người', 'cmd=2126 → bHumanoidFrame', hasHf ? SET_MAPS.bool01[c2126.bHumanoidFrame] : '(Firmware này không hỗ trợ)', 'Vẽ khung lên stream RTSP (.19)', hasHf ? mkBtnGroup('hf', c2126.bHumanoidFrame, [['act:humanframe_off', 'Tắt (0)', '0'], ['act:humanframe_on', 'Bật (1)', '1']], 'cmd=2126') : '', filter),
      renderSettingRow('AI Phát hiện tiếng khóc', 'cmd=2112 → enable', has2112 ? SET_MAPS.bool01[c2112.enable] : '(Firmware chưa bật cmd=2112)', `CryDetectEnable=${st.CryDetectEnable ?? '?'}`, has2112 ? mkBtnGroup('cry_detect', c2112.enable, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'cmd=2112') : '', filter),
      renderSettingRow('AI Thời gian rời vị trí', 'cmd=2136 → detectTime', has2136 ? `${c2136.detectTime} giây` : '(Firmware chưa bật cmd=2136)', '0 = tắt · số giây người rời khung hình', has2136 ? mkNumCtrl('set-in-depart', 'depart_detect_time', c2136.detectTime, 0, 3600, 'cmd=2136') : '', filter),
      renderSettingRow('AI Cảnh báo khói / mở rộng', 'cmd=2137 → enable', has2137 ? SET_MAPS.bool01[c2137.enable] : '(Firmware chưa bật cmd=2137)', `support_smokeDetect=${st.support_smokeDetect ?? '?'}`, has2137 ? mkBtnGroup('ai_2137', c2137.enable, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'cmd=2137') : '', filter),
    ], mdHtml);
  }

  if (showCat('record')) {
    const fileNote = rf.RecordCount !== undefined ? `RecordCount=${rf.RecordCount}, num0=${rf.record_num0}` : undefined;
    wrapCard('record', 'Bộ mã hóa Video (Main/Sub/Sub2) & Thẻ nhớ SD', `${cp.MainStreamWidth || '?'}×${cp.MainStreamHeight || '?'}`, [
      renderSettingRow('Chuẩn nén Video (H.264 / H.265)', 'cmd=2105 → videoFormat', SET_MAPS.vfmt[c2105.videoFormat] || c2105.videoFormat, 'Chỉ đọc để tránh treo luồng live hiện tại', '', filter),
      renderSettingRow('Độ phân giải luồng chính', 'get_camera_params.cgi → MainStreamWidth/Height & enc_size', `${cp.MainStreamWidth || '?'} × ${cp.MainStreamHeight || '?'} (resolution=${cp.resolution}, sub=${cp.resolutionsub}, sub2=${cp.resolutionsubsub})`, '', '', filter),
      renderSettingRow('FPS Luồng Chính (Encoder)', 'set_media.cgi?mainrate=0 → enc_framerate & enc_keyframe', `${rc.enc_framerate ?? '?'} FPS (keyframe=${rc.enc_keyframe ?? '?'})`, 'Đồng bộ enc_framerate + enc_keyframe xuống phần cứng', mkBtnGroup('enc_framerate', rc.enc_framerate, [['15', '15 FPS'], ['20', '20 FPS'], ['25', '25 FPS'], ['30', '30 FPS']], 'get_record'), filter),
      renderSettingRow('Tùy chỉnh FPS chính xác (1–30 FPS)', 'set_media.cgi → enc_framerate', rc.enc_framerate, 'Khuyến nghị 25 FPS khớp tần số 50Hz VN', mkNumCtrl('set-in-camfps', 'enc_framerate', rc.enc_framerate, 1, 30, 'get_record'), filter),
      renderSettingRow('Khoảng Keyframe GOP (enc_keyframe)', 'set_media.cgi → enc_keyframe', rc.enc_keyframe, 'Số frame giữa mỗi I-frame (1 – 150)', mkNumCtrl('set-in-camkf', 'enc_keyframe', rc.enc_keyframe, 1, 150, 'get_record'), filter),
      renderSettingRow('Điều tiết Bitrate (enc_ratemode)', 'set_media.cgi → enc_ratemode', SET_MAPS.ratemode[rc.enc_ratemode] || rc.enc_ratemode, `bitrate=${rc.enc_bitrate}kbps, quant=${rc.enc_quant}, main_mode=${rc.enc_main_mode}`, mkBtnGroup('enc_ratemode', rc.enc_ratemode, [['0', 'VBR (0)'], ['1', 'CBR (1)']], 'get_record'), filter),
      renderSettingRow('Encoder Luồng Phụ (Sub & Sub2)', 'get_record.cgi → sub_enc_* / sub_sub_enc_*', `Sub: size=${rc.sub_enc_size}, fps=${rc.sub_enc_framerate}, ${rc.sub_enc_bitrate}kbps | Sub2: size=${rc.sub_sub_enc_size}, fps=${rc.sub_sub_enc_framerate}, ${rc.sub_sub_enc_bitrate}kbps`, '', '', filter),
      renderSettingRow('Thẻ nhớ SD trên camera', 'get_record.cgi / get_status.cgi → sd*', `sdstatus=${st.sdstatus ?? rc.record_sd_status}, free=${rc.sdfree ?? st.sdfree} / total=${rc.sdtotal ?? st.sdtotal} MB, tf_enable=${rc.tf_enable}`, '', '', filter),
      renderSettingRow('Thiết lập ghi hình SD', 'get_record.cgi → record_*', `ghi_âm=${rc.record_audio}, ghi_đè=${rc.record_cover_enable}, hẹn_giờ=${rc.record_time_enable}, timer=${rc.record_timer}, size=${rc.record_size}`, '', '', filter),
      renderSettingRow('File ghi lưu trên SD', 'get_record_file.cgi', fileNote, '', '', filter),
    ]);
  }

  if (showCat('schedule')) {
    const pMotion = summarizePlan21(pm, 'motion_plan');
    const p2017_1 = summarizePlan21(setSecVals('type=1'), 'motion_record_plan');
    const secT2 = setSecVals('type=2');
    const secT4 = setSecVals('type=4');
    const p2017_2 = summarizePlan21(secT2, 'motion_push_plan') || summarizePlan21(secT4, 'motion_push_plan');
    const p2017_4 = summarizePlan21(secT4, 'record_plan');
    const p2017_5 = summarizePlan21(setSecVals('type=5'), 'nurse_plan');
    const p2017_7 = summarizePlan21(setSecVals('type=7'), 'depart_detect_plan');
    const wAlarm = summarizeWeeklySched(pm, 'alarm_schedule');
    const wRec = summarizeWeeklySched(rc, 'record_schedule');
    const fmtPlan = (p, enKey, src) => p ? `${src && src[enKey] !== undefined ? (src[enKey] === '1' ? 'Đang BẬT' : 'Đang tắt') + ' · ' : ''}${p.active.length}/${p.total} khung khác 0 (${p.rawPreview})` : '(Không có trên firmware này)';

    wrapCard('schedule', 'Báo động Chuyển động & Toàn bộ Lịch trình (cmd=2017)', `Độ nhạy ${pm.alarm_motion_sensitivity || '?'}`, [
      renderSettingRow('Độ nhạy phát hiện chuyển động', 'set_alarm.cgi → alarm_motion_sensitivity', pm.alarm_motion_sensitivity, `armed=${pm.alarm_motion_armed}, record=${pm.alarm_record}, snapshot=${pm.alarm_snapshot}, schedule=${pm.alarm_schedule_enable}`, mkNumCtrl('set-in-motion', 'motion_sensitivity', pm.alarm_motion_sensitivity, 1, 10, 'get_params'), filter),
      renderSettingRow('Hành động khi báo động', 'get_params.cgi → alarm_*', `status=${st.alarm_status}, mail=${pm.alarm_mail}, audio=${pm.alarm_audio}, http=${pm.alarm_http}, io_in=${pm.alarm_input_armed}, io_out=${pm.alarm_iolinkage}, preset=${pm.alarm_presetsit}, note=${pm.alarm_note}, upload_int=${pm.alarm_upload_interval}`, pm.alarm_http_url ? `URL: ${pm.alarm_http_url}` : '', '', filter),
      renderSettingRow('Lịch chuyển động lõi (21 khung)', 'get_params.cgi → motion_plan1..21', pMotion ? `${pMotion.active.length}/21 khung khác 0 (${pMotion.rawPreview})` : undefined, '', '', filter),
      renderSettingRow('Lịch báo động tuần', 'get_params.cgi → alarm_schedule_*', wAlarm ? `${wAlarm.active} ca khác -1 · ${wAlarm.text}` : undefined, '', '', filter),
      renderSettingRow('Lịch ghi hình SD tuần', 'get_record.cgi → record_schedule_*', wRec ? `${wRec.active} ca khác -1 · ${wRec.text}` : undefined, '', '', filter),
      renderSettingRow('Lịch ghi hình khi có CĐ (type=1)', 'cmd=2017 type=1 → motion_record_plan1..21', fmtPlan(p2017_1, 'motion_record_enable', setSecVals('type=1')), '', '', filter),
      renderSettingRow('Lịch đẩy thông báo CĐ (type=2/4)', 'cmd=2017 → motion_push_plan1..21', fmtPlan(p2017_2, 'motion_push_enable', secT2.motion_push_enable !== undefined ? secT2 : secT4), '', '', filter),
      renderSettingRow('Lịch ghi hình theo giờ (type=4)', 'cmd=2017 type=4 → record_plan1..21', fmtPlan(p2017_4, 'record_plan_enable', secT4), '', '', filter),
      renderSettingRow('Lịch Chế độ Chăm sóc (type=5)', 'cmd=2017 type=5 → nurse_plan1..21', fmtPlan(p2017_5, 'nurse_plan_enable', setSecVals('type=5')), '', '', filter),
      renderSettingRow('Lịch Rời vị trí (type=7)', 'cmd=2017 type=7 → depart_detect_plan1..21', fmtPlan(p2017_7, 'depart_detect_plan_enable', setSecVals('type=7')), '', '', filter),
    ]);
  }

  if (showCat('network')) {
    wrapCard('network', 'Mạng LAN, Wi-Fi, NTP, RTSP & ONVIF', `IP ${pm.ip || '?'} · RTSP :${rt.rtspport || '?'}`, [
      renderSettingRow('Địa chỉ MAC LAN & Wi-Fi', 'get_status.cgi → mac / wifimac', `LAN=${st.mac || ''}, Wi-Fi=${st.wifimac || ''}`, '', '', filter),
      renderSettingRow('IP mạng nội bộ', 'get_params.cgi → ip / mask / gateway / dns / port', `DHCP=${pm.dhcpen}, IP=${pm.ip}, Mask=${pm.mask}, GW=${pm.gateway}, DNS1=${pm.dns1}, DNS2=${pm.dns2 || '""'}, HTTP_Port=${pm.port}`, `Internet=${st.internet}, P2P=${st.p2pstatus}, UPnP=${pm.upnp_enable}`, '', filter),
      renderSettingRow('Kết nối Wi-Fi đang dùng', 'get_params.cgi → wifi_*', `enable=${pm.wifi_enable}, SSID="${pm.wifi_ssid || ''}", Auth=${SET_MAPS.wifiAuth[pm.wifi_authtype] || pm.wifi_authtype}, encrypt=${pm.wifi_encrypt}, ch=${pm.wifi_channel}, PSK=${pm.wifi_wpa_psk || '***'}`, `syswifi_mode=${st.syswifi_mode}, externwifi=${st.externwifi}`, '', filter),
      renderSettingRow('Bộ đệm quét Wi-Fi', 'get_wifi_scan_result.cgi → ap_number / ap_ssid', `ap_number=${ws.ap_number ?? '?'}, ap_ssid=${ws.ap_ssid ?? ''}`, 'Trống nếu camera chưa chạy wifi_scan.cgi', '', filter),
      renderSettingRow('Múi giờ & NTP Server', 'get_params.cgi → tz / ntp_*', `NTP=${SET_MAPS.bool01[pm.ntp_enable] || pm.ntp_enable}, Server=${pm.ntp_svr || ''}, ${fmtTz(pm.tz)}`, '', '', filter),
      renderSettingRow('RTSP Server & Cổng luồng gốc', 'get_rtsp.cgi & set_rtsp.cgi', `Bật=${SET_MAPS.bool01[rt.rtspenable] || rt.rtspenable}, Port=${rt.rtspport}, User=${rt.rtspuser || '""'}, Auth_param=${pm.rtsp_auth_enable}`, 'Ghi on/off trực tiếp qua set_rtsp.cgi', mkBtnGroup('rtsp_enable', rt.rtspenable, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'get_rtsp'), filter),
      renderSettingRow('Mã hóa RTSP qua TLS', 'cmd=2122 → tls_enable', SET_MAPS.bool01[c2122.tls_enable] || c2122.tls_enable, 'Ghi qua trans_cmd_string cmd=2122', mkBtnGroup('rtsp_tls', c2122.tls_enable, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'cmd=2122'), filter),
      renderSettingRow('Giao thức ONVIF', 'get_onvif.cgi & set_onvif.cgi', `onvifenable=${SET_MAPS.bool01[ov.onvifenable] || ov.onvifenable} (support=${pm.onvif_support_enable})`, 'Ghi qua set_onvif.cgi', mkBtnGroup('onvif_enable', ov.onvifenable, [['0', 'Tắt (0)'], ['1', 'Bật (1)']], 'get_onvif'), filter),
    ]);
  }

  if (showCat('cloud')) {
    const subDevs = [2, 3, 4, 5, 6, 7, 8, 9].filter((n) => pm[`dev${n}_host`] || pm[`dev${n}_alias`]);
    wrapCard('cloud', 'Cloud Factory Push, DDNS, Mail, FTP & Thiết bị phụ', `${fc.factory_server || 'Cloud'}`, [
      renderSettingRow('Cloud & Push Alarm gốc', 'get_factory_param.cgi → factory_*', `server=${fc.factory_server || ''}:${fc.factory_port || ''}, alarmserver=${fc.factory_alarmserver || ''}, heartbeat=${fc.factory_heatbeat || ''}s`, `factory_user=${fc.factory_user || ''}, status=${fc.factory_status}, mode=${fc.factory_mode}, index=${fc.factory_index}`, '', filter),
      renderSettingRow('Tính năng Cloud & Lệnh sản xuất', 'get_factory_param.cgi → support_*', `pigeon_push=${fc.support_pigeon_push}, cloud_storage=${fc.support_cloud_storage}, adpcm_ver=${fc.support_adpcm_version || '?'} | prod=${fc.production_order || '-'} | ship=${fc.shipment_order || '-'}`, '', '', filter),
      renderSettingRow('DDNS / PPPoE / Alarm Server phụ', 'get_params.cgi → ddns_* / pppoe_* / alarm_server', `ddns_service=${pm.ddns_service}, host=${pm.ddns_host || '""'}, pppoe=${pm.pppoe_enable}, alarm_svr=${pm.alarm_server || '""'}:${pm.alarm_port || 0}`, '', '', filter),
      renderSettingRow('Cảnh báo Email & FTP', 'get_params.cgi → mail_* / ftp_*', `mail_svr=${pm.mail_svr || '""'}:${pm.mail_port || 0} (ssl=${pm.mailssl}) | ftp_svr=${pm.ftp_svr || '""'}:${pm.ftp_port || 0}`, '', '', filter),
      renderSettingRow('Camera phụ liên kết (dev2..dev9)', 'get_params.cgi → dev2..dev9_*', subDevs.length ? subDevs.map((n) => `dev${n}=${pm[`dev${n}_host`]}`).join(', ') : 'Chưa cấu hình', '', '', filter),
    ]);
  }

  if (showCat('raw')) {
    settingsPayload.sections.forEach((sec, idx) => {
      const vals = sec.values;
      const badge = vals === null
        ? 'timeout/busy'
        : (vals.cgi === 'not support' ? 'not support' : `${Object.keys(vals).length} keys${sec.has_payload ? '' : ' (trống)'}`);
      let inner = '';
      const rawOpen = Boolean(filter || (settingsCat === 'raw' && sec.has_payload));
      if (filter) {
        const keys = vals ? Object.keys(vals).filter((k) => `${sec.cgi} ${sec.title} ${k} ${vals[k]}`.toLowerCase().includes(filter)) : [];
        if (!keys.length) return;
        inner = renderRawSectionInner(sec, filter);
      } else if (rawOpen) {
        inner = renderRawSectionInner(sec, '');
      }
      blocks.push(`<details ${rawOpen ? 'open' : ''} class="set-card raw-sec-card" data-cat="raw" data-raw-idx="${idx}">
        <summary><span>${escHtml(sec.title)} <code style="font-weight:400; font-size:11px;">${escHtml(sec.cgi)}</code></span><span class="badge">${escHtml(badge)}</span></summary>
        <div class="raw-sec-body">${inner}</div>
      </details>`);
    });
  }

  box.innerHTML = blocks.length
    ? blocks.join('')
    : `<div class="card-panel" style="color:var(--text-muted);">Không tìm thấy setting nào khớp từ khóa <code>${escHtml(filter)}</code>.</div>`;
}

export async function loadCameraSettings() {
  if (!settingsUid) return;
  const uid = settingsUid;
  const btn = document.getElementById('btn-read-cam-settings');
  const st = document.getElementById('settings-status');
  const box = document.getElementById('settings-content');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `${svgIcon('refresh', 'icon-sm icon-spin')}<span>Đang đọc 27 CGI...</span>`;
  }
  if (st) st.innerHTML = '<span style="color:var(--accent-text);">Đang truy vấn tuần tự 5 lô lệnh CGI &amp; trans_cmd_string (2016..2137)... vui lòng đợi vài giây.</span>';
  if (box) box.innerHTML = '';

  try {
    const { data: d } = await apiJson(`/api/camera/${uid}/settings?timeout=30`);
    if (uid !== settingsUid) return;
    if (!d.ok) {
      if (st) st.innerHTML = `<span style="color:var(--danger-text);">${escHtml(d.error || 'Không đọc được setting từ camera')}</span>`;
      if (box) box.innerHTML = '<div class="card-panel" style="color:var(--danger-text);">Không nhận được phản hồi hợp lệ từ camera. Hãy thử bấm đọc lại sau ít giây.</div>';
      return;
    }
    settingsPayload = d;
    document.getElementById('btn-copy-settings').disabled = false;
    const statusVals = setSecVals('get_status.cgi');
    const fwBadge = document.getElementById('settings-fw-badge');
    if (fwBadge && statusVals.sys_ver) {
      fwBadge.textContent = `FW ${statusVals.sys_ver}`;
      fwBadge.style.display = 'inline-flex';
    }
    const connBadge = document.getElementById('settings-conn-badge');
    if (connBadge) connBadge.textContent = d.connection_mode || 'P2P';
    if (st) {
      st.innerHTML = `<span style="color:${d.partial ? 'var(--warning-text)' : 'var(--success-text)'}; font-weight:600;">` +
        `${d.partial ? 'Đọc được một phần' : 'Đã đọc xong toàn bộ setting'}</span>` +
        ` · <b>${d.answered_count}/${d.total_count}</b> CGI phản hồi · <b>${d.total_keys}</b> trường · <b class="mono">${d.elapsed_ms} ms</b>` +
        ` · <code>${escHtml(d.connection_mode)}</code>` +
        (statusVals.sys_ver ? ` · FW <code>${escHtml(statusVals.sys_ver)}</code>` : '');
    }
    renderSettingsRail();
    renderSettingsCatalog();
  } catch (err) {
    if (uid === settingsUid && st) {
      st.innerHTML = `<span style="color:var(--danger-text);">Lỗi mạng: ${escHtml(err.message)}</span>`;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `${svgIcon('search', 'icon-sm')}<span>Đọc setting hiện tại của camera</span>`;
    }
  }
}

export function openSettingsModal(uid) {
  const cam = getCameraByUid(uid);
  settingsUid = uid;
  settingsPayload = null;
  settingsCat = 'all';

  document.getElementById('settings-filter').value = '';
  document.getElementById('btn-copy-settings').disabled = true;
  const catRail = document.getElementById('settings-cat-rail');
  catRail.style.display = 'none';
  catRail.parentElement?.classList.add('no-rail');
  document.getElementById('settings-fw-badge').style.display = 'none';

  const name = cam ? cam.room_name : uid;
  const did = cam ? (cam.did || cam.formatted_did || '') : '';
  document.getElementById('settings-title').textContent = `Setting Camera: ${name}${did ? ` (${did})` : ''}`;
  document.getElementById('settings-conn-badge').textContent = cam ? (cam.connection_mode || 'P2P') : 'P2P';

  const st = document.getElementById('settings-status');
  const box = document.getElementById('settings-content');
  if (cam && (cam.state !== 'CONNECTED' || cam.auth_failed)) {
    const why = cam.auth_failed ? 'Camera sai mật khẩu nên chưa mở được kênh lệnh.' : `Camera đang ở trạng thái ${cam.state}, chưa kết nối kênh lệnh PPPP.`;
    st.innerHTML = `<span style="color:var(--warning-text);">${escHtml(why)}</span>`;
    box.innerHTML = '<div class="card-panel" style="color:var(--text-muted);">Vui lòng chờ camera chuyển sang trạng thái kết nối xong hoặc kiểm tra mật khẩu rồi bấm <b>Đọc setting hiện tại của camera</b>.</div>';
  } else {
    st.innerHTML = 'Sẵn sàng truy vấn <b>27 CGI &amp; lệnh mở rộng (trans_cmd_string)</b> trên camera này.';
    box.innerHTML = `<div class="card-panel" style="line-height:1.65;">
      <div style="font-weight:700; margin-bottom:8px; font-size:14px;">Bộ Setting Camera Đã Reverse Engineering</div>
      <div style="color:var(--text-muted); font-size:12.5px;">
        Nhấn <b style="color:var(--accent-text);">Đọc setting hiện tại của camera</b> để truy vấn cấu hình trực tiếp qua kênh tin cậy PPPP (DRW CMD):
        <ul style="margin:10px 0 0 18px; display:grid; grid-template-columns:repeat(auto-fit, minmax(230px, 1fr)); gap:5px 16px;">
          <li>Năng lực phần cứng &amp; firmware (<code>have*</code>, <code>support_*</code>)</li>
          <li>Cảm biến ảnh, IR-cut, Flip, OSD, 50/60Hz, Mic/Loa</li>
          <li>PTZ Patrol 4 hướng, Soft Limits &amp; Đèn LED</li>
          <li>AI 21xx: Bám người (2127), Khung người (2126), Khóc (2112), Rời vị trí (2136), Khói (2137), Lưới 18×22 (2123)</li>
          <li>Luồng Main/Sub/Sub2, H.264/H.265 (2105), Thẻ SD</li>
          <li>Báo động &amp; 5 bảng Lịch trình (<code>cmd=2017</code>)</li>
          <li>Mạng, Wi-Fi Scan cache, NTP, RTSP, TLS (2122), ONVIF</li>
          <li>Cloud Factory Push, DDNS/Mail/FTP, Users &amp; Phiên P2P</li>
        </ul>
      </div>
    </div>`;
  }
  document.getElementById('settings-modal').classList.add('open');
}

export function closeSettingsModal() {
  settingsUid = null;
  document.getElementById('settings-modal').classList.remove('open');
}

export function initSettingsModal() {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;

  modal.addEventListener('click', (ev) => {
    if (ev.target === modal) closeSettingsModal();
  });

  document.getElementById('settings-close-btn')?.addEventListener('click', closeSettingsModal);
  document.getElementById('btn-read-cam-settings')?.addEventListener('click', loadCameraSettings);
  document.getElementById('settings-filter')?.addEventListener('input', renderSettingsCatalog);
  document.getElementById('settings-expand-btn')?.addEventListener('click', () => {
    document.querySelectorAll('#settings-content details.set-card').forEach((el) => {
      if (el.classList.contains('raw-sec-card') && !el.querySelector('table')) {
        const idx = Number(el.dataset.rawIdx);
        const sec = settingsPayload?.sections?.[idx];
        if (sec) el.querySelector('.raw-sec-body').innerHTML = renderRawSectionInner(sec, '');
      }
      el.open = true;
    });
  });
  document.getElementById('settings-collapse-btn')?.addEventListener('click', () => {
    document.querySelectorAll('#settings-content details.set-card').forEach((el) => { el.open = false; });
  });
  document.getElementById('btn-copy-settings')?.addEventListener('click', async () => {
    if (!settingsPayload) return;
    const txt = JSON.stringify(settingsPayload, null, 2);
    try {
      await navigator.clipboard.writeText(txt);
      showToast('Đã copy toàn bộ JSON cấu hình vào Clipboard', 'ok');
    } catch (_) {
      showToast('Trình duyệt chặn Clipboard — vui lòng cấp quyền copy', 'warn');
    }
  });

  document.getElementById('settings-cat-rail')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-set-cat]');
    if (!btn) return;
    settingsCat = btn.dataset.setCat;
    renderSettingsRail();
    renderSettingsCatalog();
  });

  const content = document.getElementById('settings-content');
  content?.addEventListener('click', (ev) => {
    const actBtn = ev.target.closest('[data-set-act]');
    if (actBtn) {
      const act = actBtn.dataset.setAct;
      const valStr = actBtn.dataset.setVal;
      const hint = actBtn.dataset.setHint || '';
      applyCamSetting(act, valStr === '' ? null : { value: Number(valStr) }, hint);
      return;
    }
    const numBtn = ev.target.closest('[data-set-num-act]');
    if (numBtn) {
      const inp = document.getElementById(numBtn.dataset.setInput);
      const act = numBtn.dataset.setNumAct;
      const hint = numBtn.dataset.setHint || '';
      if (inp) applyCamSetting(act, { value: Number(inp.value) }, hint);
    }
  });

  content?.addEventListener('toggle', (ev) => {
    const det = ev.target;
    if (!(det instanceof HTMLDetailsElement) || !det.open || !det.classList.contains('raw-sec-card')) return;
    const body = det.querySelector('.raw-sec-body');
    if (body && !body.innerHTML.trim()) {
      const sec = settingsPayload?.sections?.[Number(det.dataset.rawIdx)];
      if (sec) body.innerHTML = renderRawSectionInner(sec, '');
    }
  }, true);

  registerModalEsc(() => modal.classList.contains('open'), closeSettingsModal);
}
