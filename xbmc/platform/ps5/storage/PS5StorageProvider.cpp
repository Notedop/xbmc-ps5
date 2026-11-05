/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PS5StorageProvider.h"
#include "PS5StorageUsage.h"

#include "MediaSource.h"
#include "ServiceBroker.h"
#include "platform/Filesystem.h"
#include "resources/LocalizeStrings.h"
#include "resources/ResourcesComponent.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <thread>
#include <utility>
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

void Add(std::vector<CMediaSource>& drives,
         const char* path,
         const char* name,
         SourceType type = SourceType::LOCAL)
{
  if (!Exists(path))
    return;
  CMediaSource share;
  share.strPath = path;
  share.strName = name;
  share.m_iDriveType = type;
  drives.push_back(share);
}

std::string GetStorageDevice(const struct statfs& mountInfo)
{
  std::string device(
      mountInfo.f_mntfromname, strnlen(mountInfo.f_mntfromname, sizeof(mountInfo.f_mntfromname)));
  if (device.starts_with("/dev/cd") &&
      device.size() > 7 && device[7] >= '0' && device[7] <= '9')
  {
    // Disc eject uses the physical drive; USB unmount uses the exact mount device.
    const auto suffix = device.find_first_not_of("0123456789", 7);
    if (suffix != std::string::npos)
      device.resize(suffix);
  }
  return device;
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
  m_ejected.clear();
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
                                  "/mnt/ext0", "/mnt/ext1", "/mnt/disc"};
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
      Add(removableDrives, path, path, SourceType::REMOVABLE);
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

bool CPS5StorageProvider::Eject(const std::string& mountpath)
{
  std::string path = mountpath;
  while (path.size() > 1 && path.back() == '/')
    path.pop_back();
  if (std::none_of(std::begin(kRemovable), std::end(kRemovable),
                   [&path](const char* removable) { return path == removable; }))
  {
    CLog::Log(LOGERROR, "CPS5StorageProvider: refusing to eject non-removable path {}", mountpath);
    return false;
  }

  struct statfs mountInfo{};
  int status;
  do
  {
    status = statfs(path.c_str(), &mountInfo);
  } while (status == -1 && errno == EINTR);
  if (status != 0)
  {
    const int error = errno;
    CLog::Log(LOGERROR, "CPS5StorageProvider: cannot eject {}: {} (errno {})", path,
              std::strerror(error), error);
    return false;
  }
  const std::string mountedOn(
      mountInfo.f_mntonname, strnlen(mountInfo.f_mntonname, sizeof(mountInfo.f_mntonname)));
  if (mountedOn != path)
  {
    CLog::Log(LOGERROR, "CPS5StorageProvider: refusing to eject {}: not a mounted volume", path);
    return false;
  }

  const std::string device = GetStorageDevice(mountInfo);
  if (path == "/mnt/disc")
    return EjectPS5Disc(device.c_str());

  if (!UnmountPS5Storage(device.c_str()))
    return false;

  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds{5};
  do
  {
    struct statfs current{};
    const int result = statfs(path.c_str(), &current);
    const int error = errno;
    if ((result == 0 &&
         (std::string(current.f_mntonname,
                      strnlen(current.f_mntonname, sizeof(current.f_mntonname))) != path ||
          current.f_fsid.val[0] != mountInfo.f_fsid.val[0] ||
          current.f_fsid.val[1] != mountInfo.f_fsid.val[1])) ||
        (result != 0 && (error == ENOENT || error == ENOTDIR)))
    {
      CLog::Log(LOGINFO, "CPS5StorageProvider: safely unmounted {}", path);
      m_ejected.insert(path);
      m_changed = true;
      return true;
    }
    if (result != 0 && error != EINTR)
    {
      CLog::Log(LOGERROR, "CPS5StorageProvider: cannot confirm unmount of {}: {} (errno {})",
                path, std::strerror(error), error);
      return false;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds{50});
  } while (std::chrono::steady_clock::now() < deadline);
  CLog::Log(LOGERROR, "CPS5StorageProvider: unmount of {} was not confirmed; do not unplug", path);
  return false;
}

bool CPS5StorageProvider::PumpDriveChangeEvents(IStorageEventsCallback* callback)
{
  if (!m_changed.exchange(false))
    return false;

  bool changed = false;
  // Deliver completed unmounts before additions, even if a drive was already reinserted.
  for (const auto& path : m_ejected)
  {
    m_removable.erase(path);
    if (callback)
      callback->OnStorageSafelyRemoved({path, path, MEDIA_DETECT::STORAGE::Type::UNKNOWN});
    changed = true;
  }
  m_ejected.clear();
  std::set<std::string> present = GetRemovablePaths();
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
      {
        if (path == "/mnt/disc")
          callback->OnStorageSafelyRemoved({path, path, MEDIA_DETECT::STORAGE::Type::UNKNOWN});
        else
          callback->OnStorageUnsafelyRemoved({path, path, MEDIA_DETECT::STORAGE::Type::UNKNOWN});
      }
      changed = true;
    }
  m_removable = present;
  return changed;
}

std::vector<std::string> CPS5StorageProvider::GetDiskUsage()
{
  const auto& localize = CServiceBroker::GetResourcesComponent().GetLocalizeStrings();
  const auto size = [&localize](const std::optional<uint64_t>& bytes)
  {
    if (!bytes)
      return localize.Get(10006);
    if (*bytes == 0)
      return std::string{"0 KB"};
    constexpr std::array<const char*, 7> units = {"B", "KB", "MB", "GB", "TB", "PB", "EB"};
    size_t unit = 0;
    double value = static_cast<double>(*bytes);
    while (value >= 1000.0 && unit + 1 < units.size())
    {
      value /= 1000.0;
      ++unit;
    }
    if (unit == 0)
      return StringUtils::Format("{} B", *bytes);
    return StringUtils::Format("{:.{}f} {}", value, value >= 100.0 ? 1 : 2, units[unit]);
  };
  const auto internal = GetPS5InternalStorageUsage();
  std::vector<std::string> result;
  result.push_back(StringUtils::Format(
      "{}: {} {}, {} {}", localize.Get(39220), localize.Get(20161), size(internal.total),
      localize.Get(160), size(internal.free)));
  for (const auto& [label, bytes] :
       {std::make_pair(39222, internal.games), std::make_pair(39223, internal.media),
        std::make_pair(39224, internal.savedData), std::make_pair(29921, internal.other)})
  {
    result.push_back(StringUtils::Format("[TABS]1[/TABS]{}: {} ({})", localize.Get(label), size(bytes),
                                        localize.Get(20162)));
  }

  std::vector<CMediaSource> drives;
  GetRemovableDrives(drives);
  std::set<std::pair<int32_t, int32_t>> filesystems;
  std::vector<std::string> external;
  std::vector<std::string> usb;
  std::vector<std::string> disc;
  for (const auto& drive : drives)
  {
    const auto& path = drive.strPath;
    struct statfs mountInfo{};
    int status;
    do
    {
      status = statfs(path.c_str(), &mountInfo);
    } while (status == -1 && errno == EINTR);
    if (status == -1)
    {
      const int error = errno;
      CLog::Log(LOGERROR, "CPS5StorageProvider: statfs failed for {}: {} (errno {})", path,
                std::strerror(error), error);
      continue;
    }

    const std::string mountedOn(
        mountInfo.f_mntonname, strnlen(mountInfo.f_mntonname, sizeof(mountInfo.f_mntonname)));
    if (mountedOn != path)
      continue;

    const auto filesystem = std::make_pair(mountInfo.f_fsid.val[0], mountInfo.f_fsid.val[1]);
    if (filesystems.count(filesystem))
      continue;
    filesystems.insert(filesystem);

    auto label = path.substr(5);
    StringUtils::ToUpper(label);
    auto& section = StringUtils::StartsWith(path, "/mnt/ext") ? external
                    : StringUtils::StartsWith(path, "/mnt/usb") ? usb : disc;
    std::error_code ec;
    const auto usage = KODI::PLATFORM::FILESYSTEM::space(path, ec);
    if (ec || usage.free > usage.capacity)
    {
      CLog::Log(LOGERROR,
                "CPS5StorageProvider: invalid/failed space query for {}: {}, capacity {}, free {}",
                path, ec.message(), usage.capacity, usage.free);
      section.push_back(StringUtils::Format("[TABS]1[/TABS]{}: {}", label, localize.Get(10006)));
      continue;
    }
    section.push_back(StringUtils::Format(
        "[TABS]1[/TABS]{}: {} {}, {} {}", label, localize.Get(20162),
        size(usage.capacity - usage.free), localize.Get(160), size(usage.free)));
  }
  for (const auto& [label, section] :
       {std::make_pair(39221, &external), std::make_pair(39225, &usb), std::make_pair(427, &disc)})
  {
    if (!section->empty())
    {
      result.push_back(localize.Get(label));
      result.insert(result.end(), section->begin(), section->end());
    }
  }
  return result;
}
