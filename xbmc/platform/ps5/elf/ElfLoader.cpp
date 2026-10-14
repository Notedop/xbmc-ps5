#include "platform/ps5/elf/ElfLoader.h"

#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <sys/mman.h>
#include <unistd.h>

// ELF64 (x86-64) types and constants, defined here so the file is identical on
// the Linux host and the PS5 (FreeBSD) sysroot, whose <elf.h> differ.
namespace
{
using u8 = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;
using i64 = int64_t;

struct Ehdr { u8 e_ident[16]; u16 e_type, e_machine; u32 e_version; u64 e_entry, e_phoff, e_shoff;
              u32 e_flags; u16 e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx; };
struct Phdr { u32 p_type, p_flags; u64 p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align; };
struct Dyn  { i64 d_tag; u64 d_val; };
struct Sym  { u32 st_name; u8 st_info, st_other; u16 st_shndx; u64 st_value, st_size; };
struct Rela { u64 r_offset; u64 r_info; i64 r_addend; };

constexpr u16 ET_DYN = 3;
constexpr u16 EM_X86_64 = 62;
constexpr u32 PT_LOAD = 1, PT_DYNAMIC = 2, PT_TLS = 7, PT_GNU_EH_FRAME = 0x6474e550;
constexpr u32 PF_X = 1, PF_W = 2, PF_R = 4;

constexpr i64 DT_NULL=0, DT_HASH=4, DT_STRTAB=5, DT_SYMTAB=6, DT_RELA=7, DT_RELASZ=8,
              DT_RELAENT=9, DT_SYMENT=11, DT_INIT=12, DT_PLTGOT=3, DT_PLTRELSZ=2,
              DT_JMPREL=23, DT_INIT_ARRAY=25, DT_INIT_ARRAYSZ=27, DT_FINI=13,
              DT_FINI_ARRAY=26, DT_FINI_ARRAYSZ=28, DT_GNU_HASH=0x6ffffef5;

constexpr u16 SHN_UNDEF = 0;
constexpr u8 STB_WEAK = 2;

// x86-64 relocation types (ABI-fixed).
constexpr u32 R_X86_64_64=1, R_X86_64_GLOB_DAT=6, R_X86_64_JUMP_SLOT=7, R_X86_64_RELATIVE=8,
              R_X86_64_DTPMOD64=16, R_X86_64_DTPOFF64=17, R_X86_64_TPOFF64=18;

inline u32 R_SYM(u64 i) { return static_cast<u32>(i >> 32); }
inline u32 R_TYPE(u64 i) { return static_cast<u32>(i & 0xffffffff); }

size_t page() { long p = sysconf(_SC_PAGESIZE); return p > 0 ? static_cast<size_t>(p) : 0x4000; }
u64 trunc_page(u64 x, size_t pg) { return x & ~static_cast<u64>(pg - 1); }
u64 round_page(u64 x, size_t pg) { return (x + pg - 1) & ~static_cast<u64>(pg - 1); }
} // namespace

namespace ps5elf
{

struct Image
{
  struct Destructor
  {
    void (*function)(void*);
    void* argument;
    void* dso;
    Destructor* next;
  };
  u8* base = nullptr;      // load bias (mapping - trunc(min_vaddr))
  u8* map = nullptr;       // actual mmap start
  size_t span = 0;         // mmap length
  const Phdr* phdr = nullptr;
  u16 phnum = 0;
  const Sym* symtab = nullptr;
  const char* strtab = nullptr;
  u32 symcount = 0;      // from DT_HASH nchain / GNU hash; 0 = unknown
  size_t strsz = 0;      // DT_STRSZ, to bound st_name
  void (*init)() = nullptr;
  void (**init_array)() = nullptr;
  size_t init_arrayn = 0;
  void (*fini)() = nullptr;
  void (**fini_array)() = nullptr;
  size_t fini_arrayn = 0;
  Destructor* destructors = nullptr;
  Image* next = nullptr;
  bool initialized = false;
  bool unloading = false;
  HostResolver resolver = nullptr;
  void* user = nullptr;
  Diagnostic diagnostic = nullptr;
  void* diagnostic_user = nullptr;
  u8* eh_frame_hdr = nullptr; // PT_GNU_EH_FRAME (.eh_frame_hdr), for the unwinder
  bool eh_registered = false;
  Stats st{};
};

namespace
{
std::mutex g_images_mutex;
Image* g_images = nullptr;

bool contains(const Image* img, const void* pointer)
{
  const uintptr_t address = reinterpret_cast<uintptr_t>(pointer);
  const uintptr_t start = reinterpret_cast<uintptr_t>(img->map);
  return address >= start && address - start < img->span;
}

// Never forward add-on registrations to the process runtime: it would retain
// function pointers into memory that unload() is about to release.
int module_atexit(void (*function)(void*), void* argument, void* dso)
{
  if (!function)
    return -1;
  auto* entry = static_cast<Image::Destructor*>(std::malloc(sizeof(Image::Destructor)));
  if (!entry)
    return -1;
  std::lock_guard<std::mutex> lock(g_images_mutex);
  for (Image* img = g_images; img; img = img->next)
  {
    if ((dso && contains(img, dso)) || (!dso && contains(img, reinterpret_cast<void*>(function))))
    {
      *entry = {function, argument, dso, img->destructors};
      img->destructors = entry;
      return 0;
    }
  }
  std::free(entry);
  return -1;
}

void finalize(Image* img, void* dso, bool all)
{
  for (;;)
  {
    Image::Destructor* entry = nullptr;
    {
      std::lock_guard<std::mutex> lock(g_images_mutex);
      auto** link = &img->destructors;
      while (*link && !all && (*link)->dso != dso)
        link = &(*link)->next;
      if (!*link)
        return;
      entry = *link;
      *link = entry->next;
    }
    const auto function = entry->function;
    void* argument = entry->argument;
    std::free(entry); // remove before calling, including recursive finalize()
    function(argument);
  }
}

void module_finalize(void* dso)
{
  // A module-specific CRT passes its __dso_handle. Process-wide finalization
  // belongs to the host and must not tear down unrelated add-ons.
  if (!dso)
    return;
  Image* owner = nullptr;
  {
    std::lock_guard<std::mutex> lock(g_images_mutex);
    for (Image* img = g_images; img; img = img->next)
      if (contains(img, dso))
      {
        owner = img;
        break;
      }
  }
  if (owner)
    finalize(owner, dso, false);
}

void report(Diagnostic diagnostic, void* user, const char* fmt, ...)
{
  if (!diagnostic)
    return;
  char message[512];
  va_list args;
  va_start(args, fmt);
  std::vsnprintf(message, sizeof(message), fmt, args);
  va_end(args);
  diagnostic(user, message);
}

void report(const Image* img, const char* fmt, ...)
{
  if (!img || !img->diagnostic)
    return;
  char message[512];
  va_list args;
  va_start(args, fmt);
  std::vsnprintf(message, sizeof(message), fmt, args);
  va_end(args);
  img->diagnostic(img->diagnostic_user, message);
}

u32 gnu_hash_symcount(const u32* gh)
{
  // Derive the highest symbol index + 1 from a GNU hash table.
  const u32 nbuckets = gh[0];
  const u32 symoffset = gh[1];
  const u32 bloom_size = gh[2];
  const u32* buckets = gh + 4 + bloom_size * 2; // 64-bit bloom words
  const u32* chain = buckets + nbuckets;
  u32 last = 0;
  for (u32 i = 0; i < nbuckets; ++i)
    if (buckets[i] > last)
      last = buckets[i];
  if (last < symoffset)
    return symoffset;
  // walk the chain of the highest bucket to its terminator (low bit set)
  u32 idx = last;
  while (!(chain[idx - symoffset] & 1))
    ++idx;
  return idx + 1;
}

// Resolve the symbol a relocation refers to. Returns false on hard failure.
bool resolve(Image* img, u32 symidx, u64* out, char* err, size_t errlen)
{
  // A wrong relocation count or entry size would walk past .rela.dyn and hand
  // us a nonsense index; dereferencing it faults in unmapped memory. Fail with
  // a message instead (seen on console: page fault inside resolve()).
  if (!img->symtab || !img->strtab || img->symcount == 0 || symidx >= img->symcount)
  {
    std::snprintf(err, errlen,
                  "bad symbol index %u (symbol table has %u entries%s)", symidx, img->symcount,
                  img->symcount == 0 ? ": DT_HASH/DT_GNU_HASH missing or unparsed" : "");
    return false;
  }
  const Sym& s = img->symtab[symidx];
  if (s.st_shndx != SHN_UNDEF)
  {
    *out = reinterpret_cast<u64>(img->base) + s.st_value; // defined here
    return true;
  }
  if (img->strsz && s.st_name >= img->strsz)
  {
    std::snprintf(err, errlen, "symbol %u: name offset %u past the string table (%zu bytes)",
                  symidx, s.st_name, img->strsz);
    return false;
  }
  const char* name = img->strtab + s.st_name;
  void* h = nullptr;
  if (std::strcmp(name, "__cxa_atexit") == 0)
    h = reinterpret_cast<void*>(&module_atexit);
  else if (std::strcmp(name, "__cxa_finalize") == 0)
    h = reinterpret_cast<void*>(&module_finalize);
  else if (std::strcmp(name, "__cxa_thread_atexit") == 0 ||
           std::strcmp(name, "__cxa_thread_atexit_impl") == 0 ||
           std::strcmp(name, "atexit") == 0)
  {
    std::snprintf(err, errlen, "unsupported unload-unsafe destructor registration: %s", name);
    return false;
  }
  else
    h = img->resolver ? img->resolver(name, img->user) : nullptr;
  if (h)
  {
    *out = reinterpret_cast<u64>(h);
    return true;
  }
  if ((s.st_info >> 4) == STB_WEAK)
  {
    *out = 0; // unresolved weak -> 0, legal
    return true;
  }
  std::snprintf(err, errlen, "undefined symbol: %s", name);
  return false;
}

bool apply_rela(Image* img, const Rela* r, size_t n, char* err, size_t errlen)
{
  const u64 bias = reinterpret_cast<u64>(img->base);
  for (size_t i = 0; i < n; ++i)
  {
    const u32 type = R_TYPE(r[i].r_info);
    const u32 sym = R_SYM(r[i].r_info);
    u64* where = reinterpret_cast<u64*>(bias + r[i].r_offset);
    switch (type)
    {
      case R_X86_64_RELATIVE:
        *where = bias + static_cast<u64>(r[i].r_addend);
        img->st.relative++;
        break;
      case R_X86_64_64:
      case R_X86_64_GLOB_DAT:
      {
        u64 v = 0;
        if (!resolve(img, sym, &v, err, errlen))
        {
          const char* name = "<invalid-symbol-index>";
          if (img->symtab && sym < img->symcount)
          {
            const Sym& unresolved = img->symtab[sym];
            name = unresolved.st_name < img->strsz ? img->strtab + unresolved.st_name
                                                   : "<invalid-name-offset>";
          }
          report(img,
                 "phase=reloc-unresolved type=%u offset=0x%llx symbol=%s symbol-index=%u error=%s",
                 type, static_cast<unsigned long long>(r[i].r_offset), name, sym, err);
          return false;
        }
        *where = v + static_cast<u64>(r[i].r_addend);
        img->st.glob_dat++;
        break;
      }
      case R_X86_64_JUMP_SLOT:
      {
        u64 v = 0;
        if (!resolve(img, sym, &v, err, errlen))
        {
          const char* name = "<invalid-symbol-index>";
          if (img->symtab && sym < img->symcount)
          {
            const Sym& unresolved = img->symtab[sym];
            name = unresolved.st_name < img->strsz ? img->strtab + unresolved.st_name
                                                   : "<invalid-name-offset>";
          }
          report(img,
                 "phase=reloc-unresolved type=%u offset=0x%llx symbol=%s symbol-index=%u error=%s",
                 type, static_cast<unsigned long long>(r[i].r_offset), name, sym, err);
          return false;
        }
        *where = v;
        img->st.jump_slot++;
        break;
      }
      case R_X86_64_DTPMOD64:
      case R_X86_64_DTPOFF64:
      case R_X86_64_TPOFF64:
        img->st.tls++; // not yet supported; left as-is, reported by stats
        break;
      default:
        img->st.other++;
        break;
    }
  }
  return true;
}
} // namespace

Image* load(const void* image, size_t image_len, HostResolver resolver, void* user,
            char* err, size_t errlen, Diagnostic diagnostic, void* diagnostic_user)
{
  report(diagnostic, diagnostic_user, "phase=load-enter image=%p bytes=%zu", image, image_len);
  auto fail = [&](const char* m) -> Image* {
    std::snprintf(err, errlen, "%s", m);
    report(diagnostic, diagnostic_user, "phase=load-reject reason=%s", m);
    return nullptr;
  };
  if (image_len < sizeof(Ehdr))
    return fail("image too small");
  const auto* e = static_cast<const Ehdr*>(image);
  if (std::memcmp(e->e_ident, "\x7f""ELF", 4) != 0)
    return fail("not an ELF file");
  if (e->e_ident[4] != 2 /*ELFCLASS64*/ || e->e_ident[5] != 1 /*little-endian*/)
    return fail("not ELF64 LE");
  if (e->e_type != ET_DYN)
    return fail("not a shared object (ET_DYN)");
  if (e->e_machine != EM_X86_64)
    return fail("not x86-64");

  const size_t pg = page();
  const auto* ph = reinterpret_cast<const Phdr*>(static_cast<const u8*>(image) + e->e_phoff);

  // span of all PT_LOAD
  u64 min_v = ~0ull, max_v = 0;
  const Phdr* dynph = nullptr;
  bool has_tls = false;
  u64 eh_frame_hdr_v = 0; // PT_GNU_EH_FRAME vaddr; resolved to a pointer after base is known
  u16 load_segments = 0;
  for (u16 i = 0; i < e->e_phnum; ++i)
  {
    if (ph[i].p_type == PT_LOAD)
    {
      ++load_segments;
      min_v = ph[i].p_vaddr < min_v ? ph[i].p_vaddr : min_v;
      u64 end = ph[i].p_vaddr + ph[i].p_memsz;
      max_v = end > max_v ? end : max_v;
    }
    else if (ph[i].p_type == PT_GNU_EH_FRAME)
      eh_frame_hdr_v = ph[i].p_vaddr;
    else if (ph[i].p_type == PT_DYNAMIC)
      dynph = &ph[i];
    else if (ph[i].p_type == PT_TLS)
      has_tls = true;
  }
  if (min_v == ~0ull || !dynph)
    return fail("no PT_LOAD or no PT_DYNAMIC");

  const u64 base_v = trunc_page(min_v, pg);
  const size_t span = round_page(max_v, pg) - base_v;
  report(diagnostic, diagnostic_user,
         "phase=map-plan page=%zu load-segments=%u vaddr=0x%llx..0x%llx span=0x%zx tls=%s eh-frame-hdr=0x%llx",
         pg, load_segments, static_cast<unsigned long long>(min_v),
         static_cast<unsigned long long>(max_v), span, has_tls ? "yes" : "no",
         static_cast<unsigned long long>(eh_frame_hdr_v));
  void* m = mmap(nullptr, span, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (m == MAP_FAILED)
  {
    report(diagnostic, diagnostic_user, "phase=map-reserve result=failed span=0x%zx errno=%d (%s)",
           span, errno, std::strerror(errno));
    return fail("reservation mmap failed");
  }

  auto* img = new Image();
  img->map = static_cast<u8*>(m);
  img->span = span;
  img->base = static_cast<u8*>(m) - base_v; // load bias
  if (eh_frame_hdr_v) img->eh_frame_hdr = img->base + eh_frame_hdr_v;
  img->resolver = resolver;
  img->user = user;
  img->diagnostic = diagnostic;
  img->diagnostic_user = diagnostic_user;
  (void)has_tls;
  report(img, "phase=map-reserved map=%p bias=%p span=0x%zx", img->map, img->base, img->span);

  // Copy each PT_LOAD in, RW (route B: write first, flip to X later).
  constexpr u16 kMaxLoggedSegments = 16;
  u16 logged_rw_segments = 0;
  for (u16 i = 0; i < e->e_phnum; ++i)
  {
    if (ph[i].p_type != PT_LOAD)
      continue;
    u8* seg = img->base + ph[i].p_vaddr;
    u8* segpg = reinterpret_cast<u8*>(trunc_page(reinterpret_cast<u64>(seg), pg));
    size_t seglen = round_page(reinterpret_cast<u64>(seg) + ph[i].p_memsz, pg)
                    - reinterpret_cast<u64>(segpg);
    if (logged_rw_segments < kMaxLoggedSegments)
      report(img,
             "phase=segment-rw-enter index=%u vaddr=0x%llx filesz=0x%llx memsz=0x%llx addr=%p pages=%p+0x%zx flags=0x%x",
             i, static_cast<unsigned long long>(ph[i].p_vaddr),
             static_cast<unsigned long long>(ph[i].p_filesz),
             static_cast<unsigned long long>(ph[i].p_memsz), seg, segpg, seglen, ph[i].p_flags);
    if (mprotect(segpg, seglen, PROT_READ | PROT_WRITE) != 0)
    {
      if (logged_rw_segments < kMaxLoggedSegments)
        report(img, "phase=segment-rw-exit index=%u result=failed errno=%d (%s)", i, errno,
               std::strerror(errno));
      std::snprintf(err, errlen, "mprotect RW failed: %s", std::strerror(errno));
      munmap(m, span);
      delete img;
      return nullptr;
    }
    if (logged_rw_segments < kMaxLoggedSegments)
    {
      report(img, "phase=segment-rw-exit index=%u result=ok", i);
      ++logged_rw_segments;
    }
    std::memcpy(seg, static_cast<const u8*>(image) + ph[i].p_offset, ph[i].p_filesz);
    if (ph[i].p_memsz > ph[i].p_filesz) // .bss
      std::memset(seg + ph[i].p_filesz, 0, ph[i].p_memsz - ph[i].p_filesz);
  }
  if (load_segments > kMaxLoggedSegments)
    report(img, "phase=segment-rw-summary total=%u unlogged=%u", load_segments,
           load_segments - kMaxLoggedSegments);

  // Parse PT_DYNAMIC.
  const auto* dyn = reinterpret_cast<const Dyn*>(img->base + dynph->p_vaddr);
  const Rela* rela = nullptr; size_t relasz = 0;
  const Rela* jmprel = nullptr; size_t pltrelsz = 0;
  const u32* sysv_hash = nullptr; const u32* gnu_hash = nullptr;
  size_t dynamic_count = 0;
  size_t needed_count = 0;
  bool text_relocations = false;
  bool invalid_fini_size = false;
  for (const Dyn* d = dyn; d->d_tag != DT_NULL; ++d)
  {
    ++dynamic_count;
    if (d->d_tag == 1) // DT_NEEDED
      ++needed_count;
    else if (d->d_tag == 22) // DT_TEXTREL
      text_relocations = true;
    switch (d->d_tag)
    {
      case DT_SYMTAB: img->symtab = reinterpret_cast<const Sym*>(img->base + d->d_val); break;
      case 10: img->strsz = static_cast<size_t>(d->d_val); break; // DT_STRSZ
      case DT_STRTAB: img->strtab = reinterpret_cast<const char*>(img->base + d->d_val); break;
      case DT_RELA:   rela = reinterpret_cast<const Rela*>(img->base + d->d_val); break;
      case DT_RELASZ: relasz = d->d_val; break;
      case DT_JMPREL: jmprel = reinterpret_cast<const Rela*>(img->base + d->d_val); break;
      case DT_PLTRELSZ: pltrelsz = d->d_val; break;
      case DT_HASH:   sysv_hash = reinterpret_cast<const u32*>(img->base + d->d_val); break;
      case DT_GNU_HASH: gnu_hash = reinterpret_cast<const u32*>(img->base + d->d_val); break;
      case DT_INIT:   img->init = reinterpret_cast<void(*)()>(img->base + d->d_val); break;
      case DT_INIT_ARRAY: img->init_array = reinterpret_cast<void(**)()>(img->base + d->d_val); break;
      case DT_INIT_ARRAYSZ: img->init_arrayn = d->d_val / sizeof(void*); break;
      case DT_FINI:
        img->fini = d->d_val ? reinterpret_cast<void(*)()>(img->base + d->d_val) : nullptr;
        break;
      case DT_FINI_ARRAY: img->fini_array = reinterpret_cast<void(**)()>(img->base + d->d_val); break;
      case DT_FINI_ARRAYSZ:
        invalid_fini_size = d->d_val % sizeof(void*) != 0;
        img->fini_arrayn = d->d_val / sizeof(void*);
        break;
      default: break;
    }
  }
  // Every table pointer must land inside the mapping we just made. A d_val the
  // image did not intend (or a base we computed wrong) otherwise faults on the
  // first dereference in resolve(); seen on console as a page fault reading a
  // fixed address. Report the numbers so the cause is visible, then fail.
  {
    const u8* lo = img->map;
    const u8* hi = img->map + img->span;
    auto inside = [&](const void* p) {
      const u8* q = static_cast<const u8*>(p);
      return q >= lo && q < hi;
    };
    if ((img->symtab && !inside(img->symtab)) || (img->strtab && !inside(img->strtab)))
    {
      std::snprintf(err, errlen,
                    "dynamic tables outside the mapping: symtab %p strtab %p, "
                    "mapped %p..%p (base %p)",
                    static_cast<const void*>(img->symtab), static_cast<const void*>(img->strtab),
                    static_cast<const void*>(lo), static_cast<const void*>(hi),
                    static_cast<const void*>(img->base));
      munmap(m, span);
      delete img;
      return nullptr;
    }
  }
  if (!img->symtab || !img->strtab)
  {
    munmap(m, span); delete img; return fail("missing DT_SYMTAB/DT_STRTAB");
  }
  if (sysv_hash)
    img->symcount = sysv_hash[1]; // nchain
  else if (gnu_hash)
    img->symcount = gnu_hash_symcount(gnu_hash);
  report(img,
         "phase=dynamic tags=%zu symtab=%p strtab=%p strsz=%zu symcount=%u hash=%s rela=%zu jmprel=%zu init=%p init-array=%zu needed=%zu textrel=%s",
         dynamic_count, static_cast<const void*>(img->symtab),
         static_cast<const void*>(img->strtab), img->strsz, img->symcount,
         sysv_hash ? "sysv" : (gnu_hash ? "gnu" : "none"), relasz / sizeof(Rela),
         pltrelsz / sizeof(Rela), reinterpret_cast<void*>(img->init), img->init_arrayn,
         needed_count, text_relocations ? "yes" : "no");
  if (needed_count || text_relocations)
    report(img, "phase=dynamic-unsupported needed=%zu textrel=%s", needed_count,
           text_relocations ? "yes" : "no");
  // symcount only needed by symbol(); relocations index directly.

  // Apply relocations.
  // Refuse up front when the symbol count is unknown: resolve() would
  // otherwise index the symbol table unchecked (page fault seen on console).
  if (img->symcount == 0 && (relasz || pltrelsz))
  {
    std::snprintf(err, errlen,
                  "no usable symbol count (DT_HASH/DT_GNU_HASH missing); %zu rela + %zu jmprel "
                  "relocations need symbol lookups",
                  relasz / sizeof(Rela), pltrelsz / sizeof(Rela));
    munmap(m, span);
    delete img;
    return nullptr;
  }
  if (rela && !apply_rela(img, rela, relasz / sizeof(Rela), err, errlen))
  {
    munmap(m, span); delete img; return nullptr;
  }
  if (jmprel && !apply_rela(img, jmprel, pltrelsz / sizeof(Rela), err, errlen))
  {
    munmap(m, span); delete img; return nullptr;
  }
  auto valid_array = [&](void (**array)(), size_t count) {
    if (!count)
      return true;
    const uintptr_t start = reinterpret_cast<uintptr_t>(array);
    const uintptr_t bias = reinterpret_cast<uintptr_t>(img->base);
    for (u16 i = 0; i < e->e_phnum; ++i)
      if (ph[i].p_type == PT_LOAD && (ph[i].p_flags & PF_R) &&
          start >= bias + ph[i].p_vaddr && start - bias - ph[i].p_vaddr < ph[i].p_memsz &&
          count <= (ph[i].p_memsz - (start - bias - ph[i].p_vaddr)) / sizeof(void*))
        return true;
    return false;
  };
  auto executable = [&](void (*function)()) {
    if (!function || reinterpret_cast<uintptr_t>(function) == ~uintptr_t{0})
      return true;
    const uintptr_t address = reinterpret_cast<uintptr_t>(function);
    const uintptr_t bias = reinterpret_cast<uintptr_t>(img->base);
    for (u16 i = 0; i < e->e_phnum; ++i)
      if (ph[i].p_type == PT_LOAD && (ph[i].p_flags & PF_X) &&
          address >= bias + ph[i].p_vaddr &&
          address - bias - ph[i].p_vaddr < ph[i].p_memsz)
        return true;
    return false;
  };
  bool valid_finalizers = !invalid_fini_size &&
                          reinterpret_cast<uintptr_t>(img->fini) != ~uintptr_t{0} &&
                          valid_array(img->fini_array, img->fini_arrayn) &&
                          executable(img->fini);
  if (valid_finalizers)
    for (size_t i = 0; i < img->fini_arrayn; ++i)
      valid_finalizers = valid_finalizers && executable(img->fini_array[i]);
  if (!valid_finalizers)
  {
    munmap(m, span); delete img; return fail("invalid finalizer array or function");
  }
  report(img, "phase=relocations relative=%u glob-dat-or-64=%u jump-slot=%u tls=%u other=%u",
         img->st.relative, img->st.glob_dat, img->st.jump_slot, img->st.tls, img->st.other);

  // Final protections per p_flags (route B flip: code becomes R+X now).
  u16 logged_final_segments = 0;
  for (u16 i = 0; i < e->e_phnum; ++i)
  {
    if (ph[i].p_type != PT_LOAD)
      continue;
    int prot = ((ph[i].p_flags & PF_R) ? PROT_READ : 0) |
               ((ph[i].p_flags & PF_W) ? PROT_WRITE : 0) |
               ((ph[i].p_flags & PF_X) ? PROT_EXEC : 0);
    u8* seg = img->base + ph[i].p_vaddr;
    u8* segpg = reinterpret_cast<u8*>(trunc_page(reinterpret_cast<u64>(seg), pg));
    size_t seglen = round_page(reinterpret_cast<u64>(seg) + ph[i].p_memsz, pg)
                    - reinterpret_cast<u64>(segpg);
    if (logged_final_segments < kMaxLoggedSegments)
      report(img,
             "phase=segment-final-protection-enter index=%u addr=%p pages=%p+0x%zx prot=%c%c%c",
             i, seg, segpg, seglen, (prot & PROT_READ) ? 'r' : '-',
             (prot & PROT_WRITE) ? 'w' : '-', (prot & PROT_EXEC) ? 'x' : '-');
    if (mprotect(segpg, seglen, prot) != 0)
    {
      if (logged_final_segments < kMaxLoggedSegments)
        report(img, "phase=segment-final-protection-exit index=%u result=failed errno=%d (%s)", i,
               errno, std::strerror(errno));
      std::snprintf(err, errlen, "final mprotect failed: %s", std::strerror(errno));
      munmap(m, span); delete img; return nullptr;
    }
    if (logged_final_segments < kMaxLoggedSegments)
    {
      report(img, "phase=segment-final-protection-exit index=%u result=ok", i);
      ++logged_final_segments;
    }
  }
  if (load_segments > kMaxLoggedSegments)
    report(img, "phase=segment-final-protection-summary total=%u unlogged=%u", load_segments,
           load_segments - kMaxLoggedSegments);
  report(img, "phase=load-exit result=ok bias=%p map=%p span=0x%zx", img->base, img->map,
         img->span);
  {
    std::lock_guard<std::mutex> lock(g_images_mutex);
    img->next = g_images;
    g_images = img;
  }
  return img;
}

// A mapped add-on is invisible to the unwinder until registered. SDK libunwind's
// __register_frame accepts a single FDE, unlike libgcc's whole-section API.
#if defined(TARGET_PS5)
extern "C" void __unw_add_dynamic_eh_frame_section(const void*) __attribute__((weak));
extern "C" void __unw_remove_dynamic_eh_frame_section(const void*) __attribute__((weak));
#define PS5ELF_REGISTER_FRAME __unw_add_dynamic_eh_frame_section
#define PS5ELF_DEREGISTER_FRAME __unw_remove_dynamic_eh_frame_section
#else
extern "C" void __register_frame(const void*) __attribute__((weak));
extern "C" void __deregister_frame(const void*) __attribute__((weak));
#define PS5ELF_REGISTER_FRAME __register_frame
#define PS5ELF_DEREGISTER_FRAME __deregister_frame
#endif

namespace
{
// Decode the eh_frame_hdr's eh_frame_ptr (DW_EH_PE encoding in byte 1).
// ISA's toolchain emits pcrel|sdata4 (0x1b), the GNU default; handle that and
// the absolute encodings, and give up (no registration) on anything else.
u8* eh_frame_from_hdr(u8* hdr)
{
  if (!hdr) return nullptr;
  const u8 eh_frame_ptr_enc = hdr[1];
  const u8* p = hdr + 4; // version(1) + eh_frame_ptr_enc(1) + fde_count_enc(1) + table_enc(1)
  const u8 app = eh_frame_ptr_enc & 0x70;
  const u8 fmt = eh_frame_ptr_enc & 0x0f;
  long long val = 0;
  const u8* vp = p;
  switch (fmt)
  {
    case 0x03: { uint32_t v; std::memcpy(&v, vp, 4); val = v; break; } // udata4
    case 0x0b: { int32_t v; std::memcpy(&v, vp, 4); val = v; break; } // sdata4
    case 0x00: { uintptr_t v; std::memcpy(&v, vp, sizeof v); val = (long long)v; break; } // absptr
    default: return nullptr;
  }
  if (app == 0x10) // DW_EH_PE_pcrel: relative to the location of eh_frame_ptr
    return reinterpret_cast<u8*>(reinterpret_cast<uintptr_t>(p) + (intptr_t)val);
  if (app == 0x00) // absolute
    return reinterpret_cast<u8*>((uintptr_t)val);
  return nullptr;
}
} // namespace

void run_init(Image* img)
{
  if (!img || img->initialized || img->unloading) return;
  img->initialized = true;
  report(img, "phase=unwind-enter eh-frame-hdr=%p register-frame=%s",
         static_cast<void*>(img->eh_frame_hdr), PS5ELF_REGISTER_FRAME ? "available" : "absent");
  if (img->eh_frame_hdr && PS5ELF_REGISTER_FRAME && PS5ELF_DEREGISTER_FRAME &&
      !img->eh_registered)
  {
    if (u8* eh = eh_frame_from_hdr(img->eh_frame_hdr))
    {
      report(img, "phase=unwind-register-enter eh-frame=%p", static_cast<void*>(eh));
      PS5ELF_REGISTER_FRAME(eh);
      img->eh_registered = true;
      report(img, "phase=unwind-register-exit result=called eh-frame=%p", static_cast<void*>(eh));
    }
    else
      report(img, "phase=unwind-register-skip reason=unsupported-eh-frame-encoding");
  }
  else if (!img->eh_frame_hdr)
    report(img, "phase=unwind-register-skip reason=no-PT_GNU_EH_FRAME");
  else if (!PS5ELF_REGISTER_FRAME || !PS5ELF_DEREGISTER_FRAME)
    report(img, "phase=unwind-register-skip reason=registration-API-unavailable");
  report(img, "phase=unwind-exit registration-call=%s",
         img->eh_registered ? "completed" : "not-performed");

  constexpr size_t kMaxLoggedConstructors = 32;
  if (img->init)
  {
    report(img, "phase=ctor-entry kind=DT_INIT index=0 address=%p",
           reinterpret_cast<void*>(img->init));
    img->init();
    report(img, "phase=ctor-exit kind=DT_INIT index=0 address=%p",
           reinterpret_cast<void*>(img->init));
  }
  for (size_t i = 0; i < img->init_arrayn; ++i)
  {
    if (!img->init_array[i])
      continue;
    const bool log_constructor = i < kMaxLoggedConstructors;
    if (log_constructor)
      report(img, "phase=ctor-entry kind=DT_INIT_ARRAY index=%zu address=%p", i,
             reinterpret_cast<void*>(img->init_array[i]));
    img->init_array[i]();
    if (log_constructor)
      report(img, "phase=ctor-exit kind=DT_INIT_ARRAY index=%zu address=%p", i,
             reinterpret_cast<void*>(img->init_array[i]));
  }
  if (img->init_arrayn > kMaxLoggedConstructors)
    report(img, "phase=ctor-summary init-array=%zu only-first=%zu-logged", img->init_arrayn,
           kMaxLoggedConstructors);
  report(img, "phase=init-exit result=returned");
}

void* symbol(Image* img, const char* name)
{
  if (!img || !img->symtab || !img->strtab) return nullptr;
  for (u32 i = 0; i < img->symcount; ++i)
  {
    const Sym& s = img->symtab[i];
    if (s.st_shndx != SHN_UNDEF && std::strcmp(img->strtab + s.st_name, name) == 0)
      return img->base + s.st_value;
  }
  return nullptr;
}

Stats stats(const Image* img) { return img ? img->st : Stats{}; }

static void deregister_eh(Image* img)
{
  if (!img || !img->eh_registered)
    return;
  if (!PS5ELF_DEREGISTER_FRAME)
  {
    report(img, "phase=unwind-deregister-skip reason=removal-API-unavailable");
    return;
  }
  if (u8* eh = eh_frame_from_hdr(img->eh_frame_hdr))
  {
    report(img, "phase=unwind-deregister-enter eh-frame=%p", static_cast<void*>(eh));
    PS5ELF_DEREGISTER_FRAME(eh);
    img->eh_registered = false;
    report(img, "phase=unwind-deregister-exit result=called");
  }
  else
    report(img, "phase=unwind-deregister-skip reason=eh-frame-pointer-unavailable");
}

void unload(Image* img)
{
  if (!img || img->unloading)
    return;
  img->unloading = true;
  if (img)
    report(img, "phase=unload-enter bias=%p map=%p span=0x%zx unwind-registration-call=%s", img->base,
           img->map, img->span, img->eh_registered ? "completed" : "not-performed");
  if (img->initialized)
  {
    report(img, "phase=finalizers-enter fini-array=%zu", img->fini_arrayn);
    // Honor CRT finalizer order. A CRT may call __cxa_finalize at its own
    // position in the array; drain any registrations left by import-only DSOs.
    for (size_t i = img->fini_arrayn; i != 0; --i)
      if (img->fini_array[i - 1] &&
          reinterpret_cast<uintptr_t>(img->fini_array[i - 1]) != ~uintptr_t{0})
        img->fini_array[i - 1]();
    if (img->fini)
      img->fini();
    finalize(img, nullptr, true);
    report(img, "phase=finalizers-exit result=returned");
  }
  {
    std::lock_guard<std::mutex> lock(g_images_mutex);
    Image** link = &g_images;
    while (*link && *link != img)
      link = &(*link)->next;
    if (*link)
      *link = img->next;
  }
  deregister_eh(img);
  if (img->map)
  {
    const int result = munmap(img->map, img->span);
    report(img, "phase=unmap-exit result=%s errno=%d address=%p span=0x%zx",
           result == 0 ? "ok" : "failed", result == 0 ? 0 : errno, img->map, img->span);
  }
  delete img;
}

} // namespace ps5elf
