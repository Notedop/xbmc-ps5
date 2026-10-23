#pragma once
enum { LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR, LOGFATAL };
class CLog
{
public:
  template<typename... Args>
  static void Log(int, const char*, const Args&...) {}
};
