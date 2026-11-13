/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

extern int sceMbusInit(void);

int main(void)
{
    const uint8_t* code = (const uint8_t*)(uintptr_t)&sceMbusInit;
    printf("[remote-state] pid=%d Mbus init address=%p; no native initialization requested\n",
           getpid(), (const void*)code);
    fflush(stdout);
    /* Native text is execute-only. Use the verified console ELF's data offset. */
    if (!code || ((uintptr_t)code & 0x3fff) != 0x3d60)
    {
        puts("[remote-state] native entry does not match the reference; state not read");
        return 1;
    }
    const volatile uint32_t* state =
        (const volatile uint32_t*)(code + (0x1032c - 0x3d60));
    printf("[remote-state] reading data address=%p\n", (const void*)state);
    fflush(stdout);
    printf("[remote-state] state=%u (0=not started, 1=initializing, 2=ready)\n", *state);
    fflush(stdout);
    return 0;
}
