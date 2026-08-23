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
#define WT_CTRL_SET_MODE   0x05  /* body: u8 tx mode (see WT_TXMODE_*) */
#define WT_CTRL_SET_MUTE   0x06  /* body: u8 0/1 (not persisted) */
#define WT_CTRL_SET_GROUP  0x07  /* body: u8 count + count * wt_member_t;
                                    count 0 leaves the group. Persisted. */
#define WT_CTRL_SET_SENS   0x08  /* body: u8 mic sensitivity 0-100 (VOX TX
                                    threshold; higher = opens easier).
                                    Persisted. */
#define WT_CTRL_SET_GAIN   0x09  /* body: u8 mic gain percent 0-200 (digital,
                                    saturating; scales the captured signal
                                    before level/VOX/encode). Persisted. */

#define WT_TXMODE_ALWAYS 0  /* transmit continuously */
#define WT_TXMODE_VOX    1  /* transmit only on local speech while the far
                               end is quiet (half-duplex, echo-proof) */

#define WT_MAX_MEMBERS   8

typedef struct __attribute__((packed)) {
    uint32_t ip;         /* network order */
    uint16_t port;       /* network order */
} wt_member_t;

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
    /* Fields below are appended extensions within proto v2: parsers must
     * treat them as optional (check the payload length). */
    uint8_t  volume;     /* speaker volume, 0-100 */
    uint8_t  tx_mode;    /* WT_TXMODE_* */
    uint8_t  tx_active;  /* transmitting audio right now */
    uint8_t  rx_active;  /* playing far-end audio right now */
    uint8_t  member_count;             /* group members (not counting self) */
    wt_member_t members[WT_MAX_MEMBERS];
    uint8_t  mic_level;  /* latest 20 ms mic frame peak, peak16 >> 7 */
    uint8_t  vox_sens;   /* mic sensitivity 0-100 (see WT_CTRL_SET_SENS) */
    uint8_t  mic_gain;   /* mic gain percent 0-200 (see WT_CTRL_SET_GAIN) */
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
