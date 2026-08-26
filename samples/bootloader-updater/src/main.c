/*
 * Copyright (c) 2026 Adafruit Industries
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Bootloader updater application.
 *
 * This is an ordinary slot0 application that the bootloader (mcuboot) writes
 * into the primary slot and then boots. It carries a fresh mcuboot bootloader
 * image embedded at build time and, on boot, overwrites the "mcuboot" (boot)
 * partition with it -- i.e. it self-updates the bootloader.
 *
 * Workflow:
 *   1. Build the bootloader:        make build BOARD=<key>
 *   2. Build this updater:         make updater BOARD=<key>
 *   3. Flash build-<key>-updater/zephyr/zephyr.signed.bin to slot0 (UF2 /
 *      serial recovery / debugger). mcuboot boots it.
 *   4. The updater erases the boot partition, writes the embedded mcuboot
 *      image, verifies it, then reboots.
 *
 * Run-once / not wearing flash:
 *   - Before writing, the boot partition is read back and compared to the
 *     embedded image. If they already match the write (and erase) is skipped,
 *     so re-running the updater is harmless.
 *   - After a successful write, if the board has a zephyr,boot-mode retention
 *     area (CONFIG_RETENTION_BOOT_MODE), the updater sets it to "bootloader"
 *     before rebooting. The freshly-written bootloader then enters its
 *     serial/UF2 recovery mode instead of booting the updater again, so the
 *     user can reflash their real application. On boards without that
 *     retention area the call is a no-op and the device reboots normally.
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/logging/log.h>

#if IS_ENABLED(CONFIG_RETENTION_BOOT_MODE)
#include <zephyr/retention/bootmode.h>
#endif

#include <errno.h>
#include <string.h>

LOG_MODULE_REGISTER(bootloader_updater, LOG_LEVEL_INF);

/* The new mcuboot bootloader image, embedded at build time by CMake from the
 * mcuboot.bin produced by `make build`. See CMakeLists.txt.
 */
static const uint8_t mcuboot_image[] = {
#include <mcuboot_image.bin.inc>
};

/* The partition to overwrite: the board's "mcuboot" (boot) partition. */
#define BOOT_NODE      DT_NODELABEL(boot_partition)
#define BOOT_PART_SIZE DT_REG_SIZE(BOOT_NODE)

/* The embedded image must fit in the boot partition. */
BUILD_ASSERT(sizeof(mcuboot_image) <= BOOT_PART_SIZE,
             "embedded mcuboot image is larger than the boot partition");

/* Write chunk size. Must be a multiple of the flash write-block alignment so
 * every flash_area_write() (except the final, padded one) is aligned; checked
 * at runtime.
 */
#define CHUNK 4096

static uint8_t buf[CHUNK];

/* Return true if the boot partition already byte-for-byte matches the embedded
 * image (over the image length), so the write can be skipped. */
static bool boot_partition_matches(void)
{
    const struct flash_area *boot = NULL;
    size_t off = 0;
    int rc;

    rc = flash_area_open(PARTITION_ID(boot_partition), &boot);
    if (rc != 0 || boot == NULL) {
        LOG_WRN("could not open boot partition for compare: %d", rc);
        return false;
    }

    while (off < sizeof(mcuboot_image)) {
        size_t cmp = MIN(CHUNK, sizeof(mcuboot_image) - off);

        rc = flash_area_read(boot, off, buf, cmp);
        if (rc != 0) {
            flash_area_close(boot);
            LOG_WRN("read failed at %zu: %d", off, rc);
            return false;
        }
        if (memcmp(buf, mcuboot_image + off, cmp) != 0) {
            flash_area_close(boot);
            return false;
        }
        off += cmp;
    }

    flash_area_close(boot);
    return true;
}

static int overwrite_bootloader(void)
{
    const struct flash_area *boot = NULL;
    uint32_t align;
    uint8_t erased;
    size_t off;
    int rc;

    rc = flash_area_open(PARTITION_ID(boot_partition), &boot);
    if (rc != 0 || boot == NULL) {
        LOG_ERR("could not open boot partition: %d", rc);
        return -EIO;
    }

    if (!flash_area_device_is_ready(boot)) {
        LOG_ERR("boot partition flash device not ready");
        flash_area_close(boot);
        return -ENODEV;
    }

    align = flash_area_align(boot);
    if (align == 0) {
        align = 1;
    }
    if (align > CHUNK || (CHUNK % align) != 0) {
        LOG_ERR("write alignment %u unsupported (chunk %u)", align, CHUNK);
        flash_area_close(boot);
        return -EINVAL;
    }
    erased = flash_area_erased_val(boot);

    LOG_INF("erasing boot partition (%u bytes)", (unsigned)BOOT_PART_SIZE);
    rc = flash_area_erase(boot, 0, BOOT_PART_SIZE);
    if (rc != 0) {
        LOG_ERR("erase failed: %d", rc);
        flash_area_close(boot);
        return rc;
    }

    LOG_INF("writing %zu bytes", sizeof(mcuboot_image));
    off = 0;
    while (off < sizeof(mcuboot_image)) {
        size_t remain = sizeof(mcuboot_image) - off;
        size_t copy = MIN(CHUNK, remain);
        size_t wr = copy;

        memcpy(buf, mcuboot_image + off, copy);

        /* Pad the final (short) chunk up to the write-block alignment using
         * the erase value so the last flash_area_write() is aligned. The
         * partition beyond the image is already erased to this value, so the
         * padding does not change trailing bytes.
         */
        if (wr % align != 0) {
            size_t pad = align - (wr % align);
            memset(buf + wr, erased, pad);
            wr += pad;
            if (off + wr > BOOT_PART_SIZE) {
                wr = BOOT_PART_SIZE - off;
            }
        }

        rc = flash_area_write(boot, off, buf, wr);
        if (rc != 0) {
            LOG_ERR("write failed at %zu: %d", off, rc);
            flash_area_close(boot);
            return rc;
        }
        off += wr;
    }

    LOG_INF("verifying");
    off = 0;
    while (off < sizeof(mcuboot_image)) {
        size_t cmp = MIN(CHUNK, sizeof(mcuboot_image) - off);

        rc = flash_area_read(boot, off, buf, cmp);
        if (rc != 0) {
            LOG_ERR("verify read failed at %zu: %d", off, rc);
            flash_area_close(boot);
            return rc;
        }
        if (memcmp(buf, mcuboot_image + off, cmp) != 0) {
            LOG_ERR("verify mismatch at offset %zu", off);
            flash_area_close(boot);
            return -EIO;
        }
        off += cmp;
    }

    flash_area_close(boot);
    return 0;
}

int main(void)
{
    int rc;

    LOG_INF("Adaboot bootloader updater");
    LOG_INF("embedded mcuboot image: %zu bytes", sizeof(mcuboot_image));
    LOG_INF("boot partition: %u bytes", (unsigned)BOOT_PART_SIZE);

    if (boot_partition_matches()) {
        LOG_INF("boot partition already matches the embedded image; nothing to do");
    } else {
        rc = overwrite_bootloader();
        if (rc != 0) {
            LOG_ERR("bootloader update FAILED (%d); not rebooting", rc);
            return 0;
        }
        LOG_INF("bootloader updated successfully");
    }

#if IS_ENABLED(CONFIG_RETENTION_BOOT_MODE)
    rc = bootmode_set(BOOT_MODE_TYPE_BOOTLOADER);
    if (rc != 0) {
        LOG_WRN("bootmode_set failed: %d (rebooting normally)", rc);
    } else {
        LOG_INF("requested bootloader recovery on next boot");
    }
#else
    LOG_INF("no boot-mode retention; rebooting (reflash your app to leave the updater)");
#endif

    LOG_INF("rebooting");
    sys_reboot(SYS_REBOOT_COLD);

    return 0;
}