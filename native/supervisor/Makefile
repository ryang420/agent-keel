# Linux supervisor build. C11/POSIX, no dependencies.
#
#   make -C native/supervisor                 # production binary, no test hooks
#   make -C native/supervisor supervisor-harness   # test binary with tests/native/supervisor_harness.c
#
# Build outputs default to native/supervisor/.local (gitignored); override with BUILD=<dir>.

CC ?= cc
BUILD ?= .local
CFLAGS ?= -O2 -g
CPPFLAGS += -D_GNU_SOURCE -I.
WARNINGS = -std=c11 -Wall -Wextra -Werror
SOURCES = main.c process.c control.c receipt.c
HARNESS_SOURCE = ../../tests/native/supervisor_harness.c
PID_REUSE_SOURCE = ../../tests/native/pid_reuse.c

.PHONY: all harness supervisor-harness pid-reuse clean

all: $(BUILD)/keel-supervisor

harness: supervisor-harness

$(BUILD):
	mkdir -p $(BUILD)

# Production: no KEEL_TEST_HOOKS, therefore no barrier/trace code or environment reads.
$(BUILD)/keel-supervisor: $(SOURCES) supervisor.h | $(BUILD)
	$(CC) $(WARNINGS) $(CFLAGS) $(CPPFLAGS) -o $@ $(SOURCES)

# Test harness: identical sources plus the hooks translation unit.
$(BUILD)/supervisor-harness: $(SOURCES) $(HARNESS_SOURCE) supervisor.h | $(BUILD)
	$(CC) $(WARNINGS) $(CFLAGS) $(CPPFLAGS) -I../../tests/native -DKEEL_TEST_HOOKS -o $@ $(SOURCES) $(HARNESS_SOURCE)

supervisor-harness: $(BUILD)/supervisor-harness

# Linux-only PID-reuse proof; the Python test runs it in a private PID namespace and skips explicitly when the
# namespace or ns_last_pid is unavailable.
pid-reuse: $(BUILD)/pid-reuse

$(BUILD)/pid-reuse: $(PID_REUSE_SOURCE) | $(BUILD)
	$(CC) $(WARNINGS) $(CFLAGS) $(CPPFLAGS) -o $@ $(PID_REUSE_SOURCE)

clean:
	rm -rf $(BUILD)
