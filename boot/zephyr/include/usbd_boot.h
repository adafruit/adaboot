/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 */

#ifndef BOOT_USBD_BOOT_H_
#define BOOT_USBD_BOOT_H_

#include <stdbool.h>

#include <zephyr/kernel.h>

struct usbd_context;

/**
 * @brief Bring up the bootloader USB device, once, with all enabled
 *        update transport classes on it.
 *
 * A single USB device context serves every USB-based update transport
 * enabled in the bootloader configuration: the UF2 mass storage drive
 * (CONFIG_MCUBOOT_UF2) and/or the CDC ACM serial recovery port
 * (CONFIG_BOOT_SERIAL_CDC_ACM). This makes the bootloader a single
 * composite device instead of two mutually exclusive modes with two
 * different USB devices.
 *
 * The function is idempotent: whichever transport is entered first brings
 * up the USB device, later callers get the already-initialized device.
 *
 * When UF2 is enabled, the UF2 disk (uf2_disk_register()) must be
 * registered before this is called, so that the mass storage class can
 * find its LUN.
 *
 * @return 0 on success, negative errno on failure
 */
int boot_usb_enable(void);

/**
 * @brief Disable the bootloader USB device.
 *
 * Called before chain-loading the application so that USB does not fire
 * interrupts into it. Returns -EALREADY if the USB device was never
 * enabled (normal boot path).
 *
 * @return 0 on success, -EALREADY if not enabled, negative errno on failure
 */
int boot_usb_disable(void);

/**
 * @brief Signalled when the USB host opens the CDC ACM serial recovery
 *        port (DTR asserted).
 *
 * Only meaningful when CONFIG_BOOT_SERIAL_CDC_ACM is enabled.
 */
extern struct k_sem boot_cdc_acm_ready;

#endif /* BOOT_USBD_BOOT_H_ */
