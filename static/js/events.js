/**
 * Recorded Events History Tab & Playback Modal:
 * - True server-side filtering (Room / Date / Trigger) + cursor pagination (24/page)
 * - Smart tab caching & AbortController cancellation on rapid filter changes
 * - Incremental DOM append + IntersectionObserver auto-loading + lightweight /api/event-thumb thumbnails
 * - Video metadata-only preload & Range playback controls
 */
import { apiJson, store } from './api.js';
import { escHtml, svgIcon } from './icons.js';
import { confirmDialog, registerModalEsc, showToast } from './ui-feedback.js';

const PAGE_SIZE = 24;
const CACHE_TTL_MS = 60000;

let loadedEvents = [];
let totalEvents = 0;
let hasMoreEvents = false;
let isLoadingEvents = false;
let lastQueryKey = '';
let lastFetchTs = 0;
let lastKnownClipCount = null;
let fetchAbortCtrl = null;
let scrollObserver = null;
let onRefreshCb = null;

function formatDateGroupLabel(dateOrTs) {
  const datePart = String(dateOrTs || '').split(' ')[0];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(datePart)) return 'Khác';
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  const today = `${y}-${m}-${d}`;
  const yestDate = new Date(now.getTime() - 86400000);
  const yest = `${yestDate.getFullYear()}-${String(yestDate.getMonth() + 1).padStart(2, '0')}-${String(yestDate.getDate()).padStart(2, '0')}`;
  if (datePart === today) return 'Hôm nay';
  if (datePart === yest) return 'Hôm qua';
  const [yy, mm, dd] = datePart.split('-');
  return `${dd}/${mm}/${yy}`;
}

function getReasonMeta(reason) {
  const r = String(reason || '').toUpperCase();
  if (r.includes('HW_ALARM_AI')) return { cls: 'badge danger', label: 'AI Bám người' };
  if (r.includes('HW_ALARM_MD')) return { cls: 'badge warn', label: 'CĐ Camera' };
  if (r.includes('HW_ALARM')) return { cls: 'badge danger', label: 'Cảnh báo Camera' };
  if (r.includes('MANUAL')) return { cls: 'badge info', label: 'Ghi thủ công' };
  if (r.includes('TEST') || r.includes('TRIGGER')) return { cls: 'badge warn', label: 'Ghi nhanh' };
  if (r.includes('PFRAME')) return { cls: 'badge ok', label: 'Chuyển động Hub' };
  return { cls: 'badge', label: reason || 'Sự kiện' };
}

function getCurrentFilters() {
  return {
    room: document.getElementById('ev-room-filter')?.value || 'ALL',
    date: document.getElementById('ev-date-filter')?.value || 'ALL',
    trigger: document.getElementById('ev-trigger-filter')?.value || 'ALL',
  };
}

function syncDateFilterOptions(dates) {
  const sel = document.getElementById('ev-date-filter');
  if (!sel || !Array.isArray(dates)) return;
  const cur = sel.value || 'ALL';
  sel.innerHTML = '<option value="ALL">Tất cả ngày</option>' +
    dates.map((d) => `<option value="${escHtml(d)}">${escHtml(formatDateGroupLabel(d))} (${escHtml(d)})</option>`).join('');
  sel.value = dates.includes(cur) ? cur : 'ALL';
}

function updateEventsCounters() {
  const countBadge = document.getElementById('ev-count-badge');
  if (countBadge) {
    countBadge.textContent = loadedEvents.length < totalEvents
      ? `${loadedEvents.length}/${totalEvents} clip`
      : `${totalEvents} clip`;
  }
  const loadMoreWrap = document.getElementById('ev-load-more-wrap');
  const btn = document.getElementById('ev-load-more-btn');
  if (loadMoreWrap && btn) {
    if (hasMoreEvents) {
      loadMoreWrap.style.display = 'flex';
      const remaining = Math.max(0, totalEvents - loadedEvents.length);
      btn.disabled = isLoadingEvents;
      btn.textContent = isLoadingEvents
        ? 'Đang tải thêm...'
        : `Tải thêm ${Math.min(PAGE_SIZE, remaining)} clip (còn ${remaining})`;
    } else {
      loadMoreWrap.style.display = 'none';
    }
  }
}

function renderEventCardHtml(e) {
  const rm = getReasonMeta(e.reason);
  const timeShort = String(e.start_time || '').split(' ')[1] || e.start_time || '';
  const thumbHtml = e.thumb_rel
    ? `<img src="/api/event-thumb?rel=${encodeURIComponent(e.thumb_rel)}&w=480" loading="lazy" decoding="async" alt="${escHtml(e.room_name)}" onerror="this.style.visibility='hidden'">`
    : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--text-muted);">${svgIcon('film', 'icon-lg')}</div>`;

  return `
    <article class="event-card" data-ev-card="${escHtml(e.video_rel)}">
      <div class="event-thumb" data-ev-play="${escHtml(e.video_rel)}" data-ev-title="${escHtml(`${e.room_name} · ${e.start_time}`)}">
        ${thumbHtml}
        <div class="event-overlay-bar">
          <span class="${rm.cls}">${escHtml(rm.label)}</span>
          <span class="badge mono">${Number(e.duration_sec || 0).toFixed(0)}s</span>
        </div>
        <div class="event-play-pill">
          <div class="event-play-circle">${svgIcon('play', 'icon-lg')}</div>
        </div>
      </div>
      <div class="event-meta">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
          <strong style="font-size:13px; color:var(--text-primary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escHtml(e.room_name)}</strong>
          <span class="mono" style="color:var(--text-muted); font-size:11px;">${escHtml(timeShort)}</span>
        </div>
        <div class="mono" style="color:var(--text-muted); font-size:11px;">
          ${escHtml(String(e.codec || 'H264').toUpperCase())} · ${e.fps ?? 0} FPS · ${Number(e.size_mb || 0).toFixed(1)} MB
        </div>
        <div class="event-actions">
          <button type="button" class="btn sm primary" data-ev-play="${escHtml(e.video_rel)}" data-ev-title="${escHtml(`${e.room_name} · ${e.start_time}`)}">
            ${svgIcon('play', 'icon-sm')}<span>Phát video</span>
          </button>
          <a class="btn sm" href="/recordings/${encodeURI(e.video_rel)}" download title="Tải về máy">${svgIcon('download', 'icon-sm')}</a>
          <button type="button" class="btn sm danger" data-ev-del="${escHtml(e.video_rel)}" title="Xóa clip">${svgIcon('trash', 'icon-sm')}</button>
        </div>
      </div>
    </article>
  `;
}

function appendEventsToDom(items, reset = false) {
  const container = document.getElementById('events-container');
  if (!container) return;

  if (reset) container.innerHTML = '';

  if (!loadedEvents.length) {
    container.innerHTML = `
      <div class="card-panel" style="text-align:center; padding:42px 18px; color:var(--text-muted);">
        <div style="margin-bottom:8px;">${svgIcon('film', 'icon-lg')}</div>
        <div style="font-weight:700; color:var(--text-secondary); margin-bottom:4px;">Chưa có video sự kiện nào phù hợp bộ lọc</div>
        <div style="font-size:12.5px;">Các đoạn ghi Pre+Post 20s và cảnh báo chuyển động sẽ tự động xuất hiện tại đây.</div>
      </div>
    `;
    return;
  }

  for (const item of items) {
    const label = formatDateGroupLabel(item.start_time);
    let gridEl = container.querySelector(`.events-grid[data-date-group="${CSS.escape(label)}"]`);
    if (!gridEl) {
      const hdr = document.createElement('div');
      hdr.className = 'events-group-header';
      hdr.dataset.dateGroupHeader = label;
      hdr.innerHTML = `<span>${escHtml(label)}</span><span class="mono" style="font-size:11.5px; color:var(--text-muted);" data-group-count>0 clip</span>`;
      gridEl = document.createElement('div');
      gridEl.className = 'events-grid';
      gridEl.dataset.dateGroup = label;
      container.appendChild(hdr);
      container.appendChild(gridEl);
    }
    gridEl.insertAdjacentHTML('beforeend', renderEventCardHtml(item));
    const countEl = container.querySelector(`.events-group-header[data-date-group-header="${CSS.escape(label)}"] [data-group-count]`);
    if (countEl) countEl.textContent = `${gridEl.children.length} clip`;
  }
}

export async function loadEvents({ force = false, append = false } = {}) {
  const { room, date, trigger } = getCurrentFilters();
  const queryKey = `${room}|${date}|${trigger}`;
  const curClipCount = store.storage?.clip_count ?? null;

  if (append) {
    if (isLoadingEvents || !hasMoreEvents) return;
  } else if (!force) {
    const container = document.getElementById('events-container');
    const isFreshCache =
      loadedEvents.length > 0 &&
      lastQueryKey === queryKey &&
      lastKnownClipCount === curClipCount &&
      (Date.now() - lastFetchTs) < CACHE_TTL_MS &&
      container &&
      container.children.length > 0;
    if (isFreshCache) {
      updateEventsCounters();
      return;
    }
  }

  if (!append && fetchAbortCtrl) {
    fetchAbortCtrl.abort();
  }
  const ctrl = new AbortController();
  fetchAbortCtrl = ctrl;
  isLoadingEvents = true;
  updateEventsCounters();

  const offset = append ? loadedEvents.length : 0;
  const container = document.getElementById('events-container');
  if (!append && container && (!container.children.length || lastQueryKey !== queryKey)) {
    container.innerHTML = '<div class="card-panel" style="text-align:center; padding:28px; color:var(--text-muted);">Đang tải danh sách sự kiện...</div>';
  }

  try {
    const qs = new URLSearchParams({
      room,
      date,
      trigger,
      offset: String(offset),
      limit: String(PAGE_SIZE),
    });
    const { data } = await apiJson(`/api/events?${qs.toString()}`, { signal: ctrl.signal });
    if (ctrl.signal.aborted) return;

    const batch = Array.isArray(data.events) ? data.events : [];
    totalEvents = Number(data.total ?? batch.length);
    hasMoreEvents = Boolean(data.has_more);
    lastQueryKey = queryKey;
    lastFetchTs = Date.now();
    lastKnownClipCount = curClipCount;

    if (Array.isArray(data.dates)) syncDateFilterOptions(data.dates);

    if (append) {
      loadedEvents = loadedEvents.concat(batch);
      appendEventsToDom(batch, false);
    } else {
      loadedEvents = batch;
      appendEventsToDom(batch, true);
    }
  } catch (err) {
    if (err.name === 'AbortError') return;
    if (!append && container) {
      container.innerHTML = `<div class="card-panel" style="color:var(--danger-text);">Lỗi tải lịch sử video: ${escHtml(err.message)}</div>`;
    } else {
      showToast(`Lỗi tải thêm clip: ${err.message}`, 'error');
    }
  } finally {
    if (fetchAbortCtrl === ctrl) fetchAbortCtrl = null;
    isLoadingEvents = false;
    updateEventsCounters();
  }
}

function canBrowserDecodeHevc(video) {
  try {
    return Boolean(
      video.canPlayType('video/mp4; codecs="hvc1.1.6.L120.90"') ||
      video.canPlayType('video/mp4; codecs="hev1.1.6.L120.90"')
    );
  } catch (_) {
    return false;
  }
}

export function playEvent(rel, title) {
  const modal = document.getElementById('play-modal');
  const video = document.getElementById('play-video');
  const dl = document.getElementById('play-dl');
  const titleEl = document.getElementById('play-title');
  const loadingEl = document.getElementById('play-loading');
  if (!modal || !video || !rel) return;

  const rawUrl = `/recordings/${encodeURI(rel)}`;
  const compatUrl = `/api/event-video?rel=${encodeURIComponent(rel)}`;
  const useCompat = !canBrowserDecodeHevc(video);

  if (titleEl) titleEl.textContent = title || 'Xem lại sự kiện';
  if (dl) dl.href = rawUrl;
  video.dataset.rel = rel;
  video.dataset.compatTried = useCompat ? '1' : '0';
  if (loadingEl) {
    loadingEl.textContent = useCompat
      ? 'Đang chuyển chuẩn H.264 tương thích trình duyệt...'
      : 'Đang nạp video...';
    loadingEl.style.display = 'block';
  }
  video.src = useCompat ? compatUrl : rawUrl;
  video.playbackRate = 1;
  document.querySelectorAll('.play-speed-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.speed === '1');
  });
  modal.classList.add('open');
  video.load();
  video.play().catch(() => {});
}

export function closePlayModal() {
  const modal = document.getElementById('play-modal');
  const video = document.getElementById('play-video');
  const loadingEl = document.getElementById('play-loading');
  if (loadingEl) {
    loadingEl.textContent = 'Đang nạp video...';
    loadingEl.style.display = 'none';
  }
  if (video) {
    delete video.dataset.rel;
    delete video.dataset.compatTried;
    video.pause();
    video.removeAttribute('src');
    video.load();
  }
  modal?.classList.remove('open');
}

async function deleteEventClip(rel) {
  const ok = await confirmDialog({
    title: 'Xóa đoạn video sự kiện',
    message: 'Clip MP4 và ảnh thumbnail liên quan sẽ bị xóa vĩnh viễn khỏi ổ cứng Hub. Bạn chắc chứ?',
    confirmText: 'Xóa vĩnh viễn',
    danger: true,
  });
  if (!ok) return;
  try {
    const { res, data } = await apiJson('/api/events', {
      method: 'DELETE',
      body: { video_rel: rel },
    });
    if (!res.ok || data.ok === false) {
      showToast(data.error || 'Không thể xóa file', 'error');
      return;
    }
    loadedEvents = loadedEvents.filter((e) => e.video_rel !== rel);
    totalEvents = Math.max(0, totalEvents - 1);
    if (lastKnownClipCount !== null && lastKnownClipCount > 0) {
      lastKnownClipCount -= 1;
    }

    const card = document.querySelector(`.event-card[data-ev-card="${CSS.escape(rel)}"]`);
    if (card) {
      const grid = card.parentElement;
      const groupName = grid?.dataset?.dateGroup;
      card.remove();
      if (grid && groupName) {
        if (!grid.children.length) {
          document.querySelector(`.events-group-header[data-date-group-header="${CSS.escape(groupName)}"]`)?.remove();
          grid.remove();
        } else {
          const countEl = document.querySelector(`.events-group-header[data-date-group-header="${CSS.escape(groupName)}"] [data-group-count]`);
          if (countEl) countEl.textContent = `${grid.children.length} clip`;
        }
      }
    }
    if (!loadedEvents.length) {
      if (hasMoreEvents) {
        loadEvents({ force: true, append: false });
      } else {
        appendEventsToDom([], true);
      }
    }
    updateEventsCounters();
    showToast('Đã xóa video sự kiện', 'info');
    if (onRefreshCb) onRefreshCb();
  } catch (err) {
    showToast(`Lỗi xóa file: ${err.message}`, 'error');
  }
}

export function initEventsTab({ onRefreshStatus } = {}) {
  onRefreshCb = onRefreshStatus || null;
  document.getElementById('ev-room-filter')?.addEventListener('change', () => {
    const dateSel = document.getElementById('ev-date-filter');
    if (dateSel) dateSel.value = 'ALL';
    loadEvents({ force: true });
  });
  document.getElementById('ev-date-filter')?.addEventListener('change', () => loadEvents({ force: true }));
  document.getElementById('ev-trigger-filter')?.addEventListener('change', () => {
    const dateSel = document.getElementById('ev-date-filter');
    if (dateSel) dateSel.value = 'ALL';
    loadEvents({ force: true });
  });
  document.getElementById('ev-refresh-btn')?.addEventListener('click', () => loadEvents({ force: true }));
  document.getElementById('ev-load-more-btn')?.addEventListener('click', () => loadEvents({ append: true }));

  const sentinel = document.getElementById('ev-load-sentinel');
  if (sentinel && 'IntersectionObserver' in window) {
    scrollObserver = new IntersectionObserver((entries) => {
      if (
        store.currentTab === 'events' &&
        hasMoreEvents &&
        !isLoadingEvents &&
        entries.some((e) => e.isIntersecting)
      ) {
        loadEvents({ append: true });
      }
    }, { rootMargin: '280px 0px' });
    scrollObserver.observe(sentinel);
  }

  document.getElementById('events-container')?.addEventListener('click', (ev) => {
    const delBtn = ev.target.closest('[data-ev-del]');
    if (delBtn) {
      deleteEventClip(delBtn.dataset.evDel);
      return;
    }
    const playEl = ev.target.closest('[data-ev-play]');
    if (playEl) {
      playEvent(playEl.dataset.evPlay, playEl.dataset.evTitle);
    }
  });

  const playModal = document.getElementById('play-modal');
  const video = document.getElementById('play-video');
  const loadingEl = document.getElementById('play-loading');
  if (video && loadingEl) {
    const showL = () => {
      if (!video.dataset.rel) return;
      loadingEl.style.display = 'block';
    };
    const hideL = () => {
      loadingEl.textContent = 'Đang nạp video...';
      loadingEl.style.display = 'none';
    };
    video.addEventListener('loadstart', showL);
    video.addEventListener('waiting', showL);
    video.addEventListener('loadeddata', hideL);
    video.addEventListener('canplay', hideL);
    video.addEventListener('playing', hideL);
    video.addEventListener('error', () => {
      const rel = video.dataset.rel;
      if (!rel || !video.getAttribute('src') || !playModal?.classList.contains('open')) return;
      if (video.dataset.compatTried !== '1') {
        video.dataset.compatTried = '1';
        loadingEl.textContent = 'Trình duyệt không giải mã H.265 — đang chuyển sang H.264...';
        loadingEl.style.display = 'block';
        video.src = `/api/event-video?rel=${encodeURIComponent(rel)}`;
        video.load();
        video.play().catch(() => {});
        return;
      }
      loadingEl.textContent = 'Không phát được video trên trình duyệt này. Vui lòng bấm "Tải MP4".';
      loadingEl.style.display = 'block';
    });
  }

  if (playModal) {
    playModal.addEventListener('click', (ev) => {
      if (ev.target === playModal) closePlayModal();
    });
    document.getElementById('play-close-btn')?.addEventListener('click', closePlayModal);
    document.querySelectorAll('.play-speed-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const spd = Number(btn.dataset.speed || 1);
        if (video) video.playbackRate = spd;
        document.querySelectorAll('.play-speed-btn').forEach((b) => {
          b.classList.toggle('active', b === btn);
        });
      });
    });

    document.addEventListener('keydown', (e) => {
      if (!playModal.classList.contains('open')) return;
      if (!video) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        video.currentTime = Math.max(0, video.currentTime - 5);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        video.currentTime = Math.min(video.duration || video.currentTime + 5, video.currentTime + 5);
      } else if (e.code === 'Space' && e.target === document.body) {
        e.preventDefault();
        if (video.paused) video.play().catch(() => {});
        else video.pause();
      }
    });

    registerModalEsc(() => playModal.classList.contains('open'), closePlayModal);
  }
}
