/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "PS5StorageUsage.h"

#include "utils/log.h"

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <memory>
#include <sqlite3.h>
#include <string_view>
#include <sys/ioctl.h>
#include <unistd.h>

namespace
{
struct SaveDataSizeQuery;
}

extern "C"
{
int sceSysmoduleLoadModule(uint16_t);
int sceSysmoduleLoadModuleInternal(uint32_t);
int sceKernelDebugOutText(int, const char*);
int sceAppInstUtilInitialize();
int sceAppInstUtilTerminate();
int sceAppInstUtilAppGetAllAppSize(int, uint64_t*);
int sceSaveDataInitialize3(const void*);
int sceSaveDataTerminate();
int sceSaveDataGetAllSizeProspero(const SaveDataSizeQuery*, uint64_t*);
int sceShellCoreUtilRequestEjectDevice(const char*);
int sceUsbStorageInit(const void*);
int sceUsbStorageTerm();
}

namespace sceAutoMounterIpcClient
{
int requestUnmount(const char*);
}

namespace
{
struct SaveDataSizeQuery
{
  int32_t platform{0}; // Both PS4 and PS5 saves.
  int32_t user{-1}; // All users.
  std::array<uint64_t, 4> reserved{};
};
static_assert(sizeof(SaveDataSizeQuery) == 40);

void GetCapacity(PS5InternalStorageUsage& usage)
{
  int fd;
  do
  {
    fd = open("/user", O_RDONLY | O_DIRECTORY);
  } while (fd == -1 && errno == EINTR);
  if (fd == -1)
  {
    CLog::Log(LOGERROR, "PS5 storage: open /user failed: errno {}", errno);
    return;
  }

  // sceFsBfsGetAvailableSize's 80-byte read-only ioctl and ShellCore's reserve calculation.
  std::array<uint64_t, 10> record{};
  int status;
  do
  {
    status = ioctl(fd, 0x4050bf10UL, record.data());
  } while (status == -1 && errno == EINTR);
  const int error = errno;
  if (close(fd) != 0)
    CLog::Log(LOGERROR, "PS5 storage: close /user failed: errno {}", errno);
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: BFS capacity query failed: status {}, errno {}", status,
              error);
    return;
  }
  if (record[0] == 0 || record[1] > record[0] ||
      record[5] > std::numeric_limits<uint64_t>::max() - record[6])
  {
    CLog::Log(LOGERROR, "PS5 storage: invalid BFS capacity record");
    return;
  }

  const uint64_t reserve = record[0] / 100;
  const uint64_t reclaimable = record[5] + record[6];
  const uint64_t extraFree = reclaimable > reserve ? reclaimable - reserve : 0;
  if (extraFree > record[0] - record[1])
  {
    CLog::Log(LOGERROR, "PS5 storage: BFS free space exceeds capacity");
    return;
  }
  usage.total = record[0];
  usage.free = record[1] + extraFree;
}

std::optional<uint64_t> GetGamesUsage()
{
  auto volatile initialize = &sceAppInstUtilInitialize;
  auto volatile terminate = &sceAppInstUtilTerminate;
  auto volatile query = &sceAppInstUtilAppGetAllAppSize;
  if (!initialize || !terminate || !query)
  {
    CLog::Log(LOGERROR, "PS5 storage: app-installer native imports are not bound");
    return std::nullopt;
  }
  int status = initialize();
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: app-installer initialization failed: {:#x}", status);
    return std::nullopt;
  }
  uint64_t bytes = 0;
  status = query(0, &bytes); // Storage selector 0 is internal; 1/2 select ext0/ext1.
  const int cleanup = terminate();
  if (cleanup != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: app-installer termination failed: {:#x}", cleanup);
  }
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: internal app-size query failed: {:#x}", status);
    return std::nullopt;
  }
  return bytes;
}

std::optional<uint64_t> GetSavedDataUsage()
{
  auto volatile initialize = &sceSaveDataInitialize3;
  auto volatile terminate = &sceSaveDataTerminate;
  auto volatile query = &sceSaveDataGetAllSizeProspero;
  if (!initialize || !terminate || !query)
  {
    CLog::Log(LOGERROR, "PS5 storage: saved-data native imports are not bound");
    return std::nullopt;
  }

  const SaveDataSizeQuery request{};
  uint64_t bytes = 0;
  int status = query(&request, &bytes);
  // Do not terminate a client initialized by another consumer in this process.
  if (static_cast<uint32_t>(status) == 0x809f8002)
  {
    status = initialize(nullptr);
    if (status != 0)
    {
      CLog::Log(LOGERROR, "PS5 storage: saved-data v3 initialization failed: {:#x}",
                static_cast<uint32_t>(status));
      return std::nullopt;
    }
    status = query(&request, &bytes);
    const int cleanup = terminate();
    if (cleanup != 0)
    {
      CLog::Log(LOGERROR, "PS5 storage: saved-data termination failed: {:#x}",
                static_cast<uint32_t>(cleanup));
    }
  }
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: saved-data size query failed: {:#x}",
              static_cast<uint32_t>(status));
    return std::nullopt;
  }
  return bytes;
}

std::optional<uint64_t> GetMediaUsage()
{
  constexpr const char* path = "/system_data/priv/mms/av_content.db";
  sqlite3* connection = nullptr;
  int status = sqlite3_open_v2(path, &connection, SQLITE_OPEN_READONLY, nullptr);
  const std::unique_ptr<sqlite3, decltype(&sqlite3_close)> database(connection, sqlite3_close);
  if (status != SQLITE_OK)
  {
    CLog::Log(LOGERROR, "PS5 storage: open media database failed: {}", status);
    return std::nullopt;
  }
  sqlite3_stmt* statement = nullptr;
  status = sqlite3_prepare_v2(connection,
                             "SELECT IFNULL(SUM(size_on_storage), 0) FROM tbl_PhotoVideo",
                             -1, &statement, nullptr);
  const std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> query(statement, sqlite3_finalize);
  if (status != SQLITE_OK || (status = sqlite3_step(statement)) != SQLITE_ROW)
  {
    CLog::Log(LOGERROR, "PS5 storage: media-size query failed: {} ({})", status,
              sqlite3_errmsg(connection));
    return std::nullopt;
  }
  const auto type = sqlite3_column_type(statement, 0);
  const auto bytes = sqlite3_column_int64(statement, 0);
  if (type != SQLITE_INTEGER || bytes < 0 ||
      sqlite3_step(statement) != SQLITE_DONE)
  {
    CLog::Log(LOGERROR, "PS5 storage: invalid media-size result ({})", sqlite3_errmsg(connection));
    return std::nullopt;
  }
  return static_cast<uint64_t>(bytes);
}

template<typename Id>
bool PreloadStorageModule(const char* name, int (*load)(Id), Id id)
{
  char message[128];
  if (!load)
  {
    std::snprintf(message, sizeof(message),
                  "[kodi-ps5] %s module preload: loader import is not bound\n", name);
    sceKernelDebugOutText(0, message);
    return false;
  }
  const int status = load(id);
  std::snprintf(message, sizeof(message), "[kodi-ps5] %s module preload: 0x%08x\n",
                name, static_cast<unsigned int>(status));
  sceKernelDebugOutText(0, message);
  return status == 0;
}
} // namespace

bool EjectPS5Disc(const char* device)
{
  if (std::strcmp(device, "/dev/cd0") != 0 && std::strcmp(device, "/dev/cd1") != 0 &&
      std::strcmp(device, "/dev/cd") != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: refusing to eject unsupported disc device {}", device);
    return false;
  }
  auto volatile eject = &sceShellCoreUtilRequestEjectDevice;
  if (!eject)
  {
    CLog::Log(LOGERROR, "PS5 storage: disc eject native import is not bound; refusing eject");
    return false;
  }
  const int status = eject(device);
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: disc eject request for {} failed: {:#x}", device,
              static_cast<uint32_t>(status));
    return false;
  }
  return true;
}

bool PreloadPS5UsbStorage()
{
  static const bool available = []
  {
    auto volatile load = &sceSysmoduleLoadModule;
    constexpr uint16_t USB_STORAGE_MODULE = 0x00d5;
    return PreloadStorageModule("USB storage", load, USB_STORAGE_MODULE);
  }();
  return available;
}

bool PreloadPS5InternalStorageModules()
{
  static const bool available = []
  {
    auto volatile load = &sceSysmoduleLoadModuleInternal;
    constexpr uint32_t SAVE_DATA_MODULE = 0x8000000f;
    constexpr uint32_t APP_INST_UTIL_MODULE = 0x80000014;
    const bool savedData = PreloadStorageModule("Saved data", load, SAVE_DATA_MODULE);
    const bool appInstaller = PreloadStorageModule("App installer", load, APP_INST_UTIL_MODULE);
    return savedData && appInstaller;
  }();
  return available;
}

bool UnmountPS5Storage(const char* device)
{
  const std::string_view path(device);
  std::string_view suffix = path.starts_with("/dev/da") ? path.substr(7) : std::string_view{};
  if (suffix.ends_with(".crypt"))
    suffix.remove_suffix(6);
  const auto partition = suffix.find_first_not_of("0123456789");
  if (suffix.empty() || suffix.front() < '0' || suffix.front() > '9' ||
      (partition != std::string_view::npos &&
       ((suffix[partition] != 's' && suffix[partition] != 'x') ||
        partition + 1 == suffix.size() ||
        suffix.find_first_not_of("0123456789", partition + 1) != std::string_view::npos)))
  {
    CLog::Log(LOGERROR, "PS5 storage: refusing to unmount unsupported device {}", device);
    return false;
  }
  // A title can start with an unresolved native import slot; read it before calling.
  auto volatile unmount = &sceAutoMounterIpcClient::requestUnmount;
  auto volatile initialize = &sceUsbStorageInit;
  auto volatile terminate = &sceUsbStorageTerm;
  if (!unmount || !initialize || !terminate)
  {
    CLog::Log(LOGERROR, "PS5 storage: USB native imports are not bound; refusing unmount");
    return false;
  }
  int status = unmount(device);
  if (static_cast<uint32_t>(status) == 0x80f40009)
  {
    status = initialize(nullptr);
    if (status != 0)
    {
      CLog::Log(LOGERROR, "PS5 storage: USB client initialization failed: {:#x}",
                static_cast<uint32_t>(status));
      return false;
    }
    status = unmount(device);
    const int cleanup = terminate();
    if (cleanup != 0)
    {
      CLog::Log(LOGERROR, "PS5 storage: USB client termination failed: {:#x}",
                static_cast<uint32_t>(cleanup));
    }
  }
  if (status != 0)
  {
    CLog::Log(LOGERROR, "PS5 storage: unmount request for {} failed: {:#x}", device,
              static_cast<uint32_t>(status));
    return false;
  }
  return true;
}

PS5InternalStorageUsage GetPS5InternalStorageUsage()
{
  PS5InternalStorageUsage usage;
  GetCapacity(usage);
  usage.games = GetGamesUsage();
  usage.media = GetMediaUsage();
  usage.savedData = GetSavedDataUsage();
  if (usage.total && usage.free && usage.games && usage.media && usage.savedData)
  {
    uint64_t remaining = *usage.total - *usage.free;
    for (const uint64_t category : {*usage.games, *usage.media, *usage.savedData})
    {
      if (category > remaining)
      {
        CLog::Log(LOGERROR, "PS5 storage: category sizes exceed used capacity; Other unavailable");
        return usage;
      }
      remaining -= category;
    }
    usage.other = remaining;
  }
  return usage;
}
