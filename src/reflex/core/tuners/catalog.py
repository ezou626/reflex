from __future__ import annotations

from reflex.core.tuners.schema import TunerCatalogEntry
from reflex.core.tuners.sysctl import GenericSysctlTuner


def _sysctl(
    id: str,
    sysctl: str,
    category: str,
    description: str,
    *,
    min_value: int | float | None = None,
    max_value: int | float | None = None,
    step: int | float = 1,
    enabled: bool = True,
) -> GenericSysctlTuner:
    return GenericSysctlTuner(TunerCatalogEntry(
        id=id,
        sysctl=sysctl,
        category=category,
        description=description,
        kind="int",
        enabled=enabled,
        min_value=min_value,
        max_value=max_value,
        step=step,
    ))


# vm

vm_swappiness = _sysctl(
    "sysctl_vm_swappiness",
    "vm.swappiness",
    "vm",
    "Tendency to reclaim anonymous pages vs page cache",
    min_value=0, max_value=100, step=5,
)

vm_watermark_scale_factor = _sysctl(
    "sysctl_vm_watermark_scale_factor",
    "vm.watermark_scale_factor",
    "vm",
    "How aggressively kswapd wakes to reclaim memory before hitting watermarks",
    min_value=10, max_value=500, step=10,
)

vm_min_free_kbytes = _sysctl(
    "sysctl_vm_min_free_kbytes",
    "vm.min_free_kbytes",
    "vm",
    "Minimum free memory the kernel keeps in reserve to avoid OOM",
    min_value=16384, max_value=1048576, step=8192,
)

vm_vfs_cache_pressure = _sysctl(
    "sysctl_vm_vfs_cache_pressure",
    "vm.vfs_cache_pressure",
    "vm",
    "Tendency to reclaim inode/dentry cache vs swap",
    min_value=50, max_value=1000, step=10,
)

vm_dirty_expire_centisecs = _sysctl(
    "sysctl_vm_dirty_expire_centisecs",
    "vm.dirty_expire_centisecs",
    "vm",
    "Age (centisecs) at which dirty pages are eligible for writeback; lower forces more frequent flushes",
    min_value=100, max_value=6000, step=100,
)

vm_dirty_writeback_centisecs = _sysctl(
    "sysctl_vm_dirty_writeback_centisecs",
    "vm.dirty_writeback_centisecs",
    "vm",
    "Interval (centisecs) between pdflush/writeback wakeups; 0 disables periodic writeback",
    min_value=100, max_value=1500, step=100,
)

vm_dirty_bytes = _sysctl(
    "sysctl_vm_dirty_bytes",
    "vm.dirty_bytes",
    "vm",
    "Absolute dirty memory threshold for synchronous writeback; overrides dirty_ratio when non-zero",
    min_value=8192, max_value=536870912, step=8388608,
)

vm_page_cluster = _sysctl(
    "sysctl_vm_page_cluster",
    "vm.page-cluster",
    "vm",
    "Log2 of pages read together from swap; 0=one page, 3=eight pages",
    min_value=0, max_value=5, step=1,
    enabled=False,
)

vm_compaction_proactiveness = _sysctl(
    "sysctl_vm_compaction_proactiveness",
    "vm.compaction_proactiveness",
    "vm",
    "Aggressiveness of proactive memory compaction (0=off, 100=max); reduces allocation latency spikes",
    min_value=0, max_value=100, step=10,
    enabled=False,
)

vm_extfrag_threshold = _sysctl(
    "sysctl_vm_extfrag_threshold",
    "vm.extfrag_threshold",
    "vm",
    "Fragmentation score above which allocator prefers compaction over reclaim (0=always compact, 1000=always reclaim)",
    min_value=0, max_value=1000, step=50,
    enabled=False,
)

vm_max_map_count = _sysctl(
    "sysctl_vm_max_map_count",
    "vm.max_map_count",
    "vm",
    "Max VMAs per process; raise for JVM, Elasticsearch, or large mmap workloads",
    min_value=65530, max_value=524288, step=65536,
    enabled=False,
)

vm_overcommit_memory = _sysctl(
    "sysctl_vm_overcommit_memory",
    "vm.overcommit_memory",
    "vm",
    "Overcommit policy; 0=heuristic, 1=always allow, 2=strict limit via overcommit_kbytes",
    min_value=0, max_value=2, step=1,
    enabled=False,
)

vm_overcommit_kbytes = _sysctl(
    "sysctl_vm_overcommit_kbytes",
    "vm.overcommit_kbytes",
    "vm",
    "Absolute kbytes added to RAM for overcommit limit; only effective when vm.overcommit_memory=2",
    min_value=0, max_value=8388608, step=262144,
    enabled=False,
)

# kernel / cpu

kernel_sched_cfs_bandwidth_slice_us = _sysctl(
    "sysctl_kernel_sched_cfs_bandwidth_slice_us",
    "kernel.sched_cfs_bandwidth_slice_us",
    "cpu",
    "Cgroup CPU quota slice transfer size; larger reduces accounting overhead while smaller allows finer-grained quota consumption",
    min_value=1000, max_value=100000, step=1000,
)

kernel_sched_min_granularity_ns = _sysctl(
    "sysctl_kernel_sched_min_granularity_ns",
    "kernel.sched_min_granularity_ns",
    "cpu",
    "Minimum CFS runtime slice per runnable task; higher reduces context-switch overhead for CPU-bound batch workloads, lower improves latency/fairness",
    min_value=750000, max_value=20000000, step=750000,
)

kernel_sched_latency_ns = _sysctl(
    "sysctl_kernel_sched_latency_ns",
    "kernel.sched_latency_ns",
    "cpu",
    "Target CFS period over which runnable tasks share CPU; higher favors throughput, lower favors responsiveness",
    min_value=6000000, max_value=48000000, step=3000000,
)

kernel_sched_wakeup_granularity_ns = _sysctl(
    "sysctl_kernel_sched_wakeup_granularity_ns",
    "kernel.sched_wakeup_granularity_ns",
    "cpu",
    "How much advantage a waking task needs to preempt current task; higher reduces preemption churn, lower improves interactive latency",
    min_value=1000000, max_value=24000000, step=1000000,
)

kernel_sched_autogroup_enabled = _sysctl(
    "sysctl_kernel_sched_autogroup_enabled",
    "kernel.sched_autogroup_enabled",
    "cpu",
    "Group tasks by session for fairer scheduling; off favours batch throughput",
    min_value=0, max_value=1, step=1,
    enabled=True,
)

# net

net_core_somaxconn = _sysctl(
    "sysctl_net_core_somaxconn",
    "net.core.somaxconn",
    "net",
    "Maximum listen backlog per socket",
    min_value=128, max_value=4096, step=128,
)

net_core_netdev_max_backlog = _sysctl(
    "sysctl_net_core_netdev_max_backlog",
    "net.core.netdev_max_backlog",
    "net",
    "Max packets queued in the NIC receive ring before being processed by the kernel",
    min_value=1000, max_value=10000, step=1000,
)

net_core_netdev_budget = _sysctl(
    "sysctl_net_core_netdev_budget",
    "net.core.netdev_budget",
    "net",
    "Max packets processed per NAPI softirq poll cycle; higher reduces latency under burst at cost of CPU",
    min_value=100, max_value=600, step=50,
    enabled=False,
)

net_core_rmem_max = _sysctl(
    "sysctl_net_core_rmem_max",
    "net.core.rmem_max",
    "net",
    "Maximum socket receive buffer size in bytes",
    min_value=212992, max_value=16777216, step=1048576,
)

net_core_wmem_max = _sysctl(
    "sysctl_net_core_wmem_max",
    "net.core.wmem_max",
    "net",
    "Maximum socket send buffer size in bytes",
    min_value=212992, max_value=16777216, step=1048576,
)

net_ipv4_tcp_fin_timeout = _sysctl(
    "sysctl_net_ipv4_tcp_fin_timeout",
    "net.ipv4.tcp_fin_timeout",
    "net",
    "Seconds a FIN-WAIT-2 socket is held before teardown; lower reclaims ports faster under high connection churn",
    min_value=10, max_value=120, step=5,
)

net_ipv4_tcp_max_syn_backlog = _sysctl(
    "sysctl_net_ipv4_tcp_max_syn_backlog",
    "net.ipv4.tcp_max_syn_backlog",
    "net",
    "Max half-open connections queued per socket before SYN packets are dropped",
    min_value=512, max_value=4096, step=256,
    enabled=False,
)

net_ipv4_tcp_notsent_lowat = _sysctl(
    "sysctl_net_ipv4_tcp_notsent_lowat",
    "net.ipv4.tcp_notsent_lowat",
    "net",
    "Max unsent bytes allowed in TCP send queue per socket; lower reduces latency under send backpressure",
    min_value=16384, max_value=2097152, step=65536,
)

net_ipv4_tcp_tw_reuse = _sysctl(
    "sysctl_net_ipv4_tcp_tw_reuse",
    "net.ipv4.tcp_tw_reuse",
    "net",
    "Reuse TIME_WAIT sockets for new outgoing connections; 0=off, 1=enabled globally; reduces port exhaustion under high connection churn",
    min_value=0, max_value=1, step=1,
)

net_ipv4_tcp_slow_start_after_idle = _sysctl(
    "sysctl_net_ipv4_tcp_slow_start_after_idle",
    "net.ipv4.tcp_slow_start_after_idle",
    "net",
    "Reset TCP congestion window after idle when 1; set to 0 for persistent high-throughput connections (HTTP/2, gRPC, DB pools)",
    min_value=0, max_value=1, step=1,
)

net_ipv4_tcp_retries2 = _sysctl(
    "sysctl_net_ipv4_tcp_retries2",
    "net.ipv4.tcp_retries2",
    "net",
    "TCP retransmit attempts before dropping a connection; lower for faster dead-peer detection, default 15 (~13-30 min holdtime)",
    min_value=8, max_value=15, step=1,
    enabled=False,
)


ALL_TUNERS: tuple[GenericSysctlTuner, ...] = (
    vm_swappiness,
    vm_watermark_scale_factor,
    vm_min_free_kbytes,
    vm_vfs_cache_pressure,
    vm_dirty_expire_centisecs,
    vm_dirty_writeback_centisecs,
    vm_dirty_bytes,
    vm_page_cluster,
    vm_compaction_proactiveness,
    vm_extfrag_threshold,
    vm_max_map_count,
    vm_overcommit_memory,
    vm_overcommit_kbytes,
    kernel_sched_cfs_bandwidth_slice_us,
    kernel_sched_min_granularity_ns,
    kernel_sched_latency_ns,
    kernel_sched_wakeup_granularity_ns,
    kernel_sched_autogroup_enabled,
    net_core_somaxconn,
    net_core_netdev_max_backlog,
    net_core_netdev_budget,
    net_core_rmem_max,
    net_core_wmem_max,
    net_ipv4_tcp_fin_timeout,
    net_ipv4_tcp_max_syn_backlog,
    net_ipv4_tcp_notsent_lowat,
    net_ipv4_tcp_tw_reuse,
    net_ipv4_tcp_slow_start_after_idle,
    net_ipv4_tcp_retries2,
)
