# Apply this fork's board partition layout to the images of a sysbuild build.
#
# Included from boot/zephyr/sysbuild/CMakeLists.txt, this module's
# `sysbuild-cmake` entry point. Zephyr processes module sysbuild CMake after the
# application and mcuboot images have been added but before their CMake is
# invoked (see zephyr/share/sysbuild/images/CMakeLists.txt and
# cmake/modules/sysbuild_images.cmake), so this is late enough to know which
# images exist and early enough to still contribute overlays to them.
#
# The point: an application that has this module in its west manifest gets the
# fork's memory map on both the bootloader and the application image without
# writing any partition code of its own -- including which partition each
# image links into (dts/app-partition.overlay, dts/boot-partition.overlay).

include(${CMAKE_CURRENT_LIST_DIR}/mcuboot_boards.cmake)

# Pick the layout that matches this Zephyr board id. The fork registers one
# dtsi per dts/<vendor>/<board>.dtsi filename stem, and the dts/ tree may
# carry both a bare-name entry (e.g. rpi_pico, the default for that
# hardware) and one or more qualified entries (e.g. rpi_pico/rp2040/w) for
# variants whose layout differs.
#
# At this point Zephyr has already run board aliases (the cp_board_alias
# macro in board_aliases.cmake), so BOARD is the bare board name
# (rpi_pico) and BOARD_QUALIFIERS holds the qualifier (rp2040/w). The
# alias macro only fires when the user passed a CircuitPython-prefixed id
# (e.g. raspberrypi_rpi_pico_w_zephyr) -- if they passed the canonical
# Zephyr id directly (rpi_pico/rp2040/w) the bare name and qualifier
# come from west's normal board resolution. Either way the full id is
# `${BOARD}/${BOARD_QUALIFIERS}` with / rewritten to _ (and @ rewritten
# to _, so Zephyr revisions like `mimxrt1170_evk@A` produce a valid CMake
# cache variable name -- `${VAR}` does not handle `@`). Prefer the
# variant entry when one is registered; otherwise fall back to the bare
# name. A bare-name entry can also be missing (every dtsi lives under a
# variant key), so the bare-name fallback is the optional one.
set(adaboot_full_key "${BOARD}")
if(DEFINED BOARD_QUALIFIERS AND NOT "${BOARD_QUALIFIERS}" STREQUAL "")
  set(adaboot_full_key "${BOARD}/${BOARD_QUALIFIERS}")
endif()
string(REGEX REPLACE "/" "_" adaboot_full_key "${adaboot_full_key}")
string(REPLACE "@" "_" adaboot_full_key "${adaboot_full_key}")
string(REGEX REPLACE "[@/].*" "" adaboot_layout_key "${BOARD}")
set(adaboot_layout "${MCUBOOT_LAYOUT_${adaboot_full_key}}")
if(NOT adaboot_layout)
  set(adaboot_layout "${MCUBOOT_LAYOUT_${adaboot_layout_key}}")
endif()
# Keep adaboot_layout_key as the bare name even when a variant alias matched:
# MCUBOOT_BOARDS (checked below) is keyed by bare name, so comparing the full
# key against it would miss every board built with qualifiers.

# Which partition an image links into is per-image, not per-board: the
# application image links into the primary slot the bootloader boots, the
# bootloader image into its own boot partition. Both selections live in one
# overlay each (they name the shared role labels, so the same file fits every
# board), applied alongside the layout below.
set(adaboot_app_overlay ${CMAKE_CURRENT_LIST_DIR}/app-partition.overlay)
set(adaboot_boot_overlay ${CMAKE_CURRENT_LIST_DIR}/boot-partition.overlay)
# Only boards that boot via mcuboot have a slot0 to link into; standalone
# boards keep whatever code partition their board dts already selects.
set(adaboot_app_layout_overlays "")
if(adaboot_layout_key IN_LIST MCUBOOT_BOARDS)
  set(adaboot_app_layout_overlays ${adaboot_app_overlay})
endif()

if(adaboot_layout)
  # Optional bootloader-only board content (dts/<vendor>/<board>-boot.dtsi),
  # applied to the mcuboot image only. This is devicetree content the bootloader
  # needs but the application does not, such as the CDC ACM node that serial
  # recovery uses.
  string(REGEX REPLACE "\\.dtsi$" "-boot.dtsi" adaboot_boot_only_overlay "${adaboot_layout}")
  if(NOT EXISTS "${adaboot_boot_only_overlay}")
    set(adaboot_boot_only_overlay "")
  endif()

  set(adaboot_layout_images ${DEFAULT_IMAGE})
  if(TARGET mcuboot)
    # Same file for the bootloader, so the two images cannot disagree about
    # boot_partition/slot0_partition.
    list(APPEND adaboot_layout_images mcuboot)
  endif()

  foreach(image ${adaboot_layout_images})
    # Prefer prepending the layout to an image's own DTC_OVERLAY_FILE: the
    # layout then comes first, so labels it defines (e.g. zephyr_udc0 on
    # boards whose SoC DTS leaves the controller unlabeled) are visible to the
    # application's overlays, which reference them (CircuitPython's app.overlay
    # attaches its CDC ACM endpoints to &zephyr_udc0).
    # Only do this when the application set DTC_OVERLAY_FILE itself: setting
    # it would otherwise suppress Zephyr's automatic <app>/app.overlay lookup
    # for applications that rely on it. For those (and for images like mcuboot
    # whose app.overlay is auto-detected), fall back to appending the layout as
    # an EXTRA overlay, which is applied after the image's own overlays.
    get_property(image_dtc_overlay CACHE ${image}_DTC_OVERLAY_FILE PROPERTY VALUE)
    if(image STREQUAL DEFAULT_IMAGE AND image_dtc_overlay)
      set(adaboot_app_overlays_list ${adaboot_app_layout_overlays} ${adaboot_layout})
      set(${image}_DTC_OVERLAY_FILE "${adaboot_app_overlays_list};${image_dtc_overlay}"
          CACHE INTERNAL "Partition layout prepended to ${image} devicetree overlays" FORCE
      )
      continue()
    endif()

    # Append rather than set: sysbuild may already have queued image defaults.
    set(adaboot_overlays ${${image}_EXTRA_DTC_OVERLAY_FILE})
    set(adaboot_overlays_changed FALSE)
    if(NOT "${adaboot_layout}" IN_LIST adaboot_overlays)
      # The layout comes first; the image's own code-partition selection
      # (application slot or boot partition) is appended after it, so its
      # /chosen/zephyr,code-partition assignment wins over both the layout
      # and anything earlier in the list (later entries in
      # EXTRA_DTC_OVERLAY_FILE take precedence, and EXTRA takes precedence
      # over the image's own auto-detected app.overlay).
      list(APPEND adaboot_overlays ${adaboot_layout})
      if(image STREQUAL "mcuboot")
        list(APPEND adaboot_overlays ${adaboot_boot_only_overlay} ${adaboot_boot_overlay})
      elseif(image STREQUAL DEFAULT_IMAGE)
        list(APPEND adaboot_overlays ${adaboot_app_layout_overlays})
      endif()
      set(adaboot_overlays_changed TRUE)
    endif()

    if(adaboot_overlays_changed)
      set(${image}_EXTRA_DTC_OVERLAY_FILE "${adaboot_overlays}"
          CACHE INTERNAL "Partition layout overlay for ${image}" FORCE
      )
    endif()
  endforeach()
endif()
