/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef H_UF2_DISK_
#define H_UF2_DISK_

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief Register the UF2 virtual disk with the disk_access subsystem.
 *
 * Opens the target flash area (primary or secondary slot based on
 * CONFIG_SINGLE_APPLICATION_SLOT), initializes the UF2 processing
 * state, and registers the virtual disk.
 *
 * @return 0 on success, negative errno on failure
 */
int uf2_disk_register(void);

/**
 * @brief Check if UF2 transfer is complete.
 *
 * @return true if all UF2 blocks have been received
 */
bool uf2_disk_is_complete(void);

/**
 * @brief Check if the UF2 virtual disk is currently registered.
 *
 * The USB mass storage setup requires the disk to be registered before
 * the USB device is initialized, so this lets the USB layer verify the
 * registration order.
 *
 * @return true if uf2_disk_register() succeeded and the disk has not
 *         been closed since
 */
bool uf2_disk_is_registered(void);

/**
 * @brief Check if any UF2 data was written to the primary (application)
 *        slot.
 *
 * The completion of a UF2 transfer targeting a non-application region
 * (e.g. the storage/filesystem partition) must not trigger application
 * update bookkeeping (image confirmation / swap requests); callers can
 * use this to distinguish "firmware was updated" from "only another
 * partition was written".
 *
 * @return true if at least one UF2 block was written to the primary slot
 */
bool uf2_disk_primary_written(void);

/**
 * @brief Close the flash area and unregister the disk.
 */
void uf2_disk_close(void);

#ifdef __cplusplus
}
#endif

#endif /* H_UF2_DISK_ */
