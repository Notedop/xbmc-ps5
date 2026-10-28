/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PS5StorageProvider.h"

#include "MediaSource.h"
#include "utils/log.h"

#include <cerrno>
#include <sys/event.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

std::unique_ptr<IStorageProvider> IStorageProvider::CreateInstance()
{
  return std::make_unique<CPS5StorageProvider>();
}

namespace
{
bool Exists(const char* path)
{
  struct stat st;
  return stat(path, &st) == 0;
}

void Add(std::vector<CMediaSource>& drives, const char* path, const char* name)
{
  if (!Exists(path))
    return;
  CMediaSource share;
  share.strPath = path;
  share.strName = name;
  share.m_iDriveType = SourceType::LOCAL;
  drives.push_back(share);
}
} // namespace

CPS5StorageProvider::CPS5StorageProvider() : CThread("PS5Storage")
{
}

CPS5StorageProvider::~CPS5StorageProvider()
{
  Stop();
}

void CPS5StorageProvider::Initialize()
{
  Stop();

  m_kqueue = kqueue();
  if (m_kqueue == -1)
  {
    CLog::Log(LOGERROR, "CPS5StorageProvider: kqueue failed: {} (errno {})", std::strerror(errno),
              errno);
    return;
  }

  int wakeup[2];
  if (pipe(wakeup) == -1)
  {
    CLog::Log(LOGERROR, "CPS5StorageProvider: shutdown pipe failed: {} (errno {})",
              std::strerror(errno), errno);
    Stop();
    return;
  }
  m_wakeupRead = wakeup[0];
  m_wakeupWrite = wakeup[1];

  struct kevent events[2];
  EV_SET(&events[0], 0, EVFILT_FS, EV_ADD | EV_ENABLE | EV_CLEAR, VQ_MOUNT | VQ_UNMOUNT | VQ_DEAD,
         0, nullptr);
  EV_SET(&events[1], m_wakeupRead, EVFILT_READ, EV_ADD | EV_ENABLE | EV_CLEAR, 0, 0, nullptr);
  int result;
  do
  {
    result = kevent(m_kqueue, events, 2, nullptr, 0, nullptr);
  } while (result == -1 && errno == EINTR);
  if (result == -1)
  {
    CLog::Log(LOGERROR,
              "CPS5StorageProvider: EVFILT_FS/shutdown registration failed: {} (errno {}); "
              "storage event monitoring disabled",
              std::strerror(errno), errno);
    Stop();
    return;
  }

  // Register before taking the baseline so changes during enumeration remain queued.
  m_removable = GetRemovablePaths();
  Create();
}

void CPS5StorageProvider::Stop()
{
  StopThread(false);
  if (m_wakeupWrite != -1)
  {
    // EOF on the read end interrupts kevent without closing a descriptor in use.
    close(m_wakeupWrite);
    m_wakeupWrite = -1;
  }
  StopThread();
  if (m_wakeupRead != -1)
  {
    close(m_wakeupRead);
    m_wakeupRead = -1;
  }
  if (m_kqueue != -1)
  {
    close(m_kqueue);
    m_kqueue = -1;
  }
  m_changed = false;
  m_removable.clear();
}

void CPS5StorageProvider::Process()
{
  while (!m_bStop)
  {
    struct kevent event;
    const int result = kevent(m_kqueue, nullptr, 0, &event, 1, nullptr);
    if (m_bStop)
      return;
    if (result == -1)
    {
      if (errno == EINTR)
        continue;
      CLog::Log(LOGERROR,
                "CPS5StorageProvider: kevent wait failed: {} (errno {}); "
                "storage event monitoring stopped",
                std::strerror(errno), errno);
      return;
    }
    if (result != 1)
    {
      CLog::Log(LOGERROR,
                "CPS5StorageProvider: unexpected kevent result {}; storage event monitoring stopped",
                result);
      return;
    }
    if (event.flags & EV_ERROR)
    {
      CLog::Log(LOGERROR,
                "CPS5StorageProvider: kevent error: {} (errno {}); storage event monitoring stopped",
                std::strerror(static_cast<int>(event.data)), event.data);
      return;
    }
    if (event.filter != EVFILT_FS || event.ident != 0 || (event.flags & EV_EOF))
    {
      CLog::Log(LOGERROR,
                "CPS5StorageProvider: unexpected kevent filter {}, ident {}, flags {}; "
                "storage event monitoring stopped",
                event.filter, event.ident, event.flags);
      return;
    }
    if (event.fflags & (VQ_MOUNT | VQ_UNMOUNT | VQ_DEAD))
      m_changed = true;
    else
      CLog::Log(LOGWARNING, "CPS5StorageProvider: unexpected EVFILT_FS flags {}", event.fflags);
  }
}

void CPS5StorageProvider::GetLocalDrives(std::vector<CMediaSource>& localDrives)
{
  Add(localDrives, "/download0", "Kodi data (/download0)");
  Add(localDrives, "/app0", "Application image (/app0)");
  // Present when the loader has jailbroken the process (etaHEN / lapy daemon).
  Add(localDrives, "/data", "System data (/data)");
  Add(localDrives, "/", "System data (/root)");
}

namespace
{
// USB drives and extended storage appear here once the sandbox is open
const char* const kRemovable[] = {"/mnt/usb0", "/mnt/usb1", "/mnt/usb2", "/mnt/usb3",
                                  "/mnt/usb4", "/mnt/usb5", "/mnt/usb6", "/mnt/usb7",
                                  "/mnt/ext0", "/mnt/ext1"};
}

void CPS5StorageProvider::GetRemovableDrives(std::vector<CMediaSource>& removableDrives)
{
  for (const char* path : kRemovable)
  {
    struct statfs mountInfo{};
    int result;
    do
    {
      result = statfs(path, &mountInfo);
    } while (result == -1 && errno == EINTR);
    if (result == -1)
    {
      const int error = errno;
      if (error != ENOENT && error != ENOTDIR)
        CLog::Log(LOGERROR, "CPS5StorageProvider: statfs failed for {}: {} (errno {})", path,
                  std::strerror(error), error);
      continue;
    }

    const std::string mountedOn(
        mountInfo.f_mntonname, strnlen(mountInfo.f_mntonname, sizeof(mountInfo.f_mntonname)));
    // An empty mount directory belongs to its parent filesystem, not a removable volume.
    if (mountedOn == path)
      Add(removableDrives, path, path);
  }
}

std::set<std::string> CPS5StorageProvider::GetRemovablePaths()
{
  std::vector<CMediaSource> drives;
  GetRemovableDrives(drives);
  std::set<std::string> present;
  for (const auto& drive : drives)
    present.insert(drive.strPath);
  return present;
}

bool CPS5StorageProvider::PumpDriveChangeEvents(IStorageEventsCallback* callback)
{
  if (!m_changed.exchange(false))
    return false;

  std::set<std::string> present = GetRemovablePaths();
  bool changed = false;
  for (const auto& path : present)
    if (!m_removable.count(path))
    {
      CLog::Log(LOGINFO, "CPS5StorageProvider: {} appeared", path);
      if (callback)
        callback->OnStorageAdded({path, path, MEDIA_DETECT::STORAGE::Type::UNKNOWN});
      changed = true;
    }
  for (const auto& path : m_removable)
    if (!present.count(path))
    {
      CLog::Log(LOGINFO, "CPS5StorageProvider: {} removed", path);
      if (callback)
        callback->OnStorageUnsafelyRemoved({path, path, MEDIA_DETECT::STORAGE::Type::UNKNOWN});
      changed = true;
    }
  m_removable = present;
  return changed;
}

std::vector<std::string> CPS5StorageProvider::GetDiskUsage()
{
  // TODO: Report statfs capacity/free space for USB mounts; verify onboard/M.2 SSD mount
  // mappings before labeling internal storage, and avoid counting the same filesystem twice.
  return {};
}
