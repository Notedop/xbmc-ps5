#include "platform/ps5/elf/PS5AddonLoader.h"

#include "filesystem/File.h"
#include "filesystem/SpecialProtocol.h"
#include "platform/ps5/elf/ElfLoader.h"
#include "platform/ps5/elf/HostExports.h"
#include "utils/log.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
bool AddonDebugEnabled()
{
  const char* enabled = std::getenv("KODI_PS5_ADDON_DEBUG");
  return enabled && enabled[0] != '\0' && std::strcmp(enabled, "0") != 0;
}

void AddonDiagnostic(void*, const char* message)
{
  if (!AddonDebugEnabled())
    return;

  CLog::Log(LOGINFO, "PS5AddonDebug: {}", message);
  if (std::strncmp(message, "phase=ctor-entry", 16) == 0 ||
      std::strncmp(message, "phase=ctor-exit", 15) == 0)
  {
    char marker[560];
    std::snprintf(marker, sizeof(marker), "[PS5AddonDebug] %s\n", message);
    sceKernelDebugOutText(0, marker);
  }
}

uint64_t Fingerprint(const std::vector<uint8_t>& bytes)
{
  uint64_t hash = 14695981039346656037ull;
  for (uint8_t byte : bytes)
  {
    hash ^= byte;
    hash *= 1099511628211ull;
  }
  return hash;
}

// A packaging-generated manifest beside the primary authorizes exact helper
// basenames in that directory. The manifest is part of the signed app package,
// like the primary binary itself; absent manifests grant no helper scope.
bool RegisterAuxiliaryManifest(ps5elf::Image* image, const std::string& path,
                               char* error, size_t error_size)
{
  const std::string native = CSpecialProtocol::TranslatePath(path);
  const size_t slash = native.find_last_of('/');
  if (slash == std::string::npos || slash == 0)
    return true;
  const std::string manifest = native.substr(0, slash + 1) + ps5elf::kAuxiliaryManifestName;
  if (!XFILE::CFile::Exists(manifest))
    return true;
  XFILE::CFile file;
  std::vector<uint8_t> text;
  if (file.LoadFile(manifest, text) < 0)
  {
    std::snprintf(error, error_size, "cannot read %s", manifest.c_str());
    return false;
  }
  std::vector<std::string> helpers;
  if (!ps5elf::parse_auxiliary_manifest(reinterpret_cast<const char*>(text.data()), text.size(),
                                        native.substr(slash + 1).c_str(), helpers, error,
                                        error_size))
    return false;
  if (helpers.empty())
    return true;
  std::vector<const char*> names;
  for (const auto& helper : helpers)
    names.push_back(helper.c_str());
  CLog::Log(LOGINFO, "PS5AddonLoader: {} trusted helper(s) authorized for {}", names.size(),
            native);
  return ps5elf::register_auxiliaries(image, native.c_str(), names.data(), names.size(), error,
                                      error_size);
}
} // namespace

CPS5AddonLoader::CPS5AddonLoader(const std::string& so) : LibraryLoader(so)
{
}

CPS5AddonLoader::~CPS5AddonLoader()
{
  Unload();
}

bool CPS5AddonLoader::Load()
{
  if (m_loaded)
    return true;

  // Read the whole .so into memory; the ELF loader maps from a buffer (it must
  // write and mprotect its own pages - it cannot use the file's mapping).
  const std::string path = GetFileName();
  XFILE::CFile file;
  std::vector<uint8_t> buf;
  const bool debug = AddonDebugEnabled();
  if (debug)
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=file-read-enter path={}", path);
  const auto bytes_read = file.LoadFile(path, buf); // LoadFile returns bytes read, <0 on error
  if (bytes_read <= 0 || buf.empty())
  {
    if (debug)
      CLog::Log(LOGINFO, "PS5AddonDebug: phase=file-read-exit result=failed path={} bytes={} buffer-bytes={}",
                path, bytes_read, buf.size());
    CLog::Log(LOGERROR, "PS5AddonLoader: cannot read add-on binary {}", path);
    return false;
  }
  if (debug)
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=file-read-exit result=ok path={} bytes={} buffer-bytes={} fnv1a64={:016x}",
              path, bytes_read, buf.size(), Fingerprint(buf));

  char err[256] = {0};
  m_img = ps5elf::load(buf.data(), buf.size(), &host_export_resolver, nullptr, err, sizeof err,
                       debug ? &AddonDiagnostic : nullptr, nullptr);
  if (!m_img)
  {
    CLog::Log(LOGERROR, "PS5AddonLoader: failed to load {}: {}", path, err);
    return false;
  }
  if (!RegisterAuxiliaryManifest(m_img, path, err, sizeof(err)))
  {
    CLog::Log(LOGERROR, "PS5AddonLoader: cannot register trusted helpers for {}: {}", path, err);
    ps5elf::unload(m_img);
    m_img = nullptr;
    return false;
  }

  const ps5elf::Stats st = ps5elf::stats(m_img);
  // Run the add-on's constructors (C++ static initializers) before use.
  if (debug)
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=constructors-enter module={} image={}", path,
              static_cast<void*>(m_img));
  bool constructors_failed = false;
  try
  {
    ps5elf::run_init(m_img);
  }
  catch (...)
  {
    CLog::Log(LOGERROR, "PS5AddonLoader: constructors threw in {}", path);
    constructors_failed = true;
  }
  // Leave the catch first: the exception's destructor/typeinfo may live in
  // the add-on, so __cxa_end_catch must finish before its code is unmapped.
  if (constructors_failed)
  {
    ps5elf::unload(m_img);
    m_img = nullptr;
    return false;
  }
  m_loaded = true;
  if (debug)
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=constructors-exit module={} result=returned", path);
  CLog::Log(LOGDEBUG,
            "PS5AddonLoader: loaded {} (reloc: relative={} glob/64={} jmp={})",
            path, st.relative, st.glob_dat, st.jump_slot);
  return true;
}

void CPS5AddonLoader::Unload()
{
  if (m_img)
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO, "PS5AddonDebug: phase=release-enter module={} image={}", GetFileName(),
                static_cast<void*>(m_img));
    ps5elf::unload(m_img);
    m_img = nullptr;
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO, "PS5AddonDebug: phase=release-exit module={} result=returned", GetFileName());
  }
  m_loaded = false;
}

int CPS5AddonLoader::ResolveExport(const char* symbol, void** ptr, bool logging)
{
  if (AddonDebugEnabled())
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=export-lookup-enter module={} symbol={}", GetName(),
              symbol);
  if (!m_loaded && !Load())
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO,
                "PS5AddonDebug: phase=export-lookup-exit module={} symbol={} result=load-failed",
                GetName(), symbol);
    if (logging)
      CLog::Log(LOGWARNING, "PS5AddonLoader: cannot resolve {} in {}: not loaded", symbol,
                GetName());
    return 0;
  }
  void* s = ps5elf::symbol(m_img, symbol);
  if (!s)
  {
    if (AddonDebugEnabled())
      CLog::Log(LOGINFO, "PS5AddonDebug: phase=export-lookup-exit module={} symbol={} result=missing",
                GetName(), symbol);
    if (logging)
      CLog::Log(LOGWARNING, "PS5AddonLoader: unable to resolve {} in {}", symbol, GetName());
    return 0;
  }
  *ptr = s;
  if (AddonDebugEnabled())
    CLog::Log(LOGINFO, "PS5AddonDebug: phase=export-lookup-exit module={} symbol={} result=ok address={}",
              GetName(), symbol, s);
  return 1;
}

bool CPS5AddonLoader::IsSystemDll()
{
  return false;
}

HMODULE CPS5AddonLoader::GetHModule()
{
  return m_img;
}

bool CPS5AddonLoader::HasSymbols()
{
  return false;
}
