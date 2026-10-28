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
    """Bit-exact port of `DataEncoder.encodeMacWiFi(byte[] mac, String pwd)`."""
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
    """Bit-exact port of `VoicePlayer.convertTextToCodes(String text)`."""
    data = [VSTARCAM_HEX_CHARS.index(c) for c in hex_str.lower()]
    crc = vstarcam_crc16(data)
    full = data + [(crc >> 12) & 0xF, (crc >> 8) & 0xF, (crc >> 4) & 0xF, crc & 0xF]
    codes = [9, 0]
    prev = -1
    for val in full:
        if val != prev:
            codes.append(val + 1)
            prev = val
        else:
            codes.append(17)
            prev = 17
    codes.append(18)
    codes.append(9)
    return codes


def generate_vstarcam_sonic_wav(ssid: str, password: str, bssid: str = "", mode: str = "combo") -> dict:
    """
    Synthesize 44.1 kHz 16-bit mono PCM WAV audio for VStarcam Sonic Wi-Fi Provisioning.
    Solves the Wi-Fi 7 MLD / 5GHz phone BSSID bug by explicitly encoding the 2.4GHz BSSID suffix
    (`encodeMacWiFi`) as well as the full SSID string (`encodeSSIDWiFi`).
    """
    pwd_clean = password.strip()
    clean_bssid = re.sub(r"[^0-9a-fA-F]", "", bssid or "").lower()

    # If BSSID wasn't passed or user selected an SSID from the scan list, resolve its 2.4GHz BSSID
    if len(clean_bssid) < 4:
        info = scan_host_wifi_info()
        for net in info.get("networks", []):
            if net.get("ssid") == ssid:
                clean_bssid = net.get("bssid_24") or net.get("bssid_any") or ""
                break
        if not clean_bssid and info.get("current_ssid") == ssid:
            clean_bssid = info.get("current_bssid") or ""

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
    pause_ms = 850
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
            pcm_parts.append(lead_silence)

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

    async def start_camera_session(self, cam: proxy.CameraInfo):
        if cam.uid in self.sessions:
            return

        username = self.config["username"]
        password = self.config["password"]
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

        while self._running:
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
                connected_ips = {s.camera.ip for s in self.sessions.values()}
                cams = await proxy.discover_cameras_broadcast(timeout=2.5, target_ip=self.target_ip, skip_ips=connected_ips)
                for cam in cams:
                    if cam.uid not in self.sessions:
                        log.info("Discovered new camera on LAN: %s", cam)
                        await self.start_camera_session(cam)
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
            last_drw_age = round(now_loop - sess._last_drw_time, 1) if sess._last_drw_time > 0 else 999.0
            rem_post = 0.0
            if rec and rec.is_recording:
                rem_post = max(0.0, round(rec.record_until_mono - time.monotonic(), 1))

            c_did, f_did = proxy.pppp_uid_hex_to_did(uid)
            cameras_list.append({
                "uid": uid,
                "short_uid": uid[:10],
                "room_name": rec.room_name if rec else uid[:10],
                "hw_alias": getattr(cam, "hw_alias", ""),
                "hw_deviceid": getattr(cam, "hw_deviceid", "") or c_did,
                "formatted_did": getattr(cam, "formatted_did", "") or f_did,
                "ip": cam.ip,
                "rtsp_port": sess.rtsp_port,
                "rtsp_url": f"rtsp://{request.host.split(':')[0]}:{sess.rtsp_port}/",
                "state": sess.state,
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

        # Also include pre-registered cameras added via QR / UID that are waiting for LAN connection
        cfg_cams = self.config.get("cameras", {})
        if isinstance(cfg_cams, dict):
            for uid, c_cfg in cfg_cams.items():
                if uid in self.sessions:
                    continue
                if not isinstance(c_cfg, dict):
                    c_cfg = {"port": int(c_cfg) if isinstance(c_cfg, int) else 9555}
                c_did, f_did = proxy.pppp_uid_hex_to_did(uid)
                r_port = c_cfg.get("port", 9555)
                r_name = c_cfg.get("name") or f_did or uid[:10]
                cameras_list.append({
                    "uid": uid,
                    "short_uid": uid[:10],
                    "room_name": r_name,
                    "hw_alias": "",
                    "hw_deviceid": c_cfg.get("hw_deviceid") or c_did,
                    "formatted_did": c_cfg.get("formatted_did") or f_did,
                    "model": c_cfg.get("model", "CS22S"),
                    "ip": c_cfg.get("ip") or "Chờ Wi-Fi LAN",
                    "rtsp_port": r_port,
                    "rtsp_url": f"rtsp://{request.host.split(':')[0]}:{r_port}/",
                    "state": "WAITING_LAN",
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
                did_label = c_cfg.get("formatted_did") or f_did or uid[:12]
                r_name = c_cfg.get("name") or did_label
                svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
                  <rect width="960" height="540" fill="#0b0f17"/>
                  <circle cx="480" cy="210" r="46" fill="#161b22" stroke="#f59e0b" stroke-width="3" stroke-dasharray="8 6"/>
                  <text x="480" y="222" text-anchor="middle" fill="#f59e0b" font-family="Segoe UI, sans-serif" font-size="34">📷</text>
                  <text x="480" y="295" text-anchor="middle" fill="#e6edf3" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="22">{r_name} — Đã đăng ký từ QR ({did_label})</text>
                  <text x="480" y="335" text-anchor="middle" fill="#f59e0b" font-family="Segoe UI, sans-serif" font-size="17">⏳ Đang chờ camera kết nối vào mạng Wi-Fi LAN...</text>
                  <text x="480" y="370" text-anchor="middle" fill="#7d8590" font-family="Segoe UI, sans-serif" font-size="14">Ngay khi camera vào chung Wi-Fi, Hub sẽ tự động bắt tay PPPP (Port :{c_cfg.get('port', 9555)}) và phát Live 25 FPS</text>
                </svg>"""
                return web.Response(body=svg.encode("utf-8"), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})
            return web.Response(status=404, text="Camera not found")

        jpeg = None
        if rec:
            jpeg = await rec.get_live_snapshot(timeout=1.5)
        if not jpeg and sess.rtsp:
            jpeg = await sess.rtsp._get_snapshot_jpeg()
        if not jpeg:
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

    async def handle_ptz(self, request: web.Request) -> web.Response:
        uid = request.match_info.get("uid", "")
        sess = self.sessions.get(uid)
        if not sess or not sess.protocol:
            return web.json_response({"ok": False, "error": "Camera not connected"}, status=404)

        data = await request.json()
        action = data.get("action", "")

        ptz_map = {
            "up": (0, 1),
            "down": (2, 1),
            "left": (4, 1),
            "right": (6, 1),
            "center": (25, 0),
            "preset1_set": (30, 0),
            "preset1_go": (31, 0),
            "preset2_set": (32, 0),
            "preset2_go": (33, 0),
            "preset3_set": (34, 0),
            "preset3_go": (35, 0),
        }
        if action in ptz_map:
            cmd_code, onestep = ptz_map[action]
            await sess.protocol.send_ptz_command(cmd_code, onestep=onestep)
            if action in ("up", "down", "left", "right"):
                stop_code = cmd_code + 1
                asyncio.create_task(self._delayed_ptz_stop(sess.protocol, stop_code, 0.45))
            return web.json_response({"ok": True, "action": action})

        param_map = {
            "ir_auto": (14, 0),
            "ir_on": (14, 1),
            "ir_off": (14, 2),
            "flip_normal": (5, 0),
            "flip_v": (5, 1),
            "flip_h": (5, 2),
            "flip_vh": (5, 3),
            "osd_on": (18, 1),
        }
        if action in param_map:
            param, val = param_map[action]
            await sess.protocol.send_camera_param(param, val)
            return web.json_response({"ok": True, "action": action})

        return web.json_response({"ok": False, "error": f"Unknown action: {action}"}, status=400)

    async def _delayed_ptz_stop(self, proto: proxy.PPPPUnifiedProtocol, stop_code: int, delay: float):
        await asyncio.sleep(delay)
        try:
            await proto.send_ptz_command(stop_code, onestep=0)
        except Exception:
            pass

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

    async def handle_add_camera(self, request: web.Request) -> web.Response:
        """Register a camera by QR / sticker UID (and optional static IP) into the Hub and trigger LAN scan."""
        try:
            data = await request.json()
            did_input = str(data.get("device_id") or data.get("uid") or "").strip()
            room_name = str(data.get("room_name") or "").strip()
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
            cam_entry["hw_deviceid"] = compact_did
            cam_entry["formatted_did"] = formatted_did
            cam_entry["model"] = model or info.get("model", "CS22S")
            if target_ip:
                cam_entry["ip"] = target_ip
            cameras_dict[uid_hex] = cam_entry
            self._save_hub_config()

            # If already connected in sessions, update its room name immediately
            if uid_hex in self.sessions:
                sess = self.sessions[uid_hex]
                rec = self.recorders.get(uid_hex)
                if rec:
                    rec.set_room_name(room_name)
                sess.camera.label = f"{uid_hex[:8]} ({room_name})"
                if sess.protocol:
                    asyncio.create_task(sess.protocol.send_set_alias(room_name))
                return web.json_response({
                    "ok": True,
                    "uid": uid_hex,
                    "formatted_did": formatted_did,
                    "room_name": room_name,
                    "rtsp_port": port_val,
                    "connected": True,
                    "message": f"Camera {formatted_did} đang ONLINE và đã được cập nhật tên thành {room_name}!",
                })

            # Trigger immediate background LAN + subnet scan to connect if camera is on Wi-Fi
            asyncio.create_task(self._scan_and_connect_now(target_ip or self.target_ip))

            return web.json_response({
                "ok": True,
                "uid": uid_hex,
                "formatted_did": formatted_did,
                "room_name": room_name,
                "rtsp_port": port_val,
                "connected": False,
                "message": (
                    f"Đã thêm camera {formatted_did} ({room_name}, cổng RTSP :{port_val}) vào Hub! "
                    f"Hub đang quét toàn mạng LAN để kết nối tự động."
                ),
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def _scan_and_connect_now(self, extra_ip: Optional[str] = None) -> int:
        found_new = 0
        try:
            connected_ips = {s.camera.ip for s in self.sessions.values()}
            cams = await proxy.discover_cameras_broadcast(timeout=2.5, target_ip=extra_ip or self.target_ip, skip_ips=connected_ips)
            for cam in cams:
                if cam.uid not in self.sessions:
                    log.info("Discovered camera during active scan: %s", cam)
                    await self.start_camera_session(cam)
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
            lan_ip = get_host_lan_ip(self.target_ip)
            qs = urllib.parse.urlencode({
                "ssid": ssid,
                "password": password,
                "bssid": res["bssid_24"],
                "mode": res["mode"],
            })
            mobile_url = f"http://{lan_ip}:{self.web_port}/sonic?{qs}"

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
        """Standalone Mobile Page so user can play the 2.4GHz-corrected Sonic Wave from their Phone Speaker."""
        info = await asyncio.get_event_loop().run_in_executor(None, scan_host_wifi_info)
        ssid = request.query.get("ssid") or info.get("current_ssid") or "AM10K"
        password = request.query.get("password") or info.get("saved_password") or ""
        bssid = request.query.get("bssid") or info.get("current_bssid") or ""
        mode = request.query.get("mode") or "combo"
        qs = urllib.parse.urlencode({"ssid": ssid, "password": password, "bssid": bssid, "mode": mode})
        wav_url = f"/api/wifi/sonic.wav?{qs}"

        html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Phát Sóng Âm Nạp Wi-Fi Camera C22S</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:20px; text-align:center; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:14px; padding:20px; max-width:440px; margin:0 auto; box-shadow:0 8px 28px rgba(0,0,0,0.45); }}
  .play-btn {{ width:100%; padding:18px; font-size:18px; font-weight:800; border:none; border-radius:12px; background:#238636; color:#fff; cursor:pointer; margin-top:14px; box-shadow:0 4px 14px rgba(35,134,54,0.4); }}
  .play-btn:active {{ transform:scale(0.98); }}
  .stop-btn {{ width:100%; padding:12px; font-size:15px; font-weight:700; border:1px solid #f85149; border-radius:10px; background:rgba(248,81,73,0.15); color:#f85149; cursor:pointer; margin-top:10px; }}
  input, select {{ width:100%; padding:10px; border-radius:8px; border:1px solid #30363d; background:#0b0f17; color:#e6edf3; font-size:15px; margin-top:4px; box-sizing:border-box; }}
  label {{ display:block; text-align:left; font-size:12px; color:#8b949e; margin-top:10px; }}
</style>
</head>
<body>
  <div class="card">
    <div style="font-size:38px;">🔊📷</div>
    <h2 style="margin:8px 0 4px 0; font-size:19px;">Sóng Âm Nạp Wi-Fi C22S (Sonic Wave)</h2>
    <p style="color:#8b949e; font-size:13px; line-height:1.4;">
      Đã sửa lỗi 5GHz MLD của app Eye4: Phát chính xác mã <b>BSSID 2.4GHz ({bssid[-4:] if len(bssid)>=4 else 'Auto'})</b> cho camera C22S.
    </p>
    <label>Tên mạng Wi-Fi (SSID):</label>
    <input id="m-ssid" value="{ssid}" oninput="updateWavUrl()">
    <label>Mật khẩu Wi-Fi:</label>
    <input id="m-pwd" value="{password}" oninput="updateWavUrl()">
    <label>BSSID 2.4GHz (Tự động quét):</label>
    <input id="m-bssid" value="{bssid}" oninput="updateWavUrl()">
    <label>Chế độ mã hóa sóng âm:</label>
    <select id="m-mode" onchange="updateWavUrl()">
      <option value="combo" {"selected" if mode=="combo" else ""}>Kết hợp BSSID 2.4GHz + Tên SSID (Khuyên dùng)</option>
      <option value="mac" {"selected" if mode=="mac" else ""}>Chuẩn VStarcam MAC 2.4GHz (Nhanh nhất)</option>
      <option value="ssid" {"selected" if mode=="ssid" else ""}>Chuẩn VStarcam Full SSID</option>
    </select>

    <button class="play-btn" onclick="startPlay()">🔊 BẤM PHÁT SÓNG ÂM NẠP WI-FI</button>
    <button class="stop-btn" onclick="stopPlay()">⏹ Dừng phát</button>

    <div id="m-status" style="margin-top:12px; font-size:13px; color:#58a6ff; line-height:1.4;">
      👉 Hãy bật <b>Max Volume (100% âm lượng)</b> trên điện thoại và đặt loa điện thoại sát lỗ Mic của Camera (cách 2–5 cm).
    </div>
    <audio id="m-audio" src="{wav_url}" loop style="width:100%; margin-top:12px;" controls></audio>
  </div>
<script>
function updateWavUrl() {{
  const s = document.getElementById('m-ssid').value.trim();
  const p = document.getElementById('m-pwd').value;
  const b = document.getElementById('m-bssid').value.trim();
  const m = document.getElementById('m-mode').value;
  const u = `/api/wifi/sonic.wav?ssid=${{encodeURIComponent(s)}}&password=${{encodeURIComponent(p)}}&bssid=${{encodeURIComponent(b)}}&mode=${{encodeURIComponent(m)}}`;
  document.getElementById('m-audio').src = u;
}}
function startPlay() {{
  const a = document.getElementById('m-audio');
  a.currentTime = 0;
  a.play();
  document.getElementById('m-status').innerHTML = '🟢 <b>Đang phát sóng âm 6.5–10.1 kHz (Lặp liên tục)...</b><br>Giữ loa sát Camera (2–5 cm) cho đến khi nghe tiếng Camera báo <i>"Wi-Fi connecting"</i>!';
}}
function stopPlay() {{
  const a = document.getElementById('m-audio');
  a.pause();
  a.currentTime = 0;
  document.getElementById('m-status').innerHTML = '⏹ Đã dừng phát sóng âm.';
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
                return web.json_response({
                    "ok": True,
                    "method": "active_session",
                    "message": f"Đã bắn lệnh nạp Wi-Fi '{ssid}' trực tiếp tới camera ({self.sessions[uid].camera.ip})!",
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
  }
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
    <button class="btn primary" onclick="openQrModal()" style="font-weight:600;">📷 Thêm Camera (Upload QR)</button>
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
          <div class="dpad" style="margin-top:8px;">
            <div></div>
            <button class="btn" onclick="sendPtz('up')">▲</button>
            <div></div>
            <button class="btn" onclick="sendPtz('left')">◀</button>
            <button class="btn" onclick="sendPtz('center')">●</button>
            <button class="btn" onclick="sendPtz('right')">▶</button>
            <div></div>
            <button class="btn" onclick="sendPtz('down')">▼</button>
            <div></div>
          </div>
        </div>
        <div>
          <strong>📍 Vị trí nhớ (Presets)</strong>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:5px; margin-top:6px;">
            <button class="btn" onclick="sendPtz('preset1_go')">Gọi góc 1</button>
            <button class="btn" onclick="sendPtz('preset1_set')">Lưu góc 1</button>
            <button class="btn" onclick="sendPtz('preset2_go')">Gọi góc 2</button>
            <button class="btn" onclick="sendPtz('preset2_set')">Lưu góc 2</button>
          </div>
        </div>
        <div>
          <strong>🌙 Hồng ngoại & Hình ảnh</strong>
          <div style="display:flex; gap:5px; flex-wrap:wrap; margin-top:6px;">
            <button class="btn" onclick="sendPtz('ir_auto')">IR Tự động</button>
            <button class="btn" onclick="sendPtz('ir_on')">Bật IR</button>
            <button class="btn" onclick="sendPtz('ir_off')">Tắt IR</button>
            <button class="btn" onclick="sendPtz('flip_normal')">Hình chuẩn</button>
            <button class="btn" onclick="sendPtz('flip_vh')">Lật 180°</button>
          </div>
        </div>
        <div id="focus-info" style="color:var(--muted); line-height:1.5;"></div>
      </div>
    </div>
  </div>
</div>

<!-- ADD CAMERA BY QR & WIFI PROVISIONING MODAL -->
<div class="modal-backdrop" id="qr-modal">
  <div class="modal-box" style="max-width:780px; max-height:92vh; overflow-y:auto;">
    <div class="modal-header">
      <strong>📷 Thêm Camera bằng Ảnh QR & Nạp Wi-Fi Trực tiếp</strong>
      <button class="btn danger" onclick="closeQrModal()">✖ Đóng</button>
    </div>
    <div style="padding:16px; display:flex; flex-direction:column; gap:14px; font-size:13px;">
      <!-- STEP 1: QR IMAGE UPLOAD -->
      <div style="background:#0b0f17; border:1px solid var(--border); border-radius:8px; padding:12px;">
        <div style="font-weight:700; margin-bottom:8px; color:#58a6ff;">Bước 1: Tải ảnh chụp Tem QR dưới đáy Camera (hoặc nhập mã UID)</div>
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
          <div style="font-size:13px; font-weight:600; color:var(--text);">📤 Bấm chọn ảnh tem QR, Kéo thả ảnh, hoặc bấm <kbd style="background:#21262d; padding:2px 6px; border-radius:4px;">Ctrl + V</kbd></div>
          <div style="color:var(--muted); font-size:11px; margin-top:3px;">Tự động đọc mã UID (VD: <code>VSTJ-082279-XTJDT</code>) và tính toán mã PPPP LAN 40 ký tự</div>
        </div>

        <div id="qr-decode-result" style="display:none; margin-top:10px; padding:8px 12px; border-radius:6px; background:rgba(35,134,54,0.15); border:1px solid var(--accent); line-height:1.5; font-size:12px;"></div>

        <div style="display:grid; grid-template-columns:1fr 1fr 110px; gap:10px; margin-top:10px;">
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Mã UID trên tem Camera (*):</label>
            <input type="text" id="qr-did-input" placeholder="VD: VSTJ-082279-XTJDT" style="width:100%; font-family:monospace;">
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Tên Phòng / Vị trí hiển thị (*):</label>
            <input type="text" id="qr-room-input" placeholder="VD: Phong_102" style="width:100%;">
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Model:</label>
            <input type="text" id="qr-model-input" value="CS22S" style="width:100%;">
          </div>
        </div>
        <input type="hidden" id="qr-ip-input" value="">
      </div>

      <!-- STEP 2: WIFI PROVISIONING -->
      <div style="background:#0b0f17; border:1px solid var(--border); border-radius:8px; padding:12px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
          <span style="font-weight:700; color:#3fb950;">Bước 2: Cấu hình Wi-Fi cho Camera (Tự động quét mạng hiện tại & cho phép đổi tên mạng)</span>
          <button class="btn" onclick="loadHostWifiInfo(true)" style="font-size:11px; padding:3px 8px;">🔄 Quét lại Wi-Fi hiện tại</button>
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Tên mạng Wi-Fi (SSID — có thể sửa hoặc chọn):</label>
            <div style="display:flex; gap:6px;">
              <input type="text" id="wifi-ssid-input" list="wifi-ssid-list" placeholder="Nhập hoặc chọn tên Wi-Fi (2.4GHz)..." style="width:100%; font-weight:600;" oninput="refreshWifiQrPreview()">
              <datalist id="wifi-ssid-list"></datalist>
            </div>
          </div>
          <div>
            <label style="display:block; color:var(--muted); margin-bottom:4px; font-size:12px;">Mật khẩu Wi-Fi:</label>
            <div style="display:flex; gap:6px;">
              <input type="password" id="wifi-pwd-input" placeholder="Nhập mật khẩu Wi-Fi..." style="width:100%;" oninput="refreshWifiQrPreview()">
              <button class="btn" type="button" onclick="toggleWifiPwdVis()" title="Hiện/Ẩn mật khẩu" style="padding:5px 8px;">👁️</button>
            </div>
          </div>
        </div>
        <input type="hidden" id="wifi-bssid-input" value="">

        <div id="wifi-scan-status" style="color:var(--muted); font-size:11px; margin-top:6px;"></div>

        <!-- VStarcam Sonic Wave Audio Provisioning + Phone Speaker Helper QR -->
        <div id="wifi-qr-box" style="display:grid; margin-top:12px; padding:12px; background:#161b22; border:1px solid #238636; border-radius:8px; grid-template-columns:195px 1fr; gap:14px; align-items:center;">
          <div style="text-align:center; background:#fff; padding:8px; border-radius:8px; color:#0d1117;">
            <img id="wifi-qr-img" style="width:175px; height:175px; display:block; margin:0 auto;" alt="Phone Sonic QR">
            <div style="font-size:10px; font-weight:700; margin-top:4px; color:#111;">📱 Quét bằng ĐT nếu PC không có loa</div>
          </div>
          <div style="display:flex; flex-direction:column; gap:8px;">
            <div style="font-weight:700; color:#3fb950; font-size:14px;">🔊 Nạp Wi-Fi bằng Sóng Âm Thanh Chuẩn VStarcam C22S (Sonic Wave 6.5–10.1 kHz)</div>
            <div style="color:var(--muted); font-size:12px; line-height:1.45;">
              Model <b>CS22S (VSTJ)</b> nhận cấu hình Wi-Fi qua <b>Micro thu âm</b> (khi cam đang kêu <i>"Please use the App for Wi-Fi configuration"</i>).<br>
              • <b>Đã fix lỗi Router Wi-Fi 7 MLD (<code id="wifi-target-label">AM10K</code>):</b> App Eye4 trên ĐT lấy nhầm BSSID 5GHz (<code>b8:da</code>) khiến cam 2.4GHz không tìm thấy mạng. Hub đã tự động khóa đúng <b>BSSID 2.4GHz (<code id="sonic-bssid-badge">b8:d9</code>)</b>!
            </div>

            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
              <button class="btn primary" type="button" id="btn-play-sonic" onclick="playSonicWaveBrowser()" style="background:#238636; border-color:#2ea043; font-size:13px; padding:7px 14px;">
                🔊 Phát Sóng Âm Nạp Wi-Fi (Loa Trình duyệt)
              </button>
              <button class="btn" type="button" onclick="playSonicWavePcSpeaker()" style="border-color:#58a6ff; color:#58a6ff; font-size:12px; padding:7px 10px;">
                🖥️ Phát ra Loa PC (Windows Direct)
              </button>
              <button class="btn danger" type="button" onclick="stopSonicWave()" style="font-size:12px; padding:7px 10px;">
                ⏹ Dừng
              </button>
            </div>

            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
              <audio id="sonic-audio-player" controls loop style="height:30px; width:250px;"></audio>
              <select id="wifi-qr-fmt" onchange="refreshWifiQrPreview()" style="font-size:11px; padding:4px 6px;">
                <option value="combo" selected>Chế độ: Kết hợp BSSID 2.4GHz + Tên SSID (Khuyên dùng)</option>
                <option value="mac">Chế độ: Chuẩn VStarcam MAC 2.4GHz (1.8s/gói)</option>
                <option value="ssid">Chế độ: Chuẩn VStarcam Full SSID (Khi đổi mạng khác)</option>
              </select>
              <button class="btn" type="button" onclick="pushWifiDirectCgi()" style="font-size:11px;">📡 Bắn set_wifi.cgi</button>
            </div>

            <div id="sonic-mobile-link-box" style="font-size:11px; color:var(--muted);">
              📱 Nếu máy tính không có loa ngoài: Dùng điện thoại quét mã QR bên trái (hoặc mở <a id="sonic-mobile-link" href="/sonic" target="_blank" style="color:#58a6ff;">link này trên ĐT</a>) để phát sóng âm từ loa điện thoại sát lỗ Mic của Camera (cách 2–5 cm).
            </div>

            <div id="wifi-Wait-banner" style="padding:6px 10px; border-radius:6px; background:rgba(47,129,247,0.15); border:1px solid #2f81f7; color:#58a6ff; font-size:12px;">
              🔄 Hub đang tự động quét mạng LAN ngầm mỗi 4 giây để đón camera kết nối...
            </div>
          </div>
        </div>
      </div>

      <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
        <button class="btn" type="button" onclick="playSonicWaveBrowser()" style="border-color:#3fb950; color:#3fb950;">🔊 Phát Sóng Âm Nạp Wi-Fi Ngay</button>
        <div style="display:flex; gap:8px;">
          <button class="btn" onclick="closeQrModal()">Đóng</button>
          <button class="btn primary" id="btn-submit-qr" onclick="submitAddCameraByQr()" style="font-weight:600;">➕ Lưu Camera vào Hub & Nạp Wi-Fi</button>
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
            <span class="room-pill" id="title-${c.uid}">${c.room_name}</span>
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
      camerasData.map(c => `<option value="${c.room_name}">${c.room_name} (${c.ip})</option>`).join('');
    sel.value = curVal || 'ALL';
  }

  for (const c of camerasData) {
    const card = document.getElementById('card-' + c.uid);
    if (!card) continue;
    card.className = 'cam-card' + (c.is_recording ? ' recording' : '');
    const isWaiting = (c.state === 'WAITING_LAN');
    const liveFps = (fpsCounters[c.uid] && fpsCounters[c.uid].fps) ? fpsCounters[c.uid].fps : c.fps;
    const didTag = c.formatted_did ? ` [${c.formatted_did}]` : '';
    document.getElementById('title-' + c.uid).textContent = isWaiting
      ? `${c.room_name}${didTag} • Chờ Wi-Fi`
      : `${c.room_name} (${c.ip}) • ${liveFps} FPS`;

    const badge = document.getElementById('badge-' + c.uid);
    if (isWaiting) {
      badge.className = 'badge';
      badge.style.background = 'rgba(210,153,34,0.25)';
      badge.style.color = '#f59e0b';
      badge.textContent = `⏳ CHỜ KẾT NỐI LAN (${c.formatted_did || c.short_uid})`;
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
      ? `UID: ${c.formatted_did || c.hw_deviceid} | RTSP :${c.rtsp_port} | Tự động kết nối khi vào Wi-Fi`
      : `${c.codec.toUpperCase()} | ${c.kbps} KB/s | Cam ${c.fps}fps | RTSP :${c.rtsp_port} | Clips: ${c.clips_saved}`;

    const mbtn = document.getElementById('mbtn-' + c.uid);
    if (mbtn) {
      mbtn.className = 'btn' + (c.manual_record ? ' danger' : '');
      mbtn.textContent = c.manual_record ? '⏹ Dừng ghi' : '⏺ Ghi tay';
    }

    if (focusUid === c.uid) {
      document.getElementById('focus-info').innerHTML =
        `<b>IP:</b> ${c.ip}<br><b>RTSP:</b> <code>${c.rtsp_url}</code><br>` +
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
  focusUid = null;
  document.getElementById('focus-modal').classList.remove('open');
  syncWsConfig();
}

async function sendPtz(action) {
  if (!focusUid) return;
  await fetch(`/api/camera/${focusUid}/ptz`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action})
  });
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
      </div>
      <div style="display:flex; gap:6px;">
        <input type="text" id="rname-${c.uid}" value="${c.room_name}" style="width:150px;">
        <button class="btn primary" onclick="renameRoom('${c.uid}')">Đổi tên + OSD</button>
        <button class="btn" onclick="pushWifiToCamera('${c.uid}', '${c.room_name}')" title="Bắn cấu hình Wi-Fi mới từ xa qua mạng">📡 Đổi Wi-Fi</button>
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
let lastAddedUid = null;

function openQrModal(prefillDid = '', prefillRoom = '') {
  document.getElementById('qr-modal').classList.add('open');
  const nextIdx = (camerasData ? camerasData.length : 1) + 1;
  const roomInp = document.getElementById('qr-room-input');
  if (prefillDid) document.getElementById('qr-did-input').value = prefillDid;
  if (prefillRoom) {
    roomInp.value = prefillRoom;
  } else if (!roomInp.value) {
    roomInp.value = 'Phong_' + (100 + nextIdx);
  }
  loadHostWifiInfo(false);
  startWifiWaitPolling();
}

function closeQrModal() {
  stopSonicWave();
  document.getElementById('qr-modal').classList.remove('open');
  if (wifiPollTimer) {
    clearInterval(wifiPollTimer);
    wifiPollTimer = null;
  }
}

function startWifiWaitPolling() {
  if (wifiPollTimer) clearInterval(wifiPollTimer);
  wifiPollTimer = setInterval(async () => {
    if (!document.getElementById('qr-modal').classList.contains('open')) return;
    await fetch('/api/camera/scan', { method: 'POST' });
    await pollStatus();
    if (lastAddedUid) {
      const found = camerasData.find(c => c.uid === lastAddedUid && c.state === 'CONNECTED');
      if (found) {
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
  statusEl.textContent = '⏳ Đang quét tên mạng Wi-Fi hiện tại của máy tính...';
  try {
    const res = await fetch('/api/wifi/info');
    const data = await res.json();
    if (!data.ok) return;

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
    if (data.current_bssid) {
      bssidInp.value = data.current_bssid;
    }
    if ((forceOverwrite || !pwdInp.value) && data.saved_password) {
      pwdInp.value = data.saved_password;
    }

    let msg = data.current_ssid
      ? `✅ Đã tự động điền mạng Wi-Fi hiện tại: <b>${data.current_ssid}</b>` + (data.saved_password ? ' (đã tự động lấy mật khẩu lưu trên máy — bạn có thể sửa tên mạng hoặc mật khẩu tùy ý).' : ' (vui lòng nhập mật khẩu Wi-Fi).')
      : 'ℹ️ Hãy nhập tên mạng Wi-Fi (2.4GHz) và mật khẩu của bạn.';
    if (data.camera_hotspots && data.camera_hotspots.length > 0) {
      msg += `<br>📡 <b>Phát hiện Wi-Fi nội bộ của Camera gần đây:</b> <code>${data.camera_hotspots.join(', ')}</code>`;
    }
    statusEl.innerHTML = msg;
    if (ssidInp.value) {
      refreshWifiQrPreview(false);
    }
  } catch (e) {
    statusEl.textContent = 'Không thể tự động quét Wi-Fi: ' + e;
  }
}

function toggleWifiPwdVis() {
  const inp = document.getElementById('wifi-pwd-input');
  inp.type = (inp.type === 'password') ? 'text' : 'password';
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

async function handleQrFileSelect(file) {
  if (!file) return;
  const previewWrap = document.getElementById('qr-preview-wrap');
  const previewImg = document.getElementById('qr-preview-img');
  previewImg.src = URL.createObjectURL(file);
  previewWrap.style.display = 'block';

  const resBox = document.getElementById('qr-decode-result');
  resBox.style.display = 'block';
  resBox.style.borderColor = 'var(--border)';
  resBox.innerHTML = '⏳ Đang phân tích & giải mã ảnh QR dưới đáy camera...';

  const fd = new FormData();
  fd.append('file', file);

  try {
    const res = await fetch('/api/camera/decode-qr', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) {
      resBox.style.background = 'rgba(218,54,51,0.15)';
      resBox.style.borderColor = 'var(--danger)';
      resBox.innerHTML = `❌ <b>${data.error}</b>`;
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
  const model = document.getElementById('qr-model-input').value.trim();
  const ip = document.getElementById('qr-ip-input').value.trim();
  if (!did) {
    alert('Vui lòng tải ảnh chụp tem QR hoặc nhập mã UID trên thân camera (VD: VSTJ-082279-XTJDT)!');
    return;
  }
  const btn = document.getElementById('btn-submit-qr');
  btn.disabled = true;
  btn.textContent = '⏳ Đang lưu & tạo mã QR Wi-Fi...';
  try {
    const res = await fetch('/api/camera/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ device_id: did, room_name: room, model: model, ip: ip })
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
      banner.innerHTML = `✅ <b>Đã lưu ${data.room_name} (${data.formatted_did}) lên Grid!</b> Bây giờ hãy đưa mắt Camera quét mã QR bên trái để nạp Wi-Fi <b>${document.getElementById('wifi-ssid-input').value}</b>.`;
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '➕ Lưu Camera vào Hub & Tạo QR Nạp Wi-Fi';
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
    app = web.Application()
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
    app.router.add_post("/api/camera/decode-qr", hub.handle_decode_qr)
    app.router.add_post("/api/camera/add", hub.handle_add_camera)
    app.router.add_post("/api/camera/scan", hub.handle_scan_lan)
    app.router.add_delete("/api/camera/{uid}", hub.handle_delete_camera)
    app.router.add_post("/api/camera/{uid}/ptz", hub.handle_ptz)
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
