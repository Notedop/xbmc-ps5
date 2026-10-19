/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecFFmpeg.h"
#include "cores/VideoPlayer/Process/ProcessInfo.h"

#include <memory>

#include <gtest/gtest.h>

namespace
{
class CReopenTestCodec : public CDVDVideoCodecFFmpeg
{
public:
  explicit CReopenTestCodec(CProcessInfo& processInfo) : CDVDVideoCodecFFmpeg(processInfo) {}

  bool Open(CDVDStreamInfo&, CDVDCodecOptions&) override
  {
    if (failOpen)
      return false;

    m_pCodecContext = avcodec_alloc_context3(nullptr);
    return m_pCodecContext != nullptr;
  }

  void SetInputStarted() { m_startedInput = true; }
  bool IsOpen() const { return m_pCodecContext != nullptr; }
  bool HasStartedInput() const { return m_startedInput; }
  bool failOpen = false;
};

#if defined(TARGET_PS5)
class CFilterThreadTestCodec : public CReopenTestCodec
{
public:
  explicit CFilterThreadTestCodec(CProcessInfo& processInfo) : CReopenTestCodec(processInfo) {}

  int OpenScalingFilter()
  {
    m_pCodecContext->width = 64;
    m_pCodecContext->height = 64;
    m_pCodecContext->pix_fmt = AV_PIX_FMT_YUVJ420P;
    m_pCodecContext->time_base = {1, 25};
    m_pCodecContext->sample_aspect_ratio = {1, 1};
    m_formats = {AV_PIX_FMT_YUV420P};
    return FilterOpen("", true);
  }

  int FilterThreadCount() const { return m_pFilterGraph->nb_threads; }
};
#endif
} // namespace

TEST(TestDVDVideoCodecFFmpeg, UnopenedDecoderReportsFatalBeforeRequestingInput)
{
  std::unique_ptr<CProcessInfo> processInfo(CProcessInfo::CreateInstance());
  CReopenTestCodec codec(*processInfo);
  VideoPicture picture{};

  EXPECT_EQ(CDVDVideoCodec::VC_FATAL, codec.GetPicture(&picture));
  codec.Reset();
  EXPECT_EQ(CDVDVideoCodec::VC_FATAL, codec.GetPicture(&picture));
}

TEST(TestDVDVideoCodecFFmpeg, FailedReopenCannotDecodeOrResetFreedContext)
{
  std::unique_ptr<CProcessInfo> processInfo(CProcessInfo::CreateInstance());
  CReopenTestCodec codec(*processInfo);
  codec.Reopen();
  ASSERT_TRUE(codec.IsOpen());
  codec.SetInputStarted();
  codec.failOpen = true;
  codec.Reopen();

  EXPECT_FALSE(codec.IsOpen());
  EXPECT_FALSE(codec.HasStartedInput());
  VideoPicture picture{};
  EXPECT_EQ(CDVDVideoCodec::VC_FATAL, codec.GetPicture(&picture));
  codec.Reset();
  EXPECT_EQ(CDVDVideoCodec::VC_FATAL, codec.GetPicture(&picture));
}

TEST(TestDVDVideoCodecFFmpeg, SuccessfulReopenWaitsForFreshInput)
{
  std::unique_ptr<CProcessInfo> processInfo(CProcessInfo::CreateInstance());
  CReopenTestCodec codec(*processInfo);
  codec.Reopen();
  ASSERT_TRUE(codec.IsOpen());
  codec.SetInputStarted();
  codec.Reopen();

  EXPECT_TRUE(codec.IsOpen());
  EXPECT_FALSE(codec.HasStartedInput());
  VideoPicture picture{};
  EXPECT_EQ(CDVDVideoCodec::VC_BUFFER, codec.GetPicture(&picture));
}

#if defined(TARGET_PS5)
TEST(TestDVDVideoCodecFFmpeg, SoftwareScalingFilterUsesSixThreadLimitAfterRecreation)
{
  std::unique_ptr<CProcessInfo> processInfo(CProcessInfo::CreateInstance());
  CFilterThreadTestCodec codec(*processInfo);
  codec.Reopen();
  ASSERT_TRUE(codec.IsOpen());

  ASSERT_GE(codec.OpenScalingFilter(), 0);
  EXPECT_EQ(6, codec.FilterThreadCount());
  ASSERT_GE(codec.OpenScalingFilter(), 0);
  EXPECT_EQ(6, codec.FilterThreadCount());
}
#endif
