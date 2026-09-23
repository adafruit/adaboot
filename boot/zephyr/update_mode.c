/*
 * Copyright (c) 2026 Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Application-side helpers to request the bootloader's update mode for the
 * next reset. See include/adaboot/update_mode.h for the API and the
 * per-bootloader mechanism.
 */

#include <stdbool.h>

#include "adaboot/update_mode.h"

#if defined(CONFIG_RETENTION_BOOT_MODE)
#include <zephyr/retention/bootmode.h>
#endif

#if defined(CONFIG_SOC_NRF52840)
#include <hal/nrf_power.h>
#endif

/* Magic values that the stock Adafruit nRF52 bootloader reads from the
 * GPREGRET retention register after any reset. 0x57 (DFU_MAGIC_UF2_RESET) is
 * the value a double-tap of the reset button ends up producing: the bootloader
 * stays in its UF2 update mode. 0x4e (DFU_MAGIC_SERIAL_ONLY) requests the
 * serial DFU update mode instead.
 */
#define ADABOOT_GPREGRET_UF2_MAGIC (0x57)
#define ADABOOT_GPREGRET_SERIAL_MAGIC (0x4e)

void adaboot_request_update_mode(bool uf2) {
    #if defined(CONFIG_RETENTION_BOOT_MODE)
    // The fork's adaboot (MCUboot) reads the boot-mode retention flag:
    // io_detect_boot_mode() compares the retained byte against
    // BOOT_MODE_TYPE_BOOTLOADER and enters update mode when it matches.
    // Don't also write GPREGRET here: the same register backs the flag, and
    // the bootloader's double-tap detection shares it, so a magic value would
    // make the flag check fail.
    bootmode_set(BOOT_MODE_TYPE_BOOTLOADER);
    #elif defined(CONFIG_SOC_NRF52840)
    // Boards that boot the stock Adafruit nRF52 bootloader read raw GPREGRET;
    // no boot-mode retention region is wired in their devicetree.
    nrf_power_gpregret_set(NRF_POWER, 0, uf2 ? ADABOOT_GPREGRET_UF2_MAGIC : ADABOOT_GPREGRET_SERIAL_MAGIC);
    #endif
}

void adaboot_clear_update_request(void) {
    #if defined(CONFIG_RETENTION_BOOT_MODE)
    bootmode_set(BOOT_MODE_TYPE_NORMAL);
    #elif defined(CONFIG_SOC_NRF52840)
    nrf_power_gpregret_set(NRF_POWER, 0, 0);
    #endif
}
