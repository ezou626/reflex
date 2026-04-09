/* Reflex Phase-1 telemetry:
 * - process churn: fork/exec/exit
 * - context switch rate
 * - syscall errors (from sys_exit ret < 0 in userspace)
 * - wakeup->oncpu latency (basic run-queue wait)
 */

#include <linux/sched.h>

enum event_type {
  EVENT_EXEC = 1,
  EVENT_FORK = 2,
  EVENT_EXIT = 3,
  EVENT_SCHED_SWITCH = 4,
  EVENT_SYSCALL_EXIT = 5,
  EVENT_RQ_LATENCY = 6,
};

struct mvp_event {
  u32 event_type;
  u32 cpu;
  u32 pid;
  u32 tgid;
  u64 ts_ns;
  s32 value_i32;
  u32 value_u32;
  char comm[TASK_COMM_LEN];
};

BPF_RINGBUF_OUTPUT(events, 1 << 12);
BPF_HASH(wakeup_start_ns, u32, u64, 16384);

static __always_inline void fill_common(struct mvp_event *evt, u32 event_type) {
  u64 pid_tgid = bpf_get_current_pid_tgid();
  evt->event_type = event_type;
  evt->cpu = bpf_get_smp_processor_id();
  evt->pid = pid_tgid >> 32;
  evt->tgid = (u32)pid_tgid;
  evt->ts_ns = bpf_ktime_get_ns();
  bpf_get_current_comm(&evt->comm, sizeof(evt->comm));
}

TRACEPOINT_PROBE(sched, sched_process_exec) {
  struct mvp_event evt = {};
  fill_common(&evt, EVENT_EXEC);
  events.ringbuf_output(&evt, sizeof(evt), 0);
  return 0;
}

TRACEPOINT_PROBE(sched, sched_process_fork) {
  struct mvp_event evt = {};
  fill_common(&evt, EVENT_FORK);
  evt.value_i32 = args->parent_pid;
  evt.value_u32 = args->child_pid;
  events.ringbuf_output(&evt, sizeof(evt), 0);
  return 0;
}

TRACEPOINT_PROBE(sched, sched_process_exit) {
  struct mvp_event evt = {};
  fill_common(&evt, EVENT_EXIT);
  events.ringbuf_output(&evt, sizeof(evt), 0);
  return 0;
}

TRACEPOINT_PROBE(sched, sched_wakeup) {
  u32 pid = args->pid;
  u64 ts_ns = bpf_ktime_get_ns();
  wakeup_start_ns.update(&pid, &ts_ns);
  return 0;
}

TRACEPOINT_PROBE(sched, sched_switch) {
  struct mvp_event evt = {};
  fill_common(&evt, EVENT_SCHED_SWITCH);
  evt.value_i32 = args->prev_pid;
  evt.value_u32 = args->next_pid;
  events.ringbuf_output(&evt, sizeof(evt), 0);

  u32 next_pid = args->next_pid;
  u64 *start_ns = wakeup_start_ns.lookup(&next_pid);
  if (start_ns != 0) {
    struct mvp_event lat_evt = {};
    u64 now_ns = bpf_ktime_get_ns();
    fill_common(&lat_evt, EVENT_RQ_LATENCY);
    lat_evt.pid = next_pid;
    lat_evt.value_u32 = (u32)((now_ns - *start_ns) / 1000);
    events.ringbuf_output(&lat_evt, sizeof(lat_evt), 0);
    wakeup_start_ns.delete(&next_pid);
  }
  return 0;
}

TRACEPOINT_PROBE(raw_syscalls, sys_exit) {
  struct mvp_event evt = {};
  fill_common(&evt, EVENT_SYSCALL_EXIT);
  evt.value_u32 = args->id;
  evt.value_i32 = args->ret;
  events.ringbuf_output(&evt, sizeof(evt), 0);
  return 0;
}
