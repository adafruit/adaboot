# Standalone build of the Adaboot (MCUboot fork) bootloader.
#
# This mirrors how CircuitPython's zephyr-cp port builds against Zephyr: a west
# workspace whose manifest imports the Adafruit fork of Zephyr. The workspace
# lives in this repo root (``.west/``), Zephyr and its HALs are fetched under
# ``deps/``, and this repository is registered as the Zephyr "mcuboot" module
# directly (the live working tree) so ``make build`` compiles the tree you are
# editing -- no separate mcuboot checkout.
#
# Quick start:
#   make workspace          # one-time: west init + west update (Adafruit Zephyr + HALs)
#   make list               # boards this fork can build
#   make build BOARD=nrf54l15dk
#   make all                # build every board

WEST ?= west
ADABOOT_DIR := $(CURDIR)
ZEPHYR_CONFIG := $(ADABOOT_DIR)/zephyr-config
WORKSPACE_MANIFEST := $(ZEPHYR_CONFIG)/west.yml
WORKSPACE_MANIFEST_IN := $(ZEPHYR_CONFIG)/west.yml.in
CONF_DIR := $(ADABOOT_DIR)/conf

# Known-good Adafruit Zephyr revision (matches CircuitPython's zephyr-cp port).
ZEPHYR_REV ?= 62e7a3764b652fff733cee43f23f82217403a51d

# Boards to build: every board whose partition layout this fork owns and that
# boots via mcuboot (sourced from tools/boards.toml, the same list
# tools/partition_layout.py --gen-list turns into dts/mcuboot_boards.cmake).
BOARDS ?= $(shell python3 $(ADABOOT_DIR)/tools/standalone_build.py list)

# Per-board build directory.
BUILD ?= build-$(BOARD)

# When BOARD is given on the command line, resolve its Zephyr board id, layout
# overlay and mode once at parse time. $(shell ...) collapses newlines to spaces,
# so each field is fetched on its own (see tools/standalone_build.py get).
ifdef BOARD
WEST_BOARD := $(shell python3 $(ADABOOT_DIR)/tools/standalone_build.py get $(BOARD) west_board)
OVERLAY    := $(shell python3 $(ADABOOT_DIR)/tools/standalone_build.py get $(BOARD) overlay)
MODE       := $(shell python3 $(ADABOOT_DIR)/tools/standalone_build.py get $(BOARD) mode)
endif

.PHONY: help list show workspace update build all menuconfig flash \
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
	@echo "  make all              build every board (override: make all BOARDS='a b')"
	@echo "  make menuconfig BOARD=<key>"
	@echo "  make flash BOARD=<key>"
	@echo
	@echo "Cleanup:"
	@echo "  make clean BOARD=<key>   remove one board build dir"
	@echo "  make clean-all            remove every build-* dir"
	@echo "  make clean-workspace       remove the west workspace (deps/ + .west)"
	@echo
	@echo "Adafruit Zephyr rev: $(ZEPHYR_REV)"
	@echo "Boards ($(words $(BOARDS)): $(BOARDS)"

list:
	@python3 $(ADABOOT_DIR)/tools/standalone_build.py list | column

# Debug: show the resolved parameters for BOARD.
show:
	@echo "BOARD=$(BOARD)"
	@echo "WEST_BOARD=$(WEST_BOARD)"
	@echo "MODE=$(MODE)"
	@echo "OVERLAY=$(OVERLAY)"
	@echo "BUILD=$(BUILD)"

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
# bootloader code partition). Mode + signature defaults come from a conf
# fragment. This repo is the Zephyr module (live tree) via EXTRA_ZEPHYR_MODULES,
# so the sources you are editing are the ones that get compiled.
build:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'. Run 'make workspace' first."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'."; false; fi
	@echo "==> Building $(BOARD) (Zephyr board $(WEST_BOARD), mode $(MODE))"
	$(WEST) build -b $(WEST_BOARD) -d $(BUILD) $(ADABOOT_DIR)/boot/zephyr -- \
	  -DEXTRA_ZEPHYR_MODULES=$(ADABOOT_DIR) \
	  -DEXTRA_DTC_OVERLAY_FILE=$(OVERLAY) \
	  -DEXTRA_CONF_FILE=$(CONF_DIR)/mode-$(MODE).conf
	@cp $(BUILD)/zephyr/zephyr.bin $(BUILD)/mcuboot.bin
	@-cp $(BUILD)/zephyr/zephyr.hex $(BUILD)/mcuboot.hex 2>/dev/null || true
	@echo "==> $(BUILD)/mcuboot.bin  (elf/hex in $(BUILD)/zephyr/)"

menuconfig:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'."; false; fi
	@if [ -z "$(WEST_BOARD)" ]; then echo "Unknown board '$(BOARD)'; see 'make list'."; false; fi
	$(WEST) build -b $(WEST_BOARD) -d $(BUILD) $(ADABOOT_DIR)/boot/zephyr --target menuconfig -- \
	  -DEXTRA_ZEPHYR_MODULES=$(ADABOOT_DIR) \
	  -DEXTRA_DTC_OVERLAY_FILE=$(OVERLAY) \
	  -DEXTRA_CONF_FILE=$(CONF_DIR)/mode-$(MODE).conf

flash:
	@if [ -z "$(BOARD)" ]; then echo "Set BOARD=<key>; see 'make list'."; false; fi
	$(WEST) flash -d $(BUILD)

clean:
	@rm -rf $(BUILD)

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