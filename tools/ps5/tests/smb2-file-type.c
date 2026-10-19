/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <time.h>

#include <smb2/smb2.h>
#include <smb2/libsmb2.h>

#include "smb2-file-type.h"

static void check(uint32_t attributes, uint32_t expected)
{
  const uint32_t actual = smb2_type_from_attributes(attributes);
  if (actual != expected)
  {
    fprintf(stderr, "attributes=0x%08x: expected type=%u, got=%u\n",
            attributes, expected, actual);
    exit(EXIT_FAILURE);
  }
}

int main(void)
{
  check(0, SMB2_TYPE_FILE);
  check(SMB2_FILE_ATTRIBUTE_DIRECTORY, SMB2_TYPE_DIRECTORY);
  check(SMB2_FILE_ATTRIBUTE_REPARSE_POINT, SMB2_TYPE_LINK);
  check(SMB2_FILE_ATTRIBUTE_DIRECTORY | SMB2_FILE_ATTRIBUTE_REPARSE_POINT,
        SMB2_TYPE_DIRECTORY);
  check(0x00080431, SMB2_TYPE_DIRECTORY);
  check(0x00080420, SMB2_TYPE_LINK);
  check(0x00180020, SMB2_TYPE_FILE);

  for (uint32_t attributes = 0; attributes < 0x1000; ++attributes)
  {
    const uint32_t expected =
        (attributes & SMB2_FILE_ATTRIBUTE_DIRECTORY) ? SMB2_TYPE_DIRECTORY :
        (attributes & SMB2_FILE_ATTRIBUTE_REPARSE_POINT) ? SMB2_TYPE_LINK : SMB2_TYPE_FILE;
    check(attributes, expected);
    check(attributes | 0x80000000u, expected);
  }
  puts("PASS: SMB directory/reparse precedence and ordinary file/link types");
  return EXIT_SUCCESS;
}
