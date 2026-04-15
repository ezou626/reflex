#include <bpf/libbpf.h>
#include <stdio.h>
#include <unistd.h>

#include <sys/resource.h>
#include "collector.skel.h"


// struct event{
//     __u32 pid;
//     char filename[512];
// };

struct payload{
    uint32_t tid;
    uint32_t pid;
    uint64_t syscall_id;
    uint64_t cgroup_id; // for scheduling?
    int64_t ret_val;
    uint64_t dur_ns;
    // __u64 args[6];
}__attribute__((packed));



// static int event_logger(void* ctx, void* data, size_t len) {
//     struct event* evt = (struct event*) data;
//     printf("PID = %d and filename = %s\n", evt->pid, evt->filename);
//     return 0;
// }

static int handle_event(void *ctx, void *data, size_t data_size) {
    fwrite(data, 1, data_size, stdout); // maybe refactor this strategy later
    fflush(stdout);
    return 0;
}

/* If stick with python (or can do with Cpp / Rust directly) send pid to loader to drop those */
int main(int argc, char **argv){
    uint32_t py_pid = 0;
    if (argc > 1) {
        py_pid = strtoul(argv[1], NULL, 10);
        fprintf(stderr, "Py_pid %u\n", py_pid);
    }

    struct collector_bpf *skel;
    struct ring_buffer *rb = NULL;
    int err;

    struct rlimit rlim = {
        .rlim_cur = RLIM_INFINITY,
        .rlim_max = RLIM_INFINITY
    };
    setrlimit(RLIMIT_MEMLOCK, &rlim); // not sure purpose of this

    skel = collector_bpf__open();
    if (!skel) {
        fprintf(stderr, "Error with open\n");
        return 1;
    }

    skel->rodata->loader_pid = getpid();
    skel->rodata->python_pid = py_pid;
    skel->rodata->use_cgroup_filter = (argc > 2) ? 1 : 0;

    err = collector_bpf__load(skel);

    if (err) {
        fprintf(stderr, "Failed to load skel %d", err);
        goto cleanup;
    }

    for (int i = 2; i < argc; i++) {
        uint64_t cgid = strtoull(argv[i], NULL, 10);
        uint8_t val = 1;
        bpf_map__update_elem(skel->maps.cgroup_whitelist, &cgid, sizeof(cgid), &val, sizeof(val), BPF_ANY);
    }

    err = collector_bpf__attach(skel);
    if (err) {
        fprintf(stderr, "Error with attach%d\n", err);
        goto cleanup;
    } // ?

    rb = ring_buffer__new(bpf_map__fd(skel->maps.events), handle_event, NULL, NULL);
    if (!rb) {
        fprintf(stderr, "Error with RB\n");
        goto cleanup;
    }

    while(1) {
        ring_buffer__poll(rb, 100); // second value is how often to poll (like adding sleep to loop)
    }

cleanup:
    fprintf(stderr, "Cleanup\n");
    ring_buffer__free(rb);
    collector_bpf__destroy(skel);
    return 0;

    // const char* filename = "collector.bpf.o"; // improve this
    // const char* mapname = "ringbuf";
    // const char* progname = "detect_execve";
    // struct bpf_object *bpfObject = bpf_object__open(filename); 
    // if(!bpfObject) {
    //     printf("Error! Failed to loan %s\n", filename);
    //     return 1;
    // }
    // int err = bpf_object__load(bpfObject);
    // if(err){
    //     printf("Failed to load %s\n", filename);
    //     return 1;
    // }
    // int ringFd = bpf_object__find_map_fd_by_name(bpfObject, mapname);
    // struct ring_buffer* ringBuffer = ring_buffer__new(ringFd, event_logger, NULL, NULL);
    // if (!ringBuffer) {
    //     puts("Failed to create ring buffer");
    //     return 1;
    // }
    // struct bpf_program* bpfProg = bpf_object__find_program_by_name(bpfObject, progname);

    // if (!bpfProg) {
    //     printf("Failed to find %s\n", progname);
    //     return 1;
    // }
    // bpf_program__attach(bpfProg);
    // while(1) {
    //     ring_buffer__consume(ringBuffer); // remove stuff
    //     sleep(1); // reduce cpu load
    // }
    // return 0;
}
