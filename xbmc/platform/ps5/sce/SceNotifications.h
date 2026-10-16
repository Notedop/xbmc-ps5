#pragma once

extern "C"
{
int sceNotificationSend(int userId, bool bUnknown, const char* jsonPayload);
}
