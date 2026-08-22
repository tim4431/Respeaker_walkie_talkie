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
