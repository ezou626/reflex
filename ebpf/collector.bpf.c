// #include <linux/bpf.h>
// #include <linux/version.h>
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>

/* Struct to store event */


/* Code snippet courtesy of falco.org */
/* This version will send data to userspace using a ringbuf */

struct{
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256+1024);
} ringbuf SEC(".maps"); // Basically puts in the map section of .o 
    // for the loader library (libbpf) to use

struct execve_params{
    __u64 __unused;
    __u64 __unused2;
    char* filename;
};

struct event{
    int pid;
    char filename[512];
};


SEC("tp/syscalls/sys_enter_execve")
int detect_execve(struct execve_params* params)
{
    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    struct event* evt = bpf_ringbuf_reserve(&ringbuf, sizeof(struct event), 0);
    if (!evt) {
        bpf_printk("bpf_ringbuf_reserve failed\n");
        return 1;
    }
    evt->pid = pid;
    bpf_probe_read_user_str(evt->filename, sizeof(evt->filename), params->filename);
    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char _license[] SEC("license") = "GPL";
