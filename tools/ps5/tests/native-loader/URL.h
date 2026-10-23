#pragma once
#include <string>
class CURL
{
public:
  explicit CURL(const char*) {}
  std::string GetFileName() const { return {}; }
  static bool IsFullPath(const char* path)
  {
    return path[0] == '/' || std::string(path).starts_with("special://");
  }
};
