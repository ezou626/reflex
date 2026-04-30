#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <time.h>

/*
 * Memory pressure tester for Reflex VM tuner validation.
 *
 * Usage: tester_mem [target_pct] [burst_pct]
 *   target_pct  % of total RAM to hold as working set (default: 80)
 *   burst_pct   extra % to allocate/release each burst cycle (default: 5)
 *
 * Phases:
 *   1. Ramp   - allocate working set in CHUNK_SIZE steps, touching every page
 *   2. Sustain - continuously dirty random pages to keep memory hot/dirty
 *   3. Burst  - periodically allocate+touch extra memory then release,
 *               creating reclaim pressure waves that exercise swappiness
 */

#define CHUNK_SIZE       (16 * 1024 * 1024)   /* 16 MB per allocation step */
#define PAGE_SIZE        4096
#define BURST_INTERVAL_S 15
#define MAX_CHUNKS       4096

static long read_meminfo_kb(const char *key) {
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) return -1;
    char line[128];
    long val = -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, key, strlen(key)) == 0) {
            sscanf(line + strlen(key), ": %ld", &val);
            break;
        }
    }
    fclose(f);
    return val;
}

static void touch_region(char *p, size_t len) {
    for (size_t i = 0; i < len; i += PAGE_SIZE)
        p[i] = (char)(i & 0xFF);
}

static void dirty_random_pages(char **chunks, int nchunks, int npages) {
    if (nchunks == 0) return;
    for (int i = 0; i < npages; i++) {
        int ci = rand() % nchunks;
        int pages_in_chunk = CHUNK_SIZE / PAGE_SIZE;
        int pi = rand() % pages_in_chunk;
        chunks[ci][pi * PAGE_SIZE] ^= 0xAB;
    }
}

int main(int argc, char *argv[]) {
    int target_pct = argc > 1 ? atoi(argv[1]) : 80;
    int burst_pct  = argc > 2 ? atoi(argv[2]) : 5;

    if (target_pct < 10 || target_pct > 95) {
        fprintf(stderr, "target_pct must be 10-95\n");
        return 1;
    }

    long mem_total_kb = read_meminfo_kb("MemTotal");
    if (mem_total_kb < 0) { perror("read_meminfo_kb"); return 1; }

    long target_kb  = mem_total_kb * target_pct / 100;
    long burst_kb   = mem_total_kb * burst_pct  / 100;
    int  target_chunks = (int)(target_kb * 1024 / CHUNK_SIZE);
    int  burst_chunks  = (int)(burst_kb  * 1024 / CHUNK_SIZE);

    if (target_chunks > MAX_CHUNKS) target_chunks = MAX_CHUNKS;
    char **chunks = calloc(MAX_CHUNKS, sizeof(char *));
    if (!chunks) { perror("calloc"); return 1; }

    /* Phase 1: ramp up working set */
    int nchunks = 0;
    while (nchunks < target_chunks) {
        char *p = mmap(NULL, CHUNK_SIZE, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) break;
        touch_region(p, CHUNK_SIZE);
        chunks[nchunks++] = p;
    }

    /* Phase 2+3: sustain + periodic bursts */
    time_t last_burst = time(NULL);
    srand((unsigned)last_burst);

    while (1) {
        /* dirty ~1000 random pages per iteration to keep memory hot */
        dirty_random_pages(chunks, nchunks, 1000);

        time_t now = time(NULL);
        if (now - last_burst >= BURST_INTERVAL_S && burst_chunks > 0) {
            /* allocate burst region, touch it, then release */
            char **burst = calloc(burst_chunks, sizeof(char *));
            int nb = 0;
            for (int i = 0; i < burst_chunks; i++) {
                char *p = mmap(NULL, CHUNK_SIZE, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
                if (p == MAP_FAILED) break;
                touch_region(p, CHUNK_SIZE);
                burst[nb++] = p;
            }
            for (int i = 0; i < nb; i++)
                munmap(burst[i], CHUNK_SIZE);
            free(burst);
            last_burst = now;
        }

        usleep(10000); /* 10ms between dirty passes */
    }

    return 0;
}
