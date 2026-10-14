#include "platform/ps5/elf/HostExports.h"

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <elf.h>
#include <mutex>

#include "utils/log.h"

// The SDK's <link.h> does not define glibc's ElfW() macro, and _DYNAMIC
// may not be declared. The target is always ELF64; use the concrete types
// and declare _DYNAMIC ourselves (every PIE/-shared image defines it).
extern "C" Elf64_Dyn _DYNAMIC[];
extern "C" const unsigned char __ps5_addon_image_base[]
    __attribute__((weak, visibility("hidden")));
extern "C" const unsigned char __ps5_addon_image_end[]
    __attribute__((weak, visibility("hidden")));
extern "C" const unsigned char __ps5_addon_dynsym_end[]
    __attribute__((weak, visibility("hidden")));

// The SDK's <elf.h> spells the PLT relocation R_X86_64_JUMP_SLO (no trailing T)
// and GLOB_DAT may vary too; pin both to their ABI values regardless.
#ifndef R_X86_64_JUMP_SLOT
#define R_X86_64_JUMP_SLOT 7
#endif
#ifndef R_X86_64_GLOB_DAT
#define R_X86_64_GLOB_DAT 6
#endif

// Imported runtime functions resolve through the title's relocated GOT.
// Packaging preserves the original LLVM metadata, including plaintext names;
// the converter's separate Sony tables are used by the system loader.
// Packaging publishes linked runtime definitions in the preserved LLVM dynsym,
// independently of the converter's import-only Sony symbol table.

namespace
{
struct Self
{
  const Elf64_Sym* symtab = nullptr;
  const char* strtab = nullptr;
  size_t strsz = 0;
  size_t sym_n = 0;
  const Elf64_Rela* rela = nullptr;   // DT_RELA (.rela.dyn)
  size_t rela_n = 0;
  const Elf64_Rela* jmprel = nullptr; // DT_JMPREL (.rela.plt)
  size_t jmprel_n = 0;
  uintptr_t base = 0;
  uintptr_t end = 0;
};
Self g;
std::once_flag g_init_once;

class Sha1
{
public:
  void Update(const uint8_t* data, size_t length)
  {
    m_total_bytes += length;
    while (length != 0)
    {
      const size_t take = (length < sizeof(m_block) - m_used) ? length : sizeof(m_block) - m_used;
      std::memcpy(m_block + m_used, data, take);
      m_used += take;
      data += take;
      length -= take;
      if (m_used == sizeof(m_block))
      {
        Transform(m_block);
        m_used = 0;
      }
    }
  }

  void Final(uint8_t digest[20])
  {
    const uint64_t bit_length = static_cast<uint64_t>(m_total_bytes) * 8;
    m_block[m_used++] = 0x80;
    if (m_used > 56)
    {
      std::memset(m_block + m_used, 0, sizeof(m_block) - m_used);
      Transform(m_block);
      m_used = 0;
    }
    std::memset(m_block + m_used, 0, 56 - m_used);
    for (unsigned i = 0; i < 8; ++i)
      m_block[63 - i] = static_cast<uint8_t>(bit_length >> (i * 8));
    Transform(m_block);

    for (unsigned i = 0; i < 5; ++i)
    {
      digest[i * 4] = static_cast<uint8_t>(m_state[i] >> 24);
      digest[i * 4 + 1] = static_cast<uint8_t>(m_state[i] >> 16);
      digest[i * 4 + 2] = static_cast<uint8_t>(m_state[i] >> 8);
      digest[i * 4 + 3] = static_cast<uint8_t>(m_state[i]);
    }
  }

private:
  static uint32_t RotateLeft(uint32_t value, unsigned count)
  {
    return (value << count) | (value >> (32 - count));
  }

  void Transform(const uint8_t block[64])
  {
    uint32_t words[80];
    for (unsigned i = 0; i < 16; ++i)
      words[i] = (static_cast<uint32_t>(block[i * 4]) << 24) |
                 (static_cast<uint32_t>(block[i * 4 + 1]) << 16) |
                 (static_cast<uint32_t>(block[i * 4 + 2]) << 8) | block[i * 4 + 3];
    for (unsigned i = 16; i < 80; ++i)
      words[i] = RotateLeft(words[i - 3] ^ words[i - 8] ^ words[i - 14] ^ words[i - 16], 1);

    uint32_t a = m_state[0];
    uint32_t b = m_state[1];
    uint32_t c = m_state[2];
    uint32_t d = m_state[3];
    uint32_t e = m_state[4];
    for (unsigned i = 0; i < 80; ++i)
    {
      uint32_t f;
      uint32_t k;
      if (i < 20)
      {
        f = (b & c) | (~b & d);
        k = 0x5a827999;
      }
      else if (i < 40)
      {
        f = b ^ c ^ d;
        k = 0x6ed9eba1;
      }
      else if (i < 60)
      {
        f = (b & c) | (b & d) | (c & d);
        k = 0x8f1bbcdc;
      }
      else
      {
        f = b ^ c ^ d;
        k = 0xca62c1d6;
      }
      const uint32_t temp = RotateLeft(a, 5) + f + e + k + words[i];
      e = d;
      d = c;
      c = RotateLeft(b, 30);
      b = a;
      a = temp;
    }
    m_state[0] += a;
    m_state[1] += b;
    m_state[2] += c;
    m_state[3] += d;
    m_state[4] += e;
  }

  uint32_t m_state[5]{0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0};
  uint8_t m_block[64]{};
  size_t m_used = 0;
  size_t m_total_bytes = 0;
};

// Match SDK prospero-nid: SHA-1(symbol + salt), reversed first 64 bits, then
// the first 11 characters of its Sony-base64 encoding.
void EncodeNid(const char* symbol, char nid[12])
{
  static constexpr uint8_t kSalt[] = {0x51, 0x8d, 0x64, 0xa6, 0x35, 0xde, 0xd8, 0xc1,
                                      0xe6, 0xb0, 0x39, 0xb1, 0xc3, 0xe5, 0x52, 0x30};
  static constexpr char kAlphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-";

  Sha1 hash;
  size_t symbol_length = 0;
  while (symbol[symbol_length] != '\0')
    ++symbol_length;
  hash.Update(reinterpret_cast<const uint8_t*>(symbol), symbol_length);
  hash.Update(kSalt, sizeof(kSalt));
  uint8_t digest[20];
  hash.Final(digest);

  uint8_t encoded[16]{};
  for (unsigned i = 0; i < 8; ++i)
    encoded[i] = digest[7 - i];
  char base64[16];
  size_t output = 0;
  for (unsigned i = 0; i < 11; i += 3)
  {
    const uint32_t triplet = (static_cast<uint32_t>(encoded[i]) << 16) |
                             (static_cast<uint32_t>(encoded[i + 1]) << 8) | encoded[i + 2];
    base64[output++] = kAlphabet[(triplet >> 18) & 0x3f];
    base64[output++] = kAlphabet[(triplet >> 12) & 0x3f];
    base64[output++] = kAlphabet[(triplet >> 6) & 0x3f];
    base64[output++] = kAlphabet[triplet & 0x3f];
  }
  std::memcpy(nid, base64, 11);
  nid[11] = '\0';
}

bool AddonDebugEnabled()
{
  const char* enabled = std::getenv("KODI_PS5_ADDON_DEBUG");
  return enabled && enabled[0] != '\0' && std::strcmp(enabled, "0") != 0;
}

void init()
{
  const bool debug = AddonDebugEnabled();

  const uintptr_t dyn_run = reinterpret_cast<uintptr_t>(&_DYNAMIC[0]);
  if (debug)
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=host-resolver-enter dynamic={}",
              reinterpret_cast<const void*>(dyn_run));
  // The native title maps .text at virtual address zero, not the ELF file
  // header. Packaging supplies these hidden symbols without adding a data object.
  const uintptr_t imgbase = reinterpret_cast<uintptr_t>(__ps5_addon_image_base);
  const uintptr_t imgend = reinterpret_cast<uintptr_t>(__ps5_addon_image_end);
  if (!imgbase || imgend <= imgbase || dyn_run < imgbase || dyn_run >= imgend)
  {
    CLog::Log(LOGERROR,
              "PS5AddonLoader: host image boundaries unavailable; rebuild the packaged title");
    return;
  }
  const uintptr_t bias = imgbase;
  g.base = bias;
  g.end = imgend;

  // _DYNAMIC is the original LLVM PIE metadata retained in the title's data
  // segment, not the converter's replacement dynamic section. Its pointers
  // remain link-time virtual addresses, even when they overlap the runtime span.
  auto fix = [&](uintptr_t p) -> uintptr_t {
    return bias + p;
  };
  size_t relasz = 0, relaent = sizeof(Elf64_Rela);
  size_t pltrelsz = 0;
  bool terminated = false;
  const size_t dynamic_limit = (imgend - dyn_run) / sizeof(Elf64_Dyn);
  for (size_t i = 0; i < dynamic_limit; ++i)
  {
    const Elf64_Dyn* d = &_DYNAMIC[i];
    if (d->d_tag == DT_NULL)
    {
      terminated = true;
      break;
    }
    switch (d->d_tag)
    {
      case DT_SYMTAB:   g.symtab = reinterpret_cast<const Elf64_Sym*>(fix(d->d_un.d_ptr)); break;
      case DT_STRTAB:   g.strtab = reinterpret_cast<const char*>(fix(d->d_un.d_ptr)); break;
      case DT_STRSZ:    g.strsz = d->d_un.d_val; break;
      case DT_RELA:     g.rela = reinterpret_cast<const Elf64_Rela*>(fix(d->d_un.d_ptr)); break;
      case DT_RELASZ:   relasz = d->d_un.d_val; break;
      case DT_RELAENT:  relaent = d->d_un.d_val; break;
      case DT_JMPREL:   g.jmprel = reinterpret_cast<const Elf64_Rela*>(fix(d->d_un.d_ptr)); break;
      case DT_PLTRELSZ: pltrelsz = d->d_un.d_val; break;
      default: break;
    }
  }
  if (relaent == 0)
    relaent = sizeof(Elf64_Rela);
  g.rela_n = relasz / relaent;
  g.jmprel_n = pltrelsz / relaent;
  const uintptr_t symtab = reinterpret_cast<uintptr_t>(g.symtab);
  const uintptr_t symend = reinterpret_cast<uintptr_t>(__ps5_addon_dynsym_end);
  if (symtab >= imgbase && symend > symtab && symend <= imgend &&
      (symend - symtab) % sizeof(Elf64_Sym) == 0)
    g.sym_n = (symend - symtab) / sizeof(Elf64_Sym);
  auto inside = [&](const void* pointer, size_t bytes) {
    const uintptr_t address = reinterpret_cast<uintptr_t>(pointer);
    return address >= imgbase && address < imgend && bytes <= imgend - address;
  };
  if (!terminated || relaent != sizeof(Elf64_Rela) || relasz % relaent != 0 ||
      pltrelsz % relaent != 0 || !g.sym_n || !g.strsz ||
      !inside(g.strtab, g.strsz) || (relasz && !inside(g.rela, relasz)) ||
      (pltrelsz && !inside(g.jmprel, pltrelsz)))
  {
    g.symtab = nullptr;
    g.strtab = nullptr;
    g.rela_n = 0;
    g.jmprel_n = 0;
    CLog::Log(LOGERROR,
              "PS5AddonLoader: original host dynamic metadata missing or invalid; repackage the title");
    return;
  }

  if (debug)
  {
    size_t candidate_got = 0;
    size_t populated_got = 0;
    auto count_got = [&](const Elf64_Rela* entries, size_t count) {
      for (size_t i = 0; i < count; ++i)
      {
        const uint32_t type = ELF64_R_TYPE(entries[i].r_info);
        if (type != R_X86_64_GLOB_DAT && type != R_X86_64_JUMP_SLOT)
          continue;
        ++candidate_got;
        if (!inside(reinterpret_cast<const void*>(g.base + entries[i].r_offset), sizeof(void*)))
          continue;
        void** slot = reinterpret_cast<void**>(g.base + entries[i].r_offset);
        if (*slot)
          ++populated_got;
      }
    };
    count_got(g.rela, g.rela_n);
    count_got(g.jmprel, g.jmprel_n);
    CLog::Log(LOGINFO,
              "PS5AddonDebug: phase=host-resolver-exit result={} eboot={:#x} bias={:#x} dynamic={:#x} symtab={:#x} symcount={} strtab={:#x} strsz={} rela={} jmprel={} got-candidates={} got-populated={}",
              g.symtab && g.strtab && g.sym_n && g.strsz ? "ready" : "dynamic-tables-missing",
              imgbase, bias, dyn_run, reinterpret_cast<uintptr_t>(g.symtab), g.sym_n,
              reinterpret_cast<uintptr_t>(g.strtab), g.strsz, g.rela_n, g.jmprel_n,
              candidate_got, populated_got);
  }
}

bool names_symbol(const char* host_name, size_t host_name_length, const char* name,
                  const char* nid)
{
  if (std::strcmp(host_name, name) == 0)
    return true;
  if (host_name_length < 11 || std::memcmp(host_name, nid, 11) != 0)
    return false;
  // Prospero adds symbol metadata after the 11-character SDK NID.
  return host_name_length == 11 || host_name[11] == '#';
}

void* from_definition(const char* name)
{
  void* weak = nullptr;
  for (size_t i = 1; i < g.sym_n; ++i)
  {
    const Elf64_Sym& symbol = g.symtab[i];
    const unsigned binding = ELF64_ST_BIND(symbol.st_info);
    const unsigned type = ELF64_ST_TYPE(symbol.st_info);
    const unsigned visibility = symbol.st_other & 3;
    if (symbol.st_shndx == SHN_UNDEF || symbol.st_shndx >= SHN_LORESERVE ||
        (binding != STB_GLOBAL && binding != STB_WEAK) ||
        (visibility != STV_DEFAULT && visibility != STV_PROTECTED) ||
        (type != STT_FUNC && type != STT_OBJECT && type != STT_NOTYPE) ||
        symbol.st_name >= g.strsz || symbol.st_value >= g.end - g.base ||
        symbol.st_size > g.end - g.base - symbol.st_value)
      continue;
    const char* candidate = g.strtab + symbol.st_name;
    if (!std::memchr(candidate, '\0', g.strsz - symbol.st_name) ||
        std::strcmp(candidate, name) != 0)
      continue;
    void* address = reinterpret_cast<void*>(g.base + symbol.st_value);
    if (binding == STB_GLOBAL)
      return address;
    weak = address;
  }
  return weak;
}

// If this relocation names an import (plain or SDK-NID encoded), return the
// resolved GOT/import-anchor slot content, not the slot's address.
void* from_rela(const Elf64_Rela* r, const char* name, const char* nid)
{
  const uint32_t type = ELF64_R_TYPE(r->r_info);
  if (type != R_X86_64_GLOB_DAT && type != R_X86_64_JUMP_SLOT &&
      !(type == R_X86_64_64 && r->r_addend == 0))
    return nullptr;
  const uint32_t sym = ELF64_R_SYM(r->r_info);
  if (sym >= g.sym_n)
    return nullptr;
  if (g.symtab[sym].st_shndx != SHN_UNDEF)
    return nullptr;
  const size_t name_offset = g.symtab[sym].st_name;
  if (name_offset >= g.strsz)
    return nullptr;
  const char* host_name = g.strtab + name_offset;
  const size_t remaining = g.strsz - name_offset;
  const char* terminator = static_cast<const char*>(std::memchr(host_name, '\0', remaining));
  if (!terminator ||
      !names_symbol(host_name, static_cast<size_t>(terminator - host_name), name, nid))
    return nullptr;
  if (r->r_offset >= g.end - g.base ||
      sizeof(void*) > g.end - g.base - r->r_offset)
    return nullptr;
  void** slot = reinterpret_cast<void**>(g.base + r->r_offset);
  void* address = *slot; // the system loader filled this with the provider's address
  if (address && AddonDebugEnabled())
    CLog::Log(LOGINFO,
              "PS5AddonDebug: phase=host-resolver-match symbol={} nid={} host-name={} relocation-type={} relocation-offset={:#x} address={}",
              name, nid, host_name, type, static_cast<uintptr_t>(r->r_offset), address);
  return address;
}
} // namespace

void* host_export_resolver(const char* name, void*)
{
  // A diagnosed initialization failure is also final: keep tables unavailable
  // and emit its error once, rather than retrying against immutable metadata.
  std::call_once(g_init_once, init);
  if (!g.symtab || !g.strtab || !g.sym_n || !g.strsz)
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO,
                "PS5AddonDebug: phase=host-resolver-lookup symbol={} result=resolver-tables-unavailable",
                name);
    return nullptr;
  }
  char nid[12];
  EncodeNid(name, nid);
  // The selected add-on must use the same allocator and POSIX wrappers as
  // Kodi, not the underlying libc GOT entry bypassing title-owned policy.
  char wrapped[256];
  std::snprintf(wrapped, sizeof(wrapped), "__wrap_%s", name);
  if (void* address = from_definition(wrapped))
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO,
                "PS5AddonDebug: phase=host-resolver-wrapped symbol={} wrapper={} address={}",
                name, wrapped, address);
    return address;
  }
  if (void* address = from_definition(name))
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO, "PS5AddonDebug: phase=host-resolver-defined symbol={} address={}",
                name, address);
    return address;
  }
  // Prefer eagerly populated .rela.dyn slots, then JUMP_SLOT.
  for (size_t i = 0; i < g.rela_n; ++i)
    if (void* a = from_rela(&g.rela[i], name, nid))
      return a;
  for (size_t i = 0; i < g.jmprel_n; ++i)
    if (void* a = from_rela(&g.jmprel[i], name, nid))
      return a;
  if (AddonDebugEnabled())
    CLog::Log(LOGINFO,
              "PS5AddonDebug: phase=host-resolver-lookup symbol={} nid={} result=unresolved-in-host-runtime",
              name, nid);
  return nullptr;
}
