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

#ifndef H_IO_
#define H_IO_

#include <stddef.h>

#ifdef CONFIG_SOC_FAMILY_NORDIC_NRF
#include <helpers/nrfx_reset_reason.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Initialises the configured LED.
 */
void io_led_init(void);

/*
 * Sets value of the configured LED (solid on/off). Stops any blink
 * pattern previously started with io_led_blink().
 */
void io_led_set(int value);

/* LED blink cycle lengths (ms), mirroring the indicator tempos of
 * Adafruit_nRF52_Bootloader and tinyuf2:
 * - idle  : slow breathing, bootloader waiting around
 *           (STATE_USB_MOUNTED, tinyuf2 board_timer_start(5))
 * - wait  : fast fade, DFU mode waiting for firmware
 *           (STATE_USB_UNMOUNTED, tinyuf2 board_timer_start(1))
 * - write : very fast blink, flash write in progress
 *           (STATE_WRITING_STARTED, tinyuf2 board_timer_start(25))
 */
#define IO_LED_BLINK_IDLE_CYCLE_MS   3000
#define IO_LED_BLINK_WAIT_CYCLE_MS    300
#define IO_LED_BLINK_WRITE_CYCLE_MS   100

/*
 * Starts blinking (fading) the LED with the given cycle length in ms.
 * The LED breathes with a triangle fade like Adafruit_nRF52_Bootloader's
 * led_tick(); short cycles read as a fast blink. Repeated calls with the
 * same cycle length are ignored (the pattern keeps running), so callers
 * can invoke this on every flash write.
 */
void io_led_blink(uint32_t cycle_ms);

/*
 * Checks if GPIO is set in the required way to remain in serial recovery mode
 *
 * @retval	false for normal boot, true for serial recovery boot
 */
bool io_detect_pin(void);

/*
 * Checks if board was reset using reset pin and if device should stay in
 * serial recovery mode
 *
 * @retval	false for normal boot, true for serial recovery boot
 */
bool io_detect_pin_reset(void);

/*
 * Checks board boot mode via retention subsystem and if device should stay in
 * serial recovery mode
 *
 * @retval	false for normal boot, true for serial recovery boot
 */
bool io_detect_boot_mode(void);

/*
 * Checks for double tap of the reset button using the retention subsystem.
 * Optionally checks boot button during wait period if GPIO entrance is configured.
 *
 * @retval	false for normal boot, true for serial recovery boot
 */
bool io_detect_double_tap(void);

#ifdef CONFIG_SOC_FAMILY_NORDIC_NRF
static inline bool io_boot_skip_serial_recovery()
{
    uint32_t rr = nrfx_reset_reason_get();

#ifdef NRF_RESETINFO
    /* The following reset reasons should allow to enter firmware recovery or
     * loader through an IO state.
     */
    const uint32_t allowed_reset_reasons = (
        /* Reset from power on reset (reset reason POR or BOR). */
        NRFX_RESET_REASON_POR_MASK
#ifdef RESETINFO_RESETREAS_GLOBAL_RESETPOR_Msk
        /* Reset from power on reset (reset reason other than POR or BOR). */
        | RESETINFO_RESETREAS_GLOBAL_RESETPOR_Msk
#endif
        /* Reset from pin reset is not masked: it always enables the io-based recovery. */
        /* Reset from the watchdog timer. */
        | NRFX_RESET_REASON_DOG_MASK
        /* Reset from CTRL-AP. */
        | NRFX_RESET_REASON_CTRLAP_MASK
        /* Reset due to secure domain system reset request. */
        | NRFX_RESET_REASON_SREQ_MASK
        /* Reset due to secure domain watchdog 0 timer. */
        | NRFX_RESET_REASON_SECWDT0_MASK
        /* Reset due to secure domain watchdog 1 timer. */
        | NRFX_RESET_REASON_SECWDT1_MASK
        /* Reset due to secure domain lockup. */
        | NRFX_RESET_REASON_LOCKUP_MASK
#if NRF_RESETINFO_HAS_LOCAL_WDT
        /* Reset from the local watchdog timer 0. */
        | NRFX_RESET_REASON_LOCAL_DOG0_MASK
        /* Reset from the local watchdog timer 1. */
        | NRFX_RESET_REASON_LOCAL_DOG1_MASK
#endif
        /* Reset from the local soft reset request. */
        | NRFX_RESET_REASON_LOCAL_SREQ_MASK
        /* Reset from local CPU lockup. */
        | NRFX_RESET_REASON_LOCAL_LOCKUP_MASK
    );

    rr &= ~allowed_reset_reasons;
#endif /* NRF_RESETINFO */

    return !(rr == 0 || (rr & NRFX_RESET_REASON_RESETPIN_MASK));
}
#else
static inline bool io_boot_skip_serial_recovery()
{
    return false;
}
#endif

#ifdef __cplusplus
}
#endif

#endif
