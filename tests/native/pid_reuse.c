/*
 * PID-reuse proof for the sandbox supervisor, run in a private PID namespace.
 *
 * The supervisor keeps its process-group leader unreaped while signal authority is open, so its PID cannot be
 * reused for an unrelated process during cleanup. This harness forces the allocation question instead of waiting
 * for random PID churn: it writes /proc/sys/kernel/ns_last_pid so the next fork receives a chosen PID.
 *
 *   1. A payload forks an escaping setsid descendant (it ignores TERM and survives the group KILL), prints its
 *      leader PID, and exits. Cleanup therefore stays open until the fixed deadline and must be uncertain.
 *   2. While the leader is pinned, the harness asks for the leader's PID. The kernel must hand out another PID;
 *      the probe is killed and reaped.
 *   3. After the supervisor reports leader_reaped, the harness asks again. This time the leader's PID is reused by
 *      an unrelated process that setsid()s and ignores TERM. The supervisor then finishes its uncertain cleanup;
 *      the unrelated process must survive, and the trace must contain no signal event after leader_reaped.
 *
 * Usage: pid_reuse <supervisor-harness> <exec-root> <scratch-dir>
 * Exit: 0 proof complete, 1 assertion failure, 2 usage, 77 namespace/privilege unavailable (the Python test
 * skips explicitly with this code, never treating it as a pass).
 *
 * Requirement: a Linux kernel with PID and mount namespaces, write access to the PID namespace's ns_last_pid
 * (CAP_SYS_ADMIN in the namespace's user namespace: run as root in a privileged container, or with unprivileged
 * user namespaces enabled), and a writable /proc mount.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SKIP_EXIT 77
#define TRACE_NAME "pid-reuse-trace.jsonl"
#define BARRIER_NAME "after_leader_reaped"
#define BARRIER_DIR "pid-reuse-barrier"
#define RECEIPT_NAME "receipt.json"

static void sleep_milliseconds(long milliseconds)
{
    struct timespec delay;

    delay.tv_sec = milliseconds / 1000;
    delay.tv_nsec = (milliseconds % 1000) * 1000000L;
    (void)nanosleep(&delay, NULL);
}

static int write_all(int fd, const char *text, size_t length)
{
    size_t offset = 0;

    while (offset < length) {
        ssize_t written = write(fd, text + offset, length - offset);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        offset += (size_t)written;
    }
    return 0;
}

static int write_text_file(const char *path, const char *text)
{
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    int result;

    if (fd < 0) {
        return -1;
    }
    result = write_all(fd, text, strlen(text));
    if (close(fd) != 0) {
        result = -1;
    }
    return result;
}

static int path_join(char *out, size_t size, const char *directory, const char *name)
{
    int length = snprintf(out, size, "%s/%s", directory, name);

    return length > 0 && (size_t)length < size ? 0 : -1;
}

static int file_exists(const char *path)
{
    struct stat status;

    return stat(path, &status) == 0;
}

static int pid_namespace_available(void)
{
    int saved;

    if (unshare(CLONE_NEWPID | CLONE_NEWNS) == 0) {
        return 1;
    }
    saved = errno;
    if (geteuid() != 0 && unshare(CLONE_NEWUSER) == 0) {
        char mapping[64];

        (void)write_text_file("/proc/self/setgroups", "deny");
        (void)snprintf(mapping, sizeof(mapping), "0 %ld 1\n", (long)geteuid());
        (void)write_text_file("/proc/self/uid_map", mapping);
        (void)snprintf(mapping, sizeof(mapping), "0 %ld 1\n", (long)getegid());
        (void)write_text_file("/proc/self/gid_map", mapping);
        if (unshare(CLONE_NEWPID | CLONE_NEWNS) == 0) {
            return 1;
        }
    }
    errno = saved;
    return 0;
}

static int mount_private_proc(void)
{
    if (mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0) {
        return -1;
    }
    return 0;
}

/* Verify that ns_last_pid is writable, without changing the next allocation. */
static int ns_last_pid_writable(void)
{
    char value[32];
    ssize_t length;
    int fd;

    fd = open("/proc/sys/kernel/ns_last_pid", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        return 0;
    }
    length = read(fd, value, sizeof(value) - 1);
    if (length <= 0) {
        close(fd);
        return 0;
    }
    value[length] = '\0';
    if (lseek(fd, 0, SEEK_SET) != 0) {
        close(fd);
        return 0;
    }
    if (write_all(fd, value, (size_t)length) != 0) {
        close(fd);
        return 0;
    }
    close(fd);
    return 1;
}

static int set_last_pid(long value)
{
    char text[32];
    int length = snprintf(text, sizeof(text), "%ld", value);
    int fd;

    if (length <= 0) {
        return -1;
    }
    fd = open("/proc/sys/kernel/ns_last_pid", O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        return -1;
    }
    if (write_all(fd, text, (size_t)length) != 0) {
        close(fd);
        return -1;
    }
    return close(fd);
}

static int wait_for_file(const char *path, long timeout_milliseconds)
{
    long waited = 0;

    while (waited <= timeout_milliseconds) {
        if (file_exists(path)) {
            return 0;
        }
        sleep_milliseconds(5);
        waited += 5;
    }
    return -1;
}

static int trace_contains(const char *trace_path, const char *needle)
{
    char buffer[4096];
    ssize_t length;
    int fd;

    fd = open(trace_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return 0;
    }
    length = read(fd, buffer, sizeof(buffer) - 1);
    close(fd);
    if (length <= 0) {
        return 0;
    }
    buffer[length] = '\0';
    return strstr(buffer, needle) != NULL;
}

static int wait_for_trace(const char *trace_path, const char *needle, long timeout_milliseconds)
{
    long waited = 0;

    while (waited <= timeout_milliseconds) {
        if (trace_contains(trace_path, needle)) {
            return 0;
        }
        sleep_milliseconds(5);
        waited += 5;
    }
    return -1;
}

/* No term/kill event may appear after leader_reaped in the append-only trace. */
static int signals_after_reap(const char *trace_path)
{
    char buffer[65536];
    ssize_t length;
    char *cursor;
    char *marker;
    int fd;

    fd = open(trace_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return 1;
    }
    length = read(fd, buffer, sizeof(buffer) - 1);
    close(fd);
    if (length <= 0) {
        return 1;
    }
    buffer[length] = '\0';
    marker = strstr(buffer, "\"kind\":\"leader_reaped\"");
    if (marker == NULL) {
        return 1;
    }
    cursor = strchr(marker, '\n');
    if (cursor == NULL) {
        return 0;
    }
    cursor++;
    return strstr(cursor, "\"kind\":\"term\"") != NULL || strstr(cursor, "\"kind\":\"kill\"") != NULL;
}

static int read_line(int fd, char *buffer, size_t size, long timeout_milliseconds)
{
    size_t offset = 0;

    while (offset + 1 < size) {
        struct pollfd descriptor;
        int ready;
        ssize_t length;

        descriptor.fd = fd;
        descriptor.events = POLLIN;
        descriptor.revents = 0;
        ready = poll(&descriptor, 1, (int)timeout_milliseconds);
        if (ready <= 0) {
            return -1;
        }
        length = read(fd, buffer + offset, 1);
        if (length <= 0) {
            return -1;
        }
        if (buffer[offset] == '\n') {
            buffer[offset] = '\0';
            return 0;
        }
        offset++;
    }
    return -1;
}

static const char *payload_script =
    "import os, signal, time\n"
    "read_fd, write_fd = os.pipe()\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    os.close(read_fd)\n"
    "    os.setsid()\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    print('escaped', os.getpid(), flush=True)\n"
    "    os.write(write_fd, b'x')\n"
    "    os.close(write_fd)\n"
    "    time.sleep(30)\n"
    "    os._exit(0)\n"
    "os.close(write_fd)\n"
    "os.read(read_fd, 1)\n"
    "os.close(read_fd)\n"
    "print('leader', os.getpid(), flush=True)\n";

static int spawn_probe(pid_t *probe_out)
{
    pid_t child = fork();

    if (child < 0) {
        return -1;
    }
    if (child == 0) {
        pause();
        _exit(0);
    }
    *probe_out = child;
    return 0;
}

static void kill_and_reap(pid_t child)
{
    int status;

    if (child <= 0) {
        return;
    }
    (void)kill(child, SIGKILL);
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
    }
}

static int run_isolated(const char *harness, const char *root, const char *scratch)
{
    char trace_path[1024];
    char barrier_dir[1024];
    char ready_path[1024];
    char release_path[1024];
    char receipt_path[1024];
    char trace_fd_text[32];
    const char *control_token = "00112233445566778899aabbccddeeff";
    const char *usage_token = "ffeeddccbbaa99887766554433221100";
    char *arguments[20];
    int output[2] = {-1, -1};
    int trace_fd = -1;
    pid_t supervisor = -1;
    pid_t probe = -1;
    pid_t unrelated = -1;
    pid_t leader_pid = 0;
    pid_t escaped_pid = 0;
    pid_t probe_pid = 0;
    int index = 0;
    int exit_code = 1;
    int status = 0;

    if (mount_private_proc() != 0) {
        fprintf(stderr, "pid-reuse: skip: cannot mount a private /proc: %s\n", strerror(errno));
        return SKIP_EXIT;
    }
    if (!ns_last_pid_writable()) {
        fprintf(stderr, "pid-reuse: skip: /proc/sys/kernel/ns_last_pid is not writable in this namespace\n");
        return SKIP_EXIT;
    }
    if (path_join(trace_path, sizeof(trace_path), scratch, TRACE_NAME) != 0 ||
        path_join(barrier_dir, sizeof(barrier_dir), scratch, BARRIER_DIR) != 0 ||
        path_join(ready_path, sizeof(ready_path), barrier_dir, "ready-" BARRIER_NAME) != 0 ||
        path_join(release_path, sizeof(release_path), barrier_dir, "release-" BARRIER_NAME) != 0) {
        fprintf(stderr, "pid-reuse: scratch path is too long\n");
        return 1;
    }
    {
        int length = snprintf(receipt_path, sizeof(receipt_path), "%s/%ld/%s/%s", root,
                              (long)geteuid(), control_token, RECEIPT_NAME);
        if (length <= 0 || (size_t)length >= sizeof(receipt_path)) {
            fprintf(stderr, "pid-reuse: exec directory path is too long\n");
            return 1;
        }
    }
    (void)mkdir(barrier_dir, 0700);
    trace_fd = open(trace_path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (trace_fd < 0 || pipe(output) != 0) {
        fprintf(stderr, "pid-reuse: cannot create the supervisor transport: %s\n", strerror(errno));
        return 1;
    }
    (void)snprintf(trace_fd_text, sizeof(trace_fd_text), "%d", trace_fd);

    supervisor = fork();
    if (supervisor < 0) {
        fprintf(stderr, "pid-reuse: cannot fork the supervisor: %s\n", strerror(errno));
        return 1;
    }
    if (supervisor == 0) {
        (void)dup2(output[1], STDOUT_FILENO);
        close(output[0]);
        close(output[1]);
        (void)setenv("KEEL_TEST_TRACE_FD", trace_fd_text, 1);
        (void)setenv("KEEL_TEST_BARRIER", BARRIER_NAME, 1);
        (void)setenv("KEEL_TEST_BARRIER_DIR", barrier_dir, 1);
        arguments[index++] = (char *)harness;
        arguments[index++] = "run";
        arguments[index++] = "--root";
        arguments[index++] = (char *)root;
        arguments[index++] = "--exec-id";
        arguments[index++] = "pid-reuse";
        arguments[index++] = "--control-token";
        arguments[index++] = (char *)control_token;
        arguments[index++] = "--usage-token";
        arguments[index++] = (char *)usage_token;
        arguments[index++] = "--grace";
        arguments[index++] = "0.6";
        arguments[index++] = "--reap";
        arguments[index++] = "2.0";
        arguments[index++] = "--";
        arguments[index++] = "python3";
        arguments[index++] = "-u";
        arguments[index++] = "-c";
        arguments[index++] = (char *)payload_script;
        arguments[index] = NULL;
        execv(harness, arguments);
        _exit(127);
    }
    close(output[1]);
    output[1] = -1;
    close(trace_fd);
    trace_fd = -1;

    for (;;) {
        char line[128];
        if (read_line(output[0], line, sizeof(line), 10000) != 0) {
            fprintf(stderr, "pid-reuse: timed out waiting for the payload identity\n");
            goto cleanup;
        }
        if (sscanf(line, "leader %d", &leader_pid) == 1 && leader_pid > 0) {
            if (escaped_pid > 0) {
                break;
            }
        } else if (sscanf(line, "escaped %d", &escaped_pid) == 1 && escaped_pid > 0) {
            if (leader_pid > 0) {
                break;
            }
        }
    }
    if (leader_pid <= 0 || escaped_pid <= 0) {
        fprintf(stderr, "pid-reuse: payload did not report its processes\n");
        goto cleanup;
    }

    /* Cleanup has started but the leader is still an unreaped zombie: its PID cannot be reused. */
    if (wait_for_trace(trace_path, "\"kind\":\"cleanup\"", 10000) != 0) {
        fprintf(stderr, "pid-reuse: cleanup never started\n");
        goto cleanup;
    }
    if (set_last_pid((long)leader_pid - 1) != 0 || spawn_probe(&probe) != 0) {
        fprintf(stderr, "pid-reuse: cannot probe PID allocation: %s\n", strerror(errno));
        goto cleanup;
    }
    probe_pid = probe;
    if (probe_pid == leader_pid) {
        fprintf(stderr, "pid-reuse: the pinned leader's PID was reused during cleanup\n");
        goto cleanup;
    }
    kill_and_reap(probe);
    probe = -1;

    /* The supervisor holds at the barrier after reaping the leader, before draining descendants. */
    if (wait_for_file(ready_path, 10000) != 0) {
        fprintf(stderr, "pid-reuse: the supervisor never recorded leader_reaped\n");
        goto cleanup;
    }
    if (set_last_pid((long)leader_pid - 1) != 0) {
        fprintf(stderr, "pid-reuse: cannot steer PID allocation after reaping: %s\n", strerror(errno));
        goto cleanup;
    }
    unrelated = fork();
    if (unrelated < 0) {
        fprintf(stderr, "pid-reuse: cannot fork the unrelated process: %s\n", strerror(errno));
        goto cleanup;
    }
    if (unrelated == 0) {
        (void)setsid();
        (void)signal(SIGTERM, SIG_IGN);
        sleep_milliseconds(10000);
        _exit(0);
    }
    if (unrelated != leader_pid) {
        fprintf(stderr, "pid-reuse: expected PID %d to be reused, got %d\n", (int)leader_pid,
                (int)unrelated);
        goto cleanup;
    }
    (void)unlink(ready_path);
    {
        int release_fd = open(release_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (release_fd < 0) {
            fprintf(stderr, "pid-reuse: cannot release the supervisor barrier: %s\n", strerror(errno));
            goto cleanup;
        }
        close(release_fd);
    }

    {
        int finished = 0;
        int waited = 0;

        while (waited < 10000) {
            pid_t reaped = waitpid(supervisor, &status, WNOHANG);
            if (reaped == supervisor) {
                finished = 1;
                break;
            }
            if (reaped < 0) {
                break;
            }
            sleep_milliseconds(10);
            waited += 10;
        }
        if (!finished) {
            fprintf(stderr, "pid-reuse: the supervisor did not finish\n");
            goto cleanup;
        }
        supervisor = -1;
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 195) {
        fprintf(stderr, "pid-reuse: expected uncertain exit 195, got status %d\n", status);
        goto cleanup;
    }
    if (!file_exists(receipt_path) ||
        !trace_contains(receipt_path, "\"descendants_complete\":false") ||
        !trace_contains(receipt_path, "\"uncertain_reason\":\"descendants_unreaped\"")) {
        fprintf(stderr, "pid-reuse: the escaped descendant did not produce an uncertain receipt\n");
        goto cleanup;
    }
    if (waitpid(unrelated, &status, WNOHANG) != 0) {
        fprintf(stderr, "pid-reuse: the unrelated process did not survive the helper\n");
        goto cleanup;
    }
    if (signals_after_reap(trace_path)) {
        fprintf(stderr, "pid-reuse: a signal was recorded after leader_reaped\n");
        goto cleanup;
    }

    printf("{\"leader_pid\":%d,\"probe_pid\":%d,\"reused_pid\":%d,\"survived\":true,"
           "\"signal_after_reap\":false,\"receipt_uncertain\":true}\n",
           (int)leader_pid, (int)probe_pid, (int)unrelated);
    (void)fflush(stdout);
    exit_code = 0;

cleanup:
    if (probe > 0) {
        kill_and_reap(probe);
    }
    if (supervisor > 0) {
        kill_and_reap(supervisor);
    }
    if (unrelated > 0) {
        kill_and_reap(unrelated);
    }
    if (escaped_pid > 0) {
        kill_and_reap(escaped_pid);
    }
    if (output[0] >= 0) {
        close(output[0]);
    }
    return exit_code;
}

int main(int argc, char **argv)
{
    pid_t child;
    int status = 0;

    if (argc != 4) {
        fprintf(stderr, "usage: %s <supervisor-harness> <exec-root> <scratch-dir>\n", argv[0]);
        return 2;
    }
    if (!pid_namespace_available()) {
        fprintf(stderr, "pid-reuse: skip: a private PID namespace is unavailable: %s\n", strerror(errno));
        return SKIP_EXIT;
    }
    child = fork();
    if (child < 0) {
        fprintf(stderr, "pid-reuse: cannot fork the namespace init: %s\n", strerror(errno));
        return 1;
    }
    if (child == 0) {
        _exit(run_isolated(argv[1], argv[2], argv[3]));
    }
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}
