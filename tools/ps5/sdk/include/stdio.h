/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef _STDIO_H_
#define _STDIO_H_

#include <sys/cdefs.h>
#include <sys/_null.h>
#include <sys/_types.h>

#ifndef _SIZE_T_DECLARED
typedef __size_t size_t;
#define _SIZE_T_DECLARED
#endif

/*
 * Keep wchar.h's tag and declaration guards, but never expose a FILE layout.
 * Native streams are not the SDK's FreeBSD __sFILE implementation.
 */
#ifndef _STDFILE_DECLARED
typedef struct __sFILE FILE;
#define _STDFILE_DECLARED
#endif

/* Native fgetpos/fsetpos exchange an offset followed by 16 opaque state bytes. */
typedef struct {
  __int64_t __offset;
  unsigned char __state[16];
} fpos_t;

#if __POSIX_VISIBLE >= 200809
#ifndef _OFF_T_DECLARED
typedef __off_t off_t;
#define _OFF_T_DECLARED
#endif
#ifndef _SSIZE_T_DECLARED
typedef __ssize_t ssize_t;
#define _SSIZE_T_DECLARED
#endif
#endif

#if __POSIX_VISIBLE >= 200112 || __XSI_VISIBLE
#ifndef _VA_LIST_DECLARED
typedef __va_list va_list;
#define _VA_LIST_DECLARED
#endif
#endif

#define _IOFBF 0
#define _IOLBF 1
#define _IONBF 2
#define BUFSIZ 1024
#define EOF (-1)
#ifndef FOPEN_MAX
#define FOPEN_MAX 20
#endif
#define FILENAME_MAX 1024
#define L_tmpnam 1024
#define TMP_MAX 308915776
#ifndef SEEK_SET
#define SEEK_SET 0
#endif
#ifndef SEEK_CUR
#define SEEK_CUR 1
#endif
#ifndef SEEK_END
#define SEEK_END 2
#endif

__BEGIN_DECLS
#ifndef _STDSTREAM_DECLARED
extern FILE *__stdinp;
extern FILE *__stdoutp;
extern FILE *__stderrp;
#define _STDSTREAM_DECLARED
#endif

/* These are exported pointer objects, not the native _Stdin/_Stdout/_Stderr. */
#define stdin __stdinp
#define stdout __stdoutp
#define stderr __stderrp

void clearerr(FILE *);
int fclose(FILE *);
int feof(FILE *);
int ferror(FILE *);
int fflush(FILE *);
int fgetc(FILE *);
int fgetpos(FILE * __restrict, fpos_t * __restrict);
char *fgets(char * __restrict, int, FILE * __restrict);
FILE *fopen(const char * __restrict, const char * __restrict);
int fprintf(FILE * __restrict, const char * __restrict, ...) __printflike(2, 3);
int fputc(int, FILE *);
int fputs(const char * __restrict, FILE * __restrict);
size_t fread(void * __restrict, size_t, size_t, FILE * __restrict);
FILE *freopen(const char * __restrict, const char * __restrict, FILE * __restrict);
int fscanf(FILE * __restrict, const char * __restrict, ...) __scanflike(2, 3);
int fseek(FILE *, long, int);
int fsetpos(FILE *, const fpos_t *);
long ftell(FILE *);
size_t fwrite(const void * __restrict, size_t, size_t, FILE * __restrict);
int getc(FILE *);
int getchar(void);
#if __ISO_C_VISIBLE < 2011 && (!defined(__cplusplus) || __cplusplus < 201402L)
char *gets(char *);
#endif
void perror(const char *);
int printf(const char * __restrict, ...) __printflike(1, 2);
int putc(int, FILE *);
int putchar(int);
int puts(const char *);
int remove(const char *);
int rename(const char *, const char *);
void rewind(FILE *);
int scanf(const char * __restrict, ...) __scanflike(1, 2);
void setbuf(FILE * __restrict, char * __restrict);
int setvbuf(FILE * __restrict, char * __restrict, int, size_t);
int sprintf(char * __restrict, const char * __restrict, ...) __printflike(2, 3);
int sscanf(const char * __restrict, const char * __restrict, ...) __scanflike(2, 3);
FILE *tmpfile(void);
char *tmpnam(char *);
int ungetc(int, FILE *);
int vfprintf(FILE * __restrict, const char * __restrict, __va_list) __printflike(2, 0);
int vprintf(const char * __restrict, __va_list) __printflike(1, 0);
int vsprintf(char * __restrict, const char * __restrict, __va_list) __printflike(2, 0);

#if __ISO_C_VISIBLE >= 1999 || __POSIX_VISIBLE >= 199506
int snprintf(char * __restrict, size_t, const char * __restrict, ...) __printflike(3, 4);
int vsnprintf(char * __restrict, size_t, const char * __restrict, __va_list) __printflike(3, 0);
#endif
#if __ISO_C_VISIBLE >= 1999
int vfscanf(FILE * __restrict, const char * __restrict, __va_list) __scanflike(2, 0);
int vscanf(const char * __restrict, __va_list) __scanflike(1, 0);
int vsscanf(const char * __restrict, const char * __restrict, __va_list) __scanflike(2, 0);
#endif

#if __POSIX_VISIBLE
#define L_ctermid 1024
char *ctermid(char *);
FILE *fdopen(int, const char *);
int fileno(FILE *);
#endif
#if __POSIX_VISIBLE >= 199209
int pclose(FILE *);
FILE *popen(const char *, const char *);
#endif
#if __POSIX_VISIBLE >= 199506
int ftrylockfile(FILE *);
void flockfile(FILE *);
void funlockfile(FILE *);
int getc_unlocked(FILE *);
int getchar_unlocked(void);
int putc_unlocked(int, FILE *);
int putchar_unlocked(int);
#endif
#if __POSIX_VISIBLE >= 200112
int fseeko(FILE *, __off_t, int);
__off_t ftello(FILE *);
#endif
#if __XSI_VISIBLE
char *tempnam(const char *, const char *);
#endif
#if __POSIX_VISIBLE >= 200809
FILE *fmemopen(void * __restrict, size_t, const char * __restrict);
__ssize_t getdelim(char ** __restrict, size_t * __restrict, int, FILE * __restrict);
FILE *open_memstream(char **, size_t *);
int renameat(int, const char *, int, const char *);
int vdprintf(int, const char * __restrict, __va_list) __printflike(2, 0);
#if defined(_WITH_GETLINE) || defined(_BSD_SOURCE) || defined(_GNU_SOURCE) || \
    (defined(_POSIX_C_SOURCE) && _POSIX_C_SOURCE >= 200809)
__ssize_t getline(char ** __restrict, size_t * __restrict, FILE * __restrict);
#endif
#if defined(_WITH_DPRINTF) || defined(_BSD_SOURCE) || defined(_GNU_SOURCE) || \
    (defined(_POSIX_C_SOURCE) && _POSIX_C_SOURCE >= 200809)
int dprintf(int, const char * __restrict, ...) __printflike(2, 3);
#endif
#endif

#if __BSD_VISIBLE
int asprintf(char **, const char *, ...) __printflike(2, 3);
int vasprintf(char **, const char *, __va_list) __printflike(2, 0);
void clearerr_unlocked(FILE *);
int feof_unlocked(FILE *);
int ferror_unlocked(FILE *);
int fileno_unlocked(FILE *);

/* Native libc lacks these exports; use its public, recursively locked calls. */
static __inline int fflush_unlocked(FILE *stream) { return fflush(stream); }
static __inline int fputc_unlocked(int c, FILE *stream) { return fputc(c, stream); }
static __inline int fputs_unlocked(const char *s, FILE *stream) { return fputs(s, stream); }
static __inline size_t fread_unlocked(void *p, size_t size, size_t count, FILE *stream)
{
  return fread(p, size, count, stream);
}
static __inline size_t fwrite_unlocked(const void *p, size_t size, size_t count, FILE *stream)
{
  return fwrite(p, size, count, stream);
}
#endif
__END_DECLS

#endif /* _STDIO_H_ */
