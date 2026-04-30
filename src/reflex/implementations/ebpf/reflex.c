/*
 * reflex.c (loader2-based) — aggregates eBPF events in userspace and emits
 * one compact summary record per configured window instead of forwarding every event.
 *
 * Per-window summary (48 bytes packed):
 *     u64 window_end_ns
 *     u32 rq_p95_us
 *     u32 rq_latency_count
 *     u32 syscall_count
 *     u32 failure_count
 *     u32 blk_p95_us
 *     u32 blk_latency_count
 *     u32 ctx_switch_count
 *     u32 direct_reclaim_count
 *     u32 direct_reclaim_p95_us
 *     u32 fork_count
 */
#include <bpf/libbpf.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <inttypes.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include "reflex.skel.h"

#define CGROUP_FILE "/tmp/reflex_cgroups"
#define MAX_CGROUP_IDS 256
#define MAX_LAT_SAMPLES 32768
#define DEFAULT_WINDOW_NS 1000000000ULL /* 1 second */

/* Must match reflex.bpf.c EVENT_* constants. */
#define EVENT_FORK 2
#define EVENT_SCHED_SWITCH 4
#define EVENT_SYSCALL_EXIT 5
#define EVENT_RQ_LATENCY 6
#define EVENT_DIRECT_RECLAIM 7
#define EVENT_BLK_LATENCY 8

/* Mirrors struct payload in reflex.bpf.c (48 bytes). */
struct payload
{
    uint32_t event_type;
    uint32_t cpu;
    uint32_t pid;
    uint32_t tgid;
    uint64_t ts_ns;
    int32_t value_i32;
    uint32_t value_u32;
    char comm[16];
} __attribute__((packed));

struct summary
{
    uint64_t window_end_ns;
    uint32_t rq_p95_us;
    uint32_t rq_latency_count;
    uint32_t syscall_count;
    uint32_t failure_count;
    uint32_t blk_p95_us;
    uint32_t blk_latency_count;
    uint32_t ctx_switch_count;
    uint32_t direct_reclaim_count;
    uint32_t direct_reclaim_p95_us;
    uint32_t fork_count;
} __attribute__((packed));

_Static_assert(sizeof(struct summary) == 48, "summary ABI must match Python decoder");

static struct reflex_bpf *g_skel = NULL;
static uint64_t loaded_cgids[MAX_CGROUP_IDS];
static int n_loaded = 0;
static time_t last_mtime = 0;

/* Per-window aggregation state. */
static uint32_t rq_lat[MAX_LAT_SAMPLES];
static int rq_lat_n = 0;
static uint32_t blk_lat[MAX_LAT_SAMPLES];
static int blk_lat_n = 0;
static uint32_t reclaim_lat[MAX_LAT_SAMPLES];
static int reclaim_lat_n = 0;
static uint32_t syscall_count = 0;
static uint32_t failure_count = 0;
static uint32_t ctx_switch_count = 0;
static uint32_t direct_reclaim_count = 0;
static uint32_t fork_count = 0;
static uint64_t window_start_ns = 0;
static uint64_t window_ns = DEFAULT_WINDOW_NS;

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static int cmp_u32(const void *a, const void *b)
{
    uint32_t x = *(const uint32_t *)a;
    uint32_t y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

/* Sort buf and return the p95 sample (0 if empty). */
static uint32_t compute_p95(uint32_t *buf, int n)
{
    if (n == 0)
        return 0;
    qsort(buf, n, sizeof(buf[0]), cmp_u32);
    return buf[(int)(0.95 * (n - 1))];
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

static void flush_summary(void)
{
    struct summary s = {
        .window_end_ns = now_ns(),
        .rq_p95_us = compute_p95(rq_lat, rq_lat_n),
        .rq_latency_count = (uint32_t)rq_lat_n,
        .syscall_count = syscall_count,
        .failure_count = failure_count,
        .blk_p95_us = compute_p95(blk_lat, blk_lat_n),
        .blk_latency_count = (uint32_t)blk_lat_n,
        .ctx_switch_count = ctx_switch_count,
        .direct_reclaim_count = direct_reclaim_count,
        .direct_reclaim_p95_us = compute_p95(reclaim_lat, reclaim_lat_n),
        .fork_count = fork_count,
    };

    fwrite(&s, sizeof(s), 1, stdout);
    fflush(stdout);

    rq_lat_n = 0;
    blk_lat_n = 0;
    reclaim_lat_n = 0;
    syscall_count = 0;
    failure_count = 0;
    ctx_switch_count = 0;
    direct_reclaim_count = 0;
    fork_count = 0;
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

static int handle_event(void *ctx, void *data, size_t data_size)
{
    (void)ctx;
    if (data_size < sizeof(struct payload))
        return 0;

    const struct payload *p = (const struct payload *)data;
    switch (p->event_type)
    {
    case EVENT_SYSCALL_EXIT:
        syscall_count++;
        if (p->value_i32 < 0)
            failure_count++;
        break;
    case EVENT_RQ_LATENCY:
        if (rq_lat_n < MAX_LAT_SAMPLES)
            rq_lat[rq_lat_n++] = p->value_u32;
        break;
    case EVENT_BLK_LATENCY:
        if (blk_lat_n < MAX_LAT_SAMPLES)
            blk_lat[blk_lat_n++] = p->value_u32;
        break;
    case EVENT_SCHED_SWITCH:
        ctx_switch_count += p->value_u32 ? p->value_u32 : 1;
        break;
    case EVENT_DIRECT_RECLAIM:
        direct_reclaim_count++;
        if (reclaim_lat_n < MAX_LAT_SAMPLES)
            reclaim_lat[reclaim_lat_n++] = p->value_u32;
        break;
    case EVENT_FORK:
        fork_count++;
        break;
    default:
        break;
    }

    return 0;
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

    struct reflex_bpf *skel;
    struct ring_buffer *rb = NULL;
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

    err = reflex_bpf__attach(skel);
    if (err)
    {
        fprintf(stderr, "Error with attach %d\n", err);
        goto cleanup;
    }

    rb = ring_buffer__new(bpf_map__fd(skel->maps.events), handle_event, NULL, NULL);
    if (!rb)
    {
        fprintf(stderr, "Error with RB\n");
        goto cleanup;
    }

    window_start_ns = now_ns();
    while (1)
    {
        ring_buffer__poll(rb, 100);
        check_cgroup_file();
        if (now_ns() - window_start_ns >= window_ns)
            flush_summary();
    }

cleanup:
    fprintf(stderr, "Cleanup\n");
    ring_buffer__free(rb);
    reflex_bpf__destroy(skel);
    return 0;
}
