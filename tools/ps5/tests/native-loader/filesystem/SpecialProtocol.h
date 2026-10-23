#pragma once
#include <string>
class CSpecialProtocol
{
public:
  inline static std::string root = "/app0";
  static std::string TranslatePath(const std::string& path)
  {
    const std::string prefix = "special://xbmc/";
    return path.starts_with(prefix) ? root + "/" + path.substr(prefix.size()) : path;
  }
};
