#!/usr/bin/env python3
"""Exercise production directory ownership code with overlapping listing requests."""

import ps5_test_paths
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = ps5_test_paths.ROOT


def block(text, signature):
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


header = (ROOT / "xbmc/filesystem/VirtualDirectory.h").read_text()
source = (ROOT / "xbmc/filesystem/VirtualDirectory.cpp").read_text()
media = (ROOT / "xbmc/windows/GUIMediaWindow.cpp").read_text()
media_load = block(media, "bool CGUIMediaWindow::GetDirectoryItems(")
assert "getItems.GetDirImpl()" in media_load
assert "m_rootDir.GetDirImpl()" not in media_load
assert "m_rootDir.ReleaseDirImpl()" not in media_load

declarations = "\n".join(
    block(header, signature) + ";"
    for signature in (
        "class CVirtualDirectory :",
        "class CGetDirectoryItems final :",
    )
)
methods = "\n".join(
    block(source, signature)
    for signature in (
        "CVirtualDirectory::CVirtualDirectory(void)",
        "CVirtualDirectory::CVirtualDirectory(const CVirtualDirectory&",
        "void CVirtualDirectory::SetSources(",
        "bool CVirtualDirectory::GetDirectory(const CURL& url, CFileItemList &items)",
        "bool CVirtualDirectory::GetDirectory(const CURL& url, CFileItemList &items, bool",
        "void CVirtualDirectory::CancelDirectory()",
        "std::shared_ptr<IDirectory> CVirtualDirectory::GetDirImpl()",
        "void CVirtualDirectory::ReleaseDirImpl()",
        "void CVirtualDirectory::GetSources(",
    )
)

fixture = r"""
#include <atomic>
#include <cassert>
#include <chrono>
#include <deque>
#include <future>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
using CMediaSource = int;
struct CURL
{
  std::string path;
  std::string Get() const { return path; }
};
struct CFileItemList
{
  std::vector<CMediaSource> sources;
  void Clear() { sources.clear(); }
  void SetPath(const std::string&) {}
};
struct IRunnable
{
  virtual ~IRunnable() = default;
  virtual void Run() = 0;
  virtual void Cancel() = 0;
};
struct State
{
  std::promise<void> started;
  std::promise<void> release;
  std::shared_future<void> gate{release.get_future()};
  std::atomic<int> fetches{0};
  std::atomic<int> cancellations{0};
  std::atomic<int> destructions{0};
  std::string mask;
  int flags{0};
};
namespace XFILE
{
constexpr int DIR_FLAG_ALLOW_PROMPT = 4;
constexpr int DIR_FLAG_NO_FILE_DIRS = 2;
class IDirectory
{
public:
  virtual ~IDirectory() = default;
  virtual bool GetDirectory(const CURL&, CFileItemList&) = 0;
  virtual void CancelDirectory() {}
  virtual bool AllowAll() const { return true; }
  void SetMask(const std::string& mask) { m_strFileMask = mask; }
  void SetFlags(int flags) { m_flags = flags; }
protected:
  std::string m_strFileMask;
  int m_flags{0};
};
class FakeDirectory : public IDirectory
{
public:
  explicit FakeDirectory(std::shared_ptr<State> state) : m_state(std::move(state)) {}
  ~FakeDirectory() override { ++m_state->destructions; }
  bool GetDirectory(const CURL&, CFileItemList&) override
  {
    if (m_state->fetches++ == 0)
      m_state->started.set_value();
    m_state->gate.wait();
    return true;
  }
  void CancelDirectory() override { ++m_state->cancellations; }
  std::shared_ptr<State> m_state;
};
struct CDirectoryFactory
{
  static inline std::deque<std::shared_ptr<State>> states;
  static IDirectory* Create(const CURL&)
  {
    assert(!states.empty());
    auto state = states.front();
    states.pop_front();
    return new FakeDirectory(std::move(state));
  }
};
struct CDirectory
{
  static bool GetDirectory(const std::string&, const std::shared_ptr<IDirectory>& directory,
                           CFileItemList& items, const std::string& mask, int flags)
  {
    auto state = static_cast<FakeDirectory*>(directory.get())->m_state;
    state->mask = mask;
    state->flags = flags;
    return directory->GetDirectory(CURL{}, items) && directory->AllowAll();
  }
};
struct CSourcesDirectory
{
  bool GetDirectory(const std::vector<CMediaSource>& shares, CFileItemList& items)
  {
    items.sources = shares;
    return true;
  }
};
DECLARATIONS
}
struct URIUtils
{
  static CURL SubstitutePath(const CURL& url) { return url; }
};
struct CServiceBroker
{
  static CServiceBroker& GetMediaManager()
  {
    static CServiceBroker instance;
    return instance;
  }
  void GetRemovableDrives(std::vector<CMediaSource>&) {}
};
using namespace XFILE;
CVirtualDirectory::~CVirtualDirectory(void) = default;
METHODS

std::shared_ptr<State> QueueDirectory(bool ready)
{
  auto state = std::make_shared<State>();
  CDirectoryFactory::states.push_back(state);
  if (ready)
    state->release.set_value();
  return state;
}

void ActiveFetchSurvivesRelease()
{
  CVirtualDirectory root;
  CFileItemList items;
  const CURL url{"test://listing"};
  auto state = QueueDirectory(false);
  auto worker = std::async(std::launch::async, [&] {
    return root.GetDirectory(url, items, false, true);
  });
  assert(state->started.get_future().wait_for(5s) == std::future_status::ready);
  std::weak_ptr<IDirectory> directory = root.GetDirImpl();
  root.ReleaseDirImpl();
  assert(!root.GetDirImpl());
  assert(!directory.expired());
  state->release.set_value();
  assert(worker.get());
  assert(directory.expired());
  assert(state->destructions == 1);
}

void OverlappingRequestsKeepIndependentHandlers()
{
  CVirtualDirectory root;
  root.SetMask(".mkv");
  root.SetFlags(16);
  CFileItemList rootItems, firstItems, secondItems;
  const CURL url{"test://listing"};
  auto rootState = QueueDirectory(true);
  assert(root.GetDirectory(url, rootItems, true, true));
  CGetDirectoryItems first(root, url, firstItems, true, true);
  assert(!first.GetDirImpl());
  auto firstState = QueueDirectory(false);
  auto worker = std::async(std::launch::async, [&] { first.Run(); });
  assert(firstState->started.get_future().wait_for(5s) == std::future_status::ready);
  const auto firstDirectory = first.GetDirImpl();
  auto secondState = QueueDirectory(true);
  {
    CGetDirectoryItems second(root, url, secondItems, false, true);
    assert(!second.GetDirImpl());
    second.Run();
    assert(second.GetResult());
    assert(second.GetDirImpl() != firstDirectory);
    assert(second.GetDirImpl() != root.GetDirImpl());
  }
  assert(secondState->destructions == 1);
  root.ReleaseDirImpl();
  assert(rootState->destructions == 1);
  first.Cancel();
  assert(firstState->cancellations == 1);
  assert(secondState->cancellations == 0);
  assert(rootState->cancellations == 0);
  assert(first.GetDirImpl() == firstDirectory);
  firstState->release.set_value();
  worker.get();
  assert(first.GetResult());
  first.Run();
  assert(first.GetResult());
  assert(first.GetDirImpl() == firstDirectory);
  assert(firstState->fetches == 2);
  assert(firstState->mask == ".mkv");
  assert(firstState->flags == 16);
  assert(secondState->flags == (16 | DIR_FLAG_NO_FILE_DIRS));
}

void NonRetainedRequestReleasesItsHandler()
{
  CVirtualDirectory root;
  CFileItemList items;
  const CURL url{"test://listing"};
  auto state = QueueDirectory(true);
  CGetDirectoryItems request(root, url, items, false, false);
  request.Run();
  assert(request.GetResult());
  assert(!request.GetDirImpl());
  assert(state->destructions == 1);
}

void RootListingCopiesSources()
{
  CVirtualDirectory root;
  root.SetSources({1, 2});
  CFileItemList items;
  const CURL url{};
  CGetDirectoryItems request(root, url, items, false, false);
  root.SetSources({3});
  request.Run();
  assert(request.GetResult());
  assert((items.sources == std::vector<CMediaSource>{1, 2}));
}

int main()
{
  ActiveFetchSurvivesRelease();
  OverlappingRequestsKeepIndependentHandlers();
  NonRetainedRequestReleasesItsHandler();
  RootListingCopiesSources();
  std::cout << "PASS: active lifetime, overlapping requests, cancellation, retry, release, sources\n";
}
"""

with tempfile.TemporaryDirectory(prefix="kodi-directory-lifetime-") as temp:
    directory = Path(temp)
    cpp = directory / "test.cpp"
    binary = directory / "test"
    cpp.write_text(fixture.replace("DECLARATIONS", declarations).replace("METHODS", methods))
    subprocess.run(
        [
            *shlex.split(os.environ.get("CXX", "clang++-18")),
            "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-pthread",
            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
            str(cpp), "-o", str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=30)
