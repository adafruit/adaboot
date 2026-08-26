#!/usr/bin/env python3
"""Resolve Adaboot board build parameters for the standalone Makefile.

The Makefile builds ``boot/zephyr`` (the MCUboot Zephyr app) for every board
this fork owns the flash layout for. Each board is identified by its *partition
key* (the dtsi filename stem, e.g. ``nrf54l15dk``), which maps to a canonical
Zephyr board id and a partition-layout overlay declared in ``tools/boards.toml``
and ``dts/<vendor>/<board>.dtsi``.

Subcommands
-----------
list
    Print every partition key that boots via mcuboot (one per line). This is the
    set ``make all`` builds.
get <key> <field>
    Print a single value for ``<key>``. ``field`` is one of:

        west_board   canonical Zephyr board id, e.g. nrf54l15dk/nrf54l15/cpuapp
        overlay      absolute path to dts/<vendor>/<key>.dtsi
        mode         single_app | ram_load

    The Makefile calls this once per field (``$(shell ...)`` collapses newlines
    to spaces, so a multi-assignment blob would be parsed as a single value;
    fetching one field at a time avoids that).
"""

import pathlib
import sys
import tomllib

MODULE_DIR = pathlib.Path(__file__).resolve().parent.parent
BOARDS_TOML = MODULE_DIR / "tools" / "boards.toml"
DTS_DIR = MODULE_DIR / "dts"

# boards.toml mcuboot_mode value -> the Zephyr Kconfig the standalone build sets.
# Mirrors Zephyr's sysbuild image_configurations/BOOTLOADER_image_default.cmake
# (SB_CONFIG_MCUBOOT_MODE_* -> CONFIG_*), but for a direct boot/zephyr build
# rather than a sysbuild. "single_app" boots a single slot (no OTA secondary);
# "ram_load" copies the newer image to RAM before jumping to it.
MODE_CONFIG = {
    "single_app": "CONFIG_SINGLE_APPLICATION_SLOT=y",
    "ram_load": "CONFIG_BOOT_RAM_LOAD=y",
}


def load_boards():
    with BOARDS_TOML.open("rb") as f:
        data = tomllib.load(f)
    return data.get("boards", {})


def cmd_list():
    boards = load_boards()
    for key in boards:
        if boards[key].get("mcuboot", True):
            print(key)
    return 0


def cmd_list_all():
    boards = load_boards()
    for key in boards:
        marker = "" if boards[key].get("mcuboot", True) else " *"
        print(f"{key}{marker}")
    return 0


FIELDS = {"west_board", "overlay", "mode"}


def resolve(key):
    """Return (west_board, overlay, mode) for a partition key, or raise.

    Only mcuboot-booting boards are resolvable: standalone (mcuboot = false)
    boards have a hand-maintained layout but no mcuboot bootloader to build.
    """
    boards = load_boards()
    if key not in boards:
        raise ValueError(
            f"'{key}' is not a board this fork owns a layout for "
            f"(not in {BOARDS_TOML.relative_to(MODULE_DIR)}). "
            f"Known: {' '.join(sorted(boards))}"
        )
    if not boards[key].get("mcuboot", True):
        raise ValueError(
            f"'{key}' is a standalone board (mcuboot = false): it has no mcuboot "
            "bootloader to build. See `python3 tools/standalone_build.py list-all`."
        )
    entry = boards[key]
    west_board = entry["board"]
    vendor = entry["vendor"]
    mode = entry.get("mcuboot_mode", "single_app")
    overlay = (DTS_DIR / vendor / f"{key}.dtsi").resolve()
    if not overlay.exists():
        raise ValueError(f"layout overlay not found: {overlay}")
    if mode not in MODE_CONFIG:
        raise ValueError(f"unknown mcuboot_mode '{mode}' for {key}")
    return west_board, str(overlay), mode


def cmd_get(key, field):
    if field not in FIELDS:
        sys.stderr.write(f"error: unknown field '{field}'; one of {' '.join(sorted(FIELDS))}\n")
        return 2
    try:
        west_board, overlay, mode = resolve(key)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    print({"west_board": west_board, "overlay": overlay, "mode": mode}[field])
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    sub = argv[1]
    if sub == "list":
        return cmd_list()
    if sub == "list-all":
        return cmd_list_all()
    if sub == "get":
        if len(argv) < 4:
            sys.stderr.write("usage: standalone_build.py get <key> <field>\n")
            return 2
        return cmd_get(argv[2], argv[3])
    sys.stderr.write(f"error: unknown subcommand '{sub}'\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))