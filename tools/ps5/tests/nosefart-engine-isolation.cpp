/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <cassert>

extern "C"
{
extern unsigned char nosefart_tag_nes6502_mem_access;
extern unsigned char nosefart_0_nes6502_mem_access;
extern unsigned char nosefart_1_nes6502_mem_access;
unsigned char nosefart_tag_nsf_nes6502_mem_access();
unsigned char nosefart_0_nsf_nes6502_mem_access();
unsigned char nosefart_1_nsf_nes6502_mem_access();
extern int nosefart_tag_max_access[16];
extern int nosefart_0_max_access[16];
extern int nosefart_1_max_access[16];
}

int main()
{
  assert(&nosefart_tag_nes6502_mem_access != &nosefart_0_nes6502_mem_access);
  assert(&nosefart_tag_nes6502_mem_access != &nosefart_1_nes6502_mem_access);
  assert(&nosefart_0_nes6502_mem_access != &nosefart_1_nes6502_mem_access);

  nosefart_tag_nes6502_mem_access = 1;
  nosefart_0_nes6502_mem_access = 2;
  nosefart_1_nes6502_mem_access = 4;
  assert(nosefart_tag_nsf_nes6502_mem_access() == 1);
  assert(nosefart_0_nsf_nes6502_mem_access() == 2);
  assert(nosefart_1_nsf_nes6502_mem_access() == 4);

  nosefart_tag_max_access[0] = 17;
  nosefart_0_max_access[0] = 23;
  nosefart_1_max_access[0] = 31;
  assert(nosefart_tag_max_access[0] == 17);
  assert(nosefart_0_max_access[0] == 23);
  assert(nosefart_1_max_access[0] == 31);

  nosefart_0_nes6502_mem_access = 0;
  nosefart_0_max_access[0] = 0;
  assert(nosefart_tag_nsf_nes6502_mem_access() == 1);
  assert(nosefart_1_nsf_nes6502_mem_access() == 4);
  assert(nosefart_tag_max_access[0] == 17);
  assert(nosefart_1_max_access[0] == 31);
}
