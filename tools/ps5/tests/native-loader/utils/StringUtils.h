#pragma once
#include <cstring>
#include <strings.h>
#include <string>
#include <vector>
class StringUtils
{
public:
  static int CompareNoCase(const char* a, const char* b) { return strcasecmp(a, b); }
  static std::vector<std::string> Split(const char*, char) { return {}; }
};
