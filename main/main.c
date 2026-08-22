#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"

#include "app_config.h"
#include "protocol.h"
#include "audio_io.h"
#include "codec.h"
#include "jitter_buffer.h"
#include "net.h"
#include "led.h"

static const char *TAG = "walkie";

static volatile bool s_muted = false;

/* ---------------- capture -> encode -> UDP ---------------- */

static void capture_task(void *arg)
{
    static int16_t pcm[WT_FRAME_SAMPLES];
    static uint8_t pkt[sizeof(wt_header_t) + WT_MAX_PAYLOAD];
    uint16_t seq = 0;

    for (;;) {
        if (audio_capture(pcm) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (s_muted || !net_peer_known()) {
            continue;  /* keep draining I2S; the frame is paced by the XMOS clock */
        }

        int64_t t0 = esp_timer_get_time();
        int n = codec_encode(pcm, pkt + sizeof(wt_header_t), WT_MAX_PAYLOAD);
        int64_t dt = esp_timer_get_time() - t0;
        if (dt > WT_FRAME_MS * 1000) {
            ESP_LOGW(TAG, "encode overrun: %lld us for a %d ms frame -- lower "
                          "WT_OPUS_COMPLEXITY", dt, WT_FRAME_MS);
        }
        if (n <= 0) {
            continue;
        }

        wt_header_t hdr = {
            .magic = WT_MAGIC,
            .seq = seq++,
            .flags = WT_FLAG_AUDIO,
            .version = WT_PROTO_VERSION,
        };
        memcpy(pkt, &hdr, sizeof(hdr));
        net_send(pkt, sizeof(hdr) + n);
    }
}

/* ---------------- UDP -> jitter buffer ---------------- */

static void rx_task(void *arg)
{
    static uint8_t buf[sizeof(wt_header_t) + WT_MAX_PAYLOAD + 64];

    for (;;) {
        struct sockaddr_in src;
        socklen_t slen = sizeof(src);
        int n = recvfrom(net_socket(), buf, sizeof(buf), 0,
                         (struct sockaddr *)&src, &slen);
        if (n < (int)sizeof(wt_header_t)) {
            continue;
        }
        wt_header_t hdr;
        memcpy(&hdr, buf, sizeof(hdr));
        if (hdr.magic != WT_MAGIC || hdr.version != WT_PROTO_VERSION) {
            continue;
        }
        net_note_rx_from(&src);
        int payload = n - sizeof(wt_header_t);
        if ((hdr.flags & WT_FLAG_AUDIO) && payload > 0) {
            jb_insert(hdr.seq, buf + sizeof(wt_header_t), (uint16_t)payload);
        }
    }
}

/* ---------------- jitter buffer -> decode -> playback ---------------- */

static void playback_task(void *arg)
{
    static const int16_t silence[WT_FRAME_SAMPLES] = { 0 };
    static int16_t pcm[WT_FRAME_SAMPLES];
    static int16_t scratch[WT_FRAME_SAMPLES];
    static uint8_t buf[WT_MAX_PAYLOAD];
    uint16_t len;
    uint16_t expect = 0;
    int misses = 0;
    bool playing = false;

    for (;;) {
        if (!playing) {
            if (jb_depth() >= WT_JB_PREFILL) {
                expect = jb_head_seq() - WT_JB_PREFILL + 1;
                misses = 0;
                playing = true;
                ESP_LOGI(TAG, "playback started at seq %u", expect);
            } else {
                audio_play(silence);  /* stay fed and paced while idle */
                continue;
            }
        }

        if (jb_take(expect, buf, &len)) {
            codec_decode(buf, len, pcm);
            misses = 0;
        } else if (jb_peek((uint16_t)(expect + 1), buf, &len)) {
            codec_decode_fec(buf, len, pcm);  /* recover from next packet's FEC */
            misses++;
        } else {
            codec_decode(NULL, 0, pcm);       /* packet-loss concealment */
            misses++;
        }
        expect++;

        if (misses > WT_LOSS_RESET) {
            ESP_LOGI(TAG, "stream stalled, re-buffering");
            jb_reset();
            playing = false;
            continue;
        }

        /* The two units' 48 kHz clocks drift; if the buffer creeps up,
         * consume one extra frame to re-center latency. */
        if (jb_depth() > WT_JB_HIGH_WM && jb_take(expect, buf, &len)) {
            codec_decode(buf, len, scratch);  /* decode+discard keeps decoder state */
            expect++;
        }

        audio_play(pcm);
    }
}

/* ---------------- housekeeping: button, LED, discovery, keepalive ------- */

static void update_led(void)
{
    if (s_muted) {
        led_set_state(LED_STATE_MUTED);
    } else if (net_peer_known()) {
        led_set_state(LED_STATE_LINKED);
    } else {
        led_set_state(LED_STATE_NO_PEER);
    }
}

static void housekeeping_loop(void)
{
    gpio_config_t btn_cfg = {
        .pin_bit_mask = 1ULL << WT_PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&btn_cfg);

    bool btn_last = true;
    int64_t btn_edge_us = 0;
    int64_t last_keepalive_us = 0;
    int64_t last_discovery_us = 0;
    uint16_t ka_seq = 0;

    for (;;) {
        int64_t now = esp_timer_get_time();

        /* USER button (active low): toggle mute, 50 ms debounce */
        bool btn = gpio_get_level(WT_PIN_BUTTON);
        if (btn_last && !btn && now - btn_edge_us > 50000) {
            btn_edge_us = now;
            s_muted = !s_muted;
            ESP_LOGI(TAG, "mic %s", s_muted ? "muted" : "live");
        }
        btn_last = btn;

        /* While muted no audio flows, so keep the link alive explicitly. */
        if (s_muted && net_peer_known() && now - last_keepalive_us > WT_KEEPALIVE_US) {
            last_keepalive_us = now;
            wt_header_t ka = {
                .magic = WT_MAGIC,
                .seq = ka_seq++,
                .flags = 0,
                .version = WT_PROTO_VERSION,
            };
            net_send(&ka, sizeof(ka));
        }

        if (now - last_discovery_us > WT_DISCOVERY_US) {
            last_discovery_us = now;
            net_discovery_poll();
        }
        net_check_peer_timeout();
        update_led();

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    led_init();
    led_set_state(LED_STATE_WIFI_CONNECTING);

    ESP_ERROR_CHECK(net_wifi_start());
    ESP_ERROR_CHECK(net_udp_start());
    ESP_ERROR_CHECK(audio_init());
    ESP_ERROR_CHECK(codec_init());
    jb_init();

    /* Audio on core 1, network on core 0 (with the WiFi stack). Encode and
     * decode both use significant stack in libopus. */
    xTaskCreatePinnedToCore(capture_task, "capture", 24 * 1024, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(playback_task, "playback", 24 * 1024, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(rx_task, "udp_rx", 6 * 1024, NULL, 9, NULL, 0);

    ESP_LOGI(TAG, "walkie-talkie up");
    housekeeping_loop();
}
