#include <string.h>
#include <stdio.h>
#include "console.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs.h"
#include "net.h"

/* Line-based config console on the USB-Serial-JTAG port (the XIAO's USB-C).
 *
 * Commands (terminated by \n or \r):
 *   info                     -> "WTCFG INFO host=<h> ssid=<s> build=<tag>"
 *   wifi <ssid> <password>   -> save credentials to NVS, reply, reboot
 *   reboot                   -> restart
 *
 * Replies are written with the driver API using a short timeout so a
 * disconnected/unread port can never block. ESP_LOG keeps using the default
 * console path (direct FIFO writes) - we deliberately do NOT switch the vfs
 * to driver mode, because driver-mode blocking writes would stall logging
 * tasks (including the audio telemetry) whenever no terminal drains the
 * port. Log lines may interleave with replies; clients must match on the
 * WTCFG prefix per line. */

static const char *TAG = "console";
static const char *s_build_tag = "?";

static void reply(const char *line)
{
    usb_serial_jtag_write_bytes(line, strlen(line), pdMS_TO_TICKS(100));
    usb_serial_jtag_write_bytes("\r\n", 2, pdMS_TO_TICKS(20));
}

static void handle_wifi(char *args)
{
    /* args: "<ssid> <password...>" - ssid must not contain spaces */
    char *ssid = args;
    char *pass = ssid ? strchr(ssid, ' ') : NULL;
    if (!ssid || !*ssid || !pass) {
        reply("WTCFG ERR usage: wifi <ssid> <password>");
        return;
    }
    *pass++ = '\0';
    while (*pass == ' ') pass++;
    /* trim trailing whitespace from the password */
    size_t pl = strlen(pass);
    while (pl > 0 && (pass[pl - 1] == ' ' || pass[pl - 1] == '\t')) {
        pass[--pl] = '\0';
    }
    if (strlen(ssid) > 32 || pl > 64 || pl < 8) {
        reply("WTCFG ERR ssid max 32 chars, password 8-64 chars");
        return;
    }
    nvs_handle_t h;
    if (nvs_open("walkie", NVS_READWRITE, &h) != ESP_OK) {
        reply("WTCFG ERR nvs open failed");
        return;
    }
    esp_err_t e1 = nvs_set_str(h, "wifi_ssid", ssid);
    esp_err_t e2 = nvs_set_str(h, "wifi_pass", pass);
    nvs_commit(h);
    nvs_close(h);
    if (e1 != ESP_OK || e2 != ESP_OK) {
        reply("WTCFG ERR nvs write failed");
        return;
    }
    ESP_LOGI(TAG, "wifi credentials saved for SSID '%s', rebooting", ssid);
    reply("WTCFG OK wifi saved, rebooting");
    vTaskDelay(pdMS_TO_TICKS(300));  /* let the reply drain to the host */
    esp_restart();
}

static void handle_info(void)
{
    char ssid[33] = "";
    size_t sl = sizeof(ssid);
    nvs_handle_t h;
    if (nvs_open("walkie", NVS_READONLY, &h) == ESP_OK) {
        nvs_get_str(h, "wifi_ssid", ssid, &sl);
        nvs_close(h);
    }
    char line[128];
    snprintf(line, sizeof(line), "WTCFG INFO host=%s ssid=%s build=%s",
             net_hostname(), ssid[0] ? ssid : "(from-build)", s_build_tag);
    reply(line);
}

static void handle_line(char *line)
{
    while (*line == ' ') line++;
    if (!*line) {
        return;
    }
    char *args = strchr(line, ' ');
    if (args) {
        *args++ = '\0';
        while (*args == ' ') args++;
    }
    if (strcmp(line, "wifi") == 0) {
        handle_wifi(args);
    } else if (strcmp(line, "info") == 0) {
        handle_info();
    } else if (strcmp(line, "reboot") == 0) {
        reply("WTCFG OK rebooting");
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_restart();
    } else {
        reply("WTCFG ERR unknown command (info | wifi <ssid> <pass> | reboot)");
    }
}

static void console_task(void *arg)
{
    static char line[176];
    int pos = 0;
    for (;;) {
        uint8_t ch;
        if (usb_serial_jtag_read_bytes(&ch, 1, portMAX_DELAY) != 1) {
            continue;
        }
        if (ch == '\n' || ch == '\r') {
            if (pos > 0) {
                line[pos] = '\0';
                pos = 0;
                handle_line(line);
            }
        } else if (pos < (int)sizeof(line) - 1) {
            line[pos++] = (char)ch;
        } else {
            pos = 0;  /* oversized junk: drop the line */
        }
    }
}

void console_start(const char *build_tag)
{
    s_build_tag = build_tag;
    usb_serial_jtag_driver_config_t cfg = {
        .rx_buffer_size = 256,
        .tx_buffer_size = 512,
    };
    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "usb_serial_jtag driver install failed: %d", err);
        return;
    }
    xTaskCreatePinnedToCore(console_task, "console", 4 * 1024, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "USB config console up (info | wifi <ssid> <pass> | reboot)");
}
