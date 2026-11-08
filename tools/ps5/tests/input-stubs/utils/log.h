#pragma once

#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

inline constexpr int LOGERROR = 1;
inline constexpr int LOGWARNING = 2;
inline constexpr int LOGINFO = 3;
inline constexpr int LOGDEBUG = 4;
inline unsigned int inputErrors = 0;
inline std::vector<std::string> inputDebugLogs;

class CLog
{
public:
  template<typename... Args>
  static void Log(int level, const char* format, Args&&... args)
  {
    if (level == LOGERROR || level == LOGWARNING)
      ++inputErrors;
    if (level == LOGDEBUG)
    {
      std::ostringstream message;
      message << format;
      auto append = [&message](const auto& value)
      {
        message << ' ';
        if constexpr (std::is_integral_v<std::decay_t<decltype(value)>>)
          message << +value;
        else
          message << value;
      };
      (append(args), ...);
      inputDebugLogs.push_back(message.str());
    }
  }
};
