#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace KODI::PLATFORM::PS5::MESSAGING
{
    enum class ViewTemplateType
    {
        InteractiveToastTemplateB, // Standard interactive pop-up with an action button
        SimpleToastTemplateA // Minimal toast without actions
    };

    enum class ChannelType
    {
        Downloads, // Associated with the download system (often used for samples/payloads)
        System, // System-wide notifications
        Friend // Friend/social notifications
    };

    enum class ToastOverwriteType
    {
        No, // Create a new notification (do not overwrite)
        Yes // Overwrite the previous active notification with the same ID
    };

    struct Action
    {
        std::string actionName;
        std::string actionUrl; // Example: "pssettings:play?function=debug_settings"
        bool defaultFocus = true;
    };

    struct Payload
    {
        // --- REQUIRED FIELDS (enforced by the constructor) ---
        std::string messageBody;
        ViewTemplateType viewTemplate;
        ChannelType channel;

        // Optional notification fields.
        std::string subMessageBody = "";
        std::string iconUrl = "/path/to/icon.png";
        std::string useCaseId = "IDC";
        ToastOverwriteType overwriteType = ToastOverwriteType::No;
        bool isImmediate = true;
        int priority = 100; // 100 = High/show immediately

        // Preview behavior (when the toast is minimized)
        std::string previewIcon = "download";
        std::string previewMessage = "";

        // Actions (buttons)
        std::vector<Action> actions;

        // The constructor requires initialization of mandatory parameters
        Payload(std::string_view message, ViewTemplateType templateType, ChannelType channelType)
            : messageBody(message),
              viewTemplate(templateType),
              channel(channelType)
        {
            // Default the preview message to the main message if it is left empty
            previewMessage = std::string(message) + " is running";
        }
    };

    // Synchronous PS5 notification service; send returns the native API result.
    class Ps5NotificationMessage
    {
    public:
        int send(const Payload& payload) const;
        std::string serialize(const Payload& payload) const;
    };
} // namespace KODI::PLATFORM::PS5::MESSAGING
