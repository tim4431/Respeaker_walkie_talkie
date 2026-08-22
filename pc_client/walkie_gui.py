#!/usr/bin/env python3
"""Tkinter GUI for the PC walkie-talkie client.

Shows link state, packet stats, live mic (TX) and device (RX) waveforms,
and a mute button. No dependencies beyond walkie_pc.py's (tkinter is in
the standard library).

    python walkie_gui.py [--ip 192.168.1.42]
"""

import argparse
import threading
import time
import tkinter as tk
from array import array

from walkie_pc import FRAME, PORT, WalkieClient, resolve_target

BG = "#1e1e28"
PANEL = "#28283a"
FG = "#e8e8f0"
DIM = "#8888a0"
TX_COLOR = "#ffb454"
RX_COLOR = "#54d68a"
WAVE_W = 460
WAVE_H = 90
WAVE_POINTS = 120  # 960 samples downsampled 8:1 - trivially cheap to draw


class WaveView:
    def __init__(self, parent, label, color):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(frame, text=label, fg=DIM, bg=BG, anchor="w",
                 font=("Segoe UI", 9)).pack(fill="x")
        self.canvas = tk.Canvas(frame, width=WAVE_W, height=WAVE_H,
                                bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="x")
        mid = WAVE_H / 2
        self.canvas.create_line(0, mid, WAVE_W, mid, fill="#3a3a50")
        flat = [c for x in range(WAVE_POINTS)
                for c in (x * WAVE_W / (WAVE_POINTS - 1), mid)]
        self.line = self.canvas.create_line(*flat, fill=color, width=2)

    def update(self, pcm_bytes):
        mid = WAVE_H / 2
        if not pcm_bytes:
            pts = [c for x in range(WAVE_POINTS)
                   for c in (x * WAVE_W / (WAVE_POINTS - 1), mid)]
        else:
            samples = array("h", pcm_bytes)
            step = max(1, len(samples) // WAVE_POINTS)
            pts = []
            for i in range(WAVE_POINTS):
                s = samples[min(i * step, len(samples) - 1)]
                y = mid - (s / 32768.0) * (mid - 4)
                pts += [i * WAVE_W / (WAVE_POINTS - 1), y]
        self.canvas.coords(self.line, *pts)


class App:
    def __init__(self, root, ip, port):
        self.root = root
        self.client = None
        self.prev_tx = 0
        self.prev_rx = 0
        self.prev_t = time.monotonic()

        root.title("Walkie-Talkie")
        root.configure(bg=BG)
        root.resizable(False, False)

        status_row = tk.Frame(root, bg=BG)
        status_row.pack(fill="x", padx=12, pady=(12, 0))
        self.dot = tk.Canvas(status_row, width=14, height=14, bg=BG,
                             highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=DIM, outline="")
        self.dot.pack(side="left")
        self.status = tk.Label(status_row, text="searching via mDNS...",
                               fg=FG, bg=BG, font=("Segoe UI", 11, "bold"))
        self.status.pack(side="left", padx=(6, 0))
        self.mute_btn = tk.Button(status_row, text="Mute", width=8,
                                  command=self.toggle_mute, state="disabled",
                                  bg=PANEL, fg=FG, activebackground="#3a3a55",
                                  activeforeground=FG, relief="flat")
        self.mute_btn.pack(side="right")

        self.stats = tk.Label(root, text="", fg=DIM, bg=BG, anchor="w",
                              font=("Consolas", 9))
        self.stats.pack(fill="x", padx=12, pady=(4, 0))

        self.tx_wave = WaveView(root, "Mic -> device (TX)", TX_COLOR)
        self.rx_wave = WaveView(root, "Device -> speakers (RX)", RX_COLOR)
        tk.Label(root, text="tip: use headphones - the PC has no echo canceller",
                 fg=DIM, bg=BG, font=("Segoe UI", 8)).pack(pady=(6, 10))

        threading.Thread(target=self.connect, args=(ip, port),
                         daemon=True).start()
        self.tick()

    def connect(self, ip, port):
        target, name = resolve_target(ip, port)
        if not target:
            self.root.after(0, lambda: self.status.config(
                text="no device found - restart with --ip"))
            return
        client = WalkieClient(target)
        client.start()

        def attach():
            self.client = client
            self.name = name
            self.mute_btn.config(state="normal")

        self.root.after(0, attach)

    def toggle_mute(self):
        if self.client:
            self.client.muted = not self.client.muted
            self.mute_btn.config(text="Unmute" if self.client.muted else "Mute")

    def tick(self):
        c = self.client
        if c:
            now = time.monotonic()
            dt = max(now - self.prev_t, 1e-6)
            tx_pps = (c.tx_count - self.prev_tx) / dt
            rx_pps = (c.rx_count - self.prev_rx) / dt
            self.prev_tx, self.prev_rx, self.prev_t = c.tx_count, c.rx_count, now

            if c.muted:
                color, text = "#c060e0", f"muted  ({self.name})"
            elif c.linked():
                color, text = "#40d070", f"linked: {self.name} @ {c.target[0]}"
            else:
                color, text = "#e0a040", f"sending to {c.target[0]} - no reply yet"
            self.dot.itemconfig(self.dot_id, fill=color)
            self.status.config(text=text)
            self.stats.config(text=(
                f"tx {tx_pps:5.1f} pkt/s   rx {rx_pps:5.1f} pkt/s   "
                f"buffer {c.jb_depth()} frames"))
            self.tx_wave.update(c.last_tx_pcm if not c.muted else b"")
            self.rx_wave.update(c.last_rx_pcm)
        self.root.after(50, self.tick)  # 20 fps; stats update piggybacks


def main():
    ap = argparse.ArgumentParser(description="walkie-talkie GUI")
    ap.add_argument("--ip", help="device IP (skip mDNS discovery)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    root = tk.Tk()
    app = App(root, args.ip, args.port)
    try:
        root.mainloop()
    finally:
        if app.client:
            app.client.stop()


if __name__ == "__main__":
    main()
