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

/* Scan a UF2 block's extension tags (flag UF2_FLAG_EXTENSION_TAGS) for
 * one of @p tag_type. Records live right after the payload, each
 * [size:1 (size byte + 3-byte type + payload)][type:3 LE][payload]
 * padded to a 4-byte boundary, terminated by a 4-byte zero record (see
 * the UF2 spec and the fork's uf2conv.py --ext, which pack the same
 * layout).
 *
 * Returns the payload length and sets *payload to point into the
 * block's data field when the tag is found; -1 when the block has no
 * extension tags or the tag is absent; -2 when the tag region is
 * malformed (truncated record, so any other tag's payload is
 * untrustworthy too).
 */
static int uf2_find_ext_tag(const struct uf2_block *block, uint32_t tag_type,
			    const uint8_t **payload)
{
	if (!(block->flags & UF2_FLAG_EXTENSION_TAGS)) {
		return -1;
	}

	uint32_t off = block->payload_size;

	while (off + 4 <= UF2_DATA_SIZE) {
		uint8_t sz = block->data[off];

		/* Terminator: size 0, type 0. */
		if (sz == 0) {
			return -1;
		}

		/* Malformed: need at least the 4-byte header, and the record
		 * must fit in the data field.
		 */
		if (sz < 4 || off + sz > UF2_DATA_SIZE) {
			return -2;
		}

		uint32_t type = (uint32_t)block->data[off + 1] |
				((uint32_t)block->data[off + 2] << 8) |
				((uint32_t)block->data[off + 3] << 16);
		if (type == tag_type) {
			*payload = &block->data[off + 4];
			return (int)(sz - 4);
		}

		/* Advance past this padded record. */
		off += ((uint32_t)sz + 3u) & ~3u;
	}

	return -2;
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
		/* Only regions reachable by address participate; regions on
		 * other flash devices overlap window 0 and are reachable via
		 * their partition-name tag only. */
		if (cfg->regions != NULL && !cfg->regions[i].address_routed) {
			continue;
		}
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

/* Find the writable region whose label matches @p label.
 *
 * @return Region index, or -1 if no named region matches.
 */
static int uf2_find_region_by_label(const struct uf2_cfg *cfg,
				    const uint8_t *label, int label_len)
{
	uint8_t num_regions = uf2_num_regions(cfg);

	for (uint8_t i = 0; i < num_regions; i++) {
		const char *region_label =
			(cfg->regions != NULL) ? cfg->regions[i].label : NULL;
		size_t region_len;

		if (region_label == NULL) {
			continue;
		}
		region_len = strlen(region_label);
		if (region_len == (size_t)label_len &&
		    memcmp(region_label, label, label_len) == 0) {
			return i;
		}
	}

	return -1;
}

/* Route the block to its writable region. By default that is the region
 * containing the block's absolute target address; when the block carries
 * a partition-name extension tag, the named region must exist and contain
 * the address -- a disagreement between the generator's addressing and
 * the bootloader's is rejected here instead of silently corrupting
 * whichever partition the address happens to reach.
 */
static int uf2_route_block(const struct uf2_cfg *cfg,
			   const struct uf2_block *block)
{
	const uint8_t *part_name;
	int part_name_len = uf2_find_ext_tag(block, UF2_EXT_TAG_PARTITION,
					     &part_name);
	int region;

	if (part_name_len == -2) {
		return -1; /* Malformed tag region */
	}

	if (part_name_len >= 0) {
		region = uf2_find_region_by_label(cfg, part_name, part_name_len);
		if (region < 0) {
			/* Unknown partition name: reject rather than route by
			 * address, so a stale/mistargeted file can't write
			 * anywhere unexpected. */
			return -1;
		}
		/* The named region must contain the target address too: the
		 * tag guards the address scheme, it doesn't replace it. */
		if (block->target_addr < uf2_region_base(cfg, region) ||
		    block->target_addr >=
		    uf2_region_base(cfg, region) + uf2_region_size(cfg, region)) {
			return -1;
		}
		return region;
	}

	region = uf2_find_region(cfg, block->target_addr);
	if (region < 0) {
		return -1; /* Outside every writable region */
	}
	return region;
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
		const uint8_t *board_id;
		int board_id_len = uf2_find_ext_tag(block, UF2_EXT_TAG_BOARD_ID,
						    &board_id);
		if (board_id_len < 0 ||
		    board_id_len != (int)strlen(cfg->board_id) ||
		    memcmp(board_id, cfg->board_id, board_id_len) != 0) {
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

	int region = uf2_route_block(cfg, block);

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
