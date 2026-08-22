#pragma once

#include <stdint.h>
#include "esp_err.h"
#include "app_config.h"

esp_err_t codec_init(void);

/* Encode one 20 ms frame. Returns encoded byte count (>= 1; DTX frames are
 * 1-2 bytes), or -1 on error. Called only from the capture task. */
int codec_encode(const int16_t *pcm, uint8_t *out, int max_len);

/* Decode one packet into a 20 ms frame. data == NULL runs packet-loss
 * concealment. Called only from the playback task. */
esp_err_t codec_decode(const uint8_t *data, int len, int16_t *pcm);

/* Recover a lost frame from the in-band FEC data of the *following* packet. */
esp_err_t codec_decode_fec(const uint8_t *next_pkt, int len, int16_t *pcm);
