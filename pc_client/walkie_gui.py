#!/usr/bin/env python3
"""Walkie-talkie control center.

Left sidebar: every walkie unit on the network (mDNS discovery + live status
polled over the control protocol). Main panel: selected device's state, live
waveforms while talking, and peer assignment ("pair A with B", or Auto).

Control packets (WT_FLAG_CTRL) are ignored by the firmware's peer adoption,
so monitoring any number of devices never disturbs an ongoing audio link.

    python walkie_gui.py
"""

import math
import socket
import struct
import threading
import time
import tkinter as tk
from array import array
from collections import deque

from walkie_pc import HDR, MAGIC, PORT, VERSION, WalkieClient

FLAG_CTRL = 0x02
CTRL_STATUS_REQ = 0x01
CTRL_STATUS_RSP = 0x02
CTRL_SET_PEER = 0x03
CTRL_SET_VOL = 0x04
STATUS = struct.Struct("<BBBBBbHI24s")   # wt_status_t (volume byte appended
SET_PEER = struct.Struct("<BBHI")        # by newer firmware, parsed optionally)

BG = "#1e1e28"
PANEL = "#28283a"
SIDE = "#242433"
SEL = "#34345a"
FG = "#e8e8f0"
DIM = "#8888a0"
TX_COLOR = "#ffb454"
RX_COLOR = "#54d68a"

COLORS = {
    "offline": "#606070",
    "idle": "#4a90e0",
    "linked": "#40d070",
    "muted": "#c060e0",
}

WAVE_W = 430
WAVE_H = 64
BARS = 80
BAR_WIDTH = 3
DOT_H = 1.2


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

    def stop(self):
        self._stop.set()

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
                # devices that have answered before but went quiet: if the
                # router blackholed this flow, a fresh source port fixes it
                now = time.monotonic()
                stale = [ip for ip, d in self.devices.items()
                         if d.get("last_rsp") and now - d["last_rsp"] > 5]
            # never yank the socket while a call is riding this flow
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
            time.sleep(1.5)

    def _recv(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
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
            if flags & 0x01:  # audio riding the shared flow -> active call
                if client and addr[0] == client.target[0]:
                    client.handle_audio(seq, data[HDR.size:])
                continue
            if not flags:  # keepalive: refresh the call's liveness
                if client and addr[0] == client.target[0]:
                    client.last_rx_time = time.monotonic()
                continue
            if not flags & FLAG_CTRL or len(data) < HDR.size + STATUS.size:
                continue
            body = data[HDR.size:]
            if body[0] != CTRL_STATUS_RSP:
                continue
            (_, _, muted, linked, locked, rssi,
             peer_port, peer_ip, host) = STATUS.unpack_from(body)
            volume = body[STATUS.size] if len(body) > STATUS.size else None
            with self.lock:
                d = self.devices.setdefault(addr[0], {"port": addr[1]})
                d["name"] = host.split(b"\0")[0].decode(errors="replace") or addr[0]
                d["status"] = {
                    "muted": muted, "linked": linked, "locked": locked,
                    "rssi": rssi, "peer_ip": peer_ip, "peer_port": peer_port,
                    "volume": volume,
                }
                d["last_rsp"] = time.monotonic()

    def snapshot(self):
        with self.lock:
            return {ip: dict(d) for ip, d in self.devices.items()}

    def set_peer(self, dev_ip, dev_port, peer_ip_str, peer_port):
        """peer_ip_str None => Auto (clear lock)."""
        ip_n = 0 if not peer_ip_str else struct.unpack(
            "<I", socket.inet_aton(peer_ip_str))[0]
        body = SET_PEER.pack(CTRL_SET_PEER, 0,
                             socket.htons(peer_port) if peer_ip_str else 0, ip_n)
        pkt = HDR.pack(MAGIC, 0, FLAG_CTRL, VERSION) + body
        try:
            self.sock.sendto(pkt, (dev_ip, dev_port))
        except OSError:
            pass

    def set_volume(self, dev_ip, dev_port, volume):
        """Set the device's speaker volume (0-100)."""
        vol = max(0, min(100, int(volume)))
        pkt = (HDR.pack(MAGIC, 0, FLAG_CTRL, VERSION)
               + struct.pack("<BB", CTRL_SET_VOL, vol))
        try:
            self.sock.sendto(pkt, (dev_ip, dev_port))
        except OSError:
            pass


def state_of(dev):
    st = dev.get("status")
    if not st or time.monotonic() - dev.get("last_rsp", 0) > 6:
        return "offline"
    if st["muted"]:
        return "muted"
    if st["linked"]:
        return "linked"
    return "idle"


def local_ip_toward(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# ---------------------------------------------------------------- waveform

class WaveView:
    def __init__(self, parent, label, color):
        self.frame = tk.Frame(parent, bg=BG)
        tk.Label(self.frame, text=label, fg=DIM, bg=BG, anchor="w",
                 font=("Segoe UI", 9)).pack(fill="x")
        self.canvas = tk.Canvas(self.frame, width=WAVE_W, height=WAVE_H,
                                bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="x")
        self.levels = deque([0.0] * BARS, maxlen=BARS)
        mid = WAVE_H / 2
        step = WAVE_W / (BARS + 1)
        self.bars = [self.canvas.create_line(
            (i + 1) * step, mid - DOT_H, (i + 1) * step, mid + DOT_H,
            fill=color, width=BAR_WIDTH, capstyle="round") for i in range(BARS)]

    @staticmethod
    def _level(pcm_bytes):
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

    def update(self, pcm_bytes):
        self.levels.append(self._level(pcm_bytes))
        mid = WAVE_H / 2
        step = WAVE_W / (BARS + 1)
        span = mid - 5
        for i, lv in enumerate(self.levels):
            x = (i + 1) * step
            h = max(DOT_H, lv * span)
            self.canvas.coords(self.bars[i], x, mid - h, x, mid + h)


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root):
        self.root = root
        self.monitor = Monitor()
        self.client = None            # active WalkieClient, if talking
        self.talk_ip = None
        self.selected = None
        self.rows = {}                # ip -> row widgets

        root.title("Walkie-Talkie")
        root.configure(bg=BG)
        # fixed size: otherwise label text changes resize the whole window
        root.geometry("665x495+120+120")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.after(1500, lambda: root.attributes("-topmost", False))

        side = tk.Frame(root, bg=SIDE, width=185)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="DEVICES", fg=DIM, bg=SIDE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        self.dev_list = tk.Frame(side, bg=SIDE)
        self.dev_list.pack(fill="both", expand=True)

        self.main = tk.Frame(root, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_main()
        self.tick()

    # ------------------------------------------------ main panel widgets

    def _build_main(self):
        m = self.main
        top = tk.Frame(m, bg=BG)
        top.pack(fill="x", padx=14, pady=(12, 0))
        self.title = tk.Label(top, text="select a device", fg=FG, bg=BG,
                              font=("Segoe UI", 13, "bold"), anchor="w")
        self.title.pack(side="left")
        self.talk_btn = tk.Button(top, text="Talk", width=10, state="disabled",
                                  command=self.toggle_talk, bg=PANEL, fg=FG,
                                  activebackground="#3a3a55",
                                  activeforeground=FG, relief="flat")
        self.talk_btn.pack(side="right")

        self.info = tk.Label(m, text="", fg=DIM, bg=BG, anchor="w",
                             justify="left", font=("Consolas", 9))
        self.info.pack(fill="x", padx=14, pady=(6, 0))

        pair = tk.Frame(m, bg=BG)
        pair.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(pair, text="peer:", fg=DIM, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")
        self.peer_var = tk.StringVar(value="Auto")
        self.peer_menu = tk.OptionMenu(pair, self.peer_var, "Auto")
        self.peer_menu.configure(bg=PANEL, fg=FG, relief="flat",
                                 highlightthickness=0, activebackground=SEL,
                                 activeforeground=FG)
        self.peer_menu.pack(side="left", padx=6)
        tk.Button(pair, text="Apply", command=self.apply_peer, bg=PANEL, fg=FG,
                  activebackground="#3a3a55", activeforeground=FG,
                  relief="flat").pack(side="left")

        vol = tk.Frame(m, bg=BG)
        vol.pack(fill="x", padx=14, pady=(8, 0))
        slider_opts = dict(orient="horizontal", length=150, showvalue=False,
                           bg=BG, fg=FG, troughcolor=PANEL,
                           highlightthickness=0, sliderrelief="flat",
                           activebackground=SEL)
        tk.Label(vol, text="device spk", fg=DIM, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left")
        self._dev_vol_guard = False   # True while set() mirrors device state
        self._dev_vol_last_send = 0.0
        self.dev_vol = tk.Scale(vol, from_=0, to=100,
                                command=self._on_dev_vol, **slider_opts)
        self.dev_vol.set(100)
        self.dev_vol.pack(side="left", padx=(6, 0))
        self.dev_vol.bind("<ButtonRelease-1>", self._on_dev_vol_release)
        tk.Label(vol, text="PC out", fg=DIM, bg=BG,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 0))
        self.pc_vol_gain = 1.0
        self.pc_vol = tk.Scale(vol, from_=0, to=150,
                               command=self._on_pc_vol, **slider_opts)
        self.pc_vol.set(100)
        self.pc_vol.pack(side="left", padx=(6, 0))

        self.tx_wave = WaveView(m, "PC mic -> device (TX)", TX_COLOR)
        self.rx_wave = WaveView(m, "device -> PC speakers (RX)", RX_COLOR)
        self.tx_wave.frame.pack(fill="x", padx=14, pady=(10, 0))
        self.rx_wave.frame.pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(m, text="tip: use headphones - the PC has no echo canceller",
                 fg=DIM, bg=BG, font=("Segoe UI", 8)).pack(pady=(6, 8))

    # ------------------------------------------------ sidebar

    def _rebuild_sidebar(self, devs):
        for ip in list(self.rows):
            if ip not in devs:
                self.rows.pop(ip)["frame"].destroy()
        for ip, dev in sorted(devs.items()):
            if ip not in self.rows:
                f = tk.Frame(self.dev_list, bg=SIDE, cursor="hand2")
                f.pack(fill="x")
                dot = tk.Canvas(f, width=12, height=12, bg=SIDE,
                                highlightthickness=0)
                oid = dot.create_oval(2, 2, 10, 10, fill=COLORS["offline"],
                                      outline="")
                dot.pack(side="left", padx=(10, 4), pady=6)
                lbl = tk.Label(f, text=ip, fg=FG, bg=SIDE, anchor="w",
                               font=("Segoe UI", 10))
                lbl.pack(side="left", fill="x", expand=True)
                for w in (f, dot, lbl):
                    w.bind("<Button-1>", lambda e, ip=ip: self.select(ip))
                self.rows[ip] = {"frame": f, "dot": dot, "oid": oid, "lbl": lbl}
            row = self.rows[ip]
            row["lbl"].config(text=dev.get("name", ip))
            row["dot"].itemconfig(row["oid"], fill=COLORS[self._eff_state(ip, dev)])
            bgc = SEL if ip == self.selected else SIDE
            row["frame"].config(bg=bgc)
            row["lbl"].config(bg=bgc)
            row["dot"].config(bg=bgc)

    def select(self, ip):
        self.selected = ip
        self.talk_btn.config(state="normal")
        st = self.monitor.snapshot().get(ip, {}).get("status")
        if st and st.get("volume") is not None:
            self._dev_vol_guard = True
            self.dev_vol.set(st["volume"])
            self._dev_vol_guard = False

    # ------------------------------------------------ volume

    def _send_dev_vol(self):
        devs = self.monitor.snapshot()
        if self.selected in devs:
            self.monitor.set_volume(self.selected,
                                    devs[self.selected].get("port", PORT),
                                    self.dev_vol.get())

    def _on_dev_vol(self, _value):
        if self._dev_vol_guard or not self.selected:
            return
        # throttle mid-drag updates; the release handler sends the final value
        now = time.monotonic()
        if now - self._dev_vol_last_send > 0.15:
            self._dev_vol_last_send = now
            self._send_dev_vol()

    def _on_dev_vol_release(self, _event):
        if self.selected:
            self._send_dev_vol()

    def _on_pc_vol(self, value):
        self.pc_vol_gain = int(value) / 100.0
        if self.client:
            self.client.rx_volume = self.pc_vol_gain

    def _eff_state(self, ip, dev):
        """During a call, the call itself is the truth for that device -
        UDP status polls can be lost while the TCP audio is fine."""
        c = self.client
        if c and ip == self.talk_ip:
            return "linked" if c.linked() else "idle"
        return state_of(dev)

    # ------------------------------------------------ actions

    def toggle_talk(self):
        if self.client:
            self.client.stop()
            self.client = None
            self.talk_ip = None
            self.talk_btn.config(text="Talk")
            return
        devs = self.monitor.snapshot()
        if self.selected not in devs:
            return
        port = devs[self.selected].get("port", PORT)
        # audio rides TCP (immune to the router's UDP flow lottery);
        # UDP v2 with its own socket is the fallback for old firmware
        self.client = WalkieClient((self.selected, port))
        self.client.rx_volume = self.pc_vol_gain
        self.client.start()
        self.talk_ip = self.selected
        self.talk_btn.config(text="Stop")

    def apply_peer(self):
        devs = self.monitor.snapshot()
        if self.selected not in devs:
            return
        sel_port = devs[self.selected].get("port", PORT)
        choice = self.peer_var.get()
        if choice == "Auto":
            self.monitor.set_peer(self.selected, sel_port, None, 0)
            return
        for ip, dev in devs.items():
            if dev.get("name") == choice and ip != self.selected:
                # pair both directions so the two units talk to each other
                self.monitor.set_peer(self.selected, sel_port, ip,
                                      dev.get("port", PORT))
                self.monitor.set_peer(ip, dev.get("port", PORT),
                                      self.selected, sel_port)
                return

    # ------------------------------------------------ periodic refresh

    def _peer_label(self, devs, st):
        if not st or not st["peer_ip"]:
            return "-"
        ip = socket.inet_ntoa(struct.pack("<I", st["peer_ip"]))
        if ip in devs:
            return devs[ip].get("name", ip)
        if self.client and ip == local_ip_toward(self.selected or "8.8.8.8"):
            return "this PC"
        return ip

    def tick(self):
        # waveforms on a fixed 20 fps clock so their timebase never shifts
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        c = self.client
        if c:
            self.tx_wave.update(c.last_tx_pcm if not c.muted else b"")
            self.rx_wave.update(c.last_rx_pcm)
        else:
            self.tx_wave.update(b"")
            self.rx_wave.update(b"")

        # sidebar/status/menu at a gentler 2.5 Hz
        if self._tick_n % 8 == 1:
            devs = self.monitor.snapshot()
            self._rebuild_sidebar(devs)

            menu = self.peer_menu["menu"]
            menu.delete(0, "end")
            for opt in ["Auto"] + [d.get("name", ip)
                                   for ip, d in sorted(devs.items())
                                   if ip != self.selected]:
                menu.add_command(label=opt,
                                 command=lambda v=opt: self.peer_var.set(v))

            if self.selected and self.selected in devs:
                dev = devs[self.selected]
                st = dev.get("status")
                state = self._eff_state(self.selected, dev)
                self.title.config(
                    text=f"{dev.get('name', self.selected)}  ({state})")
                extra = "   [talking via TCP]" if (c and self.talk_ip ==
                                                   self.selected) else ""
                if st:
                    self.info.config(text=(
                        f"ip {self.selected}   rssi {st['rssi']} dBm   "
                        f"peer {self._peer_label(devs, st)}"
                        f"{'  [manual]' if st['locked'] else '  [auto]'}   "
                        f"muted {'yes' if st['muted'] else 'no'}"
                        f"{'   vol ' + str(st['volume']) if st.get('volume') is not None else ''}"
                        f"{extra}"))
                else:
                    self.info.config(
                        text=f"ip {self.selected}   (no status yet){extra}")
            elif self.selected:
                self.title.config(text=self.selected)

        self.root.after(50, self.tick)


def main():
    print("creating window...")
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    finally:
        if app.client:
            app.client.stop()
        app.monitor.stop()
    print("window closed.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("startup failed - press Enter to close")
