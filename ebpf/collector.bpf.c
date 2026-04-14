// #include <linux/bpf.h>
// #include <linux/version.h>
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>

/* Struct to store event */


/* Code snippet adopted in part from falco.org */
/* This version will send data to userspace using a ringbuf */

volatile const __u32 python_pid = 0;
volatile const __u32 loader_pid = 0;


struct payload{
    __u32 tid;
    __u32 pid;
    __u64 syscall_id;
    __u64 cgroup_id; // for scheduling?
    __s64 ret_val;
    __u64 dur_ns;
    // __u64 args[6];
} __attribute__((packed)); // need attribute packed so 32bit one doesnt mess it up

/* temporary storage hashmap for enter */

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 10240);
  __type(key, u32); // keyed by thread id (single thread waits for syscall execution)
  __type(value, u64); // val start timestamp (stored)
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
  u32 tid = bpf_get_current_pid_tgid();
  u64 ts = bpf_ktime_get_ns();
  u32 pid = bpf_get_current_pid_tgid() >> 32;

  if (pid == python_pid || pid == loader_pid) {
    return 0; // drop / exit early if its catching the program itself
  }

  bpf_map_update_elem(&enter_parking, &tid, &ts, BPF_ANY);
  return 0;

  /* no longer sending return vals to user space for enter */
  //   #pragma unroll
  // for (int i = 0; i < 6; i++) {
  //   evt->args[i] = ctx->args[i];
  // }
  // bpf_ringbuf_submit(evt, 0);
  // return 0;
}



// now the exit so we can look at syscall latency

SEC("tp/raw_syscalls/sys_exit")
int detect_syscall_exit(struct trace_event_raw_sys_exit *ctx) { 
  // need to reserve some space first


  u32 tid = bpf_get_current_pid_tgid();
  u64 *start_ts = bpf_map_lookup_elem(&enter_parking, &tid);
  if (!start_ts) return 0; // skip if no entry

  struct payload *pl = bpf_ringbuf_reserve(&syscall_info_buffer, sizeof(*pl), 0);
  if (pl) {
    pl->tid = tid;
    pl->pid = bpf_get_current_pid_tgid() >> 32;
    pl->syscall_id = ctx->id;
    pl->ret_val = ctx->ret;
    pl->dur_ns = bpf_ktime_get_ns() - *start_ts;

    bpf_ringbuf_submit(pl, 0);
  }
  bpf_map_delete_elem(&enter_parking, &tid);
  return 0;
}

// tp/raw_syscalls/sys_enter is a standard tracepoint not a raw tracepoint

char _license[] SEC("license") = "GPL";
