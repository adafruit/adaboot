# Copyright (c) 2020-2025 Nordic Semiconductor ASA
#
# SPDX-License-Identifier: Apache-2.0

# Adaboot update-mode request helpers: let any application built against this
# module ask the installed bootloader for its update mode, whether that is
# this fork's MCUboot bootloader or the stock Adafruit nRF52 one (see
# ../../zephyr/include/adaboot/update_mode.h for the per-bootloader
# mechanism). Compiled unconditionally so callers can keep using the helpers
# on boards with neither backend, where they are no-ops.
zephyr_include_directories(../../zephyr/include)
zephyr_library_named(adaboot_update_mode)
zephyr_library_sources(../../zephyr/update_mode.c)

if(CONFIG_MCUBOOT_BOOTUTIL_LIB)
  zephyr_interface_library_named(MCUBOOT_BOOTUTIL)

  target_include_directories(MCUBOOT_BOOTUTIL INTERFACE
    ../
    ../include
    ../../zephyr/include
  )

  zephyr_library_named(mcuboot_util)
  zephyr_library_sources(../src/bootutil_public.c)

  # Sensitivity to the TEST_BOOT_IMAGE_ACCESS_HOOKS define is implemented for
  # allowing the test-build with the hooks feature enabled.
  if(TEST_BOOT_IMAGE_ACCESS_HOOKS)
    zephyr_library_sources(
      ${APPLICATION_SOURCE_DIR}/hooks_sample.c
    )
  endif()

  zephyr_library_link_libraries(MCUBOOT_BOOTUTIL)
  target_link_libraries(MCUBOOT_BOOTUTIL INTERFACE zephyr_interface)

  if(CONFIG_BOOT_USE_TINYCRYPT)
    target_include_directories(MCUBOOT_BOOTUTIL INTERFACE
      ../../../ext/tinycrypt/lib/include
    )
  endif()

  if(CONFIG_BOOT_USE_PSA_CRYPTO)
    target_include_directories(MCUBOOT_BOOTUTIL INTERFACE
       ${ZEPHYR_MBEDTLS_MODULE_DIR}/include
    )
  endif()

  if(CONFIG_BOOT_USE_MBEDTLS OR CONFIG_BOOT_PSA_CRYPTO_BACKEND_MBEDTLS)
    zephyr_link_libraries_ifdef(CONFIG_MBEDTLS_BUILTIN mbedTLS)
  endif()
endif()
