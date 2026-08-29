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
import json
import os
import pathlib
import pickle
import shutil
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
KCONFIG_SYSBUILD = DTS_OUT_DIR / "Kconfig.sysbuild"
LOCK_PATH = DTS_OUT_DIR / "layouts.lock.json"
DOCS_PATH = DTS_OUT_DIR / "LAYOUTS.md"
DEFAULT_MCUBOOT_MODE = "single_app"


def _is_zephyr_tree(path):
    """True if ``path`` looks like a Zephyr source tree rather than a module dir."""
    return (path / "Kconfig.zephyr").is_file()


def _west_topdir():
    try:
        r = subprocess.run(
            ["west", "topdir"], capture_output=True, text=True, cwd=MODULE_DIR
        )
        if r.returncode == 0 and r.stdout.strip():
            return pathlib.Path(r.stdout.strip())
    except Exception:
        pass
    return None


def _find_zephyr_base():
    """Locate the Zephyr tree for the current west workspace."""
    env = os.environ.get("ZEPHYR_BASE")
    if env:
        return pathlib.Path(env)
    top = None
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
                top = _west_topdir()
                if top is not None:
                    base = top / base
            return base.resolve()
    except Exception:
        pass
    # `make workspace` does not set zephyr.base, and this repo has its own
    # zephyr/ directory holding the Zephyr *module* metadata -- which is not a
    # Zephyr tree. Look where the manifest actually puts it (path-prefix
    # "deps") before falling back, so the tool works right after `make
    # workspace` without ZEPHYR_BASE being exported by hand.
    if top is None:
        top = _west_topdir()
    for candidate in (
        (top / "deps" / "zephyr") if top is not None else None,
        MODULE_DIR.parent / "deps" / "zephyr",
    ):
        if candidate is not None and _is_zephyr_tree(candidate):
            return candidate.resolve()
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
        "mcuboot_mode": "<SB_CONFIG_MCUBOOT_MODE_* suffix, lowercase>",
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
            "mcuboot_mode": entry.get("mcuboot_mode", DEFAULT_MCUBOOT_MODE),
            # Standalone (UF2-native / XIP / not-yet-on-mcuboot) boards are
            # registered in boards.toml with mcuboot = false so the dts/ tree and
            # this registry stay in sync, but the planner does not own their
            # (hand-maintained, mapped-partition) layout.
            "mcuboot": entry.get("mcuboot", True),
        }
    return boards


def discover_boards():
    """Sorted list of partition keys declared in boards.toml."""
    return sorted(load_boards_manifest().keys())


# ── Devicetree build + load ───────────────────────────────────────────────


def cmake_only_build(board_id, build_dir, overlays=(), fatal=True):
    """Run west build --cmake-only to generate the resolved devicetree.

    The build only needs the resolved devicetree (``edt.pickle``), so it is run
    against Zephyr's ``hello_world`` sample as a throwaway application -- this
    repo's bootloader app (``boot/zephyr``) is not used because pulling it in
    would require this module to be wired up as a west module and would couple
    layout planning to the bootloader build. The board's own DTS (which is
    where the flash geometry lives) is what produces the edt, not the app.

    Without ``overlays`` the edt describes the board as Zephyr ships it, which
    is the input the planner needs. Pass overlays (this fork's
    ``dts/<vendor>/<board>.dtsi``) to instead resolve the layout this fork
    defines -- the same way ``make build`` applies it via
    EXTRA_DTC_OVERLAY_FILE. That is what ``--check`` inspects, so the tool
    validates the file it emits rather than only the board it planned from.

    With ``fatal`` false a failed build is returned as an error string instead
    of exiting, so a sweep over every board can report it and carry on.
    """
    # Start from an empty directory. Reusing one caches the CMake configuration
    # from whatever overlays it was created with, so a later run silently reads
    # back the earlier layout instead of the one it asked for.
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    sample_dir = ZEPHYR_BASE / "samples" / "hello_world"
    if not sample_dir.is_dir():
        print(
            f"Zephyr sample not found at {sample_dir}; is ZEPHYR_BASE set / "
            f"is the workspace fetched (make workspace)?",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = [
        "west",
        "build",
        "-b",
        board_id,
        "-d",
        str(build_dir),
        "--cmake-only",
        str(sample_dir),
    ]
    if overlays:
        cmd += ["--", "-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(str(o) for o in overlays)]
    result = subprocess.run(cmd, cwd=MODULE_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        if not fatal:
            return result.stderr
        print(f"Build failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return None


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


# ── Mapped-partition split (shared-flash boards) ───────────────────────────


def _looks_like_partition(node):
    """True if a node is itself a partition rather than an NVM device."""
    if "zephyr,mapped-partition" in getattr(node, "compats", []):
        return True
    parent = getattr(node, "parent", None)
    return parent is not None and getattr(parent, "name", "") == "partitions"


def _partition_device(part_node):
    """Walk up to the NVM device owning a partition, with the offset to it.

    Returns ``(device, base_offset)``. The device is the first ancestor with a
    ``reg`` that is not itself a partition: partitions nest (nRF54H20 puts
    crypto and ITS regions inside ``secure_storage_partition``, and the Nordic
    non-secure variants nest ``slot0_s``/``slot0_ns`` inside ``slot0``), and
    stopping at the first ancestor with a ``reg`` would take a partition for a
    flash device -- inventing a device with a made-up erase size and leaving
    the nested offsets relative to the wrong thing. ``base_offset`` is the sum
    of the enclosing partitions' offsets, so the result is an offset into the
    real device.
    """
    base_offset = 0
    node = part_node.parent
    while node is not None:
        reg = node.props.get("reg")
        if reg is not None:
            if not _looks_like_partition(node):
                return node, base_offset
            if len(reg.val) >= 2:
                base_offset += reg.val[0]
        node = node.parent
    return None, 0


def _find_code_partition_region(edt):
    """Find the upstream app code partition mcuboot should split.

    Some boards boot via mcuboot but share their single flash with a ROM loader
    / network processor that owns most of it (e.g. SiWx917: the M4 only owns a
    ``code_partition`` sub-region the ROM loader jumps to). The board's upstream
    DTS labels that region ``code_partition`` as a ``zephyr,mapped-partition``.
    This returns ``(dev_label, offset, size, node_labels, erase_size)`` for that
    partition, or ``None`` when there is no such partition (so this planning path
    is skipped for boards that own a whole device or already have a mcuboot
    layout upstream).
    """
    for node in edt.nodes:
        if "zephyr,mapped-partition" not in getattr(node, "compats", []):
            continue
        if "code_partition" not in getattr(node, "labels", []):
            continue
        reg = node.props.get("reg")
        if not reg or len(reg.val) < 2:
            continue
        dev = _partition_device(node)
        if dev is None:
            continue
        dev_label = dev.labels[0] if dev.labels else dev.name
        return (dev_label, reg.val[0], reg.val[1], list(node.labels), get_erase_size(dev))
    return None


def plan_code_partition_split(edt, flash_overrides=None):
    """Plan a single-app mcuboot layout within the board's upstream code region.

    For boards that share a flash with a ROM loader / network processor, the
    planner cannot plan the whole device (it would collide with the regions the
    loader owns). Instead it splits the board's existing ``code_partition``
    (a ``zephyr,mapped-partition``) into ``boot_partition`` + ``slot0_partition``
    and emits a mapped-partition overlay that deletes the upstream
    ``code_partition`` and replaces it. ``slot0_partition`` also carries the
    upstream ``code_partition`` label so the board's ``zephyr,code-partition =
    &code_partition`` chosen still resolves to it. All other upstream partitions
    are left untouched.

    Returns a dict describing the split, or ``None`` if this board does not use
    this pattern (a predefined mcuboot layout exists upstream, or there is no
    ``code_partition`` region to split).
    """
    if _has_predefined_mcuboot(edt, flash_overrides):
        return None
    region = _find_code_partition_region(edt)
    if region is None:
        return None
    dev_label, base, total, upstream_labels, erase = region
    boot_size = align_up(128 * KB, erase)
    slot0_offset = base + boot_size
    slot0_size = align_down(total - boot_size, erase)
    slot0_labels = ["slot0_partition"] + [
        l for l in upstream_labels if l != "slot0_partition"
    ]
    return {
        "dev_label": dev_label,
        "erase": erase,
        "region_size": total,
        "delete_labels": upstream_labels,
        "boot": ("mcuboot", "boot_partition", base, boot_size),
        "slot0": ("image-0", slot0_labels, slot0_offset, slot0_size),
    }


def generate_mapped_split_dtsi(plan):
    """Emit the mapped-partition overlay for a code_partition split."""
    dev = plan["dev_label"]
    boot_label, _boot_node, boot_off, boot_size = plan["boot"]
    slot0_label, slot0_labels, slot0_off, slot0_size = plan["slot0"]
    lines = []
    # Delete the upstream app code partition(s) before re-adding so the new
    # boot/slot0 nodes don't overlap the original region.
    for lbl in plan["delete_labels"]:
        lines.append(f"/delete-node/ &{lbl};")
    lines.append("")
    lines.append(f"&{dev} {{")
    lines.append("\tpartitions {")
    lines.append(f"\t\tboot_partition: partition@{boot_off:x} {{")
    lines.append('\t\t\tcompatible = "zephyr,mapped-partition";')
    lines.append(f'\t\t\tlabel = "{boot_label}";')
    lines.append(f"\t\t\treg = <0x{boot_off:x} {format_dt_size(boot_size)}>;")
    lines.append("\t\t};")
    lines.append("")
    label_decls = " ".join(f"{l}:" for l in slot0_labels)
    lines.append(f"\t\t{label_decls} partition@{slot0_off:x} {{")
    lines.append('\t\t\tcompatible = "zephyr,mapped-partition";')
    lines.append(f'\t\t\tlabel = "{slot0_label}";')
    lines.append(f"\t\t\treg = <0x{slot0_off:x} {format_dt_size(slot0_size)}>;")
    lines.append("\t\t};")
    lines.append("\t};")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


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


def discover_layouts():
    """Map partition key -> dts-relative path for every layout in dts/.

    The key is the dtsi stem (the Zephyr board name); the vendor directory only
    mirrors the Zephyr board folders, so keys must be unique across vendors.
    """
    layouts = {}
    for dtsi in sorted(DTS_OUT_DIR.glob("*/*.dtsi"), key=lambda p: (p.stem, p.parent.name)):
        key = dtsi.stem
        rel = f"{dtsi.parent.name}/{dtsi.name}"
        if key in layouts:
            raise SystemExit(
                f"Duplicate layout for '{key}': {layouts[key]} and {rel}. "
                "Board names must be unique across vendor directories."
            )
        layouts[key] = rel
    return layouts


def gen_mcuboot_boards_cmake():
    """Write dts/mcuboot_boards.cmake, the index of the layouts in dts/.

    The generated file is the fork's machine-readable answer to the two
    questions an application's sysbuild has: "where is this board's layout?"
    (MCUBOOT_LAYOUT_<board>, discovered from the dts/ tree) and "does it boot
    via mcuboot?" (MCUBOOT_BOARDS, the boards.toml entries with mcuboot = true,
    which lists exactly the boards the planner produces fixed-partitions mcuboot
    layouts for). Standalone boards are registered in boards.toml with
    mcuboot = false; they get a MCUBOOT_LAYOUT_<board> entry (so applications
    never have to guess a path or glob the vendor directory) but are kept out of
    MCUBOOT_BOARDS, since their layout is hand-maintained and they do not boot
    via mcuboot.
    """
    mcuboot_keys = sorted(k for k, v in load_boards_manifest().items() if v["mcuboot"])
    layouts = discover_layouts()

    missing = [k for k in mcuboot_keys if k not in layouts]
    if missing:
        print(
            f"  Warning: boards.toml lists {', '.join(missing)} but no dtsi exists yet;"
            " run --fix for them.",
            file=sys.stderr,
        )

    lines = [
        "# Auto-generated by tools/partition_layout.py --gen-list.",
        "# Do not edit by hand; add/remove boards in tools/boards.toml or",
        "# add/remove a dts/<vendor>/<board>.dtsi, then re-run",
        "# `python3 tools/partition_layout.py --gen-list`.",
        "#",
        "# MCUBOOT_LAYOUT_<board>  path to that board's self-contained layout overlay.",
        "# MCUBOOT_LAYOUT_BOARDS   every board with a layout here (every dtsi in dts/).",
        "# MCUBOOT_BOARDS          the subset that boots via mcuboot (Adaboot): the",
        "#                         boards.toml entries with mcuboot = true (default).",
        "#                         sysbuild builds a mcuboot image for these and",
        "#                         applies the layout to both images. Boards with a",
        "#                         layout here but not in this list (mcuboot = false)",
        "#                         run standalone -- the layout goes to the app only.",
        "",
    ]
    for key, rel in layouts.items():
        lines.append(
            f'set(MCUBOOT_LAYOUT_{key} "${{CMAKE_CURRENT_LIST_DIR}}/{rel}"'
            f' CACHE INTERNAL "{key} partition layout")'
        )
    lines.append("")
    lines.append("set(MCUBOOT_LAYOUT_BOARDS")
    lines.extend(f"    {k}" for k in layouts)
    lines.append('    CACHE INTERNAL "Boards with a partition layout in this fork"')
    lines.append(")")
    lines.append("")
    lines.append("set(MCUBOOT_BOARDS")
    lines.extend(f"    {k}" for k in mcuboot_keys)
    lines.append('    CACHE INTERNAL "Boards that boot via mcuboot (per this fork\'s boards.toml; mcuboot = true)"')
    lines.append(")")

    MCUBOOT_BOARDS_CMAKE.parent.mkdir(parents=True, exist_ok=True)
    MCUBOOT_BOARDS_CMAKE.write_text("\n".join(lines) + "\n")
    print(
        f"  Wrote {MCUBOOT_BOARDS_CMAKE.relative_to(MODULE_DIR.parent)}"
        f" ({len(layouts)} layouts, {len(mcuboot_keys)} via mcuboot)"
    )


def gen_kconfig_sysbuild():
    """Write dts/Kconfig.sysbuild, the bootloader policy for our boards.

    Whether a board boots via mcuboot -- and in which mode, unsigned -- follows
    from the layout this fork planned for it, so the fork declares it instead of
    every application repeating SB_CONFIG_* fragments. This file is wired in via
    ``sysbuild-kconfig`` in zephyr/module.yml, so sysbuild picks it up for any
    application that has this module in its west manifest.

    Zephyr sources a board's own Kconfig.sysbuild before module Kconfig, so an
    upstream board default still wins; an application can always override with
    an SB_CONFIG_ assignment in a sysbuild conf fragment.
    """
    boards = load_boards_manifest()
    keys = sorted(k for k, v in boards.items() if v["mcuboot"])

    modes = {}
    for key in keys:
        modes.setdefault(boards[key]["mcuboot_mode"], []).append(key)

    lines = [
        "# Auto-generated by tools/partition_layout.py --gen-list from boards.toml.",
        "# Do not edit by hand; change tools/boards.toml and re-run",
        "# `python3 tools/partition_layout.py --gen-list`.",
        "#",
        "# Bootloader policy for the boards whose partition layout this fork owns.",
        "# A board appears here exactly when its boards.toml entry has mcuboot = true",
        "# (the default), so the layout and the SB_CONFIG_BOOTLOADER_* settings can",
        "# never drift apart.",
        "#",
        "# These are defaults: a board's own Kconfig.sysbuild in the Zephyr tree is",
        "# parsed first and wins, and an application can override any of it with an",
        "# SB_CONFIG_ assignment in a sysbuild conf fragment.",
        "",
        "config ADABOOT_BOARD",
        "\tbool",
    ]
    for key in keys:
        lines.append(f'\tdefault y if $(BOARD) = "{key}"')
    # help must come last: everything indented below it is help text.
    lines += [
        "\thelp",
        "\t  This board's flash partition layout is owned by the Adaboot mcuboot",
        "\t  fork and was planned for booting via mcuboot.",
    ]

    lines += [
        "",
        "choice BOOTLOADER",
        "\tdefault BOOTLOADER_MCUBOOT if ADABOOT_BOARD",
        "endchoice",
        "",
        "if ADABOOT_BOARD && BOOTLOADER_MCUBOOT",
        "",
        "choice MCUBOOT_MODE",
    ]
    for mode in sorted(modes):
        symbol = f"MCUBOOT_MODE_{mode.upper()}"
        if mode == DEFAULT_MCUBOOT_MODE:
            continue
        for key in modes[mode]:
            lines.append(f'\tdefault {symbol} if $(BOARD) = "{key}"')
    lines.append(f"\tdefault MCUBOOT_MODE_{DEFAULT_MCUBOOT_MODE.upper()}")
    lines += [
        "endchoice",
        "",
        "# Adaboot is a UF2/serial-recovery bootloader, not a secure boot chain.",
        "choice BOOT_SIGNATURE_TYPE",
        "\tdefault BOOT_SIGNATURE_TYPE_NONE",
        "endchoice",
        "",
        "endif # ADABOOT_BOARD && BOOTLOADER_MCUBOOT",
    ]

    KCONFIG_SYSBUILD.parent.mkdir(parents=True, exist_ok=True)
    KCONFIG_SYSBUILD.write_text("\n".join(lines) + "\n")
    modes_desc = ", ".join(f"{len(v)} {k}" for k, v in sorted(modes.items()))
    print(f"  Wrote {KCONFIG_SYSBUILD.relative_to(MODULE_DIR.parent)} ({modes_desc})")


# ── Checking ─────────────────────────────────────────────────────────────


def _is_partition_child(node):
    """True for any node sitting under a ``partitions`` grouping node.

    Membership is decided by position, not by ``compatible``. A partition
    carries ``zephyr,mapped-partition`` or sits under a ``fixed-partitions``
    parent in the common case, but an overlay may add one with no compatible at
    all -- the node is still a partition, still occupies the space, and is
    still what the layout means to describe. Keying off the compatible would
    make those invisible here, which is the opposite of what a layout check is
    for.
    """
    parent = getattr(node, "parent", None)
    return parent is not None and getattr(parent, "name", "") == "partitions"


def iter_layout_partitions(edt):
    """Yield ``(node, device, offset, size, mapped)`` for every partition.

    ``offset``/``size`` come from the node's own ``reg`` (an offset within the
    NVM device). ``mapped`` marks ``zephyr,mapped-partition`` nodes, whose
    ``node.regs[0].addr`` is an address in the SoC's address space that the
    caller can check. Partitions under the older ``fixed-partitions`` are
    addressed by offset through the flash API, so they carry no address to
    verify -- but their geometry is still worth checking, and most layouts this
    fork emits use that binding.
    """
    for node in edt.nodes:
        mapped = "zephyr,mapped-partition" in getattr(node, "compats", [])
        if not mapped and not _is_partition_child(node):
            continue
        reg = node.props.get("reg")
        if not reg or len(reg.val) < 2:
            continue
        dev, base_offset = _partition_device(node)
        if dev is None:
            continue
        if mapped and (
            not getattr(node, "regs", None) or not getattr(dev, "regs", None)
        ):
            continue
        yield node, dev, reg.val[0] + base_offset, reg.val[1], mapped


def device_base(dev):
    """Base address of an NVM device, or None when it has none.

    Bus-attached flash (SPI/QSPI/OSPI NOR, NAND) carries a chip select in
    ``reg`` -- ``mx25lm51245`` is ``reg = <0>``, ``w25n01gv`` is ``reg = <2>``
    -- and its parent bus has ``#size-cells = <0>``, so edtlib reports an
    address with no size and nothing is translated. Reading that as a base
    address would compare partition addresses against a chip select and print
    a translation complaint that is exactly backwards. A device is only
    memory-mapped if its register has a size.
    """
    regs = getattr(dev, "regs", None)
    if not regs or regs[0].size is None:
        return None
    return regs[0].addr


def unplaceable_partitions(edt):
    """Partitions that declare no usable ``reg``.

    They occupy space and carry a role, but nothing can say where. Skipping
    them quietly is how a board that has an ``image-1`` gets reported as
    missing one, so they are surfaced instead.
    """
    out = []
    for node in edt.nodes:
        if not (
            "zephyr,mapped-partition" in getattr(node, "compats", [])
            or _is_partition_child(node)
        ):
            continue
        reg = node.props.get("reg")
        if not reg or len(reg.val) < 2:
            out.append(node.labels[0] if node.labels else node.name)
    return out


def check_layout(edt):
    """Validate a resolved layout, returning a list of problem strings.

    This runs against an edt built with the fork's dtsi applied, so it inspects
    the layout as the bootloader and application actually see it. It reports:

    * partitions whose resolved address is not ``device base + offset``, which
      means address translation is broken somewhere between the partition and
      the NVM device. The usual cause is a ``partitions`` node that was rebuilt
      by an overlay without re-declaring ``ranges;``: devicetree then treats the
      child addresses as unmapped rather than as offsets into the device, and
      the partition silently resolves to a bare offset. Nothing warns, but
      anything keying off the address (linker offsets, SoC Kconfig that matches
      a known load address) then sees the wrong value.
    * partitions that overlap, or extend past the end of their device. This part
      covers ``fixed-partitions`` too: they carry no address to verify, but most
      layouts this fork emits use that binding, so restricting the whole check
      to mapped partitions would leave those boards passing without anything
      having been looked at.

    Erase-page alignment is deliberately not checked here: it is a property of
    the plan rather than of the mapping, ``render_detail_lines`` already flags
    it, and some layouts violate it on purpose (RP2040 puts its code partition
    at 0x100, right behind the 256-byte second stage bootloader).
    """
    problems = [
        f"{label}: partition has no usable reg, so nothing can place it"
        for label in unplaceable_partitions(edt)
    ]
    by_device = {}
    for node, dev, offset, size, mapped in iter_layout_partitions(edt):
        label = node.labels[0] if node.labels else node.name
        dev_label = dev.labels[0] if dev.labels else dev.name
        total = get_total_size(dev)
        if mapped:
            # Test the resolved address against the device's own window rather
            # than against base + reg. Both forms are in use: a partition reg is
            # usually an offset, but an overlay may instead write the absolute
            # address and leave the partitions node without ranges, which
            # resolves to the same correct address. What is never right is a
            # partition resolving outside the device it lives in -- exactly what
            # a rebuilt partitions node missing ranges; produces, since the
            # offset is then left untranslated.
            base = device_base(dev)
            actual = node.regs[0].addr
            if base is None:
                by_device.setdefault(dev_label, (dev, total, []))[2].append(
                    (label, offset, size)
                )
                continue
            if total and not (base <= actual < base + total):
                problems.append(
                    f"{label}: resolves to 0x{actual:x}, outside {dev_label} "
                    f"(0x{base:x}-0x{base + total:x}) -- address translation is "
                    f"broken; does the partitions node declare ranges;?"
                )
            # Geometry below is compared in offsets from the device base, so a
            # partition declared either way lands in the same space. When the
            # translation is broken the difference is meaningless (and often
            # negative), so keep the declared offset instead of publishing an
            # address like 0x-10000000.
            translated = actual - base
            if translated >= 0 and (not total or translated < total):
                offset = translated
        by_device.setdefault(dev_label, (dev, total, []))[2].append((label, offset, size))

    for dev_label, (dev, total, parts) in sorted(by_device.items()):
        ordered = sorted(parts, key=lambda p: p[1])
        for label, offset, size in ordered:
            if total and offset + size > total:
                problems.append(
                    f"{label}: ends at 0x{offset + size:x}, past the end of "
                    f"{dev_label} (0x{total:x})"
                )
        # Compare against a running high-water mark rather than the neighbour:
        # a partition spanning several later ones only ever overlaps its
        # immediate successor in a pairwise walk.
        high_label, high_end = None, 0
        for label, offset, size in ordered:
            if offset < high_end:
                problems.append(
                    f"{high_label} (ends 0x{high_end:x}) overlaps "
                    f"{label} (starts 0x{offset:x}) on {dev_label}"
                )
            if offset + size > high_end:
                high_label, high_end = label, offset + size
    return problems


def resolved_layout(edt, erase_overrides=None):
    """Describe a resolved layout as ``[(dev_label, total, erase, parts)]``.

    ``parts`` is ``(label, node_label, offset, size)`` -- the shape the
    rendering helpers take -- with offsets normalised to the device base, so a
    partition written as an offset and one written as an absolute address are
    described identically. This is the common ground the check, the lock file
    and the generated docs all work from.
    """
    erase_overrides = erase_overrides or {}
    devices = {}
    for node, dev, offset, size, mapped in iter_layout_partitions(edt):
        dev_label = dev.labels[0] if dev.labels else dev.name
        node_label = node.labels[0] if node.labels else node.name
        label_prop = node.props.get("label")
        label = label_prop.val if label_prop else node_label
        if mapped:
            offset = node.regs[0].addr - dev.regs[0].addr
        if dev_label not in devices:
            erase = erase_overrides.get(dev_label) or get_erase_size(dev)
            devices[dev_label] = (get_total_size(dev), erase, [])
        devices[dev_label][2].append((label, node_label, offset, size))
    return [
        (dev_label, total, erase, sorted(parts, key=lambda p: p[2]))
        for dev_label, (total, erase, parts) in sorted(devices.items())
    ]


def layout_fingerprint(devices):
    """Reduce a resolved layout to the JSON the lock file stores."""
    return {
        dev_label: {
            "size": total,
            "partitions": [
                {"node": node_label, "label": label, "offset": offset, "size": size}
                # keyed on the role label below, which survives a DTS adding or
                # reordering node labels the way node labels do not
                for label, node_label, offset, size in parts
            ],
        }
        for dev_label, total, _erase, parts in devices
    }


def compare_fingerprint(locked, current):
    """Diff a locked layout against the current one.

    Returns ``(breaking, additions)``. A partition that moved, shrank, grew or
    disappeared is breaking: a device already in the field has its filesystem
    (or its settings, or its bonding keys) at the locked address, and shifting
    that is what silently destroys someone's CIRCUITPY drive on the next
    update. A partition appearing in free space takes nothing away from the
    existing ones, so it is reported without failing.
    """
    breaking = []
    additions = []
    for dev_label, locked_dev in sorted(locked.items()):
        current_dev = current.get(dev_label)
        if current_dev is None:
            breaking.append(f"{dev_label}: device is gone from the layout")
            continue
        if locked_dev.get("size") and current_dev.get("size") != locked_dev["size"]:
            breaking.append(
                f"{dev_label}: device size changed from "
                f"{format_size(locked_dev['size'])} to {format_size(current_dev['size'])}"
            )
        def key(part):
            return part.get("label") or part["node"]

        locked_parts = {key(p): p for p in locked_dev.get("partitions", [])}
        current_parts = {key(p): p for p in current_dev.get("partitions", [])}
        for node_label, locked_part in sorted(locked_parts.items()):
            current_part = current_parts.get(node_label)
            if current_part is None:
                breaking.append(f"{dev_label}/{node_label}: partition removed")
                continue
            if current_part["offset"] != locked_part["offset"]:
                breaking.append(
                    f"{dev_label}/{node_label}: moved from 0x{locked_part['offset']:x} "
                    f"to 0x{current_part['offset']:x}"
                )
            if current_part["size"] != locked_part["size"]:
                breaking.append(
                    f"{dev_label}/{node_label}: resized from "
                    f"{format_size(locked_part['size'])} to {format_size(current_part['size'])}"
                )
        for node_label in sorted(set(current_parts) - set(locked_parts)):
            part = current_parts[node_label]
            additions.append(
                f"{dev_label}/{node_label}: new partition at 0x{part['offset']:x} "
                f"({format_size(part['size'])})"
            )
    for dev_label in sorted(set(current) - set(locked)):
        additions.append(f"{dev_label}: new device in the layout")
    return breaking, additions


def load_lock():
    """Read the layout lock, or ``None`` when the layout has not been locked."""
    if not LOCK_PATH.is_file():
        return None
    try:
        with open(LOCK_PATH) as f:
            boards = json.load(f).get("boards")
    except (json.JSONDecodeError, AttributeError, OSError) as err:
        print(f"{LOCK_PATH.name} is unreadable ({err}); refusing to skip the "
              "lock check silently.", file=sys.stderr)
        sys.exit(1)
    if not isinstance(boards, dict):
        print(f"{LOCK_PATH.name} has no usable 'boards' object.", file=sys.stderr)
        sys.exit(1)
    return boards


def write_lock(boards):
    """Write the layout lock, sorted so diffs stay readable."""
    payload = {
        "_comment": (
            "Locked flash layouts. Applications place their filesystem and "
            "settings at these addresses, so moving or shrinking a partition "
            "breaks devices already in the field. partition_layout.py --check "
            "fails on any such change; re-run --lock to accept one deliberately."
        ),
        "boards": boards,
    }
    LOCK_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def render_board_markdown(board_key, board_id, devices):
    """Render one board's flash map as markdown, reusing the ASCII renderer."""
    lines = [f"## {board_key}", "", f"Zephyr board target: `{board_id}`", ""]
    for dev_label, total, erase, parts in devices:
        lines.append(f"### {dev_label} -- {format_size(total)}, {format_size(erase)} erase page")
        lines.append("")
        lines.append("```")
        lines.append(render_bar(parts, total))
        lines.extend(render_detail_lines(parts, total, erase))
        lines.append("```")
        lines.append("")
    return lines


def check_roles(devices, mcuboot=True):
    """Check that the layout provides the roles the standard scheme promises.

    A layout can resolve perfectly and still not do its job: if the overlay's
    partitions never made it into the devicetree, the board simply has no
    filesystem, and nothing says so. The point of this fork is that every board
    ends up with the same roles so an application can count on them, which
    makes a missing one a defect rather than a variation.

    ``image-1`` is deliberately not required -- a single-slot board is a valid
    configuration, and 4 of the boards here are built that way.
    """
    have = {label for _dev, _t, _e, parts in devices for label, *_ in parts}
    required = ["storage", "nvm", "filesystem"]
    required += ["mcuboot", "image-0"] if mcuboot else ["code-partition"]
    consequence = {
        "filesystem": "the board gets no CIRCUITPY drive",
        "nvm": "nothing backs the raw nvm API",
        "storage": "Zephyr has nowhere to keep settings, including BLE bonding keys",
    }
    problems = []
    for role in required:
        if role in have:
            continue
        why = consequence.get(role)
        problems.append(
            f"no {role!r} partition in the resolved layout"
            + (f" -- {why}" if why else "")
        )
    return problems


def partition_map(edt):
    """Map ``(device, node_label)`` to ``(offset, size)`` for every partition."""
    out = {}
    for node, dev, offset, size, mapped in iter_layout_partitions(edt):
        node_label = node.labels[0] if node.labels else node.name
        dev_label = dev.labels[0] if dev.labels else dev.name
        if mapped:
            offset = node.regs[0].addr - dev.regs[0].addr
        out[(dev_label, node_label)] = (offset, size)
    return out


def audit_dropped(baseline, resolved):
    """List regions the board's own DTS reserves that the layout no longer does.

    This answers "did we forget a partition?" -- the question that is otherwise
    only answerable by knowing every platform's quirks by heart. A board DTS
    reserves space for things the layout planner knows nothing about: radio
    coprocessor images, wifi firmware blobs, secure-storage and configuration
    regions a ROM or a secure element expects to find. Rebuilding the
    partitions node drops all of them, and nothing complains, because nothing
    downstream references them by label.

    Returns ``(unallocated, replaced)``. ``unallocated`` is the sharper signal:
    upstream reserved the region and now nothing claims it at all. ``replaced``
    means something else now sits there, which is expected for the app and boot
    slots this fork deliberately re-plans and worth a look for anything else.
    Both are for a human to judge, so neither is treated as a failure.
    """
    unallocated = []
    replaced = []
    for (dev_label, node_label), (offset, size) in sorted(baseline.items()):
        if (dev_label, node_label) in resolved:
            continue
        covering = sorted(
            label
            for (dev, label), (o, s) in resolved.items()
            if dev == dev_label and o < offset + size and offset < o + s
        )
        entry = (dev_label, node_label, offset, size, covering)
        (replaced if covering else unallocated).append(entry)
    return unallocated, replaced


def baseline_layout(board_key, board_id):
    """Resolve a board as Zephyr ships it, with none of this fork's overlays."""
    build_dir = MODULE_DIR / f"build-baseline-{board_key}"
    err = cmake_only_build(board_id, build_dir, overlays=(), fatal=False)
    if err is not None:
        return None
    return partition_map(load_edt(build_dir))


def resolve_board(board_key, vendor, board_id, mcuboot=True, erase_overrides=None):
    """Resolve a board's layout once, for whoever needs it.

    Returns ``(devices, edt, error)``: the resolved layout in the shape
    ``resolved_layout`` produces plus the devicetree it came from, or an error
    string when the board's dtsi is missing or does not resolve. ``--check``,
    ``--lock`` and ``--gen-docs`` all go through here so a sweep builds each
    board a single time.
    """
    if not vendor:
        return None, None, f"no vendor declared for {board_key} in boards.toml"
    dtsi_path = DTS_OUT_DIR / vendor / f"{board_key}.dtsi"
    if not dtsi_path.is_file():
        return None, None, f"no layout at {dtsi_path.relative_to(MODULE_DIR.parent)}"
    overlays = [dtsi_path]
    boot_overlay = MODULE_DIR.parent / "boot" / "zephyr" / "app.overlay"
    if mcuboot and boot_overlay.is_file():
        overlays.append(boot_overlay)
    build_dir = MODULE_DIR / f"build-check-{board_key}"
    err = cmake_only_build(board_id, build_dir, overlays=overlays, fatal=False)
    if err is not None:
        detail = next(
            (l.strip() for l in err.splitlines() if "devicetree error" in l),
            "see the build log",
        )
        return None, None, f"layout does not resolve: {detail}"
    edt = load_edt(build_dir)
    return resolved_layout(edt, erase_overrides), edt, None


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

    split = plan_code_partition_split(edt, flash_overrides)
    if split is not None:
        dev_label = split["dev_label"]
        erase = split["erase"]
        boot_label, _, boot_off, boot_size = split["boot"]
        slot0_label, _, slot0_off, slot0_size = split["slot0"]
        print(f"  {dev_label}  erase page: {format_size(erase)} (shared-flash split)")
        print(
            f"    {boot_label}: 0x{boot_off:x} + {format_dt_size(boot_size)}"
            f" ({format_size(boot_size)})"
        )
        print(
            f"    {slot0_label}: 0x{slot0_off:x} + {format_dt_size(slot0_size)}"
            f" ({format_size(slot0_size)}) [also: {', '.join(split['slot0'][1])}]"
        )
        print()
        content = generate_mapped_split_dtsi(split)
        dtsi_dir.mkdir(parents=True, exist_ok=True)
        dtsi_path.write_text(content)
        print(f"  Wrote {dtsi_path.relative_to(MODULE_DIR.parent)}")
        return

    planned = plan_partitions(edt, flash_overrides)
    if not planned:
        print("  No flash devices found to plan partitions for.")
        return

    # Show the planned layout
    for dev_label, total_size, erase_size, parts, *rest in planned:
        predefined = rest[0] if rest else set()
        kind_parts = [
            (lbl, s, e, i)
            for lbl, s, e, i in discover_all_flash(edt, flash_overrides)
            if lbl == dev_label
        ]
        kind = "internal" if kind_parts and kind_parts[0][3] else "external"
        print(
            f"  {dev_label} ({format_size(total_size)}, {kind})"
            f"  erase page: {format_size(erase_size)}"
        )
        for label, _node_label, offset, size in parts:
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
    parser.add_argument(
        "boards",
        nargs="*",
        metavar="BOARD",
        help="Partition keys (dtsi stems, e.g. nrf54l15dk). Several may be given. "
        "The read-only modes default to every declared board; --lock does not.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Write the partition portion to dts/<vendor>/<board>.dtsi",
    )
    parser.add_argument("--list", action="store_true", help="List declared boards")
    parser.add_argument(
        "--gen-list",
        action="store_true",
        help="Regenerate dts/mcuboot_boards.cmake and dts/Kconfig.sysbuild",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Resolve dts/<vendor>/<board>.dtsi and verify the layout it produces, "
        "including against the layout lock (all declared boards when no board is given)",
    )
    parser.add_argument(
        "--lock",
        action="store_true",
        help=f"Record the layouts of the named boards in {LOCK_PATH.name} as the "
        "compatible ones, replacing what was locked before",
    )
    parser.add_argument(
        "--gen-docs",
        action="store_true",
        help=f"Write the flash map of every board to {DOCS_PATH.name}",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Report regions the board's own DTS reserves that the layout drops "
        "(radio/coprocessor images, secure storage, vendor config) -- a report "
        "to read, not a pass/fail gate",
    )
    args = parser.parse_args()

    if args.audit:
        for other in ("check", "lock", "gen_docs", "fix", "gen_list"):
            if getattr(args, other, False):
                print(
                    f"--audit reports on the layout rather than acting on it; run it "
                    f"on its own, not with --{other.replace('_', '-')}.",
                    file=sys.stderr,
                )
                sys.exit(2)
        keys = args.boards or sorted(boards)
        unresolved = []
        for key in keys:
            entry = boards[key]
            print(f"\n=== {key} ({entry['board']})")
            baseline = baseline_layout(key, entry["board"])
            if baseline is None:
                print("  baseline does not resolve; skipped")
                unresolved.append(key)
                continue
            devices, edt, err = resolve_board(
                key, entry.get("vendor"), entry["board"], entry["mcuboot"]
            )
            if err is not None:
                print(f"  {err}")
                unresolved.append(key)
                continue
            unallocated, replaced = audit_dropped(baseline, partition_map(edt))
            if not unallocated and not replaced:
                print("  nothing the board reserved was dropped")
                continue
            for dev_label, label, offset, size, _ in unallocated:
                print(
                    f"  UNALLOCATED  {label} ({dev_label} 0x{offset:x}, "
                    f"{format_size(size)}) is no longer reserved by anything"
                )
            for dev_label, label, offset, size, covering in replaced:
                print(
                    f"  replaced     {label} ({dev_label} 0x{offset:x}, "
                    f"{format_size(size)}) now sits under {', '.join(covering)}"
                )
        if unresolved:
            # The report is only as complete as the boards it could resolve.
            print(
                f"\n{len(unresolved)} board(s) could not be audited: "
                f"{', '.join(unresolved)}",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.exit(0)

    if args.gen_list:
        gen_mcuboot_boards_cmake()
        gen_kconfig_sysbuild()
        sys.exit(0)

    unknown = [b for b in args.boards if b not in boards]
    if unknown:
        print(f"Unknown board(s): {', '.join(unknown)}", file=sys.stderr)
        print("\nDeclared boards:", file=sys.stderr)
        for key in sorted(boards):
            print(f"  {key}", file=sys.stderr)
        sys.exit(1)

    if args.lock and not args.boards:
        # Writing the lock is the one operation that changes what devices in
        # the field are built around, so the boards it touches are named rather
        # than swept up by a bare invocation.
        print(
            "--lock writes the compatibility contract, so it takes the boards to "
            "lock by name:\n    partition_layout.py --lock <board> [<board> ...]\n"
            "Use --check with no board to see which boards are not locked yet.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.lock and args.check:
        # One asks whether the layout still matches the lock, the other
        # declares the layout to be the new lock. Together the changed boards
        # -- the only ones a lock run is for -- would be rejected as drift and
        # left unlocked, which reads as success and does nothing.
        print(
            "--lock and --check contradict each other: --check verifies against the "
            "lock, --lock replaces it. Run --check first, then --lock to accept.",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.check or args.lock or args.gen_docs:
        keys = args.boards or sorted(boards)
        locked = load_lock() if args.check else None
        fingerprints = {}
        doc_lines = []
        failed = []
        drifted = {}
        for key in keys:
            entry = boards[key]
            overrides = {}
            ef = entry.get("external_flash")
            if ef:
                overrides[ef["label"]] = ef["erase_block_size"]
            print(f"Resolving {key} ({entry['board']})...")
            devices, edt, err = resolve_board(
                key, entry.get("vendor"), entry["board"], entry["mcuboot"], overrides
            )
            if err is not None:
                failed.append(key)
                print(f"  FAIL  {err}")
                continue
            board_docs = render_board_markdown(key, entry["board"], devices)
            # The layout is checked in every mode: a layout with a problem is
            # not one to lock in, and there is no point documenting it as
            # settled either.
            problems = check_layout(edt) + check_roles(devices, entry["mcuboot"])
            # Drift is kept apart from defects on purpose. A defect means the
            # layout is wrong and someone has to fix it; drift means the layout
            # is fine but no longer the one that was published, which is a
            # decision, not a repair. Reporting both as FAIL leaves the reader
            # to work out which of the two they are looking at.
            breaking = []
            if locked is not None and key in locked:
                breaking, additions = compare_fingerprint(
                    locked[key], layout_fingerprint(devices)
                )
                for addition in additions:
                    print(f"  note  {addition}")
            if problems:
                failed.append(key)
                for problem in problems:
                    print(f"  FAIL  {problem}")
            elif breaking:
                drifted[key] = breaking
                print("  CHANGED since it was locked:")
                for line in breaking:
                    print(f"      {line}")
            else:
                fingerprints[key] = layout_fingerprint(devices)
                # Only a layout that passed gets documented: a map drawn from a
                # broken one shows invented addresses.
                doc_lines.extend(board_docs)
                if locked is not None and key not in locked:
                    print("  ok (not in the layout lock)")
                else:
                    print("  ok")

        if args.lock:
            if not fingerprints:
                print(
                    "\nNothing to lock: no board resolved cleanly.", file=sys.stderr
                )
                sys.exit(1)
            existing = load_lock() or {}
            # Re-locking is how a deliberate layout change is accepted, so it
            # has to show what is being accepted. Overwriting the entry in
            # silence would let a partition move through the one gate meant to
            # catch exactly that.
            # Naming the boards is the deliberate act, so there is no second
            # confirmation flag. What a run replaces is still stated plainly,
            # and the lock file is reviewed as a diff like any other source.
            replaced = []
            for key, fingerprint in sorted(fingerprints.items()):
                if key not in existing:
                    print(f"  + {key}: locked for the first time")
                    continue
                breaking, additions = compare_fingerprint(existing[key], fingerprint)
                if not breaking and not additions:
                    continue
                replaced.append(key)
                print(f"  ! {key}: replacing a layout that was already locked")
                for line in breaking:
                    print(f"      breaking  {line}")
                for line in additions:
                    print(f"      added     {line}")
            existing.update(fingerprints)
            write_lock(existing)
            print(
                f"\nLocked {len(fingerprints)} board(s) in "
                f"{LOCK_PATH.relative_to(MODULE_DIR.parent)}"
            )
            if replaced:
                rel = LOCK_PATH.relative_to(MODULE_DIR.parent)
                print(
                    f"Replaced the published layout of {', '.join(replaced)}: devices "
                    f"already carrying the old one will not match it.\n"
                    f"The previous lock is the committed one -- `git diff {rel}` shows "
                    f"what this changed, `git checkout {rel}` puts it back."
                )
            if failed:
                print(
                    f"Left unlocked, they need fixing first: {', '.join(failed)}",
                    file=sys.stderr,
                )

        if args.gen_docs:
            header = [
                "# Flash layouts",
                "",
                "Generated by `python3 tools/partition_layout.py --gen-docs`; do not",
                "edit by hand. Every map here is read back out of the resolved",
                "devicetree, so it shows what a board actually gets rather than what",
                "the layout was meant to say.",
                "",
                "Legend: " + ", ".join(f"`{c}` {d}" for c, d in FILL.values() if d),
                "",
            ]
            if args.boards:
                print(
                    f"\nRefusing to write {DOCS_PATH.name} from a subset of boards: it "
                    "documents every board, and writing it here would drop the rest. "
                    "Run --gen-docs with no board named.",
                    file=sys.stderr,
                )
                sys.exit(1)
            DOCS_PATH.write_text("\n".join(header + doc_lines).rstrip() + "\n")
            print(f"\nWrote {DOCS_PATH.relative_to(MODULE_DIR.parent)}")

        # A failing board still fails, whichever mode asked for the sweep --
        # otherwise a CI line that regenerates docs is permanently green.
        status = 0
        if failed:
            print(
                f"\n{len(failed)} board(s) with layout problems, to fix: "
                f"{', '.join(failed)}"
            )
            status = 1
        if drifted:
            names = " ".join(sorted(drifted))
            print(
                f"\n{len(drifted)} board(s) no longer match the layout they were "
                f"locked with. Nothing is wrong with them -- decide whether the change "
                f"should reach devices already in the field.\n"
                f"  To publish it:   partition_layout.py --lock {names}\n"
                f"  To drop it:      revert the layout change"
            )
            status = 1
        if status:
            sys.exit(status)
        if args.lock or args.gen_docs:
            sys.exit(0)

        if failed:
            print(f"\n{len(failed)} board(s) with layout problems: {', '.join(failed)}")
            sys.exit(1)
        sys.exit(0)

    if args.list or not args.boards:
        print("Declared boards (* = standalone, no mcuboot):")
        for key, entry in sorted(boards.items()):
            marker = "" if entry["mcuboot"] else " *"
            print(f"  {key:24s} -> {entry['board']}{marker}")
        sys.exit(0 if args.list else 1)

    if len(args.boards) > 1 and not args.fix:
        print(
            "Showing a layout works on one board at a time; name just one.",
            file=sys.stderr,
        )
        sys.exit(2)

    board = args.boards[0]
    entry = boards[board]
    if not entry["mcuboot"]:
        print(
            f"{board} is a standalone board (mcuboot = false in boards.toml): "
            "its layout is a hand-maintained zephyr,mapped-partition overlay, not "
            f"something this planner emits. Edit dts/<vendor>/{board}.dtsi by hand.",
            file=sys.stderr,
        )
        sys.exit(1)

    board_id = entry["board"]
    vendor = entry.get("vendor")

    flash_overrides = {}
    ef = entry.get("external_flash")
    if ef:
        flash_overrides[ef["label"]] = {"erase_block_size": ef["erase_block_size"]}

    build_dir = MODULE_DIR / f"build-partitions-{board}"

    print(f"Running cmake-only build for {board_id}...")
    cmake_only_build(board_id, build_dir)

    edt = load_edt(build_dir)

    print(f"  {board} -- Partition Layout")
    print()

    if args.fix:
        fix_alignment(board, vendor, edt, flash_overrides)
    else:
        split = plan_code_partition_split(edt, flash_overrides)
        if split is not None:
            dev_label = split["dev_label"]
            erase = split["erase"]
            region_size = split["region_size"]
            boot_label, _, boot_off, boot_size = split["boot"]
            slot0_label, _, slot0_off, slot0_size = split["slot0"]
            print(f"  {dev_label} ({format_size(region_size)} code region, shared-flash split)  erase page: {format_size(erase)}")
            print(f"    {boot_label}: 0x{boot_off:x} + {format_size(boot_size)}")
            print(f"    {slot0_label}: 0x{slot0_off:x} + {format_size(slot0_size)}")
            print()
            print("  (upstream mapped-partitions kept; run --fix to write the overlay)")
            sys.exit(0)
        devices = extract_flash_devices(edt, flash_overrides)
        if not devices:
            print(f"No flash devices with partitions found for {board}")
            sys.exit(1)
        show_layout(devices)


if __name__ == "__main__":
    main()