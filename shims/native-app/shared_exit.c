/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "shared_tls.h"

_Noreturn void __real_exit(int status);

_Noreturn void __wrap_exit(int status)
{
  ps5_tls_thread_exit();
  __real_exit(status);
}
