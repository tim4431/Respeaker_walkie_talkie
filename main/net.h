#pragma once

#include <stdbool.h>
#include "esp_err.h"
#include "lwip/sockets.h"

/* Connect to WiFi (blocks until associated) and disable modem power save. */
esp_err_t net_wifi_start(void);

/* Bind the UDP socket and advertise _walkie._udp via mDNS. */
esp_err_t net_udp_start(void);

int  net_socket(void);
bool net_peer_known(void);

/* Send to the current peer; silently drops if no peer yet. */
void net_send(const void *buf, size_t len);

/* Called by the receive path for every valid datagram: adopts/refreshes
 * the peer address and the liveness timestamp. */
void net_note_rx_from(const struct sockaddr_in *src);

/* Housekeeping: run an mDNS query if no peer is known (rate-limited by
 * the caller), and expire a peer that has gone silent. */
void net_discovery_poll(void);
void net_check_peer_timeout(void);

/* Manual peer control (WT_CTRL_SET_PEER). ip/port in network order;
 * ip == 0 clears the lock and returns to auto discovery/adoption. */
void net_set_peer_manual(uint32_t ip, uint16_t port);

/* Fill a status reply for WT_CTRL_STATUS_REQ. */
struct wt_status;
void net_fill_status(void *status, bool muted);

const char *net_hostname(void);

/* Broadcast a tiny presence packet on the walkie port (AP keepalive +
 * discovery). */
void net_send_presence(const void *buf, size_t len);
