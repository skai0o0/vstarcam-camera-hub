#!/usr/bin/env python3
"""
Live compressed video for the web UI (`/ws/video/{uid}?codec=hevc|h264`).

Browsers decode with WebCodecs (hardware on the viewer's device) instead of the hub
decoding HEVC and re-encoding MJPEG for every viewer:

* hevc  -- the camera's own H.265 frames, passed through untouched (0% hub CPU).
* h264  -- one shared iGPU transcoder per camera (VAAPI hevc decode -> h264_vaapi),
           for browsers without HEVC; started on demand, stopped when unwatched.

Wire format: first a text message with the stream config (JSON), then binary frames
    [1B flags (bit0 = keyframe)][4B uint32 BE timestamp ms][Annex-B access unit]
A client that cannot keep up has its backlog dropped and resumes at the next keyframe.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Set

log = logging.getLogger("eye4.live")

START = b"\x00\x00\x00\x01"
AUD_H264 = b"\x00\x00\x00\x01\x09"
MAX_BACKLOG = 50            # frames (~2s) queued per client before dropping to next keyframe
TRANSCODER_IDLE_SEC = 10.0  # stop the iGPU transcoder this long after its last viewer left

# Codec strings the browser configures its VideoDecoder with
CODEC_STRINGS = {
    "hevc": "hev1.1.6.L120.90",   # HEVC Main, level 4.0, in-band parameter sets (Annex B)
    "h264": "avc1.4D4028",        # H.264 Main, level 4.0 (h264_vaapi -profile:v main)
}


def _packet(key: bool, ts_ms: int, data: bytes) -> bytes:
    return bytes([1 if key else 0]) + (ts_ms & 0xFFFFFFFF).to_bytes(4, "big") + data


class LiveClient:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.need_key = True
        self.dropped = 0

    def offer(self, key: bool, pkt: bytes):
        if self.need_key and not key:
            return
        if self.queue.qsize() >= MAX_BACKLOG:
            # Client too slow: drop the backlog, resume at the next keyframe (bounded lag)
            while not self.queue.empty():
                self.queue.get_nowait()
                self.dropped += 1
            self.need_key = True
            if not key:
                return
        self.need_key = False
        self.queue.put_nowait(pkt)


class CameraLiveVideo:
    """Fan-out of one camera's live video to WebSocket clients."""

    def __init__(self, uid: str, get_session: Callable, get_recorder: Callable,
                 get_vaapi_device: Callable):
        self.uid = uid
        self._get_session = get_session
        self._get_recorder = get_recorder
        self._get_vaapi = get_vaapi_device
        self.clients: Dict[str, Set[LiveClient]] = {"hevc": set(), "h264": set()}
        self._t0 = time.monotonic()
        self._listening_session = None
        # H.264 transcoder state
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._proc_tasks: Set[asyncio.Task] = set()
        self._feed_q: Optional[asyncio.Queue] = None
        self._h264_gop: List[bytes] = []    # packets since the last IDR, for joiners
        self._idle_since: Optional[float] = None
        self._starting = False

    # ---- helpers -------------------------------------------------------------

    def _ts(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def _header(self) -> bytes:
        rec = self._get_recorder()
        return rec._build_header_prefix() if rec else b""

    def _annexb(self, data: bytes, key: bool) -> bytes:
        body = data if data.startswith((b"\x00\x00\x01", START)) else START + data
        return (self._header() + body) if key else body

    def _ensure_listener(self):
        sess = self._get_session()
        if sess is self._listening_session:
            return
        if self._listening_session is not None:
            self._listening_session.video_listeners.discard(self._on_frame)
        self._listening_session = sess
        if sess is not None:
            sess.video_listeners.add(self._on_frame)

    def _maybe_release_listener(self):
        if not self.clients["hevc"] and self._proc is None and not self._starting:
            if self._listening_session is not None:
                self._listening_session.video_listeners.discard(self._on_frame)
                self._listening_session = None

    def _gop_from_ring(self) -> List:
        """Frames from the newest clean keyframe in the recorder's RAM ring buffer."""
        rec = self._get_recorder()
        if not rec:
            return []
        frames = list(rec._ring_buffer)
        last_gap = getattr(rec, "_last_gap_mono", 0.0)
        for i in range(len(frames) - 1, -1, -1):
            if frames[i].is_iframe and frames[i].mono_ts > last_gap:
                return frames[i:]
        return []

    # ---- frame input (called from CameraSession after the playout buffer) ------

    def _on_frame(self, frame_type: int, cam_ts: int, data: bytes):
        if not data:
            return
        key = frame_type in (0x10, 0)   # FRAME_I
        if self.clients["hevc"]:
            pkt = _packet(key, self._ts(), self._annexb(data, key))
            for c in list(self.clients["hevc"]):
                c.offer(key, pkt)
        if self._feed_q is not None:
            if self._feed_q.qsize() < 100:
                self._feed_q.put_nowait(self._annexb(data, key))

    # ---- client management ---------------------------------------------------

    def add_client(self, codec: str) -> LiveClient:
        c = LiveClient()
        self._ensure_listener()
        if codec == "hevc":
            # Join instantly: replay the current GOP from RAM (decoder catches up quickly)
            for f in self._gop_from_ring():
                c.offer(f.is_iframe, _packet(f.is_iframe, self._ts(), self._annexb(f.data, f.is_iframe)))
        else:
            for pkt in self._h264_gop:
                c.offer(pkt[0] & 1 == 1, pkt)
            self._idle_since = None
            if self._proc is None and not self._starting:
                asyncio.ensure_future(self._start_transcoder())
        self.clients[codec].add(c)
        return c

    def remove_client(self, codec: str, c: LiveClient):
        self.clients[codec].discard(c)
        if codec == "h264" and not self.clients["h264"]:
            self._idle_since = time.monotonic()
        self._maybe_release_listener()

    # ---- iGPU H.264 transcoder ---------------------------------------------------

    async def _start_transcoder(self):
        dev = self._get_vaapi()
        if not dev:
            return
        self._starting = True
        try:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-hwaccel", "vaapi", "-hwaccel_device", dev, "-hwaccel_output_format", "vaapi",
                # NB: no -flags low_delay: it breaks VAAPI HEVC decoding ("Could not find
                # ref with POC"); measured clean without it.
                "-probesize", "49152", "-analyzeduration", "50000",
                "-f", "hevc", "-i", "pipe:0",
                "-fps_mode", "passthrough",
                # This driver only offers CQP rate control; qp 28 ~ 1.5 Mbps at 1080p25.
                "-c:v", "h264_vaapi", "-profile:v", "main", "-bf", "0", "-g", "50", "-qp", "28",
                "-bsf:v", "h264_metadata=aud=insert",
                "-f", "h264", "pipe:1",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            self._proc = proc
            self._h264_gop = []
            self._feed_q = asyncio.Queue()
            # Prime from the current GOP so the first picture appears immediately
            for f in self._gop_from_ring():
                self._feed_q.put_nowait(self._annexb(f.data, f.is_iframe))
            self._ensure_listener()
            for coro in (self._feed(proc), self._read(proc), self._stderr(proc), self._idle_watch(proc)):
                t = asyncio.ensure_future(coro)
                self._proc_tasks.add(t)
                t.add_done_callback(self._proc_tasks.discard)
            log.info("[%s] iGPU H.264 live transcoder started (pid=%d)", self.uid[:10], proc.pid)
        except Exception as e:
            log.warning("[%s] iGPU H.264 transcoder failed to start: %s", self.uid[:10], e)
            self._proc = None
            self._feed_q = None
        finally:
            self._starting = False

    async def _feed(self, proc):
        try:
            while self._proc is proc and proc.returncode is None:
                chunk = await self._feed_q.get()
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except Exception:
            pass
        finally:
            await self._stop(proc)

    async def _read(self, proc):
        buf = bytearray()
        try:
            while self._proc is proc:
                data = await proc.stdout.read(65536)
                if not data:
                    break
                buf.extend(data)
                # Access units are delimited by the AUD NAL inserted by h264_metadata
                while True:
                    s = buf.find(AUD_H264)
                    if s < 0:
                        break
                    e = buf.find(AUD_H264, s + 5)
                    if e < 0:
                        if s > 0:
                            del buf[:s]
                        break
                    au = bytes(buf[s:e])
                    del buf[:e]
                    self._emit_h264(au)
        except Exception:
            pass
        finally:
            await self._stop(proc)

    def _emit_h264(self, au: bytes):
        key = False
        i = 0
        while True:
            i = au.find(b"\x00\x00\x01", i)
            if i < 0 or i + 3 >= len(au):
                break
            if au[i + 3] & 0x1F == 5:          # IDR slice
                key = True
                break
            i += 3
        pkt = _packet(key, self._ts(), au)
        if key:
            self._h264_gop = [pkt]
        elif self._h264_gop and len(self._h264_gop) < 150:
            self._h264_gop.append(pkt)
        for c in list(self.clients["h264"]):
            c.offer(key, pkt)

    async def _stderr(self, proc):
        last = 0.0
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                if time.monotonic() - last > 10:
                    last = time.monotonic()
                    log.warning("[%s] H.264 transcoder: %s", self.uid[:10],
                                line.decode(errors="replace").strip()[:200])
        except Exception:
            pass

    async def _idle_watch(self, proc):
        while self._proc is proc:
            await asyncio.sleep(2.0)
            if self.clients["h264"]:
                self._idle_since = None
            elif self._idle_since and time.monotonic() - self._idle_since > TRANSCODER_IDLE_SEC:
                log.info("[%s] No H.264 viewers for %.0fs — stopping iGPU transcoder",
                         self.uid[:10], TRANSCODER_IDLE_SEC)
                await self._stop(proc)

    async def _stop(self, proc):
        if self._proc is proc:
            self._proc = None
            self._feed_q = None
            self._h264_gop = []
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except Exception:
            pass
        self._maybe_release_listener()


async def serve_ws(ws, live: CameraLiveVideo, codec: str):
    """Run one /ws/video client until it disconnects."""
    client = live.add_client(codec)
    await ws.send_str(json.dumps({"type": "config", "codec": codec, "codecString": CODEC_STRINGS[codec]}))

    async def drain_rx():
        async for _ in ws:
            pass

    rx = asyncio.ensure_future(drain_rx())
    try:
        while not ws.closed and not rx.done():
            try:
                pkt = await asyncio.wait_for(client.queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            await ws.send_bytes(pkt)
    except (ConnectionError, RuntimeError):
        pass
    finally:
        rx.cancel()
        live.remove_client(codec, client)
