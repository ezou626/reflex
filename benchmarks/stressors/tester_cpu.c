#include <sched.h>
#include <time.h>
#include <unistd.h>

// CPU/syscall stress: hammers lightweight syscalls in a tight loop

int main(void) {
    struct timespec ts;

    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &ts);
        getpid();
        sched_yield();
        sleep(4);
    }
    return 0;
}
