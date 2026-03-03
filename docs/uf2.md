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
drag-and-drop over USB. When UF2 mode is active, the device appears as a USB
Mass Storage drive. Users copy a `.uf2` file onto the drive to update firmware.

This is the standard update mechanism used in the CircuitPython / Adafruit
ecosystem and provides a simple, tool-free update experience.

## How it works

UF2 mode presents a virtual FAT16 filesystem over USB Mass Storage. The
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
format:

``` console
# 1. Sign the image as usual
imgtool sign -k my_key.pem --align 4 -v 1.0.0 -H 0x200 -S 0x60000 \
    app.bin signed.bin

# 2. Convert to UF2
imgtool uf2 --base-addr 0x10000 --family-id 0xADA32D signed.bin firmware.uf2
```

### imgtool uf2 options

| Option | Description |
|--------|-------------|
| ``-b`` / ``--base-addr`` | Target base address for the image in flash (required) |
| ``-f`` / ``--family-id`` | UF2 family ID; set to 0 to skip family check (default: 0) |
| ``-p`` / ``--payload-size`` | Bytes of payload per UF2 block (default: 256) |

The base address should match the start of the target flash slot (primary for
single-slot, secondary for dual-slot).

The family ID is an optional safeguard. When both the UF2 file and the
bootloader specify a non-zero family ID, blocks with a mismatched ID are
silently ignored.

## Enabling UF2 mode (Zephyr)

### Kconfig

Add the following to your MCUboot configuration (or use the provided
``boot/zephyr/uf2.conf`` sample):

``` cfg
CONFIG_MCUBOOT_UF2=y
CONFIG_MCUBOOT_UF2_NO_APPLICATION=y

# Disk name must match
CONFIG_MASS_STORAGE_DISK_NAME="UF2"

# USB device descriptors
CONFIG_USB_DEVICE_PRODUCT="MCUboot UF2"
CONFIG_USB_DEVICE_VID=0x239A
CONFIG_USB_DEVICE_PID=0x0035

# USB requires a preemptible main thread
CONFIG_MAIN_THREAD_PRIORITY=0
```

``CONFIG_MCUBOOT_UF2=y`` automatically selects ``USB_DEVICE_STACK``,
``USB_MASS_STORAGE``, ``DISK_ACCESS``, and ``REBOOT``.

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
| ``MCUBOOT_UF2_BOARD_NAME`` | string | ``BOARD`` | Board name shown in INFO_UF2.TXT |
| ``MCUBOOT_UF2_BOARD_URL`` | string | ``https://mcuboot.com`` | URL for INDEX.HTM redirect |
| ``MCUBOOT_UF2_DISK_NAME`` | string | ``UF2`` | Disk name (must match ``MASS_STORAGE_DISK_NAME``) |
| ``MCUBOOT_UF2_MAX_BLOCKS`` | int | 4096 | Max UF2 blocks (4096 = 1 MB max image) |
| ``MCUBOOT_UF2_VALIDATE_AFTER_WRITE`` | bool | y | Validate image header after write completes |

### Entrance methods

| Kconfig option | Description |
|----------------|-------------|
| ``MCUBOOT_UF2_ENTRANCE_GPIO`` | Enter UF2 mode when a GPIO pin is asserted at boot |
| ``MCUBOOT_UF2_ENTRANCE_BOOT_MODE`` | Enter UF2 mode via the Zephyr boot mode retention subsystem |
| ``MCUBOOT_UF2_NO_APPLICATION`` | Enter UF2 mode if no valid application is found |

Multiple entrance methods can be enabled simultaneously.

## Updating firmware

1. Enter UF2 mode (GPIO button, boot mode request, or no application present).
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

No custom USB code is needed — Zephyr's existing USB Mass Storage class handles
all USB protocol details.
