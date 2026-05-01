# Small Note on Reflex's eBPF Stuff

Made this note after I figured out that the bpf was exploding our performance on Unixbench and destroying our metrics. We can still get usable information, but we have to be strategic

## Main Idea
Keep data in the kernel for as long as possible, and as low granularity as possible, delivered via direct reads to simple kernel structures (arrays, hashmaps). This was a painful lesson from us streaming hella events to userspace via a ring buffer for window-level aggregation. Horrendous latency issues with syscalls, 3-4x syscall time overhead as measured by Unixbench syscall. 

## What We're Collecting
- Syscall counters, failed, p95 latency (sampled 10-25%)
- Runqueue latency (sampled 10-25%)
- Fork count
- Context switch count
- Block I/O Latency
- Direct Reclaim