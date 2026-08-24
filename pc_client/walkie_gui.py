#!/usr/bin/env python3
"""Walkie group control center (Qt).

Top: one card per node (walkie units and this PC alike) with live status,
mic waveform, mute/TX-mode/volume controls. Bottom: group chat editor -
drag a node card into a group box; a node may be in several groups.

Groups are a GUI-side structure (gui_settings.json). What each device
actually stores (NVS, survives reboots, works with the GUI closed) is its
resulting send-list: the union of everyone it shares a group with. The GUI
keeps device send-lists reconciled with the group layout automatically.

Requires PySide6 (pip install PySide6).

    python walkie_gui.py     (or double-click start_gui.bat)
"""

import json
import math
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from pathlib import Path

try:
    from PySide6.QtCore import (Qt, QTimer, Signal, QObject, QSize, QMimeData,
                                QPoint, QPointF, QRectF, QByteArray)
    from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QIcon, QFont,
                               QPixmap, QDrag, QPainterPath, QPolygonF,
                               QFontMetrics)
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame,
                                   QLabel, QPushButton, QHBoxLayout,
                                   QVBoxLayout, QGridLayout, QScrollArea,
                                   QDialog, QPlainTextEdit, QLineEdit,
                                   QComboBox, QInputDialog, QSizePolicy)
except ImportError:
    print("PySide6 is required for this GUI:  python -m pip install PySide6")
    sys.exit(1)

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

BG      = "#eef1f7"
CARD    = "#ffffff"
CARD2   = "#f7f9fd"   # nested panels (node cards, group boxes)
FG      = "#16192b"
DIM     = "#5b6478"
BORDER  = "#e2e7f1"
ACCENT  = "#4263eb"
ACCENT2 = "#3b5bdb"
BTN_BG  = "#e9edf6"
BTN_HOV = "#dde3f0"
WAVE_BG = "#f1f4fa"

TX_COLOR = "#e8590c"
TX_SOFT  = "#f0a868"
RX_COLOR = "#099268"
MUTED_C  = "#ae3ec9"

COLORS = {
    "offline": "#9aa2b1",
    "idle": "#339af0",
    "linked": "#2f9e44",
    "muted": MUTED_C,
}

FONT = "Segoe UI"

QSS = f"""
* {{ font-family: "{FONT}"; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Panel {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QWidget#NodeCard {{
    background: {CARD2}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QWidget#GroupCard {{
    background: {CARD2}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QWidget#GroupCard[dropHover="true"] {{ border: 2px solid {ACCENT}; }}
QWidget#DropZone {{
    background: transparent; border: 2px dashed #c3ccdf; border-radius: 12px;
    color: {DIM};
}}
QWidget#DropZone[dropHover="true"] {{ border: 2px dashed {ACCENT}; }}
QLabel {{ color: {FG}; background: transparent; }}
QLabel[dim="true"] {{ color: {DIM}; }}
QPushButton {{
    background: {BTN_BG}; color: {FG}; border: none; border-radius: 7px;
    padding: 4px 12px; font-size: 10pt;
}}
QPushButton:hover {{ background: {BTN_HOV}; }}
QPushButton:disabled {{ color: #a3aabf; }}
QPushButton[accent="true"] {{ background: {ACCENT}; color: white; }}
QPushButton[accent="true"]:hover {{ background: {ACCENT2}; }}
QPushButton[small="true"] {{ padding: 3px 8px; font-size: 9pt; border-radius: 6px; }}
QPushButton[seg="true"] {{
    padding: 2px 8px; font-size: 8pt; border-radius: 5px; color: {DIM};
}}
QPushButton[seg="true"][on="true"] {{ background: {ACCENT}; color: white; }}
QPushButton[danger="true"] {{ background: {TX_COLOR}; color: white; }}
QPushButton[toggled="true"] {{ background: {MUTED_C}; color: white; }}
QPushButton[ghost="true"] {{ background: transparent; padding: 2px 6px; }}
QPushButton[ghost="true"]:hover {{ background: {BTN_BG}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:horizontal {{
    height: 8px; background: transparent; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #c8d0e2; border-radius: 4px; min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar:vertical {{ width: 8px; background: transparent; margin: 0; }}
QScrollBar::handle:vertical {{
    background: #c8d0e2; border-radius: 4px; min-height: 30px;
}}
QPlainTextEdit {{
    background: #f7f8fc; color: {FG}; border: 1px solid {BORDER};
    border-radius: 8px; font-family: Consolas; font-size: 9pt;
}}
QLineEdit {{
    background: #f1f4fa; color: {FG}; border: 1px solid {BORDER};
    border-radius: 7px; padding: 4px 8px;
}}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QComboBox {{
    background: {BTN_BG}; color: {FG}; border: none; border-radius: 7px;
    padding: 4px 10px;
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; color: {FG}; border: 1px solid {BORDER};
    selection-background-color: #dbe4ff; selection-color: {FG};
}}
QToolTip {{
    background: {FG}; color: white; border: none; padding: 3px 6px;
}}
"""

# ---------------------------------------------------------------- icons
# Feather-style stroke icons embedded as SVG so there is no asset folder
# and every icon can be tinted to any state color.

_ICON_BODIES = {
    "mic": '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>'
           '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/>'
           '<line x1="12" y1="19" x2="12" y2="23"/>'
           '<line x1="8" y1="23" x2="16" y2="23"/>',
    "mic-off": '<line x1="1" y1="1" x2="23" y2="23"/>'
               '<path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0'
               '-5.94-.6"/>'
               '<path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1'
               '-.11 1.23"/>'
               '<line x1="12" y1="19" x2="12" y2="23"/>'
               '<line x1="8" y1="23" x2="16" y2="23"/>',
    "speaker": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
               '<path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46'
               'a5 5 0 0 1 0 7.07"/>',
    "speaker-off": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
                   '<line x1="23" y1="9" x2="17" y2="15"/>'
                   '<line x1="17" y1="9" x2="23" y2="15"/>',
    "phone": '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63'
             '-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.12 4.18 2 2 0 0'
             ' 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0'
             ' 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11'
             '-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
    "phone-off": '<path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0'
                 ' 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7 2 2 0 0 1 1.72 2v3'
                 'a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.42 19.42'
                 ' 0 0 1-3.33-2.67m-2.67-3.34a19.79 19.79 0 0 1-3.07-8.63'
                 'A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0'
                 ' .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91"/>'
                 '<line x1="23" y1="1" x2="1" y2="23"/>',
    "radio": '<circle cx="12" cy="12" r="2"/>'
             '<path d="M16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.24a6 6 0 0 1 0'
             '-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0'
             '-14.14"/>',
    "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/>'
            '<path d="M1.42 9a16 16 0 0 1 21.16 0"/>'
            '<path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>'
            '<line x1="12" y1="20" x2="12.01" y2="20"/>',
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    "refresh": '<polyline points="23 4 23 10 17 10"/>'
               '<polyline points="1 20 1 14 7 14"/>'
               '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36'
               'A9 9 0 0 0 20.49 15"/>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/>'
            '<line x1="5" y1="12" x2="19" y2="12"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/>'
         '<line x1="6" y1="6" x2="18" y2="18"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
             '<circle cx="9" cy="7" r="4"/>'
             '<path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0'
             ' 7.75"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>'
               '<line x1="8" y1="21" x2="16" y2="21"/>'
               '<line x1="12" y1="17" x2="12" y2="21"/>',
    "cpu": '<rect x="4" y="4" width="16" height="16" rx="2" ry="2"/>'
           '<rect x="9" y="9" width="6" height="6"/>'
           '<line x1="9" y1="1" x2="9" y2="4"/>'
           '<line x1="15" y1="1" x2="15" y2="4"/>'
           '<line x1="9" y1="20" x2="9" y2="23"/>'
           '<line x1="15" y1="20" x2="15" y2="23"/>'
           '<line x1="20" y1="9" x2="23" y2="9"/>'
           '<line x1="20" y1="14" x2="23" y2="14"/>'
           '<line x1="1" y1="9" x2="4" y2="9"/>'
           '<line x1="1" y1="14" x2="4" y2="14"/>',
    "check": '<polyline points="20 6 9 17 4 12"/>',
    "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0'
             ' 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
             '<line x1="12" y1="9" x2="12" y2="13"/>'
             '<line x1="12" y1="17" x2="12.01" y2="17"/>',
    "sliders": '<line x1="4" y1="21" x2="4" y2="14"/>'
               '<line x1="4" y1="10" x2="4" y2="3"/>'
               '<line x1="12" y1="21" x2="12" y2="12"/>'
               '<line x1="12" y1="8" x2="12" y2="3"/>'
               '<line x1="20" y1="21" x2="20" y2="16"/>'
               '<line x1="20" y1="12" x2="20" y2="3"/>'
               '<line x1="1" y1="14" x2="7" y2="14"/>'
               '<line x1="9" y1="8" x2="15" y2="8"/>'
               '<line x1="17" y1="16" x2="23" y2="16"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "usb": '<line x1="22" y1="12" x2="2" y2="12"/>'
           '<path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6'
           'l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'
           '<line x1="6" y1="16" x2="6.01" y2="16"/>'
           '<line x1="10" y1="16" x2="10.01" y2="16"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
                '<polyline points="7 10 12 15 17 10"/>'
                '<line x1="12" y1="15" x2="12" y2="3"/>',
    "square": '<rect x="5" y="5" width="14" height="14" rx="2" ry="2"/>',
    "signal": '<line x1="6" y1="20" x2="6" y2="14"/>'
              '<line x1="12" y1="20" x2="12" y2="8"/>'
              '<line x1="18" y1="20" x2="18" y2="2"/>',
    "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"/>'
                   '<polyline points="12 5 19 12 12 19"/>',
    "arrow-left": '<line x1="19" y1="12" x2="5" y2="12"/>'
                  '<polyline points="12 19 5 12 12 5"/>',
    "edit": '<path d="M17 3a2.83 2.83 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5'
            'L17 3z"/>',
    "dot": '<circle cx="12" cy="12" r="4" fill="currentColor" '
           'stroke="none"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1'
                ' 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33'
                ' 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v'
                '-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06'
                '.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0'
                ' 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0'
                ' 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33'
                '-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06'
                'a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0'
                ' 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65'
                ' 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0'
                ' 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0'
                ' 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0'
                ' 0-1.51 1z"/>',
    # level bars with a dashed gate line across them (VOX threshold)
    "threshold": '<line x1="6" y1="20" x2="6" y2="13"/>'
                 '<line x1="12" y1="20" x2="12" y2="5"/>'
                 '<line x1="18" y1="20" x2="18" y2="15"/>'
                 '<line x1="2" y1="9" x2="22" y2="9" '
                 'stroke-dasharray="3 2.5"/>',
}

_icon_cache = {}


def icon(name, color=FG, size=18):
    key = (name, color, size)
    if key in _icon_cache:
        return _icon_cache[key]
    body = _ICON_BODIES[name].replace("currentColor", color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="2.2" '
           f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    dpr = QApplication.primaryScreen().devicePixelRatio() \
        if QApplication.primaryScreen() else 1.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    ic = QIcon(pm)
    _icon_cache[key] = ic
    return ic


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


# ---------------------------------------------------------------- settings


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
                targets = [(ip, d.get("port", PORT))
                           for ip, d in self.devices.items()]
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
        self._ctrl(ip, port, struct.pack("<BB", CTRL_SET_MUTE,
                                         1 if muted else 0))

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


# ---------------------------------------------------------------- levels


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


# ---------------------------------------------------------------- widgets


class Waveform(QWidget):
    """Small per-card level history with an optional VOX gate guide."""

    BARS = 44

    def __init__(self, color, height=40, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMinimumWidth(200)
        self.color = QColor(color)
        self.levels = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.gate = None
        self.gate_on = True

    def push(self, level, gate=None, gate_on=True):
        self.levels.append(max(0.0, min(1.0, level)))
        self.gate = gate
        self.gate_on = gate_on
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(WAVE_BG))
        p.drawRoundedRect(QRectF(0, 0, w, h), 7, 7)
        mid = h / 2
        span = mid - 5
        step = w / (self.BARS + 1)
        pen = QPen(self.color, 3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for i, lv in enumerate(self.levels):
            x = (i + 1) * step
            hh = max(1.2, lv * span)
            p.drawLine(QPointF(x, mid - hh), QPointF(x, mid + hh))
        # dashed lines showing where the voice gate opens: solid grey while
        # the gate is armed, pale when the node transmits unconditionally
        # and the line is only a preview
        if self.gate is not None:
            gh = max(1.0, min(1.0, self.gate) * span)
            gp = QPen(QColor("#7c869c" if self.gate_on else "#ccd3e0"), 1)
            gp.setStyle(Qt.CustomDashLine)
            gp.setDashPattern([3, 3])
            p.setPen(gp)
            p.drawLine(QPointF(3, mid - gh), QPointF(w - 3, mid - gh))
            p.drawLine(QPointF(3, mid + gh), QPointF(w - 3, mid + gh))
        p.end()


class Bar(QWidget):
    """Flat 0-vmax slider: rounded groove, colored fill, round knob."""

    changed = Signal(int)    # while dragging / on click
    released = Signal(int)   # on mouse release

    def __init__(self, color, vmax=100, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.color = QColor(color)
        self.vmax = vmax
        self.value = 0

    def set(self, v):
        self.value = max(0, min(self.vmax, int(v)))
        self.update()

    def get(self):
        return self.value

    PAD = 8   # keeps the knob inside the widget at 0 and vmax

    def _from_pos(self, x):
        w = max(self.width(), 2 * self.PAD + 2)
        v = round(max(0.0, min(1.0, (x - self.PAD) / (w - 2 * self.PAD)))
                  * self.vmax)
        if v != self.value:
            self.value = v
            self.update()
            self.changed.emit(v)

    def mousePressEvent(self, e):
        self._from_pos(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._from_pos(e.position().x())

    def mouseReleaseEvent(self, _e):
        self.released.emit(self.value)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2
        pad = self.PAD
        groove = QRectF(pad, mid - 3, w - 2 * pad, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#dbe1ee"))
        p.drawRoundedRect(groove, 3, 3)
        x = pad + (w - 2 * pad) * self.value / self.vmax
        if x > pad + 1:
            p.setBrush(self.color)
            p.drawRoundedRect(QRectF(pad, mid - 3, x - pad, 6), 3, 3)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(self.color, 2))
        p.drawEllipse(QPointF(x, mid), 6, 6)
        p.end()


class Dot(QWidget):
    """Node state indicator."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.color = QColor(COLORS["offline"])

    def set_color(self, c):
        if self.color.name() != QColor(c).name():
            self.color = QColor(c)
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(QRectF(1.5, 1.5, 9, 9))
        p.end()


class Badge(QLabel):
    """SPEAKING / ON AIR / HEARING / MUTED chip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont(FONT, 7, QFont.Bold))
        self._bg = None
        self.setVisible(False)

    def set_state(self, text, bg):
        if text == self.text() and bg == self._bg:
            return
        self._bg = bg
        self.setText(text)
        if not text:
            self.setVisible(False)
            return
        self.setStyleSheet(f"background: {bg}; color: white; "
                           f"border-radius: 5px; padding: 2px 7px;")
        self.setVisible(True)


class IconButton(QPushButton):
    """Small flat icon button whose icon recolors with its toggle state."""

    def __init__(self, icon_name, tooltip, parent=None, text=""):
        super().__init__(parent)
        self.icon_name = icon_name
        self._state = None
        if text:
            self.setText(text)
        self.setToolTip(tooltip)
        self.setProperty("small", True)
        self.setIconSize(QSize(15, 15))
        self.setCursor(Qt.PointingHandCursor)
        self.apply(icon_name, False)

    def apply(self, icon_name, on, on_color=MUTED_C, prop="toggled"):
        state = (icon_name, on, on_color, prop)
        if state == self._state:
            return
        self._state = state
        self.icon_name = icon_name
        self.setIcon(icon(icon_name, "#ffffff" if on else FG, 15))
        self.setProperty("toggled", on if prop == "toggled" else False)
        self.setProperty("danger", on if prop == "danger" else False)
        self.setProperty("accent", on if prop == "accent" else False)
        self.setStyleSheet(f"QPushButton {{ background: {on_color}; }}"
                           if on and prop == "toggled" else "")
        repolish(self)


DRAG_MIME = "application/x-walkie-node"


class DragHeader(QWidget):
    """Card region that starts a node drag (header row + waveform)."""

    def __init__(self, key_fn, name_fn, parent=None):
        super().__init__(parent)
        self.key_fn = key_fn
        self.name_fn = name_fn
        self._press = None
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.LeftButton):
            return
        if (e.position().toPoint() - self._press).manhattanLength() < 10:
            return
        self._press = None
        mime = QMimeData()
        mime.setData(DRAG_MIME, self.key_fn().encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        # ghost label following the cursor
        name = "  " + self.name_fn() + "  "
        f = QFont(FONT, 10, QFont.Bold)
        fm = QFontMetrics(f)
        wpx = fm.horizontalAdvance(name) + 12
        pm = QPixmap(wpx, 26)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(0, 0, wpx, 26), 7, 7)
        p.setPen(QColor("white"))
        p.setFont(f)
        p.drawText(QRectF(0, 0, wpx, 26), Qt.AlignCenter, name)
        p.end()
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(wpx // 2, 13))
        drag.exec(Qt.CopyAction)


class GroupGraph(QWidget):
    """Live hub-and-spoke per group: who is in it, who is broadcasting."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(236, 172)
        self.members = []
        self.info = {}

    def set_data(self, members, info):
        self.members = members
        self.info = info
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(QColor("#fbfcfe"))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 9, 9)
        cx, cy = w / 2, h / 2 - 4
        f_small = QFont(FONT, 7)
        if not self.members:
            p.setPen(QColor(DIM))
            f = QFont(FONT, 9)
            f.setItalic(True)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "drop nodes here")
            p.end()
            return
        info = self.info
        talkers = [k for k in self.members
                   if info.get(k) and info[k]["tx"] and info[k]["voice"]]
        pulse = 2.5 + 1.5 * math.sin(time.monotonic() * 6)
        n = len(self.members)
        radius = min(w, h) * 0.34
        pos = {}
        for i, key in enumerate(self.members):
            ang = -math.pi / 2 + i * (2 * math.pi / n)
            pos[key] = (cx + radius * math.cos(ang),
                        cy + radius * 0.78 * math.sin(ang), ang)
        # spokes first (under the discs)
        for key in self.members:
            node = info.get(key)
            x, y, _ = pos[key]
            speaking = bool(node and node["tx"] and node["voice"])
            on_air = bool(node and node["tx"] and not speaking)
            if speaking:
                pen = QPen(QColor(TX_COLOR), 3)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(x, y), QPointF(cx, cy))
                # arrowhead toward the hub
                ang = math.atan2(cy - y, cx - x)
                ax, ay = cx - 18 * math.cos(ang), cy - 18 * math.sin(ang)
                arrow = QPolygonF([
                    QPointF(ax + 7 * math.cos(ang), ay + 7 * math.sin(ang)),
                    QPointF(ax + 5 * math.cos(ang + 2.4),
                            ay + 5 * math.sin(ang + 2.4)),
                    QPointF(ax + 5 * math.cos(ang - 2.4),
                            ay + 5 * math.sin(ang - 2.4))])
                p.setBrush(QColor(TX_COLOR))
                p.setPen(Qt.NoPen)
                p.drawPolygon(arrow)
            elif on_air:
                p.setPen(QPen(QColor(TX_SOFT), 2))
                p.drawLine(QPointF(x, y), QPointF(cx, cy))
            else:
                p.setPen(QPen(QColor(BORDER), 1))
                p.drawLine(QPointF(cx, cy), QPointF(x, y))
        # hub: lights up while someone holds the floor
        hub_r = 15
        p.setPen(QPen(QColor(TX_COLOR if talkers else ACCENT), 2))
        p.setBrush(QColor("#fff2e8" if talkers else "#eef2ff"))
        p.drawEllipse(QPointF(cx, cy), hub_r, hub_r)
        p.setPen(QColor(TX_COLOR if talkers else ACCENT))
        p.setFont(QFont(FONT, 6, QFont.Bold))
        p.drawText(QRectF(cx - hub_r, cy - hub_r, hub_r * 2, hub_r * 2),
                   Qt.AlignCenter, "((•))" if talkers else "•••")
        # node discs
        r = 15
        for key in self.members:
            node = info.get(key)
            x, y, ang = pos[key]
            online = bool(node and node["online"])
            muted = bool(node and node["muted"])
            speaking = bool(node and node["tx"] and node["voice"])
            hearing = bool(node and node["rx"])
            col = (COLORS["muted"] if muted else
                   COLORS["linked"] if online else COLORS["offline"])
            if speaking:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(TX_COLOR), 2))
                rr = r + 3 + pulse
                p.drawEllipse(QPointF(x, y), rr, rr)
            elif hearing:
                pen = QPen(QColor(RX_COLOR), 1)
                pen.setStyle(Qt.DashLine)
                p.setBrush(Qt.NoBrush)
                p.setPen(pen)
                p.drawEllipse(QPointF(x, y), r + 3, r + 3)
            p.setBrush(QColor(CARD))
            p.setPen(QPen(QColor(col), 2))
            p.drawEllipse(QPointF(x, y), r, r)
            p.setPen(QColor(FG if online else DIM))
            p.setFont(QFont(FONT, 7, QFont.Bold))
            p.drawText(QRectF(x - r, y - r, r * 2, r * 2), Qt.AlignCenter,
                       "PC" if key == "pc" else "WT")
            label = "This PC" if key == "pc" else (
                node["name"] if node else key)
            if len(label) > 14:
                label = label[:13] + "…"
            ly = y + r + 4 if math.sin(ang) >= -0.1 else y - r - 16
            p.setFont(f_small)
            p.drawText(QRectF(x - 60, ly, 120, 12), Qt.AlignCenter, label)
        status = ("  ".join(("This PC" if k == "pc" else k) + " speaking"
                            for k in talkers[:2]) if talkers else "idle")
        p.setPen(QColor(TX_COLOR if talkers else DIM))
        f = QFont(FONT, 7)
        f.setBold(bool(talkers))
        p.setFont(f)
        p.drawText(QRectF(0, h - 16, w, 12), Qt.AlignCenter, status)
        p.end()


class DropFrame(QWidget):
    """Widget that accepts node-card drops and reports them to a callback."""

    def __init__(self, on_drop, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.on_drop = on_drop

    def _hover(self, on):
        self.setProperty("dropHover", on)
        repolish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(DRAG_MIME):
            self._hover(True)
            e.acceptProposedAction()

    def dragLeaveEvent(self, _e):
        self._hover(False)

    def dropEvent(self, e):
        self._hover(False)
        key = bytes(e.mimeData().data(DRAG_MIME)).decode()
        e.acceptProposedAction()
        self.on_drop(key)


# ---------------------------------------------------------------- node card


class NodeCard(QWidget):
    """One node: status, waveform, mute/speaker/call/AGC, mode, sliders."""

    def __init__(self, app, node, parent=None):
        super().__init__(parent)
        self.app = app
        self.nid = node["id"]
        self.setObjectName("NodeCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(268)
        self.vol_init = self.gain_init = self.sens_init = False
        self.last_vol_send = self.last_gain_send = self.last_sens_send = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        self.header = DragHeader(
            lambda: self.app.key_of(self.nid),
            lambda: self.app.name_of(self.nid))
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        self.dot = Dot()
        hl.addWidget(self.dot)
        kind = QLabel()
        kind.setPixmap(icon("monitor" if node["is_pc"] else "radio",
                            DIM, 15).pixmap(15, 15))
        hl.addWidget(kind)
        self.name = QLabel(node["name"])
        self.name.setFont(QFont(FONT, 10, QFont.Bold))
        self.name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        hl.addWidget(self.name, 1)
        # mute buttons live in the header row
        self.b_mute = IconButton("mic", "Toggle microphone mute")
        self.b_mute.clicked.connect(lambda: app.toggle_mute(self.nid))
        self.b_mute.setFixedWidth(30)
        hl.addWidget(self.b_mute)
        self.b_spk = IconButton("speaker", "Toggle speaker mute")
        self.b_spk.clicked.connect(lambda: app.toggle_spk_mute(self.nid))
        self.b_spk.setFixedWidth(30)
        hl.addWidget(self.b_spk)
        if not node["is_pc"]:
            b_cfg = IconButton("settings", "Device settings…")
            b_cfg.clicked.connect(lambda: app.open_device_config(self.nid))
            b_cfg.setFixedWidth(30)
            hl.addWidget(b_cfg)
        lay.addWidget(self.header)

        # the waveform doubles as a drag handle: it ignores mouse events and
        # its DragHeader wrapper picks them up
        self.wave = Waveform(TX_COLOR if node["is_pc"] else RX_COLOR,
                             height=40)
        self.wave.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._wrap_drag(self.wave))

        # status text left, live badge right
        sub_row = QHBoxLayout()
        sub_row.setSpacing(4)
        self.sub = QLabel("")
        self.sub.setProperty("dim", True)
        self.sub.setFont(QFont(FONT, 8))
        sub_row.addWidget(self.sub)
        sub_row.addStretch(1)
        self.badge = Badge()
        sub_row.addWidget(self.badge)
        lay.addLayout(sub_row)

        # one row: TX mode segments (the active one is filled) + AGC + call.
        # Devices offer voice (VAD) and gate (plain level threshold); the PC
        # has no VAD, so its only gated mode IS the gate.
        seg_row = QHBoxLayout()
        seg_row.setSpacing(3)
        self.seg = {}
        modes = [(0, "always"), (2, "gate")] if node["is_pc"] else \
                [(0, "always"), (1, "voice"), (2, "gate")]
        for m, label in modes:
            b = QPushButton(label)
            b.setProperty("seg", True)
            b.setToolTip("TX mode: " + {0: "transmit continuously",
                                        1: "voice activated (device VAD)",
                                        2: "open above the level gate"}[m])
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, mm=m:
                              app.set_tx_mode(self.nid, mm))
            self.seg[m] = b
            seg_row.addWidget(b)
        seg_row.addStretch(1)
        self.b_call = self.b_agc = None
        if not node["is_pc"]:
            self.b_agc = IconButton("zap", "Automatic gain control "
                                    "(runs on the device)", text="AGC")
            self.b_agc.clicked.connect(lambda: app.toggle_agc(self.nid))
            seg_row.addWidget(self.b_agc)
            self.b_call = IconButton("phone", "Direct 1:1 call with this "
                                     "device")
            self.b_call.setFixedWidth(30)
            self.b_call.clicked.connect(lambda: app.toggle_talk(self.nid))
            seg_row.addWidget(self.b_call)
        lay.addLayout(seg_row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        def bar_row(r, icon_name, tip, color, on_change, on_final, vmax=100):
            ic = QLabel()
            ic.setPixmap(icon(icon_name, DIM, 14).pixmap(14, 14))
            ic.setToolTip(tip)
            grid.addWidget(ic, r, 0)
            bar = Bar(color, vmax=vmax)
            bar.setToolTip(tip)
            bar.changed.connect(on_change)
            bar.released.connect(on_final)
            grid.addWidget(bar, r, 1)
            pct = QLabel("")
            pct.setFont(QFont(FONT, 8, QFont.Bold))
            pct.setFixedWidth(34)
            pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(pct, r, 2)
            return bar, pct

        self.vol, self.vol_pct = bar_row(
            0, "speaker", "Speaker volume", ACCENT,
            lambda v: app.on_card_vol(self.nid, v, False),
            lambda v: app.on_card_vol(self.nid, v, True))
        self.gain, self.gain_pct = bar_row(
            1, "mic", "Microphone gain", TX_COLOR,
            lambda v: app.on_card_gain(self.nid, v, False),
            lambda v: app.on_card_gain(self.nid, v, True), vmax=200)
        self.sens, self.sens_pct = bar_row(
            2, "threshold", "Voice gate threshold", RX_COLOR,
            lambda v: app.on_card_thresh(self.nid, v, False),
            lambda v: app.on_card_thresh(self.nid, v, True))
        lay.addLayout(grid)

    def _wrap_drag(self, inner):
        """Waveform doubles as a drag handle, like the tk version."""
        holder = DragHeader(lambda: self.app.key_of(self.nid),
                            lambda: self.app.name_of(self.nid))
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(inner)
        return holder

    # ---- refresh from a node dict (see App.nodes())

    def update_node(self, n):
        self.dot.set_color(COLORS[n["state"]])
        fm = self.name.fontMetrics()
        self.name.setText(fm.elidedText(n["name"], Qt.ElideRight,
                                        max(60, self.name.width())))
        self.name.setToolTip(n["name"])
        self.sub.setText(n["sub"])
        # "SPEAKING" must mean voice, not merely transmitting: an always-mode
        # node sends every frame, so it would otherwise never stop saying it
        if n["muted"]:
            self.badge.set_state("MUTED", COLORS["muted"])
        elif n["tx"] and n["voice"]:
            self.badge.set_state("SPEAKING", TX_COLOR)
        elif n["tx"]:
            self.badge.set_state("ON AIR", TX_SOFT)
        elif n["rx"]:
            self.badge.set_state("HEARING", RX_COLOR)
        else:
            self.badge.set_state("", CARD2)
        st = (n["dev"] or {}).get("status") or {}
        self.b_mute.apply("mic-off" if n["muted"] else "mic", n["muted"])
        app = self.app
        if n["is_pc"]:
            mode = 2 if app.tx_mode == "vox" else 0
            spk_muted = app.pc_spk_muted
        else:
            mode = int(st.get("tx_mode") or 0)
            spk_muted = st.get("volume") == 0
        self.b_spk.apply("speaker-off" if spk_muted else "speaker", spk_muted)
        for m, b in self.seg.items():
            on = mode == m
            if b.property("on") != on:
                b.setProperty("on", on)
                repolish(b)
        if self.b_agc is not None:
            on = bool(st.get("mic_agc"))
            live = st.get("agc_gain")
            self.b_agc.setText(f"×{live:.1f}" if (on and live) else "AGC")
            self.b_agc.apply("zap", on, on_color=ACCENT)
        if self.b_call is not None:
            in_call = app.client and app.talk_ip == self.nid
            self.b_call.setToolTip("End the call" if in_call else
                                   "Direct 1:1 call with this device")
            self.b_call.apply("phone-off" if in_call else "phone",
                              bool(in_call), on_color=TX_COLOR)
            self.b_call.setEnabled(not (app.group_client and not in_call))
        # sliders reflect the node's stored values until the user touches them
        if n["is_pc"]:
            vol_val = min(100, int(app.settings.get("pc_vol", 100)))
            sens_val = int(app.settings.get("pc_thresh", 30))
            gain_val = int(app.settings.get("pc_gain", 100))
        else:
            vol_val = st.get("volume")
            sens_val = st.get("vox_thresh")
            gain_val = st.get("mic_gain")
        if vol_val is not None and not self.vol_init:
            self.vol.set(vol_val)
            self.vol_init = True
        if sens_val is not None and not self.sens_init:
            self.sens.set(sens_val)
            self.sens_init = True
        if gain_val is not None and not self.gain_init:
            self.gain.set(gain_val)
            self.gain_init = True
        self.vol_pct.setText(f"{self.vol.get()}%")
        self.gain_pct.setText(f"{self.gain.get()}%")
        self.sens_pct.setText(f"{self.sens.get()}")


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


def wifi_input_error(ssid, pwd):
    """Returns a message if the SSID/password can't be sent, else None."""
    if not ssid or " " in ssid:
        return "SSID must be non-empty and contain no spaces"
    if not 8 <= len(pwd) <= 64:
        return "password must be 8-64 characters"
    return None


def store_wifi_via_serial(port, ssid, pwd, set_status):
    """Drive the device's serial console to store WiFi credentials in NVS.
    Blocking (run on a worker thread); returns True on WTCFG OK."""
    try:
        import serial
    except ImportError:
        set_status("pyserial is not installed: pip install pyserial")
        return False
    try:
        s = serial.Serial()
        s.port = port
        s.baudrate = 115200
        s.timeout = 0.5
        s.dtr = False       # avoid yanking the reset lines
        s.rts = False
        s.open()
    except OSError as e:
        set_status(f"cannot open {port}: {e}")
        return False
    try:
        set_status("port open - waiting for the device…")
        time.sleep(3.0)     # opening the port can reboot it
        s.reset_input_buffer()
        s.write(f"wifi {ssid} {pwd}\n".encode())
        deadline = time.monotonic() + 8
        buf = b""
        while time.monotonic() < deadline:
            buf += s.read(256)
            if b"WTCFG OK" in buf:
                return True
            if b"WTCFG ERR" in buf:
                line = [l for l in buf.splitlines()
                        if b"WTCFG ERR" in l][-1]
                set_status(line.decode(errors="replace"))
                return False
        set_status("No reply. Is this the walkie's XIAO port, "
                   "running current firmware?")
    except OSError as e:
        set_status(f"serial error: {e}")
    finally:
        try:
            s.close()
        except OSError:
            pass
    return False


def serial_ports():
    """XIAO COM ports first, all ports as a fallback; [] without pyserial."""
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
        return ports or [p.device for p in list_ports.comports()]
    except ImportError:
        return []


class SetupWizard(QDialog):
    """Guided first-time setup: detect -> flash -> WiFi -> verify."""

    STEPS = ("Connect", "Flash", "WiFi", "Done")
    STEP_ICONS = ("usb", "zap", "wifi", "check")

    log_sig = Signal(str)
    status_sig = Signal(str)
    flash_done_sig = Signal(object)
    usb_sig = Signal()

    def __init__(self, app, start=0):
        super().__init__(app)
        self.app = app
        self.step = start
        self.proc = None
        self.usb = {}
        self.log = None
        self.setWindowTitle("Set up a walkie device")
        self.setWindowIcon(icon("radio", ACCENT, 24))
        self.resize(660, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(panel)
        root = QVBoxLayout(panel)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        crumbs = QHBoxLayout()
        crumbs.setSpacing(6)
        self.crumbs = []
        for i, name in enumerate(self.STEPS):
            b = QPushButton(f" {i + 1} · {name}")
            b.setProperty("small", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, ii=i: self.goto(ii))
            crumbs.addWidget(b)
            self.crumbs.append(b)
        crumbs.addStretch(1)
        root.addLayout(crumbs)

        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 4, 0, 4)
        self.body_lay.setSpacing(6)
        root.addWidget(self.body, 1)

        foot = QHBoxLayout()
        self.status = QLabel("")
        self.status.setProperty("dim", True)
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        self.back_btn = QPushButton(" Back")
        self.back_btn.setIcon(icon("arrow-left", FG, 15))
        self.back_btn.clicked.connect(lambda: self.goto(self.step - 1))
        foot.addWidget(self.back_btn)
        self.next_btn = QPushButton(" Next")
        self.next_btn.setIcon(icon("arrow-right", "#ffffff", 15))
        self.next_btn.setProperty("accent", True)
        self.next_btn.clicked.connect(self.next_step)
        foot.addWidget(self.next_btn)
        root.addLayout(foot)

        self.log_sig.connect(self._append_log)
        self.status_sig.connect(self.status.setText)
        self.flash_done_sig.connect(self._flash_finished)
        self.usb_sig.connect(self._usb_ready)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._poll_found)

        self.refresh_usb()
        self.render()

    # ---- lifecycle

    def closeEvent(self, e):
        if self.proc and self.proc.poll() is None:
            self.status.setText("A flash is still running - it will keep "
                                "going in the background.")
        self.app.wizard = None
        super().closeEvent(e)

    def set_status(self, text):
        self.status_sig.emit(text)

    def goto(self, i):
        self.step = max(0, min(len(self.STEPS) - 1, i))
        self.render()

    def next_step(self):
        if self.step >= len(self.STEPS) - 1:
            self.close()
        else:
            self.goto(self.step + 1)

    def refresh_usb(self):
        def work():
            self.usb = probe_usb()
            self.usb_sig.emit()
        threading.Thread(target=work, daemon=True).start()

    def _usb_ready(self):
        if self.step in (0, 1, 2):
            self.render()

    # ---- rendering

    def render(self):
        for i, b in enumerate(self.crumbs):
            active = i == self.step
            b.setProperty("accent", active)
            b.setIcon(icon(self.STEP_ICONS[i],
                           "#ffffff" if active else DIM, 14))
            repolish(b)
        self.log = None
        clear_layout(self.body_lay)
        self.back_btn.setEnabled(self.step > 0)
        last = self.step == len(self.STEPS) - 1
        self.next_btn.setText(" Close" if last else " Next")
        self.next_btn.setIcon(icon("check" if last else "arrow-right",
                                   "#ffffff", 15))
        self.poll_timer.stop()
        [self._step_connect, self._step_flash, self._step_wifi,
         self._step_done][self.step]()

    def _title(self, text, sub=""):
        t = QLabel(text)
        t.setFont(QFont(FONT, 13, QFont.Bold))
        self.body_lay.addWidget(t)
        if sub:
            s = QLabel(sub)
            s.setProperty("dim", True)
            s.setWordWrap(True)
            self.body_lay.addWidget(s)

    def _row(self, lay, ok, text, warn=False):
        row = QHBoxLayout()
        row.setSpacing(8)
        mark = QLabel()
        name = "check" if ok else ("alert" if warn else "dot")
        col = COLORS["linked"] if ok else (TX_COLOR if warn else "#c3ccdf")
        mark.setPixmap(icon(name, col, 15).pixmap(15, 15))
        row.addWidget(mark, 0, Qt.AlignTop)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        if not (ok or warn):
            lbl.setProperty("dim", True)
        row.addWidget(lbl, 1)
        lay.addLayout(row)

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
        box = QWidget()
        box.setObjectName("GroupCard")
        box.setAttribute(Qt.WA_StyledBackground, True)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 10)
        cap = QLabel("DETECTED")
        cap.setProperty("dim", True)
        cap.setFont(QFont(FONT, 8, QFont.Bold))
        bl.addWidget(cap)
        if not u:
            self._row(bl, False, "scanning…")
        else:
            port = u.get("xiao_port")
            self._row(bl, bool(port),
                      f"XIAO ESP32-S3 on {port}" if port else
                      "XIAO ESP32-S3 not found (plug in the XIAO's USB-C)")
            rev = u.get("xmos_rev")
            if u.get("xmos"):
                ok = rev == XMOS_TARGET_REV
                detail = f", firmware rev {rev:04x}" if rev is not None else ""
                self._row(bl, ok, f"ReSpeaker XMOS in DFU mode{detail}",
                          warn=not ok)
                if not ok:
                    self._row(bl, False,
                              "needs the 48 kHz I2S build (rev 0110) - "
                              "step 2 will flash it", warn=True)
            else:
                self._row(bl, False,
                          "ReSpeaker XMOS not detected (only needed if its "
                          "audio firmware is not installed yet)")
            if u.get("usb_audio"):
                self._row(bl, False,
                          "DANGER: a ReSpeaker in USB-audio mode is connected "
                          "(a PC microphone?). Unplug it before XMOS flashing "
                          "- the flash script refuses to run otherwise.",
                          warn=True)
            if u.get("error"):
                self._row(bl, False, u["error"], warn=True)
        self.body_lay.addWidget(box)
        rescan = QPushButton(" Re-scan USB")
        rescan.setIcon(icon("refresh", FG, 15))
        rescan.clicked.connect(self.refresh_usb)
        self.body_lay.addWidget(rescan, 0, Qt.AlignLeft)
        self.body_lay.addStretch(1)
        if not u:
            self.set_status("scanning USB…")
        elif u.get("usb_audio"):
            self.set_status("A ReSpeaker in USB-audio mode is connected. "
                            "Unplug it before flashing the XMOS.")
        elif u.get("xiao_port"):
            self.set_status("XIAO found - continue to flashing.")
        else:
            self.set_status("Plug in the XIAO's USB-C port, then Re-scan.")

    # ---- step 2: flash

    def _step_flash(self):
        self._title(
            "Flash the firmware",
            "Runs tools/flash_all.ps1. The XMOS step is skipped automatically "
            "when its firmware is already correct. A full ESP32 build can "
            "take a few minutes the first time.")
        btns = QHBoxLayout()
        self.b_both = QPushButton(" Flash both")
        self.b_both.setIcon(icon("zap", "#ffffff", 15))
        self.b_both.setProperty("accent", True)
        self.b_both.clicked.connect(lambda: self.run_flash([]))
        btns.addWidget(self.b_both)
        self.b_esp = QPushButton(" ESP32 only")
        self.b_esp.setIcon(icon("cpu", FG, 15))
        self.b_esp.clicked.connect(lambda: self.run_flash(["-EspOnly"]))
        btns.addWidget(self.b_esp)
        self.b_xmos = QPushButton(" XMOS only")
        self.b_xmos.setIcon(icon("cpu", FG, 15))
        self.b_xmos.clicked.connect(lambda: self.run_flash(["-XmosOnly"]))
        btns.addWidget(self.b_xmos)
        btns.addStretch(1)
        self.b_stop = QPushButton(" Stop")
        self.b_stop.setIcon(icon("square", FG, 15))
        self.b_stop.clicked.connect(self.stop_flash)
        self.b_stop.setEnabled(False)
        btns.addWidget(self.b_stop)
        self.body_lay.addLayout(btns)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.body_lay.addWidget(self.log, 1)
        self.log.setPlainText("Ready." if FLASH_SCRIPT.exists() else
                              f"flash script not found at {FLASH_SCRIPT}")
        running = self.proc is not None and self.proc.poll() is None
        if not FLASH_SCRIPT.exists() or running:
            for b in (self.b_both, self.b_esp, self.b_xmos):
                b.setEnabled(False)
        if running:
            self.b_stop.setEnabled(True)
        self.set_status("Zadig may open on the first XMOS flash per PC: "
                        "select ReSpeaker Lite, then Install Driver.")

    def _append_log(self, text):
        if self.log is None:
            return
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)

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
        self.log_sig.emit("\n$ " + " ".join(cmd) + "\n")
        for b in (self.b_both, self.b_esp, self.b_xmos):
            b.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.set_status("flashing…")

        def work():
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    encoding="utf-8", errors="replace",
                    creationflags=NO_WINDOW)
            except OSError as e:
                self.log_sig.emit(f"failed to start: {e}\n")
                self.status_sig.emit("could not start PowerShell")
                self.flash_done_sig.emit(None)
                return
            for line in self.proc.stdout:
                self.log_sig.emit(line)
            code = self.proc.wait()
            self.flash_done_sig.emit(code)

        threading.Thread(target=work, daemon=True).start()

    def _flash_finished(self, code):
        if self.step == 1:
            for b in (self.b_both, self.b_esp, self.b_xmos):
                b.setEnabled(FLASH_SCRIPT.exists())
            self.b_stop.setEnabled(False)
        if code == 0:
            self.set_status("Flash finished. Continue to WiFi setup.")
        elif code is not None:
            self.set_status(f"Flash exited with code {code} - check the "
                            f"log above.")
        self.refresh_usb()

    def stop_flash(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log_sig.emit("\n[stopped by user]\n")

    # ---- step 3: wifi

    def _step_wifi(self):
        self._title(
            "Give it WiFi",
            "Credentials are stored on the device itself (NVS) and override "
            "anything compiled into the firmware, so a unit can be moved to "
            "another network without reflashing. Applying reboots the device.")
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        ports = serial_ports()
        if not ports:
            self._row(self.body_lay, False,
                      "no COM port found - is pyserial installed and the "
                      "XIAO plugged in?", warn=True)

        def lab(text, r):
            l = QLabel(text)
            l.setProperty("dim", True)
            form.addWidget(l, r, 0, Qt.AlignRight)

        lab("port", 0)
        self.port_combo = QComboBox()
        self.port_combo.addItems(ports or [""])
        pref = self.usb.get("xiao_port")
        if pref and pref in ports:
            self.port_combo.setCurrentText(pref)
        form.addWidget(self.port_combo, 0, 1, Qt.AlignLeft)
        lab("SSID", 1)
        self.ssid_edit = QLineEdit()
        self.ssid_edit.setFixedWidth(240)
        form.addWidget(self.ssid_edit, 1, 1, Qt.AlignLeft)
        lab("password", 2)
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setFixedWidth(240)
        form.addWidget(self.pwd_edit, 2, 1, Qt.AlignLeft)
        form.setColumnStretch(2, 1)
        self.body_lay.addLayout(form)
        apply_btn = QPushButton(" Store WiFi on the device")
        apply_btn.setIcon(icon("wifi", "#ffffff", 15))
        apply_btn.setProperty("accent", True)
        apply_btn.clicked.connect(self.apply_wifi)
        self.body_lay.addWidget(apply_btn, 0, Qt.AlignLeft)
        note = QLabel("SSIDs containing spaces are not supported over this "
                      "path; the password must be 8-64 characters.")
        note.setProperty("dim", True)
        note.setWordWrap(True)
        self.body_lay.addWidget(note)
        self.body_lay.addStretch(1)
        self.set_status("Plug in the XIAO's USB-C, then store your network.")

    def apply_wifi(self):
        ssid = self.ssid_edit.text().strip()
        pwd = self.pwd_edit.text()
        port = self.port_combo.currentText()
        err = ("no COM port found" if not port else
               wifi_input_error(ssid, pwd))
        if err:
            self.set_status(err)
            return
        self.set_status("connecting to the device…")
        threading.Thread(target=self._wifi_worker, args=(port, ssid, pwd),
                         daemon=True).start()

    def _wifi_worker(self, port, ssid, pwd):
        if store_wifi_via_serial(port, ssid, pwd, self.set_status):
            self.set_status("Saved. The device is rebooting onto "
                            "your network - continue to step 4.")

    # ---- step 4: verify

    def _step_done(self):
        self._title(
            "Check it joined",
            "After a reboot the device connects to WiFi (LED orange), then "
            "announces itself. It should appear here within ~15 seconds.")
        self.found_box = QWidget()
        self.found_lay = QVBoxLayout(self.found_box)
        self.found_lay.setContentsMargins(0, 4, 0, 4)
        self.found_lay.setSpacing(3)
        self.body_lay.addWidget(self.found_box)
        note = QLabel("Next: drag the new node into a group on the main "
                      "window to start talking. Volume, mic gain, TX mode "
                      "and group membership are stored on the device itself.")
        note.setProperty("dim", True)
        note.setWordWrap(True)
        self.body_lay.addWidget(note)
        self.body_lay.addStretch(1)
        self._poll_found()
        self.poll_timer.start()

    def _poll_found(self):
        if self.step != 3:
            self.poll_timer.stop()
            return
        clear_layout(self.found_lay)
        devs = self.app.monitor.snapshot()
        live = [(ip, d) for ip, d in sorted(devs.items())
                if d.get("last_rsp") and
                time.monotonic() - d["last_rsp"] < 6]
        if live:
            for ip, d in live:
                st = d.get("status") or {}
                self._row(self.found_lay, True,
                          f"{d.get('name', ip)}  ({ip})   "
                          f"rssi {st.get('rssi', '?')} dBm")
            self.set_status(f"{len(live)} device(s) online.")
        else:
            self._row(self.found_lay, False, "no device on the network yet…")
            self.set_status("Waiting - if the LED stays orange the "
                            "credentials were wrong; redo step 3.")


# ---------------------------------------------------------------- device config


class DeviceConfigDialog(QDialog):
    """Per-device configuration page: live info + WiFi provisioning over the
    device's USB serial console. Everything else (volume, gain, gate, TX
    mode, AGC, mute) lives on the node card itself."""

    status_sig = Signal(str)

    def __init__(self, app, ip):
        super().__init__(app)
        self.app = app
        self.ip = ip
        dev = app.monitor.snapshot().get(ip, {})
        self.setWindowTitle(f"{dev.get('name', ip)} - device settings")
        self.setWindowIcon(icon("settings", ACCENT, 24))
        self.resize(480, 460)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(panel)
        root = QVBoxLayout(panel)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        hi = QLabel()
        hi.setPixmap(icon("radio", ACCENT, 20).pixmap(20, 20))
        head.addWidget(hi)
        self.title = QLabel(dev.get("name", ip))
        self.title.setFont(QFont(FONT, 13, QFont.Bold))
        head.addWidget(self.title)
        head.addStretch(1)
        root.addLayout(head)

        # ---- live info grid
        info_box = QWidget()
        info_box.setObjectName("GroupCard")
        info_box.setAttribute(Qt.WA_StyledBackground, True)
        il = QGridLayout(info_box)
        il.setContentsMargins(12, 10, 12, 10)
        il.setHorizontalSpacing(14)
        il.setVerticalSpacing(4)
        self.info_vals = {}
        rows = [("address", "signal"), ("state", "dot"),
                ("tx mode", "radio"), ("AGC", "zap"),
                ("levels", "sliders"), ("sends to", "users")]
        for r, (label, ic_name) in enumerate(rows):
            li = QLabel()
            li.setPixmap(icon(ic_name, DIM, 13).pixmap(13, 13))
            il.addWidget(li, r, 0)
            ll = QLabel(label)
            ll.setProperty("dim", True)
            il.addWidget(ll, r, 1)
            v = QLabel("…")
            v.setWordWrap(True)
            il.addWidget(v, r, 2)
            self.info_vals[label] = v
        il.setColumnStretch(2, 1)
        root.addWidget(info_box)

        # ---- wifi form
        wt = QHBoxLayout()
        wi = QLabel()
        wi.setPixmap(icon("wifi", DIM, 14).pixmap(14, 14))
        wt.addWidget(wi)
        wl = QLabel("WIFI VIA USB")
        wl.setProperty("dim", True)
        wl.setFont(QFont(FONT, 8, QFont.Bold))
        wt.addWidget(wl)
        wt.addStretch(1)
        root.addLayout(wt)
        note = QLabel("Plug this unit's XIAO USB-C into the PC. Credentials "
                      "are stored on the device (NVS); applying reboots it.")
        note.setProperty("dim", True)
        note.setWordWrap(True)
        root.addWidget(note)
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        def lab(text, r):
            l = QLabel(text)
            l.setProperty("dim", True)
            form.addWidget(l, r, 0, Qt.AlignRight)

        lab("port", 0)
        self.port_combo = QComboBox()
        prow = QHBoxLayout()
        prow.setSpacing(4)
        prow.addWidget(self.port_combo)
        rescan = QPushButton()
        rescan.setProperty("ghost", True)
        rescan.setIcon(icon("refresh", DIM, 13))
        rescan.setToolTip("Re-scan COM ports")
        rescan.setCursor(Qt.PointingHandCursor)
        rescan.clicked.connect(self._fill_ports)
        prow.addWidget(rescan)
        prow.addStretch(1)
        form.addLayout(prow, 0, 1)
        lab("SSID", 1)
        self.ssid_edit = QLineEdit()
        self.ssid_edit.setFixedWidth(220)
        form.addWidget(self.ssid_edit, 1, 1, Qt.AlignLeft)
        lab("password", 2)
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setFixedWidth(220)
        form.addWidget(self.pwd_edit, 2, 1, Qt.AlignLeft)
        form.setColumnStretch(1, 1)
        root.addLayout(form)
        apply_btn = QPushButton(" Store WiFi on the device")
        apply_btn.setIcon(icon("wifi", "#ffffff", 15))
        apply_btn.setProperty("accent", True)
        apply_btn.clicked.connect(self.apply_wifi)
        root.addWidget(apply_btn, 0, Qt.AlignLeft)
        root.addStretch(1)

        foot = QHBoxLayout()
        self.status = QLabel("")
        self.status.setProperty("dim", True)
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        close_btn = QPushButton(" Close")
        close_btn.setIcon(icon("check", FG, 15))
        close_btn.clicked.connect(self.close)
        foot.addWidget(close_btn)
        root.addLayout(foot)

        self.status_sig.connect(self.status.setText)
        self._fill_ports()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.refresh_info)
        self.refresh_timer.start()
        self.refresh_info()

    def closeEvent(self, e):
        self.refresh_timer.stop()
        self.app.dev_dialogs.pop(self.ip, None)
        super().closeEvent(e)

    def _fill_ports(self):
        cur = self.port_combo.currentText()
        self.port_combo.clear()
        ports = serial_ports()
        self.port_combo.addItems(ports or [""])
        if cur and cur in ports:
            self.port_combo.setCurrentText(cur)

    def refresh_info(self):
        devs = self.app.monitor.snapshot()
        dev = devs.get(self.ip, {})
        st = dev.get("status") or {}
        self.title.setText(dev.get("name", self.ip))
        v = self.info_vals
        v["address"].setText(f"{self.ip}:{dev.get('port', PORT)}")
        state = state_of(dev)
        rssi = st.get("rssi")
        v["state"].setText(state + (f" · {rssi} dBm" if rssi is not None
                                    and state != "offline" else ""))
        v["tx mode"].setText({0: "always", 1: "voice (VAD)", 2: "level gate"}
                             .get(st.get("tx_mode") or 0, "?"))
        agc = st.get("mic_agc")
        live = st.get("agc_gain")
        v["AGC"].setText(("on" + (f" · ×{live:.1f}" if live else ""))
                         if agc else "off")
        v["levels"].setText(f"volume {st.get('volume', '?')}% · "
                            f"gain {st.get('mic_gain', '?')}% · "
                            f"gate {st.get('vox_thresh', '?')}")
        # resolve the stored send-list to friendly names
        pc = self.app.pc_addr()
        addr2name = {(ip2, d.get("port", PORT)): d.get("name", ip2)
                     for ip2, d in devs.items()}
        names = []
        for m in st.get("members") or []:
            m = tuple(m)
            names.append("This PC" if m[0] == pc[0] else
                         addr2name.get(m, f"{m[0]}:{m[1]}"))
        v["sends to"].setText(", ".join(names) if names else
                              "nobody (not in a group)")

    def apply_wifi(self):
        ssid = self.ssid_edit.text().strip()
        pwd = self.pwd_edit.text()
        port = self.port_combo.currentText()
        err = ("no COM port found" if not port else
               wifi_input_error(ssid, pwd))
        if err:
            self.status.setText(err)
            return
        self.status.setText("connecting to the device…")

        def work():
            if store_wifi_via_serial(port, ssid, pwd, self.status_sig.emit):
                self.status_sig.emit("Saved. The device is rebooting onto "
                                     "your network.")
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- app


class App(QMainWindow):
    def __init__(self):
        super().__init__()
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
        self.cards = {}             # node id -> NodeCard
        self.group_widgets = []     # [{frame, graph, index}]
        self._groups_sig = None
        self._last_desired_ips = set()
        self._tick_n = 0
        self.wizard = None
        self.dev_dialogs = {}       # device ip -> DeviceConfigDialog

        self.setWindowTitle("Walkie Group")
        self.setWindowIcon(icon("radio", ACCENT, 24))
        self.resize(1100, 680)
        self.setMinimumSize(920, 560)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(12)

        self._build_header(root)
        self._build_cards_panel(root)
        self._build_groups_panel(root)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(2500, self.import_groups_from_devices)
        QTimer.singleShot(3000, lambda: self.reconcile(force=True))

    # ------------------------------------------------ layout

    def _panel(self, root, title, icon_name, stretch=0):
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        root.addWidget(panel, stretch)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(icon(icon_name, DIM, 15).pixmap(15, 15))
        head.addWidget(ic)
        t = QLabel(title)
        t.setProperty("dim", True)
        t.setFont(QFont(FONT, 9, QFont.Bold))
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        return lay, head

    def _build_header(self, root):
        panel = QWidget()
        panel.setObjectName("Panel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        root.addWidget(panel)
        h = QHBoxLayout(panel)
        h.setContentsMargins(16, 10, 12, 10)
        h.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icon("radio", ACCENT, 22).pixmap(22, 22))
        h.addWidget(logo)
        title = QLabel("Walkie Group")
        title.setFont(QFont(FONT, 14, QFont.Bold))
        h.addWidget(title)
        h.addStretch(1)
        self.hdr_info = QLabel("")
        self.hdr_info.setProperty("dim", True)
        h.addWidget(self.hdr_info)
        setup_btn = QPushButton(" Set up new device…")
        setup_btn.setIcon(icon("plus", "#ffffff", 15))
        setup_btn.setProperty("accent", True)
        setup_btn.clicked.connect(lambda: self.setup_wizard(0))
        h.addWidget(setup_btn)

    def _build_cards_panel(self, root):
        lay, head = self._panel(root, "NODES", "monitor")
        hint = QLabel("drag a card into a group below")
        hint.setProperty("dim", True)
        hint.setFont(QFont(FONT, 8))
        head.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.cards_lay = QHBoxLayout(inner)
        self.cards_lay.setContentsMargins(0, 0, 0, 0)
        self.cards_lay.setSpacing(8)
        self.cards_lay.addStretch(1)
        scroll.setWidget(inner)
        scroll.setFixedHeight(216)
        lay.addWidget(scroll)

    def _build_groups_panel(self, root):
        lay, head = self._panel(root, "GROUPS", "users", stretch=1)
        self.grp_note = QLabel("")
        self.grp_note.setProperty("dim", True)
        self.grp_note.setFont(QFont(FONT, 8))
        head.addWidget(self.grp_note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.groups_lay = QHBoxLayout(inner)
        self.groups_lay.setContentsMargins(0, 0, 0, 0)
        self.groups_lay.setSpacing(8)
        self.groups_lay.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

    # ------------------------------------------------ node model

    def pc_addr(self):
        devs = self.monitor.snapshot()
        any_ip = next(iter(devs), "8.8.8.8")
        ip = local_ip_toward(any_ip) or "127.0.0.1"
        return ip, self.monitor.sock.getsockname()[1]

    def active_pc_client(self):
        return self.group_client or self.client

    def key_of(self, nid):
        if nid == "pc":
            return "pc"
        return self.monitor.snapshot().get(nid, {}).get("name", nid)

    def name_of(self, nid):
        if nid == "pc":
            return "This PC"
        return self.monitor.snapshot().get(nid, {}).get("name", nid)

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
                "online": bool(fresh),
                "tx": bool(fresh and st.get("tx_active")),
                "rx": bool(fresh and st.get("rx_active")),
                "muted": bool(st.get("muted")),
                "level": dev_mic_level(st.get("mic_level", 0))
                if fresh else 0.0,
                "gate": dev_gate_level(st.get("vox_thresh")),
                "gate_on": bool(st.get("tx_mode")),
                "voice": bool(fresh and st.get("tx_active")
                              and dev_mic_level(st.get("mic_level", 0))
                              >= (dev_gate_level(st.get("vox_thresh")) or 0)),
                "dev": dev, "state": state, "sub": sub,
            })
        return out

    def _refresh_cards(self, nodes):
        ids = [n["id"] for n in nodes]
        for nid in list(self.cards):
            if nid not in ids:
                card = self.cards.pop(nid)
                self.cards_lay.removeWidget(card)
                card.deleteLater()
        for i, n in enumerate(nodes):
            if n["id"] not in self.cards:
                card = NodeCard(self, n)
                self.cards[n["id"]] = card
                # keep the trailing stretch last
                self.cards_lay.insertWidget(self.cards_lay.count() - 1, card)
            self.cards[n["id"]].update_node(n)

    # ---- card actions

    def _poke_status(self, ip, **kv):
        """Optimistically update a device's cached status so the card
        reflects a control instantly; the device's next status response
        reconciles it either way."""
        with self.monitor.lock:
            d = self.monitor.devices.get(ip)
            st = d.get("status") if d else None
            if st:
                st.update(kv)

    def _refresh_now(self):
        self._refresh_cards(self.nodes())

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
                new = not st.get("muted")
                self.monitor.set_mute(nid, dev.get("port", PORT), new)
                self._poke_status(nid, muted=1 if new else 0)
        self._refresh_now()

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
            self._poke_status(nid, volume=new)
        cw = self.cards.get(nid)
        if cw:
            cw.vol_init = True
            cw.vol.set(new)
            cw.vol_pct.setText(f"{new}%")
        self._refresh_now()

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
                self._poke_status(nid, tx_mode=mode)
        self._refresh_now()

    def toggle_agc(self, nid):
        dev = self.monitor.snapshot().get(nid)
        if dev:
            st = dev.get("status") or {}
            new = not st.get("mic_agc")
            self.monitor.set_agc(nid, dev.get("port", PORT), new)
            self._poke_status(nid, mic_agc=1 if new else 0)
            self._refresh_now()

    def on_card_vol(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw.vol_init = True   # user owns the slider now
            cw.vol_pct.setText(f"{value}%")
        if nid == "pc":
            self.pc_spk_muted = False   # touching the slider unmutes
            self.settings["pc_vol"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.rx_volume = value / 100.0
        else:
            now = time.monotonic()
            if cw and (final or now - cw.last_vol_send > 0.15):
                cw.last_vol_send = now
                dev = self.monitor.snapshot().get(nid)
                if dev:
                    self.monitor.set_volume(nid, dev.get("port", PORT), value)

    def on_card_gain(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw.gain_init = True
            cw.gain_pct.setText(f"{value}%")
        if nid == "pc":
            self.settings["pc_gain"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.tx_gain = value / 100.0
        else:
            now = time.monotonic()
            if cw and (final or now - cw.last_gain_send > 0.15):
                cw.last_gain_send = now
                dev = self.monitor.snapshot().get(nid)
                if dev:
                    self.monitor.set_gain(nid, dev.get("port", PORT), value)

    def on_card_thresh(self, nid, value, final):
        value = int(value)
        cw = self.cards.get(nid)
        if cw:
            cw.sens_init = True
            cw.sens_pct.setText(f"{value}")
        if nid == "pc":
            self.settings["pc_thresh"] = value
            save_settings(self.settings)
            for c in (self.client, self.group_client):
                if c:
                    c.vox_thresh = value
        else:
            now = time.monotonic()
            if cw and (final or now - cw.last_sens_send > 0.15):
                cw.last_sens_send = now
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
        self._refresh_now()

    def _apply_pc_client_prefs(self, c):
        c.rx_volume = 0.0 if self.pc_spk_muted else \
            min(100, int(self.settings.get("pc_vol", 100))) / 100.0
        c.vox_thresh = int(self.settings.get("pc_thresh", 30))
        c.tx_gain = int(self.settings.get("pc_gain", 100)) / 100.0
        c.mode = self.tx_mode
        c.muted = self.pc_muted

    # ------------------------------------------------ groups panel

    def drop_on_group(self, gi, key):
        g = self.groups[gi]
        if key not in g["members"]:
            if len(g["members"]) >= MAX_MEMBERS:
                return
            g["members"].append(key)
            self.groups_changed()

    def _groups_signature(self, nodes):
        """Structure only - live talk/online state is repainted by
        _draw_group_graphs() instead of rebuilding widgets."""
        return json.dumps(self.groups) + repr(sorted(n["key"] for n in nodes))

    def _rebuild_groups(self, nodes):
        sig = self._groups_signature(nodes)
        if sig == self._groups_sig:
            return
        self._groups_sig = sig
        clear_layout(self.groups_lay)
        self.group_widgets = []
        for gi, g in enumerate(self.groups):
            box = DropFrame(lambda key, i=gi: self.drop_on_group(i, key))
            box.setObjectName("GroupCard")
            box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setSpacing(4)
            head = QHBoxLayout()
            gic = QLabel()
            gic.setPixmap(icon("users", DIM, 14).pixmap(14, 14))
            head.addWidget(gic)
            nm = QLabel(g.get("name", f"Group {gi + 1}"))
            nm.setFont(QFont(FONT, 10, QFont.Bold))
            head.addWidget(nm)
            rn = QPushButton()
            rn.setProperty("ghost", True)
            rn.setIcon(icon("edit", DIM, 12))
            rn.setToolTip("Rename group")
            rn.setCursor(Qt.PointingHandCursor)
            rn.clicked.connect(lambda _=False, i=gi: self._rename(i))
            head.addWidget(rn)
            head.addStretch(1)
            dl = QPushButton()
            dl.setProperty("ghost", True)
            dl.setIcon(icon("x", DIM, 13))
            dl.setToolTip("Delete group")
            dl.setCursor(Qt.PointingHandCursor)
            dl.clicked.connect(lambda _=False, i=gi: self._del_group(i))
            head.addWidget(dl)
            bl.addLayout(head)
            graph = GroupGraph()
            bl.addWidget(graph)
            chips = QHBoxLayout()
            chips.setSpacing(4)
            for key in g["members"]:
                chip = QWidget()
                chip.setStyleSheet("background: #e6eaf4; border-radius: 6px;")
                cl = QHBoxLayout(chip)
                cl.setContentsMargins(7, 1, 3, 1)
                cl.setSpacing(2)
                cl.addWidget(QLabel(("This PC" if key == "pc" else key),
                                    styleSheet=f"color: {DIM}; background: "
                                    "transparent; font-size: 8pt;"))
                xb = QPushButton()
                xb.setIcon(icon("x", DIM, 10))
                xb.setFixedSize(16, 16)
                xb.setToolTip("Remove from group")
                xb.setCursor(Qt.PointingHandCursor)
                xb.setStyleSheet("background: transparent; border: none;")
                xb.clicked.connect(lambda _=False, i=gi, k=key:
                                   self._chip_remove(i, k))
                cl.addWidget(xb)
                chips.addWidget(chip)
            chips.addStretch(1)
            bl.addLayout(chips)
            self.groups_lay.addWidget(box, 0, Qt.AlignTop)
            self.group_widgets.append({"frame": box, "graph": graph,
                                       "index": gi})
        drop = DropFrame(lambda key: self._add_group([key]))
        drop.setObjectName("DropZone")
        drop.setFixedSize(150, 214)
        dv = QVBoxLayout(drop)
        dv.setAlignment(Qt.AlignCenter)
        di = QLabel()
        di.setPixmap(icon("plus", "#a6b0c6", 24).pixmap(24, 24))
        di.setAlignment(Qt.AlignCenter)
        dv.addWidget(di)
        dt = QLabel("new group\n(drop here)")
        dt.setProperty("dim", True)
        dt.setAlignment(Qt.AlignCenter)
        dv.addWidget(dt)
        drop.mousePressEvent = lambda e: self._add_group([])
        drop.setCursor(Qt.PointingHandCursor)
        self.groups_lay.addWidget(drop, 0, Qt.AlignTop)
        self.groups_lay.addStretch(1)

    def _draw_group_graphs(self, nodes):
        info = {n["key"]: n for n in nodes}
        for gw in self.group_widgets:
            gi = gw["index"]
            if gi >= len(self.groups):
                continue
            gw["graph"].set_data(self.groups[gi]["members"], info)

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
        name, ok = QInputDialog.getText(
            self, "Rename group", "Group name:",
            text=self.groups[i].get("name", ""))
        if ok and name:
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
            self.grp_note.setText("imported existing group(s) from "
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
        self.grp_note.setText(
            "some device lists clipped to 8 members!" if clipped else
            "membership is pushed to devices and persists on them")
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

    def open_device_config(self, ip):
        dlg = self.dev_dialogs.get(ip)
        if dlg is not None:
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = DeviceConfigDialog(self, ip)
        self.dev_dialogs[ip] = dlg
        dlg.show()

    # ------------------------------------------------ setup wizard

    def setup_wizard(self, step=0):
        """Guided flash + provisioning for a new unit (see SetupWizard)."""
        if self.wizard is not None:
            self.wizard.show()
            self.wizard.raise_()
            self.wizard.activateWindow()
            self.wizard.goto(step)
            return
        self.wizard = SetupWizard(self, start=step)
        self.wizard.show()

    # ------------------------------------------------ periodic refresh

    def tick(self):
        self._tick_n += 1
        nodes = self.nodes()
        by_id = {n["id"]: n for n in nodes}

        # 20 Hz: per-card waveforms
        for nid, cw in self.cards.items():
            n = by_id.get(nid)
            if n:
                cw.wave.push(n["level"], n.get("gate"),
                             n.get("gate_on", True))

        if self._tick_n % 2 == 0:  # 10 Hz: live group graphs
            self._draw_group_graphs(nodes)
        if self._tick_n % 8 == 0:  # 2.5 Hz: cards, groups, header
            self._refresh_cards(nodes)
            self._rebuild_groups(nodes)
            self.hdr_info.setText(
                f"{len(nodes)} node(s) · {len(self.groups)} group(s)")
        if self._tick_n % 80 == 0:  # every 4 s: reconcile device routing
            self.reconcile()

    # ------------------------------------------------ shutdown

    def closeEvent(self, e):
        self.timer.stop()
        try:
            self.leave_group_on_exit()
        except Exception:
            pass
        if self.client:
            self.client.stop()
        if self.group_client:
            self.group_client.stop()
        self.monitor.stop()
        super().closeEvent(e)


def main():
    print("creating window...")
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setFont(QFont(FONT, 10))
    win = App()
    win.show()
    code = app.exec()
    print("window closed.")
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        # started via pythonw (start_gui.bat) there is no console to read
        try:
            Path(__file__).with_name("gui_crash.log").write_text(
                traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        try:
            input("startup failed - press Enter to close")
        except Exception:
            pass
