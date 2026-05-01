/*
 * reflex.c — snapshots kernel-side eBPF window aggregates and emits one
 * compact summary record per configured window.
 *
 * Per-window summary (52 bytes packed):
 *     u64 window_end_ns
 *     u32 rq_p95_us
 *     u32 rq_latency_count
 *     u32 syscall_count
 *     u32 failure_count
 *     u32 syscall_p95_us
 *     u32 blk_p95_us
 *     u32 blk_latency_count
 *     u32 ctx_switch_count
 *     u32 direct_reclaim_count
 *     u32 direct_reclaim_p95_us
 *     u32 fork_count
 */
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include "reflex.skel.h"

#define CGROUP_FILE "/tmp/reflex_cgroups"
#define MAX_CGROUP_IDS 256
#define DEFAULT_WINDOW_NS 1000000000ULL /* 1 second */
#define LAT_BUCKETS 32

struct aggregate
{
    uint64_t syscall_count;
    uint64_t syscall_failure_count;
    uint64_t syscall_latency_count;
    uint64_t rq_latency_count;
    uint64_t blk_latency_count;
    uint64_t direct_reclaim_count;
    uint64_t fork_count;
    uint64_t ctx_switch_count;
    uint64_t syscall_latency_hist[LAT_BUCKETS];
    uint64_t rq_latency_hist[LAT_BUCKETS];
    uint64_t blk_latency_hist[LAT_BUCKETS];
    uint64_t direct_reclaim_latency_hist[LAT_BUCKETS];
};

struct summary
{
    uint64_t window_end_ns;
    uint32_t rq_p95_us;
    uint32_t rq_latency_count;
    uint32_t syscall_count;
    uint32_t failure_count;
    uint32_t syscall_p95_us;
    uint32_t blk_p95_us;
    uint32_t blk_latency_count;
    uint32_t ctx_switch_count;
    uint32_t direct_reclaim_count;
    uint32_t direct_reclaim_p95_us;
    uint32_t fork_count;
} __attribute__((packed));

_Static_assert(sizeof(struct summary) == 52, "summary ABI must match Python decoder");

static struct reflex_bpf *g_skel = NULL;
static uint64_t loaded_cgids[MAX_CGROUP_IDS];
static int n_loaded = 0;
static time_t last_mtime = 0;
static uint64_t window_start_ns = 0;
static uint64_t window_ns = DEFAULT_WINDOW_NS;
static int n_cpus = 0;
static size_t aggregate_value_size = 0;

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static uint32_t clamp_u32(uint64_t value)
{
    return value > UINT32_MAX ? UINT32_MAX : (uint32_t)value;
}

static uint32_t bucket_upper_us(int bucket)
{
    if (bucket <= 0)
        return 1;
    if (bucket >= 31)
        return UINT32_MAX;
    return (uint32_t)1U << bucket;
}

static uint32_t hist_p95_us(const uint64_t hist[LAT_BUCKETS], uint64_t count)
{
    if (count == 0)
        return 0;

    uint64_t threshold = (count * 95 + 99) / 100;
    uint64_t seen = 0;
    for (int i = 0; i < LAT_BUCKETS; i++)
    {
        seen += hist[i];
        if (seen >= threshold)
            return bucket_upper_us(i);
    }
    return bucket_upper_us(LAT_BUCKETS - 1);
}

static void configure_window(void)
{
    const char *raw = getenv("REFLEX_WINDOW_SEC");
    if (!raw || !*raw)
        return;
    double sec = strtod(raw, NULL);
    if (sec <= 0.0)
        return;
    window_ns = (uint64_t)(sec * 1000000000.0);
    if (window_ns == 0)
        window_ns = DEFAULT_WINDOW_NS;
}

static void merge_aggregate(struct aggregate *dst, const struct aggregate *src)
{
    dst->syscall_count += src->syscall_count;
    dst->syscall_failure_count += src->syscall_failure_count;
    dst->syscall_latency_count += src->syscall_latency_count;
    dst->rq_latency_count += src->rq_latency_count;
    dst->blk_latency_count += src->blk_latency_count;
    dst->direct_reclaim_count += src->direct_reclaim_count;
    dst->fork_count += src->fork_count;
    dst->ctx_switch_count += src->ctx_switch_count;

    for (int i = 0; i < LAT_BUCKETS; i++)
    {
        dst->syscall_latency_hist[i] += src->syscall_latency_hist[i];
        dst->rq_latency_hist[i] += src->rq_latency_hist[i];
        dst->blk_latency_hist[i] += src->blk_latency_hist[i];
        dst->direct_reclaim_latency_hist[i] += src->direct_reclaim_latency_hist[i];
    }
}

static int read_window_aggregate(struct aggregate *out)
{
    __u32 key = 0;
    char *values = calloc((size_t)n_cpus, aggregate_value_size);
    if (!values)
        return -ENOMEM;

    int err = bpf_map_lookup_elem(bpf_map__fd(g_skel->maps.window_agg), &key, values);
    if (err)
    {
        int saved = errno;
        free(values);
        return -saved;
    }

    memset(out, 0, sizeof(*out));
    for (int cpu = 0; cpu < n_cpus; cpu++)
    {
        const struct aggregate *cpu_value =
            (const struct aggregate *)(values + (size_t)cpu * aggregate_value_size);
        merge_aggregate(out, cpu_value);
    }

    free(values);
    return 0;
}

static int clear_window_aggregate(void)
{
    __u32 key = 0;
    char *zeros = calloc((size_t)n_cpus, aggregate_value_size);
    if (!zeros)
        return -ENOMEM;

    int err = bpf_map_update_elem(bpf_map__fd(g_skel->maps.window_agg), &key, zeros, BPF_ANY);
    if (err)
    {
        int saved = errno;
        free(zeros);
        return -saved;
    }

    free(zeros);
    return 0;
}

static void flush_summary(void)
{
    struct aggregate agg;
    int err = read_window_aggregate(&agg);
    if (err)
    {
        fprintf(stderr, "Failed to read aggregate %d\n", err);
        return;
    }

    struct summary s = {
        .window_end_ns = now_ns(),
        .rq_p95_us = hist_p95_us(agg.rq_latency_hist, agg.rq_latency_count),
        .rq_latency_count = clamp_u32(agg.rq_latency_count),
        .syscall_count = clamp_u32(agg.syscall_count),
        .failure_count = clamp_u32(agg.syscall_failure_count),
        .syscall_p95_us = hist_p95_us(agg.syscall_latency_hist, agg.syscall_latency_count),
        .blk_p95_us = hist_p95_us(agg.blk_latency_hist, agg.blk_latency_count),
        .blk_latency_count = clamp_u32(agg.blk_latency_count),
        .ctx_switch_count = clamp_u32(agg.ctx_switch_count),
        .direct_reclaim_count = clamp_u32(agg.direct_reclaim_count),
        .direct_reclaim_p95_us = hist_p95_us(
            agg.direct_reclaim_latency_hist,
            agg.direct_reclaim_count
        ),
        .fork_count = clamp_u32(agg.fork_count),
    };

    fwrite(&s, sizeof(s), 1, stdout);
    fflush(stdout);

    err = clear_window_aggregate();
    if (err)
        fprintf(stderr, "Failed to clear aggregate %d\n", err);
    window_start_ns = now_ns();
}

static void add_cgid(uint64_t cgid)
{
    for (int i = 0; i < n_loaded; i++)
        if (loaded_cgids[i] == cgid)
            return;
    if (n_loaded >= MAX_CGROUP_IDS)
        return;
    uint8_t val = 1;
    bpf_map__update_elem(g_skel->maps.cgroup_whitelist,
                         &cgid, sizeof(cgid), &val, sizeof(val), BPF_ANY);
    loaded_cgids[n_loaded++] = cgid;
}

static void check_cgroup_file(void)
{
    struct stat st;
    if (stat(CGROUP_FILE, &st) != 0)
        return;
    if (st.st_mtime <= last_mtime)
        return;
    last_mtime = st.st_mtime;

    FILE *f = fopen(CGROUP_FILE, "r");
    if (!f)
        return;

    uint64_t cgid;
    while (fscanf(f, "%" SCNu64, &cgid) == 1)
        add_cgid(cgid);
    fclose(f);
}

int main(int argc, char **argv)
{
    uint32_t py_pid = 0;
    configure_window();
    if (argc > 1)
    {
        py_pid = strtoul(argv[1], NULL, 10);
        fprintf(stderr, "Py_pid %u\n", py_pid);
    }

    n_cpus = libbpf_num_possible_cpus();
    if (n_cpus <= 0)
    {
        fprintf(stderr, "Failed to determine possible CPU count\n");
        return 1;
    }
    aggregate_value_size = sizeof(struct aggregate);

    struct reflex_bpf *skel;
    int err;

    struct rlimit rlim = {
        .rlim_cur = RLIM_INFINITY,
        .rlim_max = RLIM_INFINITY,
    };
    setrlimit(RLIMIT_MEMLOCK, &rlim);

    skel = reflex_bpf__open();
    if (!skel)
    {
        fprintf(stderr, "Error with open\n");
        return 1;
    }
    g_skel = skel;

    skel->rodata->loader_pid = getpid();
    skel->rodata->python_pid = py_pid;
    skel->rodata->use_cgroup_filter = (argc > 2 || access(CGROUP_FILE, F_OK) == 0) ? 1 : 0;

    err = reflex_bpf__load(skel);
    if (err)
    {
        fprintf(stderr, "Failed to load skel %d\n", err);
        goto cleanup;
    }

    for (int i = 2; i < argc; i++)
        add_cgid(strtoull(argv[i], NULL, 10));
    check_cgroup_file();

    err = clear_window_aggregate();
    if (err)
    {
        fprintf(stderr, "Failed to initialize aggregate %d\n", err);
        goto cleanup;
    }

    err = reflex_bpf__attach(skel);
    if (err)
    {
        fprintf(stderr, "Error with attach %d\n", err);
        goto cleanup;
    }

    window_start_ns = now_ns();
    while (1)
    {
        usleep(100000);
        check_cgroup_file();
        if (now_ns() - window_start_ns >= window_ns)
            flush_summary();
    }

cleanup:
    fprintf(stderr, "Cleanup\n");
    reflex_bpf__destroy(skel);
    return 0;
}
