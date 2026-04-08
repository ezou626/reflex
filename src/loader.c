#include <linux/bpf.h>
#include <bpf/libbpf.h>
#include <stdio.h>
#include <unistd.h>


struct event{
    __u32 pid;
    char filename[512];
};

static int event_logger(void* ctx, void* data, size_t len) {
    struct event* evt = (struct event*) data;
    printf("PID = %d and filename = %s\n", evt->pid, evt->filename);
    return 0;
}

int main(){
    const char* filename = "collector.bpf.o"; // improve this
    const char* mapname = "ringbuf";
    const char* progname = "detect_execve";
    struct bpf_object *bpfObject = bpf_object__open(filename); // ?
    if(!bpfObject) {
        printf("Error! Failed to loan %s\n", filename);
        return 1;
    }
    int err = bpf_object__load(bpfObject);
    if(err){
        printf("Failed to load %s\n", filename);
        return 1;
    }
    int ringFd = bpf_object__find_map_fd_by_name(bpfObject, mapname);
    struct ring_buffer* ringBuffer = ring_buffer__new(ringFd, event_logger, NULL, NULL);
    if (!ringBuffer) {
        puts("Failed to create ring buffer");
        return 1;
    }
    struct bpf_program* bpfProg = bpf_object__find_program_by_name(bpfObject, progname);

    if (!bpfProg) {
        printf("Failed to find %s\n", progname);
        return 1;
    }
    bpf_program__attach(bpfProg);
    while(1) {
        ring_buffer__consume(ringBuffer); // remove stuff
        sleep(1); // reduce cpu load
    }
    return 0;
}