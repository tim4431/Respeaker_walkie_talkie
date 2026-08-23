#pragma once

#include <stdint.h>
#include "esp_err.h"
#include "app_config.h"

esp_err_t audio_init(void);

/* Blocking read of one 20 ms frame; fills mono[WT_FRAME_SAMPLES] with the
 * processed mic signal (I2S channel 0), 16-bit. Paced by the XMOS I2S clock. */
esp_err_t audio_capture(int16_t *mono);

/* Blocking write of one 20 ms mono frame to the XMOS (speaker + AEC reference). */
esp_err_t audio_play(const int16_t *mono);

/* Peak |raw 32-bit slot value| of the last captured frame (debug: shows
 * whether the XMOS delivers signal at all and how it is bit-aligned). */
int32_t audio_last_raw_peak(void);
