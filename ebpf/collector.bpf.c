// #include <linux/bpf.h>
// #include <linux/version.h>
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>

/* Struct to store event */


/* Code snippet adopted in part from falco.org */
/* This version will send data to userspace using a ringbuf */

volatile const __u32 python_pid = 0;
volatile const __u32 loader_pid = 0;
volatile const __u8  use_cgroup_filter = 0;


/* Unified event payload — layout is naturally 48 bytes, no padding needed.
 * value_i32/value_u32 are event-specific (see event type constants below).
 * Matches what daemon/main.py expects via struct.unpack. */
#define EVENT_EXEC         1
#define EVENT_FORK         2
#define EVENT_EXIT         3
#define EVENT_SCHED_SWITCH 4
#define EVENT_SYSCALL_EXIT 5
#define EVENT_RQ_LATENCY   6

struct payload {
    __u32 event_type;
    __u32 cpu;
    __u32 pid;
    __u32 tgid;     // thread id (kernel naming: tgid=process, pid=thread)
    __u64 ts_ns;
    __s32 value_i32; // event-specific: fork=parent_pid, switch=prev_pid, syscall=ret
    __u32 value_u32; // event-specific: fork=child_pid, switch=next_pid, syscall=id, rq=lat_us
    char  comm[16];
};

/* temporary storage hashmap for enter */

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 10240);
  __type(key, u32); // keyed by thread id (single thread waits for syscall execution)
  __type(value, u64); // val start timestamp (stored)
} enter_parking SEC(".maps");

/* wakeup timestamp storage for rq latency, keyed by pid */
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 16384);
  __type(key, u32);
  __type(value, u64);
} wakeup_ts SEC(".maps");

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 256);
  __type(key, u64);   // cgroup id
  __type(value, u8);  // presence flag
} cgroup_whitelist SEC(".maps"); // for passing the programs to track to kernel side


struct{
    __uint(type, BPF_MAP_TYPE_RINGBUF); // macro to initialize pointer with specific size to pass info
    __uint(max_entries, 256 * 1024);
} events SEC(".maps"); // Basically puts in the map section of .o
    // for the loader library (libbpf) to use

// tp and tracepoint are interchangable
// see tps.txt for full list, but if you are using RAW tracepoints theres just enter and exit

// SEC("raw_tp/sys_enter")
// this is the true raw version which handles the registers ^^


/* alloc and fill common fields for sched/process events.
 * no cgroup filter here — these are system-wide scheduler metrics. */
static __always_inline struct payload *alloc_sched_event(__u32 type) {
    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 pid = ptg >> 32;
    if (pid == python_pid || pid == loader_pid)
        return NULL;

    struct payload *e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
    if (!e)
        return NULL;

    e->event_type = type;
    e->cpu        = bpf_get_smp_processor_id();
    e->pid        = pid;
    e->tgid       = (__u32)ptg;
    e->ts_ns      = bpf_ktime_get_ns();
    e->value_i32  = 0;
    e->value_u32  = 0;
    bpf_get_current_comm(&e->comm, sizeof(e->comm));
    return e;
}

SEC("tp/sched/sched_process_exec")
int handle_exec(struct trace_event_raw_sched_process_exec *ctx) {
    struct payload *e = alloc_sched_event(EVENT_EXEC);
    if (e) bpf_ringbuf_submit(e, 0);
    return 0;
}

SEC("tp/sched/sched_process_fork")
int handle_fork(struct trace_event_raw_sched_process_fork *ctx) {
    struct payload *e = alloc_sched_event(EVENT_FORK);
    if (e) {
        e->value_i32 = ctx->parent_pid;
        e->value_u32 = ctx->child_pid;
        bpf_ringbuf_submit(e, 0);
    }
    return 0;
}

SEC("tp/sched/sched_process_exit")
int handle_exit(struct trace_event_raw_sched_process_template *ctx) {
    struct payload *e = alloc_sched_event(EVENT_EXIT);
    if (e) bpf_ringbuf_submit(e, 0);
    return 0;
}

/* record wakeup timestamp for rq latency calculation */
SEC("tp/sched/sched_wakeup")
int handle_wakeup(struct trace_event_raw_sched_wakeup_template *ctx) {
    __u32 pid = ctx->pid;
    __u64 ts  = bpf_ktime_get_ns();
    bpf_map_update_elem(&wakeup_ts, &pid, &ts, BPF_ANY);
    return 0;
}

SEC("tp/sched/sched_switch")
int handle_sched_switch(struct trace_event_raw_sched_switch *ctx) {
    // sched_switch events not emitted individually — too high volume.
    // context switch rate is derived from sched_switch count in the decision engine if needed.

    /* compute wakeup->oncpu latency for the task being switched in */
    __u32 next_pid = ctx->next_pid;
    __u64 *start = bpf_map_lookup_elem(&wakeup_ts, &next_pid);
    if (start) {
        __u64 now = bpf_ktime_get_ns();
        __u32 lat_us = (__u32)((now - *start) / 1000);
        if (lat_us >= 100) { // drop sub-100us wakeups, only emit meaningful delays
            struct payload *lat = bpf_ringbuf_reserve(&events, sizeof(*lat), 0);
            if (lat) {
                lat->event_type = EVENT_RQ_LATENCY;
                lat->cpu        = bpf_get_smp_processor_id();
                lat->pid        = next_pid;
                lat->tgid       = 0;
                lat->ts_ns      = now;
                lat->value_i32  = 0;
                lat->value_u32  = lat_us;
                bpf_get_current_comm(&lat->comm, sizeof(lat->comm));
                bpf_ringbuf_submit(lat, 0);
            }
        }
        bpf_map_delete_elem(&wakeup_ts, &next_pid);
    }
    return 0;
}


SEC("tp/raw_syscalls/sys_enter")
int detect_syscall_enter(struct trace_event_raw_sys_enter *ctx) {
  __u64 ptg = bpf_get_current_pid_tgid();
  __u32 tid = (__u32)ptg;
  __u32 pid = ptg >> 32;

  if (pid == python_pid || pid == loader_pid) {
    return 0; // drop / exit early if its catching the program itself
  }

  /* here is where filtering only on benchmarked programs is added (this is for )*/

  if (use_cgroup_filter) {
    __u64 cgid = bpf_get_current_cgroup_id();
    u8 *allowed = bpf_map_lookup_elem(&cgroup_whitelist, &cgid);
    if (!allowed) return 0;  // not whitelisted, drop
  }

  __u64 ts = bpf_ktime_get_ns();
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

  __u64 ptg = bpf_get_current_pid_tgid();
  __u32 tid = (__u32)ptg;

  __u64 *start_ts = bpf_map_lookup_elem(&enter_parking, &tid);
  if (!start_ts) return 0; // skip if no entry

  struct payload *pl = bpf_ringbuf_reserve(&events, sizeof(*pl), 0);
  if (pl) {
    pl->event_type = EVENT_SYSCALL_EXIT;
    pl->cpu        = bpf_get_smp_processor_id();
    pl->pid        = (__u32)(ptg >> 32);
    pl->tgid       = tid;
    pl->ts_ns      = bpf_ktime_get_ns();
    pl->value_u32  = (__u32)ctx->id;
    pl->value_i32  = (__s32)ctx->ret;
    bpf_get_current_comm(&pl->comm, sizeof(pl->comm));

    bpf_ringbuf_submit(pl, 0);
  }
  bpf_map_delete_elem(&enter_parking, &tid);
  return 0;
}

// tp/raw_syscalls/sys_enter is a standard tracepoint not a raw tracepoint

char _license[] SEC("license") = "GPL";
