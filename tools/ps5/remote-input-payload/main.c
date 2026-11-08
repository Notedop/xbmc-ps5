/*
 *  Copyright (C) 2026 Team Kodi
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <inttypes.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum
{
    REMOTE_USER = 0xff,
    REMOTE_PORT_TYPE = 16,
    LOTUS_LOOKBACK_MS = 100,
    CAPTURE_SECONDS = 120,
    BLOCKED_SECONDS = 15,
    HARD_LIMIT_SECONDS = 180,
    TICK_MS = 2000,
};

static const uint32_t PAD_ERROR_ALREADY_OPENED = 0x80920004u;

enum
{
    STAGE_START,
    STAGE_USERSVC,
    STAGE_INIT,
    STAGE_OPEN,
    STAGE_READSTATE,
    STAGE_GETDEVICEID,
    STAGE_DEVINIT,
    STAGE_DEVQUERY,
    STAGE_LOTUS,
    STAGE_CLOSE,
    STAGE_DONE,
};

static const char *const STAGE_NAMES[] = {
    "start", "usersvc", "init",     "open",      "readstate", "getdeviceid",
    "devinit", "devquery", "lotus", "close",     "done"};

struct pad_touch
{
    uint16_t x;
    uint16_t y;
    uint8_t finger;
    uint8_t pad[3];
};

struct pad_touch_data
{
    uint8_t fingers;
    uint8_t pad1[3];
    uint32_t pad2;
    struct pad_touch touch[2];
};

struct pad_data
{
    uint32_t buttons;
    struct
    {
        uint8_t x, y;
    } left_stick;
    struct
    {
        uint8_t x, y;
    } right_stick;
    struct
    {
        uint8_t l2, r2;
    } analog_buttons;
    uint16_t padding;
    float orientation[4];
    float angular_velocity[3];
    float acceleration[3];
    struct pad_touch_data touch;
    uint8_t connected;
    uint64_t timestamp;
    uint8_t ext[16];
    uint8_t count;
    uint8_t reserved[2];
    uint8_t payload_len;
    uint8_t payload[12];
};

_Static_assert(sizeof(struct pad_data) == 0x78, "ScePadData layout must be 0x78 bytes");
_Static_assert(offsetof(struct pad_data, payload_len) == 0x6b, "payload_len offset");
_Static_assert(offsetof(struct pad_data, payload) == 0x6c, "payload offset");

extern int sceUserServiceInitialize(const void *param);
extern int scePadInit(void);
extern int scePadOpen(int, int, int, const void *);
extern int scePadGetHandle(int, int, int);
extern int scePadClose(int);
extern int scePadReadState(int, void *);
extern int scePadGetDeviceId(int, int *);
extern int scePadLastLotusButtonData(int, uint32_t, uint32_t *);
extern int sceMbusConvertToLocalDeviceId2(uint64_t device_id, uint32_t device_class,
                                          uint64_t *local_id);
extern int sceDeviceServiceInitialize(uint32_t version, const void *param);
extern int sceDeviceServiceQueryDeviceInfo_(uint32_t type, const void *filter, uint32_t flags,
                                            void *records, int capacity, int *count, int *extra,
                                            uint32_t record_size);
extern int64_t sceKernelGetProcessTime(void);

struct device_record
{
    uint64_t device_id;
    uint8_t rest[0x68];
};

_Static_assert(sizeof(struct device_record) == 0x70, "DeviceService record layout");

static pthread_mutex_t g_output = PTHREAD_MUTEX_INITIALIZER;
static volatile int g_stage = STAGE_START;
static volatile int64_t g_stage_ms = 0;
static volatile int g_started = 0;
static volatile int g_done = 0;
static volatile int g_status = 1;
static int64_t g_start_ms = 0;

static void log_message(const char *format, ...) __attribute__((format(printf, 1, 2)));

static void log_message(const char *format, ...)
{
    char text[512];
    va_list args;
    va_start(args, format);
    vsnprintf(text, sizeof(text), format, args);
    va_end(args);
    pthread_mutex_lock(&g_output);
    fputs(text, stdout);
    fflush(stdout);
    pthread_mutex_unlock(&g_output);
}

static int64_t now_ms(void)
{
    return sceKernelGetProcessTime() / 1000;
}

static void set_stage(int stage)
{
    g_stage = stage;
    g_stage_ms = now_ms();
}

static void touch_stage(void)
{
    g_stage_ms = now_ms();
}

static void log_payload_line(const char *kind, int64_t elapsed, const struct pad_data *data)
{
    char hex[sizeof(data->payload) * 2 + 1];
    const size_t len = data->payload_len > sizeof(data->payload)
                           ? sizeof(data->payload)
                           : data->payload_len;
    for (size_t i = 0; i < len; ++i)
        snprintf(&hex[i * 2], 3, "%02x", data->payload[i]);
    hex[len * 2] = '\0';
    log_message("[remote-payload] %s t=%lldms ts=%" PRIu64 " count=%u conn=%u buttons=0x%08x "
                "payload_len=%u payload=%s\n",
                kind, (long long)elapsed, data->timestamp, data->count, data->connected,
                data->buttons, data->payload_len, hex[0] ? hex : "-");
}

static void *pad_thread(void *arg)
{
    (void)arg;
    int handle = -1;
    bool owned = false;
    int result;

    set_stage(STAGE_USERSVC);
    log_message("[remote-payload] calling sceUserServiceInitialize\n");
    result = sceUserServiceInitialize(NULL);
    log_message("[remote-payload] UserService init result=0x%08x\n", (uint32_t)result);

    set_stage(STAGE_INIT);
    log_message("[remote-payload] calling scePadInit\n");
    result = scePadInit();
    log_message("[remote-payload] Pad init result=0x%08x\n", (uint32_t)result);
    if (result != 0)
        goto out;

    set_stage(STAGE_OPEN);
    handle = scePadOpen(REMOTE_USER, REMOTE_PORT_TYPE, 0, NULL);
    owned = (uint32_t)handle != PAD_ERROR_ALREADY_OPENED;
    if (!owned)
        handle = scePadGetHandle(REMOTE_USER, REMOTE_PORT_TYPE, 0);
    log_message("[remote-payload] open %s handle=%d\n", owned ? "owned" : "borrowed", handle);
    if (handle <= 0)
        goto out;

    set_stage(STAGE_READSTATE);
    struct pad_data state = {0};
    result = scePadReadState(handle, &state);
    log_message("[remote-payload] readstate result=0x%08x\n", (uint32_t)result);
    if (result != 0)
        goto out;
    log_payload_line("state", now_ms() - g_start_ms, &state);

    set_stage(STAGE_GETDEVICEID);
    log_message("[remote-payload] waiting for the opened remote to connect; press a remote key\n");
    int device_id = -1;
    const int64_t id_deadline = now_ms() + 30000;
    while (device_id < 0 && now_ms() < id_deadline)
    {
        result = scePadGetDeviceId(handle, &device_id);
        if (result != 0)
        {
            log_message("[remote-payload] device id lookup failed result=0x%08x\n",
                        (uint32_t)result);
            goto out;
        }
        touch_stage();
        if (device_id < 0)
            usleep(20000);
    }
    if (device_id < 0)
    {
        log_message("[remote-payload] opened remote has no usable Pad device ID\n");
        goto out;
    }
    log_message("[remote-payload] Pad remote: handle=%d pad_id=0x%08x\n", handle,
                (uint32_t)device_id);

    set_stage(STAGE_DEVINIT);
    result = sceDeviceServiceInitialize(3, NULL);
    log_message("[remote-payload] DeviceService init result=0x%08x\n", (uint32_t)result);
    if (result != 0 && (uint32_t)result != 0x809b000bu)
        goto out;

    set_stage(STAGE_DEVQUERY);
    struct device_record records[16];
    memset(records, 0, sizeof(records));
    int count = -1;
    int extra = -1;
    result = sceDeviceServiceQueryDeviceInfo_(0x9001, NULL, 0, records, 16, &count, &extra,
                                             sizeof(records[0]));
    log_message("[remote-payload] DeviceService query type=0x9001 result=0x%08x count=%d extra=%d\n",
                (uint32_t)result, count, extra);
    if (result < 0 || count <= 0)
        goto out;
    int lotus_id = -1;
    for (int i = 0; i < count; ++i)
    {
        uint64_t hid_id = 0;
        const int convert = sceMbusConvertToLocalDeviceId2(records[i].device_id, 0, &hid_id);
        log_message("[remote-payload] record %d mbus_id=0x%016" PRIx64 " conv=0x%08x "
                    "hid_id=0x%016" PRIx64 "\n",
                    i, records[i].device_id, (uint32_t)convert, hid_id);
        if (convert == 0 && lotus_id < 0 && hid_id <= UINT32_MAX)
            lotus_id = (int)(int32_t)hid_id;
    }
    if (lotus_id < 0)
    {
        log_message("[remote-payload] no converted HID ID fits the 32-bit Lotus identifier\n");
        goto out;
    }

    set_stage(STAGE_LOTUS);
    log_message("[remote-payload] polling Lotus history %ds; press each key once with about 3s "
                "gaps: D-pad up, D-pad down, Play/Pause, Rewind, Forward\n",
                CAPTURE_SECONDS);
    const int64_t capture_start = now_ms();
    uint32_t previous_mask = 0;
    bool first_mask = true;
    bool saw_event = false;
    while (now_ms() - capture_start < CAPTURE_SECONDS * 1000)
    {
        uint32_t mask = 0;
        result = scePadLastLotusButtonData(lotus_id, LOTUS_LOOKBACK_MS, &mask);
        if (result != 0)
        {
            log_message("[remote-payload] Lotus query failed result=0x%08x\n", (uint32_t)result);
            goto out;
        }
        if (first_mask || mask != previous_mask)
        {
            log_message("[remote-payload] lotus t=%lldms device=0x%08x mask=0x%08x\n",
                        (long long)(now_ms() - g_start_ms), (uint32_t)lotus_id, mask);
            if (mask != 0)
                saw_event = true;
            previous_mask = mask;
            first_mask = false;
        }
        touch_stage();
        usleep(20000);
    }
    log_message("[remote-payload] capture finished saw_event=%d\n", saw_event);
    g_status = saw_event ? 0 : 1;

out:
    set_stage(STAGE_CLOSE);
    if (handle > 0 && owned)
    {
        result = scePadClose(handle);
        log_message("[remote-payload] close result=0x%08x\n", (uint32_t)result);
    }
    set_stage(STAGE_DONE);
    g_done = 1;
    return NULL;
}

int main(void)
{
    g_start_ms = now_ms();
    g_stage_ms = g_start_ms;
    log_message("[remote-payload] raw remote report capture; worker-thread Pad calls; "
                "no Kodi or media registration\n");

    /* Resolve every watchdog import before the worker can hold the loader lock. */
    usleep(1);

    pthread_t worker;
    if (pthread_create(&worker, NULL, pad_thread, NULL) != 0)
    {
        log_message("[remote-payload] failed to create worker thread\n");
        return 1;
    }
    g_started = 1;

    int64_t last_tick = g_start_ms;
    for (;;)
    {
        usleep(200000);
        const int64_t now = now_ms();
        if (g_done)
            break;
        if (now - g_start_ms > HARD_LIMIT_SECONDS * 1000)
        {
            log_message("[remote-payload] hard limit %ds reached at stage=%s; exiting\n",
                        HARD_LIMIT_SECONDS, STAGE_NAMES[g_stage]);
            _exit(3);
        }
        if (now - g_stage_ms > BLOCKED_SECONDS * 1000)
        {
            log_message("[remote-payload] blocked stage=%s for %llds; giving up\n",
                        STAGE_NAMES[g_stage], (long long)((now - g_stage_ms) / 1000));
            _exit(2);
        }
        if (now - last_tick >= TICK_MS)
        {
            log_message("[remote-payload] alive stage=%s total=%llds stage_age=%llds\n",
                        STAGE_NAMES[g_stage], (long long)((now - g_start_ms) / 1000),
                        (long long)((now - g_stage_ms) / 1000));
            last_tick = now;
        }
    }
    log_message("[remote-payload] worker finished status=%d\n", g_status);
    return g_status;
}
