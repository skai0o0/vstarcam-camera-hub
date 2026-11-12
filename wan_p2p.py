#!/usr/bin/env python3
"""
VStarcam / Eye4 CS2 PPPP WAN P2P & Cloud Relay Engine

Implements WAN P2P discovery, InitString decoding, UDP hole-punching,
and Cloud Relay fallback for VStarcam/Eye4 cameras across subnets/Internet.
"""

import asyncio
import logging
import re
import socket
import struct
from typing import Optional

log = logging.getLogger("eye4.wan")

# CS2 PPPP InitString Lookup Table (54 bytes)
PPPP_LUT = bytes.fromhex(
    "4959433db5bf6da347534f6165e371e9677f02030badb3892b2f35c16b8b959711e5a70deff1050783fb9d3bc5c713171d1f2529d3df"
)

# P2P_Proprietary permutation table (shared with eye4_rtsp_proxy)
P2P_PE_TABLE = bytes([
    0x7C, 0x9C, 0xE8, 0x4A, 0x13, 0xDE, 0xDC, 0xB2, 0x2F, 0x21, 0x23, 0xE4, 0x30, 0x7B, 0x3D, 0x8C,
    0xBC, 0x0B, 0x27, 0x0C, 0x3C, 0xF7, 0x9A, 0xE7, 0x08, 0x71, 0x96, 0x00, 0x97, 0x85, 0xEF, 0xC1,
    0x1F, 0xC4, 0xDB, 0xA1, 0xC2, 0xEB, 0xD9, 0x01, 0xFA, 0xBA, 0x3B, 0x05, 0xB8, 0x15, 0x87, 0x83,
    0x28, 0x72, 0xD1, 0x8B, 0x5A, 0xD6, 0xDA, 0x93, 0x58, 0xFE, 0xAA, 0xCC, 0x6E, 0x1B, 0xF0, 0xA3,
    0x88, 0xAB, 0x43, 0xC0, 0x0D, 0xB5, 0x45, 0x38, 0x4F, 0x50, 0x22, 0x66, 0x20, 0x7F, 0x07, 0x5B,
    0x14, 0x98, 0x1D, 0x9B, 0xA7, 0x2A, 0xB9, 0xA8, 0xCB, 0xF1, 0xFC, 0x49, 0x47, 0x06, 0x3E, 0xB1,
    0x0E, 0x04, 0x3A, 0x94, 0x5E, 0xEE, 0x54, 0x11, 0x34, 0xDD, 0x4D, 0xF9, 0xEC, 0xC7, 0xC9, 0xE3,
    0x78, 0x1A, 0x6F, 0x70, 0x6B, 0xA4, 0xBD, 0xA9, 0x5D, 0xD5, 0xF8, 0xE5, 0xBB, 0x26, 0xAF, 0x42,
    0x37, 0xD8, 0xE1, 0x02, 0x0A, 0xAE, 0x5F, 0x1C, 0xC5, 0x73, 0x09, 0x4E, 0x69, 0x24, 0x90, 0x6D,
    0x12, 0xB3, 0x19, 0xAD, 0x74, 0x8A, 0x29, 0x40, 0xF5, 0x2D, 0xBE, 0xA5, 0x59, 0xE0, 0xF4, 0x79,
    0xD2, 0x4B, 0xCE, 0x89, 0x82, 0x48, 0x84, 0x25, 0xC6, 0x91, 0x2B, 0xA2, 0xFB, 0x8F, 0xE9, 0xA6,
    0xB0, 0x9E, 0x3F, 0x65, 0xF6, 0x03, 0x31, 0x2E, 0xAC, 0x0F, 0x95, 0x2C, 0x5C, 0xED, 0x39, 0xB7,
    0x33, 0x6C, 0x56, 0x7E, 0xB4, 0xA0, 0xFD, 0x7A, 0x81, 0x53, 0x51, 0x86, 0x8D, 0x9F, 0x77, 0xFF,
    0x6A, 0x80, 0xDF, 0xE2, 0xBF, 0x10, 0xD7, 0x75, 0x64, 0x57, 0x76, 0xF3, 0x55, 0xCD, 0xD0, 0xC8,
    0x18, 0xE6, 0x36, 0x41, 0x62, 0xCF, 0x99, 0xF2, 0x32, 0x4C, 0x67, 0x60, 0x61, 0x92, 0xCA, 0xD3,
    0xEA, 0x63, 0x7D, 0x16, 0xB6, 0x8E, 0xD4, 0x68, 0x35, 0xC3, 0x52, 0x9D, 0x46, 0x44, 0x1E, 0x17,
])

WAN_SERVER_PORT = 32100

# Official VStarcam InitStrings mapped by DID prefix
WAN_INIT_STRINGS: dict[str, str] = {
    "VSTJ": "EEGDFHBLKGJIGEJNEOHEFBEIGANCHHMBHIFEAHDEAMJCKCKJDJAFDDPPHLKJIHLMBENHKDCHPHNJBODA:vstarcam2019",
    "VSTK": "EBGDEJBJKGJFGJJBEFHPFCEKHGNMHNNMHMFFBICPAJJNLDLLDHACCNONGLLPJGLKANMJLDDHODMEBOCIJEMA:vstarcam2019",
    "VSTL": "EEGDFHBLKGJIGEJIEIGNFPEEHGNMHPNBGOFIBECEBLJDLMLGDKAPCNPFGOLLJFLJAOMKLBDFOGMAAFCJJPNFJP:vstarcam2019",
    "VSTM": "EBGEEOBOKHJNHGJGEAGAEPEPHDMGHINBGIECBBCBBJIKLKLCCDBBCFODHLKLJJKPBOMELECKPNMNAICEJCNNJH:vstarcam2019",
    "VSTN": "EEGDFHBBKBIFGAIAFGHDFLFJGJNIGEMOHFFPAMDMAAIIKBKNCDBDDMOGHLKCJCKFBFMPLMCBPEMG:vstarcam2019",
    "VSTP": "EEGDFHBLKGJIGEJLEIGJFLENHLNBHCNMGAFGBNCOAIJMLKKODNALCCPKGBLHJLLHAHMBKNDFOGNGBDCIJFMB:vstarcam2019",
    "VSTG": "EEGDFHBOKCIGGFJPECHIFNEBGJNLHOMIHEFJBADPAGJELNKJDKANCBPJGHLAIALAADMDKPDGOENEBECCIK:vstarcam2018",
    "VSTH": "EEGDFHBLKGJIGEJLEKGOFMEDHAMHHJNAGGFABMCOBGJOLHLJDFAFCPPHGILKIKLMANNHKEDKOINIBNCPJOMK:vstarcam2018",
    "VSTA": "EFGFFBBOKAIEGHJAEDHJFEEOHMNGDCNJCDFKAKHLEBJHKEKMCAFCDLLLHAOCJPPMBHMNOMCJKGJEBGGHJHIOMFBDNPKNFEGCEGCBGCALMFOHBCGMFK:",
    "VSTB": "ADCBBFAOPPJAHGJGBBGLFLAGDBJJHNJGGMBFBKHIBBNKOKLDHOBHCBOEHOKJJJKJBPMFLGCPPJMJAPDOIPNL:",
    "VSTC": "ADCBBFAOPPJAHGJGBBGLFLAGDBJJHNJGGMBFBKHIBBNKOKLDHOBHCBOEHOKJJJKJBPMFLGCPPJMJAPDOIPNL:",
}

# WAN PPPP Message Types (after P2P_Proprietary decryption)
MSG_WAN_HELLO = 0x00
MSG_WAN_HELLO_ACK = 0x01
MSG_WAN_P2P_REQ = 0x20
MSG_WAN_P2P_REQ_ACK = 0x21
MSG_WAN_PUNCH_TO = 0x40
MSG_WAN_PUNCH_PKT = 0x41
MSG_WAN_P2P_RDY = 0x42
MSG_WAN_LIST_REQ = 0x67
MSG_WAN_RLY_TO = 0x82
MSG_WAN_RLY_PKT = 0x83
MSG_WAN_RLY_RDY = 0x84


def p2p_derive_key(psk: bytes) -> bytes:
    """Derive 4-byte cipher key from PSK bytes."""
    if not psk:
        return b"\x00\x00\x00\x00"
    k0 = sum(psk) & 0xFF
    k1 = (-k0) & 0xFF
    k2 = sum(b // 3 for b in psk) & 0xFF
    k3 = 0
    for b in psk:
        k3 ^= b
    return bytes([k0, k1, k2, k3])


def p2p_proprietary_encrypt(key4: bytes, data: bytes) -> bytes:
    """Encrypt data using P2P_Proprietary stateful XOR table cipher."""
    out = bytearray(len(data))
    prev = 0
    for i, p in enumerate(data):
        mask = P2P_PE_TABLE[(key4[prev & 3] + prev) & 0xFF]
        c = p ^ mask
        out[i] = c
        prev = c
    return bytes(out)


def p2p_proprietary_decrypt(key4: bytes, data: bytes) -> bytes:
    """Decrypt data using P2P_Proprietary stateful XOR table cipher."""
    out = bytearray(len(data))
    prev = 0
    for i, c in enumerate(data):
        mask = P2P_PE_TABLE[(key4[prev & 3] + prev) & 0xFF]
        out[i] = c ^ mask
        prev = c
    return bytes(out)


def decode_init_string(init_str: str) -> list[str]:
    """Decode VStarcam InitString (characters 'A'..'P', before optional :psk)
    into a list of P2P server IP/hostname strings."""
    encoded = init_str.split(":", 1)[0].strip()
    n = len(encoded) // 2
    output = bytearray(n)
    for i in range(n):
        running_xor = 0x39
        for j in range(i):
            running_xor ^= output[j]
        hi = ord(encoded[2 * i]) - ord("A")
        lo = ord(encoded[2 * i + 1]) - ord("A")
        byte_val = (hi << 4) | lo
        output[i] = (running_xor ^ PPPP_LUT[i % len(PPPP_LUT)] ^ byte_val) & 0xFF
    decoded = output.decode("ascii", errors="ignore").rstrip("\x00")
    return [s.strip() for s in decoded.split(",") if s.strip()]


def did_to_binary(did: str) -> bytes:
    """Parse 'VSTJ-082299-RLSXK' or 'VSTJ082299RLSXK' into the 20-byte binary struct:
    prefix(8B null-padded) + struct.pack('>I', int(serial)) + suffix(8B null-padded)."""
    clean = did.strip().upper().replace("-", "").replace(" ", "").replace(":", "")
    if clean.startswith("UID"):
        clean = clean[3:].strip()
    m = re.match(r"^([A-Z]+)(\d+)([A-Z0-9]+)$", clean)
    if not m:
        raise ValueError(f"Invalid VStarcam DID format: {did}")
    prefix, serial_str, suffix = m.group(1), m.group(2), m.group(3)
    return (
        prefix.encode("ascii")[:8].ljust(8, b"\x00")
        + struct.pack(">I", int(serial_str))
        + suffix.encode("ascii")[:8].ljust(8, b"\x00")
    )


def binary_to_did(bin_did: bytes) -> tuple[str, str]:
    """Unpack a 20-byte binary DID into (compact_did, formatted_did)."""
    if len(bin_did) < 20:
        return ("", "")
    prefix = bin_did[:8].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    serial = struct.unpack(">I", bin_did[8:12])[0]
    suffix = bin_did[12:20].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
    if prefix.isalpha() and suffix.isalnum() and 0 < serial < 99999999:
        return (f"{prefix}{serial:06d}{suffix}", f"{prefix}-{serial:06d}-{suffix}")
    return ("", "")


def get_p2p_servers_for_did(did: str) -> tuple[list[tuple[str, int]], bytes]:
    """Return (list_of_(ip, 32100), psk_bytes) for the camera's prefix
    (defaulting to VSTJ servers and b'vstarcam2019' if unknown)."""
    clean = (did or "").strip().upper().replace("-", "").replace(" ", "")
    if clean.startswith("UID"):
        clean = clean[3:].strip()
    m = re.match(r"^([A-Z]+)", clean)
    prefix = m.group(1) if m else "VSTJ"
    init_entry = WAN_INIT_STRINGS.get(prefix, WAN_INIT_STRINGS["VSTJ"])
    parts = init_entry.split(":", 1)
    psk_str = parts[1] if len(parts) > 1 else "vstarcam2019"
    servers = decode_init_string(parts[0])
    return ([(ip, WAN_SERVER_PORT) for ip in servers], psk_str.encode("ascii"))


def pack_sockaddr_in_le(ip_str: str, port: int) -> bytes:
    """Pack IPv4 address and port into PPPP's 16-byte little-endian sockaddr_in format:
    struct.pack('>H', 2) + struct.pack('<HBBBB8s', port, ip_b3, ip_b2, ip_b1, ip_b0, b'\\x00'*8)."""
    try:
        parts = [int(x) & 0xFF for x in ip_str.strip().split(".")]
        if len(parts) != 4:
            parts = [0, 0, 0, 0]
    except Exception:
        parts = [0, 0, 0, 0]
    b0, b1, b2, b3 = parts
    return struct.pack(">H", 2) + struct.pack(
        "<HBBBB8s", port & 0xFFFF, b3, b2, b1, b0, b"\x00" * 8
    )


def unpack_sockaddr_in_le(data: bytes) -> tuple[str, int]:
    """Unpack 16-byte PPPP sockaddr_in_le into (ip_str, port)."""
    if len(data) < 8:
        return ("0.0.0.0", 0)
    port_le, b3, b2, b1, b0 = struct.unpack("<HBBBB", data[2:8])
    return (f"{b0}.{b1}.{b2}.{b3}", port_le)


def build_wan_hello(p2p_key: bytes) -> bytes:
    """Build 0xF1 0x00 0x00 0x00 (MSG_HELLO) encrypted with P2P_Proprietary."""
    return p2p_proprietary_encrypt(p2p_key, b"\xf1\x00\x00\x00")


def build_wan_p2p_req(bin_did: bytes, local_ip: str, local_port: int, p2p_key: bytes) -> bytes:
    """Build 0xF1 0x20 0x00 0x24 + bin_did(20B) + sockaddr_in_le(16B) encrypted."""
    payload = bin_did[:20].ljust(20, b"\x00") + pack_sockaddr_in_le(local_ip, local_port)
    return p2p_proprietary_encrypt(p2p_key, b"\xf1\x20\x00\x24" + payload)


def build_wan_punch_pkt(bin_did: bytes, p2p_key: bytes) -> bytes:
    """Build 0xF1 0x41 0x00 0x14 + bin_did(20B) encrypted."""
    return p2p_proprietary_encrypt(
        p2p_key, b"\xf1\x41\x00\x14" + bin_did[:20].ljust(20, b"\x00")
    )


def build_wan_p2p_rdy(bin_did: bytes, p2p_key: bytes) -> bytes:
    """Build 0xF1 0x42 0x00 0x14 + bin_did(20B) encrypted."""
    return p2p_proprietary_encrypt(
        p2p_key, b"\xf1\x42\x00\x14" + bin_did[:20].ljust(20, b"\x00")
    )


def build_wan_list_req(bin_did: bytes, p2p_key: bytes) -> bytes:
    """Build 0xF1 0x67 0x00 0x14 + bin_did(20B) encrypted (requests relay allocation)."""
    return p2p_proprietary_encrypt(
        p2p_key, b"\xf1\x67\x00\x14" + bin_did[:20].ljust(20, b"\x00")
    )


def build_wan_rly_pkt(magic_word: bytes, bin_did: bytes, p2p_key: bytes) -> bytes:
    """Build 0xF1 0x83 0x00 0x1C + magic_word(4B) + bin_did(20B) + 4B zero padding encrypted."""
    payload = (
        magic_word[:4].ljust(4, b"\x00")
        + bin_did[:20].ljust(20, b"\x00")
        + b"\x00\x00\x00\x00"
    )
    return p2p_proprietary_encrypt(p2p_key, b"\xf1\x83\x00\x1c" + payload)


def get_local_ip_for_wan() -> str:
    """Determine the local IPv4 address used for outbound WAN traffic."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class _WanProbeProtocol(asyncio.DatagramProtocol):
    """Lightweight UDP protocol for standalone discover_camera_wan()."""

    def __init__(self, bin_did: bytes, p2p_key: bytes, servers: list[tuple[str, int]]):
        self.bin_did = bin_did
        self.p2p_key = p2p_key
        self.servers = set(servers)
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.found_event = asyncio.Event()
        self.offline_count = 0
        self.online_seen = False
        self.wan_cloud_status: str = "IDLE"
        self.punch_targets: set[tuple[str, int]] = set()
        self.peer_addr: Optional[tuple[str, int]] = None
        self.connection_mode: str = "WAN_P2P"

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple):
        if len(data) < 4:
            return
        dec = p2p_proprietary_decrypt(self.p2p_key, data)
        if dec[0] != 0xF1:
            return
        mtype = dec[1]

        if addr[1] == WAN_SERVER_PORT or addr in self.servers:
            if mtype == MSG_WAN_HELLO_ACK and len(dec) >= 20:
                wan_ip, wan_port = unpack_sockaddr_in_le(dec[4:20])
                log.debug("WAN probe HELLO_ACK from %s: external=%s:%d", addr[0], wan_ip, wan_port)
            elif mtype == MSG_WAN_P2P_REQ_ACK and len(dec) >= 5:
                status = dec[4]
                if status == 0x00:
                    self.online_seen = True
                    if self.wan_cloud_status != "CONNECTED":
                        self.wan_cloud_status = "ONLINE_PUNCHING"
                    log.info("WAN server %s reports camera is ONLINE (0x00)", addr[0])
                elif status == 0xFE:
                    self.offline_count += 1
                    if not self.online_seen and self.wan_cloud_status != "CONNECTED":
                        self.wan_cloud_status = "WAITING_FOR_WIFI"
                    log.debug("WAN server %s reports camera is OFFLINE (0xFE)", addr[0])
                elif status == 0xFF:
                    if self.wan_cloud_status != "CONNECTED":
                        self.wan_cloud_status = "INVALID_UID"
                    log.warning("WAN server %s reports INVALID_UID (0xFF)", addr[0])
            elif mtype == MSG_WAN_PUNCH_TO and len(dec) >= 20:
                self.online_seen = True
                if self.wan_cloud_status != "CONNECTED":
                    self.wan_cloud_status = "ONLINE_PUNCHING"
                tgt_ip, tgt_port = unpack_sockaddr_in_le(dec[4:20])
                if tgt_ip != "0.0.0.0" and tgt_port > 0:
                    self.punch_targets.add((tgt_ip, tgt_port))
                    log.info("WAN server %s sent PUNCH_TO -> %s:%d", addr[0], tgt_ip, tgt_port)
                    if self.transport and not self.transport.is_closing():
                        pkt = build_wan_punch_pkt(self.bin_did, self.p2p_key)
                        for _ in range(3):
                            self.transport.sendto(pkt, (tgt_ip, tgt_port))
            elif mtype == MSG_WAN_RLY_TO and len(dec) >= 24:
                self.online_seen = True
                if self.wan_cloud_status != "CONNECTED":
                    self.wan_cloud_status = "ONLINE_PUNCHING"
                rly_ip, rly_port = unpack_sockaddr_in_le(dec[4:20])
                magic = dec[20:24]
                log.info("WAN server %s sent RLY_TO -> %s:%d", addr[0], rly_ip, rly_port)
                if rly_ip != "0.0.0.0" and rly_port > 0 and self.transport and not self.transport.is_closing():
                    pkt = build_wan_rly_pkt(magic, self.bin_did, self.p2p_key)
                    for _ in range(3):
                        self.transport.sendto(pkt, (rly_ip, rly_port))
            return

        # Packet from camera or relay
        if mtype in (MSG_WAN_PUNCH_PKT, MSG_WAN_P2P_RDY):
            if len(dec) >= 24 and dec[4:24] != self.bin_did:
                return
            self.peer_addr = addr
            self.connection_mode = "WAN_P2P"
            self.wan_cloud_status = "CONNECTED"
            log.info("WAN P2P hole-punch succeeded with camera at %s:%d (msg=0x%02X)", addr[0], addr[1], mtype)
            self.found_event.set()
        elif mtype == MSG_WAN_RLY_RDY:
            self.peer_addr = addr
            self.connection_mode = "WAN_RELAY"
            self.wan_cloud_status = "CONNECTED"
            log.info("WAN Cloud Relay ready at %s:%d (msg=0x84)", addr[0], addr[1])
            self.found_event.set()


async def discover_camera_wan(did_or_uid_hex: str, timeout: float = 5.0):
    """Probe the VStarcam P2P cloud servers for `did_or_uid_hex` and return a
    `CameraInfo` if the camera is online and reachable via UDP hole punch or relay."""
    import eye4_rtsp_proxy as proxy

    cam = proxy.CameraInfo.from_did(did_or_uid_hex)
    if not cam.bin_did or not cam.wan_servers:
        return None

    _, psk_bytes = get_p2p_servers_for_did(cam.hw_deviceid or did_or_uid_hex)
    p2p_key = p2p_derive_key(psk_bytes)

    loop = asyncio.get_event_loop()
    probe = _WanProbeProtocol(cam.bin_did, p2p_key, cam.wan_servers)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: probe, local_addr=("0.0.0.0", 0)
    )

    try:
        local_ip = get_local_ip_for_wan()
        sockname = transport.get_extra_info("sockname")
        local_port = sockname[1] if sockname else 0

        hello_pkt = build_wan_hello(p2p_key)
        req_pkt = build_wan_p2p_req(cam.bin_did, local_ip, local_port, p2p_key)
        punch_pkt = build_wan_punch_pkt(cam.bin_did, p2p_key)
        list_req_pkt = build_wan_list_req(cam.bin_did, p2p_key)

        for srv in cam.wan_servers:
            transport.sendto(hello_pkt, srv)
            transport.sendto(req_pkt, srv)

        start_t = loop.time()
        resent_req = False
        sent_relay = False

        while (loop.time() - start_t) < timeout:
            if probe.found_event.is_set():
                break
            elapsed = loop.time() - start_t
            if probe.punch_targets:
                for tgt in list(probe.punch_targets):
                    transport.sendto(punch_pkt, tgt)
            if elapsed >= 1.5 and not resent_req:
                resent_req = True
                for srv in cam.wan_servers:
                    transport.sendto(req_pkt, srv)
            if elapsed >= 2.8 and not sent_relay:
                sent_relay = True
                for srv in cam.wan_servers:
                    transport.sendto(list_req_pkt, srv)
            await asyncio.sleep(0.2)

        if probe.peer_addr:
            cam.ip, cam.port = probe.peer_addr
            cam.discovery_port = probe.peer_addr[1]
            cam.connection_mode = probe.connection_mode
            cam.wan_cloud_status = probe.wan_cloud_status or "CONNECTED"
            return cam
        elif probe.online_seen and probe.punch_targets:
            tgt = next(iter(probe.punch_targets))
            cam.ip, cam.port = tgt
            cam.discovery_port = tgt[1]
            cam.connection_mode = "WAN_P2P"
            cam.wan_cloud_status = probe.wan_cloud_status or "ONLINE_PUNCHING"
            return cam
        return None
    finally:
        transport.close()
