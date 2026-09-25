#!/usr/bin/env python3
"""Resolve Adaboot board build parameters for the standalone Makefile.

The Makefile builds ``boot/zephyr`` (the MCUboot Zephyr app) for every board
this fork owns the flash layout for. Boards are addressed by their
CircuitPython-style board id, ``<vendor>_<board>`` (e.g. ``nordic_nrf54l15dk``):
the prefix names the vendor directory the board's layout dtsi and conf
fragments live in, and stripping it yields the *partition key* (the dtsi
filename stem), which maps to a canonical Zephyr board id and a
partition-layout overlay declared in ``tools/boards.toml`` and
``dts/<vendor>/<board>.dtsi``.

Subcommands
-----------
list
    Print every board id that boots via mcuboot (one per line). This is the
    set ``make all`` builds.
get <board> <field>
    Print a single value for ``<board>`` (a ``<vendor>_<board>`` id). ``field``
    is one of:

        west_board   canonical Zephyr board id, e.g. nrf54l15dk/nrf54l15/cpuapp
        overlay      absolute path to dts/<vendor>/<board>.dtsi
        mode         single_app | overwrite_only
        board_conf   absolute path to conf/<vendor>/<board>.conf, or an empty line if
                     the board has no board-specific conf fragment

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
CONF_DIR = MODULE_DIR / "conf"

# boards.toml mcuboot_mode value -> the Zephyr Kconfig the standalone build sets.
# Mirrors Zephyr's sysbuild image_configurations/BOOTLOADER_image_default.cmake
# (SB_CONFIG_MCUBOOT_MODE_* -> CONFIG_*), but for a direct boot/zephyr build
# rather than a sysbuild. "single_app" boots a single slot (no OTA secondary);
# "overwrite_only" copies an update from slot1 onto slot0 without rollback.
MODE_CONFIG = {
    "single_app": "CONFIG_SINGLE_APPLICATION_SLOT=y",
    "overwrite_only": "CONFIG_BOOT_UPGRADE_ONLY=y",
}


def load_boards():
    with BOARDS_TOML.open("rb") as f:
        data = tomllib.load(f)
    return data.get("boards", {})


def cmd_list():
    boards = load_boards()
    for key in boards:
        if boards[key].get("mcuboot", True):
            print(board_id(key, boards[key]))
    return 0


def board_id(key, entry):
    """The CircuitPython-style <vendor>_<board> id for a boards.toml entry.

    Keys that already start with the vendor name (e.g. ``adafruit_feather_rp2040``)
    are their own id; the vendor prefix is not doubled.
    """
    vendor = entry.get("vendor")
    if not vendor or key.startswith(f"{vendor}_"):
        return key
    return f"{vendor}_{key}"


def canonical_key(board, boards):
    """Map a board id to its boards.toml partition key, or None if unknown.

    Accepts the CircuitPython-style ``<vendor>_<board>`` id (the prefix must
    match the entry's vendor) and, for convenience, a bare partition key.
    """
    if board in boards:
        return board
    vendor, sep, bare = board.partition("_")
    if sep and bare in boards and boards[bare].get("vendor") == vendor:
        return bare
    return None


def cmd_list_all():
    boards = load_boards()
    for key in boards:
        entry = boards[key]
        marker = "" if entry.get("mcuboot", True) else " *"
        print(f"{board_id(key, entry)}{marker}")
    return 0


FIELDS = {"west_board", "overlay", "mode", "board_conf"}


def resolve(board):
    """Return (west_board, overlay, mode, board_conf) for a board id.

    ``board`` is a CircuitPython-style ``<vendor>_<board>`` id (a bare
    partition key is also accepted, e.g. when scripting against boards.toml).
    Only mcuboot-booting boards are resolvable: standalone (mcuboot = false)
    boards have a hand-maintained layout but no mcuboot bootloader to build.

    ``board_conf`` is the absolute path to ``conf/<vendor>/<key>.conf`` if such a
    board-specific conf fragment exists, else an empty string. The standalone
    Makefile appends it (after the mode conf) to EXTRA_CONF_FILE so a board can
    opt into UF2 / serial recovery / no-application fallback without forcing
    those (USB/UART-dependent) features on every board.
    """
    boards = load_boards()
    key = canonical_key(board, boards)
    if key is None:
        known = " ".join(board_id(k, v) for k, v in sorted(boards.items()))
        raise ValueError(
            f"'{board}' is not a board this fork owns a layout for "
            f"(not in {BOARDS_TOML.relative_to(MODULE_DIR)}). "
            f"Known: {known}"
        )
    if not boards[key].get("mcuboot", True):
        raise ValueError(
            f"'{board}' is a standalone board (mcuboot = false): it has no mcuboot "
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
    board_conf = ""
    if vendor:
        candidate = CONF_DIR / vendor / f"{key}.conf"
        board_conf = str(candidate.resolve()) if candidate.exists() else ""
    return west_board, str(overlay), mode, board_conf


def cmd_get(board, field):
    if field not in FIELDS:
        sys.stderr.write(f"error: unknown field '{field}'; one of {' '.join(sorted(FIELDS))}\n")
        return 2
    try:
        west_board, overlay, mode, board_conf = resolve(board)
    except ValueError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    print({"west_board": west_board, "overlay": overlay, "mode": mode,
           "board_conf": board_conf}[field])
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
            sys.stderr.write("usage: standalone_build.py get <vendor>_<board> <field>\n")
            return 2
        return cmd_get(argv[2], argv[3])
    sys.stderr.write(f"error: unknown subcommand '{sub}'\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))