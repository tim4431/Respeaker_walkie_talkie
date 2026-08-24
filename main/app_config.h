#pragma once

#include "driver/gpio.h"

/*
 * Audio format is dictated by the ReSpeaker Lite 48 kHz I2S firmware
 * (respeaker_lite_i2s_dfu_firmware_48k): the XMOS XU316 is I2S master,
 * 48 kHz, 32-bit slots, stereo. Channel 0 carries the processed
 * (AEC/NS/AGC) mic signal.
 */
#define WT_SAMPLE_RATE      48000
#define WT_FRAME_MS         20
#define WT_FRAME_SAMPLES    960   /* 20 ms @ 48 kHz */

/* Largest Opus payload we ever expect at <=64 kbps VBR */
#define WT_MAX_PAYLOAD      400

/* Frames batched into one datagram (latency vs packet rate); a pending
 * batch is flushed early after this many microseconds. */
#define WT_FRAMES_PER_PKT   3
#define WT_BATCH_FLUSH_US   (70 * 1000)

/* XIAO ESP32-S3 <-> XU316 wiring (fixed on the ReSpeaker Lite kit) */
#define WT_PIN_I2S_BCLK     GPIO_NUM_8
#define WT_PIN_I2S_WS       GPIO_NUM_7
#define WT_PIN_I2S_DOUT     GPIO_NUM_43  /* ESP -> XMOS: far-end audio (also AEC reference) */
#define WT_PIN_I2S_DIN      GPIO_NUM_44  /* XMOS -> ESP: processed mic */

#define WT_PIN_LED          GPIO_NUM_1   /* on-board WS2812 */
#define WT_PIN_BUTTON       GPIO_NUM_3   /* USER button (D2), active low */

/* Jitter buffer tuning */
#define WT_JB_SLOTS         32
#define WT_JB_PREFILL       3    /* frames buffered before playback starts (~60 ms) */
#define WT_JB_HIGH_WM       8    /* above this, drop a frame to re-center (clock drift) */
#define WT_LOSS_RESET       25   /* consecutive lost frames (~0.5 s) before re-buffering */

#define WT_PEER_TIMEOUT_US  (10 * 1000 * 1000)
#define WT_KEEPALIVE_US     (1 * 1000 * 1000)
#define WT_DISCOVERY_US     (5 * 1000 * 1000)

/* Device-side VOX (WT_TXMODE_VOX): open TX when the WebRTC VAD (libfvad)
 * classifies the frame as speech AND the frame's peak exceeds the threshold,
 * while the speaker has been quiet for RX_BLOCK; hold TX open for HANG.
 * The VAD rejects loud non-speech (clatter, keyboards); the energy floor
 * rejects distant speech (TV, hallway) that the deliberately permissive VAD
 * would otherwise key on. The 0-100 knob (WT_CTRL_SET_THRESH, persisted)
 * maps quadratically onto peak16 so the low end stays fine-grained:
 *   peak = 150 + thresh^2 * 2   (0 -> 150, 15 -> 600, 100 -> 20150)
 * Post-AGC speech peaks are ~10000-30000; a quiet room is a few hundred.
 * Default was 30 when the peak was the only trigger; with the VAD doing the
 * speech/non-speech work the floor only needs to clear the room tone. */
#define WT_VOX_THRESH_DEFAULT 15
#define WT_VOX_PEAK_OF(th)  (150 + (int32_t)(th) * (int32_t)(th) * 2)
#define WT_VOX_TX_HANG_US   (700 * 1000)
#define WT_VOX_RX_BLOCK_US  (400 * 1000)
/* fvad aggressiveness 0-3: higher = stricter speech test, more onset misses.
 * 3 (strictest): measured on unit 1, mode 2 still passed the XMOS-AGC-pumped
 * room noise as speech; the hold + pre-roll absorb mode 3's onset misses. */
#define WT_VAD_MODE         3
/* A single VAD hit must not open TX or re-arm the hangover: the VAD throws
 * isolated false positives on residual noise, and one hit per 700 ms was
 * enough to chain the gate open forever after speech ended. Real speech is
 * voiced for many consecutive frames, so require a short unbroken run (VAD
 * speech AND peak over the floor). The onset frames the debounce consumes
 * are recovered by the pre-roll ring. */
#define WT_VOX_OPEN_FRAMES  3     /* 60 ms of consecutive speech */
/* While the VOX gate is closed every frame is still encoded into a ring of
 * the most recent PREROLL frames; on gate open the ring is sent first, so
 * the first word's onset (the frames that triggered the VAD) isn't clipped.
 * Must leave room in the TX packet: bounded by WT_MAX_BATCH per datagram. */
#define WT_VOX_PREROLL      5     /* 100 ms */

/* Group audio: one active speaker at a time - a UDP source owns playback
 * until it has been silent this long, then another source may take over. */
#define WT_SPK_LOCK_US      (400 * 1000)

/* Auto gain + noise gate (WT_CTRL_SET_AGC). Plain digital gain raises voice
 * and hiss equally, so instead: learn the room's noise floor, boost only
 * what stands above it, and duck the rest. Deliberately independent of the
 * VOX transmit threshold - tying them together meant a quiet room gated the
 * AGC off entirely, so quiet speech could never be brought up. Levels are
 * frame peaks in int16 units, measured before gain. */
#define WT_AGC_TARGET_PEAK  20000.0f  /* aim speech peaks just under clipping */
#define WT_AGC_MAX          12.0f
#define WT_AGC_MIN          0.5f
#define WT_AGC_ATTACK       0.30f     /* too loud: come down quickly */
#define WT_AGC_RELEASE      0.04f     /* too quiet: creep up, never pump */
#define WT_NR_ATTEN         0.12f     /* ~-18 dB duck for noise-only frames */

/* Noise floor by minimum statistics: the quietest frame in each window sets
 * it. Deliberately NOT driven by the speech decision - feeding that back
 * deadlocks (a closed gate keeps speech quiet, so it never reopens; a frozen
 * floor lets steady room tone read as speech forever). A rolling minimum
 * needs no classification: steady noise sets the floor, speech does not,
 * because it dips between syllables. */
#define WT_NF_WINDOW        75        /* frames per window (1.5 s) */
#define WT_NF_SMOOTH        0.5f
#define WT_NF_MIN           20.0f
/* Speech must stand this far above the floor to count (x mult + abs margin). */
#define WT_NF_SPEECH_MULT   3.0f
#define WT_NF_SPEECH_MARGIN 60.0f
/* Hold the gate open after the last voiced frame, or syllable gaps chatter. */
#define WT_NR_HOLD_FRAMES   15        /* 300 ms */
