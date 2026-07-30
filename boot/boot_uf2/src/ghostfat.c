/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Virtual FAT16 filesystem for UF2 drag-and-drop.
 * Generates a FAT16 filesystem on the fly containing INFO_UF2.TXT,
 * INDEX.HTM, and optionally CURRENT.UF2 (a UF2-formatted dump of
 * flash contents for backup/cloning). Writes to the data area are
 * inspected for UF2 blocks and forwarded to uf2_process_block().
 */

#include <string.h>
#include <stdio.h>
#include "boot_uf2/boot_uf2.h"
#include "bootutil/bootutil_log.h"

BOOT_LOG_MODULE_DECLARE(mcuboot);

#define SECTOR_SIZE         512

/* Disk geometry */
#define NUM_FAT_SECTORS     64
#define NUM_ROOT_DIR_SECTORS 4
#define ROOT_DIR_ENTRIES     (NUM_ROOT_DIR_SECTORS * (SECTOR_SIZE / 32))
#define RESERVED_SECTORS     1
#define NUM_FATS             2
#define DATA_START_SECTOR   (RESERVED_SECTORS + (NUM_FATS * NUM_FAT_SECTORS) + \
			     NUM_ROOT_DIR_SECTORS)
/* Report a ~32 MB disk (max for FAT16 16-bit sector count) */
#define TOTAL_SECTORS       65535

/* Entries per FAT sector (512 bytes / 2 bytes per FAT16 entry) */
#define FAT_ENTRIES_PER_SECTOR  (SECTOR_SIZE / 2)

/* Cluster numbering starts at 2 in FAT */
#define CLUSTER_INFO_UF2    2
#define CLUSTER_INDEX_HTM   3
#define CLUSTER_CURRENT_UF2 4  /* first cluster for CURRENT.UF2 */

/* Virtual file contents (generated at read time) */
#define INFO_UF2_TXT_MAX    256
#define INDEX_HTM_MAX        256

/**
 * Number of UF2 blocks (= clusters = sectors) needed for CURRENT.UF2.
 * Returns 0 if readback is disabled.
 */
static uint32_t current_uf2_num_blocks(const struct uf2_cfg *cfg)
{
	if (cfg->readback_size == 0 || cfg->read == NULL) {
		return 0;
	}
	return (cfg->readback_size + UF2_PAYLOAD_SIZE - 1) / UF2_PAYLOAD_SIZE;
}

static void write_u16_le(uint8_t *buf, uint16_t val)
{
	buf[0] = (uint8_t)(val & 0xff);
	buf[1] = (uint8_t)(val >> 8);
}

static void write_u32_le(uint8_t *buf, uint32_t val)
{
	buf[0] = (uint8_t)(val & 0xff);
	buf[1] = (uint8_t)((val >> 8) & 0xff);
	buf[2] = (uint8_t)((val >> 16) & 0xff);
	buf[3] = (uint8_t)((val >> 24) & 0xff);
}

/**
 * Build the BIOS Parameter Block / boot sector (sector 0)
 * This also includes an MBR partition table so the host sees a
 * partitioned device with a single FAT16 partition.
 */
static void build_boot_sector(uint8_t *buf)
{
	memset(buf, 0, SECTOR_SIZE);

	/* Jump instruction */
	buf[0] = 0xEB;
	buf[1] = 0x3C;
	buf[2] = 0x90;

	/* OEM name */
	memcpy(&buf[3], "UF2 UF2 ", 8);

	/* Bytes per sector */
	write_u16_le(&buf[11], SECTOR_SIZE);

	/* Sectors per cluster */
	buf[13] = 1;

	/* Reserved sectors */
	write_u16_le(&buf[14], RESERVED_SECTORS);

	/* Number of FATs */
	buf[16] = NUM_FATS;

	/* Root directory entries */
	write_u16_le(&buf[17], ROOT_DIR_ENTRIES);

	/* Total sectors (16-bit) — 65535 is max, 65536 wraps to 0 */
	write_u16_le(&buf[19], TOTAL_SECTORS);

	/* Media descriptor */
	buf[21] = 0xF8;

	/* Sectors per FAT */
	write_u16_le(&buf[22], NUM_FAT_SECTORS);

	/* Sectors per track */
	write_u16_le(&buf[24], 1);

	/* Number of heads */
	write_u16_le(&buf[26], 1);

	/* Hidden sectors (0 for superfloppy) */
	write_u32_le(&buf[28], 0);

	/* Total sectors (32-bit) — set when 16-bit field is maxed out */
	write_u32_le(&buf[32], TOTAL_SECTORS);

	/* Drive number */
	buf[36] = 0x80;

	/* Extended boot signature */
	buf[38] = 0x29;

	/* Volume serial */
	write_u32_le(&buf[39], 0x00420042);

	/* Volume label (11 bytes, space-padded) */
	memcpy(&buf[43], "UF2 BOOT   ", 11);

	/* File system type */
	memcpy(&buf[54], "FAT16   ", 8);

	/* ── MBR partition table at offset 446 (0x1BE) ── */
	/* Partition 1: type 0x0E (FAT16 LBA), covers entire disk */
	buf[446] = 0x80; /* bootable */
	/* CHS start: 0/1/1 */
	buf[447] = 0x01; /* head */
	buf[448] = 0x01; /* sector (bits 0-5) + cylinder high bits */
	buf[449] = 0x00; /* cylinder low */
	buf[450] = 0x0E; /* FAT16 LBA */
	/* CHS end: not critical, set to max */
	buf[451] = 0xFE; /* head */
	buf[452] = 0xFF; /* sector */
	buf[453] = 0xFF; /* cylinder */
	/* LBA start = 0 (partition starts at sector 0) */
	write_u32_le(&buf[454], 0);
	/* LBA size = total sectors */
	write_u32_le(&buf[458], TOTAL_SECTORS);

	/* Boot sector signature */
	buf[510] = 0x55;
	buf[511] = 0xAA;
}

/**
 * Build a FAT16 table sector.
 *
 * Handles the fixed entries (media, reserved, INFO_UF2.TXT, INDEX.HTM)
 * and the CURRENT.UF2 cluster chain (when readback is enabled).
 */
static void build_fat_sector(const struct uf2_cfg *cfg, uint8_t *buf,
			     uint32_t fat_sector_idx)
{
	uint32_t num_current = current_uf2_num_blocks(cfg);
	uint32_t first_entry = fat_sector_idx * FAT_ENTRIES_PER_SECTOR;

	memset(buf, 0, SECTOR_SIZE);

	for (uint32_t i = 0; i < FAT_ENTRIES_PER_SECTOR; i++) {
		uint32_t cluster = first_entry + i;
		uint16_t val = 0;

		if (cluster == 0) {
			val = 0xFFF8; /* media descriptor */
		} else if (cluster == 1) {
			val = 0xFFFF; /* reserved */
		} else if (cluster == CLUSTER_INFO_UF2 ||
			   cluster == CLUSTER_INDEX_HTM) {
			val = 0xFFFF; /* single-cluster files */
		} else if (num_current > 0 &&
			   cluster >= CLUSTER_CURRENT_UF2 &&
			   cluster < CLUSTER_CURRENT_UF2 + num_current) {
			/* CURRENT.UF2 cluster chain */
			if (cluster == CLUSTER_CURRENT_UF2 + num_current - 1) {
				val = 0xFFFF; /* last cluster */
			} else {
				val = (uint16_t)(cluster + 1);
			}
		}

		if (val != 0) {
			write_u16_le(&buf[i * 2], val);
		}
	}
}

/**
 * Format a FAT directory entry (32 bytes)
 */
static void build_dir_entry(uint8_t *buf, const char *name11,
			    uint8_t attr, uint16_t cluster, uint32_t size)
{
	memset(buf, 0, 32);
	memcpy(buf, name11, 11);
	buf[11] = attr;
	write_u16_le(&buf[26], cluster);
	write_u32_le(&buf[28], size);
}

static uint32_t build_info_uf2_txt(const struct uf2_cfg *cfg, uint8_t *buf,
				   uint32_t max_len)
{
	const char *name = cfg->board_name ? cfg->board_name : "MCUboot";
	int len;

	len = snprintf((char *)buf, max_len,
		       "UF2 Bootloader v1.0.0\r\n"
		       "Model: %s\r\n"
		       "Board-ID: %s\r\n",
		       name, name);

	if (len < 0 || (uint32_t)len >= max_len) {
		return max_len - 1;
	}
	return (uint32_t)len;
}

static uint32_t build_index_htm(const struct uf2_cfg *cfg, uint8_t *buf,
				uint32_t max_len)
{
	const char *url = cfg->board_url ? cfg->board_url : "https://mcuboot.com";
	int len;

	len = snprintf((char *)buf, max_len,
		       "<!doctype html>\r\n"
		       "<html><head>"
		       "<meta http-equiv=\"refresh\" content=\"0;URL='%s'\">"
		       "</head><body>Redirecting to <a href=\"%s\">%s</a>"
		       "</body></html>\r\n",
		       url, url, url);

	if (len < 0 || (uint32_t)len >= max_len) {
		return max_len - 1;
	}
	return (uint32_t)len;
}

/**
 * Build root directory sectors
 */
static void build_root_dir_sector(const struct uf2_cfg *cfg, uint8_t *buf,
				  uint32_t dir_sector_idx)
{
	memset(buf, 0, SECTOR_SIZE);

	if (dir_sector_idx != 0) {
		return;
	}

	uint8_t info_buf[INFO_UF2_TXT_MAX];
	uint8_t index_buf[INDEX_HTM_MAX];
	uint32_t info_len = build_info_uf2_txt(cfg, info_buf, sizeof(info_buf));
	uint32_t index_len = build_index_htm(cfg, index_buf, sizeof(index_buf));

	/* Entry 0: Volume label */
	build_dir_entry(&buf[0], "UF2 BOOT   ", 0x08, 0, 0);

	/* Entry 1: INFO_UF2.TXT */
	build_dir_entry(&buf[32], "INFO_UF2TXT", 0x01, CLUSTER_INFO_UF2,
			info_len);

	/* Entry 2: INDEX.HTM */
	build_dir_entry(&buf[64], "INDEX   HTM", 0x01, CLUSTER_INDEX_HTM,
			index_len);

	/* Entry 3: CURRENT.UF2 (only if readback is enabled) */
	uint32_t num_current = current_uf2_num_blocks(cfg);

	if (num_current > 0) {
		uint32_t file_size = num_current * UF2_BLOCK_SIZE;

		build_dir_entry(&buf[96], "CURRENT UF2", 0x01,
				CLUSTER_CURRENT_UF2, file_size);
	}
}

/**
 * Generate a single CURRENT.UF2 block on the fly by reading from flash.
 *
 * Each 512-byte sector in CURRENT.UF2 is a complete UF2 block containing
 * 256 bytes of flash data read via the read callback.
 */
static void build_current_uf2_block(const struct uf2_cfg *cfg, uint8_t *buf,
				    uint32_t block_index, uint32_t num_blocks)
{
	uint32_t offset = block_index * UF2_PAYLOAD_SIZE;
	uint32_t payload_len = UF2_PAYLOAD_SIZE;

	if (offset + payload_len > cfg->readback_size) {
		payload_len = cfg->readback_size - offset;
	}

	memset(buf, 0, SECTOR_SIZE);

	/* UF2 header (bytes 0-31) */
	write_u32_le(&buf[0], UF2_MAGIC_START0);
	write_u32_le(&buf[4], UF2_MAGIC_START1);
	write_u32_le(&buf[8], cfg->family_id ? UF2_FLAG_FAMILY_ID : 0);
	write_u32_le(&buf[12], cfg->readback_base + offset);
	write_u32_le(&buf[16], payload_len);
	write_u32_le(&buf[20], block_index);
	write_u32_le(&buf[24], num_blocks);
	write_u32_le(&buf[28], cfg->family_id);

	/* Payload (bytes 32 .. 32+payload_len-1): read from flash */
	cfg->read(offset, &buf[32], payload_len, cfg->read_ctx);

	/* End magic (bytes 508-511) */
	write_u32_le(&buf[508], UF2_MAGIC_END);
}

uint32_t ghostfat_get_sector_count(void)
{
	return TOTAL_SECTORS;
}

static bool ghostfat_read_logged;

void ghostfat_read_sector(const struct uf2_cfg *cfg, uint32_t sector,
			  uint8_t *buf)
{
	if (!ghostfat_read_logged) {
		BOOT_LOG_DBG("ghostfat: first read at sector=%u", sector);
		ghostfat_read_logged = true;
	}

	if (sector == 0) {
		build_boot_sector(buf);
		return;
	}

	/* FAT 1 */
	if (sector >= RESERVED_SECTORS &&
	    sector < RESERVED_SECTORS + NUM_FAT_SECTORS) {
		build_fat_sector(cfg, buf, sector - RESERVED_SECTORS);
		return;
	}

	/* FAT 2 */
	if (sector >= RESERVED_SECTORS + NUM_FAT_SECTORS &&
	    sector < RESERVED_SECTORS + 2 * NUM_FAT_SECTORS) {
		build_fat_sector(cfg, buf,
				 sector - RESERVED_SECTORS - NUM_FAT_SECTORS);
		return;
	}

	/* Root directory */
	uint32_t root_start = RESERVED_SECTORS + 2 * NUM_FAT_SECTORS;

	if (sector >= root_start &&
	    sector < root_start + NUM_ROOT_DIR_SECTORS) {
		build_root_dir_sector(cfg, buf, sector - root_start);
		return;
	}

	/* Data area: virtual file content */
	if (sector >= DATA_START_SECTOR) {
		uint32_t cluster = (sector - DATA_START_SECTOR) + 2;

		if (cluster == CLUSTER_INFO_UF2) {
			memset(buf, 0, SECTOR_SIZE);
			build_info_uf2_txt(cfg, buf, SECTOR_SIZE);
			return;
		}

		if (cluster == CLUSTER_INDEX_HTM) {
			memset(buf, 0, SECTOR_SIZE);
			build_index_htm(cfg, buf, SECTOR_SIZE);
			return;
		}

		/* CURRENT.UF2: on-the-fly UF2 blocks from flash */
		uint32_t num_current = current_uf2_num_blocks(cfg);

		if (num_current > 0 &&
		    cluster >= CLUSTER_CURRENT_UF2 &&
		    cluster < CLUSTER_CURRENT_UF2 + num_current) {
			uint32_t block_idx = cluster - CLUSTER_CURRENT_UF2;

			build_current_uf2_block(cfg, buf, block_idx,
						num_current);
			return;
		}
	}

	/* Everything else: zeros */
	memset(buf, 0, SECTOR_SIZE);
}

int ghostfat_write_sector(const struct uf2_cfg *cfg, struct uf2_state *state,
			  uint32_t sector, const uint8_t *buf,
			  uint32_t max_blocks)
{
	BOOT_LOG_DBG("ghostfat: write sector=%u", sector);

	/* Only inspect data-area writes for UF2 blocks */
	if (sector < DATA_START_SECTOR) {
		return 0;
	}

	/* Check if this looks like a UF2 block */
	const struct uf2_block *block = (const struct uf2_block *)buf;

	if (block->magic_start0 != UF2_MAGIC_START0 ||
	    block->magic_start1 != UF2_MAGIC_START1 ||
	    block->magic_end != UF2_MAGIC_END) {
		/* Not a UF2 block - host OS FAT metadata, ignore */
		return 0;
	}

	return uf2_process_block(cfg, state, block, max_blocks);
}
