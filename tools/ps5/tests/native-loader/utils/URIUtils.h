#pragma once
#include <string>
class URIUtils
{
public:
  static bool IsInArchive(const char*) { return false; }
  static void AddSlashAtEnd(std::string& path) { path += '/'; }
};
