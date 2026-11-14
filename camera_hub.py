#!/usr/bin/env python3
"""
C22S Multi-Camera Hub & Smart Motion NVR (All-in-One Engine + 25fps Web Dashboard)

Runs in a single async Python process:
1. Auto-discovers Eye4/VStarcam C22S cameras over LAN UDP PPPP
2. Auto-provisions Hardware OSD Timestamp + Hardware Motion Alarm on each camera
3. Maintains a 12s RAM Circular Pre-Roll Buffer per camera (0 disk writes when static)
4. Records 10s pre-roll + motion + 10s post-roll directly to H.265 MP4 (-c:v copy, 0% encode CPU)
5. Serves a Real-Time 25 FPS Dark-Mode Web Dashboard at http://127.0.0.1:8080:
   - Single Multiplexed Binary WebSocket (`/ws/streams`) pushes 25 FPS live frames for up to 16 cameras
     without hitting the browser's 6-connection HTTP/1.1 limit!
   - On-demand FFmpeg live decoder automatically shuts down after 10s when no browser is open (0% background CPU).
"""

import argparse
import asyncio
import collections
import datetime
import json
import logging
import os
import time
from typing import Dict, Optional, Set

from aiohttp import WSMsgType, web

import eye4_rtsp_proxy as proxy
import live_video
from motion_recorder import CameraMotionRecorder, StorageManager, make_compact_jpeg_thumb

log = logging.getLogger("eye4.hub")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
STATIC_DIR = os.path.join(BASE_DIR, "static")
STATIC_ASSET_VER = "v10"



import re
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False



import base64
import subprocess
import urllib.parse
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False



import io
import math
import socket
import wave
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# =============================================================================
# VStarcam / Eye4 Official Sonic Wave Wi-Fi Provisioning Engine (voice.jar port)
# =============================================================================
# Reverse-engineered 1:1 from VStarcam's official `onekeywifi_android/libs/voice.jar`
# (`voice.encoder.DataEncoder`, `voice.CRC`, `voice.encoder.VoicePlayer`, `voice.encoder.SignalGenerator`).
# Uses 19 FSK frequencies from 6500 Hz to 10100 Hz (step 200 Hz), 65ms tone duration,
# 20% linear attack/decay envelope, and CRC-16-CCITT (poly 0x1021, init 0xFFFF).

VSTARCAM_HEX_CHARS = "0123456789abcdef"
VSTARCAM_MAX_APART_LEN = 32
VSTARCAM_SONIC_FREQS = [6500 + i * 200 for i in range(19)]

_CRC16_TAB = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if (_c & 0x8000) else (_c << 1) & 0xFFFF
    _CRC16_TAB.append(_c)


def _vs_is_digit(b: int) -> bool:
    return 48 <= b <= 57


def _vs_is_lower(b: int) -> bool:
    return 97 <= b <= 122


def _vs_is_upper(b: int) -> bool:
    return 65 <= b <= 90


def _vs_is_64(b: int) -> bool:
    return _vs_is_lower(b) or _vs_is_upper(b) or _vs_is_digit(b) or b in (95, 45)


def _vs_char64_to_int(b: int) -> int:
    if 65 <= b <= 90:
        return b - 65
    if 97 <= b <= 122:
        return b - 97 + 26
    if 48 <= b <= 57:
        return b - 48 + 52
    if b == 95:
        return 62
    if b == 45:
        return 63
    return -1


def _vs_bit_set(b: int, start: int, end: int, val: int) -> int:
    t1 = (255 >> start) if start > 0 else 255
    t2 = (255 << (8 - end)) if end < 8 else 255
    mask = (t1 & t2) & 0xFF
    return ((b & (~mask & 0xFF)) | ((val << (8 - end)) & mask)) & 0xFF


def _vs_bits_set(arr: bytearray, start_bit: int, end_bit: int, val: int) -> None:
    idx1 = start_bit // 8
    idx2 = end_bit // 8
    if idx1 == idx2:
        arr[idx1] = _vs_bit_set(arr[idx1], start_bit % 8, end_bit % 8, val)
    else:
        r1 = start_bit % 8
        r2 = end_bit % 8
        v1 = (val >> r2) & 0xFF
        v2 = val & (255 >> (8 - r2))
        arr[idx1] = _vs_bit_set(arr[idx1], r1, 8, v1)
        arr[idx2] = _vs_bit_set(arr[idx2], 0, r2, v2)


def _vs_chars_to_hex(src: bytes, start: int, hex_count: int, dst: bytearray, dst_offset: int) -> None:
    for i in range(hex_count):
        b = src[start + (i // 2)]
        if i % 2 == 0:
            dst[dst_offset + i] = ord(VSTARCAM_HEX_CHARS[(b >> 4) & 0xF])
        else:
            dst[dst_offset + i] = ord(VSTARCAM_HEX_CHARS[b & 0xF])


def _vs_digit2hex(src: bytes, start: int, length: int, dst: bytearray, dst_offset: int) -> int:
    for i in range(length):
        dst[dst_offset + i] = src[start + i]
    return length


def _vs_lower2hex(src: bytes, start: int, length: int, dst: bytearray, dst_offset: int) -> int:
    bits = length * 5
    hc = (bits // 4) + (1 if bits % 4 > 0 else 0)
    tmp = bytearray(length)
    for i in range(length):
        _vs_bits_set(tmp, i * 5, (i + 1) * 5, src[start + i] - 97)
    _vs_chars_to_hex(tmp, 0, hc, dst, dst_offset)
    return hc


def _vs_upper2hex(src: bytes, start: int, length: int, dst: bytearray, dst_offset: int) -> int:
    bits = length * 5
    hc = (bits // 4) + (1 if bits % 4 > 0 else 0)
    tmp = bytearray(length)
    for i in range(length):
        _vs_bits_set(tmp, i * 5, (i + 1) * 5, src[start + i] - 65)
    _vs_chars_to_hex(tmp, 0, hc, dst, dst_offset)
    return hc


def _vs_char64_to_hex(src: bytes, start: int, length: int, dst: bytearray, dst_offset: int) -> int:
    bits = length * 6
    hc = (bits // 4) + (1 if bits % 4 > 0 else 0)
    tmp = bytearray(length)
    for i in range(length):
        _vs_bits_set(tmp, i * 6, (i + 1) * 6, _vs_char64_to_int(src[start + i]))
    _vs_chars_to_hex(tmp, 0, hc, dst, dst_offset)
    return hc


def _vs_char256_to_hex(src: bytes, start: int, length: int, dst: bytearray, dst_offset: int) -> int:
    hc = (length * 8) // 4
    _vs_chars_to_hex(src, start, hc, dst, dst_offset)
    return hc


def _vs_encode_type(src: bytes, length: int):
    v3, v4, v5, v7, v8, v9, v10 = 1, 0, 0, 0, 1, 1, 1
    v15, v16, v17 = 0, 0, 0
    for i in range(length):
        b = src[i]
        d, l, u, c64 = _vs_is_digit(b), _vs_is_lower(b), _vs_is_upper(b), _vs_is_64(b)
        if v3 and not d:
            v3 = 0
            if i > 0:
                v7 = 1 if c64 else 0
                v17 = i
        if v7 and not c64:
            v7 = 0
        if v8 and not l:
            v8 = 0
            if i > 0:
                v4 = 1 if d else 0
                v15 = i
        if v4 and not d:
            v4 = 0
        if v10 and not u:
            v10 = 0
        if v9 and not c64:
            v9 = 0
        if v9:
            if i > 0:
                if not v5 and d:
                    v16 = i
                v5 = 1 if d else 0
        else:
            v5 = 0
    if v3:
        return 0, 0
    if v8:
        return 5, 0
    if v10:
        return 7, 0
    if v4 and v15 <= VSTARCAM_MAX_APART_LEN:
        return 1, v15
    if v5 and v16 <= VSTARCAM_MAX_APART_LEN:
        return 2, v16
    if v7:
        if v17 >= VSTARCAM_MAX_APART_LEN:
            v17 = VSTARCAM_MAX_APART_LEN
        return 4, v17
    if v9:
        return 6, 0
    return 3, 0


def _vs_type2hex(t: int, apart: int, dst: bytearray, dst_offset: int) -> int:
    dst[dst_offset] = (t << 1) & 0xFF
    hc = 2 if t in (1, 2, 4) else 1
    if hc > 1:
        am1 = apart - 1
        dst[dst_offset] |= (am1 & 16) >> 4
        dst[dst_offset + 1] = ord(VSTARCAM_HEX_CHARS[am1 & 15])
    dst[dst_offset] = ord(VSTARCAM_HEX_CHARS[dst[dst_offset]])
    return hc


def _vs_encode_data(src: bytes, dst: bytearray, dst_offset: int) -> int:
    t, apart = _vs_encode_type(src, len(src))
    n = _vs_type2hex(t, apart, dst, dst_offset)
    if t == 1:
        n += _vs_lower2hex(src, 0, apart, dst, dst_offset + n)
        n += _vs_digit2hex(src, apart, len(src) - apart, dst, dst_offset + n)
    elif t == 2:
        n += _vs_char64_to_hex(src, 0, apart, dst, dst_offset + n)
        n += _vs_digit2hex(src, apart, len(src) - apart, dst, dst_offset + n)
    elif t == 4:
        n += _vs_digit2hex(src, 0, apart, dst, dst_offset + n)
        n += _vs_char64_to_hex(src, apart, len(src) - apart, dst, dst_offset + n)
    elif t == 0:
        n += _vs_digit2hex(src, 0, len(src), dst, dst_offset + n)
    elif t == 3:
        n += _vs_char256_to_hex(src, 0, len(src), dst, dst_offset + n)
    elif t == 5:
        n += _vs_lower2hex(src, 0, len(src), dst, dst_offset + n)
    elif t == 6:
        n += _vs_char64_to_hex(src, 0, len(src), dst, dst_offset + n)
    elif t == 7:
        n += _vs_upper2hex(src, 0, len(src), dst, dst_offset + n)
    return n


def vstarcam_encode_mac_wifi(mac_bytes: bytes, pwd: str) -> str:
    """`DataEncoder.encodeMacWiFi(byte[] mac, String pwd)`.

    Layout: one nibble (len(mac) - 1), the MAC bytes as plain hex, then the password as a
    typed data field. Verified against a recording of the real Eye4 app, whose payload for
    BSSID suffix 00:1e decoded as "1" + "001e" + typed password with a valid CRC.
    (The generic voiceRecog device decoder uses a different two-nibble layout; VStarcam does not.)
    """
    try:
        b = pwd.encode("gbk")
    except Exception:
        b = pwd.encode("utf-8")
    dst = bytearray((len(mac_bytes) + len(b)) * 2 + 12)
    dst[0] = ord(VSTARCAM_HEX_CHARS[(len(mac_bytes) - 1) & 15])
    n = 1
    n += _vs_char256_to_hex(mac_bytes, 0, len(mac_bytes), dst, n)
    n += _vs_encode_data(b, dst, n)
    return dst[:n].decode("ascii")


def vstarcam_encode_ssid_wifi(ssid: str, pwd: str) -> str:
    """Bit-exact port of `DataEncoder.encodeSSIDWiFi(String ssid, String pwd)`."""
    try:
        b_ssid = ssid.encode("gbk")
    except Exception:
        b_ssid = ssid.encode("utf-8")
    try:
        b_pwd = pwd.encode("gbk")
    except Exception:
        b_pwd = pwd.encode("utf-8")
    dst = bytearray((len(b_ssid) + len(b_pwd)) * 2 + 12)
    sm1 = max(0, len(b_ssid) - 1)
    dst[0] = ord(VSTARCAM_HEX_CHARS[2 | ((sm1 & 16) >> 4)])
    dst[1] = ord(VSTARCAM_HEX_CHARS[sm1 & 15])
    n = 2
    n += _vs_encode_data(b_ssid, dst, n)
    n += _vs_encode_data(b_pwd, dst, n)
    return dst[:n].decode("ascii")


def vstarcam_crc16(int_arr) -> int:
    """Bit-exact port of `voice.CRC.calc_crc16(int[] data, int len)`."""
    crc = 0xFFFF
    for val in int_arr:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TAB[((crc >> 8) ^ (val & 0xFF)) & 0xFF]
    return crc


def vstarcam_text_to_codes(hex_str: str) -> list:
    """Hex payload -> tone indexes, framed exactly as the Eye4 app plays it.

    Frame measured from a recording of the Eye4 app: 18, 9, 0, <data + CRC16>, 18.
    Each nibble n is tone n+1; tone 17 means "same nibble as the previous one".
    """
    data = [VSTARCAM_HEX_CHARS.index(c) for c in hex_str.lower()]
    crc = vstarcam_crc16(data)
    full = data + [(crc >> 12) & 0xF, (crc >> 8) & 0xF, (crc >> 4) & 0xF, crc & 0xF]
    codes = [18, 9, 0]
    prev = -1
    for val in full:
        if val != prev:
            codes.append(val + 1)
            prev = val
        else:
            codes.append(17)
            prev = 17
    codes.append(18)
    return codes


def generate_vstarcam_sonic_wav(ssid: str, password: str, bssid: str = "", mode: str = "combo") -> dict:
    """
    Synthesize 44.1 kHz 16-bit mono PCM WAV audio for VStarcam Sonic Wi-Fi Provisioning.
    Solves the Wi-Fi 7 MLD / 5GHz phone BSSID bug by explicitly encoding the 2.4GHz BSSID suffix
    (`encodeMacWiFi`) when known, or universally encoding the full SSID string (`encodeSSIDWiFi`)
    when provisioning a camera at a remote site over the Internet.
    """
    pwd_clean = password.strip()
    clean_bssid = re.sub(r"[^0-9a-fA-F]", "", bssid or "").lower()

    # Only use the Server's local Wi-Fi scan if `ssid` actually matches one of the Server's visible networks.
    # When the Server is hosted remotely on the Internet, a remote phone's Wi-Fi SSID will not match the Server's
    # local Wi-Fi card, so we leave `clean_bssid = ""` and encode full SSID + Password (`ssid_codes`), which works universally!
    if mode != "ssid":
        info = scan_host_wifi_info()
        matched_host_bssid = ""
        for net in info.get("networks", []):
            if net.get("ssid") == ssid:
                matched_host_bssid = net.get("bssid_24") or net.get("bssid_any") or ""
                break
        if not matched_host_bssid and info.get("current_ssid") == ssid:
            matched_host_bssid = info.get("current_bssid") or ""

        if len(clean_bssid) < 4:
            clean_bssid = matched_host_bssid
        elif info.get("current_bssid") and clean_bssid == info.get("current_bssid").lower() and info.get("current_ssid") != ssid and not matched_host_bssid:
            # Guard against stale server BSSID accidentally submitted with a remote site's SSID
            clean_bssid = ""
    elif len(clean_bssid) < 4:
        clean_bssid = ""

    bssid_suffix_hex = clean_bssid[-4:] if len(clean_bssid) >= 4 else ""
    mac_hex_payload = ""
    mac_codes = []
    if len(bssid_suffix_hex) == 4:
        mac_bytes = bytes.fromhex(bssid_suffix_hex)
        mac_hex_payload = vstarcam_encode_mac_wifi(mac_bytes, pwd_clean)
        mac_codes = vstarcam_text_to_codes(mac_hex_payload)

    ssid_hex_payload = vstarcam_encode_ssid_wifi(ssid, pwd_clean)
    ssid_codes = vstarcam_text_to_codes(ssid_hex_payload)

    # Select burst sequence
    burst_list = []
    if mode == "mac" and mac_codes:
        burst_list = [mac_codes] * 5
    elif mode == "ssid" or not mac_codes:
        burst_list = [ssid_codes] * 5
    else:
        # "combo": 3 bursts of 2.4GHz MAC WiFi (fastest, 1.8s each) + 2 bursts of full SSID WiFi
        burst_list = [mac_codes, mac_codes, ssid_codes, mac_codes, ssid_codes]

    sample_rate = 44100
    tone_ms = 65
    pause_ms = 600  # Eye4 leaves ~600 ms of silence between repeats
    total_samples = (tone_ms * sample_rate) // 1000
    ramp_samples = int(total_samples * 0.2)
    max_amp = 32767
    step_amp = max_amp // (ramp_samples + 1)

    t_idx = np.arange(total_samples, dtype=np.float64)
    env = np.full(total_samples, max_amp, dtype=np.float64)
    env[:ramp_samples] = t_idx[:ramp_samples] * step_amp
    tail_mask = t_idx > (total_samples - ramp_samples + 1)
    env[tail_mask] = (total_samples - t_idx[tail_mask]) * step_amp

    # Pre-render the 19 frequency tone buffers
    tone_pcm = {}
    for code_idx, freq in enumerate(VSTARCAM_SONIC_FREQS):
        omega = (freq / float(sample_rate)) * 2.0 * math.pi
        samples = (np.sin(t_idx * omega) * env).astype(np.int16)
        tone_pcm[code_idx] = samples.tobytes()

    lead_silence = np.zeros((250 * sample_rate) // 1000, dtype=np.int16).tobytes()
    pause_silence = np.zeros((pause_ms * sample_rate) // 1000, dtype=np.int16).tobytes()

    pcm_parts = [lead_silence]
    for b_idx, codes in enumerate(burst_list):
        for c in codes:
            pcm_parts.append(tone_pcm[c])
        if b_idx < len(burst_list) - 1:
            pcm_parts.append(pause_silence)
        else:
            # The <audio loop> restart adds the 250 ms lead-in, so the seam is also one full pause.
            pcm_parts.append(np.zeros(((pause_ms - 250) * sample_rate) // 1000, dtype=np.int16).tobytes())

    full_pcm = b"".join(pcm_parts)
    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(full_pcm)
    wav_bytes = wav_io.getvalue()
    duration_sec = round(len(full_pcm) / (2.0 * sample_rate), 1)

    return {
        "wav_bytes": wav_bytes,
        "duration_sec": duration_sec,
        "bssid_24": clean_bssid,
        "bssid_suffix": bssid_suffix_hex,
        "mac_hex_payload": mac_hex_payload,
        "ssid_hex_payload": ssid_hex_payload,
        "mode": mode if mac_codes else "ssid",
    }


def get_host_lan_ip(seed_ip: str = "192.168.31.90") -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((seed_ip or "8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def scan_host_wifi_info() -> dict:
    """
    Scan the Windows host's currently connected Wi-Fi SSID, 2.4GHz BSSID, saved password,
    and list of nearby visible Wi-Fi networks using netsh wlan.
    """
    current_ssid = ""
    current_bssid = ""
    saved_password = ""
    networks = []
    camera_hotspots = []

    try:
        out_if = subprocess.check_output(
            ["netsh", "wlan", "show", "interfaces"],
            encoding="utf-8",
            errors="ignore",
            timeout=3,
        )
        for line in out_if.splitlines():
            m_s = re.match(r"^\s*SSID\s*:\s*(.+)$", line)
            if m_s and not current_ssid:
                current_ssid = m_s.group(1).strip()
            m_b = re.match(r"^\s*(?:AP\s+)?BSSID\s*:\s*([0-9a-fA-F:]{11,17})", line)
            if m_b and not current_bssid:
                current_bssid = m_b.group(1).strip().replace(":", "").lower()
    except Exception:
        pass

    try:
        out_net = subprocess.check_output(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            encoding="utf-8",
            errors="ignore",
            timeout=4,
        )
        cur_net = None
        for line in out_net.splitlines():
            m_ssid = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*)$", line)
            if m_ssid:
                name = m_ssid.group(1).strip()
                if name:
                    cur_net = {"ssid": name, "bssid_24": "", "bssid_any": "", "signal": "", "band": ""}
                    networks.append(cur_net)
                    up_name = name.upper()
                    if any(up_name.startswith(p) for p in ("@MC", "OKAM", "VST", "IPC", "AYS", "CARE", "BELL", "CAM")):
                        camera_hotspots.append(name)
                else:
                    cur_net = None
                continue
            if cur_net is not None:
                m_bssid = re.match(r"^\s*BSSID\s+\d+\s*:\s*([0-9a-fA-F:]{11,17})", line)
                if m_bssid:
                    mac_clean = m_bssid.group(1).strip().replace(":", "").lower()
                    cur_net["_last_mac"] = mac_clean
                    if not cur_net["bssid_any"]:
                        cur_net["bssid_any"] = mac_clean
                m_sig = re.match(r"^\s*Signal\s*:\s*(.+)$", line)
                if m_sig and not cur_net["signal"]:
                    cur_net["signal"] = m_sig.group(1).strip()
                m_band = re.match(r"^\s*Band\s*:\s*(.+)$", line)
                if m_band:
                    band_str = m_band.group(1).strip()
                    cur_net["band"] = band_str
                    if "2.4" in band_str and cur_net.get("_last_mac"):
                        cur_net["bssid_24"] = cur_net["_last_mac"]
    except Exception:
        pass

    # Prefer the 2.4GHz BSSID for the current SSID if available
    for n in networks:
        n.pop("_last_mac", None)
        if n["ssid"] == current_ssid:
            if n.get("bssid_24"):
                current_bssid = n["bssid_24"]
            elif not current_bssid and n.get("bssid_any"):
                current_bssid = n["bssid_any"]

    if current_ssid:
        try:
            out_prof = subprocess.check_output(
                ["netsh", "wlan", "show", "profile", f"name={current_ssid}", "key=clear"],
                encoding="utf-8",
                errors="ignore",
                timeout=3,
            )
            m_key = re.search(r"(?:Key Content|Nội dung khóa)\s*:\s*(.+)$", out_prof, re.M | re.I)
            if m_key:
                saved_password = m_key.group(1).strip()
        except Exception:
            pass

    return {
        "current_ssid": current_ssid,
        "current_bssid": current_bssid,
        "saved_password": saved_password,
        "networks": networks,
        "camera_hotspots": camera_hotspots,
    }


def generate_wifi_qr_png_data_url(ssid: str, password: str, bssid: str = "", fmt: str = "eye4") -> dict:
    """
    Generate a high-contrast QR Code PNG Data URL for optical camera lens Wi-Fi provisioning.
    Supports:
    - 'eye4': {"BS":"<bssid>","P":"<password>","U":"888888","RS":"<ssid>"} (VStarcam / Eye4 / O-Kam standard)
    - 'okam_compact': {"s":"<ssid>","p":"<password>"}
    - 'zxing': WIFI:T:WPA;S:<ssid>;P:<password>;;
    """
    if not HAS_QRCODE or not HAS_CV2:
        raise RuntimeError("Thiếu thư viện qrcode hoặc opencv-python")

    clean_bssid = (bssid or "").replace(":", "").replace("-", "").lower()
    if fmt == "eye4":
        payload_dict = {
            "BS": clean_bssid or "000000000000",
            "P": password,
            "U": "888888",
            "RS": ssid,
        }
        qr_text = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
    elif fmt == "okam_compact":
        qr_text = json.dumps({"s": ssid, "p": password}, separators=(",", ":"), ensure_ascii=False)
    else:
        qr_text = f"WIFI:T:WPA;S:{ssid};P:{password};;"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_text)
    qr.make(fit=True)
    mat = np.array(qr.get_matrix(), dtype=np.uint8)
    img = np.where(mat == 1, 0, 255).astype(np.uint8)
    scale = 10
    img_big = np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)
    ok, buf = cv2.imencode(".png", img_big)
    if not ok:
        raise RuntimeError("Không thể nén ảnh PNG cho mã QR")
    b64 = base64.b64encode(bytes(buf)).decode("ascii")
    return {
        "qr_text": qr_text,
        "data_url": f"data:image/png;base64,{b64}",
        "format": fmt,
    }


def parse_qr_or_did_text(raw_text: str) -> dict:
    """
    Parse either a full VStarcam QR payload like:
      www.vstarcam.com/s/?m=83{"ACT":"Add","ID":"VSTJ082279XTJDT","DT":"CS22S","WiFi":"0"}
    or a plain sticker UID string like:
      VSTJ-082279-XTJDT / UID: VSTJ-082279-XTJDT / VSTJ082279XTJDT
    Returns dict with device_id, formatted_did, model, wifi_flag, uid_hex, raw_qr.
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Nội dung QR / UID trống")

    model = "C22S"
    wifi_flag = "0"
    did_candidate = ""

    # 1. Check embedded JSON inside QR URL (e.g. www.vstarcam.com/s/?m=83{...})
    j_start = text.find("{")
    j_end = text.rfind("}")
    if 0 <= j_start < j_end:
        try:
            j_obj = json.loads(text[j_start : j_end + 1])
            if isinstance(j_obj, dict):
                did_candidate = str(
                    j_obj.get("ID") or j_obj.get("id") or j_obj.get("UID") or j_obj.get("did") or ""
                ).strip()
                model = str(j_obj.get("DT") or j_obj.get("model") or "CS22S").strip()
                wifi_flag = str(j_obj.get("WiFi", "0"))
        except Exception:
            pass

    # 2. Fallback regex for VStarcam UID pattern (e.g. VSTJ-082279-XTJDT or VSTJ082279XTJDT)
    if not did_candidate:
        m = re.search(r"([A-Za-z]{3,5})-?(\d{5,8})-?([A-Za-z0-9]{4,8})", text)
        if m:
            did_candidate = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        elif len(text) == 40 and all(c in "0123456789abcdefABCDEF" for c in text):
            uid_hex = text.lower()
            c_did, f_did = proxy.pppp_uid_hex_to_did(uid_hex)
            return {
                "raw_qr": text,
                "device_id": c_did or uid_hex[:12].upper(),
                "formatted_did": f_did or uid_hex[:12].upper(),
                "model": model,
                "wifi_flag": wifi_flag,
                "uid_hex": uid_hex,
            }

    if not did_candidate:
        raise ValueError(f"Không tìm thấy mã UID VStarcam hợp lệ trong chuỗi: {text}")

    uid_hex = proxy.did_to_pppp_uid_hex(did_candidate)
    compact_did, formatted_did = proxy.pppp_uid_hex_to_did(uid_hex)
    if not compact_did:
        clean = did_candidate.upper().replace("-", "").replace(" ", "")
        m2 = re.match(r"^([A-Z]+)(\d+)([A-Z0-9]+)$", clean)
        if m2:
            compact_did = f"{m2.group(1)}{int(m2.group(2)):06d}{m2.group(3)}"
            formatted_did = f"{m2.group(1)}-{int(m2.group(2)):06d}-{m2.group(3)}"
        else:
            compact_did = clean
            formatted_did = clean

    return {
        "raw_qr": text,
        "device_id": compact_did,
        "formatted_did": formatted_did,
        "model": model,
        "wifi_flag": wifi_flag,
        "uid_hex": uid_hex,
    }


def decode_camera_qr_image(image_bytes: bytes) -> dict:
    """
    Multi-scale, multi-crop, multi-threshold OpenCV QR Code + UID sticker decoder.
    Easily decodes hand-held smartphone photos of the small QR sticker on the bottom of C22S cameras.
    """
    if not HAS_CV2:
        raise RuntimeError("Thư viện opencv-python (cv2) chưa được cài đặt")

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Không thể đọc định dạng file ảnh")

    det = cv2.QRCodeDetector()
    h, w = img.shape[:2]

    # Candidate regions: full image + strategic crops (center, right half, quadrants, inner grid)
    regions = [
        img,
        img[int(h * 0.25) : int(h * 0.75), int(w * 0.25) : int(w * 0.85)],
        img[int(h * 0.30) : int(h * 0.70), int(w * 0.45) : int(w * 0.88)],
        img[int(h * 0.15) : int(h * 0.85), int(w * 0.15) : int(w * 0.85)],
        img[0 : int(h * 0.65), int(w * 0.35) : w],
        img[int(h * 0.35) : h, int(w * 0.35) : w],
        img[0 : int(h * 0.65), 0 : int(w * 0.65)],
        img[int(h * 0.35) : h, 0 : int(w * 0.65)],
    ]

    decoded_text = ""
    for reg in regions:
        if reg is None or reg.size == 0:
            continue
        try:
            val, _, _ = det.detectAndDecode(reg)
            if val:
                decoded_text = val
                break
        except Exception:
            pass

        gray = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
        try:
            val, _, _ = det.detectAndDecode(gray)
            if val:
                decoded_text = val
                break
        except Exception:
            pass

        # Try CLAHE & multiple binary thresholds for glossy camera stickers
        try:
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
            val, _, _ = det.detectAndDecode(clahe)
            if val:
                decoded_text = val
                break
        except Exception:
            pass

        for thresh in (95, 115, 135, 155, 175):
            try:
                _, bw = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
                val, _, _ = det.detectAndDecode(bw)
                if val:
                    decoded_text = val
                    break
            except Exception:
                pass
        if decoded_text:
            break

        # Try 1.6x upscale on small crops
        rh, rw = gray.shape[:2]
        if max(rh, rw) < 900:
            try:
                up = cv2.resize(gray, (int(rw * 1.6), int(rh * 1.6)), interpolation=cv2.INTER_CUBIC)
                val, _, _ = det.detectAndDecode(up)
                if val:
                    decoded_text = val
                    break
                _, bw_up = cv2.threshold(up, 125, 255, cv2.THRESH_BINARY)
                val, _, _ = det.detectAndDecode(bw_up)
                if val:
                    decoded_text = val
                    break
            except Exception:
                pass

    if not decoded_text:
        raise ValueError(
            "Không nhận diện được mã QR trong ảnh. Hãy chụp rõ nét vùng tem QR dưới đáy camera hoặc nhập trực tiếp mã UID (VD: VSTJ-082279-XTJDT)."
        )

    return parse_qr_or_did_text(decoded_text)


class CameraHubApp:
    def __init__(
        self,
        config: dict,
        config_path: str,
        recordings_dir: str,
        target_ip: Optional[str] = None,
        web_host: str = "0.0.0.0",
        web_port: int = 8080,
    ):
        self.config = config
        self.config_path = config_path
        self.recordings_dir = recordings_dir
        self.target_ip = target_ip
        self.web_host = web_host
        self.web_port = web_port

        self.sessions: Dict[str, proxy.CameraSession] = {}
        self.recorders: Dict[str, CameraMotionRecorder] = {}
        self.wifi_jobs: Dict[str, dict] = {}   # uid -> verified Wi-Fi switch progress
        self.live_video: Dict[str, "live_video.CameraLiveVideo"] = {}  # uid -> WebCodecs fan-out
        self.ws_clients: Set[web.WebSocketResponse] = set()

        # Hub NVR settings (persisted in YAML config under 'hub')
        hub_cfg = self.config.get("hub", {}) if isinstance(self.config.get("hub"), dict) else {}
        self.pre_roll_sec: float = float(hub_cfg.get("pre_roll_sec", 6.0))
        self.post_roll_sec: float = float(hub_cfg.get("post_roll_sec", 6.0))
        self.pframe_sensitivity: float = float(hub_cfg.get("pframe_sensitivity", 2.8))
        self.pframe_cooldown_sec: float = float(hub_cfg.get("pframe_cooldown_sec", 20.0))
        self.enable_pframe_trigger: bool = bool(hub_cfg.get("enable_pframe_trigger", True))
        self.auto_25fps: bool = bool(hub_cfg.get("auto_25fps", True))
        self.auto_firmware_ai: bool = bool(hub_cfg.get("auto_firmware_ai", True))
        self.max_storage_gb: float = float(hub_cfg.get("max_storage_gb", 50.0))
        self.min_free_gb: float = float(hub_cfg.get("min_free_gb", 10.0))

        self.storage = StorageManager(
            recordings_dir=self.recordings_dir,
            max_storage_gb=self.max_storage_gb,
            min_free_gb=self.min_free_gb,
        )
        self._running = False
        self._discovery_task: Optional[asyncio.Task] = None
        self._quota_task: Optional[asyncio.Task] = None
        self._fps_guard_task: Optional[asyncio.Task] = None
        self._fps_checked_protos: Dict[str, int] = {}
        self._ai_checked_protos: Dict[str, int] = {}
        self._ptz_stop_tasks: Dict[str, asyncio.Task] = {}
        self._ptz_last_move: Dict[str, tuple] = {}   # uid -> (decoder_control code, started_at)
        self._thumb_cache: collections.OrderedDict = collections.OrderedDict()
        self._h264_locks: Dict[str, asyncio.Lock] = {}

    def _save_hub_config(self):
        self.config["hub"] = {
            "pre_roll_sec": self.pre_roll_sec,
            "post_roll_sec": self.post_roll_sec,
            "pframe_sensitivity": self.pframe_sensitivity,
            "pframe_cooldown_sec": self.pframe_cooldown_sec,
            "enable_pframe_trigger": self.enable_pframe_trigger,
            "auto_25fps": self.auto_25fps,
            "auto_firmware_ai": self.auto_firmware_ai,
            "max_storage_gb": self.max_storage_gb,
            "min_free_gb": self.min_free_gb,
        }
        try:
            if not proxy.HAS_YAML:
                return
            import yaml
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            log.warning("Could not save hub config: %s", e)

    def _set_camera_cfg_field(self, uid: str, **fields):
        """Persist per-camera override fields into YAML config."""
        cams_cfg = self.config.setdefault("cameras", {})
        cur_cfg = cams_cfg.get(uid)
        if not isinstance(cur_cfg, dict):
            cur_cfg = {"port": int(cur_cfg)} if isinstance(cur_cfg, int) else {}
            cams_cfg[uid] = cur_cfg
        cur_cfg.update(fields)
        self._save_hub_config()

    def _effective_camera_ai_targets(self, uid: str):
        """Return (track_enable, human_sensitive, motion_sensitivity) per camera override or Hub auto policy."""
        cam_cfg = (self.config.get("cameras") or {}).get(uid)
        if not isinstance(cam_cfg, dict):
            cam_cfg = {}
        track_en = int(cam_cfg["track_enable"]) if "track_enable" in cam_cfg else (1 if self.auto_firmware_ai else None)
        human_sens = int(cam_cfg["human_sensitive"]) if "human_sensitive" in cam_cfg else (3 if self.auto_firmware_ai else None)
        motion_sens = int(cam_cfg["motion_sensitivity"]) if "motion_sensitivity" in cam_cfg else (10 if self.auto_firmware_ai else None)
        return track_en, human_sens, motion_sens

    def _get_room_name(self, cam: proxy.CameraInfo) -> str:
        cam_cfg = self.config.get("cameras", {}).get(cam.uid)
        if isinstance(cam_cfg, dict) and cam_cfg.get("name"):
            return str(cam_cfg["name"])
        if getattr(cam, "hw_alias", None):
            return str(cam.hw_alias)
        idx = len(self.sessions) + 1
        return f"Phong_{100 + idx}"

    def _make_wan_camera_info(self, uid_hex: str, cam_cfg: Optional[dict] = None) -> proxy.CameraInfo:
        if not isinstance(cam_cfg, dict):
            cam_cfg = {}
        ip = str(cam_cfg.get("ip") or "0.0.0.0").strip() or "0.0.0.0"
        wan_port = int(cam_cfg.get("wan_port", 32108))
        if hasattr(proxy.CameraInfo, "from_did"):
            cam = proxy.CameraInfo.from_did(uid_hex, ip=ip, port=wan_port)
        else:
            cam = proxy.CameraInfo(ip, wan_port, bytes.fromhex(uid_hex))
            c_did, f_did = proxy.pppp_uid_hex_to_did(uid_hex)
            cam.hw_deviceid = cam_cfg.get("hw_deviceid") or c_did
            cam.formatted_did = cam_cfg.get("did") or cam_cfg.get("formatted_did") or f_did
            cam.connection_mode = "LAN" if (ip and ip != "0.0.0.0") else "WAN_P2P"
        if not getattr(cam, "formatted_did", None):
            _, f_did = proxy.pppp_uid_hex_to_did(uid_hex)
            cam.formatted_did = cam_cfg.get("did") or cam_cfg.get("formatted_did") or f_did
        if not getattr(cam, "hw_deviceid", None):
            c_did, _ = proxy.pppp_uid_hex_to_did(uid_hex)
            cam.hw_deviceid = cam_cfg.get("hw_deviceid") or c_did
        return cam

    async def start_camera_session(self, cam: proxy.CameraInfo):
        if cam.uid in self.sessions:
            return

        base_port = self.config["base_port"]
        psk_str = self.config.get("psk", "vstarcam2019")
        enc_mode_cfg = self.config.get("enc_mode", "auto")
        motion_cooldown = float(self.config.get("motion_cooldown", 30))
        motion_poll_interval = float(self.config.get("motion_poll_interval", 1))

        if enc_mode_cfg == "auto":
            enc_mode = None
            p2p_key = None
            psk_list = [psk_str] + [p for p in proxy.KNOWN_PSKS if p != psk_str]
        elif enc_mode_cfg == "xor":
            enc_mode = proxy.ENC_XOR_ONLY
            p2p_key = None
            psk_list = None
        else:
            enc_mode = proxy.ENC_P2P
            p2p_key = proxy.p2p_derive_key(psk_str.encode("ascii")) if psk_str else b"\x00\x00\x00\x00"
            psk_list = [psk_str]

        cam_cfg = self.config.get("cameras", {}).get(cam.uid)
        if not isinstance(cam_cfg, dict):
            port_val = proxy.assign_port(self.config, cam.uid, base_port)
            cam_cfg = {"port": port_val, "name": self._get_room_name(cam)}
            self.config.setdefault("cameras", {})[cam.uid] = cam_cfg
        else:
            port_val = proxy.assign_port(self.config, cam.uid, base_port)
            if not cam_cfg.get("name"):
                cam_cfg["name"] = self._get_room_name(cam)

        c_did, f_did = proxy.pppp_uid_hex_to_did(cam.uid)
        if not getattr(cam, "formatted_did", None):
            cam.formatted_did = cam_cfg.get("did") or cam_cfg.get("formatted_did") or f_did
        if not getattr(cam, "hw_deviceid", None):
            cam.hw_deviceid = cam_cfg.get("hw_deviceid") or c_did
        if cam.formatted_did and not cam_cfg.get("did"):
            cam_cfg["did"] = cam.formatted_did
        if cam.formatted_did and not cam_cfg.get("formatted_did"):
            cam_cfg["formatted_did"] = cam.formatted_did

        username = cam_cfg.get("username") or self.config["username"]
        password = cam_cfg.get("password") or self.config["password"]

        room_name = cam_cfg["name"]
        cam.label = f"{cam.uid[:8]} ({room_name})"
        self._save_hub_config()

        want_track, want_hsens, want_msens = self._effective_camera_ai_targets(cam.uid)
        session = proxy.CameraSession(
            camera=cam,
            rtsp_port=port_val,
            username=username,
            password=password,
            p2p_key=p2p_key,
            psk_list=psk_list,
            enc_mode=enc_mode,
            motion_webhook=cam_cfg.get("motion_webhook"),
            motion_cooldown=motion_cooldown,
            motion_poll_interval=motion_poll_interval,
            bind_addr=self.config.get("bind_addr", "127.0.0.1"),
            auto_track=want_track,
            human_sensitive=want_hsens,
            motion_sensitivity=(want_msens if want_msens is not None else 5),
            playout_delay_ms=int(self.config.get("playout_delay_ms", 250)),
            playout_mode=str(self.config.get("playout_mode", "relay")),
        )

        async def get_snap():
            if session.rtsp:
                return await session.rtsp._get_snapshot_jpeg()
            return None

        recorder = CameraMotionRecorder(
            uid=cam.uid,
            room_name=room_name,
            recordings_dir=self.recordings_dir,
            pre_roll_sec=self.pre_roll_sec,
            post_roll_sec=self.post_roll_sec,
            pframe_sensitivity=self.pframe_sensitivity,
            enable_pframe_trigger=self.enable_pframe_trigger,
            pframe_cooldown_sec=self.pframe_cooldown_sec,
            snapshot_getter=get_snap,
        )
        session.recorder = recorder

        self.sessions[cam.uid] = session
        self.recorders[cam.uid] = recorder
        await session.start()

        pending = cam_cfg.get("pending_wifi") if isinstance(cam_cfg, dict) else None
        if pending:
            async def _push_pending(uid=cam.uid, pw=pending, cfg=cam_cfg):
                for _attempt in range(8):
                    await asyncio.sleep(3.0)
                    sess = self.sessions.get(uid)
                    if sess and sess.protocol and sess.state == "CONNECTED":
                        try:
                            for _ in range(2):
                                await sess.protocol.send_set_wifi(pw["ssid"], pw["password"])
                                await asyncio.sleep(0.8)
                            cfg.pop("pending_wifi", None)
                            self._save_hub_config()
                            log.info("Auto-pushed queued Wi-Fi '%s' to %s", pw["ssid"], uid[:10])
                            return
                        except Exception as e:
                            log.warning("Pending Wi-Fi push failed: %s", e)
            asyncio.create_task(_push_pending())

        hw_alias = getattr(cam, "hw_alias", None)
        if hw_alias and room_name.startswith("Phong_"):
            cam_cfg["name"] = hw_alias
            recorder.set_room_name(hw_alias)
            cam.label = f"{cam.uid[:8]} ({hw_alias})"
            self._save_hub_config()

    async def start_background_tasks(self, app: web.Application):
        self._running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        self._quota_task = asyncio.create_task(self._quota_loop())
        self._fps_guard_task = asyncio.create_task(self._fps_guard_loop())

    async def stop_background_tasks(self, app: web.Application):
        self._running = False
        for ws in list(self.ws_clients):
            await ws.close()
        for t in (self._discovery_task, self._quota_task, self._fps_guard_task):
            if t:
                t.cancel()
        for sess in list(self.sessions.values()):
            await sess.stop()

    async def _start_configured_wan_cameras(self):
        for uid_hex, cam_cfg in list((self.config.get("cameras") or {}).items()):
            if not isinstance(cam_cfg, dict):
                cam_cfg = {"port": int(cam_cfg) if isinstance(cam_cfg, int) else 9555}
            if uid_hex not in self.sessions:
                try:
                    if hasattr(proxy.CameraInfo, "from_did"):
                        cam = proxy.CameraInfo.from_did(
                            uid_hex,
                            ip=cam_cfg.get("ip", "0.0.0.0"),
                            port=int(cam_cfg.get("wan_port", 32108)),
                        )
                    else:
                        cam = proxy.CameraInfo(
                            cam_cfg.get("ip", "0.0.0.0"),
                            int(cam_cfg.get("wan_port", 32108)),
                            bytes.fromhex(uid_hex),
                        )
                    if not getattr(cam, "formatted_did", None):
                        _, f_did = proxy.pppp_uid_hex_to_did(uid_hex)
                        cam.formatted_did = cam_cfg.get("did") or cam_cfg.get("formatted_did") or f_did
                    log.info(
                        "Starting WAN P2P session for configured camera %s (%s)",
                        uid_hex[:12],
                        getattr(cam, "formatted_did", None) or cam_cfg.get("name"),
                    )
                    await self.start_camera_session(cam)
                except Exception as e:
                    log.error("Error starting WAN P2P session for %s: %s", uid_hex[:12], e)

    async def _discovery_loop(self):
        discovery_time = float(self.config.get("discovery_time", 3))
        log.info("=== Initial Camera Discovery ===")
        try:
            cams = await proxy.discover_cameras_broadcast(timeout=discovery_time, target_ip=self.target_ip)
            for cam in cams:
                if cam.uid not in self.sessions:
                    await self.start_camera_session(cam)
        except Exception as e:
            log.error("Initial discovery error: %s", e)

        # Auto-start any configured cameras not found on LAN over WAN P2P (Internet)
        await self._start_configured_wan_cameras()

        while self._running:
            await self._start_configured_wan_cameras()
            configured_uids = set((self.config.get("cameras") or {}).keys())
            has_waiting = any(u not in self.sessions for u in configured_uids)
            await asyncio.sleep(10 if has_waiting else 35)
            active = sum(
                1 for s in self.sessions.values()
                if s.state in (proxy.STATE_CONNECTED, proxy.STATE_STALE)
            )
            if not has_waiting and active >= len(self.sessions) and len(self.sessions) > 0:
                continue
            try:
                connected_ips = {
                    s.camera.ip for s in self.sessions.values()
                    if s.camera.ip and s.camera.ip != "0.0.0.0"
                }
                cams = await proxy.discover_cameras_broadcast(timeout=2.5, target_ip=self.target_ip, skip_ips=connected_ips)
                for cam in cams:
                    if cam.uid not in self.sessions:
                        log.info("Discovered new camera on LAN: %s", cam)
                        await self.start_camera_session(cam)
                    else:
                        sess = self.sessions[cam.uid]
                        if sess.camera.ip in ("0.0.0.0", "") and cam.ip not in ("0.0.0.0", ""):
                            sess.camera.ip = cam.ip
                            sess.camera.port = cam.port
                            sess.camera.connection_mode = "LAN"
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Periodic discovery error: %s", e)

    async def _quota_loop(self):
        while self._running:
            await asyncio.sleep(60)
            try:
                self.storage.enforce_quota()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("Quota check error: %s", e)

    async def _sync_camera_fps_once(self, uid: str, sess):
        """Ensure camera main stream encoder FPS meets target (fixes .12 firmware 15 FPS default)."""
        proto = getattr(sess, "protocol", None)
        if (not proto or not proto.enc_mode
                or sess.state != proxy.STATE_CONNECTED
                or getattr(proto, "auth_failed", False)):
            return
        proto_id = id(proto)
        if self._fps_checked_protos.get(uid) == proto_id:
            return

        cam_cfg = (self.config.get("cameras") or {}).get(uid)
        explicit = cam_cfg.get("target_fps") if isinstance(cam_cfg, dict) else None
        if explicit is None and not self.auto_25fps:
            self._fps_checked_protos[uid] = proto_id
            return

        params = await proto.query_cgi("get_camera_params.cgi", timeout=6.0)
        if not params or "enc_framerate" not in params:
            return
        try:
            cur_fps = int(float(params["enc_framerate"]))
        except (TypeError, ValueError):
            self._fps_checked_protos[uid] = proto_id
            return
        try:
            cur_kf = int(float(params.get("enc_keyframe", 0) or 0))
        except (TypeError, ValueError):
            cur_kf = 0

        if explicit is not None:
            desired = int(explicit)
            desired_kf = max(desired, min(150, desired * 2))
            need_update = (desired > 0 and cur_fps != desired) or (0 < cur_kf < desired_kf)
        else:
            desired = 25
            desired_kf = 50   # 2s GOP cuts static H.265 keyframe overhead cleanly
            need_update = (0 < cur_fps < 25) or (0 < cur_kf < desired_kf)

        if need_update:
            res = await proto.query_cgi(
                f"set_media.cgi?mainrate=0&enc_framerate={desired}&enc_keyframe={desired_kf}",
                timeout=6.0,
            )
            if res and str(res.get("result", "")).lower() not in ("-1", "-2", "erro", "error"):
                log.info(
                    "[%s] Synced camera main-stream encoder FPS %d -> %d (enc_keyframe %d -> %d)",
                    uid[:8], cur_fps, desired, cur_kf, desired_kf,
                )
            else:
                log.warning("[%s] Failed to sync camera FPS/GOP %d -> %d: %s", uid[:8], cur_fps, desired, res)
                return

        # Prefer VBR (0) over CBR (1) to save H.265 disk size on static scenes
        if str(params.get("enc_ratemode", "")) == "1":
            vbr_res = await proto.query_cgi("set_media.cgi?mainrate=0&enc_ratemode=0", timeout=5.0)
            if vbr_res and str(vbr_res.get("result", "")).lower() not in ("-1", "-2", "erro", "error"):
                log.info("[%s] Switched camera encoder rate mode CBR -> VBR (0)", uid[:8])

        self._fps_checked_protos[uid] = proto_id

    @staticmethod
    def _cgi_reply_ok(reply: Optional[dict]) -> bool:
        if not reply or reply.get("cgi") == "not support":
            return False
        return str(reply.get("result", "")).lower() not in ("-1", "-2", "erro", "error")

    async def _sync_camera_ai_once(self, uid: str, sess):
        """Ensure connected camera has target AI tracking (2127), humanoid sensitivity (2126),
        and motion alarm sensitivity (set_alarm.cgi) applied."""
        proto = getattr(sess, "protocol", None)
        if (not proto or not proto.enc_mode
                or sess.state != proxy.STATE_CONNECTED
                or getattr(proto, "auth_failed", False)
                or not getattr(proto, "_auto_provision_done", False)):
            return
        proto_id = id(proto)
        if self._ai_checked_protos.get(uid) == proto_id:
            return

        want_track, want_hsens, want_msens = self._effective_camera_ai_targets(uid)
        proto.auto_track = want_track
        proto.human_sensitive = want_hsens
        if want_msens is not None:
            proto.motion_sensitivity = max(1, min(10, int(want_msens)))
        if want_track is None and want_hsens is None and want_msens is None:
            self._ai_checked_protos[uid] = proto_id
            return

        queries = []
        if want_track is not None:
            queries.append("trans_cmd_string.cgi?cmd=2127&command=1")
        if want_hsens is not None:
            queries.append("trans_cmd_string.cgi?cmd=2126&command=1")
        if want_msens is not None:
            queries.append("get_params.cgi")
        replies = await proto.query_cgis(queries, timeout=8.0)

        any_answered = False
        all_synced = True
        changes = []

        if want_msens is not None:
            pm = replies.get("get_params.cgi")
            if pm is None:
                all_synced = False
            elif pm.get("alarm_motion_sensitivity") is not None:
                any_answered = True
                want_ms_str = str(max(1, min(10, int(want_msens))))
                if (pm.get("alarm_motion_sensitivity") != want_ms_str
                        or pm.get("alarm_motion_armed") != "1"
                        or pm.get("alarm_schedule_enable") != "1"
                        or pm.get("alarm_record") != "1"):
                    res = await proto.query_cgi(
                        f"set_alarm.cgi?motion_armed=1&motion_sensitivity={want_ms_str}"
                        "&input_armed=0&iolinkage=0&mail=0&upload_interval=0&record=1&snapshot=1&schedule_enable=1",
                        timeout=6.0,
                    )
                    if self._cgi_reply_ok(res):
                        changes.append(f"motion_sens={pm.get('alarm_motion_sensitivity')}->{want_ms_str}")
                    else:
                        all_synced = False

        if want_track is not None:
            c2127 = replies.get("trans_cmd_string.cgi?cmd=2127&command=1")
            if c2127 is None:
                all_synced = False
            elif "enable" in c2127:
                any_answered = True
                want_tr_str = "1" if want_track else "0"
                if c2127.get("enable") != want_tr_str:
                    res = await proto.query_cgi(f"trans_cmd_string.cgi?cmd=2127&command=0&enable={want_tr_str}", timeout=6.0)
                    if self._cgi_reply_ok(res):
                        changes.append(f"track_2127={c2127.get('enable')}->{want_tr_str}")
                    else:
                        all_synced = False

        if want_hsens is not None:
            c2126 = replies.get("trans_cmd_string.cgi?cmd=2126&command=1")
            if c2126 is None:
                all_synced = False
            elif "sensitive" in c2126:
                any_answered = True
                want_hs_str = str(max(0, min(3, int(want_hsens))))
                if c2126.get("sensitive") != want_hs_str:
                    res = await proto.query_cgi(f"trans_cmd_string.cgi?cmd=2126&command=0&sensitive={want_hs_str}", timeout=6.0)
                    if self._cgi_reply_ok(res):
                        changes.append(f"human_sens_2126={c2126.get('sensitive')}->{want_hs_str}")
                    else:
                        all_synced = False

        if changes:
            log.info("[%s] Synced camera firmware AI/motion: %s", uid[:8], ", ".join(changes))
        if all_synced or any_answered:
            if all_synced:
                self._ai_checked_protos[uid] = proto_id

    async def _fps_guard_loop(self):
        """Periodically ensure connected cameras match target FPS/GOP and AI/motion sensitivity."""
        while self._running:
            await asyncio.sleep(6)
            for uid, sess in list(self.sessions.items()):
                try:
                    await self._sync_camera_fps_once(uid, sess)
                    await self._sync_camera_ai_once(uid, sess)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    log.debug("[%s] Camera sync guard check failed: %s", uid[:8], e)

    # =========================================================================
    # HTTP & WebSocket API Handlers
    # =========================================================================

    async def handle_index(self, request: web.Request) -> web.Response:
        index_path = os.path.join(STATIC_DIR, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        cameras_list = []
        now_loop = asyncio.get_event_loop().time()
        for uid, sess in self.sessions.items():
            rec = self.recorders.get(uid)
            cam = sess.camera
            cam_cfg = (self.config.get("cameras") or {}).get(uid)
            if not isinstance(cam_cfg, dict):
                cam_cfg = {}
            last_drw_age = round(now_loop - sess._last_drw_time, 1) if sess._last_drw_time > 0 else 999.0
            rem_post = 0.0
            if rec and rec.is_recording:
                rem_post = max(0.0, round(rec.record_until_mono - time.monotonic(), 1))

            c_did, f_did = proxy.pppp_uid_hex_to_did(uid)
            did_str = getattr(cam, "formatted_did", None) or cam_cfg.get("did") or cam_cfg.get("formatted_did") or f_did or ""
            conn_mode = getattr(cam, "display_mode", None) or getattr(cam, "connection_mode", None) or ("WAN_P2P" if cam.ip in ("0.0.0.0", "") else "LAN")
            auth_failed = bool(sess.protocol and getattr(sess.protocol, "auth_failed", False))
            auth_error = getattr(sess.protocol, "auth_error", None) if sess.protocol else None
            wan_cloud_status = getattr(cam, "wan_cloud_status", "IDLE")
            onboarding_active = bool(getattr(cam, "onboarding_until", 0.0) > time.monotonic() and sess.state != "CONNECTED")
            cameras_list.append({
                "uid": uid,
                "short_uid": uid[:10],
                "room_name": rec.room_name if rec else uid[:10],
                "hw_alias": getattr(cam, "hw_alias", ""),
                "hw_deviceid": getattr(cam, "hw_deviceid", "") or c_did,
                "did": did_str,
                "formatted_did": did_str,
                "connection_mode": conn_mode,
                "wan_cloud_status": wan_cloud_status,
                "onboarding_active": onboarding_active,
                "ip": cam.ip,
                "port": getattr(cam, "port", 32108),
                "rtsp_port": sess.rtsp_port,
                "rtsp_url": f"rtsp://{request.host.split(':')[0]}:{sess.rtsp_port}/",
                "state": sess.state,
                "auth_failed": auth_failed,
                "auth_error": auth_error,
                "last_packet_sec": last_drw_age,
                "wan_relay_rtt_ms": getattr(cam, "wan_relay_rtt_ms", None),
                "wan_cloud_status": getattr(cam, "wan_cloud_status", None),
                "cmd_srtt_ms": (
                    round(sess.protocol._cmd_srtt * 1000.0, 1)
                    if (sess.protocol and getattr(sess.protocol, "_cmd_srtt", None) is not None)
                    else None
                ),
                "cmd_retx_count": getattr(sess.protocol, "_cmd_retx_count", 0) if sess.protocol else 0,
                "codec": (sess.rtsp._codec if (sess.rtsp and sess.rtsp._codec) else None),
                "kbps": rec.current_kbps if rec else 0.0,
                "fps": rec.current_fps if rec else 0.0,
                "ram_buffer_kb": rec.ram_buffer_kb if rec else 0.0,
                "is_recording": rec.is_recording if rec else False,
                "manual_record": rec.manual_record if rec else False,
                "motion_active": rec.motion_active if rec else False,
                "post_roll_remaining": rem_post,
                "last_trigger": rec.last_trigger_reason if rec else "",
                "clips_saved": rec.clips_saved_count if rec else 0,
                "live_decoder_active": (rec._live_proc is not None) if rec else False,
            })

        # Also include any pre-registered cameras not yet in self.sessions
        cfg_cams = self.config.get("cameras", {})
        if isinstance(cfg_cams, dict):
            for uid, c_cfg in cfg_cams.items():
                if uid in self.sessions:
                    continue
                if not isinstance(c_cfg, dict):
                    c_cfg = {"port": int(c_cfg) if isinstance(c_cfg, int) else 9555}
                c_did, f_did = proxy.pppp_uid_hex_to_did(uid)
                did_str = c_cfg.get("did") or c_cfg.get("formatted_did") or f_did or ""
                r_port = c_cfg.get("port", 9555)
                r_name = c_cfg.get("name") or did_str or uid[:10]
                cameras_list.append({
                    "uid": uid,
                    "short_uid": uid[:10],
                    "room_name": r_name,
                    "hw_alias": "",
                    "hw_deviceid": c_cfg.get("hw_deviceid") or c_did,
                    "did": did_str,
                    "formatted_did": did_str,
                    "connection_mode": c_cfg.get("connection_mode", "WAN_P2P"),
                    "wan_cloud_status": "IDLE",
                    "onboarding_active": False,
                    "model": c_cfg.get("model", "CS22S"),
                    "ip": c_cfg.get("ip") or "0.0.0.0",
                    "port": int(c_cfg.get("wan_port", 32108)),
                    "rtsp_port": r_port,
                    "rtsp_url": f"rtsp://{request.host.split(':')[0]}:{r_port}/",
                    "state": "WAITING_LAN",
                    "auth_failed": False,
                    "auth_error": None,
                    "last_packet_sec": 999.0,
                    "codec": "h265",
                    "kbps": 0.0,
                    "fps": 0.0,
                    "ram_buffer_kb": 0.0,
                    "is_recording": False,
                    "manual_record": False,
                    "motion_active": False,
                    "post_roll_remaining": 0.0,
                    "last_trigger": "",
                    "clips_saved": 0,
                    "live_decoder_active": False,
                })

        return web.json_response({
            "cameras": cameras_list,
            "storage": self.storage.get_storage_stats(),
            "settings": {
                "pre_roll_sec": self.pre_roll_sec,
                "post_roll_sec": self.post_roll_sec,
                "pframe_sensitivity": self.pframe_sensitivity,
                "pframe_cooldown_sec": self.pframe_cooldown_sec,
                "enable_pframe_trigger": self.enable_pframe_trigger,
                "auto_25fps": self.auto_25fps,
                "auto_firmware_ai": self.auto_firmware_ai,
                "max_storage_gb": self.max_storage_gb,
                "min_free_gb": self.min_free_gb,
            },
            "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    async def handle_ws_video(self, request: web.Request) -> web.WebSocketResponse:
        """Live compressed video for WebCodecs players: `/ws/video/{uid}?codec=hevc|h264`.
        hevc = camera frames passed through; h264 = shared iGPU transcode (needs VAAPI)."""
        uid = request.match_info.get("uid", "")
        codec = request.query.get("codec", "hevc")
        if uid not in self.sessions or codec not in live_video.CODEC_STRINGS:
            return web.json_response({"ok": False, "error": "camera/codec không hợp lệ"}, status=404)
        if codec == "h264" and not self.vaapi_device():
            return web.json_response({"ok": False, "error": "iGPU H.264 không khả dụng"}, status=503)
        live = self.live_video.get(uid)
        if live is None:
            live = live_video.CameraLiveVideo(
                uid,
                get_session=lambda u=uid: self.sessions.get(u),
                get_recorder=lambda u=uid: self.recorders.get(u),
                get_vaapi_device=self.vaapi_device,
            )
            self.live_video[uid] = live
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        log.info("Live video %s client +1 for %s", codec, uid[:10])
        try:
            await live_video.serve_ws(ws, live, codec)
        finally:
            if not ws.closed:
                await ws.close()
            log.info("Live video %s client -1 for %s", codec, uid[:10])
        return ws

    AUDIO_WS_CODECS = {"pcmu": 0, "pcma": 1, "pcm": 2}
    WS_WINDOW_MIN = 3     # unacknowledged JPEG frames allowed in flight per /ws/streams client...
    WS_WINDOW_MAX = 30    # ...sized from the measured ack RTT (see handle_ws_streams)
    WS_STREAM_FPS = 50.0  # frames/s the window must sustain (2 cameras x 25)
    WS_LATENCY_BUDGET = 0.4  # s of frames allowed in flight: bounds live lag on a saturated link

    async def handle_ws_audio(self, request: web.Request) -> web.WebSocketResponse:
        """Live camera audio (`/ws/audio/{uid}`): binary messages `[1B codec][payload]`,
        codec 0=G.711 u-law, 1=G.711 a-law, 2=PCM s16le; always 8kHz mono."""
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess:
            return web.json_response({"ok": False, "error": "Không tìm thấy camera"}, status=404)
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=40)   # ~4s; drop oldest when the client lags

        def listener(data: bytes, codec: str):
            code = self.AUDIO_WS_CODECS.get(codec)
            if code is None:
                return
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(bytes([code]) + data)

        sess.audio_listeners.add(listener)
        log.info("Live audio listener +1 for %s (%d)", uid[:10], len(sess.audio_listeners))

        async def drain_client():
            async for _msg in ws:   # the client sends nothing; this just notices the close
                pass

        reader = asyncio.create_task(drain_client())
        try:
            while not ws.closed and not reader.done():
                try:
                    pkt = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                await ws.send_bytes(pkt)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            sess.audio_listeners.discard(listener)
            reader.cancel()
            if not ws.closed:
                await ws.close()
            log.info("Live audio listener -1 for %s (%d)", uid[:10], len(sess.audio_listeners))
        return ws

    async def handle_ws_streams(self, request: web.Request) -> web.WebSocketResponse:
        """
        Multiplexed Binary WebSocket stream (`/ws/streams`).
        Pushes real-time 25 FPS JPEG frames (`[1B uid_len][uid_bytes][jpeg_bytes]`)
        for all cameras over a single TCP connection.
        """
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.ws_clients.add(ws)

        state = {
            "grid_fps": 25.0,
            "focus_uid": None,
            "paused": False,
        }
        last_sent_seq: Dict[str, int] = {}
        last_sent_mono: Dict[str, float] = {}
        # Flow control: clients ack every frame ("a"). Keeping only WS_WINDOW frames
        # unacknowledged bounds latency to ~window x frame size / bandwidth; a link
        # slower than the stream (e.g. Cloudflare ~14 Mbps vs ~20 Mbps of JPEG) then
        # drops FPS instead of buffering seconds of stale frames. Clients that never
        # ack (old cached JS) are served unthrottled as before.
        flow = {"inflight": 0, "acks": 0, "last_ack": time.monotonic(),
                "sent": collections.deque(maxlen=64), "rtts": collections.deque(maxlen=120),
                "window": self.WS_WINDOW_MIN, "log_at": time.monotonic() + 15.0}
        rr = {"i": 0}

        async def rx_loop():
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT and msg.data == "a":
                        now_a = time.monotonic()
                        flow["inflight"] = max(0, flow["inflight"] - 1)
                        flow["acks"] += 1
                        flow["last_ack"] = now_a
                        if flow["sent"]:
                            flow["rtts"].append(now_a - flow["sent"].popleft())
                            # Window = enough frames to keep the pipe full for one base RTT
                            # (x1.5), but at least WS_LATENCY_BUDGET worth: Cloudflare's RTT
                            # jitters 90-250ms, and a too-tight window starves throughput.
                            base_rtt = min(flow["rtts"])
                            span = max(base_rtt * 1.5, self.WS_LATENCY_BUDGET)
                            flow["window"] = int(max(self.WS_WINDOW_MIN, min(
                                self.WS_WINDOW_MAX, self.WS_STREAM_FPS * span + 1)))
                        continue
                    if msg.type == WSMsgType.TEXT:
                        try:
                            cfg = json.loads(msg.data)
                            if "grid_fps" in cfg:
                                state["grid_fps"] = float(cfg["grid_fps"])
                            if "focus_uid" in cfg:
                                state["focus_uid"] = cfg["focus_uid"] or None
                            if "paused" in cfg:
                                state["paused"] = bool(cfg["paused"])
                        except Exception:
                            pass
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break
            except Exception:
                pass

        rx_task = asyncio.create_task(rx_loop())
        try:
            while self._running and not ws.closed:
                if state["paused"] or (state["grid_fps"] <= 0 and not state["focus_uid"]):
                    await asyncio.sleep(0.15)
                    continue

                now = time.monotonic()
                sent_any = False
                focus_uid = state["focus_uid"]
                grid_fps = state["grid_fps"]
                if flow["acks"] and now >= flow["log_at"] and flow["rtts"]:
                    flow["log_at"] = now + 30.0
                    r = sorted(flow["rtts"])
                    log.info("WS stream client: ack RTT min/p50=%.0f/%.0fms window=%d frames",
                             r[0] * 1000, r[len(r) // 2] * 1000, flow["window"])
                if flow["acks"] and flow["inflight"] >= flow["window"]:
                    if now - flow["last_ack"] > 5.0:
                        flow["inflight"] = 0          # lost acks: don't stall forever
                        flow["sent"].clear()
                    await asyncio.sleep(0.005)
                    continue

                # Rotate the starting camera so a tight window is shared fairly
                items = list(self.recorders.items())
                if items:
                    k = rr["i"] % len(items)
                    rr["i"] += 1
                    items = items[k:] + items[:k]
                # The focused camera first: it is what the user is looking at
                items.sort(key=lambda kv: kv[0] != focus_uid)

                for uid, rec in items:
                    if flow["acks"] and flow["inflight"] >= flow["window"]:
                        break
                    # If Focus Modal is open and grid_fps <= 0, only decode focused camera
                    if grid_fps <= 0 and uid != focus_uid:
                        continue

                    rec.touch_viewer()
                    jpg = rec.latest_live_jpeg
                    seq = rec.latest_live_seq
                    if not jpg or seq == last_sent_seq.get(uid, -1):
                        continue

                    target_fps = 25.0 if (uid == focus_uid) else max(1.0, grid_fps)
                    if target_fps < 24.0:
                        min_dt = 0.90 / target_fps
                        if (now - last_sent_mono.get(uid, 0.0)) < min_dt:
                            continue

                    uid_bytes = uid.encode("ascii")
                    packet = bytes([len(uid_bytes)]) + uid_bytes + jpg
                    await ws.send_bytes(packet)
                    flow["inflight"] += 1
                    flow["sent"].append(time.monotonic())
                    last_sent_seq[uid] = seq
                    last_sent_mono[uid] = now
                    sent_any = True

                await asyncio.sleep(0.008 if sent_any else 0.015)
        except (ConnectionResetError, asyncio.CancelledError, Exception):
            pass
        finally:
            rx_task.cancel()
            self.ws_clients.discard(ws)
        return ws

    async def handle_snapshot(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        rec = self.recorders.get(uid)
        sess = self.sessions.get(uid)
        if not sess:
            c_cfg = (self.config.get("cameras") or {}).get(uid, {})
            if isinstance(c_cfg, dict):
                _, f_did = proxy.pppp_uid_hex_to_did(uid)
                did_label = c_cfg.get("did") or c_cfg.get("formatted_did") or f_did or uid[:12]
                r_name = c_cfg.get("name") or did_label
                svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
                  <rect width="960" height="540" fill="#0b0f17"/>
                  <circle cx="480" cy="210" r="46" fill="#161b22" stroke="#2f81f7" stroke-width="3" stroke-dasharray="8 6"/>
                  <text x="480" y="222" text-anchor="middle" fill="#2f81f7" font-family="Segoe UI, sans-serif" font-size="34">🌐</text>
                  <text x="480" y="295" text-anchor="middle" fill="#e6edf3" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="22">{r_name} — {did_label}</text>
                  <text x="480" y="335" text-anchor="middle" fill="#58a6ff" font-family="Segoe UI, sans-serif" font-size="17">⏳ Đang kết nối Camera qua WAN P2P (Internet) &amp; LAN...</text>
                  <text x="480" y="370" text-anchor="middle" fill="#7d8590" font-family="Segoe UI, sans-serif" font-size="14">Bắt tay UDP P2P / Cloud Relay (Port :{c_cfg.get('port', 9555)}) • Live 25 FPS</text>
                </svg>"""
                return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})
            return web.Response(status=404, text="Camera not found")

        jpeg = None
        if rec:
            jpeg = await rec.get_live_snapshot(timeout=1.5)
        if not jpeg and sess.rtsp:
            jpeg = await sess.rtsp._get_snapshot_jpeg()
        if not jpeg:
            _, f_did = proxy.pppp_uid_hex_to_did(uid)
            did_label = getattr(sess.camera, "formatted_did", None) or f_did or uid[:12]
            r_name = rec.room_name if rec else did_label
            mode_label = getattr(sess.camera, "connection_mode", "WAN_P2P")
            if sess.protocol and getattr(sess.protocol, "auth_failed", False):
                svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
                  <rect width="960" height="540" fill="#0b0f17"/>
                  <circle cx="480" cy="200" r="46" fill="#161b22" stroke="#d29922" stroke-width="3"/>
                  <text x="480" y="213" text-anchor="middle" fill="#d29922" font-family="Segoe UI, sans-serif" font-size="34">🔑</text>
                  <text x="480" y="285" text-anchor="middle" fill="#e6edf3" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="22">{r_name} ({did_label}) — Đã kết nối {mode_label} ({sess.camera.ip}:{getattr(sess.camera, 'port', 32108)})</text>
                  <text x="480" y="325" text-anchor="middle" fill="#f0883e" font-family="Segoe UI, sans-serif" font-size="17">⚠️ Camera yêu cầu mật khẩu riêng (khác 888888)</text>
                  <text x="480" y="362" text-anchor="middle" fill="#7d8590" font-family="Segoe UI, sans-serif" font-size="14">Bấm ➕ Thêm Camera bằng QR / UID và nhập Mật khẩu Camera để mở luồng hình</text>
                </svg>"""
                return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})
            if sess.state != "CONNECTED":
                svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
                  <rect width="960" height="540" fill="#0b0f17"/>
                  <circle cx="480" cy="210" r="46" fill="#161b22" stroke="#2f81f7" stroke-width="3" stroke-dasharray="8 6"/>
                  <text x="480" y="222" text-anchor="middle" fill="#2f81f7" font-family="Segoe UI, sans-serif" font-size="34">🌐</text>
                  <text x="480" y="295" text-anchor="middle" fill="#e6edf3" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="22">{r_name} ({did_label})</text>
                  <text x="480" y="335" text-anchor="middle" fill="#58a6ff" font-family="Segoe UI, sans-serif" font-size="17">⏳ Đang kết nối qua {mode_label} ({sess.camera.ip}:{getattr(sess.camera, 'port', 32108)})...</text>
                  <text x="480" y="370" text-anchor="middle" fill="#7d8590" font-family="Segoe UI, sans-serif" font-size="14">RTSP Port :{sess.rtsp_port} • Tự động xuyên NAT (UDP Hole-Punching / Cloud Relay)</text>
                </svg>"""
                return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})
            return web.Response(status=503, text="No frame cached yet")

        return web.Response(
            body=jpeg,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    async def handle_mjpeg(self, request: web.Request) -> web.StreamResponse:
        """Real-time 25 FPS multipart MJPEG stream (`/api/camera/{uid}/mjpeg`)."""
        uid = request.match_info.get("uid", "")
        rec = self.recorders.get(uid)
        sess = self.sessions.get(uid)
        if not sess or not rec:
            return web.Response(status=404, text="Camera not found")

        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=frame",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        last_seq = -1
        try:
            while self._running:
                rec.touch_viewer()
                jpeg = rec.latest_live_jpeg
                if jpeg and rec.latest_live_seq != last_seq:
                    last_seq = rec.latest_live_seq
                    part = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                        + jpeg
                        + b"\r\n"
                    )
                    await resp.write(part)
                await asyncio.sleep(0.035)
        except (ConnectionResetError, asyncio.CancelledError, Exception):
            pass
        return resp

    # --- Camera control (PTZ / image / settings) over the PPPP command channel ---
    #
    # Works identically on LAN, WAN P2P and Cloud Relay: every command is
    # retransmitted until the camera ACKs it, and the API reports that ACK
    # instead of blindly answering "ok".

    CMD_NO_ACK_MSG = ("Camera chưa xác nhận lệnh (đường truyền chậm hoặc mất kết nối). "
                      "Lệnh vẫn đang được gửi lại tự động.")

    # decoder_control.cgi command codes (VStarcam/Eye4)
    PTZ_MOVES = {
        "up": 0, "down": 2, "left": 4, "right": 6,
        "leftup": 90, "rightup": 91, "leftdown": 92, "rightdown": 93,
    }
    PTZ_SIMPLE = {
        "center": 25,
        "vscan": 26, "vscan_stop": 27,      # up/down patrol
        "hscan": 28, "hscan_stop": 29,      # left/right patrol
    }
    # camera_control.cgi fixed param/value pairs
    # param=14 (IR-cut) verified on live VSTJ 48.82.230.12 & 48.82.230.19:
    # 0=day/off, 1=auto (default), 2=forced night B&W / IR LEDs on.
    PARAM_ACTIONS = {
        "ir_off": (14, 0), "ir_auto": (14, 1), "ir_on": (14, 2),
        "flip_normal": (5, 0), "flip_v": (5, 1), "flip_h": (5, 2), "flip_vh": (5, 3),
        "hz_50": (3, 0), "hz_60": (3, 1),
    }
    # camera_control.cgi params taking a numeric value: action -> (param, min, max).
    # Param numbers confirmed on a live camera by read-back through get_camera_params.cgi.
    PARAM_VALUE_ACTIONS = {
        "brightness": (1, 0, 255),
        "contrast": (2, 0, 255),
        "saturation": (8, 0, 255),
        "hue": (9, 0, 255),
        "mic_volume": (24, 0, 31),        # involume
        "speaker_volume": (25, 0, 31),    # outvolume
    }
    # Other CGIs taking a numeric value: action -> (cgi template, min, max)
    CGI_VALUE_ACTIONS = {
        "led_mode": ("set_misc.cgi?led_mode={v}", 0, 2),
        "ptz_speed": ("set_misc.cgi?ptz_patrol_rate={v}&ptz_patrol_up_rate={v}&ptz_patrol_down_rate={v}"
                      "&ptz_patrol_left_rate={v}&ptz_patrol_right_rate={v}", 1, 10),
        "ptz_center_onstart": ("set_misc.cgi?ptz_center_onstart={v}", 0, 1),
        "motion_sensitivity": ("set_alarm.cgi?motion_armed=1&motion_sensitivity={v}&input_armed=0&iolinkage=0"
                               "&mail=0&upload_interval=0&record=1&snapshot=1&schedule_enable=1", 1, 10),
        "human_sensitive": ("trans_cmd_string.cgi?cmd=2126&command=0&sensitive={v}", 0, 3),
        "cry_detect": ("trans_cmd_string.cgi?cmd=2112&command=0&enable={v}", 0, 1),
        "depart_detect_time": ("trans_cmd_string.cgi?cmd=2136&command=0&detectTime={v}", 0, 3600),
        "ai_2137": ("trans_cmd_string.cgi?cmd=2137&command=0&enable={v}", 0, 1),
        "rtsp_tls": ("trans_cmd_string.cgi?cmd=2122&command=0&tls_enable={v}", 0, 1),
        "onvif_enable": ("set_onvif.cgi?onvifenable={v}", 0, 1),
        "enc_keyframe": ("set_media.cgi?mainrate=0&enc_keyframe={v}", 1, 150),
        "enc_ratemode": ("set_media.cgi?mainrate=0&enc_ratemode={v}", 0, 1),
    }
    # Fixed CGIs. The trans_cmd_string keys mirror what the camera itself reports
    # for command=1 (query): 2127 -> enable/track_status, 2126 -> bHumanoidFrame/sensitive.
    # OSD requires set_misc.cgi?osdenable=0/1 (camera_control param=18=0 is ignored by firmware).
    CGI_ACTIONS = {
        "osd_on": "set_misc.cgi?osdenable=1",
        "osd_off": "set_misc.cgi?osdenable=0",
        "track_on": "trans_cmd_string.cgi?cmd=2127&command=0&enable=1",
        "track_off": "trans_cmd_string.cgi?cmd=2127&command=0&enable=0",
        "humanframe_on": "trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=1",
        "humanframe_off": "trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=0",
    }
    # Firmware-optional trans_cmd_string actions echo their target key only when implemented.
    _REQUIRED_REPLY_KEYS = {
        "humanframe_on": ("bHumanoidFrame", "khung người (bHumanoidFrame)"),
        "humanframe_off": ("bHumanoidFrame", "khung người (bHumanoidFrame)"),
        "cry_detect": ("enable", "phát hiện tiếng khóc (cmd=2112)"),
        "ai_2137": ("enable", "cảm biến/AI mở rộng (cmd=2137)"),
        "depart_detect_time": ("detectTime", "phát hiện rời vị trí (cmd=2136)"),
    }
    # CGIs the raw console refuses: they rewrite persistent factory data / firmware
    CGI_BLOCKLIST = ("set_factory_param", "upgrade_firmware", "upgrade_htmls", "restore_factory")
    _SECRET_KEY_RE = re.compile(r"(pwd|pass|psk|wpa|loginpas|^(?:wifi_)?key\d*$)", re.I)

    def _ptz_stop_codes(self, uid: str) -> list:
        """Stop codes for whatever direction this camera was last told to move."""
        move = self._ptz_last_move.get(uid)
        code = move[0] if move else 0
        if code >= 90:
            return [1, 5]        # diagonals: stop both tilt and pan motors
        return [code + 1]

    async def _ptz_send_stop(self, uid: str, proto, wait_ack: float = 0.0) -> bool:
        ok = True
        for code in self._ptz_stop_codes(uid):
            ok = await proto.send_ptz_command(code, onestep=0, wait_ack=wait_ack) and ok
        self._ptz_last_move.pop(uid, None)
        return ok

    def _cancel_ptz_stop(self, uid: str):
        prev_stop = self._ptz_stop_tasks.pop(uid, None)
        if prev_stop and not prev_stop.done():
            prev_stop.cancel()

    def _require_connected_proto(self, uid: str):
        """Return (sess, proto, None) if connected, or (sess_or_none, None, error_response)."""
        sess = self.sessions.get(uid)
        if not sess:
            return None, None, web.json_response({"ok": False, "error": "Không tìm thấy camera"}, status=404)
        if not sess.protocol or not sess.protocol.enc_mode:
            state = getattr(sess.camera, "state", "OFFLINE")
            return sess, None, web.json_response(
                {"ok": False, "error": f"Camera chưa kết nối ({state})", "state": state},
                status=503,
            )
        return sess, sess.protocol, None

    async def handle_ptz(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        sess, proto, err_resp = self._require_connected_proto(uid)
        if err_resp:
            return err_resp

        try:
            data = await request.json()
        except Exception:
            data = {}
        action = str(data.get("action", ""))
        conn_mode = getattr(sess.camera, "display_mode", None) or getattr(sess.camera, "connection_mode", "P2P")

        def done(acked: bool, **extra):
            body = {"ok": bool(acked), "action": action, "connection_mode": conn_mode, **extra}
            if not acked and "error" not in body:
                body["error"] = self.CMD_NO_ACK_MSG
            return web.json_response(body, status=200 if acked else (extra.get("status_code") or 504))

        def int_arg(name, lo, hi):
            v = int(float(data.get(name)))
            return max(lo, min(hi, v))

        async def send_setting_cgi(cgi: str, **extra):
            reply = await proto.query_cgi(cgi, timeout=4.5)
            if reply is None:
                return done(False, executed=False)
            masked = self._mask_secrets(reply)
            if reply.get("cgi") == "not support":
                return web.json_response(
                    {"ok": False, "action": action, "connection_mode": conn_mode,
                     "error": "Firmware camera không hỗ trợ lệnh này", "values": masked},
                    status=400,
                )
            res_val = str(reply.get("result", "")).lower()
            if res_val in ("-1", "-2", "erro", "error"):
                return web.json_response(
                    {"ok": False, "action": action, "connection_mode": conn_mode,
                     "error": f"Camera từ chối lệnh (result={reply.get('result')})", "values": masked},
                    status=400,
                )
            req_key = self._REQUIRED_REPLY_KEYS.get(action)
            if req_key and req_key[0] not in reply:
                return web.json_response(
                    {"ok": False, "action": action, "connection_mode": conn_mode,
                     "error": f"Firmware camera này không hỗ trợ {req_key[1]}", "values": masked},
                    status=400,
                )
            return done(True, executed=True, values=masked, **extra)

        try:
            # --- Directional movement ---
            if action in self.PTZ_MOVES:
                code = self.PTZ_MOVES[action]
                hold = bool(data.get("hold"))
                self._cancel_ptz_stop(uid)
                self._ptz_last_move[uid] = (code, asyncio.get_event_loop().time())
                acked = await proto.send_ptz_command(code, onestep=0 if hold else 1, wait_ack=3.0)
                # Step: stop after 0.75s. Hold: the browser sends "stop" on release;
                # this is only the safety net if that request never arrives.
                self._ptz_stop_tasks[uid] = asyncio.create_task(
                    self._delayed_ptz_stop(uid, proto, 12.0 if hold else 0.75))
                return done(acked)

            if action == "stop" or action.endswith("_stop") and action[:-5] in self.PTZ_MOVES:
                self._cancel_ptz_stop(uid)
                move = self._ptz_last_move.get(uid)
                if move:
                    # Let a quick tap produce a visible nudge before stopping
                    remaining = 0.35 - (asyncio.get_event_loop().time() - move[1])
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                return done(await self._ptz_send_stop(uid, proto, wait_ack=3.0))

            if action in self.PTZ_SIMPLE:
                self._cancel_ptz_stop(uid)
                return done(await proto.send_ptz_command(self.PTZ_SIMPLE[action], onestep=0, wait_ack=3.0))

            # --- Presets 1..16: presetN_set / presetN_go ---
            m = re.match(r"^preset(\d{1,2})_(set|go)$", action)
            if m and 1 <= int(m.group(1)) <= 16:
                self._cancel_ptz_stop(uid)
                code = 30 + 2 * (int(m.group(1)) - 1) + (1 if m.group(2) == "go" else 0)
                return done(await proto.send_ptz_command(code, onestep=0, wait_ack=3.0))

            # --- Image / IR / OSD / AI / Misc (wait for CGI execution reply) ---
            if action in self.PARAM_ACTIONS:
                param, val = self.PARAM_ACTIONS[action]
                return await send_setting_cgi(f"camera_control.cgi?param={param}&value={val}")

            if action in self.PARAM_VALUE_ACTIONS:
                param, lo, hi = self.PARAM_VALUE_ACTIONS[action]
                val = int_arg("value", lo, hi)
                return await send_setting_cgi(f"camera_control.cgi?param={param}&value={val}", value=val)

            if action == "param":      # raw camera_control.cgi?param=&value=
                param, val = int_arg("param", 0, 255), int_arg("value", -65535, 65535)
                return await send_setting_cgi(f"camera_control.cgi?param={param}&value={val}", param=param, value=val)

            if action in self.CGI_ACTIONS:
                cgi = self.CGI_ACTIONS[action]
                extra = {}
                if action == "humanframe_on" and data.get("sensitive") is not None:
                    sens = int_arg("sensitive", 0, 3)
                    cgi = f"{cgi}&sensitive={sens}"
                    extra["sensitive"] = sens
                resp = await send_setting_cgi(cgi, **extra)
                if resp.status == 200:
                    if action == "track_on":
                        self._set_camera_cfg_field(uid, track_enable=1)
                        proto.auto_track = 1
                        self._ai_checked_protos[uid] = id(proto)
                    elif action == "track_off":
                        self._set_camera_cfg_field(uid, track_enable=0)
                        proto.auto_track = 0
                        self._ai_checked_protos[uid] = id(proto)
                    elif action == "humanframe_on" and "sensitive" in extra:
                        self._set_camera_cfg_field(uid, human_sensitive=extra["sensitive"])
                        proto.human_sensitive = extra["sensitive"]
                        self._ai_checked_protos[uid] = id(proto)
                return resp

            if action == "rtsp_enable":
                val = int_arg("value", 0, 1)
                cur_port = data.get("port") or proto.cgi_state.get("rtspport") or "10554"
                port = max(1, min(65535, int(cur_port)))
                return await send_setting_cgi(f"set_rtsp.cgi?rtspenable={val}&rtspport={port}", value=val, port=port)

            if action == "enc_framerate":
                val = int_arg("value", 1, 30)
                kf = max(val, min(150, val * 2))
                resp = await send_setting_cgi(
                    f"set_media.cgi?mainrate=0&enc_framerate={val}&enc_keyframe={kf}",
                    value=val,
                    enc_keyframe=kf,
                )
                if resp.status == 200:
                    self._set_camera_cfg_field(uid, target_fps=val)
                    self._fps_checked_protos[uid] = id(proto)
                return resp

            if action in self.CGI_VALUE_ACTIONS:
                tmpl, lo, hi = self.CGI_VALUE_ACTIONS[action]
                val = int_arg("value", lo, hi)
                resp = await send_setting_cgi(tmpl.format(v=val), value=val)
                if resp.status == 200:
                    if action == "human_sensitive":
                        self._set_camera_cfg_field(uid, human_sensitive=val)
                        proto.human_sensitive = val
                        self._ai_checked_protos[uid] = id(proto)
                    elif action == "motion_sensitivity":
                        self._set_camera_cfg_field(uid, motion_sensitivity=val)
                        proto.motion_sensitivity = val
                        self._ai_checked_protos[uid] = id(proto)
                return resp
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "Giá trị không hợp lệ"}, status=400)

        return web.json_response({"ok": False, "error": f"Unknown action: {action}"}, status=400)

    async def _delayed_ptz_stop(self, uid: str, proto: proxy.PPPPUnifiedProtocol, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._ptz_send_stop(uid, proto)
        except (asyncio.CancelledError, Exception):
            pass

    def _mask_secrets(self, values: dict) -> dict:
        return {k: ("***" if (v and self._SECRET_KEY_RE.search(k)) else v) for k, v in values.items()}

    # Read-only queries shown in the dashboard, in display order. All verified to
    # answer on VSTJ firmware 48.82.230.19 (get_alarm.cgi is "not support" there:
    # alarm settings live in get_params.cgi as alarm_*).
    PARAM_QUERIES = (
        ("get_status.cgi", "Trạng thái & tính năng hỗ trợ"),
        ("get_camera_params.cgi", "Hình ảnh / âm lượng"),
        ("get_misc.cgi", "PTZ & đèn báo"),
        ("trans_cmd_string.cgi?cmd=2127&command=1", "AI: bám theo người (2127)"),
        ("trans_cmd_string.cgi?cmd=2126&command=1", "AI: khung người (2126)"),
        ("get_record.cgi", "Mã hoá & ghi hình"),
        ("get_params.cgi", "Toàn bộ cấu hình"),
    )
    CMD_BUSY_MSG = ("Camera chưa trả lời kịp. Camera này xử lý lệnh tuần tự và rất chậm khi có "
                    "nhiều thiết bị cùng kết nối (app Eye4, hub khác...). Thử lại sau ít giây.")

    async def handle_camera_params(self, request: web.Request) -> web.Response:
        """Read the camera's live settings + capability flags over the command channel."""
        uid = request.match_info.get("uid", "")
        sess, proto, err_resp = self._require_connected_proto(uid)
        if err_resp:
            return err_resp
        try:
            timeout = max(3.0, min(60.0, float(request.query.get("timeout", 12))))
        except ValueError:
            timeout = 12.0
        replies = await proto.query_cgis([q for q, _ in self.PARAM_QUERIES], timeout=timeout)
        sections = []
        for cgi, title in self.PARAM_QUERIES:
            values = replies.get(cgi)
            sections.append({
                "cgi": cgi, "title": title,
                "values": None if values is None else self._mask_secrets(values),
            })
        answered = sum(1 for sec in sections if sec["values"] is not None)
        return web.json_response({
            "ok": answered > 0,
            "partial": 0 < answered < len(sections),
            "error": None if answered else self.CMD_BUSY_MSG,
            "connection_mode": getattr(sess.camera, "connection_mode", "P2P"),
            "sections": sections,
            "channel": {
                "unacked": len(proto._cmd_unacked),
                "retransmits": proto._cmd_retx_count,
            },
        }, status=200 if answered else 504)

    # Full reverse-engineered catalog, chunked into small reliable batches of (group, cgi, title).
    SETTINGS_QUERY_BATCHES = (
        (
            ("system", "get_status.cgi", "Trạng thái hệ thống, phần cứng & năng lực (have*/support_*)"),
            ("image", "get_camera_params.cgi", "Cảm biến hình ảnh, âm lượng & luồng camera"),
            ("ptz", "get_misc.cgi", "Động cơ PTZ, tốc độ xoay, giới hạn mềm & đèn báo"),
            ("record", "get_record.cgi", "Bộ mã hóa Video (Main/Sub/Sub2) & thẻ nhớ SD"),
            ("network", "get_rtsp.cgi", "Cấu hình RTSP Server (Port/Auth)"),
            ("network", "get_onvif.cgi", "Giao thức ONVIF"),
            ("system", "get_login.cgi", "Tài khoản đang đăng nhập & quyền hạn"),
            ("system", "check_user.cgi", "Số phiên người dùng đồng thời"),
        ),
        (
            ("params", "get_params.cgi", "Toàn bộ cấu hình lõi (Mạng, Wi-Fi, NTP, Báo động, Lịch trình, Dịch vụ)"),
            ("cloud", "get_factory_param.cgi", "Tham số Nhà sản xuất, Push Server, Cloud & Đơn hàng"),
            ("network", "get_wifi_scan_result.cgi", "Bộ đệm kết quả quét Wi-Fi xung quanh"),
            ("record", "get_record_file.cgi", "Danh sách file ghi trên thẻ nhớ SD"),
            ("system", "get_log.cgi", "Nhật ký hệ thống trên camera"),
        ),
        (
            ("record", "trans_cmd_string.cgi?cmd=2105&command=1", "Chuẩn mã hóa Video H.264 / H.265 (cmd=2105)"),
            ("network", "trans_cmd_string.cgi?cmd=2122&command=1", "Bảo mật RTSP qua TLS (cmd=2122)"),
            ("ai", "trans_cmd_string.cgi?cmd=2127&command=1", "AI: Tự động bám theo người (cmd=2127)"),
            ("ai", "trans_cmd_string.cgi?cmd=2126&command=1", "AI: Độ nhạy phát hiện người & Khung người (cmd=2126)"),
            ("ai", "trans_cmd_string.cgi?cmd=2112&command=1", "AI: Phát hiện tiếng khóc (cmd=2112)"),
        ),
        (
            ("ai", "trans_cmd_string.cgi?cmd=2136&command=1", "AI: Thời gian phát hiện rời vị trí (cmd=2136)"),
            ("ai", "trans_cmd_string.cgi?cmd=2137&command=1", "AI: Cảnh báo khói / mở rộng (cmd=2137)"),
            ("ai", "trans_cmd_string.cgi?cmd=2123&command=1", "AI: Lưới vùng phát hiện chuyển động 18×22 (cmd=2123)"),
            ("image", "trans_cmd_string.cgi?cmd=2016&command=1", "Ống kính / Tỉ lệ khung (cmd=2016)"),
        ),
        (
            ("schedule", "trans_cmd_string.cgi?cmd=2017&command=11&mark=123456&type=1", "Lịch trình: Ghi hình chuyển động (cmd=2017 type=1)"),
            ("schedule", "trans_cmd_string.cgi?cmd=2017&command=11&mark=123456&type=2", "Lịch trình: Đẩy cảnh báo chuyển động [v.19] (cmd=2017 type=2)"),
            ("schedule", "trans_cmd_string.cgi?cmd=2017&command=11&mark=123456&type=4", "Lịch trình: Ghi hẹn giờ [v.19] / Đẩy CĐ [v.12] (cmd=2017 type=4)"),
            ("schedule", "trans_cmd_string.cgi?cmd=2017&command=11&mark=123456&type=5", "Lịch trình: Chế độ Chăm sóc / Y tá [nurse_plan] (cmd=2017 type=5)"),
            ("schedule", "trans_cmd_string.cgi?cmd=2017&command=11&mark=123456&type=7", "Lịch trình: Phát hiện rời vị trí [v.19] (cmd=2017 type=7)"),
        ),
    )

    async def handle_camera_settings(self, request: web.Request) -> web.Response:
        """Query all reverse-engineered camera CGIs and return structured per-CGI settings."""
        uid = request.match_info.get("uid", "")
        sess, proto, err_resp = self._require_connected_proto(uid)
        if err_resp:
            return err_resp
        try:
            timeout = max(5.0, min(90.0, float(request.query.get("timeout", 25))))
        except ValueError:
            timeout = 25.0

        t0 = asyncio.get_event_loop().time()
        all_replies = {}
        per_batch_timeout = max(6.0, timeout / 2.5)
        for batch in self.SETTINGS_QUERY_BATCHES:
            remaining = timeout - (asyncio.get_event_loop().time() - t0)
            if remaining <= 1.0:
                break
            cgis = [cgi for _, cgi, _ in batch]
            batch_replies = await proto.query_cgis(cgis, timeout=min(per_batch_timeout, remaining))
            all_replies.update(batch_replies)

        sections = []
        total_keys = 0
        supported_cgis = 0
        meta_keys = {"result", "cmd", "command", "uid", "mask", "type", "status"}
        for batch in self.SETTINGS_QUERY_BATCHES:
            for group, cgi, title in batch:
                vals = all_replies.get(cgi)
                masked = None if vals is None else self._mask_secrets(vals)
                is_supported = masked is not None and masked.get("cgi") != "not support"
                if is_supported:
                    supported_cgis += 1
                    total_keys += len(masked)
                has_payload = bool(is_supported and any(k not in meta_keys for k in masked))
                sections.append({
                    "group": group,
                    "cgi": cgi,
                    "title": title,
                    "supported": is_supported,
                    "has_payload": has_payload,
                    "key_count": len(masked) if masked else 0,
                    "values": masked,
                })

        elapsed_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        rec = self.recorders.get(uid)
        return web.json_response({
            "ok": supported_cgis > 0,
            "partial": 0 < supported_cgis < len(sections),
            "error": None if supported_cgis else self.CMD_BUSY_MSG,
            "uid": uid,
            "room_name": rec.room_name if rec else getattr(sess.camera, "label", uid),
            "did": getattr(sess.camera, "formatted_did", None) or getattr(sess.camera, "hw_deviceid", ""),
            "connection_mode": getattr(sess.camera, "connection_mode", "P2P"),
            "elapsed_ms": elapsed_ms,
            "answered_count": supported_cgis,
            "total_count": len(sections),
            "total_keys": total_keys,
            "sections": sections,
            "channel": {
                "unacked": len(proto._cmd_unacked),
                "retransmits": proto._cmd_retx_count,
            },
        }, status=200 if supported_cgis else 504)

    async def handle_camera_cgi(self, request: web.Request) -> web.Response:
        """Raw CGI console: send any CGI (login params are appended) and return the reply."""
        uid = request.match_info.get("uid", "")
        sess, proto, err_resp = self._require_connected_proto(uid)
        if err_resp:
            return err_resp
        try:
            data = await request.json()
        except Exception:
            data = {}
        cgi = str(data.get("cgi") or "").strip()
        if cgi.upper().startswith("GET "):
            cgi = cgi[4:].strip()
        cgi = cgi.lstrip("/")
        if not re.match(r"^[A-Za-z0-9_]+\.cgi(\?[\x21-\x7e]*)?$", cgi) or len(cgi) > 600:
            return web.json_response(
                {"ok": False, "error": "CGI không hợp lệ (dạng: ten_lenh.cgi?a=1&b=2)"}, status=400)
        if any(b in cgi.lower() for b in self.CGI_BLOCKLIST):
            return web.json_response(
                {"ok": False, "error": "Lệnh này bị chặn vì ghi đè tham số gốc/firmware của camera"}, status=403)
        values = await sess.protocol.query_cgi(cgi, timeout=12.0)
        if values is None:
            return web.json_response({"ok": False, "error": self.CMD_BUSY_MSG}, status=504)
        return web.json_response({
            "ok": True,
            "connection_mode": getattr(sess.camera, "connection_mode", "P2P"),
            "values": self._mask_secrets(values),
        })

    async def handle_camera_reboot(self, request: web.Request) -> web.Response:
        """Send hardware reboot command to camera over active PPPP session (LAN or WAN)."""
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol:
            return web.json_response({"ok": False, "error": "Camera chưa kết nối"}, status=404)
        try:
            if not await sess.protocol.send_reboot(wait_ack=5.0):
                return web.json_response({"ok": False, "error": self.CMD_NO_ACK_MSG}, status=504)
            conn_mode = getattr(sess.camera, "display_mode", None) or getattr(sess.camera, "connection_mode", "P2P")
            return web.json_response({
                "ok": True,
                "message": f"Đã gửi lệnh khởi động lại camera ({conn_mode})!",
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_camera_reconnect(self, request: web.Request) -> web.Response:
        """Force a fresh P2P hole-punch and lowest-RTT Cloud Relay selection for this camera."""
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess:
            return web.json_response({"ok": False, "error": "Không tìm thấy session camera"}, status=404)
        try:
            started = await sess.force_reconnect()
            if not started:
                return web.json_response({"ok": False, "error": "Camera đang trong quá trình kết nối lại..."})
            room_name = self._get_room_name(sess.camera)
            return web.json_response({
                "ok": True,
                "message": f"Đang thử đục lỗ WAN P2P / chọn lại Relay nhanh nhất cho {room_name}...",
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_change_password(self, request: web.Request) -> web.Response:
        """Update camera password via set_user.cgi over active PPPP session (LAN or WAN)."""
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol:
            return web.json_response({"ok": False, "error": "Camera chưa kết nối"}, status=404)
        try:
            data = await request.json()
            new_pwd = str(data.get("password") or "").strip()
            if not new_pwd:
                return web.json_response({"ok": False, "error": "Vui lòng nhập mật khẩu mới"}, status=400)

            await sess.protocol.send_set_password(new_pwd)
            cam_cfg = self.config.setdefault("cameras", {}).get(uid)
            if isinstance(cam_cfg, dict):
                cam_cfg["password"] = new_pwd
            else:
                self.config["cameras"][uid] = {"port": sess.rtsp_port, "password": new_pwd}
            self._save_hub_config()
            conn_mode = getattr(sess.camera, "display_mode", None) or getattr(sess.camera, "connection_mode", "P2P")
            return web.json_response({
                "ok": True,
                "message": f"Đã cập nhật mật khẩu camera thành công qua {conn_mode}!",
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def handle_record_control(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        rec = self.recorders.get(uid)
        if not rec:
            return web.json_response({"ok": False, "error": "Recorder not found"}, status=404)

        data = await request.json()
        action = data.get("action", "trigger")
        if action == "trigger":
            rec.trigger_motion(reason="MANUAL_TEST")
        elif action == "manual_on":
            rec.set_manual_recording(True)
        elif action == "manual_off":
            rec.set_manual_recording(False)
        return web.json_response({
            "ok": True,
            "is_recording": rec.is_recording,
            "manual_record": rec.manual_record,
        })

    async def handle_rename(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        rec = self.recorders.get(uid)

        data = await request.json()
        new_name = str(data.get("room_name", "")).strip()
        if not new_name:
            return web.json_response({"ok": False, "error": "Name cannot be empty"}, status=400)

        cam_cfg = self.config.setdefault("cameras", {}).get(uid)
        if sess and rec:
            rec.set_room_name(new_name)
            sess.camera.label = f"{uid[:8]} ({new_name})"
            if isinstance(cam_cfg, dict):
                cam_cfg["name"] = new_name
            else:
                self.config["cameras"][uid] = {"port": sess.rtsp_port, "name": new_name}
            self._save_hub_config()
            if sess.protocol:
                await sess.protocol.send_set_alias(new_name)
            return web.json_response({"ok": True, "room_name": new_name})
        elif isinstance(cam_cfg, dict):
            cam_cfg["name"] = new_name
            self._save_hub_config()
            return web.json_response({"ok": True, "room_name": new_name})

        return web.json_response({"ok": False, "error": "Camera not found"}, status=404)

    async def handle_decode_qr(self, request: web.Request) -> web.Response:
        """Decode an uploaded camera QR sticker photo (multipart or raw bytes) and return parsed UID info."""
        try:
            image_bytes = b""
            raw_text = ""
            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                async for field in reader:
                    if field.name in ("file", "image", "qr"):
                        image_bytes = await field.read(decode=False)
                        break
                    elif field.name == "text":
                        raw_text = (await field.read(decode=True)).decode("utf-8", errors="ignore")
            elif request.content_type == "application/json":
                body = await request.json()
                raw_text = str(body.get("text", "")).strip()
            else:
                image_bytes = await request.read()

            if image_bytes:
                info = await asyncio.get_event_loop().run_in_executor(None, decode_camera_qr_image, image_bytes)
            elif raw_text:
                info = parse_qr_or_did_text(raw_text)
            else:
                return web.json_response({"ok": False, "error": "Chưa chọn file ảnh QR hoặc mã UID"}, status=400)

            uid_hex = info["uid_hex"]
            existing_cfg = (self.config.get("cameras") or {}).get(uid_hex)
            total_cams = len(set(list(self.sessions.keys()) + list((self.config.get("cameras") or {}).keys())))
            suggested_name = (
                existing_cfg.get("name")
                if isinstance(existing_cfg, dict) and existing_cfg.get("name")
                else f"Phong_{101 + total_cams}"
            )

            return web.json_response({
                "ok": True,
                **info,
                "suggested_name": suggested_name,
                "already_online": uid_hex in self.sessions,
                "already_registered": uid_hex in (self.config.get("cameras") or {}),
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_add_camera_qr(self, request: web.Request) -> web.Response:
        """Register a camera by QR / sticker UID (and optional name/password/IP) and start WAN P2P + LAN session."""
        try:
            data = await request.json()
            did_input = str(
                data.get("device_id")
                or data.get("uid")
                or data.get("did")
                or data.get("qr_text")
                or data.get("text")
                or ""
            ).strip()
            room_name = str(data.get("room_name") or data.get("name") or "").strip()
            cam_password = str(data.get("password") or "").strip()
            cam_username = str(data.get("username") or "").strip()
            target_ip = str(data.get("ip") or "").strip()
            model = str(data.get("model") or "CS22S").strip()

            if not did_input:
                return web.json_response({"ok": False, "error": "Vui lòng nhập mã UID hoặc tải ảnh QR"}, status=400)

            info = parse_qr_or_did_text(did_input)
            uid_hex = info["uid_hex"]
            compact_did = info["device_id"]
            formatted_did = info["formatted_did"]

            cameras_dict = self.config.setdefault("cameras", {})
            base_port = int(self.config.get("base_port", 9555))
            port_val = proxy.assign_port(self.config, uid_hex, base_port)

            if not room_name:
                room_name = f"Phong_{100 + len(cameras_dict)}"

            cam_entry = cameras_dict.get(uid_hex)
            if not isinstance(cam_entry, dict):
                cam_entry = {"port": port_val}
            cam_entry["port"] = port_val
            cam_entry["name"] = room_name
            cam_entry["did"] = formatted_did
            cam_entry["hw_deviceid"] = compact_did
            cam_entry["formatted_did"] = formatted_did
            cam_entry["model"] = model or info.get("model", "CS22S")
            if target_ip:
                cam_entry["ip"] = target_ip
            if cam_username:
                cam_entry["username"] = cam_username
            if cam_password:
                cam_entry["password"] = cam_password
            cameras_dict[uid_hex] = cam_entry
            self._save_hub_config()

            # If already in sessions, update its room name and credentials immediately
            if uid_hex in self.sessions:
                sess = self.sessions[uid_hex]
                rec = self.recorders.get(uid_hex)
                if rec:
                    rec.set_room_name(room_name)
                sess.camera.label = f"{uid_hex[:8]} ({room_name})"
                if cam_password:
                    sess.password = cam_password
                    if sess.protocol:
                        sess.protocol.password = cam_password
                        sess.protocol.auth_failed = False
                        sess.protocol.auth_error = None
                        asyncio.create_task(sess.protocol._send_login())
                        asyncio.create_task(sess.protocol._send_start_video())
                        asyncio.create_task(sess.protocol._send_start_audio())
                if sess.protocol and sess.state == "CONNECTED":
                    asyncio.create_task(sess.protocol.send_set_alias(room_name))
                return web.json_response({
                    "ok": True,
                    "uid": uid_hex,
                    "did": formatted_did,
                    "formatted_did": formatted_did,
                    "room_name": room_name,
                    "name": room_name,
                    "rtsp_port": port_val,
                    "connection_mode": getattr(sess.camera, "connection_mode", "WAN_P2P"),
                    "connected": sess.state == "CONNECTED",
                    "message": f"Camera {formatted_did} ({room_name}) đã được cập nhật và đang kết nối ({ getattr(sess.camera, 'connection_mode', 'WAN_P2P') })!",
                })

            # Immediately start hybrid WAN P2P + LAN session so camera connects over the Internet or LAN
            if hasattr(proxy.CameraInfo, "from_did"):
                cam = proxy.CameraInfo.from_did(
                    uid_hex,
                    ip=target_ip or cam_entry.get("ip", "0.0.0.0"),
                    port=int(cam_entry.get("wan_port", 32108)),
                )
            else:
                cam = proxy.CameraInfo(
                    target_ip or cam_entry.get("ip", "0.0.0.0"),
                    int(cam_entry.get("wan_port", 32108)),
                    bytes.fromhex(uid_hex),
                )
                cam.hw_deviceid = compact_did
                cam.formatted_did = formatted_did
                cam.connection_mode = "LAN" if target_ip else "WAN_P2P"

            log.info("Starting immediate WAN P2P + LAN session for added camera %s (%s)", formatted_did, room_name)
            await self.start_camera_session(cam)

            # Also trigger background LAN broadcast scan in case camera is on local subnet
            asyncio.create_task(self._scan_and_connect_now(target_ip or self.target_ip))

            sess = self.sessions.get(uid_hex)
            return web.json_response({
                "ok": True,
                "uid": uid_hex,
                "did": formatted_did,
                "formatted_did": formatted_did,
                "room_name": room_name,
                "name": room_name,
                "rtsp_port": port_val,
                "connection_mode": getattr(cam, "connection_mode", "WAN_P2P"),
                "connected": bool(sess and sess.state == "CONNECTED"),
                "message": (
                    f"Đã thêm camera {formatted_did} ({room_name}, cổng RTSP :{port_val}) vào Hub! "
                    f"Đang kết nối tự động qua WAN P2P (Internet) & LAN..."
                ),
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    handle_add_camera = handle_add_camera_qr

    async def handle_onboard_camera(self, request: web.Request) -> web.Response:
        """
        Unified 1-Click Onboarding Endpoint (`POST /api/camera/onboard`).
        1. Parses UID / QR sticker string (`uid_hex`, `formatted_did`).
        2. Registers the camera in `self.config["cameras"][uid_hex]` (defaulting `cam_password` to `"888888"`).
        3. Starts or updates `self.sessions[uid_hex]`, sets `sess.camera.onboarding_until = time.monotonic() + 180.0`,
           and calls `sess.activate_onboarding(180.0)` if available.
        4. If `wifi_ssid` is provided, generates both the Sonic Wave WAV (`mode="ssid"`) and Optical QR Code (`"eye4"`).
        """
        try:
            data = await request.json()
            did_input = str(
                data.get("uid")
                or data.get("device_id")
                or data.get("did")
                or data.get("qr_text")
                or data.get("text")
                or ""
            ).strip()
            room_name = str(data.get("room_name") or data.get("name") or "").strip()
            cam_password = str(data.get("cam_password") or data.get("password") or "888888").strip() or "888888"
            cam_username = str(data.get("cam_username") or data.get("username") or "admin").strip() or "admin"
            wifi_ssid = str(data.get("wifi_ssid") or data.get("ssid") or "").strip()
            wifi_password = str(data.get("wifi_password") if "wifi_password" in data else (data.get("wifi_pwd") or ""))
            wifi_bssid = str(data.get("wifi_bssid") or data.get("bssid") or "").strip()
            model = str(data.get("model") or "CS22S").strip()

            if not did_input:
                return web.json_response({"ok": False, "error": "Vui lòng nhập mã UID hoặc tải ảnh tem QR trên camera"}, status=400)

            info = parse_qr_or_did_text(did_input)
            uid_hex = info["uid_hex"]
            compact_did = info["device_id"]
            formatted_did = info["formatted_did"]

            cameras_dict = self.config.setdefault("cameras", {})
            base_port = int(self.config.get("base_port", 9555))
            port_val = proxy.assign_port(self.config, uid_hex, base_port)

            if not room_name:
                existing = cameras_dict.get(uid_hex)
                if isinstance(existing, dict) and existing.get("name"):
                    room_name = str(existing["name"])
                else:
                    room_name = f"Phong_{101 + len(cameras_dict)}"

            cam_entry = cameras_dict.get(uid_hex)
            if not isinstance(cam_entry, dict):
                cam_entry = {"port": port_val}
            cam_entry["port"] = port_val
            cam_entry["name"] = room_name
            cam_entry["did"] = formatted_did
            cam_entry["hw_deviceid"] = compact_did
            cam_entry["formatted_did"] = formatted_did
            cam_entry["model"] = model or info.get("model", "CS22S")
            cam_entry["username"] = cam_username
            cam_entry["password"] = cam_password
            cameras_dict[uid_hex] = cam_entry
            self._save_hub_config()

            if uid_hex in self.sessions:
                sess = self.sessions[uid_hex]
                rec = self.recorders.get(uid_hex)
                if rec:
                    rec.set_room_name(room_name)
                sess.camera.label = f"{uid_hex[:8]} ({room_name})"
                sess.username = cam_username
                sess.password = cam_password
                if sess.protocol:
                    sess.protocol.username = cam_username
                    sess.protocol.password = cam_password
                    sess.protocol.auth_failed = False
                    sess.protocol.auth_error = None
            else:
                cam = self._make_wan_camera_info(uid_hex, cam_entry)
                log.info("Onboarding new camera %s (%s) over WAN P2P + LAN", formatted_did, room_name)
                await self.start_camera_session(cam)

            sess = self.sessions.get(uid_hex)
            if sess:
                sess.camera.onboarding_until = time.monotonic() + 180.0
                if hasattr(sess, "activate_onboarding"):
                    act_res = sess.activate_onboarding(180.0)
                    if asyncio.iscoroutine(act_res):
                        await act_res

            # Also trigger a background LAN broadcast scan in case the Hub is on the same LAN
            asyncio.create_task(self._scan_and_connect_now(self.target_ip))

            wav_data_url = ""
            qr_data_url = ""
            qr_text = ""
            duration_sec = 0.0
            requested_mode = str(data.get("sonic_mode") or data.get("mode") or ("mac" if wifi_bssid else "ssid")).strip()
            sonic_mode = requested_mode
            if wifi_ssid:
                sonic_res = await asyncio.get_event_loop().run_in_executor(
                    None, generate_vstarcam_sonic_wav, wifi_ssid, wifi_password, wifi_bssid, requested_mode
                )
                wav_b64 = base64.b64encode(sonic_res["wav_bytes"]).decode("ascii")
                wav_data_url = f"data:audio/wav;base64,{wav_b64}"
                duration_sec = sonic_res["duration_sec"]
                sonic_mode = sonic_res["mode"]
                try:
                    qr_res = await asyncio.get_event_loop().run_in_executor(
                        None, generate_wifi_qr_png_data_url, wifi_ssid, wifi_password, wifi_bssid, "eye4"
                    )
                    qr_data_url = qr_res.get("data_url", "")
                    qr_text = qr_res.get("qr_text", "")
                except Exception as qr_err:
                    log.debug("Could not generate optical Wi-Fi QR: %s", qr_err)

            return web.json_response({
                "ok": True,
                "uid": uid_hex,
                "did": formatted_did,
                "formatted_did": formatted_did,
                "device_id": compact_did,
                "room_name": room_name,
                "name": room_name,
                "rtsp_port": port_val,
                "connection_mode": getattr(sess.camera, "connection_mode", "WAN_P2P") if sess else "WAN_P2P",
                "wan_cloud_status": getattr(sess.camera, "wan_cloud_status", "IDLE") if sess else "IDLE",
                "onboarding_active": True,
                "onboarding_duration_sec": 180.0,
                "connected": bool(sess and sess.state == "CONNECTED"),
                "wifi_ssid": wifi_ssid,
                "wav_data_url": wav_data_url,
                "qr_data_url": qr_data_url,
                "qr_text": qr_text,
                "duration_sec": duration_sec,
                "mode": sonic_mode,
                "message": (
                    f"Đã kích hoạt chế độ Khui hộp & Nạp Wi-Fi cho camera {formatted_did} ({room_name})! "
                    f"Server đang dò tìm liên tục trên P2P Cloud trong 180 giây..."
                    if wifi_ssid else
                    f"Đã liên kết camera {formatted_did} ({room_name}) lên Server! "
                    f"Server đang dò tìm và bắt tay qua WAN P2P & LAN..."
                ),
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _scan_and_connect_now(self, extra_ip: Optional[str] = None) -> int:
        found_new = 0
        try:
            connected_ips = {
                s.camera.ip for s in self.sessions.values()
                if s.camera.ip and s.camera.ip != "0.0.0.0"
            }
            cams = await proxy.discover_cameras_broadcast(timeout=2.5, target_ip=extra_ip or self.target_ip, skip_ips=connected_ips)
            for cam in cams:
                if cam.uid not in self.sessions:
                    log.info("Discovered camera during active scan: %s", cam)
                    await self.start_camera_session(cam)
                    found_new += 1
                else:
                    sess = self.sessions[cam.uid]
                    if sess.camera.ip in ("0.0.0.0", "") and cam.ip not in ("0.0.0.0", ""):
                        sess.camera.ip = cam.ip
                        sess.camera.port = cam.port
                        sess.camera.connection_mode = "LAN"
                        found_new += 1
        except Exception as e:
            log.debug("Active scan error: %s", e)
        return found_new

    async def handle_scan_lan(self, request: web.Request) -> web.Response:
        found_new = await self._scan_and_connect_now(self.target_ip)
        return web.json_response({
            "ok": True,
            "found_new": found_new,
            "active_count": len(self.sessions),
        })

    async def handle_delete_camera(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        sess = self.sessions.pop(uid, None)
        if sess:
            await sess.stop()
        rec = self.recorders.pop(uid, None)
        if rec:
            await rec.close()
        cams_cfg = self.config.get("cameras", {})
        if isinstance(cams_cfg, dict) and uid in cams_cfg:
            del cams_cfg[uid]
            self._save_hub_config()
        return web.json_response({"ok": True})

    async def handle_wifi_info(self, request: web.Request) -> web.Response:
        """Scan host's current Wi-Fi network, nearby SSIDs, and saved password."""
        info = await asyncio.get_event_loop().run_in_executor(None, scan_host_wifi_info)
        return web.json_response({"ok": True, **info})


    async def handle_wifi_sonic(self, request: web.Request) -> web.Response:
        """Generate VStarcam Sonic Wave WAV + Phone Helper QR URL."""
        try:
            data = await request.json()
            ssid = str(data.get("ssid") or "").strip()
            password = str(data.get("password") or "")
            bssid = str(data.get("bssid") or "").strip()
            mode = str(data.get("mode") or "combo").strip()
            if not ssid:
                return web.json_response({"ok": False, "error": "Vui lòng nhập Tên mạng Wi-Fi (SSID)"}, status=400)

            res = await asyncio.get_event_loop().run_in_executor(
                None, generate_vstarcam_sonic_wav, ssid, password, bssid, mode
            )
            wav_b64 = base64.b64encode(res["wav_bytes"]).decode("ascii")
            proto = request.headers.get("X-Forwarded-Proto", request.scheme)
            host = request.headers.get("X-Forwarded-Host", request.host)
            qs = urllib.parse.urlencode({
                "ssid": ssid,
                "password": password,
                "bssid": res["bssid_24"],
                "mode": res["mode"],
            })
            mobile_url = f"{proto}://{host}/sonic?{qs}"

            # Generate a QR code pointing to `mobile_url` so the user can scan with their phone
            # if their desktop PC doesn't have external speakers!
            mobile_qr_data_url = ""
            if HAS_QRCODE and HAS_CV2:
                qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
                qr.add_data(mobile_url)
                qr.make(fit=True)
                mat = np.array(qr.get_matrix(), dtype=np.uint8)
                img = np.where(mat == 1, 0, 255).astype(np.uint8)
                img_big = np.repeat(np.repeat(img, 8, axis=0), 8, axis=1)
                ok, buf = cv2.imencode(".png", img_big)
                if ok:
                    mobile_qr_data_url = "data:image/png;base64," + base64.b64encode(bytes(buf)).decode("ascii")

            return web.json_response({
                "ok": True,
                "wav_data_url": f"data:audio/wav;base64,{wav_b64}",
                "duration_sec": res["duration_sec"],
                "bssid_24": res["bssid_24"],
                "bssid_suffix": res["bssid_suffix"],
                "mac_hex_payload": res["mac_hex_payload"],
                "ssid_hex_payload": res["ssid_hex_payload"],
                "mode": res["mode"],
                "mobile_url": mobile_url,
                "mobile_qr_data_url": mobile_qr_data_url,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_wifi_sonic_wav(self, request: web.Request) -> web.Response:
        """Stream raw audio/wav for VStarcam Sonic Wave provisioning."""
        ssid = str(request.query.get("ssid") or "").strip()
        password = str(request.query.get("password") or "")
        bssid = str(request.query.get("bssid") or "").strip()
        mode = str(request.query.get("mode") or "combo").strip()
        if not ssid:
            return web.Response(status=400, text="Missing ssid")
        res = await asyncio.get_event_loop().run_in_executor(
            None, generate_vstarcam_sonic_wav, ssid, password, bssid, mode
        )
        return web.Response(
            body=res["wav_bytes"],
            content_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    async def handle_play_pc_speaker(self, request: web.Request) -> web.Response:
        """Play the VStarcam Sonic Wave directly on the Windows host's speaker via winsound."""
        try:
            data = await request.json()
            ssid = str(data.get("ssid") or "").strip()
            password = str(data.get("password") or "")
            bssid = str(data.get("bssid") or "").strip()
            mode = str(data.get("mode") or "combo").strip()
            if not ssid:
                return web.json_response({"ok": False, "error": "Vui lòng nhập Tên mạng Wi-Fi (SSID)"}, status=400)
            if not HAS_WINSOUND:
                return web.json_response({"ok": False, "error": "Hệ điều hành không hỗ trợ winsound"}, status=400)

            res = await asyncio.get_event_loop().run_in_executor(
                None, generate_vstarcam_sonic_wav, ssid, password, bssid, mode
            )

            def _play_sync(wav_bytes: bytes):
                try:
                    winsound.PlaySound(None, winsound.SND_PURGE)
                    winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
                except Exception as ex:
                    log.debug("winsound error: %s", ex)

            asyncio.get_event_loop().run_in_executor(None, _play_sync, res["wav_bytes"])
            return web.json_response({
                "ok": True,
                "duration_sec": res["duration_sec"],
                "bssid_suffix": res["bssid_suffix"],
                "message": f"Đang phát sóng âm nạp Wi-Fi '{ssid}' (2.4GHz BSSID: {res['bssid_24'] or 'SSID'}) trên Loa máy tính trong {res['duration_sec']} giây!",
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_mobile_sonic_page(self, request: web.Request) -> web.Response:
        """Standalone Mobile Onboarding & Provisioning Page (Branch 1: Link Existing vs Branch 2: Unbox & Wi-Fi)."""
        info = await asyncio.get_event_loop().run_in_executor(None, scan_host_wifi_info)
        ssid = request.query.get("ssid") or info.get("current_ssid") or ""
        password = request.query.get("password") or (info.get("saved_password") if ssid == info.get("current_ssid") else "") or ""
        bssid = request.query.get("bssid") or (info.get("current_bssid") if ssid == info.get("current_ssid") else "") or ""
        uid_q = request.query.get("uid") or request.query.get("did") or ""
        room_q = request.query.get("room_name") or ""
        mode = request.query.get("mode") or ("mac" if bssid else "ssid")
        qs = urllib.parse.urlencode({"ssid": ssid or "WiFi", "password": password, "bssid": bssid, "mode": mode})
        wav_url = f"/api/wifi/sonic.wav?{qs}"

        sonic_path = os.path.join(STATIC_DIR, "sonic.html")
        with open(sonic_path, "r", encoding="utf-8") as f:
            html = f.read()
        init_payload = json.dumps({
            "ssid": ssid,
            "password": password,
            "bssid": bssid,
            "uid": uid_q,
            "room": room_q,
            "mode": mode,
            "wav_url": wav_url,
        }).replace("</", "<\\/")
        init_tag = f'<script id="sonic-init" type="application/json">{init_payload}</script>'
        assert "<!--SONIC_INIT_JSON-->" in html, "sonic.html missing <!--SONIC_INIT_JSON-->"
        html = html.replace("<!--SONIC_INIT_JSON-->", init_tag, 1)
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    async def handle_wifi_qr(self, request: web.Request) -> web.Response:
        """Generate optical Wi-Fi provisioning QR code for the camera lens to scan."""
        try:
            data = await request.json()
            ssid = str(data.get("ssid") or "").strip()
            password = str(data.get("password") or "")
            bssid = str(data.get("bssid") or "").strip()
            fmt = str(data.get("format") or "eye4").strip()
            if not ssid:
                return web.json_response({"ok": False, "error": "Vui lòng nhập Tên mạng Wi-Fi (SSID)"}, status=400)
            res = await asyncio.get_event_loop().run_in_executor(
                None, generate_wifi_qr_png_data_url, ssid, password, bssid, fmt
            )
            return web.json_response({"ok": True, **res})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    # ------------------------------------------------------------------
    # Verified remote Wi-Fi switch (camera already online through the hub)
    # ------------------------------------------------------------------
    WIFI_SWITCH_DROP_WAIT = 25.0     # s to wait for the camera to leave the old network
    WIFI_SWITCH_BACK_WAIT = 240.0    # s to wait for it to come back (boot + join + P2P register)
    WIFI_SWITCH_LATE_WATCH = 900.0   # keep watching this long after a timeout

    @staticmethod
    def _parse_wifi_scan(values: Optional[dict]) -> list:
        aps = []
        if not values:
            return aps
        try:
            n = int(values.get("ap_number") or 0)
        except ValueError:
            n = 0
        for i in range(min(n, 64)):
            ssid = values.get(f"ap_ssid[{i}]")
            if ssid is None:
                continue
            try:
                signal = int(values.get(f"ap_dbm0[{i}]") or 0)
            except ValueError:
                signal = 0
            try:
                security = int(values.get(f"ap_security[{i}]") or 0)
            except ValueError:
                security = 0
            aps.append({"ssid": ssid, "signal": signal, "security": security})
        aps.sort(key=lambda a: -a["signal"])
        return aps

    @staticmethod
    def _net_fingerprint(params: Optional[dict]) -> Optional[tuple]:
        """(gateway, /24 of ip) identifying the network the camera is actually on."""
        if not params:
            return None
        ip, gw = params.get("ip") or "", params.get("gateway") or ""
        if not ip or ip == "0.0.0.0":
            return None
        return (gw, ".".join(ip.split(".")[:3]))

    async def _restore_old_wifi(self, uid: str, job: dict, new_ssid: str, step) -> bool:
        """Camera came back on its old network: re-save the old credentials so a later
        reboot does not strand it trying the unreachable new network."""
        old_ssid, old_psk, old_auth, old_enc = job.get("_old_creds") or (None, None, None, None)
        if not old_ssid or old_ssid == new_ssid or old_psk is None:
            step("rollback", "warn", "Không có cấu hình mạng cũ để khôi phục (camera chỉ lưu mạng mới)")
            return False
        sess = self.sessions.get(uid)
        proto = sess.protocol if sess else None
        if not proto or not proto.enc_mode:
            step("rollback", "error", "Camera mất kết nối, chưa khôi phục được cấu hình cũ")
            return False
        try:
            auth = int(old_auth) if old_auth not in (None, "") else 4
            enc = int(old_enc) if old_enc not in (None, "") else 4
        except ValueError:
            auth, enc = 4, 4
        step("rollback", "running", f"Khôi phục cấu hình Wi-Fi cũ '{old_ssid}'...")
        reply = await proto.query_cgi(proto.build_set_wifi_cgi(old_ssid, old_psk, auth, enc), timeout=12.0)
        ok = bool(reply) and str(reply.get("result", "")).strip().lower() in ("0", "ok")
        step("rollback", "ok" if ok else "error",
             f"Đã lưu lại cấu hình '{old_ssid}'" if ok else "Camera không xác nhận khôi phục")
        return ok

    def _wifi_job_public(self, job: Optional[dict]) -> Optional[dict]:
        if not job:
            return None
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["elapsed"] = round(time.monotonic() - job["_t0"], 1)
        return out

    async def _run_wifi_switch(self, uid: str, ssid: str, password: str, force: bool):
        job = self.wifi_jobs[uid]
        cam_name = job["camera"]

        def step(stage: str, status: str, message: str, **extra):
            entry = {"stage": stage, "status": status, "message": message,
                     "t": round(time.monotonic() - job["_t0"], 1), **extra}
            # A stage reports progress then a verdict: replace its running entry.
            if job["steps"] and job["steps"][-1]["stage"] == stage and job["steps"][-1]["status"] == "running":
                job["steps"][-1] = entry
            else:
                job["steps"].append(entry)
            job["stage"] = stage
            log.info("[Wi-Fi switch %s] %s %s: %s", cam_name, stage, status, message)

        def finish(result: str, message: str, advice: str = ""):
            job["state"] = "done"
            job["result"] = result          # success | fallback | failed | timeout
            job["message"] = message
            job["advice"] = advice
            job["finished_at"] = time.time()

        def live_proto():
            sess = self.sessions.get(uid)
            if sess and sess.protocol and sess.protocol.enc_mode and sess.state == "CONNECTED":
                return sess, sess.protocol
            return sess, None

        try:
            # 1. Current network
            step("precheck", "running", "Đọc cấu hình Wi-Fi hiện tại của camera...")
            sess, proto = live_proto()
            if not proto:
                step("precheck", "error", "Camera không còn online")
                return finish("failed", "Camera mất kết nối trước khi đổi Wi-Fi.",
                              "Đợi camera online lại rồi thử lại.")
            params = await proto.query_cgi("get_params.cgi", timeout=12.0) or {}
            # wifi_ssid is the SAVED config, not necessarily the network in use: the camera
            # keeps running on its current network until it reboots. ip/gateway identify
            # the network it is actually on.
            old_ssid = params.get("wifi_ssid")
            old_net = self._net_fingerprint(params)
            job["old_ssid"] = old_ssid
            job["old_net"] = f"{params.get('ip', '?')} / GW {params.get('gateway', '?')}"
            job["_old_creds"] = (old_ssid, params.get("wifi_wpa_psk"),
                                 params.get("wifi_authtype"), params.get("wifi_encrypt"))
            if old_ssid is None:
                step("precheck", "warn", "Không đọc được cấu hình Wi-Fi (camera bận) — tiếp tục")
            elif old_ssid == ssid:
                step("precheck", "warn",
                     f"Camera đã LƯU sẵn '{ssid}' nhưng vẫn chạy ở mạng {job['old_net']} "
                     "(chưa áp dụng) — gửi lại và áp dụng")
            else:
                step("precheck", "ok",
                     f"Cấu hình hiện tại '{old_ssid}' (IP {job['old_net']}) qua {sess.camera.display_mode}")

            # 2. Can the camera even see the target network?
            step("scan", "running", "Camera đang quét các mạng Wi-Fi xung quanh...")
            await proto.query_cgi("wifi_scan.cgi", timeout=10.0)
            await asyncio.sleep(3.0)
            sess, proto = live_proto()
            if not proto:
                step("scan", "error", "Camera mất kết nối khi đang quét")
                return finish("failed", "Camera mất kết nối khi đang quét Wi-Fi.")
            aps = self._parse_wifi_scan(await proto.query_cgi("get_wifi_scan_result.cgi", timeout=12.0))
            job["visible"] = [a["ssid"] for a in aps[:15]]
            target = next((a for a in aps if a["ssid"] == ssid), None)
            if target is None:
                near = next((a["ssid"] for a in aps if a["ssid"].lower() == ssid.lower()), None)
                hint = f" Có mạng '{near}' — tên phải khớp cả hoa/thường." if near else ""
                if not aps:
                    step("scan", "warn", "Camera không trả về danh sách quét — không kiểm tra được")
                elif not force:
                    step("scan", "error", f"Camera KHÔNG thấy mạng '{ssid}' trong {len(aps)} mạng quét được.{hint}")
                    return finish(
                        "failed",
                        f"Đã hủy trước khi đổi: camera không nhìn thấy Wi-Fi '{ssid}', camera vẫn ở mạng cũ.",
                        "Hotspot điện thoại phải phát băng tần 2.4GHz (tắt 'Ưu tiên 5GHz' / bật "
                        "'Tăng khả năng tương thích'), đặt gần camera, rồi thử lại. "
                        "Có thể chọn 'Vẫn gửi' nếu chắc chắn mạng tồn tại.")
                else:
                    step("scan", "warn", f"Không thấy '{ssid}' nhưng vẫn gửi theo yêu cầu")
            else:
                step("scan", "ok", f"Thấy '{ssid}' (sóng {target['signal']}%, bảo mật {target['security']})")
                if target["signal"] < 30:
                    step("scan", "warn", "Sóng yếu — camera có thể không giữ được kết nối")

            # 3. Push the credentials and require the camera's answer
            authtype = 0 if not password else (target["security"] if target and target["security"] else 4)
            encrypt = 0 if not password else 4
            step("apply", "running", f"Gửi cấu hình Wi-Fi '{ssid}' xuống camera...")
            reply = await proto.query_cgi(
                proto.build_set_wifi_cgi(ssid, password, authtype, encrypt), timeout=12.0)
            if reply is None:
                step("apply", "error", "Camera không phản hồi lệnh set_wifi")
                return finish("failed", "Lệnh đổi Wi-Fi không tới được camera (kênh lệnh bận/mất gói).",
                              "Camera vẫn ở mạng cũ. Thử lại sau vài giây.")
            res_val = str(reply.get("result", "")).strip().lower()
            if res_val not in ("0", "ok"):
                step("apply", "error", f"Camera từ chối cấu hình (result={res_val or '?'})")
                return finish("failed", f"Camera từ chối cấu hình Wi-Fi (result={res_val}).",
                              "Kiểm tra lại tên/mật khẩu Wi-Fi (WPA2, tối thiểu 8 ký tự).")
            step("apply", "ok", "Camera đã nhận cấu hình")

            # 4. Make it take effect: wait for the camera to leave the old network, reboot if it doesn't
            sess = self.sessions.get(uid)
            if sess:
                sess.activate_onboarding(self.WIFI_SWITCH_BACK_WAIT + 60)
            step("switch", "running", "Chờ camera rời mạng cũ để vào mạng mới...")
            left = False
            t_end = time.monotonic() + self.WIFI_SWITCH_DROP_WAIT
            loop = asyncio.get_event_loop()
            while time.monotonic() < t_end:
                await asyncio.sleep(1.0)
                sess = self.sessions.get(uid)
                if not sess or sess.state != "CONNECTED" or (loop.time() - sess._last_drw_time) > 6.0:
                    left = True
                    break
            if not left:
                step("switch", "warn", "Camera chưa tự chuyển mạng — khởi động lại để áp dụng")
                sess, proto = live_proto()
                if proto:
                    await proto.send_reboot(wait_ack=5.0)
            else:
                step("switch", "ok", "Camera đã ngắt khỏi mạng cũ")

            # 5. Wait for it to come back (on the new network, or the old one as fallback)
            step("reconnect", "running", "Chờ camera vào mạng mới và kết nối lại Hub...")
            saw_down = left
            t_end = time.monotonic() + self.WIFI_SWITCH_BACK_WAIT
            late = False
            while True:
                await asyncio.sleep(2.0)
                sess, proto = live_proto()
                if not proto:
                    saw_down = True
                elif saw_down and (loop.time() - sess._last_drw_time) < 5.0:
                    break
                if time.monotonic() > t_end and not late:
                    late = True
                    job["state"] = "watching"
                    step("reconnect", "error",
                         f"Camera chưa quay lại sau {int(self.WIFI_SWITCH_BACK_WAIT)}s — Hub vẫn tiếp tục chờ")
                    finish("timeout",
                           f"Camera chưa kết nối lại sau {int(self.WIFI_SWITCH_BACK_WAIT // 60)} phút.",
                           f"Có thể camera không vào được '{ssid}' (sai mật khẩu / hotspot 5GHz / hết sóng). "
                           f"Giữ hotspot '{ssid}' bật đúng tên & mật khẩu, hoặc bật lại mạng cũ "
                           f"'{old_ssid or '?'}' để camera quay về. Hub tự kết nối lại khi camera online; "
                           "nếu vẫn không được, nhấn giữ nút Reset trên camera rồi cài lại.")
                    job["state"] = "watching"
                if late and time.monotonic() > t_end + self.WIFI_SWITCH_LATE_WATCH:
                    job["state"] = "done"
                    return
            if not late:
                step("reconnect", "ok", f"Camera đã kết nối lại qua {sess.camera.display_mode}")

            # 6. Which network did it come back on?
            step("confirm", "running", "Xác nhận mạng Wi-Fi camera đang dùng...")
            now_params = {}
            for _ in range(3):
                sess, proto = live_proto()
                if proto:
                    now_params = (await proto.query_cgi("get_params.cgi", timeout=10.0)) or {}
                    if now_params:
                        break
                await asyncio.sleep(3.0)
            now_ssid = now_params.get("wifi_ssid")
            mode = sess.camera.display_mode if sess else "?"
            job["new_ssid"] = now_ssid
            job["new_net"] = f"{now_params.get('ip', '?')} / GW {now_params.get('gateway', '?')}"
            job["connection_mode"] = mode
            prefix = "(Muộn) " if late else ""
            new_net = self._net_fingerprint(now_params)
            same_net = bool(old_net and new_net and old_net == new_net)
            if same_net:
                step("confirm", "error", f"Camera vẫn ở mạng cũ (IP {job['new_net']})")
                restored = await self._restore_old_wifi(uid, job, ssid, step)
                finish("fallback",
                       f"{prefix}Camera không vào được '{ssid}' và đã quay về mạng cũ (vẫn online qua {mode})."
                       + (" Hub đã khôi phục cấu hình Wi-Fi cũ." if restored else ""),
                       "Kiểm tra mật khẩu và băng tần 2.4GHz của hotspot rồi thử lại."
                       + ("" if restored else " Cấu hình đã lưu trên camera vẫn là mạng mới — nếu camera "
                          "khởi động lại khi mạng mới không có, camera có thể mất kết nối."))
            elif now_ssid in (ssid, None):
                step("confirm", "ok", f"Camera đang ở mạng '{ssid}' (IP {job['new_net']}) qua {mode}")
                finish("success", f"{prefix}Đổi Wi-Fi thành công: camera đang ở '{ssid}', kết nối qua {mode}.")
            else:
                step("confirm", "warn", f"Camera online ở mạng khác (SSID lưu '{now_ssid}', IP {job['new_net']})")
                finish("success", f"{prefix}Camera đã online lại ở mạng mới (IP {job['new_net']}, qua {mode}).")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave the UI spinning
            log.exception("Wi-Fi switch job failed")
            step(job.get("stage") or "error", "error", f"Lỗi nội bộ: {e}")
            finish("failed", f"Lỗi khi đổi Wi-Fi: {e}")

    async def handle_wifi_job(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        job = self.wifi_jobs.get(uid)
        return web.json_response({"ok": True, "job": self._wifi_job_public(job)})

    async def handle_provision_wifi(self, request: web.Request) -> web.Response:
        """
        Push Wi-Fi credentials directly to a camera via PPPP set_wifi.cgi
        (works when connected to a camera's AP hotspot or when switching Wi-Fi of a LAN camera).
        """
        try:
            data = await request.json()
            ssid = str(data.get("ssid") or "").strip()
            password = str(data.get("password") or "")
            uid = str(data.get("uid") or "").strip()
            target_ip = str(data.get("ip") or "").strip()

            if not ssid:
                return web.json_response({"ok": False, "error": "Vui lòng nhập Tên mạng Wi-Fi (SSID)"}, status=400)

            # Case 1: Camera is online through the hub -> verified switch job
            sess = self.sessions.get(uid) if uid else None
            if sess and sess.protocol and sess.protocol.enc_mode and sess.state == "CONNECTED":
                cur = self.wifi_jobs.get(uid)
                if cur and cur.get("state") == "running":
                    return web.json_response({
                        "ok": False, "method": "job", "error": "Camera đang đổi Wi-Fi, vui lòng đợi.",
                        "job": self._wifi_job_public(cur)}, status=409)
                job = {
                    "uid": uid, "camera": (self.recorders[uid].room_name if uid in self.recorders else uid[:10]),
                    "ssid": ssid, "state": "running", "stage": "precheck", "result": None,
                    "message": "", "advice": "", "steps": [], "old_ssid": None, "new_ssid": None,
                    "visible": [], "started_at": time.time(), "_t0": time.monotonic(),
                }
                self.wifi_jobs[uid] = job
                job["_task"] = asyncio.create_task(
                    self._run_wifi_switch(uid, ssid, password, bool(data.get("force"))))
                return web.json_response({
                    "ok": True,
                    "method": "job",
                    "message": f"Bắt đầu đổi Wi-Fi sang '{ssid}' — đang kiểm tra từng bước...",
                    "job": self._wifi_job_public(job),
                })

            # Case 2: Probe AP hotspot gateways (192.168.169.1, 192.168.1.1, 192.168.43.1) or target_ip
            probe_ips = [target_ip] if target_ip else ["192.168.169.1", "192.168.1.1", "192.168.43.1", "192.168.10.1"]
            cams = []
            for p_ip in probe_ips:
                found = await proxy.discover_cameras_broadcast(
                    timeout=1.2,
                    target_ip=p_ip,
                    skip_ips={s.camera.ip for s in self.sessions.values()},
                )
                for c in found:
                    if not uid or c.uid == uid:
                        cams.append(c)
                if cams:
                    break

            if not cams and uid:
                cfg_cams = self.config.setdefault("cameras", {})
                c_cfg = cfg_cams.get(uid)
                if isinstance(c_cfg, dict):
                    c_cfg["pending_wifi"] = {"ssid": ssid, "password": password}
                    self._save_hub_config()
                    return web.json_response({
                        "ok": True,
                        "method": "queued",
                        "message": (
                            f"Camera chưa online nên Hub đã XẾP HÀNG lệnh Wi-Fi '{ssid}'. "
                            "Ngay khi camera xuất hiện trên bất kỳ mạng LAN nào Hub quét được (dải /24 hiện tại), "
                            "Hub sẽ TỰ ĐỘNG bắn cấu hình sang Wi-Fi mới."
                        ),
                    })
            if not cams:
                return web.json_response({
                    "ok": False,
                    "error": (
                        "Chưa tìm thấy camera ở chế độ AP Hotspot hoặc LAN. "
                        "Hãy hướng mắt ống kính camera vào Mã QR Wi-Fi bên dưới (cách 15-20cm) để camera tự quét và vào Wi-Fi!"
                    ),
                })

            cam = cams[0]
            username = self.config.get("username", "admin")
            cam_pwd = self.config.get("password", "888888")
            psk_str = self.config.get("psk", "vstarcam2019")
            p2p_key = proxy.p2p_derive_key(psk_str.encode("ascii"))
            proto = proxy.PPPPUnifiedProtocol(
                username=username,
                password=cam_pwd,
                p2p_key=p2p_key,
                psk_list=[psk_str] + [p for p in proxy.KNOWN_PSKS if p != psk_str],
            )
            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: proto, local_addr=("0.0.0.0", 0), allow_broadcast=True
            )
            try:
                await proto.connect_to_camera(cam)
                await asyncio.sleep(0.4)
                await proto.send_set_wifi(ssid, password)
                await asyncio.sleep(0.4)
            finally:
                transport.close()

            asyncio.create_task(self._scan_and_connect_now())
            return web.json_response({
                "ok": True,
                "method": "pppp_direct",
                "message": f"Đã nạp trực tiếp Wi-Fi '{ssid}' vào camera {cam.formatted_did or cam.ip} qua giao thức PPPP! Đang chờ camera khởi động lại Wi-Fi...",
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def handle_list_events(self, request: web.Request) -> web.Response:
        room = request.query.get("room", "ALL")
        date = request.query.get("date", "ALL")
        trigger = request.query.get("trigger", "ALL")
        try:
            offset = max(0, int(request.query.get("offset", 0)))
        except ValueError:
            offset = 0
        try:
            limit = max(1, min(100, int(request.query.get("limit", 24))))
        except ValueError:
            limit = 24
        payload = self.storage.query_events(
            room_filter=room,
            date_filter=date,
            trigger_filter=trigger,
            offset=offset,
            limit=limit,
        )
        resp = web.json_response(payload)
        resp.enable_compression()
        return resp

    async def handle_event_thumb(self, request: web.Request) -> web.Response:
        """Serve lightweight resized event thumbnails with immutable browser/CDN caching."""
        rel = (request.query.get("rel") or "").strip().lstrip("/")
        if not rel or ".." in rel or not rel.lower().endswith((".jpg", ".jpeg")):
            return web.Response(status=400, text="Invalid thumbnail path")

        rec_root = os.path.realpath(self.recordings_dir)
        fpath = os.path.realpath(os.path.join(rec_root, rel))
        if not fpath.startswith(rec_root + os.sep) or not os.path.isfile(fpath):
            return web.Response(status=404, text="Thumbnail not found")

        try:
            width = max(240, min(960, int(request.query.get("w", 480))))
        except ValueError:
            width = 480

        try:
            st = os.stat(fpath)
            cache_key = (rel, st.st_mtime_ns, width)
            body = self._thumb_cache.get(cache_key)
            if body is None:
                with open(fpath, "rb") as f:
                    raw = f.read()
                body = make_compact_jpeg_thumb(raw, max_width=width, quality=74)
                self._thumb_cache[cache_key] = body
                if len(self._thumb_cache) > 300:
                    self._thumb_cache.popitem(last=False)
            else:
                self._thumb_cache.move_to_end(cache_key)
        except OSError:
            return web.Response(status=404, text="Thumbnail unreadable")

        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    def _resolve_recording_mp4(self, rel: str) -> Optional[str]:
        if not rel or ".." in rel or rel.startswith(".h264_cache") or not rel.lower().endswith(".mp4"):
            return None
        rec_root = os.path.realpath(self.recordings_dir)
        fpath = os.path.realpath(os.path.join(rec_root, rel))
        if not fpath.startswith(rec_root + os.sep) or not os.path.isfile(fpath):
            return None
        return fpath

    @staticmethod
    def _prune_h264_cache(cache_root: str, max_files: int = 40):
        entries = []
        now = time.time()
        for root, _dirs, files in os.walk(cache_root):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                    if fname.endswith(".tmp.mp4"):
                        if (now - st.st_mtime) > 600:
                            os.remove(fpath)
                        continue
                    if fname.endswith(".mp4"):
                        entries.append((st.st_mtime, fpath))
                except OSError:
                    pass
        if len(entries) <= max_files:
            return
        entries.sort(key=lambda item: item[0])
        for _mtime, old_path in entries[: len(entries) - max_files]:
            try:
                os.remove(old_path)
            except OSError:
                pass

    def vaapi_device(self) -> Optional[str]:
        """Render node for Intel iGPU (VAAPI) work, or None when disabled/unavailable.
        Config: hw_accel: auto|off (default auto), vaapi_device: /dev/dri/renderD128."""
        if str(self.config.get("hw_accel", "auto")).lower() in ("off", "false", "0", "none", "cpu"):
            return None
        if getattr(self, "_vaapi_failed", False):
            return None
        dev = str(self.config.get("vaapi_device") or "/dev/dri/renderD128")
        return dev if os.path.exists(dev) else None

    async def _ensure_h264_mp4(self, rel: str, src: str) -> Optional[str]:
        lock = self._h264_locks.get(rel)
        if lock is None:
            lock = asyncio.Lock()
            self._h264_locks[rel] = lock
        async with lock:
            cache_root = os.path.realpath(os.path.join(self.recordings_dir, ".h264_cache"))
            dst = os.path.realpath(os.path.join(cache_root, rel))
            if not dst.startswith(cache_root + os.sep):
                return None
            src_mtime = os.path.getmtime(src)
            if os.path.isfile(dst) and os.path.getsize(dst) > 0 and os.path.getmtime(dst) >= src_mtime:
                return dst
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp_dst = dst + ".tmp.mp4"
            tail = ["-map", "0:v:0", "-map", "0:a:0?", "-c:a", "copy", "-movflags", "+faststart", tmp_dst]
            attempts = []
            dev = self.vaapi_device()
            if dev:
                # iGPU: HEVC decode + H.264 encode on the GPU (~13x less CPU than libx264).
                # No scaling: VPP (scale_vaapi) is unavailable on this driver stack.
                attempts.append(("vaapi", [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-hwaccel", "vaapi", "-hwaccel_device", dev, "-hwaccel_output_format", "vaapi",
                    "-i", src, "-c:v", "h264_vaapi", "-profile:v", "main", "-qp", "25", *tail]))
            attempts.append(("libx264", [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-threads", "4", "-i", src,
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode",
                "-crf", "26", "-pix_fmt", "yuv420p",
                "-vf", "scale='min(1280,iw)':-2", *tail]))
            err_msg = ""
            for name, cmd in attempts:
                t0 = time.monotonic()
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    stderr = b"timed out"
                if proc.returncode == 0 and os.path.isfile(tmp_dst) and os.path.getsize(tmp_dst) > 0:
                    log.info("H.264 transcode (%s) %s in %.1fs", name, rel, time.monotonic() - t0)
                    break
                err_msg = (stderr or b"").decode("utf-8", "replace").strip()[:300]
                log.warning("H.264 transcode (%s) failed for %s: %s", name, rel, err_msg)
                if name == "vaapi":
                    self._vaapi_failed = True   # stop retrying the GPU path this run
                try:
                    if os.path.exists(tmp_dst):
                        os.remove(tmp_dst)
                except OSError:
                    pass
            else:
                raise RuntimeError(err_msg or "ffmpeg failed")
            os.replace(tmp_dst, dst)
            self._prune_h264_cache(cache_root)
            return dst

    async def handle_event_video(self, request: web.Request) -> web.StreamResponse:
        """Serve browser-compatible H.264 (yuv420p) transcoded on-demand for non-HEVC browsers."""
        rel = (request.query.get("rel") or "").replace("\\", "/").strip().lstrip("/")
        src = self._resolve_recording_mp4(rel)
        if not src:
            return web.Response(status=404, text="Recording not found")
        if os.path.getsize(src) < 512:
            return web.Response(status=422, text="Recording file is incomplete or empty")
        try:
            dst = await self._ensure_h264_mp4(rel, src)
        except Exception as e:
            log.warning("On-demand H.264 transcode failed for %s: %s", rel, e)
            return web.Response(status=500, text=f"Transcode failed: {e}")
        if not dst or not os.path.isfile(dst):
            return web.Response(status=404, text="Transcoded video unavailable")
        return web.FileResponse(
            dst,
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    async def handle_delete_event(self, request: web.Request) -> web.Response:
        data = await request.json()
        video_rel = data.get("video_rel", "")
        ok = self.storage.delete_event(video_rel)
        return web.json_response({"ok": ok})

    async def handle_update_settings(self, request: web.Request) -> web.Response:
        data = await request.json()
        if "pre_roll_sec" in data:
            self.pre_roll_sec = max(2.0, min(30.0, float(data["pre_roll_sec"])))
        if "post_roll_sec" in data:
            self.post_roll_sec = max(3.0, min(60.0, float(data["post_roll_sec"])))
        if "pframe_sensitivity" in data:
            self.pframe_sensitivity = max(1.2, min(10.0, float(data["pframe_sensitivity"])))
        if "pframe_cooldown_sec" in data:
            self.pframe_cooldown_sec = max(0.0, min(300.0, float(data["pframe_cooldown_sec"])))
        if "enable_pframe_trigger" in data:
            self.enable_pframe_trigger = bool(data["enable_pframe_trigger"])
        if "auto_25fps" in data:
            self.auto_25fps = bool(data["auto_25fps"])
            if self.auto_25fps:
                self._fps_checked_protos.clear()
        if "auto_firmware_ai" in data:
            self.auto_firmware_ai = bool(data["auto_firmware_ai"])
            if self.auto_firmware_ai:
                self._ai_checked_protos.clear()
        if "max_storage_gb" in data:
            self.max_storage_gb = max(1.0, float(data["max_storage_gb"]))
            self.storage.max_storage_gb = self.max_storage_gb
        if "min_free_gb" in data:
            self.min_free_gb = max(1.0, float(data["min_free_gb"]))
            self.storage.min_free_gb = self.min_free_gb

        for rec in self.recorders.values():
            rec.pre_roll_sec = self.pre_roll_sec
            rec.post_roll_sec = self.post_roll_sec
            rec.pframe_sensitivity = self.pframe_sensitivity
            rec.pframe_cooldown_sec = self.pframe_cooldown_sec
            rec.enable_pframe_trigger = self.enable_pframe_trigger

        self._save_hub_config()
        return web.json_response({"ok": True})


# =============================================================================
# Embedded Single-Page Web Dashboard (Real-Time 25 FPS Binary WebSocket)
# =============================================================================



def build_web_app(hub: CameraHubApp) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(hub.start_background_tasks)
    app.on_cleanup.append(hub.stop_background_tasks)

    app.router.add_get("/", hub.handle_index)
    app.router.add_get("/ws/streams", hub.handle_ws_streams)
    app.router.add_get("/ws/audio/{uid}", hub.handle_ws_audio)
    app.router.add_get("/ws/video/{uid}", hub.handle_ws_video)
    app.router.add_get("/api/status", hub.handle_status)
    app.router.add_get("/api/camera/{uid}/snapshot.jpg", hub.handle_snapshot)
    app.router.add_get("/api/camera/{uid}/mjpeg", hub.handle_mjpeg)
    app.router.add_get("/api/wifi/info", hub.handle_wifi_info)
    app.router.add_post("/api/wifi/sonic", hub.handle_wifi_sonic)
    app.router.add_get("/api/wifi/sonic.wav", hub.handle_wifi_sonic_wav)
    app.router.add_post("/api/wifi/play-pc-speaker", hub.handle_play_pc_speaker)
    app.router.add_get("/sonic", hub.handle_mobile_sonic_page)
    app.router.add_post("/api/wifi/qr", hub.handle_wifi_qr)
    app.router.add_post("/api/camera/provision-wifi", hub.handle_provision_wifi)
    app.router.add_get("/api/camera/{uid}/wifi-job", hub.handle_wifi_job)
    app.router.add_post("/api/camera/onboard", hub.handle_onboard_camera)
    app.router.add_post("/api/camera/decode-qr", hub.handle_decode_qr)
    app.router.add_post("/api/camera/add", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/add-qr", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/add_qr", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/scan", hub.handle_scan_lan)
    app.router.add_delete("/api/camera/{uid}", hub.handle_delete_camera)
    app.router.add_post("/api/camera/{uid}/ptz", hub.handle_ptz)
    app.router.add_get("/api/camera/{uid}/params", hub.handle_camera_params)
    app.router.add_get("/api/camera/{uid}/settings", hub.handle_camera_settings)
    app.router.add_post("/api/camera/{uid}/cgi", hub.handle_camera_cgi)
    app.router.add_post("/api/camera/{uid}/reboot", hub.handle_camera_reboot)
    app.router.add_post("/api/camera/{uid}/reconnect", hub.handle_camera_reconnect)
    app.router.add_post("/api/camera/{uid}/password", hub.handle_change_password)
    app.router.add_post("/api/camera/{uid}/record", hub.handle_record_control)
    app.router.add_post("/api/camera/{uid}/rename", hub.handle_rename)
    app.router.add_get("/api/events", hub.handle_list_events)
    app.router.add_get("/api/event-thumb", hub.handle_event_thumb)
    app.router.add_get("/api/event-video", hub.handle_event_video)
    app.router.add_delete("/api/events", hub.handle_delete_event)
    app.router.add_get("/favicon.ico", lambda _req: web.Response(status=204))
    app.router.add_post("/api/settings", hub.handle_update_settings)

    os.makedirs(STATIC_DIR, exist_ok=True)
    app.router.add_static(f"/static/{STATIC_ASSET_VER}/", path=STATIC_DIR, show_index=False)
    app.router.add_static("/static/", path=STATIC_DIR, show_index=False)
    os.makedirs(hub.recordings_dir, exist_ok=True)
    app.router.add_static("/recordings/", path=hub.recordings_dir, show_index=False)

    async def _apply_cache_headers(request: web.Request, response: web.StreamResponse):
        # Prevent Cloudflare Edge & browsers from serving stale CSS/JS after UI deploys,
        # while allowing immutable recorded clips & thumbnails to cache cleanly.
        if request.path in ("/", "/sonic") or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        elif request.path.startswith("/recordings/") and response.status < 400:
            response.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")

    app.on_response_prepare.append(_apply_cache_headers)
    return app


def main():
    parser = argparse.ArgumentParser(description="C22S Multi-Camera Hub & Smart Motion NVR")
    parser.add_argument("--config", default=proxy.DEFAULT_CONFIG_PATH, help="Config YAML path")
    parser.add_argument("--recordings-dir", default=DEFAULT_RECORDINGS_DIR, help="Directory for saved motion clips")
    parser.add_argument("--target-ip", default="192.168.31.90", help="Primary or seed camera IP (also runs LAN broadcast)")
    parser.add_argument("--web-host", default="0.0.0.0", help="Web Dashboard bind host")
    parser.add_argument("--web-port", type=int, default=8080, help="Web Dashboard port (default: 8080)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = proxy.load_config(args.config)
    hub = CameraHubApp(
        config=config,
        config_path=args.config,
        recordings_dir=args.recordings_dir,
        target_ip=args.target_ip,
        web_host=args.web_host,
        web_port=args.web_port,
    )
    app = build_web_app(hub)
    log.info("Starting C22S Camera Hub Web Dashboard on http://127.0.0.1:%d", args.web_port)
    web.run_app(app, host=args.web_host, port=args.web_port, print=None)


if __name__ == "__main__":
    main()
