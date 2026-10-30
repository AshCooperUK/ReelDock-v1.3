/* ReelDock native socket broker: unprivileged children never set firewall marks. */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/un.h>
#include <dlfcn.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <stdint.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <poll.h>
#include <stdio.h>
#include <strings.h>
#include <time.h>

#ifndef REELDOCK_SOCKET_PATH
#define REELDOCK_SOCKET_PATH "/vpn-run/socket.sock"
#endif
#ifndef REELDOCK_DNS_PATH
#define REELDOCK_DNS_PATH "/vpn-run/dns.sock"
#endif
#ifndef REELDOCK_DNS_TIMEOUT_MS
#define REELDOCK_DNS_TIMEOUT_MS 5000
#endif

static int traffic_mark(void) {
    const char *kind = getenv("REELDOCK_TRAFFIC");
    if (!kind) return 0xD01;
    if (!strcmp(kind,"metadata")) return 0xD02;
    if (!strcmp(kind,"youtube")) return 0xD11;
    if (!strcmp(kind,"nzb")) return 0xD12;
    if (!strcmp(kind,"torrent")) return 0xD13;
    if (!strcmp(kind,"vod")) return 0xD14;
    return 0xD01;
}

static int64_t milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC,&now)) return -1;
    return (int64_t)now.tv_sec*1000 + now.tv_nsec/1000000;
}

static int wait_io(int descriptor, short event, int64_t deadline) {
    for (;;) {
        int64_t remaining=deadline-milliseconds();
        if (remaining<=0) return -1;
        struct pollfd item={.fd=descriptor,.events=event};
        int count=poll(&item,1,(int)remaining);
        if (count>0) return item.revents & event ? 0 : -1;
        if (!count || errno!=EINTR) return -1;
    }
}

static int transfer(int descriptor, void *buffer, size_t length, int sending, int64_t deadline) {
    size_t done=0;
    while (done<length) {
        if (milliseconds()>=deadline) return -1;
        ssize_t count=sending ? send(descriptor,(char*)buffer+done,length-done,MSG_NOSIGNAL|MSG_DONTWAIT)
                             : recv(descriptor,(char*)buffer+done,length-done,MSG_DONTWAIT);
        if (count>0) {done+=(size_t)count;continue;}
        if (count<0 && errno==EINTR) continue;
        if (count<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) {
            if (!wait_io(descriptor,sending ? POLLOUT : POLLIN,deadline)) continue;
        }
        return -1;
    }
    return 0;
}

static int broker_open(const char *path, int64_t deadline) {
    int (*original)(int,int,int)=dlsym(RTLD_NEXT,"socket");
    if (!original) return -1;
    int descriptor=original(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (descriptor<0) return -1;
    struct sockaddr_un address={.sun_family=AF_UNIX};
    if (strlen(path)>=sizeof(address.sun_path)) {close(descriptor);return -1;}
    strcpy(address.sun_path,path);
    if (connect(descriptor,(struct sockaddr*)&address,sizeof(address))) {
        if (errno!=EINPROGRESS || wait_io(descriptor,POLLOUT,deadline)) {close(descriptor);return -1;}
        int error=0;socklen_t length=sizeof(error);
        if (getsockopt(descriptor,SOL_SOCKET,SO_ERROR,&error,&length) || error) {close(descriptor);return -1;}
    }
    return descriptor;
}

static void close_received(struct msghdr *message) {
    for (struct cmsghdr *header=CMSG_FIRSTHDR(message);header;header=CMSG_NXTHDR(message,header)) {
        if (header->cmsg_level!=SOL_SOCKET || header->cmsg_type!=SCM_RIGHTS
                || header->cmsg_len<CMSG_LEN(0)
                || (char*)header+header->cmsg_len>(char*)message->msg_control+message->msg_controllen) continue;
        size_t count=(header->cmsg_len-CMSG_LEN(0))/sizeof(int);
        for (size_t index=0;index<count;index++) {
            int descriptor;
            memcpy(&descriptor,(char*)CMSG_DATA(header)+index*sizeof(int),sizeof(descriptor));
            if (descriptor>=0) close(descriptor);
        }
    }
}

int socket(int domain, int type, int protocol) {
    int (*original)(int,int,int)=dlsym(RTLD_NEXT,"socket");
    if (!original) {errno=ENETDOWN;return -1;}
    if (domain!=AF_INET && domain!=AF_INET6) return original(domain,type,protocol);
    int64_t deadline=milliseconds()+3000;
    int control=broker_open(REELDOCK_SOCKET_PATH,deadline);
    if (control<0) {errno=ENETDOWN;return -1;}
    int32_t request[4]={domain,type,protocol,traffic_mark()};
    if (transfer(control,request,sizeof(request),1,deadline) || wait_io(control,POLLIN,deadline)) {
        close(control);errno=ENETDOWN;return -1;
    }
    char byte=0;struct iovec io={.iov_base=&byte,.iov_len=1};
    char ancillary[CMSG_SPACE(sizeof(int))];
    memset(ancillary,0,sizeof(ancillary));
    struct msghdr message={.msg_iov=&io,.msg_iovlen=1,.msg_control=ancillary,.msg_controllen=sizeof(ancillary)};
    int count=recvmsg(control,&message,MSG_CMSG_CLOEXEC);close(control);
    struct cmsghdr *header=CMSG_FIRSTHDR(&message);
    if (count!=1 || byte!='1' || message.msg_flags & MSG_CTRUNC || !header
            || header->cmsg_level!=SOL_SOCKET || header->cmsg_type!=SCM_RIGHTS
            || header->cmsg_len!=CMSG_LEN(sizeof(int)) || CMSG_NXTHDR(&message,header)) {
        close_received(&message);
        errno=ENETDOWN;return -1;
    }
    int fd=-1;memcpy(&fd,CMSG_DATA(header),sizeof(fd));
    if (fd<0) {errno=ENETDOWN;return -1;}
    if (!(type & SOCK_CLOEXEC)) fcntl(fd,F_SETFD,0);
    return fd;
}

struct dns_record {int32_t family;unsigned char address[16];};

static int static_addresses(const char *host, struct dns_record *records, char *canonical) {
    /* Never give libc a hostname: even an /etc/hosts match can fall through
       to external NSS DNS when a requested family/address configuration differs. */
    FILE *file=fopen("/etc/hosts","r");
    if (!file) return 0;
    char line[1024];size_t total=0;int count=0;
    while (fgets(line,sizeof(line),file) && (total+=strlen(line))<=65536) {
        char *comment=strchr(line,'#');if (comment) *comment=0;
        char *context=NULL,*part=strtok_r(line," \t\r\n",&context);
        struct dns_record record={0};
        if (!part) continue;
        if (inet_pton(AF_INET,part,record.address)==1) record.family=AF_INET;
        else if (inet_pton(AF_INET6,part,record.address)==1) record.family=AF_INET6;
        else continue;
        char name[254]={0};int matched=0;
        while ((part=strtok_r(NULL," \t\r\n",&context))) {
            if (!name[0] && strlen(part)<=253) strcpy(name,part);
            if (!strcasecmp(host,part)) matched=1;
        }
        if (matched && count<128) {
            records[count++]=record;
            if (!canonical[0]) strcpy(canonical,name);
        }
    }
    fclose(file);return count;
}

int getaddrinfo(const char *host, const char *service, const struct addrinfo *hints, struct addrinfo **result) {
    int (*original)(const char*,const char*,const struct addrinfo*,struct addrinfo**)=dlsym(RTLD_NEXT,"getaddrinfo");
    void (*release)(struct addrinfo*)=dlsym(RTLD_NEXT,"freeaddrinfo");
    if (!original || !release || !result) return EAI_FAIL;
    *result=NULL;
    struct addrinfo options={0};
    if (hints) options=*hints;
    else options.ai_flags=AI_V4MAPPED|AI_ADDRCONFIG; /* Bundled glibc's NULL-hints defaults. */
    struct addrinfo numeric=options;numeric.ai_flags|=AI_NUMERICHOST;
    int status=original(host,service,&numeric,result);
    if (!status) {
        for (struct addrinfo *row=*result;row;row=row->ai_next) row->ai_flags=options.ai_flags;
        return 0;
    }
    if (!host || options.ai_flags & AI_NUMERICHOST || (status!=EAI_NONAME && status!=EAI_NODATA)) return status;
    struct dns_record records[128];char canonical[254]={0};
    int count=static_addresses(host,records,canonical);
    if (count) goto build_results;
    size_t host_length=strlen(host);
    if (!host_length || host_length>1024) return EAI_NONAME;
    int64_t deadline=milliseconds()+REELDOCK_DNS_TIMEOUT_MS;
    int control=broker_open(REELDOCK_DNS_PATH,deadline);
    if (control<0) return EAI_AGAIN;
    int32_t request[4]={traffic_mark(),options.ai_family,options.ai_flags,(int32_t)host_length};
    int32_t response[3];
    if (transfer(control,request,sizeof(request),1,deadline) || transfer(control,(void*)host,host_length,1,deadline)
            || transfer(control,response,sizeof(response),0,deadline)) {close(control);return EAI_AGAIN;}
    count=response[1];int canonical_length=response[2];
    if (response[0]<0 || response[0]>2 || count<0 || count>128 || canonical_length<0 || canonical_length>253
            || (response[0] && count) || (!response[0] && !count)) {close(control);return EAI_AGAIN;}
    if (response[0]) {close(control);return response[0]==1 ? EAI_NONAME : EAI_AGAIN;}
    if (transfer(control,records,(size_t)count*sizeof(records[0]),0,deadline)
            || transfer(control,canonical,(size_t)canonical_length,0,deadline)) {close(control);return EAI_AGAIN;}
    close(control);
    if (memchr(canonical,0,(size_t)canonical_length)) return EAI_AGAIN;
build_results:;
    int ipv6=0;
    for (int index=0;index<count;index++) {
        if (records[index].family!=AF_INET && records[index].family!=AF_INET6) return EAI_AGAIN;
        if (records[index].family==AF_INET6) ipv6=1;
    }
    struct addrinfo *head=NULL,*tail=NULL;
    numeric.ai_flags=(options.ai_flags|AI_NUMERICHOST)&~AI_CANONNAME;
    for (int index=0;index<count;index++) {
        if (records[index].family==AF_INET && options.ai_family==AF_INET6 && ipv6 && !(options.ai_flags & AI_ALL)) continue;
        char address[INET6_ADDRSTRLEN];
        if (!inet_ntop(records[index].family,records[index].address,address,sizeof(address))) {if (head) release(head);return EAI_AGAIN;}
        struct addrinfo *rows=NULL;
        status=original(address,service,&numeric,&rows);
        if (status) {
            if (status==EAI_NONAME || status==EAI_NODATA || status==EAI_ADDRFAMILY) continue;
            if (head) release(head);
            return status;
        }
        for (struct addrinfo *row=rows;row;row=row->ai_next) row->ai_flags=options.ai_flags;
        if (tail) tail->ai_next=rows;else head=rows;
        tail=rows;while (tail->ai_next) tail=tail->ai_next;
    }
    if (!head) return EAI_NONAME;
    if (options.ai_flags & AI_CANONNAME) {
        head->ai_canonname=strdup(canonical[0] ? canonical : host);
        if (!head->ai_canonname) {release(head);return EAI_MEMORY;}
    }
    *result=head;return 0;
}
