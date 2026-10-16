#pragma once

extern "C"
{
int sceNotificationSend(int userId, bool isLogged, const char* jsonPayload);
}
