/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stddef.h>

#include "../../../shims/native-app/libc_socket.c"

static int net_error, connect_result, connect_error, socket_type, type_result;
static int connect_calls, socket_calls, captured_domain, captured_type, captured_protocol;
static int captured_length, last_socket;
static SceNetSockaddr captured_address;
static char messages[4096];

void sceKernelDebugOutText(int channel, const char* text)
{
  assert(channel == 0);
  assert(strlen(messages) + strlen(text) < sizeof(messages));
  strcat(messages, text);
  errno = ERANGE; /* Logging must not clobber the intended errno. */
}

int* sceNetErrnoLoc(void)
{
  return &net_error;
}

int sceNetInit(void)
{
  return 0;
}

int sceNetPoolCreate(const char* name, int size, int flags)
{
  (void)name; (void)size; (void)flags;
  return 1;
}

int sceNetSocket(const char* name, int family, int type, int protocol)
{
  assert(strcmp(name, "python") == 0);
  ++socket_calls;
  captured_domain = family;
  captured_type = type;
  captured_protocol = protocol;
  return 7;
}

int sceNetConnect(int s, const SceNetSockaddr* addr, int len)
{
  ++connect_calls;
  last_socket = s;
  captured_length = len;
  memcpy(&captured_address, addr, (size_t)len);
  net_error = connect_error;
  return connect_result;
}

int sceNetGetsockopt(int s, int level, int opt, void* val, int* len)
{
  assert(s == 7 && level == SOL_SOCKET && opt == SO_TYPE && *len == sizeof(int));
  /* The raw connect error must already have been logged before this call. */
  assert(strstr(messages, "pysock connect 0x") != NULL);
  net_error = 0x39;
  *(int*)val = socket_type;
  return type_result;
}

int sceNetGetpeername(int s, SceNetSockaddr* name, int* len)
{
  (void)s; (void)name; (void)len;
  net_error = 0x39;
  return -1;
}

int sceNetSocketClose(int s) { (void)s; return 0; }
int sceNetBind(int s, const SceNetSockaddr* a, int l)
{ (void)s; (void)a; (void)l; return 0; }
int sceNetListen(int s, int n) { (void)s; (void)n; return 0; }
int sceNetSendto(int s, const void* b, size_t n, int f, const SceNetSockaddr* a, int l)
{ (void)s; (void)b; (void)n; (void)f; (void)a; (void)l; return 0; }
int sceNetRecvfrom(int s, void* b, size_t n, int f, SceNetSockaddr* a, int* l)
{ (void)s; (void)b; (void)n; (void)f; (void)a; (void)l; return 0; }
int sceNetSetsockopt(int s, int l, int o, const void* v, int n)
{ (void)s; (void)l; (void)o; (void)v; (void)n; return 0; }
int sceNetGetsockname(int s, SceNetSockaddr* a, int* l)
{ (void)s; (void)a; (void)l; return 0; }
int sceNetShutdown(int s, int h) { (void)s; (void)h; return 0; }

static void reset(void)
{
  messages[0] = '\0';
  connect_result = -1;
  connect_error = 0x31;
  socket_type = SOCK_DGRAM;
  type_result = 0;
  errno = 0;
}

int main(void)
{
  _Static_assert(sizeof(struct sockaddr_in) == sizeof(SceNetSockaddr), "IPv4 size");
  _Static_assert(offsetof(struct sockaddr_in, sin_port) == 2, "IPv4 port offset");
  _Static_assert(offsetof(struct sockaddr_in, sin_addr) == 4, "IPv4 address offset");
  struct sockaddr_in address = {0};
  address.sin_family = AF_INET;
  assert(inet_pton(AF_INET, "192.168.178.44", &address.sin_addr) == 1);
  reset();
  errno = EDOM;
  assert(ps5_socket(AF_INET, SOCK_DGRAM | 0x30000000, 0) == 7);
  assert(errno == EDOM && socket_calls == 1);
  assert(captured_domain == AF_INET && captured_type == SOCK_DGRAM && captured_protocol == 0);
  assert(strstr(messages, "socket fd=7 family=2") != NULL);

  reset();
  assert(ps5_connect(7, (struct sockaddr*)&address, sizeof(address)) == -1);
  assert(errno == 49 && connect_calls == 1 && last_socket == 7);
  assert(strstr(messages, "type=SOCK_DGRAM family=AF_INET addr=192.168.178.44 port=0") != NULL);
  assert(strstr(messages, "sce=0x00000031 errno=49") != NULL);
  assert(captured_length == 16 && captured_address.sa_len == 16);
  assert(memcmp((char*)&captured_address + 2, (char*)&address + 2, 14) == 0);

  reset();
  address.sin_port = htons(50152);
  socket_type = SOCK_STREAM;
  connect_error = (int)0x8041013d;
  assert(ps5_connect(7, (struct sockaddr*)&address, sizeof(address)) == -1);
  assert(errno == 61);
  assert(strstr(messages, "type=SOCK_STREAM family=AF_INET addr=192.168.178.44 port=50152") != NULL);
  assert(strstr(messages, "sce=0x8041013d errno=61") != NULL);

  reset();
  type_result = -1;
  assert(ps5_connect(7, (struct sockaddr*)&address, sizeof(address)) == -1);
  assert(errno == 49 && strstr(messages, "type=?") != NULL);

  reset();
  connect_result = 0;
  errno = EDOM;
  assert(ps5_connect(7, (struct sockaddr*)&address, sizeof(address)) == 0);
  assert(errno == EDOM && messages[0] == '\0');

  reset();
  int calls = connect_calls;
  struct sockaddr_in6 address6 = {0};
  address6.sin6_family = AF_INET6;
  assert(inet_pton(AF_INET6, "::1", &address6.sin6_addr) == 1);
  assert(ps5_connect(7, (struct sockaddr*)&address6, sizeof(address6)) == -1);
  assert(errno == EINVAL && connect_calls == calls);
  assert(strstr(messages, "family=AF_INET6 addr=::1 port=0") != NULL);
  assert(ps5_connect(7, NULL, 0) == -1 && errno == EINVAL);

  reset();
  unsigned len = sizeof(address);
  assert(ps5_getpeername(7, (struct sockaddr*)&address, &len) == -1);
  assert(errno == 57 && strstr(messages, "pysock getpeername 0x00000039") != NULL);
  for (int e = 1; e <= 96; ++e)
  {
    assert(errno_from_sce(e) == e);
    assert(errno_from_sce((int)(0x80410100u | (unsigned)e)) == e);
  }
  assert(errno_from_sce((int)0xdead0031u) == EIO);
  puts("PASS: Python socket shim");
  return 0;
}
