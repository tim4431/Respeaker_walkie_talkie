#include <string.h>
#include "net.h"
#include "app_config.h"
#include "protocol.h"
#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_mac.h"
#include "mdns.h"
#include "nvs.h"

static const char *TAG = "net";

#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_wifi_ev;
static int s_sock = -1;

static SemaphoreHandle_t s_peer_lock;
static struct sockaddr_in s_peer;
static bool s_have_peer;
static bool s_static_peer;   /* Kconfig static IP */
static bool s_locked_peer;   /* manual WT_CTRL_SET_PEER */
static int64_t s_last_rx_us;
static uint32_t s_own_ip;

static char s_hostname[32];

static wt_member_t s_members[WT_MAX_MEMBERS];
static int s_member_count;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_ev, WIFI_CONNECTED_BIT);
        ESP_LOGW(TAG, "WiFi disconnected, retrying");
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&ev->ip_info.ip));
        s_own_ip = ev->ip_info.ip.addr;
        xEventGroupSetBits(s_wifi_ev, WIFI_CONNECTED_BIT);
    }
}

esp_err_t net_wifi_start(void)
{
    s_wifi_ev = xEventGroupCreate();
    s_peer_lock = xSemaphoreCreateMutex();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();

    /* Show up as "walkie-xxxxxx" in the router's client list instead of
     * lwIP's default "espressif". */
    esp_netif_set_hostname(sta, net_hostname());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               wifi_event_handler, NULL));

    /* Credentials saved over the USB console (NVS) win over the ones baked
     * into the build, so a unit can be re-homed without reflashing. */
    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, CONFIG_WT_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_WT_WIFI_PASSWORD, sizeof(wc.sta.password));
    nvs_handle_t nh;
    if (nvs_open("walkie", NVS_READONLY, &nh) == ESP_OK) {
        char ssid[33], pass[65];
        size_t sl = sizeof(ssid), pl = sizeof(pass);
        if (nvs_get_str(nh, "wifi_ssid", ssid, &sl) == ESP_OK && ssid[0] &&
            nvs_get_str(nh, "wifi_pass", pass, &pl) == ESP_OK) {
            strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
            strlcpy((char *)wc.sta.password, pass, sizeof(wc.sta.password));
            ESP_LOGI(TAG, "using WiFi credentials from NVS (SSID: %s)", ssid);
        }
        nvs_close(nh);
    }
    if (!wc.sta.ssid[0]) {
        ESP_LOGW(TAG, "no WiFi credentials - configure over USB: "
                      "wifi <ssid> <password>");
    }
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    xEventGroupWaitBits(s_wifi_ev, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);

    /* Modem power save causes 100-300 ms audio dropouts; latency beats
     * power draw for an intercom. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    return ESP_OK;
}

esp_err_t net_udp_start(void)
{
    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (s_sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return ESP_FAIL;
    }
    struct sockaddr_in local = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_WT_UDP_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(s_sock, (struct sockaddr *)&local, sizeof(local)) < 0) {
        ESP_LOGE(TAG, "bind() failed: errno %d", errno);
        return ESP_FAIL;
    }
    int bc = 1;
    setsockopt(s_sock, SOL_SOCKET, SO_BROADCAST, &bc, sizeof(bc));

    ESP_ERROR_CHECK(mdns_init());
    ESP_ERROR_CHECK(mdns_hostname_set(s_hostname));
    ESP_ERROR_CHECK(mdns_instance_name_set(s_hostname));
    ESP_ERROR_CHECK(mdns_service_add(NULL, "_walkie", "_udp", CONFIG_WT_UDP_PORT, NULL, 0));
    ESP_LOGI(TAG, "listening on UDP %d as %s.local", CONFIG_WT_UDP_PORT, s_hostname);

    if (strlen(CONFIG_WT_PEER_IP) > 0) {
        memset(&s_peer, 0, sizeof(s_peer));
        s_peer.sin_family = AF_INET;
        s_peer.sin_port = htons(CONFIG_WT_UDP_PORT);
        s_peer.sin_addr.s_addr = inet_addr(CONFIG_WT_PEER_IP);
        s_have_peer = true;
        s_static_peer = true;
        ESP_LOGI(TAG, "static peer %s", CONFIG_WT_PEER_IP);
    } else {
        /* restore a manual pairing (WT_CTRL_SET_PEER) across reboots */
        nvs_handle_t nh;
        if (nvs_open("walkie", NVS_READONLY, &nh) == ESP_OK) {
            uint32_t ip = 0;
            uint16_t port = 0;
            if (nvs_get_u32(nh, "peer_ip", &ip) == ESP_OK && ip != 0) {
                nvs_get_u16(nh, "peer_port", &port);
                memset(&s_peer, 0, sizeof(s_peer));
                s_peer.sin_family = AF_INET;
                s_peer.sin_addr.s_addr = ip;
                s_peer.sin_port = port ? port : htons(CONFIG_WT_UDP_PORT);
                s_have_peer = true;
                s_locked_peer = true;
                s_last_rx_us = esp_timer_get_time();
                ESP_LOGI(TAG, "manual peer restored: %s:%u",
                         inet_ntoa(s_peer.sin_addr),
                         (unsigned)ntohs(s_peer.sin_port));
            }
            nvs_close(nh);
        }
    }
    {
        nvs_handle_t nh;
        if (nvs_open("walkie", NVS_READONLY, &nh) == ESP_OK) {
            size_t blen = sizeof(s_members);
            if (nvs_get_blob(nh, "members", s_members, &blen) == ESP_OK) {
                s_member_count = blen / sizeof(wt_member_t);
                ESP_LOGI(TAG, "group restored: %d member(s)", s_member_count);
            }
            nvs_close(nh);
        }
    }
    return ESP_OK;
}

/* ip/port in network order; ip 0 erases the stored pairing. */
static void persist_peer(uint32_t ip, uint16_t port)
{
    nvs_handle_t nh;
    if (nvs_open("walkie", NVS_READWRITE, &nh) != ESP_OK) {
        return;
    }
    if (ip == 0) {
        nvs_erase_key(nh, "peer_ip");
        nvs_erase_key(nh, "peer_port");
    } else {
        nvs_set_u32(nh, "peer_ip", ip);
        nvs_set_u16(nh, "peer_port", port);
    }
    nvs_commit(nh);
    nvs_close(nh);
}

int net_socket(void)
{
    return s_sock;
}

bool net_peer_known(void)
{
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    bool p = s_have_peer;
    xSemaphoreGive(s_peer_lock);
    return p;
}

void net_send(const void *buf, size_t len)
{
    struct sockaddr_in peer;
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    bool have = s_have_peer;
    peer = s_peer;
    xSemaphoreGive(s_peer_lock);
    if (!have) {
        return;
    }
    sendto(s_sock, buf, len, 0, (struct sockaddr *)&peer, sizeof(peer));
}

void net_note_rx_from(const struct sockaddr_in *src)
{
    if (src->sin_addr.s_addr == s_own_ip) {
        return;  /* our own broadcast echoed back */
    }
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    if (s_locked_peer) {
        /* manual pairing: only the assigned peer refreshes liveness */
        if (src->sin_addr.s_addr == s_peer.sin_addr.s_addr) {
            s_last_rx_us = esp_timer_get_time();
        }
        xSemaphoreGive(s_peer_lock);
        return;
    }
    if (!s_static_peer) {
        if (!s_have_peer) {
            ESP_LOGI(TAG, "peer adopted from RX: %s", inet_ntoa(src->sin_addr));
        }
        s_peer = *src;
        s_have_peer = true;
    }
    s_last_rx_us = esp_timer_get_time();
    xSemaphoreGive(s_peer_lock);
}

void net_set_peer_manual(uint32_t ip, uint16_t port)
{
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    uint16_t stored_port = 0;
    if (ip == 0) {
        s_locked_peer = false;
        s_have_peer = false;
        ESP_LOGI(TAG, "manual peer cleared - back to auto");
    } else {
        memset(&s_peer, 0, sizeof(s_peer));
        s_peer.sin_family = AF_INET;
        s_peer.sin_addr.s_addr = ip;
        s_peer.sin_port = port ? port : htons(CONFIG_WT_UDP_PORT);
        stored_port = s_peer.sin_port;
        s_have_peer = true;
        s_locked_peer = true;
        s_last_rx_us = esp_timer_get_time();
        ESP_LOGI(TAG, "manual peer set: %s:%u", inet_ntoa(s_peer.sin_addr),
                 (unsigned)ntohs(s_peer.sin_port));
    }
    xSemaphoreGive(s_peer_lock);
    persist_peer(ip, stored_port);
}

void net_fill_status(void *status, bool muted)
{
    wt_status_t *st = (wt_status_t *)status;
    memset(st, 0, sizeof(*st));
    st->cmd = WT_CTRL_STATUS_RSP;
    st->proto = WT_PROTO_VERSION;
    st->muted = muted ? 1 : 0;

    wifi_ap_record_t ap;
    st->rssi = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : 0;

    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    st->linked = (s_have_peer &&
                  esp_timer_get_time() - s_last_rx_us < 3 * 1000 * 1000) ? 1 : 0;
    st->peer_locked = s_locked_peer ? 1 : 0;
    if (s_have_peer) {
        st->peer_ip = s_peer.sin_addr.s_addr;
        st->peer_port = s_peer.sin_port;
    }
    st->member_count = s_member_count;
    memcpy(st->members, s_members, sizeof(st->members));
    xSemaphoreGive(s_peer_lock);
    strlcpy(st->hostname, net_hostname(), sizeof(st->hostname));
}

const char *net_hostname(void)
{
    if (!s_hostname[0]) {
        /* lazy: the USB console may ask before WiFi is up (MAC is in efuse) */
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        snprintf(s_hostname, sizeof(s_hostname), "walkie-%02x%02x%02x",
                 mac[3], mac[4], mac[5]);
    }
    return s_hostname;
}

void net_set_group(const void *entries, int count)
{
    if (count > WT_MAX_MEMBERS) {
        count = WT_MAX_MEMBERS;
    }
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    s_member_count = count;
    if (count > 0) {
        memcpy(s_members, entries, count * sizeof(wt_member_t));
    }
    xSemaphoreGive(s_peer_lock);
    ESP_LOGI(TAG, "group set: %d member(s)", count);

    nvs_handle_t nh;
    if (nvs_open("walkie", NVS_READWRITE, &nh) == ESP_OK) {
        if (count > 0) {
            nvs_set_blob(nh, "members", entries, count * sizeof(wt_member_t));
        } else {
            nvs_erase_key(nh, "members");
        }
        nvs_commit(nh);
        nvs_close(nh);
    }
}

int net_group_size(void)
{
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    int n = s_member_count;
    xSemaphoreGive(s_peer_lock);
    return n;
}

void net_send_all(const void *buf, size_t len)
{
    wt_member_t members[WT_MAX_MEMBERS];
    int count;
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    count = s_member_count;
    memcpy(members, s_members, sizeof(members));
    xSemaphoreGive(s_peer_lock);
    if (count == 0) {
        net_send(buf, len);
        return;
    }
    for (int i = 0; i < count; i++) {
        struct sockaddr_in a = {
            .sin_family = AF_INET,
            .sin_addr.s_addr = members[i].ip,
            .sin_port = members[i].port,
        };
        sendto(s_sock, buf, len, 0, (struct sockaddr *)&a, sizeof(a));
    }
}

void net_send_presence(const void *buf, size_t len)
{
    /* Broadcast a tiny packet: keeps the AP's station state fresh (some
     * consumer APs stop delivering downlink to a long-silent STA) and
     * doubles as peer discovery - idle units adopt each other from it. */
    struct sockaddr_in bcast = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_WT_UDP_PORT),
        .sin_addr.s_addr = htonl(INADDR_BROADCAST),
    };
    sendto(s_sock, buf, len, 0, (struct sockaddr *)&bcast, sizeof(bcast));
}

void net_discovery_poll(void)
{
    if (s_static_peer || s_locked_peer || net_peer_known()) {
        return;
    }
    mdns_result_t *results = NULL;
    esp_err_t err = mdns_query_ptr("_walkie", "_udp", 2000, 4, &results);
    if (err != ESP_OK || results == NULL) {
        return;
    }
    for (mdns_result_t *r = results; r != NULL; r = r->next) {
        if (r->hostname == NULL || strcmp(r->hostname, s_hostname) == 0) {
            continue;  /* that's us */
        }
        for (mdns_ip_addr_t *a = r->addr; a != NULL; a = a->next) {
            if (a->addr.type == ESP_IPADDR_TYPE_V4) {
                xSemaphoreTake(s_peer_lock, portMAX_DELAY);
                memset(&s_peer, 0, sizeof(s_peer));
                s_peer.sin_family = AF_INET;
                s_peer.sin_port = htons(r->port ? r->port : CONFIG_WT_UDP_PORT);
                s_peer.sin_addr.s_addr = a->addr.u_addr.ip4.addr;
                s_have_peer = true;
                s_last_rx_us = esp_timer_get_time();  /* grace period */
                xSemaphoreGive(s_peer_lock);
                ESP_LOGI(TAG, "peer discovered: %s at %s", r->hostname,
                         inet_ntoa(s_peer.sin_addr));
                mdns_query_results_free(results);
                return;
            }
        }
    }
    mdns_query_results_free(results);
}

void net_check_peer_timeout(void)
{
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
    if (!s_static_peer && !s_locked_peer && s_have_peer &&
        esp_timer_get_time() - s_last_rx_us > WT_PEER_TIMEOUT_US) {
        s_have_peer = false;
        ESP_LOGW(TAG, "peer lost (no packets for %d s)",
                 (int)(WT_PEER_TIMEOUT_US / 1000000));
    }
    xSemaphoreGive(s_peer_lock);
}
