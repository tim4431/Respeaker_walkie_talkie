#pragma once

#include <stdint.h>

/*
 * Wire format: one UDP datagram per 20 ms audio frame.
 * Header below (little-endian, both ends are the same firmware),
 * followed by a single Opus packet when WT_FLAG_AUDIO is set.
 * Header-only datagrams are keepalives (sent while muted).
 */

#define WT_MAGIC         0x314B5457u  /* "WTK1" */
#define WT_PROTO_VERSION 1

#define WT_FLAG_AUDIO    0x01

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t seq;
    uint8_t  flags;
    uint8_t  version;
} wt_header_t;
