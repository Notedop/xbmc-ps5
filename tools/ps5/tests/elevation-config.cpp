#include "platform/ps5/ElevationConfig.h"

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

using namespace KODI::PLATFORM::PS5;

namespace
{
std::string Join(const std::vector<ElevationMethodId>& order)
{
  std::string names;
  for (const auto& id : order)
  {
    if (!names.empty())
      names += ",";
    names += ElevationMethodName(id);
  }
  return names;
}

void ExpectStatus(ElevationConfigStatus expected, ElevationConfigStatus actual,
                  const std::string& json, const std::string& what)
{
  if (expected != actual)
  {
    std::printf("FAIL %s: status %d (want %d) for %s\n", what.c_str(), static_cast<int>(actual),
                static_cast<int>(expected), json.c_str());
    std::fflush(stdout);
    std::abort();
  }
}

void ExpectOrder(const std::string& expected, const std::vector<ElevationMethodId>& order,
                 const std::string& what)
{
  if (Join(order) != expected)
  {
    std::printf("FAIL %s: order \"%s\" (want \"%s\")\n", what.c_str(), Join(order).c_str(),
                expected.c_str());
    std::fflush(stdout);
    std::abort();
  }
}

void TestConfigFiles()
{
  char temporary[] = "/tmp/kodi-elevation-config-XXXXXX";
  const char* directory = mkdtemp(temporary);
  assert(directory);
  struct Cleanup
  {
    std::filesystem::path path;
    ~Cleanup() { std::filesystem::remove_all(path); }
  } cleanup{directory};
  const std::string config = std::string(directory) + "/config.json";
  const std::string defaults = std::string(directory) + "/default-config.json";
  const auto write = [](const std::string& path, const std::string& body)
  {
    std::ofstream file(path);
    file << body;
    file.close();
    assert(file);
  };
  std::vector<ElevationMethodId> order;
  std::string error = "previous error";
  const auto load = [&](ElevationConfigStatus expected, const std::string& what)
  {
    ExpectStatus(expected, LoadElevationConfig(config, defaults, order, error), "", what);
  };

  load(ElevationConfigStatus::defaults, "both files missing");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "missing files default");
  assert(error.empty());

  write(defaults, R"({"elevation":{"methods":[{"id":"jailbreak_marker"}]}})");
  load(ElevationConfigStatus::configured, "packaged fallback");
  ExpectOrder("jailbreak_marker", order, "packaged fallback");

  write(config, R"({"elevation":{"methods":[{"id":"etahen_ipc"}]}})");
  load(ElevationConfigStatus::configured, "user config takes precedence");
  ExpectOrder("etahen_ipc", order, "user config takes precedence");

  write(defaults, "{");
  load(ElevationConfigStatus::configured, "unused invalid fallback");
  ExpectOrder("etahen_ipc", order, "unused invalid fallback");

  write(defaults, R"({"elevation":{"methods":[{"id":"jailbreak_marker"}]}})");
  write(config, "{}");
  load(ElevationConfigStatus::defaults, "user config without elevation does not fall back");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "user config without elevation");

  write(config, R"({"elevation":{"methods":[]}})");
  load(ElevationConfigStatus::configured, "user config disables all methods");
  ExpectOrder("", order, "user config disables all methods");

  write(config, "{");
  load(ElevationConfigStatus::error, "invalid user config does not fall back");
  assert(error.starts_with(config + ":"));

  write(config, "");
  load(ElevationConfigStatus::error, "empty user config does not fall back");

  write(config, std::string(64u * 1024u + 1, ' '));
  load(ElevationConfigStatus::error, "oversized user config does not fall back");
  assert(error == config + ": exceeds 64 KiB");

  ExpectStatus(ElevationConfigStatus::error,
               LoadElevationConfig(config + "/config.json", defaults, order, error), "",
               "invalid parent is not a missing user config");

  std::filesystem::remove(config);
  std::filesystem::create_directory(config);
  load(ElevationConfigStatus::error, "unreadable user config does not fall back");
  assert(error.starts_with(config + ":"));
  std::filesystem::remove(config);

  write(defaults, "{");
  load(ElevationConfigStatus::error, "invalid packaged config");
  assert(error.starts_with(defaults + ":"));
  std::filesystem::remove(defaults);
  load(ElevationConfigStatus::defaults, "missing files reset a previously configured order");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "missing files reset order");
  assert(error.empty());
}
} // namespace

int main()
{
  std::vector<ElevationMethodId> order;
  std::string error;

  ExpectStatus(ElevationConfigStatus::defaults,
               ParseElevationConfig("{}", order, error), "{}", "empty object");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "empty object default");

  ExpectStatus(ElevationConfigStatus::defaults,
               ParseElevationConfig(R"({"title": "other"})", order, error), "no elevation",
               "missing elevation section");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "no elevation default");

  ExpectStatus(ElevationConfigStatus::defaults,
               ParseElevationConfig(R"({"elevation": {}})", order, error), "no methods",
               "elevation without methods");
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", order, "no methods default");

  ExpectStatus(
      ElevationConfigStatus::configured,
      ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "etahen_ipc" },
          { "id": "elf_9021", "enabled": true }]}})", order, error),
      "custom order", "explicit enabled defaults to true");
  ExpectOrder("etahen_ipc,elf_9021", order, "custom order");

  ExpectStatus(
      ElevationConfigStatus::configured,
      ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "elf_9021", "enabled": false },
          { "id": "jailbreak_marker" },
          { "id": "etahen_ipc" }]}})", order, error),
      "disabled method", "disabled entry dropped, others keep order");
  ExpectOrder("jailbreak_marker,etahen_ipc", order, "disabled method");

  ExpectStatus(ElevationConfigStatus::configured,
               ParseElevationConfig(R"({"elevation": {"methods": []}})", order, error), "empty",
               "empty list disables all methods");
  ExpectOrder("", order, "empty list");

  ExpectStatus(
      ElevationConfigStatus::configured,
      ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "elf_9021", "enabled": false },
          { "id": "etahen_ipc", "enabled": false },
          { "id": "jailbreak_marker", "enabled": false }]}})", order, error),
      "all disabled", "all-disabled stays configured, not defaults");
  ExpectOrder("", order, "all disabled");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "sudo_everything" }]}})", order, error),
      "unknown id", "unknown method id is an error");
  if (error.find("sudo_everything") == std::string::npos)
  {
    std::printf("FAIL unknown id: error text lacks the id: %s\n", error.c_str());
    std::abort();
  }

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "elf_9021" },
          { "id": "elf_9021" }]}})", order, error),
      "duplicate id", "duplicate method id is an error");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "elf_9021", "enabled": "yes" }]}})", order, error),
      "bad enabled", "non-boolean enabled is an error");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": [
          "elf_9021"]}})", order, error),
      "entry not object", "non-object method entry is an error");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": {}}})", order, error),
      "methods not array", "non-array methods is an error");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": []})", order, error), "elevation not object",
               "non-object elevation is an error");

  ExpectStatus(ElevationConfigStatus::error, ParseElevationConfig("{", order, error),
               "malformed", "malformed JSON is an error");

  ExpectStatus(ElevationConfigStatus::error, ParseElevationConfig("", order, error), "empty file",
               "empty file is malformed, not defaults");

  ExpectStatus(ElevationConfigStatus::error,
               ParseElevationConfig(R"({"elevation": {"methods": [
          { "id": "etahen_ipc" },
          { "id": "made_up", "enabled": false }]}})", order, error),
      "unknown disabled", "unknown id is an error even when disabled");

  TestConfigFiles();
  std::printf("elevation config tests passed\n");
  return 0;
}
