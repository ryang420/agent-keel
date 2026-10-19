/*
 * keel sandbox supervisor: pinned process identity, serialized cancellation and a terminal receipt.
 *
 * Linux/POSIX, C11, no dependencies. See
 * docs/design/2026-10-18-sandbox-lifecycle-coordination.md section 10.1.
 *
 * CLI contract (arguments are parsed by main.c; payload argv is never stored in control or receipt data):
 *
 *   keel-supervisor run --root <dir> --exec-id <id> --control-token <32hex> --usage-token <32hex>
 *                       --grace <seconds> --reap <seconds> -- <argv...>
 *   keel-supervisor cancel --root <dir> --control-token <32hex> --usage-token <32hex>
 *   keel-supervisor receipt --root <dir> --control-token <32hex>
 *
 * `run` verifies PR_SET_CHILD_SUBREAPER, then reserves <root>/<euid>/<control-token>/ with an exclusive-create
 * reservation before forking exactly one payload in a fresh session/process group. Clients exchange fixed
 * 37-byte frames over a private SOCK_SEQPACKET endpoint; they never send or signal numeric PIDs. The reservation
 * and terminal receipt are retained until the container is removed.
 *
 * The run exit status is the payload status only when cleanup is complete: the leader was reaped after control
 * authority closed and a nonblocking child wait returned ECHILD. Supervisor outcomes are exit codes 192-195 and
 * are documented below; a payload that exits with one of those codes is indistinguishable at the process-status
 * level, so the terminal receipt (presence, leader_status, control_closed, descendants_complete,
 * uncertain_reason) is the authoritative cleanup outcome. An uncertain receipt (descendants_complete=false) is
 * never a successful cleanup, even when the leader status is known.
 */
#ifndef KEEL_SUPERVISOR_H
#define KEEL_SUPERVISOR_H

#include <stddef.h>
#include <sys/types.h>

#define KEEL_PROTOCOL_VERSION 1

/* Fixed control frame: magic, raw control token, raw usage token, one cancel byte. */
#define KEEL_CONTROL_MAGIC "KEL1"
#define KEEL_CONTROL_MAGIC_LEN 4
#define KEEL_TOKEN_HEX_LEN 32
#define KEEL_TOKEN_BYTES 16
#define KEEL_CONTROL_FRAME_LEN (KEEL_CONTROL_MAGIC_LEN + (2 * KEEL_TOKEN_BYTES) + 1) /* 37 */

/* Bounded terminal receipt and identity limits. */
#define KEEL_RECEIPT_MAX 4096
#define KEEL_EXEC_ID_MAX 128
#define KEEL_REASON_MAX 512

/* Upper bound for --grace and --reap; every exported entry point validates it before doing time arithmetic. */
#define KEEL_MAX_SECONDS 86400.0

/* Per-exec directory contents. */
#define KEEL_CONTROL_SOCKET_NAME "control.sock"
#define KEEL_RESERVATION_NAME "reservation"
#define KEEL_RECEIPT_NAME "receipt.json"
#define KEEL_RECEIPT_TEMP_NAME "receipt.json.tmp"

/* sizeof(sockaddr_un.sun_path) - 1 on Linux; the endpoint path must stay below it. */
#define KEEL_SUN_PATH_MAX 107

/* Control socket response bytes: accepted or rejected. A terminal supervisor has already closed its endpoint and
 * clients read the retained receipt instead. */
#define KEEL_CONTROL_ACCEPTED 'A'
#define KEEL_CONTROL_REJECTED 'R'

/* `run` exit codes. Payload status is returned only with a complete descendant proof (see below). */
#define KEEL_EXIT_SETUP_FAILURE 192 /* no payload start or an unverifiable setup; receipt says why */
#define KEEL_EXIT_DUPLICATE 193     /* this exec identity is already reserved; nothing was forked */
#define KEEL_EXIT_INVALID_REQUEST 194
#define KEEL_EXIT_UNKNOWN_LEADER 195 /* the leader or descendant cleanup outcome is not proven */
#define KEEL_EXIT_SIGNAL_BASE 128
/* `cancel`: 0 accepted or terminal receipt printed, 4 rejected binding, 5 no reservation/endpoint/receipt. */
#define KEEL_EXIT_CONTROL_REJECTED 4
#define KEEL_EXIT_NO_RECEIPT 5

struct keel_request {
    const char *root;
    const char *exec_id;
    const char *control_token; /* exactly 32 lowercase hexadecimal characters */
    const char *usage_token;   /* exactly 32 lowercase hexadecimal characters */
    double grace_seconds;      /* TERM -> KILL grace, fixed when cleanup starts */
    double reap_seconds;       /* additional deadline after KILL / for descendant evidence */
    char *const *argv;         /* payload argv; inherited environment is untouched */
};

struct keel_control_request {
    const char *root;
    const char *control_token;
    const char *usage_token;
};

struct keel_receipt_request {
    const char *root;
    const char *control_token;
};

struct keel_receipt {
    const char *exec_id;
    const char *control_token;
    int has_status;
    int leader_status;
    int control_closed;
    int descendants_complete;
    const char *uncertain_reason; /* NULL is emitted as JSON null */
};

/* Serialized supervisor states. Exactly one path owns signals, timers and reaping. */
enum keel_state {
    KEEL_STATE_RESERVED = 0,
    KEEL_STATE_CHILD_SPAWNED,
    KEEL_STATE_PAYLOAD_RUNNING,
    KEEL_STATE_CLEANUP,     /* TERM sent; grace deadline fixed */
    KEEL_STATE_KILLED,      /* KILL sent; leader must still be pinned until reaped */
    KEEL_STATE_CONTROL_CLOSED,
    KEEL_STATE_LEADER_REAPED,
    KEEL_STATE_AWAITING_DESCENDANTS, /* leader reaped; waiting for the child wait to return ECHILD */
    KEEL_STATE_TERMINAL
};

const char *keel_state_name(enum keel_state state);

/* Entry points from the plan. */
int keel_supervise(const struct keel_request *request);
int keel_control(const struct keel_control_request *request);
int keel_receipt_command(const struct keel_receipt_request *request);

/* Shared validation and path helpers (control.c). */
int keel_valid_token(const char *token);
int keel_valid_exec_id(const char *exec_id);
int keel_exec_dir(const char *root, const char *control_token, char *dir, size_t size);
int keel_path(char *out, size_t size, const char *dir, const char *name);
int keel_ensure_exec_dir(const char *root, const char *control_token, char *dir, size_t dir_size,
                         char *socket_path, size_t socket_size);
int keel_reserve_identity(const char *dir, const char *exec_id, const char *control_token,
                          const char *usage_token);
int keel_reservation_usage(const char *dir, char *usage_out, size_t size);
int keel_bind_control_socket(const char *socket_path);
int keel_encode_control(const char *control_token, const char *usage_token, int cancel,
                        unsigned char out[KEEL_CONTROL_FRAME_LEN]);
int keel_decode_control(const unsigned char *frame, size_t length, const char *control_token,
                        const char *usage_token, int *cancel);
int keel_send_response(int fd, unsigned char response);

/* Terminal receipt encoding (receipt.c). */
int keel_build_receipt(const struct keel_receipt *receipt, char *out, size_t size);
int keel_write_receipt(const char *dir, const struct keel_receipt *receipt);
int keel_read_receipt(const char *dir, char *out, size_t size);

/*
 * Test-only hooks. They are declared only when the harness translation unit is built with -DKEEL_TEST_HOOKS and
 * are implemented exclusively in tests/native/supervisor_harness.c, so the production binary never references the
 * barrier or trace environment variables.
 */
#ifdef KEEL_TEST_HOOKS
void keel_test_trace(const char *kind, const char *detail, long value, int leader_pinned);
void keel_test_barrier(const char *name);
int keel_test_subreaper_failed(void);
int keel_test_wait_error(void);
#else
#define keel_test_trace(kind, detail, value, leader_pinned) ((void)0)
#define keel_test_barrier(name) ((void)0)
#define keel_test_subreaper_failed() 0
#define keel_test_wait_error() 0
#endif

#endif /* KEEL_SUPERVISOR_H */
