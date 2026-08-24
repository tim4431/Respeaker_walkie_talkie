#!/usr/bin/env python3
"""Walkie group control center.

Top: one card per node (walkie units and this PC alike) with live status,
mic waveform, mute/TX-mode/volume controls. Bottom: group chat editor -
drag a node card into a group box; a node may be in several groups.

Groups are a GUI-side structure (gui_settings.json). What each device
actually stores (NVS, survives reboots, works with the GUI closed) is its
resulting send-list: the union of everyone it shares a group with. The GUI
keeps device send-lists reconciled with the group layout automatically.

    python walkie_gui.py
"""

import json
import math
import re
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
import tkinter.simpledialog
from array import array
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLASH_SCRIPT = REPO_ROOT / "tools" / "flash_all.ps1"

from walkie_pc import (HDR, MAGIC, PORT, VERSION, WalkieClient, vox_rms_of)

SETTINGS_PATH = Path(__file__).with_name("gui_settings.json")

FLAG_CTRL = 0x02
CTRL_STATUS_REQ = 0x01
CTRL_STATUS_RSP = 0x02
CTRL_SET_VOL = 0x04
CTRL_SET_MODE = 0x05
CTRL_SET_MUTE = 0x06
CTRL_SET_GROUP = 0x07
CTRL_SET_THRESH = 0x08
CTRL_SET_GAIN = 0x09
CTRL_SET_AGC = 0x0A
STATUS = struct.Struct("<BBBBBbHI24s")   # wt_status_t base (36 B); the
MEMBER = struct.Struct("<IH")            # extension fields are optional

MAX_MEMBERS = 8

# ---------------------------------------------------------------- theme

BG      = "#eaedf4"
CARD    = "#ffffff"
CARD2   = "#f5f7fc"   # nested panels (group boxes)
SEL     = "#dbe4ff"
FG      = "#141a2a"
DIM     = "#535b73"
BORDER  = "#ccd4e3"
ACCENT  = "#4263eb"
TROUGH  = "#c7d0e2"
BTN_BG  = "#e9edf6"
BTN_ON  = "#4263eb"

TX_COLOR = "#e8590c"
RX_COLOR = "#099268"

COLORS = {
    "offline": "#9aa2b1",
    "idle": "#339af0",
    "linked": "#2f9e44",
    "muted": "#ae3ec9",
}

FONT = "Segoe UI"

# Button icons. Emoji are non-BMP characters, which Tk renders correctly
# from 8.6.12 on (Windows); main() swaps in the plain-text set on older Tk.
ICONS = {"mic": "🎤", "mic_off": "🎤✕", "spk": "🔊", "spk_off": "🔇",
         "call": "📞 Call", "end": "✕ End"}
ICONS_TEXT = {"mic": "Mic", "mic_off": "Mic ✕", "spk": "Spk",
              "spk_off": "Spk ✕", "call": "Call", "end": "End"}


def load_settings():
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(cfg):
    try:
        SETTINGS_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def local_ip_toward(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# ---------------------------------------------------------------- monitor

class Monitor:
    """Discovers walkie units and keeps their status fresh."""

    def __init__(self):
        self.devices = {}  # ip -> dict(name, port, status, last_rsp)
        self.lock = threading.Lock()
        self.active_client = None  # WalkieClient sharing our socket/flow
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self.sock.settimeout(0.5)
        self._stop = threading.Event()
        threading.Thread(target=self._browse, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._recv, daemon=True).start()
        threading.Thread(target=self._presence, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _presence(self):
        """Discover devices from their 2 s presence broadcasts on :5004 -
        these arrive even when mDNS and unicast are being dropped."""
        try:
            ps = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ps.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ps.bind(("", PORT))
            ps.settimeout(1.0)
        except OSError:
            return  # port busy (another client?) - mDNS still works
        while not self._stop.is_set():
            try:
                data, addr = ps.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < HDR.size:
                continue
            magic, _, _, version = HDR.unpack_from(data)
            if magic != MAGIC or version != VERSION:
                continue
            with self.lock:
                d = self.devices.setdefault(addr[0], {})
                d.setdefault("name", addr[0])
                d.setdefault("port", PORT)
        ps.close()

    def _browse(self):
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        monitor = self

        class Listener(ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    with monitor.lock:
                        d = monitor.devices.setdefault(ip, {})
                        d["name"] = name.split("._")[0]
                        d["port"] = info.port or PORT
            update_service = add_service

            def remove_service(self, zc, type_, name):
                pass  # keep the row; status polling will mark it offline

        zc = Zeroconf()
        ServiceBrowser(zc, "_walkie._udp.local.", Listener())
        self._stop.wait()
        zc.close()

    def _poll(self):
        req = HDR.pack(MAGIC, 0, FLAG_CTRL, VERSION) + bytes([CTRL_STATUS_REQ])
        last_rebind = time.monotonic()
        while not self._stop.is_set():
            with self.lock:
                targets = [(ip, d.get("port", PORT)) for ip, d in self.devices.items()]
                now = time.monotonic()
                stale = [ip for ip, d in self.devices.items()
                         if d.get("last_rsp") and now - d["last_rsp"] > 5]
            # never yank the socket while audio/group traffic rides this flow
            if stale and now - last_rebind > 6 and self.active_client is None:
                old = self.sock
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.bind(("", 0))
                self.sock.settimeout(0.5)
                try:
                    old.close()
                except OSError:
                    pass
                last_rebind = now
            for ip, port in targets:
                try:
                    self.sock.sendto(req, (ip, port))
                except OSError:
                    pass
            time.sleep(0.8)

    @staticmethod
    def _parse_status(body):
        (_, _, muted, linked, locked, rssi,
         peer_port, peer_ip, host) = STATUS.unpack_from(body)
        st = {
            "muted": muted, "linked": linked, "locked": locked,
            "rssi": rssi, "volume": None, "tx_mode": None,
            "tx_active": 0, "rx_active": 0, "members": [], "mic_level": 0,
            "vox_thresh": None, "mic_gain": None, "mic_agc": None,
            "agc_gain": None,
        }
        off = STATUS.size
        for i, n in enumerate(("volume", "tx_mode", "tx_active", "rx_active")):
            if len(body) > off + i:
                st[n] = body[off + i]
        mc_off = off + 4
        count = body[mc_off] if len(body) > mc_off else 0
        p = mc_off + 1
        for _ in range(count):
            if len(body) >= p + MEMBER.size:
                mip, mport = MEMBER.unpack_from(body, p)
                st["members"].append(
                    (socket.inet_ntoa(struct.pack("<I", mip)),
                     socket.ntohs(mport)))
            p += MEMBER.size
        lvl_off = mc_off + 1 + MAX_MEMBERS * MEMBER.size
        if len(body) > lvl_off:
            st["mic_level"] = body[lvl_off]
        if len(body) > lvl_off + 1:
            st["vox_thresh"] = body[lvl_off + 1]
        if len(body) > lvl_off + 2:
            st["mic_gain"] = body[lvl_off + 2]
        if len(body) > lvl_off + 3:
            st["mic_agc"] = body[lvl_off + 3]
        if len(body) > lvl_off + 4:
            st["agc_gain"] = body[lvl_off + 4] / 16.0
        host_s = host.split(b"\0")[0].decode(errors="replace")
        return host_s, st

    def _recv(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.05)  # socket was rebound; pick up the new one
                continue
            if len(data) < HDR.size + 1:
                continue
            magic, seq, flags, version = HDR.unpack_from(data)
            if magic != MAGIC or version != VERSION:
                continue
            client = self.active_client
            if flags & 0x01:  # audio (group traffic or a shared-flow call)
                if client and client.accepts(addr[0]):
                    client.handle_audio(seq, data[HDR.size:], src=addr)
                continue
            if not flags:  # keepalive: refresh the sender's liveness
                if client and client.accepts(addr[0]):
                    client.last_rx_time = time.monotonic()
                continue
            if not flags & FLAG_CTRL or len(data) < HDR.size + STATUS.size:
                continue
            body = data[HDR.size:]
            if body[0] != CTRL_STATUS_RSP:
                continue
            name, st = self._parse_status(body)
            with self.lock:
                d = self.devices.setdefault(addr[0], {"port": addr[1]})
                d["name"] = name or addr[0]
                d["status"] = st
                d["last_rsp"] = time.monotonic()

    def snapshot(self):
        with self.lock:
            return {ip: dict(d) for ip, d in self.devices.items()}

    # ---- control commands (each answered by a status we pick up in _recv)

    def _ctrl(self, dev_ip, dev_port, body):
        pkt = HDR.pack(MAGIC, 0, FLAG_CTRL, VERSION) + body
        try:
            self.sock.sendto(pkt, (dev_ip, dev_port))
        except OSError:
            pass

    def set_volume(self, ip, port, volume):
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_VOL,
                                         max(0, min(100, int(volume)))))

    def set_mode(self, ip, port, mode):
        """mode: 0 always, 1 voice (VAD), 2 plain level gate."""
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_MODE,
                                         max(0, min(2, int(mode)))))

    def set_mute(self, ip, port, muted):
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_MUTE, 1 if muted else 0))

    def set_thresh(self, ip, port, thresh):
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_THRESH,
                                         max(0, min(100, int(thresh)))))

    def set_gain(self, ip, port, gain):
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_GAIN,
                                         max(0, min(200, int(gain)))))

    def set_agc(self, ip, port, on):
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_AGC, 1 if on else 0))

    def set_group(self, ip, port, members):
        body = struct.pack("<BB", CTRL_SET_GROUP, len(members))
        for mip, mport in members:
            body += MEMBER.pack(struct.unpack("<I", socket.inet_aton(mip))[0],
                                socket.htons(mport))
        self._ctrl(ip, port, body)


def state_of(dev):
    st = dev.get("status")
    if not st or time.monotonic() - dev.get("last_rsp", 0) > 4:
        return "offline"
    if st["muted"]:
        return "muted"
    if st["linked"]:
        return "linked"
    return "idle"


# ---------------------------------------------------------------- widgets

def card(parent, bg=CARD):
    return tk.Frame(parent, bg=bg, highlightbackground=BORDER,
                    highlightthickness=1)


def flat_btn(parent, text, command, accent=False, small=False):
    return tk.Button(parent, text=text, command=command,
                     bg=BTN_ON if accent else BTN_BG,
                     fg="#ffffff" if accent else FG,
                     activebackground="#3b5bdb" if accent else "#dbe2f0",
                     activeforeground="#ffffff" if accent else FG,
                     disabledforeground="#a3aabf",
                     relief="flat", bd=0, padx=8 if small else 12,
                     pady=1 if small else 3,
                     font=(FONT, 8 if small else 10))


class MiniWave:
    """Small per-card level history."""

    BARS = 44

    def __init__(self, parent, color, height=40):
        self.h = height
        # explicit width: a Canvas defaults to 10 *centimeters*, which on a
        # high-DPI screen is ~760 px and blew every card up so wide that the
        # neighbours (and their buttons) fell off the window edge
        self.canvas = tk.Canvas(parent, height=height, width=236,
                                bg="#f4f6fb", highlightthickness=1,
                                highlightbackground=BORDER)
        self.levels = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.color = color
        self.bars = []
        self.gate = []      # threshold guide lines

    def push(self, level, thresh_level=None, gate_on=True):
        self.levels.append(max(0.0, min(1.0, level)))
        c = self.canvas
        w = max(c.winfo_width(), 60)
        mid = self.h / 2
        step = w / (self.BARS + 1)
        if len(self.bars) != self.BARS:
            c.delete("all")
            self.bars = [c.create_line(0, mid, 0, mid, fill=self.color,
                                       width=3, capstyle="round")
                         for _ in range(self.BARS)]
            self.gate = []
        span = mid - 4
        for i, lv in enumerate(self.levels):
            x = (i + 1) * step
            hh = max(1.2, lv * span)
            c.coords(self.bars[i], x, mid - hh, x, mid + hh)
        # dashed line showing where the voice gate opens
        if thresh_level is None:
            for g in self.gate:
                c.itemconfigure(g, state="hidden")
        else:
            gh = max(1.0, min(1.0, thresh_level) * span)
            if not self.gate:
                self.gate = [c.create_line(0, 0, 0, 0, dash=(3, 3))
                             for _ in range(2)]
            # solid grey while the gate is armed (voice mode), pale when the
            # node transmits unconditionally and the line is only a preview
            col = "#7c869c" if gate_on else "#cdd4e0"
            for g, y in zip(self.gate, (mid - gh, mid + gh)):
                c.coords(g, 0, y, w, y)
                c.itemconfigure(g, state="normal", fill=col)
            for g in self.gate:
                c.tag_raise(g)


def rms_to_level(rms):
    """Same curve pcm_level() uses, so a gate line lands where the bars do."""
    return min(1.0, math.sqrt(rms / 32768.0) * 1.8)


def peak_to_level(peak16):
    """Device levels are frame PEAKS, the PC's are RMS, so they get their own
    constant - but the same sqrt curve, or one card looks dead next to the
    other."""
    return min(1.0, math.sqrt(max(0.0, peak16) / 32768.0) * 1.2)


def dev_mic_level(mic_level):
    """wt_status_t.mic_level (peak16 >> 7) -> 0-1 for drawing."""
    return peak_to_level(int(mic_level) * 128)


def dev_gate_level(thresh):
    """Device VOX threshold knob -> the same 0-1 scale (WT_VOX_PEAK_OF)."""
    if thresh is None:
        return None
    return peak_to_level(150 + int(thresh) * int(thresh) * 2)


def pcm_level(pcm_bytes):
    if not pcm_bytes:
        return 0.0
    samples = array("h", pcm_bytes)
    acc = 0
    n = 0
    for i in range(0, len(samples), 4):
        s = samples[i]
        acc += s * s
        n += 1
    rms = math.sqrt(acc / max(n, 1)) / 32768.0
    return min(1.0, math.sqrt(rms) * 1.8)


class BarSlider:
    """High-contrast 0-100 slider: filled bar + knob, click/drag to set."""

    def __init__(self, parent, command=None, on_release=None,
                 width=110, height=18, color=ACCENT, vmax=100):
        self.canvas = tk.Canvas(parent, width=width, height=height,
                                bg=CARD2, highlightthickness=0)
        self.h = height
        self.color = color
        self.vmax = vmax
        self.value = 0
        self.command = command
        self.on_release = on_release
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._press)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _press(self, e):
        w = max(self.canvas.winfo_width(), 10)
        v = round(max(0.0, min(1.0, e.x / w)) * self.vmax)
        if v != self.value:
            self.value = v
            self._draw()
            if self.command:
                self.command(v)

    def _release(self, _e):
        if self.on_release:
            self.on_release(self.value)

    def set(self, v):
        self.value = max(0, min(self.vmax, int(v)))
        self._draw()

    def get(self):
        return self.value

    def _draw(self):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 10)
        h = self.h
        y0, y1 = 4, h - 4
        c.create_rectangle(1, y0, w - 2, y1, fill="#d4dae8",
                           outline="#aeb8cd")
        x = int((w - 4) * self.value / self.vmax) + 2
        if x > 2:
            c.create_rectangle(1, y0, x, y1, fill=self.color, outline="")
        c.create_rectangle(max(1, x - 3), 1, min(w - 2, x + 3), h - 1,
                           fill="#2b3a55", outline="#ffffff")


# ---------------------------------------------------------------- setup

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A walkie's XMOS in I2S mode is a *pure DFU-class* device: a device-level
# node (no "&MI_") whose CompatibleIds contain Class_FE. A ReSpeaker running
# USB-audio firmware (e.g. a PC microphone) is a composite device whose
# parent node also lacks "&MI_", so the Class_FE check is what tells them
# apart - same rule tools/flash_all.ps1 uses. REV_xxxx gives the firmware
# revision; 0110 is the 48 kHz I2S build this project needs.
XMOS_QUERY = (
    "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
    "Where-Object { $_.InstanceId -match 'VID_2886&PID_0019' } | "
    "ForEach-Object { $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId "
    "-KeyName DEVPKEY_Device_HardwareIds,DEVPKEY_Device_CompatibleIds "
    "-ErrorAction SilentlyContinue; "
    "\"$($_.InstanceId)`t$(($p | ForEach-Object { $_.Data }) -join ';')\" }"
)
XMOS_TARGET_REV = 0x0110


def probe_usb():
    """What is plugged in right now: XIAO COM port, XMOS DFU state, and
    whether a USB-audio ReSpeaker (the PC-mic unit) is present - dfu-util
    must never run while one is connected."""
    out = {"xiao_port": None, "xmos": False, "xmos_rev": None,
           "usb_audio": False, "error": None}
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid == 0x303A:
                out["xiao_port"] = p.device
                break
    except ImportError:
        out["error"] = "pyserial not installed"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", XMOS_QUERY],
                           capture_output=True, text=True, timeout=25,
                           creationflags=NO_WINDOW)
        for line in r.stdout.splitlines():
            if "VID_2886&PID_0019" not in line:
                continue
            inst, _, props = line.partition("\t")
            if "&MI_" in inst:
                out["usb_audio"] = True   # composite = USB-audio firmware
                continue
            if "Class_fe" not in props.lower():
                continue                  # composite parent, not a DFU device
            out["xmos"] = True
            m = re.search(r"REV_([0-9A-Fa-f]{4})", props)
            if m:
                out["xmos_rev"] = int(m.group(1), 16)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


class SetupWizard:
    """Guided first-time setup: detect -> flash -> WiFi -> verify."""

    STEPS = ("1 · Connect", "2 · Flash", "3 · WiFi", "4 · Done")

    def __init__(self, app, start=0):
        self.app = app
        self.step = start
        self.proc = None
        self.usb = {}
        self.win = win = tk.Toplevel(app.root)
        win.title("Set up a walkie device")
        win.configure(bg=CARD)
        win.geometry("620x520")
        win.transient(app.root)
        win.protocol("WM_DELETE_WINDOW", self.close)

        head = tk.Frame(win, bg=CARD)
        head.pack(fill="x", padx=16, pady=(14, 6))
        self.crumbs = []
        for i, name in enumerate(self.STEPS):
            lbl = tk.Label(head, text=name, bg=CARD, font=(FONT, 10),
                           padx=8, pady=3, cursor="hand2")
            lbl.pack(side="left", padx=(0, 4))
            lbl.bind("<Button-1>", lambda e, i=i: self.goto(i))
            self.crumbs.append(lbl)

        self.body = tk.Frame(win, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=16, pady=4)

        foot = tk.Frame(win, bg=CARD)
        foot.pack(fill="x", padx=16, pady=(4, 14))
        self.status = tk.Label(foot, text="", bg=CARD, fg=DIM, anchor="w",
                               justify="left", wraplength=380,
                               font=(FONT, 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.next_btn = flat_btn(foot, "Next", self.next_step, accent=True)
        self.next_btn.pack(side="right")
        self.back_btn = flat_btn(foot, "Back",
                                 lambda: self.goto(self.step - 1))
        self.back_btn.pack(side="right", padx=(0, 6))

        self.refresh_usb(async_=True)
        self.render()

    # ---- lifecycle

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.set_status("A flash is still running - it will keep going "
                            "in the background.")
        self.app.wizard = None
        self.win.destroy()

    def _ui(self, fn):
        """Run fn on the Tk thread. Worker threads outlive the window (a
        flash keeps going after Close), and a destroyed interpreter raises
        RuntimeError as well as TclError - both mean 'nothing to update'."""
        try:
            self.win.after(0, fn)
        except (tk.TclError, RuntimeError):
            pass

    def set_status(self, text):
        self._ui(lambda: self.status.config(text=text))

    def goto(self, i):
        self.step = max(0, min(len(self.STEPS) - 1, i))
        self.render()

    def next_step(self):
        if self.step >= len(self.STEPS) - 1:
            self.close()
        else:
            self.goto(self.step + 1)

    def refresh_usb(self, async_=False):
        def work():
            info = probe_usb()
            self.usb = info
            self._ui(self._usb_ready)
        if async_:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    def _usb_ready(self):
        if self.step in (0, 1, 2):
            self.render()

    # ---- rendering

    def render(self):
        for i, lbl in enumerate(self.crumbs):
            active = i == self.step
            lbl.config(bg=BTN_ON if active else BTN_BG,
                       fg="#ffffff" if active else DIM,
                       font=(FONT, 10, "bold" if active else "normal"))
        for w in self.body.winfo_children():
            w.destroy()
        self.back_btn.config(state="normal" if self.step else "disabled")
        self.next_btn.config(text="Close" if self.step == len(self.STEPS) - 1
                             else "Next")
        [self._step_connect, self._step_flash, self._step_wifi,
         self._step_done][self.step]()

    def _title(self, text, sub=""):
        tk.Label(self.body, text=text, bg=CARD, fg=FG, anchor="w",
                 font=(FONT, 13, "bold")).pack(fill="x")
        if sub:
            tk.Label(self.body, text=sub, bg=CARD, fg=DIM, anchor="w",
                     justify="left", wraplength=560,
                     font=(FONT, 10)).pack(fill="x", pady=(2, 8))

    def _row(self, parent, ok, text, warn=False):
        f = tk.Frame(parent, bg=CARD)
        f.pack(fill="x", pady=2)
        mark = "✓" if ok else ("!" if warn else "·")
        col = COLORS["linked"] if ok else (TX_COLOR if warn else DIM)
        tk.Label(f, text=mark, bg=CARD, fg=col, width=2,
                 font=(FONT, 11, "bold")).pack(side="left")
        tk.Label(f, text=text, bg=CARD, fg=FG if ok or warn else DIM,
                 anchor="w", justify="left", wraplength=520,
                 font=(FONT, 10)).pack(side="left", fill="x", expand=True)

    # ---- step 1: connect

    def _step_connect(self):
        self._title(
            "Connect the device",
            "The kit has two USB-C ports and they do different jobs.\n"
            "• The XIAO module's port: ESP32 flashing, serial console, WiFi "
            "setup.\n"
            "• The ReSpeaker board's own port: XMOS audio firmware (DFU) "
            "only.\n"
            "Plugging in both at once is fine.")
        u = self.usb
        box = card(self.body, bg=CARD2)
        box.pack(fill="x", pady=(4, 8))
        inner = tk.Frame(box, bg=CARD2)
        inner.pack(fill="x", padx=10, pady=8)
        tk.Label(inner, text="detected", bg=CARD2, fg=DIM, anchor="w",
                 font=(FONT, 9, "bold")).pack(fill="x", pady=(0, 4))
        self._detect_rows(inner)
        flat_btn(self.body, "Re-scan USB",
                 lambda: self.refresh_usb(async_=True)).pack(anchor="w")
        if not u:
            self.set_status("scanning USB…")
        elif u.get("usb_audio"):
            self.set_status("A ReSpeaker in USB-audio mode is connected. "
                            "Unplug it before flashing the XMOS.")
        elif u.get("xiao_port"):
            self.set_status("XIAO found - continue to flashing.")
        else:
            self.set_status("Plug in the XIAO's USB-C port, then Re-scan.")

    def _detect_rows(self, parent):
        u = self.usb
        if not u:
            self._row(parent, False, "scanning…")
            return
        port = u.get("xiao_port")
        self._row(parent, bool(port),
                  f"XIAO ESP32-S3 on {port}" if port else
                  "XIAO ESP32-S3 not found (plug in the XIAO's USB-C)")
        rev = u.get("xmos_rev")
        if u.get("xmos"):
            ok = rev == XMOS_TARGET_REV
            detail = f", firmware rev {rev:04x}" if rev is not None else ""
            self._row(parent, ok, f"ReSpeaker XMOS in DFU mode{detail}",
                      warn=not ok)
            if not ok:
                self._row(parent, False,
                          "needs the 48 kHz I2S build (rev 0110) - step 2 "
                          "will flash it", warn=True)
        else:
            self._row(parent, False,
                      "ReSpeaker XMOS not detected (only needed if its audio "
                      "firmware is not installed yet)")
        if u.get("usb_audio"):
            self._row(parent, False,
                      "DANGER: a ReSpeaker in USB-audio mode is connected "
                      "(a PC microphone?). Unplug it before XMOS flashing - "
                      "the flash script refuses to run otherwise.", warn=True)
        if u.get("error"):
            self._row(parent, False, u["error"], warn=True)

    # ---- step 2: flash

    def _step_flash(self):
        self._title(
            "Flash the firmware",
            "Runs tools/flash_all.ps1. The XMOS step is skipped automatically "
            "when its firmware is already correct. A full ESP32 build can "
            "take a few minutes the first time.")
        btns = tk.Frame(self.body, bg=CARD)
        btns.pack(fill="x", pady=(0, 6))
        self.b_both = flat_btn(btns, "Flash both", lambda: self.run_flash([]),
                               accent=True)
        self.b_both.pack(side="left")
        self.b_esp = flat_btn(btns, "ESP32 only",
                              lambda: self.run_flash(["-EspOnly"]))
        self.b_esp.pack(side="left", padx=6)
        self.b_xmos = flat_btn(btns, "XMOS only",
                               lambda: self.run_flash(["-XmosOnly"]))
        self.b_xmos.pack(side="left")
        self.b_stop = flat_btn(btns, "Stop", self.stop_flash)
        self.b_stop.pack(side="right")
        self.b_stop.config(state="disabled")

        wrap = tk.Frame(self.body, bg=CARD)
        wrap.pack(fill="both", expand=True)
        self.log = tk.Text(wrap, height=12, bg="#f7f8fc", fg=FG,
                           relief="flat", wrap="word", font=("Consolas", 9),
                           highlightthickness=1, highlightbackground=BORDER)
        sb = tk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.insert("end", "Ready.\n" if FLASH_SCRIPT.exists() else
                        f"flash script not found at {FLASH_SCRIPT}\n")
        self.log.config(state="disabled")
        if not FLASH_SCRIPT.exists():
            for b in (self.b_both, self.b_esp, self.b_xmos):
                b.config(state="disabled")
        self.set_status("Zadig may open on the first XMOS flash per PC: "
                        "select ReSpeaker Lite, then Install Driver.")

    def _log(self, text):
        def write():
            try:
                self.log.config(state="normal")
                self.log.insert("end", text)
                self.log.see("end")
                self.log.config(state="disabled")
            except tk.TclError:
                pass    # the log pane belongs to a step that is gone
        self._ui(write)

    def run_flash(self, extra):
        if self.proc and self.proc.poll() is None:
            return
        if "-EspOnly" not in extra and self.usb.get("usb_audio"):
            self.set_status("Refusing: unplug the USB-audio ReSpeaker first, "
                            "or use ESP32 only.")
            return
        cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File",
               str(FLASH_SCRIPT)] + extra
        port = self.usb.get("xiao_port")
        if port and "-XmosOnly" not in extra:
            cmd += ["-Port", port]
        self._log("\n$ " + " ".join(cmd) + "\n")
        for b in (self.b_both, self.b_esp, self.b_xmos):
            b.config(state="disabled")
        self.b_stop.config(state="normal")
        self.set_status("flashing…")

        def work():
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    encoding="utf-8", errors="replace",
                    creationflags=NO_WINDOW)
            except OSError as e:
                self._log(f"failed to start: {e}\n")
                self.set_status("could not start PowerShell")
                self._flash_done(None)
                return
            for line in self.proc.stdout:
                self._log(line)
            code = self.proc.wait()
            self._flash_done(code)

        threading.Thread(target=work, daemon=True).start()

    def _flash_done(self, code):
        def finish():
            try:
                for b in (self.b_both, self.b_esp, self.b_xmos):
                    b.config(state="normal")
                self.b_stop.config(state="disabled")
            except tk.TclError:
                return
            if code == 0:
                self.set_status("Flash finished. Continue to WiFi setup.")
            elif code is not None:
                self.set_status(f"Flash exited with code {code} - check the "
                                f"log above.")
            self.refresh_usb(async_=True)
        self._ui(finish)

    def stop_flash(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log("\n[stopped by user]\n")

    # ---- step 3: wifi

    def _step_wifi(self):
        self._title(
            "Give it WiFi",
            "Credentials are stored on the device itself (NVS) and override "
            "anything compiled into the firmware, so a unit can be moved to "
            "another network without reflashing. Applying reboots the device.")
        form = tk.Frame(self.body, bg=CARD)
        form.pack(fill="x", pady=(2, 8))
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
            if not ports:
                ports = [p.device for p in list_ports.comports()]
        except ImportError:
            ports = []
            self._row(self.body, False,
                      "pyserial is not installed: pip install pyserial",
                      warn=True)
        self.port_var = tk.StringVar(value=self.usb.get("xiao_port")
                                     or (ports[0] if ports else ""))
        rows = [("port", tk.OptionMenu(form, self.port_var, *(ports or [""])))]
        self.wifi_entries = {}
        for label in ("SSID", "password"):
            e = tk.Entry(form, bg="#f1f4fa", fg=FG, insertbackground=FG,
                         relief="flat", width=28, font=(FONT, 10),
                         show="*" if label == "password" else "")
            self.wifi_entries[label] = e
            rows.append((label, e))
        for i, (label, widget) in enumerate(rows):
            tk.Label(form, text=label, bg=CARD, fg=DIM, font=(FONT, 10)
                     ).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            if isinstance(widget, tk.OptionMenu):
                widget.configure(bg=BTN_BG, fg=FG, relief="flat",
                                 highlightthickness=0, activebackground=SEL,
                                 activeforeground=FG, font=(FONT, 10))
            widget.grid(row=i, column=1, sticky="w", pady=3)
        flat_btn(self.body, "Store WiFi on the device", self.apply_wifi,
                 accent=True).pack(anchor="w")
        tk.Label(self.body,
                 text="SSIDs containing spaces are not supported over this "
                      "path; the password must be 8-64 characters.",
                 bg=CARD, fg=DIM, anchor="w", justify="left",
                 wraplength=560, font=(FONT, 9)).pack(fill="x", pady=(8, 0))
        self.set_status("Plug in the XIAO's USB-C, then store your network.")

    def apply_wifi(self):
        ssid = self.wifi_entries["SSID"].get().strip()
        pwd = self.wifi_entries["password"].get()
        port = self.port_var.get()
        if not port:
            self.set_status("no COM port found")
            return
        if not ssid or " " in ssid:
            self.set_status("SSID must be non-empty and contain no spaces")
            return
        if not 8 <= len(pwd) <= 64:
            self.set_status("password must be 8-64 characters")
            return
        self.set_status("connecting to the device…")
        threading.Thread(target=self._wifi_worker, args=(port, ssid, pwd),
                         daemon=True).start()

    def _wifi_worker(self, port, ssid, pwd):
        try:
            import serial
        except ImportError:
            self.set_status("pyserial is not installed: pip install pyserial")
            return
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = 115200
            s.timeout = 0.5
            s.dtr = False       # avoid yanking the reset lines
            s.rts = False
            s.open()
        except OSError as e:
            self.set_status(f"cannot open {port}: {e}")
            return
        try:
            self.set_status("port open - waiting for the device…")
            time.sleep(3.0)     # opening the port can reboot it
            s.reset_input_buffer()
            s.write(f"wifi {ssid} {pwd}\n".encode())
            deadline = time.monotonic() + 8
            buf = b""
            while time.monotonic() < deadline:
                buf += s.read(256)
                if b"WTCFG OK" in buf:
                    self.set_status("Saved. The device is rebooting onto "
                                    "your network - continue to step 4.")
                    return
                if b"WTCFG ERR" in buf:
                    line = [l for l in buf.splitlines()
                            if b"WTCFG ERR" in l][-1]
                    self.set_status(line.decode(errors="replace"))
                    return
            self.set_status("No reply. Is this the walkie's XIAO port, "
                            "running current firmware?")
        except OSError as e:
            self.set_status(f"serial error: {e}")
        finally:
            try:
                s.close()
            except OSError:
                pass

    # ---- step 4: verify

    def _step_done(self):
        self._title(
            "Check it joined",
            "After a reboot the device connects to WiFi (LED orange), then "
            "announces itself. It should appear here within ~15 seconds.")
        self.found_box = tk.Frame(self.body, bg=CARD)
        self.found_box.pack(fill="x", pady=(4, 8))
        tk.Label(self.body,
                 text="Next: drag the new node into a group on the main "
                      "window to start talking. Volume, mic gain, TX mode "
                      "and group membership are stored on the device itself.",
                 bg=CARD, fg=DIM, anchor="w", justify="left",
                 wraplength=560, font=(FONT, 10)).pack(fill="x")
        self._poll_found()

    def _poll_found(self):
        if self.step != 3:
            return
        try:
            for w in self.found_box.winfo_children():
                w.destroy()
        except tk.TclError:
            return
        devs = self.app.monitor.snapshot()
        live = [(ip, d) for ip, d in sorted(devs.items())
                if d.get("last_rsp") and
                time.monotonic() - d["last_rsp"] < 6]
        if live:
            for ip, d in live:
                st = d.get("status") or {}
                self._row(self.found_box, True,
                          f"{d.get('name', ip)}  ({ip})   "
                          f"rssi {st.get('rssi', '?')} dBm")
            self.set_status(f"{len(live)} device(s) online.")
        else:
            self._row(self.found_box, False, "no device on the network yet…")
            self.set_status("Waiting - if the LED stays orange the "
                            "credentials were wrong; redo step 3.")
        try:
            self.win.after(2000, self._poll_found)
        except (tk.TclError, RuntimeError):
            pass


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root):
        self.root = root
        self.monitor = Monitor()
        self.client = None          # direct 1:1 TCP call
        self.talk_ip = None
        self.group_client = None    # PC-as-node UDP group client
        self.group_targets = []
        self.settings = load_settings()
        self.groups = self.settings.get("groups", [])  # [{name, members}]
        self.tx_mode = self.settings.get("mode", "full")
        self.pc_muted = False
        self.pc_spk_muted = False   # PC speaker mute (not persisted)
        self._spk_prev = {}         # device ip -> volume to restore on unmute
        self.cards = {}             # node id -> card widget dict
        self.group_boxes = []       # [{frame, ...}] parallel to self.groups
        self._groups_sig = None
        self._last_desired_ips = set()
        self._drag = None
        self._tick_n = 0
        self.new_group_btn = None
        self.wizard = None

        root.title("Walkie Group")
        root.configure(bg=BG)
        # Tk scales fonts by DPI but our pixel geometry was tuned for 96 dpi;
        # scale the default window (clamped to the screen) so the node cards
        # don't run off the right edge on a high-DPI display.
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        w = min(int(1040 * scale), root.winfo_screenwidth() - 80)
        h = min(int(620 * scale), root.winfo_screenheight() - 120)
        root.geometry(f"{w}x{h}+40+40")
        root.minsize(min(int(900 * scale), w), min(int(560 * scale), h))

        self._build_header()
        self._build_cards_panel()
        self._build_groups_panel()
        self.tick()
        self.root.after(2500, self.import_groups_from_devices)
        self.root.after(3000, lambda: self.reconcile(force=True))

    # ------------------------------------------------ layout

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=1)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Walkie Group", fg=FG, bg=CARD,
                 font=(FONT, 15, "bold")).pack(side="left", padx=16, pady=9)
        flat_btn(hdr, "Set up new device...", self.setup_wizard,
                 accent=True).pack(side="right", padx=(6, 14), pady=8)
        flat_btn(hdr, "WiFi via USB...",
                 lambda: self.setup_wizard(step=2)).pack(side="right", pady=8)
        self.hdr_info = tk.Label(hdr, text="", fg=DIM, bg=CARD,
                                 font=(FONT, 10))
        self.hdr_info.pack(side="right", padx=10)

    def _build_cards_panel(self):
        panel = card(self.root)
        panel.pack(fill="x", padx=12, pady=(12, 0))
        top = tk.Frame(panel, bg=CARD)
        top.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(top, text="NODES", fg=DIM, bg=CARD,
                 font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(top, text="drag a card into a group below",
                 fg=DIM, bg=CARD, font=(FONT, 9)).pack(side="right")
        self.cards_row = tk.Frame(panel, bg=CARD)
        self.cards_row.pack(fill="x", padx=8, pady=8)

    def _build_groups_panel(self):
        panel = card(self.root)
        panel.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        top = tk.Frame(panel, bg=CARD)
        top.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(top, text="GROUPS", fg=DIM, bg=CARD,
                 font=(FONT, 10, "bold")).pack(side="left")
        self.grp_note = tk.Label(top, text="", fg=DIM, bg=CARD,
                                 font=(FONT, 9))
        self.grp_note.pack(side="right")
        self.groups_row = tk.Frame(panel, bg=CARD)
        self.groups_row.pack(fill="x", anchor="n", padx=8, pady=8)

    # ------------------------------------------------ node model

    def pc_addr(self):
        devs = self.monitor.snapshot()
        any_ip = next(iter(devs), "8.8.8.8")
        ip = local_ip_toward(any_ip) or "127.0.0.1"
        return ip, self.monitor.sock.getsockname()[1]

    def active_pc_client(self):
        return self.group_client or self.client

    def nodes(self):
        devs = self.monitor.snapshot()
        c = self.active_pc_client()
        pc_level = pcm_level(c.last_tx_pcm) if c else 0.0
        # gate line in the same normalized units the waveforms draw in
        pc_thresh = int(self.settings.get("pc_thresh", 30))
        pc_gate = rms_to_level(vox_rms_of(pc_thresh))
        out = [{
            "id": "pc", "key": "pc", "name": "This PC", "is_pc": True,
            "online": True, "tx": bool(c and c.tx_active),
            "rx": bool(c and time.monotonic() - c.last_rx_time < 0.5),
            "muted": self.pc_muted, "level": pc_level, "gate": pc_gate,
            "gate_on": self.tx_mode == "vox",
            "voice": bool(c and pc_level >= pc_gate),
            "dev": None,
            "state": "muted" if self.pc_muted else "idle",
            "sub": "in group" if self.group_client else
                   (f"calling {self.talk_ip}" if self.client else "ready"),
        }]
        for ip, dev in sorted(devs.items()):
            st = dev.get("status") or {}
            fresh = dev.get("last_rsp") and \
                time.monotonic() - dev["last_rsp"] < 4
            state = state_of(dev)
            if self.client and ip == self.talk_ip and state == "offline":
                state = "linked"
            mode_name = {0: "always", 1: "voice", 2: "gate"}.get(
                st.get("tx_mode") or 0, "?")
            sub = (f"{st.get('rssi', '?')} dBm · tx {mode_name}"
                   if fresh else "offline")
            out.append({
                "id": ip, "key": dev.get("name", ip),
                "name": dev.get("name", ip), "is_pc": False,
                "online": bool(fresh), "tx": bool(fresh and st.get("tx_active")),
                "rx": bool(fresh and st.get("rx_active")),
                "muted": bool(st.get("muted")),
                "level": dev_mic_level(st.get("mic_level", 0)) if fresh else 0.0,
                "gate": dev_gate_level(st.get("vox_thresh")),
                "gate_on": bool(st.get("tx_mode")),
                "voice": bool(fresh and st.get("tx_active")
                              and dev_mic_level(st.get("mic_level", 0))
                              >= (dev_gate_level(st.get("vox_thresh")) or 0)),
                "dev": dev, "state": state, "sub": sub,
            })
        return out

    # ------------------------------------------------ node cards

    def _make_card(self, n):
        f = card(self.cards_row, bg=CARD2)
        f.configure(width=252)
        hdrrow = tk.Frame(f, bg=CARD2, cursor="fleur")
        hdrrow.pack(fill="x", padx=8, pady=(6, 0))
        dot = tk.Canvas(hdrrow, width=12, height=12, bg=CARD2,
                        highlightthickness=0)
        oid = dot.create_oval(2, 2, 10, 10, fill=COLORS["offline"],
                              outline="")
        dot.pack(side="left", pady=2)
        name = tk.Label(hdrrow, text=n["name"], fg=FG, bg=CARD2,
                        font=(FONT, 11, "bold"), cursor="fleur")
        name.pack(side="left", padx=6)
        badge = tk.Label(hdrrow, text="", bg=CARD2, fg="#ffffff",
                         font=(FONT, 8, "bold"), padx=5)
        badge.pack(side="right")

        wave = MiniWave(f, TX_COLOR if n["is_pc"] else RX_COLOR, height=38)
        wave.canvas.configure(cursor="fleur")
        wave.canvas.pack(in_=f, fill="x", padx=8, pady=(4, 0))

        sub = tk.Label(f, text="", fg=DIM, bg=CARD2, anchor="w",
                       font=(FONT, 9))
        sub.pack(fill="x", padx=9)

        btns = tk.Frame(f, bg=CARD2)
        btns.pack(fill="x", padx=8, pady=(2, 0))
        b_mute = flat_btn(btns, ICONS["mic"],
                          lambda i=n["id"]: self.toggle_mute(i), small=True)
        b_mute.pack(side="left")
        b_spk = flat_btn(btns, ICONS["spk"],
                         lambda i=n["id"]: self.toggle_spk_mute(i), small=True)
        b_spk.pack(side="left", padx=(4, 0))
        # TX mode as a segmented setting (the active segment is filled): a
        # single "VOX on/off" chip read as a status badge, not a control.
        # Devices offer voice (VAD) and gate (plain level threshold); the PC
        # has no VAD, so its only gated mode IS the level gate.
        tk.Label(btns, text="tx", fg=DIM, bg=CARD2,
                 font=(FONT, 8)).pack(side="left", padx=(6, 1))
        b_always = flat_btn(btns, "always",
                            lambda i=n["id"]: self.set_tx_mode(i, 0),
                            small=True)
        b_always.pack(side="left")
        b_voice = None
        if not n["is_pc"]:
            b_voice = flat_btn(btns, "voice",
                               lambda i=n["id"]: self.set_tx_mode(i, 1),
                               small=True)
            b_voice.pack(side="left", padx=(1, 0))
        b_gate = flat_btn(btns, "gate",
                          lambda i=n["id"]: self.set_tx_mode(i, 2),
                          small=True)
        b_gate.pack(side="left", padx=(1, 4))
        b_call = b_agc = None
        if not n["is_pc"]:
            b_call = flat_btn(btns, ICONS["call"],
                              lambda i=n["id"]: self.toggle_talk(i),
                              small=True)
            b_call.pack(side="left")
            # device-side only: the ESP32 runs the adaptive gain / noise gate
            b_agc = flat_btn(btns, "AGC",
                             lambda i=n["id"]: self.toggle_agc(i), small=True)
            b_agc.pack(side="left", padx=(4, 0))

        def bar_row(label, color, on_change, on_final, vmax=100):
            row = tk.Frame(f, bg=CARD2)
            row.pack(fill="x", padx=8, pady=(1, 2))
            tk.Label(row, text=label, fg=DIM, bg=CARD2, width=4, anchor="w",
                     font=(FONT, 9)).pack(side="left")
            pct = tk.Label(row, text="", fg=FG, bg=CARD2, width=5,
                           anchor="e", font=(FONT, 9, "bold"))
            pct.pack(side="right")
            bar = BarSlider(row, command=on_change, on_release=on_final,
                            color=color, vmax=vmax)
            bar.canvas.pack(side="left", fill="x", expand=True, padx=(4, 4))
            return bar, pct

        vol, vol_pct = bar_row(
            "vol", ACCENT,
            lambda v, i=n["id"]: self._on_card_vol(i, v, False),
            lambda v, i=n["id"]: self._on_card_vol(i, v, True))
        gain, gain_pct = bar_row(
            "gain", TX_COLOR,
            lambda v, i=n["id"]: self._on_card_gain(i, v, False),
            lambda v, i=n["id"]: self._on_card_gain(i, v, True), vmax=200)
        sens, sens_pct = bar_row(
            "gate", RX_COLOR,
            lambda v, i=n["id"]: self._on_card_thresh(i, v, False),
            lambda v, i=n["id"]: self._on_card_thresh(i, v, True))
        tk.Frame(f, bg=CARD2, height=4).pack()

        for w in (hdrrow, name, wave.canvas):
            w.bind("<ButtonPress-1>", lambda e, i=n["id"]: self._drag_start(e, i))
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_drop)

        cardw = {"frame": f, "dot": dot, "oid": oid, "name": name,
                 "badge": badge, "wave": wave, "sub": sub, "mute": b_mute,
                 "spk": b_spk, "mode_always": b_always, "mode_voice": b_voice,
                 "mode_gate": b_gate, "call": b_call, "agc": b_agc,
                 "vol": vol, "vol_pct": vol_pct, "vol_init": False,
                 "gain": gain, "gain_pct": gain_pct, "gain_init": False,
                 "sens": sens, "sens_pct": sens_pct, "sens_init": False,
                 "last_vol_send": 0.0, "last_sens_send": 0.0,
                 "last_gain_send": 0.0}
        f.pack(side="left", padx=4, pady=2, fill="y")
        return cardw

    def _update_card(self, cw, n):
        cw["dot"].itemconfig(cw["oid"], fill=COLORS[n["state"]])
        cw["name"].config(text=n["name"])
        cw["sub"].config(text=n["sub"])
        # "SPEAKING" must mean voice, not merely transmitting: an always-mode
        # node sends every frame, so it would otherwise never stop saying it
        if n["muted"]:
            cw["badge"].config(text="MUTED", bg=COLORS["muted"])
        elif n["tx"] and n["voice"]:
            cw["badge"].config(text="SPEAKING", bg=TX_COLOR)
        elif n["tx"]:
            cw["badge"].config(text="ON AIR", bg="#f0a868")
        elif n["rx"]:
            cw["badge"].config(text="HEARING", bg=RX_COLOR)
        else:
            cw["badge"].config(text="", bg=CARD2)
        st = (n["dev"] or {}).get("status") or {}
        cw["mute"].config(text=ICONS["mic_off"] if n["muted"] else ICONS["mic"],
                          bg=COLORS["muted"] if n["muted"] else BTN_BG,
                          fg="#ffffff" if n["muted"] else FG)
        if n["is_pc"]:
            mode = 2 if self.tx_mode == "vox" else 0
            spk_muted = self.pc_spk_muted
        else:
            mode = int(st.get("tx_mode") or 0)
            spk_muted = st.get("volume") == 0
        cw["spk"].config(text=ICONS["spk_off"] if spk_muted else ICONS["spk"],
                         bg=COLORS["muted"] if spk_muted else BTN_BG,
                         fg="#ffffff" if spk_muted else FG)
        for m, key in ((0, "mode_always"), (1, "mode_voice"),
                       (2, "mode_gate")):
            b = cw.get(key)
            if b is not None:
                on = mode == m
                b.config(bg=BTN_ON if on else BTN_BG,
                         fg="#ffffff" if on else FG)
        if cw["agc"] is not None:
            on = bool(st.get("mic_agc"))
            live = st.get("agc_gain")
            cw["agc"].config(
                text=f"AGC ×{live:.1f}" if (on and live) else "AGC",
                bg=BTN_ON if on else BTN_BG,
                fg="#ffffff" if on else FG)
        if cw["call"] is not None:
            in_call = self.client and self.talk_ip == n["id"]
            cw["call"].config(text=ICONS["end"] if in_call else ICONS["call"],
                              bg=TX_COLOR if in_call else BTN_BG,
                              fg="#ffffff" if in_call else FG,
                              state="disabled" if (self.group_client and
                                                   not in_call)
                              else "normal")
        # sliders reflect the node's stored values until the user touches them
        if n["is_pc"]:
            vol_val = min(100, int(self.settings.get("pc_vol", 100)))
            sens_val = int(self.settings.get("pc_thresh", 30))
            gain_val = int(self.settings.get("pc_gain", 100))
        else:
            vol_val = st.get("volume")
            sens_val = st.get("vox_thresh")
            gain_val = st.get("mic_gain")
        if vol_val is not None and not cw["vol_init"]:
            cw["vol"].set(vol_val)
            cw["vol_init"] = True
        if sens_val is not None and not cw["sens_init"]:
            cw["sens"].set(sens_val)
            cw["sens_init"] = True
        if gain_val is not None and not cw["gain_init"]:
            cw["gain"].set(gain_val)
            cw["gain_init"] = True
        cw["vol_pct"].config(text=f"{cw['vol'].get()}%")
        cw["gain_pct"].config(text=f"{cw['gain'].get()}%")
        cw["sens_pct"].config(text=f"{cw['sens'].get()}")

    def _refresh_cards(self, nodes):
        ids = [n["id"] for n in nodes]
        for nid in list(self.cards):
            if nid not in ids:
                self.cards.pop(nid)["frame"].destroy()
        for n in nodes:
            if n["id"] not in self.cards:
                self.cards[n["id"]] = self._make_card(n)
            self._update_card(self.cards[n["id"]], n)

    # ---- card actions

    def toggle_mute(self, nid):
        if nid == "pc":
            self.pc_muted = not self.pc_muted
            for c in (self.client, self.group_client):
                if c:
                    c.muted = self.pc_muted
        else:
            dev = self.monitor.snapshot().get(nid)
            if dev:
                st = dev.get("status") or {}
                self.monitor.set_mute(nid, dev.get("port", PORT),
                                      not st.get("muted"))

    def toggle_spk_mute(self, nid):
        """Speaker mute. The PC's is a local flag over its playback volume;
        a device's is volume 0 on the device itself (so it also persists and
        works with the GUI closed), restored to the pre-mute value."""
        if nid == "pc":
            self.pc_spk_muted = not self.pc_spk_muted
            vol = 0 if self.pc_spk_muted else \
                min(100, int(self.settings.get("pc_vol", 100)))
            for c in (self.client, self.group_client):
                if c:
                    c.rx_volume = vol / 100.0
            new = vol
        else:
            dev = self.monitor.snapshot().get(nid)
            if not dev:
                return
            st = dev.get("status") or {}
            vol = st.get("volume")
            if vol is None:
                return  # status too old to know what to restore
            if vol > 0:
                self._spk_prev[nid] = vol
                new = 0
            else:
                new = self._spk_prev.get(nid, 100)
            self.monitor.set_volume(nid, dev.get("port", PORT), new)
        cw = self.cards.get(nid)
        if cw:
            cw["vol_init"] = True
            cw["vol"].set(new)
            cw["vol_pct"].config(text=f"{new}%")

    def set_tx_mode(self, nid, mode):
        """mode: 0 always, 1 voice (device VAD), 2 level gate. The PC only
        distinguishes always/gated - its gated mode is the RMS gate."""
        if nid == "pc":
            self.tx_mode = "vox" if mode else "full"
            for c in (self.client, self.group_client):
                if c:
                    c.mode = self.tx_mode
            self.settings["mode"] = self.tx_mode
            save_settings(self.settings)
        else:
            dev = self.monitor.snapshot().get(nid)
            if dev:
                self.monitor.set_mode(nid, dev.get("port", PORT), mode)

    def _on_card_vol(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw["vol_init"] = True   # user owns the slider now
            cw["vol_pct"].config(text=f"{value}%")
        if nid == "pc":
            self.pc_spk_muted = False   # touching the slider unmutes
            self.settings["pc_vol"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.rx_volume = value / 100.0
        else:
            now = time.monotonic()
            if cw and (final or now - cw["last_vol_send"] > 0.15):
                cw["last_vol_send"] = now
                dev = self.monitor.snapshot().get(nid)
                if dev:
                    self.monitor.set_volume(nid, dev.get("port", PORT),
                                            value)

    def toggle_agc(self, nid):
        dev = self.monitor.snapshot().get(nid)
        if dev:
            st = dev.get("status") or {}
            self.monitor.set_agc(nid, dev.get("port", PORT),
                                 not st.get("mic_agc"))

    def _on_card_gain(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw["gain_init"] = True
            cw["gain_pct"].config(text=f"{value}%")
        if nid == "pc":
            self.settings["pc_gain"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.tx_gain = value / 100.0
        else:
            now = time.monotonic()
            if cw and (final or now - cw["last_gain_send"] > 0.15):
                cw["last_gain_send"] = now
                dev = self.monitor.snapshot().get(nid)
                if dev:
                    self.monitor.set_gain(nid, dev.get("port", PORT), value)

    def _on_card_thresh(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw["sens_init"] = True
            cw["sens_pct"].config(text=f"{value}")
        if nid == "pc":
            self.settings["pc_thresh"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.vox_thresh = value
        else:
            now = time.monotonic()
            if cw and (final or now - cw["last_sens_send"] > 0.15):
                cw["last_sens_send"] = now
                dev = self.monitor.snapshot().get(nid)
                if dev:
                    self.monitor.set_thresh(nid, dev.get("port", PORT), value)

    def toggle_talk(self, ip):
        if self.client:
            was = self.talk_ip
            self.client.stop()
            self.client = None
            self.talk_ip = None
            if was == ip:
                return
        if self.group_client:
            return
        devs = self.monitor.snapshot()
        if ip not in devs:
            return
        port = devs[ip].get("port", PORT)
        self.client = WalkieClient((ip, port))
        self._apply_pc_client_prefs(self.client)
        self.client.start()
        self.talk_ip = ip

    def _apply_pc_client_prefs(self, c):
        c.rx_volume = 0.0 if self.pc_spk_muted else \
            min(100, int(self.settings.get("pc_vol", 100))) / 100.0
        c.vox_thresh = int(self.settings.get("pc_thresh", 30))
        c.tx_gain = int(self.settings.get("pc_gain", 100)) / 100.0
        c.mode = self.tx_mode
        c.muted = self.pc_muted

    # ------------------------------------------------ drag and drop

    def _drag_start(self, event, nid):
        self._drag = {"id": nid, "ghost": None, "x": event.x_root,
                      "y": event.y_root}

    def _drag_move(self, event):
        d = self._drag
        if not d:
            return
        if d["ghost"] is None:
            if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) < 8:
                return
            g = tk.Toplevel(self.root)
            g.overrideredirect(True)
            try:
                g.attributes("-alpha", 0.85)
            except tk.TclError:
                pass
            name = ("This PC" if d["id"] == "pc" else
                    self.monitor.snapshot().get(d["id"], {}).get("name",
                                                                 d["id"]))
            tk.Label(g, text="  " + name + "  ", bg=ACCENT, fg="#ffffff",
                     font=(FONT, 10, "bold"), padx=6, pady=3).pack()
            d["ghost"] = g
        d["ghost"].geometry(f"+{event.x_root + 12}+{event.y_root + 8}")

    def _hit(self, widget, x, y):
        try:
            wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
            return (wx <= x <= wx + widget.winfo_width() and
                    wy <= y <= wy + widget.winfo_height())
        except tk.TclError:
            return False

    def _drag_drop(self, event):
        d = self._drag
        self._drag = None
        if not d:
            return
        if d["ghost"] is None:
            return  # plain click, no drag
        d["ghost"].destroy()
        nid = d["id"]
        key = ("pc" if nid == "pc" else
               self.monitor.snapshot().get(nid, {}).get("name", nid))
        for i, box in enumerate(self.group_boxes):
            if self._hit(box["frame"], event.x_root, event.y_root):
                g = self.groups[i]
                if key not in g["members"]:
                    if len(g["members"]) >= MAX_MEMBERS:
                        return
                    g["members"].append(key)
                    self.groups_changed()
                return
        if self.new_group_btn is not None and \
                self._hit(self.new_group_btn, event.x_root, event.y_root):
            self._add_group([key])

    # ------------------------------------------------ groups panel

    def _groups_signature(self, nodes):
        """Structure only - live talk/online state is repainted by
        _draw_group_graphs() instead of rebuilding widgets."""
        return json.dumps(self.groups) + repr(sorted(n["key"] for n in nodes))

    def _rebuild_groups(self, nodes):
        sig = self._groups_signature(nodes)
        if sig == self._groups_sig:
            return
        self._groups_sig = sig
        for w in self.groups_row.winfo_children():
            w.destroy()
        self.group_boxes = []
        for gi, g in enumerate(self.groups):
            box = card(self.groups_row, bg=CARD2)
            box.pack(side="left", anchor="n", padx=4, pady=2)
            head = tk.Frame(box, bg=CARD2)
            head.pack(fill="x", padx=8, pady=(6, 2))
            nm = tk.Label(head, text=g.get("name", f"Group {gi + 1}"),
                          fg=FG, bg=CARD2, font=(FONT, 10, "bold"))
            nm.pack(side="left")
            nm.bind("<Double-Button-1>", lambda e, i=gi: self._rename(i))
            tk.Button(head, text="✕", command=lambda i=gi: self._del_group(i),
                      bg=CARD2, fg=DIM, activebackground=CARD2,
                      activeforeground=FG, relief="flat", bd=0,
                      font=(FONT, 9)).pack(side="right")
            graph = tk.Canvas(box, width=228, height=168, bg="#fbfcfe",
                              highlightthickness=1,
                              highlightbackground=BORDER)
            graph.pack(padx=8, pady=(0, 4))
            chips = tk.Frame(box, bg=CARD2)
            chips.pack(fill="x", padx=8, pady=(0, 7))
            for key in g["members"]:
                chip = tk.Frame(chips, bg="#e6eaf4")
                chip.pack(side="left", padx=(0, 4), pady=1)
                tk.Label(chip, text=key if key != "pc" else "This PC",
                         bg="#e6eaf4", fg=DIM, font=(FONT, 8),
                         padx=4).pack(side="left")
                tk.Button(chip, text="✕",
                          command=lambda i=gi, k=key: self._chip_remove(i, k),
                          bg="#e6eaf4", fg=DIM, activebackground="#e6eaf4",
                          activeforeground=FG, relief="flat", bd=0,
                          font=(FONT, 7)).pack(side="right")
            self.group_boxes.append({"frame": box, "graph": graph,
                                     "index": gi})
        self.new_group_btn = tk.Label(self.groups_row,
                                      text="+\nnew group\n(drop here)",
                                      fg=DIM, bg=CARD,
                                      highlightbackground=BORDER,
                                      highlightthickness=1,
                                      font=(FONT, 9), justify="center",
                                      padx=22, pady=60)
        self.new_group_btn.pack(side="left", anchor="n", padx=6, pady=2)
        self.new_group_btn.bind("<Button-1>", lambda e: self._add_group([]))

    def _draw_group_graphs(self, nodes):
        """Live hub-and-spoke per group: who is in it, who is broadcasting."""
        info = {n["key"]: n for n in nodes}
        pulse = 2.5 + 1.5 * math.sin(time.monotonic() * 6)
        for box in self.group_boxes:
            gi = box["index"]
            if gi >= len(self.groups):
                continue
            c = box["graph"]
            c.delete("all")
            w = max(int(c["width"]), 60)
            h = max(int(c["height"]), 60)
            members = self.groups[gi]["members"]
            cx, cy = w / 2, h / 2 - 4
            if not members:
                c.create_text(cx, cy, text="drop nodes here", fill=DIM,
                              font=(FONT, 9, "italic"))
                continue
            talkers = [k for k in members
                       if info.get(k) and info[k]["tx"] and info[k]["voice"]]
            # hub: lights up while someone holds the floor
            hub_r = 15
            c.create_oval(cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r,
                          fill="#fff2e8" if talkers else "#eef2ff",
                          outline=TX_COLOR if talkers else ACCENT, width=2)
            c.create_text(cx, cy, text="((•))" if talkers else "•••",
                          fill=TX_COLOR if talkers else ACCENT,
                          font=(FONT, 7, "bold"))
            n = len(members)
            radius = min(w, h) * 0.34
            for i, key in enumerate(members):
                node = info.get(key)
                ang = -math.pi / 2 + i * (2 * math.pi / n)
                x = cx + radius * math.cos(ang)
                y = cy + radius * 0.78 * math.sin(ang)
                online = bool(node and node["online"])
                muted = bool(node and node["muted"])
                speaking = bool(node and node["tx"] and node["voice"])
                on_air = bool(node and node["tx"] and not speaking)
                hearing = bool(node and node["rx"])
                col = (COLORS["muted"] if muted else
                       COLORS["linked"] if online else COLORS["offline"])
                # spoke: audio flowing from this node to the rest
                if speaking:
                    c.create_line(x, y, cx, cy, fill=TX_COLOR, width=3,
                                  arrow="last", arrowshape=(7, 8, 3))
                elif on_air:
                    c.create_line(x, y, cx, cy, fill="#f0a868", width=2)
                else:
                    c.create_line(cx, cy, x, y, fill=BORDER, width=1)
                r = 15
                if speaking:
                    c.create_oval(x - r - 3 - pulse, y - r - 3 - pulse,
                                  x + r + 3 + pulse, y + r + 3 + pulse,
                                  outline=TX_COLOR, width=2)
                elif hearing:
                    c.create_oval(x - r - 3, y - r - 3, x + r + 3, y + r + 3,
                                  outline=RX_COLOR, width=1, dash=(2, 2))
                c.create_oval(x - r, y - r, x + r, y + r, fill=CARD,
                              outline=col, width=2)
                c.create_text(x, y, text="PC" if key == "pc" else "WT",
                              fill=FG if online else DIM,
                              font=(FONT, 8, "bold"))
                label = "This PC" if key == "pc" else (
                    node["name"] if node else key)
                if len(label) > 14:
                    label = label[:13] + "…"
                ly = y + r + 8 if math.sin(ang) >= -0.1 else y - r - 8
                c.create_text(x, ly, text=label, fill=FG if online else DIM,
                              font=(FONT, 7))
            status = ("  ".join(("This PC" if k == "pc" else k) + " speaking"
                                for k in talkers[:2])
                      if talkers else "idle")
            c.create_text(w / 2, h - 7, text=status,
                          fill=TX_COLOR if talkers else DIM,
                          font=(FONT, 7, "bold" if talkers else "normal"))

    def _add_group(self, members):
        self.groups.append({"name": f"Group {len(self.groups) + 1}",
                            "members": list(members)})
        self.groups_changed()

    def _del_group(self, i):
        del self.groups[i]
        self.groups_changed()

    def _chip_remove(self, gi, key):
        g = self.groups[gi]
        if key in g["members"]:
            g["members"].remove(key)
        self.groups_changed()

    def _rename(self, i):
        name = tkinter.simpledialog.askstring(
            "Rename group", "Group name:",
            initialvalue=self.groups[i].get("name", ""), parent=self.root)
        if name:
            self.groups[i]["name"] = name.strip()[:24]
            self.groups_changed()

    def groups_changed(self):
        self.settings["groups"] = self.groups
        save_settings(self.settings)
        self._groups_sig = None
        self.reconcile(force=True)

    # ------------------------------------------------ external-group import

    def import_groups_from_devices(self):
        """If the GUI has no groups, reconstruct them from the member lists
        the devices report (configured elsewhere / a previous session).
        Union lists can't encode overlapping groups, so each connected
        component becomes one group."""
        if self.groups:
            return
        devs = self.monitor.snapshot()
        pc_ip, _ = self.pc_addr()
        addr2key = {(ip, d.get("port", PORT)): d.get("name", ip)
                    for ip, d in devs.items()}
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        for ip, d in devs.items():
            st = d.get("status") or {}
            k = d.get("name", ip)
            for m in st.get("members") or []:
                mk = addr2key.get(tuple(m))
                if mk is None and m[0] == pc_ip:
                    mk = "pc"  # our addr (any port - ports change per run)
                if mk is not None:
                    union(k, mk)
        comps = {}
        for k in parent:
            comps.setdefault(find(k), []).append(k)
        groups = [sorted(v) for v in comps.values() if len(v) >= 2]
        if groups:
            self.groups = [{"name": f"Group {i + 1}", "members": g}
                           for i, g in enumerate(groups)]
            self.grp_note.config(text="imported existing group(s) from "
                                      "device member lists")
            self.settings["groups"] = self.groups
            save_settings(self.settings)
            self._groups_sig = None

    # ------------------------------------------------ routing reconcile

    def compute_desired(self):
        """GUI groups -> per-device send-lists + PC targets (unions)."""
        devs = self.monitor.snapshot()
        name2dev = {d.get("name", ip): (ip, d.get("port", PORT))
                    for ip, d in devs.items()}
        pc = self.pc_addr()
        desired = {}      # device ip -> set((ip, port))
        pc_targets = set()
        for g in self.groups:
            mem = [m for m in g["members"]]
            for m in mem:
                others = [o for o in mem if o != m]
                if m == "pc":
                    for o in others:
                        if o in name2dev:
                            pc_targets.add(name2dev[o])
                elif m in name2dev:
                    s = desired.setdefault(name2dev[m][0], set())
                    for o in others:
                        if o == "pc":
                            s.add(pc)
                        elif o in name2dev:
                            s.add(name2dev[o])
        return devs, desired, pc_targets

    def reconcile(self, force=False):
        """Push the group layout into device NVS member lists (diff-based)."""
        devs, desired, pc_targets = self.compute_desired()
        clipped = False
        for ip, want in desired.items():
            if len(want) > MAX_MEMBERS:
                want = set(sorted(want)[:MAX_MEMBERS])
                clipped = True
            dev = devs.get(ip)
            st = dev.get("status") if dev else None
            fresh = dev and dev.get("last_rsp") and \
                time.monotonic() - dev["last_rsp"] < 4
            if not st or not fresh:
                continue
            if set(st.get("members") or []) != want or force:
                self.monitor.set_group(ip, dev.get("port", PORT),
                                       sorted(want))
        # devices that dropped out of all groups: clear once
        for ip in self._last_desired_ips - set(desired):
            dev = devs.get(ip)
            if dev:
                self.monitor.set_group(ip, dev.get("port", PORT), [])
        self._last_desired_ips = set(desired)
        self.grp_note.config(
            text=("some device lists clipped to 8 members!" if clipped else
                  "membership is pushed to devices and persists on them"))
        # PC side
        targets = sorted(pc_targets)
        if targets:
            if (not self.group_client or
                    self.group_targets != targets):
                self._start_group_client(targets)
        else:
            self._stop_group_client()

    def _start_group_client(self, targets):
        if self.client:
            self.client.stop()
            self.client = None
            self.talk_ip = None
        self._stop_group_client()
        gc = WalkieClient(sock=self.monitor.sock, external_rx=True,
                          use_tcp=False, targets=targets)
        self._apply_pc_client_prefs(gc)
        self.group_client = gc
        self.group_targets = targets
        self.monitor.active_client = gc
        gc.start()

    def _stop_group_client(self):
        if self.group_client:
            self.monitor.active_client = None
            self.group_client.stop()
            self.group_client = None
            self.group_targets = []

    def leave_group_on_exit(self):
        """Best effort: remove this PC from every device's member list.
        Device-to-device routing is left untouched - it keeps working
        with the GUI closed."""
        if not self.group_client:
            return
        pc = self.pc_addr()
        for ip, dev in self.monitor.snapshot().items():
            st = dev.get("status") or {}
            members = [m for m in (st.get("members") or []) if m != pc]
            if len(members) != len(st.get("members") or []):
                self.monitor.set_group(ip, dev.get("port", PORT), members)

    # ------------------------------------------------ setup wizard

    def setup_wizard(self, step=0):
        """Guided flash + provisioning for a new unit (see SetupWizard)."""
        if self.wizard is not None:
            try:
                self.wizard.win.deiconify()
                self.wizard.win.lift()
                self.wizard.goto(step)
                return
            except tk.TclError:
                self.wizard = None
        self.wizard = SetupWizard(self, start=step)

    # ------------------------------------------------ periodic refresh

    def tick(self):
        self._tick_n += 1
        nodes = self.nodes()
        by_id = {n["id"]: n for n in nodes}

        # 20 Hz: per-card waveforms
        for nid, cw in self.cards.items():
            n = by_id.get(nid)
            if n:
                cw["wave"].push(n["level"], n.get("gate"),
                                n.get("gate_on", True))

        if self._tick_n % 2 == 0:  # 10 Hz: live group graphs
            self._draw_group_graphs(nodes)
        if self._tick_n % 8 == 0:  # 2.5 Hz: cards, groups, header
            self._refresh_cards(nodes)
            self._rebuild_groups(nodes)
            grouped = sum(1 for g in self.groups for _ in g["members"])
            self.hdr_info.config(
                text=f"{len(nodes)} node(s) · {len(self.groups)} group(s)")
        if self._tick_n % 80 == 0:  # every 4 s: reconcile device routing
            self.reconcile()

        self.root.after(50, self.tick)


def main():
    print("creating window...")
    root = tk.Tk()
    try:
        patch = tuple(int(x) for x in
                      str(root.tk.call("info", "patchlevel")).split("."))
    except (ValueError, tk.TclError):
        patch = (0,)
    if patch < (8, 6, 12):
        ICONS.update(ICONS_TEXT)  # emoji would render as garbage boxes
    app = App(root)
    try:
        root.mainloop()
    finally:
        try:
            app.leave_group_on_exit()
        except Exception:
            pass
        if app.client:
            app.client.stop()
        if app.group_client:
            app.group_client.stop()
        app.monitor.stop()
    print("window closed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("startup failed - press Enter to close")
