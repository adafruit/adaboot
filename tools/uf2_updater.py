#!/usr/bin/env python3
"""UF2 helpers for the standalone Adaboot build.

A board's bootloader is "UF2-capable" when its board-specific conf fragment
(``conf/<vendor>/<key>.conf``) enables ``CONFIG_MCUBOOT_UF2=y``. Only those
bootloaders present a USB mass-storage drive you can drag a ``.uf2`` onto, so
only their updaters are worth shipping as ``.uf2``.

This module backs the Makefile's ``uf2`` / ``all-uf2`` targets:

list
    Print every board id (``<vendor>_<board>``) whose bootloader conf enables
    UF2 (the set ``make all-uf2`` builds a ``.uf2`` updater for).

base <updater-build-dir>
    Print the slot0 flash offset (``fa_off``) the UF2 bootloader writes
    incoming blocks at -- i.e. the ``-b``/``--base`` to give ``tools/uf2conv.py``.
    Parsed from ``<updater-build-dir>/zephyr/edt.pickle`` (the same EDT Zephyr
    generated for the build) so it always matches what the bootloader actually
    uses (``target_fap->fa_off``), no matter how the partition is mapped.

family <boot-build-dir>
    Print the bootloader's ``CONFIG_MCUBOOT_UF2_FAMILY_ID`` as a hex int, read
    from ``<boot-build-dir>/zephyr/.config``. The UF2 file's family ID should
    match the bootloader's so its family check accepts the blocks.

Run ``make uf2 BOARD=<key>`` (which runs ``make updater`` first) and these are
read from the freshly built ``build-<key>`` / ``build-<key>-updater`` trees.
"""

import pathlib
import re
import sys

MODULE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONF_DIR = MODULE_DIR / "conf"
BOARDS_TOML = MODULE_DIR / "tools" / "boards.toml"

# python-devicetree ships inside the Zephyr checkout; _pydt_src() locates it
# from the build's CMakeCache (or the standalone deps/zephyr fallback).


def _pydt_src(build_dir):
    """Locate Zephyr's python-devicetree sources.

    Preferred: the Zephyr checkout the build actually used, from the build's
    CMakeCache (ZEPHYR_BASE) -- this works wherever the west workspace lives
    (this fork's own deps/ layout, or an application workspace such as
    CircuitPython's zephyr-cp port whose Zephyr checkout is elsewhere in the
    repo). Falls back to the standalone `make workspace` layout (deps/zephyr).
    """
    # CMakeCache.txt sits at the build-dir root for plain (non-sysbuild)
    # builds and under zephyr/ for sysbuild image build dirs.
    for cache in (pathlib.Path(build_dir) / "CMakeCache.txt",
                  pathlib.Path(build_dir) / "zephyr" / "CMakeCache.txt"):
        if not cache.exists():
            continue
        for line in cache.read_text(errors="replace").splitlines():
            if line.startswith("ZEPHYR_BASE:PATH="):
                pydt = (pathlib.Path(line.split("=", 1)[1].strip())
                        / "scripts" / "dts" / "python-devicetree" / "src")
                if pydt.exists():
                    return pydt
                break
    return MODULE_DIR / "deps" / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src"


def _load_boards():
    import tomllib  # py311+

    with BOARDS_TOML.open("rb") as f:
        return tomllib.load(f).get("boards", {})


def cmd_list():
    """Print every mcuboot board whose conf/<vendor>/<key>.conf enables UF2."""
    boards = _load_boards()
    for key in boards:
        if not boards[key].get("mcuboot", True):
            continue
        conf = CONF_DIR / boards[key].get("vendor", "") / f"{key}.conf"
        if not conf.exists():
            continue
        text = conf.read_text()
        # Ignore commented-out lines.
        enabled = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                continue
            if re.match(r"CONFIG_MCUBOOT_UF2=y\b", line):
                enabled = True
                break
        if enabled:
            vendor = boards[key].get("vendor")
            if not vendor or key.startswith(f"{vendor}_"):
                print(key)
            else:
                print(f"{vendor}_{key}")
    return 0


def _load_edt(updater_dir):
    pydt_src = _pydt_src(updater_dir)
    if not pydt_src.exists():
        raise SystemExit(
            f"error: python-devicetree sources not found (looked in {pydt_src}) "
            "-- run 'make workspace' first to fetch the Adafruit Zephyr "
            "checkout (it ships python-devicetree)."
        )
    sys.path.insert(0, str(pydt_src))
    import pickle  # needs the sys.path tweak above

    edt_pickle = pathlib.Path(updater_dir) / "zephyr" / "edt.pickle"
    if not edt_pickle.exists():
        raise SystemExit(
            f"error: {edt_pickle} not found -- run 'make updater BOARD=<key>' "
            "first so the build generates it."
        )
    with edt_pickle.open("rb") as f:
        return pickle.load(f)


def cmd_base(updater_dir):
    """Print slot0_partition's flash offset (fa_off) as 0x... hex."""
    edt = _load_edt(updater_dir)
    hits = [n for n in edt.nodes if "slot0_partition" in n.labels]
    if not hits:
        raise SystemExit("error: no slot0_partition node in the EDT")
    slot0 = hits[0]
    if not slot0.regs:
        raise SystemExit("error: slot0_partition has no reg")

    # The flash device is the parent of the fixed-partitions container:
    # <flash-dev> -> partitions (fixed-partitions) -> slot0_partition.
    partitions = slot0.parent
    flash_dev = partitions.parent
    # Walk up until we find an ancestor with a reg (the flash array base).
    while flash_dev is not None and not flash_dev.regs:
        flash_dev = flash_dev.parent
    if flash_dev is None or not flash_dev.regs:
        raise SystemExit("error: could not locate the flash device backing slot0")

    # Both addrs are EDT-translated (to CPU where a ranges chain exists, else
    # device-local), so their difference is the slot's offset within the flash
    # device -- exactly the fa_off the UF2 bootloader compares target_addr to.
    fa_off = slot0.regs[0].addr - flash_dev.regs[0].addr
    if fa_off < 0:
        raise SystemExit(
            f"error: slot0 fa_off {fa_off:#x} is negative "
            f"(slot0 {slot0.regs[0].addr:#x} < flash dev {flash_dev.regs[0].addr:#x})"
        )
    print(f"0x{fa_off:x}")
    return 0


def cmd_family(boot_dir):
    """Print CONFIG_MCUBOOT_UF2_FAMILY_ID from the bootloader .config as 0x.. hex."""
    config = pathlib.Path(boot_dir) / "zephyr" / ".config"
    if not config.exists():
        raise SystemExit(f"error: {config} not found -- run 'make build BOARD=<key>' first.")
    for line in config.read_text().splitlines():
        m = re.match(r"^CONFIG_MCUBOOT_UF2_FAMILY_ID=(0x[0-9a-fA-F]+|\d+)\s*$", line)
        if m:
            val = int(m.group(1), 0)
            print(f"0x{val:x}")
            return 0
    # Family ID is optional in Kconfig (default 0x0); absent means accept any.
    print("0x0")
    return 0


USAGE = __doc__


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub = argv[1]
    if sub == "list":
        return cmd_list()
    if sub == "base" and len(argv) >= 3:
        return cmd_base(argv[2])
    if sub == "family" and len(argv) >= 3:
        return cmd_family(argv[2])
    sys.stderr.write(
        "usage: uf2_updater.py {list | base <updater-build-dir> | family <boot-build-dir>}\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
