#!/usr/bin/env python3
"""Linux host checks; requires C++, fmt/SQLite headers and libsqlite3.so.0."""

import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
STORAGE = ROOT / "xbmc/platform/ps5/storage"
LOGGER = """
#pragma once
constexpr int LOGERROR = 1;
inline int errors = 0;
struct CLog {
  template<class... T> static void Log(int, const char*, const T&...) { ++errors; }
};
"""

ACCOUNTING = r"""
#include "PS5StorageUsage.h"
#include "utils/log.h"
#include <array>
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <sqlite3.h>
#include <string>

std::array<uint64_t, 10> record;
std::string databasePath;
int openError, ioctlError, gamesError, saveError, initializeError, saveInitializeError, terminateError;
int loadError, resolveError, openInterrupts, ioctlInterrupts;
int closes, appInitializes, appTerminates, saveInitializes, saveTerminates, unloads;
bool savedInitialized;
uint64_t gamesBytes, savedBytes;
extern "C" int __real_sqlite3_open_v2(const char*, sqlite3**, int, const char*);
extern "C" int __wrap_sqlite3_open_v2(const char* path, sqlite3** db, int flags, const char* vfs)
{
  assert(std::string(path) == "/system_data/priv/mms/av_content.db");
  assert(flags == SQLITE_OPEN_READONLY);
  return __real_sqlite3_open_v2(databasePath.c_str(), db, flags, vfs);
}
extern "C" int __real_open(const char*, int, ...);
extern "C" int __wrap_open(const char* path, int flags, ...)
{
  if (std::string(path) != "/user")
    return __real_open(path, flags);
  assert(flags == (O_RDONLY | O_DIRECTORY));
  if (openInterrupts-- > 0) { errno = EINTR; return -1; }
  if (openError) { errno = openError; return -1; }
  return 1001;
}
extern "C" int __wrap_ioctl(int fd, unsigned long request, ...)
{
  assert(fd == 1001 && request == 0x4050bf10UL);
  if (ioctlInterrupts-- > 0) { errno = EINTR; return -1; }
  if (ioctlError) { errno = ioctlError; return -1; }
  va_list args;
  va_start(args, request);
  std::memcpy(va_arg(args, void*), record.data(), sizeof(record));
  va_end(args);
  return 0;
}
extern "C" int __real_close(int);
extern "C" int __wrap_close(int fd)
{
  if (fd != 1001) return __real_close(fd);
  ++closes;
  return 0;
}
int AppInitialize() { ++appInitializes; return initializeError; }
int AppTerminate() { ++appTerminates; return terminateError; }
int AppQuery(int storage, uint64_t* out)
{
  assert(storage == 0);
  *out = gamesBytes;
  return gamesError;
}
int SaveInitialize(const void* param)
{
  assert(param == nullptr);
  ++saveInitializes;
  savedInitialized = saveInitializeError == 0;
  return saveInitializeError;
}
int SaveTerminate() { ++saveTerminates; savedInitialized = false; return terminateError; }
struct SaveRequest { int32_t platform, user; std::array<uint64_t, 4> reserved; };
int SaveQuery(const SaveRequest* request, uint64_t* out)
{
  assert(request->platform == 0 && request->user == -1);
  for (auto item : request->reserved) assert(item == 0);
  if (!savedInitialized) return static_cast<int>(0x809f8002);
  *out = savedBytes;
  return saveError;
}
extern "C" int sceKernelLoadStartModule(const char* path, size_t, const void*,
                                       unsigned int, const void*, int* start)
{
  *start = 0;
  if (loadError) return -1;
  if (std::string(path) == "/system/common/lib/libSceAppInstUtil.sprx") return 10;
  assert(std::string(path) == "/system/common/lib/libSceSaveData.sprx");
  return 11;
}
extern "C" int sceKernelStopUnloadModule(int handle, size_t, const void*, unsigned int,
                                        const void*, int* stop)
{
  assert(handle == 10 || handle == 11);
  *stop = 0;
  ++unloads;
  return 0;
}
extern "C" int sceKernelDlsym(int handle, const char* name, void** out)
{
  if (resolveError) return -1;
  const std::string symbol(name);
  if (handle == 10)
  {
    if (symbol == "sceAppInstUtilInitialize") *out = reinterpret_cast<void*>(AppInitialize);
    else if (symbol == "sceAppInstUtilTerminate") *out = reinterpret_cast<void*>(AppTerminate);
    else { assert(symbol == "sceAppInstUtilAppGetAllAppSize"); *out = reinterpret_cast<void*>(AppQuery); }
  }
  else
  {
    assert(handle == 11);
    if (symbol == "sceSaveDataInitialize3") *out = reinterpret_cast<void*>(SaveInitialize);
    else if (symbol == "sceSaveDataTerminate") *out = reinterpret_cast<void*>(SaveTerminate);
    else { assert(symbol == "sceSaveDataGetAllSizeProspero"); *out = reinterpret_cast<void*>(SaveQuery); }
  }
  return 0;
}
void Reset()
{
  record = {1000, 600, 0, 0, 0, 50, 60};
  openError = ioctlError = gamesError = saveError = initializeError = terminateError = 0;
  saveInitializeError = 0;
  loadError = resolveError = openInterrupts = ioctlInterrupts = 0;
  closes = appInitializes = appTerminates = saveInitializes = saveTerminates = unloads = 0;
  savedInitialized = false;
  gamesBytes = 100;
  savedBytes = 20;
  errors = 0;
}
int main(int argc, char** argv)
{
  assert(argc == 4);
  databasePath = argv[1];
  Reset();
  auto usage = GetPS5InternalStorageUsage();
  assert(usage.total == 1000 && usage.free == 700);
  assert(usage.games == 100 && usage.media == 30 && usage.savedData == 20 && usage.other == 150);
  assert(!errors && closes == 1 && unloads == 2);
  assert(appInitializes == 1 && appTerminates == 1 && saveInitializes == 1 && saveTerminates == 1);
  Reset(); record[5] = 4; record[6] = 5;
  usage = GetPS5InternalStorageUsage();
  assert(usage.free == 600 && usage.other == 250);
  Reset(); openInterrupts = ioctlInterrupts = 1;
  usage = GetPS5InternalStorageUsage();
  assert(usage.free == 700 && closes == 1 && !errors);
  for (int failure : {EACCES, ENOENT})
  {
    Reset(); openError = failure;
    usage = GetPS5InternalStorageUsage();
    assert(!usage.total && !usage.free && !usage.other && usage.games == 100 && errors);
    assert(closes == 0);
  }
  Reset(); ioctlError = ENOTTY;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.total && !usage.other && closes == 1 && errors);
  for (int invalid = 0; invalid < 4; ++invalid)
  {
    Reset();
    if (invalid == 0) record[0] = 0;
    if (invalid == 1) record[1] = 1001;
    if (invalid == 2) { record[5] = std::numeric_limits<uint64_t>::max(); record[6] = 1; }
    if (invalid == 3) record[5] = 1000;
    usage = GetPS5InternalStorageUsage();
    assert(!usage.total && !usage.free && !usage.other && errors);
  }
  Reset(); gamesBytes = 1001;
  usage = GetPS5InternalStorageUsage();
  assert(usage.games == 1001 && !usage.other && errors);
  Reset(); record[0] = uint64_t{1} << 40; record[1] = uint64_t{1} << 39;
  gamesBytes = uint64_t{1} << 34; savedBytes = uint64_t{1} << 33;
  usage = GetPS5InternalStorageUsage();
  assert(usage.games == gamesBytes && usage.savedData == savedBytes);
  assert(usage.other == record[0] - record[1] - gamesBytes - savedBytes - 30 && !errors);
  Reset(); gamesError = -5;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.games && !usage.other && usage.media == 30 && usage.savedData == 20 && errors);
  Reset(); saveError = -5;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.savedData && !usage.other && saveTerminates == 1 && errors);
  Reset(); initializeError = saveInitializeError = -5;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.games && !usage.savedData && !usage.other && errors);
  Reset(); saveInitializeError = static_cast<int>(0x809f000b);
  usage = GetPS5InternalStorageUsage();
  assert(usage.games == 100 && usage.media == 30 && !usage.savedData && !usage.other && errors);
  Reset(); savedInitialized = true;
  usage = GetPS5InternalStorageUsage();
  assert(usage.savedData == 20 && !saveInitializes && !saveTerminates);
  Reset(); loadError = 1;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.games && !usage.savedData && usage.media == 30 && !unloads && errors);
  Reset(); resolveError = 1;
  usage = GetPS5InternalStorageUsage();
  assert(!usage.games && !usage.savedData && unloads == 2 && errors);
  Reset(); terminateError = -5;
  usage = GetPS5InternalStorageUsage();
  assert(usage.games == 100 && usage.savedData == 20 && !unloads && errors);
  Reset(); databasePath = argv[2]; gamesBytes = savedBytes = 0; record[1] = 900;
  usage = GetPS5InternalStorageUsage();
  assert(usage.free == 1000 && usage.media == 0 && usage.games == 0 && usage.other == 0 && !errors);
  for (const auto& path : {std::string(argv[3]), std::string(argv[3]) + ".missing",
                           std::string(argv[3]) + ".schema", std::string(argv[3]) + ".type"})
  {
    Reset(); databasePath = path;
    usage = GetPS5InternalStorageUsage();
    assert(!usage.media && !usage.other && errors);
  }
}
"""

GROUPING = r"""
#define FMT_HEADER_ONLY
#include <fmt/format.h>
#include "PS5StorageUsage.h"
#include "utils/log.h"
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <system_error>
#include <utility>
#include <vector>
#include <cerrno>
struct CMediaSource { std::string strPath; };
struct statfs { char f_mntonname[64]{}; struct { int32_t val[2]{}; } f_fsid; };
struct Space { uint64_t capacity = 1000, free = 600; };
struct Mount { std::string path; int id; Space space; int error = 0; };
std::map<std::string, Mount> mounts;
int statfs(const char* path, struct statfs* out)
{
  auto it = mounts.find(path);
  if (it == mounts.end()) { errno = ENOENT; return -1; }
  std::strcpy(out->f_mntonname, it->second.path.c_str());
  out->f_fsid.val[0] = it->second.id;
  return 0;
}
void Add(std::vector<CMediaSource>& drives, const char* path, const char*)
{
  drives.push_back({path});
}
const char* const kRemovable[] = {"/mnt/usb0", "/mnt/usb1", "/mnt/usb2", "/mnt/usb3",
  "/mnt/usb4", "/mnt/usb5", "/mnt/usb6", "/mnt/usb7", "/mnt/ext0", "/mnt/ext1", "/mnt/disc"};
namespace KODI::PLATFORM::FILESYSTEM {
Space space(const std::string& path, std::error_code& ec)
{
  ec = std::error_code(mounts.at(path).error, std::generic_category());
  return mounts.at(path).space;
}
}
struct Strings
{
  std::string Get(int id) const
  {
    return std::map<int, std::string>{{39220,"Internal"},{39221,"External"},{39222,"Games and apps"},
      {39223,"Media gallery"},{39224,"Saved data"},{39225,"USB"},{29921,"Other"},{427,"Disc"},
      {10006,"N/A"},{20161,"Total"},{20162,"Used"},{160,"Free"}}.at(id);
  }
};
struct Resources { Strings GetLocalizeStrings() const { return {}; } };
struct CServiceBroker { static Resources GetResourcesComponent() { return {}; } };
struct StringUtils
{
  static std::string SizeToString(uint64_t bytes) { return std::to_string(bytes); }
  static bool StartsWith(const std::string& s, const std::string& prefix) { return s.starts_with(prefix); }
  static void ToUpper(std::string& s) { for (char& c : s) c = std::toupper(c); }
  template<class... T> static std::string Format(std::string pattern, const T&... args)
  {
    return fmt::format(fmt::runtime(pattern), args...);
  }
};
PS5InternalStorageUsage internalUsage{1000, 600, 100, 30, 20, 250};
PS5InternalStorageUsage GetPS5InternalStorageUsage() { return internalUsage; }
class CPS5StorageProvider
{
public:
  void GetRemovableDrives(std::vector<CMediaSource>&);
  std::vector<std::string> GetDiskUsage();
};
"""

GROUPING_CHECKS = r"""
int main()
{
  CPS5StorageProvider provider;
  auto rows = provider.GetDiskUsage();
  assert(rows.size() == 5 && rows[0] == "Internal: Total 1.00 KB, Free 600 B");
  assert(rows[1] == "[TABS]1[/TABS]Games and apps: 100 B (Used)");
  assert(rows[4] == "[TABS]1[/TABS]Other: 250 B (Used)");
  internalUsage = {667200000000, 640200000000, 5400000000, 0, 46180000, 21460000000};
  rows = provider.GetDiskUsage();
  assert(rows[0] == "Internal: Total 667.2 GB, Free 640.2 GB");
  assert(rows[1] == "[TABS]1[/TABS]Games and apps: 5.40 GB (Used)");
  assert(rows[2] == "[TABS]1[/TABS]Media gallery: 0 KB (Used)");
  assert(rows[3] == "[TABS]1[/TABS]Saved data: 46.18 MB (Used)");
  assert(rows[4] == "[TABS]1[/TABS]Other: 21.46 GB (Used)");
  internalUsage = {};
  rows = provider.GetDiskUsage();
  assert(rows[0] == "Internal: Total N/A, Free N/A");
  assert(rows[3] == "[TABS]1[/TABS]Saved data: N/A (Used)");
  internalUsage = {1000, 600, 100, 30, 20, 250};
  mounts["/mnt/usb0"] = {"/", 1, {}};
  rows = provider.GetDiskUsage();
  assert(rows.size() == 5);
  mounts["/mnt/usb0"] = {"/mnt/usb0", 1, {}};
  mounts["/mnt/ext0"] = {"/mnt/ext0", 2, {}};
  rows = provider.GetDiskUsage();
  assert(rows.size() == 9 && rows[5] == "External" && rows[7] == "USB");
  assert(rows[6] == "[TABS]1[/TABS]EXT0: Used 400 B, Free 600 B");
  assert(rows[8] == "[TABS]1[/TABS]USB0: Used 400 B, Free 600 B");
  mounts["/mnt/usb1"] = {"/mnt/usb1", 1, {}};
  rows = provider.GetDiskUsage(); assert(rows.size() == 9);
  mounts.clear();
  for (int i = 0; i < 8; ++i) {
    const auto path = "/mnt/usb" + std::to_string(i);
    mounts[path] = {path, i+1, {}};
  }
  mounts["/mnt/ext0"] = {"/mnt/ext0", 10, {}};
  mounts["/mnt/ext1"] = {"/mnt/ext1", 11, {}};
  mounts["/mnt/disc"] = {"/mnt/disc", 12, {}};
  rows = provider.GetDiskUsage();
  assert(rows.size() == 19 && rows.back() == "[TABS]1[/TABS]DISC: Used 400 B, Free 600 B");
  mounts.clear(); rows = provider.GetDiskUsage(); assert(rows.size() == 5);
  mounts["/mnt/usb0"] = {"/mnt/usb0", 1, {}, EIO};
  rows = provider.GetDiskUsage();
  assert(rows.size() == 7 && rows[5] == "USB" && rows[6] == "[TABS]1[/TABS]USB0: N/A" && errors);
  mounts["/mnt/usb0"].error = 0; mounts["/mnt/usb0"].space.free = 1001;
  rows = provider.GetDiskUsage(); assert(rows[6] == "[TABS]1[/TABS]USB0: N/A");
}
"""

class StorageUsageTests(unittest.TestCase):
    def test_accounting_and_grouping(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler, "A host C++ compiler is required")
        with tempfile.TemporaryDirectory(prefix="ps5-storage-test-") as directory:
            temp = pathlib.Path(directory)
            (temp / "utils").mkdir()
            (temp / "utils/log.h").write_text(LOGGER)
            fixtures = []
            for name, sizes in [("media", [10, 20]), ("empty", []), ("invalid", [-1])]:
                path = temp / (name + ".db")
                with sqlite3.connect(path) as database:
                    database.execute("CREATE TABLE tbl_PhotoVideo(size_on_storage INTEGER)")
                    database.executemany("INSERT INTO tbl_PhotoVideo VALUES(?)", [(n,) for n in sizes])
                fixtures.append(str(path))
            with sqlite3.connect(fixtures[-1] + ".schema") as database:
                database.execute("CREATE TABLE unrelated(value INTEGER)")
            with sqlite3.connect(fixtures[-1] + ".type") as database:
                database.execute("CREATE TABLE tbl_PhotoVideo(size_on_storage REAL)")
                database.execute("INSERT INTO tbl_PhotoVideo VALUES(3.5)")
            (temp / "accounting.cpp").write_text(ACCOUNTING)
            subprocess.run(
                [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(temp),
                 "-I", str(STORAGE), str(STORAGE / "PS5StorageUsage.cpp"),
                 str(temp / "accounting.cpp"), "-l:libsqlite3.so.0", "-Wl,--wrap=open",
                 "-Wl,--wrap=close", "-Wl,--wrap=ioctl", "-Wl,--wrap=sqlite3_open_v2",
                 "-o", str(temp / "accounting")], check=True,
            )
            subprocess.run([str(temp / "accounting"), *fixtures], check=True)
            self.assertFalse(pathlib.Path(fixtures[-1] + ".missing").exists())
            source = (STORAGE / "PS5StorageProvider.cpp").read_text()
            removable = source[source.index("void CPS5StorageProvider::GetRemovableDrives("):
                               source.index("std::set<std::string> CPS5StorageProvider::GetRemovablePaths(")]
            grouping = source[source.index("std::vector<std::string> CPS5StorageProvider::GetDiskUsage()"):]
            (temp / "grouping.cpp").write_text(GROUPING + removable + grouping + GROUPING_CHECKS)
            subprocess.run(
                [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(temp),
                 "-I", str(STORAGE), str(temp / "grouping.cpp"), "-o", str(temp / "grouping")],
                check=True,
            )
            subprocess.run([str(temp / "grouping")], check=True)
            for path in fixtures:
                with sqlite3.connect(path) as database:
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM tbl_PhotoVideo").fetchone()[0],
                                     2 if pathlib.Path(path).stem == "media" else
                                     0 if pathlib.Path(path).stem == "empty" else 1)


if __name__ == "__main__":
    unittest.main()
