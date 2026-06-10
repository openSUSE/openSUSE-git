#define _GNU_SOURCE
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <dlfcn.h>

uid_t geteuid(void) {
    return 499; /* postgres UID */
}

uid_t getuid(void) {
    return 499; /* postgres UID */
}

gid_t getegid(void) {
    return 498; /* postgres GID */
}

gid_t getgid(void) {
    return 498; /* postgres GID */
}

int stat(const char *path, struct stat *buf) {
    int (*orig_stat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "stat");
    int ret = orig_stat(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int lstat(const char *path, struct stat *buf) {
    int (*orig_lstat)(const char *, struct stat *) = dlsym(RTLD_NEXT, "lstat");
    int ret = orig_lstat(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int fstat(int fd, struct stat *buf) {
    int (*orig_fstat)(int, struct stat *) = dlsym(RTLD_NEXT, "fstat");
    int ret = orig_fstat(fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __xstat(int ver, const char *path, struct stat *buf) {
    int (*orig_xstat)(int, const char *, struct stat *) = dlsym(RTLD_NEXT, "__xstat");
    int ret = orig_xstat(ver, path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __lxstat(int ver, const char *path, struct stat *buf) {
    int (*orig_lxstat)(int, const char *, struct stat *) = dlsym(RTLD_NEXT, "__lxstat");
    int ret = orig_lxstat(ver, path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int __fxstat(int ver, int fd, struct stat *buf) {
    int (*orig_fxstat)(int, int, struct stat *) = dlsym(RTLD_NEXT, "__fxstat");
    int ret = orig_fxstat(ver, fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int stat64(const char *path, struct stat64 *buf) {
    int (*orig_stat64)(const char *, struct stat64 *) = dlsym(RTLD_NEXT, "stat64");
    int ret = orig_stat64(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int lstat64(const char *path, struct stat64 *buf) {
    int (*orig_lstat64)(const char *, struct stat64 *) = dlsym(RTLD_NEXT, "lstat64");
    int ret = orig_lstat64(path, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}

int fstat64(int fd, struct stat64 *buf) {
    int (*orig_fstat64)(int, struct stat64 *) = dlsym(RTLD_NEXT, "fstat64");
    int ret = orig_fstat64(fd, buf);
    if (ret == 0) {
        buf->st_uid = 499;
        buf->st_gid = 498;
    }
    return ret;
}
