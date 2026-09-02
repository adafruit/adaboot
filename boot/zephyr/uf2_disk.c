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
#ifdef CONFIG_MCUBOOT_INDICATION_LED
#include "io/io.h"
#endif

BOOT_LOG_MODULE_REGISTER(uf2_disk);

/* Block tracking bitmap */
static uint8_t block_map_storage[(CONFIG_MCUBOOT_UF2_MAX_BLOCKS + 7) / 8];

/* UF2 state - uses flexible array member, so we overlay on block_map_storage */
static struct {
	struct uf2_state state;
	uint8_t block_map[sizeof(block_map_storage)];
} uf2_state_buf;

static struct uf2_cfg uf2_cfg;
static bool disk_registered;

/* Writable flash regions (partitions). Region 0 is always the primary
 * application slot; optional regions (storage/filesystem partition, the
 * MCUboot partition itself) are appended per Kconfig. UF2 blocks are
 * routed by absolute target address to the containing region.
 */
struct uf2_disk_region {
	const struct flash_area *fap;
	size_t write_block_size;
	uint32_t bytes_written;
};

static struct uf2_disk_region disk_regions[UF2_MAX_REGIONS];
static struct uf2_region uf2_regions[UF2_MAX_REGIONS];
static uint8_t num_disk_regions;

static struct uf2_disk_region *uf2_disk_region_of(const struct flash_area *fap)
{
	for (uint8_t i = 0; i < num_disk_regions; i++) {
		if (disk_regions[i].fap == fap) {
			return &disk_regions[i];
		}
	}

	return NULL;
}

/* Flash callbacks for write/erase (per-region flash_area as ctx) */
static int uf2_flash_write(uint32_t offset, const void *data, uint32_t len,
			   void *ctx)
{
	const struct flash_area *fap = ctx;
	struct uf2_disk_region *r = uf2_disk_region_of(fap);
	uint32_t aligned_len = len;
	int rc;

#ifdef CONFIG_MCUBOOT_INDICATION_LED
	/* Flashing in progress: very fast blink, like the
	 * STATE_WRITING_STARTED indicator of Adafruit_nRF52_Bootloader
	 * and tinyuf2. */
	io_led_blink(IO_LED_BLINK_WRITE_CYCLE_MS);
#endif

	/* UF2 payloads are 256 bytes, but the last block of a file can be a
	 * shorter partial. Some flash drivers (e.g. Nordic RRAM) require both
	 * the write offset and length to be a multiple of the device's
	 * write-block size, so we round a partial payload up to that
	 * alignment. The UF2 block's data field is zero-padded past
	 * payload_size, so the extra bytes read as 0 and land in the file's
	 * trailing padding inside the region (harmless: they are overwritten
	 * when real content is later loaded). 1 means "any length/offset
	 * accepted" (the no-op case).
	 */
	if (r != NULL && r->write_block_size > 1) {
		uint32_t mask = r->write_block_size - 1;

		aligned_len = (aligned_len + mask) & ~mask;
		/* Never write past the end of the flash area. */
		if (offset + aligned_len > fap->fa_size) {
			aligned_len = len;
		}
	}

	rc = flash_area_write(fap, offset, data, aligned_len);
	if (rc == 0 && r != NULL) {
		r->bytes_written += len;
	}

	return rc;
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

static void uf2_disk_close_regions(void)
{
	for (uint8_t i = 0; i < num_disk_regions; i++) {
		if (disk_regions[i].fap != NULL) {
			flash_area_close(disk_regions[i].fap);
			disk_regions[i].fap = NULL;
		}
	}
	num_disk_regions = 0;
}

/**
 * Add a flash area to the writable region table. Optional areas may
 * fail to open (e.g. on a different flash device); callers log and move
 * on. Returns 0 or a negative errno.
 */
static int uf2_disk_add_region(int area_id)
{
	const struct flash_area *fap;
	const struct device *flash_dev;
	const struct flash_parameters *params;
	size_t write_block_size = 1;
	uint8_t idx;
	int rc;

	if (num_disk_regions >= UF2_MAX_REGIONS) {
		BOOT_LOG_WRN("UF2: region table full, flash area %d "
			     "not writable", area_id);
		return -ENOSPC;
	}

	rc = flash_area_open(area_id, &fap);
	if (rc != 0) {
		return rc;
	}

	/* Determine the region's flash device write-block size. UF2 payloads
	 * are 256 bytes but the last block of a file can be a shorter
	 * partial; see uf2_flash_write() for why we may need to round up.
	 */
	flash_dev = flash_area_get_device(fap);
	if (flash_dev != NULL) {
		params = flash_get_parameters(flash_dev);
		if (params != NULL) {
			write_block_size = params->write_block_size;
		}
	}

	idx = num_disk_regions++;
	disk_regions[idx].fap = fap;
	disk_regions[idx].write_block_size = write_block_size;
	disk_regions[idx].bytes_written = 0;

	uf2_regions[idx].base = fap->fa_off;
	uf2_regions[idx].size = fap->fa_size;
	uf2_regions[idx].ctx = (void *)fap;

	BOOT_LOG_INF("UF2 writable region %u: flash area %d "
		     "(offset 0x%x, size 0x%x)",
		     idx, area_id,
		     (unsigned int)fap->fa_off,
		     (unsigned int)fap->fa_size);

	return 0;
}

int uf2_disk_register(void)
{
	int rc;

	BOOT_LOG_DBG("uf2_disk: register start");

	/*
	 * Adaboot recovery (UF2 drag-and-drop *and* serial recovery) always writes
	 * the primary slot (slot0) directly -- an overwrite of the running app, no
	 * swap -- matching what serial recovery does (boot_serial.c opens the
	 * primary slot when MCUBOOT_SERIAL_DIRECT_IMAGE_UPLOAD is off). The
	 * bootloader is still built swap-using-offset (slot1 present), so the *app*
	 * can stage its own OTA into slot1 and mcuboot will swap it in on the next
	 * boot; recovery never touches slot1, so it doesn't depend on the external
	 * NOR being initialized in the recovery path.
	 *
	 * Region 0 is therefore the primary slot. The optional regions below
	 * make other partitions writable too; blocks are routed to them by
	 * target address, so dropping e.g. a filesystem image onto the drive
	 * writes the storage partition instead of the application.
	 */
	rc = uf2_disk_add_region(FLASH_AREA_IMAGE_PRIMARY(0));
	if (rc != 0) {
		BOOT_LOG_ERR("Failed to open primary slot for UF2: %d", rc);
		return rc;
	}

#if defined(CONFIG_MCUBOOT_UF2_WRITABLE_STORAGE)
#if PARTITION_EXISTS(storage_partition)
	/* Filesystem (storage) partition: lets users drag a filesystem image
	 * (or wipe the filesystem) via UF2 in addition to firmware. Blocks
	 * addressed inside the partition are erased and written there;
	 * everything else is untouched. */
	rc = uf2_disk_add_region(PARTITION_ID(storage_partition));
	if (rc != 0) {
		BOOT_LOG_WRN("Storage partition not writable via UF2: %d", rc);
	}
#else
	BOOT_LOG_WRN("MCUBOOT_UF2_WRITABLE_STORAGE set but devicetree has no "
		     "partition labeled 'storage_partition'");
#endif
#endif

#if defined(CONFIG_MCUBOOT_UF2_WRITABLE_BOOTLOADER)
#if PARTITION_EXISTS(mcuboot)
	/* MCUboot's own partition. Off by default: the bootloader is actively
	 * executing while UF2 mode is active. Only enable where the SoC can
	 * write flash while executing from it; it makes CURRENT.UF2 readbacks
	 * fully restorable by drag-and-drop. */
	rc = uf2_disk_add_region(PARTITION_ID(mcuboot));
	if (rc != 0) {
		BOOT_LOG_WRN("Bootloader partition not writable via UF2: %d", rc);
	}
#else
	BOOT_LOG_WRN("MCUBOOT_UF2_WRITABLE_BOOTLOADER set but devicetree has "
		     "no partition labeled 'mcuboot'");
#endif
#endif

	/* Configure UF2 processing */
	uf2_cfg.write = uf2_flash_write;
	uf2_cfg.erase = uf2_flash_erase;
	uf2_cfg.cb_ctx = NULL;
	uf2_cfg.regions = uf2_regions;
	uf2_cfg.num_regions = num_disk_regions;
	uf2_cfg.family_id = CONFIG_MCUBOOT_UF2_FAMILY_ID;
	uf2_cfg.board_id = CONFIG_MCUBOOT_UF2_BOARD_ID;

	/* Use a safe default erase size */
	uf2_cfg.erase_size = 4096;

	uf2_cfg.board_name = CONFIG_MCUBOOT_UF2_BOARD_NAME;
	uf2_cfg.board_url = CONFIG_MCUBOOT_UF2_BOARD_URL;

	/* CURRENT.UF2 readback: read from the flash device covering
	 * offset 0 through the end of the primary slot. This captures
	 * the bootloader and the current application image.
	 */
	{
		const struct flash_area *primary_fap = disk_regions[0].fap;
		uintptr_t flash_base_addr;

		flash_device_base(flash_area_get_device_id(primary_fap),
				  &flash_base_addr);

		uf2_cfg.read = uf2_flash_read;
		uf2_cfg.read_ctx = (void *)flash_area_get_device(primary_fap);
		uf2_cfg.readback_base = (uint32_t)flash_base_addr;
		uf2_cfg.readback_size = primary_fap->fa_off +
					primary_fap->fa_size;

		BOOT_LOG_INF("CURRENT.UF2 readback: base 0x%x, size 0x%x",
			     (unsigned int)uf2_cfg.readback_base,
			     (unsigned int)uf2_cfg.readback_size);
	}

	/* Initialize UF2 state */
	uf2_init(&uf2_state_buf.state, CONFIG_MCUBOOT_UF2_MAX_BLOCKS);

	/* Register the virtual disk */
	rc = disk_access_register(&uf2_disk_info);
	if (rc != 0) {
		BOOT_LOG_ERR("Failed to register UF2 disk: %d", rc);
		uf2_disk_close_regions();
		return rc;
	}

	disk_registered = true;

	BOOT_LOG_INF("UF2 disk registered, %u writable region(s)",
		     num_disk_regions);

	return 0;
}

bool uf2_disk_primary_written(void)
{
	return num_disk_regions > 0 && disk_regions[0].bytes_written > 0;
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

	uf2_disk_close_regions();
}
