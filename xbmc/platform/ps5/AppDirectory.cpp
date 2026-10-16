/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "AppDirectory.h"

#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstring>

#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/errno.h>
#include <unistd.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);

namespace
{
void Klogf(const char* format, ...) __attribute__((format(printf, 1, 2)));
void Klogf(const char* format, ...)
{
  char buffer[512];
  va_list args;
  va_start(args, format);
  std::vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  sceKernelDebugOutText(0, buffer);
}
} // namespace

namespace KODI::PLATFORM::PS5
{
AppDirectory::AppDirectory() noexcept
{
  m_fd = open("/app0", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (m_fd < 0)
    Klogf("[kodi-ps5] cannot open /app0: %s\n", std::strerror(errno));
  else if (fstat(m_fd, &m_identity) != 0)
  {
    Klogf("[kodi-ps5] cannot identify /app0: %s\n", std::strerror(errno));
    close(m_fd);
    m_fd = -1;
  }
}

AppDirectory::~AppDirectory()
{
  if (m_fd >= 0)
    close(m_fd);
}

bool AppDirectory::Resolve(std::string& title) const
{
  if (m_fd < 0)
    return false;
  DIR* sandbox = opendir("/mnt/sandbox");
  if (!sandbox)
  {
    Klogf("[kodi-ps5] cannot scan /mnt/sandbox: %s\n", std::strerror(errno));
    return false;
  }

  unsigned int matches = 0;
  int readError = 0;
  char matchedPath[PATH_MAX]{};
  for (;;)
  {
    errno = 0;
    const dirent* entry = readdir(sandbox);
    if (!entry)
    {
      readError = errno;
      break;
    }
    if (std::strcmp(entry->d_name, ".") == 0 || std::strcmp(entry->d_name, "..") == 0)
      continue;

    char path[PATH_MAX]{};
    const int length = std::snprintf(path, sizeof(path), "/mnt/sandbox/%s/app0", entry->d_name);
    if (length < 0 || static_cast<std::size_t>(length) >= sizeof(path))
    {
      Klogf("[kodi-ps5] sandbox candidate path too long\n");
      continue;
    }

    struct stat identity{};
    if (stat(path, &identity) != 0)
    {
      const int error = errno;
      if (error != ENOENT && error != ENOTDIR)
        Klogf("[kodi-ps5] sandbox stat failed: %s: %s\n", path, std::strerror(error));
      continue;
    }
    if (S_ISDIR(identity.st_mode) && identity.st_dev == m_identity.st_dev &&
        identity.st_ino == m_identity.st_ino)
    {
      ++matches;
      std::strcpy(matchedPath, path);
    }
  }
  closedir(sandbox);
  if (readError != 0)
  {
    Klogf("[kodi-ps5] sandbox readdir failed: %s\n", std::strerror(readError));
    return false;
  }
  if (matches != 1)
  {
    Klogf("[kodi-ps5] cannot resolve app directory: %u identity matches\n", matches);
    return false;
  }
  title = matchedPath;
  Klogf("[kodi-ps5] app directory: %s\n", title.c_str());
  return true;
}
} // namespace KODI::PLATFORM::PS5
