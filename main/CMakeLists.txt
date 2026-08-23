idf_component_register(
    SRCS
        "main.c"
        "audio_io.c"
        "codec.c"
        "jitter_buffer.c"
        "net.c"
        "led.c"
        "console.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_wifi nvs_flash esp_event esp_netif esp_timer lwip
)
