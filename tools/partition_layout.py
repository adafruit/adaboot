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

The layout is written as a pair of files under dts/<vendor>/:

    <board>.dtsi              hand-maintained board glue (device enablement,
                              delete-nodes, aliases/chosen); created by --fix
                              when missing, then never rewritten
    <board>-partitions.dtsi   the generated partition nodes (the memory map);
                              rewritten by every --fix

The main dtsi includes the partitions file, so it remains a single
self-contained overlay for both the bootloader and application images.

Usage (run from any west workspace that contains Zephyr and this module):

    python3 bootloader/mcuboot/tools/partition_layout.py <board>          # show layout
    python3 bootloader/mcuboot/tools/partition_layout.py --fix <board>    # write dtsi pair
    python3 bootloader/mcuboot/tools/partition_layout.py --list           # list boards

``<board>`` is the partition key (the dtsi filename stem, e.g. ``nrf54l15dk``),
which maps to a canonical Zephyr board id declared in ``boards.toml``.
"""

import argparse
import os
import pathlib
import pickle
import re
import subprocess
import sys
import tomllib

KB = 1024
MB = 1024 * 1024

# Storage (Zephyr settings: BLE bonding keys, etc.) partition size. The
# planner sizes every storage partition it adds at this many bytes, rounded
# up to the device's erase-block size (a predefined upstream storage
# partition keeps its own size).
STORAGE_SIZE = 32 * KB

BAR_WIDTH = 72

MODULE_DIR = pathlib.Path(__file__).resolve().parent
MANIFEST_PATH = MODULE_DIR / "boards.toml"
DTS_OUT_DIR = MODULE_DIR.parent / "dts"
MCUBOOT_BOARDS_CMAKE = DTS_OUT_DIR / "mcuboot_boards.cmake"
KCONFIG_SYSBUILD = DTS_OUT_DIR / "Kconfig.sysbuild"
BOARDS_MK = DTS_OUT_DIR / "boards.mk"
SECTORS_CONF_DIR = MODULE_DIR.parent / "conf"
DEFAULT_MCUBOOT_MODE = "single_app"


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
            # Plan a single-app layout (no image-1) even when the flash
            # geometry would allow a second slot: used for boards with no
            # wireless loading path, where the filesystem gets the space
            # instead (the fork's partitioning philosophy).
            "single_app": entry.get("single_app", False),
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


def cmake_only_build(board_id, build_dir, overlay=None):
    """Run west build --cmake-only to generate the resolved devicetree.

    The build only needs the resolved devicetree (``edt.pickle``), so it is run
    against Zephyr's ``hello_world`` sample as a throwaway application -- this
    repo's bootloader app (``boot/zephyr``) is not used because pulling it in
    would require this module to be wired up as a west module and would couple
    layout planning to the bootloader build. The board's own DTS (which is
    where the flash geometry lives) is what produces the edt, not the app.

    The fork's board overlay (dts/<vendor>/<key>.dtsi) is applied when given:
    some boards move the partitioned flash into a fork-added node (e.g. the
    nucleo_n657x0_q's XIP soc-nv-flash child of its XSPI NOR), and the layout
    must be planned against the overlay's flash geometry.
    """
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
    if overlay:
        cmd += ["--", f"-DEXTRA_DTC_OVERLAY_FILE={overlay}"]
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
        # Skip raw NAND: Zephyr exposes no plain flash driver for it, so it
        # cannot host slots, storage or the filesystem (boards use it via a
        # dedicated driver and its own upstream partition, e.g. rw612's
        # nand-storage).
        if any("nand" in c for c in node.compats):
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
        # Accept both the classic `compatible = "fixed-partitions"` grouping
        # and the newer bare `partitions { ranges; }` style (Nordic, NXP)
        # whose children are zephyr,mapped-partition nodes.
        has_reg_children = any("reg" in p.props for p in partitions_node.children.values())
        if "fixed-partitions" not in getattr(partitions_node, "compats", []) and not has_reg_children:
            continue
        dev_label = node.labels[0] if node.labels else node.name
        parts = []
        for pname, pnode in partitions_node.children.items():
            reg_prop = pnode.props.get("reg")
            if not reg_prop:
                continue
            label_prop = pnode.props.get("label")
            node_label = pnode.labels[0] if pnode.labels else pname
            # Partitions without a `label` property are keyed by their node
            # label (e.g. the nRF54H20 VPR code regions) so the planner can
            # see -- and preserve -- regions it does not manage.
            parts.append(
                (
                    label_prop.val if label_prop else node_label,
                    node_label,
                    reg_prop.val[0],
                    reg_prop.val[1],
                )
            )
        partitions_by_label[dev_label] = parts

    # Return all flash devices, attaching partitions where they exist
    devices = []
    for dev_label, total_size, erase_size, internal in discover_all_flash(edt, flash_overrides):
        parts = partitions_by_label.get(dev_label, [])
        devices.append((dev_label, total_size, erase_size, parts, internal))
    return devices


# Node labels of the partitions the planner owns and regenerates on every
# --fix. Any other partition node label found in a previously generated
# layout is carried forward unchanged: those are board-specific regions the
# generic layout must keep (e.g. the nRF54H20's SoC-referenced VPR code
# regions and its netcore firmware region, or nrf7002dk's nRF70 co-processor
# firmware region).
MANAGED_NODE_LABELS = {
    "boot_partition",
    "slot0_partition",
    "slot1_partition",
    "storage_partition",
    "nvm_partition",
    # The filesystem node label is fatfs_partition on boards with native USB
    # and littlefs_partition on the rest (see filesystem_node_label).
    "fatfs_partition",
    "littlefs_partition",
}


def parse_existing_partitions_dtsi(path):
    """Parse a previously generated <board>-partitions.dtsi.

    --fix rewrites that file, but some boards carry board-specific partitions
    the planner cannot derive (VPR code regions a SoC phandle points at, a
    netcore / co-processor firmware region). Those are seeded by hand once
    and must survive regeneration, so --fix reads the current file first and
    carries them forward: every partition whose node labels the planner does
    not manage is preserved at its current offset and size, and slot0 growth
    is capped so it does not clobber a carried partition that lived outside
    the previous slot0.

    Returns {dev_label: {"slot0": (offset, size) or None,
                         "carried": [(label_prop or None, node_label, offset, size)]}}.
    """
    result = {}
    path = pathlib.Path(path)
    if not path.exists():
        return result

    dev_re = re.compile(r"^&(\w+)\s*\{")
    part_re = re.compile(r"^\s+([\w:]+):\s*partition@([0-9a-f]+)\s*\{")
    label_re = re.compile(r'label\s*=\s*"([^"]+)"')
    reg_re = re.compile(r"reg\s*=\s*<\s*0x([0-9a-f]+)\s+([^>]+?)\s*>")

    def parse_size(text):
        text = text.strip()
        m = re.fullmatch(r"DT_SIZE_K\((\d+)\)", text)
        if m:
            return int(m.group(1)) * KB
        m = re.fullmatch(r"DT_SIZE_M\((\d+)\)", text)
        if m:
            return int(m.group(1)) * MB
        if text.startswith("0x"):
            return int(text, 16)
        return None

    current_dev = None
    pending = None  # (node_labels_str, label_prop or None, offset)
    pending_size = None

    def commit():
        nonlocal pending, pending_size
        if current_dev is None or pending is None or pending_size is None:
            return
        node_labels_str, label_prop, offset = pending
        pending = None
        size = pending_size
        pending_size = None
        labels = [lbl for lbl in re.split(r"\s*:\s*", node_labels_str) if lbl]
        dev = result.setdefault(current_dev, {"slot0": None, "carried": []})
        if any(lbl in MANAGED_NODE_LABELS for lbl in labels):
            if "slot0_partition" in labels:
                dev["slot0"] = (offset, size)
            return
        dev["carried"].append((label_prop, labels[0], offset, size))

    for line in path.read_text().splitlines():
        m = dev_re.match(line)
        if m:
            commit()
            current_dev = m.group(1)
            continue
        m = part_re.match(line)
        if m:
            commit()
            pending = (m.group(1), None, int(m.group(2), 16))
            pending_size = None
            continue
        if current_dev is not None and pending is not None:
            m = label_re.search(line)
            if m:
                pending = (pending[0], m.group(1), pending[2])
                continue
            m = reg_re.search(line)
            if m:
                pending_size = parse_size(m.group(2))
                commit()
    commit()
    return result


def glue_deleted_partition_devices(glue_path):
    """Device labels whose whole `partitions` node the hand-maintained glue
    deletes (`&dev { /delete-node/ partitions; };`).

    For those devices the generated file must be self-contained: it has to
    re-emit every partition (including the upstream boot/slot ones, since
    the glue removed them) instead of skipping predefined partitions.
    """
    result = set()
    path = pathlib.Path(glue_path)
    if not path.exists():
        return result
    text = path.read_text()
    for m in re.finditer(r"&(\w+)\s*\{", text):
        dev_label = m.group(1)
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block = text[m.end() : i]
        if re.search(r"/delete-node/\s*partitions\s*;", block):
            result.add(dev_label)
    return result


def mapped_partition_devices(edt):
    """Device node labels that can carry ``zephyr,mapped-partition`` children.

    Zephyr's gen_defines.py requires every ``zephyr,mapped-partition`` node to
    descend from an NVM memory node whose compatible list literally contains
    ``soc-nv-flash`` (the binding include alone is not enough), and
    ``DT_MTD_FROM_MAPPED_PARTITION`` resolves the flash device through that
    node's parent -- correct for a ``flash@0`` under a flash-controller device,
    wrong for a NOR hanging off a QSPI/OSPI bus. SoCs that spell their NVM nodes
    differently (Renesas RA's ``renesas,ra-nv-code-flash``, external NORs like
    ``renesas,ra-qspi-nor``) therefore cannot use mapped partitions; their
    partitions stay classic ``fixed-partitions`` children, which everything
    (PARTITION_ID(), flash_map, the bootloader's CMake) still supports.
    """
    result = set()
    for node in edt.nodes:
        if "soc-nv-flash" in getattr(node, "compats", []):
            result.update(node.labels or [node.name])
    return result


def has_native_usb(edt):
    """Whether the board's devicetree enables a native USB device controller.

    Zephyr's USB device stack binds to the node carrying the ``zephyr_udc0``
    label. A board with one can present the filesystem as a USB mass-storage
    drive (CIRCUITPY), which requires a PC-readable format (FAT); boards
    without one use littlefs instead.
    """
    for node in edt.nodes:
        if "zephyr_udc0" not in getattr(node, "labels", []):
            continue
        if getattr(node, "status", "okay") == "okay":
            return True
    return False


def filesystem_node_label(edt):
    """The filesystem partition's node label, chosen by native USB.

    ``fatfs_partition`` when the board has native USB (the partition is served
    over USB mass storage, so it must be FAT) and ``littlefs_partition`` when
    it does not. The ``label = "filesystem"`` role stays the same either way;
    only the node label applications reference changes.
    """
    return "fatfs_partition" if has_native_usb(edt) else "littlefs_partition"


def _swap_supported(edt):
    """Whether mcuboot's swap upgrade modes can run on this board.

    Swap writes the image trailer at the chosen flash's write-block
    alignment, and bootutil asserts MCUBOOT_BOOT_MAX_ALIGN is 8..32 bytes
    for swap modes. Boards whose chosen flash programs in bigger blocks
    (e.g. Renesas RA code flash: 128 bytes) cannot swap at all and get a
    single-app layout (slot0 only, no image-1).
    """
    chosen = getattr(edt, "chosen_node", None)
    node = chosen("zephyr,flash") if callable(chosen) else None
    if node is None:
        return True
    prop = node.props.get("write-block-size")
    if prop is None:
        return True
    # Like the Zephyr Kconfig default, the effective alignment is the max of
    # the write-block-size and 8; swap needs it to be 8..32 bytes.
    return max(prop.val, 8) <= 32


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


def plan_partitions_predefined(
    edt,
    flash_overrides=None,
    existing=None,
    deleted_devices=None,
    single_app=False,
    overwrite_only=False,
):
    """Plan partitions for boards with a predefined mcuboot layout.

    When the predefined mcuboot layout is on internal flash and separate
    external flash is available, slot1 is moved to external flash and slot0
    is grown to fill the freed internal space:
        Internal: mcuboot + slot0 (grown) + [nvm] + storage
        External: [nvm] + slot1 (slot0 size + 1 max-erase sector) + filesystem

    When there is no separate external flash -- or the mcuboot layout itself
    lives on the external (XIP) flash -- the upstream slots are kept as-is
    and the upstream storage partition is replaced by the fork's standard
    tail appended after them:
        storage (STORAGE_SIZE) + [nvm] + filesystem (rest)

    Partitions carried from the previously generated layout (board-specific
    regions the planner does not manage) are preserved, and slot0 growth is
    capped so a carried partition that lived outside the previous slot0
    (e.g. the nRF54H20 netcore firmware region) is never clobbered.

    Returns list of (dev_label, total_size, erase_size, [(label, node_label, offset, size)],
    predefined_labels) where predefined_labels is the set of partition labels from the
    upstream DTS (so the dtsi generator can skip them).
    """
    existing = existing or {}
    deleted_devices = deleted_devices or set()
    devices = extract_flash_devices(edt, flash_overrides)
    all_flash = discover_all_flash(edt, flash_overrides)
    data_flash = [(l, s, e) for l, s, e, i in all_flash if i and s < 64 * KB]
    external = [(l, s, e) for l, s, e, i in all_flash if not i and s >= 1 * MB]
    secondary_slot = not single_app and (_swap_supported(edt) or overwrite_only)
    fs_node_label = filesystem_node_label(edt)

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

        # Board-specific partitions carried from the previous generated
        # layout, plus where the previous slot0 ended (partitions carried
        # from inside it are allowed to stay overlapped; ones beyond it cap
        # the new slot0 so they are not clobbered).
        carry = existing.get(dev_label, {})
        carried = sorted(carry.get("carried", []), key=lambda c: c[2])
        old_slot0 = carry.get("slot0")

        # Separate external flash the slots can spill into. XIP boards boot
        # from the external flash itself, so it is not spare -- their
        # upstream layout is kept in place instead.
        grow_ext = (
            [(l, s, e) for l, s, e in external if l != dev_label]
            if secondary_slot
            else []
        )

        if grow_ext:
            # ── Move slot1 to external flash, grow slot0 ──
            ext_label, ext_size, ext_erase = grow_ext[0]
            max_erase = max(erase_size, ext_erase)

            # Find the predefined mcuboot partition and ensure it's at
            # least MCUBOOT_SIZE since we're regenerating all partitions.
            boot = [p for p in parts if p[0] == "mcuboot"][0]
            boot_size = max(boot[3], align_up(128 * KB, erase_size))
            boot_end = boot[2] + boot_size

            # Find the predefined storage partition (if any).
            storage_parts = [p for p in parts if p[0] == "storage"]
            storage_size = (
                storage_parts[0][3] if storage_parts else align_up(STORAGE_SIZE, erase_size)
            )

            # Place NVM on internal flash if internal is RRAM (good
            # endurance, small erase pages) or if data flash exists.
            nvm_on_internal = "rram" in dev_label or data_flash
            nvm_size = erase_size if nvm_on_internal and not data_flash else 0

            # Place storage + nvm at the end of internal flash, then grow
            # slot0 to fill the remaining space -- capped at the first
            # carried partition that lived beyond the previous slot0.
            tail_size = storage_size + nvm_size
            tail_start = align_down(total_size - tail_size, erase_size)
            slot0_offset = align_up(boot_end, erase_size)
            slot0_limit = tail_start
            for c in carried:
                if old_slot0 is None or c[2] >= old_slot0[0] + old_slot0[1]:
                    slot0_limit = min(slot0_limit, c[2])
                    break
            slot0_size = align_down(slot0_limit - slot0_offset, max_erase)

            int_parts = [
                ("mcuboot", "boot_partition", boot[2], boot_size),
                ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ]

            # Carried partitions live at their current offsets: ones inside
            # the previous slot0 stay overlapped by the grown slot0 (SoC
            # phandle references just need the labels to exist), ones beyond
            # it were protected by the cap above. The app tail goes after
            # whichever of slot0 / the carried partitions ends last.
            carried_end = max((c[2] + c[3] for c in carried), default=0)
            cursor = max(slot0_offset + slot0_size, align_up(carried_end, erase_size))
            if nvm_size > 0:
                nvm_offset = align_up(cursor, erase_size)
                int_parts.append(("nvm", "nvm_partition", nvm_offset, nvm_size))
                cursor = nvm_offset + nvm_size

            storage_offset = align_up(cursor, erase_size)
            if storage_offset + storage_size > total_size:
                print(
                    f"  Warning: {dev_label} has no room for the "
                    f"{format_size(storage_size)} storage partition after the "
                    "carried partitions; layout will overflow the device.",
                    file=sys.stderr,
                )
            int_parts.append(("storage", "storage_partition", storage_offset, storage_size))

            for label_prop, node_label, off, size in carried:
                int_parts.append((label_prop, node_label, off, size))

            # All partitions must be regenerated since the upstream partitions
            # node is deleted to allow slot0 to grow.
            result.append((dev_label, total_size, erase_size, int_parts, set()))

            # External: slot1 + filesystem
            # Swap-using-offset needs one extra max-erase sector in slot1.
            # Overwrite-only copies slot1 onto slot0 and needs no scratch.
            ext_carry = existing.get(ext_label, {})
            ext_carried = sorted(ext_carry.get("carried", []), key=lambda c: c[2])
            slot1_size = slot0_size if overwrite_only else slot0_size + max_erase
            ext_parts = []
            cursor = 0

            if not nvm_on_internal and not data_flash:
                nvm_ext_size = ext_erase
                ext_parts.append(("nvm", "nvm_partition", cursor, nvm_ext_size))
                cursor = align_up(cursor + nvm_ext_size, ext_erase)

            slot1_offset = align_up(cursor, ext_erase)
            if ext_carried:
                # Shrink slot1 to fit in front of the first carried
                # partition instead of clobbering it.
                avail = align_down(ext_carried[0][2], ext_erase) - slot1_offset
                slot1_size = min(slot1_size, align_down(avail, ext_erase))
            ext_parts.append(("image-1", "slot1_partition", slot1_offset, slot1_size))
            cursor = align_up(slot1_offset + slot1_size, ext_erase)

            for label_prop, node_label, off, size in ext_carried:
                ext_parts.append((label_prop, node_label, off, size))
                cursor = max(cursor, align_up(off + size, ext_erase))

            fs_size = align_down(ext_size - cursor, ext_erase)
            if fs_size > 0:
                ext_parts.append(("filesystem", fs_node_label, cursor, fs_size))

            result.append((ext_label, ext_size, ext_erase, ext_parts, set()))
        else:
            # ── Keep the upstream slots; replace the upstream app tail ──
            # No separate external flash (or XIP: the mcuboot layout lives
            # on the external flash itself): upstream mcuboot/slot0/slot1 are
            # kept as-is and the fork's storage/nvm/filesystem trio replaces
            # whatever the upstream layout had after them.
            upstream = [
                p
                for p in parts
                if p[0] not in APP_PARTITION_LABELS
                and p[0] != "storage"
                and not (single_app and p[0] == "image-1")
            ]
            # When the hand-maintained glue deletes the whole upstream
            # partitions node of this device, the generated file must be
            # self-contained: re-emit the upstream partitions too (the
            # board dts still references some of their labels), instead of
            # relying on nodes the glue removed.
            predefined = (
                set() if dev_label in deleted_devices else {p[0] for p in upstream}
            )
            kept = sorted(upstream, key=lambda p: p[2])

            # Find the end of the last kept upstream partition.
            last_end = max((p[2] + p[3] for p in kept), default=0)
            cursor = align_up(last_end, erase_size)

            storage_size = align_up(STORAGE_SIZE, erase_size)
            if cursor + storage_size <= total_size:
                kept.append(("storage", "storage_partition", cursor, storage_size))
                cursor += storage_size

            if not data_flash:
                nvm_size = erase_size
                if cursor + nvm_size <= total_size:
                    kept.append(("nvm", "nvm_partition", cursor, nvm_size))
                    cursor = align_up(cursor + nvm_size, erase_size)

            fs_size = align_down(total_size - cursor, erase_size)
            if fs_size > 0:
                kept.append(("filesystem", fs_node_label, cursor, fs_size))

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


def _partition_device(part_node):
    """Walk up from a partition node to the NVM memory node that owns it.

    The memory node is the first ancestor with a ``reg`` property (the
    partition's own ``reg`` is an offset+size within it; the ``partitions``
    grouping node has none).
    """
    p = part_node.parent
    while p is not None:
        if p.props.get("reg") is not None:
            return p
        p = p.parent
    return None


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


def plan_partitions(
    edt,
    flash_overrides=None,
    existing=None,
    deleted_devices=None,
    single_app=False,
    overwrite_only=False,
):
    """Determine the partition layout based on available flash devices.

    If the board already has a predefined mcuboot layout (from the upstream
    board DTS), the existing partitions are kept and nvm/filesystem are added
    to the free space.

    Otherwise, partitions are planned from scratch:
    - If internal flash exists and there is also external flash:
        Internal: mcuboot + slot0 (fills remaining internal flash)
        External: storage (32 KB) + slot1 (same size as slot0) + filesystem (rest)
        (single-app boards -- whose chosen flash cannot support mcuboot swap
        -- get no slot1, and the filesystem fills the external flash)
    - If internal flash exists with no external flash:
        Internal: mcuboot + slot0 + storage (32 KB) + filesystem (rest)
        No slot1 (no OTA update support).
    - If only external flash (XIP, e.g. FlexSPI):
        External: mcuboot + slot0 + slot1 (same size as slot0) + storage (32 KB)
        + nvm + filesystem (rest).

    NVM partition placement:
    - If small internal data flash exists (< 64 KB, e.g. RA6/RA8 data flash),
      NVM uses the entire data flash device (ideal: small erase pages).
    - Otherwise, NVM gets 1 erase page on the same device as storage.

    Returns list of (dev_label, total_size, erase_size, [(label, node_label, offset, size)]).
    """
    # Check for predefined mcuboot layout first.
    if _has_predefined_mcuboot(edt, flash_overrides):
        return plan_partitions_predefined(
            edt,
            flash_overrides,
            existing,
            deleted_devices,
            single_app,
            overwrite_only,
        )

    all_flash = discover_all_flash(edt, flash_overrides)
    if not all_flash:
        return []

    fs_node_label = filesystem_node_label(edt)

    # Separate small internal data flash (< 64 KB) from main internal flash.
    # Data flash (e.g. RA6/RA8 flash1) has small erase pages ideal for NVM.
    data_flash = [(l, s, e) for l, s, e, i in all_flash if i and s < 64 * KB]
    internal = [(l, s, e) for l, s, e, i in all_flash if i and s >= 64 * KB]
    # Filter out tiny external flash regions
    external = [(l, s, e) for l, s, e, i in all_flash if not i and s >= 1 * MB]
    secondary_slot = not single_app and (_swap_supported(edt) or overwrite_only)

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

        if not secondary_slot:
            # ── Single-app: no slot1 / swap, so no max_erase rounding and no
            # scratch sector; slot0 fills internal flash up to the app tail
            # and the filesystem takes all of the external flash. ──
            storage_size = align_up(STORAGE_SIZE, int_erase)
            nvm_size = int_erase if not data_flash else 0
            slot0_size = align_down(
                int_size - slot0_offset - storage_size - nvm_size, int_erase
            )
            int_parts = [
                ("mcuboot", "boot_partition", 0, boot_size),
                ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ]
            cursor = slot0_offset + slot0_size
            int_parts.append(("storage", "storage_partition", cursor, storage_size))
            cursor += storage_size
            if nvm_size:
                int_parts.append(("nvm", "nvm_partition", cursor, nvm_size))
            result.append((int_label, int_size, int_erase, int_parts, set()))

            fs_size = align_down(ext_size, ext_erase)
            ext_parts = [("filesystem", fs_node_label, 0, fs_size)]
            result.append((ext_label, ext_size, ext_erase, ext_parts, set()))
            # NVM comes from the data flash device (if any) at the end of
            # this function.
        else:
            slot0_size = align_down(int_size - slot0_offset, max_erase)

            int_parts = [
                ("mcuboot", "boot_partition", 0, boot_size),
                ("image-0", "slot0_partition", slot0_offset, slot0_size),
            ]

            # If there's leftover internal space after slot0 (due to max_erase
            # rounding), use it for storage instead of wasting external flash.
            int_leftover = int_size - (slot0_offset + slot0_size)
            storage_size = align_up(STORAGE_SIZE, int_erase)
            storage_on_internal = int_leftover >= storage_size

            if storage_on_internal:
                storage_offset = slot0_offset + slot0_size
                int_parts.append(("storage", "storage_partition", storage_offset, storage_size))

            result.append((int_label, int_size, int_erase, int_parts, set()))

            # External: slot1 + filesystem (and storage/nvm if they didn't fit internally)
            # Swap-using-offset needs one extra max-erase sector in slot1.
            # Overwrite-only copies slot1 onto slot0 and needs no scratch.
            slot1_size = slot0_size if overwrite_only else slot0_size + max_erase
            nvm_on_ext = not data_flash
            nvm_ext_size = ext_erase if nvm_on_ext else 0

            if storage_on_internal:
                nvm_ext_offset = 0
                slot1_offset = nvm_ext_size
            else:
                storage_size = align_up(STORAGE_SIZE, ext_erase)
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
                ("filesystem", fs_node_label, fs_offset, fs_size),
            ]
            result.append((ext_label, ext_size, ext_erase, ext_parts, set()))

    elif internal:
        # ── Internal only ──
        int_label, int_size, int_erase = internal[0]

        boot_size = align_up(MCUBOOT_SIZE, int_erase)
        slot0_offset = boot_size
        # Reserve space at the end for storage + nvm + filesystem.
        # NVM is 1 erase page; storage is STORAGE_SIZE.
        storage_size = align_up(STORAGE_SIZE, int_erase)
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
        int_parts.append(("filesystem", fs_node_label, fs_offset, fs_size))
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
        storage_size = align_up(STORAGE_SIZE, ext_erase)
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
            ("filesystem", fs_node_label, fs_offset, fs_size),
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


def generate_partitions_dtsi(planned_devices, mapped_devices=None):
    """Generate the partition portion of a partitions .dtsi file.

    Each entry in planned_devices is:
        (dev_label, total_size, erase_size, parts, predefined_labels)
    where predefined_labels is a set of partition labels already defined in the
    upstream DTS. Those partitions are skipped in the generated output.

    mapped_devices is the set of device labels that may carry
    ``zephyr,mapped-partition`` children (see mapped_partition_devices()); None
    means every device may (kept for callers that don't know the board's NVM
    compatibles).
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
            if mapped_devices is None or dev_label in mapped_devices:
                lines.append('\t\t\tcompatible = "zephyr,mapped-partition";')
            # Carried board-specific partitions may have no `label` property
            # (e.g. the nRF54H20 VPR code regions); keep them unlabeled.
            if label:
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
    Per-board "-partitions.dtsi" files (the generated partition geometry that
    <board>.dtsi includes) are skipped: a board's layout is its main dtsi,
    which is what gets applied to an image. Per-board "-updater.dtsi" files
    (updater-only chosen/glue overlays, applied after <board>.dtsi by the
    Makefile) are skipped too: they are not layouts.
    """
    layouts = {}
    dtsis = (
        p
        for p in DTS_OUT_DIR.glob("*/*.dtsi")
        if not p.stem.endswith("-partitions") and not p.stem.endswith("-updater")
    )
    for dtsi in sorted(dtsis, key=lambda p: (p.stem, p.parent.name)):
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


def gen_boards_mk():
    """Write dts/boards.mk, the Make fragment the standalone Makefile consumes.

    The standalone build used to resolve each board's Zephyr board id, layout
    overlay and mode at Make-parse time via ``tools/standalone_build.py get``.
    Now the Makefile only needs the canonical board id and the layout overlay
    path (the upgrade mode follows the layout through Kconfig, see
    ``boot/zephyr/Kconfig``'s ``BOOT_IMAGE_UPGRADE_MODE`` default keyed on the
    ``slot1_partition`` devicetree nodelabel), generated here from
    ``boards.toml`` so the Makefile never shells out to Python to look a board
    up.
    """
    boards = load_boards_manifest()
    keys = sorted(k for k, v in boards.items() if v["mcuboot"])

    lines = [
        "# Auto-generated by tools/partition_layout.py --gen-list from boards.toml.",
        "# Do not edit by hand; change tools/boards.toml and re-run",
        "# `python3 tools/partition_layout.py --gen-list`.",
        "#",
        "# Make fragment consumed by the standalone Makefile. Maps each mcuboot",
        "# board partition key to its canonical Zephyr board id and layout overlay.",
        "# The upgrade mode (single_app vs swap-using-offset) is NOT here: it is",
        "# chosen by Kconfig from the devicetree (slot1 present -> swap).",
        "#",
        "# MCUBOOT_BOARDS   every partition key that boots via mcuboot",
        "# <key>_BOARD      canonical Zephyr board id (value passed to `west build -b`)",
        "# <key>_DTSI       path to the layout overlay (dts/<vendor>/<key>.dtsi)",
        "",
        f"MCUBOOT_BOARDS := {' '.join(keys)}",
        "",
    ]
    for key in keys:
        entry = boards[key]
        vendor = entry.get("vendor")
        lines.append(f"{key}_BOARD := {entry['board']}")
        if vendor:
            lines.append(f"{key}_DTSI := dts/{vendor}/{key}.dtsi")
        lines.append("")

    BOARDS_MK.parent.mkdir(parents=True, exist_ok=True)
    BOARDS_MK.write_text("\n".join(lines) + "\n")
    print(f"  Wrote {BOARDS_MK.relative_to(MODULE_DIR.parent)} ({len(keys)} boards)")


def nor_page_layout_kconfigs(edt):
    """Map flash device labels to the Kconfig that pins their page layout.

    NOR drivers whose erase geometry is discovered at runtime (SFDP) rather
    than taken from the devicetree expose the flash page layout through a
    driver Kconfig that defaults to the 64K block erase. That would enumerate
    such a slot as a handful of 64K pages instead of erase-size pages, making
    it incompatible with a slot on internal flash (mcuboot refuses:
    "Cannot upgrade: not a compatible amount of sectors"). Returns
    {dev_label: kconfig_name} for the devices whose driver has such a Kconfig.
    """
    compat_to_kconfig = {
        # drivers/flash/spi_nor.c
        "jedec,spi-nor": "SPI_NOR_FLASH_LAYOUT_PAGE_SIZE",
        # drivers/flash/nrf_qspi_nor.c
        "nordic,qspi-nor": "NORDIC_QSPI_NOR_FLASH_LAYOUT_PAGE_SIZE",
        # drivers/flash/flash_mspi_nor.c
        "jedec,nor": "FLASH_MSPI_NOR_LAYOUT_PAGE_SIZE",
    }
    kconfigs = {}
    for node in edt.nodes:
        if not node.labels:
            continue
        for compat in node.compats:
            kconfig = compat_to_kconfig.get(compat)
            if kconfig:
                kconfigs[node.labels[0]] = kconfig
                break
    return kconfigs


def autogen_conf_header(board_key):
    """File header for conf/<key>-autogen.conf, the generated conf fragment
    that pairs with the optional hand-maintained conf/<key>.conf."""
    return (
        f"# Auto-generated conf fragment for {board_key} (slot layout).\n"
        "# Generated by tools/partition_layout.py --fix; do not edit by hand --\n"
        f"# re-run `python3 tools/partition_layout.py --fix {board_key}` after\n"
        "# changing the layout. Hand-maintained board opt-ins (UF2, serial,\n"
        "# retention, ...) belong in conf/<key>.conf, which is applied before\n"
        "# this fragment so the computed sector geometry wins.\n"
        "#\n"
    )


def sectors_conf_block(max_sectors, layout_kconfig="", nor_page_size=0,
                       has_slot1=True):
    """The generated Kconfig block pinning a board's slot sector layout,
    written to conf/<key>-autogen.conf."""
    if has_slot1:
        reason = (
            "# BOOT_MAX_IMG_SECTORS sizes the swap-status trailer. AUTO can't derive\n"
            "# it here: slot1 is on external SPI NOR whose jedec,spi-nor binding has\n"
            "# no static erase-block-size (SFDP runtime discovery), so the build\n"
            "# under-counts slot1's sectors. The planner knows the erase sizes, so it\n"
            "# sets the max sector count directly: max(slot0, slot1).\n"
        )
    else:
        reason = (
            "# BOOT_MAX_IMG_SECTORS caps the number of sectors per image slot. This\n"
            "# board has a single-app layout (no slot1, hence no swap trailer), so\n"
            "# the value simply pins slot0's sector count. Pinning it keeps every\n"
            "# board's sector geometry explicit and future-proofs a slot1 being\n"
            "# added later. The planner knows the erase sizes, so it sets the max\n"
            "# sector count directly from slot0.\n"
        )
    return (
        reason
        + "#\n"
        "# CONFIG_BOOT_MAX_IMG_SECTORS_AUTO is not set\n"
        f"CONFIG_BOOT_MAX_IMG_SECTORS={max_sectors}\n"
        + (
            "#\n"
            "# This board's external NOR driver discovers its geometry at runtime\n"
            "# (SFDP), and its page-layout Kconfig defaults to the 64K block\n"
            "# erase -- which would enumerate slot1 as a handful of 64K pages\n"
            "# instead of erase-size pages, making the slots' sector layouts\n"
            "# incompatible. Pin it to the external flash's erase size.\n"
            f"CONFIG_{layout_kconfig}={nor_page_size}\n"
            if nor_page_size
            else ""
        )
    )


def gen_sectors_conf(board_key, planned, nor_kconfigs=None, split=None):
    """Write (or remove) conf/<key>-autogen.conf, the generated conf fragment
    that pairs with the optional hand-maintained conf/<key>.conf, setting
    CONFIG_BOOT_MAX_IMG_SECTORS.

    ``split`` carries the shared-flash split layout (SiWx917-style boards whose
    code partition is split into boot+slot0); when given, the sector count is
    computed from the split's slot0 instead of ``planned``.

    BOOT_MAX_IMG_SECTORS sizes the swap-status trailer; CONFIG_BOOT_MAX_IMG_SECTORS_AUTO
    derives it from each slot's erase-block-size. That works for internal flash (the
    DT carries erase-block-size) but not for a slot on external SPI NOR: the
    jedec,spi-nor binding has no static erase-block-size (the chip discovers it at
    runtime via SFDP), so the build under-counts that slot and mis-places the
    trailer magic -- which can trigger a bogus swap and clobber slot0. The
    planner knows the erase sizes (internal from the DT, external from
    boards.toml's [external_flash].erase_block_size), so it sets the max sector
    count directly: max(slot0, slot1) sectors. Single-app boards (no slot1) get
    the fragment too, pinning slot0's sector count, so every board has the
    sector geometry explicit.

    The same SFDP discovery gap also affects the flash page layout the driver
    reports: NOR drivers with runtime-discovered geometry expose it through a
    Kconfig (see nor_page_layout_kconfigs()) that defaults to the 64K block
    erase, so an external-NOR slot1 enumerates as a few 64K pages while slot0
    enumerates as many 4K pages -- the slots then disagree on both the amount
    and the size of their sectors and mcuboot refuses to upgrade
    ("Cannot upgrade: not a compatible amount of sectors"). The drivers accept
    any non-zero multiple of the chip's smallest erase size (4K on the boards we
    use), so when slot1 is on a different (external) flash device than slot0,
    also pin that driver's layout page size to the external device's erase size
    so both slots enumerate in the same erase units.

    The fragment is applied after conf/<key>.conf (see the Makefile's
    EXTRA_CONF_FILE order), so the computed geometry wins over anything
    hand-written. A legacy conf/<key>-sectors.conf from before the rename
    is removed if found.
    """
    autogen_path = SECTORS_CONF_DIR / f"{board_key}-autogen.conf"
    legacy_path = SECTORS_CONF_DIR / f"{board_key}-sectors.conf"
    if legacy_path.exists():
        legacy_path.unlink()
        print(f"  Removed legacy {legacy_path.relative_to(MODULE_DIR.parent)}; "
              f"its content now lives in {autogen_path.name}")

    max_sectors = 0
    has_slot1 = False
    slot0_dev = None
    slot1_dev = None
    slot1_erase_size = 0
    if split is not None:
        # Shared-flash split layout: boot + slot0 on the split region's erase
        # page; there is no slot1 by construction.
        _s0_label, _s0_nodes, _s0_off, s0_size = split["slot0"]
        slot0_dev = split["dev_label"]
        max_sectors = s0_size // split["erase"]
    for dev, _total, erase_size, parts, *_rest in planned:
        for label, _node_label, _off, size in parts:
            if label == "image-0" and erase_size:
                slot0_dev = dev
                max_sectors = max(max_sectors, size // erase_size)
            elif label == "image-1" and erase_size:
                has_slot1 = True
                slot1_dev = dev
                slot1_erase_size = erase_size
                max_sectors = max(max_sectors, size // erase_size)

    # Slot1 on a different flash device than slot0: pin the external NOR
    # driver's page layout to the erase size so both slots enumerate in the
    # same sector units (see docstring).
    nor_kconfigs = nor_kconfigs or {}
    layout_kconfig = nor_kconfigs.get(slot1_dev) if slot1_dev != slot0_dev else None
    nor_page_size = slot1_erase_size if layout_kconfig else 0

    if not max_sectors:
        # No image slot to size (shouldn't happen for a mcuboot board); remove
        # a stale fragment if the board used to have a slot1.
        if autogen_path.exists():
            autogen_path.unlink()
            print(f"  Removed stale {autogen_path.relative_to(MODULE_DIR.parent)}")
        return

    SECTORS_CONF_DIR.mkdir(parents=True, exist_ok=True)
    autogen_path.write_text(
        autogen_conf_header(board_key)
        + sectors_conf_block(max_sectors, layout_kconfig, nor_page_size,
                             has_slot1=has_slot1)
    )
    print(
        f"  Wrote {autogen_path.relative_to(MODULE_DIR.parent)} "
        f"(BOOT_MAX_IMG_SECTORS={max_sectors}"
        + (
            f", {layout_kconfig}={nor_page_size}"
            if nor_page_size
            else ""
        )
        + ")"
    )


def gen_all_sectors_conf():
    """Regenerate conf/<key>-autogen.conf for every mcuboot board.

    Plans each board's layout from a cmake-only build (the same EDT --fix uses)
    and emits CONFIG_BOOT_MAX_IMG_SECTORS, without rewriting any dtsi -- so the
    hand-maintained glue in each dtsi is preserved. Boards whose cmake-only
    build can't run (no workspace yet, or the board isn't fetchable) are skipped
    with a warning so --gen-list still emits the other artifacts.
    """
    boards = load_boards_manifest()
    for key in sorted(k for k, v in boards.items() if v["mcuboot"]):
        entry = boards[key]
        flash_overrides = {}
        ef = entry.get("external_flash")
        if ef:
            flash_overrides[ef["label"]] = {"erase_block_size": ef["erase_block_size"]}
        build_dir = MODULE_DIR / f"build-partitions-{key}"
        try:
            print(f"  {key}: planning layout...")
            cmake_only_build(entry["board"], build_dir)
            edt = load_edt(build_dir)
        except SystemExit:
            print(f"  Skipped {key} (cmake-only build failed; is the workspace set up?)",
                  file=sys.stderr)
            continue
        split = plan_code_partition_split(edt, flash_overrides)
        if split is not None:
            gen_sectors_conf(key, [], nor_page_layout_kconfigs(edt), split=split)
        else:
            partitions_path = DTS_OUT_DIR / entry["vendor"] / f"{key}-partitions.dtsi"
            glue_path = DTS_OUT_DIR / entry["vendor"] / f"{key}.dtsi"
            planned = plan_partitions(
                edt,
                flash_overrides,
                existing=parse_existing_partitions_dtsi(partitions_path),
                deleted_devices=glue_deleted_partition_devices(glue_path),
                single_app=entry.get("single_app", False),
                overwrite_only=entry["mcuboot_mode"] == "overwrite_only",
            )
            gen_sectors_conf(key, planned if planned else [], nor_page_layout_kconfigs(edt))


def partitions_dtsi_header(board_key, vendor):
    """Comment header for the generated dts/<vendor>/<board>-partitions.dtsi."""
    return (
        f"/* Partition layout for {board_key}: the flash memory map shared by the\n"
        " * bootloader and every application image. Generated by\n"
        " * tools/partition_layout.py; do not edit by hand -- re-run\n"
        " * `python3 tools/partition_layout.py --fix " + board_key + "` after changing\n"
        " * the upstream board geometry or tools/boards.toml.\n"
        " *\n"
        " * Board-specific overlay glue (delete-nodes, device enablement,\n"
        f" * aliases/chosen) lives in dts/{vendor}/{board_key}.dtsi, which includes\n"
        " * this file.\n"
        " */\n"
    )


def ensure_glue_dtsi(board_key, vendor):
    """Create dts/<vendor>/<board>.dtsi if missing; otherwise verify it
    includes the generated partitions file.

    The main dtsi is hand-maintained: it carries the board-specific glue the
    planner cannot compute, and pulls in the generated partition layout with a
    #include so it stays a complete, self-contained overlay for both the
    bootloader and application images. It is only written when it does not
    exist yet; an existing file is never rewritten (that is the whole point of
    the split), but a missing #include is reported because without it the
    board silently loses its partition layout.
    """
    dtsi_dir = DTS_OUT_DIR / vendor
    dtsi_path = dtsi_dir / f"{board_key}.dtsi"
    partitions_name = f"{board_key}-partitions.dtsi"

    if dtsi_path.exists():
        if f'#include "{partitions_name}"' not in dtsi_path.read_text():
            print(
                f"  Warning: {dtsi_path.relative_to(MODULE_DIR.parent)} does not include "
                f'`#include "{partitions_name}"\'; add it or the board will not '
                "get the partition layout.",
                file=sys.stderr,
            )
        return

    dtsi_dir.mkdir(parents=True, exist_ok=True)
    dtsi_path.write_text(
        f"/* Board-specific overlay glue for {board_key}.\n"
        " *\n"
        " * Hand-maintained: device enablement (status = \"okay\"), delete-nodes\n"
        " * for partitions that must be replaced, aliases and chosen nodes. The\n"
        " * flash partition layout lives in " + partitions_name + ", which is\n"
        " * generated by tools/partition_layout.py --fix and included below.\n"
        " */\n"
        "\n"
        f'#include "{partitions_name}"\n'
    )
    print(f"  Wrote {dtsi_path.relative_to(MODULE_DIR.parent)}")


def fix_alignment(
    board_key,
    vendor,
    edt,
    flash_overrides=None,
    single_app=False,
    overwrite_only=False,
):
    """Plan and write the partition layout to
    dts/<vendor>/<board>-partitions.dtsi.

    The generated file contains only the partition nodes (the part the planner
    can compute from the devicetree). Board-specific overlay glue -- enabling
    external flash devices, the boot-mode retention cell, chosen/aliases,
    delete-nodes -- is hand-maintained in dts/<vendor>/<board>.dtsi, which
    includes the generated file so it stays a complete, self-contained
    overlay for both the bootloader and application images. The main dtsi
    is only created when it does not exist; it is never rewritten.
    """
    if not vendor:
        print(f"  No vendor declared for {board_key} in boards.toml; cannot place dtsi.", file=sys.stderr)
        return
    dtsi_dir = DTS_OUT_DIR / vendor
    partitions_path = dtsi_dir / f"{board_key}-partitions.dtsi"

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
        content = partitions_dtsi_header(board_key, vendor) + "\n" + generate_mapped_split_dtsi(split)
        dtsi_dir.mkdir(parents=True, exist_ok=True)
        partitions_path.write_text(content)
        print(f"  Wrote {partitions_path.relative_to(MODULE_DIR.parent)}")
        ensure_glue_dtsi(board_key, vendor)
        # Single-app split layout: pin slot0's sector count.
        gen_sectors_conf(board_key, [], nor_page_layout_kconfigs(edt), split=split)
        return

    planned = plan_partitions(
        edt,
        flash_overrides,
        existing=parse_existing_partitions_dtsi(partitions_path),
        deleted_devices=glue_deleted_partition_devices(dtsi_dir / f"{board_key}.dtsi"),
        single_app=single_app,
        overwrite_only=overwrite_only,
    )
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
                f"    {label or node_label}: 0x{offset:x} + {format_dt_size(size)}"
                f" ({format_size(size)}){marker}"
            )
        print()

    content = (
        partitions_dtsi_header(board_key, vendor)
        + "\n"
        + generate_partitions_dtsi(planned, mapped_partition_devices(edt))
    )

    dtsi_dir.mkdir(parents=True, exist_ok=True)
    partitions_path.write_text(content)
    print(f"  Wrote {partitions_path.relative_to(MODULE_DIR.parent)}")
    ensure_glue_dtsi(board_key, vendor)
    gen_sectors_conf(board_key, planned, nor_page_layout_kconfigs(edt))


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
        help="Write the partition layout to dts/<vendor>/<board>-partitions.dtsi "
             "(creating dts/<vendor>/<board>.dtsi if missing)",
    )
    parser.add_argument("--list", action="store_true", help="List declared boards")
    parser.add_argument(
        "--gen-list",
        action="store_true",
        help="Regenerate dts/mcuboot_boards.cmake, dts/Kconfig.sysbuild, "
             "dts/boards.mk and conf/<key>-autogen.conf for every board",
    )
    args = parser.parse_args()

    if args.gen_list:
        gen_mcuboot_boards_cmake()
        gen_kconfig_sysbuild()
        gen_boards_mk()
        gen_all_sectors_conf()
        sys.exit(0)

    if args.list or not args.board:
        print("Declared boards (* = standalone, no mcuboot):")
        for key, entry in sorted(boards.items()):
            marker = "" if entry["mcuboot"] else " *"
            print(f"  {key:24s} -> {entry['board']}{marker}")
        sys.exit(0 if args.list else 1)

    if args.board not in boards:
        print(f"Unknown board: {args.board}", file=sys.stderr)
        print("\nDeclared boards:")
        for key in sorted(boards):
            print(f"  {key}")
        sys.exit(1)

    entry = boards[args.board]
    if not entry["mcuboot"]:
        print(
            f"{args.board} is a standalone board (mcuboot = false in boards.toml): "
            "its layout is a hand-maintained zephyr,mapped-partition overlay, not "
            f"something this planner emits. Edit dts/<vendor>/{args.board}.dtsi by hand.",
            file=sys.stderr,
        )
        sys.exit(1)

    board_id = entry["board"]
    vendor = entry.get("vendor")

    flash_overrides = {}
    ef = entry.get("external_flash")
    if ef:
        flash_overrides[ef["label"]] = {"erase_block_size": ef["erase_block_size"]}

    build_dir = MODULE_DIR / f"build-partitions-{args.board}"

    overlay = None
    if vendor:
        overlay = DTS_OUT_DIR / vendor / f"{args.board}.dtsi"
        if not overlay.exists():
            overlay = None

    print(f"Running cmake-only build for {board_id}...")
    cmake_only_build(board_id, build_dir, overlay=overlay)

    edt = load_edt(build_dir)

    print(f"  {args.board} -- Partition Layout")
    print()

    if args.fix:
        fix_alignment(
            args.board,
            vendor,
            edt,
            flash_overrides,
            single_app=entry.get("single_app", False),
            overwrite_only=entry["mcuboot_mode"] == "overwrite_only",
        )
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
            print(f"No flash devices with partitions found for {args.board}")
            sys.exit(1)
        show_layout(devices)


if __name__ == "__main__":
    main()