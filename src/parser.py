import sys
import struct
import re

STRUCT_FORMAT = "I Q Q q Q"
STRUCT_SIZE = struct.calcsize(STRUCT_FORMAT)


# def get_syscalls(header_path="/usr/include/unistd.h"):
#     syscalls = {}
#     pattern = re.compile(r"#define\s+__NR_(\w+)\s+(\d+)")

#     try:
#         with open(header_path, "r") as f:
#             for line in f:
#                 match = pattern.match(line)
#                 if match:
#                     name, nr = match.groups()
#                     syscalls[int(nr)] = name
#     except FileNotFoundError:
#         print("Header file not found")
#         return {}

def get_syscall_name(syscall_id, table):
    return table.get(syscall_id, f"unkown({syscall_id})")

sc_dict = {
        0: "read", 1: "write", 2: "open", 3: "close", 4: "stat",
        5: "fstat", 6: "lstat", 7: "poll", 8: "lseek", 9: "mmap",
        10: "mprotect", 11: "munmap", 12: "brk", 13: "rt_sigaction",
        14: "rt_sigprocmask", 15: "rt_sigreturn", 16: "ioctl", 17: "pread64",
        18: "pwrite64", 19: "readv", 20: "writev", 21: "access", 22: "pipe",
        23: "select", 24: "sched_yield", 25: "mremap", 26: "msync",
        27: "mincore", 28: "madvise", 29: "shmget", 30: "shmat", 31: "shmctl",
        32: "dup", 33: "dup2", 34: "pause", 35: "nanosleep", 39: "getpid",
        41: "socket", 42: "connect", 43: "accept", 44: "sendto", 45: "recvfrom",
        46: "sendmsg", 47: "recvmsg", 48: "shutdown", 49: "bind", 50: "listen",
        51: "getsockname", 52: "getpeername", 53: "socketpair", 54: "setsockopt",
        55: "getsockopt", 56: "clone", 57: "fork", 58: "vfork", 59: "execve",
        60: "exit", 61: "wait4", 62: "kill", 63: "uname", 72: "fcntl",
        78: "getdents", 79: "getcwd", 80: "chdir", 89: "readlink", 137: "statfs",
        202: "futex", 217: "getdents64", 228: "clock_gettime", 231: "exit_group",
        257: "openat", 258: "mkdirat", 262: "fstatat", 281: "epoll_wait",
        288: "accept4", 290: "eventfd2", 293: "pipe2", 318: "getrandom"
    }

try:
    while True:
        raw_data = sys.stdin.buffer.read(STRUCT_SIZE)
        if not raw_data:
            break
        data = struct.unpack(STRUCT_FORMAT, raw_data)
        # set to the right variables
        tid, syscall_id, cgroup_id, ret_val, dur_ns = data
        if syscall_id not in sc_dict:
            continue
        name = get_syscall_name(syscall_id, sc_dict)
        print(f"HI: tid {tid} called {name}")
except KeyboardInterrupt:
    pass

