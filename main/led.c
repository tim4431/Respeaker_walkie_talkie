#include "led.h"
#include "app_config.h"
#include "led_strip.h"

static led_strip_handle_t s_strip;
static led_state_t s_state = LED_STATE_WIFI_CONNECTING;

esp_err_t led_init(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = WT_PIN_LED,
        .max_leds = 1,
        .led_pixel_format = LED_PIXEL_FORMAT_GRB,
        .led_model = LED_MODEL_WS2812,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .resolution_hz = 10 * 1000 * 1000,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip);
    if (err == ESP_OK) {
        led_set_state(LED_STATE_WIFI_CONNECTING);
    }
    return err;
}

void led_set_state(led_state_t state)
{
    if (s_strip == NULL) {
        return;
    }
    s_state = state;
    /* Kept dim on purpose; the WS2812 is bright. */
    switch (s_state) {
    case LED_STATE_WIFI_CONNECTING: led_strip_set_pixel(s_strip, 0, 25, 10, 0);  break;
    case LED_STATE_NO_PEER:         led_strip_set_pixel(s_strip, 0, 0, 0, 25);   break;
    case LED_STATE_LINKED:          led_strip_set_pixel(s_strip, 0, 0, 25, 0);   break;
    case LED_STATE_MUTED:           led_strip_set_pixel(s_strip, 0, 20, 0, 20);  break;
    }
    led_strip_refresh(s_strip);
}
