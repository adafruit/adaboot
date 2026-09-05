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
# writing any partition code of its own.

include(${CMAKE_CURRENT_LIST_DIR}/mcuboot_boards.cmake)

# By now Zephyr has normalized BOARD to the bare board name (revision and
# qualifiers live in BOARD_REVISION/BOARD_QUALIFIERS); strip anyway so this also
# works if it is included from a context that still has the full board id.
string(REGEX REPLACE "[@/].*" "" adaboot_layout_key "${BOARD}")
set(adaboot_layout "${MCUBOOT_LAYOUT_${adaboot_layout_key}}")

if(adaboot_layout)
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
      set(${image}_DTC_OVERLAY_FILE "${adaboot_layout};${image_dtc_overlay}"
          CACHE INTERNAL "Partition layout prepended to ${image} devicetree overlays" FORCE
      )
      continue()
    endif()

    # Append rather than set: sysbuild may already have queued image defaults.
    set(adaboot_overlays ${${image}_EXTRA_DTC_OVERLAY_FILE})
    if(NOT "${adaboot_layout}" IN_LIST adaboot_overlays)
      list(APPEND adaboot_overlays ${adaboot_layout})
      set(${image}_EXTRA_DTC_OVERLAY_FILE "${adaboot_overlays}"
          CACHE INTERNAL "Partition layout overlay for ${image}" FORCE
      )
    endif()
  endforeach()
endif()
