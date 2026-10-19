/*
 *  Name resolution for a native title. The SDK binds getaddrinfo & co to
 *  libScePosixForWebKit, which titles do not get, so implement them on
 *  Sony's resolver in libSceNet (loaded in every title): numeric IPv4/IPv6
 *  literals directly, host names via sceNetResolverStartNtoa (IPv4 A records).
 *  gethostname() is the console's system name; that name resolves to the
 *  console's own interface addresses (as nss-myhostname does on Linux).
 *  SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <arpa/inet.h>
#include <errno.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netdb.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

int sceNetPoolCreate(const char* name, int size, int flags);
int sceNetPoolDestroy(int pool);
int sceNetResolverCreate(const char* name, int pool, int flags);
int sceNetResolverDestroy(int resolver);
int sceNetResolverStartNtoa(int resolver, const char* host, uint32_t* address, int timeout_us,
                            int retries, int flags);
extern void sceKernelDebugOutText(int channel, const char* text);
int sceSystemServiceParamGetString(int param_id, char* buf, size_t size);

#define PS5_PARAM_ID_SYSTEM_NAME 6 /* Settings > System > Console name */
#define PS5_SYSTEM_NAME_SIZE 65
#define PS5_MAX_LOCAL_ADDRS 8

/* Sony's libc gethostname() succeeds with an empty name in a title; the
   console's name is a system-service parameter instead. */
static int system_name(char* buf, size_t size)
{
  static int logged;
  memset(buf, 0, size);
  int r = sceSystemServiceParamGetString(PS5_PARAM_ID_SYSTEM_NAME, buf, size);
  buf[size - 1] = '\0';
  if (!logged)
  {
    char message[160];
    snprintf(message, sizeof(message),
             "[kodi-ps5] system name: result=0x%08x, name='%s'\n", (unsigned int)r,
             r == 0 ? buf : "");
    sceKernelDebugOutText(0, message);
    logged = 1;
  }
  if (r != 0 || !buf[0])
  {
    buf[0] = '\0';
    return -1;
  }
  return 0;
}

int gethostname(char* name, size_t len)
{
  char own[PS5_SYSTEM_NAME_SIZE];
  if (!name)
  {
    errno = EFAULT;
    return -1;
  }
  system_name(own, sizeof(own)); /* failure keeps the previous result: "" */
  size_t n = strlen(own);
  if (n >= len)
  {
    errno = ENAMETOOLONG;
    return -1;
  }
  memcpy(name, own, n + 1);
  return 0;
}

/* The console's own name -> IPv4 addresses of its up, non-loopback
   interfaces. Returns how many were found; 0 for any other name. */
static int own_host_ipv4(const char* node, struct in_addr* out, int max)
{
  char own[PS5_SYSTEM_NAME_SIZE];
  if (!*node || system_name(own, sizeof(own)) != 0 || strcasecmp(node, own) != 0)
    return 0;
  struct ifaddrs* list = NULL;
  if (getifaddrs(&list) != 0)
  {
    char message[160];
    snprintf(message, sizeof(message), "[kodi-ps5] own host '%s': getifaddrs failed, errno=%d\n",
             node, errno);
    sceKernelDebugOutText(0, message);
    return 0;
  }
  int n = 0;
  for (const struct ifaddrs* ifa = list; ifa && n < max; ifa = ifa->ifa_next)
  {
    if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET || !(ifa->ifa_flags & IFF_UP) ||
        (ifa->ifa_flags & IFF_LOOPBACK))
      continue;
    out[n++] = ((const struct sockaddr_in*)ifa->ifa_addr)->sin_addr;
  }
  freeifaddrs(list);
  char message[160];
  snprintf(message, sizeof(message), "[kodi-ps5] own host '%s': %d local IPv4 address(es)\n",
           node, n);
  sceKernelDebugOutText(0, message);
  return n;
}

static int resolve_ipv4(const char* host, struct in_addr* out)
{
  char message[1024];
  int pool = sceNetPoolCreate("kodi-dns", 0x4000, 0);
  if (pool < 0)
  {
    snprintf(message, sizeof(message),
             "[kodi-ps5] DNS host='%s': sceNetPoolCreate failed, result=0x%08x\n",
             host, (unsigned int)pool);
    sceKernelDebugOutText(0, message);
    return EAI_MEMORY;
  }
  int resolver = sceNetResolverCreate("kodi-dns", pool, 0);
  if (resolver < 0)
  {
    snprintf(message, sizeof(message),
             "[kodi-ps5] DNS host='%s': sceNetResolverCreate failed, result=0x%08x\n",
             host, (unsigned int)resolver);
    sceKernelDebugOutText(0, message);
    sceNetPoolDestroy(pool);
    return EAI_FAIL;
  }
  uint32_t addr = 0;
  int r = sceNetResolverStartNtoa(resolver, host, &addr, 5 * 1000 * 1000, 2, 0);
  char address[INET_ADDRSTRLEN];
  const char* address_text = inet_ntop(AF_INET, &addr, address, sizeof(address));
  snprintf(message, sizeof(message),
           "[kodi-ps5] DNS host='%s': result=0x%08x, address=%s\n",
           host, (unsigned int)r, address_text ? address_text : "<invalid>");
  sceKernelDebugOutText(0, message);
  sceNetResolverDestroy(resolver);
  sceNetPoolDestroy(pool);
  if (r < 0 || addr == 0)
    return EAI_NONAME;
  out->s_addr = addr; /* network byte order */
  return 0;
}

static int parse_service(const char* service, int socktype, uint16_t* port_be)
{
  *port_be = 0;
  if (!service || !*service)
    return 0;
  char* end = NULL;
  long v = strtol(service, &end, 10);
  if (end && *end == '\0' && v >= 0 && v <= 65535)
  {
    *port_be = htons((uint16_t)v);
    return 0;
  }
  static const struct { const char* name; uint16_t port; } known[] = {
      {"http", 80}, {"https", 443}, {"ftp", 21}, {"ssh", 22}, {"smtp", 25}, {"domain", 53},
      {"nfs", 2049}, {"microsoft-ds", 445}, {"netbios-ssn", 139}, {"rtsp", 554}, {"ntp", 123}};
  for (size_t i = 0; i < sizeof(known) / sizeof(known[0]); ++i)
    if (strcmp(service, known[i].name) == 0)
    {
      *port_be = htons(known[i].port);
      return 0;
    }
  (void)socktype;
  return EAI_SERVICE;
}

/* One allocation per entry: addrinfo + sockaddr_storage + canonical name. */
static struct addrinfo* make_entry(int family, const void* addr, uint16_t port_be, int socktype,
                                   int protocol, const char* canon)
{
  size_t clen = canon ? strlen(canon) + 1 : 0;
  struct addrinfo* ai = (struct addrinfo*)calloc(1, sizeof(*ai) + sizeof(struct sockaddr_storage) + clen);
  if (!ai)
    return NULL;
  struct sockaddr_storage* ss = (struct sockaddr_storage*)(ai + 1);
  if (family == AF_INET)
  {
    struct sockaddr_in* sin = (struct sockaddr_in*)ss;
#ifndef __linux__ /* BSD sockaddr length byte (host unit tests use Linux) */
    sin->sin_len = sizeof(*sin);
#endif
    sin->sin_family = AF_INET;
    sin->sin_port = port_be;
    memcpy(&sin->sin_addr, addr, sizeof(sin->sin_addr));
    ai->ai_addrlen = sizeof(*sin);
  }
  else
  {
    struct sockaddr_in6* sin6 = (struct sockaddr_in6*)ss;
#ifndef __linux__
    sin6->sin6_len = sizeof(*sin6);
#endif
    sin6->sin6_family = AF_INET6;
    sin6->sin6_port = port_be;
    memcpy(&sin6->sin6_addr, addr, sizeof(sin6->sin6_addr));
    ai->ai_addrlen = sizeof(*sin6);
  }
  ai->ai_family = family;
  ai->ai_socktype = socktype;
  ai->ai_protocol = protocol;
  ai->ai_addr = (struct sockaddr*)ss;
  if (canon)
  {
    ai->ai_canonname = (char*)(ss + 1);
    memcpy(ai->ai_canonname, canon, clen);
  }
  return ai;
}

void freeaddrinfo(struct addrinfo* ai)
{
  while (ai)
  {
    struct addrinfo* next = ai->ai_next;
    free(ai);
    ai = next;
  }
}

int getaddrinfo(const char* node, const char* service, const struct addrinfo* hints,
                struct addrinfo** res)
{
  char message[1024];
  snprintf(message, sizeof(message),
           "[kodi-ps5] getaddrinfo node='%s', service='%s', family=%d, "
           "socktype=%d, protocol=%d, flags=0x%x\n",
           node ? node : "<null>", service ? service : "<null>",
           hints ? hints->ai_family : AF_UNSPEC, hints ? hints->ai_socktype : 0,
           hints ? hints->ai_protocol : 0, (unsigned int)(hints ? hints->ai_flags : 0));
  sceKernelDebugOutText(0, message);
  if (!res)
    return EAI_FAIL;
  *res = NULL;
  if (!node && !service)
    return EAI_NONAME;
  int family = hints ? hints->ai_family : AF_UNSPEC;
  int flags = hints ? hints->ai_flags : 0;
  int socktype = hints ? hints->ai_socktype : 0;
  int protocol = hints ? hints->ai_protocol : 0;
  if (family != AF_UNSPEC && family != AF_INET && family != AF_INET6)
    return EAI_FAMILY;

  uint16_t port = 0;
  int err = parse_service(service, socktype, &port);
  if (err)
    return err;

  unsigned char addr[PS5_MAX_LOCAL_ADDRS][16];
  int addr_count = 1;
  int addr_family;
  if (!node)
  {
    /* passive: wildcard; active: loopback */
    addr_family = family == AF_INET6 ? AF_INET6 : AF_INET;
    memset(addr[0], 0, sizeof(addr[0]));
    if (!(flags & AI_PASSIVE))
    {
      if (addr_family == AF_INET)
      {
        struct in_addr lo = {htonl(INADDR_LOOPBACK)};
        memcpy(addr[0], &lo, sizeof(lo));
      }
      else
        addr[0][15] = 1;
    }
  }
  else if (family != AF_INET6 && inet_pton(AF_INET, node, addr[0]) == 1)
    addr_family = AF_INET;
  else if (family != AF_INET && inet_pton(AF_INET6, node, addr[0]) == 1)
    addr_family = AF_INET6;
  else
  {
    if (flags & AI_NUMERICHOST)
      return EAI_NONAME;
    if (family == AF_INET6)
      return EAI_NONAME; /* resolver answers A records only */
    struct in_addr local[PS5_MAX_LOCAL_ADDRS];
    int local_count = own_host_ipv4(node, local, PS5_MAX_LOCAL_ADDRS);
    if (local_count > 0)
    {
      for (int i = 0; i < local_count; ++i)
        memcpy(addr[i], &local[i], sizeof(local[i]));
      addr_count = local_count;
    }
    else
    {
      struct in_addr in;
      err = resolve_ipv4(node, &in);
      if (err)
        return err;
      memcpy(addr[0], &in, sizeof(in));
    }
    addr_family = AF_INET;
  }

  const char* canon = (flags & AI_CANONNAME) && node ? node : NULL;
  static const int types[2][2] = {{SOCK_STREAM, IPPROTO_TCP}, {SOCK_DGRAM, IPPROTO_UDP}};
  struct addrinfo* head = NULL;
  struct addrinfo** tail = &head;
  for (int a = 0; a < addr_count; ++a)
  {
    int added = 0;
    for (int i = 0; i < 2; ++i)
    {
      if (socktype && socktype != types[i][0])
        continue;
      int proto = protocol ? protocol : types[i][1];
      struct addrinfo* ai = make_entry(addr_family, addr[a], port, types[i][0], proto, canon);
      if (!ai)
      {
        freeaddrinfo(head);
        return EAI_MEMORY;
      }
      canon = NULL; /* canonical name on the first entry only */
      *tail = ai;
      tail = &ai->ai_next;
      added = 1;
    }
    if (!added) /* unusual socktype (e.g. SOCK_RAW): one entry as asked */
    {
      struct addrinfo* ai = make_entry(addr_family, addr[a], port, socktype, protocol, canon);
      if (!ai)
      {
        freeaddrinfo(head);
        return EAI_MEMORY;
      }
      canon = NULL;
      *tail = ai;
      tail = &ai->ai_next;
    }
  }
  *res = head;
  return 0;
}

int getnameinfo(const struct sockaddr* sa, socklen_t salen, char* host, size_t hostlen, char* serv,
                size_t servlen, int flags)
{
  /* No reverse DNS: always numeric. */
  if (!sa)
    return EAI_FAIL;
  const void* addr;
  uint16_t port;
  if (sa->sa_family == AF_INET && salen >= sizeof(struct sockaddr_in))
  {
    addr = &((const struct sockaddr_in*)sa)->sin_addr;
    port = ((const struct sockaddr_in*)sa)->sin_port;
  }
  else if (sa->sa_family == AF_INET6 && salen >= sizeof(struct sockaddr_in6))
  {
    addr = &((const struct sockaddr_in6*)sa)->sin6_addr;
    port = ((const struct sockaddr_in6*)sa)->sin6_port;
  }
  else
    return EAI_FAMILY;
  if (host && hostlen)
  {
    if (flags & NI_NAMEREQD)
      return EAI_NONAME;
    if (!inet_ntop(sa->sa_family, addr, host, (socklen_t)hostlen))
      return EAI_OVERFLOW;
  }
  if (serv && servlen)
  {
    if (snprintf(serv, servlen, "%u", (unsigned)ntohs(port)) >= (int)servlen)
      return EAI_OVERFLOW;
  }
  return 0;
}

const char* gai_strerror(int ecode)
{
  switch (ecode)
  {
    case 0: return "Success";
    case EAI_AGAIN: return "Temporary failure in name resolution";
    case EAI_BADFLAGS: return "Invalid value for ai_flags";
    case EAI_FAIL: return "Non-recoverable failure in name resolution";
    case EAI_FAMILY: return "ai_family not supported";
    case EAI_MEMORY: return "Memory allocation failure";
    case EAI_NONAME: return "Name does not resolve";
    case EAI_SERVICE: return "Service not supported";
    case EAI_SOCKTYPE: return "ai_socktype not supported";
    case EAI_OVERFLOW: return "Argument buffer overflow";
    default: return "Unknown name resolution error";
  }
}

/* h_errno is (*__h_errno()) in FreeBSD's <netdb.h>; the function, too, was
   only in the WebKit module. Per-thread, like the real one. */
int* __h_errno(void)
{
  static _Thread_local int value;
  return &value;
}

struct hostent* gethostbyname(const char* name)
{
  static struct in_addr addr;
  static char* addr_list[2];
  static char* aliases[1];
  static char hname[256];
  static struct hostent he;
  if (!name)
    return NULL;
  if (inet_pton(AF_INET, name, &addr) != 1 && resolve_ipv4(name, &addr) != 0)
  {
    h_errno = HOST_NOT_FOUND;
    return NULL;
  }
  snprintf(hname, sizeof(hname), "%s", name);
  addr_list[0] = (char*)&addr;
  addr_list[1] = NULL;
  aliases[0] = NULL;
  he.h_name = hname;
  he.h_aliases = aliases;
  he.h_addrtype = AF_INET;
  he.h_length = sizeof(addr);
  he.h_addr_list = addr_list;
  return &he;
}
