/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Zephyr disk_access backend for UF2 drag-and-drop updates.
 * Implements struct disk_operations to present a virtual FAT16
 * filesystem over USB Mass Storage.
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/disk.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>

#include "bootutil/bootutil_log.h"
#include "boot_uf2/boot_uf2.h"
#include "uf2/uf2_disk.h"
#include "sysflash/sysflash.h"
#include "flash_map_backend/flash_map_backend.h"

BOOT_LOG_MODULE_REGISTER(uf2_disk);

/* Block tracking bitmap */
static uint8_t block_map_storage[(CONFIG_MCUBOOT_UF2_MAX_BLOCKS + 7) / 8];

/* UF2 state - uses flexible array member, so we overlay on block_map_storage */
static struct {
	struct uf2_state state;
	uint8_t block_map[sizeof(block_map_storage)];
} uf2_state_buf;

static struct uf2_cfg uf2_cfg;
static const struct flash_area *target_fap;
static bool disk_registered;

/* Write-block size of the target flash device. UF2 payloads are 256 bytes,
 * but the last block of an image can be a shorter partial. Some flash drivers
 * (e.g. Nordic RRAM) require both the write offset and length to be a
 * multiple of the device's write-block size, so we round a partial payload up
 * to that alignment. The UF2 block's data field is zero-padded past
 * payload_size, so the extra bytes read as 0 and land in the image's trailing
 * padding inside the slot (harmless: they are overwritten when a real app
 * is later loaded). 1 means "any length/offset accepted" (the no-op case).
 */
static size_t uf2_write_block_size = 1;

/* Flash callbacks for write/erase (target slot via flash_area) */
static int uf2_flash_write(uint32_t offset, const void *data, uint32_t len,
			   void *ctx)
{
	const struct flash_area *fap = ctx;
	uint32_t aligned_len = len;

	if (uf2_write_block_size > 1) {
		uint32_t mask = uf2_write_block_size - 1;

		aligned_len = (aligned_len + mask) & ~mask;
		/* Never write past the end of the flash area. */
		if (offset + aligned_len > fap->fa_size) {
			aligned_len = len;
		}
	}

	return flash_area_write(fap, offset, data, aligned_len);
}

static int uf2_flash_erase(uint32_t offset, uint32_t len, void *ctx)
{
	const struct flash_area *fap = ctx;

	return flash_area_erase(fap, offset, len);
}

/* Flash read callback for CURRENT.UF2 (reads from flash device) */
static int uf2_flash_read(uint32_t offset, void *data, uint32_t len,
			  void *ctx)
{
	const struct device *flash_dev = ctx;

	return flash_read(flash_dev, offset, data, len);
}

/* Disk operations */
static int uf2_disk_access_init(struct disk_info *disk)
{
	BOOT_LOG_DBG("uf2_disk: access_init");
	return 0;
}

static int uf2_disk_access_status(struct disk_info *disk)
{
	return DISK_STATUS_OK;
}

static int uf2_disk_access_read(struct disk_info *disk, uint8_t *buf,
				uint32_t sector, uint32_t count)
{
	BOOT_LOG_DBG("uf2_disk: read sector=%u count=%u", sector, count);
	for (uint32_t i = 0; i < count; i++) {
		ghostfat_read_sector(&uf2_cfg, sector + i,
				     buf + i * UF2_BLOCK_SIZE);
	}
	return 0;
}

static int uf2_disk_access_write(struct disk_info *disk, const uint8_t *buf,
				 uint32_t sector, uint32_t count)
{
	BOOT_LOG_DBG("uf2_disk: write sector=%u count=%u", sector, count);
	for (uint32_t i = 0; i < count; i++) {
		int rc = ghostfat_write_sector(&uf2_cfg, &uf2_state_buf.state,
					       sector + i,
					       buf + i * UF2_BLOCK_SIZE,
					       CONFIG_MCUBOOT_UF2_MAX_BLOCKS);
		if (rc != 0) {
			BOOT_LOG_ERR("UF2 write error at sector %u: %d",
				     sector + i, rc);
			return rc;
		}
	}
	return 0;
}

static int uf2_disk_access_erase(struct disk_info *disk, uint32_t start_sector,
				 uint32_t num_sector)
{
	/* No-op: ghost FAT has no persistent storage to erase */
	return 0;
}

static int uf2_disk_access_ioctl(struct disk_info *disk, uint8_t cmd,
				 void *buf)
{
	switch (cmd) {
	case DISK_IOCTL_GET_SECTOR_COUNT:
		*(uint32_t *)buf = ghostfat_get_sector_count();
		return 0;
	case DISK_IOCTL_GET_SECTOR_SIZE:
		*(uint32_t *)buf = UF2_BLOCK_SIZE;
		return 0;
	case DISK_IOCTL_GET_ERASE_BLOCK_SZ:
		*(uint32_t *)buf = 1;
		return 0;
	case DISK_IOCTL_CTRL_SYNC:
		return 0;
	case DISK_IOCTL_CTRL_INIT:
		return 0;
	case DISK_IOCTL_CTRL_DEINIT:
		return 0;
	default:
		return -EINVAL;
	}
}

static const struct disk_operations uf2_disk_ops = {
	.init = uf2_disk_access_init,
	.status = uf2_disk_access_status,
	.read = uf2_disk_access_read,
	.write = uf2_disk_access_write,
	.erase = uf2_disk_access_erase,
	.ioctl = uf2_disk_access_ioctl,
};

static struct disk_info uf2_disk_info = {
	.name = CONFIG_MCUBOOT_UF2_DISK_NAME,
	.ops = &uf2_disk_ops,
};

int uf2_disk_register(void)
{
	int rc;
	int area_id;

	BOOT_LOG_DBG("uf2_disk: register start");

#ifdef CONFIG_SINGLE_APPLICATION_SLOT
	/* Single slot: write directly to primary */
	area_id = FLASH_AREA_IMAGE_PRIMARY(0);
#else
	/* Dual slot: write to secondary, swap later */
	area_id = FLASH_AREA_IMAGE_SECONDARY(0);
#endif

	rc = flash_area_open(area_id, &target_fap);
	if (rc != 0) {
		BOOT_LOG_ERR("Failed to open flash area %d: %d", area_id, rc);
		return rc;
	}

	/* Configure UF2 processing */
	uf2_cfg.write = uf2_flash_write;
	uf2_cfg.erase = uf2_flash_erase;
	uf2_cfg.cb_ctx = (void *)target_fap;
	uf2_cfg.flash_base = target_fap->fa_off;
	uf2_cfg.flash_size = target_fap->fa_size;
	uf2_cfg.family_id = CONFIG_MCUBOOT_UF2_FAMILY_ID;
	uf2_cfg.board_id = CONFIG_MCUBOOT_UF2_BOARD_ID;

	/* Use the flash device's erase block size.
	 * Default to 4096 if we can't determine it.
	 */
	const struct device *flash_dev = flash_area_get_device(target_fap);

	if (flash_dev != NULL) {
		const struct flash_parameters *params =
			flash_get_parameters(flash_dev);
		if (params != NULL) {
			if (params->erase_value != 0xFF) {
				/* Some devices might not have standard erase */
			}
			/* UF2 payloads are 256 bytes but the last block of an
			 * image can be a shorter partial; round writes up to the
			 * device's write-block size so drivers that require
			 * aligned writes (e.g. Nordic RRAM, which rejects a
			 * 168-byte trailing payload with -EINVAL) accept it.
			 * The UF2 block's data field is zero-padded past
			 * payload_size, so the extra bytes land in the image's
			 * trailing padding inside the slot.
			 */
			uf2_write_block_size = params->write_block_size;
		}
	}
	/* Use a safe default erase size */
	uf2_cfg.erase_size = 4096;

	uf2_cfg.board_name = CONFIG_MCUBOOT_UF2_BOARD_NAME;
	uf2_cfg.board_url = CONFIG_MCUBOOT_UF2_BOARD_URL;

	/* CURRENT.UF2 readback: read from the flash device covering
	 * offset 0 through the end of the primary slot. This captures
	 * the bootloader and the current application image.
	 */
	if (flash_dev != NULL) {
		const struct flash_area *primary_fap;

		rc = flash_area_open(FLASH_AREA_IMAGE_PRIMARY(0),
				     &primary_fap);
		if (rc == 0) {
			uintptr_t flash_base_addr;

			flash_device_base(flash_area_get_device_id(primary_fap),
					  &flash_base_addr);

			uf2_cfg.read = uf2_flash_read;
			uf2_cfg.read_ctx = (void *)flash_dev;
			uf2_cfg.readback_base = (uint32_t)flash_base_addr;
			uf2_cfg.readback_size = primary_fap->fa_off +
						primary_fap->fa_size;

			flash_area_close(primary_fap);

			BOOT_LOG_INF("CURRENT.UF2 readback: base 0x%x, "
				     "size 0x%x",
				     (unsigned int)uf2_cfg.readback_base,
				     (unsigned int)uf2_cfg.readback_size);
		}
	}

	/* Initialize UF2 state */
	uf2_init(&uf2_state_buf.state, CONFIG_MCUBOOT_UF2_MAX_BLOCKS);

	/* Register the virtual disk */
	rc = disk_access_register(&uf2_disk_info);
	if (rc != 0) {
		BOOT_LOG_ERR("Failed to register UF2 disk: %d", rc);
		flash_area_close(target_fap);
		return rc;
	}

	disk_registered = true;

	BOOT_LOG_INF("UF2 disk registered, target flash area %d "
		     "(offset 0x%x, size 0x%x)",
		     area_id,
		     (unsigned int)target_fap->fa_off,
		     (unsigned int)target_fap->fa_size);

	return 0;
}

bool uf2_disk_is_complete(void)
{
	return uf2_is_complete(&uf2_state_buf.state);
}

bool uf2_disk_is_registered(void)
{
	return disk_registered;
}

void uf2_disk_close(void)
{
	disk_access_unregister(&uf2_disk_info);
	disk_registered = false;

	if (target_fap != NULL) {
		flash_area_close(target_fap);
		target_fap = NULL;
	}
}
