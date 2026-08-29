#!/usr/bin/env python3
"""Slot1 updater variant: sign the updater as a swap-style test upgrade.

The bootloader-updater application (``samples/bootloader-updater``) is, by
default, a transient slot0 installer: ``make updater`` builds it single-app /
overwrite-only (no swap trailer) so it can be flashed straight to slot0
(serial / UF2 / debugger) to self-update the bootloader.

On boards whose partition layout has a secondary slot (``slot1_partition``),
the same updater can also be delivered as an OTA into the secondary slot:
mcuboot then swaps it into slot0 on the next boot, the updater runs once to
overwrite the bootloader, and -- because it is a *test* upgrade (the pending
trailer is written by imgtool ``--pad`` but the image is *not* confirmed) --
mcuboot reverts on the following boot, restoring the real app to slot0. The
user then re-flashes (the updater requests serial/UF2 recovery mode anyway).

This module produces that second variant by re-signing the updater's raw
``zephyr.bin`` with imgtool swap-style arguments (``--slot-size`` = slot1,
``--align`` = the flash write-block-size) plus ``--pad`` (no ``--confirm``):

    <updater>/zephyr/zephyr.bin  -\u003e  <updater>/zephyr/zephyr.slot1.signed.bin

It reads slot1's size and the write-block-size from the build's EDT pickle, and
the imgtool version / header-size from the build's ``.config``, so it always
matches what the bootloader expects -- the same sources ``mcuboot.cmake`` uses,
just run as a Make post-step (see the Makefile's ``updater`` target) instead of
inside CMake (whose ``extra_post_build_commands`` are consumed before the app
CMakeLists runs, so a second imgtool pass cannot be registered from there).

If the layout has no ``slot1_partition`` this is a clean no-op (exit 0): the
slot1 variant only exists for swap-capable boards.
"""

import pathlib
import re
import subprocess
import sys

MODULE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONF_DIR = MODULE_DIR / "conf"
IMGTOOL = MODULE_DIR / "scripts" / "imgtool.py"
PYDT_SRC = MODULE_DIR / "deps" / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src"


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


def _config_value(build_dir, name):
    """Read a CONFIG_<name> from <build_dir>/zephyr/.config (the merged config)."""
    config = pathlib.Path(build_dir) / "zephyr" / ".config"
    if not config.exists():
        raise SystemExit(f"error: {config} not found -- build the updater first.")
    pat = re.compile(rf'^{re.escape(name)}=(.*)$')
    for line in config.read_text().splitlines():
        m = pat.match(line.strip())
        if m:
            val = m.group(1)
            # Kconfig quotes string values in .config (e.g. CONFIG_...="0.0.0+0");
            # int/hex values are unquoted. Strip one surrounding quote pair.
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            return val
    raise SystemExit(f"error: {name} not set in {config}")


def _slot1_size_and_align(edt):
    """Return (slot1_partition size, write-block-size) or (None, None).

    write-block-size comes from the ``zephyr,flash`` chosen node (the same
    node mcuboot.cmake reads), falling back to 4 like mcuboot.cmake does.
    """
    slot1 = next((n for n in edt.nodes if "slot1_partition" in n.labels), None)
    if slot1 is None or not slot1.regs:
        return None, None
    slot1_size = slot1.regs[0].size

    flash = edt.chosen_node("zephyr,flash") if hasattr(edt, "chosen_node") else None
    if flash is None:
        # python-devicetree exposes chosen nodes via edt.chosen_nodes.
        flash = edt.chosen_nodes.get("zephyr,flash")
    wbs = None
    if flash is not None:
        wbs = flash.props.get("write-block-size")
        if wbs is not None:
            wbs = wbs.val
    if not wbs:
        wbs = 4
    return slot1_size, wbs


def cmd_slot1(updater_dir):
    updater_dir = pathlib.Path(updater_dir)
    edt = _load_edt(updater_dir)
    slot1_size, wbs = _slot1_size_and_align(edt)
    if slot1_size is None:
        # No secondary slot in this layout: the slot1 variant does not apply.
        return 0

    version = _config_value(updater_dir, "CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION")
    header_size = _config_value(updater_dir, "CONFIG_ROM_START_OFFSET")

    raw_bin = updater_dir / "zephyr" / "zephyr.bin"
    if not raw_bin.exists():
        raise SystemExit(f"error: {raw_bin} not found -- build the updater first.")
    out_bin = updater_dir / "zephyr" / "zephyr.slot1.signed.bin"

    if not IMGTOOL.exists():
        raise SystemExit(f"error: {IMGTOOL} not found (this repo is the mcuboot module).")

    cmd = [
        sys.executable, str(IMGTOOL), "sign",
        "--version", str(version),
        "--header-size", str(header_size),
        "--slot-size", str(slot1_size),
        "--align", str(wbs),
        "--pad",  # pending trailer: a test upgrade (no --confirm), so mcuboot reverts
        str(raw_bin), str(out_bin),
    ]
    print(f"==> Signing slot1 updater variant (test upgrade, slot-size {slot1_size}, "
          f"align {wbs}): {out_bin.name}")
    subprocess.run(cmd, check=True)
    print(f"==> {out_bin}  (upload to slot1 via smpmgr; mcuboot swaps it in, then reverts)")
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[1] == "slot1":
        if len(argv) < 3:
            sys.stderr.write("usage: updater_sign.py slot1 <updater-build-dir>\n")
            return 2
        return cmd_slot1(argv[2])
    sys.stderr.write(f"error: unknown subcommand '{argv[1]}'; try 'slot1 <updater-build-dir>'\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))