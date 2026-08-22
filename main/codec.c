#include "codec.h"
#include "opus.h"
#include "esp_log.h"
#include "sdkconfig.h"

static const char *TAG = "codec";

static OpusEncoder *s_enc;
static OpusDecoder *s_dec;

esp_err_t codec_init(void)
{
    int err = OPUS_OK;

    s_enc = opus_encoder_create(WT_SAMPLE_RATE, 1, OPUS_APPLICATION_VOIP, &err);
    if (err != OPUS_OK) {
        ESP_LOGE(TAG, "encoder create failed: %s", opus_strerror(err));
        return ESP_FAIL;
    }
    opus_encoder_ctl(s_enc, OPUS_SET_BITRATE(CONFIG_WT_OPUS_BITRATE));
    opus_encoder_ctl(s_enc, OPUS_SET_COMPLEXITY(CONFIG_WT_OPUS_COMPLEXITY));
    opus_encoder_ctl(s_enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    /* Cap at wideband: SILK then runs at 16 kHz internally, which keeps
     * encode cost low even though we feed it 48 kHz. */
    opus_encoder_ctl(s_enc, OPUS_SET_MAX_BANDWIDTH(OPUS_BANDWIDTH_WIDEBAND));
    /* Silence costs (almost) nothing; lost packets are recoverable from
     * the next one. */
    opus_encoder_ctl(s_enc, OPUS_SET_DTX(1));
    opus_encoder_ctl(s_enc, OPUS_SET_INBAND_FEC(1));
    opus_encoder_ctl(s_enc, OPUS_SET_PACKET_LOSS_PERC(10));

    s_dec = opus_decoder_create(WT_SAMPLE_RATE, 1, &err);
    if (err != OPUS_OK) {
        ESP_LOGE(TAG, "decoder create failed: %s", opus_strerror(err));
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Opus up: %d bps, complexity %d, DTX+FEC",
             CONFIG_WT_OPUS_BITRATE, CONFIG_WT_OPUS_COMPLEXITY);
    return ESP_OK;
}

int codec_encode(const int16_t *pcm, uint8_t *out, int max_len)
{
    int n = opus_encode(s_enc, pcm, WT_FRAME_SAMPLES, out, max_len);
    if (n < 0) {
        ESP_LOGE(TAG, "encode failed: %s", opus_strerror(n));
        return -1;
    }
    return n;
}

esp_err_t codec_decode(const uint8_t *data, int len, int16_t *pcm)
{
    int n = opus_decode(s_dec, data, len, pcm, WT_FRAME_SAMPLES, 0);
    return (n == WT_FRAME_SAMPLES) ? ESP_OK : ESP_FAIL;
}

esp_err_t codec_decode_fec(const uint8_t *next_pkt, int len, int16_t *pcm)
{
    int n = opus_decode(s_dec, next_pkt, len, pcm, WT_FRAME_SAMPLES, 1);
    return (n == WT_FRAME_SAMPLES) ? ESP_OK : ESP_FAIL;
}
