/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PS5_NATIVE_ABI_CHECK_H
#define PS5_NATIVE_ABI_CHECK_H

#if !defined(__x86_64__) || !defined(__znver2__)
#error "PS5 native modules require the x86-64 znver2 target"
#endif
#if !defined(__FreeBSD__) || __FreeBSD__ != 9
#error "PS5 native modules require FreeBSD 9 compiler ABI"
#endif
#if !defined(__SIZEOF_WCHAR_T__) || __SIZEOF_WCHAR_T__ != 2 || __WCHAR_WIDTH__ != 16
#error "PS5 native modules require 16-bit wchar_t"
#endif
#if !defined(__BIGGEST_ALIGNMENT__) || __BIGGEST_ALIGNMENT__ != 32
#error "PS5 native modules require the SDK 32-byte biggest alignment"
#endif
#if !defined(__PROSPERO__) || __PROSPERO__ != 1 || !defined(__SCE__) || __SCE__ != 1
#error "PS5 native modules require the Sony SDK platform guards"
#endif
#if !defined(__STDC_NO_THREADS__) || __STDC_NO_THREADS__ != 1 || \
    !defined(__STDC_NO_COMPLEX__) || __STDC_NO_COMPLEX__ != 1
#error "PS5 native modules require the SDK no-threads/no-complex guards"
#endif
#if defined(__FLOAT128__) || defined(__SIZEOF_FLOAT128__) || defined(__STDC_MB_MIGHT_NEQ_WC__)
#error "PS5 native modules require the SDK floating-point and wchar feature guards"
#endif

#endif
