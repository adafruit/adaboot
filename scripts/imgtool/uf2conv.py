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

UF2_BLOCK_SIZE = 512
UF2_DATA_OFFSET = 32  # header is 32 bytes
UF2_DATA_SIZE = 476   # data region in a UF2 block


def bin_to_uf2(data, base_addr, family_id=0, payload_size=256):
    """Convert binary data to UF2 format.

    Args:
        data: Binary data to convert.
        base_addr: Target address for the first byte.
        family_id: UF2 family ID (0 to skip family check).
        payload_size: Bytes of payload per UF2 block (default 256).

    Returns:
        bytes: UF2 formatted data.
    """
    if payload_size > UF2_DATA_SIZE or payload_size <= 0:
        raise ValueError(f"Payload size must be between 1 and {UF2_DATA_SIZE}")

    num_blocks = (len(data) + payload_size - 1) // payload_size
    flags = UF2_FLAG_FAMILY_ID if family_id else 0

    result = bytearray()

    for block_no in range(num_blocks):
        offset = block_no * payload_size
        chunk = data[offset:offset + payload_size]
        target_addr = base_addr + offset

        # Build the 476-byte data field (payload + zero padding)
        block_data = bytearray(UF2_DATA_SIZE)
        block_data[:len(chunk)] = chunk

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
