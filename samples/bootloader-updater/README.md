# Bootloader updater

A Zephyr application that **self-updates the bootloader**. It is an ordinary
slot0 application the bootloader (mcuboot) writes into the primary slot and
boots; on boot it overwrites the board's `mcuboot` (boot) partition with a fresh
mcuboot image it carries embedded inside itself.

This is the Adaboot replacement for a dedicated bootloader flasher: instead of
needing a debugger or a host tool to update the bootloader, you flash one
ordinary application and the bootloader updates itself.

## How it works

1. `make build BOARD=<key>` builds the bootloader into `build-<key>/mcuboot.bin`.
2. `make updater BOARD=<key>` builds this application with that `mcuboot.bin`
   embedded as a C array (`mcuboot_image[]` in `src/main.c`, generated from
   `mcuboot.bin` by `generate_inc_file_for_target` in `CMakeLists.txt`).
3. Flash `build-<key>-updater/zephyr/zephyr.signed.bin` to **slot0** -- via UF2
   drag-and-drop, serial recovery, or a debugger -- exactly like any other
   application image.
4. mcuboot validates and boots the updater. The updater:
   - compares the boot partition to the embedded image and **skips** the write
     if they already match (so re-running it is harmless and never wears flash);
   - otherwise erases the `mcuboot` partition, writes the embedded image, and
     verifies it by reading back;
   - if the board has a `zephyr,boot-mode` retention area, sets it to
     "bootloader" so the next boot stays in the bootloader's serial/UF2 recovery
     mode (the user can then reflash their real application);
   - reboots.

The output is a hash-only (unsigned) mcuboot image, matching the
hash-only bootloader this fork builds by default
(`CONFIG_BOOT_SIGNATURE_TYPE_NONE` + `CONFIG_MCUBOOT_GENERATE_UNSIGNED_IMAGE`).

## Building

From the repo root (after `make workspace`):

```
make updater BOARD=nrf54l15dk
```

This runs `make build` first (to produce the embedded `mcuboot.bin`) and then
builds the updater. Output:

```
build-nrf54l15dk-updater/zephyr/zephyr.signed.bin
```

Flash that to slot0. For a UF2-capable bootloader (`conf/<key>.conf` with
`CONFIG_MCUBOOT_UF2=y`), `make uf2 BOARD=<key>` instead produces a
`.uf2` you can drag onto the bootloader's USB drive:

```
make uf2 BOARD=nrf54lm20dk
# -> build-nrf54lm20dk-updater/mcuboot-updater.uf2
```

See [docs/standalone-build.md](../docs/standalone-build.md) "Updater UF2 files".

For a manual build (without the Makefile):

```
west build -b nrf54l15dk/nrf54l15/cpuapp samples/bootloader-updater -- \
    -DEXTRA_ZEPHYR_MODULES=$PWD \
    -DEXTRA_DTC_OVERLAY_FILE=$PWD/dts/nordic/nrf54l15dk.dtsi \
    -DMCUBOOT_IMAGE_BIN=$PWD/build-nrf54l15dk/mcuboot.bin
```

The updater's configuration (single-app signing, SPI_NOR, XIP) is entirely in
its own `prj.conf`, so no extra conf fragment is needed.

## Layout it assumes

Like every Adaboot application, this one references the shared partition roles
owned by this fork's `dts/<vendor>/<board>.dtsi` (applied via
`-DEXTRA_DTC_OVERLAY_FILE`):

| role        | used for                                            |
|-------------|-----------------------------------------------------|
| `mcuboot`   | the boot partition the updater **overwrites**       |
| `image-0`   | slot0, where the updater **links** and is booted from |

`app.overlay` points `zephyr,code-partition` at `slot0_partition` (the
bootloader's own `app.overlay` instead points it at `boot_partition`).

## Notes and caveats

- **Run-once / recovery**: setting the boot-mode retention register only breaks
  the reboot loop if the *bootloader* is built to honor it (serial/UF2 entrance
  via boot mode). The standalone `make build` produces a minimal bootloader with
  those entrances off, so on that image the updater just reboots and re-runs --
  but the skip-if-matches check means no flash erase/write happens on the
  re-run. Reflash your real application to leave the updater.
- **Erasing the boot partition while executing from slot0**: on devices where
  the boot partition and slot0 share one flash bank, erasing the boot partition
  can stall (or, on some MCUs, fault) the CPU while it is executing from slot0.
  Adaboot's single-slot boards (e.g. nRF54L RRAM) tolerate this.
- **Bricking risk**: a failed write or power loss mid-update leaves the boot
  partition partially erased -- the device will not boot until the bootloader
  is recovered with a debugger. The updater erases the whole partition up
  front, then writes and verifies before rebooting.