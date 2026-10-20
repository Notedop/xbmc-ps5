set(SOURCES AppDirectory.cpp
            AsyncLogSink.cpp
            CacheDiagnostics.cpp
            CPUInfoPS5.cpp
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
            CPUInfoPS5.h
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
