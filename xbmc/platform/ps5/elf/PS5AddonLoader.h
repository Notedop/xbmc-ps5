/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */
#pragma once

#include "cores/DllLoader/LibraryLoader.h"

class CPS5AddonLoader : public LibraryLoader
{
public:
  explicit CPS5AddonLoader(const std::string& library);
  ~CPS5AddonLoader() override;

  bool Load() override;
  void Unload() override;
  int ResolveExport(const char* symbol, void** ptr, bool logging = true) override;
  bool IsSystemDll() override;
  HMODULE GetHModule() override;
  bool HasSymbols() override;

private:
  int m_handle = -1;
  bool m_releaseFailed = false;
};
