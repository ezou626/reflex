# Benchmark and host-metrics backlog

## Implemented (daemon `host_features` + workload_only sampler)

These are emitted by [`daemon/host_metrics.py`](daemon/host_metrics.py), wired from [`daemon/main.py`](daemon/main.py) `ProcSampleState` and [`benchmarks/sample_host_metrics.py`](benchmarks/sample_host_metrics.py).

- [x] **`host_loadavg_5m` / `host_loadavg_15m`** — multi-horizon load vs 1m only (`/proc/loadavg`).
- [x] **`host_loadavg_nr_running` / `host_loadavg_nr_threads`** — runnable / total threads snapshot from loadavg’s `nr/TOTAL` field when present.
- [x] **`host_procs_running` / `host_procs_blocked`** — from `procs_running` / `procs_blocked` lines in `/proc/stat`.
- [x] **`host_process_count`** — count of numeric `/proc/<pid>` directories (task population proxy).
- [x] **Meminfo ratios (of `MemTotal`)** — `host_mem_sreclaimable_ratio`, `host_mem_active_file_ratio`, `host_mem_inactive_file_ratio` from `SReclaimable`, `Active(file)`, `Inactive(file)`.
- [x] **`/proc/vmstat` rates (per second)** — `host_vmstat_pgfault_per_sec`, `pgmajfault`, `pswpin`, `pswpout`, `pgscan_direct`, `pgscan_kswapd`.
- [x] **Aggregate disk activity** — `host_disk_read_sectors_per_sec`, `host_disk_write_sectors_per_sec` summed over `/proc/diskstats` rows (excludes `loop*`).

Existing fields remain: `host_cpu_busy_ratio`, `host_ctxt_rate_per_sec`, `host_mem_available_ratio`, swap/dirty, `host_loadavg_1m`, PSI, etc.

## Not yet implemented (candidates for future work)

- [ ] **`/proc/schedstat`** — scheduler run-queue / wait-time counters as rates (stronger contention signal; parsing is kernel-specific).
- [ ] **Per-device or root-disk `diskstats`** — attribute I/O to workload device instead of system-wide sum.
- [ ] **`/proc/interrupts` / `/proc/softirqs`** — IRQ and softirq rates (network / block layer visibility).
- [ ] **CPU frequency** — `scaling_cur_freq` (or `cpuinfo_cur_freq`) when cpufreq exists (explains governor/thermal shifts, especially on VMs).
- [ ] **More `vmstat`** — e.g. `compact_stall`, `oom_kill`, THP counters, as needed for memory profiles.
- [ ] ** cgroup v2** — scoped `memory.events`, `cpu.stat`, `io.stat` when a workload cgroup is known.
- [ ] **Top-K processes** — bounded RSS / CPU by `pid` (higher cardinality; keep K small).
- [ ] **Daemon self RSS** — `/proc/self/status` `VmRSS` to separate Python/BCC footprint from kernel-wide meminfo.
- [ ] **Bytes/s from disk** — multiply sector rates by 512 (or use BLKSSZGET) for human-friendly I/O throughput.

## Benchmark / scorecard follow-ups

- [ ] Optional **IQR** or confidence bands on trial aggregates (today: median + min + max in [`benchmarks/scorecard_trials_aggregate.py`](benchmarks/scorecard_trials_aggregate.py)).
- [ ] **Scorecard key families** — tag metrics as gauge vs counter vs rate for smarter default inclusion (beyond `_total` PSI exclusion).
