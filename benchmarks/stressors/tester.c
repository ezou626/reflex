#include <unistd.h>
#include <stdio.h>
#include <time.h>

int main() {
    const int n = 1000000;
    for (int i = 0; i < n; i++) {
        getpid();
        sleep(5);
    }
    printf("done: %d getpid calls\n", n);
    return 0;
}