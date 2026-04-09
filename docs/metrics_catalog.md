# Reflex Metrics Catalog

This catalog documents candidate feature sources for decisioning and future ML.

## Always-on / low overhead (sample every 1s-5s)

| Feature family | Source | Example features | Notes |
|---|---|---|---|
| CPU totals | `/proc/stat` | busy ratio, ctxt/sec, intr/sec | Prefer file parsing over spawning `vmstat`. |
| Load | `/proc/loadavg` | loadavg_1m/5m/15m | EWMA, not instantaneous queue depth. |
| Memory | `/proc/meminfo` | MemAvailable ratio, Dirty KB, swap free ratio | Stable and cheap. |
| VM counters | `/proc/vmstat` | pgfault/sec, pgmajfault/sec, pswpin/sec, pswpout/sec | Convert monotonic counters to rates. |
| Disk | `/proc/diskstats` | r/s, w/s, rkB/s, wkB/s | Better than shelling out to `iostat` continuously. |
| Network | `/proc/net/dev` | rx/tx bytes/sec, drops/sec | Interface-level only. |
| TCP/IP | `/proc/net/snmp`, `/proc/net/netstat` | retransmits, in errs, out resets | Good for congestion and instability signals. |
| Socket pressure | `/proc/net/sockstat` | TCP inuse, orphan, mem pages | Good lightweight distress indicator. |
| PSI stalls | `/proc/pressure/{cpu,memory,io}` | some/full avg10/60/300 | Strong early warning for contention. |
| cgroup v2 | `/sys/fs/cgroup/*` | cpu throttling, memory.events, io.stat | Useful when scoped to workload cgroup. |
| top-K processes | `/proc/<pid>/{stat,status,io}` | cpu time delta, rss, read/write bytes | Keep bounded to top-K to avoid cardinality blow-up. |

## On-demand / diagnostic (higher overhead)

| Source | Typical use |
|---|---|
| `perf stat` | cycles/instructions/cache behavior while investigating spikes |
| `pidstat` | process-level burst diagnostics and attribution |
| `iostat -x` | storage queue and utilization deep dive |
| `sar` | subsystem retrospection with richer reporting |
| `/proc/<pid>/smaps*` | detailed memory attribution (PSS/private/shared) |
| `journalctl` | correlate kernel/service warnings with telemetry anomalies |

## Phase-1 eBPF probes currently implemented

- `sched:sched_process_fork`
- `sched:sched_process_exec`
- `sched:sched_process_exit`
- `sched:sched_switch`
- `sched:sched_wakeup` (for wakeup->oncpu latency pairing)
- `raw_syscalls:sys_exit`

## ML feature engineering defaults

- Use fixed windows (1s sample, 10s decision window).
- Convert counters to rates before modeling.
- Keep missing-source flags (`source_unavailable`) for portability.
- Add run metadata fields (`kernel`, `is_wsl`, `cores`, `ram_gb`, `cgroup_mode`).
- Persist labels separately from features to avoid training leakage:
  - `bottleneck_class`: `CPU_BOUND`, `MEM_BOUND`, `IO_BOUND`, `SCHED_CONTENTION`, `NET_BOUND`, `MIXED`, `IDLE`
  - `action_outcome`: `improved`, `neutral`, `regressed`
