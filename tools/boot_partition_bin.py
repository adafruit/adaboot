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

Bootloaders whose SoC layer signs a loader image post-build (STM32N6's
FSBL flow: the SigningTool prepends the header the boot ROM needs) produce
``zephyr.signed.bin`` — that is the flashable bootloader image, so it is the
preferred payload when present.

RAM-linked bootloaders (CONFIG_BOOT_RAM_LOAD, or the N6's non-XIP FSBL) link
at a RAM load address, so the hex carries no segments in the boot partition's
flash-address window. There the boot partition simply stores the whole linked
image, i.e. the flat ``zephyr.bin`` (or the signed variant above).

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


def _is_ram_load(build_dir):
    """True if the build is a RAM-load (CONFIG_BOOT_RAM_LOAD) build."""
    config = pathlib.Path(build_dir) / "zephyr" / ".config"
    if not config.exists():
        return False
    text = config.read_text()
    return ("CONFIG_BOOT_RAM_LOAD=y" in text
            or "CONFIG_SINGLE_APPLICATION_SLOT_RAM_LOAD=y" in text)


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

    build = pathlib.Path(args.build_dir)
    out = pathlib.Path(args.output) if args.output else build / "mcuboot.bin"
    hex_path = build / "zephyr" / "zephyr.hex"
    bin_path = build / "zephyr" / "zephyr.bin"
    signed_path = build / "zephyr" / "zephyr.signed.bin"

    def _fit_check(data):
        """Fail if the payload would not fit the boot partition (best effort)."""
        try:
            edt = _load_edt(build)
            boot = [n for n in edt.nodes if "boot_partition" in n.labels][0]
            size = boot.regs[0].size
            if len(data) > size:
                raise SystemExit(
                    f"error: boot partition image ({len(data)} bytes) does not "
                    f"fit in the boot partition ({size} bytes)"
                )
            return size
        except SystemExit:
            raise
        except Exception:
            return None

    # Preferred: an SoC-signed loader image (e.g. the STM32N6 FSBL header +
    # image produced by the SigningTool post-build step). The boot ROM needs
    # the header to load the bootloader from the boot partition, so the
    # partition must store the signed image, not the raw one.
    if signed_path.exists():
        data = signed_path.read_bytes()
        size = _fit_check(data)
        out.write_bytes(data)
        if size is not None:
            print(
                f"copied {signed_path} to {out} ({len(data)} bytes of the "
                f"{size}-byte boot partition; SoC-signed loader image)"
            )
        else:
            print(f"copied {signed_path} to {out} (SoC-signed loader image)")
        return 0

    # RAM-load build: the image is linked at its RAM load address, so the
    # hex never overlaps the boot partition's flash-address window. The boot
    # partition stores the whole linked image; the flat binary already is it.
    if _is_ram_load(build):
        if not bin_path.exists():
            raise SystemExit(f"error: {bin_path} does not exist")
        data = bin_path.read_bytes()
        size = _fit_check(data)
        out.write_bytes(data)
        print(
            f"copied {bin_path} to {out} ({len(data)} bytes"
            + (f" of the {size}-byte boot partition" if size is not None else "")
            + "; RAM-load image, linked at its RAM load address)"
        )
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
        # No overlap: the image is linked outside the boot partition's
        # flash-address window (e.g. a non-XIP bootloader that a ROM loader
        # copies to RAM). The partition then stores the whole linked image.
        if not bin_path.exists():
            raise SystemExit(
                f"error: {hex_path} has no data in the boot partition window "
                f"and {bin_path} does not exist"
            )
        data = bin_path.read_bytes()
        size = _fit_check(data)
        out.write_bytes(data)
        print(
            f"copied {bin_path} to {out} ({len(data)} bytes; image linked "
            "outside the boot partition window -- RAM-resident bootloader)"
        )
        return 0
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