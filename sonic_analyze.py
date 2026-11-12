#!/usr/bin/env python3
"""
Sonic Wi-Fi provisioning analyser.

Reads a WAV recording of a "sound wave" Wi-Fi pairing signal (the one the Eye4 /
VStarcam app plays, or the one camera_hub.py generates) and prints what is
actually on the air: the tone frequencies, the tone length, the burst gaps and,
when the framing is understood, the decoded SSID / MAC / password.

Use it to compare the official app against camera_hub.py:

    python sonic_analyze.py eye4_recording.wav
    python sonic_analyze.py hub_recording.wav
    python sonic_analyze.py --selftest

Recording tips: record at 44.1 kHz or 48 kHz, uncompressed WAV, with the phone
speaker a few cm from the microphone. Convert m4a/mp3 first, e.g.
    ffmpeg -i rec.m4a -ac 1 -ar 44100 rec.wav

The payload decoder is a transcription of the device-side decoder of the
voiceRecog library (vr_decodeData / vr_decodeWiFi / vr_decodeSSIDWiFi).
"""
import argparse
import sys
import wave

import numpy as np

HEX = "0123456789abcdef"
KNOWN_GRIDS = [(6500, 200), (4000, 150), (16000, 150), (17200, 200)]


# ----------------------------------------------------------------------------
# Payload decoder (hex string -> ssid/mac + password)
# ----------------------------------------------------------------------------
def _bits(nibs, a, b):
    s = "".join(format(HEX.index(c), "04b") for c in nibs)
    return int(s[a:b], 2)


def _c64(v):
    if v < 26:
        return chr(65 + v)
    if v < 52:
        return chr(97 + v - 26)
    if v < 62:
        return chr(48 + v - 52)
    return "_" if v == 62 else "-"


def _packed(nibs, n, width, conv):
    out, cost, i = "", 0, 0
    while n < 0 or i < n:
        end = (i + 1) * width
        if end > len(nibs) * 4:
            break
        out += conv(_bits(nibs, i * width, end))
        cost = (end + 3) // 4
        i += 1
    return out, cost


def _digits(nibs, n):
    k = len(nibs) if n < 0 else min(n, len(nibs))
    return nibs[:k], k


def _c256(nibs, n):
    k = len(nibs) // 2 if n < 0 else min(n, len(nibs) // 2)
    return "".join(chr(int(nibs[2 * i:2 * i + 2], 16)) for i in range(k)), 2 * k


def decode_data(nibs, n):
    """One typed field. Returns (type, text, nibbles consumed)."""
    t = HEX.index(nibs[0]) >> 1
    cost, apart = 1, n
    if t in (1, 2, 4):
        apart = (((HEX.index(nibs[0]) & 1) << 4) | HEX.index(nibs[1])) + 1
        cost = 2
    lower = lambda v: chr(97 + v)
    upper = lambda v: chr(65 + v)

    def rest(r):
        return n if n < 0 else n - len(r)

    if t == 1:
        r, c = _packed(nibs[cost:], apart, 5, lower); cost += c
        r2, c = _digits(nibs[cost:], rest(r)); r += r2; cost += c
    elif t == 2:
        r, c = _packed(nibs[cost:], apart, 6, _c64); cost += c
        r2, c = _digits(nibs[cost:], rest(r)); r += r2; cost += c
    elif t == 4:
        r, c = _digits(nibs[cost:], apart); cost += c
        r2, c = _packed(nibs[cost:], rest(r), 6, _c64); r += r2; cost += c
    elif t == 0:
        r, c = _digits(nibs[cost:], n); cost += c
    elif t == 3:
        r, c = _c256(nibs[cost:], n); cost += c
    elif t == 5:
        r, c = _packed(nibs[cost:], n, 5, lower); cost += c
    elif t == 6:
        r, c = _packed(nibs[cost:], n, 6, _c64); cost += c
    else:
        r, c = _packed(nibs[cost:], n, 5, upper); cost += c
    return t, r, cost


def decode_payload(p):
    if p[0] not in "23":
        # VStarcam encodeMacWiFi: one nibble (len - 1), raw MAC hex, typed password.
        n = HEX.index(p[0]) + 1
        mac = p[1:1 + 2 * n]
        t2, pwd, _ = decode_data(p[1 + 2 * n:], -1)
        return {"kind": "MAC+password", "first": mac, "first_len": n, "first_type": 3,
                "pwd": pwd, "pwd_type": t2, "len_ok": len(mac) == 2 * n}
    info = HEX.index(p[0]) >> 1
    first_len = (((HEX.index(p[0]) & 1) << 4) | HEX.index(p[1])) + 1
    t1, first, c = decode_data(p[2:], first_len)
    t2, pwd, _ = decode_data(p[2 + c:], -1)
    kind = {0: "MAC+password", 1: "SSID+password", 2: "phone", 3: "string"}.get(info, "type %d" % info)
    if info == 0:
        first = first.encode("latin1").hex()
    return {"kind": kind, "first": first, "first_len": first_len,
            "first_type": t1, "pwd": pwd, "pwd_type": t2,
            "len_ok": (len(first) // 2 if info == 0 else len(first)) == first_len}


def crc16(vals):
    crc = 0xFFFF
    for v in vals:
        crc ^= (v & 0xFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def codes_to_payload(codes):
    """
    Undo the tone framing: start token 0, stop token 18, token 17 = "same as
    previous nibble", data nibble n is sent as tone index n+1.
    Returns (hex_string, crc_ok) or (None, False).
    """
    if 0 not in codes:
        return None, False
    body = codes[codes.index(0) + 1:]
    if 18 in body:
        body = body[:body.index(18)]
    vals, prev = [], None
    for c in body:
        if c == 17:
            if prev is None:
                return None, False
            vals.append(prev)
        elif 1 <= c <= 16:
            prev = c - 1
            vals.append(prev)
        else:
            return None, False
    if len(vals) <= 4:
        return None, False
    data, tail = vals[:-4], vals[-4:]
    got = (tail[0] << 12) | (tail[1] << 8) | (tail[2] << 4) | tail[3]
    return "".join(HEX[v] for v in data), crc16(data) == got


# ----------------------------------------------------------------------------
# Audio analysis
# ----------------------------------------------------------------------------
def load_wav(path):
    with wave.open(path, "rb") as wf:
        sr, ch, sw, n = wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()
        raw = wf.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise SystemExit("Unsupported sample width: %d bytes (use 16-bit PCM WAV)" % sw)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x


def find_tones(sr, x, fmin=3000.0, floor_db=-30.0):
    """Returns a list of (start_s, duration_s, freq_hz, level_db)."""
    win_n = int(sr * 0.020)
    hop = int(sr * 0.005)
    if len(x) < win_n:
        return []
    nfft = 1 << int(np.ceil(np.log2(win_n * 4)))
    win = np.hanning(win_n)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    lo = int(np.searchsorted(freqs, fmin))
    frames = []
    for start in range(0, len(x) - win_n, hop):
        spec = np.abs(np.fft.rfft(x[start:start + win_n] * win, nfft))
        band = spec[lo:]
        k = int(np.argmax(band))
        peak = band[k]
        # A tone is a narrow peak: compare it to the median of the band.
        frames.append((start / sr, freqs[lo + k], peak, peak / (np.median(band) + 1e-12)))
    if not frames:
        return []
    top = max(f[2] for f in frames)
    thr = top * (10 ** (floor_db / 20.0))
    tones, cur = [], None
    for t, f, mag, dom in frames:
        ok = mag >= thr and dom >= 8.0
        if ok and cur and abs(f - cur["f"][-1]) < 70:
            cur["f"].append(f); cur["m"].append(mag); cur["end"] = t
        else:
            if cur:
                tones.append(cur)
            cur = {"start": t, "end": t, "f": [f], "m": [mag]} if ok else None
    if cur:
        tones.append(cur)
    out = []
    for c in tones:
        dur = c["end"] - c["start"] + hop / sr
        if dur < 0.025:
            continue
        out.append((c["start"], dur, float(np.median(c["f"])),
                    20 * np.log10(max(c["m"]) / top + 1e-12)))
    return out


def fit_grid(freq_list):
    best = None
    for base, step in KNOWN_GRIDS:
        idx = [round((f - base) / step) for f in freq_list]
        if min(idx) < 0 or max(idx) > 18:
            continue
        err = float(np.mean([abs(f - (base + i * step)) for f, i in zip(freq_list, idx)]))
        if err < step * 0.2 and (best is None or err < best[2]):
            best = (base, step, err)
    return best


def analyse(path, fmin=3000.0):
    sr, x = load_wav(path)
    print("File: %s  (%d Hz, %.1f s)" % (path, sr, len(x) / sr))
    if sr < 22050:
        print("  ! Sample rate too low to see tones above %d Hz - re-record at 44.1/48 kHz." % (sr // 2))
    tones = find_tones(sr, x, fmin)
    if not tones:
        print("  No tones found above %.0f Hz. Is the recording too quiet or low-pass filtered?" % fmin)
        return []
    bursts, cur = [], [tones[0]]
    for prev, t in zip(tones, tones[1:]):
        if t[0] - (prev[0] + prev[1]) > 0.30:
            bursts.append(cur); cur = []
        cur.append(t)
    bursts.append(cur)

    all_f = [t[2] for t in tones]
    durs = [t[1] for t in tones]
    print("  Tones: %d in %d burst(s)" % (len(tones), len(bursts)))
    print("  Frequency range: %.0f - %.0f Hz" % (min(all_f), max(all_f)))
    uniq = sorted(set(int(round(f / 50.0)) * 50 for f in all_f))
    print("  Distinct frequencies (50 Hz bins): %s" % uniq)
    print("  Tone length: median %.0f ms (min %.0f, max %.0f)" %
          (np.median(durs) * 1000, min(durs) * 1000, max(durs) * 1000))
    gaps = [b[0][0] - (a[-1][0] + a[-1][1]) for a, b in zip(bursts, bursts[1:])]
    if gaps:
        print("  Silence between bursts: %s ms" % [int(g * 1000) for g in gaps])
    grid = fit_grid(all_f)
    if not grid:
        print("  ! Frequencies do not fit any known 19-tone grid %s." % KNOWN_GRIDS)
        print("    Work out base/step from the list above and set VSTARCAM_SONIC_FREQS to match.")
        return []
    base, step, err = grid
    print("  Grid: base %d Hz, step %d Hz (mean error %.0f Hz)" % (base, step, err))

    results = []
    for n, burst in enumerate(bursts, 1):
        codes = [int(round((t[2] - base) / step)) for t in burst]
        weak = min(t[3] for t in burst)
        print("\n  Burst %d: %d tones, %.2f s, weakest tone %.0f dB" %
              (n, len(codes), burst[-1][0] + burst[-1][1] - burst[0][0], weak))
        print("    tone indexes: %s" % " ".join(str(c) for c in codes))
        payload, ok = codes_to_payload(codes)
        if payload is None:
            print("    framing: NOT understood (no start token 0 / unexpected tokens)")
            continue
        print("    payload: %s   CRC: %s" % (payload, "OK" if ok else "FAIL"))
        try:
            info = decode_payload(payload)
        except Exception as exc:  # malformed payload
            print("    decode: failed (%r)" % exc)
            continue
        label = "MAC" if info["kind"].startswith("MAC") else "SSID"
        print("    decode: %s  %s=%r  password=%r%s" %
              (info["kind"], label, info["first"], info["pwd"],
               "" if info["len_ok"] else "   <-- length field does not match!"))
        results.append((ok, info))
    return results


def selftest():
    import os
    import tempfile
    import io
    import math
    import re
    import types
    # Load only the encoder section of camera_hub.py so the web/video dependencies are not needed.
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "camera_hub.py"), encoding="utf-8").read()
    ns = {"io": io, "math": math, "re": re, "wave": wave, "np": np}
    exec(src[src.index("VSTARCAM_HEX_CHARS ="):src.index("def get_host_lan_ip")], ns)
    hub = types.SimpleNamespace(**ns)
    ns["scan_host_wifi_info"] = lambda: {"networks": []}
    cases = [("Test-5Auu", "abcd1234", "", "ssid"),
             ("Test-5Auu", "abcd1234", "0011223344fe", "mac"),
             ("MyHome_2.4G", "P@ssw0rd!", "", "ssid"),
             ("11111111", "1" * 32, "", "ssid")]
    failed = 0
    for ssid, pwd, bssid, mode in cases:
        res = hub.generate_vstarcam_sonic_wav(ssid, pwd, bssid, mode)
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.write(fd, res["wav_bytes"]); os.close(fd)
        out = analyse(path)
        os.unlink(path)
        want = bssid[-4:] if mode == "mac" else ssid
        good = out and all(ok and i["first"] == want and i["pwd"] == pwd for ok, i in out)
        print("\n=> %s %s/%s: %s\n" % (mode, ssid, pwd, "PASS" if good else "FAIL"))
        failed += 0 if good else 1
    return failed


def main():
    ap = argparse.ArgumentParser(description="Analyse a sonic Wi-Fi provisioning recording (WAV).")
    ap.add_argument("wav", nargs="*", help="WAV file(s) to analyse")
    ap.add_argument("--fmin", type=float, default=3000.0, help="ignore everything below this frequency (Hz)")
    ap.add_argument("--selftest", action="store_true", help="generate with camera_hub.py and decode it back")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(1 if selftest() else 0)
    if not args.wav:
        ap.error("give a WAV file or --selftest")
    for path in args.wav:
        analyse(path, args.fmin)
        print()


if __name__ == "__main__":
    main()
