/*
 * Control identity, per-exec directory, Unix-domain endpoint and the `cancel` client.
 *
 * The endpoint is a SOCK_SEQPACKET socket inside <root>/<euid>/<control-token>/; both directories are mode 0700
 * and the socket is mode 0600. Control clients only exchange fixed 37-byte frames. They never learn or signal a
 * numeric PID.
 */
#include "supervisor.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define KEEL_CLIENT_TIMEOUT_MS 2000

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

int keel_valid_token(const char *token)
{
    size_t index;

    if (token == NULL || strlen(token) != KEEL_TOKEN_HEX_LEN) {
        return 0;
    }
    for (index = 0; index < KEEL_TOKEN_HEX_LEN; index++) {
        char character = token[index];
        if (!((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f'))) {
            return 0;
        }
    }
    return 1;
}

int keel_valid_exec_id(const char *exec_id)
{
    size_t length;
    size_t index;

    if (exec_id == NULL) {
        return 0;
    }
    length = strlen(exec_id);
    if (length == 0 || length > KEEL_EXEC_ID_MAX) {
        return 0;
    }
    for (index = 0; index < length; index++) {
        char character = exec_id[index];
        int safe = (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') ||
                   (character >= '0' && character <= '9') || character == '.' || character == '_' ||
                   character == ':' || character == '-';
        if (!safe) {
            return 0;
        }
    }
    return 1;
}

int keel_path(char *out, size_t size, const char *dir, const char *name)
{
    int length = snprintf(out, size, "%s/%s", dir, name);

    if (length < 0 || (size_t)length >= size) {
        return -1;
    }
    return 0;
}

int keel_exec_dir(const char *root, const char *control_token, char *dir, size_t size)
{
    int length;

    if (root == NULL || root[0] != '/' || !keel_valid_token(control_token)) {
        return -1;
    }
    length = snprintf(dir, size, "%s/%lu/%s", root, (unsigned long)geteuid(), control_token);
    if (length < 0 || (size_t)length >= size) {
        return -1;
    }
    /* The sibling socket path is "<dir>/control.sock"; both must fit in sockaddr_un.sun_path. */
    if (strlen(dir) + 1 + strlen(KEEL_CONTROL_SOCKET_NAME) > KEEL_SUN_PATH_MAX) {
        return -1;
    }
    return 0;
}

static int ensure_private_dir(const char *path)
{
    struct stat status;

    if (mkdir(path, 0700) == 0) {
        return 0;
    }
    if (errno != EEXIST) {
        return -1;
    }
    if (lstat(path, &status) != 0) {
        return -1;
    }
    if (!S_ISDIR(status.st_mode) || status.st_uid != geteuid()) {
        return -1;
    }
    if (chmod(path, 0700) != 0) {
        return -1;
    }
    return 0;
}

int keel_ensure_exec_dir(const char *root, const char *control_token, char *dir, size_t dir_size,
                         char *socket_path, size_t socket_size)
{
    char uid_dir[PATH_MAX];
    char full_dir[PATH_MAX];
    int length;

    if (root == NULL || root[0] != '/') {
        return -1;
    }
    if (ensure_private_dir(root) != 0) {
        return -1;
    }
    length = snprintf(uid_dir, sizeof(uid_dir), "%s/%lu", root, (unsigned long)geteuid());
    if (length < 0 || (size_t)length >= sizeof(uid_dir)) {
        return -1;
    }
    if (ensure_private_dir(uid_dir) != 0) {
        return -1;
    }
    if (keel_exec_dir(root, control_token, full_dir, sizeof(full_dir)) != 0) {
        return -1;
    }
    if (ensure_private_dir(full_dir) != 0) {
        return -1;
    }
    if (keel_path(socket_path, socket_size, full_dir, KEEL_CONTROL_SOCKET_NAME) != 0) {
        return -1;
    }
    length = snprintf(dir, dir_size, "%s", full_dir);
    if (length < 0 || (size_t)length >= dir_size) {
        return -1;
    }
    return 0;
}

/*
 * Reserve the never-reused exec identity before forking. Success is exactly one O_CREAT|O_EXCL file. The file
 * carries the identity binding (including the usage token) so a `cancel` after the supervisor exits can still
 * reject a wrong usage token before printing the original terminal receipt.
 */
int keel_reserve_identity(const char *dir, const char *exec_id, const char *control_token,
                          const char *usage_token)
{
    char path[PATH_MAX];
    char record[512];
    int fd;
    int length;

    if (keel_path(path, sizeof(path), dir, KEEL_RESERVATION_NAME) != 0) {
        return -1;
    }
    fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        return errno == EEXIST ? 1 : -1;
    }
    length = snprintf(record, sizeof(record),
                      "{\"version\":%d,\"exec_id\":\"%s\",\"control_token\":\"%s\",\"usage_token\":\"%s\"}\n",
                      KEEL_PROTOCOL_VERSION, exec_id, control_token, usage_token);
    if (length < 0 || (size_t)length >= sizeof(record) || write_all(fd, record, (size_t)length) != 0) {
        close(fd);
        (void)unlink(path); /* nothing was forked; do not burn the identity on a local write failure */
        return -1;
    }
    if (close(fd) != 0) {
        (void)unlink(path);
        return -1;
    }
    return 0;
}

int keel_reservation_usage(const char *dir, char *usage_out, size_t size)
{
    char path[PATH_MAX];
    char record[512];
    const char *marker = "\"usage_token\":\"";
    char *start;
    ssize_t length;
    int fd;

    if (size < KEEL_TOKEN_HEX_LEN + 1 || keel_path(path, sizeof(path), dir, KEEL_RESERVATION_NAME) != 0) {
        return -1;
    }
    fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        return -1;
    }
    length = read(fd, record, sizeof(record) - 1);
    close(fd);
    if (length <= 0 || (size_t)length >= sizeof(record) - 1) {
        return -1;
    }
    record[length] = '\0';
    start = strstr(record, marker);
    if (start == NULL) {
        return -1;
    }
    start += strlen(marker);
    if (strlen(start) < KEEL_TOKEN_HEX_LEN + 1 || start[KEEL_TOKEN_HEX_LEN] != '"') {
        return -1;
    }
    memcpy(usage_out, start, KEEL_TOKEN_HEX_LEN);
    usage_out[KEEL_TOKEN_HEX_LEN] = '\0';
    return keel_valid_token(usage_out) ? 0 : -1;
}

int keel_bind_control_socket(const char *socket_path)
{
    struct sockaddr_un address;
    int fd;

    if (socket_path == NULL || strlen(socket_path) > KEEL_SUN_PATH_MAX) {
        return -1;
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) {
        return -1;
    }
    /* The directory is private to this identity, so a stale socket from a crashed attempt cannot be live. */
    (void)unlink(socket_path);
    if (bind(fd, (const struct sockaddr *)&address, sizeof(address)) != 0) {
        close(fd);
        return -1;
    }
    if (chmod(socket_path, 0600) != 0 || listen(fd, 16) != 0) {
        (void)unlink(socket_path);
        close(fd);
        return -1;
    }
    return fd;
}

static int hex_value(char character)
{
    if (character >= '0' && character <= '9') {
        return character - '0';
    }
    if (character >= 'a' && character <= 'f') {
        return character - 'a' + 10;
    }
    return -1;
}

static int decode_token(const char *token, unsigned char out[KEEL_TOKEN_BYTES])
{
    size_t index;

    if (!keel_valid_token(token)) {
        return -1;
    }
    for (index = 0; index < KEEL_TOKEN_BYTES; index++) {
        int high = hex_value(token[index * 2]);
        int low = hex_value(token[index * 2 + 1]);
        if (high < 0 || low < 0) {
            return -1;
        }
        out[index] = (unsigned char)((high << 4) | low);
    }
    return 0;
}

int keel_encode_control(const char *control_token, const char *usage_token, int cancel,
                        unsigned char out[KEEL_CONTROL_FRAME_LEN])
{
    if (decode_token(control_token, out + KEEL_CONTROL_MAGIC_LEN) != 0 ||
        decode_token(usage_token, out + KEEL_CONTROL_MAGIC_LEN + KEEL_TOKEN_BYTES) != 0) {
        return -1;
    }
    memcpy(out, KEEL_CONTROL_MAGIC, KEEL_CONTROL_MAGIC_LEN);
    out[KEEL_CONTROL_FRAME_LEN - 1] = cancel ? 1 : 0;
    return 0;
}

int keel_decode_control(const unsigned char *frame, size_t length, const char *control_token,
                        const char *usage_token, int *cancel)
{
    unsigned char expected_control[KEEL_TOKEN_BYTES];
    unsigned char expected_usage[KEEL_TOKEN_BYTES];

    if (frame == NULL || length != KEEL_CONTROL_FRAME_LEN || cancel == NULL) {
        return -1;
    }
    if (memcmp(frame, KEEL_CONTROL_MAGIC, KEEL_CONTROL_MAGIC_LEN) != 0) {
        return -1;
    }
    if (decode_token(control_token, expected_control) != 0 ||
        decode_token(usage_token, expected_usage) != 0) {
        return -1;
    }
    if (memcmp(frame + KEEL_CONTROL_MAGIC_LEN, expected_control, KEEL_TOKEN_BYTES) != 0 ||
        memcmp(frame + KEEL_CONTROL_MAGIC_LEN + KEEL_TOKEN_BYTES, expected_usage, KEEL_TOKEN_BYTES) != 0) {
        return -1;
    }
    if (frame[KEEL_CONTROL_FRAME_LEN - 1] != 0 && frame[KEEL_CONTROL_FRAME_LEN - 1] != 1) {
        return -1;
    }
    *cancel = frame[KEEL_CONTROL_FRAME_LEN - 1];
    return 0;
}

int keel_send_response(int fd, unsigned char response, const char *receipt, size_t receipt_length)
{
    if (send(fd, &response, 1, MSG_NOSIGNAL) != 1) {
        return -1;
    }
    if (receipt != NULL && receipt_length > 0 &&
        send(fd, receipt, receipt_length, MSG_NOSIGNAL) != (ssize_t)receipt_length) {
        return -1;
    }
    return 0;
}

static int send_frame(int fd, const unsigned char frame[KEEL_CONTROL_FRAME_LEN])
{
    ssize_t sent;

    do {
        sent = send(fd, frame, KEEL_CONTROL_FRAME_LEN, MSG_NOSIGNAL);
    } while (sent < 0 && errno == EINTR);
    return sent == KEEL_CONTROL_FRAME_LEN ? 0 : -1;
}

static int wait_readable(int fd, int timeout_ms)
{
    struct pollfd descriptor;
    int ready;

    descriptor.fd = fd;
    descriptor.events = POLLIN;
    descriptor.revents = 0;
    do {
        ready = poll(&descriptor, 1, timeout_ms);
    } while (ready < 0 && errno == EINTR);
    if (ready <= 0) {
        return -1;
    }
    return (descriptor.revents & (POLLIN | POLLHUP | POLLERR)) != 0 ? 0 : -1;
}

static int connect_control(const char *socket_path)
{
    struct sockaddr_un address;
    int fd;

    if (strlen(socket_path) > KEEL_SUN_PATH_MAX) {
        return -1;
    }
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    memcpy(address.sun_path, socket_path, strlen(socket_path) + 1);
    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return -1;
    }
    if (connect(fd, (const struct sockaddr *)&address, sizeof(address)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static void print_receipt(const char *receipt, size_t length)
{
    if (length > 0) {
        (void)fwrite(receipt, 1, length, stdout);
    }
    (void)fputc('\n', stdout);
    (void)fflush(stdout);
}

/* A delayed client sees a closed endpoint or the original terminal receipt, never a new exec identity. */
static int print_retained_receipt(const char *dir)
{
    char receipt[KEEL_RECEIPT_MAX + 1];
    ssize_t length;
    int attempt;

    /*
     * The endpoint closes just before the leader is reaped and the receipt is renamed into place. A control that
     * loses that race waits briefly for the retained terminal receipt instead of reporting an unknown outcome.
     */
    for (attempt = 0; attempt < 100; attempt++) {
        length = keel_read_receipt(dir, receipt, sizeof(receipt));
        if (length >= 0) {
            print_receipt(receipt, (size_t)length);
            return 0;
        }
        usleep(20000);
    }
    fprintf(stderr, "keel-supervisor: terminal receipt is not available\n");
    return KEEL_EXIT_NO_RECEIPT;
}

/*
 * `cancel`: reject a wrong binding, then deliver one fixed frame. An accepted request joins the original cleanup
 * deadline; a terminal supervisor returns its original receipt. If the endpoint has closed, the retained receipt
 * is the terminal answer. Nothing here signals a process.
 */
int keel_control(const struct keel_control_request *request)
{
    char dir[PATH_MAX];
    char socket_path[PATH_MAX];
    char reservation_usage[KEEL_TOKEN_HEX_LEN + 1];
    char receipt[KEEL_RECEIPT_MAX + 1];
    unsigned char frame[KEEL_CONTROL_FRAME_LEN];
    unsigned char response;
    ssize_t length;
    int fd;

    if (request == NULL || !keel_valid_token(request->control_token) ||
        !keel_valid_token(request->usage_token)) {
        fprintf(stderr, "keel-supervisor: invalid control request\n");
        return KEEL_EXIT_INVALID_REQUEST;
    }
    if (keel_exec_dir(request->root, request->control_token, dir, sizeof(dir)) != 0 ||
        keel_path(socket_path, sizeof(socket_path), dir, KEEL_CONTROL_SOCKET_NAME) != 0) {
        fprintf(stderr, "keel-supervisor: invalid control path\n");
        return KEEL_EXIT_INVALID_REQUEST;
    }
    if (keel_reservation_usage(dir, reservation_usage, sizeof(reservation_usage)) != 0) {
        fprintf(stderr, "keel-supervisor: exec identity is not reserved\n");
        return KEEL_EXIT_NO_RECEIPT;
    }
    if (strcmp(reservation_usage, request->usage_token) != 0) {
        fprintf(stderr, "keel-supervisor: control binding rejected\n");
        return KEEL_EXIT_CONTROL_REJECTED;
    }

    length = keel_read_receipt(dir, receipt, sizeof(receipt));
    if (length >= 0) {
        print_receipt(receipt, (size_t)length);
        return 0;
    }

    if (keel_encode_control(request->control_token, request->usage_token, 1, frame) != 0) {
        fprintf(stderr, "keel-supervisor: invalid control request\n");
        return KEEL_EXIT_INVALID_REQUEST;
    }
    fd = connect_control(socket_path);
    if (fd < 0 || send_frame(fd, frame) != 0 || wait_readable(fd, KEEL_CLIENT_TIMEOUT_MS) != 0) {
        if (fd >= 0) {
            close(fd);
        }
        return print_retained_receipt(dir);
    }
    do {
        length = recv(fd, &response, 1, 0);
    } while (length < 0 && errno == EINTR);
    if (length == 1 && response == KEEL_CONTROL_REJECTED) {
        close(fd);
        fprintf(stderr, "keel-supervisor: control binding rejected\n");
        return KEEL_EXIT_CONTROL_REJECTED;
    }
    if (length != 1 || (response != KEEL_CONTROL_ACCEPTED && response != KEEL_CONTROL_TERMINAL)) {
        close(fd);
        return print_retained_receipt(dir);
    }
    if (response == KEEL_CONTROL_TERMINAL) {
        if (wait_readable(fd, KEEL_CLIENT_TIMEOUT_MS) == 0) {
            do {
                length = recv(fd, receipt, KEEL_RECEIPT_MAX, 0);
            } while (length < 0 && errno == EINTR);
            close(fd);
            if (length > 0) {
                print_receipt(receipt, (size_t)length);
                return 0;
            }
        } else {
            close(fd);
        }
        return print_retained_receipt(dir);
    }
    close(fd);
    return 0;
}
