#!/usr/bin/env python3
"""PC counterpart for the ReSpeaker Lite walkie-talkie.

Speaks the same protocol as the firmware: one UDP datagram per 20 ms frame,
8-byte header (magic 'WTK1', seq, flags, version) + a 48 kHz mono Opus packet.
The device adopts whoever sends it valid packets, so this client just needs
the device's address - found automatically via mDNS, or pass --ip.

Usage:
    python walkie_pc.py            # discover the device via mDNS
    python walkie_pc.py --ip 192.168.1.42

For the graphical version run walkie_gui.py instead.

Use headphones: unlike the device (XMOS AEC), your PC does no echo
cancellation, so open speakers will feed your mic.
"""

import argparse
import socket
import struct
import sys
import threading
import time

from array import array

import sounddevice as sd
from pyogg import OpusDecoder, OpusEncoder

MAGIC = 0x314B5457  # "WTK1"
VERSION = 2
FLAG_AUDIO = 0x01
HDR = struct.Struct("<IHBB")  # magic, seq, flags, version

# v2 batching: several [u16 len][opus frame] entries per datagram, hdr.seq
# is the first frame's; keeps packet rate below router flood-protection
# thresholds (50 pps of tiny UDP looks like an attack to some routers).
FRAMES_PER_PKT = 3

RATE = 48000
FRAME = 960  # 20 ms
PORT = 5004

JB_PREFILL = 3
JB_MAX_AHEAD = 32
LOSS_RESET = 25


def discover_device(timeout=10.0):
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found = {}
    event = threading.Event()

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if info and info.addresses:
                found["ip"] = socket.inet_ntoa(info.addresses[0])
                found["port"] = info.port
                found["name"] = name.split("._")[0]
                event.set()

        def update_service(self, zc, type_, name):
            pass

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, "_walkie._udp.local.", Listener())
    try:
        if not event.wait(timeout):
            return None
        return found
    finally:
        zc.close()


def seq_dist(a, b):
    """Signed 16-bit distance a-b with wraparound."""
    d = (a - b) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


class Jitter:
    """Sequence-indexed store of encoded packets, mirroring the firmware."""

    def __init__(self):
        self.lock = threading.Lock()
        self.slots = {}
        self.head = 0

    def insert(self, seq, data):
        with self.lock:
            if self.slots and abs(seq_dist(seq, self.head)) > JB_MAX_AHEAD:
                self.slots.clear()
            self.slots[seq] = data
            if len(self.slots) == 1 or seq_dist(seq, self.head) > 0:
                self.head = seq

    def take(self, seq):
        with self.lock:
            return self.slots.pop(seq, None)

    def depth(self):
        with self.lock:
            return len(self.slots)

    def head_seq(self):
        with self.lock:
            return self.head

    def clear(self):
        with self.lock:
            self.slots.clear()


class WalkieClient:
    """Runs the three audio/network loops; exposes state for UIs.

    Readable attributes (updated continuously, safe to read from any thread):
      muted        - set True/False to stop/resume sending mic audio
      rx_volume    - playback gain for the PC speakers (1.0 = unity, may
                     exceed 1.0; output is clamped to int16)
      tx_count     - datagrams sent
      rx_count     - datagrams received
      last_rx_time - time.monotonic() of the last packet from the device
      last_tx_pcm  - most recent 20 ms mic frame (bytes, int16 mono)
      last_rx_pcm  - most recent 20 ms decoded device frame (bytes)
      jb_depth()   - current jitter buffer depth in frames
    """

    def __init__(self, target, sock=None, external_rx=False):
        """Audio transport is TCP on target_port+1 (this router forwards TCP
        perfectly while randomly blackholing UDP flows); falls back to the
        v2 UDP protocol if the TCP connect fails (older firmware).

        sock/external_rx: share an existing UDP socket whose flow the router
        already forwards - the owner then feeds incoming audio via
        handle_audio() instead of our own rx loop. UDP fallback only."""
        self.target = target
        self.build_tag = None  # device firmware id from its TCP banner
        self.tcp = self._tcp_connect(timeout=3.0)  # None -> UDP fallback
        self.muted = False
        self.rx_volume = 1.0
        self.tx_count = 0
        self.rx_count = 0
        self.last_rx_time = 0.0
        self.last_tx_pcm = b""
        self.last_rx_pcm = b""
        self._own_sock = sock is None
        self._external_rx = external_rx

        if sock is not None:
            self.sock = sock
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("", 0))
            self.sock.settimeout(1.0)
        self.rebinds = 0
        self._last_rebind = time.monotonic()

        self.enc = OpusEncoder()
        self.enc.set_application("voip")
        self.enc.set_sampling_frequency(RATE)
        self.enc.set_channels(1)

        self.dec = OpusDecoder()
        self.dec.set_channels(1)
        self.dec.set_sampling_frequency(RATE)

        self.jb = Jitter()
        self._stop = threading.Event()
        self._threads = []

    def linked(self):
        return time.monotonic() - self.last_rx_time < 3.0

    def _rebind(self):
        """Move to a fresh source port: some routers' flow-offload engines
        blackhole individual UDP flows; a new 5-tuple gets a fresh chance
        (the device re-adopts us from the first packet it hears)."""
        old = self.sock
        new = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        new.bind(("", 0))
        new.settimeout(1.0)
        self.sock = new
        try:
            old.close()
        except OSError:
            pass
        self.rebinds += 1
        self._last_rebind = time.monotonic()

    def jb_depth(self):
        return self.jb.depth()

    def start(self):
        if self.tcp is not None:
            loops = [self._tcp_tx_loop, self._tcp_rx_loop, self._play_loop]
        else:
            loops = [self._tx_loop, self._play_loop]
            if not self._external_rx:
                loops.append(self._rx_loop)
        for f in loops:
            t = threading.Thread(target=f, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        if self.tcp is not None:
            try:
                self.tcp.close()
            except OSError:
                pass
        if self._own_sock:
            self.sock.close()

    # ---- TCP transport: [u16 len][opus frame] stream, len 0 = heartbeat

    def _tcp_connect(self, timeout=2.0):
        try:
            t = socket.create_connection((self.target[0], self.target[1] + 6),
                                         timeout=timeout)
            t.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            t.settimeout(1.0)
            return t
        except OSError:
            return None

    def _tcp_reconnect(self):
        """Replace a dead connection; returns once connected or stopping."""
        try:
            self.tcp.close()
        except OSError:
            pass
        while not self._stop.is_set():
            t = self._tcp_connect()
            if t is not None:
                self.tcp = t
                self.jb.clear()
                self.reconnects = getattr(self, "reconnects", 0) + 1
                return
            time.sleep(1.0)

    def _tcp_tx_loop(self):
        last_hb = 0.0
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                               blocksize=FRAME) as mic:
            while not self._stop.is_set():
                pcm, _ = mic.read(FRAME)
                pcm = bytes(pcm)
                self.last_tx_pcm = pcm
                if self.muted:
                    # heartbeat so the device knows we're alive, not a zombie
                    now = time.monotonic()
                    if now - last_hb > 0.5:
                        last_hb = now
                        try:
                            self.tcp.sendall(b"\x00\x00")
                        except OSError:
                            pass
                    continue
                packet = bytes(self.enc.encode(pcm))
                try:
                    self.tcp.sendall(struct.pack("<H", len(packet)) + packet)
                except OSError:
                    if self._stop.is_set():
                        break
                    time.sleep(0.1)
                    continue
                self.tx_count += 1

    def _tcp_recv_all(self, n):
        buf = b""
        while len(buf) < n and not self._stop.is_set():
            try:
                chunk = self.tcp.recv(n - len(buf))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf if len(buf) == n else None

    def _tcp_rx_loop(self):
        seq = 0
        while not self._stop.is_set():
            hdr = self._tcp_recv_all(2)
            if hdr is None:
                self._tcp_reconnect()
                continue
            (flen,) = struct.unpack("<H", hdr)
            if flen > 512:  # desynced/corrupt stream: start over
                self._tcp_reconnect()
                continue
            self.last_rx_time = time.monotonic()
            if flen == 0:
                continue  # heartbeat
            frame = self._tcp_recv_all(flen)
            if frame is None:
                self._tcp_reconnect()
                continue
            if frame.startswith(b"WTKI"):
                self.build_tag = frame.decode(errors="replace")
                continue  # identity banner, not audio
            self.rx_count += 1
            self.jb.insert(seq, frame)
            seq = (seq + 1) & 0xFFFF

    def handle_audio(self, seq, body):
        """Feed a received audio payload (v2 batch, header stripped)."""
        self.last_rx_time = time.monotonic()
        self.rx_count += 1
        pos = 0
        while pos + 2 < len(body):
            (flen,) = struct.unpack_from("<H", body, pos)
            pos += 2
            if flen == 0 or pos + flen > len(body):
                break
            self.jb.insert(seq, body[pos:pos + flen])
            seq = (seq + 1) & 0xFFFF
            pos += flen

    def _tx_loop(self):
        seq = 0
        batch = []
        batch_seq = 0
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                               blocksize=FRAME) as mic:
            while not self._stop.is_set():
                pcm, _ = mic.read(FRAME)
                pcm = bytes(pcm)
                self.last_tx_pcm = pcm
                if self.muted:
                    batch.clear()
                    continue
                packet = bytes(self.enc.encode(pcm))
                if not batch:
                    batch_seq = seq
                batch.append(struct.pack("<H", len(packet)) + packet)
                seq += 1
                if len(batch) < FRAMES_PER_PKT:
                    continue
                hdr = HDR.pack(MAGIC, batch_seq & 0xFFFF, FLAG_AUDIO, VERSION)
                try:
                    self.sock.sendto(hdr + b"".join(batch), self.target)
                except OSError:
                    if self._stop.is_set():
                        break
                self.tx_count += len(batch)
                batch.clear()
                # flow watchdog: sending but hearing nothing -> new flow
                # (own socket only; a shared socket's flow is managed by
                # its owner and is already known-good)
                now = time.monotonic()
                if (self._own_sock and now - self.last_rx_time > 2.5 and
                        now - self._last_rebind > 2.5):
                    self._rebind()

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.05)  # socket was rebound; pick up the new one
                continue
            if len(data) <= HDR.size:
                continue
            magic, seq, flags, version = HDR.unpack_from(data)
            if magic != MAGIC or version != VERSION:
                continue
            if flags & FLAG_AUDIO:
                self.handle_audio(seq, data[HDR.size:])
            else:
                self.last_rx_time = time.monotonic()
                self.rx_count += 1

    def _play_loop(self):
        silence = bytes(FRAME * 2)
        playing = False
        expect = 0
        misses = 0
        with sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16",
                                blocksize=FRAME) as spk:
            while not self._stop.is_set():
                if not playing:
                    if self.jb.depth() >= JB_PREFILL:
                        expect = (self.jb.head_seq() - JB_PREFILL + 1) & 0xFFFF
                        playing = True
                        misses = 0
                    else:
                        self.last_rx_pcm = b""
                        spk.write(silence)
                        continue
                pkt = self.jb.take(expect)
                expect = (expect + 1) & 0xFFFF
                if pkt is not None:
                    misses = 0
                    try:
                        pcm = bytes(self.dec.decode(memoryview(bytearray(pkt))))
                    except Exception:
                        pcm = silence  # never let one bad packet kill playback
                    if len(pcm) != FRAME * 2:
                        pcm = silence
                else:
                    misses += 1
                    pcm = silence
                    if misses > LOSS_RESET:
                        self.jb.clear()
                        playing = False
                # waveform shows the device's signal regardless of local gain
                self.last_rx_pcm = pcm
                vol = self.rx_volume
                if vol != 1.0 and pcm is not silence:
                    samples = array("h")
                    samples.frombytes(pcm)
                    pcm = array("h", (
                        min(32767, max(-32768, int(s * vol)))
                        for s in samples)).tobytes()
                spk.write(pcm)


def resolve_target(ip=None, port=PORT, timeout=10.0):
    """Return ((ip, port), name) from --ip or mDNS discovery."""
    if ip:
        return (ip, port), ip
    dev = discover_device(timeout)
    if not dev:
        return None, None
    return (dev["ip"], dev["port"]), dev["name"]


def main():
    ap = argparse.ArgumentParser(description="PC walkie-talkie client")
    ap.add_argument("--ip", help="device IP (skip mDNS discovery)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if not args.ip:
        print("searching for a walkie unit via mDNS...")
    target, name = resolve_target(args.ip, args.port)
    if not target:
        sys.exit("no _walkie._udp device found - is it on the network? (try --ip)")
    print(f"found {name} at {target[0]}:{target[1]}")

    client = WalkieClient(target)
    client.start()
    print("linked - talking full duplex. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()
