/*
 * Copyright (c) 2012-2014 Wind River Systems, Inc.
 * Copyright (c) 2020 Arm Limited
 * Copyright (c) 2021-2023 Nordic Semiconductor ASA
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include <assert.h>
#include <zephyr/kernel.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/sys/__assert.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/drivers/timer/system_timer.h>
#include <zephyr/usb/usb_device.h>
#include <soc.h>
#include <zephyr/linker/linker-defs.h>

#include "target.h"
#include "bootutil/bootutil_log.h"

BOOT_LOG_MODULE_DECLARE(mcuboot);

#if defined(CONFIG_BOOT_SERIAL_PIN_RESET) || defined(CONFIG_BOOT_FIRMWARE_LOADER_PIN_RESET)
#include <zephyr/drivers/hwinfo.h>
#endif

#if defined(CONFIG_BOOT_SERIAL_BOOT_MODE) || defined(CONFIG_BOOT_FIRMWARE_LOADER_BOOT_MODE) || \
    defined(CONFIG_BOOT_SERIAL_DOUBLE_TAP) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_BOOT_MODE) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
#include <zephyr/retention/bootmode.h>
#endif

/* Validate serial recovery configuration */
#ifdef CONFIG_MCUBOOT_SERIAL
#if !defined(CONFIG_BOOT_SERIAL_ENTRANCE_GPIO) && \
    !defined(CONFIG_BOOT_SERIAL_WAIT_FOR_DFU) && \
    !defined(CONFIG_BOOT_SERIAL_BOOT_MODE) && \
    !defined(CONFIG_BOOT_SERIAL_NO_APPLICATION) && \
    !defined(CONFIG_BOOT_SERIAL_PIN_RESET) && \
    !defined(CONFIG_BOOT_SERIAL_DOUBLE_TAP)
#error "Serial recovery selected without an entrance mode set"
#endif
#endif

/* Validate firmware loader configuration */
#ifdef CONFIG_BOOT_FIRMWARE_LOADER
#if !defined(CONFIG_BOOT_FIRMWARE_LOADER_ENTRANCE_GPIO) && \
    !defined(CONFIG_BOOT_FIRMWARE_LOADER_BOOT_MODE) && \
    !defined(CONFIG_BOOT_FIRMWARE_LOADER_NO_APPLICATION) && \
    !defined(CONFIG_BOOT_FIRMWARE_LOADER_PIN_RESET)
#error "Firmware loader selected without an entrance mode set"
#endif
#endif

#ifdef CONFIG_MCUBOOT_INDICATION_LED

/*
 * The led0 devicetree alias is optional. If present, we'll use it
 * to turn on the LED whenever the button is pressed.
 */
#if DT_NODE_EXISTS(DT_ALIAS(mcuboot_led0))
#define LED0_NODE DT_ALIAS(mcuboot_led0)
#endif

#if DT_NODE_HAS_STATUS(LED0_NODE, okay) && DT_NODE_HAS_PROP(LED0_NODE, gpios)
static const struct gpio_dt_spec led0 = GPIO_DT_SPEC_GET(LED0_NODE, gpios);
#else
/* A build error here means your board isn't set up to drive an LED. */
#error "Unsupported board: led0 devicetree alias is not defined"
#endif

/*
 * Blinking LED indicator, matching the behaviour of
 * Adafruit_nRF52_Bootloader and tinyuf2: the LED fades in a triangle
 * pattern (breathing) whose tempo is selected by the cycle length --
 * slow while idle, fast while waiting for firmware, very fast (reads
 * as a plain blink) while flash is being written. Adafruit drives a
 * hardware PWM with the duty cycle capped at 0x4f/0xff (~31%); we
 * approximate it with a software PWM clocked by a 1 ms k_timer.
 */
#define LED_FADE_STEPS  16
/* Peak brightness: ~31% duty cycle, like Adafruit's 0x4f/0xff cap. */
#define LED_FADE_PEAK   5

static struct k_timer led_blink_timer;
static volatile uint32_t led_blink_ticks;
/* 0 = solid (io_led_set() owns the pin), otherwise fade cycle in ms. */
static volatile uint32_t led_blink_cycle_ms;

static void led_blink_timer_fn(struct k_timer *timer)
{
    ARG_UNUSED(timer);

    uint32_t cycle_ms = led_blink_cycle_ms;
    if (cycle_ms == 0) {
        return;
    }

    uint32_t tick = ++led_blink_ticks;

    /* Triangle fade over the cycle, like Adafruit's led_tick(). */
    uint32_t half = cycle_ms / 2;
    uint32_t phase = tick % cycle_ms;
    if (phase > half) {
        phase = cycle_ms - phase;
    }
    uint32_t level = LED_FADE_PEAK * phase / half;

    /* Software PWM: on for `level` of every LED_FADE_STEPS 1 ms slots. */
    gpio_pin_set_dt(&led0, (int)((tick % LED_FADE_STEPS) < level));
}

void io_led_blink(uint32_t cycle_ms)
{
    if (cycle_ms == 0) {
        return;
    }

    /* Keep the pattern phase when re-requesting the same tempo. */
    if (led_blink_cycle_ms != cycle_ms) {
        led_blink_ticks = 0;
        led_blink_cycle_ms = cycle_ms;
    }
    k_timer_start(&led_blink_timer, K_NO_WAIT, K_MSEC(1));
}

void io_led_init(void)
{
    if (!device_is_ready(led0.port)) {
        BOOT_LOG_ERR("Didn't find LED device referred by the LED0_NODE\n");
        return;
    }

    gpio_pin_configure_dt(&led0, GPIO_OUTPUT);
    gpio_pin_set_dt(&led0, 0);

    k_timer_init(&led_blink_timer, led_blink_timer_fn, NULL);
}

void io_led_set(int value)
{
    led_blink_cycle_ms = 0;
    k_timer_stop(&led_blink_timer);
    gpio_pin_set_dt(&led0, value);
}
#endif /* CONFIG_MCUBOOT_INDICATION_LED */

#if defined(CONFIG_BOOT_SERIAL_ENTRANCE_GPIO) || defined(CONFIG_BOOT_USB_DFU_GPIO) || \
    defined(CONFIG_BOOT_FIRMWARE_LOADER_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)

#if defined(CONFIG_MCUBOOT_SERIAL)
#define BUTTON_0_DETECT_DELAY CONFIG_BOOT_SERIAL_DETECT_DELAY
#elif defined(CONFIG_BOOT_FIRMWARE_LOADER)
#define BUTTON_0_DETECT_DELAY CONFIG_BOOT_FIRMWARE_LOADER_DETECT_DELAY
#elif defined(CONFIG_MCUBOOT_UF2_ENTRANCE_GPIO)
#define BUTTON_0_DETECT_DELAY 0
#elif defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
#define BUTTON_0_DETECT_DELAY 0
#else
#define BUTTON_0_DETECT_DELAY CONFIG_BOOT_USB_DFU_DETECT_DELAY
#endif

#define BUTTON_0_NODE DT_ALIAS(mcuboot_button0)

#if DT_NODE_EXISTS(BUTTON_0_NODE) && DT_NODE_HAS_PROP(BUTTON_0_NODE, gpios)
static const struct gpio_dt_spec button0 = GPIO_DT_SPEC_GET(BUTTON_0_NODE, gpios);
#else
#error "Serial recovery/USB DFU button must be declared in device tree as 'mcuboot_button0'"
#endif

bool io_detect_pin(void)
{
    int rc;
    int pin_active;

    BOOT_LOG_DBG("io_detect_pin: checking button0");

    if (!device_is_ready(button0.port)) {
        BOOT_LOG_DBG("io_detect_pin: GPIO device is not ready.");
        return false;
    }

    rc = gpio_pin_configure_dt(&button0, GPIO_INPUT);
    if (rc != 0) {
        BOOT_LOG_DBG("io_detect_pin: Failed to init boot detect pin, rc=%d", rc);
        return false;
    }

    rc = gpio_pin_get_dt(&button0);
    pin_active = rc;

    if (rc < 0) {
        BOOT_LOG_DBG("io_detect_pin: Failed to read boot detect pin, rc=%d", rc);
        return false;
    }

    BOOT_LOG_DBG("io_detect_pin: initial read = %d", pin_active);

    if (pin_active) {
        if (BUTTON_0_DETECT_DELAY > 0) {
#ifdef CONFIG_MULTITHREADING
            k_sleep(K_MSEC(50));
#else
            k_busy_wait(50000);
#endif

            /* Get the uptime for debounce purposes. */
            int64_t timestamp = k_uptime_get();

            for(;;) {
                rc = gpio_pin_get_dt(&button0);
                pin_active = rc;
                if (rc < 0) {
                    BOOT_LOG_DBG("Failed to read boot detect pin.");
                    return false;
                }

                /* Get delta from when this started */
                uint32_t delta = k_uptime_get() -  timestamp;

                /* If not pressed OR if pressed > debounce period, stop. */
                if (delta >= BUTTON_0_DETECT_DELAY || !pin_active) {
                    break;
                }

                /* Delay 1 ms */
#ifdef CONFIG_MULTITHREADING
                k_sleep(K_MSEC(1));
#else
                k_busy_wait(1000);
#endif
            }
        }
    }

    BOOT_LOG_DBG("io_detect_pin: final result = %d", pin_active);
    return (bool)pin_active;
}
#endif

#if defined(CONFIG_BOOT_SERIAL_PIN_RESET) || defined(CONFIG_BOOT_FIRMWARE_LOADER_PIN_RESET)
bool io_detect_pin_reset(void)
{
    uint32_t reset_cause;
    int rc;

    rc = hwinfo_get_reset_cause(&reset_cause);

    if (rc == 0 && (reset_cause & RESET_PIN)) {
        (void)hwinfo_clear_reset_cause();
        return true;
    }

    return false;
}
#endif

#if defined(CONFIG_BOOT_SERIAL_BOOT_MODE) || defined(CONFIG_BOOT_FIRMWARE_LOADER_BOOT_MODE) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_BOOT_MODE)
bool io_detect_boot_mode(void)
{
    int32_t boot_mode;

    boot_mode = bootmode_check(BOOT_MODE_TYPE_BOOTLOADER);
    BOOT_LOG_DBG("io_detect_boot_mode: bootmode_check returned %d", (int)boot_mode);

    if (boot_mode == 1) {
        /* Boot mode to stay in bootloader, clear status and enter serial
         * recovery mode
         */
        BOOT_LOG_DBG("io_detect_boot_mode: boot mode flag set, clearing and entering");
        bootmode_clear();

        return true;
    }

    BOOT_LOG_DBG("io_detect_boot_mode: no boot mode flag");
    return false;
}
#endif

#if defined(CONFIG_BOOT_SERIAL_DOUBLE_TAP) || defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)

#if defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
#define DOUBLE_TAP_DELAY_MS CONFIG_MCUBOOT_UF2_DOUBLE_TAP_DELAY
#else
#define DOUBLE_TAP_DELAY_MS CONFIG_BOOT_SERIAL_DOUBLE_TAP_DELAY
#endif

bool io_detect_double_tap(void)
{
    BOOT_LOG_DBG("io_detect_double_tap: checking");

#if defined(CONFIG_BOOT_SERIAL_ENTRANCE_GPIO) || defined(CONFIG_BOOT_USB_DFU_GPIO) || \
    defined(CONFIG_BOOT_FIRMWARE_LOADER_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
    /* Check GPIO button immediately — no delay for button-hold entrance */
    if (device_is_ready(button0.port)) {
        gpio_pin_configure_dt(&button0, GPIO_INPUT);
        if (gpio_pin_get_dt(&button0) > 0) {
            BOOT_LOG_DBG("io_detect_double_tap: GPIO button held, entering");
            return true;
        }
    }
#endif

    /* Check if the boot mode flag was already set from a previous boot */
    if (bootmode_check(BOOT_MODE_TYPE_BOOTLOADER) == 1) {
        BOOT_LOG_DBG("io_detect_double_tap: double tap detected (flag was set)");
        bootmode_clear();
        return true;
    }

    /* Set the flag — if we reset during the wait window, next boot detects it */
    BOOT_LOG_DBG("io_detect_double_tap: setting flag, waiting %d ms",
                 DOUBLE_TAP_DELAY_MS);
    bootmode_set(BOOT_MODE_TYPE_BOOTLOADER);

    int64_t start = k_uptime_get();

#if defined(CONFIG_BOOT_SERIAL_ENTRANCE_GPIO) || defined(CONFIG_BOOT_USB_DFU_GPIO) || \
    defined(CONFIG_BOOT_FIRMWARE_LOADER_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
    /* Re-configure after bootmode_set may have changed pin state */
    gpio_pin_configure_dt(&button0, GPIO_INPUT);
#endif

    while ((k_uptime_get() - start) < DOUBLE_TAP_DELAY_MS) {
#if defined(CONFIG_BOOT_SERIAL_ENTRANCE_GPIO) || defined(CONFIG_BOOT_USB_DFU_GPIO) || \
    defined(CONFIG_BOOT_FIRMWARE_LOADER_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_GPIO) || \
    defined(CONFIG_MCUBOOT_UF2_ENTRANCE_DOUBLE_TAP)
        if (gpio_pin_get_dt(&button0) > 0) {
            BOOT_LOG_DBG("io_detect_double_tap: button pressed during window");
            bootmode_clear();
            return true;
        }
#endif
#ifdef CONFIG_MULTITHREADING
        k_sleep(K_MSEC(1));
#else
        k_busy_wait(1000);
#endif
    }

    /* Timeout expired, no double tap or button press */
    BOOT_LOG_DBG("io_detect_double_tap: timeout, no double tap");
    bootmode_clear();
    return false;
}
#endif
