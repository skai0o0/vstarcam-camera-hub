#!/usr/bin/env python3
"""
Smart Motion Pre-Roll / Post-Roll Recorder & Real-Time 25fps Live Stream Engine
for Eye4/VStarcam C22S Camera Hub.

Key Features:
- Circular RAM Ring Buffer (holds ~12-14s of raw H.265/H.264 GOPs in RAM; 0 disk writes when idle)
- On-Demand Persistent FFmpeg Pipe Decoder:
    * Instantly primes from the RAM Ring Buffer's latest GOPs so live stream starts in ~40ms
    * Decodes every P-Frame and I-Frame at full 25 FPS (zero 4-second keyframe lag!)
    * Automatically shuts down after 10s when no browser/viewer is connected (0% background CPU)
- Dual-Trigger Motion Detection (0% decode CPU):
    1. Camera Hardware AI/Motion Alarm (`cmd=2127` `track_status=1`)
    2. H.265/H.264 P-Frame byte-size spike analysis
- Pre-roll (-10s) & Post-roll (+10s) seamless clip muxing (`-c:v copy` to `.mp4`)
"""

import asyncio
import collections
import datetime
import json
import logging
import os
import shutil
import time
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from eye4_rtsp_proxy import FFMPEG_BIN, FRAME_I

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    cv2 = None
    np = None
    HAS_CV2 = False

log = logging.getLogger("eye4.recorder")


def make_compact_jpeg_thumb(raw_jpg: bytes, max_width: int = 480, quality: int = 74) -> bytes:
    """Downscale a JPEG thumbnail to keep event history fast & light on WAN/mobile."""
    if not raw_jpg or not HAS_CV2:
        return raw_jpg
    try:
        arr = np.frombuffer(raw_jpg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return raw_jpg
        h, w = img.shape[:2]
        if w <= max_width:
            return raw_jpg
        new_h = max(2, int(round(h * (max_width / float(w)))))
        small = cv2.resize(img, (max_width, new_h), interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if ok:
            return bytes(enc)
    except Exception:
        pass
    return raw_jpg


class VideoFrameItem:
    __slots__ = ("mono_ts", "wall_ts", "frame_type", "cam_ts", "data", "is_iframe")

    def __init__(self, mono_ts: float, wall_ts: float, frame_type: int, cam_ts: int, data: bytes):
        self.mono_ts = mono_ts
        self.wall_ts = wall_ts
        self.frame_type = frame_type
        self.cam_ts = cam_ts
        self.data = data
        self.is_iframe = (frame_type == FRAME_I or frame_type == 0)


class CameraMotionRecorder:
    """
    Manages the RAM circular pre-roll buffer, real-time 25fps live MJPEG pipe,
    dual-trigger motion detector, and zero-transcode MP4 clip writer for a single camera.
    """

    LIVE_IDLE_TIMEOUT_SEC = 10.0

    def __init__(
        self,
        uid: str,
        room_name: str,
        recordings_dir: str,
        pre_roll_sec: float = 6.0,
        post_roll_sec: float = 6.0,
        max_clip_sec: float = 300.0,
        pframe_sensitivity: float = 2.8,
        enable_pframe_trigger: bool = True,
        pframe_cooldown_sec: float = 20.0,
        snapshot_getter: Optional[Callable[[], Awaitable[Optional[bytes]]]] = None,
    ):
        self.uid = uid
        self.room_name = room_name
        self.recordings_dir = recordings_dir
        self.pre_roll_sec = float(pre_roll_sec)
        self.post_roll_sec = float(post_roll_sec)
        self.max_clip_sec = float(max_clip_sec)
        self.pframe_sensitivity = float(pframe_sensitivity)
        self.enable_pframe_trigger = bool(enable_pframe_trigger)
        self.pframe_cooldown_sec = max(0.0, float(pframe_cooldown_sec))
        self.snapshot_getter = snapshot_getter

        # Circular RAM buffer holding (pre_roll_sec + 4s) of frames
        self._ring_buffer: Deque[VideoFrameItem] = collections.deque()
        self._ring_bytes: int = 0
        # Matching audio ring: (arrival_mono, payload, codec) chunks of 8kHz mono G.711/PCM
        self._audio_ring: Deque[tuple] = collections.deque()
        self._active_audio: List[tuple] = []
        self.audio_codec: Optional[str] = None

        # Codec parameter sets (extracted by RTSPServer or directly)
        self.codec: str = "h265"
        self.vps: Optional[bytes] = None
        self.sps: Optional[bytes] = None
        self.pps: Optional[bytes] = None

        # Motion & recording state
        self.is_recording: bool = False
        self.manual_record: bool = False
        self.last_motion_mono: float = 0.0
        self.last_motion_wall: float = 0.0
        self.last_trigger_reason: str = ""
        self.record_start_mono: float = 0.0
        self.record_start_wall: float = 0.0
        self.record_until_mono: float = 0.0
        self._active_frames: List[VideoFrameItem] = []
        self._active_bytes: int = 0
        self._pending_thumbnail: Optional[bytes] = None
        self._mux_tasks: set = set()

        # P-Frame spike analysis state (0% CPU motion detector)
        self._pframe_ema: float = 0.0
        self._pframe_spike_count: int = 0
        self._warmup_frames: int = 0
        self._last_saved_end_mono: float = 0.0
        self._last_clip_finished_mono: float = 0.0

        # Telemetry metrics (bitrate & FPS)
        self._stat_window_start: float = time.monotonic()
        self._stat_bytes: int = 0
        self._stat_frames: int = 0
        self.current_kbps: float = 0.0
        self.current_fps: float = 0.0
        self.clips_saved_count: int = 0
        self.last_clip_path: Optional[str] = None

        # Real-time 25fps Live Stream Broadcaster state (On-Demand Persistent FFmpeg Pipe)
        self.latest_live_jpeg: Optional[bytes] = None
        self.latest_live_seq: int = 0
        self.latest_live_mono: float = 0.0
        self._live_frame_event: asyncio.Event = asyncio.Event()
        self._last_viewer_mono: float = 0.0
        self._live_proc: Optional[asyncio.subprocess.Process] = None
        self._live_in_queue: asyncio.Queue = asyncio.Queue(maxsize=90)
        self._live_starting: bool = False
        self._live_tasks: set = set()
        self._live_need_keyframe: bool = True
        self._last_gap_mono: float = 0.0

    def set_room_name(self, name: str):
        self.room_name = name.strip() or self.uid[:12]

    def sync_codec_params(self, codec: str, vps: Optional[bytes], sps: Optional[bytes], pps: Optional[bytes]):
        prev_codec = self.codec
        prev_params = (self.vps, self.sps, self.pps)
        if codec:
            self.codec = codec
        if vps:
            self.vps = vps
        if sps:
            self.sps = sps
        if pps:
            self.pps = pps
        new_params = (self.vps, self.sps, self.pps)
        if self._live_proc is not None and (prev_codec != self.codec or prev_params != new_params):
            self._live_need_keyframe = True
            self._drain_live_queue()
            if prev_codec != self.codec:
                try:
                    self._live_proc.kill()
                except ProcessLookupError:
                    pass

    @property
    def ram_buffer_kb(self) -> float:
        return round((self._ring_bytes + self._active_bytes) / 1024.0, 1)

    @property
    def motion_active(self) -> bool:
        return (time.monotonic() - self.last_motion_mono) < 3.0 if self.last_motion_mono > 0 else False

    def _build_header_prefix(self) -> bytes:
        start_code = b"\x00\x00\x00\x01"
        is_hevc = (self.codec == "h265")
        header = b""
        if is_hevc and self.vps:
            header += start_code + self.vps
        if self.sps:
            header += start_code + self.sps
        if self.pps:
            header += start_code + self.pps
        return header

    @staticmethod
    def _wrap_annexb(data: bytes, header_prefix: bytes = b"") -> bytes:
        start_code = b"\x00\x00\x00\x01"
        has_sc = data.startswith(b"\x00\x00\x01") or data.startswith(b"\x00\x00\x00\x01")
        body = data if has_sc else (start_code + data)
        return (header_prefix + body) if header_prefix else body

    # =========================================================================
    # On-Demand Real-Time 25fps Live Decoder (Persistent FFmpeg Pipe)
    # =========================================================================

    def touch_viewer(self):
        """Mark that a browser/viewer is actively watching this camera right now."""
        self._last_viewer_mono = time.monotonic()
        if self._live_proc is None and not self._live_starting and self.sps and self.pps:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._start_live_decoder())
                self._live_tasks.add(task)
                task.add_done_callback(self._live_tasks.discard)
            except RuntimeError:
                pass

    async def get_live_snapshot(self, timeout: float = 1.5) -> Optional[bytes]:
        """Return the latest 25fps decoded JPEG frame in 0.1ms from RAM."""
        self.touch_viewer()
        now = time.monotonic()
        if self.latest_live_jpeg and (now - self.latest_live_mono) < 1.0:
            return self.latest_live_jpeg

        try:
            await asyncio.wait_for(self._live_frame_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.latest_live_jpeg

    def _drain_live_queue(self) -> int:
        """Discard queued pre-gap frames so the live FFmpeg decoder won't consume stale P-frames."""
        dropped = 0
        q = getattr(self, "_live_in_queue", None)
        if q is not None:
            while True:
                try:
                    q.get_nowait()
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
        return dropped

    def on_video_gap(self):
        """Mark an upstream video packet gap/reset so live decoder waits for an intact I-frame."""
        self._last_gap_mono = time.monotonic()
        self._live_need_keyframe = True
        dropped = self._drain_live_queue()
        if dropped:
            log.debug("[%s] Video gap: purged %d stale queued frame(s), waiting for clean IDR",
                      self.room_name, dropped)

    async def _drain_live_stderr(self, proc):
        """Read FFmpeg stderr asynchronously to avoid pipe blocking and catch POC ref errors."""
        stderr = getattr(proc, "stderr", None)
        if stderr is None:
            return
        last_warn = 0.0
        last_poc_reset = 0.0
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="replace").strip()
                if not msg:
                    continue
                now = time.monotonic()
                if "Could not find ref with POC" in msg or "co located POCs" in msg:
                    recent_idr = (now - getattr(self, "_last_live_idr_mono", 0.0)) < 0.25
                    if not recent_idr and (now - last_poc_reset) >= 2.0:
                        last_poc_reset = now
                        self._live_need_keyframe = True
                        self._drain_live_queue()
                if (now - last_warn) >= 5.0:
                    last_warn = now
                    log.warning("[%s] Live FFmpeg stderr: %s", self.room_name, msg[:200])
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _get_prime_bitstream(self) -> bytes:
        """
        Grab the latest unbroken GOP from the RAM Ring Buffer (starting on an I-Frame
        after the last packet gap and ending at the latest P-Frame) so FFmpeg
        completes stream probing quickly on a clean reference frame.
        """
        frames = list(self._ring_buffer)
        if not frames:
            self._live_primed_mono = 0.0
            self._live_need_keyframe = True
            return b""

        iframe_indices = [
            i for i, f in enumerate(frames)
            if f.is_iframe and f.mono_ts > self._last_gap_mono
        ]
        if not iframe_indices:
            self._live_primed_mono = 0.0
            self._live_need_keyframe = True
            return b""

        # Prefer the latest I-frame; only include the preceding clean GOP if the tail slice
        # is < 48KB so FFmpeg's 48KB stream probe finishes without buffering a 4s extra GOP.
        chosen_idx = iframe_indices[-1]
        tail_bytes = sum(len(f.data) for f in frames[chosen_idx:])
        if tail_bytes < 49152 and len(iframe_indices) >= 2:
            chosen_idx = iframe_indices[-2]

        self._live_primed_mono = time.monotonic()
        self._last_live_idr_mono = frames[chosen_idx].mono_ts
        header = self._build_header_prefix()
        chunks = []
        for f in frames[chosen_idx:]:
            chunks.append(self._wrap_annexb(f.data, header if f.is_iframe else b""))
        return b"".join(chunks)

    async def _start_live_decoder(self):
        if self._live_starting or self._live_proc is not None:
            return
        if not self.sps or not self.pps:
            return

        self._live_starting = True
        try:
            is_hevc = (self.codec == "h265")
            fmt = "hevc" if is_hevc else "h264"

            cmd = [
                FFMPEG_BIN,
                "-hide_banner",
                "-loglevel",
                "error",
                "-probesize",
                "49152",
                "-analyzeduration",
                "50000",
                "-flags",
                "low_delay",
                "-f",
                fmt,
                "-i",
                "pipe:0",
                "-fps_mode",
                "passthrough",
                "-vf",
                "scale=960:540:flags=fast_bilinear",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-q:v",
                "5",
                "-flush_packets",
                "1",
                "pipe:1",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Atomic prime-to-live handoff (zero awaits between reading ring buffer and arming
            # _live_proc) so no P-frame is dropped while FFmpeg spawns.
            while not self._live_in_queue.empty():
                try:
                    self._live_in_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            prime_bytes = self._get_prime_bitstream()
            self._live_proc = proc
            self._live_need_keyframe = (
                (not prime_bytes)
                or (getattr(self, "_live_primed_mono", 0.0) <= getattr(self, "_last_gap_mono", 0.0))
            )
            self._live_stderr_task = asyncio.create_task(self._drain_live_stderr(proc))
            log.info(
                "[%s] Started real-time 25fps live stream decoder (pid=%d, prime=%.1f KB)",
                self.room_name, proc.pid, len(prime_bytes) / 1024.0,
            )

            # Start stdout reader FIRST so OS pipe buffer never deadlocks while draining prime_bytes
            t_out = asyncio.create_task(self._read_live_stdout(proc))
            t_in = asyncio.create_task(self._feed_live_stdin(proc, prime_bytes))
            self._live_tasks.update((t_in, t_out))
            t_in.add_done_callback(self._live_tasks.discard)
            t_out.add_done_callback(self._live_tasks.discard)
        except Exception as e:
            log.warning("[%s] Failed to start live stream decoder: %s", self.room_name, e)
            self._live_proc = None
        finally:
            self._live_starting = False

    async def _feed_live_stdin(self, proc: asyncio.subprocess.Process, prime_bytes: bytes = b""):
        try:
            if prime_bytes and proc.stdin:
                # Feed prime bitstream in 16KB chunks so stdout reader drains decoded JPEGs concurrently
                mv = memoryview(prime_bytes)
                for off in range(0, len(mv), 16384):
                    if self._live_proc is not proc or proc.returncode is not None:
                        break
                    proc.stdin.write(mv[off : off + 16384])
                    await proc.stdin.drain()

            while self._live_proc is proc and proc.returncode is None:
                if (time.monotonic() - self._last_viewer_mono) > self.LIVE_IDLE_TIMEOUT_SEC:
                    log.info("[%s] No active viewers for %.0fs — stopping live decoder to save CPU (0%%)",
                             self.room_name, self.LIVE_IDLE_TIMEOUT_SEC)
                    break
                try:
                    chunk = await asyncio.wait_for(self._live_in_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if proc.stdin is None or proc.stdin.is_closing():
                    break
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except Exception:
            pass
        finally:
            await self._stop_live_proc(proc)

    async def _read_live_stdout(self, proc: asyncio.subprocess.Process):
        buf = bytearray()
        try:
            while self._live_proc is proc and proc.returncode is None and proc.stdout:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    break
                buf.extend(chunk)
                # Extract all complete JPEG frames (\xff\xd8\xff ... \xff\xd9)
                latest_jpg = None
                while True:
                    s = buf.find(b"\xff\xd8\xff")
                    if s < 0:
                        if len(buf) > 4:
                            del buf[:-4]
                        break
                    if s > 0:
                        del buf[:s]
                    e = buf.find(b"\xff\xd9", 3)
                    if e < 0:
                        break
                    latest_jpg = bytes(buf[: e + 2])
                    del buf[: e + 2]

                if latest_jpg:
                    self.latest_live_jpeg = latest_jpg
                    self.latest_live_seq += 1
                    self.latest_live_mono = time.monotonic()
                    self._live_frame_event.set()
                    self._live_frame_event.clear()
        except Exception:
            pass
        finally:
            await self._stop_live_proc(proc)

    async def _stop_live_proc(self, proc: asyncio.subprocess.Process):
        if self._live_proc is proc:
            self._live_proc = None
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except Exception:
            pass

    async def close(self):
        """Shut down live decoder subprocess cleanly when recorder is removed."""
        proc = self._live_proc
        self._drain_live_queue()
        if proc is not None:
            await self._stop_live_proc(proc)

    # =========================================================================
    # Motion Detection & Pre-Roll / Post-Roll Recording
    # =========================================================================

    _TRIGGER_PRIORITY = (
        ("MANUAL", 5),
        ("HW_ALARM_AI", 4),
        ("HW_ALARM_MD", 3),
        ("HW_ALARM", 3),
        ("MANUAL_TEST", 2),
        ("PFRAME_SPIKE", 1),
    )

    @classmethod
    def _reason_priority(cls, reason: str) -> int:
        base = (reason or "").split("(", 1)[0].upper()
        for prefix, prio in cls._TRIGGER_PRIORITY:
            if base == prefix:
                return prio
        return 0

    def on_hardware_alarm(self, status: int, source: str = "HW_ALARM"):
        """Called when camera reports AI track_status > 0 or hardware alarm_status > 0."""
        if status > 0:
            if source == "AI_TRACK":
                prefix = "HW_ALARM_AI"
            elif source == "HW_MOTION":
                prefix = "HW_ALARM_MD"
            else:
                prefix = "HW_ALARM"
            self.trigger_motion(reason=f"{prefix}({status})")

    def set_manual_recording(self, enabled: bool):
        """Toggle manual recording override from UI."""
        self.manual_record = bool(enabled)
        if self.manual_record:
            self.trigger_motion(reason="MANUAL")
        else:
            if self.is_recording:
                self.record_until_mono = time.monotonic() - 0.1
                self._finalize_active_recording()

    def trigger_motion(self, reason: str = "MOTION"):
        now_mono = time.monotonic()
        if (
            not self.is_recording
            and reason == "PFRAME_SPIKE"
            and self.pframe_cooldown_sec > 0
            and self._last_clip_finished_mono > 0
            and (now_mono - self._last_clip_finished_mono) < self.pframe_cooldown_sec
        ):
            return

        now_wall = time.time()
        self.last_motion_mono = now_mono
        self.last_motion_wall = now_wall
        self.record_until_mono = now_mono + self.post_roll_sec

        if not self.is_recording:
            self.last_trigger_reason = reason
            self._start_recording_from_preroll(now_mono, now_wall, reason)
        elif self._reason_priority(reason) >= self._reason_priority(self.last_trigger_reason):
            self.last_trigger_reason = reason

    # 8kHz mono; bytes per second for each payload codec the camera delivers
    _AUDIO_BPS = {"pcmu": 8000, "pcma": 8000, "pcm": 16000}
    _AUDIO_SILENCE = {"pcmu": b"\xff", "pcma": b"\xd5", "pcm": b"\x00"}
    _AUDIO_FFMPEG_FMT = {"pcmu": "mulaw", "pcma": "alaw", "pcm": "s16le"}

    def on_audio_frame(self, data: bytes, codec: str):
        """Keep camera audio next to the video ring so clips can carry sound."""
        if not data or codec not in self._AUDIO_BPS:
            return
        now_mono = time.monotonic()
        if codec != self.audio_codec:
            # Codec switch: older chunks cannot be muxed into the same stream
            self._audio_ring.clear()
            self._active_audio = []
            self.audio_codec = codec
        chunk = (now_mono, data, codec)
        self._audio_ring.append(chunk)
        cutoff = now_mono - (self.pre_roll_sec + 4.0)
        while self._audio_ring and self._audio_ring[0][0] < cutoff:
            self._audio_ring.popleft()
        if self.is_recording:
            self._active_audio.append(chunk)

    @classmethod
    def _build_clip_audio(cls, chunks: List[tuple], start_mono: float, duration_sec: float):
        """Lay audio chunks on the clip timeline (t=0 at the first video frame).

        Each chunk is stamped with its arrival time, i.e. its END. Gaps (lost
        chunks) are filled with silence so sound stays in sync with the picture.
        Returns (codec, bytes) or None when there is too little audio."""
        if not chunks:
            return None
        codec = chunks[-1][2]
        bps = cls._AUDIO_BPS[codec]
        align = 2 if codec == "pcm" else 1
        silence = cls._AUDIO_SILENCE[codec]
        total = int(duration_sec * bps) // align * align
        out = bytearray()
        real = 0
        for ts, data, c in chunks:
            if c != codec:
                continue
            pos = int((ts - len(data) / bps - start_mono) * bps) // align * align
            if pos + len(data) <= 0:
                continue
            if pos < 0:
                data = data[-pos:]
                pos = 0
            if pos > len(out) + bps // 20:          # >50ms hole: pad with silence
                out.extend(silence * (pos - len(out)))
            out.extend(data)
            real += len(data)
            if len(out) >= total:
                break
        if real < bps:                               # under 1s of real audio: skip track
            return None
        del out[total:]
        if len(out) < total:
            out.extend(silence * (total - len(out)))
        return codec, bytes(out)

    def on_video_frame(self, frame_type: int, cam_ts: int, data: bytes):
        """Feed every incoming reassembled H.265/H.264 frame into RAM Ring Buffer, Live Pipe & Detector."""
        if not data:
            return

        now_mono = time.monotonic()
        now_wall = time.time()
        frame_len = len(data)

        # 1. Update telemetry stats (every 1.5 seconds)
        self._stat_bytes += frame_len
        self._stat_frames += 1
        elapsed_stat = now_mono - self._stat_window_start
        if elapsed_stat >= 1.5:
            self.current_kbps = round((self._stat_bytes / 1024.0) / elapsed_stat, 1)
            self.current_fps = round(self._stat_frames / elapsed_stat, 1)
            self._stat_window_start = now_mono
            self._stat_bytes = 0
            self._stat_frames = 0

        item = VideoFrameItem(now_mono, now_wall, frame_type, cam_ts, data)

        # 2. Feed live 25fps decoder if active
        if self._live_proc is not None and (now_mono - self._last_viewer_mono) <= self.LIVE_IDLE_TIMEOUT_SEC:
            if self._live_need_keyframe and not item.is_iframe:
                pass
            else:
                if item.is_iframe:
                    self._live_need_keyframe = False
                    self._last_live_idr_mono = now_mono
                header = self._build_header_prefix() if item.is_iframe else b""
                annexb = self._wrap_annexb(data, header)
                if self._live_in_queue.full():
                    while not self._live_in_queue.empty():
                        try:
                            self._live_in_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    if not item.is_iframe:
                        self._live_need_keyframe = True
                        annexb = b""
                if annexb:
                    try:
                        self._live_in_queue.put_nowait(annexb)
                    except asyncio.QueueFull:
                        pass

        # 3. P-Frame byte-size spike analysis (0% CPU motion detector)
        if not item.is_iframe:
            self._warmup_frames += 1
            if self._pframe_ema <= 0:
                self._pframe_ema = float(frame_len)
            else:
                alpha = 0.02 if frame_len > self._pframe_ema * 1.4 else 0.08
                self._pframe_ema = (1.0 - alpha) * self._pframe_ema + alpha * frame_len

            in_pframe_cooldown = (
                not self.is_recording
                and self.pframe_cooldown_sec > 0
                and self._last_clip_finished_mono > 0
                and (now_mono - self._last_clip_finished_mono) < self.pframe_cooldown_sec
            )
            if in_pframe_cooldown:
                self._pframe_spike_count = 0
            elif self.enable_pframe_trigger and self._warmup_frames > 60:
                threshold = max(self._pframe_ema * self.pframe_sensitivity, self._pframe_ema + 3000, 6500)
                if frame_len > threshold:
                    self._pframe_spike_count += 1
                    if self._pframe_spike_count >= 8:
                        self._pframe_spike_count = 0
                        self.trigger_motion(reason="PFRAME_SPIKE")
                else:
                    if self._pframe_spike_count > 0:
                        self._pframe_spike_count -= 1

        # 4. Maintain Circular RAM Pre-Roll Buffer
        self._ring_buffer.append(item)
        self._ring_bytes += frame_len
        max_keep_sec = self.pre_roll_sec + 4.0
        cutoff_mono = now_mono - max_keep_sec

        while len(self._ring_buffer) > 2 and self._ring_buffer[0].mono_ts < cutoff_mono:
            evicted = self._ring_buffer.popleft()
            self._ring_bytes -= len(evicted.data)

        # 5. Handle active recording state
        if self.manual_record:
            self.record_until_mono = now_mono + self.post_roll_sec

        if self.is_recording:
            self._active_frames.append(item)
            self._active_bytes += frame_len

            clip_duration = now_mono - self.record_start_mono
            if now_mono >= self.record_until_mono or clip_duration >= self.max_clip_sec:
                self._finalize_active_recording()
                if self.manual_record or now_mono < self.record_until_mono:
                    self._start_recording_from_preroll(now_mono, now_wall, self.last_trigger_reason, split_segment=True)

    def _start_recording_from_preroll(
        self, now_mono: float, now_wall: float, reason: str, split_segment: bool = False
    ):
        """Extract the last `pre_roll_sec` from the RAM ring buffer (starting on an I-Frame)."""
        self.is_recording = True
        if split_segment:
            target_mono = now_mono
        else:
            target_mono = now_mono - self.pre_roll_sec
            if self._last_saved_end_mono > 0:
                target_mono = max(target_mono, self._last_saved_end_mono)

        frames_list = list(self._ring_buffer)
        start_idx = 0
        found_iframe = False
        for i, f in enumerate(frames_list):
            if f.is_iframe:
                start_idx = i
                found_iframe = True
                if f.mono_ts >= target_mono:
                    break

        if not found_iframe and frames_list:
            start_idx = 0

        selected = frames_list[start_idx:] if frames_list else []
        self._active_frames = list(selected)
        self._active_bytes = sum(len(f.data) for f in self._active_frames)

        if self._active_frames:
            self.record_start_mono = self._active_frames[0].mono_ts
            self.record_start_wall = self._active_frames[0].wall_ts
        else:
            self.record_start_mono = now_mono
            self.record_start_wall = now_wall
        # Pre-roll audio covering the same span (chunks are stamped at their end)
        self._active_audio = [a for a in self._audio_ring if a[0] >= self.record_start_mono - 0.5]

        log.info(
            "[%s] MOTION RECORD START (%s): pre-roll=%d frames (%.1fs, %.1f KB from RAM)",
            self.room_name,
            reason,
            len(self._active_frames),
            now_mono - self.record_start_mono,
            self._active_bytes / 1024.0,
        )

        # Capture event thumbnail asynchronously
        if self.latest_live_jpeg:
            self._pending_thumbnail = self.latest_live_jpeg
        elif self.snapshot_getter:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._capture_thumbnail())
            except RuntimeError:
                pass

    async def _capture_thumbnail(self):
        try:
            await asyncio.sleep(0.2)
            if self.latest_live_jpeg:
                self._pending_thumbnail = self.latest_live_jpeg
            elif self.snapshot_getter:
                jpg = await self.snapshot_getter()
                if jpg:
                    self._pending_thumbnail = jpg
        except Exception:
            pass

    def _finalize_active_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        self._last_clip_finished_mono = time.monotonic()
        self._pframe_spike_count = 0
        frames = self._active_frames
        self._active_frames = []
        self._active_bytes = 0
        audio_chunks = self._active_audio
        self._active_audio = []
        thumb = self._pending_thumbnail or self.latest_live_jpeg
        self._pending_thumbnail = None

        if len(frames) < 5:
            return
        self._last_saved_end_mono = max(self._last_saved_end_mono, frames[-1].mono_ts)

        start_wall = self.record_start_wall
        end_wall = frames[-1].wall_ts
        duration_sec = max(1.0, frames[-1].mono_ts - frames[0].mono_ts)
        reason = self.last_trigger_reason or "MOTION"

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._mux_frames_to_mp4(
                    frames=frames,
                    start_wall=start_wall,
                    end_wall=end_wall,
                    duration_sec=duration_sec,
                    reason=reason,
                    thumb_bytes=thumb,
                    audio=self._build_clip_audio(audio_chunks, frames[0].mono_ts, duration_sec),
                )
            )
            self._mux_tasks.add(task)
            task.add_done_callback(self._mux_tasks.discard)
        except RuntimeError:
            pass

    async def _mux_frames_to_mp4(
        self,
        frames: List[VideoFrameItem],
        start_wall: float,
        end_wall: float,
        duration_sec: float,
        reason: str,
        thumb_bytes: Optional[bytes],
        audio: Optional[tuple] = None,
    ):
        """Remux raw H.265/H.264 NAL units from RAM directly into an MP4 file on disk (-c:v copy),
        plus the camera's G.711 audio encoded to AAC when `audio` = (codec, bytes) is given."""
        safe_room = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in self.room_name).strip("_") or "Camera"
        dt_start = datetime.datetime.fromtimestamp(start_wall)
        dt_end = datetime.datetime.fromtimestamp(end_wall)
        date_folder = dt_start.strftime("%Y-%m-%d")

        out_dir = os.path.join(self.recordings_dir, safe_room, date_folder)
        os.makedirs(out_dir, exist_ok=True)

        base_name = f"{dt_start.strftime('%Y-%m-%d_%H-%M-%S')}_to_{dt_end.strftime('%H-%M-%S')}_{reason.split('(')[0]}"
        mp4_path = os.path.join(out_dir, f"{base_name}.mp4")
        jpg_path = os.path.join(out_dir, f"{base_name}.jpg")
        meta_path = os.path.join(out_dir, f"{base_name}.json")

        actual_fps = max(10.0, min(30.0, len(frames) / duration_sec))
        fps_str = f"{actual_fps:.2f}"

        header_prefix = self._build_header_prefix()
        chunks = [header_prefix] if header_prefix else []
        for f in frames:
            chunks.append(self._wrap_annexb(f.data, header_prefix if f.is_iframe else b""))

        raw_bitstream = b"".join(chunks)
        is_hevc = (self.codec == "h265")
        fmt = "hevc" if is_hevc else "h264"

        cmd = [
            FFMPEG_BIN,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-r",
            fps_str,
            "-f",
            fmt,
            "-i",
            "pipe:0",
        ]
        audio_tmp = None
        if audio:
            a_codec, a_bytes = audio
            audio_tmp = mp4_path + ".audio.tmp"
            try:
                with open(audio_tmp, "wb") as af:
                    af.write(a_bytes)
                cmd.extend(["-f", self._AUDIO_FFMPEG_FMT[a_codec], "-ar", "8000", "-ac", "1", "-i", audio_tmp,
                            "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "32k"])
            except OSError:
                audio_tmp = None
        cmd.extend(["-c:v", "copy"])
        if is_hevc:
            cmd.extend(["-tag:v", "hvc1"])
        cmd.extend(["-movflags", "+faststart", mp4_path])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(input=raw_bitstream), timeout=15.0)
            if proc.returncode != 0:
                log.warning("[%s] FFmpeg MP4 mux error: %s", self.room_name, stderr.decode(errors="replace")[:200])
                if audio:
                    # Never lose the clip over its sound: retry video-only
                    log.warning("[%s] Retrying clip without audio", self.room_name)
                    return await self._mux_frames_to_mp4(frames, start_wall, end_wall, duration_sec,
                                                          reason, thumb_bytes, audio=None)
                return
        except Exception as e:
            log.warning("[%s] Failed to mux MP4 clip: %s", self.room_name, e)
            return
        finally:
            if audio_tmp:
                try:
                    os.remove(audio_tmp)
                except OSError:
                    pass

        if not os.path.exists(mp4_path) or os.path.getsize(mp4_path) < 8192:
            log.warning("[%s] Discarding corrupt/empty MP4 clip (< 8 KB): %s", self.room_name, os.path.basename(mp4_path))
            try:
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
            except OSError:
                pass
            return

        if thumb_bytes:
            try:
                compact = make_compact_jpeg_thumb(thumb_bytes, max_width=480, quality=74)
                with open(jpg_path, "wb") as jf:
                    jf.write(compact)
            except OSError:
                pass
        else:
            try:
                p_thumb = await asyncio.create_subprocess_exec(
                    FFMPEG_BIN,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    mp4_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:-2",
                    "-q:v",
                    "7",
                    jpg_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(p_thumb.wait(), timeout=5.0)
            except Exception:
                pass

        file_size = os.path.getsize(mp4_path) if os.path.exists(mp4_path) else len(raw_bitstream)
        meta = {
            "id": f"{safe_room}/{date_folder}/{base_name}",
            "uid": self.uid,
            "room_name": self.room_name,
            "safe_room": safe_room,
            "date": date_folder,
            "start_time": dt_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": dt_end.strftime("%Y-%m-%d %H:%M:%S"),
            "start_ts": start_wall,
            "end_ts": end_wall,
            "duration_sec": round(duration_sec, 1),
            "frames": len(frames),
            "fps": round(actual_fps, 1),
            "codec": self.codec,
            "audio": bool(audio_tmp),
            "size_bytes": file_size,
            "size_mb": round(file_size / (1024.0 * 1024.0), 2),
            "reason": reason,
            "video_rel": f"{safe_room}/{date_folder}/{base_name}.mp4",
            "thumb_rel": f"{safe_room}/{date_folder}/{base_name}.jpg" if os.path.exists(jpg_path) else "",
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as mf:
                json.dump(meta, mf, ensure_ascii=False, indent=2)
        except OSError:
            pass

        self.clips_saved_count += 1
        self.last_clip_path = mp4_path
        log.info(
            "[%s] Saved motion clip: %s (%.1fs, %.2f MB, %d frames, audio=%s, reason=%s)",
            self.room_name,
            os.path.basename(mp4_path),
            duration_sec,
            meta["size_mb"],
            len(frames),
            "yes" if meta["audio"] else "no",
            reason,
        )


class StorageManager:
    """Manages disk quota, lists recorded motion clips, and auto-cleans oldest events."""

    def __init__(self, recordings_dir: str, max_storage_gb: float = 50.0, min_free_gb: float = 10.0):
        self.recordings_dir = recordings_dir
        self.max_storage_gb = float(max_storage_gb)
        self.min_free_gb = float(min_free_gb)
        os.makedirs(self.recordings_dir, exist_ok=True)

    def _scan_events(
        self,
        room_filter: Optional[str] = None,
        date_filter: Optional[str] = None,
        trigger_filter: Optional[str] = None,
    ) -> Tuple[List[dict], List[str]]:
        events: List[dict] = []
        avail_dates = set()
        if not os.path.isdir(self.recordings_dir):
            return events, []

        trig_upper = str(trigger_filter or "ALL").upper()
        for root, dirs, files in os.walk(self.recordings_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if room_filter and room_filter != "ALL":
                        if meta.get("safe_room") != room_filter and meta.get("room_name") != room_filter and meta.get("uid") != room_filter:
                            continue
                    if trig_upper != "ALL":
                        if trig_upper not in str(meta.get("reason") or "").upper():
                            continue
                    ev_date = meta.get("date")
                    if ev_date:
                        avail_dates.add(ev_date)
                    if date_filter and date_filter != "ALL" and ev_date != date_filter:
                        continue
                    events.append(meta)
                except Exception:
                    continue

        events.sort(key=lambda x: x.get("start_ts", 0), reverse=True)
        return events, sorted(avail_dates, reverse=True)

    def query_events(
        self,
        room_filter: Optional[str] = None,
        date_filter: Optional[str] = None,
        trigger_filter: Optional[str] = None,
        offset: int = 0,
        limit: int = 24,
    ) -> dict:
        """Return paginated, filtered event metadata plus available dates."""
        events, dates = self._scan_events(
            room_filter=room_filter,
            date_filter=date_filter,
            trigger_filter=trigger_filter,
        )
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(100, int(limit)))
        page = events[safe_offset : safe_offset + safe_limit]
        total = len(events)
        return {
            "events": page,
            "total": total,
            "offset": safe_offset,
            "limit": safe_limit,
            "has_more": (safe_offset + len(page)) < total,
            "dates": dates,
        }

    def list_events(self, room_filter: Optional[str] = None, date_filter: Optional[str] = None, limit: int = 200) -> List[dict]:
        events, _ = self._scan_events(room_filter=room_filter, date_filter=date_filter)
        return events[:limit]

    def get_storage_stats(self) -> dict:
        total_bytes = 0
        clip_count = 0
        if os.path.isdir(self.recordings_dir):
            for root, _dirs, files in os.walk(self.recordings_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        sz = os.path.getsize(fpath)
                        total_bytes += sz
                        if fname.endswith(".mp4"):
                            clip_count += 1
                    except OSError:
                        pass

        disk = shutil.disk_usage(self.recordings_dir)
        return {
            "recordings_dir": self.recordings_dir,
            "used_gb": round(total_bytes / (1024 ** 3), 3),
            "used_mb": round(total_bytes / (1024 ** 2), 2),
            "clip_count": clip_count,
            "max_storage_gb": self.max_storage_gb,
            "min_free_gb": self.min_free_gb,
            "disk_total_gb": round(disk.total / (1024 ** 3), 1),
            "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        }

    def enforce_quota(self) -> int:
        """Delete oldest events if recordings exceed max_storage_gb or disk free < min_free_gb."""
        stats = self.get_storage_stats()
        if stats["used_gb"] <= self.max_storage_gb and stats["disk_free_gb"] >= self.min_free_gb:
            return 0

        all_events = self.list_events(limit=10000)
        all_events.sort(key=lambda x: x.get("start_ts", 0))
        deleted = 0
        for ev in all_events:
            if self.delete_event(ev.get("video_rel", "")):
                deleted += 1
            st = self.get_storage_stats()
            if st["used_gb"] <= self.max_storage_gb and st["disk_free_gb"] >= self.min_free_gb:
                break
        if deleted > 0:
            log.info("Storage quota cleanup: removed %d oldest motion clip(s)", deleted)
        return deleted

    def delete_event(self, video_rel: str) -> bool:
        if not video_rel or ".." in video_rel:
            return False
        mp4_path = os.path.normpath(os.path.join(self.recordings_dir, video_rel))
        if not mp4_path.startswith(os.path.normpath(self.recordings_dir)):
            return False
        base = os.path.splitext(mp4_path)[0]
        removed = False
        for ext in (".mp4", ".jpg", ".json"):
            p = base + ext
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed = True
                except OSError:
                    pass
        cache_root = os.path.realpath(os.path.join(self.recordings_dir, ".h264_cache"))
        cache_mp4 = os.path.realpath(os.path.join(cache_root, video_rel.lstrip("/\\")))
        if cache_mp4.startswith(cache_root + os.sep):
            for p in (cache_mp4, cache_mp4 + ".tmp.mp4"):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return removed
