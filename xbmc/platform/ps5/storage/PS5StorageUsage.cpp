/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "PS5StorageUsage.h"

#include "utils/log.h"

#include <array>
#include <cerrno>
#include <cstddef>
#include <fcntl.h>
#include <limits>
#include <memory>
#include <sqlite3.h>
#include <sys/ioctl.h>
#include <unistd.h>

extern "C"
{
int sceKernelLoadStartModule(const char*, size_t, const void*, unsigned int, const void*, int*);
int sceKernelDlsym(int, const char*, void**);
int sceKernelStopUnloadModule(int, size_t, const void*, unsigned int, const void*, int*);
}

namespace
{
class StorageModule
{
public:
  explicit StorageModule(const char* path) : m_path(path)
  {
    int start = 0;
    m_handle = sceKernelLoadStartModule(path, 0, nullptr, 0, nullptr, &start);
    if (m_handle < 0 || start != 0)
    {
      CLog::Log(LOGERROR, "PS5 storage: load {} failed: handle={:#x}, start={:#x}", path,
                m_handle, start);
      m_started = false;
    }
  }

  ~StorageModule()
  {
    if (m_handle < 0 || !m_release)
      return;
    int stop = 0;
    const int status = sceKernelStopUnloadModule(m_handle, 0, nullptr, 0, nullptr, &stop);
    if (status != 0 || stop != 0)
      CLog::Log(LOGERROR, "PS5 storage: unload {} failed: status={:#x}, stop={:#x}", m_path,
                status, stop);
  }

  StorageModule(const StorageModule&) = delete;
  StorageModule& operator=(const StorageModule&) = delete;

  template<typename T>
  T Resolve(const char* name)
  {
    if (m_handle < 0 || !m_started)
      return nullptr;
    void* address = nullptr;
    const int status = sceKernelDlsym(m_handle, name, &address);
    if (status != 0 || !address)
    {
      CLog::Log(LOGERROR, "PS5 storage: resolve {} in {} failed: {:#x}", name, m_path, status);
      return nullptr;
    }
    return reinterpret_cast<T>(address);
  }

  void Retain() { m_release = false; }

private:
  const char* m_path;
  int m_handle{-1};
  bool m_started{true};
  bool m_release{true};
};

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
  StorageModule module("/system/common/lib/libSceAppInstUtil.sprx");
  const auto initialize = module.Resolve<int (*)()>("sceAppInstUtilInitialize");
  const auto terminate = module.Resolve<int (*)()>("sceAppInstUtilTerminate");
  const auto query =
      module.Resolve<int (*)(int, uint64_t*)>("sceAppInstUtilAppGetAllAppSize");
  if (!initialize || !terminate || !query)
    return std::nullopt;
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
    module.Retain();
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
  struct Query
  {
    int32_t platform{0}; // Both PS4 and PS5 saves.
    int32_t user{-1}; // All users.
    std::array<uint64_t, 4> reserved{};
  };
  static_assert(sizeof(Query) == 40);

  StorageModule module("/system/common/lib/libSceSaveData.sprx");
  const auto initialize = module.Resolve<int (*)(const void*)>("sceSaveDataInitialize3");
  const auto terminate = module.Resolve<int (*)()>("sceSaveDataTerminate");
  const auto query =
      module.Resolve<int (*)(const Query*, uint64_t*)>("sceSaveDataGetAllSizeProspero");
  if (!initialize || !terminate || !query)
    return std::nullopt;

  const Query request{};
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
      module.Retain();
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
} // namespace

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
