#include "platform/ps5/ConfigPS5.h"

#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

using namespace KODI::PLATFORM::PS5;

namespace
{
void ExpectOrder(const std::string& expected, const Config& config)
{
  std::string names;
  for (const auto id : config.elevationMethods)
  {
    if (!names.empty())
      names += ",";
    names += ElevationMethodName(id);
  }
  if (names != expected)
  {
    std::printf("FAIL order \"%s\" (want \"%s\")\n", names.c_str(), expected.c_str());
    std::abort();
  }
}

Config Parse(const std::string& json)
{
  Config config;
  std::string error = "previous error";
  if (!ParseConfig(json, config, error))
  {
    std::printf("FAIL parse %s: %s\n", json.c_str(), error.c_str());
    std::abort();
  }
  assert(error.empty());
  return config;
}

void TestParsing()
{
  for (const auto* json : {"{}", R"({"title":"other"})", R"({"elevation":{}})"})
  {
    const Config config = Parse(json);
    ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", config);
    assert(!config.debug.enabled && !config.debug.video && !config.debug.addons);
  }
  ExpectOrder("etahen_ipc,elf_9021", Parse(R"({"elevation":{"methods":[
      {"id":"etahen_ipc"},{"id":"elf_9021","enabled":true}]}})"));
  ExpectOrder("jailbreak_marker,etahen_ipc", Parse(R"({"elevation":{"methods":[
      {"id":"elf_9021","enabled":false},{"id":"jailbreak_marker"},{"id":"etahen_ipc"}]}})"));
  ExpectOrder("", Parse(R"({"elevation":{"methods":[]}})"));
  ExpectOrder("", Parse(R"({"elevation":{"methods":[
      {"id":"elf_9021","enabled":false},{"id":"etahen_ipc","enabled":false},
      {"id":"jailbreak_marker","enabled":false}]}})"));

  for (const auto* json :
       {"", "{", "[]", "null", "true", R"({"elevation":[]})",
        R"({"elevation":{"methods":{}}})", R"({"elevation":{"methods":["elf_9021"]}})",
        R"({"elevation":{"methods":[{}]}})", R"({"elevation":{"methods":[{"id":42}]}})",
        R"({"elevation":{"methods":[{"id":"unknown"}]}})",
        R"({"elevation":{"methods":[{"id":"unknown","enabled":false}]}})",
        R"({"elevation":{"methods":[{"id":"elf_9021"},{"id":"elf_9021"}]}})",
        R"({"elevation":{"methods":[{"id":"elf_9021","enabled":"yes"}]}})"})
  {
    Config config;
    std::string error;
    assert(!ParseConfig(json, config, error));
    assert(!error.empty());
  }

  for (const auto* section : {"null", "[]", "true", "1", R"("yes")"})
  {
    Config config;
    std::string error;
    assert(!ParseConfig(std::string(R"({"debug":)") + section + "}", config, error));
    assert(error == "\"debug\" must be an object");
  }
  for (const auto* key : {"enabled", "video", "addons"})
  {
    for (const auto* value : {"null", "[]", "{}", "0", "1", R"("true")"})
    {
      Config config;
      std::string error;
      const std::string json = std::string(R"({"debug":{")") + key + "\":" + value + "}}";
      assert(!ParseConfig(json, config, error));
      assert(error == std::string("debug.") + key + ": must be a boolean");
    }
  }

  const Config video = Parse(R"({"debug":{"video":true}})");
  assert(!video.debug.enabled && video.debug.video && !video.debug.addons);
  const Config addons = Parse(R"({"debug":{"addons":true}})");
  assert(!addons.debug.enabled && !addons.debug.video && addons.debug.addons);
  const Config debug = Parse(R"({"debug":{"enabled":true}})");
  assert(debug.debug.enabled && !debug.debug.video && !debug.debug.addons);
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", debug);
  const Config combined = Parse(R"({"debug":{"enabled":true,"video":true,"addons":true},
                                   "elevation":{"methods":[{"id":"etahen_ipc"}]}})");
  assert(combined.debug.enabled && combined.debug.video && combined.debug.addons);
  ExpectOrder("etahen_ipc", combined);

  Config config = combined;
  std::string error;
  assert(!ParseConfig(R"({"elevation":{"methods":[]},"debug":{"video":"yes"}})", config, error));
  ExpectOrder("etahen_ipc", config);
  assert(config.debug.enabled && config.debug.video && config.debug.addons);
  assert(ParseConfig("{}", config, error));
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", config);
  assert(!config.debug.enabled && !config.debug.video && !config.debug.addons);
}

void TestConfigFiles()
{
  char temporary[] = "/tmp/kodi-config-XXXXXX";
  const char* directory = mkdtemp(temporary);
  assert(directory);
  struct Cleanup
  {
    std::filesystem::path path;
    ~Cleanup() { std::filesystem::remove_all(path); }
  } cleanup{directory};
  const std::string path = std::string(directory) + "/config.json";
  const std::string defaults = std::string(directory) + "/default-config.json";
  const auto write = [](const std::string& filename, const std::string& body)
  {
    std::ofstream file(filename);
    file << body;
    file.close();
    assert(file);
  };
  Config config;
  std::string error = "previous error";
  const auto load = [&](ConfigStatus expected)
  {
    const ConfigStatus actual = LoadConfig(path, defaults, config, error);
    if (expected != actual)
    {
      std::printf("FAIL load: status %d (want %d): %s\n", static_cast<int>(actual),
                  static_cast<int>(expected), error.c_str());
      std::abort();
    }
  };

  load(ConfigStatus::defaults);
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", config);
  assert(error.empty());

  write(defaults, R"({"debug":{"video":true},
                     "elevation":{"methods":[{"id":"jailbreak_marker"}]}})");
  load(ConfigStatus::loaded);
  ExpectOrder("jailbreak_marker", config);
  assert(config.debug.video);

  write(path, R"({"debug":{"addons":true},"elevation":{"methods":[{"id":"etahen_ipc"}]}})");
  load(ConfigStatus::loaded);
  ExpectOrder("etahen_ipc", config);
  assert(config.debug.addons && !config.debug.video);

  write(defaults, "{");
  load(ConfigStatus::loaded);
  ExpectOrder("etahen_ipc", config);
  assert(config.debug.addons);

  write(defaults, R"({"debug":{"video":true},
                     "elevation":{"methods":[{"id":"jailbreak_marker"}]}})");
  write(path, "{}");
  load(ConfigStatus::loaded);
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", config);
  assert(!config.debug.enabled && !config.debug.video && !config.debug.addons);

  write(path, R"({"elevation":{"methods":[]}})");
  load(ConfigStatus::loaded);
  ExpectOrder("", config);

  for (const auto* invalid : {"{", "", R"({"debug":{"enabled":1}})"})
  {
    write(path, invalid);
    load(ConfigStatus::error);
    assert(error.starts_with(path + ":"));
  }

  std::string atLimit = "{}";
  atLimit.resize(64u * 1024u, ' ');
  write(path, atLimit);
  load(ConfigStatus::loaded);
  write(path, atLimit + " ");
  load(ConfigStatus::error);
  assert(error == path + ": exceeds 64 KiB");

  assert(LoadConfig(path + "/config.json", defaults, config, error) == ConfigStatus::error);

  std::filesystem::remove(path);
  std::filesystem::create_directory(path);
  load(ConfigStatus::error);
  assert(error.starts_with(path + ":"));
  std::filesystem::remove(path);

  write(defaults, "{");
  load(ConfigStatus::error);
  assert(error.starts_with(defaults + ":"));
  std::filesystem::remove(defaults);
  config = Parse(R"({"debug":{"enabled":true,"video":true,"addons":true},
                    "elevation":{"methods":[]}})");
  load(ConfigStatus::defaults);
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", config);
  assert(!config.debug.enabled && !config.debug.video && !config.debug.addons);
  assert(error.empty());
}

void TestDebugEnvironment()
{
  const char* names[] = {"KODI_PS5_DEBUG", "KODI_PS5_VIDEO_DEBUG", "KODI_PS5_ADDON_DEBUG"};
  struct SavedVariable
  {
    std::string name;
    bool present;
    std::string value;
    ~SavedVariable()
    {
      if (present)
        assert(setenv(name.c_str(), value.c_str(), 1) == 0);
      else
        assert(unsetenv(name.c_str()) == 0);
    }
  };
  const auto save = [](const char* name)
  {
    const char* value = getenv(name);
    return SavedVariable{name, value != nullptr, value ? value : ""};
  };
  const SavedVariable saved[] = {save(names[0]), save(names[1]), save(names[2])};
  for (unsigned int mask = 0; mask < 8; ++mask)
  {
    for (const auto* name : names)
      assert(setenv(name, "inherited", 1) == 0);
    const DebugConfig config{(mask & 1) != 0, (mask & 2) != 0, (mask & 4) != 0};
    std::string error = "previous error";
    assert(ApplyDebugConfig(config, error));
    assert(error.empty());
    for (unsigned int index = 0; index < 3; ++index)
    {
      const char* value = getenv(names[index]);
      if ((mask & (1u << index)) != 0)
        assert(value && std::string(value) == "1");
      else
        assert(!value);
    }
  }
}
} // namespace

int main(int argc, char* argv[])
{
  assert(argc == 2);
  Config packaged;
  std::string error;
  assert(LoadConfig(argv[1], argv[1], packaged, error) == ConfigStatus::loaded);
  ExpectOrder("elf_9021,jailbreak_marker,etahen_ipc", packaged);
  assert(!packaged.debug.enabled && !packaged.debug.video && !packaged.debug.addons);
  TestParsing();
  TestConfigFiles();
  TestDebugEnvironment();
  std::printf("PS5 configuration tests passed\n");
  return 0;
}
