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

import sounddevice as sd
from pyogg import OpusDecoder, OpusEncoder

MAGIC = 0x314B5457  # "WTK1"
VERSION = 1
FLAG_AUDIO = 0x01
HDR = struct.Struct("<IHBB")  # magic, seq, flags, version

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
      tx_count     - datagrams sent
      rx_count     - datagrams received
      last_rx_time - time.monotonic() of the last packet from the device
      last_tx_pcm  - most recent 20 ms mic frame (bytes, int16 mono)
      last_rx_pcm  - most recent 20 ms decoded device frame (bytes)
      jb_depth()   - current jitter buffer depth in frames
    """

    def __init__(self, target):
        self.target = target
        self.muted = False
        self.tx_count = 0
        self.rx_count = 0
        self.last_rx_time = 0.0
        self.last_tx_pcm = b""
        self.last_rx_pcm = b""

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self.sock.settimeout(1.0)

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

    def jb_depth(self):
        return self.jb.depth()

    def start(self):
        for f in (self._tx_loop, self._rx_loop, self._play_loop):
            t = threading.Thread(target=f, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self.sock.close()

    def _tx_loop(self):
        seq = 0
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                               blocksize=FRAME) as mic:
            while not self._stop.is_set():
                pcm, _ = mic.read(FRAME)
                pcm = bytes(pcm)
                self.last_tx_pcm = pcm
                if self.muted:
                    continue
                packet = self.enc.encode(pcm)
                hdr = HDR.pack(MAGIC, seq & 0xFFFF, FLAG_AUDIO, VERSION)
                try:
                    self.sock.sendto(hdr + bytes(packet), self.target)
                except OSError:
                    break
                seq += 1
                self.tx_count += 1

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) <= HDR.size:
                continue
            magic, seq, flags, version = HDR.unpack_from(data)
            if magic != MAGIC or version != VERSION:
                continue
            self.last_rx_time = time.monotonic()
            self.rx_count += 1
            if flags & FLAG_AUDIO:
                self.jb.insert(seq, data[HDR.size:])

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
                    pcm = bytes(self.dec.decode(memoryview(bytearray(pkt))))
                    if len(pcm) != FRAME * 2:
                        pcm = silence
                else:
                    misses += 1
                    pcm = silence
                    if misses > LOSS_RESET:
                        self.jb.clear()
                        playing = False
                self.last_rx_pcm = pcm
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
