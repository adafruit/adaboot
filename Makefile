# Standalone build of the Adaboot (MCUboot fork) bootloader.
#
# This mirrors how CircuitPython's zephyr-cp port builds against Zephyr: a west
# workspace whose manifest imports the Adafruit fork of Zephyr. The workspace
# lives in this repo root (``.west/``), Zephyr and its HALs are fetched under
# ``deps/``, and this repository is registered as the Zephyr "mcuboot" module
# directly (the live working tree) so ``make build`` compiles the tree you are
# editing -- no separate mcuboot checkout.
#
# The board registry is tools/boards.toml. tools/partition_layout.py --gen-list
# turns it into the generated, committed artifacts the build consumes:
#   dts/<vendor>/<board>.dtsi      the partition layout (the source of truth)
#   dts/boards.mk                 this Makefile's per-board lookup (board id,
#                                 overlay path, RAM-load flag)
#   dts/Kconfig.sysbuild           sysbuild bootloader policy
# The upgrade mode (single_app vs swap-using-offset) is NOT looked up here: it
# follows the layout through Kconfig (slot1_partition present -> swap), see
# boot/zephyr/Kconfig BOOT_IMAGE_UPGRADE_MODE. RAM-load is the one mode not
# derivable from the layout; it is flagged in dts/boards.mk and applied via a
# conf fragment.
#
# Quick start:
#   make workspace          # one-time: west init + west update (Adafruit Zephyr + HALs)
#   make list               # boards this fork can build
#   make build BOARD=nrf54l15dk
#   make all                # build every board

WEST ?= west
ADABOOT_DIR := $(CURDIR)
ZEPHYR_CONFIG := $(ADABOOT_DIR)/zephyr-config
WORKSPACE_MANIFEST := $(ADABOOT_DIR)/zephyr-config/west.yml
WORKSPACE_MANIFEST_IN := $(ADABOOT_DIR)/zephyr-config/west.yml.in
CONF_DIR := $(ADABOOT_DIR)/conf

# Known-good Adafruit Zephyr revision (matches CircuitPython's zephyr-cp port).
ZEPHYR_REV ?= 62e7a3764b652fff733cee43f23f82217403a51d

# Generated, committed Make fragment: maps each partition key to its canonical
# Zephyr board id, layout overlay and RAM-load flag. Regenerate with
# `python3 tools/partition_layout.py --gen-list` after editing boards.toml.
-include $(ADABOOT_DIR)/dts/boards.mk

# Boards to build: every board whose partition layout this fork owns and that
# boots via mcuboot (the MCUBOOT_BOARDS list from dts/boards.mk).
BOARDS ?= $(MCUBOOT_BOARDS)

# Per-board build directory.
BUILD ?= build-$(BOARD)
# Per-board build directory for the bootloader-updater application.
UPDATER ?= $(BUILD)-updater

# Boards whose bootloader is UF2-capable: those whose conf/<key>.conf enables
# CONFIG_MCUBOOT_UF2=y (sourced from tools/uf2_updater.py). Only these have a
# USB mass-storage drive to drag a .uf2 onto, so only their updaters get a UF2.
UF2_BOARDS ?= $(shell python3 $(ADABOOT_DIR)/tools/uf2_updater.py list)

# Resolve a board's build parameters from dts/boards.mk. The upgrade mode is not
# here (Kconfig derives single_app vs swap from the slot1_partition DT nodelabel).
ifdef BOARD
WEST_BOARD := $($(BOARD)_BOARD)
OVERLAY    := $(ADABOOT_DIR)/$($(BOARD)_DTSI)
RAM_LOAD   := $($(BOARD)_RAM_LOAD)
# Optional board-specific conf fragment (conf/<key>.conf), empty if absent.
BOARD_CONF := $(wildcard $(CONF_DIR)/$(BOARD).conf)
endif

# Bootloader EXTRA_CONF_FILE:
#   conf/adaboot.conf            universal Adaboot defaults (signature-none, SPI_NOR)
#   conf/mode-ram_load.conf      RAM-load override (RAM-load boards only)
#   conf/<key>.conf              board-specific opt-ins (UF2, serial, retention)
# Zephyr treats EXTRA_CONF_FILE as a CMake list (semicolon-separated).
BOOT_CONF_FILE := $(CONF_DIR)/adaboot.conf
ifeq ($(RAM_LOAD),y)
BOOT_CONF_FILE := $(BOOT_CONF_FILE);$(CONF_DIR)/mode-ram_load.conf
endif
ifneq ($(strip $(BOARD_CONF)),)
BOOT_CONF_FILE := $(BOOT_CONF_FILE);$(BOARD_CONF)
endif

# Updater EXTRA_CONF_FILE: only the RAM-load override (the updater's single-app
# mode and SPI_NOR are in its own prj.conf). The board-specific conf
# (conf/<key>.conf) is NOT applied here -- it carries bootloader-only symbols
# (BOOT_SERIAL_*, MCUBOOT_UF2_*, retention) that are undefined in the updater
# app and would abort the Kconfig merge; the updater gets what it needs
# (flash_map, retention for the recovery-mode request) from its own prj.conf.
UPDATER_CONF_FILE :=
ifeq ($(RAM_LOAD),y)
UPDATER_CONF_FILE := $(CONF_DIR)/app-mode-ram_load.conf
endif
UPDATER_CONF_FLAG := $(if $(strip $(UPDATER_CONF_FILE)),-DEXTRA_CONF_FILE="$(UPDATER_CONF_FILE)")

.PHONY: help list show workspace update build updater uf2 all-uf2 all menuconfig flash \
        clean clean-all clean-workspace

help:
	@echo "Adaboot standalone build (MCUboot fork, using the Adafruit Zephyr fork)."
	@echo
	@echo "First-time setup:"
	@echo "  make workspace        west init + west update (clones Adafruit Zephyr + HALs under deps/)"
	@echo
	@echo "Build:"
	@echo "  make list             list boards this fork can build"
	@echo "  make build BOARD=<key>   build the bootloader for one board"
	@echo "  make updater BOARD=<key>  build the bootloader + a slot0 updater that rewrites it"
	@echo "                           (and, on swap-capable boards, a slot1 test-upgrade variant)"
	@echo "  make uf2 BOARD=<key>     build the updater and emit a .uf2 (UF2-capable boards only)"
	@echo "  make all              build every board (override: make all BOARDS='a b')"
	@echo "  make all-uf2          build a .uf2 updater for every UF2-capable board"
	@echo "  make menuconfig BOARD=<key>"
	@echo "  make flash BOARD=<key>"
	@echo
	@echo "Cleanup:"
	@echo "  make clean BOARD=<key>   remove one board build dir"
	@echo "  make clean-all            remove every build-* dir"
	@echo "  make clean-workspace       remove the west workspace (deps/ + .west)"
	@echo
	@echo "Adafruit Zephyr rev: $(ZEPHYR_REV)"
	@echo "Boards ($(words $(BOARDS))): $(BOARDS)"
	@echo "UF2-capable ($(words $(UF2_BOARDS))): $(UF2_BOARDS)"

list:
	@echo "$(BOARDS)" | tr ' ' '\n' | column

# Debug: show the resolved parameters for BOARD.
show:
	@echo "BOARD=$(BOARD)"
	@echo "WEST_BOARD=$(WEST_BOARD)"
	@echo "RAM_LOAD=$(RAM_LOAD)"
	@echo "OVERLAY=$(OVERLAY)"
	@echo "BOARD_CONF=$(BOARD_CONF)"
	@echo "BOOT_CONF_FILE=$(BOOT_CONF_FILE)"
	@echo "UPDATER_CONF_FLAG=$(UPDATER_CONF_FLAG)"
	@echo "BUILD=$(BUILD)"
	@echo "UPDATER=$(UPDATER)"

# Generate the workspace manifest from the template (pins the Zephyr revision).
$(WORKSPACE_MANIFEST): $(WORKSPACE_MANIFEST_IN)
	@sed -e 's#@ZEPHYR_REV@#$(ZEPHYR_REV)#g' < $< > $@
	@echo "Generated $(WORKSPACE_MANIFEST) (Zephyr rev $(ZEPHYR_REV))"

# One-time: create the west workspace and fetch Adafruit Zephyr + HALs.
workspace: $(WORKSPACE_MANIFEST)
	@if [ ! -d "$(ADABOOT_DIR)/.west" ]; then \
	  $(WEST) init -l $(ZEPHYR_CONFIG); \
	else \
	  echo ".west already exists; run 'make update' to refresh or 'make clean-workspace' to start over."; \
	fi
	$(WEST) update

# Refresh fetched projects (e.g. after bumping ZEPHYR_REV).
update: $(WORKSPACE_MANIFEST)
	$(WEST) update

# Build the bootloader for one board.
#
# The board's partition layout (dts/<vendor>/<board>.dtsi) is applied via
# EXTRA_DTC_OVERLAY_FILE (after boot/zephyr's app.overlay, which sets the
# bootloader code partition). Adaboot defaults (signature-none, SPI_NOR) come
# from conf/adaboot.conf; RAM-load and board-specific opt-ins layer on top.
# The upgrade mode is chosen by Kconfig from the layout (slot1 -> swap), not by
# a conf fragment. This repo is the Zephyr module (live tree) via
# EXTRA_ZEPHYR_MODULES, so the sources you are editing are the ones that get
# compiled.
build:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'. Run 'make workspace' first."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'. If dts/boards.mk is missing, run 'python3 tools/partition_layout.py --gen-list'."; false; fi
	@echo "==> Building $(BOARD) (Zephyr board $(WEST_BOARD))"
	$(WEST) build -b $(WEST_BOARD) -d $(BUILD) $(ADABOOT_DIR)/boot/zephyr -- \
	  -DEXTRA_ZEPHYR_MODULES=$(ADABOOT_DIR) \
	  -DEXTRA_DTC_OVERLAY_FILE=$(OVERLAY) \
	  -DEXTRA_CONF_FILE="$(BOOT_CONF_FILE)"
	@cp $(BUILD)/zephyr/zephyr.bin $(BUILD)/mcuboot.bin
	@-cp $(BUILD)/zephyr/zephyr.hex $(BUILD)/mcuboot.hex 2>/dev/null || true
	@echo "==> $(BUILD)/mcuboot.bin  (elf/hex in $(BUILD)/zephyr/)"

# Build the bootloader-updater application for one board.
#
# Builds the bootloader first (target `build`), then builds
# samples/bootloader-updater -- an ordinary slot0 application that embeds that
# mcuboot.bin as a payload. When the bootloader writes the updater into slot0
# and boots it, the updater overwrites the boot ("mcuboot") partition with the
# embedded image, i.e. it self-updates the bootloader.
#
# The updater is signed single-app/overwrite-style (no swap trailer) in its
# prj.conf so it flashes straight to slot0 (serial / UF2 / debugger):
#   $(UPDATER)/zephyr/zephyr.signed.bin
# On swap-capable boards (slot1_partition present) a second variant is signed
# as a *test* upgrade (imgtool --pad, no --confirm) so it can be uploaded to the
# secondary slot instead -- mcuboot swaps it in, the updater runs, then reverts:
#   $(UPDATER)/zephyr/zephyr.slot1.signed.bin
updater:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'. Run 'make workspace' first."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'."; false; fi
	@echo "==> Building bootloader payload for $(BOARD)"
	@$(MAKE) --no-print-directory build BOARD=$(BOARD)
	@echo "==> Building updater for $(BOARD) (Zephyr board $(WEST_BOARD))"
	$(WEST) build -b $(WEST_BOARD) -d $(UPDATER) $(ADABOOT_DIR)/samples/bootloader-updater -- \
	  -DEXTRA_ZEPHYR_MODULES=$(ADABOOT_DIR) \
	  -DEXTRA_DTC_OVERLAY_FILE=$(OVERLAY) \
	  $(UPDATER_CONF_FLAG) \
	  -DMCUBOOT_IMAGE_BIN=$(ADABOOT_DIR)/$(BUILD)/mcuboot.bin
	@echo "==> $(UPDATER)/zephyr/zephyr.signed.bin  (slot0: flash directly to self-update)"
	@python3 $(ADABOOT_DIR)/tools/updater_sign.py slot1 $(UPDATER)

# Build the updater and convert it to a UF2 file, for one UF2-capable board.
#
# Runs `updater` first (so the bootloader + updater builds exist), then turns
# the updater's signed image into a .uf2 you can drag onto the bootloader's
# UF2 mass-storage drive. The UF2 base address is slot0's flash offset (fa_off)
# -- the address the UF2 bootloader writes incoming blocks at -- and the
# family ID is the bootloader's CONFIG_MCUBOOT_UF2_FAMILY_ID; both are read
# from the freshly built trees by tools/uf2_updater.py (base from the updater
# EDT pickle, family from the bootloader .config).
#
# The base/family are computed inside the recipe (shell `$$()`, not Make's
# `$(shell)`) so they read the build dirs *after* `updater` has populated them.
# Conversion uses the vendored upstream uf2conv.py (tools/uf2conv.py, from
# microsoft/uf2); `-c` makes it write a file instead of flashing a drive.
#
# Output:
#   $(UPDATER)/mcuboot-updater.uf2   (drag this onto the UF2 drive)
uf2:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'. Run 'make workspace' first."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'."; false; fi
	@$(MAKE) --no-print-directory updater BOARD=$(BOARD)
	@base=$$(python3 $(ADABOOT_DIR)/tools/uf2_updater.py base $(UPDATER)); \
	 family=$$(python3 $(ADABOOT_DIR)/tools/uf2_updater.py family $(BUILD)); \
	 echo "==> Converting $(BOARD) updater to UF2 (base $$base, family $$family)"; \
	 python3 $(ADABOOT_DIR)/tools/uf2conv.py -c -b $$base -f $$family \
	   -o $(UPDATER)/zephyr.signed.uf2 \
	   $(UPDATER)/zephyr/zephyr.signed.bin; \
	 cp $(UPDATER)/zephyr.signed.uf2 $(UPDATER)/mcuboot-updater.uf2; \
	 echo "==> $(UPDATER)/mcuboot-updater.uf2  (drag onto the UF2 drive to self-update the bootloader)"

menuconfig:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'.."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'."; false; fi
	$(WEST) build -b $(WEST_BOARD) -d $(BUILD) $(ADABOOT_DIR)/boot/zephyr --target menuconfig -- \
	  -DEXTRA_ZEPHYR_MODULES=$(ADABOOT_DIR) \
	  -DEXTRA_DTC_OVERLAY_FILE=$(OVERLAY) \
	  -DEXTRA_CONF_FILE="$(BOOT_CONF_FILE)"

flash:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'."; false; fi
	$(WEST) flash -d $(BUILD)

clean:
	@rm -rf $(BUILD) $(UPDATER)

clean-all:
	@rm -rf $(wildcard build-*)

# Remove the generated manifest and the fetched workspace (deps/ + .west).
# The committed template (zephyr-config/west.yml.in) and this repo are untouched.
clean-workspace:
	@rm -rf $(ADABOOT_DIR)/.west $(ADABOOT_DIR)/deps $(WORKSPACE_MANIFEST)

# Build every board. Continues on error so one failing board does not stop the
# rest. To stop on first failure, use: make all STOP_ON_ERROR=1
all:
	@set -e; \
	failed=""; \
	for b in $(BOARDS); do \
	  echo "==> $$b"; \
	  if $(MAKE) build BOARD=$$b; then :; else \
	    if [ -n "$(STOP_ON_ERROR)" ]; then echo "!! build failed for $$b" >&2; exit 1; fi; \
	    echo "!! build failed for $$b (continuing)" >&2; failed="$$failed $$b"; \
	  fi; \
	done; \
	if [ -n "$$failed" ]; then echo "Failed boards:$$failed" >&2; exit 1; fi

# Build a .uf2 updater for every UF2-capable board (conf/<key>.conf enables
# CONFIG_MCUBOOT_UF2=y). Continues on error so one failing board does not stop
# the rest. To stop on first failure, use: make all-uf2 STOP_ON_ERROR=1.
# Override the set with: make all-uf2 UF2_BOARDS='nrf54lm20dk'
all-uf2:
	@set -e; \
	failed=""; \
	for b in $(UF2_BOARDS); do \
	  echo "==> $$b"; \
	  if $(MAKE) uf2 BOARD=$$b; then :; else \
	    if [ -n "$(STOP_ON_ERROR)" ]; then echo "!! uf2 failed for $$b" >&2; exit 1; fi; \
	    echo "!! uf2 failed for $$b (continuing)" >&2; failed="$$failed $$b"; \
	  fi; \
	done; \
	if [ -n "$$failed" ]; then echo "Failed UF2 boards:$$failed" >&2; exit 1; fi