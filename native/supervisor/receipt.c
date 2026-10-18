/*
 * Bounded terminal receipts: exactly version=1, exec_id, control_token, leader_status, control_closed,
 * descendants_complete and uncertain_reason. Receipts are written atomically (temporary file + rename) and are
 * retained alongside the reservation until the container is removed.
 */
#include "supervisor.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int write_all(int fd, const void *data, size_t length)
{
    const unsigned char *cursor = data;

    while (length > 0) {
        ssize_t written = write(fd, cursor, length);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += (size_t)written;
        length -= (size_t)written;
    }
    return 0;
}

static int valid_reason(const char *reason)
{
    size_t length;
    size_t index;

    if (reason == NULL) {
        return 1;
    }
    length = strlen(reason);
    if (length == 0 || length > KEEL_REASON_MAX) {
        return 0;
    }
    for (index = 0; index < length; index++) {
        char character = reason[index];
        if (!((character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') ||
              character == '_')) {
            return 0;
        }
    }
    return 1;
}

int keel_build_receipt(const struct keel_receipt *receipt, char *out, size_t size)
{
    char status[16];
    char reason[KEEL_REASON_MAX + 3];
    int length;

    if (receipt == NULL || out == NULL || !keel_valid_exec_id(receipt->exec_id) ||
        !keel_valid_token(receipt->control_token) || !valid_reason(receipt->uncertain_reason)) {
        return -1;
    }
    if (receipt->has_status && (receipt->leader_status < 0 || receipt->leader_status > 255)) {
        return -1;
    }
    if (receipt->has_status) {
        (void)snprintf(status, sizeof(status), "%d", receipt->leader_status);
    } else {
        (void)snprintf(status, sizeof(status), "null");
    }
    if (receipt->uncertain_reason == NULL) {
        (void)snprintf(reason, sizeof(reason), "null");
    } else {
        int written = snprintf(reason, sizeof(reason), "\"%s\"", receipt->uncertain_reason);
        if (written < 0 || (size_t)written >= sizeof(reason)) {
            return -1;
        }
    }
    length = snprintf(out, size,
                      "{\"version\":%d,\"exec_id\":\"%s\",\"control_token\":\"%s\",\"leader_status\":%s,"
                      "\"control_closed\":%s,\"descendants_complete\":%s,\"uncertain_reason\":%s}",
                      KEEL_PROTOCOL_VERSION, receipt->exec_id, receipt->control_token, status,
                      receipt->control_closed ? "true" : "false",
                      receipt->descendants_complete ? "true" : "false", reason);
    if (length < 0 || (size_t)length >= size) {
        return -1;
    }
    return length;
}

int keel_write_receipt(const char *dir, const struct keel_receipt *receipt)
{
    char text[KEEL_RECEIPT_MAX + 1];
    char temp_path[PATH_MAX];
    char final_path[PATH_MAX];
    int fd;
    int length;

    length = keel_build_receipt(receipt, text, sizeof(text));
    if (length < 0) {
        return -1;
    }
    if (keel_path(temp_path, sizeof(temp_path), dir, KEEL_RECEIPT_TEMP_NAME) != 0 ||
        keel_path(final_path, sizeof(final_path), dir, KEEL_RECEIPT_NAME) != 0) {
        return -1;
    }
    fd = open(temp_path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        return -1;
    }
    if (write_all(fd, text, (size_t)length) != 0 || fsync(fd) != 0) {
        close(fd);
        (void)unlink(temp_path);
        return -1;
    }
    if (close(fd) != 0) {
        (void)unlink(temp_path);
        return -1;
    }
    if (rename(temp_path, final_path) != 0) {
        (void)unlink(temp_path);
        return -1;
    }
    return 0;
}

int keel_read_receipt(const char *dir, char *out, size_t size)
{
    char path[PATH_MAX];
    ssize_t length;
    int fd;

    if (out == NULL || size < 2 || keel_path(path, sizeof(path), dir, KEEL_RECEIPT_NAME) != 0) {
        return -1;
    }
    fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return -1;
    }
    length = read(fd, out, size);
    close(fd);
    if (length <= 0 || (size_t)length > KEEL_RECEIPT_MAX || (size_t)length >= size) {
        return -1;
    }
    out[length] = '\0';
    if (out[0] != '{' || out[length - 1] != '}') {
        return -1;
    }
    return (int)length;
}

int keel_receipt_command(const struct keel_receipt_request *request)
{
    char dir[PATH_MAX];
    char receipt[KEEL_RECEIPT_MAX + 1];
    int length;

    if (request == NULL || !keel_valid_token(request->control_token) ||
        keel_exec_dir(request->root, request->control_token, dir, sizeof(dir)) != 0) {
        fprintf(stderr, "keel-supervisor: invalid receipt request\n");
        return KEEL_EXIT_INVALID_REQUEST;
    }
    length = keel_read_receipt(dir, receipt, sizeof(receipt));
    if (length < 0) {
        fprintf(stderr, "keel-supervisor: terminal receipt is not available\n");
        return KEEL_EXIT_NO_RECEIPT;
    }
    (void)fwrite(receipt, 1, (size_t)length, stdout);
    (void)fputc('\n', stdout);
    (void)fflush(stdout);
    return 0;
}
