# Building Adaboot standalone

Adaboot is a [Zephyr sysbuild module][dts-readme]: normally an *application*
pulls this repo into its west manifest and sysbuild applies the fork's flash
partition layout to both the bootloader and the application. This directory
documents how to build **just the bootloader** from this repo, with one command,
using the [Adafruit fork of Zephyr][adafruit-zephyr] -- the same Zephyr
CircuitPython's `zephyr-cp` port builds against.

[dts-readme]: ../dts/README.md
[adafruit-zephyr]: https://github.com/adafruit/zephyr

## What "standalone" means here

The bootloader (`boot/zephyr`) is an ordinary Zephyr application, so it cannot
build without Zephyr and its HALs. "Standalone" therefore means: one `make`
command from this repo sets up a west workspace (Adafruit Zephyr + HALs under
`deps/`) and builds `boot/zephyr` for any board this fork owns a layout for -- no
separate west manifest of your own, no copying the repo elsewhere.

The setup mirrors CircuitPython's `zephyr-cp` port: a `zephyr-config/west.yml`
manifest (generated from `zephyr-config/west.yml.in`) imports the Adafruit Zephyr
fork. Two differences from a plain Zephyr import:

- Zephyr is imported with `path-prefix: deps` so its projects land under `deps/`
  and never collide with this repo's own `zephyr/` (the Zephyr *module* metadata)
  or `tools/` directories.
- Zephyr's manifest would normally also import the upstream `mcuboot` project.
  It is block-listed: **this repository is the mcuboot fork**, and is registered
  as the Zephyr module directly -- the live working tree -- via
  `EXTRA_ZEPHYR_MODULES`. So `make build` compiles the tree you are editing, and
  there is no second mcuboot checkout (which would otherwise create a duplicate
  `MCUBOOT_BOOTUTIL` target).

## Prerequisites

- `west` (e.g. `pip install west`).
- A Zephyr toolchain. The Zephyr SDK is the easiest; see the Zephyr getting
  started guide. The build picks the toolchain the way any Zephyr build does
  (`ZEPHYR_SDK_INSTALL_DIR`, `ZEPHYR_TOOLCHAIN_VARIANT`, etc.).
- `python3 >= 3.11` (the helper uses `tomllib`).

## One-time setup

```
make workspace
```

This generates `zephyr-config/west.yml` from the template (pinning the Adafruit
Zephyr revision), runs `west init -l zephyr-config`, and `west update` -- cloning
Adafruit Zephyr and the HALs into `deps/`. It is slow the first time; subsequent
builds are offline.

To use a different Adafruit Zephyr revision:

```
make workspace ZEPHYR_REV=<sha>
```

## Build one board

The board keys are the partition-layout names in `dts/<vendor>/` (the dtsi
filename stem), which are the same keys in `tools/boards.toml` and
`dts/mcuboot_boards.cmake`:

```
make list
make build BOARD=nrf54l15dk
make updater BOARD=nrf54l15dk
```

`make build` produces the bootloader. `make updater` builds the bootloader and
then [samples/bootloader-updater](../samples/bootloader-updater/README.md) --
an ordinary slot0 application that embeds that `mcuboot.bin` and, when booted,
overwrites the `mcuboot` (boot) partition with it (a self-update of the
bootloader). Its output, `build-<key>-updater/zephyr/zephyr.signed.bin`, is a
hash-only mcuboot image you flash to slot0 (UF2 / serial recovery / debugger).

Output lands in `build-nrf54l15dk/`:

- `mcuboot.bin` / `mcuboot.hex` -- copies of `zephyr/zephyr.{bin,hex}`
- `zephyr/zephyr.elf` -- the ELF

What `make build` actually runs is, for `nrf54l15dk`:

```
west build -b nrf54l15dk/nrf54l15/cpuapp -d build-nrf54l15dk boot/zephyr -- \
  -DEXTRA_ZEPHYR_MODULES=$PWD \
  -DEXTRA_DTC_OVERLAY_FILE=$PWD/dts/nordic/nrf54l15dk.dtsi \
  -DEXTRA_CONF_FILE=$PWD/conf/mode-single_app.conf
```

- `EXTRA_ZEPHYR_MODULES` makes this repo the mcuboot Zephyr module (so the
  `MCUBOOT_BOOTUTIL` library and the sysbuild entries come from this tree).
- `EXTRA_DTC_OVERLAY_FILE` applies the board's partition layout *after*
  `boot/zephyr/app.overlay` (which sets the bootloader's own code partition).
- `EXTRA_CONF_FILE` sets the mode and signature defaults (see below).

## Modes and signatures

Each board's `tools/boards.toml` entry has a `mcuboot_mode` (default
`single_app`), which selects a conf fragment in `conf/`:

| mode          | conf fragment            | sets                                            |
|---------------|--------------------------|-------------------------------------------------|
| `single_app`  | `conf/mode-single_app.conf` | `CONFIG_SINGLE_APPLICATION_SLOT=y`           |
| `ram_load`    | `conf/mode-ram_load.conf`    | `CONFIG_BOOT_RAM_LOAD=y`                     |

Both also set `CONFIG_BOOT_SIGNATURE_TYPE_NONE=y`. Adaboot is a UF2 /
serial-recovery bootloader, not a secure boot chain, so the bootloader image is
built hash-only. To add image verification, set a signature type and key in a
board-specific conf fragment instead.

These are the same defaults the fork's `dts/Kconfig.sysbuild` applies under
sysbuild (`SB_CONFIG_MCUBOOT_MODE_*` -> `CONFIG_*`), translated for a direct
`boot/zephyr` build. See `tools/standalone_build.py` for the mapping.

## Build every board

```
make all                       # build every board, continue on error
make all STOP_ON_ERROR=1       # stop on the first failing board
make all BOARDS='nrf54l15dk nucleo_u575zi_q'   # build a subset
```

## Other targets

```
make menuconfig BOARD=nrf54l15dk
make flash BOARD=nrf54l15dk
make clean BOARD=nrf54l15dk      # remove one build dir
make clean-all                   # remove every build-* dir
make clean-workspace              # remove deps/ and .west (keeps the repo)
make update                      # re-run west update (e.g. after bumping ZEPHYR_REV)
```

## Notes

- `make build` builds a **minimal** bootloader (boot slot0, swap/ram-load per
  mode, hash-only). UF2 and serial recovery are board-specific (they need a USB
  or UART backend) and are not enabled by the standalone conf fragments; enable
  `CONFIG_MCUBOOT_SERIAL` / `CONFIG_MCUBOOT_UF2` in a board conf fragment when
  the board has the hardware.
- The partition layout comes from this fork's `dts/`; if you edit a dtsi you see
  the change immediately (the overlay is read from this tree, not from `deps/`).
- Everything under `deps/`, `.west/`, `build-*/`, and the generated
  `zephyr-config/west.yml` is git-ignored.