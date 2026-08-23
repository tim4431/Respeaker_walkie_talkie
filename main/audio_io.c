#include <string.h>
#include "audio_io.h"
#include "freertos/FreeRTOS.h"
#include "driver/i2s_std.h"
#include "esp_check.h"

static const char *TAG = "audio_io";

static i2s_chan_handle_t s_tx;
static i2s_chan_handle_t s_rx;

/* 32-bit stereo scratch buffers for one frame (7680 bytes each) */
static int32_t s_rx_raw[WT_FRAME_SAMPLES * 2];
static int32_t s_tx_raw[WT_FRAME_SAMPLES * 2];
static int32_t s_raw_peak;

esp_err_t audio_init(void)
{
    /* The XU316 is I2S master; we are slave on both directions. */
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 240;  /* 5 ms per descriptor */
    /* On TX underrun send silence, not a loop of the last DMA buffer -
     * without this a starved speaker path turns into buzzing noise. */
    chan_cfg.auto_clear = true;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_tx, &s_rx), TAG, "new channel");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(WT_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = WT_PIN_I2S_BCLK,
            .ws   = WT_PIN_I2S_WS,
            .dout = WT_PIN_I2S_DOUT,
            .din  = WT_PIN_I2S_DIN,
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_tx, &std_cfg), TAG, "init tx");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx, &std_cfg), TAG, "init rx");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx), TAG, "enable tx");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx), TAG, "enable rx");

    ESP_LOGI(TAG, "I2S slave up: %d Hz, 32-bit stereo slots", WT_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t audio_capture(int16_t *mono, float g0, float g1)
{
    size_t want = sizeof(s_rx_raw);
    size_t got = 0;
    uint8_t *dst = (uint8_t *)s_rx_raw;
    while (got < want) {
        size_t n = 0;
        esp_err_t err = i2s_channel_read(s_rx, dst + got, want - got, &n, portMAX_DELAY);
        if (err != ESP_OK) {
            return err;
        }
        got += n;
    }
    /* Channel 0 = processed mic; samples are MSB-aligned in the 32-bit slot.
     * Scale in the 32-bit domain: 1/65536 is the plain int16 conversion, so
     * a gain of g lands at g * the old value but keeps the bits that used
     * to be truncated away. s_raw_peak stays pre-gain, so callers can judge
     * the true input level however loud they have driven the output. */
    int32_t rp = 0;
    const float step = (g1 - g0) / (float)WT_FRAME_SAMPLES;
    float g = g0;
    for (int i = 0; i < WT_FRAME_SAMPLES; i++) {
        int32_t raw = s_rx_raw[2 * i];
        int32_t a = raw < 0 ? -raw : raw;
        if (a > rp) rp = a;
        float v = (float)raw * (g * (1.0f / 65536.0f));
        g += step;
        if (v > 32767.0f) {
            v = 32767.0f;
        } else if (v < -32768.0f) {
            v = -32768.0f;
        }
        mono[i] = (int16_t)v;
    }
    s_raw_peak = rp;
    return ESP_OK;
}

int32_t audio_last_raw_peak(void)
{
    return s_raw_peak;
}

esp_err_t audio_play(const int16_t *mono)
{
    for (int i = 0; i < WT_FRAME_SAMPLES; i++) {
        int32_t s = ((int32_t)mono[i]) << 16;
        s_tx_raw[2 * i]     = s;
        s_tx_raw[2 * i + 1] = s;
    }
    size_t want = sizeof(s_tx_raw);
    size_t put = 0;
    const uint8_t *src = (const uint8_t *)s_tx_raw;
    while (put < want) {
        size_t n = 0;
        esp_err_t err = i2s_channel_write(s_tx, src + put, want - put, &n, portMAX_DELAY);
        if (err != ESP_OK) {
            return err;
        }
        put += n;
    }
    return ESP_OK;
}
