# AI Agent Guide — ReSpeaker Lite IP Walkie-Talkie

Operational manual for AI assistants (Claude, Copilot, Cursor, etc.) working
on this repo: how to connect to the hardware, flash both processors, and
debug effectively. Read this in full before touching anything.

## What this is

Full-duplex intercom on Seeed ReSpeaker Lite kits (XMOS XU316 audio front-end
+ XIAO ESP32-S3). The XMOS does AEC/NS/AGC and is **I2S clock master** (48 kHz,
32-bit stereo slots; ESP32 is slave). Firmware is ESP-IDF (`main/`), PC client
is Python (`pc_client/`: `walkie_pc.py` CLI + `walkie_gui.py` Qt/PySide6 GUI,
launched with `start_gui.bat`).

Audio: Opus 48 kHz mono, 20 ms frames (960 samples), VOIP mode, wideband cap,
DTX **off**. PC↔device audio runs over **TCP port 5010** (`[u16 len][opus]`
stream, len 0 = heartbeat; device sends an identity banner frame starting with
ASCII `WTKI` on accept — clients must not decode it as audio). Device↔device
and group audio use UDP protocol v2 on **port 5004** (see `main/protocol.h`:
8-byte header + batched `[u16 len][opus]` frames). While a TCP client is
connected, UDP audio is NOT inserted into the jitter buffer (the two seq
spaces are unrelated; interleaving them garbles playback) — TCP wins.

**Group chat**: every node (walkie units and the PC alike) can hold a
persisted member list (`WT_CTRL_SET_GROUP`: u8 count + count × {u32 ip,
u16 port}, network order; count 0 leaves; stored in NVS). A node with
members sends its audio/keepalives to all of them (`net_send_all`).
Playback is one-speaker-at-a-time: a UDP source owns the jitter buffer
until it has been silent for 400 ms (`WT_SPK_LOCK_US`); other members'
frames are dropped meanwhile. The PC joins as a plain UDP node: the GUI
registers the monitor socket's addr in each device's member list and runs
a `WalkieClient(targets=[...], use_tcp=False)` on the shared socket.

Control on UDP 5004 with `WT_FLAG_CTRL`: `SET_VOL` (0x04, volume 0–100,
NVS), `SET_MODE` (0x05, 0 = always transmit, 1 = voice/VAD, 2 = plain level
gate, NVS),
`SET_MUTE` (0x06, not persisted), `SET_GROUP` (0x07), `SET_THRESH` (0x08, VOX
TX threshold 0–100, NVS; higher = louder speech needed, mapped quadratically
onto peak16 by `WT_VOX_PEAK_OF`: `150 + thresh² * 2`, default 15 ≈ peak 600.
On-device the threshold is only an energy floor AND'ed with the WebRTC VAD
(`components/libfvad`) — the VAD decides speech vs non-speech; units flashed
before the VAD change keep their old NVS value, typically 30),
`SET_GAIN` (0x09, mic gain 0–200 %, NVS; saturating digital gain
applied to captured samples before level/VOX/encode — so gain also shifts
where the gate opens), plus status/peer as before. Control never affects peer
adoption. Every ctrl request is answered with `wt_status_t`, which appends
optional fields after hostname: volume, tx_mode, tx_active, rx_active,
member_count, members[8], mic_level (frame peak >> 7), vox_thresh, mic_gain —
parse by length. The PC client mirrors the same knobs on RMS
(`vox_rms_of`: `20 + thresh² * 0.3`); the GUI draws each node's gate as a
dashed line on its waveform, so "level above the line = transmitting". VOX timing lives in `app_config.h` (`WT_VOX_*`). The device
broadcasts an 8-byte presence packet to `:5004` every 2 s (discovery + AP
keepalive). GUI group semantics: the GUI stores the group structure
(multi-group) in `gui_settings.json`, pushes each device's union send-list,
re-asserts it every 4 s for devices it manages, and imports groups from
device member lists at startup when it has none of its own.

## Hardware connections (two different USB-C ports!)

- **XIAO's USB-C** (on the small module): ESP32 flashing + serial console.
  Enumerates as `USB\VID_303A&PID_1001` → a COM port ("USB 串行设备").
- **ReSpeaker board's USB-C**: XMOS firmware DFU only. Enumerates as
  `USB\VID_2886&PID_0019`. Both cables at once is fine.
- **DANGER**: the user has a second ReSpeaker Lite used as PC microphone
  (USB-audio firmware, composite device with a runtime "DFU FACTORY"
  interface). NEVER run dfu-util operations while it is plugged in.
  `tools/flash_all.ps1` refuses when it detects one; keep that guard.

Identify what's connected:
```powershell
Get-PnpDevice -PresentOnly -Class Ports | Where-Object { $_.InstanceId -match 'VID_303A' }  # XIAO COM port
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_2886&PID_0019' }      # ReSpeaker XMOS
```
A walkie unit's XMOS in I2S mode is a *pure DFU-class* device (CompatibleIds
contain `Class_FE`, no `&MI_` interfaces); the USB-audio PC mic is a composite
device. Firmware revision = the `REV_xxxx` in HardwareIds (0x0110 = correct
48 kHz I2S v1.1.0; 0x0108 = factory 16 kHz build that must be upgraded).

## Environments

- ESP-IDF **v5.4**: `. $env:USERPROFILE\esp\v5.4\esp-idf\export.ps1` (dot-source
  in PowerShell; wrap with `$ErrorActionPreference='Continue'` — it prints to
  stderr). Tools incl. dfu-util live under `$env:USERPROFILE\.espressif\tools`.
- PC client venv: `pc_client\.venv` (created from Python 3.14). Run scripts as
  `pc_client\.venv\Scripts\python.exe`. Deps in `pc_client/requirements.txt`
  (PyOgg must come from its GitHub repo — the PyPI release lacks OpusEncoder).
- WiFi credentials: copy `wifi.conf.example` → `wifi.conf` (gitignored) at repo
  root; `flash_all.ps1` patches them into `sdkconfig` automatically.

## Flashing

The GUI wraps all of this in **Set up new device…** (detect → flash → WiFi →
verify), streaming the script's output into the window. It probes USB itself
and refuses XMOS flashing while a USB-audio ReSpeaker is attached. For
scripted/CI use, drive the script directly:

One command does everything:
```powershell
powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1          # both processors
powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1 -EspOnly -Port COM6
powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1 -XmosOnly
```
- XMOS: checks USB REV, flashes `tools/firmware/respeaker_lite_i2s_dfu_firmware_48k_v1.1.0_ch0-asr_ch1-mww.bin`
  only if needed. First time on a new PC, Windows needs a WinUSB driver on the
  DFU interface: the script launches `tools/zadig-2.9.exe` (user clicks
  ReSpeaker Lite → Install Driver once). Never pass `-e` to dfu-util.
- ESP32: auto-detects the VID_303A COM port, applies `wifi.conf`, builds and
  flashes. `sdkconfig.defaults` changes do NOT retrofit an existing
  `sdkconfig` — patch `sdkconfig` directly or delete it to regenerate.
- **COM port wedge**: after heavy serial open/close cycling, esptool fails
  with "A serial exception error occurred: Write timeout" even though the
  port opens fine. No software fix works — ask the user to unplug/replug the
  XIAO cable, then retry.

## Debugging

### USB config console (COM port, 115200)

The firmware runs a line-based config console on the XIAO's USB serial port
(same COM port as the logs). Commands: `info` (identity), `wifi <ssid>
<password>` (save WiFi credentials to NVS and reboot; NVS credentials
override the ones baked into the build), `reboot`. Replies are single lines
prefixed `WTCFG` and may interleave with log output — match per line. The
GUI's "WiFi via USB..." button wraps this. Opening the port may reboot the
device.

Persistence: WiFi credentials (NVS after `wifi`, else compiled-in), speaker
volume (NVS), and a manual peer pairing (NVS) all survive reboots; mute
always boots off. The GUI stores PC volume and TX mode in
`pc_client/gui_settings.json` (gitignored). The PC client's TX modes:
`full` (always transmit) and `vox` (transmit only while the PC mic hears
speech and the far end is quiet — the echo fix for open PC speakers, since
only the XMOS side has AEC).

### Serial log console (COM port, 115200)

- **CRITICAL GOTCHA**: the USB-Serial-JTAG console delivers *stale buffered
  logs* — lines can be minutes old, with mid-line fragments and timestamp
  jumps. Never treat a captured line as "live" without checking its `I (ms)`
  uptime against wall clock. Drain the port until output pauses before
  trusting state. This once produced a false "device sees no TCP client"
  diagnosis for hours.
- Opening the port (and DTR/RTS changes) can RESET the board. A reset also
  tears down its WiFi/TCP state — never run serial capture concurrently with
  a network test unless a mid-test reboot is acceptable.
- Reset on purpose: open port, `DtrEnable=$false; RtsEnable=$true`, 250 ms,
  `RtsEnable=$false`.
- Telemetry line (every 2 s from the capture task):
  `mic peak16=<mic level> sent=<frames tx>/100 enc_max=<largest opus frame>B tcp=<0|1> peer=<0|1> rx_dgrams=<udp in>`
  Speech near the device shows peak16 ≥ ~10000; quiet room ~100–900;
  0 would suggest hardware mute (the board's MUTE button latches in the XMOS).

### Network probes (run from `pc_client\.venv\Scripts\python.exe`)

Status query (UDP, works even during calls; never steals the audio peer):
```python
import socket, struct
HDR = struct.Struct('<IHBB'); STATUS = struct.Struct('<BBBBBbHI24s')  # matches wt_status_t
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
s.sendto(HDR.pack(0x314B5457, 0, 2, 2) + bytes([1]), ('<device-ip>', 5004))
print(STATUS.unpack_from(s.recvfrom(2048)[0][8:]))  # cmd,proto,muted,linked,locked,rssi,port,ip,hostname
```
Presence check (proves device alive regardless of unicast health):
bind UDP `('', 5004)` and expect an 8-byte packet every 2 s.
TCP identity: connect to `<ip>:5010`, first frame is the `WTKI-...` build tag
— proves which firmware build is actually serving.

- PowerShell mangles quotes in `python -c` one-liners — write probe scripts
  to files instead. In PS 5.1, `2>&1` on native commands throws under
  `$ErrorActionPreference='Stop'`; route through `cmd /c "... 2>&1"`.


### Firmware layout / invariants

- `main/main.c`: tasks — capture(core1,prio10) encodes+sends (TCP preferred,
  UDP batch fallback), playback(core1,prio10) drains the jitter buffer,
  rx_task(core0) handles UDP audio/ctrl, tcp_srv_task(core0) owns TCP 5010,
  housekeeping in app_main (button GPIO3, LED, keepalives, mDNS discovery).
- Audio tasks need large stacks (48 KB) — libopus is stack-hungry: 24 KB
  overflowed in opus_encode the moment a call started (panic + reboot on
  every TCP connect; ~2 frames escaped per boot, so the PC saw a flat RX
  waveform and choppy "noise" played on the device between reboots).
- `tcp_send_frame` is called from two tasks; its static buffer must stay
  guarded by `s_tcp_tx_lock` (memcpy INSIDE the lock).
- Device SNDTIMEO must stay generous (2 s): WiFi retry bursts stall sends
  hundreds of ms; a 200 ms timeout tore down healthy calls.
- lwIP: `CONFIG_LWIP_TCP_MSL=6000` keeps reconnect churn from exhausting the
  PCB pool (default 60 s MSL parks every closed conn in TIME_WAIT for 2 min).
- WiFi power save is disabled (`WIFI_PS_NONE`) — do not re-enable.
- I2S pins (fixed by kit wiring): BCLK 8, WS 7, mic-in 44, spk-out 43;
  WS2812 LED GPIO1, USER button GPIO3 (mic mute), MUTE button GPIO4
  (speaker on/off; needs the board's MUTE pad jumpered to D3, same scheme as
  USER→D2). LED: orange=WiFi connecting,
  blue=no peer, green=linked, purple=muted.
- Samples are MSB-aligned in 32-bit slots (`>>16` to int16); channel 0 is the
  processed (ASR) mic.

### Verifying a change end-to-end

1. Flash, wait ~10 s, confirm presence broadcasts on :5004.
2. Status query → hostname/rssi/linked sane.
3. TCP connect :5010 → banner matches the build tag you just set (bump
   `WT_BUILD_TAG` in main.c when behavior changes — it is the only reliable
   way to prove which build is serving).
4. Read-only TCP: expect ~50 frames/s (DTX off) when the network path is
   healthy; ~1 frame/s means the router is in a bad phase, not an app bug.
5. Full call: `pc_client\walkie_gui.py` → select device → Talk; green RX bars
   on speech near the device.
