#pragma once

inline constexpr int LOGERROR = 1;
inline constexpr int LOGWARNING = 2;
inline constexpr int LOGINFO = 3;
inline unsigned int inputErrors = 0;

class CLog
{
public:
  template<typename... Args>
  static void Log(int level, const char*, Args&&...)
  {
    if (level == LOGERROR || level == LOGWARNING)
      ++inputErrors;
  }
};
