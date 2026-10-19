/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "PictureInfoLoader.h"

#include "FileItem.h"
#include "FileItemList.h"
#include "PictureInfoTag.h"
#include "ServiceBroker.h"
#include "URL.h"
#include "network/NetworkFileItemClassify.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/log.h"
#include "video/VideoFileItemClassify.h"

#include <chrono>

using namespace KODI;

CPictureInfoLoader::CPictureInfoLoader()
{
  m_mapFileItems = new CFileItemList;
  m_tagReads = 0;
}

CPictureInfoLoader::~CPictureInfoLoader()
{
  StopThread();
  delete m_mapFileItems;
}

void CPictureInfoLoader::OnLoaderStart()
{
  // Load previously cached items from HD
  m_mapFileItems->SetPath(m_pVecItems->GetPath());
  m_mapFileItems->Load();
  m_mapFileItems->SetFastLookup(true);

  m_tagReads = 0;
  m_loadTags = CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(CSettings::SETTING_PICTURES_USETAGS);

#if defined(TARGET_PS5)
  CLog::Log(LOGDEBUG, "[kodi-ps5] Picture metadata: phase=begin path='{:.256}' items={} tags={}",
            CURL::GetRedacted(m_pVecItems->GetPath()), m_pVecItems->Size(), m_loadTags);
#endif

  if (m_pProgressCallback)
    m_pProgressCallback->SetProgressMax(m_pVecItems->GetFileCount());
}

bool CPictureInfoLoader::LoadItem(CFileItem* pItem)
{
  bool result  = LoadItemCached(pItem);
       result |= LoadItemLookup(pItem);

  return result;
}

bool CPictureInfoLoader::LoadItemCached(CFileItem* pItem)
{
  if (!pItem->IsPicture() || pItem->IsZIP() || pItem->IsRAR() || pItem->IsCBR() || pItem->IsCBZ() ||
      NETWORK::IsInternetStream(*pItem) || VIDEO::IsVideo(*pItem))
    return false;

  if (pItem->HasPictureInfoTag())
    return true;

  // Check the cached item
  CFileItemPtr mapItem = (*m_mapFileItems)[pItem->GetPath()];
  if (mapItem && mapItem->HasPictureInfoTag() && mapItem->GetDateTime() == pItem->GetDateTime())
  { // Query map if we previously cached the file on HD
    *pItem->GetPictureInfoTag() = *mapItem->GetPictureInfoTag();
    pItem->SetArt("thumb", mapItem->GetArt("thumb"));
    return true;
  }

  return true;
}

bool CPictureInfoLoader::LoadItemLookup(CFileItem* pItem)
{
  if (m_pProgressCallback && !pItem->IsFolder())
    m_pProgressCallback->SetProgressAdvance();

  if (!pItem->IsPicture() || pItem->IsZIP() || pItem->IsRAR() || pItem->IsCBR() || pItem->IsCBZ() ||
      NETWORK::IsInternetStream(*pItem) || VIDEO::IsVideo(*pItem))
    return false;

  if (pItem->HasPictureInfoTag())
    return false;

  if (m_loadTags)
  { // Nothing found, load tag from file
#if defined(TARGET_PS5)
    const auto start = std::chrono::steady_clock::now();
    CLog::Log(LOGDEBUG,
              "[kodi-ps5] Picture metadata: phase=read_begin path='{:.256}' listed_bytes={} "
              "item_folder={} read={}",
              CURL::GetRedacted(pItem->GetPath()), pItem->GetSize(), pItem->IsFolder(), m_tagReads + 1);
    const bool loaded = pItem->GetPictureInfoTag()->Load(pItem->GetPath());
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - start);
    CLog::Log(LOGDEBUG,
              "[kodi-ps5] Picture metadata: phase=read_end path='{:.256}' loaded={} elapsed_ms={}",
              CURL::GetRedacted(pItem->GetPath()), loaded, elapsed.count());
#else
    pItem->GetPictureInfoTag()->Load(pItem->GetPath());
#endif
    m_tagReads++;
  }

  return true;
}

void CPictureInfoLoader::OnLoaderFinish()
{
  // cleanup cache loaded from HD
  m_mapFileItems->Clear();

  // Save loaded items to HD
  if (!m_bStop && m_tagReads > 0)
    m_pVecItems->Save();

#if defined(TARGET_PS5)
  const bool stopped = m_bStop;
  CLog::Log(LOGDEBUG, "[kodi-ps5] Picture metadata: phase=end path='{:.256}' reads={} stopped={}",
            CURL::GetRedacted(m_pVecItems->GetPath()), m_tagReads, stopped);
#endif
}
