/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "PS5AddonLoader.h"

#include "cores/DllLoader/DllLoaderContainer.h"
#include "filesystem/SpecialProtocol.h"
#include "utils/log.h"

#include <cerrno>
#include <cstddef>
#include <sys/stat.h>

extern "C"
{
int sceKernelLoadStartModule(const char*, size_t, const void*, unsigned int, const void*, int*);
int sceKernelDlsym(int, const char*, void**);
int sceKernelStopUnloadModule(int, size_t, const void*, unsigned int, const void*, int*);
}

CPS5AddonLoader::CPS5AddonLoader(const std::string& library) : LibraryLoader(library)
{
  DllLoaderContainer::RegisterDll(this);
}

CPS5AddonLoader::~CPS5AddonLoader()
{
  if (!m_releaseFailed)
    Unload();
  DllLoaderContainer::UnRegisterDll(this);
}

bool CPS5AddonLoader::Load()
{
  if (m_releaseFailed)
  {
    CLog::Log(LOGERROR, "PS5 SPRX: {} is quarantined after failed module release", GetFileName());
    return false;
  }
  if (m_handle >= 0)
    return true;

  const std::string path = CSpecialProtocol::TranslatePath(GetFileName());
  if (path.empty() || path.front() != '/' || !path.ends_with(".sprx"))
  {
    CLog::Log(LOGERROR, "PS5 SPRX: expected an absolute native .sprx path, got {}", path);
    return false;
  }

  struct stat status{};
  if (stat(path.c_str(), &status) != 0)
  {
    CLog::Log(LOGERROR, "PS5 SPRX: stat failed path={} errno={}", path, errno);
    return false;
  }
  if (!S_ISREG(status.st_mode))
  {
    CLog::Log(LOGERROR, "PS5 SPRX: expected a regular module file, got {}", path);
    return false;
  }

  // Kodi's ZIP installation copies files without their executable permissions.
  constexpr mode_t executeBits = S_IXUSR | S_IXGRP | S_IXOTH;
  if ((status.st_mode & executeBits) != executeBits)
  {
    const mode_t mode = (status.st_mode & (S_IRWXU | S_IRWXG | S_IRWXO)) | executeBits;
    if (chmod(path.c_str(), mode) != 0)
    {
      CLog::Log(LOGERROR, "PS5 SPRX: cannot make module executable path={} mode={:o} errno={}",
                path, mode, errno);
      return false;
    }
    CLog::Log(LOGDEBUG, "PS5 SPRX: made module executable path={} mode={:o}", path, mode);
  }

  int startResult = 0;
  CLog::Log(LOGINFO, "PS5 SPRX: load-start path={}", path);
  m_handle = sceKernelLoadStartModule(path.c_str(), 0, nullptr, 0, nullptr, &startResult);
  CLog::Log(m_handle < 0 || startResult != 0 ? LOGERROR : LOGINFO,
            "PS5 SPRX: load-start path={} handle={:#x} start={:#x}", path, m_handle, startResult);
  if (m_handle < 0)
    return false;
  if (startResult != 0)
  {
    Unload();
    return false;
  }
  return true;
}

void CPS5AddonLoader::Unload()
{
  if (m_handle < 0 || m_releaseFailed)
    return;

  int stopResult = 0;
  const int result = sceKernelStopUnloadModule(m_handle, 0, nullptr, 0, nullptr, &stopResult);
  CLog::Log(result != 0 || stopResult != 0 ? LOGERROR : LOGINFO,
            "PS5 SPRX: stop-unload path={} handle={:#x} result={:#x} stop={:#x}", GetFileName(),
            m_handle, result, stopResult);
  if (result != 0 || stopResult != 0)
  {
    m_releaseFailed = true;
    return;
  }
  m_handle = -1;
}

int CPS5AddonLoader::ResolveExport(const char* symbol, void** ptr, bool logging)
{
  *ptr = nullptr;
  if (!Load())
    return 0;

  const int result = sceKernelDlsym(m_handle, symbol, ptr);
  if (result != 0 || !*ptr)
  {
    *ptr = nullptr;
    if (logging)
      CLog::Log(LOGERROR, "PS5 SPRX: dlsym path={} handle={:#x} symbol={} result={:#x}",
                GetFileName(), m_handle, symbol, result);
    return 0;
  }
  CLog::Log(LOGDEBUG, "PS5 SPRX: dlsym path={} symbol={} resolved", GetFileName(), symbol);
  return 1;
}

bool CPS5AddonLoader::IsSystemDll()
{
  return false;
}

HMODULE CPS5AddonLoader::GetHModule()
{
  return m_handle >= 0 ? &m_handle : nullptr;
}

bool CPS5AddonLoader::HasSymbols()
{
  return false;
}
