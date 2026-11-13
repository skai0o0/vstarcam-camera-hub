#!/usr/bin/env python3
"""
Command-channel reliability test (no real camera needed).

Spins up a fake PPPP camera on localhost that behaves like the real firmware on
the command channel: it ACKs every DRW it receives but only *executes* command
index N after it has received every index below N (ordered, reliable stream).
The link between proxy and camera randomly drops packets in both directions,
like a WAN P2P / Cloud Relay path does.

Asserts that every CGI command (PTZ, IR, flip, ...) is executed exactly once and
in order despite the loss — the bug this guards against is fire-and-forget
sending, where one lost packet parks every later command forever.

    python3 test_cmd_reliability.py
"""

import asyncio
import random
import struct
import sys

import eye4_rtsp_proxy as proxy

LOSS = 0.30          # 30% loss each way — far worse than a real WAN link
KEY = proxy.p2p_derive_key(b"vstarcam2019")


class FakeCamera(asyncio.DatagramProtocol):
    def __init__(self, rng):
        self.rng = rng
        self.transport = None
        self.buffered = {}        # idx -> cgi text (received, not yet executable)
        self.next_idx = 0
        self.executed = []        # cgi texts in execution order
        self.rx = 0
        self.dropped = 0
        self.resp_idx = 0

    def connection_made(self, transport):
        self.transport = transport

    def _send(self, pkt, addr):
        if self.rng.random() < LOSS:
            self.dropped += 1
            return
        self.transport.sendto(pkt, addr)

    def datagram_received(self, data, addr):
        if self.rng.random() < LOSS:      # lost on the way to the camera
            self.dropped += 1
            return
        parsed = proxy.parse_drw_packet(proxy.p2p_proprietary_decrypt(KEY, data))
        if not parsed or parsed["channel"] != proxy.CH_CMD:
            return
        self.rx += 1
        idx = parsed["index"]
        # ACK everything we receive, duplicates included
        self._send(proxy.build_drw_ack(proxy.CH_CMD, idx, KEY, proxy.ENC_P2P), addr)
        if idx < self.next_idx or idx in self.buffered:
            return
        payload = parsed["payload"]
        n = struct.unpack("<I", payload[4:8])[0]
        self.buffered[idx] = payload[8:8 + n].decode("ascii")
        while self.next_idx in self.buffered:
            cgi = self.buffered.pop(self.next_idx)
            self.next_idx += 1
            self.executed.append(cgi)
            if "get_camera_params.cgi" in cgi:
                body = 'var resolution=0;\r\nvar vbright=128;\r\nvar flip=3;\r\nvar ircut=1;\r\nvar alias="Phong khach";\r\n'
                text = body.encode("ascii")
                # Reply header as sent by live firmware: 01 0A <id16> <len16> 00 01
                resp = b"\x01\x0a" + struct.pack("<HH", 0x6003, len(text)) + b"\x00\x01" + text
                # Split across two DRWs mid-token to exercise response reassembly
                for part in (resp[:31], resp[31:]):
                    pkt = proxy.build_drw_packet(proxy.CH_CMD, self.resp_idx, part, KEY, proxy.ENC_P2P)
                    self.resp_idx += 1
                    self.transport.sendto(pkt, addr)   # responses: lossless (camera retransmits in reality)


async def main():
    rng = random.Random(1234)
    loop = asyncio.get_event_loop()
    cam = FakeCamera(rng)
    cam_tr, _ = await loop.create_datagram_endpoint(lambda: cam, local_addr=("127.0.0.1", 0))
    cam_addr = cam_tr.get_extra_info("sockname")

    proto = proxy.PPPPUnifiedProtocol(username="admin", password="888888",
                                      p2p_key=KEY, enc_mode=proxy.ENC_P2P)
    tr, _ = await loop.create_datagram_endpoint(lambda: proto, local_addr=("127.0.0.1", 0))
    proto._drw_addr = cam_addr          # session already "established"

    # 1. A burst of 60 commands, like status polls interleaved with PTZ presses
    expected = []
    for i in range(20):
        await proto._send_login()                       # get_status poll
        expected.append("get_status.cgi")
        await proto.send_ptz_command(i % 8, onestep=1)  # move
        expected.append(f"decoder_control.cgi?command={i % 8}&")
        await proto.send_camera_param(5, i % 4)         # flip
        expected.append(f"camera_control.cgi?param=5&value={i % 4}&")
        await asyncio.sleep(0.01)

    # 2. A command we explicitly wait on (what the web UI does)
    acked = await proto.send_ptz_command(25, onestep=0, wait_ack=10.0)
    expected.append("decoder_control.cgi?command=25&")
    assert acked, "PTZ command was never ACKed"

    # 3. A query whose response is fragmented across DRW packets
    values = await proto.query_cgi("get_camera_params.cgi", timeout=10.0)
    expected.append("get_camera_params.cgi")
    assert values is not None, "query got no reply"

    for _ in range(200):
        if not proto._cmd_unacked:
            break
        await asyncio.sleep(0.05)

    assert not proto._cmd_unacked, f"{len(proto._cmd_unacked)} commands never ACKed"
    assert len(cam.executed) == len(expected), \
        f"camera executed {len(cam.executed)} of {len(expected)} commands"
    for got, want in zip(cam.executed, expected):
        assert want in got, f"out of order: got {got!r}, wanted {want!r}"
    assert values.get("vbright") == "128" and values.get("flip") == "3" \
        and values.get("ircut") == "1" and values.get("alias") == "Phong khach", values
    assert proto.cmd_stalled_seconds() == 0.0

    # 4. parse_drw_ack returns every index of a batched ACK
    batch = proxy.p2p_proprietary_decrypt(
        KEY, proxy.build_drw_ack_batch(0, [7, 8, 300], KEY, proxy.ENC_P2P))
    assert proxy.parse_drw_ack(batch)["indices"] == [7, 8, 300]

    print(f"OK: {len(cam.executed)} commands executed in order "
          f"with {LOSS:.0%} loss each way "
          f"({cam.dropped} packets dropped, {proto._cmd_retx_count} retransmits)")

    await proto.stop()
    cam_tr.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e)
        sys.exit(1)
