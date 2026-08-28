<!--
  - SPDX-License-Identifier: Apache-2.0

  - Copyright (c) 2026 Scott Shawcroft for Adafruit Industries

  - Licensed to the Apache Software Foundation (ASF) under one
  - or more contributor license agreements.  See the NOTICE file
  - distributed with this work for additional information
  - regarding copyright ownership.  The ASF licenses this file
  - to you under the Apache License, Version 2.0 (the
  - "License"); you may not use this file except in compliance
  - with the License.  You may obtain a copy of the License at

  -  http://www.apache.org/licenses/LICENSE-2.0

  - Unless required by applicable law or agreed to in writing,
  - software distributed under the License is distributed on an
  - "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
  - KIND, either express or implied.  See the License for the
  - specific language governing permissions and limitations
  - under the License.
-->

# UF2 Drag-and-Drop Update

MCUboot supports firmware updates via [UF2](https://github.com/microsoft/uf2)
drag-and-drop over USB. When the bootloader update mode is active, the device
appears as a USB Mass Storage drive. Users copy a `.uf2` file onto the drive
to update firmware.

This is the standard update mechanism used in the CircuitPython / Adafruit
ecosystem and provides a simple, tool-free update experience.

## Combined mode: UF2 + serial recovery

When both UF2 (``CONFIG_MCUBOOT_UF2``) and serial recovery
(``CONFIG_MCUBOOT_SERIAL``) are enabled, there is a **single** bootloader
update mode, not two alternatives: entering the bootloader (button,
double-tap, boot-mode request, or no valid application) activates both
transports at the same time --

- the UF2 drag-and-drop drive (USB Mass Storage), and
- SMP (`mcumgr`) over the serial recovery port.

When the serial recovery port is CDC ACM on the device's native USB
controller (``CONFIG_BOOT_SERIAL_CDC_ACM``), both transports live on one
composite USB device: the host sees the mass storage drive *and* the serial
port simultaneously, sharing the UF2 USB identity
(``CONFIG_MCUBOOT_UF2_USB_VID``/``PID``). A board opts into this by enabling
both options and adding a ``zephyr,cdc-acm-uart`` node to its USB controller
(see ``dts/nordic/nrf54lm20dk.dtsi`` and ``conf/nrf54lm20dk.conf`` for an
example).

While the update mode is active, incoming UF2 blocks and incoming serial
commands are serviced concurrently; whichever transport receives a complete
image first reboots the device to apply it.

> **Note:** ``CONFIG_BOOT_SERIAL_WAIT_FOR_DFU`` is not available in the
> combined UF2 + CDC ACM configuration; use one of the entrance methods
> below instead.

## How it works

The update mode presents a virtual FAT16 filesystem over USB Mass Storage. The
filesystem is generated on the fly (a "ghost FAT") and contains:

- **INFO_UF2.TXT** — board name and bootloader version
- **INDEX.HTM** — redirects to the board's product page
- **CURRENT.UF2** — a UF2-formatted dump of the current flash contents
  (bootloader + application), useful for backup and cloning

When the host computer writes a `.uf2` file to the drive, MCUboot inspects
each 512-byte sector for UF2 magic numbers. Valid UF2 blocks have their
payloads extracted and written to flash. Non-UF2 writes (FAT metadata from
the host OS) are silently ignored.

Flash is erased progressively as data arrives, avoiding a long erase pause at
the start of the transfer.

Once all UF2 blocks have been received, MCUboot reboots the device to run
the new firmware.

## Slot modes

The target flash slot is determined by the existing MCUboot slot configuration:

- **Single slot** (``CONFIG_SINGLE_APPLICATION_SLOT``): The UF2 payload is
  written directly to the primary slot and the device reboots into the new
  image.

- **Dual slot** (default): The UF2 payload is written to the secondary slot.
  MCUboot marks the image as pending (``boot_set_pending(0)``) and reboots.
  The normal MCUboot swap mechanism then applies the update.

In both cases, the UF2 file must contain a complete, signed MCUboot image
(header + firmware + TLVs).

## Creating UF2 files

Use ``imgtool`` to sign your firmware, then convert the signed image to UF2
format with the upstream ``uf2conv.py`` converter (vendored at
``tools/uf2conv.py`` from the
`microsoft/uf2 <https://github.com/microsoft/uf2>`_ project):

``` console
# 1. Sign the image as usual
imgtool sign -k my_key.pem --align 4 -v 1.0.0 -H 0x200 -S 0x60000 \
    app.bin signed.bin

# 2. Convert to UF2 (use -c so it writes a file instead of flashing a drive)
python3 tools/uf2conv.py -c -b 0x10000 -f 0xADA32D \
    -o firmware.uf2 signed.bin
```
### uf2conv.py options

| Option | Description |
|--------|-------------|
| ``-b`` / ``--base`` | Target base address for the image in flash (default: ``0x2000``) |
| ``-f`` / ``--family`` | UF2 family ID (number or name); ``0`` skips the family check (default: ``0x0``) |
| ``-o`` / ``--output`` | Output file path |
| ``-c`` / ``--convert`` | Convert only; write the file and do not try to flash a mounted drive |

The base address should match the start of the target flash slot (primary for
single-slot, secondary for dual-slot).

The family ID is an optional safeguard. When both the UF2 file and the
bootloader specify a non-zero family ID, blocks with a mismatched ID are
silently ignored.

### Binding images to a board (board identity)

The family ID (``-f`` / ``CONFIG_MCUBOOT_UF2_FAMILY_ID``) is shared
across an entire product line. To bind a UF2 file to a **specific board
variant**, use the board identity (``CONFIG_MCUBOOT_UF2_BOARD_ID``):
a ``<vendor>_<board>`` string, one value per board. The bootloader accepts a
block only when its board identity matches, so firmware built for one board is
silently ignored on another.

Embed the board identity with the converter's ``--ext`` option, using the
Adaboot board-id tag type ``0x4D7C3A``:

``` console
python3 tools/uf2conv.py -c -b 0x10000 -f 0xADA32D \
    --ext 0x4D7C3A:adafruit_feather_nrf52840 \
    -o firmware.uf2 signed.bin
```

and in the board's bootloader config:

``` cfg
CONFIG_MCUBOOT_UF2_FAMILY_ID=0xADA32D
CONFIG_MCUBOOT_UF2_BOARD_ID="adafruit_feather_nrf52840"
```

The board identity is carried as a [UF2 extension tag](https://github.com/microsoft/uf2#extension-tags)
(official flag ``0x00008000``, no new top-level flag). Each UF2 block carries
one board-identity tag — a 24-bit type (Adaboot's ``0x4D7C3A``, chosen at
random outside the standard set) plus a UTF-8 ``<vendor>_<board>`` payload —
right after the payload, terminated by a 4-byte zero record. The bootloader
parses the tags in ``uf2_process_block`` and rejects blocks whose board
identity does not match ``CONFIG_MCUBOOT_UF2_BOARD_ID``. Leave the config
empty (and omit ``--ext``) to disable the check.

> **Note:** because Adaboot does not verify image signatures
(``CONFIG_BOOT_SIGNATURE_TYPE_NONE``), the board identity is a safeguard
against **accidental** cross-board flashes, not a security boundary. A
``.uf2`` can be edited, or the raw binary flashed over SWD/JTAG, to bypass it.
(A re-flash via ``CURRENT.UF2`` readback does carry the board identity, so a
same-board backup restores cleanly.)

## Enabling UF2 mode (Zephyr)

### Kconfig

Add the following to your MCUboot configuration (or use the provided
``boot/zephyr/uf2.conf`` sample):

``` cfg
CONFIG_MCUBOOT_UF2=y
CONFIG_MCUBOOT_UF2_NO_APPLICATION=y

# USB identity of the bootloader device
CONFIG_MCUBOOT_UF2_USB_VID=0x239A
CONFIG_MCUBOOT_UF2_USB_PID=0x002F

# USB requires a preemptible main thread
CONFIG_MAIN_THREAD_PRIORITY=0

# Optional: combine with SMP serial recovery over native USB
# CONFIG_MCUBOOT_SERIAL=y
# CONFIG_BOOT_SERIAL_CDC_ACM=y
```

``CONFIG_MCUBOOT_UF2=y`` automatically selects ``USB_DEVICE_STACK_NEXT``,
``USBD_MSC_CLASS``, ``DISK_ACCESS``, and ``REBOOT``.

### Device tree overlay

Your board must have a USB device controller enabled. Many boards enable USB
by default; if not, use an overlay like the provided
``boot/zephyr/usb_msc_uf2.overlay``:

``` dts
&usbd {
    status = "okay";
};
```

### Building

``` console
west build -b <your_board> boot/zephyr -- \
    -DEXTRA_CONF_FILE=uf2.conf \
    -DDTC_OVERLAY_FILE=usb_msc_uf2.overlay
```

## Configuration reference

| Kconfig option | Type | Default | Description |
|----------------|------|---------|-------------|
| ``MCUBOOT_UF2`` | bool | n | Enable UF2 drag-and-drop update mode |
| ``MCUBOOT_UF2_FAMILY_ID`` | hex | 0x0 | UF2 family ID to accept (0 = any) |
| ``MCUBOOT_UF2_BOARD_ID`` | string | "" | Per-board identity to accept ("<vendor>_<board>"; empty = any) |
| ``MCUBOOT_UF2_BOARD_NAME`` | string | ``BOARD`` | Board name shown in INFO_UF2.TXT |
| ``MCUBOOT_UF2_BOARD_URL`` | string | ``https://mcuboot.com`` | URL for INDEX.HTM redirect |
| ``MCUBOOT_UF2_DISK_NAME`` | string | ``UF2`` | Disk name registered with disk_access (exposed as the MSC LUN) |
| ``MCUBOOT_UF2_MAX_BLOCKS`` | int | 4096 | Max UF2 blocks (4096 = 1 MB max image) |
| ``MCUBOOT_UF2_VALIDATE_AFTER_WRITE`` | bool | y | Validate image header after write completes |

### Entrance methods

| Kconfig option | Description |
|----------------|-------------|
| ``MCUBOOT_UF2_ENTRANCE_GPIO`` | Enter the update mode when a GPIO pin is asserted at boot |
| ``MCUBOOT_UF2_ENTRANCE_BOOT_MODE`` | Enter the update mode via the Zephyr boot mode retention subsystem |
| ``MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP`` | Enter the update mode on a double tap of the reset button |
| ``MCUBOOT_UF2_NO_APPLICATION`` | Enter the update mode if no valid application is found |

Multiple entrance methods can be enabled simultaneously. When serial recovery
(``CONFIG_MCUBOOT_SERIAL``) is enabled too, the *serial recovery* entrance
methods (``BOOT_SERIAL_ENTRANCE_GPIO``, ``BOOT_SERIAL_DOUBLE_TAP``,
``BOOT_SERIAL_BOOT_MODE``, ``BOOT_SERIAL_NO_APPLICATION``, ...) enter this
same combined mode — there is no separate serial-only or UF2-only mode to
choose between.

## Updating firmware

1. Enter the bootloader update mode (GPIO button, double-tap, boot-mode
   request, or no application present).
2. A USB drive named **UF2 BOOT** appears on your computer.
3. Copy the `.uf2` file to the drive.
4. The device automatically reboots into the new firmware.

## CURRENT.UF2 — flash readback

When UF2 mode is active, the virtual drive contains a **CURRENT.UF2** file
that captures the entire flash contents from address 0 through the end of
the primary application slot. This includes the bootloader and the currently
installed application.

Copying CURRENT.UF2 to your computer gives you a complete UF2-formatted
backup of the device. This file can be:

- Dragged back onto the same device (or another identical device) to restore
  the exact flash state
- Used for cloning devices in production
- Inspected with UF2 tools to verify flash contents

The readback is generated on the fly — each sector read from the CURRENT.UF2
region triggers a flash read and wraps the data into a UF2 block.

## Can UF2 update the bootloader?

No. The bootloader (MCUboot) is actively executing when UF2 mode is active,
so it cannot safely overwrite its own code. UF2 updates target the application
slot(s) only.

To update the bootloader itself, use SWD/JTAG or a two-stage bootloader
design where an immutable first-stage bootloader can update MCUboot.

However, CURRENT.UF2 *does* include the bootloader in its readback, so you
can back up the complete flash state (bootloader + application) by copying
CURRENT.UF2 from the drive.

## Architecture

```
USB Host  -->  Zephyr USB MSC  -->  disk_access API  -->  uf2_disk.c
                                                            |
                                                   ghostfat.c / boot_uf2.c
                                                            |
                                                      flash_area_* APIs
```

The implementation follows the ``boot_serial`` pattern with a platform-independent
library in ``boot/boot_uf2/`` and Zephyr integration in ``boot/zephyr/``:

- **boot/boot_uf2/src/boot_uf2.c** — UF2 block validation, progressive erase,
  flash writes, block tracking via bitmask
- **boot/boot_uf2/src/ghostfat.c** — Virtual FAT16 filesystem generation
- **boot/zephyr/uf2_disk.c** — Zephyr ``disk_operations`` backend bridging
  the ghost FAT to flash via ``flash_area_*`` APIs
- **boot/zephyr/usbd_boot.c** — the bootloader's single USB device setup:
  one ``USBD`` context registers every enabled update transport class (the
  UF2 mass storage LUN and/or the CDC ACM serial recovery port), so with
  native USB both are active at once in one bootloader mode

No custom USB protocol code is needed — Zephyr's existing USB device stack
and classes handle all USB details.
