# ReSpeaker Lite IP Walkie-Talkie

Full-duplex, two-way intercom over WiFi for two **Seeed ReSpeaker Lite kits
(with XIAO ESP32-S3)**. Audio is captured through the XMOS XU316 front-end
(AEC / noise suppression / AGC — this is what makes speakerphone-style full
duplex possible), compressed with **Opus** (~24 kbps, DTX + in-band FEC), and
streamed peer-to-peer over **UDP** on the local network. Units find each other
automatically via **mDNS**.

```
mics → XU316 (AEC/NS/AGC) → I2S → ESP32-S3 → Opus encode → UDP ⇄ UDP → jitter
buffer → Opus decode → I2S → XU316 → codec/amp → speaker
```

Mouth-to-ear latency is roughly 100 ms on a normal LAN (20 ms frame + ~60 ms
jitter buffer + network/DMA).

## Hardware

- 2 × ReSpeaker Lite kit with XIAO ESP32-S3 pre-soldered
- 2 × speakers on the JST connector (or the 3.5 mm jack)
- USB-C power

Pinout used (fixed by the kit wiring):

| Signal | GPIO |
|---|---|
| I2S BCLK | 8 |
| I2S LRCLK/WS | 7 |
| Mic data (XMOS → ESP) | 44 |
| Speaker data (ESP → XMOS) | 43 |
| WS2812 LED | 1 |
| USER button | 3 |

The ESP32-S3 runs as **I2S slave** — the XU316 is clock master at 48 kHz,
32-bit stereo slots. Opus is fed 48 kHz directly and capped at wideband, so no
manual resampling code is needed.

## Flashing (automated)

`tools\flash_all.ps1` detects and flashes both processors of a connected unit:

```powershell
powershell -ExecutionPolicy Bypass -File tools\flash_all.ps1
```

- **XMOS XU316** (plug in the ReSpeaker Lite's own USB-C): reads the USB DFU
  revision and, unless it already reports the 48 kHz v1.1.0 I2S firmware
  (rev `0110`), flashes `tools\firmware\respeaker_lite_i2s_dfu_firmware_48k_v1.1.0_ch0-asr_ch1-mww.bin`
  with dfu-util. On the first run per PC it launches Zadig for the one-time
  WinUSB driver install (select *ReSpeaker Lite* → *Install Driver*), then
  continues automatically.
- **ESP32-S3** (plug in the XIAO's USB-C): finds the Espressif COM port and
  runs `idf.py flash`. Use `-Monitor` to attach the serial monitor after,
  `-EspOnly` / `-XmosOnly` to restrict, `-Port COMx` to override detection.

Set your WiFi credentials once by copying `wifi.conf.example` to `wifi.conf`
(gitignored) and filling in `WIFI_SSID` / `WIFI_PASSWORD` — the flash script
applies it to the build automatically. (Alternatively use `idf.py menuconfig`
→ Walkie-Talkie Configuration.)

(Manual XMOS flashing, if you ever need it:
`dfu-util -R -e -a 1 -D tools\firmware\respeaker_lite_i2s_dfu_firmware_48k_v1.1.0_ch0-asr_ch1-mww.bin`
— see the [Seeed DFU guide](https://github.com/respeaker/ReSpeaker_Lite/blob/master/xmos_firmwares/dfu_guide.md).)

Repeat for the second unit (same config is fine — units identify themselves by
MAC-derived hostname `walkie-xxxxxx.local`). With more than two units on the
network, each unit pairs with the first peer it hears; use a static peer IP
(`menuconfig → Static peer IP`) to pin pairs.

## PC client

With only one unit (or for testing), your PC can be the other walkie-talkie —
it speaks the same UDP/Opus protocol:

```powershell
cd pc_client
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python walkie_gui.py        # GUI: state, stats, live waveforms, mute
.venv\Scripts\python walkie_pc.py         # headless CLI version
```

Both discover the device via mDNS (or pass `--ip`). Use headphones — the PC
side has no echo cancellation.

## LED states

| Color | Meaning |
|---|---|
| Orange | Connecting to WiFi |
| Blue | On WiFi, searching for a peer (mDNS) |
| Green | Linked — full-duplex audio flowing |
| Purple | Mic muted (USER button toggles) |

The small round button labeled **MUTE** is handled by the XMOS in hardware and
also works. The **USER** button toggles software mute (TX stops entirely).

## Design notes

- **Transport:** one UDP datagram per 20 ms frame with an 8-byte header
  (magic, sequence, flags). Peer address is learned via mDNS
  (`_walkie._udp`) and refreshed from every received datagram.
- **Opus settings:** VOIP mode, 24 kbps, complexity 3, DTX (silence costs
  ~nothing — DTX frames double as keepalives), in-band FEC with 10 % expected
  loss. A lost frame is first recovered from the next packet's FEC data,
  otherwise concealed (PLC).
- **Jitter buffer:** 32 slots of encoded packets, 3-frame (~60 ms) prefill,
  frame-drop above 8 to absorb clock drift between the two units' crystals,
  re-buffer after ~0.5 s of continuous loss.
- **WiFi power save is disabled** (`WIFI_PS_NONE`) — modem sleep otherwise
  causes periodic 100-300 ms dropouts.
- **AEC reference:** everything we play is written through the XU316, so its
  echo canceller sees the far-end signal automatically — that's what stops
  the remote voice from being re-transmitted back.

## Tuning

- Choppy audio on busy WiFi → raise `WT_JB_PREFILL` (latency ↑, robustness ↑).
- "encode overrun" warnings in the log → lower Opus complexity in menuconfig.
- Bandwidth: ~24 kbps + UDP/IP overhead per direction while talking; ~2 kbps
  when silent (DTX).
