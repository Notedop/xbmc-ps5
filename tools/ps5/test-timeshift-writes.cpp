/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Host fixture compiled by test-addon-build.py with the actual patched classes/methods.
 */

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <sys/types.h>
#include <kodi/c-api/addon-instance/inputstream/demux_packet.h>
#include "../utils/Log.h"

std::atomic<unsigned int> errors{0};
void Log(const LogLevel level, const char*, ...)
{
  if (level == LOGLEVEL_ERROR)
    ++errors;
}

namespace kodi
{
namespace vfs
{
bool FileExists(const std::string&) { return false; }
class CFile
{
public:
  bool opened = false, block = false, blocked = false, seekFails = false;
  ssize_t result = -2;
  size_t maxWrite = 0, position = 0;
  std::vector<uint8_t> bytes;
  std::vector<size_t> requests;
  std::mutex mutex;
  std::condition_variable condition;

  bool OpenFileForWrite(const std::string&, bool)
  {
    opened = true;
    return true;
  }
  bool IsOpen() { return opened; }
  void Close() { opened = false; }
  int64_t Seek(int64_t offset)
  {
    if (seekFails)
      return -1;
    position = offset;
    return offset;
  }
  ssize_t Write(const void* data, size_t size)
  {
    std::unique_lock<std::mutex> lock(mutex);
    requests.push_back(size);
    if (block)
    {
      blocked = true;
      condition.notify_all();
      condition.wait(lock, [&] { return !block; });
    }
    if (result != -2)
      return result;
    const size_t count = maxWrite ? std::min(size, maxWrite) : size;
    if (position + count > bytes.size())
      bytes.resize(position + count);
    if (count)
      std::memcpy(bytes.data() + position, data, count);
    position += count;
    return count;
  }
  void WaitBlocked()
  {
    std::unique_lock<std::mutex> lock(mutex);
    assert(condition.wait_for(lock, std::chrono::seconds(5), [&] { return blocked; }));
  }
  void Release()
  {
    std::lock_guard<std::mutex> lock(mutex);
    block = false;
    condition.notify_all();
  }
};
}
namespace tools
{
struct StringUtils
{
  static std::string Format(const char* format, ...)
  {
    char text[4096];
    va_list args;
    va_start(args, format);
    const int size = vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    assert(size >= 0 && static_cast<size_t>(size) < sizeof(text));
    return text;
  }
};
}
}
using kodi::tools::StringUtils;
struct CURL
{
  static std::string GetRedacted(const std::string& path) { return path; }
};
struct DiskUtils
{
  static bool GetFreeDiskSpaceMB(const std::string&, uint64_t&) { return false; }
};
struct AVPacketSideData { uint8_t* data; size_t size; int type; };
struct DEMUX_CRYPTO_INFO
{
  unsigned int numSubSamples = 0;
  uint16_t flags = 0;
  uint16_t* clearBytes = nullptr;
  uint32_t* cipherBytes = nullptr;
  uint8_t iv[16]{}, kid[16]{};
};
struct IManageDemuxPacket
{
  DEMUX_PACKET* AllocateDemuxPacketFromInputStreamAPI(int size)
  {
    auto* packet = new DEMUX_PACKET{};
    packet->iSize = size;
    packet->pData = new uint8_t[size];
    return packet;
  }
  void FreeDemuxPacketFromInputStreamAPI(DEMUX_PACKET* packet)
  {
    assert(!packet->cryptoInfo && !packet->iSideDataElems);
    delete[] packet->pData;
    delete packet;
  }
};

// INSERT_ACTUAL_CLASSES
// INSERT_ACTUAL_METHODS

void TimeshiftSegment::CopySideData(DEMUX_PACKET* source, DEMUX_PACKET* target)
{
  assert(source->iSideDataElems == 0);
  target->iSideDataElems = 0;
  target->pSideData = nullptr;
}
void TimeshiftSegment::FreeSideData(std::shared_ptr<DEMUX_PACKET>& packet)
{
  assert(packet->iSideDataElems == 0);
}
void TimeshiftSegment::LoadSegment() { assert(m_loaded); }
void TimeshiftSegment::ForceLoadSegment() { assert(false && "unexpected disk fallback"); }
TimeshiftBuffer::TimeshiftBuffer(IManageDemuxPacket* manager)
  : m_demuxPacketManager(manager)
{
  m_timeshiftBufferPath = "fixture";
  m_currentDemuxTimeIndex = 0;
}
TimeshiftBuffer::~TimeshiftBuffer() = default;
void TimeshiftBuffer::RemoveOldestInMemoryAndOnDiskSegments() { assert(false); }
SegmentIndexOnDiskEntry TimeshiftBuffer::SearchOnDiskIndex(const SegmentIndexSearchBy&, int)
{
  return {};
}

IManageDemuxPacket manager;
std::unique_ptr<TimeshiftSegment> MakeSegment()
{
  return std::unique_ptr<TimeshiftSegment>(
      new TimeshiftSegment(&manager, "fixture", 0, "fixture"));
}
DEMUX_PACKET* Packet(size_t size, double seconds)
{
  auto* packet = manager.AllocateDemuxPacketFromInputStreamAPI(size);
  std::memset(packet->pData, 0x5a, size);
  packet->pts = packet->dts = seconds * STREAM_TIME_BASE;
  return packet;
}

int main()
{
  auto measured = MakeSegment();
  auto original = MakeSegment();
  int32_t count = 0;
  original->m_fileHandle.Write(&count, sizeof(count));
  std::vector<uint8_t> payload(8192, 0xa5);
  AVPacketSideData sideData[] = {{payload.data(), 3, 5}, {payload.data(), 0, 6}};
  uint16_t clear[] = {1, 2};
  uint32_t cipher[] = {3, 4};
  DEMUX_CRYPTO_INFO crypto;
  crypto.clearBytes = clear;
  crypto.cipherBytes = cipher;
  auto packet = std::make_shared<DEMUX_PACKET>();
  packet->pData = payload.data();
  packet->iSize = payload.size();
  packet->pSideData = sideData;
  for (int i = 0; i < 1000; ++i)
  {
    packet->iSideDataElems = i % 4 ? 2 : 0;
    packet->cryptoInfo = i % 4 >= 2 ? &crypto : nullptr;
    crypto.numSubSamples = i % 4 == 3 ? 2 : 0;
    measured->AppendWriteBuffer(&count, sizeof(count));
    measured->WritePacket(packet);
    original->m_fileHandle.Write(&count, sizeof(count));
    original->BaselineWritePacket(packet);
    ++count;
    assert(measured->m_writeBuffer.size() < measured->WRITE_BUFFER_SIZE);
  }
  measured->m_currentPacketIndex = count;
  assert(measured->FinishWrite());
  original->m_fileHandle.Seek(0);
  original->m_fileHandle.Write(&count, sizeof(count));
  assert(measured->m_fileHandle.bytes == original->m_fileHandle.bytes);
  assert(measured->m_fileHandle.requests.size() ==
         (measured->m_fileHandle.bytes.size() + measured->WRITE_BUFFER_SIZE - 1) /
             measured->WRITE_BUFFER_SIZE + 1);
  assert(measured->m_fileHandle.requests.size() * 250 < original->m_fileHandle.requests.size());
  for (auto size : measured->m_fileHandle.requests)
    assert(size <= measured->WRITE_BUFFER_SIZE);
  assert(measured->m_writeBuffer.capacity() == 0);
  const auto writes = measured->m_fileHandle.requests.size();
  measured->MarkAsComplete();
  assert(measured->m_fileHandle.requests.size() == writes);

  auto oversized = MakeSegment();
  oversized->AddPacket(Packet(oversized->WRITE_BUFFER_SIZE * 3 + 17, 1));
  oversized->MarkAsComplete();
  assert(oversized->m_persisted && oversized->m_completed);
  for (auto size : oversized->m_fileHandle.requests)
    assert(size <= oversized->WRITE_BUFFER_SIZE);

  auto partial = MakeSegment();
  partial->m_fileHandle.maxWrite = 3;
  partial->AddPacket(Packet(17, 1));
  partial->MarkAsComplete();
  assert(partial->m_persisted && partial->m_fileHandle.bytes.size() > 17);
  int32_t header;
  std::memcpy(&header, partial->m_fileHandle.bytes.data(), sizeof(header));
  assert(header == 1);
  auto complete = MakeSegment();
  complete->AddPacket(Packet(17, 1));
  complete->MarkAsComplete();
  assert(partial->m_fileHandle.bytes == complete->m_fileHandle.bytes);

  for (int failure = 0; failure < 3; ++failure)
  {
    const auto previousErrors = errors.load();
    auto failed = MakeSegment();
    failed->AddPacket(Packet(17, 1));
    if (failure < 2)
      failed->m_fileHandle.result = failure == 0 ? -1 : 0;
    else
      failed->m_fileHandle.seekFails = true;
    failed->MarkAsComplete();
    assert(errors > previousErrors && !failed->m_persisted && failed->m_completed);
    failed->ClearPackets();
    assert(failed->Seek(1000));
    auto* read = failed->ReadPacket();
    assert(read->iSize == 17);
    manager.FreeDemuxPacketFromInputStreamAPI(read);
  }

  auto blocked = MakeSegment();
  blocked->m_fileHandle.block = true;
  std::thread writer([&] { blocked->AddPacket(Packet(blocked->WRITE_BUFFER_SIZE + 1, 1)); });
  blocked->m_fileHandle.WaitBlocked();
  auto reader = std::async(std::launch::async, [&] {
    assert(blocked->Seek(1000));
    auto* read = blocked->ReadPacket();
    assert(read->iSize == static_cast<int>(blocked->WRITE_BUFFER_SIZE + 1));
    assert(read->pData[0] == 0x5a);
    manager.FreeDemuxPacketFromInputStreamAPI(read);
  });
  assert(reader.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
  reader.get();
  assert(!blocked->ReadAllPackets());
  blocked->m_fileHandle.Release();
  writer.join();
  blocked->MarkAsComplete();
  assert(blocked->ReadAllPackets());

  TimeshiftBuffer buffer(&manager);
  assert(buffer.Start("fixture"));
  buffer.AddPacket(Packet(17, 1));
  auto previous = buffer.m_writeSegment;
  previous->m_fileHandle.block = true;
  std::thread rollover([&] { buffer.AddPacket(Packet(17, 13)); });
  previous->m_fileHandle.WaitBlocked();
  auto playback = std::async(std::launch::async, [&] {
    assert(buffer.Seek(1000));
    auto* read = buffer.ReadPacket();
    assert(read->pts == STREAM_TIME_BASE);
    manager.FreeDemuxPacketFromInputStreamAPI(read);
  });
  assert(playback.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
  playback.get();
  assert(!previous->m_completed);
  previous->m_fileHandle.Release();
  rollover.join();
  assert(previous->m_completed && previous->GetNextSegment() == buffer.m_writeSegment);
  auto* empty = buffer.ReadPacket();
  assert(empty->iSize == 0);
  manager.FreeDemuxPacketFromInputStreamAPI(empty);
  auto* next = buffer.ReadPacket();
  assert(next->pts == 13 * STREAM_TIME_BASE);
  manager.FreeDemuxPacketFromInputStreamAPI(next);
  buffer.FinishWrite();
  assert(buffer.m_writeSegment->m_persisted);

  TimeshiftBuffer failedBuffer(&manager);
  assert(failedBuffer.Start("failed"));
  failedBuffer.AddPacket(Packet(17, 1));
  auto failedHistory = failedBuffer.m_writeSegment;
  failedHistory->m_fileHandle.result = -1;
  failedBuffer.AddPacket(Packet(17, 13));
  const std::string index(failedBuffer.m_segmentIndexFileHandle.bytes.begin(),
                          failedBuffer.m_segmentIndexFileHandle.bytes.end());
  assert(index.size() == 30 && index.substr(0, 9) == "       -1");
  auto* first = failedBuffer.ReadPacket();
  manager.FreeDemuxPacketFromInputStreamAPI(first);
  assert(!failedHistory->m_packetBuffer.empty());
  assert(failedBuffer.Seek(1000));
  auto* retained = failedBuffer.ReadPacket();
  assert(retained->iSize == 17);
  manager.FreeDemuxPacketFromInputStreamAPI(retained);
  failedBuffer.FinishWrite();

  for (bool fail : {false, true})
  {
    TimeshiftBuffer indexBuffer(&manager);
    assert(indexBuffer.Start("index"));
    indexBuffer.AddPacket(Packet(17, 1));
    indexBuffer.m_segmentIndexFileHandle.maxWrite = 3;
    if (fail)
      indexBuffer.m_segmentIndexFileHandle.result = 0;
    const auto previousErrors = errors.load();
    indexBuffer.AddPacket(Packet(17, 13));
    if (fail)
      assert(!indexBuffer.m_segmentIndexFileHandle.IsOpen() && errors > previousErrors);
    else
      assert(indexBuffer.m_segmentIndexFileHandle.bytes.size() == 30);
    assert(indexBuffer.m_writeSegment->HasPacketAvailable());
    indexBuffer.FinishWrite();
  }
}
