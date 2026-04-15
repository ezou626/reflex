#include <sys/mman.h>
#include <unistd.h>

// Memory stress: hammers mmap/munmap syscalls in a tight loop

#define MAP_SIZE (64 * 1024)

int main(void) {
    while (1) {
        void *p = mmap(NULL, MAP_SIZE, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) continue;
        munmap(p, MAP_SIZE);
        sleep(5);
    }
    return 0;
}
