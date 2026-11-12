set(SOURCES AppDirectory.cpp
            AsyncLogSink.cpp
            CacheDiagnostics.cpp
            ConfigPS5.cpp
            CPUInfoPS5.cpp
            ElevationElf.cpp
            ElevationIpc.cpp
            GPUInfoPS5.cpp
            MemUtils.cpp
            PlatformPS5.cpp
            PS5InterfaceForCLog.cpp
            SandboxPS5.cpp
            VideoOutInfo.cpp
            strptime.cpp
            )

set(HEADERS AppDirectory.h
            AsyncLogSink.h
            CacheDiagnostics.h
            ConfigPS5.h
            CPUInfoPS5.h
            ElevationMethods.h
            GPUInfoPS5.h
            PlatformPS5.h
            PS5InterfaceForCLog.h
            SandboxPS5.h
            VideoOutInfo.h
            sce/SceAudioOut.h
            sce/SceImeDialog.h
            sce/ScePad.h
            sce/SceNotifications.h
            sce/SceUserService.h
)

core_add_library(platform_ps5)

target_link_libraries(${CORE_LIBRARY} PRIVATE SceNotification)
