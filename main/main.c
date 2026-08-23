#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "nvs.h"
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
#include "console.h"

static const char *TAG = "walkie";
#define WT_BUILD_TAG "WTKI-2026-08-23-J"

static volatile bool s_muted = false;
static volatile uint8_t s_volume = 100;     /* speaker volume percent, 0-100 */
static volatile bool s_vol_dirty = false;   /* pending NVS save (debounced) */
static volatile int64_t s_vol_change_us = 0;
static volatile uint8_t s_tx_mode = WT_TXMODE_ALWAYS;  /* NVS-persisted */
static volatile bool s_tx_active = false;   /* transmitting right now */
static volatile uint8_t s_mic_level = 0;    /* latest frame peak >> 7 */
static volatile uint8_t s_vox_thresh = WT_VOX_THRESH_DEFAULT;  /* NVS */
static volatile uint8_t s_mic_gain = 100;   /* percent, 0-200, NVS-persisted */
static volatile bool s_mic_agc = false;     /* auto gain + noise gate, NVS */
static volatile uint8_t s_agc_report = 16;  /* current AGC gain, 1/16 steps */
static volatile int64_t s_last_play_us = 0; /* last real far-end frame played */
static nvs_handle_t s_nvs;
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

    int64_t vox_hold_until = 0;

    float applied_gain = 1.0f;   /* gain the last frame ended on (ramp start) */
    float agc = 1.0f;            /* adaptive component, speech-driven only */
    float noise_floor = WT_NF_MIN;
    float nf_run = 1e9f;         /* minimum seen in the current window */
    int nf_count = 0;
    int nr_hold = 0;             /* frames the noise gate stays open */

    for (;;) {
        /* All mic conditioning is folded into one gain that audio_capture
         * applies in the 32-bit domain and ramps across the frame, so a
         * quiet mic can be boosted without amplifying truncation noise and
         * gate/AGC moves never click. The level driving it is the previous
         * frame's pre-gain peak (20 ms of lag, standard for AGC). */
        float manual = (float)s_mic_gain / 100.0f;
        float target = manual;
        if (s_mic_agc) {
            float in_peak = (float)audio_last_raw_peak() / 65536.0f;
            /* rolling minimum -> the room's own noise floor */
            if (in_peak < nf_run) {
                nf_run = in_peak;
            }
            if (++nf_count >= WT_NF_WINDOW) {
                noise_floor += (nf_run - noise_floor) * WT_NF_SMOOTH;
                if (noise_floor < WT_NF_MIN) {
                    noise_floor = WT_NF_MIN;
                }
                nf_run = in_peak;
                nf_count = 0;
            }
            bool voiced = in_peak > noise_floor * WT_NF_SPEECH_MULT
                                    + WT_NF_SPEECH_MARGIN;
            nr_hold = voiced ? WT_NR_HOLD_FRAMES
                             : (nr_hold > 0 ? nr_hold - 1 : 0);
            if (voiced) {
                /* converge on a consistent output level for speech */
                float want = WT_AGC_TARGET_PEAK / (in_peak * manual + 1.0f);
                if (want < WT_AGC_MIN) want = WT_AGC_MIN;
                if (want > WT_AGC_MAX) want = WT_AGC_MAX;
                float rate = (want < agc) ? WT_AGC_ATTACK : WT_AGC_RELEASE;
                agc += (want - agc) * rate;
            }
            /* noise-only frames keep the adapted gain but get ducked */
            target = manual * agc * (nr_hold > 0 ? 1.0f : WT_NR_ATTEN);
            s_agc_report = (uint8_t)(agc * 16.0f > 255.0f ? 255 : agc * 16.0f);
        } else {
            agc = 1.0f;
            noise_floor = WT_NF_MIN;
            nf_run = 1e9f;
            nf_count = 0;
            nr_hold = 0;
            s_agc_report = 16;
        }
        if (audio_capture(pcm, applied_gain, target) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        applied_gain = target;
        /* Gate decisions use the input level (this frame's pre-gain peak,
         * scaled by manual gain only) - NOT the output. Reading back the
         * ducked output would be a trap: once the noise gate closed, the
         * first word would arrive attenuated, stay under the threshold, and
         * the gate could never reopen. It is also the level the UI's gate
         * line is drawn against, so "bars above the line" stays truthful. */
        float in_level = ((float)audio_last_raw_peak() / 65536.0f) * manual;
        int16_t fpeak = in_level > 32767.0f ? 32767 : (int16_t)in_level;
        int16_t opeak = 0;  /* what actually goes out, for the log */
        for (int i = 0; i < WT_FRAME_SAMPLES; i++) {
            int16_t a = pcm[i] < 0 ? -pcm[i] : pcm[i];
            if (a > opeak) opeak = a;
        }
        if (opeak > peak) peak = opeak;
        s_mic_level = (uint8_t)(fpeak >> 7);  /* live level for UIs */
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
        bool have_route = tcp_active || net_group_size() > 0 || net_peer_known();
        bool gate = true;
        if (s_tx_mode == WT_TXMODE_VOX) {
            /* transmit only on local speech while our speaker is quiet;
             * the XMOS AEC keeps speaker audio out of the mic, so the
             * far end can't hold the gate open. */
            int64_t nowv = esp_timer_get_time();
            int32_t th = WT_VOX_PEAK_OF(s_vox_thresh);
            if (fpeak >= th &&
                nowv - s_last_play_us > WT_VOX_RX_BLOCK_US) {
                vox_hold_until = nowv + WT_VOX_TX_HANG_US;
            }
            gate = nowv < vox_hold_until;
        }
        s_tx_active = have_route && !s_muted && gate;
        if (!s_tx_active) {
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
            if (n > 0 && tcp_send_frame(enc_buf, n)) {
                sent++;
            }
        }
        /* UDP path: group members always; the adopted/manual peer only
         * when no TCP client is being served (TCP wins a 1:1 call). */
        bool udp_route = net_group_size() > 0 ||
                         (!tcp_active && net_peer_known());
        if (!udp_route) {
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
            net_send_all(pkt, batch_len);
            sent += batch_cnt;
            batch_cnt = 0;
            batch_len = sizeof(wt_header_t);
        }
    }
}

/* ---------------- UDP -> jitter buffer ---------------- */

static bool send_status_reply(const struct sockaddr_in *dst)
{
    uint8_t rsp[sizeof(wt_header_t) + sizeof(wt_status_t)];
    wt_header_t rh = { .magic = WT_MAGIC, .seq = 0,
                       .flags = WT_FLAG_CTRL,
                       .version = WT_PROTO_VERSION };
    memcpy(rsp, &rh, sizeof(rh));
    wt_status_t *st = (wt_status_t *)(rsp + sizeof(rh));
    net_fill_status(st, s_muted);
    st->volume = s_volume;
    st->tx_mode = s_tx_mode;
    st->tx_active = s_tx_active ? 1 : 0;
    st->rx_active =
        (esp_timer_get_time() - s_last_play_us < 400 * 1000) ? 1 : 0;
    st->mic_level = s_mic_level;
    st->vox_thresh = s_vox_thresh;
    st->mic_gain = s_mic_gain;
    st->mic_agc = s_mic_agc ? 1 : 0;
    st->agc_gain = s_agc_report;
    if (s_tcp_fd >= 0) {  /* a TCP call counts as linked */
        st->linked = 1;
    }
    int sn = sendto(net_socket(), rsp, sizeof(rsp), 0,
                    (struct sockaddr *)dst, sizeof(*dst));
    return sn == (int)sizeof(rsp);
}

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
                if (send_status_reply(&src)) {
                    s_tx_ctrl++;
                } else {
                    ESP_LOGW(TAG, "ctrl reply sendto failed: errno %d", errno);
                }
            } else if (cmd == WT_CTRL_SET_PEER &&
                       payload >= (int)sizeof(wt_set_peer_t)) {
                wt_set_peer_t sp;
                memcpy(&sp, buf + sizeof(wt_header_t), sizeof(sp));
                net_set_peer_manual(sp.ip, sp.port);
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_VOL &&
                       payload >= (int)sizeof(wt_set_vol_t)) {
                wt_set_vol_t sv;
                memcpy(&sv, buf + sizeof(wt_header_t), sizeof(sv));
                uint8_t vol = sv.volume > 100 ? 100 : sv.volume;
                if (vol != s_volume) {
                    s_volume = vol;
                    s_vol_dirty = true;
                    s_vol_change_us = esp_timer_get_time();
                    ESP_LOGI(TAG, "speaker volume set to %u%%", (unsigned)vol);
                }
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_MODE && payload >= 2) {
                uint8_t m = buf[sizeof(wt_header_t) + 1] ? WT_TXMODE_VOX
                                                         : WT_TXMODE_ALWAYS;
                if (m != s_tx_mode) {
                    s_tx_mode = m;
                    if (s_nvs) {
                        nvs_set_u8(s_nvs, "tx_mode", m);
                        nvs_commit(s_nvs);
                    }
                    ESP_LOGI(TAG, "tx mode: %s",
                             m == WT_TXMODE_VOX ? "voice" : "always");
                }
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_THRESH && payload >= 2) {
                uint8_t sv = buf[sizeof(wt_header_t) + 1];
                if (sv > 100) {
                    sv = 100;
                }
                if (sv != s_vox_thresh) {
                    s_vox_thresh = sv;
                    if (s_nvs) {
                        nvs_set_u8(s_nvs, "vox_th", sv);
                        nvs_commit(s_nvs);
                    }
                    ESP_LOGI(TAG, "vox threshold set to %u (peak %ld)",
                             (unsigned)sv, (long)WT_VOX_PEAK_OF(sv));
                }
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_GAIN && payload >= 2) {
                uint8_t gv = buf[sizeof(wt_header_t) + 1];
                if (gv > 200) {
                    gv = 200;
                }
                if (gv != s_mic_gain) {
                    s_mic_gain = gv;
                    if (s_nvs) {
                        nvs_set_u8(s_nvs, "mic_gain", gv);
                        nvs_commit(s_nvs);
                    }
                    ESP_LOGI(TAG, "mic gain set to %u%%", (unsigned)gv);
                }
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_AGC && payload >= 2) {
                bool on = buf[sizeof(wt_header_t) + 1] != 0;
                if (on != s_mic_agc) {
                    s_mic_agc = on;
                    if (s_nvs) {
                        nvs_set_u8(s_nvs, "mic_agc", on ? 1 : 0);
                        nvs_commit(s_nvs);
                    }
                    ESP_LOGI(TAG, "auto gain + noise gate %s",
                             on ? "on" : "off");
                }
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_MUTE && payload >= 2) {
                s_muted = buf[sizeof(wt_header_t) + 1] != 0;
                ESP_LOGI(TAG, "mic %s (remote)", s_muted ? "muted" : "live");
                send_status_reply(&src);
            } else if (cmd == WT_CTRL_SET_GROUP && payload >= 2) {
                int count = buf[sizeof(wt_header_t) + 1];
                if (count <= WT_MAX_MEMBERS &&
                    payload >= 2 + count * (int)sizeof(wt_member_t)) {
                    net_set_group(buf + sizeof(wt_header_t) + 2, count);
                }
                send_status_reply(&src);
            }
            continue;
        }

        net_note_rx_from(&src);
        /* While a TCP client is talking, the jitter buffer belongs to that
         * stream: its seq space (0,1,2,...) is unrelated to a UDP peer's, and
         * interleaving the two garbles playback into noise. TCP wins. */
        if ((hdr.flags & WT_FLAG_AUDIO) && payload > 0 && s_tcp_fd < 0) {
            /* Group audio: one speaker holds the floor at a time. A source
             * owns playback until it has been silent for WT_SPK_LOCK_US;
             * other members' audio is dropped meanwhile (their seq spaces
             * are unrelated - mixing them would garble the buffer). */
            static uint32_t spk_ip;
            static uint16_t spk_port;
            static int64_t spk_last_us;
            int64_t nowr = esp_timer_get_time();
            if (src.sin_addr.s_addr != spk_ip || src.sin_port != spk_port) {
                if (nowr - spk_last_us < WT_SPK_LOCK_US) {
                    continue;
                }
                spk_ip = src.sin_addr.s_addr;
                spk_port = src.sin_port;
            }
            spk_last_us = nowr;
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

static void apply_volume(int16_t *pcm)
{
    uint8_t vol = s_volume;
    if (vol >= 100) {
        return;
    }
    int32_t q = ((int32_t)vol << 15) / 100;  /* Q15 gain */
    for (int i = 0; i < WT_FRAME_SAMPLES; i++) {
        pcm[i] = (int16_t)(((int32_t)pcm[i] * q) >> 15);
    }
}

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
            s_last_play_us = esp_timer_get_time();
        } else if (jb_peek((uint16_t)(expect + 1), buf, &len)) {
            codec_decode_fec(buf, len, pcm);  /* recover from next packet's FEC */
            misses++;
            s_last_play_us = esp_timer_get_time();
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

        apply_volume(pcm);
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

static void settings_nvs_load(void)
{
    if (nvs_open("walkie", NVS_READWRITE, &s_nvs) != ESP_OK) {
        ESP_LOGW(TAG, "nvs_open failed; settings will not persist");
        s_nvs = 0;
        return;
    }
    uint8_t v;
    if (nvs_get_u8(s_nvs, "spk_vol", &v) == ESP_OK && v <= 100) {
        s_volume = v;
        ESP_LOGI(TAG, "speaker volume restored: %u%%", (unsigned)v);
    }
    if (nvs_get_u8(s_nvs, "tx_mode", &v) == ESP_OK && v <= WT_TXMODE_VOX) {
        s_tx_mode = v;
        ESP_LOGI(TAG, "tx mode restored: %s",
                 v == WT_TXMODE_VOX ? "voice" : "always");
    }
    if (nvs_get_u8(s_nvs, "vox_th", &v) == ESP_OK && v <= 100) {
        s_vox_thresh = v;
        ESP_LOGI(TAG, "vox threshold restored: %u", (unsigned)v);
    }
    if (nvs_get_u8(s_nvs, "mic_gain", &v) == ESP_OK && v <= 200) {
        s_mic_gain = v;
        ESP_LOGI(TAG, "mic gain restored: %u%%", (unsigned)v);
    }
    if (nvs_get_u8(s_nvs, "mic_agc", &v) == ESP_OK) {
        s_mic_agc = v != 0;
        ESP_LOGI(TAG, "auto gain restored: %s", s_mic_agc ? "on" : "off");
    }
}

/* Debounced: flash writes briefly stall the other core's cache, so save
 * once the slider has settled instead of on every drag tick. */
static void volume_nvs_save_if_due(int64_t now)
{
    if (!s_vol_dirty || s_nvs == 0 || now - s_vol_change_us < 1000000) {
        return;
    }
    s_vol_dirty = false;
    if (nvs_set_u8(s_nvs, "spk_vol", s_volume) == ESP_OK) {
        nvs_commit(s_nvs);
    }
}

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
            if (net_peer_known() || net_group_size() > 0) {
                net_send_all(&ka, sizeof(ka));
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
        volume_nvs_save_if_due(now);
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
    settings_nvs_load();

    /* Console first: a factory-fresh unit has no WiFi credentials and must
     * be configurable over USB while net_wifi_start() waits for a network. */
    console_start(WT_BUILD_TAG);

    ESP_ERROR_CHECK(net_wifi_start());
    ESP_ERROR_CHECK(net_udp_start());
    ESP_ERROR_CHECK(audio_init());
    ESP_ERROR_CHECK(codec_init());
    jb_init();

    /* Audio on core 1, network on core 0 (with the WiFi stack). libopus is
     * stack-hungry: opus_encode at 48 kHz overflowed a 24 KB stack the moment
     * a call started (proven by a capture-task stack overflow panic on every
     * TCP connect), so both audio tasks get 48 KB. */
    xTaskCreatePinnedToCore(capture_task, "capture", 48 * 1024, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(playback_task, "playback", 48 * 1024, NULL, 10, NULL, 1);
    xTaskCreatePinnedToCore(rx_task, "udp_rx", 6 * 1024, NULL, 9, NULL, 0);
    xTaskCreatePinnedToCore(tcp_srv_task, "tcp_srv", 6 * 1024, NULL, 9, NULL, 0);

    ESP_LOGI(TAG, "walkie-talkie up, build " WT_BUILD_TAG);
    housekeeping_loop();
}
