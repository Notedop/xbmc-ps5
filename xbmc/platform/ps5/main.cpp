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
#include "platform/ps5/JitProbe.h"
#include "platform/ps5/SandboxPS5.h"
#include "platform/ps5/input/PS5ImeDialog.h"
#include "platform/ps5/video/VideoDec2.h"
#include "platform/xbmc.h"

#include <cerrno>
#include <chrono>
#include <clocale>
#include <cstdarg>
#include <cstddef>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <dirent.h>
#include <sys/errno.h>
#include <sys/stat.h>
#include <unistd.h>

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

void NotifySandboxFailure()
{
    // Kernel debug-toast layout from the SDK's notify_debug sample.
    struct
    {
        char reserved[45];
        char message[3075];
    } notification{};
    static_assert(sizeof(notification) == 3120);
    std::snprintf(notification.message, sizeof(notification.message),
                  "Kodi cannot access its home directory. Sandbox escape or directory creation failed.");
    const int result = sceKernelSendNotificationRequest(0, &notification, sizeof(notification), 0);
    if (result != 0)
        Klogf("[kodi-ps5] debug notification failed (0x%08x)\n",
              static_cast<unsigned int>(result));
}

bool OpenSandbox()
{
    KODI::PLATFORM::PS5::RequestSandboxOpen();
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (!KODI::PLATFORM::PS5::IsSandboxOpen())
    {
        const int error = errno;
        if (std::chrono::steady_clock::now() >= deadline)
        {
            Klogf("[kodi-ps5] cannot create or open %s: %s (errno=%d)\n",
                  HOME_PATH, std::strerror(error), error);
            NotifySandboxFailure();
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
    }
    return true;
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

bool SwitchPresent(const std::string& path)
{
    struct stat st{};
    if (stat(path.c_str(), &st) == 0)
        return true;
    if (errno != ENOENT)
        Klogf("[kodi-ps5] switch %s: %s\n", path.c_str(), std::strerror(errno));
    return false;
}

// Do not follow symlinks when resetting data.
bool RemoveDataTree(const std::string& path)
{
    struct stat st{};
    if (lstat(path.c_str(), &st) != 0)
    {
        if (errno == ENOENT)
            return true;
        Klogf("[kodi-ps5] lstat %s: %s\n", path.c_str(), std::strerror(errno));
        return false;
    }
    bool success = true;
    if (S_ISDIR(st.st_mode))
    {
        DIR* dir = opendir(path.c_str());
        if (!dir)
        {
            Klogf("[kodi-ps5] opendir %s: %s\n", path.c_str(), std::strerror(errno));
            return false;
        }
        int readError = 0;
        for (;;)
        {
            errno = 0;
            const dirent* entry = readdir(dir);
            if (!entry)
            {
                readError = errno;
                break;
            }
            if (std::strcmp(entry->d_name, ".") == 0 || std::strcmp(entry->d_name, "..") == 0)
                continue;
            if (!RemoveDataTree(path + "/" + entry->d_name))
                success = false;
        }
        closedir(dir);
        if (readError != 0)
        {
            Klogf("[kodi-ps5] readdir %s: %s\n", path.c_str(), std::strerror(readError));
            success = false;
        }
    }
    const int result = S_ISDIR(st.st_mode) ? rmdir(path.c_str()) : unlink(path.c_str());
    if (result != 0)
    {
        Klogf("[kodi-ps5] remove %s: %s\n", path.c_str(),
              std::strerror(errno));
        success = false;
    }
    return success;
}

bool ConsumeSwitch(const std::string& path)
{
    if (unlink(path.c_str()) == 0)
        return true;
    Klogf("[kodi-ps5] cannot remove switch %s: %s\n", path.c_str(), std::strerror(errno));
    return false;
}

bool ApplySwitches(const std::string& data, bool& uninstall)
{
    const std::string home = HOME_PATH;
    uninstall = SwitchPresent(home + "/kodi-uninstall");
    const std::string reset = home + (uninstall ? "/kodi-uninstall" : "/kodi-reset");
    if ((uninstall || SwitchPresent(reset)) &&
        (!RemoveDataTree(data) || !ConsumeSwitch(reset)))
        return false;
    if (uninstall)
        return true;
    const std::string jit = home + "/kodi-jitprobe";
    if (SwitchPresent(jit))
    {
        XBMC_PS5_RunJitProbe();
        if (!ConsumeSwitch(jit))
            return false;
    }
    const std::pair<const char*, const char*> switches[] = {
        {"kodi-debug", "KODI_PS5_DEBUG"},
        {"kodi-video-debug", "KODI_PS5_VIDEO_DEBUG"},
        {"kodi-addon-debug", "KODI_PS5_ADDON_DEBUG"},
    };
    for (const auto& [file, variable] : switches)
    {
        if (SwitchPresent(home + "/" + file) && !SetEnvironment(variable, "1"))
            return false;
    }
    return true;
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
    std::string title;
    {
        KODI::PLATFORM::PS5::AppDirectory appDirectory;
        if (!appDirectory.IsValid())
            return ExitRequest(EXIT_FAILURE);
        // Module loads require the original sandbox, before the jailbreak request.
        KODI::PLATFORM::PS5::CVideoDec2::LoadModule();
        KODI::PLATFORM::PS5::CPS5ImeDialog::Preload();
        if (!OpenSandbox())
            return ExitRequest(EXIT_FAILURE);
        if (!appDirectory.Resolve(title))
            return ExitRequest(EXIT_FAILURE);
    }

    const std::string data = std::string(HOME_PATH) + "/.kodi";
    bool uninstall = false;
    if (!ApplySwitches(data, uninstall))
        return ExitRequest(EXIT_FAILURE);
    if (uninstall)
        return ExitRequest(EXIT_SUCCESS);
    if (!InitializeEnvironment(title))
        return ExitRequest(EXIT_FAILURE);

    std::vector<char*> args(argv, argv + argc);
    static char debugFlag[] = "--debug";
    if (getenv("KODI_PS5_DEBUG"))
        args.push_back(debugFlag);
    CAppParamParser parser;
    parser.Parse(args.data(), static_cast<int>(args.size()));
    CAppEnvironment::SetUp(parser.GetAppParams());
    const int status = XBMC_Run(true);
    CAppEnvironment::TearDown();
    return ExitRequest(status);
}
