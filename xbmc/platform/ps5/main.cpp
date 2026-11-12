/*
 *  Copyright (C) 2026 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "application/AppEnvironment.h"
#include "application/AppParamParser.h"
#include "platform/posix/PlatformPosix.h"
#include "platform/ps5/AppDirectory.h"
#include "platform/ps5/BuildStamp.h"
#include "platform/ps5/ConfigPS5.h"
#include "platform/ps5/SandboxPS5.h"
#include "platform/ps5/input/PS5ImeDialog.h"
#include "platform/ps5/storage/PS5StorageUsage.h"
#include "platform/ps5/video/VideoDec2.h"
#include "platform/xbmc.h"

#include <cerrno>
#include <clocale>
#include <cstdarg>
#include <cstddef>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <sys/errno.h>
#include <sys/stat.h>

extern "C" int sceKernelDebugOutText(int channel, const char* text);
extern "C" int sceKernelSendNotificationRequest(int device, void* request, std::size_t size,
                                               int blocking);
extern "C" int sceSystemServiceLoadExec(const char* path, const char* argv[]);

namespace
{
constexpr const char* HOME_PATH = "/data/homebrew/kodi-psmc";

void Klog(const char* text)
{
    sceKernelDebugOutText(0, text);
}

void Klogf(const char* format, ...) __attribute__((format(printf, 1, 2)));
void Klogf(const char* format, ...)
{
    char buffer[512];
    va_list args;
    va_start(args, format);
    std::vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    Klog(buffer);
}

extern "C" void XBMC_PS5_HandleSignal(int)
{
    CPlatformPosix::RequestQuit();
}

int ExitRequest(int status)
{
    Klogf("[kodi-ps5] requesting title exit (Kodi status=%d)\n", status);
    const int result = sceSystemServiceLoadExec("exit", nullptr);
    Klogf("[kodi-ps5] LoadExec(exit) returned 0x%08x\n", static_cast<unsigned int>(result));
    return status;
}

void NotifyStartupFailure(const char* message)
{
    // Kernel debug-toast layout from the SDK's notify_debug sample.
    struct
    {
        char reserved[45];
        char message[3075];
    } notification{};
    static_assert(sizeof(notification) == 3120);
    std::snprintf(notification.message, sizeof(notification.message),
                  "%s", message);
    const int result = sceKernelSendNotificationRequest(0, &notification, sizeof(notification), 0);
    if (result != 0)
        Klogf("[kodi-ps5] debug notification failed (0x%08x)\n",
              static_cast<unsigned int>(result));
}

bool OpenSandbox(const KODI::PLATFORM::PS5::Config& config)
{
    const auto result = KODI::PLATFORM::PS5::RequestSandboxOpen(config);
    if (result == KODI::PLATFORM::PS5::ElevationResult::opened)
        return true;
    const char* reason =
        result == KODI::PLATFORM::PS5::ElevationResult::unsafe
            ? "elevation completion uncertain"
            : "no elevation method opened the sandbox";
    Klogf("[kodi-ps5] cannot create or open %s: %s\n", HOME_PATH, reason);
    NotifyStartupFailure(
        "Kodi cannot access its home directory. Sandbox escape or directory creation failed.");
    return false;
}

bool SetEnvironment(const char* name, const std::string& value)
{
    if (setenv(name, value.c_str(), 1) == 0)
        return true;
    Klogf("[kodi-ps5] cannot set %s: %s\n", name, std::strerror(errno));
    return false;
}

bool MakeDirs(const std::string& path)
{
    for (std::size_t pos = 1; pos <= path.size(); ++pos)
    {
        if (pos != path.size() && path[pos] != '/')
            continue;
        const std::string part = path.substr(0, pos);
        if (mkdir(part.c_str(), 0777) != 0 && errno != EEXIST)
        {
            Klogf("[kodi-ps5] mkdir %s: %s\n", part.c_str(), std::strerror(errno));
            return false;
        }
    }
    return true;
}

bool InitializeEnvironment(const std::string& title)
{
    const std::string resources = title + "/share/kodi";
    const std::string temp = std::string(HOME_PATH) + "/.kodi/temp";
    struct stat st{};
    if (stat((resources + "/userdata").c_str(), &st) != 0 || !S_ISDIR(st.st_mode))
    {
        Klogf("[kodi-ps5] missing installation resources: %s/userdata\n", resources.c_str());
        return false;
    }
    return MakeDirs(temp) &&
           SetEnvironment("HOME", HOME_PATH) &&
           SetEnvironment("KODI_HOME", resources) &&
           SetEnvironment("KODI_BIN_HOME", resources) &&
           SetEnvironment("PYTHONHOME", resources + "/python") &&
           SetEnvironment("PYTHONNOUSERSITE", "1") &&
           SetEnvironment("TMPDIR", temp) &&
           SetEnvironment("SSL_CERT_FILE", resources + "/system/certs/cacert.pem");
}

} // namespace

int main(int argc, char* argv[])
{
    struct sigaction handler{};
    handler.sa_handler = &XBMC_PS5_HandleSignal;
    handler.sa_flags = SA_RESTART;
    if (sigaction(SIGINT, &handler, nullptr) != 0 ||
        sigaction(SIGTERM, &handler, nullptr) != 0)
    {
        Klogf("[kodi-ps5] cannot install shutdown handlers: %s\n", std::strerror(errno));
        return ExitRequest(EXIT_FAILURE);
    }
    setlocale(LC_NUMERIC, "C");
    Klogf("[kodi-ps5] build %s\n", KODI_PS5_BUILD_STAMP);
    KODI::PLATFORM::PS5::Config config;
    std::string error;
    const auto configStatus = KODI::PLATFORM::PS5::LoadConfig(
        "/app0/config.json", "/app0/default-config.json", config, error);
    if (configStatus == KODI::PLATFORM::PS5::ConfigStatus::error ||
        !KODI::PLATFORM::PS5::ApplyDebugConfig(config.debug, error))
    {
        Klogf("[kodi-ps5] configuration error: %s\n", error.c_str());
        NotifyStartupFailure("Kodi configuration failed. Check config.json or default-config.json and the debug log.");
        return ExitRequest(EXIT_FAILURE);
    }
    Klogf("[kodi-ps5] configuration: %s; debug enabled=%d video=%d addons=%d\n",
          configStatus == KODI::PLATFORM::PS5::ConfigStatus::defaults ? "built-in defaults" : "loaded",
          config.debug.enabled, config.debug.video, config.debug.addons);
    std::string title;
    {
        KODI::PLATFORM::PS5::AppDirectory appDirectory;
        if (!appDirectory.IsValid())
            return ExitRequest(EXIT_FAILURE);
        // Activate these modules in the original title sandbox.
        KODI::PLATFORM::PS5::CVideoDec2::LoadModule();
        KODI::PLATFORM::PS5::CPS5ImeDialog::Preload();
        PreloadPS5UsbStorage();
        if (!OpenSandbox(config))
            return ExitRequest(EXIT_FAILURE);
        PreloadPS5InternalStorageModules();
        if (!appDirectory.Resolve(title))
            return ExitRequest(EXIT_FAILURE);
    }

    if (!InitializeEnvironment(title))
        return ExitRequest(EXIT_FAILURE);

    std::vector<char*> args(argv, argv + argc);
    static char debugFlag[] = "--debug";
    if (config.debug.enabled)
        args.push_back(debugFlag);
    CAppParamParser parser;
    parser.Parse(args.data(), static_cast<int>(args.size()));
    CAppEnvironment::SetUp(parser.GetAppParams());
    const int status = XBMC_Run(true);
    CAppEnvironment::TearDown();
    return ExitRequest(status);
}
