#include <string.h>
#include "net.h"
#include "app_config.h"
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

static const char *TAG = "net";

#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_wifi_ev;
static int s_sock = -1;

static SemaphoreHandle_t s_peer_lock;
static struct sockaddr_in s_peer;
static bool s_have_peer;
static bool s_static_peer;
static int64_t s_last_rx_us;

static char s_hostname[32];

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
        xEventGroupSetBits(s_wifi_ev, WIFI_CONNECTED_BIT);
    }
}

esp_err_t net_wifi_start(void)
{
    s_wifi_ev = xEventGroupCreate();
    s_peer_lock = xSemaphoreCreateMutex();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               wifi_event_handler, NULL));

    wifi_config_t wc = { 0 };
    strlcpy((char *)wc.sta.ssid, CONFIG_WT_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_WT_WIFI_PASSWORD, sizeof(wc.sta.password));
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

    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_hostname, sizeof(s_hostname), "walkie-%02x%02x%02x", mac[3], mac[4], mac[5]);

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
    }
    return ESP_OK;
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
    xSemaphoreTake(s_peer_lock, portMAX_DELAY);
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

void net_discovery_poll(void)
{
    if (s_static_peer || net_peer_known()) {
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
    if (!s_static_peer && s_have_peer &&
        esp_timer_get_time() - s_last_rx_us > WT_PEER_TIMEOUT_US) {
        s_have_peer = false;
        ESP_LOGW(TAG, "peer lost (no packets for %d s)",
                 (int)(WT_PEER_TIMEOUT_US / 1000000));
    }
    xSemaphoreGive(s_peer_lock);
}
