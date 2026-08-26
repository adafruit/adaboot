# SPDX-License-Identifier: Apache-2.0
#
# Bootloader updater application.
#
# This is an ordinary Zephyr application that boots from slot0. It embeds a
# fresh mcuboot.bin (built by `make build`) and, on boot, overwrites the
# board's "mcuboot" (boot) partition with it -- a self-update of the
# bootloader. See src/main.c for the runtime logic and ../README.md for usage.
#
# The mcuboot image to embed is supplied via the MCUBOOT_IMAGE_BIN CMake cache
# variable (an absolute path to a mcuboot.bin). The standalone Makefile's
# `updater` target sets this automatically; for a manual build:
#
#   west build -b <board> samples/bootloader-updater -- \
#       -DEXTRA_ZEPHYR_MODULES=$PWD \
#       -DEXTRA_DTC_OVERLAY_FILE=<path-to-board-layout.dtsi> \
#       -DEXTRA_CONF_FILE=conf/app-mode-<single_app|ram_load>.conf \
#       -DMCUBOOT_IMAGE_BIN=<path-to>/mcuboot.bin

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(bootloader_updater)

target_sources(app PRIVATE src/main.c)

# The mcuboot.bin to embed and write to the boot partition. Required.
set(MCUBOOT_IMAGE_BIN "" CACHE FILEPATH
    "Path to the mcuboot.bin to embed and write to the boot partition")
if(NOT MCUBOOT_IMAGE_BIN)
    message(FATAL_ERROR
        "MCUBOOT_IMAGE_BIN is not set. Point it at a mcuboot.bin, e.g. the one "
        "produced by `make build BOARD=<key>` (build-<key>/mcuboot.bin).")
endif()
if(NOT EXISTS "${MCUBOOT_IMAGE_BIN}")
    message(FATAL_ERROR
        "MCUBOOT_IMAGE_BIN does not exist: '${MCUBOOT_IMAGE_BIN}'. "
        "Build the bootloader first with `make build BOARD=<key>`.")
endif()
get_filename_component(MCUBOOT_IMAGE_BIN_ABS "${MCUBOOT_IMAGE_BIN}" ABSOLUTE)
message(STATUS "Bootloader updater: embedding ${MCUBOOT_IMAGE_BIN_ABS}")

# Embed the binary as a C array initializer (mcuboot_image.bin.inc) consumed by
# `static const uint8_t mcuboot_image[] = { #include <mcuboot_image.bin.inc> };`
# in src/main.c. generate_inc_file_for_target re-runs when the .bin changes.
set(gen_dir ${ZEPHYR_BINARY_DIR}/include/generated)
generate_inc_file_for_target(app ${MCUBOOT_IMAGE_BIN_ABS}
                             ${gen_dir}/mcuboot_image.bin.inc)