#
# Copyright (c) 2026 Scott Shawcroft for Adafruit Industries
#
# SPDX-License-Identifier: Apache-2.0
#

"""UF2 conversion utilities for MCUboot images."""

import struct

UF2_MAGIC_START0 = 0x0A324655  # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000
UF2_FLAG_EXTENSION_TAGS = 0x00008000

UF2_BLOCK_SIZE = 512
UF2_DATA_OFFSET = 32  # header is 32 bytes
UF2_DATA_SIZE = 476   # data region in a UF2 block

# Adaboot board-identity extension tag (see the UF2 spec's "Extension tags"
# section). The 24-bit type is stored little-endian on the wire; this random
# value avoids the standard tag set (0x9fc7bc, 0x650d9d, 0x0be9f7, 0xb46db0,
# 0xc8a729).
UF2_EXT_TAG_BOARD_ID = 0x4D7C3A


def _pack_extension_tag(tag, payload):
    """Build a single UF2 extension tag record (padded to 4 bytes)."""
    size = 4 + len(payload)  # 1 size byte + 3 type bytes + payload
    rec = bytes([size]) + tag.to_bytes(3, 'little') + payload
    # Pad to a 4-byte boundary with zeros.
    pad = (-len(rec)) % 4
    return rec + b'\x00' * pad


def bin_to_uf2(data, base_addr, family_id=0, payload_size=256, board_id=None):
    """Convert binary data to UF2 format.

    Args:
        data: Binary data to convert.
        base_addr: Target address for the first byte.
        family_id: UF2 family ID (0 to skip family check).
        payload_size: Bytes of payload per UF2 block (default 256).
        board_id: Per-board identity string of the form "<vendor>_<board>"
            (None or empty to omit). When set, each block carries it in a UF2
            extension tag (flag 0x8000) right after the payload, terminated by
            a 4-byte zero record.

    Returns:
        bytes: UF2 formatted data.
    """
    if payload_size > UF2_DATA_SIZE or payload_size <= 0:
        raise ValueError(f"Payload size must be between 1 and {UF2_DATA_SIZE}")

    board_id = board_id or None
    if board_id is not None:
        board_id_bytes = board_id.encode('utf-8')
        # 1-byte size field limits a tag's unpadded size to 255 bytes.
        if len(board_id_bytes) > 251:
            raise ValueError("board_id string is too long (max 251 bytes)")

    num_blocks = (len(data) + payload_size - 1) // payload_size
    flags = (UF2_FLAG_FAMILY_ID if family_id else 0) | \
           (UF2_FLAG_EXTENSION_TAGS if board_id else 0)

    result = bytearray()

    for block_no in range(num_blocks):
        offset = block_no * payload_size
        chunk = data[offset:offset + payload_size]
        target_addr = base_addr + offset

        # Build the 476-byte data field (payload + zero padding)
        block_data = bytearray(UF2_DATA_SIZE)
        block_data[:len(chunk)] = chunk

        # Extension tags start right after this block's payload
        # (at data offset == the block's payloadSize field).
        if board_id:
            ext = bytearray()
            ext += _pack_extension_tag(UF2_EXT_TAG_BOARD_ID, board_id_bytes)
            ext += b'\x00\x00\x00\x00'  # terminator record
            end = len(chunk) + len(ext)
            if end > UF2_DATA_SIZE:
                raise ValueError(
                    "board_id extension does not fit in a UF2 block "
                    f"with payload_size {payload_size}")
            block_data[len(chunk):len(chunk) + len(ext)] = ext

        block = struct.pack('<IIIIIIII',
                            UF2_MAGIC_START0,
                            UF2_MAGIC_START1,
                            flags,
                            target_addr,
                            len(chunk),
                            block_no,
                            num_blocks,
                            family_id)
        block += bytes(block_data)
        block += struct.pack('<I', UF2_MAGIC_END)

        assert len(block) == UF2_BLOCK_SIZE
        result.extend(block)

    return bytes(result)
