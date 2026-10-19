/*
 * Test-only supervisor hooks. This translation unit is linked only into the `supervisor-harness` target with
 * -DKEEL_TEST_HOOKS; the production `keel-supervisor` binary never references these environment variables.
 *
 * KEEL_TEST_TRACE_FD     append JSONL events to this file descriptor
 * KEEL_TEST_BARRIER      comma-separated barrier names to stop at
 * KEEL_TEST_BARRIER_DIR  directory where ready-<name>/release-<name> files are exchanged
 * KEEL_TEST_FAIL_SUBREAPER  simulate unavailable PR_SET_CHILD_SUBREAPER support
 * KEEL_TEST_WAIT_ERROR   simulate an unexpected child-wait error during descendant draining
 *
 * Events (one JSON object per line):
 *   reservation, duplicate, subreaper, fork, handshake, payload_start, cleanup, supervisor_signal, control,
 *   control_rejected, leader_exit, term, kill, control_closed, leader_reaped, descendant_reaped, echild,
 *   wait_failed, reap_failed, setup_failed, receipt, receipt_written, receipt_failed, barrier. Signal events
 *   term/kill carry leader_pinned.
 */
#include "supervisor.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define KEEL_HARNESS_BARRIER_TIMEOUT_MS 60000

static int trace_fd = -1;
static int trace_initialized = 0;
static int environment_initialized = 0;
static int fail_subreaper = 0;
static int wait_error = 0;

static void environment_init(void)
{
    const char *configured;

    if (environment_initialized) {
        return;
    }
    environment_initialized = 1;
    configured = getenv("KEEL_TEST_FAIL_SUBREAPER");
    fail_subreaper = configured != NULL && *configured != '\0';
    configured = getenv("KEEL_TEST_WAIT_ERROR");
    wait_error = configured != NULL && *configured != '\0';
}

int keel_test_subreaper_failed(void)
{
    environment_init();
    return fail_subreaper;
}

int keel_test_wait_error(void)
{
    environment_init();
    return wait_error;
}

static long long monotonic_milliseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (long long)now.tv_sec * 1000LL + (long long)now.tv_nsec / 1000000LL;
}

static void trace_init(void)
{
    const char *configured;
    char *end = NULL;
    long value;

    if (trace_initialized) {
        return;
    }
    trace_initialized = 1;
    configured = getenv("KEEL_TEST_TRACE_FD");
    if (configured == NULL || *configured == '\0') {
        return;
    }
    value = strtol(configured, &end, 10);
    if (end == NULL || *end != '\0' || value < 0 || value > INT_MAX) {
        return;
    }
    trace_fd = (int)value;
    (void)fcntl(trace_fd, F_SETFD, FD_CLOEXEC);
}

void keel_test_trace(const char *kind, const char *detail, long value, int leader_pinned)
{
    char line[512];
    int length;

    trace_init();
    if (trace_fd < 0) {
        return;
    }
    length = snprintf(line, sizeof(line),
                      "{\"t_ms\":%lld,\"kind\":\"%s\",\"detail\":%s%s%s,\"value\":%ld,"
                      "\"leader_pinned\":%s}\n",
                      monotonic_milliseconds(), kind == NULL ? "unknown" : kind,
                      detail == NULL ? "null" : "\"", detail == NULL ? "" : detail,
                      detail == NULL ? "" : "\"", value, leader_pinned ? "true" : "false");
    if (length > 0 && (size_t)length < sizeof(line)) {
        ssize_t written;
        size_t offset = 0;

        while (offset < (size_t)length) {
            written = write(trace_fd, line + offset, (size_t)length - offset);
            if (written < 0) {
                if (errno == EINTR) {
                    continue;
                }
                return;
            }
            offset += (size_t)written;
        }
    }
}

static int barrier_armed(const char *name)
{
    const char *configured = getenv("KEEL_TEST_BARRIER");
    const char *cursor;
    size_t name_length;

    if (configured == NULL || name == NULL) {
        return 0;
    }
    name_length = strlen(name);
    cursor = configured;
    while (*cursor != '\0') {
        const char *end = strchr(cursor, ',');
        size_t length = end == NULL ? strlen(cursor) : (size_t)(end - cursor);

        if (length == name_length && strncmp(cursor, name, length) == 0) {
            return 1;
        }
        if (end == NULL) {
            break;
        }
        cursor = end + 1;
    }
    return 0;
}

static int touch_file(const char *directory, const char *prefix, const char *name)
{
    char path[PATH_MAX];
    int length;
    int fd;

    length = snprintf(path, sizeof(path), "%s/%s-%s", directory, prefix, name);
    if (length < 0 || (size_t)length >= sizeof(path)) {
        return -1;
    }
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) {
        return -1;
    }
    (void)close(fd);
    return 0;
}

static int file_exists(const char *directory, const char *prefix, const char *name)
{
    char path[PATH_MAX];
    struct stat status;
    int length;

    length = snprintf(path, sizeof(path), "%s/%s-%s", directory, prefix, name);
    if (length < 0 || (size_t)length >= sizeof(path)) {
        return 0;
    }
    return stat(path, &status) == 0;
}

/*
 * Stop at an armed barrier and wait for the test to release it. Only the harness build compiles this; production
 * supervisor code calls the no-op macro from supervisor.h.
 */
void keel_test_barrier(const char *name)
{
    const char *directory = getenv("KEEL_TEST_BARRIER_DIR");
    long long deadline;

    if (directory == NULL || *directory == '\0' || !barrier_armed(name)) {
        return;
    }
    keel_test_trace("barrier", name, 0, 0);
    if (touch_file(directory, "ready", name) != 0) {
        return;
    }
    deadline = monotonic_milliseconds() + KEEL_HARNESS_BARRIER_TIMEOUT_MS;
    while (!file_exists(directory, "release", name)) {
        if (monotonic_milliseconds() >= deadline) {
            return;
        }
        usleep(2000);
    }
}
