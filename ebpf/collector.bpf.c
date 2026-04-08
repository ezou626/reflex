// #include <linux/bpf.h>
// #include <linux/version.h>
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>

/* Struct to store event */


/* Code snippet adopted in part from falco.org */
/* This version will send data to userspace using a ringbuf */




struct payload{
    __u32 pid;
    __u64 syscall_id;
    __u64 cgroup_id; // for scheduling?
    __u64 ts_ns;
    __u64 args[6];
};

/* temporary storage hashmap for enter */

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 10240);
  __type(key, __u32); // keyed by thread id (single thread waits for syscall execution)
  __type(value, __u64); // val start timestamp (stored)
} enter_parking SEC(".maps");


struct{
    __uint(type, BPF_MAP_TYPE_RINGBUF); // macro to initialize pointer with specific size to pass info
    __uint(max_entries, 256 * 1024);
} syscall_info_buffer SEC(".maps"); // Basically puts in the map section of .o 
    // for the loader library (libbpf) to use

// tp and tracepoint are interchangable
// see tps.txt for full list, but if you are using RAW tracepoints theres just enter and exit

// SEC("raw_tp/sys_enter")
// this is the true raw version which handles the registers ^^ 


SEC("tp/raw_syscalls/sys_enter")
int detect_syscall_enter(struct trace_event_raw_sys_enter *ctx) {
  __u32 tid = bpf_get_current_pid_tgid();
  __u64 ts = bpf_ktime_get_ns();

  bpf_map_update_elem(&start_times, &tid, &ts, BPF_ANY)
}




// trace_event_raw_sys_enter defined in header i think
int detect_syscall_enter(struct trace_event_raw_sys_enter *ctx) { 
  // need to reserve some space first
  struct payload *pl = bpf_ringbuf_reserve(&syscall_info_buffer, sizeof(*pl), 0);
  if (!evt) return 0;

  pl->ts_ns = bpf_ktime_get_ns();
  pl->pid = bpf_get_current_pid_tgid() >> 32;
  pl->syscall_id = ctx->id;
  pl->cgroup_id = bpf_get_current_cgroup_id();

  #pragma unroll
  for (int i = 0; i < 6; i++) {
    evt->args[i] = ctx->args[i];
  }
  bpf_ringbuf_submit(evt, 0);
  return 0;
}

// now the exit so we can look at syscall latency
SEC("tp/raw_syscalls/sys_exit")
int detect_syscall_exit(struct )


// tp/raw_syscalls/sys_enter is a standard tracepoint not a raw tracepoint

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
