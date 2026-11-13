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
import datetime
import json
import logging
import os
import time
from typing import Dict, Optional, Set

from aiohttp import WSMsgType, web

import eye4_rtsp_proxy as proxy
from motion_recorder import CameraMotionRecorder, StorageManager

log = logging.getLogger("eye4.hub")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")



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
        self.ws_clients: Set[web.WebSocketResponse] = set()

        # Hub NVR settings (persisted in YAML config under 'hub')
        hub_cfg = self.config.get("hub", {}) if isinstance(self.config.get("hub"), dict) else {}
        self.pre_roll_sec: float = float(hub_cfg.get("pre_roll_sec", 10.0))
        self.post_roll_sec: float = float(hub_cfg.get("post_roll_sec", 10.0))
        self.pframe_sensitivity: float = float(hub_cfg.get("pframe_sensitivity", 2.25))
        self.enable_pframe_trigger: bool = bool(hub_cfg.get("enable_pframe_trigger", True))
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
        self._ptz_stop_tasks: Dict[str, asyncio.Task] = {}
        self._ptz_last_move: Dict[str, tuple] = {}   # uid -> (decoder_control code, started_at)

    def _save_hub_config(self):
        self.config["hub"] = {
            "pre_roll_sec": self.pre_roll_sec,
            "post_roll_sec": self.post_roll_sec,
            "pframe_sensitivity": self.pframe_sensitivity,
            "enable_pframe_trigger": self.enable_pframe_trigger,
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

    async def stop_background_tasks(self, app: web.Application):
        self._running = False
        for ws in list(self.ws_clients):
            await ws.close()
        for t in (self._discovery_task, self._quota_task):
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

    # =========================================================================
    # HTTP & WebSocket API Handlers
    # =========================================================================

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")

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
            conn_mode = getattr(cam, "connection_mode", None) or ("WAN_P2P" if cam.ip in ("0.0.0.0", "") else "LAN")
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
                "codec": sess.rtsp._codec if sess.rtsp else "h265",
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
                "enable_pframe_trigger": self.enable_pframe_trigger,
                "max_storage_gb": self.max_storage_gb,
                "min_free_gb": self.min_free_gb,
            },
            "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

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

        async def rx_loop():
            try:
                async for msg in ws:
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

                for uid, rec in list(self.recorders.items()):
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
    PARAM_ACTIONS = {
        "ir_auto": (14, 0), "ir_on": (14, 1), "ir_off": (14, 2),
        "flip_normal": (5, 0), "flip_v": (5, 1), "flip_h": (5, 2), "flip_vh": (5, 3),
        "osd_on": (18, 1), "osd_off": (18, 0),
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
    }
    # Fixed CGIs. The trans_cmd_string keys mirror what the camera itself reports
    # for command=1 (query): 2127 -> enable/track_status, 2126 -> bHumanoidFrame/sensitive.
    CGI_ACTIONS = {
        "track_on": "trans_cmd_string.cgi?cmd=2127&command=0&enable=1",
        "track_off": "trans_cmd_string.cgi?cmd=2127&command=0&enable=0",
        "humanframe_on": "trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=1",
        "humanframe_off": "trans_cmd_string.cgi?cmd=2126&command=0&bHumanoidFrame=0",
    }
    # CGIs the raw console refuses: they rewrite persistent factory data / firmware
    CGI_BLOCKLIST = ("set_factory_param", "upgrade_firmware", "upgrade_htmls", "restore_factory")
    _SECRET_KEY_RE = re.compile(r"(pwd|pass|psk|wpa|loginpas|^key\d*$)", re.I)

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

    async def handle_ptz(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol or not sess.protocol.enc_mode:
            return web.json_response({"ok": False, "error": "Camera chưa kết nối"}, status=404)
        proto = sess.protocol

        try:
            data = await request.json()
        except Exception:
            data = {}
        action = str(data.get("action", ""))
        conn_mode = getattr(sess.camera, "connection_mode", "P2P")

        def done(acked: bool, **extra):
            body = {"ok": bool(acked), "action": action, "connection_mode": conn_mode, **extra}
            if not acked:
                body["error"] = self.CMD_NO_ACK_MSG
            return web.json_response(body, status=200 if acked else 504)

        def int_arg(name, lo, hi):
            v = int(float(data.get(name)))
            return max(lo, min(hi, v))

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

            # --- Image / IR / OSD ---
            if action in self.PARAM_ACTIONS:
                param, val = self.PARAM_ACTIONS[action]
                return done(await proto.send_camera_param(param, val, wait_ack=3.0))

            if action in self.PARAM_VALUE_ACTIONS:
                param, lo, hi = self.PARAM_VALUE_ACTIONS[action]
                val = int_arg("value", lo, hi)
                return done(await proto.send_camera_param(param, val, wait_ack=3.0), value=val)

            if action == "param":      # raw camera_control.cgi?param=&value=
                param, val = int_arg("param", 0, 255), int_arg("value", -65535, 65535)
                return done(await proto.send_camera_param(param, val, wait_ack=3.0), param=param, value=val)

            if action in self.CGI_ACTIONS:
                return done(await proto.send_cgi(self.CGI_ACTIONS[action], wait_ack=3.0))

            if action in self.CGI_VALUE_ACTIONS:
                tmpl, lo, hi = self.CGI_VALUE_ACTIONS[action]
                val = int_arg("value", lo, hi)
                return done(await proto.send_cgi(tmpl.format(v=val), wait_ack=3.0), value=val)
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
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol or not sess.protocol.enc_mode:
            return web.json_response({"ok": False, "error": "Camera chưa kết nối"}, status=404)
        proto = sess.protocol
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

    async def handle_camera_cgi(self, request: web.Request) -> web.Response:
        """Raw CGI console: send any CGI (login params are appended) and return the reply."""
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol or not sess.protocol.enc_mode:
            return web.json_response({"ok": False, "error": "Camera chưa kết nối"}, status=404)
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
            conn_mode = getattr(sess.camera, "connection_mode", "P2P")
            return web.json_response({
                "ok": True,
                "message": f"Đã gửi lệnh khởi động lại camera ({conn_mode})!",
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
            conn_mode = getattr(sess.camera, "connection_mode", "P2P")
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

        html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>🚀 Liên Kết & Nạp Wi-Fi Camera C22S</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:14px; margin:0; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:14px; padding:18px; max-width:480px; margin:0 auto; box-shadow:0 8px 28px rgba(0,0,0,0.45); }}
  .nav-tabs {{ display:flex; gap:6px; margin-bottom:12px; background:#0b0f17; padding:4px; border-radius:10px; border:1px solid #30363d; }}
  .nav-tab-btn {{ flex:1; padding:9px 6px; font-size:12px; font-weight:700; border:none; border-radius:8px; background:transparent; color:#8b949e; cursor:pointer; text-align:center; transition:0.15s; }}
  .nav-tab-btn.active {{ background:#21262d; color:#58a6ff; box-shadow:0 2px 8px rgba(0,0,0,0.4); }}
  .play-btn {{ width:100%; padding:15px; font-size:15px; font-weight:800; border:none; border-radius:12px; background:linear-gradient(135deg,#238636,#2ea043); color:#fff; cursor:pointer; margin-top:12px; box-shadow:0 4px 14px rgba(35,134,54,0.4); }}
  .play-btn.link-btn {{ background:linear-gradient(135deg,#1f6feb,#238636); box-shadow:0 4px 14px rgba(31,111,235,0.4); }}
  .play-btn:active {{ transform:scale(0.98); }}
  .stop-btn {{ width:100%; padding:11px; font-size:14px; font-weight:700; border:1px solid #f85149; border-radius:10px; background:rgba(248,81,73,0.15); color:#f85149; cursor:pointer; margin-top:8px; }}
  input, select {{ width:100%; padding:10px; border-radius:8px; border:1px solid #30363d; background:#0b0f17; color:#e6edf3; font-size:15px; margin-top:4px; box-sizing:border-box; }}
  label {{ display:block; text-align:left; font-size:12px; color:#8b949e; margin-top:10px; font-weight:600; }}
  .step-item {{ text-align:left; padding:9px 11px; border-radius:8px; background:#0b0f17; border:1px solid #30363d; margin-top:7px; font-size:13px; line-height:1.4; transition:0.2s; }}
  .step-item.active {{ border-color:#2f81f7; background:rgba(47,129,247,0.12); color:#58a6ff; }}
  .step-item.done {{ border-color:#3fb950; background:rgba(63,185,80,0.14); color:#3fb950; }}
  .step-item.warn {{ border-color:#d29922; background:rgba(210,153,34,0.14); color:#f0883e; }}
</style>
</head>
<body>
  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <span style="font-size:26px;">🚀📷</span>
      <a href="/" style="color:#58a6ff; font-size:13px; text-decoration:none; font-weight:600;">← Về Bảng Điều Khiển Live</a>
    </div>

    <!-- 2 TABS / BRANCHES -->
    <div class="nav-tabs" style="margin-top:10px;">
      <button class="nav-tab-btn active" id="tab-btn-link" onclick="switchMobileTab('link')">
        🔗 Camera Đã Có Wi-Fi<br><span style="font-size:10.5px; font-weight:normal;">Chỉ liên kết Server</span>
      </button>
      <button class="nav-tab-btn" id="tab-btn-unbox" onclick="switchMobileTab('unbox')">
        📦 Khui Hộp Mới<br><span style="font-size:10.5px; font-weight:normal;">Nạp Wi-Fi qua loa</span>
      </button>
    </div>

    <div id="m-desc-link" style="color:#8b949e; font-size:12px; line-height:1.45; text-align:left; margin-bottom:10px;">
      ⚡ <b>Chỉ Liên Kết Server:</b> Camera đang sử dụng hoặc đã vào Wi-Fi / cắm mạng LAN. Server sẽ kết nối trực tiếp qua Internet (WAN P2P / LAN) mà <b>không cần nạp Wi-Fi</b> hay phát âm thanh!
    </div>
    <div id="m-desc-unbox" style="color:#8b949e; font-size:12px; line-height:1.45; text-align:left; margin-bottom:10px; display:none;">
      🔊 <b>Khui Hộp &amp; Nạp Wi-Fi:</b> Phát sóng âm từ loa điện thoại nạp Wi-Fi cho Camera mới khui. Ngay khi Camera vào mạng, Server sẽ bắt tay và <b>tự động tắt sóng âm</b>!
    </div>

    <!-- COMMON: QR IMAGE / UID -->
    <div style="background:#0b0f17; border:1px dashed #30363d; border-radius:10px; padding:10px; text-align:center; cursor:pointer;" onclick="document.getElementById('m-qr-file').click()">
      <input type="file" id="m-qr-file" accept="image/*" style="display:none" onchange="decodeQrPhoto(this.files[0])">
      <div style="font-size:13px; font-weight:700; color:#58a6ff;">📷 Chụp / Tải ảnh Tem QR dưới đáy Camera</div>
      <div id="m-qr-msg" style="font-size:11px; color:#8b949e; margin-top:3px;">Hoặc bấm nút "📷 Quét / Chụp" cạnh ô UID bên dưới</div>
    </div>

    <div style="display:grid; grid-template-columns:1.3fr 1fr; gap:8px;">
      <div>
        <label>Mã UID Camera (trên tem đáy) (*):</label>
        <div style="display:flex; gap:6px;">
          <input id="m-uid" value="{uid_q}" placeholder="VD: VSTJ082299RLSXK" style="font-family:monospace; flex:1;">
          <button class="btn" type="button" onclick="document.getElementById('m-qr-file').click()" style="padding:0 8px; font-weight:700; background:#21262d; border-color:#58a6ff; color:#58a6ff; font-size:11px; white-space:nowrap;">📷 Quét / Chụp</button>
        </div>
      </div>
      <div>
        <label>Tên Phòng / Vị trí:</label>
        <input id="m-room" value="{room_q}" placeholder="VD: C22S_1">
      </div>
    </div>

    <div>
      <label>Mật khẩu Camera:</label>
      <input id="m-campwd" value="888888" placeholder="Mặc định: 888888 (hoặc mật khẩu riêng bạn đã đổi)" style="font-family:monospace;">
    </div>

    <!-- BRANCH 2: WIFI INPUTS (ONLY FOR UNBOX MODE) -->
    <div id="m-unbox-wifi-section" style="display:none; margin-top:8px; padding-top:8px; border-top:1px solid #30363d;">
      <label>Tên mạng Wi-Fi 2.4GHz tại nơi lắp Camera (*):</label>
      <input id="m-ssid" value="{ssid}" placeholder="Nhập đúng tên Wi-Fi 2.4GHz..." oninput="updateWavUrl()">
      <label>Mật khẩu Wi-Fi (*):</label>
      <input id="m-pwd" value="{password}" placeholder="Nhập mật khẩu Wi-Fi..." oninput="updateWavUrl()">

      <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
        <div>
          <label>Chế độ Sóng âm:</label>
          <select id="m-mode" onchange="updateWavUrl()">
            <option value="combo" {"selected" if mode=="combo" else ""}>Siêu nhạy: Kết hợp MAC + SSID</option>
            <option value="mac" {"selected" if mode=="mac" else ""}>Siêu tốc: MAC Router (Như Eye4)</option>
            <option value="ssid" {"selected" if mode=="ssid" else ""}>Chuẩn: Full SSID (Mọi mạng)</option>
          </select>
        </div>
        <div>
          <label>MAC Router / BSSID (Khuyên dùng):</label>
          <input id="m-bssid" value="{bssid}" placeholder="VD: 001e hoặc xem đáy router" autocapitalize="off" autocomplete="off" oninput="syncModeFromBssid(); updateWavUrl()">
        </div>
      </div>

      <div style="display:flex; gap:8px; margin-top:8px;">
        <button class="stop-btn" onclick="stopPlay()" style="flex:1;">⏹ Dừng phát âm thanh</button>
        <button class="stop-btn" onclick="toggleOpticalQr()" style="flex:1; border-color:#58a6ff; color:#58a6ff; background:rgba(47,129,247,0.12);">📱 Hiện Mã QR Quang Học</button>
      </div>

      <div id="m-opt-qr-wrap" style="display:none; margin-top:10px; background:#fff; padding:10px; border-radius:10px; color:#111; text-align:center;">
        <img id="m-opt-qr-img" style="width:200px; height:200px; display:block; margin:0 auto;" alt="Optical WiFi QR">
        <div style="font-size:12px; font-weight:700; margin-top:6px;">Đưa màn hình ĐT cách mắt ống kính Camera 15–20 cm</div>
      </div>
      <audio id="m-audio" src="{wav_url}" loop style="width:100%; margin-top:10px;" controls></audio>
    </div>

    <!-- ACTION BUTTONS -->
    <div id="m-action-link">
      <button class="play-btn link-btn" id="m-btn-link" onclick="startMobileLinkOnly()">🔗 Liên Kết Server Ngay (Không Cần Nạp Wi-Fi)</button>
    </div>
    <div id="m-action-unbox" style="display:none;">
      <button class="play-btn" id="m-btn-onboard" onclick="startOneClickMobile()">🔊 Nạp Wi-Fi &amp; Tự Động Liên Kết Server</button>
    </div>

    <!-- PROGRESS STEPS -->
    <div id="m-steps" style="margin-top:12px; display:none;">
      <div id="m-step-1" class="step-item"><b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P...</div>
      <div id="m-step-2" class="step-item"><b>Bước 2:</b> 🌐 Chờ Camera báo danh lên P2P Cloud...</div>
      <div id="m-step-3" class="step-item"><b>Bước 3:</b> 🔗 Server đục lỗ UDP &amp; xác thực (<code>CONNECTED</code>)...</div>
    </div>

    <div id="m-status" style="margin-top:12px; font-size:13px; color:#58a6ff; line-height:1.45; text-align:left;"></div>
  </div>
<script>
let onboardPollTimer = null;
let targetUidHex = null;
let currentMobileTab = 'link';

function switchMobileTab(mode) {{
  currentMobileTab = mode;
  stopPlay();
  const btnLink = document.getElementById('tab-btn-link');
  const btnUnbox = document.getElementById('tab-btn-unbox');
  const descLink = document.getElementById('m-desc-link');
  const descUnbox = document.getElementById('m-desc-unbox');
  const wifiSec = document.getElementById('m-unbox-wifi-section');
  const actLink = document.getElementById('m-action-link');
  const actUnbox = document.getElementById('m-action-unbox');
  const statusEl = document.getElementById('m-status');
  document.getElementById('m-steps').style.display = 'none';

  if (mode === 'link') {{
    btnLink.className = 'nav-tab-btn active';
    btnUnbox.className = 'nav-tab-btn';
    descLink.style.display = 'block';
    descUnbox.style.display = 'none';
    wifiSec.style.display = 'none';
    actLink.style.display = 'block';
    actUnbox.style.display = 'none';
    statusEl.innerHTML = '';
  }} else {{
    btnLink.className = 'nav-tab-btn';
    btnUnbox.className = 'nav-tab-btn active';
    descLink.style.display = 'none';
    descUnbox.style.display = 'block';
    wifiSec.style.display = 'block';
    actLink.style.display = 'none';
    actUnbox.style.display = 'block';
    statusEl.innerHTML = '👉 Bật <b>Max Volume (100% âm lượng)</b> trên điện thoại và đặt loa sát lỗ Mic của Camera (cách 2–5 cm).';
    updateWavUrl();
  }}
}}

function downscaleImageFile(file, maxDim = 1600, quality = 0.85) {{
  return new Promise((resolve) => {{
    if (!file || !file.type || !file.type.startsWith('image/')) {{
      return resolve(file);
    }}
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {{
      URL.revokeObjectURL(url);
      let w = img.width;
      let h = img.height;
      if (w <= maxDim && h <= maxDim) {{
        return resolve(file);
      }}
      if (w > h) {{
        h = Math.round((h * maxDim) / w);
        w = maxDim;
      }} else {{
        w = Math.round((w * maxDim) / h);
        h = maxDim;
      }}
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, w, h);
      canvas.toBlob((blob) => {{
        if (blob) {{
          resolve(blob);
        }} else {{
          resolve(file);
        }}
      }}, 'image/jpeg', quality);
    }};
    img.onerror = () => {{
      URL.revokeObjectURL(url);
      resolve(file);
    }};
    img.src = url;
  }});
}}

async function decodeQrPhoto(file) {{
  if (!file) return;
  const msg = document.getElementById('m-qr-msg');
  msg.textContent = '⏳ Đang tối ưu dung lượng ảnh & giải mã tem QR...';
  try {{
    const uploadBlob = await downscaleImageFile(file, 1600, 0.85);
    const fd = new FormData();
    fd.append('file', uploadBlob, 'qr.jpg');
    const res = await fetch('/api/camera/decode-qr', {{ method: 'POST', body: fd }});
    if (res.status === 413) {{
      msg.innerHTML = '<span style="color:#f85149;">❌ Ảnh quá lớn (413 Request Entity Too Large). Vui lòng thử lại với ảnh nhỏ hơn hoặc gõ UID.</span>';
      return;
    }}
    const d = await res.json();
    if (res.ok && d.ok) {{
      document.getElementById('m-uid').value = d.formatted_did;
      if (d.suggested_name && !document.getElementById('m-room').value) {{
        document.getElementById('m-room').value = d.suggested_name;
      }}
      msg.innerHTML = `<span style="color:#3fb950;">✅ Đã nhận UID: <b>${{d.formatted_did}}</b></span>`;
    }} else {{
      msg.innerHTML = `<span style="color:#f85149;">❌ ${{d.error || 'Không nhận diện được tem QR'}}</span>`;
    }}
  }} catch (e) {{
    msg.textContent = 'Lỗi giải mã QR: ' + e;
  }}
}}

// Eye4 only ever plays the MAC variant, so pick it as soon as a usable MAC is typed.
function syncModeFromBssid() {{
  const hex = ((document.getElementById('m-bssid') || {{}}).value || '').replace(/[^0-9a-fA-F]/g, '');
  document.getElementById('m-mode').value = hex.length >= 4 ? 'mac' : 'ssid';
}}

function updateWavUrl() {{
  const s = document.getElementById('m-ssid').value.trim();
  const p = document.getElementById('m-pwd').value;
  const m = document.getElementById('m-mode').value;
  const b = (document.getElementById('m-bssid') || {{}}).value.trim();
  if (!s) return;
  const u = `/api/wifi/sonic.wav?ssid=${{encodeURIComponent(s)}}&password=${{encodeURIComponent(p)}}&bssid=${{encodeURIComponent(b)}}&mode=${{encodeURIComponent(m)}}`;
  const a = document.getElementById('m-audio');
  if (a.paused) a.src = u;
}}

async function startMobileLinkOnly() {{
  const uid = document.getElementById('m-uid').value.trim();
  const room = document.getElementById('m-room').value.trim();
  const camPwd = document.getElementById('m-campwd').value.trim() || '888888';

  if (!uid) {{
    alert('Vui lòng chụp/tải ảnh tem QR hoặc nhập Mã UID trên thân Camera (VD: VSTJ082299RLSXK)!');
    document.getElementById('m-uid').focus();
    return;
  }}

  const btn = document.getElementById('m-btn-link');
  btn.disabled = true;
  btn.textContent = '⏳ Đang đăng ký & Kết nối WAN P2P...';

  const sBox = document.getElementById('m-steps');
  const s1 = document.getElementById('m-step-1');
  const s2 = document.getElementById('m-step-2');
  const s3 = document.getElementById('m-step-3');
  sBox.style.display = 'block';
  s1.className = 'step-item active';
  s1.innerHTML = '<b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P (180s fast-poll)...';
  s2.className = 'step-item';
  s2.innerHTML = '<b>Bước 2:</b> 🌐 Bắt tay P2P Cloud (<code>WAITING_FOR_WIFI</code> → <code>ONLINE_PUNCHING</code>)...';
  s3.className = 'step-item';
  s3.innerHTML = '<b>Bước 3:</b> 🔗 Server đục lỗ UDP &amp; xác thực (<code>CONNECTED</code>)...';

  try {{
    const res = await fetch('/api/camera/onboard', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        uid: uid,
        room_name: room,
        cam_password: camPwd,
        wifi_ssid: ""
      }})
    }});
    const d = await res.json();
    if (!d.ok) {{
      alert('Lỗi: ' + d.error);
      return;
    }}
    targetUidHex = d.uid;
    document.getElementById('m-status').innerHTML =
      `🌐 <b>Đã đăng ký Camera <code>${{d.formatted_did}}</code> (${{d.room_name}})!</b><br>` +
      `Server đang tự động dò tìm và đục lỗ UDP qua WAN P2P &amp; LAN mỗi 1.5s...`;

    if (onboardPollTimer) clearInterval(onboardPollTimer);
    onboardPollTimer = setInterval(pollMobileOnboardStatus, 1200);
  }} catch (e) {{
    document.getElementById('m-status').innerHTML = '❌ Lỗi kết nối: ' + e;
  }} finally {{
    btn.disabled = false;
    btn.textContent = '🔗 Liên Kết Server Ngay (Không Cần Nạp Wi-Fi)';
  }}
}}

async function startOneClickMobile() {{
  const uid = document.getElementById('m-uid').value.trim();
  const room = document.getElementById('m-room').value.trim();
  const ssid = document.getElementById('m-ssid').value.trim();
  const pwd = document.getElementById('m-pwd').value;
  const camPwd = document.getElementById('m-campwd').value.trim() || '888888';

  if (!ssid) {{
    alert('Vui lòng nhập Tên mạng Wi-Fi (2.4GHz) tại nơi đặt camera!');
    document.getElementById('m-ssid').focus();
    return;
  }}

  const a = document.getElementById('m-audio');
  if (!uid) {{
    updateWavUrl();
    a.currentTime = 0;
    a.play();
    document.getElementById('m-status').innerHTML = '🟢 <b>Đang phát sóng âm 6.5–10.1 kHz (Lặp liên tục)...</b><br>Hãy nhập thêm Mã UID Camera ở trên để Server tự động liên kết và tự ngắt sóng âm khi hoàn tất!';
    return;
  }}

  document.getElementById('m-steps').style.display = 'block';
  const s1 = document.getElementById('m-step-1');
  const s2 = document.getElementById('m-step-2');
  const s3 = document.getElementById('m-step-3');
  s1.className = 'step-item active';
  s1.innerHTML = `<b>Bước 1:</b> 🔊 Đang phát sóng âm nạp Wi-Fi <b>"${{ssid}}"</b> từ điện thoại sang Camera...`;
  s2.className = 'step-item';
  s2.innerHTML = '<b>Bước 2:</b> 🌐 Chờ Camera vào mạng Wi-Fi &amp; báo danh lên P2P Cloud (<code>WAITING_FOR_WIFI</code> → <code>ONLINE_PUNCHING</code>)...';
  s3.className = 'step-item';
  s3.innerHTML = '<b>Bước 3:</b> 🔗 Server đục lỗ UDP &amp; xác thực (<code>CONNECTED</code>)...';

  try {{
    const bssid = (document.getElementById('m-bssid') || {{}}).value.trim();
    const mode = (document.getElementById('m-mode') || {{}}).value || 'combo';
    const res = await fetch('/api/camera/onboard', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        uid: uid,
        room_name: room,
        cam_password: camPwd,
        wifi_ssid: ssid,
        wifi_password: pwd,
        wifi_bssid: bssid,
        sonic_mode: mode
      }})
    }});
    const d = await res.json();
    if (!d.ok) {{
      alert('Lỗi: ' + d.error);
      return;
    }}
    targetUidHex = d.uid;
    if (d.wav_data_url) {{
      a.src = d.wav_data_url;
      a.currentTime = 0;
      await a.play();
    }}
    if (d.qr_data_url) {{
      document.getElementById('m-opt-qr-img').src = d.qr_data_url;
    }}
    document.getElementById('m-status').innerHTML =
      `🔊 <b>Đang phát sóng âm nạp Wi-Fi "${{ssid}}" cho Camera <code>${{d.formatted_did}}</code>!</b><br>` +
      `Giữ loa điện thoại sát lỗ Mic của Camera (2–5 cm). Server đang dò tìm Camera liên tục mỗi 1.5s...`;

    if (onboardPollTimer) clearInterval(onboardPollTimer);
    onboardPollTimer = setInterval(pollMobileOnboardStatus, 1200);
  }} catch (e) {{
    document.getElementById('m-status').innerHTML = '❌ Lỗi: ' + e;
  }}
}}

async function pollMobileOnboardStatus() {{
  if (!targetUidHex) return;
  try {{
    const res = await fetch('/api/status');
    const data = await res.json();
    const cam = (data.cameras || []).find(c => c.uid === targetUidHex);
    if (!cam) return;

    const s1 = document.getElementById('m-step-1');
    const s2 = document.getElementById('m-step-2');
    const s3 = document.getElementById('m-step-3');
    const cloudSt = cam.wan_cloud_status || 'WAITING_FOR_WIFI';

    // 1. Fully connected
    if (cam.state === 'CONNECTED' && !cam.auth_failed) {{
      stopPlay();
      if (onboardPollTimer) {{ clearInterval(onboardPollTimer); onboardPollTimer = null; }}
      s1.className = 'step-item done';
      s1.innerHTML = (currentMobileTab === 'unbox')
        ? '<b>Bước 1:</b> ✅ Đã nạp Wi-Fi thành công (Đã tự động tắt sóng âm)'
        : '<b>Bước 1:</b> ✅ Đã lưu UID &amp; kích hoạt kết nối';
      s2.className = 'step-item done';
      s2.innerHTML = '<b>Bước 2:</b> ✅ Camera đã vào mạng &amp; báo danh P2P Cloud (<code>CONNECTED</code>)';
      s3.className = 'step-item done';
      s3.innerHTML = `<b>Bước 3:</b> ✅ Server đã đục lỗ UDP (<code>${{cam.connection_mode}}</code>) &amp; nhận luồng Live 25 FPS!`;
      document.getElementById('m-status').innerHTML =
        `<div style="padding:12px; border-radius:10px; background:rgba(63,185,80,0.2); border:1px solid #3fb950; color:#3fb950; font-weight:700;">` +
        `✅ Liên kết hoàn tất! Camera <b>${{cam.room_name}} (${{cam.formatted_did}})</b> đã kết nối trực tiếp với Server qua Internet.<br>` +
        `<a href="/" style="display:inline-block; margin-top:8px; padding:8px 14px; background:#238636; color:#fff; border-radius:6px; text-decoration:none;">📹 Xem Live Stream Ngay</a></div>`;
      return;
    }}

    // 2. Auth failed (wrong camera password)
    if (cam.auth_failed) {{
      stopPlay();
      s1.className = 'step-item done';
      s2.className = 'step-item done';
      s3.className = 'step-item warn';
      s3.innerHTML = '<b>Bước 3:</b> ⚠️ Đã đục lỗ UDP nhưng sai Mật khẩu Camera (hãy nhập đúng mật khẩu Camera vào ô Mật khẩu rồi bấm lại)!';
      return;
    }}

    // 3. Camera is online on P2P Cloud -> Hole punching actively in progress!
    if (cloudSt === 'ONLINE_PUNCHING') {{
      s1.className = 'step-item done';
      s1.innerHTML = (currentMobileTab === 'unbox')
        ? '<b>Bước 1:</b> ✅ Camera đã nhận sóng âm Wi-Fi'
        : '<b>Bước 1:</b> ✅ Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P';
      s2.className = 'step-item done';
      s2.innerHTML = '<b>Bước 2:</b> 🟢 Camera đã ONLINE trên P2P Cloud (Đã bắt được IP &amp; Port công khai)!';
      s3.className = 'step-item active';
      s3.innerHTML = `<b>Bước 3:</b> 🔄 Server đang đục lỗ UDP &amp; bắt tay trực tiếp với Camera (<code>${{cam.state}}</code>)...`;
      return;
    }}

    // 4. Camera is OFFLINE on Cloud (WAITING_FOR_WIFI or IDLE)
    if (cloudSt === 'WAITING_FOR_WIFI' || cloudSt === 'IDLE') {{
      if (currentMobileTab === 'unbox') {{
        s1.className = 'step-item active';
        s1.innerHTML = '<b>Bước 1:</b> 🔊 Đang phát sóng âm nạp Wi-Fi từ điện thoại sang Camera...';
        s2.className = 'step-item warn';
        s2.innerHTML = '<b>Bước 2:</b> ⏳ Đang phát sóng âm... Chờ Camera nhận sóng âm và online trên P2P Cloud (<code>WAITING_FOR_WIFI</code>)...';
      }} else {{
        s1.className = 'step-item done';
        s1.innerHTML = '<b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P / LAN';
        s2.className = 'step-item warn';
        s2.innerHTML = '<b>Bước 2:</b> ⏳ Chờ Camera vào mạng &amp; báo danh lên P2P Cloud (Hiện Camera đang <b>OFFLINE</b> — Hãy chắc chắn Camera đã cắm nguồn và có mạng Wi-Fi/LAN, hoặc chuyển sang tab <b>Khui Hộp Mới</b> để nạp Wi-Fi!)';
      }}
      s3.className = 'step-item';
      s3.innerHTML = '<b>Bước 3:</b> ⏸ Chờ Bước 2 (Camera phải Online trên P2P Cloud trước khi Server có thể đục lỗ UDP)...';
      return;
    }}

    // 5. Invalid UID
    if (cloudSt === 'INVALID_UID') {{
      stopPlay();
      s2.className = 'step-item warn';
      s2.innerHTML = '<b>Bước 2:</b> ❌ Mã UID không hợp lệ hoặc không tồn tại trên hệ thống P2P Cloud!';
      s3.className = 'step-item';
      s3.innerHTML = '<b>Bước 3:</b> ⏸ Đã dừng kết nối.';
      return;
    }}

    // 6. Fallback
    s2.className = 'step-item active';
    s2.innerHTML = `<b>Bước 2:</b> 🌐 Chờ Camera báo danh lên P2P Cloud (Trạng thái: <code>${{cloudSt}}</code>)...`;
    s3.className = 'step-item';
    s3.innerHTML = '<b>Bước 3:</b> ⏸ Chờ Bước 2...';
  }} catch (e) {{}}
}}

function toggleOpticalQr() {{
  const w = document.getElementById('m-opt-qr-wrap');
  const s = document.getElementById('m-ssid').value.trim();
  const p = document.getElementById('m-pwd').value;
  if (w.style.display === 'none') {{
    w.style.display = 'block';
    if (s && !document.getElementById('m-opt-qr-img').src) {{
      fetch('/api/wifi/qr', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ssid: s, password: p, format: 'eye4'}})
      }}).then(r => r.json()).then(d => {{
        if (d.ok) document.getElementById('m-opt-qr-img').src = d.data_url;
      }});
    }}
  }} else {{
    w.style.display = 'none';
  }}
}}

function stopPlay() {{
  const a = document.getElementById('m-audio');
  if (a) {{
    a.pause();
    a.currentTime = 0;
  }}
}}
</script>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

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

            # Case 1: Camera is already connected in self.sessions
            if uid and uid in self.sessions and self.sessions[uid].protocol:
                await self.sessions[uid].protocol.send_set_wifi(ssid, password)
                conn_mode = getattr(self.sessions[uid].camera, "connection_mode", "P2P")
                return web.json_response({
                    "ok": True,
                    "method": "active_session",
                    "message": f"Đã bắn lệnh nạp Wi-Fi '{ssid}' trực tiếp tới camera qua {conn_mode}!",
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
        events = self.storage.list_events(room_filter=room, date_filter=date, limit=300)
        return web.json_response({"events": events})

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
            self.pframe_sensitivity = max(1.2, min(4.0, float(data["pframe_sensitivity"])))
        if "enable_pframe_trigger" in data:
            self.enable_pframe_trigger = bool(data["enable_pframe_trigger"])
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
            rec.enable_pframe_trigger = self.enable_pframe_trigger

        self._save_hub_config()
        return web.json_response({"ok": True})


# =============================================================================
# Embedded Single-Page Web Dashboard (Real-Time 25 FPS Binary WebSocket)
# =============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C22S Multi-Camera Hub & Smart Motion NVR (25 FPS Live)</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #2f81f7;
    --danger: #f85149;
    --success: #3fb950;
    --warn: #d29922;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 10px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 700;
    font-size: 16px;
  }
  .badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 99px;
    font-weight: 600;
    background: #1f2937;
    border: 1px solid var(--border);
  }
  .badge.ok { color: var(--success); border-color: rgba(63,185,80,0.4); }
  .badge.rec { color: #fff; background: var(--danger); border-color: var(--danger); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.55; } }
  .tabs {
    display: flex;
    gap: 6px;
  }
  .tab-btn {
    background: transparent;
    color: var(--muted);
    border: 1px solid transparent;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 13px;
  }
  .tab-btn.active {
    background: #21262d;
    color: var(--text);
    border-color: var(--border);
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--muted);
  }
  select, input, button.btn {
    background: #21262d;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
  }
  button.btn { cursor: pointer; font-weight: 600; }
  button.btn:hover { border-color: var(--muted); }
  button.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.btn.danger { background: rgba(248,81,73,0.18); border-color: var(--danger); color: var(--danger); }
  main { flex: 1; padding: 14px; }
  .view-section { display: none; }
  .view-section.active { display: block; }

  /* Camera Grid */
  .cam-grid {
    display: grid;
    gap: 10px;
    grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  }
  .cam-grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
  .cam-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
  .cam-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }

  .cam-card {
    background: var(--panel);
    border: 2px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    position: relative;
    display: flex;
    flex-direction: column;
    transition: border-color 0.2s;
  }
  .cam-card.recording {
    border-color: var(--danger);
    box-shadow: 0 0 12px rgba(248,81,73,0.35);
  }
  .cam-video-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
    cursor: pointer;
    overflow: hidden;
  }
  .cam-video-wrap img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }
  .cam-overlay-top {
    position: absolute;
    top: 6px; left: 8px; right: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    pointer-events: none;
  }
  .room-pill {
    background: rgba(0,0,0,0.72);
    padding: 3px 9px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 700;
  }
  .cam-footer {
    padding: 7px 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    color: var(--muted);
    background: var(--panel);
    border-top: 1px solid var(--border);
  }
  .cam-actions { display: flex; gap: 5px; }

  /* Focus Modal */
  .modal-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.84);
    z-index: 100;
    align-items: center;
    justify-content: center;
    padding: 18px;
  }
  .modal-backdrop.open { display: flex; }
  .modal-box {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    width: 100%;
    max-width: 1180px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .modal-header {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .modal-body {
    display: grid;
    grid-template-columns: 1fr 270px;
    gap: 0;
  }
  @media (max-width: 860px) { .modal-body { grid-template-columns: 1fr; } }
  .focus-stage {
    background: #000;
    aspect-ratio: 16 / 9;
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .focus-stage img, .focus-stage video {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .ptz-sidebar {
    padding: 14px;
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 14px;
    font-size: 12px;
    max-height: 82vh;
    overflow-y: auto;
  }
  .ptz-sidebar .row { display:flex; gap:5px; flex-wrap:wrap; margin-top:6px; align-items:center; }
  .ptz-sidebar input[type=range] { flex:1; min-width:90px; }
  .ptz-sidebar label.sl { display:flex; align-items:center; gap:6px; margin-top:6px; }
  .ptz-sidebar label.sl span { width:74px; color:var(--muted); }
  .dpad button { touch-action: none; user-select: none; }
  .cam-params { max-height:220px; overflow:auto; margin-top:6px; border:1px solid var(--border); border-radius:4px; }
  .cam-params table { width:100%; border-collapse:collapse; font-size:11px; }
  .cam-params td { padding:2px 6px; border-bottom:1px solid var(--border); word-break:break-all; }
  .cam-params td:first-child { color:var(--muted); white-space:nowrap; }
  .cam-params tr.sec td { color:var(--text, inherit); font-weight:600; background:rgba(127,127,127,0.12); }
  .dpad {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    width: 150px;
    margin: 0 auto;
  }
  .dpad button {
    height: 40px;
    font-size: 15px;
  }

  /* Events Grid */
  .events-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
    gap: 12px;
    margin-top: 12px;
  }
  .event-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .event-thumb {
    aspect-ratio: 16 / 9;
    background: #000;
    position: relative;
    cursor: pointer;
  }
  .event-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .event-meta { padding: 10px; font-size: 12px; display: flex; flex-direction: column; gap: 4px; }

  /* Settings Table */
  .settings-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 16px;
  }
  .card-panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }
  .form-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px solid rgba(48,54,61,0.5);
    font-size: 13px;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <span>📹 C22S Camera Hub & Smart Motion NVR</span>
    <span class="badge ok" id="hdr-cam-count">0 Camera</span>
    <span class="badge ok" id="hdr-ws-status">⚡ Live 25 FPS</span>
    <span class="badge" id="hdr-storage">RAM Buffer: 0 KB</span>
  </div>
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('live', this)">Chế độ Lưới (Live Grid)</button>
    <button class="tab-btn" onclick="switchTab('events', this)">Lịch sử Chuyển động (Events)</button>
    <button class="tab-btn" onclick="switchTab('settings', this)">Cài đặt & Quản lý Phòng</button>
  </div>
  <div class="toolbar">
    <button class="btn primary" onclick="openQrModal()" style="font-weight:700; background:linear-gradient(135deg,#238636,#2ea043); border-color:#2ea043;">🚀 Khui Camera Mới / Thêm QR (1-Click)</button>
    <a class="btn" href="/sonic" target="_blank" style="text-decoration:none; border-color:#58a6ff; color:#58a6ff;" title="Trang nạp Wi-Fi rút gọn tối ưu cho màn hình Điện thoại">📱 Chế độ ĐT (/sonic)</a>
    <button class="btn" id="btn-scan-lan" onclick="scanLanNow()" title="Quét toàn bộ 254 IP trong mạng LAN để bắt camera mới">🔍 Quét LAN</button>
    <label>Bố cục:</label>
    <select id="grid-cols" onchange="updateGridCols()">
      <option value="auto">Tự động</option>
      <option value="cols-2">Lưới 2×2</option>
      <option value="cols-3">Lưới 3×3</option>
      <option value="cols-4">Lưới 4×4 (15-16 Phòng)</option>
    </select>
    <label>Độ mượt Lưới:</label>
    <select id="grid-fps" onchange="syncWsConfig()">
      <option value="25" selected>⚡ 25 FPS (Real-time Siêu mượt)</option>
      <option value="15">⚖️ 15 FPS (Cân bằng)</option>
      <option value="6">🍃 6 FPS (Tiết kiệm CPU cho i3-6100)</option>
      <option value="0">⏸ Tắt hình Lưới (0% Decode CPU)</option>
    </select>
  </div>
</header>

<main>
  <!-- TAB 1: LIVE GRID -->
  <section id="tab-live" class="view-section active">
    <div id="cam-grid" class="cam-grid"></div>
  </section>

  <!-- TAB 2: EVENTS PLAYBACK -->
  <section id="tab-events" class="view-section">
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
      <label>Lọc theo phòng:</label>
      <select id="ev-room-filter" onchange="loadEvents()"><option value="ALL">Tất cả phòng</option></select>
      <button class="btn primary" onclick="loadEvents()">🔄 Làm mới danh sách</button>
      <span id="ev-summary" style="color:var(--muted); font-size:12px;"></span>
    </div>
    <div id="events-grid" class="events-grid"></div>
  </section>

  <!-- TAB 3: SETTINGS & STORAGE -->
  <section id="tab-settings" class="view-section">
    <div class="settings-grid">
      <div class="card-panel">
        <h3 style="margin-bottom:12px; font-size:15px;">⚙️ Cấu hình Bộ đệm RAM & Ghi hình Chuyển động</h3>
        <div class="form-row">
          <span>Ghi trước chuyển động (RAM Pre-roll):</span>
          <div><input type="number" id="set-preroll" value="10" min="2" max="25" style="width:70px"> giây</div>
        </div>
        <div class="form-row">
          <span>Ghi sau chuyển động (Post-roll):</span>
          <div><input type="number" id="set-postroll" value="10" min="3" max="60" style="width:70px"> giây</div>
        </div>
        <div class="form-row">
          <span>Độ nhạy đột biến P-Frame H.265 (1.5 = nhạy, 2.5 = chuẩn):</span>
          <div><input type="number" id="set-sens" value="2.25" step="0.05" min="1.2" max="3.5" style="width:70px">x</div>
        </div>
        <div class="form-row">
          <span>Kích hoạt P-Frame Spike Trigger (kết hợp cùng AI/Alarm phần cứng):</span>
          <input type="checkbox" id="set-pframe-en" checked>
        </div>
        <div class="form-row">
          <span>Giới hạn dung lượng thư mục Recordings tối đa:</span>
          <div><input type="number" id="set-maxgb" value="50" min="1" max="2000" style="width:70px"> GB</div>
        </div>
        <div class="form-row">
          <span>Dung lượng ổ cứng trống tối thiểu (tự xóa xoay vòng):</span>
          <div><input type="number" id="set-freegb" value="10" min="1" max="500" style="width:70px"> GB</div>
        </div>
        <div style="margin-top:14px; text-align:right;">
          <button class="btn primary" onclick="saveSettings()">💾 Lưu Cấu Hình</button>
        </div>
      </div>

      <div class="card-panel">
        <h3 style="margin-bottom:12px; font-size:15px;">🏷️ Quản lý Danh sách Phòng & Đổi tên OSD</h3>
        <div id="room-Rename-list"></div>
      </div>
    </div>
  </section>
</main>

<!-- FOCUS & PTZ MODAL -->
<div class="modal-backdrop" id="focus-modal">
  <div class="modal-box">
    <div class="modal-header">
      <div style="display:flex; align-items:center; gap:10px;">
        <strong id="focus-title">Phòng</strong>
        <span class="badge ok" id="focus-fps-badge">25 FPS</span>
      </div>
      <div style="display:flex; gap:8px;">
        <button class="btn" onclick="triggerMotionManual(focusUid, 'trigger')">⚡ Ghi thử 20s (Pre+Post)</button>
        <button class="btn danger" onclick="closeFocusModal()">✖ Đóng</button>
      </div>
    </div>
    <div class="modal-body">
      <div class="focus-stage" id="focus-stage">
        <img id="focus-img" alt="Focus Live Stream">
      </div>
      <div class="ptz-sidebar">
        <div>
          <strong>🕹️ Điều khiển Xoay PTZ 360°</strong>
          <div style="color:var(--muted); margin-top:2px;">Chạm = nhích 1 bước · Giữ = xoay liên tục</div>
          <div class="dpad" style="margin-top:8px;">
            <button class="btn" data-ptz="leftup">◤</button>
            <button class="btn" data-ptz="up">▲</button>
            <button class="btn" data-ptz="rightup">◥</button>
            <button class="btn" data-ptz="left">◀</button>
            <button class="btn" onclick="sendPtz('center')" title="Về giữa">●</button>
            <button class="btn" data-ptz="right">▶</button>
            <button class="btn" data-ptz="leftdown">◣</button>
            <button class="btn" data-ptz="down">▼</button>
            <button class="btn" data-ptz="rightdown">◢</button>
          </div>
          <div class="row">
            <button class="btn" onclick="sendPtz('hscan')">↔ Quét ngang</button>
            <button class="btn" onclick="sendPtz('vscan')">↕ Quét dọc</button>
            <button class="btn danger" onclick="sendPtz('hscan_stop'); sendPtz('vscan_stop'); sendPtz('stop');">■ Dừng</button>
          </div>
          <label class="sl"><span>Tốc độ xoay</span>
            <input type="range" min="1" max="10" value="10" id="sl-ptzspeed" onchange="sendPtz('ptz_speed', {value: this.value})">
          </label>
        </div>
        <div>
          <strong>📍 Vị trí nhớ (Presets)</strong>
          <div class="row">
            <select id="ptz-preset" style="flex:1;">
              <option value="1">Góc 1</option><option value="2">Góc 2</option><option value="3">Góc 3</option>
              <option value="4">Góc 4</option><option value="5">Góc 5</option><option value="6">Góc 6</option>
              <option value="7">Góc 7</option><option value="8">Góc 8</option><option value="9">Góc 9</option>
              <option value="10">Góc 10</option><option value="11">Góc 11</option><option value="12">Góc 12</option>
              <option value="13">Góc 13</option><option value="14">Góc 14</option><option value="15">Góc 15</option>
              <option value="16">Góc 16</option>
            </select>
            <button class="btn primary" onclick="sendPtz('preset' + document.getElementById('ptz-preset').value + '_go')">Gọi</button>
            <button class="btn" onclick="if(confirm('Lưu vị trí hiện tại vào góc ' + document.getElementById('ptz-preset').value + '?')) sendPtz('preset' + document.getElementById('ptz-preset').value + '_set')">Lưu</button>
          </div>
        </div>
        <div>
          <strong>🌙 Hồng ngoại & Hình ảnh</strong>
          <div class="row">
            <button class="btn" onclick="sendPtz('ir_auto')">IR Tự động</button>
            <button class="btn" onclick="sendPtz('ir_on')">Bật IR</button>
            <button class="btn" onclick="sendPtz('ir_off')">Tắt IR</button>
          </div>
          <div class="row">
            <button class="btn" onclick="sendPtz('flip_normal')">Hình chuẩn</button>
            <button class="btn" onclick="sendPtz('flip_vh')">Lật 180°</button>
            <button class="btn" onclick="sendPtz('flip_v')">Lật dọc</button>
            <button class="btn" onclick="sendPtz('flip_h')">Gương</button>
          </div>
          <label class="sl"><span>Độ sáng</span>
            <input type="range" min="0" max="255" value="128" id="sl-brightness" onchange="sendPtz('brightness', {value: this.value})">
          </label>
          <label class="sl"><span>Tương phản</span>
            <input type="range" min="0" max="255" value="128" id="sl-contrast" onchange="sendPtz('contrast', {value: this.value})">
          </label>
          <label class="sl"><span>Bão hoà</span>
            <input type="range" min="0" max="255" value="128" id="sl-saturation" onchange="sendPtz('saturation', {value: this.value})">
          </label>
          <label class="sl"><span>Âm lượng mic</span>
            <input type="range" min="0" max="31" value="31" id="sl-mic" onchange="sendPtz('mic_volume', {value: this.value})">
          </label>
          <label class="sl"><span>Âm lượng loa</span>
            <input type="range" min="0" max="31" value="31" id="sl-speaker" onchange="sendPtz('speaker_volume', {value: this.value})">
          </label>
          <div class="row">
            <button class="btn" onclick="sendPtz('osd_on')">Hiện OSD</button>
            <button class="btn" onclick="sendPtz('osd_off')">Ẩn OSD</button>
            <button class="btn" onclick="sendPtz('hz_50')">50Hz</button>
            <button class="btn" onclick="sendPtz('hz_60')">60Hz</button>
          </div>
        </div>
        <div>
          <strong>🔔 Cảnh báo & Đèn</strong>
          <label class="sl"><span>Độ nhạy CĐ</span>
            <input type="range" min="1" max="10" value="5" id="sl-motion" onchange="sendPtz('motion_sensitivity', {value: this.value})">
          </label>
          <div class="row">
            <button class="btn" onclick="sendPtz('led_mode', {value: 1})">Bật đèn báo</button>
            <button class="btn" onclick="sendPtz('led_mode', {value: 0})">Tắt đèn báo</button>
          </div>
          <div class="row">
            <button class="btn" onclick="sendPtz('track_on')">Bám theo người: Bật</button>
            <button class="btn" onclick="sendPtz('track_off')">Tắt</button>
          </div>
          <div class="row">
            <button class="btn" onclick="sendPtz('humanframe_on')">Khung người: Bật</button>
            <button class="btn" onclick="sendPtz('humanframe_off')">Tắt</button>
          </div>
          <div style="color:var(--muted); margin-top:4px;">Bấm “Đọc từ camera” bên dưới để xem trạng thái thực sau khi đổi.</div>
        </div>
        <div>
          <strong>⚙️ Thiết Lập Camera Từ Xa (Internet/LAN)</strong>
          <div style="display:flex; gap:5px; flex-wrap:wrap; margin-top:6px;">
            <button class="btn" onclick="rebootCamera(focusUid, 'Camera')">🔄 Khởi động lại</button>
            <button class="btn" onclick="changeCameraPassword(focusUid, 'Camera')">🔑 Đổi mật khẩu</button>
            <button class="btn" onclick="pushWifiToCamera(focusUid, 'Camera')">📡 Đổi Wi-Fi</button>
          </div>
        </div>
        <div id="ptz-status" style="min-height:22px; font-size:12px; margin-top:4px; padding:6px 10px; border-radius:4px; display:none;"></div>
        <div>
          <strong>📋 Thông số & tính năng camera</strong>
          <div class="row">
            <button class="btn" onclick="loadCamParams()">Đọc từ camera</button>
            <input type="text" id="cam-params-filter" placeholder="lọc..." oninput="renderCamParams()" style="flex:1; min-width:60px;">
          </div>
          <div id="cam-caps" style="display:none; margin-top:6px;"></div>
          <div class="cam-params" id="cam-params" style="display:none;"></div>
        </div>
        <details>
          <summary style="cursor:pointer;"><strong>🧪 Gửi CGI thủ công (nâng cao)</strong></summary>
          <div class="row">
            <input type="text" id="raw-cgi" placeholder="get_misc.cgi  hoặc  camera_control.cgi?param=1&value=128" style="flex:1; min-width:120px;" onkeydown="if(event.key==='Enter')sendRawCgi()">
            <button class="btn" onclick="sendRawCgi()">Gửi</button>
          </div>
          <pre id="raw-cgi-out" style="display:none; white-space:pre-wrap; word-break:break-all; font-size:11px; max-height:160px; overflow:auto; margin:6px 0 0; padding:6px; border:1px solid var(--border); border-radius:4px;"></pre>
        </details>
        <div id="focus-info" style="color:var(--muted); line-height:1.5;"></div>
      </div>
    </div>
  </div>
</div>

<!-- ADD CAMERA BY QR & WIFI PROVISIONING MODAL -->
<div class="modal-backdrop" id="qr-modal">
  <div class="modal-box" style="max-width:820px; max-height:94vh; overflow-y:auto;">
    <div class="modal-header">
      <strong>🚀 Thêm Camera (QR &amp; UID) / Khui Hộp Nạp Wi-Fi 1-Click</strong>
      <button class="btn danger" onclick="closeQrModal()">✖ Đóng</button>
    </div>
    <div style="padding:16px; display:flex; flex-direction:column; gap:14px; font-size:13px;">

      <!-- 2-TAB SEGMENTED CONTROL -->
      <div style="display:flex; border-bottom:2px solid var(--border); gap:8px;">
        <button id="modal-tab-link" type="button" onclick="switchModalTab('link')"
                style="padding:10px 18px; font-size:13px; font-weight:700; border:none; background:none; cursor:pointer; color:#58a6ff; border-bottom:3px solid #1f6feb; margin-bottom:-2px; display:flex; align-items:center; gap:6px;">
          🔗 Camera Đã Có Wi-Fi (Chỉ Liên Kết Server)
        </button>
        <button id="modal-tab-unbox" type="button" onclick="switchModalTab('unbox')"
                style="padding:10px 18px; font-size:13px; font-weight:700; border:none; background:none; cursor:pointer; color:var(--muted); border-bottom:3px solid transparent; margin-bottom:-2px; display:flex; align-items:center; gap:6px;">
          📦 Khui Hộp Mới (Nạp Wi-Fi Bằng Sóng Âm &amp; Liên Kết Tự Động)
        </button>
      </div>

      <!-- DESCRIPTIONS -->
      <div id="modal-desc-link" style="color:var(--muted); font-size:12.5px; line-height:1.45; background:#161b22; padding:10px 12px; border-radius:6px; border:1px solid var(--border);">
        💡 <b>Camera đã có mạng Wi-Fi hoặc cắm dây LAN:</b> Chỉ cần quét/nhập UID. Server từ xa sẽ kích hoạt tìm kiếm WAN P2P &amp; quét LAN, đục lỗ UDP trực tiếp qua Internet mà không cần nạp lại Wi-Fi.
      </div>
      <div id="modal-desc-unbox" style="display:none; color:var(--muted); font-size:12.5px; line-height:1.45; background:#161b22; padding:10px 12px; border-radius:6px; border:1px solid #238636;">
        📦 <b>Camera mới khui hộp (Mật khẩu mặc định <code>888888</code>):</b> Nhập UID + Tên &amp; Mật khẩu Wi-Fi tại nơi lắp. Loa điện thoại/PC sẽ phát sóng âm nạp Wi-Fi vào Camera và Server sẽ tự động đục lỗ UDP bắt tay, rồi <b>tự động tắt sóng âm</b> ngay khi kết nối thành công ("Một lần và mãi mãi")!
      </div>

      <!-- STEP 1: QR IMAGE UPLOAD & UID -->
      <div style="background:#0b0f17; border:1px solid var(--border); border-radius:8px; padding:12px;">
        <div style="font-weight:700; margin-bottom:8px; color:#58a6ff;">Bước 1: Quét/Tải ảnh Tem QR hoặc Nhập mã UID — Hỗ trợ kết nối Camera ở BẤT KỲ MẠNG NÀO qua Internet (🌐 WAN P2P / Cloud Relay) &amp; LAN</div>
        <div id="qr-dropzone"
             onclick="document.getElementById('qr-file-input').click()"
             ondragover="event.preventDefault(); this.style.borderColor='#2f81f7';"
             ondragleave="this.style.borderColor='var(--border)';"
             ondrop="handleQrDrop(event)"
             style="border:2px dashed var(--border); border-radius:8px; padding:14px; text-align:center; cursor:pointer; background:#161b22; transition:0.15s;">
          <input type="file" id="qr-file-input" accept="image/*" style="display:none" onchange="handleQrFileSelect(this.files[0])">
          <div id="qr-preview-wrap" style="display:none; margin-bottom:8px;">
            <img id="qr-preview-img" style="max-height:130px; border-radius:6px; border:1px solid var(--border);">
          </div>
          <div style="font-size:13px; font-weight:600; color:var(--text);">📤 Chụp/Chọn ảnh tem QR dưới đáy Camera, Kéo thả ảnh, hoặc bấm <kbd style="background:#21262d; padding:2px 6px; border-radius:4px;">Ctrl + V</kbd></div>
          <div style="color:var(--muted); font-size:11px; margin-top:3px;">Camera ở mạng khác / từ xa qua Internet (WAN P2P) có thể thêm trực tiếp bằng mã UID (VD: <code>VSTJ082299RLSXK</code> hoặc <code>VSTJ-082299-RLSXK</code>)</div>
        </div>

        <div id="qr-decode-result" style="display:none; margin-top:10px; padding:8px 12px; border-radius:6px; background:rgba(35,134,54,0.15); border:1px solid var(--accent); line-height:1.5; font-size:12px;"></div>

        <div style="display:grid; grid-template-columns:1.2fr 1fr 1fr 90px; gap:10px; margin-top:10px;">
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Mã UID trên tem Camera (*):</label>
            <input type="text" id="qr-did-input" placeholder="VD: VSTJ082299RLSXK" style="width:100%; font-family:monospace;">
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Tên Phòng / Vị trí hiển thị (*):</label>
            <input type="text" id="qr-room-input" placeholder="VD: C22S_1" style="width:100%;">
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Mật khẩu Camera:</label>
            <input type="text" id="qr-pwd-input" value="888888" placeholder="Mặc định: 888888" style="width:100%; font-family:monospace;">
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Model:</label>
            <input type="text" id="qr-model-input" value="CS22S" style="width:100%;">
          </div>
        </div>
        <input type="hidden" id="qr-ip-input" value="">

        <!-- ACTION IN STEP 1 WHEN IN LINK-ONLY MODE -->
        <div id="modal-action-link" style="margin-top:12px; display:flex; gap:10px; align-items:center;">
          <button class="btn primary" type="button" id="btn-modal-link-only" onclick="startOneClickOnboarding(false)"
                  style="flex:1; background:linear-gradient(135deg,#1f6feb,#238636); border-color:#388bfd; font-size:14px; font-weight:800; padding:11px 18px; border-radius:8px; box-shadow:0 4px 14px rgba(31,111,235,0.35);">
            🔗 Liên Kết Server Ngay (Không Cần Nạp Wi-Fi)
          </button>
        </div>
      </div>

      <!-- STEP 2: 1-CLICK UNBOXING & WIFI PROVISIONING (ONLY IN UNBOX MODE) -->
      <div id="modal-unbox-step-2" style="display:none; background:#0b0f17; border:1px solid #238636; border-radius:8px; padding:14px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; flex-wrap:wrap; gap:6px;">
          <span style="font-weight:700; color:#3fb950; font-size:14px;">Bước 2: Nạp Wi-Fi cho Camera mới khui hộp (Qua loa Điện thoại / PC)</span>
          <div style="display:flex; gap:6px;">
            <a class="btn" href="/sonic" target="_blank" style="font-size:11px; padding:3px 8px; text-decoration:none; color:#58a6ff; border-color:#58a6ff;">📱 Mở trang Mobile /sonic</a>
            <button class="btn" onclick="loadHostWifiInfo(true)" style="font-size:11px; padding:3px 8px;">🔄 Gợi ý Wi-Fi</button>
          </div>
        </div>
        <div style="color:var(--muted); font-size:12px; margin-bottom:10px; line-height:1.45;">
          <b>"Một lần và mãi mãi":</b> Điện thoại chỉ làm cầu nối tạm thời để phát sóng âm nạp Wi-Fi tại nơi lắp đặt. Ngay khi Camera vào Wi-Fi và báo danh lên P2P Cloud, Server từ xa sẽ tự động đục lỗ UDP bắt tay trực tiếp và <b>tự động ngắt sóng âm</b>!
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Tên Wi-Fi (2.4GHz) tại nơi đặt Camera (*):</label>
            <div style="display:flex; gap:6px;">
              <input type="text" id="wifi-ssid-input" list="wifi-ssid-list" placeholder="Nhập tên mạng Wi-Fi 2.4GHz tại nơi lắp..." style="width:100%; font-weight:600;" oninput="onWifiSsidInput()">
              <datalist id="wifi-ssid-list"></datalist>
            </div>
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Mật khẩu Wi-Fi tại nơi đặt Camera (*):</label>
            <div style="display:flex; gap:6px;">
              <input type="password" id="wifi-pwd-input" placeholder="Nhập mật khẩu Wi-Fi..." style="width:100%;" oninput="refreshWifiQrPreview()">
              <button class="btn" type="button" onclick="toggleWifiPwdVis()" title="Hiện/Ẩn mật khẩu" style="padding:5px 8px;">👁️</button>
            </div>
          </div>
        </div>
        <div style="margin-top:8px;">
          <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">4 số cuối MAC router, in trên tem đáy router (VD: <code>001e</code>) — nên nhập, đây là cách app Eye4 nạp Wi-Fi:</label>
          <input type="text" id="wifi-bssid-input" value="" placeholder="VD: 001e (để trống = phát Full SSID, chưa kiểm chứng trên C22S)" style="width:100%;" autocapitalize="off" autocomplete="off" oninput="syncSonicModeFromBssid(); refreshWifiQrPreview()">
        </div>

        <div id="wifi-scan-status" style="color:var(--muted); font-size:11px; margin-top:6px;"></div>

        <!-- 1-CLICK ONBOARD BUTTON & LIVE 3-STEP TRACKER -->
        <div id="modal-action-unbox" style="margin-top:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <button class="btn primary" type="button" id="btn-oneclick-onboard" onclick="startOneClickOnboarding(true)"
                  style="flex:1; min-width:260px; background:linear-gradient(135deg,#238636,#2ea043); border-color:#3fb950; font-size:15px; font-weight:800; padding:12px 18px; border-radius:8px; box-shadow:0 4px 14px rgba(35,134,54,0.35);">
            🔊 Nạp Wi-Fi &amp; Tự Động Liên Kết Server
          </button>
          <button class="btn" type="button" onclick="toggleOpticalWifiQrModal()" style="border-color:#58a6ff; color:#58a6ff; padding:11px 12px; font-size:12px;">
            👁️ Hiện QR Quang Học (Quét bằng mắt Cam)
          </button>
          <button class="btn danger" type="button" onclick="stopSonicWave()" style="padding:11px 12px; font-size:12px;">
            ⏹ Dừng phát âm
          </button>
        </div>

        <!-- OPTICAL WIFI QR CODE BOX (FOR CAMERA LENS SCANNING) -->
        <div id="optical-wifi-qr-box" style="display:none; margin-top:12px; padding:12px; background:#fff; color:#111; border-radius:8px; text-align:center;">
          <img id="optical-wifi-qr-img" style="width:210px; height:210px; display:block; margin:0 auto;" alt="Optical Wi-Fi QR">
          <div style="font-size:12px; font-weight:700; margin-top:6px;">📷 Đưa màn hình mã QR này ra trước ống kính Camera (cách 15–20 cm) nếu muốn quét bằng mắt Camera</div>
        </div>

        <!-- VStarcam Sonic Wave Audio Provisioning + Phone Speaker Helper QR -->
        <div id="wifi-qr-box" style="display:grid; margin-top:12px; padding:12px; background:#161b22; border:1px solid var(--border); border-radius:8px; grid-template-columns:175px 1fr; gap:14px; align-items:center;">
          <div style="text-align:center; background:#fff; padding:8px; border-radius:8px; color:#0d1117;">
            <img id="wifi-qr-img" style="width:155px; height:155px; display:block; margin:0 auto;" alt="Phone Sonic QR">
            <div style="font-size:10px; font-weight:700; margin-top:4px; color:#111;">📱 Quét mở trên Điện thoại</div>
          </div>
          <div style="display:flex; flex-direction:column; gap:8px;">
            <div style="font-weight:700; color:#3fb950; font-size:13px;">🔊 Bộ phát Sóng Âm VStarcam Sonic Wave (6.5–10.1 kHz)</div>
            <div style="color:var(--muted); font-size:11.5px; line-height:1.45;">
              Hãy bật <b>Max Volume (80–100% âm lượng)</b> và đặt loa Điện thoại sát lỗ Mic của Camera (cách 2–5 cm) khi Camera đang kêu <i>"Please use the App for Wi-Fi configuration"</i>.<br>
              • Mạng mục tiêu: <code id="wifi-target-label">Chưa nhập</code> (Mã hóa: <code id="sonic-bssid-badge">Full SSID</code>)
            </div>

            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
              <audio id="sonic-audio-player" controls loop style="height:30px; width:240px;"></audio>
              <select id="wifi-qr-fmt" onchange="refreshWifiQrPreview()" style="font-size:11px; padding:4px 6px;">
                <option value="ssid" selected>Chế độ: Chuẩn VStarcam Full SSID (Mọi mạng Internet/Từ xa)</option>
                <option value="combo">Chế độ: Kết hợp BSSID 2.4GHz + Tên SSID</option>
                <option value="mac">Chế độ: Chuẩn VStarcam MAC 2.4GHz (1.8s/gói)</option>
              </select>
              <button class="btn" type="button" onclick="playSonicWavePcSpeaker()" style="border-color:#58a6ff; color:#58a6ff; font-size:11px; padding:4px 8px;">
                🖥️ Loa PC
              </button>
              <button class="btn" type="button" onclick="pushWifiDirectCgi()" style="font-size:11px; padding:4px 8px;">📡 Bắn set_wifi.cgi</button>
            </div>

            <div id="sonic-mobile-link-box" style="font-size:11px; color:var(--muted);">
              📱 Đang ngồi trên PC? Dùng ĐT quét mã QR bên trái hoặc mở <a id="sonic-mobile-link" href="/sonic" target="_blank" style="color:#58a6ff;">trang Mobile /sonic</a> để mang ĐT ra chỗ lắp Camera!
            </div>

            <div id="wifi-Wait-banner" style="padding:6px 10px; border-radius:6px; background:rgba(47,129,247,0.15); border:1px solid #2f81f7; color:#58a6ff; font-size:12px;">
              🔄 Nhập UID + Wi-Fi rồi bấm <b>"🔊 Nạp Wi-Fi &amp; Tự Động Liên Kết Server"</b> để bắt đầu!
            </div>
          </div>
        </div>
      </div>

      <!-- 3-STEP LIVE PROGRESS TRACKER (COMMON TO BOTH MODES) -->
      <div id="onboard-progress-box" style="display:none; padding:12px; background:#161b22; border:1px solid #2f81f7; border-radius:8px;">
        <div style="font-weight:700; font-size:13px; color:#e6edf3; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center;">
          <span id="onboard-progress-title">🚀 Tiến trình Liên Kết WAN P2P &amp; LAN (Fast-Poll 1.2s)</span>
          <span class="badge" id="onboard-cloud-badge" style="color:#58a6ff; border-color:#2f81f7;">WAITING_FOR_WIFI</span>
        </div>
        <div style="display:flex; flex-direction:column; gap:7px; font-size:12.5px;">
          <div id="onboard-step-1" style="padding:8px 11px; border-radius:6px; background:rgba(47,129,247,0.14); border:1px solid #2f81f7; color:#58a6ff;">
            <b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P / LAN...
          </div>
          <div id="onboard-step-2" style="padding:8px 11px; border-radius:6px; background:#0b0f17; border:1px solid var(--border); color:var(--muted);">
            <b>Bước 2:</b> 🌐 Chờ Camera vào mạng &amp; báo danh lên P2P Cloud (<code>WAITING_FOR_WIFI</code> → <code>ONLINE_PUNCHING</code>)...
          </div>
          <div id="onboard-step-3" style="padding:8px 11px; border-radius:6px; background:#0b0f17; border:1px solid var(--border); color:var(--muted);">
            <b>Bước 3:</b> 🔗 Server đục lỗ UDP &amp; xác thực (<code>CONNECTED</code>)...
          </div>
        </div>
        <div id="onboard-final-banner" style="display:none; margin-top:10px; padding:10px 12px; border-radius:6px; background:rgba(63,185,80,0.2); border:1px solid #3fb950; color:#3fb950; font-weight:700; font-size:13px;"></div>
      </div>

      <!-- MODAL FOOTER BUTTONS -->
      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; border-top:1px solid var(--border); padding-top:10px;">
        <button class="btn danger" onclick="closeQrModal()">✖ Đóng</button>
        <div style="display:flex; gap:8px;">
          <button class="btn primary" id="modal-bottom-link-btn" onclick="startOneClickOnboarding(false)" style="font-weight:700;">
            🔗 Liên Kết Server Ngay
          </button>
          <button class="btn primary" id="modal-bottom-unbox-btn" onclick="startOneClickOnboarding(true)" style="display:none; background:linear-gradient(135deg,#238636,#2ea043); border-color:#3fb950; font-weight:700;">
            🔊 Nạp Wi-Fi &amp; Liên Kết Ngay
          </button>
        </div>
      </div>
    </div>
  </div>
</div>
    </div>
  </div>
</div>

<!-- VIDEO PLAYBACK MODAL -->
<div class="modal-backdrop" id="play-modal">
  <div class="modal-box" style="max-width:960px;">
    <div class="modal-header">
      <strong id="play-title">Xem lại Sự kiện</strong>
      <div style="display:flex; gap:6px; align-items:center;">
        <button class="btn" onclick="setPlaySpeed(1)">1x</button>
        <button class="btn" onclick="setPlaySpeed(2)">2x</button>
        <button class="btn" onclick="setPlaySpeed(4)">4x</button>
        <a id="play-download" class="btn primary" download style="text-decoration:none;">⬇ Tải MP4</a>
        <button class="btn danger" onclick="closePlayModal()">✖ Đóng</button>
      </div>
    </div>
    <div class="focus-stage">
      <video id="play-video" controls autoplay playsinline></video>
    </div>
  </div>
</div>

<script>
let camerasData = [];
let focusUid = null;
let currentTab = 'live';
let settingsLoaded = false;
let ws = null;
const prevBlobUrls = {};
const fpsCounters = {}; // uid -> { count, lastTs, fps }

function switchTab(tab, btn) {
  currentTab = tab;
  document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  btn.classList.add('active');
  if (tab === 'events') loadEvents();
  syncWsConfig();
}

function updateGridCols() {
  const val = document.getElementById('grid-cols').value;
  const grid = document.getElementById('cam-grid');
  grid.className = 'cam-grid ' + (val === 'auto' ? '' : val);
}

// =========================================================================
// Multiplexed Real-Time 25 FPS Binary WebSocket Stream
// =========================================================================

function connectStreamWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/streams`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    const b = document.getElementById('hdr-ws-status');
    b.textContent = '⚡ Live Stream Connected';
    b.className = 'badge ok';
    syncWsConfig();
  };

  ws.onmessage = (ev) => {
    if (!(ev.data instanceof ArrayBuffer)) return;
    const u8 = new Uint8Array(ev.data);
    if (u8.length < 4) return;
    const ulen = u8[0];
    if (u8.length <= 1 + ulen) return;

    let uid = '';
    for (let i = 1; i <= ulen; i++) uid += String.fromCharCode(u8[i]);

    const jpegSlice = u8.subarray(1 + ulen);
    const blob = new Blob([jpegSlice], { type: 'image/jpeg' });
    const url = URL.createObjectURL(blob);

    // Update grid card image
    const gridImg = document.getElementById('img-' + uid);
    if (gridImg) gridImg.src = url;

    // Update focus modal image if focused on this camera
    if (focusUid === uid) {
      const focusImg = document.getElementById('focus-img');
      if (focusImg) focusImg.src = url;
    }

    // Revoke previous Blob URL to prevent memory leak
    if (prevBlobUrls[uid]) URL.revokeObjectURL(prevBlobUrls[uid]);
    prevBlobUrls[uid] = url;

    // Calculate live rendered FPS
    const now = performance.now();
    if (!fpsCounters[uid]) fpsCounters[uid] = { count: 0, lastTs: now, fps: 0 };
    const fc = fpsCounters[uid];
    fc.count++;
    if (now - fc.lastTs >= 1000) {
      fc.fps = Math.round((fc.count * 1000) / (now - fc.lastTs));
      fc.count = 0;
      fc.lastTs = now;
      if (focusUid === uid) {
        document.getElementById('focus-fps-badge').textContent = `⚡ ${fc.fps} FPS Real-Time`;
      }
    }
  };

  ws.onclose = () => {
    const b = document.getElementById('hdr-ws-status');
    b.textContent = '🔄 Reconnecting...';
    b.className = 'badge';
    setTimeout(connectStreamWs, 1000);
  };
}

function syncWsConfig() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const gridFps = parseFloat(document.getElementById('grid-fps').value || '25');
  const paused = document.hidden || (currentTab !== 'live' && !focusUid);
  ws.send(JSON.stringify({
    grid_fps: gridFps,
    focus_uid: focusUid,
    paused: paused,
  }));
}

document.addEventListener('visibilitychange', syncWsConfig);

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    camerasData = data.cameras || [];
    const st = data.storage || {};
    const totalRamKb = camerasData.reduce((acc, c) => acc + (c.ram_buffer_kb || 0), 0).toFixed(0);
    const recCount = camerasData.filter(c => c.is_recording).length;

    const camBadge = document.getElementById('hdr-cam-count');
    camBadge.textContent = `${camerasData.length} Camera Online` + (recCount > 0 ? ` (${recCount} ĐANG GHI)` : '');
    camBadge.className = 'badge ' + (recCount > 0 ? 'rec' : 'ok');

    document.getElementById('hdr-storage').textContent =
      `RAM Pre-roll: ${totalRamKb} KB | Đã lưu: ${st.clip_count || 0} clip (${st.used_mb || 0} MB / Trống ${st.disk_free_gb || 0} GB)`;

    if (!settingsLoaded && data.settings) {
      settingsLoaded = true;
      document.getElementById('set-preroll').value = data.settings.pre_roll_sec;
      document.getElementById('set-postroll').value = data.settings.post_roll_sec;
      document.getElementById('set-sens').value = data.settings.pframe_sensitivity;
      document.getElementById('set-pframe-en').checked = data.settings.enable_pframe_trigger;
      document.getElementById('set-maxgb').value = data.settings.max_storage_gb;
      document.getElementById('set-freegb').value = data.settings.min_free_gb;
    }

    renderCameraCards();
    renderRoomRenameList();
  } catch (e) {}
}

function renderCameraCards() {
  const grid = document.getElementById('cam-grid');
  const existingUids = new Set(Array.from(grid.children).map(el => el.dataset.uid));
  const currentUids = new Set(camerasData.map(c => c.uid));

  if (existingUids.size !== currentUids.size || [...existingUids].some(u => !currentUids.has(u))) {
    grid.innerHTML = '';
    for (const c of camerasData) {
      const card = document.createElement('div');
      card.className = 'cam-card';
      card.dataset.uid = c.uid;
      card.id = 'card-' + c.uid;
      card.innerHTML = `
        <div class="cam-video-wrap" onclick="openFocusModal('${c.uid}')">
          <img id="img-${c.uid}" src="/api/camera/${c.uid}/snapshot.jpg?t=${Date.now()}" alt="${c.room_name}">
          <div class="cam-overlay-top">
            <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
              <span class="room-pill" id="title-${c.uid}">${c.room_name}</span>
              <span class="badge" id="mode-${c.uid}">🌐 WAN P2P</span>
            </div>
            <span class="badge" id="badge-${c.uid}">IDLE</span>
          </div>
        </div>
        <div class="cam-footer">
          <span id="stat-${c.uid}"></span>
          <div class="cam-actions">
            <button class="btn" onclick="triggerMotionManual('${c.uid}', 'trigger')" title="Ghi ngay đoạn 10s trước + 10s sau">⚡ Ghi 20s</button>
            <button class="btn" id="mbtn-${c.uid}" onclick="toggleManualRecord('${c.uid}')">⏺ Ghi tay</button>
            <button class="btn primary" onclick="openFocusModal('${c.uid}')">🔍 Phóng to / PTZ</button>
          </div>
        </div>
      `;
      grid.appendChild(card);
    }

    const sel = document.getElementById('ev-room-filter');
    const curVal = sel.value;
    sel.innerHTML = '<option value="ALL">Tất cả phòng</option>' +
      camerasData.map(c => `<option value="${c.room_name}">${c.room_name} (${c.did || c.formatted_did || c.ip})</option>`).join('');
    sel.value = curVal || 'ALL';
  }

  for (const c of camerasData) {
    const card = document.getElementById('card-' + c.uid);
    if (!card) continue;
    card.className = 'cam-card' + (c.is_recording ? ' recording' : '');
    const isWaiting = (c.state === 'WAITING_LAN' || c.state === 'CONNECTING' || c.state === 'DISCONNECTED');
    const liveFps = (fpsCounters[c.uid] && fpsCounters[c.uid].fps) ? fpsCounters[c.uid].fps : c.fps;
    const didStr = c.did || c.formatted_did || c.hw_deviceid || c.short_uid;
    const ipPort = (c.ip && c.ip !== '0.0.0.0' && c.ip !== 'Chờ Wi-Fi LAN')
      ? (c.port ? `${c.ip}:${c.port}` : c.ip)
      : '0.0.0.0:32108';
    const connMode = (c.connection_mode || 'LAN').toUpperCase();

    const modeBadge = document.getElementById('mode-' + c.uid);
    if (modeBadge) {
      if (connMode.includes('RELAY')) {
        modeBadge.textContent = `☁️ WAN RELAY • ${didStr}`;
        modeBadge.style.background = 'rgba(163,113,247,0.22)';
        modeBadge.style.borderColor = 'rgba(163,113,247,0.55)';
        modeBadge.style.color = '#d2a8ff';
      } else if (connMode.includes('WAN')) {
        modeBadge.textContent = `🌐 WAN P2P • ${didStr}`;
        modeBadge.style.background = 'rgba(47,129,247,0.22)';
        modeBadge.style.borderColor = 'rgba(47,129,247,0.55)';
        modeBadge.style.color = '#58a6ff';
      } else {
        modeBadge.textContent = `🏠 LAN • ${didStr}`;
        modeBadge.style.background = 'rgba(63,185,80,0.20)';
        modeBadge.style.borderColor = 'rgba(63,185,80,0.50)';
        modeBadge.style.color = '#3fb950';
      }
    }

    document.getElementById('title-' + c.uid).textContent = isWaiting
      ? `${c.room_name} (${ipPort})`
      : `${c.room_name} (${ipPort}) • ${liveFps} FPS`;

    const badge = document.getElementById('badge-' + c.uid);
    if (isWaiting) {
      badge.className = 'badge';
      const cloudSt = c.wan_cloud_status || 'IDLE';
      if (c.onboarding_active) {
        badge.style.background = 'rgba(47,129,247,0.28)';
        badge.style.color = '#58a6ff';
        badge.style.borderColor = '#2f81f7';
        badge.textContent = `🚀 KHUI HỘP • ${cloudSt} (${didStr})`;
      } else {
        badge.style.background = 'rgba(210,153,34,0.25)';
        badge.style.color = '#f59e0b';
        badge.textContent = `⏳ KẾT NỐI ${connMode.replace('_', ' ')} • ${cloudSt}`;
      }
    } else if (c.is_recording) {
      badge.style.background = '';
      badge.style.color = '';
      badge.className = 'badge rec';
      badge.textContent = `● REC (${c.last_trigger || 'MOTION'} | +${c.post_roll_remaining}s)`;
    } else {
      badge.style.background = '';
      badge.style.color = '';
      badge.className = 'badge ok';
      badge.textContent = `RAM Pre-roll (${c.ram_buffer_kb} KB)`;
    }

    document.getElementById('stat-' + c.uid).textContent = isWaiting
      ? `UID: ${didStr} | ${ipPort} | RTSP :${c.rtsp_port} | Đang bắt tay ${connMode.replace('_', ' ')}`
      : `${didStr} (${ipPort}) | ${c.codec.toUpperCase()} | ${c.kbps} KB/s | Cam ${c.fps}fps | RTSP :${c.rtsp_port} | Clips: ${c.clips_saved}`;

    const mbtn = document.getElementById('mbtn-' + c.uid);
    if (mbtn) {
      mbtn.className = 'btn' + (c.manual_record ? ' danger' : '');
      mbtn.textContent = c.manual_record ? '⏹ Dừng ghi' : '⏺ Ghi tay';
    }

    if (focusUid === c.uid) {
      document.getElementById('focus-info').innerHTML =
        `<b>UID (DID):</b> <code>${didStr}</code><br>` +
        `<b>Chế độ:</b> ${connMode} (<code>${ipPort}</code>)<br>` +
        `<b>RTSP:</b> <code>${c.rtsp_url}</code><br>` +
        `<b>Bitrate:</b> ${c.kbps} KB/s (${c.fps} fps)<br>` +
        `<b>Bộ đệm RAM:</b> ${c.ram_buffer_kb} KB<br>` +
        `<b>Trạng thái:</b> ${c.is_recording ? '🔴 Đang ghi chuyển động' : '🟢 Đang chờ trên RAM (0 ghi đĩa)'}`;
    }
  }
}

function openFocusModal(uid) {
  const cam = camerasData.find(c => c.uid === uid);
  if (cam && cam.state === 'WAITING_LAN') {
    lastAddedUid = uid;
    openQrModal(cam.formatted_did || '', cam.room_name || '');
    setTimeout(() => refreshWifiQrPreview(true), 250);
    return;
  }
  focusUid = uid;
  document.getElementById('focus-title').textContent = cam ? `📹 ${cam.room_name} (${cam.ip})` : uid;
  if (prevBlobUrls[uid]) {
    document.getElementById('focus-img').src = prevBlobUrls[uid];
  } else {
    document.getElementById('focus-img').src = `/api/camera/${uid}/snapshot.jpg?t=${Date.now()}`;
  }
  document.getElementById('focus-modal').classList.add('open');
  syncWsConfig();
}

function closeFocusModal() {
  ptzHoldEnd();
  focusUid = null;
  camParamsData = null;
  for (const id of ['cam-params', 'cam-caps']) {
    const cp = document.getElementById(id);
    if (cp) { cp.style.display = 'none'; cp.innerHTML = ''; }
  }
  document.getElementById('focus-modal').classList.remove('open');
  syncWsConfig();
}

let ptzToastTimer = null;
function showPtzToast(msg, isOk = true) {
  const el = document.getElementById('ptz-status');
  if (!el) return;
  el.style.display = 'block';
  el.style.background = isOk ? 'rgba(46, 204, 113, 0.15)' : 'rgba(231, 76, 60, 0.15)';
  el.style.color = isOk ? '#2ecc71' : '#e74c3c';
  el.style.border = isOk ? '1px solid rgba(46, 204, 113, 0.4)' : '1px solid rgba(231, 76, 60, 0.4)';
  el.textContent = msg;
  if (ptzToastTimer) clearTimeout(ptzToastTimer);
  ptzToastTimer = setTimeout(() => {
    el.style.display = 'none';
  }, 2500);
}

async function sendPtz(action, extra, quiet) {
  if (!focusUid) return null;
  if (!quiet) showPtzToast('⏳ Đang gửi lệnh ' + action + '...', true);
  try {
    const res = await fetch(`/api/camera/${focusUid}/ptz`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({action}, extra || {}))
    });
    const d = await res.json();
    if (d.ok) {
      if (!quiet) showPtzToast(`✅ Camera đã nhận lệnh "${action}" (${d.connection_mode || 'P2P'})`, true);
    } else {
      showPtzToast(`❌ ${d.error || 'Thao tác thất bại'}`, false);
    }
    return d;
  } catch (err) {
    showPtzToast(`❌ Lỗi mạng: ${err.message}`, false);
    return null;
  }
}

// D-pad: tap = one step, press-and-hold = continuous move until release.
let ptzHold = null;   // {action, timer, holding, started}
function ptzHoldStart(action) {
  ptzHoldEnd();
  const st = {action, holding: false, started: null, timer: null};
  st.timer = setTimeout(() => {
    st.holding = true;
    st.started = sendPtz(action, {hold: true});
  }, 280);
  ptzHold = st;
}
function ptzHoldEnd() {
  const st = ptzHold;
  if (!st) return;
  ptzHold = null;
  clearTimeout(st.timer);
  if (st.holding) {
    // Make sure the stop is sent after the move request has gone out
    Promise.resolve(st.started).then(() => sendPtz('stop', null, true));
  } else {
    sendPtz(st.action);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.dpad [data-ptz]').forEach(btn => {
    btn.addEventListener('pointerdown', ev => {
      ev.preventDefault();
      try { btn.setPointerCapture(ev.pointerId); } catch (e) {}
      ptzHoldStart(btn.dataset.ptz);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(t =>
      btn.addEventListener(t, () => ptzHoldEnd()));
    btn.addEventListener('contextmenu', ev => ev.preventDefault());
  });
});
window.addEventListener('blur', () => ptzHoldEnd());

let camParamsData = null;   // [{cgi, title, values|null}]

function escHtml(v) {
  return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderCamParams() {
  const box = document.getElementById('cam-params');
  if (!box || !camParamsData) return;
  const filter = (document.getElementById('cam-params-filter').value || '').toLowerCase();
  let html = '<table>';
  const seen = new Set();
  for (const sec of camParamsData) {
    const vals = sec.values;
    html += `<tr class="sec"><td colspan="2">${escHtml(sec.title)}</td></tr>`;
    if (vals === null) {
      html += '<tr><td colspan="2">⏳ Camera chưa trả lời mục này (đang bận) — bấm đọc lại</td></tr>';
      continue;
    }
    if (vals.cgi === 'not support') {
      html += '<tr><td colspan="2" style="color:var(--muted);">Firmware không hỗ trợ</td></tr>';
      continue;
    }
    let shown = 0;
    for (const k of Object.keys(vals).sort()) {
      if (seen.has(k) || k === 'result' || k === 'command' || k === 'uid') continue;
      seen.add(k);
      if (filter && !k.toLowerCase().includes(filter) && !String(vals[k]).toLowerCase().includes(filter)) continue;
      html += `<tr><td>${escHtml(k)}</td><td>${escHtml(vals[k])}</td></tr>`;
      shown++;
    }
    if (!shown) html += '<tr><td colspan="2" style="color:var(--muted);">(không có mục khớp)</td></tr>';
  }
  box.innerHTML = html + '</table>';
  box.style.display = 'block';
}

function syncCamControls() {
  if (!camParamsData) return;
  const all = Object.assign({}, ...camParamsData.map(s => s.values || {}));
  const num = k => (all[k] !== undefined && all[k] !== '' && !isNaN(Number(all[k]))) ? Number(all[k]) : null;
  const setv = (id, key) => { const el = document.getElementById(id); const v = num(key); if (el && v !== null) el.value = v; };
  setv('sl-brightness', 'vbright');
  setv('sl-contrast', 'vcontrast');
  setv('sl-saturation', 'vsaturation');
  setv('sl-mic', 'involume');
  setv('sl-speaker', 'outvolume');
  setv('sl-motion', 'alarm_motion_sensitivity');
  setv('sl-ptzspeed', 'ptz_patrol_rate');
  // Capability summary from the camera's own support_*/have* flags
  const caps = [
    ['haveMotor', 'Xoay PTZ'], ['haveMic', 'Micro'], ['haveHorn', 'Loa'], ['haveTf', 'Thẻ nhớ'],
    ['support_humanDetect', 'Phát hiện người'], ['support_humanoidFrame', 'Khung người'],
    ['support_motionArea', 'Vùng chuyển động'], ['support_cryDetect', 'Phát hiện tiếng khóc'],
    ['support_smokeDetect', 'Báo khói'], ['support_departDetect', 'Phát hiện rời đi'],
    ['support_pixel_shift', 'Đổi độ phân giải'], ['support_h264_h265_shift', 'Đổi H.264/H.265'],
  ];
  const el = document.getElementById('cam-caps');
  if (el && all.sys_ver !== undefined) {
    const chips = caps.filter(([k]) => all[k] !== undefined).map(([k, label]) =>
      `<span class="badge ${all[k] === '1' ? 'ok' : ''}" style="font-size:10px; ${all[k] === '1' ? '' : 'opacity:.45; text-decoration:line-through;'}">${label}</span>`).join(' ');
    const state = [];
    if (all.enable !== undefined) state.push('Bám người: ' + (all.enable === '1' ? 'BẬT' : 'tắt'));
    if (all.bHumanoidFrame !== undefined) state.push('Khung người: ' + (all.bHumanoidFrame === '1' ? 'BẬT' : 'tắt'));
    if (all.led_mode !== undefined) state.push('Đèn báo: ' + (all.led_mode === '0' ? 'tắt' : 'BẬT'));
    if (all.flip !== undefined) state.push('Lật: ' + ({'0':'chuẩn','1':'dọc','2':'gương','3':'180°'}[all.flip] || all.flip));
    if (all.MainStreamWidth) state.push(`Luồng chính: ${all.MainStreamWidth}×${all.MainStreamHeight}`);
    el.innerHTML = `<div style="color:var(--muted);">Firmware ${escHtml(all.sys_ver)}</div>`
      + `<div style="display:flex; gap:4px; flex-wrap:wrap; margin-top:4px;">${chips}</div>`
      + (state.length ? `<div style="margin-top:4px;">${escHtml(state.join(' · '))}</div>` : '');
    el.style.display = 'block';
  }
}

async function loadCamParams() {
  if (!focusUid) return;
  const uid = focusUid;
  showPtzToast('⏳ Đang đọc thông số từ camera...', true);
  try {
    const res = await fetch(`/api/camera/${uid}/params`);
    const d = await res.json();
    if (uid !== focusUid) return;
    camParamsData = d.sections || [];
    renderCamParams();
    if (!d.ok) { showPtzToast('❌ ' + (d.error || 'Không đọc được'), false); return; }
    syncCamControls();
    showPtzToast(d.partial
      ? '⚠️ Camera mới trả lời một phần (đang bận) — bấm đọc lại sau ít giây'
      : `✅ Đã đọc thông số (${d.connection_mode || 'P2P'})`, !d.partial);
  } catch (err) {
    showPtzToast('❌ Lỗi mạng: ' + err.message, false);
  }
}

async function sendRawCgi() {
  if (!focusUid) return;
  const cgi = document.getElementById('raw-cgi').value.trim();
  const out = document.getElementById('raw-cgi-out');
  if (!cgi) return;
  out.style.display = 'block';
  out.textContent = '⏳ Đang gửi...';
  try {
    const res = await fetch(`/api/camera/${focusUid}/cgi`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({cgi})
    });
    const d = await res.json();
    if (!d.ok) { out.textContent = '❌ ' + (d.error || 'Thất bại'); return; }
    const keys = Object.keys(d.values || {}).sort();
    out.textContent = keys.length
      ? keys.map(k => `${k} = ${d.values[k]}`).join('\n')
      : '✅ Camera đã nhận lệnh (không có dữ liệu trả về).';
  } catch (err) {
    out.textContent = '❌ Lỗi mạng: ' + err.message;
  }
}

async function triggerMotionManual(uid, action) {
  if (!uid) return;
  await fetch(`/api/camera/${uid}/record`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action})
  });
  pollStatus();
}

async function toggleManualRecord(uid) {
  const cam = camerasData.find(c => c.uid === uid);
  if (!cam) return;
  const action = cam.manual_record ? 'manual_off' : 'manual_on';
  await triggerMotionManual(uid, action);
}

async function loadEvents() {
  const room = document.getElementById('ev-room-filter').value || 'ALL';
  const res = await fetch(`/api/events?room=${encodeURIComponent(room)}`);
  const data = await res.json();
  const events = data.events || [];
  document.getElementById('ev-summary').textContent = `Hiển thị ${events.length} video sự kiện`;

  const grid = document.getElementById('events-grid');
  if (events.length === 0) {
    grid.innerHTML = `<div style="color:var(--muted); padding:20px;">Chưa có video chuyển động nào. Bạn có thể bấm nút "⚡ Ghi 20s" trên ô camera hoặc đi qua trước camera để test!</div>`;
    return;
  }
  grid.innerHTML = events.map(ev => `
    <div class="event-card">
      <div class="event-thumb" onclick="playEvent('${ev.video_rel}', '${ev.room_name} | ${ev.start_time}')">
        ${ev.thumb_rel ? `<img src="/recordings/${ev.thumb_rel}" loading="lazy">` : ''}
        <div class="cam-overlay-top">
          <span class="room-pill">${ev.room_name}</span>
          <span class="badge">${ev.duration_sec}s | ${ev.size_mb} MB</span>
        </div>
      </div>
      <div class="event-meta">
        <div><strong>🕒 ${ev.start_time}</strong> → ${ev.end_time.split(' ')[1] || ''}</div>
        <div style="color:var(--muted);">Trigger: <code>${ev.reason}</code> | ${ev.codec.toUpperCase()} ${ev.fps}fps</div>
        <div style="display:flex; justify-content:space-between; margin-top:6px;">
          <button class="btn primary" onclick="playEvent('${ev.video_rel}', '${ev.room_name} | ${ev.start_time}')">▶ Xem Video</button>
          <a class="btn" href="/recordings/${ev.video_rel}" download style="text-decoration:none;">⬇ Tải về</a>
          <button class="btn danger" onclick="deleteEvent('${ev.video_rel}')">🗑 Xóa</button>
        </div>
      </div>
    </div>
  `).join('');
}

function playEvent(videoRel, title) {
  const v = document.getElementById('play-video');
  document.getElementById('play-title').textContent = title;
  document.getElementById('play-download').href = '/recordings/' + videoRel;
  v.src = '/recordings/' + videoRel;
  v.playbackRate = 1.0;
  document.getElementById('play-modal').classList.add('open');
  v.play().catch(() => {});
}

function setPlaySpeed(rate) {
  const v = document.getElementById('play-video');
  v.playbackRate = rate;
}

function closePlayModal() {
  const v = document.getElementById('play-video');
  v.pause();
  v.src = '';
  document.getElementById('play-modal').classList.remove('open');
}

async function deleteEvent(videoRel) {
  if (!confirm('Xóa đoạn video sự kiện này?')) return;
  await fetch('/api/events', {
    method: 'DELETE',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({video_rel: videoRel})
  });
  loadEvents();
  pollStatus();
}

function renderRoomRenameList() {
  const container = document.getElementById('room-Rename-list');
  if (container.dataset.count === String(camerasData.length)) return;
  container.dataset.count = String(camerasData.length);
  container.innerHTML = camerasData.map(c => `
    <div class="form-row">
      <div>
        <strong>${c.ip}</strong> <span style="color:var(--muted); font-size:11px;">(${c.formatted_did || c.hw_deviceid || c.short_uid})</span>
        <span class="badge ${c.connected ? 'ok' : 'danger'}" style="margin-left:6px; font-size:10px;">${c.connection_mode || (c.connected ? 'LAN' : 'OFFLINE')}</span>
      </div>
      <div style="display:flex; gap:6px; flex-wrap:wrap;">
        <input type="text" id="rname-${c.uid}" value="${c.room_name}" style="width:130px;">
        <button class="btn primary" onclick="renameRoom('${c.uid}')">Đổi tên + OSD</button>
        <button class="btn" onclick="pushWifiToCamera('${c.uid}', '${c.room_name}')" title="Bắn cấu hình Wi-Fi mới từ xa qua mạng Internet/LAN">📡 Đổi Wi-Fi</button>
        <button class="btn" onclick="changeCameraPassword('${c.uid}', '${c.room_name}')" title="Đổi mật khẩu camera">🔑 Mật khẩu</button>
        <button class="btn" onclick="rebootCamera('${c.uid}', '${c.room_name}')" title="Khởi động lại camera từ xa">🔄 Reboot</button>
        <button class="btn danger" onclick="deleteCamera('${c.uid}', '${c.room_name}')" title="Xóa camera này khỏi Hub">🗑️</button>
      </div>
    </div>
  `).join('');
}

async function renameRoom(uid) {
  const val = document.getElementById('rname-' + uid).value;
  await fetch(`/api/camera/${uid}/rename`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({room_name: val})
  });
  document.getElementById('room-Rename-list').dataset.count = '';
  pollStatus();
}

async function saveSettings() {
  const payload = {
    pre_roll_sec: parseFloat(document.getElementById('set-preroll').value),
    post_roll_sec: parseFloat(document.getElementById('set-postroll').value),
    pframe_sensitivity: parseFloat(document.getElementById('set-sens').value),
    enable_pframe_trigger: document.getElementById('set-pframe-en').checked,
    max_storage_gb: parseFloat(document.getElementById('set-maxgb').value),
    min_free_gb: parseFloat(document.getElementById('set-freegb').value),
  };
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  alert('Đã lưu cấu hình thành công!');
}


// =========================================================================
// QR Image Upload, Clipboard Paste & Camera Registration
// =========================================================================

let wifiPollTimer = null;
let onboardFastPollTimer = null;
let lastAddedUid = null;
let hostCurrentSsid = '';
let hostCurrentBssid = '';
let currentModalTab = 'link';

function switchModalTab(mode) {
  currentModalTab = mode;
  stopSonicWave();
  const btnLink = document.getElementById('modal-tab-link');
  const btnUnbox = document.getElementById('modal-tab-unbox');
  const descLink = document.getElementById('modal-desc-link');
  const descUnbox = document.getElementById('modal-desc-unbox');
  const step2 = document.getElementById('modal-unbox-step-2');
  const actLink = document.getElementById('modal-action-link');
  const bottomLink = document.getElementById('modal-bottom-link-btn');
  const bottomUnbox = document.getElementById('modal-bottom-unbox-btn');
  const pBox = document.getElementById('onboard-progress-box');
  if (pBox) pBox.style.display = 'none';

  if (mode === 'link') {
    if (btnLink) {
      btnLink.style.color = '#58a6ff';
      btnLink.style.borderBottom = '3px solid #1f6feb';
    }
    if (btnUnbox) {
      btnUnbox.style.color = 'var(--muted)';
      btnUnbox.style.borderBottom = '3px solid transparent';
    }
    if (descLink) descLink.style.display = 'block';
    if (descUnbox) descUnbox.style.display = 'none';
    if (step2) step2.style.display = 'none';
    if (actLink) actLink.style.display = 'flex';
    if (bottomLink) bottomLink.style.display = 'inline-block';
    if (bottomUnbox) bottomUnbox.style.display = 'none';
  } else {
    if (btnLink) {
      btnLink.style.color = 'var(--muted)';
      btnLink.style.borderBottom = '3px solid transparent';
    }
    if (btnUnbox) {
      btnUnbox.style.color = '#3fb950';
      btnUnbox.style.borderBottom = '3px solid #238636';
    }
    if (descLink) descLink.style.display = 'none';
    if (descUnbox) descUnbox.style.display = 'block';
    if (step2) step2.style.display = 'block';
    if (actLink) actLink.style.display = 'none';
    if (bottomLink) bottomLink.style.display = 'none';
    if (bottomUnbox) bottomUnbox.style.display = 'inline-block';
    loadHostWifiInfo(false);
  }
}

function openQrModal(prefillDid = '', prefillRoom = '', defaultTab = 'link') {
  document.getElementById('qr-modal').classList.add('open');
  const nextIdx = (camerasData ? camerasData.length : 1) + 1;
  const roomInp = document.getElementById('qr-room-input');
  if (prefillDid) document.getElementById('qr-did-input').value = prefillDid;
  if (prefillRoom) {
    roomInp.value = prefillRoom;
  } else if (!roomInp.value) {
    roomInp.value = 'Phong_' + (100 + nextIdx);
  }
  switchModalTab(defaultTab);
  startWifiWaitPolling();
}

function closeQrModal() {
  stopSonicWave();
  document.getElementById('qr-modal').classList.remove('open');
  if (wifiPollTimer) {
    clearInterval(wifiPollTimer);
    wifiPollTimer = null;
  }
  if (onboardFastPollTimer) {
    clearInterval(onboardFastPollTimer);
    onboardFastPollTimer = null;
  }
}

function onWifiSsidInput() {
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const bssidInp = document.getElementById('wifi-bssid-input');
  if (hostCurrentSsid && ssid !== hostCurrentSsid && bssidInp.value === hostCurrentBssid) {
    bssidInp.value = '';
  } else if (hostCurrentSsid && ssid === hostCurrentSsid && hostCurrentBssid) {
    bssidInp.value = hostCurrentBssid;
  }
  refreshWifiQrPreview();
}

function toggleOpticalWifiQrModal() {
  const box = document.getElementById('optical-wifi-qr-box');
  const img = document.getElementById('optical-wifi-qr-img');
  if (box.style.display === 'none') {
    const ssid = document.getElementById('wifi-ssid-input').value.trim();
    const pwd = document.getElementById('wifi-pwd-input').value;
    const bssid = document.getElementById('wifi-bssid-input').value.trim();
    if (!ssid) {
      alert('Vui lòng nhập Tên Wi-Fi (2.4GHz) trước khi hiện mã QR quang học!');
      return;
    }
    box.style.display = 'block';
    fetch('/api/wifi/qr', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ssid, password: pwd, bssid, format: 'eye4' })
    }).then(r => r.json()).then(d => {
      if (d.ok && d.data_url) img.src = d.data_url;
    }).catch(() => {});
  } else {
    box.style.display = 'none';
  }
}

async function startOneClickOnboarding(isUnbox = null) {
  if (isUnbox === null) {
    isUnbox = (currentModalTab === 'unbox');
  }
  const did = document.getElementById('qr-did-input').value.trim();
  const room = document.getElementById('qr-room-input').value.trim();
  const camPwd = ((document.getElementById('qr-pwd-input') || {}).value || '888888').trim() || '888888';
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const wifiPwd = document.getElementById('wifi-pwd-input').value;
  const wifiBssid = document.getElementById('wifi-bssid-input').value.trim();

  if (!did) {
    alert('Vui lòng chụp/tải ảnh tem QR hoặc nhập Mã UID trên thân Camera (VD: VSTJ082299RLSXK)!');
    document.getElementById('qr-did-input').focus();
    return;
  }
  if (isUnbox && !ssid) {
    alert('Vui lòng nhập Tên mạng Wi-Fi (2.4GHz) tại nơi đặt Camera!');
    document.getElementById('wifi-ssid-input').focus();
    return;
  }

  const btnLink = document.getElementById('btn-modal-link-only');
  const btnUnbox = document.getElementById('btn-oneclick-onboard');
  const btmLink = document.getElementById('modal-bottom-link-btn');
  const btmUnbox = document.getElementById('modal-bottom-unbox-btn');
  if (btnLink) btnLink.disabled = true;
  if (btnUnbox) btnUnbox.disabled = true;
  if (btmLink) btmLink.disabled = true;
  if (btmUnbox) btmUnbox.disabled = true;

  if (isUnbox) {
    if (btnUnbox) btnUnbox.textContent = '⏳ Đang đăng ký UID & Tạo sóng âm nạp Wi-Fi...';
  } else {
    if (btnLink) btnLink.textContent = '⏳ Đang kích hoạt kết nối WAN P2P & LAN...';
  }

  const pBox = document.getElementById('onboard-progress-box');
  const pTitle = document.getElementById('onboard-progress-title');
  const s1 = document.getElementById('onboard-step-1');
  const s2 = document.getElementById('onboard-step-2');
  const s3 = document.getElementById('onboard-step-3');
  const fBanner = document.getElementById('onboard-final-banner');
  const cBadge = document.getElementById('onboard-cloud-badge');

  pBox.style.display = 'block';
  fBanner.style.display = 'none';
  if (pTitle) {
    pTitle.textContent = isUnbox
      ? '🚀 Tiến trình Khui Hộp & Nạp Wi-Fi (Fast-Poll 1.2s)'
      : '🚀 Tiến trình Liên Kết WAN P2P & LAN (Fast-Poll 1.2s)';
  }
  s1.style.cssText = 'padding:8px 11px; border-radius:6px; background:rgba(47,129,247,0.16); border:1px solid #2f81f7; color:#58a6ff;';
  if (isUnbox) {
    s1.innerHTML = `<b>Bước 1:</b> 🔊 Đang phát sóng âm nạp Wi-Fi <b>"${ssid}"</b> từ điện thoại/loa sang Camera...`;
  } else {
    s1.innerHTML = `<b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P / LAN (180s fast-poll)...`;
  }
  s2.style.cssText = 'padding:8px 11px; border-radius:6px; background:#0b0f17; border:1px solid var(--border); color:var(--muted);';
  s2.innerHTML = '<b>Bước 2:</b> 🌐 Chờ Camera vào mạng &amp; báo danh lên P2P Cloud (<code>WAITING_FOR_WIFI</code> → <code>ONLINE_PUNCHING</code>)...';
  s3.style.cssText = 'padding:8px 11px; border-radius:6px; background:#0b0f17; border:1px solid var(--border); color:var(--muted);';
  s3.innerHTML = '<b>Bước 3:</b> 🔗 Server đục lỗ UDP &amp; xác thực (<code>CONNECTED</code>)...';

  try {
    const res = await fetch('/api/camera/onboard', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        uid: did,
        room_name: room,
        cam_password: camPwd,
        wifi_ssid: isUnbox ? ssid : '',
        wifi_password: isUnbox ? wifiPwd : '',
        wifi_bssid: isUnbox ? wifiBssid : '',
        sonic_mode: isUnbox ? (wifiBssid ? 'mac' : 'ssid') : 'ssid'
      })
    });
    const data = await res.json();
    if (!data.ok) {
      alert('Lỗi: ' + data.error);
      return;
    }

    lastAddedUid = data.uid;
    document.getElementById('room-Rename-list').dataset.count = '';
    if (cBadge) cBadge.textContent = data.wan_cloud_status || 'WAITING_FOR_WIFI';
    let autoplayBlocked = false;

    if (isUnbox && data.qr_data_url) {
      document.getElementById('optical-wifi-qr-img').src = data.qr_data_url;
    }

    if (isUnbox && data.wav_data_url) {
      const player = document.getElementById('sonic-audio-player');
      player.src = data.wav_data_url;
      player.currentTime = 0;
      try {
        await player.play();
      } catch (playErr) {
        console.warn('Autoplay blocked:', playErr);
        autoplayBlocked = true;
        const qrBox = document.getElementById('wifi-qr-box');
        if (qrBox) qrBox.style.display = 'grid';
      }
    }

    const banner = document.getElementById('wifi-Wait-banner');
    if (banner) {
      banner.style.borderColor = '#3fb950';
      banner.style.color = '#3fb950';
      if (isUnbox && autoplayBlocked) {
        banner.style.borderColor = '#d29922';
        banner.style.color = '#f0883e';
        banner.innerHTML =
          `⚠️ <b>Trình duyệt đã chặn tự phát âm thanh.</b> Hãy bấm nút ▶ trên thanh phát âm bên dưới để bắt đầu phát sóng âm (chế độ: <b>${data.mode || ''}</b>).`;
      } else if (isUnbox) {
        banner.innerHTML =
          `🔊 <b>Đang phát Sóng Âm Nạp Wi-Fi "${ssid}" cho Camera <code>${data.formatted_did}</code> (${data.room_name})!</b> (chế độ: <b>${data.mode || ''}</b>)<br>` +
          `👉 Hãy bật <b>Max Volume (80–100%)</b> và đặt loa Điện thoại sát lỗ Mic của Camera (2–5 cm). Sóng âm sẽ <b>tự động tắt</b> ngay khi Server bắt tay xong!`;
      } else {
        banner.innerHTML =
          `🌐 <b>Đã đăng ký Camera <code>${data.formatted_did}</code> (${data.room_name})!</b><br>` +
          `Server đang tích cực đục lỗ UDP qua WAN P2P &amp; quét LAN mỗi 1.2s...`;
      }
    }

    await pollStatus();
    if (onboardFastPollTimer) clearInterval(onboardFastPollTimer);
    onboardFastPollTimer = setInterval(pollOnboardingProgress, 1200);
  } catch (e) {
    alert('Lỗi kết nối tới Server: ' + e);
  } finally {
    if (btnLink) {
      btnLink.disabled = false;
      btnLink.textContent = '🔗 Liên Kết Server Ngay (Không Cần Nạp Wi-Fi)';
    }
    if (btnUnbox) {
      btnUnbox.disabled = false;
      btnUnbox.textContent = '🔊 Nạp Wi-Fi & Tự Động Liên Kết Server';
    }
    if (btmLink) btmLink.disabled = false;
    if (btmUnbox) btmUnbox.disabled = false;
  }
}

async function pollOnboardingProgress() {
  if (!lastAddedUid) return;
  await pollStatus();
  const cam = camerasData.find(c => c.uid === lastAddedUid);
  if (!cam) return;

  const s1 = document.getElementById('onboard-step-1');
  const s2 = document.getElementById('onboard-step-2');
  const s3 = document.getElementById('onboard-step-3');
  const fBanner = document.getElementById('onboard-final-banner');
  const cBadge = document.getElementById('onboard-cloud-badge');
  const cloudSt = cam.wan_cloud_status || 'WAITING_FOR_WIFI';
  if (cBadge) cBadge.textContent = cloudSt;

  const doneStyle = 'padding:8px 11px; border-radius:6px; background:rgba(63,185,80,0.16); border:1px solid #3fb950; color:#3fb950;';
  const activeStyle = 'padding:8px 11px; border-radius:6px; background:rgba(47,129,247,0.16); border:1px solid #2f81f7; color:#58a6ff;';
  const warnStyle = 'padding:8px 11px; border-radius:6px; background:rgba(210,153,34,0.16); border:1px solid #d29922; color:#f0883e;';
  const pendingStyle = 'padding:8px 11px; border-radius:6px; background:#0b0f17; border:1px solid var(--border); color:var(--muted);';

  // 1. Fully connected
  if (cam.state === 'CONNECTED' && !cam.auth_failed) {
    stopSonicWave();
    if (onboardFastPollTimer) {
      clearInterval(onboardFastPollTimer);
      onboardFastPollTimer = null;
    }
    if (cBadge) {
      cBadge.textContent = 'CONNECTED';
      cBadge.style.color = '#3fb950';
      cBadge.style.borderColor = '#3fb950';
    }
    s1.style.cssText = doneStyle;
    s1.innerHTML = (currentModalTab === 'unbox')
      ? '<b>Bước 1:</b> ✅ Đã nạp Wi-Fi từ điện thoại sang Camera (Đã tự động ngắt sóng âm!)'
      : '<b>Bước 1:</b> ✅ Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P / LAN';
    s2.style.cssText = doneStyle;
    s2.innerHTML = '<b>Bước 2:</b> ✅ Camera đã vào mạng &amp; báo danh lên P2P Cloud (<code>CONNECTED</code>)';
    s3.style.cssText = doneStyle;
    s3.innerHTML = `<b>Bước 3:</b> ✅ Server đã đục lỗ UDP (<code>${cam.connection_mode}</code>) &amp; xác thực thành công!`;

    fBanner.style.display = 'block';
    fBanner.innerHTML =
      `✅ Liên kết hoàn tất! Camera <b>${cam.room_name} (${cam.formatted_did})</b> đã kết nối trực tiếp với Server qua Internet. Bạn có thể xem Live stream ngay!`;

    const wBanner = document.getElementById('wifi-Wait-banner');
    if (wBanner) {
      wBanner.style.background = 'rgba(35,134,54,0.25)';
      wBanner.style.borderColor = '#3fb950';
      wBanner.style.color = '#3fb950';
      wBanner.innerHTML = `✅ Liên kết hoàn tất! Camera đã kết nối trực tiếp với Server qua Internet.`;
    }
    return;
  }

  // 2. Auth failed
  if (cam.auth_failed) {
    stopSonicWave();
    s1.style.cssText = doneStyle;
    s2.style.cssText = doneStyle;
    s3.style.cssText = warnStyle;
    s3.innerHTML = '<b>Bước 3:</b> ⚠️ Đã đục lỗ UDP tới Camera nhưng sai Mật khẩu Camera (hãy nhập đúng mật khẩu Camera vào ô Mật khẩu rồi bấm lại)!';
    return;
  }

  // 3. Camera is ONLINE on P2P Cloud -> Hole punching actively in progress!
  if (cloudSt === 'ONLINE_PUNCHING') {
    s1.style.cssText = doneStyle;
    s1.innerHTML = (currentModalTab === 'unbox')
      ? '<b>Bước 1:</b> ✅ Camera đã nhận sóng âm Wi-Fi'
      : '<b>Bước 1:</b> ✅ Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P';
    s2.style.cssText = doneStyle;
    s2.innerHTML = `<b>Bước 2:</b> 🟢 Camera đã ONLINE trên P2P Cloud (Đã bắt được IP &amp; Port công khai)!`;
    s3.style.cssText = activeStyle;
    s3.innerHTML = `<b>Bước 3:</b> 🔄 Server đang đục lỗ UDP &amp; bắt tay trực tiếp với Camera (<code>${cam.state}</code>)...`;
    return;
  }

  // 4. Camera is OFFLINE on Cloud (WAITING_FOR_WIFI or IDLE)
  if (cloudSt === 'WAITING_FOR_WIFI' || cloudSt === 'IDLE') {
    if (currentModalTab === 'unbox') {
      s1.style.cssText = activeStyle;
      s1.innerHTML = '<b>Bước 1:</b> 🔊 Đang phát sóng âm nạp Wi-Fi từ điện thoại/loa sang Camera...';
      s2.style.cssText = warnStyle;
      s2.innerHTML = '<b>Bước 2:</b> ⏳ Đang phát sóng âm... Chờ Camera nhận sóng âm và online trên P2P Cloud (<code>WAITING_FOR_WIFI</code>)...';
    } else {
      s1.style.cssText = doneStyle;
      s1.innerHTML = '<b>Bước 1:</b> 📋 Đã lưu UID &amp; kích hoạt tìm kiếm WAN P2P / LAN';
      s2.style.cssText = warnStyle;
      s2.innerHTML = '<b>Bước 2:</b> ⏳ Chờ Camera kết nối Wi-Fi &amp; báo danh lên P2P Cloud (Hiện Camera đang <b>OFFLINE</b> — Hãy chắc chắn Camera đã cắm nguồn và có mạng Wi-Fi/LAN, hoặc chuyển sang tab <b>Khui Hộp Mới</b> để nạp Wi-Fi!)';
    }
    s3.style.cssText = pendingStyle;
    s3.innerHTML = '<b>Bước 3:</b> ⏸ Chờ Bước 2 (Camera phải Online trên P2P Cloud trước khi Server có thể đục lỗ UDP)...';
    return;
  }

  // 5. Invalid UID
  if (cloudSt === 'INVALID_UID') {
    stopSonicWave();
    s2.style.cssText = warnStyle;
    s2.innerHTML = '<b>Bước 2:</b> ❌ Mã UID không hợp lệ hoặc không tồn tại trên hệ thống P2P Cloud!';
    s3.style.cssText = pendingStyle;
    s3.innerHTML = '<b>Bước 3:</b> ⏸ Đã dừng kết nối.';
    return;
  }

  // 6. Fallback
  s2.style.cssText = activeStyle;
  s2.innerHTML = `<b>Bước 2:</b> 🌐 Chờ Camera báo danh lên P2P Cloud (Trạng thái: <code>${cloudSt}</code>)...`;
  s3.style.cssText = pendingStyle;
  s3.innerHTML = '<b>Bước 3:</b> ⏸ Chờ Bước 2...';
}

function startWifiWaitPolling() {
  if (wifiPollTimer) clearInterval(wifiPollTimer);
  wifiPollTimer = setInterval(async () => {
    if (!document.getElementById('qr-modal').classList.contains('open')) return;
    await fetch('/api/camera/scan', { method: 'POST' });
    await pollStatus();
    if (lastAddedUid) {
      const found = camerasData.find(c => c.uid === lastAddedUid && c.state === 'CONNECTED' && !c.auth_failed);
      if (found) {
        stopSonicWave();
        const banner = document.getElementById('wifi-Wait-banner');
        if (banner) {
          banner.style.background = 'rgba(35,134,54,0.25)';
          banner.style.borderColor = '#3fb950';
          banner.style.color = '#3fb950';
          banner.innerHTML = `🎉 <b>Thành công!</b> Camera <b>${found.room_name} (${found.ip})</b> đã kết nối Wi-Fi và đang phát <b>Live 25 FPS</b> trên Grid!`;
        }
      }
    }
  }, 4000);
}

async function loadHostWifiInfo(forceOverwrite = false) {
  const statusEl = document.getElementById('wifi-scan-status');
  statusEl.textContent = '⏳ Đang kiểm tra gợi ý Wi-Fi...';
  try {
    const res = await fetch('/api/wifi/info');
    const data = await res.json();
    if (!data.ok) return;

    hostCurrentSsid = data.current_ssid || '';
    hostCurrentBssid = data.current_bssid || '';

    const ssidInp = document.getElementById('wifi-ssid-input');
    const pwdInp = document.getElementById('wifi-pwd-input');
    const bssidInp = document.getElementById('wifi-bssid-input');
    const dlist = document.getElementById('wifi-ssid-list');

    if (data.networks && data.networks.length) {
      dlist.innerHTML = data.networks.map(n =>
        `<option value="${n.ssid}">${n.ssid} (${n.band || 'Wi-Fi'} - ${n.signal || ''})</option>`
      ).join('');
    }

    if ((forceOverwrite || !ssidInp.value) && data.current_ssid) {
      ssidInp.value = data.current_ssid;
    }
    if (ssidInp.value === data.current_ssid && data.current_bssid) {
      bssidInp.value = data.current_bssid;
    } else if (hostCurrentBssid && bssidInp.value === hostCurrentBssid) {
      // Only drop a value we auto-filled ourselves; never wipe a MAC the user typed.
      bssidInp.value = '';
    }
    if ((forceOverwrite || !pwdInp.value) && data.saved_password && ssidInp.value === data.current_ssid) {
      pwdInp.value = data.saved_password;
    }

    let msg = data.current_ssid
      ? `✅ Gợi ý Wi-Fi: <b>${data.current_ssid}</b> (Nếu bạn đang mở trên Điện thoại ở nơi khác, hãy sửa thành <b>Tên Wi-Fi 2.4GHz</b> tại nơi lắp Camera).`
      : 'ℹ️ Hãy nhập Tên mạng Wi-Fi (2.4GHz) và mật khẩu Wi-Fi tại nơi đặt Camera.';
    if (data.camera_hotspots && data.camera_hotspots.length > 0) {
      msg += `<br>📡 <b>Phát hiện Wi-Fi nội bộ của Camera gần đây:</b> <code>${data.camera_hotspots.join(', ')}</code>`;
    }
    statusEl.innerHTML = msg;
    if (ssidInp.value) {
      refreshWifiQrPreview(false);
    }
  } catch (e) {
    statusEl.textContent = 'Hãy nhập Tên mạng Wi-Fi (2.4GHz) và mật khẩu Wi-Fi tại nơi đặt Camera.';
  }
}

function toggleWifiPwdVis() {
  const inp = document.getElementById('wifi-pwd-input');
  inp.type = (inp.type === 'password') ? 'text' : 'password';
}

// Eye4 only ever plays the MAC variant, so pick it as soon as a usable MAC is typed.
function syncSonicModeFromBssid() {
  const hex = document.getElementById('wifi-bssid-input').value.replace(/[^0-9a-fA-F]/g, '');
  const sel = document.getElementById('wifi-qr-fmt');
  if (sel) sel.value = hex.length >= 4 ? 'mac' : 'ssid';
}

let qrDebounce = null;
function refreshWifiQrPreview(forceShow = false) {
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const pwd = document.getElementById('wifi-pwd-input').value;
  const bssid = document.getElementById('wifi-bssid-input').value.trim();
  const mode = document.getElementById('wifi-qr-fmt').value || 'combo';
  const box = document.getElementById('wifi-qr-box');
  if (forceShow) box.style.display = 'grid';
  if (!ssid) return;

  if (qrDebounce) clearTimeout(qrDebounce);
  qrDebounce = setTimeout(async () => {
    try {
      const res = await fetch('/api/wifi/sonic', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ ssid, password: pwd, bssid, mode })
      });
      const data = await res.json();
      if (data.ok) {
        if (data.mobile_qr_data_url) {
          document.getElementById('wifi-qr-img').src = data.mobile_qr_data_url;
        }
        document.getElementById('wifi-target-label').textContent = ssid;
        const bBadge = document.getElementById('sonic-bssid-badge');
        if (bBadge) bBadge.textContent = data.bssid_suffix || 'SSID';
        const mLink = document.getElementById('sonic-mobile-link');
        if (mLink && data.mobile_url) {
          mLink.href = data.mobile_url;
          mLink.textContent = data.mobile_url;
        }
        const player = document.getElementById('sonic-audio-player');
        if (player && player.paused) {
          player.src = data.wav_data_url;
        }
        box.style.display = 'grid';
      }
    } catch (e) {}
  }, 180);
}

async function playSonicWaveBrowser() {
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const pwd = document.getElementById('wifi-pwd-input').value;
  const bssid = document.getElementById('wifi-bssid-input').value.trim();
  const mode = document.getElementById('wifi-qr-fmt').value || 'combo';
  if (!ssid) {
    alert('Vui lòng nhập Tên mạng Wi-Fi (SSID)!');
    return;
  }
  const banner = document.getElementById('wifi-Wait-banner');
  banner.style.borderColor = '#3fb950';
  banner.style.color = '#3fb950';
  banner.innerHTML = `⏳ Đang tạo sóng âm VStarcam Sonic Wave (6.5–10.1 kHz) cho mạng <b>${ssid}</b>...`;
  try {
    const res = await fetch('/api/wifi/sonic', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ssid, password: pwd, bssid, mode })
    });
    const data = await res.json();
    if (!data.ok) {
      banner.innerHTML = `❌ Lỗi: ${data.error}`;
      return;
    }
    const player = document.getElementById('sonic-audio-player');
    player.src = data.wav_data_url;
    player.currentTime = 0;
    await player.play();
    banner.innerHTML =
      `🔊 <b>Đang phát Sóng Âm Nạp Wi-Fi "${ssid}" (BSSID 2.4GHz: <code>${data.bssid_24 || 'SSID'}</code>)!</b><br>` +
      `👉 Hãy vặn <b>Max Volume (80–100%)</b> và đặt loa sát lỗ Mic của Camera (cách 2–10 cm) cho đến khi nghe tiếng Camera báo <i>"Wi-Fi connecting"</i>!`;
  } catch (e) {
    banner.innerHTML = `❌ Không thể phát âm thanh trên trình duyệt: ${e}`;
  }
}

async function playSonicWavePcSpeaker() {
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const pwd = document.getElementById('wifi-pwd-input').value;
  const bssid = document.getElementById('wifi-bssid-input').value.trim();
  const mode = document.getElementById('wifi-qr-fmt').value || 'combo';
  if (!ssid) {
    alert('Vui lòng nhập Tên mạng Wi-Fi (SSID)!');
    return;
  }
  // Stop browser player first so they don't overlap
  stopSonicWave();
  const banner = document.getElementById('wifi-Wait-banner');
  banner.style.borderColor = '#58a6ff';
  banner.style.color = '#58a6ff';
  banner.innerHTML = `⏳ Đang phát sóng âm trực tiếp ra cổng âm thanh Windows PC...`;
  try {
    const res = await fetch('/api/wifi/play-pc-speaker', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ssid, password: pwd, bssid, mode })
    });
    const data = await res.json();
    if (data.ok) {
      banner.innerHTML = `🖥️ <b>${data.message}</b> Đặt Camera lại gần loa máy tính (cách 2–10 cm)!`;
    } else {
      banner.innerHTML = `❌ ${data.error}`;
    }
  } catch (e) {
    banner.innerHTML = `❌ Lỗi: ${e}`;
  }
}

function stopSonicWave() {
  const player = document.getElementById('sonic-audio-player');
  if (player) {
    player.pause();
    player.currentTime = 0;
  }
}

async function pushWifiDirectCgi() {
  const ssid = document.getElementById('wifi-ssid-input').value.trim();
  const pwd = document.getElementById('wifi-pwd-input').value;
  if (!ssid) {
    alert('Vui lòng nhập Tên mạng Wi-Fi (SSID)!');
    return;
  }
  const banner = document.getElementById('wifi-Wait-banner');
  banner.innerHTML = `⏳ Đang gửi lệnh <code>set_wifi.cgi</code> nạp mạng <b>${ssid}</b> tới camera...`;
  try {
    const res = await fetch('/api/camera/provision-wifi', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ssid, password: pwd, uid: lastAddedUid || '' })
    });
    const data = await res.json();
    if (data.ok) {
      banner.style.borderColor = '#3fb950';
      banner.style.color = '#3fb950';
      banner.innerHTML = `✅ ${data.message}`;
    } else {
      banner.innerHTML = `ℹ️ ${data.error}`;
    }
  } catch (e) {
    banner.innerHTML = `❌ Lỗi gửi lệnh Wi-Fi: ${e}`;
  }
}

function handleQrDrop(ev) {
  ev.preventDefault();
  document.getElementById('qr-dropzone').style.borderColor = 'var(--border)';
  if (ev.dataTransfer.files && ev.dataTransfer.files[0]) {
    handleQrFileSelect(ev.dataTransfer.files[0]);
  }
}

window.addEventListener('paste', (ev) => {
  if (!document.getElementById('qr-modal').classList.contains('open')) return;
  const items = (ev.clipboardData || ev.originalEvent.clipboardData).items;
  for (const item of items) {
    if (item.type.indexOf('image') === 0) {
      const file = item.getAsFile();
      if (file) handleQrFileSelect(file);
      break;
    }
  }
});

function downscaleImageFile(file, maxDim = 1600, quality = 0.85) {
  return new Promise((resolve) => {
    if (!file || !file.type || !file.type.startsWith('image/')) {
      return resolve(file);
    }
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      let w = img.width;
      let h = img.height;
      if (w <= maxDim && h <= maxDim) {
        return resolve(file);
      }
      if (w > h) {
        h = Math.round((h * maxDim) / w);
        w = maxDim;
      } else {
        w = Math.round((w * maxDim) / h);
        h = maxDim;
      }
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, w, h);
      canvas.toBlob((blob) => {
        if (blob) {
          resolve(blob);
        } else {
          resolve(file);
        }
      }, 'image/jpeg', quality);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve(file);
    };
    img.src = url;
  });
}

async function handleQrFileSelect(file) {
  if (!file) return;
  const previewWrap = document.getElementById('qr-preview-wrap');
  const previewImg = document.getElementById('qr-preview-img');
  previewImg.src = URL.createObjectURL(file);
  previewWrap.style.display = 'block';

  const resBox = document.getElementById('qr-decode-result');
  resBox.style.display = 'block';
  resBox.style.borderColor = 'var(--border)';
  resBox.innerHTML = '⏳ Đang tối ưu dung lượng ảnh & giải mã tem QR...';

  try {
    const uploadBlob = await downscaleImageFile(file, 1600, 0.85);
    const fd = new FormData();
    fd.append('file', uploadBlob, 'qr.jpg');

    const res = await fetch('/api/camera/decode-qr', { method: 'POST', body: fd });
    if (res.status === 413) {
      resBox.style.background = 'rgba(218,54,51,0.15)';
      resBox.style.borderColor = 'var(--danger)';
      resBox.innerHTML = '❌ <b>Ảnh quá lớn (413 Request Entity Too Large). Vui lòng thử lại với ảnh nhỏ hơn hoặc nhập mã UID.</b>';
      return;
    }
    const data = await res.json();
    if (!res.ok || !data.ok) {
      resBox.style.background = 'rgba(218,54,51,0.15)';
      resBox.style.borderColor = 'var(--danger)';
      resBox.innerHTML = `❌ <b>${data.error || 'Lỗi giải mã ảnh QR'}</b>`;
      return;
    }
    document.getElementById('qr-did-input').value = data.formatted_did;
    document.getElementById('qr-model-input').value = data.model || 'CS22S';
    if (data.suggested_name) {
      document.getElementById('qr-room-input').value = data.suggested_name;
    }
    lastAddedUid = data.uid_hex;
    resBox.style.background = 'rgba(35,134,54,0.15)';
    resBox.style.borderColor = 'var(--accent)';
    resBox.innerHTML =
      `✅ <b>Giải mã QR thành công!</b> ` +
      `Mã UID: <code>${data.formatted_did}</code> | Model: <code>${data.model}</code> | ` +
      `Mã PPPP LAN: <code style="font-size:11px;">${data.uid_hex}</code>`;
    refreshWifiQrPreview(true);
  } catch (e) {
    resBox.style.background = 'rgba(218,54,51,0.15)';
    resBox.style.borderColor = 'var(--danger)';
    resBox.innerHTML = `❌ Lỗi kết nối khi giải mã QR: ${e}`;
  }
}

async function submitAddCameraByQr() {
  const did = document.getElementById('qr-did-input').value.trim();
  const room = document.getElementById('qr-room-input').value.trim();
  const pwd = (document.getElementById('qr-pwd-input') || {}).value || '888888';
  const model = document.getElementById('qr-model-input').value.trim();
  const ip = document.getElementById('qr-ip-input').value.trim();
  if (!did) {
    alert('Vui lòng tải ảnh chụp tem QR hoặc nhập mã UID trên thân camera (VD: VSTJ082299RLSXK)!');
    return;
  }
  const btn = document.getElementById('btn-submit-qr');
  btn.disabled = true;
  btn.textContent = '⏳ Đang lưu & kết nối WAN P2P / LAN...';
  try {
    const res = await fetch('/api/camera/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        device_id: did,
        uid: did,
        room_name: room,
        name: room,
        password: pwd.trim(),
        model: model,
        ip: ip
      })
    });
    const data = await res.json();
    if (!data.ok) {
      alert('Lỗi: ' + data.error);
      return;
    }
    lastAddedUid = data.uid;
    document.getElementById('room-Rename-list').dataset.count = '';
    await pollStatus();
    refreshWifiQrPreview(true);
    const banner = document.getElementById('wifi-Wait-banner');
    if (banner) {
      banner.innerHTML = `✅ <b>Đã thêm ${data.room_name} (${data.formatted_did}) và bắt đầu kết nối WAN P2P (Internet) &amp; LAN!</b> Nếu camera chưa có Wi-Fi, bạn có thể phát sóng âm bên trên để nạp Wi-Fi.`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '➕ Lưu Camera vào Hub & Kết nối WAN P2P / Nạp Wi-Fi';
  }
}

async function scanLanNow() {
  const btn = document.getElementById('btn-scan-lan');
  btn.disabled = true;
  btn.textContent = '⏳ Đang quét 254 IP...';
  try {
    const res = await fetch('/api/camera/scan', { method: 'POST' });
    const data = await res.json();
    await pollStatus();
    alert(`Đã quét toàn bộ dải mạng LAN! Phát hiện mới: ${data.found_new} camera (Đang kết nối: ${data.active_count} camera).`);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Quét LAN';
  }
}

async function pushWifiToCamera(uid, name) {
  const ssid = prompt(`Tên Wi-Fi MỚI cho "${name}":`, (document.getElementById('wifi-ssid-input') || {}).value || '');
  if (!ssid) return;
  const pwd = prompt(`Mật khẩu Wi-Fi "${ssid}":`, (document.getElementById('wifi-pwd-input') || {}).value || '');
  if (pwd === null) return;
  const res = await fetch('/api/camera/provision-wifi', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ ssid, password: pwd, uid })
  });
  const d = await res.json();
  alert(d.ok ? d.message : ('Lỗi: ' + d.error));
}

async function rebootCamera(uid, name) {
  if (!confirm(`Khởi động lại camera "${name}" (${(uid||'').substring(0,8)}) từ xa qua mạng?`)) return;
  showPtzToast('⏳ Đang gửi lệnh khởi động lại...', true);
  try {
    const res = await fetch(`/api/camera/${uid}/reboot`, { method: 'POST' });
    const d = await res.json();
    showPtzToast(d.ok ? ('✅ ' + d.message) : ('❌ ' + d.error), d.ok);
    alert(d.ok ? d.message : ('Lỗi: ' + d.error));
  } catch (err) {
    showPtzToast('❌ Lỗi kết nối: ' + err.message, false);
    alert('Lỗi kết nối: ' + err.message);
  }
}

async function changeCameraPassword(uid, name) {
  const newPwd = prompt(`Nhập mật khẩu mới cho camera "${name}":`);
  if (!newPwd || !newPwd.trim()) return;
  showPtzToast('⏳ Đang cập nhật mật khẩu camera...', true);
  try {
    const res = await fetch(`/api/camera/${uid}/password`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ password: newPwd.trim() })
    });
    const d = await res.json();
    showPtzToast(d.ok ? ('✅ ' + d.message) : ('❌ ' + d.error), d.ok);
    alert(d.ok ? d.message : ('Lỗi: ' + d.error));
  } catch (err) {
    showPtzToast('❌ Lỗi kết nối: ' + err.message, false);
    alert('Lỗi kết nối: ' + err.message);
  }
}

async function deleteCamera(uid, name) {
  if (!confirm(`Bạn có chắc muốn xóa camera "${name}" khỏi danh sách Hub?`)) return;
  await fetch(`/api/camera/${uid}`, { method: 'DELETE' });
  document.getElementById('room-Rename-list').dataset.count = '';
  pollStatus();
}

pollStatus();
setInterval(pollStatus, 1500);
connectStreamWs();
</script>
</body>
</html>
"""


def build_web_app(hub: CameraHubApp) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(hub.start_background_tasks)
    app.on_cleanup.append(hub.stop_background_tasks)

    app.router.add_get("/", hub.handle_index)
    app.router.add_get("/ws/streams", hub.handle_ws_streams)
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
    app.router.add_post("/api/camera/onboard", hub.handle_onboard_camera)
    app.router.add_post("/api/camera/decode-qr", hub.handle_decode_qr)
    app.router.add_post("/api/camera/add", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/add-qr", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/add_qr", hub.handle_add_camera_qr)
    app.router.add_post("/api/camera/scan", hub.handle_scan_lan)
    app.router.add_delete("/api/camera/{uid}", hub.handle_delete_camera)
    app.router.add_post("/api/camera/{uid}/ptz", hub.handle_ptz)
    app.router.add_get("/api/camera/{uid}/params", hub.handle_camera_params)
    app.router.add_post("/api/camera/{uid}/cgi", hub.handle_camera_cgi)
    app.router.add_post("/api/camera/{uid}/reboot", hub.handle_camera_reboot)
    app.router.add_post("/api/camera/{uid}/password", hub.handle_change_password)
    app.router.add_post("/api/camera/{uid}/record", hub.handle_record_control)
    app.router.add_post("/api/camera/{uid}/rename", hub.handle_rename)
    app.router.add_get("/api/events", hub.handle_list_events)
    app.router.add_delete("/api/events", hub.handle_delete_event)
    app.router.add_post("/api/settings", hub.handle_update_settings)

    os.makedirs(hub.recordings_dir, exist_ok=True)
    app.router.add_static("/recordings/", path=hub.recordings_dir, show_index=False)
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
