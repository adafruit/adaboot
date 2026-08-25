#!/usr/bin/env python3
"""Plan, visualize, and generate the flash partition layout for a board.

This tool is owned by the mcuboot fork so that the memory map it defines is the
single source of truth shared by every application that boots via this
bootloader (CircuitPython, Arduino, Wippersnapper, ...). Applications consume
the generated ``dts/<vendor>/<board>.dtsi`` as a devicetree overlay applied to
both the bootloader image and the application image; they never own the
geometry themselves.

The tool runs a ``west build --cmake-only`` against the Zephyr tree in the
current west workspace to obtain the resolved devicetree (``edt.pickle``), then
parses it for flash devices, erase-page sizes, and any predefined partitions
before planning the full layout.

Usage (run from any west workspace that contains Zephyr and this module):

    python3 bootloader/mcuboot/tools/partition_layout.py <board>          # show layout
    python3 bootloader/mcuboot/tools/partition_layout.py --fix <board>    # write dtsi
    python3 bootloader/mcuboot/tools/partition_layout.py --list           # list boards

``<board>`` is the partition key (the dtsi filename stem, e.g. ``nrf54l15dk``),
which maps to a canonical Zephyr board id declared in ``boards.toml``.
"""

import argparse
import os
import pathlib
import pickle
import subprocess
import sys
import tomllib

KB = 1024
MB = 1024 * 1024

BAR_WIDTH = 72

MODULE_DIR = pathlib.Path(__file__).resolve().parent
MANIFEST_PATH = MODULE_DIR / "boards.toml"
DTS_OUT_DIR = MODULE_DIR.parent / "dts"
MCUBOOT_BOARDS_CMAKE = DTS_OUT_DIR / "mcuboot_boards.cmake"


def _find_zephyr_base():
    """Locate the Zephyr tree for the current west workspace."""
    env = os.environ.get("ZEPHYR_BASE")
    if env:
        return pathlib.Path(env)
    try:
        r = subprocess.run(
            ["west", "config", "zephyr.base"],
            capture_output=True,
            text=True,
            cwd=MODULE_DIR,
        )
        if r.returncode == 0 and r.stdout.strip():
            base = pathlib.Path(r.stdout.strip())
            if not base.is_absolute():
                top = subprocess.run(
                    ["west", "topdir"], capture_output=True, text=True, cwd=MODULE_DIR
                )
                if top.returncode == 0 and top.stdout.strip():
                    base = pathlib.Path(top.stdout.strip()) / base
            return base.resolve()
    except Exception:
        pass
    # Fallback (only used for unit tests that build fake paths from this value).
    return (MODULE_DIR.parent / "zephyr").resolve()


ZEPHYR_BASE = _find_zephyr_base()
EDT_MODULE = ZEPHYR_BASE / "scripts" / "dts" / "python-devicetree" / "src"

# Partition roles. These labels are the shared vocabulary across all apps that
# use this bootloader. The node labels (``*_partition``) are what applications
# reference via FIXED_PARTITION_ID(...).
FILL = {
    "mcuboot": ("B", "mcuboot"),
    "image-0": ("0", "image-0 (app slot)"),
    "image-1": ("1", "image-1 (update slot)"),
    "image-2": ("2", "image-2 (netcore)"),
    "storage": ("S", "storage (settings)"),
    "nvm": ("N", "nvm (non-volatile memory)"),
    "filesystem": ("#", "filesystem"),
    "nrf70_fw": ("F", "nrf70 firmware"),
    "free": (".", "free / unallocated"),
}

# Partitions the planner adds on top of any predefined mcuboot layout. These are
# stripped from upstream before re-adding so a board's own overlay (applied
# before this one) cannot leave stale copies.
APP_PARTITION_LABELS = {"filesystem", "nvm"}


# ── Board registry ────────────────────────────────────────────────────────


def load_boards_manifest():
    """Load the fork's board registry (boards.toml).

    Returns a dict keyed by partition key (dtsi stem) -> {
        "board": "<canonical Zephyr board id>",
        "external_flash": {"label": ..., "erase_block_size": ...} | None,
    }
    """
    if not MANIFEST_PATH.exists():
        return {}
    with open(MANIFEST_PATH, "rb") as f:
        data = tomllib.load(f)
    boards = {}
    for key, entry in data.get("boards", {}).items():
        boards[key] = {
            "board": entry["board"],
            "vendor": entry.get("vendor"),
            "external_flash": entry.get("external_flash"),
        }
    return boards


def discover_boards():
    """Sorted list of partition keys declared in boards.toml."""
    return sorted(load_boards_manifest().keys())


# ── Devicetree build + load ───────────────────────────────────────────────


def cmake_only_build(board_id, build_dir):
    """Run west build --cmake-only to generate the resolved devicetree."""
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "west",
        "build",
        "-b",
        board_id,
        "-d",
        str(build_dir),
        "--cmake-only",
    ]
    result = subprocess.run(cmd, cwd=MODULE_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)


def load_edt(build_dir):
    """Load the pickled EDT from a build directory."""
    sys.path.insert(0, str(EDT_MODULE))
    edt_path = build_dir / "zephyr" / "edt.pickle"
    if not edt_path.exists():
        print(f"edt.pickle not found at {edt_path}", file=sys.stderr)
        sys.exit(1)
    with open(edt_path, "rb") as f:
        return pickle.load(f)


# ── EDT parsing ───────────────────────────────────────────────────────────


def get_erase_size(node):
    """Get erase block size from a flash device node."""
    erase_size_prop = node.props.get("erase-block-size")
    if erase_size_prop:
        return erase_size_prop.val
    if "pages_layout" in node.children:
        erase_size = 0
        pl = node.children["pages_layout"]
        for layout_name, layout_node in pl.children.items():
            ps = layout_node.props.get("pages-size")
            if ps and ps.val > erase_size:
                erase_size = ps.val
        if erase_size > 0:
            return erase_size
    return 4096


def get_total_size(node):
    """Get total flash size from a device node."""
    size_prop = node.props.get("size")
    if size_prop:
        return size_prop.val // 8
    reg = node.props.get("reg")
    if reg and len(reg.val) >= 2:
        return reg.val[1]
    return 0


def is_internal_flash(node):
    """Check if a flash node is internal (SoC-defined) flash.

    SoC flash is defined in zephyr/dts/ (SoC dtsi files).
    Board flash is defined in zephyr/boards/ (board dts files).
    """
    filename = getattr(node, "filename", "")
    if not filename:
        return False
    path = pathlib.Path(filename)
    try:
        rel = path.relative_to(ZEPHYR_BASE)
        # SoC dtsi files live under dts/, board files under boards/.
        return rel.parts[0] == "dts"
    except ValueError:
        return False


def discover_all_flash(edt, flash_overrides=None):
    """Find all flash device nodes in the EDT, even those without partitions.

    flash_overrides is an optional dict mapping device labels to
    {"erase_block_size": int} for SPI NOR flash that doesn't expose it in DTS.

    Returns list of (dev_label, total_size, erase_size, is_internal).
    """
    if flash_overrides is None:
        flash_overrides = {}
    override_labels = set(flash_overrides.keys())

    devices = []
    for node in edt.nodes:
        dev_label = node.labels[0] if node.labels else node.name
        has_erase = node.props.get("erase-block-size") or "pages_layout" in getattr(
            node, "children", {}
        )
        has_override = dev_label in override_labels
        if not has_erase and not has_override:
            continue
        # Must have a size
        total_size = get_total_size(node)
        if total_size == 0:
            continue
        # Skip flash controllers (they wrap the actual flash node)
        if any("controller" in c for c in node.compats):
            continue
        if has_override:
            erase_size = flash_overrides[dev_label]["erase_block_size"]
        else:
            erase_size = get_erase_size(node)
        internal = is_internal_flash(node)
        devices.append((dev_label, total_size, erase_size, internal))
    return devices


def extract_flash_devices(edt, flash_overrides=None):
    """Extract all flash devices from the EDT, with their partitions (if any)."""
    # Build a map of partitions keyed by device label
    partitions_by_label = {}
    for node in edt.nodes:
        if not hasattr(node, "children") or "partitions" not in node.children:
            continue
        partitions_node = node.children["partitions"]
        if "fixed-partitions" not in getattr(partitions_node, "compats", []):
            continue
        dev_label = node.labels[0] if node.labels else node.name
        parts = []
        for pname, pnode in partitions_node.children.items():
            label_prop = pnode.props.get("label")
            reg_prop = pnode.props.get("reg")
            if label_prop and reg_prop:
                node_label = pnode.labels[0] if pnode.labels else pname
                parts.append((label_prop.val, node_label, reg_prop.val[0], reg_prop.val[1]))
        partitions_by_label[dev_label] = parts

    # Return all flash devices, attaching partitions where they exist
    devices = []
    for dev_label, total_size, erase_size, internal in discover_all_flash(edt, flash_overrides):
        parts = partitions_by_label.get(dev_label, [])
        devices.append((dev_label, total_size, erase_size, parts, internal))
    return devices


# ── Rendering ────────────────────────────────────────────────────────────


def make_partitions_with_gaps(parts, total_size):
    parts = sorted(parts, key=lambda p: p[2])  # sort by offset
    result = []
    cursor = 0
    for label, node_label, offset, size in parts:
        if offset > cursor:
            result.append(("free", cursor, offset - cursor))
        result.append((label, offset, size))
        cursor = offset + size
    if cursor < total_size:
        result.append(("free", cursor, total_size - cursor))
    return result


def format_size(size_bytes):
    if size_bytes >= MB:
        val = size_bytes / MB
        return f"{val:.1f} MB" if val != int(val) else f"{int(val)} MB"
    val = size_bytes / KB
    return f"{val:.0f} KB" if val == int(val) else f"{val:.1f} KB"


def render_bar(parts, total_size):
    full = make_partitions_with_gaps(parts, total_size)
    bar = []
    for label, offset, size in full:
        char = FILL.get(label, ("?", ""))[0]
        cols = max(1, round(size / total_size * BAR_WIDTH))
        bar.append(char * cols)
    line = "".join(bar)
    return line[:BAR_WIDTH].ljust(BAR_WIDTH)


def render_detail_lines(parts, total_size, erase_size):
    full = make_partitions_with_gaps(parts, total_size)
    lines = []
    for label, offset, size in full:
        if label == "free" and size < 1024:
            continue
        char = FILL.get(label, ("?", ""))[0]
        end = offset + size - 1
        pages = size / erase_size
        pages_str = f"{int(pages)} pgs" if pages == int(pages) else f"{pages:.1f} pgs"
        warn = ""
        if offset % erase_size != 0:
            warn += " !offset"
        if size % erase_size != 0:
            warn += " !size"
        lines.append(
            f"    [{char}] 0x{offset:08X}..0x{end:08X}  {format_size(size):>10s}  {pages_str:>8s}  {label}{warn}"
        )
    return lines


def show_layout(devices):
    print("  Legend:")
    for key, (char, desc) in FILL.items():
        print(f"    {char}  {desc}")
    print()

    for dev_label, total_size, erase_size, parts, internal in devices:
        kind = "internal" if internal else "external"
        name = f"{dev_label} ({format_size(total_size)}, {kind})"
        print(f"  {name}  erase page: {format_size(erase_size)}")
        bar = render_bar(parts, total_size)
        print(f"  |{bar}|")
        for line in render_detail_lines(parts, total_size, erase_size):
            print(f"  {line}")
        print()


# ── Fix mode ─────────────────────────────────────────────────────────────


def align_up(val, alignment):
    return ((val + alignment - 1) // alignment) * alignment


def align_down(val, alignment):
    return (val // alignment) * alignment


def format_dt_size(size_bytes):
    """Format a size as a DT_SIZE_K() or DT_SIZE_M() macro call."""
    if size_bytes % MB == 0:
        return f"DT_SIZE_M({size_bytes // MB})"
    if size_bytes % KB == 0:
        return f"DT_SIZE_K({size_bytes // KB})"
    return f"0x{size_bytes:x}"


def _has_predefined_mcuboot(edt, flash_overrides=None):
    """Check if any flash device already has mcuboot partitions defined upstream.

    Returns the existing partitions by device label if mcuboot is found, else None.
    """
    existing = extract_flash_devices(edt, flash_overrides)
    for dev_label, total_size, erase_size, parts, internal in existing:
        labels = {p[0] for p in parts}
        if "mcuboot" in labels and "image-0" in labels:
            return existing
    return None


def plan_partitions_predefined(edt, flash_overrides=None):
    """Plan partitions for boards with a predefined mcuboot layout.

    When external flash is available, slot1 is moved to external flash and
    slot0 is grown to fill the freed internal space.  Layout:
        Internal: mcuboot + slot0 (grown) + storage
        External: nvm + slot1 (slot0 size + 1 max-erase sector) + filesystem

    When no external flash is available, upstream partitions are kept and
    nvm/filesystem are added to the remaining free space on internal flash.

    Returns list of (dev_label, total_size, erase_size, [(label, node_label, offset, size)],
    predefined_labels) where predefined_labels is the set of partition labels from the
    upstream DTS (so the dtsi generator can skip them).
    """
    devices = extract_flash_devices(edt, flash_overrides)
    all_flash = discover_all_flash(edt, flash_overrides)
    data_flash = [(l, s, e) for l, s, e, i in all_flash if i and s < 64 * KB]
    external = [(l, s, e) for l, s, e, i in all_flash if not i and s >= 1 * MB]

    result = []
    for dev_label, total_size, erase_size, parts, internal in devices:
        existing_labels = {p[0] for p in parts}

        # If this device has no mcuboot partitions, pass it through unchanged
        # (or skip if empty).
        if "mcuboot" not in existing_labels:
            if parts:
                upstream = [p for p in parts if p[0] not in APP_PARTITION_LABELS]
                predefined = {p[0] for p in upstream}
                result.append((dev_label, total_size, erase_size, list(upstream), predefined))
            continue

        if external:
            # ── Move slot1 to external flash, grow slot0 ──
            ext_label, ext_size, ext_erase = external[0]
            max_erase = max(erase_size, ext_erase)

            # Find the predefined mcuboot partition and ensure it's at
            # least MCUBOOT_SIZE since we're regenerating all partitions.
            boot = [p for p in parts if p[0] == "mcuboot"][0]
            boot_size = max(boot[3], align_up(128 * KB, erase_size))
            boot_end = boot[2] + boot_size

            # Find the predefined storage partition (if any).
            storage_parts = [p for p in parts if p[0] == "storage"]
            storage_size = storage_parts[0][3] if storage_parts else erase_size

            # Place NVM on internal flash if internal is RRAM (good
            # endurance, small erase pages) or if data flash exists.
            nvm_on_internal = "rram" in dev_label or data_flash
            nvm_size = erase_size if nvm_on_internal and not data_flash else 0

            # Place storage + nvm at the end of internal flash, then grow
            # slot0 to fill the remaining space.
            tail_size = storage_size + nvm_size
            tail_start = align_down(total_size - tail_size, erase_size)
            slot0_offset = align_up(boot_end, erase_size)
            slot0_size = align_down(tail_start - slot0_offset, max_erase)

            int_parts = [
                ("mcuboot", "boot_partition", boot[2], boot_size),
                ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ]

            cursor = slot0_offset + slot0_size
            if nvm_size > 0:
                nvm_offset = align_up(cursor, erase_size)
                int_parts.append(("nvm", "nvm_partition", nvm_offset, nvm_size))
                cursor = nvm_offset + nvm_size

            storage_offset = align_up(cursor, erase_size)
            int_parts.append(("storage", "storage_partition", storage_offset, storage_size))

            # All partitions must be regenerated since the upstream partitions
            # node is deleted to allow slot0 to grow.
            result.append((dev_label, total_size, erase_size, int_parts, set()))

            # External: slot1 + filesystem
            # slot1 needs slot0_size + 1 max-erase sector for mcuboot
            # swap-using-offset scratch area.
            slot1_size = slot0_size + max_erase
            ext_parts = []
            cursor = 0

            if not nvm_on_internal and not data_flash:
                nvm_ext_size = ext_erase
                ext_parts.append(("nvm", "nvm_partition", cursor, nvm_ext_size))
                cursor = align_up(cursor + nvm_ext_size, ext_erase)

            slot1_offset = align_up(cursor, ext_erase)
            ext_parts.append(("image-1", "slot1_partition", slot1_offset, slot1_size))
            cursor = align_up(slot1_offset + slot1_size, ext_erase)

            fs_size = align_down(ext_size - cursor, ext_erase)
            if fs_size > 0:
                ext_parts.append(("filesystem", "filesystem_partition", cursor, fs_size))

            result.append((ext_label, ext_size, ext_erase, ext_parts, set()))
        else:
            # ── No external flash: keep upstream layout, add app partitions ──
            upstream = [p for p in parts if p[0] not in APP_PARTITION_LABELS]
            predefined = {p[0] for p in upstream}
            kept = sorted(upstream, key=lambda p: p[2])

            # Find the end of the last upstream partition.
            last_end = max(p[2] + p[3] for p in kept)

            # Add nvm + filesystem in the free space after the last partition.
            cursor = align_up(last_end, erase_size)

            if not data_flash:
                nvm_size = erase_size
                if cursor + nvm_size <= total_size:
                    kept.append(("nvm", "nvm_partition", cursor, nvm_size))
                    cursor = align_up(cursor + nvm_size, erase_size)

            fs_size = align_down(total_size - cursor, erase_size)
            if fs_size > 0:
                kept.append(("filesystem", "filesystem_partition", cursor, fs_size))

            result.append((dev_label, total_size, erase_size, kept, predefined))

    # If data flash exists, use it entirely for NVM.
    result_labels = {d[0] for d in result}
    if data_flash:
        for df_label, df_size, df_erase in data_flash:
            if df_label in result_labels:
                continue
            nvm_size = align_down(df_size, df_erase)
            if nvm_size > 0:
                result.append(
                    (df_label, df_size, df_erase, [("nvm", "nvm_partition", 0, nvm_size)], set())
                )

    return result


def plan_partitions(edt, flash_overrides=None):
    """Determine the partition layout based on available flash devices.

    If the board already has a predefined mcuboot layout (from the upstream
    board DTS), the existing partitions are kept and nvm/filesystem are added
    to the free space.

    Otherwise, partitions are planned from scratch:
    - If internal flash exists and there is also external flash:
        Internal: mcuboot + slot0 (fills remaining internal flash)
        External: storage (1 erase page) + slot1 (same size as slot0) + filesystem (rest)
    - If internal flash exists with no external flash:
        Internal: mcuboot + slot0 + storage (1 erase page) + filesystem (rest)
        No slot1 (no OTA update support).
    - If only external flash (XIP, e.g. FlexSPI):
        External: mcuboot + slot0 + slot1 (same size as slot0) + filesystem (rest)
        No storage partition.

    NVM partition placement:
    - If small internal data flash exists (< 64 KB, e.g. RA6/RA8 data flash),
      NVM uses the entire data flash device (ideal: small erase pages).
    - Otherwise, NVM gets 1 erase page on the same device as storage.

    Returns list of (dev_label, total_size, erase_size, [(label, node_label, offset, size)]).
    """
    # Check for predefined mcuboot layout first.
    if _has_predefined_mcuboot(edt, flash_overrides):
        return plan_partitions_predefined(edt, flash_overrides)

    all_flash = discover_all_flash(edt, flash_overrides)
    if not all_flash:
        return []

    # Separate small internal data flash (< 64 KB) from main internal flash.
    # Data flash (e.g. RA6/RA8 flash1) has small erase pages ideal for NVM.
    data_flash = [(l, s, e) for l, s, e, i in all_flash if i and s < 64 * KB]
    internal = [(l, s, e) for l, s, e, i in all_flash if i and s >= 64 * KB]
    # Filter out tiny external flash regions
    external = [(l, s, e) for l, s, e, i in all_flash if not i and s >= 1 * MB]

    MCUBOOT_SIZE = 128 * KB
    result = []

    if internal and external:
        # ── Internal + External ──
        int_label, int_size, int_erase = internal[0]
        ext_label, ext_size, ext_erase = external[0]

        # Both slots must be multiples of the largest erase size so
        # mcuboot's swap algorithm works across the two flash devices.
        max_erase = max(int_erase, ext_erase)

        # Internal: mcuboot + slot0
        boot_size = align_up(MCUBOOT_SIZE, int_erase)
        slot0_offset = boot_size
        slot0_size = align_down(int_size - slot0_offset, max_erase)

        int_parts = [
            ("mcuboot", "boot_partition", 0, boot_size),
            ("image-0", "slot0_partition", slot0_offset, slot0_size),
        ]

        # If there's leftover internal space after slot0 (due to max_erase
        # rounding), use it for storage instead of wasting external flash.
        int_leftover = int_size - (slot0_offset + slot0_size)
        storage_on_internal = int_leftover >= int_erase

        if storage_on_internal:
            storage_offset = slot0_offset + slot0_size
            storage_size = align_down(int_leftover, int_erase)
            int_parts.append(("storage", "storage_partition", storage_offset, storage_size))

        result.append((int_label, int_size, int_erase, int_parts, set()))

        # External: slot1 + filesystem (and storage/nvm if they didn't fit internally)
        # slot1 is slot0 + 1 sector of the largest erase size (mcuboot
        # swap-using-offset needs the extra sector as a scratch area).
        slot1_size = slot0_size + max_erase
        nvm_on_ext = not data_flash
        nvm_ext_size = ext_erase if nvm_on_ext else 0

        if storage_on_internal:
            nvm_ext_offset = 0
            slot1_offset = nvm_ext_size
        else:
            storage_size = ext_erase  # minimum: 1 erase page
            nvm_ext_offset = storage_size
            slot1_offset = nvm_ext_offset + nvm_ext_size

        slot1_offset = align_up(slot1_offset, ext_erase)
        fs_offset = align_up(slot1_offset + slot1_size, ext_erase)
        fs_size = align_down(ext_size - fs_offset, ext_erase)

        ext_parts = []
        if not storage_on_internal:
            ext_parts.append(("storage", "storage_partition", 0, storage_size))
        if nvm_on_ext:
            ext_parts.append(("nvm", "nvm_partition", nvm_ext_offset, nvm_ext_size))
        ext_parts += [
            ("image-1", "slot1_partition", slot1_offset, slot1_size),
            ("filesystem", "filesystem_partition", fs_offset, fs_size),
        ]
        result.append((ext_label, ext_size, ext_erase, ext_parts, set()))

    elif internal:
        # ── Internal only ──
        int_label, int_size, int_erase = internal[0]

        boot_size = align_up(MCUBOOT_SIZE, int_erase)
        slot0_offset = boot_size
        # Reserve space at the end for storage + nvm + filesystem.
        # Storage and NVM are each 1 erase page.
        storage_size = int_erase
        nvm_size = int_erase if not data_flash else 0
        # Give slot0 roughly half the remaining space.
        remaining = int_size - boot_size - storage_size - nvm_size
        slot0_size = align_down(remaining // 2, int_erase)
        storage_offset = slot0_offset + slot0_size
        nvm_offset = storage_offset + storage_size
        fs_offset = nvm_offset + nvm_size
        fs_size = align_down(int_size - fs_offset, int_erase)

        int_parts = [
            ("mcuboot", "boot_partition", 0, boot_size),
            ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ("storage", "storage_partition", storage_offset, storage_size),
        ]
        if nvm_size > 0:
            int_parts.append(("nvm", "nvm_partition", nvm_offset, nvm_size))
        int_parts.append(("filesystem", "filesystem_partition", fs_offset, fs_size))
        result.append((int_label, int_size, int_erase, int_parts, set()))

    elif external:
        # ── External only (XIP) ──
        ext_label, ext_size, ext_erase = external[0]

        boot_size = align_up(MCUBOOT_SIZE * 2, ext_erase)  # 128K for XIP boards
        slot0_offset = boot_size
        # slot0 and slot1 each get a portion; filesystem gets the rest.
        # Use roughly 1/8 of flash for each slot, rest for filesystem.
        slot_size = align_down(ext_size // 8, ext_erase)
        slot0_size = slot_size
        slot1_offset = slot0_offset + slot0_size
        slot1_size = slot_size
        storage_offset = slot1_offset + slot1_size
        storage_size = ext_erase  # minimum: 1 erase page
        nvm_offset = storage_offset + storage_size
        nvm_size = ext_erase
        fs_offset = nvm_offset + nvm_size
        fs_size = align_down(ext_size - fs_offset, ext_erase)

        ext_parts = [
            ("mcuboot", "boot_partition", 0, boot_size),
            ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ("image-1", "slot1_partition", slot1_offset, slot1_size),
            ("storage", "storage_partition", storage_offset, storage_size),
            ("nvm", "nvm_partition", nvm_offset, nvm_size),
            ("filesystem", "filesystem_partition", fs_offset, fs_size),
        ]
        result.append((ext_label, ext_size, ext_erase, ext_parts, set()))

    # If data flash exists, use it entirely for NVM.
    if data_flash:
        df_label, df_size, df_erase = data_flash[0]
        nvm_size = align_down(df_size, df_erase)
        if nvm_size > 0:
            result.append(
                (df_label, df_size, df_erase, [("nvm", "nvm_partition", 0, nvm_size)], set())
            )

    return result


def generate_partitions_dtsi(planned_devices):
    """Generate the partition portion of a partitions .dtsi file.

    Each entry in planned_devices is:
        (dev_label, total_size, erase_size, parts, predefined_labels)
    where predefined_labels is a set of partition labels already defined in the
    upstream DTS. Those partitions are skipped in the generated output.
    """
    lines = []

    for dev_label, total_size, erase_size, parts, *rest in planned_devices:
        predefined = rest[0] if rest else set()
        new_parts = [(l, nl, o, s) for l, nl, o, s in parts if l not in predefined]
        if not new_parts:
            continue

        lines.append(f"&{dev_label} {{")
        lines.append("\tpartitions {")
        # Only emit compatible/cells if the device has no predefined partitions
        # (i.e. the partitions node doesn't exist yet).
        if not predefined:
            lines.append('\t\tcompatible = "fixed-partitions";')
            lines.append("\t\t#address-cells = <1>;")
            lines.append("\t\t#size-cells = <1>;")

        for label, node_label, offset, size in new_parts:
            lines.append("")
            lines.append(f"\t\t{node_label}: partition@{offset:x} {{")
            lines.append(f'\t\t\tlabel = "{label}";')
            lines.append(f"\t\t\treg = <0x{offset:x} {format_dt_size(size)}>;")
            lines.append("\t\t};")

        lines.append("\t};")
        lines.append("};")
        lines.append("")

    return "\n".join(lines)


def gen_mcuboot_boards_cmake():
    """Write dts/mcuboot_boards.cmake from the boards.toml registry.

    boards.toml lists exactly the boards whose layout boots via mcuboot (the
    planner produces fixed-partitions mcuboot layouts for them). Boards that
    run standalone are hand-maintained and not in boards.toml. sysbuild includes
    the generated file to decide whether to build a mcuboot image.
    """
    boards = load_boards_manifest()
    keys = sorted(boards.keys())
    lines = [
        "# Auto-generated by tools/partition_layout.py --gen-list from boards.toml.",
        "# Do not edit by hand; add/remove mcuboot boards in tools/boards.toml and",
        "# re-run `python3 tools/partition_layout.py --gen-list`.",
        "#",
        "# Boards whose layout boots via mcuboot (Adaboot). sysbuild builds a mcuboot",
        "# image for these and applies their dts/<vendor>/<board>.dtsi overlay",
        "# to both the mcuboot and application images. Boards NOT in this list run",
        "# standalone (no mcuboot domain) -- their overlay is applied to the app only.",
        "set(MCUBOOT_BOARDS",
    ]
    for k in keys:
        lines.append(f"    {k}")
    lines.append('    CACHE INTERNAL "Boards that boot via mcuboot (per this fork\'s boards.toml)"')
    lines.append(")")
    MCUBOOT_BOARDS_CMAKE.parent.mkdir(parents=True, exist_ok=True)
    MCUBOOT_BOARDS_CMAKE.write_text("\n".join(lines) + "\n")
    print(f"  Wrote {MCUBOOT_BOARDS_CMAKE.relative_to(MODULE_DIR.parent)} ({len(keys)} boards)")


def fix_alignment(board_key, vendor, edt, flash_overrides=None):
    """Plan and write the partition portion to
    dts/<vendor>/<board_key>.dtsi.

    The generated file contains only the partition nodes (the part the planner
    can compute from the devicetree). Board-specific overlay glue -- enabling
    external flash devices, the boot-mode retention cell, chosen/aliases -- is
    hand-maintained alongside it in the same dtsi, so the file is a complete,
    self-contained overlay for both the bootloader and application images.
    """
    if not vendor:
        print(f"  No vendor declared for {board_key} in boards.toml; cannot place dtsi.", file=sys.stderr)
        return
    dtsi_dir = DTS_OUT_DIR / vendor
    dtsi_path = dtsi_dir / f"{board_key}.dtsi"

    planned = plan_partitions(edt, flash_overrides)
    if not planned:
        print("  No flash devices found to plan partitions for.")
        return

    # Show the planned layout
    for dev_label, total_size, erase_size, parts, *rest in planned:
        predefined = rest[0] if rest else set()
        kind_parts = [
            (l, s, e, i)
            for l, s, e, i in discover_all_flash(edt, flash_overrides)
            if l == dev_label
        ]
        kind = "internal" if kind_parts and kind_parts[0][3] else "external"
        print(
            f"  {dev_label} ({format_size(total_size)}, {kind})"
            f"  erase page: {format_size(erase_size)}"
        )
        for label, node_label, offset, size in parts:
            marker = " (predefined)" if label in predefined else ""
            print(
                f"    {label}: 0x{offset:x} + {format_dt_size(size)} ({format_size(size)}){marker}"
            )
        print()

    content = generate_partitions_dtsi(planned)

    dtsi_dir.mkdir(parents=True, exist_ok=True)
    dtsi_path.write_text(content)
    print(f"  Wrote {dtsi_path.relative_to(MODULE_DIR.parent)}")


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    boards = load_boards_manifest()

    parser = argparse.ArgumentParser(
        description="Visualize or fix the flash partition layout owned by this mcuboot fork."
    )
    parser.add_argument("board", nargs="?", help="Partition key (dtsi stem, e.g. nrf54l15dk)")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Write the partition portion to dts/<vendor>/<board>.dtsi",
    )
    parser.add_argument("--list", action="store_true", help="List declared boards")
    parser.add_argument(
        "--gen-list",
        action="store_true",
        help="Regenerate dts/mcuboot_boards.cmake from boards.toml",
    )
    args = parser.parse_args()

    if args.gen_list:
        gen_mcuboot_boards_cmake()
        sys.exit(0)

    if args.list or not args.board:
        print("Declared boards:")
        for key, entry in sorted(boards.items()):
            print(f"  {key:24s} -> {entry['board']}")
        sys.exit(0 if args.list else 1)

    if args.board not in boards:
        print(f"Unknown board: {args.board}", file=sys.stderr)
        print("\nDeclared boards:")
        for key in sorted(boards):
            print(f"  {key}")
        sys.exit(1)

    entry = boards[args.board]
    board_id = entry["board"]
    vendor = entry.get("vendor")

    flash_overrides = {}
    ef = entry.get("external_flash")
    if ef:
        flash_overrides[ef["label"]] = {"erase_block_size": ef["erase_block_size"]}

    build_dir = MODULE_DIR / f"build-partitions-{args.board}"

    print(f"Running cmake-only build for {board_id}...")
    cmake_only_build(board_id, build_dir)

    edt = load_edt(build_dir)

    print(f"  {args.board} -- Partition Layout")
    print()

    if args.fix:
        fix_alignment(args.board, vendor, edt, flash_overrides)
    else:
        devices = extract_flash_devices(edt, flash_overrides)
        if not devices:
            print(f"No flash devices with partitions found for {args.board}")
            sys.exit(1)
        show_layout(devices)


if __name__ == "__main__":
    main()