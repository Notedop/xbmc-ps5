/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include <stdint.h>

enum
{
  PS5_SPRX_TRACE_ENTER = 1,
  PS5_SPRX_TRACE_LEAVE = 2,
};

typedef struct
{
  uint32_t version;
  void (*emit)(int stage);
} PS5SprxStartTrace;
