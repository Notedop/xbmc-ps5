#!/usr/bin/env python3
"""Exercise extracted PVR lifecycle code with controlled concurrent workers."""

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def block(text, signature):
    start = text.index(signature)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--addon-source", type=Path, required=True)
args = parser.parse_args()
clients = (ROOT / "xbmc/pvr/addons/PVRClients.cpp").read_text()
update = block(clients, "void CPVRClients::UpdateClients")
lock_line = "  std::unique_lock updateLock(m_updateClientsMutex);\n"
assert lock_line in update
assert update.index(lock_line) < update.index("GetAddonsWithStatus(")
assert "updateLock.unlock" not in update
assert update.rsplit("}", 1)[0].rstrip().endswith(
    "CServiceBroker::GetPVRManager().Start();\n  }")
# The original snapshot path returns false in the fixture; the rest of the
# transaction stays in the same outer lock scope, checked above.
snapshot = update[:update.index("  std::vector<std::shared_ptr<CPVRClient>>")] + "}\n"
manager = (ROOT / "xbmc/pvr/PVRManager.cpp").read_text()
startup = block(manager, "void CPVRManager::Process()")
startup = startup[:startup.index("  bool bRestart(false);")] + "}\n"
manager_methods = "\n".join((
    startup,
    block(manager, "void CPVRManager::UpdateComponents(ManagerState stateToCheck)"),
    block(manager, "bool CPVRManager::UpdateComponents(ManagerState stateToCheck,"),
    block(manager, "void CPVRManager::UnloadComponents()"),
    block(manager, "bool CPVRManager::IsKnownClient(int clientID) const"),
))

fixture = r"""
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <future>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>
using namespace std::chrono_literals;
using AddonInfoPtr = int;
struct State
{
  std::mutex mutex;
  std::condition_variable cv;
  int snapshots = 0;
  bool releaseSnapshot = false;
  bool joinEntered = false;
  bool releaseCallback = false;
  bool demuxersAlive = true;
  bool callbackUsedLiveDemuxers = false;
};
class CPVRClients
{
public:
  explicit CPVRClients(State& state) : state(state) {}
  void UpdateClients(const std::string& changedAddonId = "");
private:
  bool GetAddonsWithStatus(const std::string&, std::vector<std::pair<int, bool>>&)
  {
    std::unique_lock lock(state.mutex);
    ++state.snapshots;
    state.cv.notify_all();
    state.cv.wait(lock, [&] { return state.releaseSnapshot; });
    return false;
  }
  std::recursive_mutex m_updateClientsMutex;
  State& state;
};
class Connection
{
public:
  explicit Connection(State& state) : state(state), worker([&state] {
    std::unique_lock lock(state.mutex);
    state.cv.wait(lock, [&] { return state.releaseCallback; });
    state.callbackUsedLiveDemuxers = state.demuxersAlive;
  }) {}
  ~Connection() { StopThread(); }
  void Stop() {}
  void StopThread()
  {
    {
      std::lock_guard lock(state.mutex);
      state.joinEntered = true;
      state.cv.notify_all();
    }
    if (worker.joinable())
      worker.join();
  }
private:
  State& state;
  std::thread worker;
};
struct Demuxer
{
  State& state;
  ~Demuxer()
  {
    std::lock_guard lock(state.mutex);
    state.demuxersAlive = false;
  }
  void Close() {}
};
class CTvheadend
{
public:
  explicit CTvheadend(State& state)
    : m_dmx{new Demuxer{state}}, m_conn(new Connection(state)) {}
  ~CTvheadend();
  void Stop();
private:
  void StopThread() {}
  std::vector<Demuxer*> m_dmx;
  Connection* m_conn;
};
constexpr int LOGDEBUG = 0, LOGINFO = 1, LOGERROR = 2, LOGPVR = 0;
struct CLog
{
  template<typename... Args> static void Log(Args&&...) {}
  template<typename... Args> static void LogF(Args&&...) {}
  template<typename... Args> static void LogFC(Args&&...) {}
};
struct CPVRGUIProgressHandler
{
  explicit CPVRGUIProgressHandler(const std::string&) {}
  void UpdateProgress(const std::string&, int) {}
};
struct Resources
{
  Resources& GetLocalizeStrings() { return *this; }
  std::string Get(int) { return {}; }
};
struct CServiceBroker
{
  static Resources& GetResourcesComponent()
  {
    static Resources resources;
    return resources;
  }
};
namespace XbmcThreads
{
template<typename = void> struct EndTime
{
  template<typename Duration> explicit EndTime(Duration) {}
  bool IsTimePast() const { return false; }
};
}
struct CThread
{
  static void Sleep(std::chrono::milliseconds) { std::this_thread::yield(); }
};
struct CPVRClient
{
  int id;
  bool ignored = false;
  int GetID() const { return id; }
  bool IgnoreClient() const { return ignored; }
};
using CPVRClientMap = std::map<int, std::shared_ptr<CPVRClient>>;
enum class PVREvent { ClientsInvalidated };
struct Clients
{
  CPVRClientMap clients;
  void Continue() {}
  bool HasCreatedClients() const { return !clients.empty(); }
  CPVRClientMap GetCreatedClients() const { return clients; }
};
struct Component
{
  std::shared_ptr<int> allChannelsGroup;
  std::function<void()> onUpdate;
  int updates = 0;
  bool Update(const std::vector<std::shared_ptr<CPVRClient>>&)
  {
    ++updates;
    allChannelsGroup = std::make_shared<int>(1);
    if (onUpdate)
      onUpdate();
    return true;
  }
  void Unload() { allChannelsGroup.reset(); }
  void Open() {}
  void ReInit() {}
  void Start() {}
};
class CPVRManager
{
public:
  enum class ManagerState { STATE_STARTING, STATE_STOPPING, STATE_STARTED };
  void Process();
  void UpdateComponents(ManagerState stateToCheck);
  bool UpdateComponents(ManagerState stateToCheck, CPVRGUIProgressHandler* progressHandler);
  void UnloadComponents();
  bool IsKnownClient(int clientID) const;
  ManagerState GetState() const { return state; }
  bool IsInitialising() const { return state == ManagerState::STATE_STARTING; }
  void SetState(ManagerState next)
  {
    if (next == ManagerState::STATE_STARTED)
      startedWithChannelGroups = m_channelGroups->allChannelsGroup != nullptr;
    state = next;
  }
  void PublishEvent(PVREvent) {}
  ManagerState state = ManagerState::STATE_STARTING;
  bool startedWithChannelGroups = false;
  std::vector<std::shared_ptr<CPVRClient>> m_knownClients;
  std::shared_ptr<Clients> m_addons = std::make_shared<Clients>();
  std::shared_ptr<Component> m_database = std::make_shared<Component>();
  std::shared_ptr<Component> m_providers = std::make_shared<Component>();
  std::shared_ptr<Component> m_channelGroups = std::make_shared<Component>();
  std::shared_ptr<Component> m_recordings = std::make_shared<Component>();
  std::shared_ptr<Component> m_timers = std::make_shared<Component>();
  std::shared_ptr<Component> m_epgContainer = std::make_shared<Component>();
  std::shared_ptr<Component> m_guiInfo = std::make_shared<Component>();
  std::shared_ptr<Component> m_playbackState = std::make_shared<Component>();
  std::shared_ptr<Component> m_pendingUpdates = std::make_shared<Component>();
};
@METHODS@
bool TestSerialization()
{
  State state;
  CPVRClients clients(state);
  auto first = std::async(std::launch::async, [&] { clients.UpdateClients(); });
  {
    std::unique_lock lock(state.mutex);
    state.cv.wait(lock, [&] { return state.snapshots == 1; });
  }
  std::promise<void> launched;
  auto second = std::async(std::launch::async, [&] {
    launched.set_value();
    clients.UpdateClients();
  });
  launched.get_future().wait();
  bool overlapped;
  {
    std::unique_lock lock(state.mutex);
    overlapped = state.cv.wait_for(lock, 100ms, [&] { return state.snapshots > 1; });
    state.releaseSnapshot = true;
    state.cv.notify_all();
  }
  first.get();
  second.get();
  return !overlapped && state.snapshots == 2;
}
bool TestTeardown()
{
  State state;
  auto teardown = std::async(std::launch::async, [&] {
    CTvheadend client(state);
  });
  bool aliveAtJoin;
  {
    std::unique_lock lock(state.mutex);
    state.cv.wait(lock, [&] { return state.joinEntered; });
    aliveAtJoin = state.demuxersAlive;
    state.releaseCallback = true;
    state.cv.notify_all();
  }
  teardown.get();
  return aliveAtJoin && state.callbackUsedLiveDemuxers && !state.demuxersAlive;
}
bool TestAbortedStartup()
{
  using State = CPVRManager::ManagerState;
  CPVRManager manager;
  manager.m_addons->clients.emplace(1, std::make_shared<CPVRClient>(CPVRClient{1}));
  manager.m_providers->onUpdate = [&] { manager.SetState(State::STATE_STOPPING); };
  manager.Process();
  if (manager.GetState() != State::STATE_STOPPING || manager.m_knownClients.size() != 1 ||
      manager.m_channelGroups->allChannelsGroup)
    return false;

  manager.m_addons->clients[1] = std::make_shared<CPVRClient>(CPVRClient{1, true});
  manager.m_providers->onUpdate = {};
  manager.SetState(State::STATE_STARTING);
  manager.UnloadComponents();
  if (!manager.m_knownClients.empty() ||
      manager.UpdateComponents(State::STATE_STARTING, nullptr))
    return false;

  manager.m_addons->clients[1]->ignored = false;
  manager.Process();
  if (manager.GetState() != State::STATE_STARTED || !manager.startedWithChannelGroups ||
      !manager.m_channelGroups->allChannelsGroup || manager.m_channelGroups->updates != 1 ||
      manager.m_knownClients.size() != 1 ||
      manager.m_knownClients.front() != manager.m_addons->clients.at(1))
    return false;

  const int loaded = manager.m_channelGroups->updates;
  if (!manager.UpdateComponents(State::STATE_STARTED, nullptr) ||
      manager.m_channelGroups->updates != loaded)
    return false;

  manager.UnloadComponents();
  manager.UnloadComponents();
  return manager.m_knownClients.empty() && !manager.m_channelGroups->allChannelsGroup;
}
int main()
{
  const bool serialized = TestSerialization();
  const bool joined = TestTeardown();
  const bool restarted = TestAbortedStartup();
  std::cout << "serialized=" << serialized << " callbacksJoinedBeforeFree=" << joined
            << " abortedStartupReloaded=" << restarted << '\n';
  return serialized && joined && restarted ? 0 : 1;
}
"""

with tempfile.TemporaryDirectory(prefix="pvr-lifecycle-") as directory:
    tree = Path(directory)
    source = tree / "src/Tvheadend.cpp"
    source.parent.mkdir()
    shutil.copy2(args.addon_source / "src/Tvheadend.cpp", source)
    patch_command = [
        "cmake", f"-DSOURCE_DIR={tree}",
        f"-DSERIES_DIR={ROOT / 'tools/ps5/patches/addons/pvr.hts'}",
        "-DSERIES_NAME=pvr.hts", "-P",
        str(ROOT / "tools/ps5/apply-patch-series.cmake")]
    subprocess.run(patch_command, check=True)
    patched = source.read_bytes()
    subprocess.run(patch_command, check=True)
    assert source.read_bytes() == patched
    text = source.read_text()
    methods = "\n".join((
        snapshot, block(text, "CTvheadend::~CTvheadend()"),
        block(text, "void CTvheadend::Stop()"), manager_methods))
    compiler = shlex.split(os.environ.get("CXX", "clang++-18"))
    for name, body, expected in (
        ("fixed", methods, 0),
        ("without-serialization", methods.replace(lock_line, ""), 1),
        ("without-connection-join", methods.replace("  m_conn->StopThread();\n", ""), 1),
        ("without-cache-reset", methods.replace(
            block(manager, "void CPVRManager::UnloadComponents()"),
            block(manager, "void CPVRManager::UnloadComponents()").replace(
                "  m_knownClients.clear();\n", "")), 1),
    ):
        cpp = tree / f"{name}.cpp"
        binary = tree / name
        cpp.write_text(fixture.replace("@METHODS@", body))
        subprocess.run(compiler + [
            "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
            str(cpp), "-o", str(binary)], check=True)
        result = subprocess.run([str(binary)], check=False, timeout=10)
        if result.returncode != expected:
            raise RuntimeError(f"{name}: expected exit {expected}, got {result.returncode}")
    print("PASS: serialization, callback lifetime, aborted startup, patch idempotence, and mutations")
