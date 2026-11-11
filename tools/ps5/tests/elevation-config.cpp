#include "platform/ps5/ElevationConfig.h"

#include <cassert>
#include <cstdio>
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

  std::printf("elevation config tests passed\n");
  return 0;
}
