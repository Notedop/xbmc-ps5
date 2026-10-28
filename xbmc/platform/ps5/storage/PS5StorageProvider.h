/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "storage/IStorageProvider.h"
#include "threads/Thread.h"

#include <atomic>
#include <set>
#include <string>
#include <vector>

class CPS5StorageProvider : public IStorageProvider, private CThread
{
public:
  CPS5StorageProvider();
  ~CPS5StorageProvider() override;

  void Initialize() override;
  void Stop() override;
  void GetLocalDrives(std::vector<CMediaSource>& localDrives) override;
  void GetRemovableDrives(std::vector<CMediaSource>& removableDrives) override;
  bool Eject(const std::string& mountpath) override { return false; }
  std::vector<std::string> GetDiskUsage() override;
  bool PumpDriveChangeEvents(IStorageEventsCallback* callback) override;

private:
  void Process() override;
  std::set<std::string> GetRemovablePaths();

  int m_kqueue = -1;
  int m_wakeupRead = -1;
  int m_wakeupWrite = -1;
  std::atomic<bool> m_changed{false};
  std::set<std::string> m_removable;
};
