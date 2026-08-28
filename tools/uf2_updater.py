#!/usr/bin/env python3
"""UF2 helpers for the standalone Adaboot build.

A board's bootloader is "UF2-capable" when its board-specific conf fragment
(``conf/<key>.conf``) enables ``CONFIG_MCUBOOT_UF2=y``. Only those bootloaders
present a USB mass-storage drive you can drag a ``.uf2`` onto, so only their
updaters are worth shipping as ``.uf2``.

This module backs the Makefile's ``uf2`` / ``all-uf2`` targets:

list
    Print every partition key whose bootloader conf enables UF2 (the set
    ``make all-uf2`` builds a ``.uf2`` updater for).

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

# python-devicetree ships inside the Adafruit Zephyr checkout the standalone
# build fetches under deps/zephyr (the workspace `make workspace` creates).
PYDT_SRC = MODULE_DIR / "deps" / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src"


def _load_boards():
    import tomllib  # py311+

    with BOARDS_TOML.open("rb") as f:
        return tomllib.load(f).get("boards", {})


def cmd_list():
    """Print every mcuboot board whose conf/<key>.conf enables UF2."""
    boards = _load_boards()
    for key in boards:
        if not boards[key].get("mcuboot", True):
            continue
        conf = CONF_DIR / f"{key}.conf"
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
            print(key)
    return 0


def _load_edt(updater_dir):
    if not PYDT_SRC.exists():
        raise SystemExit(
            f"error: {PYDT_SRC} not found -- run 'make workspace' first to fetch "
            "the Adafruit Zephyr checkout (it ships python-devicetree)."
        )
    sys.path.insert(0, str(PYDT_SRC))
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
