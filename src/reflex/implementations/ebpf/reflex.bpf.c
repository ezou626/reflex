#include "vmlinux.h"
#include <bpf/bpf_helpers.h>

volatile const __u32 python_pid = 0;
volatile const __u32 loader_pid = 0;
volatile const __u8  use_cgroup_filter = 0;
volatile const __u32 syscall_sample_rate = 4;
volatile const __u32 sched_switch_sample_rate = 4;
volatile const __u32 rq_sample_rate = 4;

#define LAT_BUCKETS 32

/*
 * Kernel-side window aggregate.
 *
 * Scope notes:
 * - syscall counters/latency honor the cgroup whitelist when enabled.
 * - scheduler, block, and reclaim signals remain system-wide, matching the
 *   previous ring-buffer behavior, except for existing self-PID exclusions.
 * - latency histograms are log2 microsecond buckets; userspace reports p95 as
 *   the selected bucket's upper bound.
 */
struct aggregate {
    __u64 syscall_count;
    __u64 syscall_failure_count;
    __u64 syscall_latency_count;
    __u64 rq_latency_count;
    __u64 blk_latency_count;
    __u64 direct_reclaim_count;
    __u64 fork_count;
    __u64 ctx_switch_count;
    __u64 syscall_latency_hist[LAT_BUCKETS];
    __u64 rq_latency_hist[LAT_BUCKETS];
    __u64 blk_latency_hist[LAT_BUCKETS];
    __u64 direct_reclaim_latency_hist[LAT_BUCKETS];
};

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct aggregate);
} window_agg SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} syscall_stride SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} sched_switch_stride SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} rq_stride SEC(".maps");

/* temporary storage hashmap for syscall enter timestamps, keyed by thread id */
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 10240);
  __type(key, u32);
  __type(value, u64);
} enter_parking SEC(".maps");

/* wakeup timestamp storage for rq latency, keyed by pid */
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 16384);
  __type(key, u32);
  __type(value, u64);
} wakeup_ts SEC(".maps");

/* direct reclaim start timestamps, keyed by pid */
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 4096);
  __type(key, u32);
  __type(value, u64);
} reclaim_ts SEC(".maps");

/* block I/O issue timestamps, keyed by (dev<<32)|sector */
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 4096);
  __type(key, u64);
  __type(value, u64);
} blk_issue_ts SEC(".maps");

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 256);
  __type(key, u64);
  __type(value, u8);
} cgroup_whitelist SEC(".maps");

static __always_inline struct aggregate *current_agg(void)
{
    __u32 key = 0;
    return bpf_map_lookup_elem(&window_agg, &key);
}

static __always_inline __u32 latency_bucket(__u64 lat_us)
{
    if (lat_us <= 1)
        return 0;

    __u64 value = lat_us - 1;
    __u32 bucket = 1;
    if (value >= (1ULL << 16)) {
        value >>= 16;
        bucket += 16;
    }
    if (value >= (1ULL << 8)) {
        value >>= 8;
        bucket += 8;
    }
    if (value >= (1ULL << 4)) {
        value >>= 4;
        bucket += 4;
    }
    if (value >= (1ULL << 2)) {
        value >>= 2;
        bucket += 2;
    }
    if (value >= (1ULL << 1))
        bucket += 1;

    if (bucket >= LAT_BUCKETS)
        return LAT_BUCKETS - 1;
    return bucket;
}

static __always_inline void add_latency(__u64 *hist, __u64 lat_us)
{
    __u32 bucket = latency_bucket(lat_us);
    hist[bucket]++;
}

static __always_inline int skip_self(void)
{
    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 pid = ptg >> 32;
    return pid == python_pid || pid == loader_pid;
}

static __always_inline __u32 normalized_rate(__u32 rate)
{
    return rate == 0 ? 1 : rate;
}

static __always_inline int stride_sample(void *stride_map, __u32 rate)
{
    __u32 key = 0;
    __u64 *counter = bpf_map_lookup_elem(stride_map, &key);
    if (!counter)
        return true;

    __u32 stride = normalized_rate(rate);
    __u64 next = *counter + 1;
    if (next >= stride) {
        *counter = 0;
        return 1;
    }
    *counter = next;
    return 0;
}

SEC("tp/sched/sched_process_exec")
int handle_exec(struct trace_event_raw_sched_process_exec *ctx)
{
    return 0;
}

SEC("tp/sched/sched_process_fork")
int handle_fork(struct trace_event_raw_sched_process_fork *ctx)
{
    if (skip_self())
        return 0;

    struct aggregate *agg = current_agg();
    if (agg)
        agg->fork_count++;
    return 0;
}

SEC("tp/sched/sched_process_exit")
int handle_exit(struct trace_event_raw_sched_process_template *ctx)
{
    return 0;
}

SEC("tp/sched/sched_wakeup")
int handle_wakeup(struct trace_event_raw_sched_wakeup_template *ctx)
{
    if (!stride_sample(&rq_stride, rq_sample_rate))
        return 0;

    __u32 pid = ctx->pid;
    __u64 ts  = bpf_ktime_get_ns();
    bpf_map_update_elem(&wakeup_ts, &pid, &ts, BPF_ANY);
    return 0;
}

SEC("tp/sched/sched_switch")
int handle_sched_switch(struct trace_event_raw_sched_switch *ctx)
{
    struct aggregate *agg = current_agg();
    if (agg && stride_sample(&sched_switch_stride, sched_switch_sample_rate)) {
        __u32 switch_rate = normalized_rate(sched_switch_sample_rate);
        agg->ctx_switch_count += switch_rate;
    }

    __u32 next_pid = ctx->next_pid;
    __u64 *start = bpf_map_lookup_elem(&wakeup_ts, &next_pid);
    if (start) {
        __u64 now = bpf_ktime_get_ns();
        __u64 lat_us = (now - *start) / 1000;
        __u32 rq_rate = normalized_rate(rq_sample_rate);
        if (lat_us >= 100 && agg) {
            agg->rq_latency_count += rq_rate;
            add_latency(agg->rq_latency_hist, lat_us);
        }
        bpf_map_delete_elem(&wakeup_ts, &next_pid);
    }
    return 0;
}

SEC("tp/raw_syscalls/sys_enter")
int detect_syscall_enter(struct trace_event_raw_sys_enter *ctx)
{
    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 tid = (__u32)ptg;
    __u32 pid = ptg >> 32;

    if (pid == python_pid || pid == loader_pid)
        return 0;

    if (use_cgroup_filter) {
        __u64 cgid = bpf_get_current_cgroup_id();
        u8 *allowed = bpf_map_lookup_elem(&cgroup_whitelist, &cgid);
        if (!allowed)
            return 0;
    }

    if (!stride_sample(&syscall_stride, syscall_sample_rate))
        return 0;

    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&enter_parking, &tid, &ts, BPF_ANY);
    return 0;
}

SEC("tp/raw_syscalls/sys_exit")
int detect_syscall_exit(struct trace_event_raw_sys_exit *ctx)
{
    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 tid = (__u32)ptg;

    __u64 *start_ts = bpf_map_lookup_elem(&enter_parking, &tid);
    if (!start_ts)
        return 0;

    struct aggregate *agg = current_agg();
    if (agg) {
        __u64 now = bpf_ktime_get_ns();
        __u64 lat_us = (now - *start_ts) / 1000;
        __u32 rate = normalized_rate(syscall_sample_rate);
        agg->syscall_count += rate;
        agg->syscall_latency_count += rate;
        if (ctx->ret < 0)
            agg->syscall_failure_count += rate;
        add_latency(agg->syscall_latency_hist, lat_us);
    }

    bpf_map_delete_elem(&enter_parking, &tid);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_direct_reclaim_begin")
int handle_reclaim_begin(struct trace_event_raw_mm_vmscan_direct_reclaim_begin_template *ctx)
{
    if (skip_self())
        return 0;

    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 pid = ptg >> 32;
    __u64 ts = bpf_ktime_get_ns();
    bpf_map_update_elem(&reclaim_ts, &pid, &ts, BPF_ANY);
    return 0;
}

SEC("tp/vmscan/mm_vmscan_direct_reclaim_end")
int handle_reclaim_end(struct trace_event_raw_mm_vmscan_direct_reclaim_end_template *ctx)
{
    __u64 ptg = bpf_get_current_pid_tgid();
    __u32 pid = ptg >> 32;
    __u64 *start = bpf_map_lookup_elem(&reclaim_ts, &pid);
    if (!start)
        return 0;

    __u64 now = bpf_ktime_get_ns();
    __u64 lat_us = (now - *start) / 1000;
    bpf_map_delete_elem(&reclaim_ts, &pid);

    struct aggregate *agg = current_agg();
    if (agg) {
        agg->direct_reclaim_count++;
        add_latency(agg->direct_reclaim_latency_hist, lat_us);
    }
    return 0;
}

SEC("tp/block/block_rq_issue")
int handle_blk_issue(struct trace_event_raw_block_rq *ctx)
{
    __u64 key = ((__u64)ctx->dev << 32) | ((__u64)ctx->sector & 0xFFFFFFFF);
    __u64 ts  = bpf_ktime_get_ns();
    bpf_map_update_elem(&blk_issue_ts, &key, &ts, BPF_ANY);
    return 0;
}

SEC("tp/block/block_rq_complete")
int handle_blk_complete(struct trace_event_raw_block_rq_completion *ctx)
{
    __u64 key = ((__u64)ctx->dev << 32) | ((__u64)ctx->sector & 0xFFFFFFFF);
    __u64 *start = bpf_map_lookup_elem(&blk_issue_ts, &key);
    if (!start)
        return 0;

    __u64 now = bpf_ktime_get_ns();
    __u64 lat_us = (now - *start) / 1000;
    bpf_map_delete_elem(&blk_issue_ts, &key);

    struct aggregate *agg = current_agg();
    if (agg) {
        agg->blk_latency_count++;
        add_latency(agg->blk_latency_hist, lat_us);
    }
    return 0;
}

char _license[] SEC("license") = "GPL";
