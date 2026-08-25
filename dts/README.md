# Partition layouts

This directory holds the canonical flash partition layout for every board this
bootloader fork supports, so CircuitPython, Arduino, and Wippersnapper share one
memory map and cannot drift. Layouts are organized like the Zephyr board folders:

```
dts/<vendor>/<board>.dtsi
```

Each `<board>.dtsi` is a **self-contained devicetree overlay** that deletes any
Zephyr-default `partitions` node it replaces, enables the flash devices it uses,
sets up bootloader UI the bootloader needs, and defines the partition map with
these shared role labels:

| label        | node label             | used by                       |
|--------------|------------------------|-------------------------------|
| `mcuboot`    | `boot_partition`       | bootloader                    |
| `image-0`    | `slot0_partition`      | app slot (links here)         |
| `image-1`    | `slot1_partition`      | OTA update slot               |
| `image-2`    | `netcore_partition`    | net/radio core firmware       |
| `storage`    | `storage_partition`    | Zephyr settings / BT bonding  |
| `nvm`        | `nvm_partition`        | raw non-volatile byte access  |
| `filesystem` | `filesystem_partition` | user filesystem (CIRCUITPY / Arduino data / Wip) |

These are the **only** labels applications should reference (via
`FIXED_PARTITION_ID(<node_label>)`). The geometry is owned here, not by any
application.

## mcuboot vs standalone

A board either boots via mcuboot (Adaboot) or it doesn't. The fork declares which
boards boot via mcuboot in `dts/mcuboot_boards.cmake` (generated from
`tools/boards.toml` by `tools/partition_layout.py --gen-list`):

- **mcuboot boards** (listed in `mcuboot_boards.cmake`) — the overlay is applied
  to **both** the mcuboot image and the application image, and a mcuboot domain
  is built. Their layout defines (or appends to) `mcuboot`/`image-0`/`image-1`.
- **standalone boards** (everything else with a layout here) — no mcuboot domain;
  the overlay is applied to the application image only. These include UF2-native
  boards (RP2), XIP/direct-boot boards (SiWG917, STM32H750B-DK), and boards not
  yet on mcuboot (nRF54LM20 DK). Their layout is hand-maintained (the planner
  emits `fixed-partitions` for mcuboot boards, not XIP `mapped-partition`
  layouts), so they are not in `tools/boards.toml`.

## Consuming the layout

Applications never `#include` a layout and don't keep their own copy. With Zephyr
sysbuild, the dtsi key is the Zephyr board name (the part of the board id before
the first `/` or `@`); the vendor is globbed so sysbuild doesn't need to know it:

```cmake
string(REGEX REPLACE "[@/].*" "" _key "${BOARD}")
file(GLOB _overlay CONFIGURE_DEPENDS
     "${ZEPHYR_MCUBOOT_MODULE_DIR}/dts/*/${_key}.dtsi")
include("${ZEPHYR_MCUBOOT_MODULE_DIR}/dts/mcuboot_boards.cmake")
if(_overlay)
  set(<app-image>_EXTRA_DTC_OVERLAY_FILE ${_overlay} CACHE INTERNAL "")
  if(${_key} IN_LIST MCUBOOT_BOARDS)
    set(mcuboot_EXTRA_DTC_OVERLAY_FILE ${_overlay} CACHE INTERNAL "")
    # ... enable MCUboot (adaboot.conf + board confs) ...
  endif()
endif()
```

## Regenerating

The partition *numbers* for mcuboot boards are planned by
`tools/partition_layout.py`, which runs a `west build --cmake-only` to read the
board's flash topology from Zephyr and emit the `&<dev> { partitions { ... } }`
blocks. Board-specific glue (device enables, retention cell, chosen/aliases,
`/delete-node/`) is hand-maintained in the same dtsi around the generated
blocks. Run from any west workspace that contains Zephyr and this module:

```sh
python3 bootloader/mcuboot/tools/partition_layout.py --list
python3 bootloader/mcuboot/tools/partition_layout.py nrf54l15dk        # visualize
python3 bootloader/mcuboot/tools/partition_layout.py --fix nrf54l15dk # write dtsi
python3 bootloader/mcuboot/tools/partition_layout.py --gen-list       # refresh mcuboot_boards.cmake
```

The board registry (canonical Zephyr board id, vendor, and SPI-NOR erase-size
overrides) lives in `tools/boards.toml`; `--fix` writes to
`dts/<vendor>/<board>.dtsi`. After editing `boards.toml`, re-run
`--gen-list` so `mcuboot_boards.cmake` stays in sync.