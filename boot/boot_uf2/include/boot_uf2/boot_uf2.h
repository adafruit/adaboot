/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef H_BOOT_UF2_
#define H_BOOT_UF2_

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* UF2 block magic numbers */
#define UF2_MAGIC_START0  0x0A324655u  /* "UF2\n" */
#define UF2_MAGIC_START1  0x9E5D5157u
#define UF2_MAGIC_END     0x0AB16F30u

/* UF2 flags */
#define UF2_FLAG_FAMILY_ID  0x00002000u

/* UF2 block size is always 512 bytes */
#define UF2_BLOCK_SIZE     512
#define UF2_PAYLOAD_SIZE   256

/**
 * @brief UF2 block structure (512 bytes)
 */
struct uf2_block {
	uint32_t magic_start0;
	uint32_t magic_start1;
	uint32_t flags;
	uint32_t target_addr;
	uint32_t payload_size;
	uint32_t block_no;
	uint32_t num_blocks;
	uint32_t family_id;
	uint8_t  data[476];
	uint32_t magic_end;
};

/**
 * @brief Flash operation callbacks
 */
typedef int (*uf2_flash_write_cb)(uint32_t offset, const void *data, uint32_t len,
				  void *ctx);
typedef int (*uf2_flash_erase_cb)(uint32_t offset, uint32_t len, void *ctx);
typedef int (*uf2_flash_read_cb)(uint32_t offset, void *data, uint32_t len,
				 void *ctx);

/**
 * @brief UF2 configuration
 */
struct uf2_cfg {
	uf2_flash_write_cb write;
	uf2_flash_erase_cb erase;
	void *cb_ctx;
	uint32_t flash_base;
	uint32_t flash_size;
	uint32_t family_id;
	uint32_t erase_size;
	const char *board_name;
	const char *board_url;

	/* CURRENT.UF2 readback configuration.
	 * When read is non-NULL and readback_size > 0, the ghost FAT
	 * exposes a CURRENT.UF2 file that lets users download the
	 * current flash contents as a UF2 file.
	 */
	uf2_flash_read_cb read;
	void *read_ctx;
	uint32_t readback_base;  /* physical address for UF2 target_addr */
	uint32_t readback_size;  /* bytes to read back (0 = disabled) */
};

/**
 * @brief UF2 processing state
 */
struct uf2_state {
	uint32_t num_blocks;
	uint32_t blocks_received;
	uint32_t erase_frontier;
	bool     complete;
	/* Bitmask tracking which blocks have been received.
	 * Size determined by CONFIG_MCUBOOT_UF2_MAX_BLOCKS / 8.
	 */
	uint8_t  block_map[];
};

/**
 * @brief Initialize UF2 processing state
 *
 * @param state     State structure (must be sized for max_blocks)
 * @param max_blocks Maximum number of UF2 blocks supported
 */
void uf2_init(struct uf2_state *state, uint32_t max_blocks);

/**
 * @brief Process a single UF2 block
 *
 * Validates the block, erases flash progressively as needed,
 * and writes the payload to the target flash area.
 *
 * @param cfg   UF2 configuration
 * @param state UF2 processing state
 * @param block Pointer to 512-byte UF2 block data
 * @param max_blocks Maximum blocks the state can track
 *
 * @return 0 on success, negative errno on error
 */
int uf2_process_block(const struct uf2_cfg *cfg, struct uf2_state *state,
		      const struct uf2_block *block, uint32_t max_blocks);

/**
 * @brief Check if all blocks have been received
 */
static inline bool uf2_is_complete(const struct uf2_state *state)
{
	return state->complete;
}

/*
 * Ghost FAT virtual filesystem
 */

/**
 * @brief Get the total number of sectors in the virtual disk
 */
uint32_t ghostfat_get_sector_count(void);

/**
 * @brief Read a sector from the virtual FAT filesystem
 *
 * @param cfg    UF2 configuration (for board name / URL)
 * @param sector Sector number to read
 * @param buf    Buffer to fill (must be >= 512 bytes)
 */
void ghostfat_read_sector(const struct uf2_cfg *cfg, uint32_t sector,
			  uint8_t *buf);

/**
 * @brief Write a sector to the virtual FAT filesystem
 *
 * Data-area writes are inspected for UF2 magic; UF2 blocks are
 * processed via uf2_process_block(). Non-UF2 writes (FAT metadata
 * from host OS) are silently ignored.
 *
 * @param cfg        UF2 configuration
 * @param state      UF2 processing state
 * @param sector     Sector number being written
 * @param buf        Data being written (512 bytes)
 * @param max_blocks Maximum blocks the state can track
 *
 * @return 0 on success, negative errno on error
 */
int ghostfat_write_sector(const struct uf2_cfg *cfg, struct uf2_state *state,
			  uint32_t sector, const uint8_t *buf,
			  uint32_t max_blocks);

#ifdef __cplusplus
}
#endif

#endif /* H_BOOT_UF2_ */
