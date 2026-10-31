/*
 * The serialized supervisor state machine.
 *
 * One thread owns SIGCHLD/SIGTERM/SIGINT (blocked and read through signalfd), the TERM grace and cleanup deadlines,
 * KILL, control admission and reaping. PR_SET_CHILD_SUBREAPER is enabled and read back before the payload fork, so
 * orphaned descendants are adopted by this supervisor. The leader is recorded at fork and observed with
 * waitid(P_PID, ..., WEXITED | WNOWAIT) so it stays unreaped while signaling is possible; killpg is only issued
 * while this supervisor still owns the original leader.
 *
 * No child is reaped while signal authority remains open: adopted children may stay zombies during cleanup. After
 * the final TERM/grace/KILL attempt and a confirmed leader exit, control authority closes, the endpoint is
 * invalidated and every timer that could issue a signal is cancelled; only then is the leader reaped. A
 * nonblocking waitpid(-1) then drains adopted children. Zero means children remain and is not success; only ECHILD
 * proves descendant completion. The fixed cleanup deadline is never extended by duplicate controls.
 *
 * Once the leader has exited, the TERM grace ends early when its pinned group has no live member left: the final
 * KILL is then sent at once instead of after the whole grace. That check reads /proc and is advisory only. It never
 * chooses whom to signal (the target is always the pinned group) and never proves completion (ECHILD does).
 */
#include "supervisor.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define KEEL_POLL_INTERVAL_MS 500
/* How often a group member that is handling TERM is looked for again; its exit need not wake the loop. */
#define KEEL_GROUP_POLL_MS 20
/*
 * A connected control client must send its fixed frame promptly. Without this idle deadline a silent same-uid
 * peer could occupy the single client slot indefinitely and block a legitimate cancellation.
 */
#define KEEL_CLIENT_IDLE_SECONDS 0.25

struct keel_supervisor {
    const struct keel_request *request;
    char dir[PATH_MAX];
    char socket_path[PATH_MAX];
    int listen_fd;
    int signal_fd;
    int client_fd;
    int handshake_pipe[2];
    int handshake_fd;
    pid_t leader;
    int subreaper_failed;
    int handshake_ok;
    int setup_failed;
    int group_unverified;
    int signal_failed;
    int leader_exit_observed;
    int leader_has_status;
    int leader_status;
    int leader_reaped;
    int reap_failed;
    int cleanup_started;
    int kill_sent;
    int cancel_requested;
    int timers_cancelled;
    int control_closed;
    int awaiting_descendants;
    int receipt_failed;
    int terminal;
    mode_t payload_umask;
    double grace_seconds;
    double reap_seconds;
    struct timespec grace_deadline;
    struct timespec reap_deadline;
    struct timespec client_deadline;
    enum keel_state state;
    int exit_code;
};

const char *keel_state_name(enum keel_state state)
{
    switch (state) {
    case KEEL_STATE_RESERVED:
        return "reserved";
    case KEEL_STATE_CHILD_SPAWNED:
        return "child_spawned";
    case KEEL_STATE_PAYLOAD_RUNNING:
        return "payload_running";
    case KEEL_STATE_CLEANUP:
        return "cleanup";
    case KEEL_STATE_KILLED:
        return "killed";
    case KEEL_STATE_CONTROL_CLOSED:
        return "control_closed";
    case KEEL_STATE_LEADER_REAPED:
        return "leader_reaped";
    case KEEL_STATE_AWAITING_DESCENDANTS:
        return "awaiting_descendants";
    case KEEL_STATE_TERMINAL:
        return "terminal";
    }
    return "unknown";
}

static struct timespec monotonic_now(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        now.tv_sec = 0;
        now.tv_nsec = 0;
    }
    return now;
}

static struct timespec deadline_after(struct timespec base, double seconds)
{
    long long nanoseconds = (long long)(seconds * 1000000000.0);

    base.tv_sec += (time_t)(nanoseconds / 1000000000LL);
    base.tv_nsec += (long)(nanoseconds % 1000000000LL);
    if (base.tv_nsec >= 1000000000L) {
        base.tv_sec += 1;
        base.tv_nsec -= 1000000000L;
    }
    return base;
}

static int deadline_reached(struct timespec now, struct timespec deadline)
{
    if (now.tv_sec != deadline.tv_sec) {
        return now.tv_sec > deadline.tv_sec;
    }
    return now.tv_nsec >= deadline.tv_nsec;
}

static int deadline_before(struct timespec left, struct timespec right)
{
    if (left.tv_sec != right.tv_sec) {
        return left.tv_sec < right.tv_sec;
    }
    return left.tv_nsec < right.tv_nsec;
}

static void close_fd(int *fd)
{
    int saved;

    if (*fd < 0) {
        return;
    }
    saved = errno;
    (void)close(*fd);
    *fd = -1;
    errno = saved;
}

static int signals_allowed(const struct keel_supervisor *supervisor)
{
    return supervisor->handshake_ok && !supervisor->setup_failed && !supervisor->group_unverified &&
           supervisor->leader > 0 && !supervisor->leader_reaped;
}

/*
 * Whether a process other than a zombie may still belong to the process group: any doubt (no /proc, an entry that
 * cannot be parsed) answers yes, so the caller keeps waiting out the grace exactly as before.
 */
static int group_may_have_live_members(pid_t group)
{
    DIR *proc;
    struct dirent *entry;
    int live = 0;

    proc = opendir("/proc");
    if (proc == NULL) {
        return 1;
    }
    errno = 0;
    while (!live && (entry = readdir(proc)) != NULL) {
        char path[64];
        char record[512];
        const char *cursor;
        const char *name = entry->d_name;
        char state;
        int parent;
        int member_group;
        ssize_t length;
        int fd;

        if (!isdigit((unsigned char)name[0])) {
            continue;
        }
        if (snprintf(path, sizeof(path), "/proc/%s/stat", name) >= (int)sizeof(path)) {
            live = 1;
            break;
        }
        fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0) {
            live = errno != ENOENT && errno != ESRCH;
            continue;
        }
        length = read(fd, record, sizeof(record) - 1);
        close_fd(&fd);
        if (length <= 0) {
            live = length < 0 && errno != ESRCH;
            continue;
        }
        record[length] = '\0';
        /* The command name may hold spaces and parentheses; the fields after it start at the last ')'. */
        cursor = strrchr(record, ')');
        if (cursor == NULL || sscanf(cursor + 1, " %c %d %d", &state, &parent, &member_group) != 3) {
            live = 1;
            break;
        }
        if (member_group == (int)group && state != 'Z' && state != 'X' && state != 'x') {
            live = 1;
        }
        errno = 0;
    }
    if (!live && errno != 0) {
        live = 1;
    }
    (void)closedir(proc);
    return live;
}

/* The leader has exited and its pinned group holds no live member, so waiting out the grace serves no one. */
static int group_drained(const struct keel_supervisor *supervisor)
{
    if (!supervisor->leader_exit_observed || group_may_have_live_members(supervisor->leader)) {
        return 0;
    }
    keel_test_trace("group_drained", NULL, (long)supervisor->leader, !supervisor->leader_reaped);
    return 1;
}

static void begin_cleanup(struct keel_supervisor *supervisor, const char *reason);
static void observe_leader(struct keel_supervisor *supervisor);

static int write_code(int fd, char code)
{
    ssize_t written;

    do {
        written = write(fd, &code, 1);
    } while (written < 0 && errno == EINTR);
    return written == 1 ? 0 : -1;
}

/* The child establishes its own session/group, confirms it over a close-on-exec pipe, then execs the payload. */
static void child_exec(struct keel_supervisor *supervisor)
{
    sigset_t empty;

    close_fd(&supervisor->signal_fd);
    close_fd(&supervisor->listen_fd);
    close_fd(&supervisor->client_fd);
    close_fd(&supervisor->handshake_pipe[0]);
    (void)umask(supervisor->payload_umask);
    sigemptyset(&empty);
    (void)sigprocmask(SIG_SETMASK, &empty, NULL);

    if (setsid() < 0) {
        (void)write_code(supervisor->handshake_pipe[1], 'E');
        _exit(127);
    }
    keel_test_barrier("handshake_before");
    if (write_code(supervisor->handshake_pipe[1], 'S') != 0) {
        _exit(127);
    }
    keel_test_trace("payload_start", "payload", (long)getpid(), 1);
    execvp(supervisor->request->argv[0], supervisor->request->argv);
    (void)write_code(supervisor->handshake_pipe[1], 'E');
    _exit(127);
}

static int reset_sigchld(void)
{
    struct sigaction action;

    memset(&action, 0, sizeof(action));
    action.sa_handler = SIG_DFL;
    sigemptyset(&action.sa_mask);
    return sigaction(SIGCHLD, &action, NULL);
}

/*
 * Subreaper ownership is what lets this supervisor adopt and reap orphaned descendants, including double-forked
 * processes. Setting it is not enough: the value is read back, and any failure refuses to launch the payload.
 */
static int enable_subreaper(void)
{
    int value = 0;

    if (keel_test_subreaper_failed()) {
        errno = ENOTSUP;
        return -1;
    }
    if (prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0) {
        return -1;
    }
    if (prctl(PR_GET_CHILD_SUBREAPER, &value, 0, 0, 0) != 0 || value != 1) {
        errno = ENOTSUP;
        return -1;
    }
    return 0;
}

static int install_signal_fd(struct keel_supervisor *supervisor)
{
    sigset_t mask;

    sigemptyset(&mask);
    sigaddset(&mask, SIGCHLD);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    if (sigprocmask(SIG_BLOCK, &mask, NULL) != 0) {
        return -1;
    }
    supervisor->signal_fd = signalfd(-1, &mask, SFD_CLOEXEC | SFD_NONBLOCK);
    return supervisor->signal_fd < 0 ? -1 : 0;
}

/*
 * Close signal authority: invalidate the endpoint, drop queued control clients and stop every timer that could
 * issue another signal. Called before reaping the leader and before any terminal receipt.
 */
static void close_control_authority(struct keel_supervisor *supervisor)
{
    if (supervisor->control_closed) {
        return;
    }
    supervisor->timers_cancelled = 1;
    supervisor->state = KEEL_STATE_CONTROL_CLOSED;
    close_fd(&supervisor->client_fd);
    close_fd(&supervisor->listen_fd);
    if (supervisor->socket_path[0] != '\0') {
        (void)unlink(supervisor->socket_path);
    }
    close_fd(&supervisor->handshake_fd);
    supervisor->control_closed = 1;
    keel_test_trace("control_closed", NULL, 0, supervisor->leader > 0 && !supervisor->leader_reaped);
}

static void write_terminal_receipt(struct keel_supervisor *supervisor, const char *reason,
                                   int descendants_complete)
{
    struct keel_receipt receipt;

    memset(&receipt, 0, sizeof(receipt));
    receipt.exec_id = supervisor->request->exec_id;
    receipt.control_token = supervisor->request->control_token;
    receipt.has_status = supervisor->leader_has_status;
    receipt.leader_status = supervisor->leader_status;
    receipt.control_closed = 1;
    receipt.descendants_complete = descendants_complete ? 1 : 0;
    receipt.uncertain_reason = reason;

    if (keel_write_receipt(supervisor->dir, &receipt) != 0) {
        fprintf(stderr, "keel-supervisor: cannot write terminal receipt\n");
        supervisor->receipt_failed = 1;
    }
    supervisor->state = KEEL_STATE_TERMINAL;
    supervisor->terminal = 1;
    keel_test_trace("receipt", reason, receipt.has_status ? receipt.leader_status : -1,
                    descendants_complete);
    keel_test_trace(supervisor->receipt_failed ? "receipt_failed" : "receipt_written", reason,
                    descendants_complete ? 1 : 0, 0);
}

/* Reap the pinned leader only after control authority and all timers are closed. */
static void reap_leader(struct keel_supervisor *supervisor)
{
    int status = 0;
    pid_t reaped;

    if (supervisor->leader <= 0 || supervisor->leader_reaped) {
        return;
    }
    do {
        reaped = waitpid(supervisor->leader, &status, 0);
    } while (reaped < 0 && errno == EINTR);
    if (reaped != supervisor->leader) {
        supervisor->reap_failed = 1;
        keel_test_trace("reap_failed", NULL, 0, 0);
        return;
    }
    supervisor->leader_reaped = 1;
    supervisor->state = KEEL_STATE_LEADER_REAPED;
    if (WIFEXITED(status)) {
        supervisor->leader_status = WEXITSTATUS(status);
        supervisor->leader_has_status = 1;
    } else if (WIFSIGNALED(status)) {
        supervisor->leader_status = KEEL_EXIT_SIGNAL_BASE + WTERMSIG(status);
        supervisor->leader_has_status = 1;
    }
    keel_test_trace("leader_reaped", NULL,
                    supervisor->leader_has_status ? supervisor->leader_status : -1, 0);
}

/*
 * Close authority and persist an uncertain receipt. Uncertainty is never success, even when the leader status is
 * known; callers must inspect the receipt. A setup failure keeps its own exit code.
 */
static void finish_uncertain(struct keel_supervisor *supervisor, const char *reason)
{
    close_control_authority(supervisor);
    write_terminal_receipt(supervisor, reason, 0);
    supervisor->exit_code =
        supervisor->setup_failed ? KEEL_EXIT_SETUP_FAILURE : KEEL_EXIT_UNKNOWN_LEADER;
}

static void finish_after_echild(struct keel_supervisor *supervisor)
{
    const char *reason = NULL;

    if (supervisor->setup_failed) {
        reason = "setup_failed";
    } else if (supervisor->group_unverified) {
        reason = "group_unverified";
    } else if (supervisor->signal_failed) {
        reason = "signal_failed";
    }
    keel_test_trace("echild", reason, 0, 0);
    if (reason != NULL) {
        write_terminal_receipt(supervisor, reason, 0);
        supervisor->exit_code =
            supervisor->setup_failed ? KEEL_EXIT_SETUP_FAILURE : KEEL_EXIT_UNKNOWN_LEADER;
        return;
    }
    write_terminal_receipt(supervisor, NULL, 1);
    if (supervisor->receipt_failed || !supervisor->leader_has_status) {
        supervisor->exit_code = KEEL_EXIT_UNKNOWN_LEADER;
    } else {
        supervisor->exit_code = supervisor->leader_status;
    }
}

/*
 * Descendant draining starts only after the leader has been reaped, so this wait can never consume the pinned
 * leader. A zero return means children remain: it is not success, and the state machine waits again until the
 * fixed cleanup deadline. Only ECHILD proves that this supervisor has no remaining payload descendants.
 */
static void drain_descendants(struct keel_supervisor *supervisor)
{
    int status = 0;
    pid_t reaped;

    for (;;) {
        if (keel_test_wait_error()) {
            keel_test_trace("wait_failed", "injected", 0, 0);
            finish_uncertain(supervisor, "wait_failed");
            return;
        }
        do {
            reaped = waitpid(-1, &status, WNOHANG);
        } while (reaped < 0 && errno == EINTR);
        if (reaped > 0) {
            keel_test_trace("descendant_reaped", NULL, (long)reaped, 0);
            continue;
        }
        if (reaped == 0) {
            return; /* children remain; handle_timers waits again within the fixed deadline */
        }
        if (errno == ECHILD) {
            finish_after_echild(supervisor);
            return;
        }
        keel_test_trace("wait_failed", "error", (long)errno, 0);
        finish_uncertain(supervisor, "wait_failed");
        return;
    }
}

/*
 * The leader exited and the final TERM/grace/KILL attempt has run. Close signal authority first, then reap the
 * leader and prove descendant completion. No signal may be sent after this point.
 */
static void finish_normal(struct keel_supervisor *supervisor)
{
    close_control_authority(supervisor);
    reap_leader(supervisor);
    if (supervisor->reap_failed) {
        finish_uncertain(supervisor, "leader_reap_failed");
        return;
    }
    supervisor->awaiting_descendants = 1;
    supervisor->state = KEEL_STATE_AWAITING_DESCENDANTS;
    keel_test_barrier("after_leader_reaped");
    drain_descendants(supervisor);
}

/* The cleanup deadline expired; do not reopen signal authority or reap an unconfirmed leader. */
static void finish_timeout(struct keel_supervisor *supervisor, const char *reason)
{
    finish_uncertain(supervisor, reason);
}

static void observe_leader(struct keel_supervisor *supervisor)
{
    siginfo_t info;

    if (supervisor->leader <= 0 || supervisor->leader_exit_observed) {
        return;
    }
    memset(&info, 0, sizeof(info));
    if (waitid(P_PID, (id_t)supervisor->leader, &info, WEXITED | WNOWAIT | WNOHANG) != 0) {
        return;
    }
    if (info.si_pid != supervisor->leader) {
        return;
    }
    supervisor->leader_exit_observed = 1;
    if (info.si_code == CLD_EXITED) {
        supervisor->leader_status = info.si_status;
        supervisor->leader_has_status = 1;
    } else if (info.si_code == CLD_KILLED || info.si_code == CLD_DUMPED) {
        supervisor->leader_status = KEEL_EXIT_SIGNAL_BASE + info.si_status;
        supervisor->leader_has_status = 1;
    }
    keel_test_trace("leader_exit", NULL,
                    supervisor->leader_has_status ? supervisor->leader_status : -1, 1);
    if (!supervisor->setup_failed && !supervisor->cleanup_started && supervisor->handshake_ok) {
        begin_cleanup(supervisor, "natural");
    }
}

/*
 * Begin cleanup once the fresh group is confirmed. A cancel that arrived during setup is recorded and only applied
 * here; an unverified inherited group is never signalled.
 */
static void begin_cleanup(struct keel_supervisor *supervisor, const char *reason)
{
    (void)reason; /* only the test trace names the cleanup reason */
    if (supervisor->cleanup_started || supervisor->terminal) {
        return;
    }
    if (!supervisor->handshake_ok) {
        supervisor->cancel_requested = 1;
        return;
    }
    supervisor->cleanup_started = 1;
    supervisor->state = KEEL_STATE_CLEANUP;
    supervisor->grace_deadline = deadline_after(monotonic_now(), supervisor->grace_seconds);
    supervisor->reap_deadline = deadline_after(supervisor->grace_deadline, supervisor->reap_seconds);
    keel_test_trace("cleanup", reason, (long)supervisor->leader, 1);
    if (!signals_allowed(supervisor)) {
        return;
    }
    keel_test_barrier("term_before");
    keel_test_trace("term", "SIGTERM", (long)supervisor->leader, 1);
    if (killpg(supervisor->leader, SIGTERM) != 0 && errno != ESRCH) {
        supervisor->signal_failed = 1;
    }
}

static void begin_setup_failure(struct keel_supervisor *supervisor)
{
    if (supervisor->setup_failed || supervisor->terminal) {
        return;
    }
    supervisor->setup_failed = 1;
    supervisor->state = KEEL_STATE_CLEANUP;
    supervisor->reap_deadline =
        deadline_after(monotonic_now(), supervisor->grace_seconds + supervisor->reap_seconds);
    keel_test_trace("setup_failed", NULL, 0, 1);
}

static void handle_handshake(struct keel_supervisor *supervisor)
{
    char buffer[16];
    ssize_t length;
    ssize_t index;

    if (supervisor->handshake_fd < 0) {
        return;
    }
    do {
        length = read(supervisor->handshake_fd, buffer, sizeof(buffer));
    } while (length < 0 && errno == EINTR);
    if (length > 0) {
        for (index = 0; index < length; index++) {
            if (buffer[index] == 'S' && !supervisor->handshake_ok && !supervisor->setup_failed) {
                supervisor->handshake_ok = 1;
                supervisor->state = KEEL_STATE_PAYLOAD_RUNNING;
                if (getpgid(supervisor->leader) != supervisor->leader ||
                    getsid(supervisor->leader) != supervisor->leader) {
                    supervisor->group_unverified = 1;
                }
                keel_test_barrier("handshake_after");
                keel_test_trace("handshake", supervisor->group_unverified ? "unverified" : "verified",
                                (long)supervisor->leader, 1);
                if (supervisor->cancel_requested) {
                    begin_cleanup(supervisor, "cancel");
                } else if (supervisor->leader_exit_observed && !supervisor->cleanup_started) {
                    begin_cleanup(supervisor, "natural");
                }
            } else if (buffer[index] == 'E') {
                begin_setup_failure(supervisor);
            } else {
                begin_setup_failure(supervisor);
            }
        }
        return;
    }
    if (length == 0) {
        close_fd(&supervisor->handshake_fd);
        if (!supervisor->handshake_ok) {
            begin_setup_failure(supervisor);
        }
        return;
    }
    if (errno != EAGAIN && errno != EWOULDBLOCK) {
        close_fd(&supervisor->handshake_fd);
        if (!supervisor->handshake_ok) {
            begin_setup_failure(supervisor);
        }
    }
}

static void read_client(struct keel_supervisor *supervisor)
{
    unsigned char frame[KEEL_CONTROL_FRAME_LEN + 16];
    ssize_t length;
    int cancel = 0;

    if (supervisor->client_fd < 0) {
        return;
    }
    do {
        length = recv(supervisor->client_fd, frame, sizeof(frame), 0);
    } while (length < 0 && errno == EINTR);
    if (length < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        if (deadline_reached(monotonic_now(), supervisor->client_deadline)) {
            keel_test_trace("client_idle", "closed", 0,
                            supervisor->leader > 0 && !supervisor->leader_reaped);
            close_fd(&supervisor->client_fd);
        }
        return;
    }
    if (length <= 0) {
        close_fd(&supervisor->client_fd);
        return;
    }
    if (keel_decode_control(frame, (size_t)length, supervisor->request->control_token,
                            supervisor->request->usage_token, &cancel) != 0) {
        (void)keel_send_response(supervisor->client_fd, KEEL_CONTROL_REJECTED);
        keel_test_trace("control_rejected", "binding", 0,
                        supervisor->leader > 0 && !supervisor->leader_reaped);
        close_fd(&supervisor->client_fd);
        return;
    }
    if (cancel == 0) {
        (void)keel_send_response(supervisor->client_fd, KEEL_CONTROL_ACCEPTED);
        keel_test_trace("control", "status", 0, supervisor->leader > 0 && !supervisor->leader_reaped);
        close_fd(&supervisor->client_fd);
        return;
    }
    if (supervisor->cleanup_started) {
        /* Retries join the original cleanup deadline; they never restart it. */
        (void)keel_send_response(supervisor->client_fd, KEEL_CONTROL_ACCEPTED);
        keel_test_trace("control", "duplicate", 0, supervisor->leader > 0 && !supervisor->leader_reaped);
        close_fd(&supervisor->client_fd);
        return;
    }
    keel_test_trace("control", supervisor->handshake_ok ? "cancel" : "setup_cancel", 0,
                    supervisor->leader > 0 && !supervisor->leader_reaped);
    if (!supervisor->handshake_ok) {
        supervisor->cancel_requested = 1;
    }
    (void)keel_send_response(supervisor->client_fd, KEEL_CONTROL_ACCEPTED);
    close_fd(&supervisor->client_fd);
    begin_cleanup(supervisor, "cancel");
}

static void accept_client(struct keel_supervisor *supervisor)
{
    int fd;

    if (supervisor->listen_fd < 0 || supervisor->client_fd >= 0) {
        return;
    }
    do {
        fd = accept4(supervisor->listen_fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    } while (fd < 0 && errno == EINTR);
    if (fd >= 0) {
        supervisor->client_fd = fd;
        supervisor->client_deadline = deadline_after(monotonic_now(), KEEL_CLIENT_IDLE_SECONDS);
    }
}

static void handle_signals(struct keel_supervisor *supervisor)
{
    struct signalfd_siginfo info;
    ssize_t length;

    if (supervisor->signal_fd < 0) {
        return;
    }
    for (;;) {
        do {
            length = read(supervisor->signal_fd, &info, sizeof(info));
        } while (length < 0 && errno == EINTR);
        if (length != (ssize_t)sizeof(info)) {
            return;
        }
        if (info.ssi_signo == SIGCHLD) {
            observe_leader(supervisor);
        } else if (info.ssi_signo == SIGTERM || info.ssi_signo == SIGINT) {
            keel_test_trace("supervisor_signal", info.ssi_signo == SIGTERM ? "SIGTERM" : "SIGINT", 0,
                            supervisor->leader > 0 && !supervisor->leader_reaped);
            if (supervisor->handshake_ok) {
                begin_cleanup(supervisor, "supervisor_signal");
            } else {
                supervisor->cancel_requested = 1;
            }
        }
    }
}

static void handle_timers(struct keel_supervisor *supervisor)
{
    struct timespec now;

    if (supervisor->terminal) {
        return;
    }
    now = monotonic_now();
    if (supervisor->awaiting_descendants) {
        const char *reason;

        drain_descendants(supervisor);
        if (supervisor->terminal) {
            return;
        }
        if (!deadline_reached(now, supervisor->reap_deadline)) {
            return;
        }
        if (supervisor->setup_failed) {
            reason = "setup_failed";
        } else if (supervisor->group_unverified) {
            reason = "group_unverified";
        } else if (supervisor->signal_failed) {
            reason = "signal_failed";
        } else {
            reason = "descendants_unreaped";
        }
        keel_test_trace("deadline", reason, 0, 0);
        finish_uncertain(supervisor, reason);
        return;
    }
    if (supervisor->timers_cancelled) {
        return;
    }
    if (supervisor->setup_failed) {
        if (supervisor->leader_exit_observed) {
            finish_normal(supervisor);
        } else if (deadline_reached(now, supervisor->reap_deadline)) {
            finish_timeout(supervisor, "setup_timeout");
        }
        return;
    }
    if (!supervisor->cleanup_started) {
        return;
    }
    if (supervisor->leader_exit_observed && (!signals_allowed(supervisor) || supervisor->kill_sent)) {
        finish_normal(supervisor);
        return;
    }
    if (!supervisor->kill_sent && signals_allowed(supervisor) &&
        (deadline_reached(now, supervisor->grace_deadline) || group_drained(supervisor))) {
        keel_test_barrier("kill_before");
        /* The ownership invariant spans this syscall: the leader is unreaped, so its PGID cannot be reused. */
        keel_test_trace("kill", "SIGKILL", (long)supervisor->leader, !supervisor->leader_reaped);
        if (killpg(supervisor->leader, SIGKILL) != 0 && errno != ESRCH) {
            supervisor->signal_failed = 1;
        }
        supervisor->kill_sent = 1;
        supervisor->state = KEEL_STATE_KILLED;
        if (supervisor->leader_exit_observed) {
            finish_normal(supervisor);
        }
        return;
    }
    if (!supervisor->leader_exit_observed && deadline_reached(now, supervisor->reap_deadline)) {
        finish_timeout(supervisor, "cleanup_timeout");
    }
}

static int poll_timeout_ms(const struct keel_supervisor *supervisor)
{
    struct timespec now;
    struct timespec deadline;
    long long milliseconds;
    int have_deadline = 0;

    if (supervisor->terminal) {
        return -1;
    }
    if (supervisor->awaiting_descendants) {
        deadline = supervisor->reap_deadline;
        have_deadline = 1;
    } else if (!supervisor->timers_cancelled) {
        if (supervisor->setup_failed) {
            deadline = supervisor->reap_deadline;
            have_deadline = 1;
        } else if (supervisor->cleanup_started) {
            deadline = (!supervisor->kill_sent && signals_allowed(supervisor))
                           ? supervisor->grace_deadline
                           : supervisor->reap_deadline;
            have_deadline = 1;
        }
    }
    if (supervisor->client_fd >= 0 &&
        (!have_deadline || deadline_before(supervisor->client_deadline, deadline))) {
        deadline = supervisor->client_deadline;
        have_deadline = 1;
    }
    if (!have_deadline) {
        return -1;
    }
    now = monotonic_now();
    milliseconds = ((long long)deadline.tv_sec - (long long)now.tv_sec) * 1000LL +
                   ((long long)deadline.tv_nsec - (long long)now.tv_nsec) / 1000000LL;
    if (milliseconds < 0) {
        milliseconds = 0;
    }
    if (milliseconds > KEEL_POLL_INTERVAL_MS) {
        milliseconds = KEEL_POLL_INTERVAL_MS;
    }
    if (supervisor->leader_exit_observed && supervisor->cleanup_started && !supervisor->kill_sent &&
        !supervisor->timers_cancelled && signals_allowed(supervisor) && milliseconds > KEEL_GROUP_POLL_MS) {
        milliseconds = KEEL_GROUP_POLL_MS;
    }
    return (int)milliseconds;
}

static void wait_for_events(struct keel_supervisor *supervisor)
{
    struct pollfd descriptors[4];
    nfds_t count = 0;
    int ready;

    descriptors[count].fd = supervisor->signal_fd;
    descriptors[count].events = POLLIN;
    descriptors[count].revents = 0;
    count++;
    if (supervisor->listen_fd >= 0 && supervisor->client_fd < 0) {
        /* Do not poll a queued connection while the single client slot is busy: it would spin the loop. */
        descriptors[count].fd = supervisor->listen_fd;
        descriptors[count].events = POLLIN;
        descriptors[count].revents = 0;
        count++;
    }
    if (supervisor->handshake_fd >= 0) {
        descriptors[count].fd = supervisor->handshake_fd;
        descriptors[count].events = POLLIN;
        descriptors[count].revents = 0;
        count++;
    }
    if (supervisor->client_fd >= 0) {
        descriptors[count].fd = supervisor->client_fd;
        descriptors[count].events = POLLIN;
        descriptors[count].revents = 0;
        count++;
    }
    do {
        ready = poll(descriptors, count, poll_timeout_ms(supervisor));
    } while (ready < 0 && errno == EINTR);
}

static void supervisor_loop(struct keel_supervisor *supervisor)
{
    while (!supervisor->terminal) {
        handle_signals(supervisor);
        if (supervisor->terminal) {
            break;
        }
        read_client(supervisor);
        accept_client(supervisor);
        handle_handshake(supervisor);
        observe_leader(supervisor);
        handle_timers(supervisor);
        if (supervisor->terminal) {
            break;
        }
        wait_for_events(supervisor);
    }
}

static int valid_request(const struct keel_request *request)
{
    return request != NULL && request->root != NULL && request->root[0] == '/' &&
           keel_valid_exec_id(request->exec_id) && keel_valid_token(request->control_token) &&
           keel_valid_token(request->usage_token) && isfinite(request->grace_seconds) &&
           isfinite(request->reap_seconds) && request->grace_seconds >= 0.0 &&
           request->grace_seconds <= KEEL_MAX_SECONDS && request->reap_seconds >= 0.0 &&
           request->reap_seconds <= KEEL_MAX_SECONDS && request->argv != NULL &&
           request->argv[0] != NULL && request->argv[0][0] != '\0';
}

int keel_supervise(const struct keel_request *request)
{
    struct keel_supervisor supervisor;
    int reserved;

    memset(&supervisor, 0, sizeof(supervisor));
    supervisor.request = request;
    supervisor.listen_fd = -1;
    supervisor.signal_fd = -1;
    supervisor.client_fd = -1;
    supervisor.handshake_fd = -1;
    supervisor.handshake_pipe[0] = -1;
    supervisor.handshake_pipe[1] = -1;
    supervisor.leader = -1;
    supervisor.state = KEEL_STATE_RESERVED;
    supervisor.exit_code = KEEL_EXIT_SETUP_FAILURE;
    if (!valid_request(request)) {
        fprintf(stderr, "keel-supervisor: invalid run request\n");
        return KEEL_EXIT_INVALID_REQUEST;
    }
    supervisor.grace_seconds = request->grace_seconds;
    supervisor.reap_seconds = request->reap_seconds;
    supervisor.payload_umask = umask(0077);

    if (keel_ensure_exec_dir(request->root, request->control_token, supervisor.dir, sizeof(supervisor.dir),
                             supervisor.socket_path, sizeof(supervisor.socket_path)) != 0) {
        fprintf(stderr, "keel-supervisor: cannot create the exec directory\n");
        return KEEL_EXIT_SETUP_FAILURE;
    }
    reserved = keel_reserve_identity(supervisor.dir, request->exec_id, request->control_token,
                                     request->usage_token);
    if (reserved == 1) {
        keel_test_trace("duplicate", NULL, 0, 0);
        fprintf(stderr, "keel-supervisor: exec identity is already reserved\n");
        return KEEL_EXIT_DUPLICATE;
    }
    if (reserved != 0) {
        fprintf(stderr, "keel-supervisor: cannot reserve the exec identity\n");
        return KEEL_EXIT_SETUP_FAILURE;
    }
    keel_test_trace("reservation", NULL, 0, 0);

    supervisor.listen_fd = keel_bind_control_socket(supervisor.socket_path);
    if (supervisor.listen_fd < 0) {
        fprintf(stderr, "keel-supervisor: cannot bind the control endpoint\n");
        goto setup_failure;
    }
    if (reset_sigchld() != 0 || install_signal_fd(&supervisor) != 0) {
        fprintf(stderr, "keel-supervisor: cannot install the signal state\n");
        goto setup_failure;
    }
    if (enable_subreaper() != 0) {
        supervisor.subreaper_failed = 1;
        keel_test_trace("subreaper_failed", NULL, 0, 0);
        fprintf(stderr, "keel-supervisor: cannot enable child subreaper tracking\n");
        goto setup_failure;
    }
    keel_test_trace("subreaper", "verified", 1, 0);
    if (pipe2(supervisor.handshake_pipe, O_CLOEXEC | O_NONBLOCK) != 0) {
        fprintf(stderr, "keel-supervisor: cannot create the handshake pipe\n");
        goto setup_failure;
    }
    supervisor.leader = fork();
    if (supervisor.leader < 0) {
        supervisor.leader = -1;
        fprintf(stderr, "keel-supervisor: cannot fork the payload\n");
        goto setup_failure;
    }
    if (supervisor.leader == 0) {
        child_exec(&supervisor);
    }
    close_fd(&supervisor.handshake_pipe[1]);
    supervisor.handshake_fd = supervisor.handshake_pipe[0];
    supervisor.handshake_pipe[0] = -1;
    supervisor.state = KEEL_STATE_CHILD_SPAWNED;
    keel_test_trace("fork", "payload", (long)supervisor.leader, 1);

    supervisor_loop(&supervisor);
    return supervisor.exit_code;

setup_failure:
    close_control_authority(&supervisor);
    supervisor.leader_has_status = 0;
    write_terminal_receipt(&supervisor,
                           supervisor.subreaper_failed ? "subreaper_unavailable" : "setup_failed", 0);
    return KEEL_EXIT_SETUP_FAILURE;
}
