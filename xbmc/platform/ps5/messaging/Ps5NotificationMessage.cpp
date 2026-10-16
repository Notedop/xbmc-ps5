#include "Ps5NotificationMessage.h"

#include "platform/ps5/sce/SceNotifications.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <ctime>
#include <stdexcept>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

namespace
{
    constexpr int SCE_NOTIFICATION_LOCAL_USER_ID_SYSTEM = -1;

    std::string CurrentUtcTimestamp()
    {
        using namespace std::chrono;

        const auto now = system_clock::now();
        const auto millis = duration_cast<milliseconds>(now.time_since_epoch());
        const auto millisecondPart = (millis - duration_cast<seconds>(millis)).count();
        const std::time_t time = static_cast<std::time_t>(
            duration_cast<seconds>(millis).count());
        std::tm utc{};
        if (gmtime_r(&time, &utc) == nullptr)
            throw std::runtime_error("Failed to convert notification timestamp to UTC");

        char timestamp[32];
        if (std::strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S", &utc) == 0)
            throw std::runtime_error("Failed to format notification timestamp");

        const std::string millisecondsText = std::to_string(millisecondPart);
        return std::string(timestamp) + "." + std::string(3 - millisecondsText.length(), '0') +
            millisecondsText + "Z";
    }

    std::string NextNotificationId()
    {
        static std::atomic<std::uint64_t> nextId{1};
        return std::to_string(nextId.fetch_add(1, std::memory_order_relaxed));
    }
} // namespace

namespace KODI::PLATFORM::PS5::MESSAGING
{
    std::string Ps5NotificationMessage::serialize(const Payload& payload) const
    {
        nlohmann::json json;

        const std::string templateType =
            (payload.viewTemplate == ViewTemplateType::InteractiveToastTemplateB)
                ? "InteractiveToastTemplateB"
                : "SimpleToastTemplateA";
        const std::string channelType = (payload.channel == ChannelType::Downloads)
                                            ? "Downloads"
                                            : (payload.channel == ChannelType::System)
                                            ? "System"
                                            : "Friend";
        const std::string overwriteType =
            (payload.overwriteType == ToastOverwriteType::No) ? "No" : "Yes";

        json["rawData"]["viewTemplateType"] = templateType;
        json["rawData"]["channelType"] = channelType;
        json["rawData"]["useCaseId"] = payload.useCaseId;
        json["rawData"]["toastOverwriteType"] = overwriteType;
        json["rawData"]["isImmediate"] = payload.isImmediate;
        json["rawData"]["priority"] = payload.priority;

        json["rawData"]["viewData"]["icon"]["type"] = "Url";
        json["rawData"]["viewData"]["icon"]["parameters"]["url"] = payload.iconUrl;
        json["rawData"]["viewData"]["message"]["body"] = payload.messageBody;

        if (!payload.subMessageBody.empty())
            json["rawData"]["viewData"]["subMessage"]["body"] = payload.subMessageBody;

        json["rawData"]["viewData"]["actions"] = nlohmann::json::array();
        for (const auto& action : payload.actions)
        {
            nlohmann::json actionObject;
            actionObject["actionName"] = action.actionName;
            actionObject["actionType"] = "DeepLink";
            actionObject["defaultFocus"] = action.defaultFocus;
            actionObject["parameters"]["actionUrl"] = action.actionUrl;
            json["rawData"]["viewData"]["actions"].push_back(std::move(actionObject));
        }

        json["rawData"]["platformViews"]["previewDisabled"]["viewData"]["icon"]["type"] = "Predefined";
        json["rawData"]["platformViews"]["previewDisabled"]["viewData"]["icon"]["parameters"]["icon"] =
            payload.previewIcon;
        json["rawData"]["platformViews"]["previewDisabled"]["viewData"]["message"]["body"] =
            payload.previewMessage;

        json["createdDateTime"] = CurrentUtcTimestamp();
        json["localNotificationId"] = NextNotificationId();
        return json.dump(2);
    }

    int Ps5NotificationMessage::send(const Payload& payload) const
    {
        const std::string jsonPayload = serialize(payload);
        return sceNotificationSend(SCE_NOTIFICATION_LOCAL_USER_ID_SYSTEM, true, jsonPayload.c_str());
    }
} // namespace KODI::PLATFORM::PS5::MESSAGING
