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

log = logging.getLogger("eye4.recorder")


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
        pre_roll_sec: float = 10.0,
        post_roll_sec: float = 10.0,
        max_clip_sec: float = 300.0,
        pframe_sensitivity: float = 2.25,
        enable_pframe_trigger: bool = True,
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
        self.snapshot_getter = snapshot_getter

        # Circular RAM buffer holding (pre_roll_sec + 4s) of frames
        self._ring_buffer: Deque[VideoFrameItem] = collections.deque()
        self._ring_bytes: int = 0

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

    def set_room_name(self, name: str):
        self.room_name = name.strip() or self.uid[:12]

    def sync_codec_params(self, codec: str, vps: Optional[bytes], sps: Optional[bytes], pps: Optional[bytes]):
        if codec:
            self.codec = codec
        if vps:
            self.vps = vps
        if sps:
            self.sps = sps
        if pps:
            self.pps = pps

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

        if self.latest_live_jpeg is None:
            try:
                await asyncio.wait_for(self._live_frame_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return self.latest_live_jpeg

    def _get_prime_bitstream(self) -> bytes:
        """
        Grab the latest 1-2 GOPs from the RAM Ring Buffer (starting on an I-Frame
        and ending at the latest P-Frame, at least ~80 KB when available) so FFmpeg
        completes stream probing in ~35ms and lands on the exact live reference frame.
        """
        frames = list(self._ring_buffer)
        if not frames:
            return b""

        iframe_indices = [i for i, f in enumerate(frames) if f.is_iframe]
        if not iframe_indices:
            return b""

        # Prefer the latest I-frame; if that slice is < 125KB and an earlier I-frame exists,
        # include the earlier GOP so FFmpeg's stream probe (~115KB) completes immediately.
        chosen_idx = iframe_indices[-1]
        tail_bytes = sum(len(f.data) for f in frames[chosen_idx:])
        if tail_bytes < 125000 and len(iframe_indices) >= 2:
            chosen_idx = iframe_indices[-2]

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
            # Clear stale queue items before priming
            while not self._live_in_queue.empty():
                try:
                    self._live_in_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            prime_bytes = self._get_prime_bitstream()
            is_hevc = (self.codec == "h265")
            fmt = "hevc" if is_hevc else "h264"

            cmd = [
                FFMPEG_BIN,
                "-hide_banner",
                "-loglevel",
                "error",
                "-probesize",
                "262144",
                "-analyzeduration",
                "100000",
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
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._live_proc = proc
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

    # =========================================================================
    # Motion Detection & Pre-Roll / Post-Roll Recording
    # =========================================================================

    def on_hardware_alarm(self, status: int):
        """Called when camera reports AI track_status > 0."""
        if status > 0:
            self.trigger_motion(reason=f"HW_ALARM({status})")

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
        now_wall = time.time()
        self.last_motion_mono = now_mono
        self.last_motion_wall = now_wall
        self.last_trigger_reason = reason
        self.record_until_mono = now_mono + self.post_roll_sec

        if not self.is_recording:
            self._start_recording_from_preroll(now_mono, now_wall, reason)

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
            header = self._build_header_prefix() if item.is_iframe else b""
            annexb = self._wrap_annexb(data, header)
            if self._live_in_queue.full():
                try:
                    self._live_in_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
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

            if self.enable_pframe_trigger and self._warmup_frames > 60:
                threshold = max(self._pframe_ema * self.pframe_sensitivity, self._pframe_ema + 2200, 4800)
                if frame_len > threshold:
                    self._pframe_spike_count += 1
                    if self._pframe_spike_count >= 4:
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
        target_mono = now_mono if split_segment else (now_mono - self.pre_roll_sec)

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
        frames = self._active_frames
        self._active_frames = []
        self._active_bytes = 0
        thumb = self._pending_thumbnail or self.latest_live_jpeg
        self._pending_thumbnail = None

        if len(frames) < 5:
            return

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
    ):
        """Remux raw H.265/H.264 NAL units from RAM directly into an MP4 file on disk (-c:v copy)."""
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
            "-c:v",
            "copy",
        ]
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
                return
        except Exception as e:
            log.warning("[%s] Failed to mux MP4 clip: %s", self.room_name, e)
            return

        if thumb_bytes:
            try:
                with open(jpg_path, "wb") as jf:
                    jf.write(thumb_bytes)
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
                    "-q:v",
                    "5",
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
            "[%s] Saved motion clip: %s (%.1fs, %.2f MB, %d frames, reason=%s)",
            self.room_name,
            os.path.basename(mp4_path),
            duration_sec,
            meta["size_mb"],
            len(frames),
            reason,
        )


class StorageManager:
    """Manages disk quota, lists recorded motion clips, and auto-cleans oldest events."""

    def __init__(self, recordings_dir: str, max_storage_gb: float = 50.0, min_free_gb: float = 10.0):
        self.recordings_dir = recordings_dir
        self.max_storage_gb = float(max_storage_gb)
        self.min_free_gb = float(min_free_gb)
        os.makedirs(self.recordings_dir, exist_ok=True)

    def list_events(self, room_filter: Optional[str] = None, date_filter: Optional[str] = None, limit: int = 200) -> List[dict]:
        events = []
        if not os.path.isdir(self.recordings_dir):
            return events

        for root, _dirs, files in os.walk(self.recordings_dir):
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
                    if date_filter and date_filter != "ALL":
                        if meta.get("date") != date_filter:
                            continue
                    events.append(meta)
                except Exception:
                    continue

        events.sort(key=lambda x: x.get("start_ts", 0), reverse=True)
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
        return removed
