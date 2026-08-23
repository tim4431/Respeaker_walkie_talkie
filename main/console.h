#pragma once

/* USB-serial config console: lets a PC configure the device over the XIAO's
 * USB port (line-based commands, replies prefixed "WTCFG"). */
void console_start(const char *build_tag);
