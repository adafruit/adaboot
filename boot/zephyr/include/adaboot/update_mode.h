/*
 * Copyright (c) 2026 Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Requesting the installed bootloader's update mode (the state a double-tap
 * of the reset button produces) for the next reset, from an application.
 *
 * The request mechanism depends on the bootloader actually installed on the
 * board, not on whether this fork is the bootloader:
 *
 * - Boards running this fork's MCUboot bootloader (Adaboot) read the
 *   boot-mode retention flag on boot (io.c's io_detect_boot_mode()), so the
 *   request is a bootmode_set(BOOT_MODE_TYPE_BOOTLOADER) write. The
 *   application needs the retained-memory chain (RETAINED_MEM -> RETENTION
 *   -> RETENTION_BOOT_MODE) enabled for the flag to exist.
 * - Boards running the stock Adafruit nRF52 bootloader read the raw GPREGRET
 *   register after any reset, so the request is a GPREGRET magic value.
 * - Boards with neither backend (no boot-mode retention region and no
 *   Adafruit bootloader) compile these helpers to no-ops: the next reset
 *   simply boots normally.
 */

#ifndef ZEPHYR_ADABOOT_UPDATE_MODE_H_
#define ZEPHYR_ADABOOT_UPDATE_MODE_H_

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Ask the installed bootloader to enter its update mode (UF2 drive and/or
 * serial recovery) on the next reset. uf2 selects the UF2 flavor of the
 * request on bootloaders that distinguish one; both flavors end up in the
 * same update mode.
 */
void adaboot_request_update_mode(bool uf2);

/*
 * Drop a request set by a previous adaboot_request_update_mode() call, so the
 * next reset boots normally.
 */
void adaboot_clear_update_request(void);

/*
 * Registers the SMP bootloader-info hook (ports/zephyr-cp/supervisor/
 * adaboot_info.c) so the application reports the fork's bootloader name and
 * the shared boot state. Callers must invoke this from an always-linked
 * translation unit: the hook file is an archive member that nothing else
 * references, so a SYS_INIT inside it would never be pulled in by the
 * linker.
 */
void adaboot_info_init(void);

#ifdef __cplusplus
}
#endif

#endif /* ZEPHYR_ADABOOT_UPDATE_MODE_H_ */
