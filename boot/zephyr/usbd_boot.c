// SPDX-License-Identifier: Apache-2.0
/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * Unified USB device setup for the bootloader's update transports.
 *
 * A single USBD device context serves every USB-based update transport
 * enabled in the bootloader configuration:
 *
 *   - UF2 drag-and-drop over USB Mass Storage (CONFIG_MCUBOOT_UF2)
 *   - SMP serial recovery over CDC ACM (CONFIG_BOOT_SERIAL_CDC_ACM)
 *
 * This way a bootloader with native USB presents the UF2 drive and the
 * serial recovery port at the same time, in a single bootloader mode,
 * instead of two mutually exclusive modes with two different USB devices.
 */

#include <zephyr/kernel.h>
#include <zephyr/usb/usbd.h>
#include <zephyr/usb/usbd_msg.h>

#ifdef CONFIG_MCUBOOT_UF2
#include <zephyr/usb/class/usbd_msc.h>
#include "uf2/uf2_disk.h"
#endif

#include "bootutil/bootutil_log.h"
#ifdef CONFIG_MCUBOOT_INDICATION_LED
#include "io/io.h"
#endif
#include "usbd_boot.h"

BOOT_LOG_MODULE_REGISTER(usbd_boot);

/* USB identity of the bootloader device. When UF2 is enabled, its VID/PID
 * and descriptors identify the bootloader (the same identity the UF2 drive
 * has always used, also when serial recovery is combined onto it); the
 * serial recovery identity is only used by a serial-recovery-only
 * bootloader.
 */
#if defined(CONFIG_MCUBOOT_UF2)
#define BOOT_USB_VID			CONFIG_MCUBOOT_UF2_USB_VID
#define BOOT_USB_PID			CONFIG_MCUBOOT_UF2_USB_PID
#define BOOT_USB_MANUFACTURER_STRING	"Adafruit"
#define BOOT_USB_PRODUCT_STRING		CONFIG_MCUBOOT_UF2_BOARD_NAME
#define BOOT_USB_ATTRIBUTES		USB_SCD_SELF_POWERED
#define BOOT_USB_MAX_POWER		250
#else
#define BOOT_USB_VID			CONFIG_BOOT_SERIAL_CDC_ACM_VID
#define BOOT_USB_PID			CONFIG_BOOT_SERIAL_CDC_ACM_PID
#define BOOT_USB_MANUFACTURER_STRING	CONFIG_BOOT_SERIAL_CDC_ACM_MANUFACTURER_STRING
#define BOOT_USB_PRODUCT_STRING		CONFIG_BOOT_SERIAL_CDC_ACM_PRODUCT_STRING
#define BOOT_USB_ATTRIBUTES \
	COND_CODE_1(CONFIG_BOOT_SERIAL_CDC_ACM_SELF_POWERED, (USB_SCD_SELF_POWERED), (0))
#define BOOT_USB_MAX_POWER		CONFIG_BOOT_SERIAL_CDC_ACM_MAX_POWER
#endif

USBD_DEVICE_DEFINE(boot_usbd,
		   DEVICE_DT_GET(DT_NODELABEL(zephyr_udc0)),
		   BOOT_USB_VID,
		   BOOT_USB_PID);

USBD_DESC_LANG_DEFINE(boot_usbd_lang);
USBD_DESC_MANUFACTURER_DEFINE(boot_usbd_mfr, BOOT_USB_MANUFACTURER_STRING);
USBD_DESC_PRODUCT_DEFINE(boot_usbd_product, BOOT_USB_PRODUCT_STRING);
IF_ENABLED(CONFIG_HWINFO, (USBD_DESC_SERIAL_NUMBER_DEFINE(boot_usbd_sn)));

USBD_DESC_CONFIG_DEFINE(boot_usbd_fs_cfg, "FS Configuration");
USBD_CONFIGURATION_DEFINE(boot_usbd_fs_config,
			  BOOT_USB_ATTRIBUTES,
			  BOOT_USB_MAX_POWER,
			  &boot_usbd_fs_cfg);

#if USBD_SUPPORTS_HIGH_SPEED
USBD_DESC_CONFIG_DEFINE(boot_usbd_hs_cfg, "HS Configuration");
USBD_CONFIGURATION_DEFINE(boot_usbd_hs_config,
			  BOOT_USB_ATTRIBUTES,
			  BOOT_USB_MAX_POWER,
			  &boot_usbd_hs_cfg);
#endif

#ifdef CONFIG_MCUBOOT_UF2
/* LUN of the UF2 boot drive. The disk itself is the virtual ghost-FAT
 * disk_access backend implemented in uf2_disk.c. */
USBD_DEFINE_MSC_LUN(uf2, CONFIG_MCUBOOT_UF2_DISK_NAME,
		    "Adafruit", "UF2 Bootloader", "1.0");
#endif

/* Given when the USB host opens (asserts DTR on) the CDC ACM serial
 * recovery port. */
K_SEM_DEFINE(boot_cdc_acm_ready, 0, 1);

/* Set once the USB device is brought up, so boot_usb_enable() is
 * idempotent and boot_usb_disable() knows whether there is anything to
 * disable. */
static bool usb_initialized;

static void boot_usbd_msg_cb(struct usbd_context *const ctx,
			     const struct usbd_msg *const msg)
{
	ARG_UNUSED(ctx);

	if (IS_ENABLED(CONFIG_BOOT_SERIAL_CDC_ACM) &&
	    msg->type == USBD_MSG_CDC_ACM_CONTROL_LINE_STATE) {
		k_sem_give(&boot_cdc_acm_ready);
	}

#ifdef CONFIG_MCUBOOT_INDICATION_LED
	/* Mirror tinyuf2's mount/unmount indication: once the host sets a
	 * configuration (the UF2 drive is mounted and ready) breathe slowly;
	 * when the device is unconfigured (msg->status == 0) or the bus
	 * suspends (cable unplugged / host asleep) go back to the fast
	 * "waiting for firmware" fade. A flash write (uf2_disk.c) always
	 * overrides with the very fast write blink. */
	if (msg->type == USBD_MSG_CONFIGURATION) {
		io_led_blink(msg->status > 0 ? IO_LED_BLINK_IDLE_CYCLE_MS
					     : IO_LED_BLINK_WAIT_CYCLE_MS);
	} else if (msg->type == USBD_MSG_SUSPEND) {
		io_led_blink(IO_LED_BLINK_WAIT_CYCLE_MS);
	}
#endif
}

/* Register every enabled update class into the USB configuration for
 * @p speed. All transports live on one composite device, so the UF2
 * mass storage drive and the serial recovery CDC ACM port are exposed
 * simultaneously. */
static int boot_usbd_register_classes(const enum usbd_speed speed)
{
	int err;

#ifdef CONFIG_MCUBOOT_UF2
	if (!uf2_disk_is_registered()) {
		BOOT_LOG_ERR("UF2 disk not registered before USB setup");
		return -EINVAL;
	}

	err = usbd_register_class(&boot_usbd, "msc_0", speed, 1);
	if (err) {
		BOOT_LOG_ERR("Failed to register MSC class: %d", err);
		return err;
	}
#endif

#ifdef CONFIG_BOOT_SERIAL_CDC_ACM
	err = usbd_register_class(&boot_usbd, "cdc_acm_0", speed, 1);
	if (err) {
		BOOT_LOG_ERR("Failed to register CDC ACM class: %d", err);
		return err;
	}

	/* CDC ACM is an IAD-based function; describe the (composite)
	 * device as miscellaneous so hosts bind the right drivers. */
	err = usbd_device_set_code_triple(&boot_usbd, speed,
					  USB_BCC_MISCELLANEOUS, 0x02, 0x01);
	if (err) {
		BOOT_LOG_ERR("Failed to set code triple: %d", err);
		return err;
	}
#endif

	return 0;
}

static int boot_usbd_add_configuration(const enum usbd_speed speed)
{
	struct usbd_config_node *cfg_nd;
	int err;

#if USBD_SUPPORTS_HIGH_SPEED
	if (speed == USBD_SPEED_HS) {
		cfg_nd = &boot_usbd_hs_config;
	} else
#endif
	{
		cfg_nd = &boot_usbd_fs_config;
	}

	err = usbd_add_configuration(&boot_usbd, speed, cfg_nd);
	if (err) {
		BOOT_LOG_ERR("Failed to add USB configuration: %d", err);
		return err;
	}

	return boot_usbd_register_classes(speed);
}

int boot_usb_enable(void)
{
	int err;

	/* Idempotent: whichever update transport is entered first brings up
	 * the USB device (with all enabled transport classes on it); later
	 * callers just get the already-initialized device. */
	if (usb_initialized) {
		return 0;
	}

	err = usbd_add_descriptor(&boot_usbd, &boot_usbd_lang);
	if (err) {
		BOOT_LOG_ERR("Failed to add language descriptor: %d", err);
		return err;
	}

	err = usbd_add_descriptor(&boot_usbd, &boot_usbd_mfr);
	if (err) {
		BOOT_LOG_ERR("Failed to add manufacturer descriptor: %d", err);
		return err;
	}

	err = usbd_add_descriptor(&boot_usbd, &boot_usbd_product);
	if (err) {
		BOOT_LOG_ERR("Failed to add product descriptor: %d", err);
		return err;
	}

	IF_ENABLED(CONFIG_HWINFO, (
		err = usbd_add_descriptor(&boot_usbd, &boot_usbd_sn);
		if (err) {
			BOOT_LOG_ERR("Failed to add serial number descriptor: %d",
				     err);
			return err;
		}
	))

	if (USBD_SUPPORTS_HIGH_SPEED &&
	    usbd_caps_speed(&boot_usbd) == USBD_SPEED_HS) {
		err = boot_usbd_add_configuration(USBD_SPEED_HS);
		if (err) {
			return err;
		}
	}

	err = boot_usbd_add_configuration(USBD_SPEED_FS);
	if (err) {
		return err;
	}

	usbd_self_powered(&boot_usbd,
			  (BOOT_USB_ATTRIBUTES & USB_SCD_SELF_POWERED) != 0);

	err = usbd_msg_register_cb(&boot_usbd, boot_usbd_msg_cb);
	if (err) {
		BOOT_LOG_ERR("Failed to register message callback: %d", err);
		return err;
	}

	err = usbd_init(&boot_usbd);
	if (err) {
		BOOT_LOG_ERR("Failed to init USB device: %d", err);
		return err;
	}

	err = usbd_enable(&boot_usbd);
	if (err) {
		BOOT_LOG_ERR("Failed to enable USB device: %d", err);
		return err;
	}

	usb_initialized = true;

	BOOT_LOG_INF("USB device initialized (VID=0x%04x PID=0x%04x)",
		     BOOT_USB_VID, BOOT_USB_PID);

	return 0;
}

int boot_usb_disable(void)
{
	if (!usb_initialized) {
		return -EALREADY;
	}

	usb_initialized = false;

	return usbd_disable(&boot_usbd);
}
