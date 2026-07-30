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
 * @brief Close the flash area and unregister the disk.
 */
void uf2_disk_close(void);

/**
 * @brief Initialize USB next-gen stack with MSC for UF2.
 *
 * Creates the USB device context, adds descriptors and MSC class,
 * and enables the USB device. Must be called after uf2_disk_register().
 *
 * @return 0 on success, negative errno on failure
 */
int uf2_usb_init(void);

#ifdef __cplusplus
}
#endif

#endif /* H_UF2_DISK_ */
