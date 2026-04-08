/* Minimal eBPF program: push exec events to a BPF ring buffer.
 *
 * Pattern mirrors BCC ringbuf examples and the hook style used in
 * external/KernMLOps (see python/kernmlops/data_collection/bpf_instrumentation/).
 */

#include <linux/sched.h>

struct mvp_event {
  u32 pid;
  u64 ts_ns;
  char comm[TASK_COMM_LEN];
};

BPF_RINGBUF_OUTPUT(events, 1 << 4);

TRACEPOINT_PROBE(sched, sched_process_exec) {
  struct mvp_event evt = {};

  evt.pid = (u32)(bpf_get_current_pid_tgid() >> 32);
  evt.ts_ns = bpf_ktime_get_ns();
  bpf_get_current_comm(&evt.comm, sizeof(evt.comm));

  events.ringbuf_output(&evt, sizeof(evt), 0);
  return 0;
}
