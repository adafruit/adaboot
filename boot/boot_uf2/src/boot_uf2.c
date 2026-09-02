/*
 * Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <string.h>
#include "boot_uf2/boot_uf2.h"

void uf2_init(struct uf2_state *state, uint32_t max_blocks)
{
	memset(state, 0, sizeof(*state) + (max_blocks + 7) / 8);
}

/* Region accessors. In multi-region mode (regions != NULL) they index the
 * region table; in legacy single-region mode there is exactly one region
 * described by flash_base/flash_size with ctx cb_ctx.
 */
static inline uint8_t uf2_num_regions(const struct uf2_cfg *cfg)
{
	return (cfg->regions != NULL && cfg->num_regions > 0)
		       ? cfg->num_regions : 1;
}

static inline uint32_t uf2_region_base(const struct uf2_cfg *cfg, uint8_t idx)
{
	return (cfg->regions != NULL && cfg->num_regions > 0)
		       ? cfg->regions[idx].base : cfg->flash_base;
}

static inline uint32_t uf2_region_size(const struct uf2_cfg *cfg, uint8_t idx)
{
	return (cfg->regions != NULL && cfg->num_regions > 0)
		       ? cfg->regions[idx].size : cfg->flash_size;
}

static inline void *uf2_region_ctx(const struct uf2_cfg *cfg, uint8_t idx)
{
	return (cfg->regions != NULL && cfg->num_regions > 0)
		       ? cfg->regions[idx].ctx : cfg->cb_ctx;
}

/**
 * Find the writable region containing an absolute flash address.
 *
 * @return Region index, or -1 if the address falls outside every region.
 */
static int uf2_find_region(const struct uf2_cfg *cfg, uint32_t addr)
{
	uint8_t num_regions = uf2_num_regions(cfg);

	for (uint8_t i = 0; i < num_regions; i++) {
		if (addr >= uf2_region_base(cfg, i) &&
		    addr < uf2_region_base(cfg, i) + uf2_region_size(cfg, i)) {
			return i;
		}
	}

	return -1;
}

/**
 * Progressively erase region @p region from its current erase frontier up
 * to (and including) the sector that contains @p end_offset. This avoids
 * erasing the entire region up front, which can cause long pauses on flash
 * with slow erase times, and keeps unwritten regions untouched.
 */
static int erase_up_to(const struct uf2_cfg *cfg, struct uf2_state *state,
		       uint8_t region, uint32_t end_offset)
{
	int rc;
	uint32_t region_size = uf2_region_size(cfg, region);
	uint32_t *frontier = &state->erase_frontier[region];

	/* Round end_offset up to the next erase-block boundary */
	uint32_t erase_end =
		((end_offset / cfg->erase_size) + 1) * cfg->erase_size;

	if (erase_end > region_size) {
		erase_end = region_size;
	}

	while (*frontier < erase_end) {
		uint32_t len = cfg->erase_size;

		if (*frontier + len > region_size) {
			len = region_size - *frontier;
		}

		rc = cfg->erase(*frontier, len, uf2_region_ctx(cfg, region));
		if (rc != 0) {
			return rc;
		}
		*frontier += len;
	}

	return 0;
}

int uf2_process_block(const struct uf2_cfg *cfg, struct uf2_state *state,
		      const struct uf2_block *block, uint32_t max_blocks)
{
	int rc;

	/* Validate magic numbers */
	if (block->magic_start0 != UF2_MAGIC_START0 ||
	    block->magic_start1 != UF2_MAGIC_START1 ||
	    block->magic_end != UF2_MAGIC_END) {
		return -1; /* Not a UF2 block, silently ignore */
	}

	/* Defensive: the region table must fit the state's frontier array */
	if (cfg->regions != NULL && cfg->num_regions > UF2_MAX_REGIONS) {
		return -1;
	}

	/* Check family ID if configured */
	if (cfg->family_id != 0) {
		if (!(block->flags & UF2_FLAG_FAMILY_ID) ||
		    block->family_id != cfg->family_id) {
			return 0; /* Wrong family, silently ignore */
		}
	}

	/* Check board ID if configured. The board_id is a UTF-8 string of the
	 * form "<vendor>_<board>" carried in a UF2 extension tag
	 * (flag UF2_FLAG_EXTENSION_TAGS) right after the payload. This mirrors
	 * the family_id check but binds the image to a specific board variant.
	 */
	if (cfg->board_id != NULL && cfg->board_id[0] != '\0') {
		size_t want_len = strlen(cfg->board_id);
		bool match = false;

		if (block->flags & UF2_FLAG_EXTENSION_TAGS) {
			uint32_t off = block->payload_size;

			while (off + 4 <= UF2_DATA_SIZE) {
				uint8_t sz = block->data[off];

				/* Terminator: size 0, type 0. */
				if (sz == 0) {
					break;
				}

				/* Malformed: need at least the 4-byte header,
				 * and the record must fit in the data field.
				 */
				if (sz < 4 || off + sz > UF2_DATA_SIZE) {
					break;
				}

				if (block->data[off + 1] == UF2_EXT_TAG_BOARD_ID_B0 &&
				    block->data[off + 2] == UF2_EXT_TAG_BOARD_ID_B1 &&
				    block->data[off + 3] == UF2_EXT_TAG_BOARD_ID_B2) {
					uint8_t plen = (uint8_t)(sz - 4);

					if (plen == want_len &&
					    memcmp(&block->data[off + 4],
					           cfg->board_id, want_len) == 0) {
						match = true;
					}
					break; /* board tag found (match or not) */
				}

				/* Advance past this padded record. */
				off += ((uint32_t)sz + 3u) & ~3u;
			}
		}

		if (!match) {
			return 0; /* Wrong board, silently ignore */
		}
	}

	/* Validate block numbers */
	if (block->num_blocks == 0 || block->block_no >= block->num_blocks) {
		return -1;
	}

	if (block->num_blocks > max_blocks) {
		return -1;
	}

	/* Validate payload size */
	if (block->payload_size > UF2_PAYLOAD_SIZE || block->payload_size == 0) {
		return -1;
	}

	/* Route the block's absolute target address to a writable region */
	int region = uf2_find_region(cfg, block->target_addr);

	if (region < 0) {
		return -1; /* Outside every writable region */
	}

	uint32_t offset = block->target_addr - uf2_region_base(cfg, region);

	if (offset + block->payload_size > uf2_region_size(cfg, region)) {
		return -1;
	}

	/* On the first block, record expected total */
	if (state->num_blocks == 0) {
		state->num_blocks = block->num_blocks;
	} else if (state->num_blocks != block->num_blocks) {
		return -1;
	}

	/* Check if already received */
	uint32_t byte_idx = block->block_no / 8;
	uint8_t bit_mask = 1u << (block->block_no % 8);

	if (state->block_map[byte_idx] & bit_mask) {
		return 0; /* Duplicate, ignore */
	}

	/* Progressive erase: ensure flash is erased up to this write */
	rc = erase_up_to(cfg, state, (uint8_t)region,
			 offset + block->payload_size);
	if (rc != 0) {
		return rc;
	}

	/* Write payload to flash */
	rc = cfg->write(offset, block->data, block->payload_size,
			uf2_region_ctx(cfg, (uint8_t)region));
	if (rc != 0) {
		return rc;
	}

	/* Mark block as received */
	state->block_map[byte_idx] |= bit_mask;
	state->blocks_received++;

	/* Check for completion */
	if (state->blocks_received >= state->num_blocks) {
		state->complete = true;
	}

	return 0;
}
