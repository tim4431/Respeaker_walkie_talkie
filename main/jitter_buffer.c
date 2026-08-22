#include <string.h>
#include "jitter_buffer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

typedef struct {
    bool     valid;
    uint16_t seq;
    uint16_t len;
    uint8_t  data[WT_MAX_PAYLOAD];
} jb_slot_t;

static jb_slot_t s_slots[WT_JB_SLOTS];
static int s_depth;
static uint16_t s_head_seq;
static SemaphoreHandle_t s_lock;

void jb_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    jb_reset();
}

void jb_reset(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    memset(s_slots, 0, sizeof(s_slots));
    s_depth = 0;
    xSemaphoreGive(s_lock);
}

void jb_insert(uint16_t seq, const uint8_t *data, uint16_t len)
{
    if (len > WT_MAX_PAYLOAD) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);

    /* A large jump (reboot of the peer, long mute) invalidates everything. */
    int16_t jump = (int16_t)(seq - s_head_seq);
    if (s_depth > 0 && (jump > WT_JB_SLOTS || jump < -WT_JB_SLOTS)) {
        memset(s_slots, 0, sizeof(s_slots));
        s_depth = 0;
    }

    jb_slot_t *slot = &s_slots[seq % WT_JB_SLOTS];
    if (!slot->valid) {
        s_depth++;
    }
    slot->valid = true;
    slot->seq = seq;
    slot->len = len;
    memcpy(slot->data, data, len);

    if (s_depth == 1 || (int16_t)(seq - s_head_seq) > 0) {
        s_head_seq = seq;
    }
    xSemaphoreGive(s_lock);
}

int jb_depth(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    int d = s_depth;
    xSemaphoreGive(s_lock);
    return d;
}

uint16_t jb_head_seq(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    uint16_t s = s_head_seq;
    xSemaphoreGive(s_lock);
    return s;
}

static bool fetch(uint16_t seq, uint8_t *out, uint16_t *len, bool remove)
{
    bool hit = false;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    jb_slot_t *slot = &s_slots[seq % WT_JB_SLOTS];
    if (slot->valid && slot->seq == seq) {
        memcpy(out, slot->data, slot->len);
        *len = slot->len;
        if (remove) {
            slot->valid = false;
            s_depth--;
        }
        hit = true;
    }
    xSemaphoreGive(s_lock);
    return hit;
}

bool jb_take(uint16_t seq, uint8_t *out, uint16_t *len)
{
    return fetch(seq, out, len, true);
}

bool jb_peek(uint16_t seq, uint8_t *out, uint16_t *len)
{
    return fetch(seq, out, len, false);
}
