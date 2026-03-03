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

/**
 * Progressively erase flash from the current erase frontier up to (and
 * including) the sector that contains @p end_offset. This avoids erasing
 * the entire slot up front, which can cause long pauses on flash with
 * slow erase times.
 */
static int erase_up_to(const struct uf2_cfg *cfg, struct uf2_state *state,
		       uint32_t end_offset)
{
	int rc;

	/* Round end_offset up to the next erase-block boundary */
	uint32_t erase_end = ((end_offset / cfg->erase_size) + 1) * cfg->erase_size;

	if (erase_end > cfg->flash_size) {
		erase_end = cfg->flash_size;
	}

	while (state->erase_frontier < erase_end) {
		uint32_t len = cfg->erase_size;

		if (state->erase_frontier + len > cfg->flash_size) {
			len = cfg->flash_size - state->erase_frontier;
		}

		rc = cfg->erase(state->erase_frontier, len, cfg->cb_ctx);
		if (rc != 0) {
			return rc;
		}
		state->erase_frontier += len;
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

	/* Check family ID if configured */
	if (cfg->family_id != 0) {
		if (!(block->flags & UF2_FLAG_FAMILY_ID) ||
		    block->family_id != cfg->family_id) {
			return 0; /* Wrong family, silently ignore */
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

	/* Compute the target offset within the flash area */
	if (block->target_addr < cfg->flash_base) {
		return -1;
	}

	uint32_t offset = block->target_addr - cfg->flash_base;

	if (offset + block->payload_size > cfg->flash_size) {
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
	rc = erase_up_to(cfg, state, offset + block->payload_size);
	if (rc != 0) {
		return rc;
	}

	/* Write payload to flash */
	rc = cfg->write(offset, block->data, block->payload_size, cfg->cb_ctx);
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
