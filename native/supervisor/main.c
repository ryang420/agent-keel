/*
 * CLI entry point. Only fixed diagnostics are printed; payload argv, environment values and tokens are never
 * embedded in messages or receipts.
 */
#include "supervisor.h"

#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int usage(void)
{
    fprintf(stderr,
            "usage: keel-supervisor run --root DIR --exec-id ID --control-token HEX32 --usage-token HEX32 "
            "--grace SECONDS --reap SECONDS -- ARGV...\n"
            "       keel-supervisor cancel --root DIR --control-token HEX32 --usage-token HEX32\n"
            "       keel-supervisor receipt --root DIR --control-token HEX32\n");
    return KEEL_EXIT_INVALID_REQUEST;
}

static int take_value(int argc, char **argv, int *index, const char *option, const char **value)
{
    if (*index + 1 >= argc || strcmp(argv[*index], option) != 0) {
        return -1;
    }
    *value = argv[++(*index)];
    return 0;
}

static int parse_seconds(const char *text, double *out)
{
    char *end = NULL;
    double value;

    if (text == NULL || *text == '\0') {
        return -1;
    }
    errno = 0;
    value = strtod(text, &end);
    if (errno != 0 || end == NULL || *end != '\0' || !isfinite(value) || value < 0.0 ||
        value > KEEL_MAX_SECONDS) {
        return -1;
    }
    *out = value;
    return 0;
}

static int run_command(int argc, char **argv)
{
    struct keel_request request;
    const char *root = NULL;
    const char *exec_id = NULL;
    const char *control_token = NULL;
    const char *usage_token = NULL;
    const char *grace_text = NULL;
    const char *reap_text = NULL;
    int index;

    memset(&request, 0, sizeof(request));
    for (index = 2; index < argc; index++) {
        if (strcmp(argv[index], "--") == 0) {
            index++;
            break;
        }
        if (take_value(argc, argv, &index, "--root", &root) != 0 &&
            take_value(argc, argv, &index, "--exec-id", &exec_id) != 0 &&
            take_value(argc, argv, &index, "--control-token", &control_token) != 0 &&
            take_value(argc, argv, &index, "--usage-token", &usage_token) != 0 &&
            take_value(argc, argv, &index, "--grace", &grace_text) != 0 &&
            take_value(argc, argv, &index, "--reap", &reap_text) != 0) {
            return usage();
        }
    }
    if (index >= argc || root == NULL || !keel_valid_exec_id(exec_id) ||
        !keel_valid_token(control_token) || !keel_valid_token(usage_token) ||
        parse_seconds(grace_text, &request.grace_seconds) != 0 ||
        parse_seconds(reap_text, &request.reap_seconds) != 0) {
        return usage();
    }
    request.root = root;
    request.exec_id = exec_id;
    request.control_token = control_token;
    request.usage_token = usage_token;
    request.argv = &argv[index];
    return keel_supervise(&request);
}

static int cancel_command(int argc, char **argv)
{
    struct keel_control_request request;
    int index;

    memset(&request, 0, sizeof(request));
    for (index = 2; index < argc; index++) {
        if (take_value(argc, argv, &index, "--root", &request.root) != 0 &&
            take_value(argc, argv, &index, "--control-token", &request.control_token) != 0 &&
            take_value(argc, argv, &index, "--usage-token", &request.usage_token) != 0) {
            return usage();
        }
    }
    if (request.root == NULL || !keel_valid_token(request.control_token) ||
        !keel_valid_token(request.usage_token)) {
        return usage();
    }
    return keel_control(&request);
}

static int receipt_command(int argc, char **argv)
{
    struct keel_receipt_request request;
    int index;

    memset(&request, 0, sizeof(request));
    for (index = 2; index < argc; index++) {
        if (take_value(argc, argv, &index, "--root", &request.root) != 0 &&
            take_value(argc, argv, &index, "--control-token", &request.control_token) != 0) {
            return usage();
        }
    }
    if (request.root == NULL || !keel_valid_token(request.control_token)) {
        return usage();
    }
    return keel_receipt_command(&request);
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        return usage();
    }
    if (strcmp(argv[1], "run") == 0) {
        return run_command(argc, argv);
    }
    if (strcmp(argv[1], "cancel") == 0) {
        return cancel_command(argc, argv);
    }
    if (strcmp(argv[1], "receipt") == 0) {
        return receipt_command(argc, argv);
    }
    return usage();
}
