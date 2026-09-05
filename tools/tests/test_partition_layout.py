# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 Scott Shawcroft for Adafruit Industries

"""Tests for tools/partition_layout.py partition planning and rendering."""

import sys
import pathlib
from types import SimpleNamespace

import pytest

# Add the tools dir to path so we can import partition_layout.
TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

from partition_layout import (
    KB,
    MB,
    align_up,
    align_down,
    format_size,
    format_dt_size,
    make_partitions_with_gaps,
    render_bar,
    render_detail_lines,
    generate_partitions_dtsi,
    mapped_partition_devices,
    plan_partitions,
    _has_predefined_mcuboot,
    discover_all_flash,
    get_erase_size,
    get_total_size,
    has_native_usb,
    filesystem_node_label,
    ZEPHYR_BASE,
)


# ── Helpers to build fake EDT nodes ──────────────────────────────────────


def _prop(val):
    return SimpleNamespace(val=val)


def _flash_node(
    label,
    size_bits=None,
    reg=None,
    erase_block_size=4096,
    compats=None,
    filename=None,
    children=None,
    labels=None,
):
    """Build a minimal fake flash node for testing."""
    props = {}
    if erase_block_size is not None:
        props["erase-block-size"] = _prop(erase_block_size)
    if size_bits is not None:
        props["size"] = _prop(size_bits)
    if reg is not None:
        props["reg"] = _prop(reg)
    return SimpleNamespace(
        name=label,
        labels=labels or [label],
        props=props,
        compats=compats or ["soc-nv-flash"],
        children=children or {},
        filename=filename or "",
    )


def _partition_node(label, node_label, offset, size):
    """Build a fake partition child node."""
    return SimpleNamespace(
        name=node_label,
        labels=[node_label],
        props={
            "label": _prop(label),
            "reg": _prop([offset, size]),
        },
    )


def _usb_node(labels=("zephyr_udc0",), status="okay"):
    """Build a minimal fake USB device controller node (no reg/props needed:
    the planner only looks at its labels and status)."""
    return SimpleNamespace(
        name="usbd",
        labels=list(labels),
        props={},
        compats=["vendor,usbd"],
        children={},
        status=status,
    )


def _flash_with_partitions(label, size, erase, partitions, filename=None):
    """Build a flash node with predefined partition children.

    partitions: list of (label, node_label, offset, size) tuples.
    """
    part_children = {}
    for plabel, pnode_label, poffset, psize in partitions:
        part_children[pnode_label] = _partition_node(plabel, pnode_label, poffset, psize)
    partitions_node = SimpleNamespace(
        compats=["fixed-partitions"],
        children=part_children,
    )
    return _flash_node(
        label,
        size_bits=8 * size,
        erase_block_size=erase,
        filename=filename or str(ZEPHYR_BASE / "boards" / "vendor" / "board.dts"),
        children={"partitions": partitions_node},
    )


def _make_edt(*nodes):
    """Build a minimal fake EDT with the given nodes."""
    return SimpleNamespace(nodes=list(nodes))


# ── Unit tests: align_up / align_down ────────────────────────────────────


class TestAlign:
    def test_align_up_already_aligned(self):
        assert align_up(4096, 4096) == 4096

    def test_align_up_not_aligned(self):
        assert align_up(4097, 4096) == 8192

    def test_align_up_zero(self):
        assert align_up(0, 4096) == 0

    def test_align_down_already_aligned(self):
        assert align_down(8192, 4096) == 8192

    def test_align_down_not_aligned(self):
        assert align_down(8193, 4096) == 8192

    def test_align_down_zero(self):
        assert align_down(0, 4096) == 0


# ── Unit tests: format_size ──────────────────────────────────────────────


class TestFormatSize:
    def test_megabytes_exact(self):
        assert format_size(1 * MB) == "1 MB"
        assert format_size(2 * MB) == "2 MB"

    def test_megabytes_fractional(self):
        assert format_size(int(1.5 * MB)) == "1.5 MB"

    def test_kilobytes_exact(self):
        assert format_size(64 * KB) == "64 KB"
        assert format_size(256 * KB) == "256 KB"

    def test_kilobytes_fractional(self):
        assert format_size(int(4.5 * KB)) == "4.5 KB"

    def test_small(self):
        assert format_size(512) == "0.5 KB"


# ── Unit tests: format_dt_size ───────────────────────────────────────────


class TestFormatDtSize:
    def test_megabytes(self):
        assert format_dt_size(1 * MB) == "DT_SIZE_M(1)"
        assert format_dt_size(4 * MB) == "DT_SIZE_M(4)"

    def test_kilobytes(self):
        assert format_dt_size(64 * KB) == "DT_SIZE_K(64)"
        assert format_dt_size(256 * KB) == "DT_SIZE_K(256)"

    def test_raw_hex(self):
        assert format_dt_size(4097) == "0x1001"


# ── Unit tests: make_partitions_with_gaps ────────────────────────────────


class TestMakePartitionsWithGaps:
    def test_no_partitions(self):
        result = make_partitions_with_gaps([], 1 * MB)
        assert result == [("free", 0, 1 * MB)]

    def test_single_partition_at_start(self):
        parts = [("mcuboot", "boot_partition", 0, 64 * KB)]
        result = make_partitions_with_gaps(parts, 1 * MB)
        assert result[0] == ("mcuboot", 0, 64 * KB)
        assert result[1] == ("free", 64 * KB, 1 * MB - 64 * KB)

    def test_gap_between_partitions(self):
        parts = [
            ("mcuboot", "boot_partition", 0, 64 * KB),
            ("image-0", "slot0_partition", 128 * KB, 256 * KB),
        ]
        result = make_partitions_with_gaps(parts, 1 * MB)
        assert result[0] == ("mcuboot", 0, 64 * KB)
        assert result[1] == ("free", 64 * KB, 64 * KB)
        assert result[2] == ("image-0", 128 * KB, 256 * KB)
        assert result[3] == ("free", 384 * KB, 1 * MB - 384 * KB)

    def test_contiguous_partitions(self):
        parts = [
            ("mcuboot", "boot_partition", 0, 64 * KB),
            ("image-0", "slot0_partition", 64 * KB, 448 * KB),
        ]
        result = make_partitions_with_gaps(parts, 512 * KB)
        assert len(result) == 2
        assert result[0] == ("mcuboot", 0, 64 * KB)
        assert result[1] == ("image-0", 64 * KB, 448 * KB)

    def test_fills_entire_flash(self):
        parts = [("image-0", "slot0_partition", 0, 1 * MB)]
        result = make_partitions_with_gaps(parts, 1 * MB)
        assert len(result) == 1


# ── Unit tests: render_bar ───────────────────────────────────────────────


class TestRenderBar:
    def test_single_partition(self):
        parts = [("mcuboot", "boot_partition", 0, 1 * MB)]
        bar = render_bar(parts, 1 * MB)
        assert len(bar) == 72
        assert "B" in bar

    def test_mixed_partitions(self):
        parts = [
            ("mcuboot", "boot_partition", 0, 64 * KB),
            ("image-0", "slot0_partition", 64 * KB, 448 * KB),
            ("filesystem", "fatfs_partition", 512 * KB, 512 * KB),
        ]
        bar = render_bar(parts, 1 * MB)
        assert len(bar) == 72
        assert "B" in bar
        assert "0" in bar
        assert "#" in bar


# ── Unit tests: render_detail_lines ──────────────────────────────────────


class TestRenderDetailLines:
    def test_alignment_warnings(self):
        erase = 4096
        parts = [("mcuboot", "boot_partition", 100, 5000)]  # misaligned offset and size
        lines = render_detail_lines(parts, 1 * MB, erase)
        mcuboot_line = [l for l in lines if "mcuboot" in l][0]
        assert "!offset" in mcuboot_line
        assert "!size" in mcuboot_line

    def test_no_warnings_when_aligned(self):
        erase = 4096
        parts = [("mcuboot", "boot_partition", 0, 64 * KB)]
        lines = render_detail_lines(parts, 1 * MB, erase)
        assert "!offset" not in lines[0]
        assert "!size" not in lines[0]

    def test_small_free_gaps_hidden(self):
        erase = 4096
        parts = [
            ("mcuboot", "boot_partition", 0, 64 * KB),
            ("image-0", "slot0_partition", 64 * KB + 512, 256 * KB),
        ]
        lines = render_detail_lines(parts, 1 * MB, erase)
        # The 512-byte gap should be hidden (< 1024)
        labels = [l.strip().split()[-1] for l in lines]
        assert "free" not in labels


# ── Unit tests: get_erase_size ───────────────────────────────────────────


class TestGetEraseSize:
    def test_from_erase_block_size_prop(self):
        node = _flash_node("flash0", erase_block_size=8192)
        assert get_erase_size(node) == 8192

    def test_from_pages_layout(self):
        layout_child = SimpleNamespace(
            props={"pages-size": _prop(32768)},
            children={},
        )
        pages_layout = SimpleNamespace(children={"layout0": layout_child})
        node = _flash_node(
            "flash0", erase_block_size=None, children={"pages_layout": pages_layout}
        )
        assert get_erase_size(node) == 32768

    def test_default_4096(self):
        node = _flash_node("flash0", erase_block_size=None)
        assert get_erase_size(node) == 4096


# ── Unit tests: get_total_size ───────────────────────────────────────────


class TestGetTotalSize:
    def test_from_size_prop_bits(self):
        node = _flash_node("flash0", size_bits=8 * 1 * MB)
        assert get_total_size(node) == 1 * MB

    def test_from_reg_prop(self):
        node = _flash_node("flash0", reg=[0x0, 512 * KB])
        assert get_total_size(node) == 512 * KB

    def test_zero_when_no_size(self):
        node = _flash_node("flash0")
        assert get_total_size(node) == 0


# ── Unit tests: discover_all_flash ───────────────────────────────────────


class TestDiscoverAllFlash:
    def test_finds_flash_node(self):
        node = _flash_node("flash0", size_bits=8 * MB * 8, erase_block_size=4096)
        edt = _make_edt(node)
        devices = discover_all_flash(edt)
        assert len(devices) == 1
        assert devices[0][0] == "flash0"  # dev_label
        assert devices[0][1] == 8 * MB  # total_size
        assert devices[0][2] == 4096  # erase_size

    def test_skips_controllers(self):
        node = _flash_node(
            "flash_controller",
            size_bits=8 * MB * 8,
            erase_block_size=4096,
            compats=["nrf-flash-controller"],
        )
        edt = _make_edt(node)
        devices = discover_all_flash(edt)
        assert len(devices) == 0

    def test_skips_zero_size(self):
        node = _flash_node("flash0", erase_block_size=4096)
        edt = _make_edt(node)
        devices = discover_all_flash(edt)
        assert len(devices) == 0

    def test_internal_detection(self):
        internal_path = str(ZEPHYR_BASE / "dts" / "arm" / "some_soc.dtsi")
        node = _flash_node("flash0", size_bits=8 * MB * 8, filename=internal_path)
        edt = _make_edt(node)
        devices = discover_all_flash(edt)
        assert devices[0][3] is True  # is_internal

    def test_external_detection(self):
        external_path = str(ZEPHYR_BASE / "boards" / "vendor" / "board.dts")
        node = _flash_node("mx25r", size_bits=8 * 16 * MB, filename=external_path)
        edt = _make_edt(node)
        devices = discover_all_flash(edt)
        assert devices[0][3] is False  # is_internal


# ── Unit tests: mapped-partition capability ───────────────────────────


class TestMappedPartitionDevices:
    def test_soc_nv_flash_device_is_mapped_capable(self):
        flash = SimpleNamespace(
            name="flash@0",
            labels=["flash0"],
            compats=["vendor,nv-flash", "soc-nv-flash"],
        )
        edt = _make_edt(flash)
        assert mapped_partition_devices(edt) == {"flash0"}

    def test_ra_style_device_is_not_mapped_capable(self):
        """Renesas RA code flash includes the soc-nv-flash *binding* but does
        not carry the literal compatible, so it cannot host mapped partitions."""
        flash = SimpleNamespace(
            name="flash@0",
            labels=["flash0"],
            compats=["renesas,ra-nv-code-flash"],
        )
        nor = SimpleNamespace(
            name="qspi-nor-flash@60000000",
            labels=["mx25l25645g"],
            compats=["renesas,ra-qspi-nor"],
        )
        edt = _make_edt(flash, nor)
        assert mapped_partition_devices(edt) == set()


# ── Unit tests: native USB detection ───────────────────────────


class TestNativeUsb:
    def test_no_usb_nodes(self):
        edt = _make_edt(_internal_flash())
        assert has_native_usb(edt) is False
        assert filesystem_node_label(edt) == "littlefs_partition"

    def test_udc0_enabled(self):
        edt = _make_edt(_internal_flash(), _usb_node())
        assert has_native_usb(edt) is True
        assert filesystem_node_label(edt) == "fatfs_partition"

    def test_udc0_disabled(self):
        """A disabled controller is not available, even if it exists."""
        edt = _make_edt(_internal_flash(), _usb_node(status="disabled"))
        assert has_native_usb(edt) is False
        assert filesystem_node_label(edt) == "littlefs_partition"

    def test_other_usb_node_without_label(self):
        """A USB node without the zephyr_udc0 label is not the device stack's
        controller (e.g. a host controller on nrf54lm20dk)."""
        edt = _make_edt(
            _internal_flash(),
            _usb_node(labels=("usbhs", "zephyr_uhc0")),
        )
        assert has_native_usb(edt) is False


class TestFilesystemNodeLabel:
    def test_internal_only_gets_littlefs_without_usb(self):
        edt = _make_edt(_internal_flash())
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        fs = [p for p in parts if p[0] == "filesystem"][0]
        assert fs[1] == "littlefs_partition"

    def test_internal_only_gets_fatfs_with_usb(self):
        edt = _make_edt(_internal_flash(), _usb_node())
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        fs = [p for p in parts if p[0] == "filesystem"][0]
        assert fs[1] == "fatfs_partition"

    def test_external_gets_fatfs_with_usb(self):
        edt = _make_edt(
            _internal_flash(), _external_flash(), _usb_node()
        )
        result = plan_partitions(edt)
        ext = [r for r in result if r[0] == "mx25r"][0]
        fs = [p for p in ext[3] if p[0] == "filesystem"][0]
        assert fs[1] == "fatfs_partition"

    def test_external_only_gets_fatfs_with_usb(self):
        edt = _make_edt(
            _external_flash(size=16 * MB, erase=4096), _usb_node()
        )
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        fs = [p for p in parts if p[0] == "filesystem"][0]
        assert fs[1] == "fatfs_partition"

    def test_predefined_gets_fatfs_with_usb(self):
        """Predefined layouts also pick the node label from the board USB."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash, _usb_node())
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        fs = [p for p in parts if p[0] == "filesystem"][0]
        assert fs[1] == "fatfs_partition"


# ── Unit tests: plan_partitions ──────────────────────────────────────────


def _internal_flash(label="flash0", size=2 * MB, erase=4096):
    return _flash_node(
        label,
        size_bits=8 * size,
        erase_block_size=erase,
        filename=str(ZEPHYR_BASE / "dts" / "arm" / "soc.dtsi"),
    )


def _external_flash(label="mx25r", size=16 * MB, erase=4096):
    return _flash_node(
        label,
        size_bits=8 * size,
        erase_block_size=erase,
        filename=str(ZEPHYR_BASE / "boards" / "vendor" / "board.dts"),
    )


class TestPlanPartitions:
    def test_empty_edt(self):
        edt = _make_edt()
        assert plan_partitions(edt) == []

    def test_internal_only(self):
        edt = _make_edt(_internal_flash())
        result = plan_partitions(edt)
        assert len(result) == 1
        dev_label, total_size, erase_size, parts, _predefined = result[0]
        assert dev_label == "flash0"
        labels = [p[0] for p in parts]
        assert labels == ["mcuboot", "image-0", "storage", "nvm", "filesystem"]
        # No slot1 when internal-only
        assert "image-1" not in labels

    def test_internal_only_alignment(self):
        edt = _make_edt(_internal_flash())
        result = plan_partitions(edt)
        _, _, erase_size, parts, _ = result[0]
        for label, node_label, offset, size in parts:
            assert offset % erase_size == 0, f"{label} offset 0x{offset:x} not aligned"
            assert size % erase_size == 0, f"{label} size 0x{size:x} not aligned"

    def test_internal_only_no_overlap(self):
        edt = _make_edt(_internal_flash())
        result = plan_partitions(edt)
        _, total_size, _, parts, _ = result[0]
        for i in range(len(parts) - 1):
            end_i = parts[i][2] + parts[i][3]
            start_next = parts[i + 1][2]
            assert end_i <= start_next, (
                f"{parts[i][0]} ends at 0x{end_i:x} but {parts[i + 1][0]} starts at 0x{start_next:x}"
            )
        # Last partition should not exceed flash
        last_end = parts[-1][2] + parts[-1][3]
        assert last_end <= total_size

    def test_internal_plus_external(self):
        edt = _make_edt(_internal_flash(), _external_flash())
        result = plan_partitions(edt)
        assert len(result) == 2

        int_label, _, _, int_parts, _ = result[0]
        ext_label, _, _, ext_parts, _ = result[1]
        assert int_label == "flash0"
        assert ext_label == "mx25r"

        int_labels = [p[0] for p in int_parts]
        ext_labels = [p[0] for p in ext_parts]

        assert "mcuboot" in int_labels
        assert "image-0" in int_labels
        assert "image-1" in ext_labels
        assert "filesystem" in ext_labels
        # NVM should be on external when no data flash
        assert "nvm" in ext_labels

    def test_internal_plus_external_alignment(self):
        edt = _make_edt(
            _internal_flash(erase=4096),
            _external_flash(erase=65536),
        )
        result = plan_partitions(edt)
        for dev_label, total_size, erase_size, parts, _ in result:
            for label, node_label, offset, size in parts:
                assert offset % erase_size == 0, (
                    f"{dev_label}/{label} offset 0x{offset:x} not aligned to {erase_size}"
                )
                assert size % erase_size == 0, (
                    f"{dev_label}/{label} size 0x{size:x} not aligned to {erase_size}"
                )

    def test_internal_plus_external_slot_sizes(self):
        """slot1 should be slot0 + max_erase for swap-using-offset."""
        edt = _make_edt(
            _internal_flash(erase=4096),
            _external_flash(erase=4096),
        )
        result = plan_partitions(edt)
        int_parts = {p[0]: p for p in result[0][3]}
        ext_parts = {p[0]: p for p in result[1][3]}
        slot0_size = int_parts["image-0"][3]
        slot1_size = ext_parts["image-1"][3]
        max_erase = max(result[0][2], result[1][2])
        assert slot1_size == slot0_size + max_erase

    def test_ra6_internal_32k_external_4k(self):
        """RA6-like: 2MB internal @ 32K erase, 16MB external @ 4K erase.

        slot0 should be 60 * 32K pages (filling internal after the 128K
        mcuboot partition). slot1 must be 61 * 32K pages -- one extra
        max_erase sector for mcuboot swap-using-offset scratch, even though
        external is 4K pages.
        """
        edt = _make_edt(
            _internal_flash(size=2 * MB, erase=32 * KB),
            _external_flash(size=16 * MB, erase=4096),
        )
        result = plan_partitions(edt)
        assert len(result) == 2

        int_parts = {p[0]: p for p in result[0][3]}
        ext_parts = {p[0]: p for p in result[1][3]}

        # slot0 fills internal after the 128K mcuboot, aligned to 32K
        assert int_parts["mcuboot"][3] == 128 * KB
        assert int_parts["image-0"][3] == 60 * 32 * KB

        # slot1 = slot0 + one 32K sector (max_erase), NOT one 4K sector
        assert ext_parts["image-1"][3] == 61 * 32 * KB

        # All partitions on each device must be aligned to that device's erase
        for dev_label, total_size, erase_size, parts, _ in result:
            for label, node_label, offset, size in parts:
                assert offset % erase_size == 0, (
                    f"{dev_label}/{label} offset 0x{offset:x} not aligned to {erase_size}"
                )
                assert size % erase_size == 0, (
                    f"{dev_label}/{label} size 0x{size:x} not aligned to {erase_size}"
                )

    def test_external_only(self):
        edt = _make_edt(_external_flash(size=16 * MB, erase=4096))
        result = plan_partitions(edt)
        assert len(result) == 1
        _, _, _, parts, _ = result[0]
        labels = [p[0] for p in parts]
        assert labels == ["mcuboot", "image-0", "image-1", "storage", "nvm", "filesystem"]

    def test_external_only_alignment(self):
        edt = _make_edt(_external_flash(size=16 * MB, erase=65536))
        result = plan_partitions(edt)
        _, _, erase_size, parts, _ = result[0]
        for label, node_label, offset, size in parts:
            assert offset % erase_size == 0, f"{label} offset not aligned"
            assert size % erase_size == 0, f"{label} size not aligned"

    def test_overwrite_only_keeps_slot1_with_large_write_block(self):
        """Overwrite mode supports an RA8-like flash that cannot swap."""
        edt = _make_edt(
            _internal_flash(size=2 * MB, erase=32 * KB),
            _external_flash(size=64 * MB, erase=4096),
        )
        edt.chosen_node = lambda name: SimpleNamespace(
            props={"write-block-size": _prop(128)}
        )

        result = plan_partitions(edt, overwrite_only=True)
        int_parts = {p[0]: p for p in result[0][3]}
        ext_parts = {p[0]: p for p in result[1][3]}

        assert ext_parts["image-1"][3] == int_parts["image-0"][3]

    def test_small_external_ignored(self):
        """External flash < 1MB should be ignored (e.g. small SPI flash)."""
        edt = _make_edt(
            _internal_flash(),
            _external_flash(label="spi_flash", size=512 * KB),
        )
        result = plan_partitions(edt)
        # Should behave as internal-only (small external ignored)
        assert len(result) == 1
        assert result[0][0] == "flash0"

    def test_mcuboot_at_least_64k(self):
        edt = _make_edt(_internal_flash(erase=4096))
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        boot = [p for p in parts if p[0] == "mcuboot"][0]
        assert boot[3] >= 64 * KB

    def test_data_flash_nvm(self):
        """Small internal data flash (like RA6 flash1) should be used for NVM."""
        edt = _make_edt(
            _internal_flash(label="flash0", size=2 * MB, erase=4096),
            _internal_flash(label="flash1", size=8 * KB, erase=64),
            _external_flash(size=16 * MB, erase=4096),
        )
        result = plan_partitions(edt)
        # Should have: internal (flash0), external (mx25r), data flash (flash1)
        dev_labels = [r[0] for r in result]
        assert "flash1" in dev_labels

        # flash1 should contain only an NVM partition
        flash1 = [r for r in result if r[0] == "flash1"][0]
        _, _, _, parts, _ = flash1
        assert len(parts) == 1
        assert parts[0][0] == "nvm"
        assert parts[0][1] == "nvm_partition"
        # NVM should use the whole data flash (aligned to erase size)
        assert parts[0][3] == 8 * KB

        # External should NOT have NVM (data flash handles it)
        ext = [r for r in result if r[0] == "mx25r"][0]
        ext_labels = [p[0] for p in ext[3]]
        assert "nvm" not in ext_labels

    def test_data_flash_nvm_internal_only(self):
        """Data flash + internal only: NVM on data flash, not on main internal."""
        edt = _make_edt(
            _internal_flash(label="flash0", size=2 * MB, erase=4096),
            _internal_flash(label="flash1", size=8 * KB, erase=64),
        )
        result = plan_partitions(edt)
        dev_labels = [r[0] for r in result]
        assert "flash1" in dev_labels

        # Main internal should not have NVM
        flash0 = [r for r in result if r[0] == "flash0"][0]
        flash0_labels = [p[0] for p in flash0[3]]
        assert "nvm" not in flash0_labels

        # Data flash should have NVM
        flash1 = [r for r in result if r[0] == "flash1"][0]
        flash1_labels = [p[0] for p in flash1[3]]
        assert "nvm" in flash1_labels

    def test_nvm_on_external_when_no_data_flash(self):
        """Without data flash, NVM goes on external (1 erase page)."""
        edt = _make_edt(
            _internal_flash(erase=4096),
            _external_flash(erase=4096),
        )
        result = plan_partitions(edt)
        ext = [r for r in result if r[0] == "mx25r"][0]
        ext_parts = {p[0]: p for p in ext[3]}
        assert "nvm" in ext_parts
        # NVM should be 1 erase page
        assert ext_parts["nvm"][3] == 4096

    def test_nvm_on_internal_when_internal_only(self):
        """Internal-only boards get NVM on main flash (1 erase page)."""
        edt = _make_edt(_internal_flash(erase=4096))
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        nvm = [p for p in parts if p[0] == "nvm"]
        assert len(nvm) == 1
        assert nvm[0][3] == 4096


# ── Unit tests: generate_partitions_dtsi ─────────────────────────────────


class TestGeneratePartitionsDtsi:
    def test_basic_output(self):
        planned = [
            (
                "flash0",
                512 * KB,
                4096,
                [
                    ("mcuboot", "boot_partition", 0, 64 * KB),
                    ("image-0", "slot0_partition", 64 * KB, 192 * KB),
                    ("storage", "storage_partition", 256 * KB, 4096),
                    ("filesystem", "littlefs_partition", 260 * KB, 252 * KB),
                ],
            )
        ]
        dtsi = generate_partitions_dtsi(planned)
        assert "&flash0 {" in dtsi
        assert 'compatible = "fixed-partitions"' in dtsi
        assert "boot_partition: partition@0" in dtsi
        assert 'label = "mcuboot"' in dtsi
        assert "DT_SIZE_K(64)" in dtsi
        assert "slot0_partition: partition@10000" in dtsi
        assert dtsi.count('compatible = "zephyr,mapped-partition"') == 4

    def test_mapped_only_for_soc_nv_flash_devices(self):
        """mapped_devices limits zephyr,mapped-partition to devices whose NVM
        node literally carries soc-nv-flash (Renesas RA code/data flash and
        QSPI/OSPI NORs do not, so their partitions stay plain fixed-partitions
        children -- Zephyr's gen_defines otherwise errors out)."""
        planned = [
            (
                "flash0",
                1 * MB,
                4096,
                [("mcuboot", "boot_partition", 0, 64 * KB)],
            ),
            (
                "mx25l25645g",
                16 * MB,
                4096,
                [("filesystem", "fatfs_partition", 0, 16 * MB)],
            ),
        ]
        dtsi = generate_partitions_dtsi(planned, {"flash0"})
        assert dtsi.count('compatible = "zephyr,mapped-partition"') == 1
        # The plain fixed-partitions style is emitted for the NOR device.
        assert "&mx25l25645g {" in dtsi
        assert 'label = "filesystem"' in dtsi

    def test_mapped_disabled_everywhere_with_empty_set(self):
        planned = [
            (
                "flash0",
                1 * MB,
                4096,
                [("mcuboot", "boot_partition", 0, 64 * KB)],
            )
        ]
        dtsi = generate_partitions_dtsi(planned, set())
        assert 'compatible = "zephyr,mapped-partition"' not in dtsi
        assert "boot_partition: partition@0" in dtsi

    def test_empty_parts_skipped(self):
        planned = [("flash0", 512 * KB, 4096, [])]
        dtsi = generate_partitions_dtsi(planned)
        assert dtsi == ""

    def test_two_devices(self):
        planned = [
            (
                "flash0",
                512 * KB,
                4096,
                [("mcuboot", "boot_partition", 0, 64 * KB)],
            ),
            (
                "mx25r",
                16 * MB,
                4096,
                [("filesystem", "fatfs_partition", 0, 16 * MB)],
            ),
        ]
        dtsi = generate_partitions_dtsi(planned)
        assert "&flash0 {" in dtsi
        assert "&mx25r {" in dtsi

    def test_predefined_partitions_skipped(self):
        """Predefined partitions should not appear in the generated dtsi."""
        predefined = {"mcuboot", "image-0", "image-1", "storage"}
        planned = [
            (
                "flash0",
                4 * MB,
                4096,
                [
                    ("mcuboot", "boot_partition", 0x2400, 0xDC00),
                    ("image-0", "slot0_partition", 0x10000, 512 * KB),
                    ("image-1", "slot1_partition", 0x90000, 512 * KB),
                    ("storage", "storage_partition", 0x110000, 32 * KB),
                    ("nvm", "nvm_partition", 0x118000, 4096),
                    ("filesystem", "littlefs_partition", 0x119000, 4 * MB - 0x119000),
                ],
                predefined,
            )
        ]
        dtsi = generate_partitions_dtsi(planned)
        # Only nvm and filesystem should appear
        assert 'label = "nvm"' in dtsi
        assert 'label = "filesystem"' in dtsi
        assert 'label = "mcuboot"' not in dtsi
        assert 'label = "image-0"' not in dtsi
        assert 'label = "image-1"' not in dtsi
        assert 'label = "storage"' not in dtsi
        # Should NOT emit compatible since partitions node already exists
        assert 'compatible = "fixed-partitions"' not in dtsi

    def test_predefined_all_skipped_means_no_output(self):
        """If all partitions are predefined, nothing should be generated."""
        predefined = {"mcuboot", "image-0"}
        planned = [
            (
                "flash0",
                1 * MB,
                4096,
                [
                    ("mcuboot", "boot_partition", 0, 64 * KB),
                    ("image-0", "slot0_partition", 64 * KB, 512 * KB),
                ],
                predefined,
            )
        ]
        dtsi = generate_partitions_dtsi(planned)
        assert dtsi == ""


# ── Unit tests: predefined mcuboot layout ────────────────────────────────


# DA14695-like: 4MB flash, mcuboot+slots+storage already defined upstream
_DA14695_PARTITIONS = [
    ("mcuboot", "boot_partition", 0x2400, 0xDC00),
    ("image-0", "slot0_partition", 0x10000, 512 * KB),
    ("image-1", "slot1_partition", 0x90000, 512 * KB),
    ("storage", "storage_partition", 0x110000, 32 * KB),
]


class TestPredefinedMcuboot:
    def test_has_predefined_mcuboot_detected(self):
        """A board with existing mcuboot partitions should be detected."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        assert _has_predefined_mcuboot(edt) is not None

    def test_no_predefined_mcuboot(self):
        """A board without existing partitions should not be detected."""
        edt = _make_edt(_internal_flash())
        assert _has_predefined_mcuboot(edt) is None

    def test_predefined_adds_nvm_and_filesystem(self):
        """Predefined layout should get nvm and filesystem added in free space."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        assert len(result) == 1
        _, _, _, parts, predefined = result[0]
        labels = [p[0] for p in parts]
        # Original partitions kept
        assert "mcuboot" in labels
        assert "image-0" in labels
        assert "image-1" in labels
        assert "storage" in labels
        # New partitions added
        assert "nvm" in labels
        assert "filesystem" in labels
        # The 512K upstream slots are grown (SLOT0_MIN_SIZE) and therefore
        # re-emitted; only the untouched boot partition stays predefined.
        # storage is NOT predefined: the fork always regenerates it
        # (STORAGE_SIZE) after the slots.
        assert predefined == {"mcuboot"}
        storage = [p for p in parts if p[0] == "storage"][0]
        assert storage[2] == 0x210000
        assert storage[3] == 32 * KB

    def test_predefined_preserves_original_offsets(self):
        """Un-touched upstream partitions keep their offsets and sizes."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, _, _, parts, _ = result[0]
        parts_by_label = {p[0]: p for p in parts}
        # The boot partition is below SLOT0_MIN_SIZE's reach and is kept.
        assert parts_by_label["mcuboot"][2] == 0x2400
        assert parts_by_label["mcuboot"][3] == 0xDC00
        # The slots are grown instead (see the growth test).

    def test_predefined_slot0_min_grows_slots(self):
        """Slots below SLOT0_MIN_SIZE are grown and the tail shifts."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, total_size, erase, parts, predefined = result[0]
        parts_by_label = {p[0]: p for p in parts}
        # The boot partition is untouched; slot0 grows from boot_end.
        assert parts_by_label["image-0"][2] == 0x10000
        assert parts_by_label["image-0"][3] == 1 * MB
        # slot1 grows to match and moves after the grown slot0.
        assert parts_by_label["image-1"][2] == 0x110000
        assert parts_by_label["image-1"][3] == 1 * MB
        # The tail shifts past slot1.
        assert parts_by_label["storage"][2] == 0x210000
        # The grown slots are re-emitted (they override the upstream sizes);
        # the untouched boot partition stays predefined.
        assert predefined == {"mcuboot"}

    def test_predefined_slot0_min_noop_when_big_enough(self):
        """Slots at or above SLOT0_MIN_SIZE keep the upstream geometry."""
        big = [
            ("mcuboot", "boot_partition", 0x2400, 0xDC00),
            ("image-0", "slot0_partition", 0x10000, 1536 * KB),
            ("image-1", "slot1_partition", 0x190000, 1536 * KB),
        ]
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, big)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, _, _, parts, predefined = result[0]
        parts_by_label = {p[0]: p for p in parts}
        assert parts_by_label["image-0"][2] == 0x10000
        assert parts_by_label["image-1"][2] == 0x190000
        assert predefined == {"mcuboot", "image-0", "image-1"}

    def test_predefined_slot0_min_skipped_when_tail_would_not_fit(self):
        """Growth is skipped (upstream sizes kept) when the tail cannot fit."""
        # A 1M flash cannot hold 1M slot0 + 1M slot1 plus the tail, so the
        # upstream geometry is kept (with a warning).
        small = [
            ("mcuboot", "boot_partition", 0x2400, 0xDC00),
            ("image-0", "slot0_partition", 0x10000, 512 * KB),
            ("image-1", "slot1_partition", 0x90000, 300 * KB),
        ]
        flash = _flash_with_partitions("flash0", 1 * MB, 4096, small)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, _, _, parts, predefined = result[0]
        parts_by_label = {p[0]: p for p in parts}
        # slot0/slot1 keep their upstream geometry.
        assert parts_by_label["image-0"][3] == 512 * KB
        assert predefined == {"mcuboot", "image-0", "image-1"}

    def test_predefined_new_partitions_dont_overlap(self):
        """New partitions should not overlap with existing ones."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, total_size, _, parts, _ = result[0]
        sorted_parts = sorted(parts, key=lambda p: p[2])
        for i in range(len(sorted_parts) - 1):
            end_i = sorted_parts[i][2] + sorted_parts[i][3]
            start_next = sorted_parts[i + 1][2]
            assert end_i <= start_next, (
                f"{sorted_parts[i][0]} ends at 0x{end_i:x} but "
                f"{sorted_parts[i + 1][0]} starts at 0x{start_next:x}"
            )
        last_end = sorted_parts[-1][2] + sorted_parts[-1][3]
        assert last_end <= total_size

    def test_predefined_new_partitions_aligned(self):
        """New partitions should be aligned to the erase size."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, _, erase_size, parts, predefined = result[0]
        for label, node_label, offset, size in parts:
            if label not in predefined:
                assert offset % erase_size == 0, f"{label} offset not aligned"
                assert size % erase_size == 0, f"{label} size not aligned"

    def test_predefined_filesystem_fills_remaining(self):
        """filesystem should use all remaining flash after nvm."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, total_size, _, parts, _ = result[0]
        fs = [p for p in parts if p[0] == "filesystem"][0]
        # filesystem should end at the flash boundary
        assert fs[2] + fs[3] == total_size

    def test_predefined_dtsi_only_has_new_partitions(self):
        """Generated dtsi only contains non-predefined (or grown) partitions."""
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, _DA14695_PARTITIONS)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        dtsi = generate_partitions_dtsi(result)
        assert 'label = "nvm"' in dtsi
        assert 'label = "filesystem"' in dtsi
        # The untouched boot partition stays predefined.
        assert 'label = "mcuboot"' not in dtsi
        # The 512K upstream slots are below SLOT0_MIN_SIZE, so the grown
        # slots are re-emitted to override the upstream sizes.
        assert 'label = "image-0"' in dtsi
        assert 'label = "image-1"' in dtsi

    def test_existing_app_partitions_replaced(self):
        """Existing filesystem/nvm partitions from a prior overlay should be replaced."""
        # Simulate da14695 with its overlay-added filesystem partition. The
        # board's fake EDT here has no USB node, so the regenerated node label
        # is littlefs_partition; the entry is dropped from the plan (via the
        # label = "filesystem" role in APP_PARTITION_LABELS) and re-added at a
        # new offset regardless of its previous one.
        parts_with_app = _DA14695_PARTITIONS + [
            ("filesystem", "littlefs_partition", 0x118000, 4 * MB - 0x118000),
        ]
        flash = _flash_with_partitions("flash0", 4 * MB, 4096, parts_with_app)
        edt = _make_edt(flash)
        result = plan_partitions(edt)
        _, _, _, parts, predefined = result[0]
        labels = [p[0] for p in parts]

        # filesystem and nvm should both be present (regenerated)
        assert "filesystem" in labels
        assert "nvm" in labels

        # They should NOT be in the predefined set
        assert "filesystem" not in predefined
        assert "nvm" not in predefined

        # The filesystem offset should differ (nvm now takes a page before it)
        fs = [p for p in parts if p[0] == "filesystem"][0]
        assert fs[2] != 0x118000  # not the old overlay offset
        # ...and the node label is regenerated under its new name
        assert fs[1] == "littlefs_partition"