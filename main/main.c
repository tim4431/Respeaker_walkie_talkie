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
#define WT_BUILD_TAG "WTKI-2026-08-22-A"

static volatile bool s_muted = false;
static volatile uint32_t s_rx_dgrams = 0;   /* all valid datagrams */
static volatile uint32_t s_rx_ctrl = 0;     /* ctrl requests handled */
static volatile uint32_t s_tx_ctrl = 0;     /* ctrl replies sent OK */

/* TCP audio transport (defined below the audio tasks) */
static volatile int s_tcp_fd = -1;
static bool tcp_send_frame(const uint8_t *data, int n);

/* ---------------- capture -> encode -> UDP ---------------- */

static void capture_task(void *arg)
{
    static int16_t pcm[WT_FRAME_SAMPLES];
    static uint8_t enc_buf[WT_MAX_PAYLOAD];
    static uint8_t pkt[sizeof(wt_header_t) + WT_MAX_BATCH * (2 + WT_MAX_PAYLOAD)];
    uint16_t seq = 0;
    int frames = 0, sent = 0, enc_max = 0;
    int16_t peak = 0;
    int32_t raw_peak = 0;
    /* pending batch of consecutive encoded frames */
    size_t batch_len = sizeof(wt_header_t);
    int batch_cnt = 0;
    uint16_t batch_seq = 0;
    int64_t batch_t0 = 0;

    for (;;) {
        if (audio_capture(pcm) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        /* mic health telemetry every ~2 s: peak of converted samples, peak of
         * raw 32-bit slot, and how many frames actually went out (DTX) */
        for (int i = 0; i < WT_FRAME_SAMPLES; i++) {
            int16_t a = pcm[i] < 0 ? -pcm[i] : pcm[i];
            if (a > peak) peak = a;
        }
        int32_t rp = audio_last_raw_peak();
        if (rp > raw_peak) raw_peak = rp;
        if (++frames >= 100) {
            ESP_LOGI(TAG, "mic peak16=%d sent=%d/%d enc_max=%dB tcp=%d peer=%d rx_dgrams=%lu",
                     peak, sent, frames, enc_max, (int)(s_tcp_fd >= 0),
                     (int)net_peer_known(), (unsigned long)s_rx_dgrams);
            frames = 0; sent = 0; peak = 0; raw_peak = 0; enc_max = 0;
            s_rx_dgrams = 0;
        }
        bool tcp_active = (s_tcp_fd >= 0);
        if (s_muted || (!tcp_active && !net_peer_known())) {
            batch_cnt = 0;               /* drop any half-built batch */
            batch_len = sizeof(wt_header_t);
            continue;  /* keep draining I2S; the frame is paced by the XMOS clock */
        }

        int64_t t0 = esp_timer_get_time();
        int n = codec_encode(pcm, enc_buf, WT_MAX_PAYLOAD);
        if (n > enc_max) {
            enc_max = n;
        }
        int64_t dt = esp_timer_get_time() - t0;
        if (dt > WT_FRAME_MS * 1000) {
            ESP_LOGW(TAG, "encode overrun: %lld us for a %d ms frame -- lower "
                          "WT_OPUS_COMPLEXITY", dt, WT_FRAME_MS);
        }

        if (tcp_active) {
            /* TCP peer (the PC) takes priority over any UDP peer */
            if (n > 0 && tcp_send_frame(enc_buf, n)) {
                sent++;
            }
            batch_cnt = 0;
            batch_len = sizeof(wt_header_t);
            continue;
        }

        bool flush = false;
        if (n > 0) {
            if (batch_cnt == 0) {
                batch_seq = seq;
                batch_t0 = t0;
            }
            pkt[batch_len] = (uint8_t)(n & 0xFF);
            pkt[batch_len + 1] = (uint8_t)(n >> 8);
            memcpy(pkt + batch_len + 2, enc_buf, n);
            batch_len += 2 + n;
            batch_cnt++;
            seq++;
            if (batch_cnt >= WT_FRAMES_PER_PKT) {
                flush = true;
            }
        } else if (batch_cnt > 0) {
            flush = true;  /* DTX gap: keep batched frames consecutive */
        }
        if (batch_cnt > 0 && esp_timer_get_time() - batch_t0 > WT_BATCH_FLUSH_US) {
            flush = true;
        }
        if (flush) {
            wt_header_t hdr = {
                .magic = WT_MAGIC,
                .seq = batch_seq,
                .flags = WT_FLAG_AUDIO,
                .version = WT_PROTO_VERSION,
            };
            memcpy(pkt, &hdr, sizeof(hdr));
            net_send(pkt, batch_len);
            sent += batch_cnt;
            batch_cnt = 0;
            batch_len = sizeof(wt_header_t);
        }
    }
}

/* ---------------- UDP -> jitter buffer ---------------- */

static void rx_task(void *arg)
{
    static uint8_t buf[sizeof(wt_header_t) + WT_MAX_BATCH * (2 + WT_MAX_PAYLOAD) + 64];

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
        int payload = n - sizeof(wt_header_t);
        s_rx_dgrams++;

        if (hdr.flags & WT_FLAG_CTRL) {
            /* control traffic: replies go to the requester and it never
             * counts as a peer (monitoring must not hijack the link) */
            if (payload < 1) {
                continue;
            }
            uint8_t cmd = buf[sizeof(wt_header_t)];
            if (cmd == WT_CTRL_STATUS_REQ) {
                s_rx_ctrl++;
                uint8_t rsp[sizeof(wt_header_t) + sizeof(wt_status_t)];
                wt_header_t rh = { .magic = WT_MAGIC, .seq = 0,
                                   .flags = WT_FLAG_CTRL,
                                   .version = WT_PROTO_VERSION };
                memcpy(rsp, &rh, sizeof(rh));
                net_fill_status(rsp + sizeof(rh), s_muted);
                if (s_tcp_fd >= 0) {  /* a TCP call counts as linked */
                    ((wt_status_t *)(rsp + sizeof(rh)))->linked = 1;
                }
                int sn = sendto(net_socket(), rsp, sizeof(rsp), 0,
                                (struct sockaddr *)&src, sizeof(src));
                if (sn == (int)sizeof(rsp)) {
                    s_tx_ctrl++;
                } else {
                    ESP_LOGW(TAG, "ctrl reply sendto failed: %d errno %d", sn, errno);
                }
            } else if (cmd == WT_CTRL_SET_PEER &&
                       payload >= (int)sizeof(wt_set_peer_t)) {
                wt_set_peer_t sp;
                memcpy(&sp, buf + sizeof(wt_header_t), sizeof(sp));
                net_set_peer_manual(sp.ip, sp.port);
                uint8_t rsp[sizeof(wt_header_t) + sizeof(wt_status_t)];
                wt_header_t rh = { .magic = WT_MAGIC, .seq = 0,
                                   .flags = WT_FLAG_CTRL,
                                   .version = WT_PROTO_VERSION };
                memcpy(rsp, &rh, sizeof(rh));
                net_fill_status(rsp + sizeof(rh), s_muted);
                sendto(net_socket(), rsp, sizeof(rsp), 0,
                       (struct sockaddr *)&src, sizeof(src));
            }
            continue;
        }

        net_note_rx_from(&src);
        if ((hdr.flags & WT_FLAG_AUDIO) && payload > 0) {
            /* v2: [u16 len][opus frame] repeated, consecutive seq */
            const uint8_t *p = buf + sizeof(wt_header_t);
            int rem = payload;
            uint16_t fseq = hdr.seq;
            while (rem >= 3) {
                uint16_t flen = (uint16_t)(p[0] | (p[1] << 8));
                p += 2;
                rem -= 2;
                if (flen == 0 || flen > rem || flen > WT_MAX_PAYLOAD) {
                    break;
                }
                jb_insert(fseq++, p, flen);
                p += flen;
                rem -= flen;
            }
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

/* ---------------- TCP audio transport ----------------------------------- */

/* This router blackholes random UDP flows but forwards TCP perfectly
 * (measured: 50 msg/s, 0 loss, 3.6 ms median RTT), so PC<->device audio
 * runs over TCP on port UDP_PORT+1. Stream format both ways: repeated
 * [u16 len][opus frame]; len 0 is a heartbeat. TCP is ordered, so the
 * receiver assigns its own consecutive seq numbers into the jitter
 * buffer and playback logic stays unchanged. */

static SemaphoreHandle_t s_tcp_tx_lock;

static bool tcp_send_frame(const uint8_t *data, int n)
{
    int fd = s_tcp_fd;
    if (fd < 0) {
        return false;
    }
    static uint8_t out[2 + WT_MAX_PAYLOAD];  /* guarded by s_tcp_tx_lock */
    xSemaphoreTake(s_tcp_tx_lock, portMAX_DELAY);
    out[0] = (uint8_t)(n & 0xFF);
    out[1] = (uint8_t)(n >> 8);
    if (n > 0) {
        memcpy(out + 2, data, n);
    }
    bool ok = send(fd, out, 2 + n, 0) == 2 + n;
    xSemaphoreGive(s_tcp_tx_lock);
    if (!ok) {
        shutdown(fd, SHUT_RDWR);  /* rx loop cleans up */
    }
    return ok;
}

static int recv_all(int fd, uint8_t *p, int n)
{
    while (n > 0) {
        int r = recv(fd, p, n, 0);
        if (r <= 0) {
            return -1;
        }
        p += r;
        n -= r;
    }
    return 0;
}

static void tcp_srv_task(void *arg)
{
    int ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    struct sockaddr_in a = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_WT_UDP_PORT + 6),  /* 5010: 5005 acts haunted */
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (bind(ls, (struct sockaddr *)&a, sizeof(a)) < 0 || listen(ls, 1) < 0) {
        ESP_LOGE(TAG, "tcp bind/listen failed: errno %d", errno);
        vTaskDelete(NULL);
        return;
    }
    ESP_LOGI(TAG, "tcp listener up on port %d", CONFIG_WT_UDP_PORT + 6);
    for (;;) {
        struct sockaddr_in ra;
        socklen_t ral = sizeof(ra);
        ESP_LOGI(TAG, "tcp: waiting for client (fd=%d)", ls);
        int c = accept(ls, (struct sockaddr *)&ra, &ral);
        if (c < 0) {
            ESP_LOGW(TAG, "tcp accept failed: errno %d", errno);
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        ESP_LOGI(TAG, "tcp: accepted fd=%d from %s:%u", c,
                 inet_ntoa(ra.sin_addr), (unsigned)ntohs(ra.sin_port));
        {
            /* identity banner: lets the PC verify WHICH build is serving */
            const char *tag = WT_BUILD_TAG;
            uint8_t banner[2 + 32];
            int tl = strlen(tag);
            banner[0] = (uint8_t)tl;
            banner[1] = 0;
            memcpy(banner + 2, tag, tl);
            send(c, banner, 2 + tl, 0);
        }
        setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        /* generous: ordinary WiFi retry bursts stall sends for hundreds
         * of ms; only a truly dead connection should tear the call down */
        struct timeval tv = { .tv_sec = 2, .tv_usec = 0 };
        setsockopt(c, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        /* a live PC sends frames or heartbeats constantly; 5 s of silence
         * means a zombie connection - drop it or it blocks accept forever */
        struct timeval rtv = { .tv_sec = 5, .tv_usec = 0 };
        setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &rtv, sizeof(rtv));
        /* RST on close: an audio stream has no data worth flushing, and
         * this keeps closed connections out of TIME_WAIT (small PCB pool) */
        struct linger lg = { .l_onoff = 1, .l_linger = 0 };
        setsockopt(c, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
        ESP_LOGI(TAG, "tcp audio client connected");
        jb_reset();
        s_tcp_fd = c;
        uint16_t fseq = 0;
        /* a legal Opus packet can be up to 1275 bytes */
        static uint8_t fb[1280];
        for (;;) {
            uint8_t lb[2];
            if (recv_all(c, lb, 2) != 0) {
                ESP_LOGW(TAG, "tcp: header recv failed (errno %d)", errno);
                break;
            }
            uint16_t flen = (uint16_t)(lb[0] | (lb[1] << 8));
            if (flen == 0) {
                continue;  /* heartbeat */
            }
            if (flen > sizeof(fb)) {
                ESP_LOGW(TAG, "tcp: bogus frame len %u - dropping client", flen);
                break;
            }
            if (recv_all(c, fb, flen) != 0) {
                ESP_LOGW(TAG, "tcp: body recv failed (errno %d)", errno);
                break;
            }
            /* oversized frames play fine after decode; jb slots hold up to
             * WT_MAX_PAYLOAD, so clamp-skip anything bigger (rare) */
            if (flen <= WT_MAX_PAYLOAD) {
                jb_insert(fseq, fb, flen);
            }
            fseq++;
        }
        s_tcp_fd = -1;
        close(c);
        jb_reset();
        ESP_LOGI(TAG, "tcp audio client gone (last errno %d)", errno);
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

        /* Never go radio-silent, and keep the peer's liveness fresh even
         * when DTX suppresses all audio: 1/s unicast keepalive to the peer,
         * plus a 2 s presence broadcast for discovery (idle units adopt
         * each other from it; monitors find us without mDNS). */
        if (now - last_keepalive_us > WT_KEEPALIVE_US) {
            last_keepalive_us = now;
            wt_header_t ka = {
                .magic = WT_MAGIC,
                .seq = ka_seq++,
                .flags = 0,
                .version = WT_PROTO_VERSION,
            };
            if (s_tcp_fd >= 0) {
                tcp_send_frame(NULL, 0);  /* heartbeat keeps liveness fresh */
            }
            if (net_peer_known()) {
                net_send(&ka, sizeof(ka));
            }
            if ((ka_seq & 1) == 0) {
                net_send_presence(&ka, sizeof(ka));
            }
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
    s_tcp_tx_lock = xSemaphoreCreateMutex();

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
    xTaskCreatePinnedToCore(tcp_srv_task, "tcp_srv", 6 * 1024, NULL, 9, NULL, 0);

    ESP_LOGI(TAG, "walkie-talkie up, build " WT_BUILD_TAG);
    housekeeping_loop();
}
