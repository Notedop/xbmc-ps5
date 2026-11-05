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
constexpr int LOGINFO = 2;
inline int errors = 0;
struct CLog {
  template<class... T> static void Log(int level, const char*, const T&...) {
    if (level == LOGERROR) ++errors;
  }
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
int openInterrupts, ioctlInterrupts;
int closes, appInitializes, appTerminates, saveInitializes, saveTerminates;
bool savedInitialized;
bool usbInitialized;
extern "C" int sceSysmoduleLoadModule(uint16_t) { assert(false); return -1; }
extern "C" int sceSysmoduleLoadModuleInternal(uint32_t) { assert(false); return -1; }
extern "C" int sceKernelDebugOutText(int, const char*) { assert(false); return -1; }
int usbInitializes, usbTerminates, usbRequests, discRequests, ejectError, usbInitializeError;
std::string usbDevice = "/dev/da0";
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
extern "C" int sceAppInstUtilInitialize() { ++appInitializes; return initializeError; }
extern "C" int sceAppInstUtilTerminate() { ++appTerminates; return terminateError; }
extern "C" int sceAppInstUtilAppGetAllAppSize(int storage, uint64_t* out)
{
  assert(storage == 0);
  *out = gamesBytes;
  return gamesError;
}
extern "C" int sceSaveDataInitialize3(const void* param)
{
  assert(param == nullptr);
  ++saveInitializes;
  savedInitialized = saveInitializeError == 0;
  return saveInitializeError;
}
extern "C" int sceSaveDataTerminate()
{
  ++saveTerminates;
  savedInitialized = false;
  return terminateError;
}
extern "C" int sceUsbStorageInit(const void* param)
{
  assert(param == nullptr);
  ++usbInitializes;
  usbInitialized = usbInitializeError == 0;
  return usbInitializeError;
}
extern "C" int sceUsbStorageTerm() { ++usbTerminates; usbInitialized = false; return terminateError; }
namespace sceAutoMounterIpcClient
{
int requestUnmount(const char* device)
{
  assert(std::string(device) == usbDevice);
  ++usbRequests;
  return usbInitialized ? ejectError : static_cast<int>(0x80f40009);
}
}
extern "C" int sceShellCoreUtilRequestEjectDevice(const char* device)
{
  assert(std::string(device) == "/dev/cd0" || std::string(device) == "/dev/cd1" ||
         std::string(device) == "/dev/cd");
  ++discRequests;
  return ejectError;
}
struct SaveRequest { int32_t platform, user; std::array<uint64_t, 4> reserved; };
extern "C" int sceSaveDataGetAllSizeProspero(const SaveRequest* request, uint64_t* out)
{
  assert(request->platform == 0 && request->user == -1);
  for (auto item : request->reserved) assert(item == 0);
  if (!savedInitialized) return static_cast<int>(0x809f8002);
  *out = savedBytes;
  return saveError;
}
void Reset()
{
  record = {1000, 600, 0, 0, 0, 50, 60};
  openError = ioctlError = gamesError = saveError = initializeError = terminateError = 0;
  saveInitializeError = 0;
  openInterrupts = ioctlInterrupts = 0;
  closes = appInitializes = appTerminates = saveInitializes = saveTerminates = 0;
  savedInitialized = false;
  usbInitialized = false;
  usbDevice = "/dev/da0";
  usbInitializes = usbTerminates = usbRequests = discRequests = ejectError = usbInitializeError = 0;
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
  assert(!errors && closes == 1);
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
  Reset(); terminateError = -5;
  usage = GetPS5InternalStorageUsage();
  assert(usage.games == 100 && usage.savedData == 20 && errors);
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
  Reset();
  for (const auto* device : {"/dev/cd0", "/dev/cd1", "/dev/cd"})
    assert(EjectPS5Disc(device));
  assert(discRequests == 3 && !errors);
  assert(!EjectPS5Disc("/dev/da0") && discRequests == 3 && errors);
  Reset(); ejectError = -5;
  assert(!EjectPS5Disc("/dev/cd0") && errors);
  Reset();
  assert(UnmountPS5Storage("/dev/da0"));
  assert(usbRequests == 2 && usbInitializes == 1 && usbTerminates == 1 && !errors);
  Reset(); usbInitialized = true;
  assert(UnmountPS5Storage("/dev/da0"));
  assert(usbRequests == 1 && !usbInitializes && !usbTerminates && !errors);
  Reset(); usbInitializeError = -5;
  assert(!UnmountPS5Storage("/dev/da0") && usbInitializes == 1 && !usbTerminates && errors);
  Reset(); ejectError = static_cast<int>(0x80f40014);
  assert(!UnmountPS5Storage("/dev/da0") && usbTerminates == 1 && errors);
  Reset(); terminateError = -5;
  assert(UnmountPS5Storage("/dev/da0") && errors);
  Reset();
  for (const auto* device : {"/dev/da1s1", "/dev/da12x1", "/dev/da0s1.crypt",
                            "/dev/da12x2.crypt", "/dev/da1.crypt"})
  {
    Reset();
    usbDevice = device;
    assert(UnmountPS5Storage(device));
    assert(usbRequests == 2 && usbInitializes == 1 && usbTerminates == 1 && !errors);
  }
  Reset();
  for (const auto* device : {"/mnt/usb0", "/dev/", "/dev/da_all", "/dev/da", "/dev/cd0",
                            "/dev/da1s", "/dev/da1x", "/dev/das1", "/dev/da1s1/..",
                            "/dev/da1s-1", "/dev/da1x1junk", "/dev/da1.crypt.crypt"})
    assert(!UnmountPS5Storage(device) && !usbRequests && errors);
}
"""

GROUPING = r"""
#define FMT_HEADER_ONLY
#include <fmt/format.h>
#include "PS5StorageUsage.h"
#include "utils/log.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>
#include <cerrno>
enum class SourceType { LOCAL, REMOVABLE };
struct CMediaSource { std::string strPath; SourceType m_iDriveType; };
struct statfs {
  char f_mntonname[64]{}, f_mntfromname[64]{};
  struct { int32_t val[2]{}; } f_fsid;
};
struct Space { uint64_t capacity = 1000, free = 600; };
struct Mount {
  std::string path;
  int id;
  Space space;
  int error = 0;
  std::string device = "/dev/da0";
};
std::map<std::string, Mount> mounts;
int statfsError = 0;
int statfs(const char* path, struct statfs* out)
{
  if (statfsError) { errno = statfsError; return -1; }
  auto it = mounts.find(path);
  if (it == mounts.end()) { errno = ENOENT; return -1; }
  std::strcpy(out->f_mntonname, it->second.path.c_str());
  out->f_fsid.val[0] = it->second.id;
  std::strcpy(out->f_mntfromname,
              std::string(path) == "/mnt/disc" ? "/dev/cd0" : it->second.device.c_str());
  return 0;
}
void Add(std::vector<CMediaSource>& drives, const char* path, const char*, SourceType type)
{
  drives.push_back({path, type});
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
int unmountMode = 0, unmountRequests = 0, discRequests = 0;
std::string lastUnmountDevice;
std::string GetStorageDevice(const struct statfs&);
bool UnmountPS5Storage(const char* device)
{
  assert(std::any_of(mounts.begin(), mounts.end(),
                    [device](const auto& mount) { return mount.second.device == device; }));
  lastUnmountDevice = device;
  ++unmountRequests;
  if (unmountMode == 1) return false;
  if (unmountMode == 2) return true;
  if (unmountMode == 3) { statfsError = EIO; return true; }
  if (unmountMode == 4) { ++mounts.at("/mnt/usb0").id; return true; }
  for (auto it = mounts.begin(); it != mounts.end();)
  {
    struct statfs info{};
    std::strcpy(info.f_mntfromname, it->second.device.c_str());
    if (GetStorageDevice(info) == device) it = mounts.erase(it);
    else ++it;
  }
  return true;
}
bool EjectPS5Disc(const char* device)
{
  assert(std::string(device) == "/dev/cd0");
  ++discRequests;
  return unmountMode != 1;
}
namespace MEDIA_DETECT::STORAGE {
enum class Type { UNKNOWN };
struct StorageDevice { std::string label, path; Type type; };
}
struct IStorageEventsCallback
{
  std::vector<std::string> events;
  void OnStorageAdded(const MEDIA_DETECT::STORAGE::StorageDevice& d) { events.push_back("add:" + d.path); }
  void OnStorageSafelyRemoved(const MEDIA_DETECT::STORAGE::StorageDevice& d) { events.push_back("safe:" + d.path); }
  void OnStorageUnsafelyRemoved(const MEDIA_DETECT::STORAGE::StorageDevice& d) { events.push_back("unsafe:" + d.path); }
};
class CPS5StorageProvider
{
public:
  void GetRemovableDrives(std::vector<CMediaSource>&);
  std::set<std::string> GetRemovablePaths();
  bool Eject(const std::string&);
  bool PumpDriveChangeEvents(IStorageEventsCallback*);
  std::vector<std::string> GetDiskUsage();
  std::atomic<bool> m_changed{false};
  std::set<std::string> m_removable, m_ejected;
};
"""

REMOVAL_CHECKS = r"""
void TestRemoval()
{
  CPS5StorageProvider provider;
  IStorageEventsCallback callback;
  struct statfs info{};
  for (const auto* device : {"/dev/da0", "/dev/da1s1", "/dev/da0x1",
                            "/dev/da0s1.crypt", "/dev/da12x1"})
  {
    std::strcpy(info.f_mntfromname, device);
    assert(GetStorageDevice(info) == device);
  }
  std::strcpy(info.f_mntfromname, "/dev/cd0.crypt");
  assert(GetStorageDevice(info) == "/dev/cd0");
  assert(!provider.PumpDriveChangeEvents(&callback));
  for (const auto* path : {"/", "/app0", "/data", "/mnt/usb8", "/mnt/usb0/file", "/mnt/usb0/../usb1"})
    assert(!provider.Eject(path));
  assert(!unmountRequests && !discRequests);
  assert(!provider.Eject("/mnt/usb0"));
  mounts["/mnt/usb0"] = {"/", 1, {}};
  assert(!provider.Eject("/mnt/usb0") && !unmountRequests);
  mounts["/mnt/usb0"].path = "/mnt/usb0";
  std::vector<CMediaSource> drives;
  provider.GetRemovableDrives(drives);
  assert(drives.size() == 1 && drives[0].m_iDriveType == SourceType::REMOVABLE);
  provider.m_removable = provider.GetRemovablePaths();
  assert(provider.Eject("/mnt/usb0/"));
  assert(provider.PumpDriveChangeEvents(&callback));
  assert(callback.events == std::vector<std::string>{"safe:/mnt/usb0"});
  assert(!provider.PumpDriveChangeEvents(&callback));
  mounts["/mnt/usb0"] = {"/mnt/usb0", 1, {}};
  provider.m_changed = true;
  provider.PumpDriveChangeEvents(&callback);
  mounts.clear(); provider.m_changed = true;
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events.back() == "unsafe:/mnt/usb0");
  mounts["/mnt/disc"] = {"/mnt/disc", 2, {}};
  provider.m_removable = provider.GetRemovablePaths();
  assert(provider.Eject("/mnt/disc") && discRequests == 1);
  mounts.clear(); provider.m_changed = true;
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events.back() == "safe:/mnt/disc");
  mounts["/mnt/usb0"] = {"/mnt/usb0", 3, {}};
  provider.m_removable = provider.GetRemovablePaths();
  assert(provider.Eject("/mnt/usb0"));
  mounts["/mnt/usb0"] = {"/mnt/usb0", 4, {}};
  callback.events.clear();
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events == (std::vector<std::string>{"safe:/mnt/usb0", "add:/mnt/usb0"}));
  mounts.clear(); provider.m_changed = true;
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events.back() == "unsafe:/mnt/usb0");
  for (int mode : {1, 2, 3})
  {
    mounts["/mnt/usb0"] = {"/mnt/usb0", 5, {}};
    provider.m_removable = provider.GetRemovablePaths();
    unmountMode = mode;
    assert(!provider.Eject("/mnt/usb0") && provider.m_ejected.empty());
    statfsError = 0;
    mounts.clear(); provider.m_changed = true;
    provider.PumpDriveChangeEvents(&callback);
    assert(callback.events.back() == "unsafe:/mnt/usb0");
  }
  mounts["/mnt/usb0"] = {"/mnt/usb0", 6, {}};
  provider.m_removable = provider.GetRemovablePaths();
  unmountMode = 4;
  assert(provider.Eject("/mnt/usb0"));
  callback.events.clear();
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events == (std::vector<std::string>{"safe:/mnt/usb0", "add:/mnt/usb0"}));
  unmountMode = 0;
  assert(provider.Eject("/mnt/usb0"));
  assert(provider.PumpDriveChangeEvents(nullptr) && provider.m_removable.empty() && provider.m_ejected.empty());
  for (const auto* path : {"/mnt/ext0", "/mnt/ext1", "/mnt/usb7"})
  {
    mounts[path] = {path, 7, {}};
    assert(provider.Eject(path));
    assert(provider.PumpDriveChangeEvents(nullptr));
  }
  mounts["/mnt/usb0"] = {"/mnt/usb0", 8, {}, 0, "/dev/da1s1"};
  mounts["/mnt/usb1"] = {"/mnt/usb1", 9, {}, 0, "/dev/da1s2"};
  mounts["/mnt/usb2"] = {"/mnt/usb2", 10, {}, 0, "/dev/da1x1"};
  provider.m_removable = provider.GetRemovablePaths();
  assert(provider.Eject("/mnt/usb0") && mounts.size() == 2 &&
         mounts.count("/mnt/usb1") && mounts.count("/mnt/usb2"));
  assert(lastUnmountDevice == "/dev/da1s1");
  callback.events.clear();
  provider.PumpDriveChangeEvents(&callback);
  assert(callback.events == (std::vector<std::string>{"safe:/mnt/usb0"}));
  mounts.clear(); errors = 0;
}
"""

GROUPING_CHECKS = r"""
int main()
{
  TestRemoval();
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
    def test_unbound_disc_import_fails_without_lookup_or_load(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        implementation = (STORAGE / "PS5StorageUsage.cpp").read_text()
        implementation = implementation[
            implementation.index("bool EjectPS5Disc("):
            implementation.index("bool PreloadPS5UsbStorage(")]
        self.assertNotIn("StorageModule", implementation)
        self.assertNotIn("Resolve", implementation)
        with tempfile.TemporaryDirectory(prefix="ps5-disc-bindings-") as directory:
            temp = pathlib.Path(directory)
            source = temp / "disc.cpp"
            source.write_text(r"""
#include <cassert>
#include <cstdint>
#include <cstring>
constexpr int LOGERROR = 1;
int errors = 0;
struct CLog {
  template<class... T> static void Log(int level, const char*, const T&...) {
    assert(level == LOGERROR);
    ++errors;
  }
};
extern "C" int sceShellCoreUtilRequestEjectDevice(const char*) __attribute__((weak));
""" + implementation + r"""
int main() {
  assert(!EjectPS5Disc("/dev/cd0"));
  assert(!EjectPS5Disc("/dev/cd1"));
  assert(!EjectPS5Disc("/dev/cd"));
  assert(!EjectPS5Disc("/dev/da1s1"));
  assert(errors == 4);
}
""")
            binary = temp / "disc"
            subprocess.run(
                [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                 str(source), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)

    def test_storage_preloads_are_cached_and_attempted_independently(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        implementation = (STORAGE / "PS5StorageUsage.cpp").read_text()
        helper = implementation[implementation.index("template<typename Id>"):
                                implementation.index("} // namespace")]
        implementation = implementation[implementation.index("bool PreloadPS5UsbStorage("):
                                        implementation.index("bool UnmountPS5Storage(")]
        main = (ROOT / "xbmc/platform/ps5/main.cpp").read_text()
        startup = main[main.index("int main("):]
        self.assertLess(startup.index("PreloadPS5UsbStorage();"),
                        startup.index("if (!OpenSandbox())"))
        self.assertGreater(startup.index("PreloadPS5InternalStorageModules();"),
                           startup.index("if (!OpenSandbox())"))
        self.assertIn("return ExitRequest(EXIT_FAILURE);", startup[
            startup.index("if (!OpenSandbox())"):
            startup.index("PreloadPS5InternalStorageModules();")])
        self.assertNotIn("if (!PreloadPS5UsbStorage", startup)
        self.assertNotIn("if (!PreloadPS5InternalStorageModules", startup)
        with tempfile.TemporaryDirectory(prefix="ps5-storage-preload-") as directory:
            temp = pathlib.Path(directory)
            for usb_status, save_status, app_status, missing in (
                    (0, 0, 0, None), (-1, 0, 0, None), (0, -2, 0, None),
                    (0, 0, -3, None), (0, 0, 0, "public"), (0, 0, 0, "internal")):
                with self.subTest(usb=usb_status, save=save_status, app=app_status, missing=missing):
                    declarations = r"""
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
std::vector<uint32_t> calls;
std::vector<std::string> messages;
extern "C" int sceKernelDebugOutText(int channel, const char* text) {
  assert(channel == 0);
  messages.emplace_back(text);
  return 0;
}
"""
                    if missing == "public":
                        declarations += (
                            'extern "C" int sceSysmoduleLoadModule(uint16_t) '
                            '__attribute__((weak));\n')
                    else:
                        declarations += (
                            'extern "C" int sceSysmoduleLoadModule(uint16_t id) { '
                            f'assert(id == 0x00d5); calls.push_back(id); return {usb_status}; }}\n')
                    if missing == "internal":
                        declarations += (
                            'extern "C" int sceSysmoduleLoadModuleInternal(uint32_t) '
                            '__attribute__((weak));\n')
                    else:
                        declarations += (
                            'extern "C" int sceSysmoduleLoadModuleInternal(uint32_t id) { '
                            'calls.push_back(id); '
                            f'if (id == 0x8000000f) return {save_status}; '
                            f'assert(id == 0x80000014); return {app_status}; }}\n')
                    expected_usb = "true" if missing != "public" and usb_status == 0 else "false"
                    expected_internal = "true" if missing != "internal" and not (
                        save_status or app_status) else "false"
                    ids = ([0x00d5] if missing != "public" else []) + (
                        [0x8000000f, 0x80000014] if missing != "internal" else [])
                    texts = []
                    for status, loader in ((usb_status, "public"), (save_status, "internal"),
                                           (app_status, "internal")):
                        texts.append("loader import is not bound" if missing == loader else
                                     f"0x{status & 0xffffffff:08x}")
                    source = temp / "preload.cpp"
                    source.write_text(declarations + helper + implementation + f"""
int main() {{
  assert(PreloadPS5UsbStorage() == {expected_usb});
  assert(messages.size() == 1);
  assert(PreloadPS5InternalStorageModules() == {expected_internal});
  assert(PreloadPS5UsbStorage() == {expected_usb});
  assert(PreloadPS5InternalStorageModules() == {expected_internal});
  assert(calls == std::vector<uint32_t>({{{', '.join(hex(id) for id in ids)}}}));
  assert(messages.size() == 3);
  assert(messages[0].find("USB storage module preload") != std::string::npos);
  assert(messages[1].find("Saved data module preload") != std::string::npos);
  assert(messages[2].find("App installer module preload") != std::string::npos);
  assert(messages[0].find("{texts[0]}") != std::string::npos);
  assert(messages[1].find("{texts[1]}") != std::string::npos);
  assert(messages[2].find("{texts[2]}") != std::string::npos);
}}
""")
                    binary = temp / "preload"
                    subprocess.run(
                        [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                         str(source), "-o", str(binary)],
                        check=True, capture_output=True)
                    subprocess.run([str(binary)], check=True, capture_output=True)

    def test_unbound_accounting_imports_fail_before_calling_any_client(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        implementation = (STORAGE / "PS5StorageUsage.cpp").read_text()
        for forbidden in ("class StorageModule", "sceKernelLoadStartModule", "sceKernelDlsym",
                          "sceKernelStopUnloadModule"):
            self.assertNotIn(forbidden, implementation)
        record = implementation[implementation.index("struct SaveDataSizeQuery\n"):
                                implementation.index("void GetCapacity(")]
        functions = implementation[implementation.index("std::optional<uint64_t> GetGamesUsage("):
                                   implementation.index("std::optional<uint64_t> GetMediaUsage(")]
        with tempfile.TemporaryDirectory(prefix="ps5-accounting-bindings-") as directory:
            temp = pathlib.Path(directory)
            declarations = r"""
#include <array>
#include <cassert>
#include <cstdint>
#include <optional>
constexpr int LOGERROR = 1;
int errors = 0, calls = 0;
struct CLog {
  template<class... T> static void Log(int level, const char*, const T&...) {
    assert(level == LOGERROR);
    ++errors;
  }
};
""" + record + r"""
extern "C" {
int sceAppInstUtilInitialize() __attribute__((weak));
int sceAppInstUtilTerminate() __attribute__((weak));
int sceAppInstUtilAppGetAllAppSize(int, uint64_t*) __attribute__((weak));
int sceSaveDataInitialize3(const void*) __attribute__((weak));
int sceSaveDataTerminate() __attribute__((weak));
int sceSaveDataGetAllSizeProspero(const SaveDataSizeQuery*, uint64_t*) __attribute__((weak));
}
"""
            definitions = {
                "sceAppInstUtilInitialize": "()",
                "sceAppInstUtilTerminate": "()",
                "sceAppInstUtilAppGetAllAppSize": "(int, uint64_t*)",
                "sceSaveDataInitialize3": "(const void*)",
                "sceSaveDataTerminate": "()",
                "sceSaveDataGetAllSizeProspero": "(const SaveDataSizeQuery*, uint64_t*)",
            }
            for missing in definitions:
                with self.subTest(missing=missing):
                    operation = ("GetGamesUsage" if missing.startswith("sceAppInstUtil") else
                                 "GetSavedDataUsage")
                    source = temp / "accounting.cpp"
                    source.write_text(declarations + "".join(
                        f'extern "C" int {name}{signature} {{ ++calls; return 0; }}\n'
                        for name, signature in definitions.items() if name != missing) +
                        functions + f"""
int main() {{
  assert(!{operation}());
  assert(calls == 0 && errors == 1);
}}
""")
                    binary = temp / "accounting"
                    subprocess.run(
                        [compiler, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                         str(source), "-o", str(binary)], check=True, capture_output=True)
                    subprocess.run([str(binary)], check=True, capture_output=True)

    def test_unbound_usb_imports_fail_without_calling_address_zero(self):
        compiler = shutil.which("c++")
        self.assertIsNotNone(compiler)
        implementation = (STORAGE / "PS5StorageUsage.cpp").read_text()
        implementation = implementation[
            implementation.index("bool UnmountPS5Storage("):
            implementation.index("PS5InternalStorageUsage GetPS5InternalStorageUsage()")]
        with tempfile.TemporaryDirectory(prefix="ps5-usb-bindings-") as directory:
            temp = pathlib.Path(directory)
            source = temp / "test.cpp"
            for missing in ("unmount", "initialize", "terminate"):
                with self.subTest(missing=missing):
                    declarations = r"""
#include <cassert>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <sstream>
#include <string>
#include <string_view>
constexpr int LOGERROR = 1;
constexpr int LOGINFO = 2;
int errors = 0, calls = 0;
std::string messages;
struct CLog {
  template<class... T> static void Log(int level, const char* format, const T&... values) {
    if (level == LOGERROR) ++errors;
    std::ostringstream line;
    line << format;
    ((line << ' ' << values), ...);
    messages += line.str() + '\n';
  }
};
extern "C" int sceUsbStorageInit(const void*) __attribute__((weak));
extern "C" int sceUsbStorageTerm() __attribute__((weak));
namespace sceAutoMounterIpcClient {
int requestUnmount(const char*) __attribute__((weak));
}
"""
                    definitions = {
                        "unmount": "namespace sceAutoMounterIpcClient { "
                                   "int requestUnmount(const char*) { ++calls; return 0; } }\n",
                        "initialize": 'extern "C" int sceUsbStorageInit(const void*) '
                                      "{ ++calls; return 0; }\n",
                        "terminate": 'extern "C" int sceUsbStorageTerm() '
                                     "{ ++calls; return 0; }\n",
                    }
                    source.write_text(declarations +
                                      "".join(code for name, code in definitions.items()
                                              if name != missing) + implementation +
                                      r'''
int main() {
  assert(!UnmountPS5Storage("/dev/da1s1"));
  assert(calls == 0 && errors == 1);
  assert(messages.find("USB native imports are not bound") != std::string::npos);
}
''')
                    binary = temp / "test"
                    subprocess.run([compiler, "-std=c++20", "-O2", "-Wall", "-Wextra",
                                    "-Werror", str(source), "-o", str(binary)],
                                   check=True, capture_output=True)
                    subprocess.run([str(binary)], check=True, capture_output=True)

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
            device = source[source.index("std::string GetStorageDevice("):
                            source.index("} // namespace")]
            removable = source[source.index("void CPS5StorageProvider::GetRemovableDrives("):
                               source.index("std::set<std::string> CPS5StorageProvider::GetRemovablePaths(")]
            events = source[source.index("std::set<std::string> CPS5StorageProvider::GetRemovablePaths("):
                            source.index("std::vector<std::string> CPS5StorageProvider::GetDiskUsage()")]
            grouping = source[source.index("std::vector<std::string> CPS5StorageProvider::GetDiskUsage()"):]
            (temp / "grouping.cpp").write_text(
                GROUPING + device + removable + events + grouping + REMOVAL_CHECKS + GROUPING_CHECKS)
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
