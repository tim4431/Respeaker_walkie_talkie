#pragma once

#include <stdint.h>

/*
 * Wire format v2: one UDP datagram carries up to WT_MAX_BATCH consecutive
 * 20 ms Opus frames, each prefixed with a little-endian u16 length:
 *
 *   header | len0 frame0 | len1 frame1 | ...
 *
 * hdr.seq is the sequence number of the FIRST frame; the rest are
 * consecutive. Batching (default 3 frames = 60 ms per datagram) keeps the
 * packet rate low - consumer routers' UDP flood protection can blacklist
 * a 50 pps single-frame stream. Header-only datagrams are keepalives.
 */

#define WT_MAGIC         0x314B5457u  /* "WTK1" */
#define WT_PROTO_VERSION 2
#define WT_MAX_BATCH     4

#define WT_FLAG_AUDIO    0x01
#define WT_FLAG_CTRL     0x02

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t seq;
    uint8_t  flags;
    uint8_t  version;
} wt_header_t;

/*
 * Control payloads (WT_FLAG_CTRL): first byte is the command.
 * Control packets never influence peer adoption, so monitoring tools can
 * poll freely without hijacking the audio link.
 */
#define WT_CTRL_STATUS_REQ 0x01  /* no body; reply goes to the requester */
#define WT_CTRL_STATUS_RSP 0x02  /* body: wt_status_t */
#define WT_CTRL_SET_PEER   0x03  /* body: wt_set_peer_t; ip 0 = back to auto */
#define WT_CTRL_SET_VOL    0x04  /* body: wt_set_vol_t; reply: wt_status_t */

typedef struct __attribute__((packed)) {
    uint8_t  cmd;        /* WT_CTRL_STATUS_RSP */
    uint8_t  proto;      /* WT_PROTO_VERSION */
    uint8_t  muted;
    uint8_t  linked;     /* heard the peer within the last 3 s */
    uint8_t  peer_locked;/* manual SET_PEER in effect */
    int8_t   rssi;
    uint16_t peer_port;  /* network order; 0 if no peer */
    uint32_t peer_ip;    /* network order; 0 if no peer */
    char     hostname[24];
    uint8_t  volume;     /* speaker volume, 0-100 (appended in proto v2;
                            parsers must treat it as optional) */
} wt_status_t;

typedef struct __attribute__((packed)) {
    uint8_t  cmd;        /* WT_CTRL_SET_PEER */
    uint8_t  reserved;
    uint16_t port;       /* network order */
    uint32_t ip;         /* network order; 0 clears the lock (auto mode) */
} wt_set_peer_t;

typedef struct __attribute__((packed)) {
    uint8_t  cmd;        /* WT_CTRL_SET_VOL */
    uint8_t  volume;     /* speaker volume percent, clamped to 0-100 */
} wt_set_vol_t;
