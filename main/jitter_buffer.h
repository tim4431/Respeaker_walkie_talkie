#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "app_config.h"

/* Sequence-indexed buffer of *encoded* Opus packets. The receive task
 * inserts; the playback task pops in sequence order and decodes, so
 * FEC/PLC fall out naturally for missing entries. */

void     jb_init(void);
void     jb_reset(void);
void     jb_insert(uint16_t seq, const uint8_t *data, uint16_t len);
int      jb_depth(void);
uint16_t jb_head_seq(void);   /* newest seq inserted (valid only if depth > 0) */

/* Copy out the packet for `seq` and free its slot. False if absent. */
bool jb_take(uint16_t seq, uint8_t *out, uint16_t *len);

/* Copy out the packet for `seq` without freeing it (for FEC lookahead). */
bool jb_peek(uint16_t seq, uint8_t *out, uint16_t *len);
