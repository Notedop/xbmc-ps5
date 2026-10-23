set(SOURCES ConvUtils.cpp
            CPUInfoPosix.cpp
            Filesystem.cpp
            GPUInfoPosix.cpp
            MessagePrinter.cpp
            PlatformPosix.cpp
            PosixMountProvider.cpp
            PosixResourceCounter.cpp
            XHandle.cpp
            XTimeUtils.cpp)

set(HEADERS ConvUtils.h
            CPUInfoPosix.h
            GPUInfoPosix.h
            PlatformDefs.h
            PlatformPosix.h
            PosixMountProvider.h
            PosixResourceCounter.h
            XHandle.h)

if(CORE_SYSTEM_NAME STREQUAL "ps5")
  # PS5 uses native sensors and storage APIs, not desktop shell commands.
  list(REMOVE_ITEM SOURCES CPUInfoPosix.cpp GPUInfoPosix.cpp PosixMountProvider.cpp)
endif()

core_add_library(platform_posix)
