#pragma once
namespace XFILE
{
class CFile
{
public:
  static bool Exists(const char*) { return false; }
  static bool Exists(const std::string&) { return false; }
  static bool Copy(const char*, const std::string&) { return false; }
};
}
