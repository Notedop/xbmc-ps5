/*
 *  Minimal in-process loader for ELF64 shared objects (x86-64), the memory +
 *  relocation core for binary add-ons on PS5. It maps an add-on's PT_LOAD
 *  segments writable, applies relocations, resolves undefined symbols through
 *  a host callback, flips code pages to execute-only-after-write (the W^X
 *  route the on-console probe confirmed: map RW, write, mprotect RX), then
 *  runs the object's constructors. No dependency on a system dynamic loader,
 *  which a PS5 title does not have.
 *
 *  This header is deliberately free of <elf.h>: the same source builds on the
 *  Linux host (for tests) and the PS5 sysroot, whose elf headers differ.
 */
#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace ps5elf
{

// Resolve an undefined symbol by name to its address in the host (the running
// Kodi). Returns nullptr if unknown; the loader treats that as an error unless
// the reference is weak. `user` is passed through from elf_load.
using HostResolver = void* (*)(const char* name, void* user);
using Diagnostic = void (*)(void* user, const char* message);

struct Image; // opaque

// Load a shared object from an in-memory image. Returns nullptr on failure and
// writes a reason into errbuf. Does not run constructors (call run_init).
Image* load(const void* image, size_t image_len, HostResolver resolver, void* user,
            char* errbuf, size_t errlen, Diagnostic diagnostic = nullptr,
            void* diagnostic_user = nullptr);

// Run DT_INIT then DT_INIT_ARRAY (C++ static constructors). Call once, after
// load, before using the object.
void run_init(Image* img);

// Address of an exported symbol by name, or nullptr.
void* symbol(Image* img, const char* name);

// Opt-in for trusted, ordinary helper DSOs shipped next to this primary image.
// Call before run_init. Names must be exact basenames, not paths. Only calls
// bound to this primary image can open helpers in the resulting scope.
// Helpers cannot open further helpers (no dependency graph is implemented).
// The owner directory is pinned with a descriptor; symlinks outside it are
// rejected. Dependency loading (DT_NEEDED), ELF TLS and RTLD_GLOBAL remain
// unsupported. Scope operations are serialized and reentrant on one thread;
// trusted helper initializers/finalizers must not join a thread performing
// another loader operation. Last close has the same quiescence requirement
// as unload(). Repeated opens share a helper only within this owner.
bool register_auxiliaries(Image* img, const char* primary_path,
                          const char* const* names, size_t count,
                          char* errbuf, size_t errlen);

// Name of the packaging-generated helper allowlist beside a primary image.
inline constexpr const char* kAuxiliaryManifestName = "ps5-auxiliaries.list";

// Parse a packaged auxiliary manifest. The format is line based:
//   format 1
//   primary <primary basename>
//   helper <helper basename>   (zero or more)
// Blank lines and lines starting with '#' are ignored. Any other content,
// a primary mismatch, duplicates or invalid basenames reject the manifest.
bool parse_auxiliary_manifest(const char* text, size_t size, const char* primary_name,
                              std::vector<std::string>& helpers,
                              char* errbuf, size_t errlen);

// Diagnostics: counts of relocation kinds applied (for the test harness/logs).
struct Stats
{
  unsigned relative;
  unsigned glob_dat;   // GLOB_DAT + R_X86_64_64
  unsigned jump_slot;
  unsigned tls;        // rejected TLS relocations (zero on a successful load)
  unsigned other;      // rejected relocation types (zero on a successful load)
};
Stats stats(const Image* img);

// Caller must first destroy Kodi instances and quiesce all add-on threads and
// callbacks. Runs DT_FINI_ARRAY in reverse order, DT_FINI and remaining
// module-owned __cxa_atexit registrations while unwind data is still live.
// atexit and thread-local destructor imports are rejected until safely scoped.
void unload(Image* img);

} // namespace ps5elf
