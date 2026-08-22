#pragma once

#include "esp_err.h"

typedef enum {
    LED_STATE_WIFI_CONNECTING,  /* orange */
    LED_STATE_NO_PEER,          /* blue   */
    LED_STATE_LINKED,           /* green  */
    LED_STATE_MUTED,            /* purple */
} led_state_t;

esp_err_t led_init(void);
void led_set_state(led_state_t state);
