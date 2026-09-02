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
``zephyr.bin`` with imgtool ``--pad`` (no ``--confirm``) and ``--slot-size`` =
slot1. The signing style follows the *bootloader's* upgrade mode, read from
the bootloader build's ``.config``:

* Swap mode (the default): swap-style arguments (``--align`` = the flash
  write-block-size) so the image carries a swap trailer.
* Overwrite mode (``CONFIG_BOOT_UPGRADE_ONLY=y``): ``--overwrite-only`` and no
  ``--align``. The bootloader copies slot1 over slot0 without swapping, so no
  swap alignment is needed -- and some boards' write blocks exceed imgtool's
  32-byte ``--align`` cap anyway (e.g. the RA8's 128-byte code-flash write
  block, which is why such boards use overwrite in the first place).

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


def _config_value(build_dir, name, required=True):
    """Read a CONFIG_<name> from <build_dir>/zephyr/.config (the merged config).

    Returns None (instead of aborting) when `required` is false and the symbol
    is not set.
    """
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
    if required:
        raise SystemExit(f"error: {name} not set in {config}")
    return None


def _boot_overwrite_mode(boot_dir):
    """True if the bootloader build upgrades by overwrite, not swap.

    The upgrade mode is a bootloader-only setting: conf/<key>.conf sets
    CONFIG_BOOT_UPGRADE_ONLY for boards whose write block exceeds MCUboot's
    supported swap alignment (e.g. ek_ra8d1). BOOT_UPGRADE_ONLY maps to
    MCUBOOT_OVERWRITE_ONLY in the bootloader, so imgtool must be told the
    same (``--overwrite-only``, and no ``--align``).
    """
    return _config_value(boot_dir, "CONFIG_BOOT_UPGRADE_ONLY", required=False) == "y"


def _boot_build_dir(updater_dir, boot_dir=None):
    """Resolve the bootloader build dir from the updater's (or an explicit arg)."""
    if boot_dir is not None:
        return pathlib.Path(boot_dir)
    name = pathlib.Path(updater_dir).name
    if name.endswith("-updater"):
        # The Makefile builds UPDATER ?= $(BUILD)-updater.
        return pathlib.Path(updater_dir).with_name(name[:-len("-updater")])
    raise SystemExit(
        f"error: cannot derive the bootloader build dir from '{updater_dir}' "
        "(expected it to end in '-updater'); pass it explicitly as a second arg."
    )


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


def cmd_slot1(updater_dir, boot_dir=None):
    updater_dir = pathlib.Path(updater_dir)
    boot_dir = _boot_build_dir(updater_dir, boot_dir)
    overwrite = _boot_overwrite_mode(boot_dir)
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
    ]
    if overwrite:
        # The bootloader overwrites slot0 with slot1 (no swap), so the image
        # needs no swap alignment -- skip --align entirely and use the
        # overwrite-only trailer. (Keeps boards whose write block exceeds
        # imgtool's 32-byte --align cap, like the RA8's 128, working.)
        cmd.append("--overwrite-only")
        print(f"==> Signing slot1 updater variant (overwrite-only, no align) ...")
    else:
        cmd += ["--align", str(wbs)]
        print(f"==> Signing slot1 updater variant (swap, align {wbs}) ...")
    cmd += [
        "--pad",  # pending trailer: a test upgrade (no --confirm), so mcuboot reverts
        str(raw_bin), str(out_bin),
    ]
    print(f"    slot-size {slot1_size}: {out_bin.name}")
    subprocess.run(cmd, check=True)
    if overwrite:
        print(f"==> {out_bin}  (upload to slot1 via smpmgr; the overwrite-only "
              "bootloader copies it over slot0, then the updater runs)")
    else:
        print(f"==> {out_bin}  (upload to slot1 via smpmgr; mcuboot swaps it in, "
              "then reverts)")
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[1] == "slot1":
        if len(argv) < 3:
            sys.stderr.write(
                "usage: updater_sign.py slot1 <updater-build-dir> [<bootloader-build-dir>]\n")
            return 2
        return cmd_slot1(argv[2], argv[3] if len(argv) > 3 else None)
    sys.stderr.write(f"error: unknown subcommand '{argv[1]}'; try 'slot1 <updater-build-dir>'\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))