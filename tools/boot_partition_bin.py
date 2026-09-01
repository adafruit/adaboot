#!/usr/bin/env python3
"""Extract the boot ("mcuboot") partition image from a bootloader build.

``make build`` turns the bootloader's output into ``mcuboot.bin``, the payload
the bootloader-updater app embeds and writes over the boot partition. The naive
source, Zephyr's flat ``zephyr.bin``, is an objcopy image spanning *every*
loadable section's LMA -- so boards whose ELF carries sections outside the
code flash blow up. Renesas RA is the offender today: its ``.option_setting_*``
sections live at 0x0100a1xx (a separate option-bytes flash bank), padding the
flat binary to ~16 MB even though the code-flash image is ~25 KB. The updater
only ever rewrites the boot partition, so the correct payload is just the
bytes in the [boot partition start, +size) window.

This reads the build's ``zephyr.hex`` (address-based, so gap-free) plus its
``edt.pickle`` (the boot partition geometry the build actually used) and
writes that window to ``mcuboot.bin``, gap-filling with 0xFF. Boards whose
hex is missing fall back to a plain copy of ``zephyr.bin``.

Usage (from the ``build`` target):

    python3 tools/boot_partition_bin.py <boot-build-dir> [-o <out.bin>]
"""

import argparse
import pathlib
import shutil
import sys

MODULE_DIR = pathlib.Path(__file__).resolve().parent.parent
PYDT_SRC = MODULE_DIR / "deps" / "zephyr" / "scripts" / "dts" / "python-devicetree" / "src"


def _load_edt(build_dir):
    if not PYDT_SRC.exists():
        raise SystemExit(
            f"error: {PYDT_SRC} not found -- run 'make workspace' first to fetch "
            "the Adafruit Zephyr checkout (it ships python-devicetree)."
        )
    sys.path.insert(0, str(PYDT_SRC))

    edt_pickle = pathlib.Path(build_dir) / "zephyr" / "edt.pickle"
    if not edt_pickle.exists():
        raise SystemExit(
            f"error: {edt_pickle} not found -- run 'make build BOARD=<key>' "
            "first so the build generates it."
        )
    import pickle

    with edt_pickle.open("rb") as f:
        return pickle.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Write the boot partition's flash image from a bootloader build."
    )
    parser.add_argument("build_dir", help="bootloader build directory (e.g. build-nrf54l15dk)")
    parser.add_argument("-o", "--output", help="output file (default: <build_dir>/mcuboot.bin)")
    args = parser.parse_args()

    build = pathlib.Path(args.build_dir)
    out = pathlib.Path(args.output) if args.output else build / "mcuboot.bin"
    hex_path = build / "zephyr" / "zephyr.hex"
    bin_path = build / "zephyr" / "zephyr.bin"

    # Fallback: no hex means a single contiguous load region; the flat binary
    # already is the boot partition image.
    if not hex_path.exists():
        if not bin_path.exists():
            raise SystemExit(f"error: neither {hex_path} nor {bin_path} exists")
        shutil.copyfile(bin_path, out)
        print(f"copied {bin_path} to {out} (no hex to crop)")
        return 0

    edt = _load_edt(build)
    hits = [n for n in edt.nodes if "boot_partition" in n.labels]
    if not hits:
        raise SystemExit("error: no boot_partition node in the EDT")
    boot = hits[0]
    if not boot.regs:
        raise SystemExit("error: boot_partition has no reg")
    # A fixed-partitions child's reg is an *offset* within its parent flash
    # device (the ``partitions`` grouping node itself has no reg). The hex,
    # however, is address-based and carries absolute LMAs, so the boot
    # partition window in the hex is [parent_base + offset, parent_base + end).
    # Most boards map their code flash at 0x0 so base + offset == offset and
    # this is a no-op; Renesas RA (e.g. ek_ra8d1) maps it at 0x02000000, where
    # the offset alone (0x0) would find nothing and the build would abort.
    base = 0
    parent = boot.parent
    while parent is not None:
        if parent.regs:
            base = parent.regs[0].addr
            break
        parent = parent.parent
    start = base + boot.regs[0].addr
    size = boot.regs[0].size
    end = start + size

    from intelhex import IntelHex

    ih = IntelHex(str(hex_path))
    # Only the segments overlapping the boot partition window; everything else
    # (e.g. RA's option bytes at 0x0100a1xx) is a different flash bank the
    # updater cannot -- and must not -- touch.
    segs = [(s, e) for s, e in ih.segments() if s < end and e > start]
    if not segs:
        raise SystemExit(f"error: {hex_path} has no data in the boot partition window")
    # The image needs to cover only up to the last written byte; the rest of
    # the partition is left erased (the updater erases it first anyway).
    image_end = min(max(e for _, e in segs), end)
    data = ih.tobinstr(start=start, end=image_end - 1)

    if len(data) > size:
        raise SystemExit(
            f"error: boot partition image ({len(data)} bytes) does not fit "
            f"in the boot partition ({size} bytes)"
        )
    out.write_bytes(data)
    print(
        f"wrote {out} ({len(data)} bytes of the {size}-byte boot partition, "
        f"window {start:#x}-{end:#x})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())